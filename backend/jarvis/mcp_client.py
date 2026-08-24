"""Connect to external MCP servers and expose their tools to the JARVIS agent."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .config import McpServerEntry, store
from .tools.base import Tool, ToolContext, ToolResult, prop

log = logging.getLogger("jarvis.mcp_client")

_NAME_SAFE = re.compile(r"[^a-z0-9_]+")


@dataclass
class RemoteMcpTool:
    server_id: str
    server_name: str
    name: str
    description: str
    input_schema: dict[str, Any]
    destructive: bool = False


@dataclass
class McpServerStatus:
    id: str
    name: str
    url: str
    enabled: bool
    connected: bool = False
    error: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)


def _proxy_tool_name(server_id: str, tool_name: str) -> str:
    sid = _NAME_SAFE.sub("_", server_id.lower()).strip("_") or "mcp"
    tname = _NAME_SAFE.sub("_", tool_name.lower()).strip("_") or "tool"
    return f"mcp_{sid}_{tname}"[:64]


class _CmTransport:
    """Adapts an async-generator transport factory to the MCP ``Transport`` protocol."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self._cm: Any = None

    async def __aenter__(self) -> Any:
        self._cm = self._factory()
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc_info: Any) -> Any:
        cm, self._cm = self._cm, None
        if cm is None:
            return None
        return await cm.__aexit__(*exc_info)


def _transport_for(entry: McpServerEntry) -> Any:
    """Build the transport for a configured server: local subprocess or remote HTTP.

    Remote servers get their configured headers applied — an Authorization header is
    the difference between "connected" and "401" for most hosted MCP servers.
    """
    if entry.is_stdio:
        command = entry.command.strip()
        if not command:
            raise ValueError(f"MCP server {entry.name} has no command to run")
        params = StdioServerParameters(
            command=command,
            args=list(entry.args or []),
            env=dict(entry.env or {}) or None,
            cwd=entry.cwd.strip() or None,
        )
        return _CmTransport(lambda: stdio_client(params))

    url = entry.url.strip()
    if not url:
        raise ValueError(f"MCP server {entry.name} has no URL")
    headers = {k: v for k, v in (entry.headers or {}).items() if k and v}
    if not headers:
        return url  # Client() builds the default HTTP transport itself.
    return _CmTransport(
        lambda: streamable_http_client(url, http_client=create_mcp_http_client(headers=headers))
    )


def _format_exc(exc: BaseException) -> str:
    """Unwrap TaskGroup / ExceptionGroup errors from the MCP SDK."""
    if isinstance(exc, BaseExceptionGroup):
        parts = [_format_exc(e) for e in exc.exceptions]
        detail = "; ".join(p for p in parts if p)
        return detail or str(exc)
    return str(exc)


def _schema_to_parameters(input_schema: dict[str, Any]) -> dict[str, Any]:
    props = input_schema.get("properties") or {}
    required = set(input_schema.get("required") or [])
    out: dict[str, Any] = {}
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        ptype = spec.get("type", "string")
        if isinstance(ptype, list):
            ptype = next((t for t in ptype if t != "null"), "string")
        desc = str(spec.get("description") or "")
        optional = name not in required
        extra: dict[str, Any] = {}
        if ptype == "array" and isinstance(spec.get("items"), dict):
            extra["items"] = spec["items"]
        out[name] = prop(str(ptype), desc, optional=optional, **extra)
    return out


def _content_blocks_to_text(blocks: Any) -> str:
    if blocks is None:
        return ""
    if isinstance(blocks, str):
        return blocks
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        elif hasattr(block, "type") and getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "") or ""))
        elif hasattr(block, "model_dump"):
            dumped = block.model_dump()
            if dumped.get("type") == "text":
                parts.append(str(dumped.get("text") or ""))
            else:
                parts.append(json.dumps(dumped, ensure_ascii=False))
        else:
            parts.append(str(block))
    return "\n".join(p for p in parts if p).strip()


