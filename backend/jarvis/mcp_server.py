"""MCP (Model Context Protocol) server exposing JARVIS tools, resources, and chat."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server import MCPServer
from mcp_types import ToolAnnotations

from .agent import Agent, RunControl
from .config import store
from .device import profile_summary
from .llm import LLMClient
from .memory import Memory
from .rem import DREAMS_MD, MEMORY_MD
from .tools import ToolContext, all_tools
from .tools.base import Tool

_TYPE_MAP = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


@dataclass
class McpDeps:
    memory: Memory
    tasks: Any
    rem_touch: Callable[[], None]
    begin_run: Callable[[], None]
    end_run: Callable[[], None]
    peripheral_items: Callable[[], list[dict[str, Any]]]


_deps: McpDeps | None = None
_server: MCPServer[None] | None = None
_http_app = None


def init_mcp(deps: McpDeps) -> MCPServer[None]:
    """Build and register the JARVIS MCP server (idempotent)."""
    global _deps, _server, _http_app
    _deps = deps
    if _server is not None:
        return _server

    server = MCPServer(
        name="JARVIS",
        title="JARVIS",
        description="Self-hosted butler-style AI agent with full local device control.",
        instructions=(
            "JARVIS is a local-first AI butler that controls your machine, browses the "
            "web, remembers facts, and manages background tasks. Prefer `jarvis_ask` for "
            "open-ended requests; call individual tools for direct actions. Irreversible "
            "operations (delete, overwrite, shutdown/reboot, send email) require JARVIS "
            "auto-approve or confirmation in the web UI."
        ),
        version="0.1.0",
    )

    for tool in all_tools():
        _register_jarvis_tool(server, tool)

    @server.tool(
        name="jarvis_ask",
        title="Ask JARVIS",
        description=(
            "Send a natural-language request to JARVIS and receive its full reply after "
            "planning and tool use. Use for complex tasks; prefer direct tools for a "
            "single known action."
        ),
    )
    async def jarvis_ask(message: str, conversation_id: int | None = None) -> str:
        return await _run_agent(message, conversation_id)

    @server.resource(
        "jarvis://memory",
        name="long_term_memory",
        title="Long-term memory",
        description="Consolidated facts from JARVIS REM sleep (MEMORY.md).",
        mime_type="text/markdown",
    )
    def read_memory_md() -> str:
        return _read_text_file(MEMORY_MD, "# JARVIS memory\n\n(No consolidated memory yet.)\n")

    @server.resource(
        "jarvis://dreams",
        name="dream_journal",
        title="Dream journal",
        description="Themes and reflections from JARVIS REM sleep (DREAMS.md).",
        mime_type="text/markdown",
    )
    def read_dreams_md() -> str:
        return _read_text_file(DREAMS_MD, "# JARVIS dreams\n\n(No dream journal yet.)\n")

    @server.resource(
        "jarvis://device",
        name="device_profile",
        title="Device profile",
        description="Learned host profile (OS, hardware, control utilities).",
        mime_type="application/json",
    )
    def read_device_profile() -> str:
        deps = _require_deps()
        devices = deps.memory.list_devices()
        if not devices:
            return json.dumps({"status": "not_learned"}, indent=2)
        profile = devices[0].get("profile") or {}
        if isinstance(profile, str):
            try:
                profile = json.loads(profile)
            except json.JSONDecodeError:
                profile = {"raw": profile}
        return json.dumps(
            {
                "hostname": profile.get("hostname"),
                "summary": profile_summary(profile),
                "profile": profile,
            },
            indent=2,
        )

    @server.resource(
        "jarvis://peripherals",
        name="peripherals",
        title="Peripheral inventory",
        description="USB, Bluetooth, audio, display, and network peripherals JARVIS knows about.",
        mime_type="application/json",
    )
    def read_peripherals() -> str:
        deps = _require_deps()
        items = deps.peripheral_items()
        return json.dumps(items, indent=2)

    @server.resource(
        "jarvis://tools",
        name="tool_catalog",
        title="Tool catalog",
        description="All JARVIS agent tools with JSON schemas.",
        mime_type="application/json",
    )
    def read_tool_catalog() -> str:
        catalog = []
        for tool in all_tools():
            schema = tool.schema()["function"]
            catalog.append(
                {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                    "dangerous": bool(
                        tool.dangerous is True or callable(tool.dangerous)
                    ),
                }
            )
        return json.dumps(catalog, indent=2)

    @server.resource(
        "jarvis://conversations/{conversation_id}",
        name="conversation",
        title="Conversation messages",
        description="Chat messages for a JARVIS conversation id.",
        mime_type="application/json",
    )
    def read_conversation(conversation_id: int) -> str:
        deps = _require_deps()
        messages = deps.memory.get_messages(conversation_id)
        return json.dumps(messages, indent=2)

    @server.prompt(
        name="ask_jarvis",
        title="Ask JARVIS",
        description="Start a butler-style task for JARVIS.",
    )
    def ask_jarvis_prompt(task: str = "") -> str:
        body = task.strip() or "How may I assist you?"
        return (
            f"You are connected to JARVIS, a local butler agent.\n\n"
            f"Task: {body}\n\n"
            "Use JARVIS tools or jarvis_ask to fulfill this request on the host machine."
        )

    _server = server
    _http_app = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
    )
    return server


def mcp_http_app():
    """Return the Streamable HTTP ASGI app (call init_mcp first)."""
    if _http_app is None:
        raise RuntimeError("MCP not initialized — call init_mcp() during app startup")
    return _http_app


def mcp_session_lifespan():
    """Async context manager keeping the MCP session manager alive."""
    if _server is None:
        raise RuntimeError("MCP not initialized — call init_mcp() during app startup")
    return _server.session_manager.run()


def _require_deps() -> McpDeps:
    if _deps is None:
        raise RuntimeError("MCP dependencies not configured")
    return _deps


def _read_text_file(path, fallback: str) -> str:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return fallback


def _tool_context(conversation_id: int | None = None) -> ToolContext:
    deps = _require_deps()
    cfg = store.get()
    return ToolContext(
        config=cfg,
        memory=deps.memory,
        llm=LLMClient(cfg.llm),
        tasks=deps.tasks,
        conversation_id=conversation_id,
    )


async def _invoke_jarvis_tool(tool: Tool, args: dict[str, Any]) -> str:
    cfg = store.get()
    if tool.is_dangerous(args) and not cfg.permissions.auto_approve:
        return (
            f"Denied: `{tool.name}` is gated and needs approval in the JARVIS UI "
            "(or enable auto-approve in Settings / JARVIS_AUTO_APPROVE)."
        )
    deps = _require_deps()
    deps.rem_touch()
    result = await tool.run(args, _tool_context())
    prefix = "" if result.ok else "Error: "
    return f"{prefix}{result.output}"


def _register_jarvis_tool(server: MCPServer[None], tool: Tool) -> None:
    """Register one JARVIS Tool as an MCP tool with matching JSON schema."""
    param_parts: list[str] = []
    annotations: dict[str, Any] = {"return": str}
    for name, spec in tool.parameters.items():
        py_type = _TYPE_MAP.get(spec.get("type", "string"), "str")
        if spec.get("_optional"):
            param_parts.append(f"{name}: {py_type} | None = None")
            annotations[name] = eval(f"{py_type} | None")  # noqa: S307 — mapped literals only
        else:
            param_parts.append(f"{name}: {py_type}")
            annotations[name] = eval(py_type)  # noqa: S307

    param_str = ", ".join(param_parts)
    namespace: dict[str, Any] = {
        "_invoke": _invoke_jarvis_tool,
        "_tool": tool,
    }
    exec(  # noqa: S102 — dynamic signature mirrors the tool schema
        f"async def _handler({param_str}):\n"
        "    args = {k: v for k, v in locals().items() if v is not None}\n"
        "    return await _invoke(_tool, args)\n",
        namespace,
    )
    handler = namespace["_handler"]
    handler.__name__ = tool.name
    handler.__doc__ = tool.description
    handler.__annotations__ = annotations

    destructive = tool.dangerous is True or callable(tool.dangerous)
    tool_annotations = (
        ToolAnnotations(destructiveHint=True, openWorldHint=True) if destructive else None
    )
    server.add_tool(
        handler,
        name=tool.name,
        description=tool.description,
        annotations=tool_annotations,
    )


async def _run_agent(message: str, conversation_id: int | None) -> str:
    deps = _require_deps()
    cfg = store.get()
    cid = conversation_id
    if cid is None:
        cid = deps.memory.create_conversation(title="MCP")
    else:
        cid = int(cid)

    reply_parts: list[str] = []
    control = RunControl()
    control.conversation_id = cid

    async def emit(event: dict) -> None:
        etype = event.get("type")
        if etype in {"assistant", "say"}:
            text = (event.get("text") or "").strip()
            if text:
                reply_parts.append(text)
        elif etype == "interrupted":
            reply_parts.append(event.get("message") or "Interrupted.")
        elif etype == "error":
            reply_parts.append(f"Error: {event.get('message') or 'Agent failure'}")

    async def confirm(call: dict) -> bool:
        if cfg.permissions.auto_approve:
            return True
        name = call.get("name") or "action"
        reply_parts.append(
            f"Denied gated tool `{name}` — MCP cannot approve destructive actions. "
            "Use the JARVIS web UI or enable auto-approve."
        )
        return False

    deps.rem_touch()
    deps.begin_run()
    agent = Agent(cfg, deps.memory)
    try:
        await agent.run(cid, message, emit, confirm, control=control)
    finally:
        deps.end_run()
        deps.rem_touch()

    if reply_parts:
        return reply_parts[-1]
    return "JARVIS finished without a text reply."
