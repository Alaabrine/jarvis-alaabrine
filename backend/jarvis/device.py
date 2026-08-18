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

    if "audio_pipewire" in available:
        hints.append("Volume/mute: wpctl set-volume @DEFAULT_AUDIO_SINK@ 50% ; wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle")
    elif "audio_pulse" in available:
        hints.append("Volume/mute: pactl set-sink-volume @DEFAULT_SINK@ 50% ; pactl set-sink-mute @DEFAULT_SINK@ toggle")
    elif "audio_alsa" in available:
        hints.append("Volume: amixer set Master 50% ; amixer set Master toggle")

    if "brightness" in available:
        hints.append("Brightness: brightnessctl set 50% ; brightnessctl set +10% ; brightnessctl set 10%-")

    if "display_wayland_hypr" in available:
        hints.append("Hyprland: hyprctl dispatch exec <app> ; hyprctl clients")
    elif "display_wayland_sway" in available:
        hints.append("Sway: swaymsg exec <app>")
    elif "display_x11" in available:
        hints.append("Display: xrandr --listmonitors ; xrandr --output <name> --brightness 0.8")

    if "network_nm" in available:
        hints.append("Wi-Fi: nmcli dev wifi list ; nmcli dev wifi connect <ssid> password <pass> ; nmcli radio wifi off|on")
    elif "network_iw" in available:
        hints.append("Wi-Fi: iw dev ; iw dev wlan0 scan")

    if "bluetooth" in available:
        hints.append("Bluetooth: bluetoothctl power on ; bluetoothctl scan on ; bluetoothctl pair <mac>")

    if "media_player" in available:
        hints.append("Media: playerctl play-pause ; playerctl next ; playerctl volume 0.5")

    if "power_profiles" in available:
        hints.append("Power profile: powerprofilesctl set balanced|power-saver|performance")

    if "init_systemd" in available:
        hints.append("Services: systemctl status/start/stop/restart <unit> ; journalctl -u <unit> -n 50")

    if os_name == "Linux":
        if "package_pacman" in available:
            hints.append("Packages (Arch): pacman -S <pkg> ; pacman -Qs <query>")
        elif "package_apt" in available:
            hints.append("Packages (Debian/Ubuntu): apt install <pkg> ; apt search <query>")
        elif "package_dnf" in available:
            hints.append("Packages (Fedora): dnf install <pkg> ; dnf search <query>")
        if "package_flatpak" in available:
            hints.append("Flatpak: flatpak install ; flatpak run <app>")
        if "package_snap" in available:
            hints.append("Snap: snap install <pkg>")

    if os_name == "Darwin":
        hints.append("macOS: open -a <App> ; osascript for automation ; brew install <pkg>")

    if os_name == "Windows":
        hints.append("Windows: Start-Process ; Get-Process ; winget install <pkg>")

    if session == "wayland":
        hints.append("Session is Wayland — prefer wpctl/nmcli over legacy x-only tools.")
    elif session == "x11":
        hints.append("Session is X11 — xrandr and xdotool may be available.")

    opener = "xdg-open" if os_name == "Linux" else "open" if os_name == "Darwin" else "start"
    if shutil.which(opener.split()[0]) or os_name == "Windows":
        hints.append(f"Open URL/app: {opener} <url-or-app>")

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
        "boot_time": psutil.boot_time(),
        "desktop": desktop,
        "init_system": init_sys,
        "control_capabilities": caps,
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
    return (
        f"Device '{profile['hostname']}' ({profile['os']} {profile['os_release']}, "
        f"{profile['architecture']}, {de}): "
        f"{profile['cpu_cores_logical']} CPUs, {profile['memory_total_gb']} GB RAM, "
        f"user={profile.get('user')}. "
        f"Control utilities: {len(caps.get('available') or {})} detected, "
        f"{hint_count} control hints. Apps: {apps}."
    )


def format_device_context(profile: dict[str, Any]) -> str:
    """Format the device profile for injection into the system prompt."""
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
        lines.append(f"Battery: {b.get('percent')}% {'(plugged in)' if b.get('plugged') else '(on battery)'}")

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
        lines.append("\nHow to control this device (use device_control with shell/open actions):")
        for h in hints:
            lines.append(f"  • {h}")

    lines.append(
        "\nYou already know this machine. Control it directly via device_control — "
        "do not ask to scan it first. When you discover new control methods, call remember."
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

            # Store individual control hints as retrievable memories.
            caps = profile.get("control_capabilities") or {}
            for hint in caps.get("hints") or []:
                text = f"Control on {profile['hostname']}: {hint}"
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
