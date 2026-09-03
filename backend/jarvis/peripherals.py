"""Detect, learn, and control peripherals attached to (or near) this machine.

USB, Bluetooth, audio, displays, HID, cameras, printers, removable storage,
Wi-Fi, LAN/mDNS neighbours — inventory on startup, refresh periodically, and
expose connect / control primitives the agent can call without inventing shell.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .config import store
from .memory import Memory

EmitFn = Callable[[dict[str, Any]], Any]

USB_CLASS = {
    "00": "composite",
    "01": "audio",
    "02": "communications",
    "03": "hid",
    "06": "imaging",
    "07": "printer",
    "08": "storage",
    "09": "hub",
    "0a": "cdc-data",
    "0e": "video",
    "e0": "wireless",
    "ef": "misc",
    "ff": "vendor",
}

_BORING_HID = {
    "power button",
    "sleep button",
    "lid switch",
    "video bus",
    "pc speaker",
    "hda intel pch",
    "sof-hda-dsp",
    "gpio-keys",
    "intel hid events",
    "intel hid 5 button array",
}

_BORING_USB_VENDORS = {"1d6b"}  # Linux Foundation root hubs

KIND_ORDER = (
    "bluetooth",
    "usb",
    "audio",
    "display",
    "hid",
    "lighting",
    "camera",
    "printer",
    "storage",
    "wifi",
    "network",
    "radio",
)

_LIGHTING_COMMANDS = {
    "lighting",
    "light",
    "lights",
    "rgb",
    "led",
    "backlight",
    "rainbow",
    "spectrum",
    "wave",
    "static",
    "breathing",
    "off",
    "on",
}

_EFFECT_ALIASES = {
    "rainbow": ["rainbow", "Rainbow Wave", "spectrum", "Spectrum Cycle", "wave"],
    "spectrum": ["spectrum", "Spectrum Cycle", "rainbow", "Rainbow Wave"],
    "wave": ["wave", "Rainbow Wave", "rainbow"],
    "breathing": ["breathing", "breathe", "pulse"],
    "static": ["static", "direct"],
    "off": ["off"],
    "on": ["static", "on"],
}


def _run(cmd: list[str], timeout: float = 5.0, input_text: str | None = None) -> str:
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            input=input_text,
        )
        return (out.stdout or out.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _run_shell(
    command: str,
    timeout: float = 8.0,
    input_text: str | None = None,
) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            input=input_text,
            env=os.environ.copy(),
        )
        text = (out.stdout or "") + (("\n" + out.stderr) if out.stderr else "")
        return out.returncode == 0, text.strip()[:12_000]
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s: {command}"
    except OSError as exc:
        return False, str(exc)


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(errors="replace").strip() or default
    except OSError:
        return default


def _make(
    *,
    kind: str,
    name: str,
    address: str = "",
    vendor: str = "",
    product: str = "",
    serial: str = "",
    connected: bool = False,
    paired: bool = False,
    trusted: bool = False,
    icon: str = "",
    hints: list[str] | None = None,
    identifiers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ident = address or serial or product or name
    pid = f"{kind}:{ident}".lower().replace(" ", "_")
    pid = re.sub(r"[^a-z0-9:._-]+", "-", pid)[:80]
    return {
        "id": pid,
        "kind": kind,
        "name": name or ident or kind,
        "address": address,
        "vendor": vendor,
        "product": product,
        "serial": serial,
        "connected": bool(connected),
        "paired": bool(paired),
        "trusted": bool(trusted),
        "available": True,
        "icon": icon or kind,
        "control_hints": hints or [],
        "identifiers": identifiers or {},
        "extra": extra or {},
        "facts": [],
    }


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------

def scan_usb() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    root = Path("/sys/bus/usb/devices")
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not (d / "idVendor").exists():
                continue
            vendor = _read(d / "idVendor")
            product = _read(d / "idProduct")
            if not vendor or vendor in _BORING_USB_VENDORS:
                continue
            cls = _read(d / "bDeviceClass").lower()
            if cls in {"09", "e0"}:
                continue
            manu = _read(d / "manufacturer")
            prod = _read(d / "product")
            if not prod and not manu:
                continue
            prod = prod or f"{vendor}:{product}"
            serial = _read(d / "serial")
            cls_name = USB_CLASS.get(cls, cls or "usb")
            name = f"{manu} {prod}".strip() if manu else prod
            hints = [f"Inspect: lsusb -d {vendor}:{product} -v", f"sysfs: {d}"]
            if cls_name == "hid":
                hints.append("Input device — typically plug-and-play; no explicit connect.")
            found.append(
                _make(
                    kind="usb",
                    name=name,
                    address=f"{vendor}:{product}",
                    vendor=manu or vendor,
                    product=prod,
                    serial=serial,
                    connected=True,
                    icon=cls_name,
                    hints=hints,
                    identifiers={"sysfs": str(d), "vid": vendor, "pid": product},
                    extra={"usb_class": cls_name, "bus": d.name},
                )
            )
        if found:
            return found[:40]

    raw = _run(["lsusb"], timeout=3)
    for line in raw.splitlines():
        m = re.search(r"ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s+(.*)$", line)
        if not m:
            continue
        vid, pid, rest = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()
        if vid in _BORING_USB_VENDORS:
            continue
        found.append(
            _make(
                kind="usb",
                name=rest or f"{vid}:{pid}",
                address=f"{vid}:{pid}",
                vendor=vid,
                product=pid,
                connected=True,
                hints=[f"Inspect: lsusb -d {vid}:{pid} -v"],
                identifiers={"vid": vid, "pid": pid},
            )
        )
    return found[:40]


def scan_bluetooth() -> list[dict[str, Any]]:
    if not shutil.which("bluetoothctl"):
        return []
    listed = _run(["bluetoothctl", "--timeout", "4", "devices"], timeout=6)
    paired_raw = _run(["bluetoothctl", "--timeout", "4", "devices", "Paired"], timeout=6)
    if not paired_raw:
        paired_raw = _run(["bluetoothctl", "paired-devices"], timeout=5)
    conn_raw = _run(["bluetoothctl", "--timeout", "4", "devices", "Connected"], timeout=6)

    def _macs(text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in text.splitlines():
            m = re.search(
                r"Device\s+([0-9A-Fa-f:]{11,17})\s+(.*)$", line.strip()
            )
            if m:
                out[m.group(1).upper()] = m.group(2).strip()
        return out

    names = _macs(listed)
    paired = set(_macs(paired_raw))
    connected = set(_macs(conn_raw))
    for mac, name in _macs(paired_raw).items():
        names.setdefault(mac, name)
    for mac, name in _macs(conn_raw).items():
        names.setdefault(mac, name)

    found: list[dict[str, Any]] = []
    for mac, name in names.items():
        info = _run(["bluetoothctl", "--timeout", "3", "info", mac], timeout=5)
        icon = ""
        trusted = False
        alias = name
        uuids: list[str] = []
        for line in info.splitlines():
            s = line.strip()
            if s.startswith("Alias:"):
                alias = s.split(":", 1)[1].strip() or alias
            elif s.startswith("Icon:"):
                icon = s.split(":", 1)[1].strip()
            elif s.startswith("Trusted:"):
                trusted = "yes" in s.lower()
            elif s.startswith("Paired:") and "yes" in s.lower():
                paired.add(mac)
            elif s.startswith("Connected:") and "yes" in s.lower():
                connected.add(mac)
            elif s.startswith("UUID:"):
                uuids.append(s.split(":", 1)[1].strip()[:80])
        is_conn = mac in connected
        is_paired = mac in paired
        hints = [
            f"Connect: bluetoothctl connect {mac}",
            f"Disconnect: bluetoothctl disconnect {mac}",
        ]
        if not is_paired:
            hints.append(f"Pair: bluetoothctl pair {mac} ; bluetoothctl trust {mac}")
        found.append(
            _make(
                kind="bluetooth",
                name=alias or name,
                address=mac,
                connected=is_conn,
                paired=is_paired,
                trusted=trusted,
                icon=icon or "bluetooth",
                hints=hints,
                identifiers={"mac": mac},
                extra={"uuids": uuids[:8], "icon": icon},
            )
        )
    return found[:30]


def discover_bluetooth(timeout: float = 8.0) -> list[dict[str, Any]]:
    """Active inquiry for nearby unpaired adapters. Slow; on-demand only."""
    if not shutil.which("bluetoothctl"):
        return []
    _run(["bluetoothctl", "--timeout", "2", "power", "on"], timeout=4)
    _run(["bluetoothctl", "--timeout", str(int(timeout)), "scan", "on"], timeout=timeout + 2)
    return scan_bluetooth()


def scan_audio() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if shutil.which("pactl"):
        for kind, cmd in (
            ("sink", ["pactl", "list", "short", "sinks"]),
            ("source", ["pactl", "list", "short", "sources"]),
        ):
            for line in _run(cmd, timeout=3).splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                idx, name = parts[0], parts[1]
                state = parts[-1] if len(parts) > 2 else ""
                if ".monitor" in name:
                    continue
                pretty = name.replace("_", " ")
                running = state.upper() == "RUNNING"
                hints = (
                    [
                        f"Volume: pactl set-sink-volume {name} 50%",
                        f"Mute: pactl set-sink-mute {name} toggle",
                        f"Default: pactl set-default-sink {name}",
                    ]
                    if kind == "sink"
                    else [
                        f"Volume: pactl set-source-volume {name} 50%",
                        f"Mute: pactl set-source-mute {name} toggle",
                        f"Default: pactl set-default-source {name}",
                    ]
                )
                found.append(
                    _make(
                        kind="audio",
                        name=f"{pretty} ({kind})",
                        address=name,
                        connected=running,
                        icon="speaker" if kind == "sink" else "microphone",
                        hints=hints,
                        identifiers={"pactl_name": name, "pactl_index": idx, "role": kind},
                        extra={"state": state, "role": kind},
                    )
                )
        if found:
            return found[:20]

    if shutil.which("wpctl"):
        status = _run(["wpctl", "status"], timeout=3)
        section = ""
        for line in status.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sinks:"):
                section = "sink"
                continue
            if stripped.startswith("Sources:"):
                section = "source"
                continue
            if stripped.startswith("Filters:") or stripped.startswith("Streams:"):
                section = ""
                continue
            if section not in {"sink", "source"}:
                continue
            m = re.search(r"(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[vol:.*)?$", stripped)
            if not m:
                continue
            default, idx, name = bool(m.group(1)), m.group(2), m.group(3).strip()
            hints = [f"Volume: wpctl set-volume {idx} 50%", f"Mute: wpctl set-mute {idx} toggle"]
            if section == "sink":
                hints.append(f"Default: wpctl set-default {idx}")
            found.append(
                _make(
                    kind="audio",
                    name=f"{name} ({section})",
                    address=idx,
                    connected=default,
                    icon="speaker" if section == "sink" else "microphone",
                    hints=hints,
                    identifiers={"wpctl_id": idx, "role": section},
                    extra={"default": default, "role": section},
                )
            )
    return found[:20]


def scan_displays() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if shutil.which("hyprctl"):
        raw = _run(["hyprctl", "monitors", "-j"], timeout=3)
        try:
            monitors = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            monitors = []
        if isinstance(monitors, list):
            for m in monitors:
                name = str(m.get("name") or "display")
                w, h = m.get("width"), m.get("height")
                res = f"{w}x{h}" if w and h else ""
                found.append(
                    _make(
                        kind="display",
                        name=f"{name} {res}".strip(),
                        address=name,
                        connected=True,
                        icon="display",
                        hints=[
                            f"Focus: hyprctl dispatch focusmonitor {name}",
                            "Brightness: brightnessctl set 50%",
                        ],
                        identifiers={"connector": name},
                        extra={"refresh": m.get("refreshRate"), "focused": m.get("focused")},
                    )
                )
            if found:
                return found[:12]

    drm = Path("/sys/class/drm")
    if drm.is_dir():
        for card in sorted(drm.glob("card*-*")):
            status = _read(card / "status")
            if status not in {"connected", "disconnected"}:
                continue
            name = card.name.split("-", 1)[-1] if "-" in card.name else card.name
            enabled = status == "connected"
            hints = ["Brightness: brightnessctl set 50%"]
            if shutil.which("xrandr"):
                hints.append(f"xrandr --output {name} --auto" if enabled else f"xrandr --output {name} --off")
            found.append(
                _make(
                    kind="display",
                    name=name,
                    address=name,
                    connected=enabled,
                    icon="display",
                    hints=hints,
                    identifiers={"connector": name, "sysfs": str(card)},
                    extra={"drm_status": status},
                )
            )
        if found:
            return found[:12]

    if shutil.which("xrandr"):
        for line in _run(["xrandr", "--query"], timeout=3).splitlines():
            m = re.match(r"^(\S+)\s+(connected|disconnected)\b(.*)$", line)
            if not m:
                continue
            name, st, rest = m.group(1), m.group(2), m.group(3).strip()
            found.append(
                _make(
                    kind="display",
                    name=f"{name} {rest}".strip()[:80],
                    address=name,
                    connected=st == "connected",
                    hints=[f"xrandr --output {name} --auto", f"xrandr --output {name} --off"],
                    identifiers={"connector": name},
                )
            )
    return found[:12]


def scan_hid() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    by_id = Path("/dev/input/by-id")
    if by_id.is_dir():
        seen: set[str] = set()
        for link in sorted(by_id.iterdir()):
            if "hidraw" in link.name:
                continue
            if link.name.endswith(("-mouse", "-kbd")) and "event-" not in link.name:
                continue
            name = link.name.replace("usb-", "").replace("-event-", " ")
            name = name.replace("-if", " if").replace("_", " ")
            low = name.lower()
            if any(b in low for b in _BORING_HID):
                continue
            key = re.sub(r"\s+", " ", name).strip().lower()
            if key in seen:
                continue
            seen.add(key)
            kind_hint = "keyboard" if "kbd" in link.name else "mouse" if "mouse" in link.name else "hid"
            hints = ["HID input — plug-and-play; no explicit connect needed."]
            mouseish = any(w in low for w in ("mouse", "deathadder"))
            if kind_hint == "keyboard" and not mouseish:
                hints = _lighting_control_hints() or hints
            found.append(
                _make(
                    kind="hid",
                    name=name,
                    address=str(link),
                    connected=True,
                    icon=kind_hint,
                    hints=hints,
                    identifiers={"dev": str(link), "hid_role": kind_hint},
                    extra={"hid_role": kind_hint},
                )
            )
        if found:
            return found[:24]

    inp = Path("/sys/class/input")
    if inp.is_dir():
        seen_names: set[str] = set()
        for ev in sorted(inp.glob("event*")):
            nm = _read(ev / "device" / "name")
            if not nm or nm.lower() in _BORING_HID or nm.lower() in seen_names:
                continue
            seen_names.add(nm.lower())
            found.append(
                _make(
                    kind="hid",
                    name=nm,
                    address=ev.name,
                    connected=True,
                    identifiers={"sysfs": str(ev)},
                )
            )
    return found[:24]


def _lighting_control_hints() -> list[str]:
    hints: list[str] = []
    if shutil.which("openrgb"):
        hints.append(
            "RGB: peripherals action=control command=lighting value=rainbow "
            "(OpenRGB: openrgb --device <n> --mode rainbow)"
        )
    if shutil.which("polychromatic-cli"):
        hints.append("RGB: polychromatic-cli -d keyboard -o spectrum")
    if shutil.which("razer-cli"):
        hints.append("RGB: razer-cli effect spectrum")
    if shutil.which("brightnessctl"):
        hints.append("Backlight: brightnessctl --device='*:kbd_backlight' set 50%")
    if not hints:
        hints.append(
            "Lighting: install OpenRGB or polychromatic, then peripherals "
            "action=control command=lighting value=rainbow."
        )
    return hints


def _kbd_led_dirs() -> list[Path]:
    root = Path("/sys/class/leds")
    if not root.is_dir():
        return []
    out: list[Path] = []
    for d in sorted(root.iterdir()):
        low = d.name.lower()
        if "kbd" in low and "backlight" in low:
            out.append(d)
        elif "keyboard" in low and any(x in low for x in ("backlight", "rgb", "led")):
            out.append(d)
    return out


def scan_lighting() -> list[dict[str, Any]]:
    """RGB controllers and keyboard backlights as first-class peripherals."""
    found: list[dict[str, Any]] = []

    for d in _kbd_led_dirs():
        brightness = _read(d / "brightness")
        max_b = _read(d / "max_brightness")
        level = ""
        try:
            if brightness and max_b and int(max_b) > 0:
                level = f"{round(100 * int(brightness) / int(max_b))}%"
        except ValueError:
            level = brightness
        hints = [
            f"Backlight: peripherals action=control command=brightness value=50% (now {level or '?'})",
            f"brightnessctl --device='{d.name}' set 50%",
        ]
        found.append(
            _make(
                kind="lighting",
                name=d.name.replace("::", " ").replace(":", " ").replace("_", " "),
                address=d.name,
                connected=True,
                icon="keyboard",
                hints=hints,
                identifiers={"sysfs_led": str(d), "brightnessctl": d.name},
                extra={"brightness": brightness, "max_brightness": max_b, "kind": "backlight"},
            )
        )

    if shutil.which("openrgb"):
        raw = _run(["openrgb", "--list-devices"], timeout=8) or _run(["openrgb", "-l"], timeout=8)
        parsed = 0
        for line in (raw or "").splitlines():
            m = re.match(r"^(\d+):\s+(.+)$", line.strip())
            if not m:
                continue
            idx, name = m.group(1), m.group(2).strip()
            found.append(
                _make(
                    kind="lighting",
                    name=name,
                    address=f"openrgb:{idx}",
                    connected=True,
                    icon="rgb",
                    hints=[
                        f"RGB: peripherals action=control command=lighting value=rainbow",
                        f"openrgb --device {idx} --mode rainbow",
                    ],
                    identifiers={"openrgb_id": idx},
                    extra={"provider": "openrgb", "index": idx},
                )
            )
            parsed += 1
        if parsed == 0:
            found.append(
                _make(
                    kind="lighting",
                    name="OpenRGB controller",
                    address="openrgb",
                    connected=bool(raw),
                    icon="rgb",
                    hints=[
                        "RGB: peripherals action=control command=lighting value=rainbow",
                        "openrgb --list-devices ; openrgb --device 0 --mode rainbow",
                    ],
                    identifiers={"openrgb_id": "0"},
                    extra={"provider": "openrgb", "list": (raw or "")[:800]},
                )
            )

    if shutil.which("polychromatic-cli") and not any(
        (p.get("extra") or {}).get("provider") == "polychromatic" for p in found
    ):
        found.append(
            _make(
                kind="lighting",
                name="OpenRazer keyboard",
                address="polychromatic",
                connected=True,
                icon="keyboard",
                hints=[
                    "RGB: peripherals action=control command=lighting value=rainbow",
                    "polychromatic-cli -d keyboard -o spectrum",
                ],
                identifiers={"polychromatic": "keyboard"},
                extra={"provider": "polychromatic"},
            )
        )

    if shutil.which("razer-cli") and not any(
        (p.get("extra") or {}).get("provider") in {"polychromatic", "razer"} for p in found
    ):
        found.append(
            _make(
                kind="lighting",
                name="Razer lighting",
                address="razer-cli",
                connected=True,
                icon="keyboard",
                hints=[
                    "RGB: peripherals action=control command=lighting value=rainbow",
                    "razer-cli effect spectrum",
                ],
                identifiers={"razer_cli": "1"},
                extra={"provider": "razer"},
            )
        )

    found.extend(scan_openrazer())
    found.extend(scan_asus_nkey())

    if not found and _lighting_control_hints():
        found.append(
            _make(
                kind="lighting",
                name="Keyboard / RGB lighting",
                address="lighting",
                connected=True,
                icon="keyboard",
                hints=_lighting_control_hints(),
                identifiers={},
                extra={"provider": "generic"},
            )
        )
    return found[:16]


_RAZER_DRIVERS = ("razekbd", "razermouse", "razerkraken", "razeraccessory")

_OPENRAZER_EFFECTS = {
    "rainbow": (
        "matrix_effect_spectrum",
        "logo_matrix_effect_spectrum",
        "scroll_matrix_effect_spectrum",
        "left_matrix_effect_spectrum",
        "right_matrix_effect_spectrum",
        "matrix_effect_wave",
        "logo_matrix_effect_breath",
        "matrix_effect_breath",
    ),
    "spectrum": (
        "matrix_effect_spectrum",
        "logo_matrix_effect_spectrum",
        "logo_matrix_effect_breath",
        "matrix_effect_breath",
    ),
    "wave": ("matrix_effect_wave", "matrix_effect_spectrum"),
    "breathing": ("matrix_effect_breath", "logo_matrix_effect_breath"),
    "off": (
        "matrix_effect_none",
        "logo_matrix_effect_none",
        "scroll_matrix_effect_none",
    ),
    "on": (
        "matrix_effect_static",
        "logo_matrix_effect_static",
        "matrix_effect_spectrum",
    ),
    "static": ("matrix_effect_static", "logo_matrix_effect_static"),
}


def _write_sysfs(path: Path, data: bytes | str) -> tuple[bool, str]:
    try:
        raw = data.encode() if isinstance(data, str) else data
        path.write_bytes(raw)
        return True, f"Wrote {path.name}"
    except OSError as exc:
        return False, f"{path.name}: {exc}"


def scan_openrazer() -> list[dict[str, Any]]:
    """OpenRazer kernel devices (keyboards, mice) with sysfs lighting controls."""
    found: list[dict[str, Any]] = []
    root = Path("/sys/bus/hid/drivers")
    for drv in _RAZER_DRIVERS:
        d = root / drv
        if not d.is_dir():
            continue
        for node in sorted(d.iterdir()):
            if not node.is_dir() or node.name in {"module", "bind", "unbind", "new_id"}:
                continue
            effects = [
                p.name
                for p in node.iterdir()
                if "effect" in p.name or p.name.endswith("_led_brightness")
            ]
            if not effects:
                continue
            dtype = _read(node / "device_type") or node.name
            serial = _read(node / "device_serial")
            role = (
                "keyboard"
                if "kbd" in drv or "keyboard" in dtype.lower()
                else "mouse"
                if "mouse" in drv or "mouse" in dtype.lower()
                else "razer"
            )
            caps = ", ".join(effects[:8])
            found.append(
                _make(
                    kind="lighting",
                    name=dtype,
                    address=serial or node.name,
                    connected=True,
                    icon=role,
                    vendor="Razer",
                    hints=[
                        f"OpenRazer {role}: peripherals action=control command=lighting "
                        f"value=rainbow|breathing|off ({caps})",
                    ],
                    identifiers={
                        "openrazer_sysfs": str(node),
                        "razer_role": role,
                        "serial": serial,
                    },
                    extra={"provider": "openrazer", "driver": drv, "effects": effects},
                )
            )
    return found[:12]


def scan_asus_nkey() -> list[dict[str, Any]]:
    """ASUS ROG N-KEY / Aura RGB keyboard (USB 0b05:19b6 and cousins)."""
    node = _asus_aura_hidraw()
    if not node:
        return []
    return [
        _make(
            kind="lighting",
            name="ASUS ROG Aura keyboard",
            address=str(node),
            vendor="ASUS",
            connected=True,
            icon="keyboard",
            hints=[
                "RGB: peripherals action=control command=lighting value=rainbow "
                "(Aura HID color cycle on the N-KEY device)",
            ],
            identifiers={"aura_hidraw": str(node)},
            extra={"provider": "asus-aura", "kind": "rgb"},
        )
    ]


def _apply_openrazer_sysfs(p: dict[str, Any], effect: str) -> tuple[bool, str]:
    ident = p.get("identifiers") or {}
    node_s = ident.get("openrazer_sysfs")
    if not node_s:
        return False, ""
    node = Path(str(node_s))
    if not node.is_dir():
        return False, f"OpenRazer path missing: {node}"
    files = _OPENRAZER_EFFECTS.get(effect) or _OPENRAZER_EFFECTS.get("on") or ()
    last = "No matching OpenRazer effect file."
    for fname in files:
        path = node / fname
        if not path.exists():
            continue
        payload: bytes | str = "1"
        if "static" in fname:
            payload = bytes([255, 255, 255])
        elif "wave" in fname:
            payload = "1"
        ok, out = _write_sysfs(path, payload)
        last = out
        if not ok:
            continue
        bright = node / "logo_led_brightness"
        if effect != "off" and bright.exists():
            _write_sysfs(bright, "255")
        elif effect == "off":
            if bright.exists():
                _write_sysfs(bright, "0")
            kbd = node / "matrix_brightness"
            if kbd.exists():
                _write_sysfs(kbd, "0")
        note = ""
        if effect in {"rainbow", "spectrum"} and "spectrum" not in fname:
            note = (
                " This Razer device has no rainbow/spectrum mode; "
                f"I set {fname} instead."
            )
        role = ident.get("razer_role") or "device"
        return True, f"OpenRazer {role} ({p.get('name')}): {fname}.{note}"
    return False, last


def resolve_lighting(target: str, items: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    pool = items if items is not None else scan_lighting()
    helper = PeripheralLearner.__new__(PeripheralLearner)
    helper._items = pool
    helper.memory = type("M", (), {"list_peripherals": lambda self: pool})()
    return helper.resolve(target)


def control_lighting_direct(target: str, command: str, value: str = "") -> tuple[bool, str, dict[str, Any] | None]:
    """Act on live lighting devices without a full peripheral rescan."""
    items = scan_lighting()
    p = resolve_lighting(target, items)
    if p is None and items:
        p = items[0]
    if p is None:
        return False, f"No lighting device matching '{target}'.", None
    ok, out = control_peripheral(p, command, value)
    # Same physical ROG keyboard: also hit Aura if we only resolved the WMI backlight.
    if not (p.get("identifiers") or {}).get("aura_hidraw"):
        aura = next((i for i in items if (i.get("identifiers") or {}).get("aura_hidraw")), None)
        if aura is not None:
            a_ok, a_out = control_peripheral(aura, command, value)
            out = f"{out}\n{a_out}"
            ok = ok or a_ok
            if a_ok:
                p = aura
    return ok, out, p


def scan_cameras() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for node in sorted(Path("/dev").glob("video*")):
        if not node.name[5:].isdigit():
            continue
        name = node.name
        extra: dict[str, str] = {}
        if shutil.which("udevadm"):
            props = _run(["udevadm", "info", "--query=property", f"--name={node}"], timeout=3)
            for line in props.splitlines():
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k in {"ID_V4L_PRODUCT", "ID_MODEL", "ID_VENDOR", "ID_SERIAL", "ID_V4L_CAPABILITIES"}:
                    extra[k] = v
            caps = extra.get("ID_V4L_CAPABILITIES", "")
            if caps and "capture" not in caps:
                continue
            name = extra.get("ID_V4L_PRODUCT") or extra.get("ID_MODEL") or name
        found.append(
            _make(
                kind="camera",
                name=str(name),
                address=str(node),
                vendor=extra.get("ID_VENDOR", ""),
                serial=extra.get("ID_SERIAL", ""),
                connected=True,
                icon="camera",
                hints=[f"Capture: ffmpeg -f v4l2 -i {node} ...", f"Info: v4l2-ctl --device={node} --all"],
                identifiers={"dev": str(node)},
                extra=extra,
            )
        )
    return found[:8]


def scan_printers() -> list[dict[str, Any]]:
    if not shutil.which("lpstat"):
        return []
    found: list[dict[str, Any]] = []
    default = ""
    dest = _run(["lpstat", "-d"], timeout=3)
    m = re.search(r":\s*(\S+)", dest)
    if m:
        default = m.group(1)
    for line in _run(["lpstat", "-p"], timeout=4).splitlines():
        m = re.match(r"printer\s+(\S+)\s+(is\s+idle|now printing|disabled|enabled)?", line)
        if not m:
            continue
        name = m.group(1)
        idle = "idle" in line.lower()
        found.append(
            _make(
                kind="printer",
                name=name + (" (default)" if name == default else ""),
                address=name,
                connected="disabled" not in line.lower(),
                icon="printer",
                hints=[f"Print: lp -d {name} <file>", f"Queue: lpq -P {name}", f"Cancel: cancel -a {name}"],
                identifiers={"queue": name},
                extra={"default": name == default, "idle": idle, "raw": line[:200]},
            )
        )
    return found[:12]


def scan_storage() -> list[dict[str, Any]]:
    raw = _run(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,TRAN,RM,HOTPLUG,SERIAL,UUID"], timeout=4)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return []
    found: list[dict[str, Any]] = []

    def walk(dev: dict[str, Any], parent_tran: str = "") -> None:
        dtype = (dev.get("type") or "").lower()
        tran = (dev.get("tran") or parent_tran or "").lower()
        removable = bool(dev.get("rm") or dev.get("hotplug"))
        interesting = removable or tran in {"usb", "mmc"} or dtype == "rom"
        name = dev.get("name") or ""
        model = (dev.get("model") or "").strip()
        children = dev.get("children") or []
        if dtype in {"disk", "rom"} and interesting and name:
            mounted = any((c.get("mountpoint") for c in children)) or bool(dev.get("mountpoint"))
            node = f"/dev/{name}"
            hints = []
            if shutil.which("udisksctl"):
                hints.append(f"Mount: udisksctl mount -b {node}")
                hints.append(f"Unmount: udisksctl unmount -b {node}")
            found.append(
                _make(
                    kind="storage",
                    name=f"{model or name} ({dev.get('size') or '?'})",
                    address=node,
                    serial=dev.get("serial") or "",
                    connected=mounted,
                    icon="disk",
                    hints=hints or [f"lsblk {node}"],
                    identifiers={"dev": node, "lsblk": name},
                    extra={"tran": tran, "removable": removable, "fstype": dev.get("fstype")},
                )
            )
        for child in children:
            walk(child, tran)

    for d in data.get("blockdevices") or []:
        walk(d)
    # Prefer removable; if none, keep USB/mmc only (already filtered).
    return found[:16]


def _nmcli_fields(line: str) -> list[str]:
    """Split nmcli terse output, honouring backslash-escaped colons (BSSIDs)."""
    parts: list[str] = []
    buf: list[str] = []
    esc = False
    for ch in line:
        if esc:
            buf.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def scan_wifi() -> list[dict[str, Any]]:
    if not shutil.which("nmcli"):
        return []
    found: list[dict[str, Any]] = []
    active = _run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE,STATE", "connection", "show", "--active"], timeout=4)
    active_ssids = set()
    for line in active.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and "wireless" in parts[1]:
            active_ssids.add(parts[0])

    listed = _run(
        ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE,BSSID", "device", "wifi", "list", "--rescan", "no"],
        timeout=6,
    )
    seen: set[str] = set()
    rows: list[tuple[int, dict[str, Any]]] = []
    for line in listed.splitlines():
        parts = _nmcli_fields(line)
        if len(parts) < 3:
            continue
        ssid = parts[0].strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        try:
            signal = int(parts[1] or 0)
        except ValueError:
            signal = 0
        security = parts[2] if len(parts) > 2 else ""
        in_use = "*" in (parts[3] if len(parts) > 3 else "") or ssid in active_ssids
        bssid = parts[4] if len(parts) > 4 else ""
        rows.append(
            (
                signal,
                _make(
                    kind="wifi",
                    name=ssid,
                    address=bssid or ssid,
                    connected=in_use,
                    paired=ssid in active_ssids,
                    icon="wifi",
                    hints=[
                        f"Connect (known): nmcli connection up '{ssid}'",
                        f"Connect (new): nmcli dev wifi connect '{ssid}' password <pass>",
                    ],
                    identifiers={"ssid": ssid},
                    extra={"signal": signal, "security": security},
                ),
            )
        )
    rows.sort(key=lambda x: (-int(x[1].get("connected")), -x[0]))
    found = [r[1] for r in rows[:15]]
    return found


def scan_network() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if shutil.which("nmcli"):
        for line in _run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            timeout=4,
        ).splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            dev, typ, state = parts[0], parts[1], parts[2]
            conn = parts[3] if len(parts) > 3 else ""
            if typ in {"loopback", "bridge", "tun", "vlan", "wifi-p2p"} or dev.startswith(
                ("lo", "docker", "br-", "veth", "/net/")
            ):
                continue
            found.append(
                _make(
                    kind="network",
                    name=f"{dev} ({typ}" + (f" · {conn}" if conn else "") + ")",
                    address=dev,
                    connected=state == "connected",
                    icon=typ,
                    hints=[
                        f"Status: nmcli device show {dev}",
                        f"Disconnect: nmcli device disconnect {dev}",
                        f"Connect: nmcli device connect {dev}",
                    ],
                    identifiers={"iface": dev, "nm_type": typ},
                    extra={"state": state, "connection": conn, "type": typ},
                )
            )

    if shutil.which("avahi-browse"):
        dump = _run(["avahi-browse", "-atkpr"], timeout=6)
        seen: set[str] = set()
        for line in dump.splitlines():
            if not line.startswith("="):
                continue
            parts = line.split(";")
            if len(parts) < 8:
                continue
            sname, stype, host, addr = parts[3], parts[4], parts[6], parts[7]
            key = f"{sname}|{addr}"
            if key in seen or not addr or addr.startswith("fe80"):
                continue
            seen.add(key)
            found.append(
                _make(
                    kind="network",
                    name=sname.replace("\\032", " "),
                    address=addr,
                    connected=True,
                    icon="mdns",
                    hints=[f"mDNS {stype} at {host} ({addr})"],
                    identifiers={"host": host, "mdns_type": stype, "ip": addr},
                    extra={"service": stype, "host": host},
                )
            )
            if len(seen) >= 16:
                break
    return found[:28]


def scan_radios() -> list[dict[str, Any]]:
    if not shutil.which("rfkill"):
        return []
    found: list[dict[str, Any]] = []
    raw = _run(["rfkill", "-J"], timeout=3)
    devices = []
    if raw:
        try:
            parsed = json.loads(raw)
            devices = parsed if isinstance(parsed, list) else parsed.get("", parsed.get("rfkill", []))
            if isinstance(parsed, dict):
                for v in parsed.values():
                    if isinstance(v, list):
                        devices = v
                        break
        except json.JSONDecodeError:
            devices = []
    if not devices:
        for line in _run(["rfkill", "list"], timeout=3).splitlines():
            m = re.match(r"(\d+):\s+([^:]+):\s+(\S+)", line)
            if m:
                found.append(
                    _make(
                        kind="radio",
                        name=m.group(2).strip(),
                        address=m.group(1),
                        connected=True,
                        hints=["rfkill unblock all", "rfkill block bluetooth"],
                        identifiers={"rfkill_id": m.group(1)},
                    )
                )
        return found[:8]
    for d in devices:
        ident = str(d.get("id", d.get("identifier", "")))
        name = str(d.get("device") or d.get("type") or ident)
        soft = str(d.get("soft") or "").lower() in {"blocked", "1", "true"}
        hard = str(d.get("hard") or "").lower() in {"blocked", "1", "true"}
        found.append(
            _make(
                kind="radio",
                name=name,
                address=ident,
                connected=not (soft or hard),
                hints=[
                    f"Enable: rfkill unblock {d.get('type', ident)}",
                    f"Disable: rfkill block {d.get('type', ident)}",
                ],
                identifiers={"rfkill_id": ident, "type": str(d.get("type", ""))},
                extra={"soft_blocked": soft, "hard_blocked": hard},
            )
        )
    return found[:8]


def scan_all(*, discover: bool = False) -> list[dict[str, Any]]:
    """Inventory currently attached / known peripherals. discover=True runs BT inquiry."""
    scanners = [
        scan_usb,
        scan_audio,
        scan_displays,
        scan_hid,
        scan_lighting,
        scan_cameras,
        scan_printers,
        scan_storage,
        scan_wifi,
        scan_network,
        scan_radios,
    ]
    found: list[dict[str, Any]] = []
    if discover:
        found.extend(discover_bluetooth())
    else:
        found.extend(scan_bluetooth())
    for fn in scanners:
        try:
            found.extend(fn())
        except Exception:  # noqa: BLE001
            continue
    # Stable unique ids — if collision, suffix.
    seen: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for p in found:
        pid = p["id"]
        n = seen.get(pid, 0)
        seen[pid] = n + 1
        if n:
            p = {**p, "id": f"{pid}-{n}"}
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

_VOLUME_RE = re.compile(r"^\d{1,3}%?$")


def _bt_mac(p: dict[str, Any]) -> str:
    return (p.get("identifiers") or {}).get("mac") or p.get("address") or ""


def _normalize_effect(command: str, value: str) -> str:
    raw = (value or command or "").strip().lower()
    if command in {"lighting", "light", "lights", "rgb", "led", "backlight"}:
        raw = (value or "on").strip().lower()
    aliases = {
        "rainbow": "rainbow",
        "spectrum": "spectrum",
        "cycle": "spectrum",
        "wave": "wave",
        "breathing": "breathing",
        "breathe": "breathing",
        "pulse": "breathing",
        "static": "static",
        "solid": "static",
        "off": "off",
        "black": "off",
        "0": "off",
        "on": "on",
        "white": "on",
    }
    return aliases.get(raw, raw)


def _asus_aura_hidraw() -> Path | None:
    root = Path("/sys/class/hidraw")
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir()):
        uevent = _read(d / "device" / "uevent")
        if "00000B05" not in uevent.upper() and "0B05" not in uevent:
            continue
        if not any(pid in uevent.upper() for pid in ("19B6", "1854", "1869", "1866", "1A30", "18C6")):
            continue
        desc = d / "device" / "report_descriptor"
        try:
            raw = desc.read_bytes()
        except OSError:
            raw = b""
        if raw and b"\x85\x5d" not in raw:
            continue
        node = Path("/dev") / d.name
        if node.exists():
            return node
    return None


def _aura_pad(data: bytes, length: int = 64) -> bytes:
    if len(data) >= length:
        return data[:length]
    return data + bytes(length - len(data))


def _aura_mode_packet(mode: int, r: int = 255, g: int = 0, b: int = 0, speed: int = 0xEB) -> bytes:
    msg = bytearray(64)
    msg[0], msg[1], msg[3] = 0x5D, 0xB3, mode
    msg[4], msg[5], msg[6], msg[7] = r, g, b, speed
    return bytes(msg)


def _aura_messages(effect: str) -> list[bytes]:
    """64-byte Aura Core packets (OpenRGB / asusctl / hid-asus report 0x5A+0x5D)."""
    level = 0 if effect == "off" else 3
    if effect == "rainbow":
        body = _aura_mode_packet(3, 0xFF, 0, 0)  # rainbow wave
    elif effect in {"spectrum", "cycle"}:
        body = _aura_mode_packet(2, 0xFF, 0, 0)  # color cycle
    elif effect == "wave":
        body = _aura_mode_packet(3, 0xFF, 0, 0)
    elif effect in {"breathing"}:
        pkt = bytearray(_aura_mode_packet(1, 0xFF, 0xFF, 0xFF))
        pkt[9], pkt[10], pkt[11], pkt[12] = 1, 0x40, 0x40, 0x40
        body = bytes(pkt)
    elif effect == "off":
        body = _aura_mode_packet(0, 0, 0, 0)
    else:
        body = _aura_mode_packet(0, 0xFF, 0xFF, 0xFF)
    ident_5d = _aura_pad(b"]ASUS Tech.Inc.")
    ident_5a = _aura_pad(bytes([0x5A]) + b"ASUS Tech.Inc.\x00")
    return [
        _aura_pad(bytes([0x5D, 0xB9])),
        ident_5d,
        ident_5a,
        _aura_pad(bytes([0x5D, 0x05, 0x20, 0x31, 0x00, 0x10])),
        _aura_pad(bytes([0x5D, 0xBD, 0x01, 0xFF, 0x1F, 0xFF, 0xFF, 0xFF])),
        _aura_pad(bytes([0x5D, 0xBA, 0xC5, 0xC4, level])),
        _aura_pad(bytes([0x5A, 0xBA, 0xC5, 0xC4, level])),
        body,
        _aura_pad(bytes([0x5D, 0xB5])),
        _aura_pad(bytes([0x5D, 0xB4])),
    ]


def _usb_devnode_for_hidraw(hidraw: Path) -> Path | None:
    sysfs = Path("/sys/class/hidraw") / hidraw.name / "device"
    try:
        cur = sysfs.resolve()
    except OSError:
        return None
    for parent in [cur, *cur.parents]:
        bus_f, dev_f = parent / "busnum", parent / "devnum"
        if not (bus_f.is_file() and dev_f.is_file()):
            continue
        try:
            bus = int(bus_f.read_text().strip())
            dev = int(dev_f.read_text().strip())
        except ValueError:
            continue
        node = Path(f"/dev/bus/usb/{bus:03d}/{dev:03d}")
        if node.exists():
            return node
    return None


def _hid_sfeature_ioctl(length: int) -> int:
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x06


def _hidraw_send_feature(path: Path, packets: list[bytes]) -> tuple[bool, str]:
    """HIDIOCSFEATURE only. A hidraw write() can succeed without changing Aura LEDs."""
    import array
    import fcntl

    try:
        fd = os.open(path, os.O_RDWR)
    except OSError as exc:
        return False, str(exc)
    sent = 0
    last = "no packets"
    try:
        for pkt in packets:
            err: OSError | None = None
            for length in (64, 17):
                raw = _aura_pad(pkt, length)
                buf = array.array("B", raw)
                try:
                    fcntl.ioctl(fd, _hid_sfeature_ioctl(length), buf)
                    err = None
                    break
                except OSError as exc:
                    err = exc
            if err is not None:
                return False, f"{path.name}: {err} after {sent} reports"
            sent += 1
            last = f"{path.name} HIDIOCSFEATURE x{sent}"
        return True, last
    finally:
        os.close(fd)


_AURA_USB_SENDER = r"""
import array, ctypes, fcntl, os, sys

