"""Discover, install, and self-provision MCP servers from public registries."""

from __future__ import annotations

import json
from typing import Any

from .base import Tool, ToolContext, ToolResult, prop
from .. import mcp_catalog as catalog


def _parse_obj(raw: Any) -> dict[str, str]:
    """Accept a JSON object, a JSON string, or KEY=VALUE lines."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if str(k).strip()}
    text = str(raw).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    out: dict[str, str] = {}
    for chunk in text.replace(",", "\n").splitlines():
        key, _, value = chunk.partition("=")
        if key.strip():
            out[key.strip()] = value.strip()
    return out


def _parse_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(a) for a in raw if str(a).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            return [str(a) for a in data]
    import shlex

    return shlex.split(text)


def _format_install(result: dict[str, Any]) -> str:
    if result.get("already_installed"):
        endpoint = result.get("url") or result.get("command") or ""
        return f"Already installed: {result.get('name')} ({endpoint})"
    lines = [
        f"Installed MCP server '{result.get('name')}' as {result.get('server_id')} "
        f"({result.get('transport', 'http')})."
    ]
    endpoint = result.get("url") or result.get("command")
    if endpoint:
        lines.append(f"Endpoint: {endpoint}")
    lines.append(f"Connected: {result.get('connected')}")
    lines.append(f"Imported tools: {result.get('tool_count', 0)}")
    tools = result.get("tools") or []
    if tools:
        names = ", ".join(str(t.get("proxy_name") or t.get("name")) for t in tools[:15])
        extra = f" (+{len(tools) - 15} more)" if len(tools) > 15 else ""
        lines.append(f"Call them directly: {names}{extra}")
    if result.get("missing_env"):
        lines.append(
            "Missing required settings: "
            + ", ".join(result["missing_env"])
            + " — ask for the value, then reinstall with env={\"KEY\": \"value\"}."
        )
    if result.get("error"):
        lines.append(f"Note: {result['error']}")
    return "\n".join(lines)


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    del ctx
    mode = (args.get("mode") or "search").strip().lower()

    if mode == "search":
        query = (args.get("query") or args.get("goal") or "").strip()
        limit = int(args.get("limit") or 12)
        try:
            items = await catalog.search_catalog(query, limit=min(max(limit, 1), 30))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, f"MCP catalog search failed: {exc}")
        if not items:
            return ToolResult(True, f"No MCP servers found for '{query or 'featured'}'.")
        items.sort(key=catalog.rank_key(catalog.goal_keywords(query)))
        lines = [f"MCP catalog results ({len(items)}), best-first:"]
        for item in items:
            flags = []
            if item.get("installed"):
                flags.append("installed")
            if item.get("requires_auth"):
                flags.append("auth required")
            spec = catalog.stdio_spec_from_packages(item.get("packages"))
            if not item.get("url"):
                flags.append("local only" if spec else "no remote URL")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(
                f"- {item.get('title') or item.get('name')} ({item.get('source')}){suffix}\n"
                f"  id: {item.get('id')}\n"
                f"  url: {item.get('url') or '—'}"
            )
            if spec:
                lines.append(f"  runs locally: {spec['command']} {' '.join(spec['args'])}")
                if spec.get("required_env"):
                    lines.append(f"  needs env: {', '.join(spec['required_env'])}")
            lines.append(f"  {str(item.get('description') or '')[:200]}")
        lines.append(
            "\nInstall with mode=install catalog_id=<id>, or skip the picking entirely "
            "with mode=ensure goal=<what you need>."
        )
        return ToolResult(True, "\n".join(lines))

    if mode == "ensure":
        goal = (args.get("goal") or args.get("query") or "").strip()
        if not goal:
            return ToolResult(False, "ensure mode requires goal (what capability you need).")
        try:
            result = await catalog.ensure_capability(
                goal,
                max_attempts=int(args.get("max_attempts") or 3),
                env=_parse_obj(args.get("env")),
                authorization=(args.get("authorization") or args.get("auth") or "").strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, f"MCP provisioning failed: {exc}")
        if result.get("already_available"):
            tools = result.get("tools") or []
            names = ", ".join(str(t.get("proxy_name") or t.get("name")) for t in tools[:15])
            return ToolResult(
                True,
                f"Already available via '{result.get('name')}' "
                f"({len(tools)} tool(s)). Call them now: {names}",
            )
        if not result.get("ok") and not result.get("connected"):
            detail = result.get("error") or "no candidate connected"
            attempts = result.get("attempts") or []
            lines = [f"Could not provision an MCP server for '{goal}': {detail}"]
            for att in attempts:
                lines.append(
                    f"  - tried {att.get('name') or att.get('id')}: "
                    + (att.get("error") or f"connected={att.get('connected')}")
                )
            for gated in result.get("needs_auth") or []:
                lines.append(
                    f"  - {gated.get('name')} (id {gated.get('id')}) needs credentials: "
                    f"{gated.get('auth_hint')}"
                )
            if result.get("needs_auth"):
                lines.append(
                    "Ask which of these to use and for the API key, then retry with "
                    "mode=install catalog_id=<id> authorization=<key>."
                )
            return ToolResult(False, "\n".join(lines))
        return ToolResult(True, f"Provisioned MCP support for '{goal}'.\n" + _format_install(result))

    if mode == "install":
        command = (args.get("command") or "").strip()
        entry_id = (args.get("catalog_id") or args.get("id") or "").strip()
        if command:
            try:
                result = await catalog.install_local_server(
                    (args.get("name") or command).strip(),
                    command,
                    _parse_list(args.get("args")),
                    env=_parse_obj(args.get("env")),
                    cwd=(args.get("cwd") or "").strip(),
                )
            except Exception as exc:  # noqa: BLE001
                return ToolResult(False, f"Local MCP install failed: {exc}")
            return ToolResult(True, _format_install(result))
        if not entry_id:
            return ToolResult(False, "install mode requires catalog_id, or command for a local server.")
        try:
            result = await catalog.install_catalog_entry(
                entry_id,
                url=(args.get("url") or "").strip() or None,
                authorization=(args.get("authorization") or args.get("auth") or "").strip() or None,
                name=(args.get("name") or "").strip() or None,
                env=_parse_obj(args.get("env")),
                prefer=(args.get("prefer") or "auto").strip().lower(),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, f"MCP install failed: {exc}")
        return ToolResult(True, _format_install(result))

    if mode in {"list", "installed"}:
        from ..mcp_client import get_manager

        statuses = get_manager().status_list()
        if not statuses:
            return ToolResult(True, "No MCP servers are installed. Use mode=ensure goal=<need>.")
        lines = [f"Installed MCP servers ({len(statuses)}):"]
        for st in statuses:
            state = "online" if st.connected else ("disabled" if not st.enabled else "offline")
            lines.append(f"- {st.name} [{state}] id={st.id} endpoint={st.url}")
            if st.error and not st.connected:
                lines.append(f"    error: {st.error}")
            for tool in st.tools[:20]:
                lines.append(
                    f"    • {tool.get('proxy_name')} ({tool.get('name')})"
                    + (f" — {str(tool.get('description'))[:80]}" if tool.get("description") else "")
                )
            if len(st.tools) > 20:
                lines.append(f"    • … +{len(st.tools) - 20} more")
        return ToolResult(True, "\n".join(lines))

    if mode == "refresh":
        from ..mcp_client import get_manager

        try:
            statuses = await get_manager().refresh()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, f"MCP refresh failed: {exc}")
        lines = [f"Refreshed {len(statuses)} configured MCP server(s):"]
        for st in statuses:
            mark = "ok" if st.connected else "error"
            lines.append(
                f"- {st.name}: {mark}, {len(st.tools)} tool(s)"
                + (f" — {st.error}" if st.error else "")
            )
        return ToolResult(True, "\n".join(lines))

    if mode in {"remove", "uninstall"}:
        target = (args.get("catalog_id") or args.get("id") or args.get("name") or "").strip()
        if not target:
            return ToolResult(False, "remove mode requires id or name of an installed server.")
        try:
            removed = await catalog.remove_server(target)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, f"MCP removal failed: {exc}")
        return ToolResult(True, f"Removed MCP server '{removed}'.")

    return ToolResult(
        False,
        "Unknown mode. Use: ensure, search, install, list, refresh, or remove.",
    )


mcp_discover = Tool(
    name="mcp_discover",
    description=(
        "Give yourself a capability you do not have yet, by provisioning MCP servers. "
        "mode=ensure goal=<what you need> is the autonomous path: it searches the public "
        "registries, picks the candidate most likely to work unattended, installs it "
        "(remote Streamable HTTP, or locally over stdio via npx/uvx/docker), connects it, "
        "and reports the tool names you can call immediately — no catalog ids needed. "
        "Use it the moment a request needs an integration JARVIS lacks, without asking "
        "permission first. "
        "mode=search: browse candidates by keyword. "
        "mode=install: add a specific catalog_id, or a local server with "
        "command/args/env. "
        "mode=list: show installed servers and their imported tool names. "
        "mode=refresh: reconnect configured servers. mode=remove: uninstall one."
    ),
    parameters={
        "mode": prop(
            "string",
            "ensure (provision a capability), search, install, list, refresh, or remove.",
            optional=True,
        ),
        "goal": prop(
            "string",
            "What you need to be able to do, e.g. 'control Spotify playback', "
            "'query a Postgres database', 'read Linear issues' (mode=ensure).",
            optional=True,
        ),
        "query": prop("string", "Search keywords (mode=search).", optional=True),
        "catalog_id": prop(
            "string",
            "Catalog entry id from search results, e.g. official:ai.example/foo "
            "(mode=install/remove).",
            optional=True,
        ),
        "url": prop("string", "Override MCP URL when installing.", optional=True),
        "command": prop(
            "string",
            "Executable for a local stdio MCP server, e.g. npx, uvx, docker (mode=install).",
            optional=True,
        ),
        "args": prop(
            "array",
            "Arguments for the local command, e.g. ['-y','@scope/server'].",
            optional=True,
            items={"type": "string"},
        ),
        "env": prop(
            "object",
            "Environment variables the server needs (API keys, paths).",
            optional=True,
        ),
        "cwd": prop("string", "Working directory for a local server.", optional=True),
        "authorization": prop(
            "string",
            "Authorization header value or Bearer token for a remote server.",
            optional=True,
        ),
        "name": prop("string", "Display name override.", optional=True),
        "prefer": prop(
            "string",
            "auto (default), remote, or local — how to install a catalog entry.",
            optional=True,
        ),
        "limit": prop("integer", "Max search results (default 12).", optional=True),
        "max_attempts": prop(
            "integer",
            "How many candidates to try in mode=ensure (default 3).",
            optional=True,
        ),
    },
    run=_run,
)
