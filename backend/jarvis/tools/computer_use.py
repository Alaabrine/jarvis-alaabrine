"""GUI computer-use — screenshots, mouse, keyboard, clipboard, windows.

One universal tool so JARVIS can operate the desktop the way a human would:
see the screen, then click, type, scroll, and switch windows. Backends are
selected for the host OS/session (Wayland, X11, macOS, Windows).
"""

from __future__ import annotations

from pathlib import Path

from ..computer import run_computer_action
from ..media import _image_part, _normalise_image_bytes
from .base import Tool, ToolContext, ToolResult, prop


def _dangerous(args: dict) -> bool:
    # GUI actions are reversible in the common case; hard gates stay on shell/files/email.
    return False


def _preview(args: dict) -> str:
    action = args.get("action", "?")
    if action == "click":
        return f"Click ({args.get('x')}, {args.get('y')}) {args.get('button', 'left')}"
    if action == "type":
        text = str(args.get("text") or "")
        return f"Type {len(text)} character(s)"
    if action == "key":
        return f"Key: {args.get('keys') or args.get('key')}"
    if action == "screenshot":
        return "Capture the screen"
    return f"computer_use({action})"


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    action = (args.get("action") or "").strip().lower()
    if not action:
        return ToolResult(
            False,
            "Missing action. Use: screenshot, click, move, drag, scroll, type, key, "
            "clipboard, windows, focus, status.",
        )

    ok, output, capture = await run_computer_action(
        action,
        x=args.get("x"),
        y=args.get("y"),
        x2=args.get("x2"),
        y2=args.get("y2"),
        destination_x=args.get("destination_x"),
        destination_y=args.get("destination_y"),
        button=args.get("button"),
        clicks=args.get("clicks"),
        dx=args.get("dx"),
        dy=args.get("dy"),
        amount=args.get("amount"),
        text=args.get("text"),
        keys=args.get("keys") or args.get("key"),
        mode=args.get("mode"),
        query=args.get("query") or args.get("title"),
    )

    images = None
    if ok and capture is not None:
        try:
            raw = Path(capture.path).read_bytes()
            normalised, mime = _normalise_image_bytes(raw)
            images = [_image_part(normalised, mime)]
            output = (
                f"{output}\n"
                f"[Screen image attached for vision — coordinates are 0..{capture.width - 1} "
                f"x 0..{capture.height - 1}. Identify the next target, then click/type.]"
            )
        except Exception as exc:  # noqa: BLE001
            output = f"{output}\n(Warning: could not attach image for vision: {exc})"

    return ToolResult(ok, output, images=images)


computer_use = Tool(
    name="computer_use",
    description=(
        "Control the desktop GUI on THIS machine like a human operator. "
        "For on-screen tasks (apps already open, dialogs, forms): "
        "1) action=screenshot and look at the attached image, "
        "2) action=click/move/drag/scroll/type/key to interact, "
        "3) screenshot again to verify. "
        "Also: clipboard (get/set), windows (list), focus (bring window forward), "
        "status (what GUI backends are available). "
        "Coordinates use the screenshot pixel grid (origin top-left). "
        "Do NOT use this to open a URL or launch an app — use device_control "
        "action=open (url=… or app=…) instead. Prefer this over shell for clicking UI; "
        "use device_control shell for CLIs and peripherals for hardware "
        "(volume, RGB, Bluetooth)."
    ),
    parameters={
        "action": prop(
            "string",
            "One of: screenshot, click, move, drag, scroll, type, key, "
            "clipboard, windows, focus, status.",
        ),
        "x": prop("integer", "X pixel (click/move/drag start/scroll).", optional=True),
        "y": prop("integer", "Y pixel (click/move/drag start/scroll).", optional=True),
        "x2": prop("integer", "End X for drag.", optional=True),
        "y2": prop("integer", "End Y for drag.", optional=True),
        "destination_x": prop("integer", "Alias for x2 (drag).", optional=True),
        "destination_y": prop("integer", "Alias for y2 (drag).", optional=True),
        "button": prop("string", "Mouse button: left, right, middle (default left).", optional=True),
        "clicks": prop("integer", "Click count (1 or 2).", optional=True),
        "dx": prop("integer", "Horizontal scroll notches.", optional=True),
        "dy": prop("integer", "Vertical scroll notches (positive = down).", optional=True),
        "amount": prop("integer", "Alias for dy (scroll).", optional=True),
        "text": prop("string", "Text to type, or clipboard set value.", optional=True),
        "keys": prop(
            "string",
            "Key or chord, e.g. Return, Tab, ctrl+c, alt+Tab, cmd+v.",
            optional=True,
        ),
        "key": prop("string", "Alias for keys.", optional=True),
        "mode": prop("string", "clipboard mode: get or set.", optional=True),
        "query": prop("string", "Window title/class substring for focus.", optional=True),
        "title": prop("string", "Alias for query (focus).", optional=True),
    },
    run=_run,
    dangerous=_dangerous,
    preview=_preview,
)
