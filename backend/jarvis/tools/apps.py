"""Launching applications and URLs via the host OS handlers."""

from __future__ import annotations

import asyncio
import platform
import shutil

from .base import Tool, ToolContext, ToolResult, prop


def _opener() -> list[str] | None:
    system = platform.system()
    if system == "Linux":
        if shutil.which("xdg-open"):
            return ["xdg-open"]
    elif system == "Darwin":
        return ["open"]
    elif system == "Windows":
        return ["cmd", "/c", "start", ""]
    return None


async def _spawn(cmd: list[str]) -> ToolResult:
    try:
        await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        return ToolResult(False, f"Failed to launch {cmd}: {exc}")
    return ToolResult(True, f"Launched: {' '.join(cmd)}")


async def _open_app(args: dict, ctx: ToolContext) -> ToolResult:
    app = args["app"]
    app_args = args.get("args", [])
    if isinstance(app_args, str):
        app_args = [app_args]
    if shutil.which(app):
        return await _spawn([app, *app_args])
    opener = _opener()
    if opener:
        return await _spawn([*opener, app])
    return ToolResult(False, f"Could not find a way to open '{app}' on this platform.")


async def _open_url(args: dict, ctx: ToolContext) -> ToolResult:
    url = args["url"]
    opener = _opener()
    if not opener:
        return ToolResult(False, "No URL opener available on this platform.")
    return await _spawn([*opener, url])


open_app = Tool(
    name="open_app",
    description="Open/launch an application on this device by name or executable path.",
    parameters={
        "app": prop("string", "Application name or executable (e.g. 'firefox', 'code')."),
        "args": prop("array", "Optional launch arguments.", optional=True, items={"type": "string"}),
    },
    run=_open_app,
    preview=lambda a: f"Open application: {a.get('app','')}",
)

open_url = Tool(
    name="open_url",
    description="Open a URL or file in the default handler (browser, viewer, etc.).",
    parameters={"url": prop("string", "The URL or file path to open.")},
    run=_open_url,
    preview=lambda a: f"Open URL: {a.get('url','')}",
)
