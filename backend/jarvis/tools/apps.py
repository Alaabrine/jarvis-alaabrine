"""Launching applications and URLs — resolved against the host application catalog.

``_open_app`` accepts whatever the user or the model said ("Vinegar", "roblox studio",
"org.vinegarhq.Vinegar", "firefox") and finds the matching installed application,
including Flatpak, Snap, Wine and macOS-bundle apps that never appear on ``PATH``.
"""

from __future__ import annotations

import asyncio
import platform
import shutil

from ..appcatalog import get_catalog
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


async def _spawn(cmd: list[str], label: str = "") -> ToolResult:
    try:
        await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        return ToolResult(False, f"Failed to launch {cmd}: {exc}")
    detail = f"{label} — " if label else ""
    return ToolResult(True, f"Launched: {detail}{' '.join(cmd)}")


async def _open_app(args: dict, ctx: ToolContext) -> ToolResult:
    app = (args.get("app") or "").strip()
    if not app:
        return ToolResult(False, "open requires an app name.")
    app_args = args.get("args", [])
    if isinstance(app_args, str):
        app_args = [app_args]
    app_args = [str(a) for a in app_args if str(a).strip()]
    target = (args.get("target_file") or args.get("uri") or "").strip()

    # A path or URI is a document, never an application name — resolving it against
    # the catalog would match on incidental words in the path.
    if "://" in app or app.startswith(("/", "~", "./", "../")):
        return await _open_url({"url": app}, ctx)

    # 1. The application catalog knows Flatpak/Snap/Wine/bundle apps too, and maps a
    #    purpose ("roblox studio") onto the program that serves it.
    catalog = get_catalog()
    hits = catalog.resolve(app, limit=3)
    if hits and hits[0][1] >= 20.0:
        entry, score = hits[0]
        argv = entry.launch_argv(target)
        argv.extend(app_args)
        result = await _spawn(argv, label=f"{entry.name} [{entry.kind}]")
        if result.ok and len(hits) > 1 and score < 60.0:
            others = ", ".join(f"{e.name}" for e, _ in hits[1:])
            result.output += f"\n(Other candidates for '{app}': {others})"
        return result

    # 2. A bare executable on PATH.
    if shutil.which(app):
        return await _spawn([app, *([target] if target else []), *app_args])

    # 3. A path or URI can go to the desktop handler; a bare unknown name cannot —
    #    xdg-open would "succeed" while opening nothing at all.
    opener = _opener()
    looks_like_uri = "." in app.split()[0]
    if opener and looks_like_uri:
        return await _spawn([*opener, app])

    suggestions = catalog.resolve(app, limit=4, floor=1.0)
    near = ", ".join(f"{e.name} ({e.kind}:{e.id})" for e, _ in suggestions)
    return ToolResult(
        False,
        f"No installed application matches '{app}'."
        + (f" Closest catalogue entries: {near}." if near else "")
        + " Use device_control action=apps query=<purpose> to search the catalogue and "
        "get install commands, then install it and retry.",
    )


async def _open_url(args: dict, ctx: ToolContext) -> ToolResult:
    del ctx
    url = args["url"]
    app = (args.get("app") or "").strip()
    if app:
        # "open <url> in <app>" — launch the app with the URL as its argument.
        entry = get_catalog().best(app)
        if entry:
            return await _spawn(entry.launch_argv(url), label=entry.name)
    opener = _opener()
    if not opener:
        return ToolResult(False, "No URL opener available on this platform.")
    return await _spawn([*opener, url])


async def _find_apps(args: dict, ctx: ToolContext) -> ToolResult:
    """Search installed applications by name or purpose; offer installs when empty."""
    del ctx
    query = (args.get("query") or args.get("app") or "").strip()
    catalog = get_catalog()
    if not query:
        apps = catalog.apps()
        gui = [a for a in apps if a.gui]
        lines = [f"{catalog.summary()}. GUI applications:"]
        for entry in gui[:80]:
            lines.append(f"  - {entry.name} [{entry.kind}:{entry.id}] {entry.purpose()}".rstrip())
        return ToolResult(True, "\n".join(lines))

    hits = catalog.resolve(query, limit=8)
    lines: list[str] = []
    if hits:
        lines.append(f"Installed applications matching '{query}':")
        for entry, score in hits:
            launch = " ".join(entry.launch_argv())
            bits = [f"  - {entry.name} [{entry.kind}:{entry.id}] score={score:.0f}"]
            if entry.purpose():
                bits.append(f"    purpose: {entry.purpose()}")
            bits.append(f"    launch: device_control action=open app={entry.id}   ({launch})")
            if entry.aliases:
                bits.append(f"    good for: {', '.join(entry.aliases[:6])}")
            lines.append("\n".join(bits))
        lines.append("Open the best match yourself with device_control action=open.")
    else:
        lines.append(f"Nothing installed matches '{query}'.")

    if not hits or hits[0][1] < 45.0:
        suggestions = await asyncio.to_thread(_install_candidates, query)
        if suggestions:
            lines.append(f"\nInstall candidates for '{query}' (run with device_control action=shell):")
            lines.extend(f"  $ {s}" for s in suggestions)
    return ToolResult(True, "\n".join(lines))


def _install_candidates(query: str) -> list[str]:
    from ..appcatalog import install_suggestions

    return install_suggestions(query)


open_app = Tool(
    name="open_app",
    description="Open/launch an application on this device by name, purpose, or app id.",
    parameters={
        "app": prop("string", "Application name, id, or purpose (e.g. 'firefox', 'roblox studio')."),
        "args": prop("array", "Optional launch arguments.", optional=True, items={"type": "string"}),
        "target_file": prop("string", "Optional file or URL to open in the app.", optional=True),
    },
    run=_open_app,
    preview=lambda a: f"Open application: {a.get('app','')}",
)

open_url = Tool(
    name="open_url",
    description="Open a URL or file in the default handler (browser, viewer, etc.).",
    parameters={
        "url": prop("string", "The URL or file path to open."),
        "app": prop("string", "Optional application to open it in.", optional=True),
    },
    run=_open_url,
    preview=lambda a: f"Open URL: {a.get('url','')}",
)
