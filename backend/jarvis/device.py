"""Autonomous device learning: profile the host, discover control capabilities, and
inject that knowledge into every agent run without requiring explicit scan tools."""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import psutil

from . import controls as _controls
from .appcatalog import get_catalog as _catalog
from .config import Config, store
from .memory import Memory

EmitFn = Callable[[dict[str, Any]], Any]

# Common launchable executables — expanded beyond the old system_scan list.
_APP_CANDIDATES = [
    "firefox", "chromium", "google-chrome", "brave", "code", "codium", "vim", "nvim",
    "emacs", "gimp", "blender", "libreoffice", "thunderbird", "vlc", "obs",
    "docker", "podman", "git", "node", "python3", "cargo", "go", "java", "kitty",
    "gnome-terminal", "konsole", "alacritty", "nautilus", "dolphin", "spotify",
    "flatpak", "snap", "systemctl", "journalctl", "nmcli", "bluetoothctl",
    "pactl", "wpctl", "amixer", "brightnessctl", "xrandr", "swaymsg", "hyprctl",
    "playerctl", "powerprofilesctl", "upower", "rfkill", "iwconfig", "ip",
    "openrgb", "polychromatic-cli", "razer-cli",
]

# Utilities probed to build OS-specific control hints.
_CONTROL_PROBES: dict[str, str] = {
    "audio_pulse": "pactl",
    "audio_pipewire": "wpctl",
    "audio_alsa": "amixer",
    "brightness": "brightnessctl",
    "display_x11": "xrandr",
    "display_wayland_sway": "swaymsg",
    "display_wayland_hypr": "hyprctl",
    "media_player": "playerctl",
    "network_nm": "nmcli",
    "network_iw": "iw",
    "bluetooth": "bluetoothctl",
    "power": "upower",
    "power_profiles": "powerprofilesctl",
    "containers": "docker",
    "package_apt": "apt",
    "package_pacman": "pacman",
    "package_dnf": "dnf",
    "package_brew": "brew",
    "package_flatpak": "flatpak",
    "package_snap": "snap",
    "init_systemd": "systemctl",
    "wifi_rfkill": "rfkill",
    "rgb_openrgb": "openrgb",
    "rgb_polychromatic": "polychromatic-cli",
    "rgb_razer": "razer-cli",
    "rgb_asusctl": "asusctl",
    # GUI / computer-use
    "gui_grim": "grim",
    "gui_maim": "maim",
    "gui_scrot": "scrot",
    "gui_xdotool": "xdotool",
    "gui_ydotool": "ydotool",
    "gui_wtype": "wtype",
    "gui_wl_clipboard": "wl-copy",
    "gui_cliclick": "cliclick",
    "gui_screencapture": "screencapture",
}


def fingerprint() -> str:
    node = platform.node()
    machine = platform.machine()
    return hashlib.sha256(f"{node}-{machine}".encode()).hexdigest()[:16]