def pad(data, n):
    return (data + bytes(n))[:n] if len(data) >= n else data + bytes(n - len(data))

def hid_sfeature(n):
    return (3 << 30) | (n << 16) | (ord("H") << 8) | 6

def send_hidraw(path, packets):
    fd = os.open(path, os.O_RDWR)
    try:
        for pkt in packets:
            ok = False
            last = None
            for length in (64, 17):
                buf = array.array("B", pad(pkt, length))
                try:
                    fcntl.ioctl(fd, hid_sfeature(length), buf)
                    ok = True
                    break
                except OSError as exc:
                    last = exc
            if not ok:
                raise last
        return "hidraw-feature"
    finally:
        os.close(fd)

class Ctrl(ctypes.Structure):
    _fields_ = [
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]

class UsbIoctl(ctypes.Structure):
    _fields_ = [
        ("ifno", ctypes.c_int),
        ("ioctl_code", ctypes.c_int),
        ("data", ctypes.c_void_p),
    ]

USBDEVFS_CONTROL = (3 << 30) | (ctypes.sizeof(Ctrl) << 16) | (ord("U") << 8) | 0
USBDEVFS_IOCTL = (3 << 30) | (ctypes.sizeof(UsbIoctl) << 16) | (ord("U") << 8) | 18
USBDEVFS_CLAIMINTERFACE = (2 << 30) | (4 << 16) | (ord("U") << 8) | 15
USBDEVFS_RELEASEINTERFACE = (2 << 30) | (4 << 16) | (ord("U") << 8) | 16
USBDEVFS_DISCONNECT = (ord("U") << 8) | 22
USBDEVFS_CONNECT = (ord("U") << 8) | 23

