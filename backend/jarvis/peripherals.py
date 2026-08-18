"""Detect, learn, and control peripherals attached to (or near) this machine.

USB, Bluetooth, audio, displays, HID, cameras, printers, removable storage,
Wi-Fi, LAN/mDNS neighbours — inventory on startup, refresh periodically, and
expose connect / control primitives the agent can call without inventing shell.
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
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
    "camera",
    "printer",
    "storage",
    "wifi",
    "network",
    "radio",
)


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


def _run_shell(command: str, timeout: float = 8.0) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
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
            found.append(
                _make(
                    kind="hid",
                    name=name,
                    address=str(link),
                    connected=True,
                    icon=kind_hint,
                    hints=["HID input — plug-and-play; no explicit connect needed."],
                    identifiers={"dev": str(link)},
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

    if command in {"brightness"} and kind in {"display", "usb"}:
        level = value.strip() or "50%"
        if shutil.which("brightnessctl"):
            return _run_shell(f"brightnessctl set {level}")
        return False, "brightnessctl not available."

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


def format_inventory(peripherals: list[dict[str, Any]], *, limit: int = 48) -> str:
    if not peripherals:
        return "=== Peripherals ===\nNone detected yet. Use the peripherals tool (action=scan) to search."
    connected = sum(1 for p in peripherals if p.get("connected"))
    available = sum(1 for p in peripherals if p.get("available", True))
    lines = [
        f"=== Peripherals ({available} present, {connected} connected) ===",
        "Use the peripherals tool to scan, inspect, connect, or control these. "
        "Match by id or name — do not guess shell flags when a control hint exists.",
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
        if self._items:
            return format_inventory(self._items)
        stored = self.memory.list_peripherals()
        if stored:
            return format_inventory(stored)
        return ""

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
        matches = [
            p
            for p in pool
            if needle in (p.get("name") or "").lower() or needle in (p.get("id") or "").lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            # Prefer connected, then available.
            matches.sort(key=lambda x: (not x.get("connected"), not x.get("available", True)))
            return matches[0]
        return None

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