def _run_capture(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        return (out.stdout or out.stderr or "").strip()[:2000]
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _desktop_environment() -> dict[str, str]:
    env = {}
    for key in (
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_TYPE",
        "XDG_SESSION_DESKTOP",
        "DESKTOP_SESSION",
        "WAYLAND_DISPLAY",
        "DISPLAY",
    ):
        val = os.environ.get(key, "")
        if val:
            env[key] = val
    return env


def _detect_init() -> str:
    if shutil.which("systemctl"):
        return "systemd"
    if Path("/sbin/openrc").exists():
        return "openrc"
    if Path("/etc/runit").exists():
        return "runit"
    return "unknown"


def _installed_apps() -> list[str]:
    return [c for c in _APP_CANDIDATES if shutil.which(c)]


def _control_capabilities() -> dict[str, Any]:
    """Discover which control utilities exist and emit actionable hints."""
    available: dict[str, str] = {}
    hints: list[str] = []

    for name, binary in _CONTROL_PROBES.items():
        path = shutil.which(binary)
        if path:
            available[name] = path

    os_name = platform.system()
    session = os.environ.get("XDG_SESSION_TYPE", "")

    # Concrete command syntax for volume, brightness, Wi-Fi, services, packages and
    # the rest now comes from the control catalogue (jarvis.controls), which probes
    # this host and publishes one verified command per action. Only hints the
    # catalogue cannot express — tool routing and session caveats — stay here.
    if "rgb_openrgb" not in available and "rgb_polychromatic" not in available \
            and "rgb_razer" not in available and "rgb_asusctl" not in available:
        hints.append(
            "RGB lighting: no OpenRGB/OpenRazer/asusctl CLI detected. Prefer peripherals "
            "control command=lighting; install openrgb, polychromatic, or asusctl "
            "(device_control action=control control=packages.install) if RGB effects fail."
        )

    if session == "wayland":
        hints.append("Session is Wayland — prefer wpctl/nmcli over legacy x-only tools.")
    elif session == "x11":
        hints.append("Session is X11 — xrandr and xdotool may be available.")

    opener = "xdg-open" if os_name == "Linux" else "open" if os_name == "Darwin" else "start"
    if shutil.which(opener.split()[0]) or os_name == "Windows":
        hints.append(
            f"Open URL/app: device_control action=open (or {opener} <url-or-app>) "
            "— do not use computer_use just to launch a browser/URL."
        )

    # GUI computer-use capability summary (native backends in jarvis.computer).
    try:
        from .computer import format_computer_hints

        hints.extend(format_computer_hints())
    except Exception:  # noqa: BLE001
        hints.append(
            "GUI: use computer_use (screenshot → click/type). "
            "On Wayland install grim + ydotool; on X11 install maim + xdotool."
        )

    return {"available": available, "hints": hints}


def scan_device() -> dict[str, Any]:
    """Gather a comprehensive profile of the machine JARVIS is running on."""
    vm = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append(
                {
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total_gb": round(usage.total / 1e9, 1),
                    "used_gb": round(usage.used / 1e9, 1),
                }
            )
        except (PermissionError, OSError):
            continue

    try:
        addrs = {socket.gethostname(): socket.gethostbyname(socket.gethostname())}
    except socket.error:
        addrs = {}

    net_ifaces = {}
    try:
        for name, addrs_list in psutil.net_if_addrs().items():
            net_ifaces[name] = [
                {"family": str(a.family), "address": a.address}
                for a in addrs_list
                if a.address and not a.address.startswith("fe80")
            ]
    except (OSError, AttributeError):
        pass

    battery = None
    try:
        bat = psutil.sensors_battery()
        if bat is not None:
            battery = {
                "percent": bat.percent,
                "plugged": bat.power_plugged,
                "secsleft": bat.secsleft,
            }
    except (AttributeError, OSError):
        pass

    caps = _control_capabilities()
    desktop = _desktop_environment()
    init_sys = _detect_init()

    profile: dict[str, Any] = {
        "fingerprint": fingerprint(),
        "hostname": platform.node(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "python": platform.python_version(),
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "cpu_cores_logical": psutil.cpu_count(logical=True),
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "memory_total_gb": round(vm.total / 1e9, 1),
        "memory_available_gb": round(vm.available / 1e9, 1),
        "disks": disks,
        "network": addrs,
        "network_interfaces": net_ifaces,
        "battery": battery,
        "installed_apps": _installed_apps(),
        "applications": [a.as_dict() for a in _catalog().apps(refresh=True)[:250]],
        "boot_time": psutil.boot_time(),
        "desktop": desktop,
        "init_system": init_sys,
        "control_capabilities": caps,
        "controls": [c.id for c in _controls.available(refresh=True)],
        "controls_installable": _controls.installable(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "home": str(Path.home()),
        "shell": os.environ.get("SHELL") or "",
        "cwd": os.getcwd(),
        "scanned_at": time.time(),
    }

    # Best-effort GPU probe (non-fatal).
    if shutil.which("nvidia-smi"):
        gpu_out = _run_capture(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"])
        if gpu_out:
            profile["gpu"] = gpu_out
    elif platform.system() == "Linux" and Path("/sys/class/drm").exists():
        cards = [p.name for p in Path("/sys/class/drm").glob("card*/device/vendor")]
        if cards:
            profile["gpu"] = f"DRM devices: {', '.join(sorted(cards)[:4])}"

    return profile


def profile_summary(profile: dict[str, Any]) -> str:
    apps = ", ".join(profile.get("installed_apps") or []) or "none detected"
    desktop = profile.get("desktop") or {}
    de = desktop.get("XDG_CURRENT_DESKTOP") or desktop.get("DESKTOP_SESSION") or "unknown"
    caps = profile.get("control_capabilities") or {}
    hint_count = len(caps.get("hints") or [])
    installed = profile.get("applications") or []
    gui = [a for a in installed if a.get("kind") != "cli"]
    return (
        f"Device '{profile['hostname']}' ({profile['os']} {profile['os_release']}, "
        f"{profile['architecture']}, {de}): "
        f"{profile['cpu_cores_logical']} CPUs, {profile['memory_total_gb']} GB RAM, "
        f"user={profile.get('user')}. "
        f"Control utilities: {len(caps.get('available') or {})} detected, "
        f"{len(profile.get('controls') or [])} verified control actions, "
        f"{hint_count} routing hints. "
        f"Applications: {len(gui)} launchable GUI apps catalogued. Tools: {apps}."
    )


def _full_context_enabled() -> bool:
    try:
        from .config import store

        return bool(store.get().agent.full_device_context)
    except Exception:  # noqa: BLE001
        return False


def format_device_context(profile: dict[str, Any], *, full: bool | None = None) -> str:
    """Format the device profile for injection into the system prompt.

    By default the control and application catalogues are summarised into searchable
    indexes rather than listed in full — see ``config.agent.full_device_context``.
    """
    if full is None:
        full = _full_context_enabled()
    lines = [
        "=== This device (learned autonomously) ===",
        f"Hostname: {profile['hostname']}",
        f"OS: {profile['os']} {profile['os_release']} ({profile['architecture']})",
        f"User: {profile.get('user')} | Home: {profile.get('home')} | Shell: {profile.get('shell') or 'default'}",
        f"CPU: {profile['processor']} — {profile['cpu_cores_physical']} physical / "
        f"{profile['cpu_cores_logical']} logical cores ({profile['cpu_percent']}% in use)",
        f"Memory: {profile['memory_available_gb']} GB free of {profile['memory_total_gb']} GB",
        f"Init: {profile.get('init_system', 'unknown')}",
    ]

    desktop = profile.get("desktop") or {}
    if desktop:
        de = desktop.get("XDG_CURRENT_DESKTOP") or desktop.get("DESKTOP_SESSION")
        session = desktop.get("XDG_SESSION_TYPE")
        if de or session:
            lines.append(f"Desktop: {de or '?'} ({session or 'unknown session'})")

    if profile.get("battery"):
        b = profile["battery"]
        try:
            pct = f"{float(b.get('percent') or 0):.0f}"
        except (TypeError, ValueError):
            pct = str(b.get("percent"))
        lines.append(f"Battery: {pct}% {'(plugged in)' if b.get('plugged') else '(on battery)'}")

    lines.append("Disks:")
    for d in profile.get("disks") or []:
        lines.append(f"  - {d['device']} at {d['mountpoint']}: {d['used_gb']}/{d['total_gb']} GB used")

    if profile.get("gpu"):
        lines.append(f"GPU: {profile['gpu']}")

    apps = profile.get("installed_apps") or []
    if apps:
        lines.append(f"Installed tools/apps: {', '.join(apps)}")

    caps = profile.get("control_capabilities") or {}
    hints = caps.get("hints") or []
    if hints:
        lines.append(
            "\nHow to control this device "
            "(device_control for shell/files; computer_use for GUI; peripherals for hardware):"
        )
        for h in hints:
            lines.append(f"  • {h}")

    controls_ctx = (
        _controls.format_controls_context()
        if full
        else _controls.format_controls_index()
    )
    if controls_ctx:
        lines.append("")
        lines.append(controls_ctx)

    gaps = (profile.get("controls_installable") or _controls.installable()) if full else []
    if gaps:
        lines.append(
            "\nControls this device could gain — install the package yourself with "
            "device_control action=control control=packages.install value=<pkg>, then "
            "perform the action. Do not report these as impossible:"
        )
        for gap in gaps[:12]:
            lines.append(
                f"  • {gap['package']} → {gap['unlocks']}"
            )

    app_ctx = _catalog().context(full=full)
    if app_ctx:
        lines.append("")
        lines.append(app_ctx)

    lines.append(
        "\nYou already know this machine. Control it directly via device_control / "
        "computer_use / peripherals — do not ask to scan it first. When you discover "
        "new control methods, call remember."
    )
    return "\n".join(lines)


class DeviceLearner:
    """Learns about the host device on startup and keeps the profile fresh."""

    DEFAULT_REFRESH_HOURS = 6

    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self._profile: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._on_event: EmitFn | None = None

    def on_event(self, fn: EmitFn) -> None:
        self._on_event = fn

    async def _emit(self, event: dict[str, Any]) -> None:
        if self._on_event:
            try:
                result = self._on_event(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                pass

    @property
    def profile(self) -> dict[str, Any] | None:
        return self._profile

    def get_context(self) -> str:
        if self._profile:
            return format_device_context(self._profile)
        # Fall back to stored device from DB.
        devices = self.memory.list_devices()
        if devices:
            try:
                p = devices[0]["profile"]
                if isinstance(p, dict):
                    return format_device_context(p)
            except (KeyError, TypeError):
                pass
        return ""

    async def learn(self, *, llm: Any | None = None, reason: str = "startup") -> dict[str, Any]:
        """Scan the device, persist the profile, and store semantic memories."""
        async with self._lock:
            profile = await asyncio.to_thread(scan_device)
            self._profile = profile
            self.memory.upsert_device(profile["fingerprint"], profile["hostname"], profile)
            summary = profile_summary(profile)
            embedding = None
            if llm is not None:
                try:
                    embedding = await llm.embed(summary)
                except Exception:  # noqa: BLE001
                    embedding = None
            self.memory.add_memory("device", summary, embedding)

            # Store what each purpose-bearing application is for, so a later request
            # phrased by purpose ("edit a photo") can recall the app that serves it.
            for app in _catalog().apps():
                if not app.aliases:
                    continue
                text = (
                    f"Application on {profile['hostname']}: {app.name} "
                    f"({app.kind}:{app.id}) — {app.purpose() or 'installed application'}. "
                    f"Use it for: {', '.join(app.aliases[:6])}. "
                    f"Launch: device_control action=open app={app.id}"
                )
                if self.memory.has_memory("application", text):
                    continue
                app_emb = None
                if llm is not None:
                    try:
                        app_emb = await llm.embed(text)
                    except Exception:  # noqa: BLE001
                        app_emb = None
                self.memory.add_memory("application", text, app_emb)

            # Store every verified control action so a request phrased any which way
            # ("how hot is it", "quieter") can recall the exact action that serves it.
            for text in _controls.format_control_memories(profile["hostname"]):
                if self.memory.has_memory("device_control", text):
                    continue
                ctl_emb = None
                if llm is not None:
                    try:
                        ctl_emb = await llm.embed(text)
                    except Exception:  # noqa: BLE001
                        ctl_emb = None
                self.memory.add_memory("device_control", text, ctl_emb)

            # Store individual control hints as retrievable memories.
            caps = profile.get("control_capabilities") or {}
            for hint in caps.get("hints") or []:
                text = f"Control on {profile['hostname']}: {hint}"
                if self.memory.has_memory("device_control", text):
                    continue
                hint_emb = None
                if llm is not None:
                    try:
                        hint_emb = await llm.embed(text)
                    except Exception:  # noqa: BLE001
                        hint_emb = None
                self.memory.add_memory("device_control", text, hint_emb)

            await self._emit({
                "type": "device_learned",
                "reason": reason,
                "hostname": profile["hostname"],
                "summary": summary,
            })
            return profile

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    def _refresh_seconds(self) -> float:
        cfg = store.get()
        hours = getattr(getattr(cfg, "device", None), "refresh_hours", None)
        if hours is None:
            hours = self.DEFAULT_REFRESH_HOURS
        return max(1.0, float(hours)) * 3600.0

    async def _loop(self) -> None:
        from .llm import LLMClient

        llm = LLMClient(store.get().llm)
        try:
            await self.learn(llm=llm, reason="startup")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

        while True:
            try:
                await asyncio.sleep(self._refresh_seconds())
                await self.learn(llm=llm, reason="periodic")
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(60)


# Module-level singleton — initialized from main.py
_learner: DeviceLearner | None = None


def init_learner(memory: Memory) -> DeviceLearner:
    global _learner
    _learner = DeviceLearner(memory)
    return _learner


def get_learner() -> DeviceLearner | None:
    return _learner
