"""Unified device control — one tool for all local actions (shell, files, apps, URLs).

JARVIS learns the device autonomously; this tool is how it acts on that knowledge
without needing separate explicit tools for each operation type.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
from typing import Any

from . import apps, files, shell
from .base import Tool, ToolContext, ToolResult, prop
from .. import controls as ctl


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
    if action == "control":
        control = _find_control(args)
        if control is None:
            return False
        if control.risky:
            return True
        try:
            return _is_shell_dangerous(_render(control, args))
        except ValueError:
            return False
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
    if action == "control":
        control = _find_control(args)
        if control is None:
            return f"Device control: {args.get('control') or args.get('query') or '?'}"
        try:
            return f"{control.summary}: {_render(control, args)}"
        except ValueError as exc:
            return f"{control.id} ({exc})"
    if action in {"controls", "capabilities"}:
        return f"List device controls: {args.get('query') or 'all'}"
    return f"device_control({action})"


def _control_params(args: dict) -> dict[str, str]:
    """Collect parameters for a control from value=, params={...}, or named keys."""
    out: dict[str, str] = {}
    raw = args.get("params")
    if isinstance(raw, dict):
        out.update({str(k): str(v) for k, v in raw.items()})
    elif isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            out.update({str(k): str(v) for k, v in parsed.items()})
    for key in ("value", "ssid", "password", "index", "iface", "servers"):
        if args.get(key) not in (None, ""):
            out[key] = str(args[key])
    return out


def _resolve_control(args: dict) -> tuple[Any, dict[str, str]]:
    """Resolve control=<id> — or plain speech — into a control and its parameters."""
    params = _control_params(args)
    ref = str(
        args.get("control") or args.get("capability") or args.get("query") or ""
    ).strip()
    if not ref:
        return None, params
    exact = ctl.get(ref)
    if exact is not None:
        return exact, params
    resolved = ctl.resolve(ref)
    if resolved is not None:
        # Phrasing like "dim my screen" carries its own default amount.
        merged = dict(resolved.params)
        merged.update(params)
        return resolved.control, merged
    needle = ref.lower().replace(" ", ".").replace("_", ".")
    matches = [c for c in ctl.available() if needle in c.id.lower()]
    return (matches[0] if len(matches) == 1 else None), params


def _find_control(args: dict):
    return _resolve_control(args)[0]


def _render(control, args: dict) -> str:
    control, params = _resolve_control(args) if control is None else (control, None)
    if params is None:
        params = _resolve_control(args)[1]
    command = control.render(params)
    if control.sudo and platform.system() != "Windows" and os.geteuid() != 0:
        command = f"sudo {command}"
    return command


def _suggest_controls(ref: str, limit: int = 8) -> str:
    tokens = [t for t in ref.lower().replace(".", " ").split() if len(t) > 2]
    scored = []
    for control in ctl.available():
        haystack = f"{control.id} {control.summary}".lower()
        hits = sum(1 for t in tokens if t in haystack)
        if hits:
            scored.append((hits, control))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    if not scored:
        cats = sorted({c.category for c in ctl.available()})
        return (
            "No control matches that. Categories available: "
            + ", ".join(cats)
            + ". Call action=controls query=<word> to list them."
        )
    return "Closest controls:\n" + "\n".join(
        f"  {c.signature()} — {c.summary}" for _, c in scored[:limit]
    )


async def _run_control(args: dict, ctx: ToolContext) -> ToolResult:
    ref = str(
        args.get("control") or args.get("capability") or args.get("query") or ""
    ).strip()
    if not ref:
        return ToolResult(
            False,
            "control action requires control=<id>. Call action=controls to list them.",
        )
    control, params = _resolve_control(args)
    if control is None:
        return ToolResult(False, f"Unknown control '{ref}'.\n" + _suggest_controls(ref))
    try:
        command = control.render(params)
    except ValueError as exc:
        return ToolResult(False, f"{exc}\n{_suggest_controls(control.id)}")
    if control.sudo and platform.system() != "Windows" and os.geteuid() != 0:
        command = f"sudo {command}"
    result = await shell._run(
        {"command": command, "timeout": args.get("timeout") or control.timeout},
        ctx,
    )
    label = f"[{control.id}] {control.summary}"
    return ToolResult(result.ok, f"{label}\n{result.output}")


async def _list_controls(args: dict) -> ToolResult:
    query = str(args.get("query") or args.get("control") or "").strip().lower()
    controls = ctl.available()
    if query:
        tokens = [t for t in query.replace(".", " ").split() if t]
        controls = [
            c
            for c in controls
            if all(t in f"{c.id} {c.summary} {c.category}".lower() for t in tokens)
        ]
    if not controls:
        return ToolResult(True, f"No device control matches '{query}'.")
    grouped: dict[str, list] = {}
    for control in controls:
        grouped.setdefault(control.category, []).append(control)
    lines = [f"Device controls available here ({len(controls)}):"]
    for category in sorted(grouped):
        lines.append(f"\n{category}:")
        for control in sorted(grouped[category], key=lambda c: c.id):
            marks = []
            if control.reads:
                marks.append("read")
            if control.risky:
                marks.append("confirm")
            if control.sudo:
                marks.append("sudo")
            suffix = f" [{', '.join(marks)}]" if marks else ""
            lines.append(f"  {control.signature()} — {control.summary}{suffix}")
    gaps = ctl.installable()
    if gaps and not query:
        lines.append("\nInstall to unlock more:")
        for gap in gaps[:8]:
            lines.append(f"  {gap['package']} → {gap['unlocks']}")
    return ToolResult(True, "\n".join(lines))


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    action = (args.get("action") or "").strip().lower()
    if not action:
        return ToolResult(
            False,
            "Missing action. Use: control, controls, shell, read, write, list, move, delete, open, apps.",
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

    if action == "control":
        return await _run_control(args, ctx)

    if action in {"controls", "capabilities"}:
        return await _list_controls(args)

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
        f"Unknown action '{action}'. Use: control, controls, shell, read, write, list, move, delete, open, apps.",
    )


device_control = Tool(
    name="device_control",
    description=(
        "Control THIS device directly. JARVIS already learned its OS, hardware, and "
        "available utilities — every action it can perform is listed in your context "
        "under 'Device controls available on this machine'. "
        "Actions: "
        "control (PREFERRED — perform one catalogued action by id: control=<id> "
        "value=<v>, e.g. control=display.brightness.set value=30, "
        "control=audio.volume.down value=10, control=network.wifi.connect value=<ssid>. "
        "The command, backend, and OS quirks are already resolved for this host), "
        "controls (list or search every action available here — query=<word>), "
        "shell (run any command — for anything the catalogue does not cover), "
        "read/write/list/move/delete (files), "
        "open (launch an app or URL — prefer this over computer_use for opening "
        "websites, YouTube channels, or apps in a new window; app may be a name, an "
        "app id like org.vinegarhq.Vinegar, or a purpose like 'roblox studio', and "
        "file=<path-or-url> opens that document inside the app), "
        "apps (search the catalogue of every installed application — native, Flatpak, "
        "Snap, Wine, macOS bundle — by name or purpose, and get install candidates "
        "when nothing installed fits). "
        "Reach for action=control first: it is the exhaustive, verified way to change "
        "brightness, volume, Wi-Fi, Bluetooth, power, displays, media, services, "
        "packages, windows, clipboard, and lighting on this machine. Drop to "
        "action=shell only for something the catalogue lacks. Call this tool and run "
        "the command yourself — never paste a command in chat, never scaffold an "
        "unrelated project, and do not ask them to scan first."
    ),
    parameters={
        "action": prop(
            "string",
            "One of: control, controls, shell, read, write, list, move, delete, "
            "open, apps.",
        ),
        "control": prop(
            "string",
            "Control id to perform, e.g. display.brightness.set, audio.mute, "
            "network.wifi.off, power.lock (action=control).",
            optional=True,
        ),
        "value": prop(
            "string",
            "The control's value — a percentage, step, name, choice, or path "
            "(action=control).",
            optional=True,
        ),
        "params": prop(
            "object",
            "Extra named parameters for controls that need more than one, e.g. "
            "{\"ssid\": \"home\", \"password\": \"…\"} (action=control).",
            optional=True,
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
