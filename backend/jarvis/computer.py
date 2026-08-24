"""Cross-platform desktop control: screenshots, mouse, keyboard, clipboard, windows.

JARVIS runs on the host and drives that machine's GUI through OS-native utilities.
Backends are selected from the live session (Wayland / X11 / macOS / Windows).
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from .config import DATA_DIR

SCREENSHOT_DIR = DATA_DIR / "screenshots"
MAX_SCREENSHOTS = 40


class ComputerError(RuntimeError):
    """Raised when a GUI action cannot be performed on this host."""


@dataclass
class ScreenCapture:
    path: Path
    width: int
    height: int
    backend: str
    note: str = ""


@dataclass
class BackendInfo:
    name: str
    os: str
    session: str
    screenshot: bool
    mouse: bool
    keyboard: bool
    clipboard: bool
    windows: bool
    tools: dict[str, str] = field(default_factory=dict)
    hints: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _run(
    cmd: list[str],
    *,
    timeout: float = 15.0,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env=merged,
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise ComputerError(f"Command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ComputerError(f"Timed out: {' '.join(cmd)}") from exc


def _ensure_screenshot_dir() -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return SCREENSHOT_DIR


def _prune_screenshots() -> None:
    files = sorted(SCREENSHOT_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[MAX_SCREENSHOTS:]:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass


def _new_screenshot_path() -> Path:
    _ensure_screenshot_dir()
    path = SCREENSHOT_DIR / f"{uuid.uuid4().hex}.png"
    _prune_screenshots()
    return path


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size


def _session_type() -> str:
    return (
        os.environ.get("XDG_SESSION_TYPE")
        or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else "")
        or ("x11" if os.environ.get("DISPLAY") else "")
        or "unknown"
    ).lower()


def _parse_keys(keys: str | list[str]) -> list[str]:
    if isinstance(keys, list):
        parts = [str(k).strip() for k in keys if str(k).strip()]
    else:
        raw = (keys or "").strip()
        if not raw:
            return []
        # Accept "ctrl+shift+t" or "Return"
        parts = [p.strip() for p in raw.replace(" ", "").split("+") if p.strip()]
    return [p.lower() for p in parts]


class ComputerBackend(Protocol):
    def info(self) -> BackendInfo: ...
    def screenshot(self) -> ScreenCapture: ...
    def mouse_move(self, x: int, y: int) -> None: ...
    def click(self, x: int | None, y: int | None, button: str, clicks: int) -> None: ...
    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str) -> None: ...
    def scroll(self, dx: int, dy: int, x: int | None, y: int | None) -> None: ...
    def type_text(self, text: str) -> None: ...
    def key(self, keys: str | list[str]) -> None: ...
    def clipboard_get(self) -> str: ...
    def clipboard_set(self, text: str) -> None: ...
    def windows(self) -> list[dict[str, Any]]: ...
    def focus_window(self, query: str) -> str: ...


# ---------------------------------------------------------------------------
# Linux Wayland
# ---------------------------------------------------------------------------


class WaylandBackend:
    """grim + ydotool/wtype/dotool (+ hyprctl/swaymsg for windows)."""

    def __init__(self) -> None:
        self._grim = _which("grim")
        self._ydotool = _which("ydotool")
        self._wtype = _which("wtype")
        self._dotool = _which("dotool")
        self._wl_copy = _which("wl-copy")
        self._wl_paste = _which("wl-paste")
        self._hyprctl = _which("hyprctl")
        self._swaymsg = _which("swaymsg")
        self._gnome_shot = _which("gnome-screenshot")
        self._spectacle = _which("spectacle")

    def info(self) -> BackendInfo:
        tools: dict[str, str] = {}
        for label, path in (
            ("grim", self._grim),
            ("ydotool", self._ydotool),
            ("wtype", self._wtype),
            ("dotool", self._dotool),
            ("wl-copy", self._wl_copy),
            ("wl-paste", self._wl_paste),
            ("hyprctl", self._hyprctl),
            ("swaymsg", self._swaymsg),
        ):
            if path:
                tools[label] = path
        missing: list[str] = []
        if not (self._grim or self._gnome_shot or self._spectacle):
            missing.append("grim (or gnome-screenshot / spectacle) for screenshots")
        if not (self._ydotool or self._dotool):
            missing.append("ydotool (or dotool) for mouse + keyboard injection")
        elif self._ydotool and not self._wtype:
            pass  # ydotool covers keyboard
        if not self._ydotool and not self._dotool and not self._wtype:
            missing.append("wtype for keyboard-only typing")
        hints = [
            "computer_use: screenshot → look → click/type/scroll to drive the GUI.",
            "Wayland: prefer grim + ydotool (start ydotoold; user in input group).",
        ]
        if self._hyprctl:
            hints.append("Hyprland windows: computer_use action=windows / focus.")
        return BackendInfo(
            name="wayland",
            os="Linux",
            session="wayland",
            screenshot=bool(self._grim or self._gnome_shot or self._spectacle),
            mouse=bool(self._ydotool or self._dotool or self._hyprctl),
            keyboard=bool(self._ydotool or self._dotool or self._wtype),
            clipboard=bool(self._wl_copy and self._wl_paste),
            windows=bool(self._hyprctl or self._swaymsg),
            tools=tools,
            hints=hints,
            missing=missing,
        )

    def screenshot(self) -> ScreenCapture:
        path = _new_screenshot_path()
        if self._grim:
            r = _run([self._grim, str(path)])
            if r.returncode != 0 or not path.exists():
                raise ComputerError(f"grim failed: {(r.stderr or r.stdout or '').strip()}")
        elif self._gnome_shot:
            r = _run([self._gnome_shot, "-f", str(path)])
            if r.returncode != 0 or not path.exists():
                raise ComputerError(f"gnome-screenshot failed: {(r.stderr or r.stdout or '').strip()}")
        elif self._spectacle:
            r = _run([self._spectacle, "-b", "-n", "-o", str(path)])
            if r.returncode != 0 or not path.exists():
                raise ComputerError(f"spectacle failed: {(r.stderr or r.stdout or '').strip()}")
        else:
            raise ComputerError(
                "No Wayland screenshot tool. Install grim (recommended): pacman -S grim"
            )
        w, h = _image_size(path)
        return ScreenCapture(path, w, h, "wayland")

    def mouse_move(self, x: int, y: int) -> None:
        if self._ydotool:
            r = _run([self._ydotool, "mousemove", "--absolute", "-x", str(x), "-y", str(y)])
            if r.returncode != 0:
                raise ComputerError(_ydotool_hint(r))
            return
        if self._hyprctl:
            r = _run([self._hyprctl, "dispatch", "movecursor", str(x), str(y)])
            if r.returncode != 0:
                raise ComputerError(f"hyprctl movecursor failed: {(r.stderr or r.stdout or '').strip()}")
            return
        if self._dotool:
            _dotool_cmd(f"mousemove {x} {y}")
            return
        raise ComputerError("No Wayland mouse tool. Install ydotool and start ydotoold.")

    def click(self, x: int | None, y: int | None, button: str, clicks: int) -> None:
        if x is not None and y is not None:
            self.mouse_move(x, y)
            time.sleep(0.05)
        btn = _mouse_button_code(button)
        clicks = max(1, min(int(clicks or 1), 5))
        if self._ydotool:
            for _ in range(clicks):
                r = _run([self._ydotool, "click", str(btn)])
                if r.returncode != 0:
                    raise ComputerError(_ydotool_hint(r))
                time.sleep(0.05)
            return
        if self._dotool:
            name = {"left": "left", "right": "right", "middle": "middle"}.get(button, "left")
            for _ in range(clicks):
                _dotool_cmd(f"click {name}")
                time.sleep(0.05)
            return
        raise ComputerError("No Wayland click tool. Install ydotool and start ydotoold.")

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str) -> None:
        if self._ydotool:
            self.mouse_move(x1, y1)
            time.sleep(0.05)
            r_down = _run([self._ydotool, "click", _mouse_button_down(button)])
            if r_down.returncode != 0:
                raise ComputerError(_ydotool_hint(r_down))
            self.mouse_move(x2, y2)
            time.sleep(0.05)
            r_up = _run([self._ydotool, "click", _mouse_button_up(button)])
            if r_up.returncode != 0:
                raise ComputerError(_ydotool_hint(r_up))
            return
        if self._dotool:
            _dotool_cmd(f"mousemove {x1} {y1}")
            _dotool_cmd(f"mousedown {_dotool_button(button)}")
            _dotool_cmd(f"mousemove {x2} {y2}")
            _dotool_cmd(f"mouseup {_dotool_button(button)}")
            return
        raise ComputerError("No Wayland drag tool. Install ydotool.")

    def scroll(self, dx: int, dy: int, x: int | None, y: int | None) -> None:
        if x is not None and y is not None:
            self.mouse_move(x, y)
            time.sleep(0.05)
        if self._ydotool:
            # Wheel: positive y on --wheel moves wheel; invert so positive dy = scroll down
            amount = -int(dy) if dy else int(dx)
            if amount == 0:
                return
            r = _run([self._ydotool, "mousemove", "--wheel", "-x", "0", "-y", str(amount)])
            if r.returncode != 0:
                # Fallback: click wheel buttons (4=up 5=down in X numbering; ydotool uses 0xC0 style)
                for _ in range(min(abs(amount), 20)):
                    # SIDE/EXTR not ideal; use relative wheel only
                    pass
                raise ComputerError(_ydotool_hint(r))
            return
        if self._dotool and dy:
            direction = "up" if dy < 0 else "down"
            for _ in range(min(abs(int(dy)), 20)):
                _dotool_cmd(f"wheel {direction}")
            return
        raise ComputerError("No Wayland scroll tool. Install ydotool.")

    def type_text(self, text: str) -> None:
        if not text:
            return
        if self._ydotool:
            r = _run([self._ydotool, "type", "--", text])
            if r.returncode != 0:
                raise ComputerError(_ydotool_hint(r))
            return
        if self._wtype:
            r = _run([self._wtype, "--", text])
            if r.returncode != 0:
                raise ComputerError(f"wtype failed: {(r.stderr or r.stdout or '').strip()}")
            return
        if self._dotool:
            _dotool_cmd(f"type {text}")
            return
        raise ComputerError("No Wayland typer. Install ydotool or wtype.")

    def key(self, keys: str | list[str]) -> None:
        parts = _parse_keys(keys)
        if not parts:
            raise ComputerError("key requires keys like 'Return' or 'ctrl+c'.")
        if self._ydotool:
            events = _ydotool_key_events(parts)
            r = _run([self._ydotool, "key", *events])
            if r.returncode != 0:
                raise ComputerError(_ydotool_hint(r))
            return
        if self._wtype and len(parts) == 1:
            r = _run([self._wtype, "-k", parts[0]])
            if r.returncode != 0:
                raise ComputerError(f"wtype key failed: {(r.stderr or r.stdout or '').strip()}")
            return
        if self._wtype:
            args: list[str] = []
            for p in parts[:-1]:
                args.extend(["-M", p])
            args.extend(["-k", parts[-1]])
            for p in reversed(parts[:-1]):
                args.extend(["-m", p])
            r = _run([self._wtype, *args])
            if r.returncode != 0:
                raise ComputerError(f"wtype chord failed: {(r.stderr or r.stdout or '').strip()}")
            return
        if self._dotool:
            _dotool_cmd("key " + " ".join(parts))
            return
        raise ComputerError("No Wayland key tool. Install ydotool or wtype.")

    def clipboard_get(self) -> str:
        if not self._wl_paste:
            raise ComputerError("wl-paste not found. Install wl-clipboard.")
        r = _run([self._wl_paste, "-n"])
        if r.returncode != 0:
            raise ComputerError(f"wl-paste failed: {(r.stderr or '').strip()}")
        return r.stdout

    def clipboard_set(self, text: str) -> None:
        if not self._wl_copy:
            raise ComputerError("wl-copy not found. Install wl-clipboard.")
        r = _run([self._wl_copy], input_text=text)
        if r.returncode != 0:
            raise ComputerError(f"wl-copy failed: {(r.stderr or '').strip()}")

    def windows(self) -> list[dict[str, Any]]:
        if self._hyprctl:
            r = _run([self._hyprctl, "clients", "-j"])
            if r.returncode != 0:
                raise ComputerError(f"hyprctl clients failed: {(r.stderr or '').strip()}")
            import json

            try:
                clients = json.loads(r.stdout or "[]")
            except json.JSONDecodeError as exc:
                raise ComputerError("Could not parse hyprctl clients JSON.") from exc
            out = []
            for c in clients:
                out.append(
                    {
                        "id": c.get("address") or c.get("pid"),
                        "title": c.get("title") or "",
                        "class": c.get("class") or "",
                        "workspace": (c.get("workspace") or {}).get("name"),
                        "focused": bool(c.get("focusHistoryID") == 0),
                    }
                )
            return out
        if self._swaymsg:
            r = _run([self._swaymsg, "-t", "get_tree"])
            if r.returncode != 0:
                raise ComputerError(f"swaymsg failed: {(r.stderr or '').strip()}")
            import json

            try:
                tree = json.loads(r.stdout or "{}")
            except json.JSONDecodeError as exc:
                raise ComputerError("Could not parse sway tree.") from exc
            return _flatten_sway(tree)
        raise ComputerError("No window manager CLI (hyprctl/swaymsg).")

    def focus_window(self, query: str) -> str:
        q = (query or "").strip().lower()
        if not q:
            raise ComputerError("focus requires a window title/class query.")
        wins = self.windows()
        for w in wins:
            blob = f"{w.get('title', '')} {w.get('class', '')}".lower()
            if q in blob:
                wid = w.get("id")
                if self._hyprctl and wid:
                    r = _run([self._hyprctl, "dispatch", "focuswindow", f"address:{wid}"])
                    if r.returncode != 0:
                        r = _run([self._hyprctl, "dispatch", "focuswindow", str(wid)])
                    if r.returncode == 0:
                        return f"Focused: {w.get('title') or w.get('class') or wid}"
                if self._swaymsg:
                    title = w.get("title") or ""
                    r = _run([self._swaymsg, f'[title="{title}"]', "focus"])
                    if r.returncode == 0:
                        return f"Focused: {title}"
        raise ComputerError(f"No window matching '{query}'.")


def _ydotool_hint(r: subprocess.CompletedProcess[str]) -> str:
    err = (r.stderr or r.stdout or "").strip()
    return (
        f"ydotool failed: {err or f'exit {r.returncode}'}. "
        "Ensure ydotoold is running (systemctl --user start ydotoold) and your user "
        "can access /dev/uinput (input group)."
    )


def _mouse_button_code(button: str) -> str:
    """ydotool click codes: 0xC0 left click, 0xC1 right, 0xC2 middle."""
    return {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}.get(
        (button or "left").lower(), "0xC0"
    )


def _mouse_button_down(button: str) -> str:
    base = {"left": 0x00, "right": 0x01, "middle": 0x02}.get((button or "left").lower(), 0x00)
    return hex(base | 0x40)


def _mouse_button_up(button: str) -> str:
    base = {"left": 0x00, "right": 0x01, "middle": 0x02}.get((button or "left").lower(), 0x00)
    return hex(base | 0x80)


# Linux input-event-codes.h KEY_* values for ydotool.
_YDOTOOL_KEYCODES: dict[str, int] = {
    "esc": 1,
    "escape": 1,
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6": 7,
    "7": 8,
    "8": 9,
    "9": 10,
    "0": 11,
    "minus": 12,
    "equal": 13,
    "backspace": 14,
    "tab": 15,
    "q": 16,
    "w": 17,
    "e": 18,
    "r": 19,
    "t": 20,
    "y": 21,
    "u": 22,
    "i": 23,
    "o": 24,
    "p": 25,
    "leftbrace": 26,
    "rightbrace": 27,
    "enter": 28,
    "return": 28,
    "ctrl": 29,
    "control": 29,
    "leftctrl": 29,
    "a": 30,
    "s": 31,
    "d": 32,
    "f": 33,
    "g": 34,
    "h": 35,
    "j": 36,
    "k": 37,
    "l": 38,
    "semicolon": 39,
    "apostrophe": 40,
    "grave": 41,
    "shift": 42,
    "leftshift": 42,
    "backslash": 43,
    "z": 44,
    "x": 45,
    "c": 46,
    "v": 47,
    "b": 48,
    "n": 49,
    "m": 50,
    "comma": 51,
    "dot": 52,
    "slash": 53,
    "rightshift": 54,
    "kpasterisk": 55,
    "alt": 56,
    "leftalt": 56,
    "space": 57,
    "capslock": 58,
    "f1": 59,
    "f2": 60,
    "f3": 61,
    "f4": 62,
    "f5": 63,
    "f6": 64,
    "f7": 65,
    "f8": 66,
    "f9": 67,
    "f10": 68,
    "f11": 87,
    "f12": 88,
    "rightctrl": 97,
    "rightalt": 100,
    "home": 102,
    "up": 103,
    "pageup": 104,
    "left": 105,
    "right": 106,
    "end": 107,
    "down": 108,
    "pagedown": 109,
    "insert": 110,
    "delete": 111,
    "super": 125,
    "meta": 125,
    "win": 125,
    "cmd": 125,
    "leftmeta": 125,
    "rightmeta": 126,
}


def _ydotool_key_events(parts: list[str]) -> list[str]:
    """Build ydotool key args: down all modifiers+key, then up in reverse."""
    codes: list[int] = []
    for p in parts:
        code = _YDOTOOL_KEYCODES.get(p.lower())
        if code is None and len(p) == 1:
            code = _YDOTOOL_KEYCODES.get(p.lower())
        if code is None:
            raise ComputerError(
                f"Unknown key '{p}' for ydotool. Use names like Return, ctrl, c, Tab, F5."
            )
        codes.append(code)
    events: list[str] = [f"{c}:1" for c in codes]
    events.extend(f"{c}:0" for c in reversed(codes))
    return events


def _ydotool_key(name: str) -> str:
    """Deprecated name helper — prefer _ydotool_key_events."""
    code = _YDOTOOL_KEYCODES.get(name.lower())
    if code is None:
        raise ComputerError(f"Unknown key '{name}' for ydotool.")
    return str(code)


def _dotool_button(button: str) -> str:
    return {"left": "left", "right": "right", "middle": "middle"}.get(
        (button or "left").lower(), "left"
    )


def _dotool_cmd(line: str) -> None:
    dotool = _which("dotool")
    if not dotool:
        raise ComputerError("dotool not found.")
    r = _run([dotool], input_text=line + "\n")
    if r.returncode != 0:
        raise ComputerError(f"dotool failed: {(r.stderr or r.stdout or '').strip()}")


def _flatten_sway(node: dict[str, Any], out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out = out if out is not None else []
    if node.get("type") == "con" and node.get("name") and not node.get("nodes"):
        out.append(
            {
                "id": node.get("id"),
                "title": node.get("name") or "",
                "class": (node.get("app_id") or (node.get("window_properties") or {}).get("class") or ""),
                "focused": bool(node.get("focused")),
            }
        )
    for child in node.get("nodes") or []:
        _flatten_sway(child, out)
    for child in node.get("floating_nodes") or []:
        _flatten_sway(child, out)
    return out


# ---------------------------------------------------------------------------
# Linux X11
# ---------------------------------------------------------------------------


class X11Backend:
    def __init__(self) -> None:
        self._xdotool = _which("xdotool")
        self._import = _which("import")  # ImageMagick
        self._maim = _which("maim")
        self._scrot = _which("scrot")
        self._xclip = _which("xclip")
        self._xsel = _which("xsel")
        self._wmctrl = _which("wmctrl")

    def info(self) -> BackendInfo:
        tools = {}
        for label, path in (
            ("xdotool", self._xdotool),
            ("import", self._import),
            ("maim", self._maim),
            ("scrot", self._scrot),
            ("xclip", self._xclip),
            ("wmctrl", self._wmctrl),
        ):
            if path:
                tools[label] = path
        missing = []
        if not (self._maim or self._import or self._scrot):
            missing.append("maim, scrot, or ImageMagick import for screenshots")
        if not self._xdotool:
            missing.append("xdotool for mouse and keyboard")
        return BackendInfo(
            name="x11",
            os="Linux",
            session="x11",
            screenshot=bool(self._maim or self._import or self._scrot),
            mouse=bool(self._xdotool),
            keyboard=bool(self._xdotool),
            clipboard=bool(self._xclip or self._xsel),
            windows=bool(self._xdotool or self._wmctrl),
            tools=tools,
            hints=[
                "computer_use: screenshot → look → click/type to drive the GUI.",
                "X11: install xdotool and maim (or scrot).",
            ],
            missing=missing,
        )

    def screenshot(self) -> ScreenCapture:
        path = _new_screenshot_path()
        if self._maim:
            r = _run([self._maim, str(path)])
        elif self._import:
            r = _run([self._import, "-window", "root", str(path)])
        elif self._scrot:
            r = _run([self._scrot, str(path)])
        else:
            raise ComputerError("No X11 screenshot tool. Install maim or scrot.")
        if r.returncode != 0 or not path.exists():
            raise ComputerError(f"Screenshot failed: {(r.stderr or r.stdout or '').strip()}")
        w, h = _image_size(path)
        return ScreenCapture(path, w, h, "x11")

    def _need_xdotool(self) -> str:
        if not self._xdotool:
            raise ComputerError("xdotool not found. Install xdotool.")
        return self._xdotool

    def mouse_move(self, x: int, y: int) -> None:
        xd = self._need_xdotool()
        r = _run([xd, "mousemove", "--sync", str(x), str(y)])
        if r.returncode != 0:
            raise ComputerError(f"xdotool mousemove failed: {(r.stderr or '').strip()}")

    def click(self, x: int | None, y: int | None, button: str, clicks: int) -> None:
        xd = self._need_xdotool()
        btn = {"left": "1", "middle": "2", "right": "3"}.get((button or "left").lower(), "1")
        clicks = max(1, min(int(clicks or 1), 5))
        cmd = [xd]
        if x is not None and y is not None:
            cmd.extend(["mousemove", "--sync", str(x), str(y), "click", "--repeat", str(clicks), btn])
        else:
            cmd.extend(["click", "--repeat", str(clicks), btn])
        r = _run(cmd)
        if r.returncode != 0:
            raise ComputerError(f"xdotool click failed: {(r.stderr or '').strip()}")

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str) -> None:
        xd = self._need_xdotool()
        btn = {"left": "1", "middle": "2", "right": "3"}.get((button or "left").lower(), "1")
        r = _run([
            xd, "mousemove", "--sync", str(x1), str(y1),
            "mousedown", btn,
            "mousemove", "--sync", str(x2), str(y2),
            "mouseup", btn,
        ])
        if r.returncode != 0:
            raise ComputerError(f"xdotool drag failed: {(r.stderr or '').strip()}")

    def scroll(self, dx: int, dy: int, x: int | None, y: int | None) -> None:
        xd = self._need_xdotool()
        if x is not None and y is not None:
            self.mouse_move(x, y)
        btn = "4" if dy < 0 else "5" if dy > 0 else ("7" if dx < 0 else "6")
        n = min(abs(int(dy or dx or 0)), 20)
        for _ in range(max(1, n)):
            r = _run([xd, "click", btn])
            if r.returncode != 0:
                raise ComputerError(f"xdotool scroll failed: {(r.stderr or '').strip()}")

    def type_text(self, text: str) -> None:
        xd = self._need_xdotool()
        r = _run([xd, "type", "--clearmodifiers", "--", text])
        if r.returncode != 0:
            raise ComputerError(f"xdotool type failed: {(r.stderr or '').strip()}")

    def key(self, keys: str | list[str]) -> None:
        xd = self._need_xdotool()
        parts = _parse_keys(keys)
        if not parts:
            raise ComputerError("key requires keys like 'Return' or 'ctrl+c'.")
        chord = "+".join(_xdotool_key(p) for p in parts)
        r = _run([xd, "key", "--clearmodifiers", chord])
        if r.returncode != 0:
            raise ComputerError(f"xdotool key failed: {(r.stderr or '').strip()}")

    def clipboard_get(self) -> str:
        if self._xclip:
            r = _run([self._xclip, "-selection", "clipboard", "-o"])
        elif self._xsel:
            r = _run([self._xsel, "--clipboard", "--output"])
        else:
            raise ComputerError("Install xclip or xsel for clipboard.")
        if r.returncode != 0:
            raise ComputerError(f"clipboard get failed: {(r.stderr or '').strip()}")
        return r.stdout

    def clipboard_set(self, text: str) -> None:
        if self._xclip:
            r = _run([self._xclip, "-selection", "clipboard"], input_text=text)
        elif self._xsel:
            r = _run([self._xsel, "--clipboard", "--input"], input_text=text)
        else:
            raise ComputerError("Install xclip or xsel for clipboard.")
        if r.returncode != 0:
            raise ComputerError(f"clipboard set failed: {(r.stderr or '').strip()}")

    def windows(self) -> list[dict[str, Any]]:
        xd = self._need_xdotool()
        r = _run([xd, "search", "--name", ".*"])
        if r.returncode != 0:
            raise ComputerError(f"xdotool search failed: {(r.stderr or '').strip()}")
        out = []
        for line in (r.stdout or "").splitlines():
            wid = line.strip()
            if not wid.isdigit():
                continue
            name = _run([xd, "getwindowname", wid]).stdout.strip()
            out.append({"id": wid, "title": name, "class": ""})
        return out[:80]

    def focus_window(self, query: str) -> str:
        xd = self._need_xdotool()
        q = (query or "").strip()
        if not q:
            raise ComputerError("focus requires a window title query.")
        r = _run([xd, "search", "--name", q, "windowactivate", "--sync"])
        if r.returncode != 0:
            raise ComputerError(f"Could not focus window matching '{q}'.")
        return f"Focused window matching '{q}'."


def _xdotool_key(name: str) -> str:
    aliases = {
        "ctrl": "ctrl",
        "control": "ctrl",
        "alt": "alt",
        "shift": "shift",
        "super": "super",
        "meta": "meta",
        "win": "super",
        "enter": "Return",
        "return": "Return",
        "esc": "Escape",
        "escape": "Escape",
        "space": "space",
        "tab": "Tab",
        "backspace": "BackSpace",
        "delete": "Delete",
    }
    return aliases.get(name.lower(), name)


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


class DarwinBackend:
    def __init__(self) -> None:
        self._screencapture = _which("screencapture")
        self._cliclick = _which("cliclick")
        self._osascript = _which("osascript")
        self._pbcopy = _which("pbcopy")
        self._pbpaste = _which("pbpaste")

    def info(self) -> BackendInfo:
        tools = {k: v for k, v in {
            "screencapture": self._screencapture,
            "cliclick": self._cliclick,
            "osascript": self._osascript,
            "pbcopy": self._pbcopy,
            "pbpaste": self._pbpaste,
        }.items() if v}
        missing = []
        if not self._screencapture:
            missing.append("screencapture (built-in)")
        if not self._cliclick:
            missing.append("cliclick (brew install cliclick) for precise mouse — osascript fallback used")
        return BackendInfo(
            name="darwin",
            os="Darwin",
            session="aqua",
            screenshot=bool(self._screencapture),
            mouse=bool(self._cliclick or self._osascript),
            keyboard=bool(self._osascript or self._cliclick),
            clipboard=bool(self._pbcopy and self._pbpaste),
            windows=bool(self._osascript),
            tools=tools,
            hints=[
                "computer_use: screenshot → look → click/type to drive the GUI.",
                "macOS: screencapture is built-in; brew install cliclick for better mouse control.",
                "Grant Accessibility permission to the terminal/JARVIS process in System Settings.",
            ],
            missing=missing,
        )

    def screenshot(self) -> ScreenCapture:
        if not self._screencapture:
            raise ComputerError("screencapture not found.")
        path = _new_screenshot_path()
        r = _run([self._screencapture, "-x", str(path)])
        if r.returncode != 0 or not path.exists():
            raise ComputerError(f"screencapture failed: {(r.stderr or '').strip()}")
        w, h = _image_size(path)
        return ScreenCapture(path, w, h, "darwin")

    def mouse_move(self, x: int, y: int) -> None:
        if self._cliclick:
            r = _run([self._cliclick, f"m:{x},{y}"])
            if r.returncode != 0:
                raise ComputerError(f"cliclick move failed: {(r.stderr or '').strip()}")
            return
        raise ComputerError("Install cliclick (brew install cliclick) for mouse move.")

    def click(self, x: int | None, y: int | None, button: str, clicks: int) -> None:
        clicks = max(1, min(int(clicks or 1), 5))
        if self._cliclick:
            if x is None or y is None:
                raise ComputerError("click requires x and y on macOS without relative click support.")
            btn = (button or "left").lower()
            if btn == "right":
                cmd = [self._cliclick, f"rc:{x},{y}"]
            elif clicks >= 2:
                cmd = [self._cliclick, f"dc:{x},{y}"]
            else:
                cmd = [self._cliclick, f"c:{x},{y}"]
            r = _run(cmd)
            if r.returncode != 0:
                raise ComputerError(f"cliclick click failed: {(r.stderr or '').strip()}")
            for _ in range(clicks - (2 if clicks >= 2 and btn == "left" else 1)):
                _run([self._cliclick, f"c:{x},{y}"])
            return
        if self._osascript and x is not None and y is not None:
            # Limited: click at position via cliclick alternative using Python Quartz if available
            raise ComputerError("Install cliclick for mouse clicks (brew install cliclick).")
        raise ComputerError("No mouse click backend. Install cliclick.")

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str) -> None:
        if not self._cliclick:
            raise ComputerError("Install cliclick for drag.")
        r = _run([self._cliclick, f"dd:{x1},{y1}", f"du:{x2},{y2}"])
        if r.returncode != 0:
            raise ComputerError(f"cliclick drag failed: {(r.stderr or '').strip()}")

    def scroll(self, dx: int, dy: int, x: int | None, y: int | None) -> None:
        if self._cliclick:
            if x is not None and y is not None:
                self.mouse_move(x, y)
            # cliclick doesn't scroll well; use osascript
        if not self._osascript:
            raise ComputerError("osascript required for scroll.")
        amount = int(dy or 0)
        if amount == 0 and dx:
            amount = int(dx)
        script = f'tell application "System Events" to scroll ({{0, {amount}}})'
        # System Events scroll is unreliable; use key pages
        if amount:
            key = "page down" if amount > 0 else "page up"
            for _ in range(min(abs(amount), 10)):
                _run([self._osascript, "-e", f'tell application "System Events" to key code {_mac_key_code(key)}'])

    def type_text(self, text: str) -> None:
        if self._cliclick:
            # cliclick type: t:text — escapes poorly; prefer osascript
            pass
        if not self._osascript:
            raise ComputerError("osascript not found.")
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        r = _run([
            self._osascript, "-e",
            f'tell application "System Events" to keystroke "{escaped}"',
        ])
        if r.returncode != 0:
            # Fallback: clipboard paste
            self.clipboard_set(text)
            self.key("cmd+v")

    def key(self, keys: str | list[str]) -> None:
        parts = _parse_keys(keys)
        if not parts:
            raise ComputerError("key requires keys like 'Return' or 'cmd+c'.")
        if not self._osascript:
            raise ComputerError("osascript not found.")
        mods = []
        key = parts[-1]
        for p in parts[:-1]:
            mods.append(_mac_modifier(p))
        using = f" using {{{', '.join(m + ' down' for m in mods)}}}" if mods else ""
        code = _mac_key_code(key)
        if code is not None:
            script = f'tell application "System Events" to key code {code}{using}'
        else:
            escaped = key.replace('"', '\\"')
            script = f'tell application "System Events" to keystroke "{escaped}"{using}'
        r = _run([self._osascript, "-e", script])
        if r.returncode != 0:
            raise ComputerError(f"osascript key failed: {(r.stderr or '').strip()}")

    def clipboard_get(self) -> str:
        if not self._pbpaste:
            raise ComputerError("pbpaste not found.")
        r = _run([self._pbpaste])
        if r.returncode != 0:
            raise ComputerError("pbpaste failed.")
        return r.stdout

    def clipboard_set(self, text: str) -> None:
        if not self._pbcopy:
            raise ComputerError("pbcopy not found.")
        r = _run([self._pbcopy], input_text=text)
        if r.returncode != 0:
            raise ComputerError("pbcopy failed.")

    def windows(self) -> list[dict[str, Any]]:
        if not self._osascript:
            raise ComputerError("osascript not found.")
        script = '''
        tell application "System Events"
          set wins to {}
          repeat with p in (every process whose background only is false)
            try
              repeat with w in windows of p
                set end of wins to (name of p as text) & " | " & (name of w as text)
              end repeat
            end try
          end repeat
          set AppleScript's text item delimiters to linefeed
          return wins as text
        end tell
        '''
        r = _run([self._osascript, "-e", script], timeout=20)
        if r.returncode != 0:
            raise ComputerError(f"window list failed: {(r.stderr or '').strip()}")
        out = []
        for line in (r.stdout or "").splitlines():
            if " | " in line:
                app, title = line.split(" | ", 1)
                out.append({"id": "", "title": title, "class": app})
        return out[:80]

    def focus_window(self, query: str) -> str:
        if not self._osascript:
            raise ComputerError("osascript not found.")
        q = (query or "").strip()
        if not q:
            raise ComputerError("focus requires an app or window name.")
        # Try activate by process/app name first
        escaped = q.replace('"', '\\"')
        r = _run([self._osascript, "-e", f'tell application "{escaped}" to activate'])
        if r.returncode == 0:
            return f"Activated '{q}'."
        r2 = _run([
            self._osascript, "-e",
            f'tell application "System Events" to set frontmost of first process whose name contains "{escaped}" to true',
        ])
        if r2.returncode == 0:
            return f"Focused process matching '{q}'."
        raise ComputerError(f"Could not focus '{q}'.")


def _mac_modifier(name: str) -> str:
    return {
        "cmd": "command",
        "command": "command",
        "meta": "command",
        "super": "command",
        "ctrl": "control",
        "control": "control",
        "alt": "option",
        "option": "option",
        "shift": "shift",
    }.get(name.lower(), name.lower())


def _mac_key_code(name: str) -> int | None:
    codes = {
        "return": 36,
        "enter": 36,
        "tab": 48,
        "space": 49,
        "delete": 51,
        "escape": 53,
        "esc": 53,
        "left": 123,
        "right": 124,
        "down": 125,
        "up": 126,
        "page up": 116,
        "page down": 121,
    }
    return codes.get(name.lower())


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


class WindowsBackend:
    def info(self) -> BackendInfo:
        return BackendInfo(
            name="windows",
            os="Windows",
            session="win32",
            screenshot=True,
            mouse=True,
            keyboard=True,
            clipboard=True,
            windows=True,
            tools={"powershell": _which("powershell") or _which("pwsh") or "powershell"},
            hints=[
                "computer_use: screenshot → look → click/type to drive the GUI.",
                "Windows uses PowerShell/.NET for screen capture and input.",
            ],
            missing=[],
        )

    def _ps(self) -> str:
        return _which("powershell") or _which("pwsh") or "powershell"

    def screenshot(self) -> ScreenCapture:
        path = _new_screenshot_path()
        # Use forward-safe path for PowerShell
        ps_path = str(path).replace("'", "''")
        script = f"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save('{ps_path}')
$g.Dispose(); $bmp.Dispose()
Write-Output "$($bounds.Width)x$($bounds.Height)"
"""
        r = _run([self._ps(), "-NoProfile", "-Command", script], timeout=30)
        if r.returncode != 0 or not path.exists():
            raise ComputerError(f"Windows screenshot failed: {(r.stderr or r.stdout or '').strip()}")
        w, h = _image_size(path)
        return ScreenCapture(path, w, h, "windows")

    def mouse_move(self, x: int, y: int) -> None:
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({int(x)}, {int(y)})
"""
        r = _run([self._ps(), "-NoProfile", "-Command", script])
        if r.returncode != 0:
            raise ComputerError(f"mouse move failed: {(r.stderr or '').strip()}")

    def click(self, x: int | None, y: int | None, button: str, clicks: int) -> None:
        if x is not None and y is not None:
            self.mouse_move(x, y)
            time.sleep(0.05)
        clicks = max(1, min(int(clicks or 1), 5))
        btn = (button or "left").lower()
        down, up = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }.get(btn, (0x0002, 0x0004))
        script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class JMouse {{
  [DllImport("user32.dll")] public static extern void mouse_event(int f, int x, int y, int d, int e);
}}
"@
1..{clicks} | ForEach-Object {{
  [JMouse]::mouse_event({down}, 0, 0, 0, 0)
  [JMouse]::mouse_event({up}, 0, 0, 0, 0)
  Start-Sleep -Milliseconds 40
}}
"""
        r = _run([self._ps(), "-NoProfile", "-Command", script])
        if r.returncode != 0:
            raise ComputerError(f"click failed: {(r.stderr or '').strip()}")

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str) -> None:
        self.mouse_move(x1, y1)
        script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class JMouse2 {{
  [DllImport("user32.dll")] public static extern void mouse_event(int f, int x, int y, int d, int e);
}}
"@
[JMouse2]::mouse_event(0x0002, 0, 0, 0, 0)
"""
        _run([self._ps(), "-NoProfile", "-Command", script])
        self.mouse_move(x2, y2)
        _run([self._ps(), "-NoProfile", "-Command", """
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class JMouse3 {{
  [DllImport("user32.dll")] public static extern void mouse_event(int f, int x, int y, int d, int e);
}}
"@
[JMouse3]::mouse_event(0x0004, 0, 0, 0, 0)
"""])

    def scroll(self, dx: int, dy: int, x: int | None, y: int | None) -> None:
        if x is not None and y is not None:
            self.mouse_move(x, y)
        amount = int(-dy * 120) if dy else int(dx * 120)
        script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class JScroll {{
  [DllImport("user32.dll")] public static extern void mouse_event(int f, int x, int y, int d, int e);
}}
"@
[JScroll]::mouse_event(0x0800, 0, 0, {amount}, 0)
"""
        r = _run([self._ps(), "-NoProfile", "-Command", script])
        if r.returncode != 0:
            raise ComputerError(f"scroll failed: {(r.stderr or '').strip()}")

    def type_text(self, text: str) -> None:
        # SendKeys — escape special chars
        escaped = (
            text.replace("{", "{{")
            .replace("}", "}}")
            .replace("+", "{+}")
            .replace("^", "{^}")
            .replace("%", "{%}")
            .replace("~", "{~}")
            .replace("(", "{(}")
            .replace(")", "{)}")
        )
        ps_text = escaped.replace("'", "''")
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait('{ps_text}')
"""
        r = _run([self._ps(), "-NoProfile", "-Command", script])
        if r.returncode != 0:
            raise ComputerError(f"type failed: {(r.stderr or '').strip()}")

    def key(self, keys: str | list[str]) -> None:
        parts = _parse_keys(keys)
        if not parts:
            raise ComputerError("key requires keys like 'Enter' or 'ctrl+c'.")
        send = _win_sendkeys(parts)
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait('{send}')
"""
        r = _run([self._ps(), "-NoProfile", "-Command", script])
        if r.returncode != 0:
            raise ComputerError(f"key failed: {(r.stderr or '').strip()}")

    def clipboard_get(self) -> str:
        r = _run([self._ps(), "-NoProfile", "-Command", "Get-Clipboard -Raw"])
        if r.returncode != 0:
            raise ComputerError(f"clipboard get failed: {(r.stderr or '').strip()}")
        return r.stdout

    def clipboard_set(self, text: str) -> None:
        # Use stdin pipeline to avoid escaping hell
        r = _run([self._ps(), "-NoProfile", "-Command", "Set-Clipboard -Value $input"], input_text=text)
        if r.returncode != 0:
            raise ComputerError(f"clipboard set failed: {(r.stderr or '').strip()}")

    def windows(self) -> list[dict[str, Any]]:
        script = """
Get-Process | Where-Object { $_.MainWindowTitle } | ForEach-Object {
  "{0}`t{1}`t{2}" -f $_.Id, $_.ProcessName, $_.MainWindowTitle
}
"""
        r = _run([self._ps(), "-NoProfile", "-Command", script])
        if r.returncode != 0:
            raise ComputerError(f"window list failed: {(r.stderr or '').strip()}")
        out = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                out.append({"id": parts[0], "class": parts[1], "title": parts[2]})
        return out[:80]

    def focus_window(self, query: str) -> str:
        q = (query or "").strip().replace("'", "''")
        if not q:
            raise ComputerError("focus requires a window title query.")
        script = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class JFocus {{
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}}
"@
$p = Get-Process | Where-Object {{ $_.MainWindowTitle -match '{q}' }} | Select-Object -First 1
if (-not $p) {{ throw "No window matching {q}" }}
[JFocus]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
$p.MainWindowTitle
"""
        r = _run([self._ps(), "-NoProfile", "-Command", script])
        if r.returncode != 0:
            raise ComputerError(f"focus failed: {(r.stderr or r.stdout or '').strip()}")
        return f"Focused: {(r.stdout or '').strip() or q}"


def _win_sendkeys(parts: list[str]) -> str:
    mods = []
    key = parts[-1]
    for p in parts[:-1]:
        if p in {"ctrl", "control"}:
            mods.append("^")
        elif p in {"alt"}:
            mods.append("%")
        elif p in {"shift"}:
            mods.append("+")
        elif p in {"win", "super", "meta"}:
            mods.append("^{ESC}")  # poor substitute; Windows key limited in SendKeys
    special = {
        "enter": "{ENTER}",
        "return": "{ENTER}",
        "tab": "{TAB}",
        "esc": "{ESC}",
        "escape": "{ESC}",
        "space": " ",
        "backspace": "{BACKSPACE}",
        "delete": "{DELETE}",
        "up": "{UP}",
        "down": "{DOWN}",
        "left": "{LEFT}",
        "right": "{RIGHT}",
    }
    body = special.get(key.lower(), key if len(key) == 1 else "{" + key.upper() + "}")
    return "".join(mods) + body


# ---------------------------------------------------------------------------
# Detection + public API
# ---------------------------------------------------------------------------

_backend: ComputerBackend | None = None


def detect_backend() -> ComputerBackend:
    system = platform.system()
    if system == "Darwin":
        return DarwinBackend()
    if system == "Windows":
        return WindowsBackend()
    # Linux and other Unix
    session = _session_type()
    if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return WaylandBackend()
    if session == "x11" or os.environ.get("DISPLAY"):
        # Prefer X11 tools when DISPLAY is set even under mislabeled sessions
        x11 = X11Backend()
        if x11.info().mouse or x11.info().screenshot:
            return x11
        if os.environ.get("WAYLAND_DISPLAY"):
            return WaylandBackend()
        return x11
    # Fallback: try Wayland then X11
    if os.environ.get("WAYLAND_DISPLAY"):
        return WaylandBackend()
    return X11Backend()


def get_backend() -> ComputerBackend:
    global _backend
    if _backend is None:
        _backend = detect_backend()
    return _backend


def reset_backend() -> None:
    global _backend
    _backend = None


def computer_capabilities() -> dict[str, Any]:
    info = get_backend().info()
    return {
        "backend": info.name,
        "os": info.os,
        "session": info.session,
        "screenshot": info.screenshot,
        "mouse": info.mouse,
        "keyboard": info.keyboard,
        "clipboard": info.clipboard,
        "windows": info.windows,
        "tools": info.tools,
        "hints": info.hints,
        "missing": info.missing,
    }


def format_computer_hints() -> list[str]:
    caps = computer_capabilities()
    hints = list(caps.get("hints") or [])
    missing = caps.get("missing") or []
    if missing:
        hints.append("computer_use missing deps: " + "; ".join(missing))
    else:
        hints.append(
            f"computer_use ready ({caps.get('backend')}): "
            "screenshot, click, type, key, scroll, drag, clipboard, windows/focus."
        )
    return hints


async def run_computer_action(action: str, **kwargs: Any) -> tuple[bool, str, ScreenCapture | None]:
    """Execute a computer_use action in a worker thread. Returns (ok, output, capture?)."""
    return await asyncio.to_thread(_run_action_sync, action, kwargs)


def _run_action_sync(action: str, kwargs: dict[str, Any]) -> tuple[bool, str, ScreenCapture | None]:
    action = (action or "").strip().lower()
    backend = get_backend()
    try:
        if action in {"screenshot", "screen", "capture"}:
            cap = backend.screenshot()
            msg = (
                f"Screenshot saved ({cap.width}x{cap.height}, backend={cap.backend}). "
                f"Coordinates are in this pixel space (origin top-left). "
                f"path={cap.path}"
            )
            return True, msg, cap

        if action in {"move", "mouse_move", "mousemove"}:
            x, y = _require_xy(kwargs)
            backend.mouse_move(x, y)
            return True, f"Moved pointer to ({x}, {y}).", None

        if action == "click":
            x = kwargs.get("x")
            y = kwargs.get("y")
            xi = int(x) if x is not None and str(x) != "" else None
            yi = int(y) if y is not None and str(y) != "" else None
            button = str(kwargs.get("button") or "left")
            clicks = int(kwargs.get("clicks") or 1)
            backend.click(xi, yi, button, clicks)
            where = f" at ({xi}, {yi})" if xi is not None else ""
            return True, f"Clicked {button} x{clicks}{where}.", None

        if action == "drag":
            x1 = int(kwargs["x"])
            y1 = int(kwargs["y"])
            x2 = int(kwargs.get("x2") if kwargs.get("x2") is not None else kwargs["destination_x"])
            y2 = int(kwargs.get("y2") if kwargs.get("y2") is not None else kwargs["destination_y"])
            button = str(kwargs.get("button") or "left")
            backend.drag(x1, y1, x2, y2, button)
            return True, f"Dragged {button} from ({x1},{y1}) to ({x2},{y2}).", None

        if action == "scroll":
            dx = int(kwargs.get("dx") or 0)
            dy = int(kwargs.get("dy") or kwargs.get("amount") or 0)
            x = kwargs.get("x")
            y = kwargs.get("y")
            xi = int(x) if x is not None and str(x) != "" else None
            yi = int(y) if y is not None and str(y) != "" else None
            backend.scroll(dx, dy, xi, yi)
            return True, f"Scrolled dx={dx} dy={dy}.", None

        if action in {"type", "type_text"}:
            text = kwargs.get("text")
            if text is None:
                return False, "type requires text.", None
            backend.type_text(str(text))
            return True, f"Typed {len(str(text))} character(s).", None

        if action == "key":
            keys = kwargs.get("keys") or kwargs.get("key") or ""
            backend.key(keys)
            return True, f"Pressed keys: {keys}.", None

        if action == "clipboard":
            mode = (kwargs.get("mode") or ("set" if kwargs.get("text") else "get")).lower()
            if mode == "get":
                data = backend.clipboard_get()
                preview = data if len(data) <= 2000 else data[:2000] + "…"
                return True, f"Clipboard ({len(data)} chars):\n{preview}", None
            text = kwargs.get("text")
            if text is None:
                return False, "clipboard set requires text.", None
            backend.clipboard_set(str(text))
            return True, f"Clipboard set ({len(str(text))} chars).", None

        if action == "windows":
            wins = backend.windows()
            if not wins:
                return True, "No windows listed.", None
            lines = [
                f"- [{w.get('id')}] {w.get('class') or ''} — {w.get('title') or ''}"
                for w in wins
            ]
            return True, "Windows:\n" + "\n".join(lines), None

        if action == "focus":
            query = str(kwargs.get("query") or kwargs.get("text") or kwargs.get("title") or "")
            msg = backend.focus_window(query)
            return True, msg, None

        if action in {"status", "info", "capabilities"}:
            caps = computer_capabilities()
            missing = caps.get("missing") or []
            lines = [
                f"Backend: {caps.get('backend')} ({caps.get('os')}, session={caps.get('session')})",
                f"screenshot={caps.get('screenshot')} mouse={caps.get('mouse')} "
                f"keyboard={caps.get('keyboard')} clipboard={caps.get('clipboard')} "
                f"windows={caps.get('windows')}",
            ]
            if caps.get("tools"):
                lines.append("Tools: " + ", ".join(f"{k}={v}" for k, v in caps["tools"].items()))
            if missing:
                lines.append("Missing: " + "; ".join(missing))
            return True, "\n".join(lines), None

        return (
            False,
            "Unknown action. Use: screenshot, click, move, drag, scroll, type, key, "
            "clipboard, windows, focus, status.",
            None,
        )
    except ComputerError as exc:
        return False, str(exc), None
    except Exception as exc:  # noqa: BLE001
        return False, f"computer_use error: {exc}", None


def _require_xy(kwargs: dict[str, Any]) -> tuple[int, int]:
    if kwargs.get("x") is None or kwargs.get("y") is None:
        raise ComputerError("x and y are required.")
    return int(kwargs["x"]), int(kwargs["y"])
