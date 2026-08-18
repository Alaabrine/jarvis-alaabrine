"""Device scanning: gather a profile of the machine and remember it."""

from __future__ import annotations

import asyncio
import hashlib
import platform
import shutil
import socket

import psutil

from .base import Tool, ToolContext, ToolResult, prop


def _fingerprint() -> str:
    node = platform.node()
    machine = platform.machine()
    return hashlib.sha256(f"{node}-{machine}".encode()).hexdigest()[:16]


def _installed_apps() -> list[str]:
    """Best-effort list of common launchable executables found on PATH."""
    candidates = [
        "firefox", "chromium", "google-chrome", "code", "codium", "vim", "nvim",
        "emacs", "gimp", "blender", "libreoffice", "thunderbird", "vlc", "obs",
        "docker", "git", "node", "python3", "cargo", "go", "java", "kitty",
        "gnome-terminal", "konsole", "alacritty", "nautilus", "dolphin", "spotify",
    ]
    return [c for c in candidates if shutil.which(c)]


def _scan_sync() -> dict:
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

    return {
        "fingerprint": _fingerprint(),
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
        "installed_apps": _installed_apps(),
        "boot_time": psutil.boot_time(),
    }


def _summary(profile: dict) -> str:
    return (
        f"Device '{profile['hostname']}' ({profile['os']} {profile['os_release']}, "
        f"{profile['architecture']}): {profile['cpu_cores_logical']} logical CPUs, "
        f"{profile['memory_total_gb']} GB RAM. "
        f"Installed apps detected: {', '.join(profile['installed_apps']) or 'none'}."
    )


async def _scan(args: dict, ctx: ToolContext) -> ToolResult:
    profile = await asyncio.to_thread(_scan_sync)
    ctx.memory.upsert_device(profile["fingerprint"], profile["hostname"], profile)
    summary = _summary(profile)
    embedding = await ctx.llm.embed(summary)
    ctx.memory.add_memory("device", summary, embedding)

    lines = [
        f"Hostname: {profile['hostname']}",
        f"OS: {profile['os']} {profile['os_release']} ({profile['architecture']})",
        f"CPU: {profile['processor']} - {profile['cpu_cores_physical']} physical / "
        f"{profile['cpu_cores_logical']} logical cores, {profile['cpu_percent']}% in use",
        f"Memory: {profile['memory_available_gb']} GB free of {profile['memory_total_gb']} GB",
        "Disks:",
    ]
    for d in profile["disks"]:
        lines.append(f"  - {d['device']} at {d['mountpoint']}: {d['used_gb']}/{d['total_gb']} GB used")
    lines.append(f"Installed apps: {', '.join(profile['installed_apps']) or 'none detected'}")
    return ToolResult(True, "\n".join(lines))


system_scan = Tool(
    name="system_scan",
    description=(
        "Scan this device and report its hardware, OS, disks, network, and installed apps. "
        "The result is remembered so it can be recalled in future conversations."
    ),
    parameters={
        "detail": prop("string", "Optional level of detail ('summary' or 'full').", optional=True),
    },
    run=_scan,
)
