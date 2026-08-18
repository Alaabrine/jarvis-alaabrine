"""Tool registry for JARVIS."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult
from . import apps, email_tool, files, shell, system, tasks, web


def all_tools() -> list[Tool]:
    """Return every registered tool instance."""
    return [
        web.web_search,
        web.web_fetch,
        files.read_file,
        files.list_dir,
        files.write_file,
        files.move_path,
        files.delete_path,
        shell.run_shell,
        apps.open_app,
        apps.open_url,
        email_tool.send_email,
        email_tool.read_email,
        email_tool.list_email_profiles,
        system.system_scan,
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