def send_usb(path, packets):
    ufd = os.open(path, os.O_RDWR)
    iface = ctypes.c_uint(0)
    try:
        try:
            fcntl.ioctl(ufd, USBDEVFS_IOCTL, UsbIoctl(0, USBDEVFS_DISCONNECT, None))
        except OSError:
            pass
        fcntl.ioctl(ufd, USBDEVFS_CLAIMINTERFACE, iface)
        for pkt in packets:
            raw = pad(pkt, 17)
            buf = ctypes.create_string_buffer(raw, 17)
            wvalues = (0x0300 | raw[0], 0x035D)
            last = None
            ok = False
            for wvalue in wvalues:
                ctrl = Ctrl(0x21, 9, wvalue, 0, 17, 2000, ctypes.addressof(buf))
                try:
                    fcntl.ioctl(ufd, USBDEVFS_CONTROL, ctrl)
                    ok = True
                    break
                except OSError as exc:
                    last = exc
            if not ok:
                raise last
        return "usb-set-report"
    finally:
        try:
            fcntl.ioctl(ufd, USBDEVFS_RELEASEINTERFACE, iface)
        except OSError:
            pass
        try:
            fcntl.ioctl(ufd, USBDEVFS_IOCTL, UsbIoctl(0, USBDEVFS_CONNECT, None))
        except OSError:
            pass
        os.close(ufd)

