"""Unified device control — one tool for all local actions (shell, files, apps, URLs).

JARVIS learns the device autonomously; this tool is how it acts on that knowledge
without needing separate explicit tools for each operation type.
"""

from __future__ import annotations

import shlex

from . import apps, files, shell
from .base import Tool, ToolContext, ToolResult, prop


def _is_shell_dangerous(command: str) -> bool:
    return shell._is_dangerous({"command": command})


def _dangerous(args: dict) -> bool:
    action = args.get("action", "")
    if action == "shell":
        return _is_shell_dangerous(args.get("command", ""))
    if action == "write":
        path = args.get("path", "")
        if path:
            return files._write_dangerous({"path": path})
        return True
    if action in {"move", "delete"}:
        return True
    return False


def _preview(args: dict) -> str:
    action = args.get("action", "?")
    if action == "shell":
        return f"Run: {args.get('command', '')}"
    if action == "read":
        return f"Read file: {args.get('path', '')}"
    if action == "write":
        return f"Write file: {args.get('path', '')}"
    if action == "list":
        return f"List directory: {args.get('path', '.')}"
    if action == "move":
        return f"Move: {args.get('source', '')} → {args.get('destination', '')}"
    if action == "delete":
        return f"Delete: {args.get('path', '')}"
    if action == "open":
        target = args.get("app") or args.get("url") or args.get("target") or args.get("file") or "?"
        in_file = args.get("file") or ""
        if args.get("app") and in_file:
            return f"Open {in_file} in {args['app']}"
        return f"Open: {target}"
    if action in {"apps", "find_app", "applications"}:
        return f"Find installed app: {args.get('query') or args.get('app') or 'all'}"
    return f"device_control({action})"


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    action = (args.get("action") or "").strip().lower()
    if not action:
        return ToolResult(
            False,
            "Missing action. Use: shell, read, write, list, move, delete, open, apps.",
        )

    if action == "shell":
        command = args.get("command", "").strip()
        if not command:
            return ToolResult(False, "shell action requires command.")
        return await shell._run(
            {
                "command": command,
                "cwd": args.get("cwd"),
                "timeout": args.get("timeout", 60),
            },
            ctx,
        )

    if action == "read":
        path = args.get("path", "").strip()
        if not path:
            return ToolResult(False, "read action requires path.")
        return await files._read_file({"path": path}, ctx)

    if action == "write":
        path = args.get("path", "").strip()
        if not path:
            return ToolResult(False, "write action requires path.")
        return await files._write_file(
            {"path": path, "content": args.get("content", "")},
            ctx,
        )

    if action == "list":
        return await files._list_dir({"path": args.get("path", ".")}, ctx)

    if action == "move":
        source = args.get("source", "").strip()
        destination = args.get("destination", "").strip()
        if not source or not destination:
            return ToolResult(False, "move action requires source and destination.")
        return await files._move({"source": source, "destination": destination}, ctx)

    if action == "delete":
        path = args.get("path", "").strip()
        if not path:
            return ToolResult(False, "delete action requires path.")
        return await files._delete({"path": path}, ctx)

    if action in {"apps", "find_app", "applications"}:
        return await apps._find_apps(
            {"query": args.get("query") or args.get("app") or args.get("target") or ""},
            ctx,
        )

    if action == "open":
        url = (args.get("url") or "").strip()
        app = (args.get("app") or args.get("target") or "").strip()
        file_target = (args.get("file") or "").strip()
        app_args = args.get("args", [])
        if isinstance(app_args, str):
            app_args = shlex.split(app_args)

        if url and not app:
            if url.startswith(("http://", "https://", "file://")) or "/" in url or "." in url:
                return await apps._open_url({"url": url}, ctx)
            # A bare name with no scheme is an application, not a URL.
            app = url
            url = ""
        if app:
            return await apps._open_app(
                {"app": app, "args": app_args, "target_file": file_target or url},
                ctx,
            )
        if file_target:
            return await apps._open_url({"url": file_target}, ctx)
        return ToolResult(False, "open action requires url, app/target, or file.")

    return ToolResult(
        False,
        f"Unknown action '{action}'. Use: shell, read, write, list, move, delete, open, apps.",
    )


device_control = Tool(
    name="device_control",
    description=(
        "Control THIS device directly. JARVIS already learned its OS, hardware, and "
        "available utilities — use the control hints in your context. "
        "Actions: "
        "shell (run any command — primary way to control the machine), "
        "read/write/list/move/delete (files), "
        "open (launch an app or URL — prefer this over computer_use for opening "
        "websites, YouTube channels, or apps in a new window; app may be a name, an "
        "app id like org.vinegarhq.Vinegar, or a purpose like 'roblox studio', and "
        "file=<path-or-url> opens that document inside the app), "
        "apps (search the catalogue of every installed application — native, Flatpak, "
        "Snap, Wine, macOS bundle — by name or purpose, and get install candidates "
        "when nothing installed fits). "
        "Prefer shell with the correct utility for services, packages, drivers, lighting, "
        "and anything not covered by the peripherals tool. Call this tool and run the "
        "command yourself — never paste it in chat, never scaffold an unrelated project, "
        "and do not ask them to scan first."
    ),
    parameters={
        "action": prop(
            "string",
            "One of: shell, read, write, list, move, delete, open, apps.",
        ),
        "command": prop("string", "Shell command (action=shell).", optional=True),
        "path": prop("string", "File or directory path (read/write/list/delete).", optional=True),
        "content": prop("string", "File content (action=write).", optional=True),
        "source": prop("string", "Source path (action=move).", optional=True),
        "destination": prop("string", "Destination path (action=move).", optional=True),
        "url": prop("string", "URL to open (action=open).", optional=True),
        "app": prop("string", "Application name or path (action=open).", optional=True),
        "target": prop("string", "Alias for app (action=open).", optional=True),
        "file": prop(
            "string",
            "File or URL to open inside the app (action=open).",
            optional=True,
        ),
        "query": prop(
            "string",
            "App name or purpose to look up, e.g. 'roblox studio' (action=apps).",
            optional=True,
        ),
        "args": prop(
            "array",
            "Launch arguments for app (action=open).",
            optional=True,
            items={"type": "string"},
        ),
        "cwd": prop("string", "Working directory for shell.", optional=True),
        "timeout": prop("integer", "Shell timeout in seconds (default 60).", optional=True),
    },
    run=_run,
    dangerous=_dangerous,
    preview=_preview,
)
