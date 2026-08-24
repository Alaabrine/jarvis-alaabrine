"""Invoke tools on installed remote MCP servers through one stable agent-facing tool."""

from __future__ import annotations

import json
from typing import Any

from .base import Tool, ToolContext, ToolResult, prop


async def _run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    del ctx
    from ..mcp_client import get_manager
    server_ref = (args.get("server") or "").strip()
    tool_ref = (args.get("tool") or "").strip()
    if not server_ref:
        return ToolResult(False, "server is required (installed MCP server name, e.g. Roblox).")
    if not tool_ref:
        return ToolResult(False, "tool is required (remote tool name, e.g. user_by_username).")

    raw_args = args.get("arguments")
    if raw_args is None:
        raw_args = args.get("args")
    if raw_args is None:
        arguments: dict[str, Any] = {}
    elif isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            arguments = parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError as exc:
            return ToolResult(False, f"arguments must be valid JSON: {exc}")
    elif isinstance(raw_args, dict):
        arguments = raw_args
    else:
        return ToolResult(False, "arguments must be a JSON object.")

    mgr = get_manager()
    try:
        entry = mgr.resolve_server(server_ref)
    except ValueError as exc:
        return ToolResult(False, str(exc))

    try:
        remote_name = mgr.resolve_remote_tool(entry.id, tool_ref)
    except ValueError as exc:
        return ToolResult(False, str(exc))

    try:
        text = await mgr.call_remote(entry.id, remote_name, arguments)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(False, f"MCP tool failed: {exc}")

    return ToolResult(True, text)


mcp_invoke = Tool(
    name="mcp_invoke",
    description=(
        "Call a tool on an installed remote MCP server. Use this whenever the user asks "
        "to use an MCP server or MCP tool (Roblox, ClawFetch, etc.). "
        "Provide the server display name and the remote tool name; pass tool arguments in "
        "arguments. Do not claim MCP access is unavailable — use this tool."
    ),
    parameters={
        "server": prop(
            "string",
            "Installed MCP server name or id (e.g. Roblox, ai.clawfetch/mcp).",
        ),
        "tool": prop(
            "string",
            "Remote tool name on that server (e.g. user_by_username, fetch_url).",
        ),
        "arguments": prop(
            "object",
            "JSON object of arguments accepted by the remote MCP tool.",
            optional=True,
        ),
    },
    run=_run,
)