hid, usb, blob = sys.argv[1], sys.argv[2], sys.argv[3]
packets = [bytes.fromhex(x) for x in blob.split(",") if x]
how, errs = [], []
# USB SET_REPORT with kernel-driver detach is what actually drives Aura on 0b05:19b6.
for name, fn, target in (
    ("usb", send_usb, usb),
    ("hidraw", send_hidraw, hid),
):
    if not target or target == "-":
        continue
    try:
        how.append(fn(target, packets))
    except OSError as exc:
        errs.append(f"{name}: {exc}")
if how:
    print("OK", "+".join(how))
    sys.exit(0)
print("FAIL", "; ".join(errs) or "no transport")
sys.exit(1)
"""


def _sudo_send_aura(hidraw: Path, packets: list[bytes]) -> tuple[bool, str]:
    try:
        password = (store.get().permissions.sudo_password or "").strip()
    except Exception:  # noqa: BLE001
        password = ""
    if not password:
        return False, (
            f"Permission denied opening {hidraw} "
            "(save a sudo password in Systems, or: sudo chmod a+rw "
            f"{hidraw})"
        )
    usb = _usb_devnode_for_hidraw(hidraw)
    blob = ",".join(p.hex() for p in packets)
    cmd = (
        "sudo -S -p '' python3 -c "
        + shlex.quote(_AURA_USB_SENDER)
        + " "
        + shlex.quote(str(hidraw))
        + " "
        + shlex.quote(str(usb) if usb else "-")
        + " "
        + shlex.quote(blob)
    )
    ok, out = _run_shell(cmd, timeout=20, input_text=password + "\n")
    if ok and out.startswith("OK"):
        how = out.split(None, 1)[-1] if " " in out else "privileged"
        return True, f"{hidraw.name} via sudo ({how})"
    return False, out or "sudo Aura send failed"


def _apply_asus_aura(effect: str, hidraw: Path | None = None) -> tuple[bool, str]:
    node = hidraw or _asus_aura_hidraw()
    if node is None:
        return False, "No ASUS Aura N-KEY hidraw device found."
    packets = _aura_messages(effect)
    ok, out = _hidraw_send_feature(node, packets)
    if ok:
        return True, f"Aura {effect} on {out}"
    ok, sudo_out = _sudo_send_aura(node, packets)
    if ok:
        return True, f"Aura {effect} on {sudo_out}"
    return False, f"{out}; {sudo_out}"


def _logind_sessions() -> list[str]:
    ids: list[str] = []
    env_sid = os.environ.get("XDG_SESSION_ID")
    if env_sid:
        ids.append(env_sid)
    raw = _run(["loginctl", "list-sessions", "--no-legend"], timeout=3)
    for line in raw.splitlines():
        parts = line.split()
        if parts:
            ids.append(parts[0])
    return list(dict.fromkeys(ids))


def _logind_set_led(device: str, value: int) -> tuple[bool, str]:
    last = "no logind session"
    for sid in _logind_sessions():
        ok, out = _run_shell(
            "busctl call org.freedesktop.login1 "
            f"/org/freedesktop/login1/session/{sid} "
            "org.freedesktop.login1.Session SetBrightness ssu "
            f"leds {shlex.quote(device)} {int(value)}"
        )
        last = out or f"logind session {sid}"
        if ok:
            return True, f"logind {device}={value} (session {sid})"
    return False, last


def _set_led_brightness(p: dict[str, Any], level: str) -> tuple[bool, str]:
    ident = p.get("identifiers") or {}
    level = (level or "50%").strip() or "50%"
    device = ident.get("brightnessctl") or ident.get("sysfs_led") or "asus::kbd_backlight"
    if device and "/" in str(device):
        device = Path(str(device)).name
    led_path = Path(str(ident.get("sysfs_led") or f"/sys/class/leds/{device}"))
    max_b = 3
    try:
        max_b = int(_read(led_path / "max_brightness") or "3")
    except ValueError:
        max_b = 3
    try:
        pct = int(str(level).rstrip("%"))
        raw = max(0, min(max_b, round(max_b * pct / 100) if pct > max_b else pct))
        if str(level).endswith("%") or pct > max_b:
            raw = max(0, min(max_b, round(max_b * min(pct, 100) / 100)))
    except ValueError:
        raw = max_b

    before = _read(led_path / "brightness")
    logs: list[str] = []
    ok, out = _logind_set_led(device, raw)
    logs.append(out)
    if not ok and shutil.which("brightnessctl"):
        ok, out = _run_shell(f"brightnessctl --device={shlex.quote(device)} set {raw}")
        logs.append(out)
    after = _read(led_path / "brightness")
    if after == str(raw):
        # Pulse if it was already at the target so the user sees a change.
        if before == after and raw > 0:
            _logind_set_led(device, 0)
            time.sleep(0.12)
            _logind_set_led(device, raw)
        return True, f"Keyboard brightness {after}/{max_b} on {device}"
    if ok:
        return False, (
            f"logind reported success but sysfs is still {after or '?'} "
            f"(wanted {raw}). " + " ".join(logs)
        )
    return False, "Could not set backlight brightness. " + " ".join(logs)


def _apply_lighting(p: dict[str, Any], command: str, value: str) -> tuple[bool, str]:
    """Set RGB effect or keyboard backlight on a lighting/HID device."""
    ident = p.get("identifiers") or {}
    effect = _normalize_effect(command, value)
    if effect.endswith("%") or (effect.isdigit() and command in {"lighting", "backlight", "brightness"}):
        return _set_led_brightness(p, value or command)

    logs: list[str] = []
    name_hint = (p.get("name") or "") + " " + str(ident.get("openrgb_id") or "")

    if ident.get("openrazer_sysfs"):
        ok, out = _apply_openrazer_sysfs(p, effect)
        if ok:
            return True, out
        if out:
            logs.append(out)

    keyboardish = (
        bool(ident.get("aura_hidraw") or ident.get("sysfs_led") or ident.get("brightnessctl"))
        or (p.get("extra") or {}).get("kind") in {"backlight", "rgb"}
        or (p.get("extra") or {}).get("provider") == "asus-aura"
        or any(w in (p.get("name") or "").lower() for w in ("keyboard", "asus", "kbd", "n-key", "aura"))
    )
    if keyboardish:
        hid = Path(str(ident["aura_hidraw"])) if ident.get("aura_hidraw") else None
        bright_ok, bright_out = _set_led_brightness(
            p, "0%" if effect == "off" else "100%"
        )
        logs.append(bright_out)
        aura_ok, aura_out = _apply_asus_aura(effect, hid)
        logs.append(aura_out)
        if aura_ok:
            return True, f"{aura_out}. {bright_out}"
        if effect in {"rainbow", "spectrum", "wave", "breathing", "static", "on"}:
            return False, (
                f"RGB did not change ({aura_out}). "
                f"Brightness layer: {bright_out}. "
                "This ROG keyboard's visible colours are Aura on the N-KEY USB device, "
                "not the 0–3 WMI backlight. I need hidraw access — save a sudo password "
                "in Systems, or run: sudo chmod a+rw /dev/hidraw3"
            )
        if bright_ok:
            return True, bright_out

    if effect in {"off", "on"} and (
        ident.get("sysfs_led") or ident.get("brightnessctl") or (p.get("extra") or {}).get("kind") == "backlight"
    ):
        ok, out = _set_led_brightness(p, "0%" if effect == "off" else "100%")
        logs.append(out)
        if ok:
            return ok, out

    rgb_id = ident.get("openrgb_id")
    if shutil.which("openrgb"):
        idx = rgb_id if rgb_id is not None and str(rgb_id) != "" else "0"
        if effect == "off":
            ok, out = _run_shell(f"openrgb --device {idx} --mode static --color 000000", timeout=12)
            logs.append(out)
            if ok:
                return True, out or f"OpenRGB device {idx} off"
        elif effect == "on":
            ok, out = _run_shell(f"openrgb --device {idx} --mode static --color FFFFFF", timeout=12)
            logs.append(out)
            if ok:
                return True, out or f"OpenRGB device {idx} on"
        else:
            modes = _EFFECT_ALIASES.get(effect, [effect])
            for mode in modes:
                ok, out = _run_shell(f'openrgb --device {idx} --mode "{mode}"', timeout=12)
                logs.append(out)
                if ok:
                    return True, out or f"OpenRGB device {idx} mode {mode}"

    if shutil.which("polychromatic-cli"):
        poly = {
            "rainbow": "spectrum",
            "spectrum": "spectrum",
            "wave": "wave",
            "breathing": "breathing",
            "static": "static",
            "off": "off",
            "on": "static",
        }.get(effect, effect)
        device = ident.get("polychromatic") or "keyboard"
        ok, out = _run_shell(f"polychromatic-cli -d {device} -o {poly}", timeout=12)
        logs.append(out)
        if ok:
            return True, out or f"polychromatic {poly}"

    if shutil.which("razer-cli"):
        razer = {
            "rainbow": "spectrum",
            "spectrum": "spectrum",
            "wave": "wave",
            "breathing": "breathing",
            "static": "static",
            "off": "off",
            "on": "static",
        }.get(effect, effect)
        ok, out = _run_shell(f"razer-cli effect {razer}", timeout=12)
        logs.append(out)
        if ok:
            return True, out or f"razer-cli {razer}"

    backlightish = bool(
        ident.get("sysfs_led")
        or ident.get("brightnessctl")
        or (p.get("extra") or {}).get("kind") == "backlight"
        or (p.get("kind") in {"hid", "lighting"} and "kbd" in (p.get("name") or "").lower())
    )
    if backlightish:
        level = "0%" if effect == "off" else (
            value if (value or "").endswith("%") else "100%"
        )
        ok, out = _set_led_brightness(p, level)
        logs.append(out)
        if ok:
            if effect in {"rainbow", "spectrum", "wave", "breathing"}:
                razer_note = ""
                razer_devs = scan_openrazer()
                mice = [d.get("name") for d in razer_devs if (d.get("identifiers") or {}).get("razer_role") == "mouse"]
                kbds = [d.get("name") for d in razer_devs if (d.get("identifiers") or {}).get("razer_role") == "keyboard"]
                if kbds:
                    razer_note = f" Razer keyboard(s) also present: {', '.join(kbds)}."
                elif mice:
                    razer_note = (
                        f" No Razer keyboard is attached — OpenRazer only sees "
                        f"{', '.join(mice)}, which is a mouse (no rainbow keyboard matrix)."
                    )
                else:
                    razer_note = " No RGB keyboard controller is available on this machine."
                return True, (
                    f"{out}\nThis LED is a brightness backlight, not RGB. I set it to full."
                    f"{razer_note}"
                )
            return True, out

    missing = []
    if not shutil.which("openrgb"):
        missing.append("openrgb")
    if not shutil.which("polychromatic-cli"):
        missing.append("polychromatic-cli")
    if not shutil.which("razer-cli"):
        missing.append("razer-cli")
    detail = "\n".join(x for x in logs if x)[:2000]
    hint = (
        f"Couldn't set lighting on {p.get('name') or name_hint} to '{effect}'. "
        + (f"Missing: {', '.join(missing)}. Install via device_control shell, then retry. " if missing else "")
        + (f"Output:\n{detail}" if detail else "")
    )
    return False, hint.strip()


def control_peripheral(
    p: dict[str, Any],
    command: str,
    value: str = "",
    password: str = "",
) -> tuple[bool, str]:
    """Execute a named control against a peripheral. Never echo secrets."""
    command = (command or "").strip().lower()
    kind = p.get("kind") or ""
    ident = p.get("identifiers") or {}
    addr = p.get("address") or ""

    if command in {"connect", "disconnect", "pair", "unpair", "trust"} and kind == "bluetooth":
        mac = _bt_mac(p)
        if not mac:
            return False, "No Bluetooth MAC on this peripheral."
        verb = {"unpair": "remove"}.get(command, command)
        if command == "connect":
            _run(["bluetoothctl", "--timeout", "3", "power", "on"], timeout=5)
        ok, out = _run_shell(f"bluetoothctl --timeout 12 {verb} {mac}", timeout=14)
        return ok, out or f"bluetoothctl {verb} {mac}"

    if command in {"connect", "disconnect"} and kind == "wifi":
        ssid = ident.get("ssid") or p.get("name") or ""
        if command == "disconnect":
            ok, out = _run_shell(f"nmcli connection down '{ssid}'", timeout=10)
            if not ok:
                iface = ident.get("iface") or ""
                if iface:
                    ok, out = _run_shell(f"nmcli device disconnect {iface}", timeout=10)
            return ok, out
        if password:
            ok, out = _run_shell(
                f"nmcli dev wifi connect '{ssid}' password '{password}'",
                timeout=20,
            )
            out = out.replace(password, "********")
            return ok, out
        ok, out = _run_shell(f"nmcli connection up '{ssid}'", timeout=15)
        if not ok:
            ok, out = _run_shell(f"nmcli dev wifi connect '{ssid}'", timeout=20)
        return ok, out

    if command in {"connect", "disconnect"} and kind == "network":
        iface = ident.get("iface") or addr
        verb = "connect" if command == "connect" else "disconnect"
        return _run_shell(f"nmcli device {verb} {iface}", timeout=12)

    if command in {"mount", "unmount", "connect", "disconnect"} and kind == "storage":
        node = ident.get("dev") or addr
        verb = "mount" if command in {"mount", "connect"} else "unmount"
        if shutil.which("udisksctl"):
            return _run_shell(f"udisksctl {verb} -b {node}", timeout=15)
        return False, "udisksctl not available."

    if command in {"volume", "mute", "unmute", "default"} and kind == "audio":
        pactl_name = ident.get("pactl_name")
        wp_id = ident.get("wpctl_id")
        role = ident.get("role") or "sink"
        if command == "volume":
            vol = value.strip() or "50%"
            if not vol.endswith("%"):
                vol += "%"
            if not _VOLUME_RE.match(vol.replace("%", "") + "%"):
                return False, "Volume must be like 50 or 50%."
            if pactl_name and shutil.which("pactl"):
                target = "sink" if role == "sink" else "source"
                return _run_shell(f"pactl set-{target}-volume {pactl_name} {vol}")
            if wp_id and shutil.which("wpctl"):
                return _run_shell(f"wpctl set-volume {wp_id} {vol}")
        if command in {"mute", "unmute"}:
            flag = "1" if command == "mute" else "0"
            if command == "mute" and value.lower() in {"toggle", ""}:
                flag = "toggle"
            if pactl_name and shutil.which("pactl"):
                target = "sink" if role == "sink" else "source"
                return _run_shell(f"pactl set-{target}-mute {pactl_name} {flag}")
            if wp_id and shutil.which("wpctl"):
                arg = "toggle" if flag == "toggle" else flag
                return _run_shell(f"wpctl set-mute {wp_id} {arg}")
        if command == "default":
            if pactl_name and shutil.which("pactl"):
                target = "sink" if role == "sink" else "source"
                return _run_shell(f"pactl set-default-{target} {pactl_name}")
            if wp_id and shutil.which("wpctl"):
                return _run_shell(f"wpctl set-default {wp_id}")
        return False, "No audio control utility (pactl/wpctl) for this device."

    if command in {"brightness"} and kind in {"display", "usb", "hid", "lighting"}:
        ident = p.get("identifiers") or {}
        name_l = (p.get("name") or "").lower()
        is_keyboard = (
            kind in {"hid", "lighting"}
            or bool(ident.get("sysfs_led") or ident.get("brightnessctl") or ident.get("openrgb_id"))
            or ident.get("hid_role") == "keyboard"
            or any(w in name_l for w in ("keyboard", "kbd", "backlight"))
        )
        if is_keyboard:
            return _set_led_brightness(p, value.strip() or "50%")
        level = value.strip() or "50%"
        if shutil.which("brightnessctl"):
            return _run_shell(f"brightnessctl set {level}")
        return False, "brightnessctl not available."

    if command in _LIGHTING_COMMANDS and kind != "display":
        return _apply_lighting(p, command, value)

    if command in {"enable", "disable", "on", "off"} and kind == "display":
        conn = ident.get("connector") or addr
        if shutil.which("hyprctl") and command in {"disable", "off"}:
            return _run_shell("hyprctl dispatch dpms off")
        if shutil.which("hyprctl") and command in {"enable", "on"}:
            return _run_shell("hyprctl dispatch dpms on")
        if shutil.which("xrandr"):
            flag = "--auto" if command in {"enable", "on"} else "--off"
            return _run_shell(f"xrandr --output {conn} {flag}")
        return False, "No display control utility found."

    if command in {"play", "pause", "play-pause", "next", "previous", "stop"}:
        if shutil.which("playerctl"):
            verb = "play-pause" if command == "pause" and not value else command
            return _run_shell(f"playerctl {verb}")
        return False, "playerctl not available."

    if command in {"power", "block", "unblock"} and kind == "radio":
        typ = ident.get("type") or p.get("name") or ""
        verb = "unblock" if command in {"power", "unblock"} and value.lower() not in {"off", "block"} else "block"
        if value.lower() in {"off", "0"}:
            verb = "block"
        if value.lower() in {"on", "1"}:
            verb = "unblock"
        return _run_shell(f"rfkill {verb} {typ or ident.get('rfkill_id', '')}")

    if command == "print" and kind == "printer":
        path = value.strip()
        if not path:
            return False, "control value must be a file path to print."
        queue = ident.get("queue") or addr
        return _run_shell(f"lp -d {queue} {path}", timeout=20)

    hints = p.get("control_hints") or []
    hint_txt = "\n".join(f"  • {h}" for h in hints) or "  (none recorded)"
    return False, (
        f"Don't know how to '{command}' on {p.get('name')} ({kind}). "
        f"Known hints:\n{hint_txt}"
    )


def inspect_peripheral(p: dict[str, Any]) -> dict[str, Any]:
    """Deep-dive a single peripheral and return extra facts."""
    facts: list[str] = []
    extra: dict[str, Any] = dict(p.get("extra") or {})
    kind = p.get("kind")
    ident = p.get("identifiers") or {}

    if kind == "bluetooth":
        mac = _bt_mac(p)
        info = _run(["bluetoothctl", "--timeout", "4", "info", mac], timeout=6) if mac else ""
        if info:
            extra["info"] = info[:2500]
            facts.append(info.split("\n")[0][:200])
            for line in info.splitlines():
                s = line.strip()
                if s.startswith(("Name:", "Alias:", "Icon:", "Paired:", "Connected:", "Trusted:", "Battery")):
                    facts.append(s)

    if kind == "usb":
        vid = ident.get("vid")
        pid = ident.get("pid")
        if vid and pid:
            extra["lsusb"] = _run(["lsusb", "-d", f"{vid}:{pid}", "-v"], timeout=6)[:2500]
        sysfs = ident.get("sysfs")
        if sysfs:
            extra["uevent"] = _read(Path(sysfs) / "uevent")[:1500]

    if kind == "audio" and ident.get("pactl_name") and shutil.which("pactl"):
        extra["pactl"] = _run(["pactl", "list", "sinks"], timeout=5)[:2000]

    if kind == "camera" and ident.get("dev"):
        if shutil.which("v4l2-ctl"):
            extra["v4l2"] = _run(["v4l2-ctl", f"--device={ident['dev']}", "--all"], timeout=5)[:2000]
        extra["udev"] = _run(["udevadm", "info", f"--name={ident['dev']}"], timeout=4)[:1500]

    if kind == "storage" and ident.get("dev"):
        extra["lsblk"] = _run(["lsblk", "-f", ident["dev"]], timeout=4)[:1500]

    if kind == "printer" and ident.get("queue"):
        extra["lpstat"] = _run(["lpstat", "-l", "-p", ident["queue"]], timeout=4)[:1500]

    if kind == "network":
        iface = ident.get("iface")
        if iface and shutil.which("nmcli"):
            extra["nmcli"] = _run(["nmcli", "device", "show", iface], timeout=5)[:2000]
        host = ident.get("host")
        if host:
            facts.append(f"mDNS host {host} at {p.get('address')}")

    if kind == "wifi":
        extra["nmcli"] = _run(["nmcli", "-f", "SSID,SIGNAL,SECURITY,BARS", "dev", "wifi"], timeout=6)[:1500]

    if kind in {"hid", "lighting"}:
        extra["lighting"] = "\n".join(_lighting_control_hints())[:1500]
        if ident.get("sysfs_led"):
            extra["led"] = _read(Path(ident["sysfs_led"]) / "uevent")[:800]
        if shutil.which("openrgb"):
            extra["openrgb"] = _run(["openrgb", "--list-devices"], timeout=8)[:1500]

    name = p.get("name")
    facts.insert(
        0,
        f"{name} is a {kind} peripheral"
        + (f" at {p.get('address')}" if p.get("address") else "")
        + (" (connected)" if p.get("connected") else " (not connected)")
        + ".",
    )
    for h in p.get("control_hints") or []:
        facts.append(f"Control: {h}")
    extra["inspected_at"] = time.time()
    return {"facts": facts, "extra": extra}


def format_inventory_index(peripherals: list[dict[str, Any]]) -> str:
    """Connected hardware plus counts — not the whole scan.

    Offline USB ids and per-device shell hints belong in a tool result, not in every
    prompt: the model only needs to know what is attached and that the peripherals
    tool acts on it by name.
    """
    if not peripherals:
        return (
            "=== Peripherals ===\nNone inventoried yet. peripherals action=scan finds them."
        )
    live = [p for p in peripherals if p.get("connected")]
    # Nearby Wi-Fi networks are scan results, not attached hardware: 20-odd SSIDs in
    # the prompt is noise the model starts narrating. The count and a scan suffice.
    present = [
        p
        for p in peripherals
        if p.get("available", True)
        and not p.get("connected")
        and (p.get("kind") or "") != "wifi"
    ]
    counts: dict[str, int] = {}
    for p in peripherals:
        counts[p.get("kind") or "other"] = counts.get(p.get("kind") or "other", 0) + 1
    lines = [
        f"=== Peripherals: {len(live)} connected, {len(present)} more present ===",
        "The peripherals tool lists, scans, inspects, connects, pairs and controls these "
        "by name — action=connect target=<name> does discovery, pairing and audio routing "
        "in one call. Keyboard RGB/backlight: action=control command=lighting "
        "value=rainbow|spectrum|off|50%.",
        "Inventory: " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())),
    ]
    if live:
        lines.append("Connected now:")
        for p in live[:12]:
            lines.append(f"  {p.get('name')} [{p.get('kind')}]")
    if present:
        lines.append(
            "Also present: "
            + ", ".join(str(p.get("name")) for p in present[:10])
            + (f" (+{len(present) - 10} more)" if len(present) > 10 else "")
        )
    lines.append(
        "peripherals action=list (optionally kind=<kind>) returns the full inventory with "
        "ids and addresses when you need one."
    )
    return "\n".join(lines)


def format_inventory(peripherals: list[dict[str, Any]], *, limit: int = 48) -> str:
    if not peripherals:
        return "=== Peripherals ===\nNone detected yet. Use the peripherals tool (action=scan) to search."
    connected = sum(1 for p in peripherals if p.get("connected"))
    available = sum(1 for p in peripherals if p.get("available", True))
    lines = [
        f"=== Peripherals ({available} present, {connected} connected) ===",
        "Use the peripherals tool to scan, inspect, connect, or control these. "
        "Match by id or name — do not guess shell flags when a control hint exists. "
        "Keyboard RGB/backlight: peripherals action=control command=lighting "
        "value=rainbow|spectrum|off|50%.",
    ]
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for p in peripherals:
        by_kind.setdefault(p.get("kind") or "other", []).append(p)
    shown = 0
    for kind in KIND_ORDER:
        group = by_kind.get(kind) or []
        if not group:
            continue
        lines.append(f"\n[{kind}]")
        for p in group:
            if shown >= limit:
                lines.append(f"  … {sum(len(v) for v in by_kind.values()) - shown} more omitted")
                return "\n".join(lines)
            flags = []
            if p.get("connected"):
                flags.append("connected")
            elif p.get("paired"):
                flags.append("paired")
            elif p.get("available", True):
                flags.append("present")
            else:
                flags.append("offline")
            addr = p.get("address") or ""
            hint = (p.get("control_hints") or [""])[0]
            extra = f" — {hint}" if hint else ""
            lines.append(
                f"  • {p.get('name')}  id={p.get('id')}  {addr}  [{', '.join(flags)}]{extra}"
            )
            shown += 1
    leftover = [k for k in by_kind if k not in KIND_ORDER]
    for kind in leftover:
        for p in by_kind[kind]:
            if shown >= limit:
                break
            lines.append(f"  • [{kind}] {p.get('name')} id={p.get('id')}")
            shown += 1
    return "\n".join(lines)


def peripheral_summary(p: dict[str, Any]) -> str:
    flags = "connected" if p.get("connected") else ("paired" if p.get("paired") else "present")
    return (
        f"{p.get('kind')}: {p.get('name')} ({p.get('address') or p.get('id')}) "
        f"[{flags}] on {platform.node()}"
    )


# ---------------------------------------------------------------------------
# Learner
# ---------------------------------------------------------------------------

class PeripheralLearner:
    """Keeps a live inventory of peripherals and remembers what we learn about them."""

    DEFAULT_REFRESH_MINUTES = 15.0

    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self._items: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._on_event: EmitFn | None = None

    def on_event(self, fn: EmitFn) -> None:
        self._on_event = fn

    async def _emit(self, event: dict[str, Any]) -> None:
        if not self._on_event:
            return
        try:
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            pass

    @property
    def items(self) -> list[dict[str, Any]]:
        return list(self._items)

    def get_context(self) -> str:
        try:
            from .config import store as _store

            full = bool(_store.get().agent.full_device_context)
        except Exception:  # noqa: BLE001
            full = False
        render = format_inventory if full else format_inventory_index
        items = self._items or self.memory.list_peripherals()
        return render(items) if items else ""

    def resolve(self, target: str) -> dict[str, Any] | None:
        needle = (target or "").strip().lower()
        if not needle:
            return None
        pool = self._items or self.memory.list_peripherals()
        for p in pool:
            if p.get("id", "").lower() == needle:
                return p
        for p in pool:
            if (p.get("address") or "").lower() == needle:
                return p

        tokens = [needle]
        if needle in {"keyboard", "kbd", "keyboards"}:
            tokens.extend(("keyboard", "kbd", "backlight", "keypad"))
        if needle in {"backlight", "backlights", "lights", "lighting", "rgb", "led"}:
            tokens.extend(("backlight", "lighting", "rgb", "kbd"))
        if needle in {"razer"} or needle.startswith("razer"):
            tokens.extend(("razer", "openrazer"))
        # unique, keep order
        seen_t: set[str] = set()
        tokens = [t for t in tokens if not (t in seen_t or seen_t.add(t))]

        matches = [
            p
            for p in pool
            if any(
                t in (p.get("name") or "").lower()
                or t in (p.get("id") or "").lower()
                or t in (p.get("vendor") or "").lower()
                or t in ((p.get("extra") or {}).get("driver") or "").lower()
                for t in tokens
            )
        ]
        if not matches:
            return None
        lighting_ask = any(
            w in needle for w in ("keyboard", "kbd", "backlight", "rgb", "light", "razer")
        )
        want_keyboard = any(w in needle for w in ("keyboard", "kbd", "backlight"))
        want_razer = "razer" in needle

        def _rank(x: dict[str, Any]) -> tuple:
            name_l = (x.get("name") or "").lower()
            ident = x.get("identifiers") or {}
            role = ident.get("razer_role") or ""
            mouseish = any(w in name_l for w in ("mouse", "deathadder", "g502")) or role == "mouse"
            return (
                0 if lighting_ask and x.get("kind") == "lighting" else 1,
                0 if want_razer and ident.get("openrazer_sysfs") else 1,
                0 if want_keyboard and "backlight" in name_l else 1,
                0 if want_keyboard and role == "keyboard" else 1,
                1 if want_keyboard and mouseish else 0,
                not x.get("connected"),
                not x.get("available", True),
            )

        matches.sort(key=_rank)
        return matches[0]

    async def scan(self, *, llm: Any | None = None, reason: str = "manual", discover: bool = False) -> list[dict[str, Any]]:
        async with self._lock:
            live = await asyncio.to_thread(scan_all, discover=discover)
            now = time.time()
            previous = {p["id"]: p for p in self.memory.list_peripherals()}
            live_ids = {p["id"] for p in live}

            merged: list[dict[str, Any]] = []
            newly: list[dict[str, Any]] = []
            for p in live:
                old = previous.get(p["id"])
                if old:
                    p["facts"] = old.get("facts") or []
                    p["first_seen"] = old.get("first_seen") or now
                    # Keep inspect extras that aren't stale identity.
                    extra = dict(old.get("extra") or {})
                    extra.update(p.get("extra") or {})
                    p["extra"] = extra
                else:
                    p["first_seen"] = now
                    newly.append(p)
                p["last_seen"] = now
                p["available"] = True
                self.memory.upsert_peripheral(p)
                merged.append(p)

            for pid, old in previous.items():
                if pid not in live_ids:
                    old = {**old, "available": False, "connected": False}
                    self.memory.upsert_peripheral(old)
                    merged.append(old)

            self._items = merged

            if llm is not None:
                for p in newly[:12]:
                    text = peripheral_summary(p)
                    try:
                        emb = await llm.embed(text)
                    except Exception:  # noqa: BLE001
                        emb = None
                    self.memory.add_memory("peripheral", text, emb)

            await self._emit({
                "type": "peripherals_updated",
                "reason": reason,
                "count": len(live),
                "connected": sum(1 for p in live if p.get("connected")),
                "discovered": [p["id"] for p in newly],
            })
            return merged

    async def inspect(self, target: str, *, llm: Any | None = None) -> dict[str, Any] | None:
        p = self.resolve(target)
        if p is None:
            return None
        detail = await asyncio.to_thread(inspect_peripheral, p)
        facts = list(p.get("facts") or [])
        for fact in detail.get("facts") or []:
            if fact not in facts:
                facts.append(fact)
        extra = dict(p.get("extra") or {})
        extra.update(detail.get("extra") or {})
        p = {**p, "facts": facts[-40:], "extra": extra}
        self.memory.upsert_peripheral(p)
        self._items = [p if x.get("id") == p["id"] else x for x in (self._items or [p])]
        summary = " | ".join(facts[:6])
        if llm is not None and summary:
            try:
                emb = await llm.embed(summary)
            except Exception:  # noqa: BLE001
                emb = None
            self.memory.add_memory("peripheral", summary[:800], emb)
        await self._emit({"type": "peripheral_inspected", "id": p["id"], "name": p.get("name")})
        return p

    async def remember_fact(self, target: str, fact: str, *, llm: Any | None = None) -> dict[str, Any] | None:
        p = self.resolve(target)
        if p is None:
            return None
        facts = list(p.get("facts") or [])
        if fact not in facts:
            facts.append(fact)
        p = {**p, "facts": facts[-40:]}
        self.memory.upsert_peripheral(p)
        self._items = [p if x.get("id") == p["id"] else x for x in (self._items or [p])]
        if llm is not None:
            try:
                emb = await llm.embed(fact)
            except Exception:  # noqa: BLE001
                emb = None
            self.memory.add_memory("peripheral", fact, emb)
        return p

    async def act(
        self,
        target: str,
        command: str,
        value: str = "",
        password: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        p = self.resolve(target)
        if p is None:
            return False, f"No peripheral matching '{target}'. Scan first.", None
        ok, output = await asyncio.to_thread(control_peripheral, p, command, value, password)
        # Refresh just this class of device after a state-changing action.
        if ok and command in {"connect", "disconnect", "pair", "unpair", "mount", "unmount", "default"}:
            try:
                await self.scan(reason=f"after-{command}", discover=False)
                p = self.resolve(p["id"]) or p
            except Exception:  # noqa: BLE001
                pass
        await self._emit({
            "type": "peripheral_action",
            "id": p.get("id") if p else target,
            "name": (p or {}).get("name"),
            "command": command,
            "ok": ok,
        })
        return ok, output, p

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    def _refresh_seconds(self) -> float:
        cfg = store.get()
        minutes = getattr(getattr(cfg, "device", None), "peripheral_refresh_minutes", None)
        if minutes is None:
            minutes = self.DEFAULT_REFRESH_MINUTES
        return max(1.0, float(minutes)) * 60.0

    async def _loop(self) -> None:
        from .llm import LLMClient

        llm = LLMClient(store.get().llm)
        try:
            await self.scan(llm=llm, reason="startup", discover=False)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        while True:
            try:
                await asyncio.sleep(self._refresh_seconds())
                await self.scan(llm=llm, reason="periodic", discover=False)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(30)


_learner: PeripheralLearner | None = None


def init_learner(memory: Memory) -> PeripheralLearner:
    global _learner
    _learner = PeripheralLearner(memory)
    return _learner


def get_learner() -> PeripheralLearner | None:
    return _learner
