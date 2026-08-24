"""Tool registry for JARVIS.

JARVIS uses a small set of universal tools instead of many explicit per-action tools.
The agent learns the device autonomously on startup and controls it via device_control,
computer_use (GUI), and peripherals.
"""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult
from . import (
    browse,
    communicate,
    computer_use,
    device_control,
    mcp_discover,
    mcp_invoke,
    peripherals,
    remember,
    tasks,
)


def _core_tools() -> list[Tool]:
    return [
        device_control.device_control,
        computer_use.computer_use,
        peripherals.peripherals,
        browse.browse,
        communicate.communicate,
        remember.remember,
        mcp_discover.mcp_discover,
        mcp_invoke.mcp_invoke,
        tasks.start_task,
        tasks.run_background_shell,
        tasks.check_task,
        tasks.list_tasks,
        tasks.cancel_task,
    ]


def all_tools() -> list[Tool]:
    """Return every registered tool instance (core + remote MCP proxies)."""
    try:
        from ..mcp_client import get_manager

        return _core_tools() + get_manager().proxy_tools()
    except Exception:  # noqa: BLE001
        return _core_tools()


def registry() -> dict[str, Tool]:
    return {t.name: t for t in all_tools()}


def openai_schemas_for_agent() -> list[dict]:
    """Schemas for the chat LLM: core tools plus imported MCP proxy tools."""
    return [t.schema() for t in all_tools()]


def openai_schemas() -> list[dict]:
    return openai_schemas_for_agent()


__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "all_tools",
    "openai_schemas",
    "openai_schemas_for_agent",
    "registry",
]