class McpClientManager:
    """Discover and call tools on configured remote MCP servers."""

    def __init__(self) -> None:
        self._remote: dict[str, list[RemoteMcpTool]] = {}
        self._proxy_tools: dict[str, Tool] = {}
        self._status: dict[str, McpServerStatus] = {}

    def reload(self) -> None:
        """Rebuild proxy tools from the current config (sync; clears cache)."""
        self._remote.clear()
        self._proxy_tools.clear()
        self._status.clear()
        for entry in store.get().mcp.servers:
            self._status[entry.id] = McpServerStatus(
                id=entry.id,
                name=entry.name,
                url=entry.target,
                enabled=entry.enabled,
            )

    async def refresh(self) -> list[McpServerStatus]:
        """Connect to each enabled server and cache its tool list."""
        self.reload()
        cfg = store.get().mcp
        for entry in cfg.servers:
            status = self._status[entry.id]
            if not entry.enabled:
                continue
            if not entry.url.strip() and not entry.command.strip():
                status.error = "A URL (remote) or command (local stdio) is required"
                continue
            try:
                tools = await self._discover(entry)
            except Exception as exc:  # noqa: BLE001
                status.connected = False
                status.error = _format_exc(exc)
                log.warning("MCP server %s (%s) failed: %s", entry.name, entry.target, status.error)
                continue
            status.connected = True
            status.error = ""
            status.tools = [
                {
                    "name": t.name,
                    "proxy_name": _proxy_tool_name(entry.id, t.name),
                    "description": t.description,
                    "destructive": t.destructive,
                }
                for t in tools
            ]
            self._remote[entry.id] = tools
            for remote in tools:
                proxy = self._build_proxy_tool(entry, remote)
                self._proxy_tools[proxy.name] = proxy
        return self.status_list()

    async def _discover(self, entry: McpServerEntry) -> list[RemoteMcpTool]:
        async with Client(_transport_for(entry), read_timeout_seconds=30.0) as client:
            result = await client.list_tools()
            tools = getattr(result, "tools", result) or []
            out: list[RemoteMcpTool] = []
            for item in tools:
                name = getattr(item, "name", None) or (item.get("name") if isinstance(item, dict) else "")
                if not name:
                    continue
                description = getattr(item, "description", None) or (
                    item.get("description") if isinstance(item, dict) else ""
                ) or ""
                schema = getattr(item, "inputSchema", None) or (
                    item.get("inputSchema") if isinstance(item, dict) else {}
                ) or {}
                annotations = getattr(item, "annotations", None) or (
                    item.get("annotations") if isinstance(item, dict) else None
                )
                destructive = bool(
                    getattr(annotations, "destructiveHint", False)
                    if annotations is not None and not isinstance(annotations, dict)
                    else (annotations or {}).get("destructiveHint")
                )
                out.append(
                    RemoteMcpTool(
                        server_id=entry.id,
                        server_name=entry.name,
                        name=str(name),
                        description=str(description),
                        input_schema=schema if isinstance(schema, dict) else {},
                        destructive=destructive,
                    )
                )
            return out

    def _build_proxy_tool(self, entry: McpServerEntry, remote: RemoteMcpTool) -> Tool:
        proxy_name = _proxy_tool_name(entry.id, remote.name)
        description = (
            f"[MCP · {entry.name}] {remote.description or remote.name}".strip()
        )
        parameters = _schema_to_parameters(remote.input_schema)

        async def _run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            del ctx
            try:
                text = await self.call_remote(entry.id, remote.name, args)
                return ToolResult(True, text)
            except Exception as exc:  # noqa: BLE001
                return ToolResult(False, f"MCP tool failed: {exc}")

        def _dangerous(_args: dict[str, Any]) -> bool:
            return remote.destructive

        return Tool(
            name=proxy_name,
            description=description,
            parameters=parameters or {"payload": prop("object", "Tool arguments.", optional=True)},
            run=_run,
            dangerous=_dangerous if remote.destructive else False,
            preview=lambda a, n=remote.name, s=entry.name: f"MCP {s}: {n}({a})",
        )

    async def call_remote(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        entry = self._find_entry(server_id)
        if entry is None:
            raise ValueError(f"Unknown MCP server: {server_id}")
        if not entry.enabled:
            raise ValueError(f"MCP server {entry.name} is disabled")
        async with Client(_transport_for(entry), read_timeout_seconds=120.0) as client:
            result = await client.call_tool(tool_name, arguments or {})
        if getattr(result, "isError", False):
            text = _content_blocks_to_text(getattr(result, "content", None))
            raise RuntimeError(text or "Remote MCP tool returned an error")
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False, indent=2)
        text = _content_blocks_to_text(getattr(result, "content", None))
        return text or "(empty result)"

    async def test_server(self, entry: McpServerEntry) -> McpServerStatus:
        status = McpServerStatus(
            id=entry.id,
            name=entry.name,
            url=entry.target,
            enabled=entry.enabled,
        )
        if not entry.url.strip() and not entry.command.strip():
            status.error = "A URL (remote) or command (local stdio) is required"
            return status
        try:
            tools = await self._discover(entry)
        except Exception as exc:  # noqa: BLE001
            status.error = _format_exc(exc)
            return status
        status.connected = True
        status.tools = [
            {
                "name": t.name,
                "proxy_name": _proxy_tool_name(entry.id, t.name),
                "description": t.description,
                "destructive": t.destructive,
            }
            for t in tools
        ]
        return status

    def proxy_tools(self) -> list[Tool]:
        return list(self._proxy_tools.values())

    def resolve_server(self, ref: str) -> McpServerEntry:
        """Match a configured server by id or display name (case-insensitive, partial ok)."""
        needle = ref.strip().lower()
        if not needle:
            raise ValueError("server is required")
        entries = [
            e for e in store.get().mcp.servers if e.url.strip() or e.command.strip()
        ]
        for entry in entries:
            if entry.id.lower() == needle or entry.name.lower() == needle:
                return entry
        partial = [
            e
            for e in entries
            if needle in e.name.lower() or needle in e.id.lower()
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            names = ", ".join(e.name for e in partial[:6])
            raise ValueError(f"Ambiguous MCP server '{ref}'. Matches: {names}")
        if entries:
            names = ", ".join(e.name for e in entries[:8])
            raise ValueError(f"Unknown MCP server '{ref}'. Installed: {names}")
        raise ValueError("No MCP servers are installed.")

    def resolve_remote_tool(self, server_id: str, ref: str) -> str:
        """Match a remote tool name on a server (exact, then partial)."""
        needle = ref.strip().lower()
        if not needle:
            raise ValueError("tool is required")
        tools = self._remote.get(server_id) or []
        if not tools:
            raise ValueError("No tools cached for that server — refresh MCP tools in Settings.")
        for remote in tools:
            if remote.name.lower() == needle:
                return remote.name
        proxy_needle = needle.replace("-", "_")
        for remote in tools:
            proxy = _proxy_tool_name(server_id, remote.name).lower()
            if proxy == needle or proxy.endswith(f"_{proxy_needle}"):
                return remote.name
        partial = [
            t
            for t in tools
            if needle in t.name.lower()
            or needle in _proxy_tool_name(server_id, t.name).lower()
        ]
        if len(partial) == 1:
            return partial[0].name
        if len(partial) > 1:
            names = ", ".join(t.name for t in partial[:8])
            raise ValueError(f"Ambiguous MCP tool '{ref}'. Matches: {names}")
        names = ", ".join(t.name for t in tools[:12])
        extra = f" (+{len(tools) - 12} more)" if len(tools) > 12 else ""
        raise ValueError(f"Unknown MCP tool '{ref}' on that server. Available: {names}{extra}")

    def list_remote_tools(self, server_id: str) -> list[RemoteMcpTool]:
        return list(self._remote.get(server_id) or [])

    def status_list(self) -> list[McpServerStatus]:
        return list(self._status.values())

    @staticmethod
    def _find_entry(server_id: str) -> McpServerEntry | None:
        for entry in store.get().mcp.servers:
            if entry.id == server_id:
                return entry
        return None


_manager = McpClientManager()


def get_manager() -> McpClientManager:
    return _manager


def mcp_endpoint_url() -> str:
    cfg = store.get()
    return f"http://{cfg.host}:{cfg.port}/mcp"


async def startup_refresh() -> None:
    if store.get().mcp.servers:
        try:
            await _manager.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("MCP startup refresh failed: %s", exc)


def format_mcp_context(*, max_tools_per_server: int = 24) -> str:
    """Compact summary of connected MCP servers and proxy tool names for the system prompt."""
    statuses = [s for s in _manager.status_list() if s.enabled]
    proxies = _manager.proxy_tools()
    if not statuses and not proxies:
        return ""

    lines = [
        "=== Imported MCP tools ===",
        (
            f"{len(proxies)} imported tool(s) from {len(statuses)} configured server(s). "
            "Call them directly by proxy name (mcp_<server>_<tool>) or via mcp_invoke "
            "(server=<name>, tool=<remote_tool_name>, arguments={{...}}). "
            "Never claim MCP access is unavailable."
        ),
    ]
    for st in statuses:
        state = "online" if st.connected else "offline"
        lines.append(f"\nServer: {st.name} [{state}]")
        lines.append(f"  Endpoint: {st.url}")
        if st.error and not st.connected:
            lines.append(f"  Error: {st.error}")
        tools = st.tools or []
        if tools:
            shown = tools[:max_tools_per_server]
            for t in shown:
                remote = str(t.get("name") or "")
                proxy = str(t.get("proxy_name") or "")
                desc = str(t.get("description") or remote).strip()
                if len(desc) > 100:
                    desc = desc[:97] + "…"
                label = f"{proxy} ({remote})" if proxy and proxy != remote else remote
                lines.append(f"  • {label}" + (f" — {desc}" if desc else ""))
            if len(tools) > max_tools_per_server:
                lines.append(f"  • … +{len(tools) - max_tools_per_server} more tool(s)")
        elif st.connected:
            lines.append("  (connected — tool list empty)")
    return "\n".join(lines)
