"""Tool registry for JARVIS.

JARVIS uses a small set of universal tools instead of many explicit per-action tools.
The agent learns the device autonomously on startup and controls it via device_control.
"""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult
from . import browse, communicate, device_control, remember, tasks


def all_tools() -> list[Tool]:
    """Return every registered tool instance."""
    return [
        device_control.device_control,
        browse.browse,
        communicate.communicate,
        remember.remember,
        tasks.start_task,
        tasks.run_background_shell,
        tasks.check_task,
        tasks.list_tasks,
        tasks.cancel_task,
    ]


def registry() -> dict[str, Tool]:
    return {t.name: t for t in all_tools()}


def openai_schemas() -> list[dict]:
    return [t.schema() for t in all_tools()]


__all__ = ["Tool", "ToolContext", "ToolResult", "all_tools", "registry", "openai_schemas"]
