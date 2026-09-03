"""Exhaustive catalogue of device-control commands, filtered to what this host can do.

Every way JARVIS can change or read the machine is declared here once, as a
:class:`Control`: a logical action id, the command template that performs it, the
binaries it needs, and the natural-language phrases that mean it.

Several entries may share an id — those are alternative backends for the same logical
action (``wpctl`` vs ``pactl`` vs ``amixer`` vs ``osascript`` for "set the volume").
:func:`available` probes the host once, keeps the first backend that actually exists,
and discards the rest. What survives is the exhaustive list of things JARVIS can
genuinely do on *this* device, which is injected into the prompt, stored as memories,
exposed as ``device_control action=control``, and matched directly against speech so
that "dim my screen" runs rather than being narrated back.
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

#: Value kinds a control accepts for its parameters.
#: ``none``    — no parameter
#: ``percent`` — 0-100, inserted bare (template supplies % and sign)
#: ``step``    — 1-100 relative amount
#: ``number``  — integer or decimal
#: ``choice``  — one of ``choices``
#: ``text``    — free text, shell-quoted on render
#: ``path``    — filesystem path, shell-quoted on render
VALUE_KINDS = ("none", "percent", "step", "number", "choice", "text", "path")

_SAFE_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


@dataclass(frozen=True)
class Control:
    """One concrete way to read or change something on this machine."""

    id: str
    category: str
    summary: str
    command: str
    platforms: tuple[str, ...] = ("Linux",)
    requires: tuple[str, ...] = ()
    session: str = ""  # "wayland" | "x11" | "" (any)
    env: tuple[str, ...] = ()  # environment variables that must be set
    value: str = "none"
    choices: tuple[str, ...] = ()
    params: tuple[str, ...] = ("value",)
    reads: bool = False  # read-only: safe to run unprompted, returns state
    fast: bool = False  # safe + unambiguous enough to run straight from speech
    risky: bool = False  # needs the usual confirmation (power off, wipe, forget)
    sudo: bool = False  # requires root
    timeout: int = 30

    def needs_value(self) -> bool:
        return self.value != "none"

    def placeholder(self) -> str:
        if self.value == "choice":
            return "|".join(self.choices)
        return {
            "percent": "0-100",
            "step": "amount",
            "number": "n",
            "text": "text",
            "path": "path",
        }.get(self.value, "")

    def signature(self) -> str:
        if not self.needs_value():
            return self.id
        if len(self.params) > 1:
            return f"{self.id} " + " ".join(f"{p}=<…>" for p in self.params)
        return f"{self.id} value=<{self.placeholder()}>"

    def render(self, params: dict[str, Any] | None = None) -> str:
        """Fill the template, validating every substitution."""
        given = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        rendered: dict[str, str] = {}
        if not self.needs_value():
            # Still run through format() so ``{{…}}`` escapes (playerctl format
            # strings, awk bodies) collapse the same way they do for templates.
            return self.command.format()
        for name in self.params:
            raw = given.get(name)
            if raw is None and name == "value":
                # Tolerate a single positional value passed under any of the names.
                for alt in ("value", *self.params):
                    if given.get(alt) is not None:
                        raw = given[alt]
                        break
            if raw is None:
                raise ValueError(f"{self.id} requires {name}=<{self.placeholder()}>")
            rendered[name] = self._coerce(str(raw).strip())
        return self.command.format(**rendered)

    def _coerce(self, raw: str) -> str:
        kind = self.value
        if kind in {"text", "path"}:
            if not raw:
                raise ValueError(f"{self.id} needs a non-empty value")
            return shlex.quote(os.path.expanduser(raw) if kind == "path" else raw)
        if kind == "choice":
            low = raw.lower()
            for choice in self.choices:
                if choice.lower() == low:
                    return choice
            raise ValueError(f"{self.id} value must be one of: {', '.join(self.choices)}")
        cleaned = raw.rstrip("%").strip()
        if not _SAFE_NUMBER.match(cleaned):
            raise ValueError(f"{self.id} needs a number, got {raw!r}")
        number = float(cleaned)
        if kind in {"percent", "step"}:
            number = max(0.0, min(100.0, number))
            return str(int(round(number)))
        return str(int(number)) if number.is_integer() else str(number)


# ---------------------------------------------------------------------------
# Natural-language triggers, shared by every backend of a logical action.
# Longer phrases win, so put specific ones first.
# ---------------------------------------------------------------------------

_PHRASES: dict[str, tuple[str, ...]] = {
    "display.brightness.set": (
        "set brightness", "set the brightness", "screen brightness", "brightness to",
        "set screen brightness", "brightness at",
    ),
    "display.brightness.down": (
        "dim my screen", "dim the screen", "dim the display", "dim my display",
        "dim the monitor", "lower the brightness", "turn down the brightness",
        "reduce the brightness", "less bright", "darker", "dim it", "dim screen", "dim",
    ),
    "display.brightness.up": (
        "brighten my screen", "brighten the screen", "brighten the display",
        "raise the brightness", "turn up the brightness", "increase the brightness",
        "brighter", "brighten",
    ),
    "display.brightness.max": ("full brightness", "max brightness", "brightness to full"),
    "display.brightness.min": ("minimum brightness", "dimmest", "lowest brightness"),
    "display.keyboard_backlight.set": (
        "keyboard backlight", "keyboard brightness", "backlight on the keyboard",
    ),
    "display.off": ("turn off the screen", "turn off my screen", "screen off", "blank the screen"),
    "display.on": ("turn on the screen", "wake the screen", "screen on"),
    "display.nightlight.on": (
        "night light", "night mode", "warm the screen", "blue light filter", "night shift",
    ),
    "display.nightlight.off": ("turn off night light", "disable night mode", "stop night light"),
    "display.list": ("list displays", "what monitors", "my monitors", "which displays"),
    "audio.volume.set": (
        "set the volume", "set volume", "volume to", "volume at", "set the sound",
    ),
    "audio.volume.up": (
        "turn up the volume", "turn it up", "volume up", "louder", "raise the volume",
        "increase the volume", "turn the sound up",
    ),
    "audio.volume.down": (
        "turn down the volume", "turn it down", "volume down", "quieter", "lower the volume",
        "decrease the volume", "turn the sound down",
    ),
    "audio.mute": ("mute the sound", "mute the audio", "mute my", "mute it", "silence", "mute"),
    "audio.unmute": ("unmute", "sound back on", "turn the sound back on"),
    "audio.mute.toggle": ("toggle mute",),
    "audio.mic.mute": ("mute the mic", "mute my microphone", "mute microphone"),
    "audio.mic.unmute": ("unmute the mic", "unmute my microphone", "unmute microphone"),
    "audio.outputs.list": ("list audio outputs", "audio devices", "sound outputs"),
    "media.playpause": ("play pause", "pause the music", "resume the music", "play the music"),
    "media.next": ("next track", "next song", "skip this song", "skip the track", "skip song"),
    "media.previous": ("previous track", "previous song", "go back a song", "last song"),
    "media.stop": ("stop the music", "stop playback"),
    "media.now_playing": ("what's playing", "what is playing", "now playing", "current song"),
    "network.wifi.list": ("list wifi", "scan for wifi", "available networks", "nearby networks"),
    "network.wifi.on": ("turn on wifi", "enable wifi", "wifi on"),
    "network.wifi.off": ("turn off wifi", "disable wifi", "wifi off"),
    "network.wifi.current": ("which wifi", "what wifi", "current network", "am i connected"),
    "network.wifi.connect": ("connect to wifi", "join the network", "connect to the network"),
    "network.airplane.on": ("airplane mode on", "enable airplane mode", "flight mode"),
    "network.airplane.off": ("airplane mode off", "disable airplane mode"),
    "network.ip.local": ("my ip", "local ip", "ip address"),
    "network.ip.public": ("public ip", "external ip", "what's my ip address online"),
    "bluetooth.power.on": ("turn on bluetooth", "enable bluetooth", "bluetooth on"),
    "bluetooth.power.off": ("turn off bluetooth", "disable bluetooth", "bluetooth off"),
    "power.profile.set": ("power profile", "power mode", "performance mode", "battery saver"),
    "power.battery": ("battery level", "battery status", "how much battery", "battery percentage"),
    "power.lock": ("lock the screen", "lock my screen", "lock the computer", "lock it"),
    "power.suspend": ("go to sleep", "suspend the machine", "sleep the computer", "suspend"),
    "power.uptime": ("uptime", "how long has this been running"),
    "system.temperature": ("temperature", "how hot", "thermals", "cpu temp"),
    "system.memory": ("memory usage", "how much ram", "ram usage", "free memory"),
    "storage.usage": (
        "disk space", "disk usage", "how full", "storage left", "space left",
        "free space",
    ),
    "storage.disks": ("list my disks", "what drives", "my drives"),
    "system.processes.top": ("what's using cpu", "top processes", "heaviest processes"),
    "session.screenshot.full": ("take a screenshot", "screenshot the screen", "capture the screen"),
    "session.clipboard.paste": ("what's in my clipboard", "read the clipboard"),
    "packages.update": ("update the system", "update my packages", "upgrade the system"),
}

#: Default relative amount when the user says "dim" without a number.
_STEP_DEFAULTS: dict[str, str] = {
    "display.brightness.up": "20",
    "display.brightness.down": "20",
    "display.keyboard_backlight.up": "20",
    "display.keyboard_backlight.down": "20",
    "audio.volume.up": "10",
    "audio.volume.down": "10",
}


def _c(*args: Any, **kwargs: Any) -> Control:
    return Control(*args, **kwargs)


# ---------------------------------------------------------------------------
# The catalogue. Order matters: for a shared id, the first available entry wins.
# ---------------------------------------------------------------------------

_CONTROLS: tuple[Control, ...] = (
    # ---------------------------------------------------------------- display
    _c("display.brightness.get", "Display", "Read screen brightness",
       "brightnessctl -m", requires=("brightnessctl",), reads=True, fast=True),
    _c("display.brightness.get", "Display", "Read screen brightness",
       "ddcutil getvcp 10", requires=("ddcutil",), reads=True),
    _c("display.brightness.get", "Display", "Read screen brightness",
       "brightness -l", platforms=("Darwin",), requires=("brightness",), reads=True),
    _c("display.brightness.set", "Display", "Set screen brightness to a percentage",
       "brightnessctl set {value}%", requires=("brightnessctl",), value="percent", fast=True),
    _c("display.brightness.set", "Display", "Set screen brightness to a percentage",
       "ddcutil setvcp 10 {value}", requires=("ddcutil",), value="percent", fast=True),
    _c("display.brightness.set", "Display", "Set screen brightness (software gamma)",
       "xrandr --output \"$(xrandr | awk '/ connected/{{print $1; exit}}')\" "
       "--brightness $(awk \"BEGIN{{print {value}/100}}\")",
       requires=("xrandr",), session="x11", value="percent"),
    _c("display.brightness.set", "Display", "Set screen brightness to a percentage",
       "brightness $(awk \"BEGIN{{print {value}/100}}\")",
       platforms=("Darwin",), requires=("brightness",), value="percent", fast=True),
    _c("display.brightness.set", "Display", "Set screen brightness to a percentage",
       "powershell -Command \"(Get-WmiObject -Namespace root/WMI -Class "
       "WmiMonitorBrightnessMethods).WmiSetBrightness(1,{value})\"",
       platforms=("Windows",), value="percent", fast=True),
    _c("display.brightness.up", "Display", "Raise screen brightness by a step",
       "brightnessctl set +{value}%", requires=("brightnessctl",), value="step", fast=True),
    _c("display.brightness.down", "Display", "Lower screen brightness by a step",
       "brightnessctl set {value}%-", requires=("brightnessctl",), value="step", fast=True),
    _c("display.brightness.max", "Display", "Screen brightness to maximum",
       "brightnessctl set 100%", requires=("brightnessctl",), fast=True),
    _c("display.brightness.min", "Display", "Screen brightness to minimum (stays visible)",
       "brightnessctl set 5%", requires=("brightnessctl",), fast=True),
    _c("display.keyboard_backlight.get", "Display", "Read keyboard backlight level",
       "brightnessctl --device='*kbd_backlight' -m", requires=("brightnessctl",), reads=True),
    _c("display.keyboard_backlight.set", "Display", "Set keyboard backlight level",
       "brightnessctl --device='*kbd_backlight' set {value}%",
       requires=("brightnessctl",), value="percent", fast=True),
    _c("display.keyboard_backlight.up", "Display", "Raise keyboard backlight",
       "brightnessctl --device='*kbd_backlight' set +{value}%",
       requires=("brightnessctl",), value="step", fast=True),
    _c("display.keyboard_backlight.down", "Display", "Lower keyboard backlight",
       "brightnessctl --device='*kbd_backlight' set {value}%-",
       requires=("brightnessctl",), value="step", fast=True),
    _c("display.list", "Display", "List connected monitors and their modes",
       "hyprctl monitors", requires=("hyprctl",), reads=True, fast=True),
    _c("display.list", "Display", "List connected monitors and their modes",
       "swaymsg -t get_outputs", requires=("swaymsg",), reads=True, fast=True),
    _c("display.list", "Display", "List connected monitors and their modes",
       "wlr-randr", requires=("wlr-randr",), reads=True, fast=True),
    _c("display.list", "Display", "List connected monitors and their modes",
       "xrandr --listmonitors", requires=("xrandr",), session="x11", reads=True, fast=True),
    _c("display.list", "Display", "List connected monitors",
       "system_profiler SPDisplaysDataType", platforms=("Darwin",), reads=True),
    _c("display.modes", "Display", "List every resolution each monitor supports",
       "xrandr", requires=("xrandr",), session="x11", reads=True),
    _c("display.configure", "Display", "Reconfigure a monitor (full compositor spec)",
       "hyprctl keyword monitor {value}", requires=("hyprctl",), value="text"),
    _c("display.configure", "Display", "Reconfigure a monitor (mode/position/scale)",
       "wlr-randr {value}", requires=("wlr-randr",), value="text"),
    _c("display.configure", "Display", "Reconfigure a monitor (xrandr arguments)",
       "xrandr {value}", requires=("xrandr",), session="x11", value="text"),
    _c("display.rotate", "Display", "Rotate the primary display",
       "hyprctl keyword monitor \",preferred,auto,1,transform,{value}\"",
       requires=("hyprctl",), value="choice", choices=("0", "1", "2", "3")),
    _c("display.rotate", "Display", "Rotate the primary display",
       "xrandr --output \"$(xrandr | awk '/ connected/{{print $1; exit}}')\" --rotate {value}",
       requires=("xrandr",), session="x11", value="choice",
       choices=("normal", "left", "right", "inverted")),
    _c("display.off", "Display", "Blank the screen now",
       "hyprctl dispatch dpms off", requires=("hyprctl",), fast=True),
    _c("display.off", "Display", "Blank the screen now",
       "swaymsg 'output * dpms off'", requires=("swaymsg",), fast=True),
    _c("display.off", "Display", "Blank the screen now",
       "xset dpms force off", requires=("xset",), session="x11", fast=True),
    _c("display.off", "Display", "Put the display to sleep",
       "pmset displaysleepnow", platforms=("Darwin",), fast=True),
    _c("display.on", "Display", "Wake the screen",
       "hyprctl dispatch dpms on", requires=("hyprctl",), fast=True),
    _c("display.on", "Display", "Wake the screen",
       "swaymsg 'output * dpms on'", requires=("swaymsg",), fast=True),
    _c("display.on", "Display", "Wake the screen",
       "xset dpms force on", requires=("xset",), session="x11", fast=True),
    _c("display.nightlight.on", "Display", "Warm the screen colour temperature (Kelvin)",
       "hyprsunset -t {value}", requires=("hyprsunset",), value="number", fast=True),
    _c("display.nightlight.on", "Display", "Warm the screen colour temperature (Kelvin)",
       "gammastep -O {value}", requires=("gammastep",), value="number", fast=True),
    _c("display.nightlight.on", "Display", "Warm the screen colour temperature (Kelvin)",
       "wlsunset -T {value} -t {value}", requires=("wlsunset",), value="number"),
    _c("display.nightlight.on", "Display", "Warm the screen colour temperature (Kelvin)",
       "redshift -O {value}", requires=("redshift",), value="number", fast=True),
    _c("display.nightlight.off", "Display", "Restore normal screen colour",
       "pkill hyprsunset || hyprctl hyprsunset identity", requires=("hyprsunset",), fast=True),
    _c("display.nightlight.off", "Display", "Restore normal screen colour",
       "gammastep -x", requires=("gammastep",), fast=True),
    _c("display.nightlight.off", "Display", "Restore normal screen colour",
       "redshift -x", requires=("redshift",), fast=True),
    _c("display.blank_timeout.set", "Display", "Seconds of idle before the screen blanks",
       "xset s {value}", requires=("xset",), session="x11", value="number"),
    _c("display.screensaver.off", "Display", "Stop the screen blanking",
       "xset s off -dpms", requires=("xset",), session="x11"),

    # ------------------------------------------------------------------ audio
    _c("audio.volume.get", "Audio", "Read the output volume",
       "wpctl get-volume @DEFAULT_AUDIO_SINK@", requires=("wpctl",), reads=True, fast=True),
    _c("audio.volume.get", "Audio", "Read the output volume",
       "pactl get-sink-volume @DEFAULT_SINK@", requires=("pactl",), reads=True, fast=True),
    _c("audio.volume.get", "Audio", "Read the output volume",
       "amixer get Master", requires=("amixer",), reads=True, fast=True),
    _c("audio.volume.get", "Audio", "Read the output volume",
       "osascript -e 'output volume of (get volume settings)'",
       platforms=("Darwin",), reads=True, fast=True),
    _c("audio.volume.set", "Audio", "Set the output volume to a percentage",
       "wpctl set-volume @DEFAULT_AUDIO_SINK@ {value}%", requires=("wpctl",),
       value="percent", fast=True),
    _c("audio.volume.set", "Audio", "Set the output volume to a percentage",
       "pactl set-sink-volume @DEFAULT_SINK@ {value}%", requires=("pactl",),
       value="percent", fast=True),
    _c("audio.volume.set", "Audio", "Set the output volume to a percentage",
       "amixer -q set Master {value}%", requires=("amixer",), value="percent", fast=True),
    _c("audio.volume.set", "Audio", "Set the output volume to a percentage",
       "osascript -e 'set volume output volume {value}'", platforms=("Darwin",),
       value="percent", fast=True),
    _c("audio.volume.up", "Audio", "Raise the output volume by a step",
       "wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ {value}%+", requires=("wpctl",),
       value="step", fast=True),
    _c("audio.volume.up", "Audio", "Raise the output volume by a step",
       "pactl set-sink-volume @DEFAULT_SINK@ +{value}%", requires=("pactl",),
       value="step", fast=True),
    _c("audio.volume.up", "Audio", "Raise the output volume by a step",
       "amixer -q set Master {value}%+", requires=("amixer",), value="step", fast=True),
    _c("audio.volume.down", "Audio", "Lower the output volume by a step",
       "wpctl set-volume @DEFAULT_AUDIO_SINK@ {value}%-", requires=("wpctl",),
       value="step", fast=True),
    _c("audio.volume.down", "Audio", "Lower the output volume by a step",
       "pactl set-sink-volume @DEFAULT_SINK@ -{value}%", requires=("pactl",),
       value="step", fast=True),
    _c("audio.volume.down", "Audio", "Lower the output volume by a step",
       "amixer -q set Master {value}%-", requires=("amixer",), value="step", fast=True),
    _c("audio.mute", "Audio", "Mute the speakers",
       "wpctl set-mute @DEFAULT_AUDIO_SINK@ 1", requires=("wpctl",), fast=True),
    _c("audio.mute", "Audio", "Mute the speakers",
       "pactl set-sink-mute @DEFAULT_SINK@ 1", requires=("pactl",), fast=True),
    _c("audio.mute", "Audio", "Mute the speakers",
       "amixer -q set Master mute", requires=("amixer",), fast=True),
    _c("audio.mute", "Audio", "Mute the speakers",
       "osascript -e 'set volume output muted true'", platforms=("Darwin",), fast=True),
    _c("audio.unmute", "Audio", "Unmute the speakers",
       "wpctl set-mute @DEFAULT_AUDIO_SINK@ 0", requires=("wpctl",), fast=True),
    _c("audio.unmute", "Audio", "Unmute the speakers",
       "pactl set-sink-mute @DEFAULT_SINK@ 0", requires=("pactl",), fast=True),
    _c("audio.unmute", "Audio", "Unmute the speakers",
       "amixer -q set Master unmute", requires=("amixer",), fast=True),
    _c("audio.unmute", "Audio", "Unmute the speakers",
       "osascript -e 'set volume output muted false'", platforms=("Darwin",), fast=True),
    _c("audio.mute.toggle", "Audio", "Toggle speaker mute",
       "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle", requires=("wpctl",), fast=True),
    _c("audio.mute.toggle", "Audio", "Toggle speaker mute",
       "pactl set-sink-mute @DEFAULT_SINK@ toggle", requires=("pactl",), fast=True),
    _c("audio.mic.volume.set", "Audio", "Set microphone input level",
       "wpctl set-volume @DEFAULT_AUDIO_SOURCE@ {value}%", requires=("wpctl",),
       value="percent", fast=True),
    _c("audio.mic.volume.set", "Audio", "Set microphone input level",
       "pactl set-source-volume @DEFAULT_SOURCE@ {value}%", requires=("pactl",),
       value="percent", fast=True),
    _c("audio.mic.mute", "Audio", "Mute the microphone",
       "wpctl set-mute @DEFAULT_AUDIO_SOURCE@ 1", requires=("wpctl",), fast=True),
    _c("audio.mic.mute", "Audio", "Mute the microphone",
       "pactl set-source-mute @DEFAULT_SOURCE@ 1", requires=("pactl",), fast=True),
    _c("audio.mic.unmute", "Audio", "Unmute the microphone",
       "wpctl set-mute @DEFAULT_AUDIO_SOURCE@ 0", requires=("wpctl",), fast=True),
    _c("audio.mic.unmute", "Audio", "Unmute the microphone",
       "pactl set-source-mute @DEFAULT_SOURCE@ 0", requires=("pactl",), fast=True),
    _c("audio.outputs.list", "Audio", "List audio outputs (sinks)",
       "wpctl status", requires=("wpctl",), reads=True, fast=True),
    _c("audio.outputs.list", "Audio", "List audio outputs (sinks)",
       "pactl list short sinks", requires=("pactl",), reads=True, fast=True),
    _c("audio.inputs.list", "Audio", "List audio inputs (sources)",
       "pactl list short sources", requires=("pactl",), reads=True, fast=True),
    _c("audio.output.set", "Audio", "Route audio to a specific output (id or name)",
       "wpctl set-default {value}", requires=("wpctl",), value="text"),
    _c("audio.output.set", "Audio", "Route audio to a specific output (sink name)",
       "pactl set-default-sink {value}", requires=("pactl",), value="text"),
    _c("audio.input.set", "Audio", "Use a specific microphone (source name)",
       "pactl set-default-source {value}", requires=("pactl",), value="text"),
    _c("audio.app.volume.set", "Audio", "Set one application's volume (sink-input index)",
       "pactl set-sink-input-volume {index} {value}%", requires=("pactl",),
       value="percent", params=("index", "value")),
    _c("audio.streams.list", "Audio", "List per-application audio streams",
       "pactl list short sink-inputs", requires=("pactl",), reads=True),

    # ------------------------------------------------------------------ media
    _c("media.playpause", "Media", "Play or pause whatever is playing",
       "playerctl play-pause", requires=("playerctl",), fast=True),
    _c("media.playpause", "Media", "Play or pause whatever is playing",
       "osascript -e 'tell application \"Music\" to playpause'", platforms=("Darwin",), fast=True),
    _c("media.play", "Media", "Resume playback", "playerctl play",
       requires=("playerctl",), fast=True),
    _c("media.pause", "Media", "Pause playback", "playerctl pause",
       requires=("playerctl",), fast=True),
    _c("media.stop", "Media", "Stop playback", "playerctl stop",
       requires=("playerctl",), fast=True),
    _c("media.next", "Media", "Skip to the next track", "playerctl next",
       requires=("playerctl",), fast=True),
    _c("media.previous", "Media", "Go back to the previous track", "playerctl previous",
       requires=("playerctl",), fast=True),
    _c("media.seek", "Media", "Seek forward this many seconds",
       "playerctl position {value}+", requires=("playerctl",), value="number", fast=True),
    _c("media.volume.set", "Media", "Set the media player's own volume (0-100)",
       "playerctl volume $(awk \"BEGIN{{print {value}/100}}\")",
       requires=("playerctl",), value="percent", fast=True),
    _c("media.shuffle.toggle", "Media", "Toggle shuffle", "playerctl shuffle Toggle",
       requires=("playerctl",), fast=True),
    _c("media.loop.set", "Media", "Set repeat mode", "playerctl loop {value}",
       requires=("playerctl",), value="choice", choices=("None", "Track", "Playlist"), fast=True),
    _c("media.now_playing", "Media", "What is currently playing",
       "playerctl metadata --format '{{{{artist}}}} — {{{{title}}}} ({{{{playerName}}}})'",
       requires=("playerctl",), reads=True, fast=True),
    _c("media.players.list", "Media", "List media players that can be controlled",
       "playerctl -l", requires=("playerctl",), reads=True, fast=True),

    # ---------------------------------------------------------------- network
    _c("network.status", "Network", "Overall connectivity status",
       "nmcli general status", requires=("nmcli",), reads=True, fast=True),
    _c("network.interfaces", "Network", "List network interfaces and addresses",
       "ip -br addr", requires=("ip",), reads=True, fast=True),
    _c("network.interfaces", "Network", "List network interfaces and addresses",
       "ifconfig", platforms=("Darwin",), requires=("ifconfig",), reads=True),
    _c("network.ip.local", "Network", "This machine's LAN address",
       "ip -4 -br addr show scope global", requires=("ip",), reads=True, fast=True),
    _c("network.ip.public", "Network", "This machine's public address",
       "curl -s --max-time 8 https://api.ipify.org", requires=("curl",), reads=True, fast=True),
    _c("network.wifi.list", "Network", "Scan for nearby Wi-Fi networks",
       "nmcli -f SSID,SIGNAL,SECURITY dev wifi list --rescan yes",
       requires=("nmcli",), reads=True, fast=True, timeout=45),
    _c("network.wifi.list", "Network", "List known Wi-Fi networks",
       "networksetup -listpreferredwirelessnetworks en0",
       platforms=("Darwin",), reads=True, fast=True),
    _c("network.wifi.list", "Network", "Scan for nearby Wi-Fi networks",
       "netsh wlan show networks", platforms=("Windows",), reads=True, fast=True),
    _c("network.wifi.current", "Network", "Which Wi-Fi network is connected",
       "nmcli -t -f ACTIVE,SSID,SIGNAL dev wifi | grep '^yes'",
       requires=("nmcli",), reads=True, fast=True),
    _c("network.wifi.connect", "Network", "Join an open Wi-Fi network by SSID",
       "nmcli dev wifi connect {value}", requires=("nmcli",), value="text", timeout=60),
    _c("network.wifi.connect_password", "Network", "Join a Wi-Fi network with a password",
       "nmcli dev wifi connect {ssid} password {password}", requires=("nmcli",),
       value="text", params=("ssid", "password"), timeout=60),
    _c("network.wifi.disconnect", "Network", "Leave the current Wi-Fi network",
       "nmcli con down id {value}", requires=("nmcli",), value="text"),
    _c("network.wifi.forget", "Network", "Forget a saved Wi-Fi network",
       "nmcli connection delete id {value}", requires=("nmcli",), value="text", risky=True),
    _c("network.wifi.saved", "Network", "List saved connections",
       "nmcli -f NAME,TYPE,DEVICE connection show", requires=("nmcli",), reads=True, fast=True),
    _c("network.wifi.on", "Network", "Turn the Wi-Fi radio on",
       "nmcli radio wifi on", requires=("nmcli",), fast=True),
    _c("network.wifi.on", "Network", "Turn the Wi-Fi radio on",
       "networksetup -setairportpower en0 on", platforms=("Darwin",), fast=True),
    _c("network.wifi.off", "Network", "Turn the Wi-Fi radio off",
       "nmcli radio wifi off", requires=("nmcli",), fast=True),
    _c("network.wifi.off", "Network", "Turn the Wi-Fi radio off",
       "networksetup -setairportpower en0 off", platforms=("Darwin",), fast=True),
    _c("network.airplane.on", "Network", "Block every radio (airplane mode)",
       "rfkill block all", requires=("rfkill",), fast=True),
    _c("network.airplane.off", "Network", "Unblock every radio",
       "rfkill unblock all", requires=("rfkill",), fast=True),
    _c("network.radios", "Network", "Show radio (Wi-Fi/Bluetooth) block state",
       "rfkill list", requires=("rfkill",), reads=True, fast=True),
    _c("network.ethernet.up", "Network", "Bring an interface up",
       "nmcli dev connect {value}", requires=("nmcli",), value="text"),
    _c("network.ethernet.down", "Network", "Bring an interface down",
       "nmcli dev disconnect {value}", requires=("nmcli",), value="text"),
    _c("network.hotspot.start", "Network", "Share this connection as a Wi-Fi hotspot",
       "nmcli dev wifi hotspot ssid {ssid} password {password}",
       requires=("nmcli",), value="text", params=("ssid", "password")),
    _c("network.hotspot.stop", "Network", "Stop the Wi-Fi hotspot",
       "nmcli con down Hotspot", requires=("nmcli",)),
    _c("network.vpn.list", "Network", "List configured VPNs",
       "nmcli -f NAME,TYPE con show | grep -i vpn", requires=("nmcli",), reads=True),
    _c("network.vpn.up", "Network", "Connect a VPN by name",
       "nmcli con up id {value}", requires=("nmcli",), value="text", timeout=60),
    _c("network.vpn.down", "Network", "Disconnect a VPN by name",
       "nmcli con down id {value}", requires=("nmcli",), value="text"),
    _c("network.dns.show", "Network", "Show DNS servers in use",
       "resolvectl status", requires=("resolvectl",), reads=True, fast=True),
    _c("network.dns.flush", "Network", "Flush the DNS cache",
       "resolvectl flush-caches", requires=("resolvectl",), fast=True),
    _c("network.dns.set", "Network", "Set DNS servers for an interface",
       "resolvectl dns {iface} {servers}", requires=("resolvectl",),
       value="text", params=("iface", "servers"), sudo=True),
    _c("network.ping", "Network", "Ping a host four times",
       "ping -c 4 {value}", requires=("ping",), value="text", reads=True, fast=True, timeout=20),
    _c("network.trace", "Network", "Trace the route to a host",
       "traceroute -m 15 {value}", requires=("traceroute",), value="text", reads=True, timeout=60),
    _c("network.ports.listening", "Network", "What is listening on this machine",
       "ss -tulpn", requires=("ss",), reads=True, fast=True),
    _c("network.lan.neighbours", "Network", "Devices seen on the local network",
       "ip neigh show", requires=("ip",), reads=True, fast=True),
    _c("network.lan.scan", "Network", "Scan the local subnet for hosts",
       "nmap -sn {value}", requires=("nmap",), value="text", reads=True, timeout=120),
    _c("network.speedtest", "Network", "Measure connection speed",
       "speedtest-cli --simple", requires=("speedtest-cli",), reads=True, timeout=120),
    _c("network.firewall.status", "Network", "Firewall state",
       "ufw status verbose", requires=("ufw",), reads=True, sudo=True),
    _c("network.firewall.status", "Network", "Firewall state",
       "firewall-cmd --state", requires=("firewall-cmd",), reads=True),
    _c("network.firewall.enable", "Network", "Enable the firewall",
       "ufw --force enable", requires=("ufw",), sudo=True, risky=True),
    _c("network.firewall.disable", "Network", "Disable the firewall",
       "ufw disable", requires=("ufw",), sudo=True, risky=True),
    _c("network.hostname.set", "Network", "Change this machine's hostname",
       "hostnamectl set-hostname {value}", requires=("hostnamectl",),
       value="text", sudo=True, risky=True),

    # -------------------------------------------------------------- bluetooth
    _c("bluetooth.power.on", "Bluetooth", "Turn the Bluetooth radio on",
       "bluetoothctl power on", requires=("bluetoothctl",), fast=True),
    _c("bluetooth.power.off", "Bluetooth", "Turn the Bluetooth radio off",
       "bluetoothctl power off", requires=("bluetoothctl",), fast=True),
    _c("bluetooth.scan", "Bluetooth", "Scan for Bluetooth devices for 10s",
       "bluetoothctl --timeout 10 scan on", requires=("bluetoothctl",),
       reads=True, fast=True, timeout=25),
    _c("bluetooth.devices", "Bluetooth", "List known Bluetooth devices",
       "bluetoothctl devices", requires=("bluetoothctl",), reads=True, fast=True),
    _c("bluetooth.paired", "Bluetooth", "List paired Bluetooth devices",
       "bluetoothctl devices Paired", requires=("bluetoothctl",), reads=True, fast=True),
    _c("bluetooth.info", "Bluetooth", "Details for one Bluetooth device (MAC)",
       "bluetoothctl info {value}", requires=("bluetoothctl",), value="text", reads=True),
    _c("bluetooth.pair", "Bluetooth", "Pair a Bluetooth device by MAC",
       "bluetoothctl pair {value}", requires=("bluetoothctl",), value="text", timeout=45),
    _c("bluetooth.trust", "Bluetooth", "Trust a Bluetooth device by MAC",
       "bluetoothctl trust {value}", requires=("bluetoothctl",), value="text"),
    _c("bluetooth.connect", "Bluetooth", "Connect a Bluetooth device by MAC",
       "bluetoothctl connect {value}", requires=("bluetoothctl",), value="text", timeout=45),
    _c("bluetooth.disconnect", "Bluetooth", "Disconnect a Bluetooth device by MAC",
       "bluetoothctl disconnect {value}", requires=("bluetoothctl",), value="text"),
    _c("bluetooth.remove", "Bluetooth", "Forget a Bluetooth device by MAC",
       "bluetoothctl remove {value}", requires=("bluetoothctl",), value="text", risky=True),
    _c("bluetooth.discoverable", "Bluetooth", "Make this machine discoverable",
       "bluetoothctl discoverable on", requires=("bluetoothctl",)),

    # ------------------------------------------------------------------ power
    _c("power.battery", "Power", "Battery charge and health",
       "upower -i $(upower -e | grep -m1 BAT)", requires=("upower",), reads=True, fast=True),
    _c("power.battery", "Power", "Battery charge",
       "pmset -g batt", platforms=("Darwin",), reads=True, fast=True),
    _c("power.profile.get", "Power", "Current power profile",
       "powerprofilesctl get", requires=("powerprofilesctl",), reads=True, fast=True),
    _c("power.profile.get", "Power", "Current power profile",
       "tuned-adm active", requires=("tuned-adm",), reads=True, fast=True),
    _c("power.profile.set", "Power", "Switch power profile",
       "powerprofilesctl set {value}", requires=("powerprofilesctl",), value="choice",
       choices=("performance", "balanced", "power-saver"), fast=True),
    _c("power.profile.set", "Power", "Switch tuned profile",
       "tuned-adm profile {value}", requires=("tuned-adm",), value="text", sudo=True),
    _c("power.profiles.list", "Power", "Available power profiles",
       "powerprofilesctl list", requires=("powerprofilesctl",), reads=True, fast=True),
    _c("power.cpu.governor.set", "Power", "Set the CPU frequency governor",
       "cpupower frequency-set -g {value}", requires=("cpupower",), value="choice",
       choices=("performance", "powersave", "schedutil", "ondemand", "conservative"), sudo=True),
    _c("power.cpu.frequency", "Power", "Current CPU frequencies",
       "grep 'MHz' /proc/cpuinfo", reads=True, fast=True),
    _c("power.lock", "Power", "Lock the session",
       "loginctl lock-session", requires=("loginctl",), fast=True),
    _c("power.lock", "Power", "Lock the screen", "hyprlock", requires=("hyprlock",), fast=True),
    _c("power.lock", "Power", "Lock the screen", "swaylock -f", requires=("swaylock",), fast=True),
    _c("power.lock", "Power", "Lock the screen",
       "xdg-screensaver lock", requires=("xdg-screensaver",), session="x11", fast=True),
    _c("power.lock", "Power", "Lock the screen",
       "pmset displaysleepnow", platforms=("Darwin",), fast=True),
    _c("power.lock", "Power", "Lock the workstation",
       "rundll32.exe user32.dll,LockWorkStation", platforms=("Windows",), fast=True),
    _c("power.suspend", "Power", "Suspend to RAM",
       "systemctl suspend", requires=("systemctl",), risky=True),
    _c("power.suspend", "Power", "Sleep now", "pmset sleepnow", platforms=("Darwin",), risky=True),
    _c("power.hibernate", "Power", "Hibernate to disk",
       "systemctl hibernate", requires=("systemctl",), risky=True),
    _c("power.reboot", "Power", "Restart the machine",
       "systemctl reboot", requires=("systemctl",), risky=True),
    _c("power.reboot", "Power", "Restart the machine",
       "shutdown -r now", platforms=("Darwin",), sudo=True, risky=True),
    _c("power.reboot", "Power", "Restart the machine",
       "shutdown /r /t 0", platforms=("Windows",), risky=True),
    _c("power.shutdown", "Power", "Power the machine off",
       "systemctl poweroff", requires=("systemctl",), risky=True),
    _c("power.shutdown", "Power", "Power the machine off",
       "shutdown -h now", platforms=("Darwin",), sudo=True, risky=True),
    _c("power.shutdown", "Power", "Power the machine off",
       "shutdown /s /t 0", platforms=("Windows",), risky=True),
    _c("power.logout", "Power", "End the desktop session",
       "hyprctl dispatch exit", requires=("hyprctl",), risky=True),
    _c("power.logout", "Power", "End the desktop session",
       "swaymsg exit", requires=("swaymsg",), risky=True),
    _c("power.logout", "Power", "End the desktop session",
       "loginctl terminate-session \"$XDG_SESSION_ID\"", requires=("loginctl",), risky=True),
    _c("power.idle.inhibit", "Power", "Keep the machine awake for N seconds",
       "systemd-inhibit --what=idle:sleep --why='JARVIS' sleep {value}",
       requires=("systemd-inhibit",), value="number"),
    _c("power.uptime", "Power", "How long the machine has been up",
       "uptime -p", reads=True, fast=True),
    _c("power.wake.schedule", "Power", "Wake the machine at a time (HH:MM)",
       "rtcwake -m no -t $(date -d 'today {value}' +%s)",
       requires=("rtcwake",), value="text", sudo=True),

    # --------------------------------------------------------------- services
    _c("service.status", "Services", "Status of a systemd unit",
       "systemctl status {value} --no-pager", requires=("systemctl",),
       value="text", reads=True, fast=True),
    _c("service.start", "Services", "Start a systemd unit",
       "systemctl start {value}", requires=("systemctl",), value="text", sudo=True),
    _c("service.stop", "Services", "Stop a systemd unit",
       "systemctl stop {value}", requires=("systemctl",), value="text", sudo=True),
    _c("service.restart", "Services", "Restart a systemd unit",
       "systemctl restart {value}", requires=("systemctl",), value="text", sudo=True),
    _c("service.enable", "Services", "Enable a unit at boot",
       "systemctl enable --now {value}", requires=("systemctl",), value="text", sudo=True),
    _c("service.disable", "Services", "Disable a unit at boot",
       "systemctl disable --now {value}", requires=("systemctl",), value="text", sudo=True),
    _c("service.list", "Services", "Running services",
       "systemctl list-units --type=service --state=running --no-pager",
       requires=("systemctl",), reads=True, fast=True),
    _c("service.failed", "Services", "Failed services",
       "systemctl --failed --no-pager", requires=("systemctl",), reads=True, fast=True),
    _c("service.user.status", "Services", "Status of a user unit",
       "systemctl --user status {value} --no-pager", requires=("systemctl",),
       value="text", reads=True, fast=True),
    _c("service.user.restart", "Services", "Restart a user unit",
       "systemctl --user restart {value}", requires=("systemctl",), value="text"),
    _c("service.user.list", "Services", "Running user services",
       "systemctl --user list-units --type=service --state=running --no-pager",
       requires=("systemctl",), reads=True, fast=True),
    _c("service.logs", "Services", "Recent log lines for a unit",
       "journalctl -u {value} -n 60 --no-pager", requires=("journalctl",),
       value="text", reads=True, fast=True),
    _c("service.list", "Services", "Loaded launch agents",
       "launchctl list", platforms=("Darwin",), reads=True, fast=True),
    _c("service.list", "Services", "Running services",
       "powershell -Command \"Get-Service | Where-Object Status -eq Running\"",
       platforms=("Windows",), reads=True, fast=True),

    # ----------------------------------------------------------------- system
    _c("system.info", "System", "Kernel and machine identity",
       "uname -a", reads=True, fast=True),
    _c("system.os", "System", "Distribution / OS release",
       "cat /etc/os-release", reads=True, fast=True),
    _c("system.temperature", "System", "Temperature sensors",
       "sensors", requires=("sensors",), reads=True, fast=True),
    _c("system.fans", "System", "Fan speeds",
       "sensors | grep -i -A1 fan", requires=("sensors",), reads=True, fast=True),
    _c("system.memory", "System", "Memory use",
       "free -h", requires=("free",), reads=True, fast=True),
    _c("system.memory", "System", "Memory use",
       "vm_stat", platforms=("Darwin",), reads=True, fast=True),
    _c("system.load", "System", "Load average and uptime",
       "uptime", reads=True, fast=True),
    _c("system.cpu", "System", "CPU model and topology",
       "lscpu", requires=("lscpu",), reads=True, fast=True),
    _c("system.gpu", "System", "GPU status",
       "nvidia-smi", requires=("nvidia-smi",), reads=True, fast=True),
    _c("system.gpu", "System", "Graphics hardware",
       "lspci | grep -i -E 'vga|3d|display'", requires=("lspci",), reads=True, fast=True),
    _c("system.processes.top", "System", "Processes using the most CPU",
       "ps -eo pid,pcpu,pmem,comm --sort=-pcpu | head -20",
       requires=("ps",), reads=True, fast=True),
    _c("system.processes.memory", "System", "Processes using the most memory",
       "ps -eo pid,pcpu,pmem,comm --sort=-pmem | head -20",
       requires=("ps",), reads=True, fast=True),
    _c("system.process.find", "System", "Find processes by name",
       "pgrep -af {value}", requires=("pgrep",), value="text", reads=True, fast=True),
    _c("system.process.kill", "System", "Kill processes matching a name",
       "pkill -f {value}", requires=("pkill",), value="text", risky=True),
    _c("system.process.kill_pid", "System", "Kill one process by PID",
       "kill -TERM {value}", requires=("kill",), value="number", risky=True),
    _c("system.usb.list", "System", "Attached USB devices",
       "lsusb", requires=("lsusb",), reads=True, fast=True),
    _c("system.pci.list", "System", "PCI devices",
       "lspci", requires=("lspci",), reads=True, fast=True),
    _c("system.modules", "System", "Loaded kernel modules",
       "lsmod", requires=("lsmod",), reads=True, fast=True),
    _c("system.module.load", "System", "Load a kernel module",
       "modprobe {value}", requires=("modprobe",), value="text", sudo=True),
    _c("system.module.unload", "System", "Unload a kernel module",
       "modprobe -r {value}", requires=("modprobe",), value="text", sudo=True, risky=True),
    _c("system.kernel.log", "System", "Kernel ring buffer warnings and errors",
       "dmesg --level=err,warn | tail -40", requires=("dmesg",), reads=True),
    _c("system.logs.recent", "System", "Recent system log lines",
       "journalctl -n 100 --no-pager", requires=("journalctl",), reads=True, fast=True),
    _c("system.logs.errors", "System", "Errors since boot",
       "journalctl -p err -b --no-pager | tail -60",
       requires=("journalctl",), reads=True, fast=True),
    _c("system.boot.time", "System", "Boot performance breakdown",
       "systemd-analyze", requires=("systemd-analyze",), reads=True, fast=True),
    _c("system.users", "System", "Who is logged in", "who", requires=("who",), reads=True, fast=True),
    _c("system.time", "System", "Clock, timezone, and NTP state",
       "timedatectl", requires=("timedatectl",), reads=True, fast=True),
    _c("system.timezone.set", "System", "Change the timezone",
       "timedatectl set-timezone {value}", requires=("timedatectl",), value="text", sudo=True),
    _c("system.ntp.enable", "System", "Sync the clock over the network",
       "timedatectl set-ntp true", requires=("timedatectl",), sudo=True),
    _c("system.locale", "System", "Locale settings",
       "localectl status", requires=("localectl",), reads=True, fast=True),
    _c("system.locale.set", "System", "Change the system locale",
       "localectl set-locale LANG={value}", requires=("localectl",), value="text", sudo=True),
    _c("system.keymap.set", "System", "Change the console/X keyboard layout",
       "localectl set-x11-keymap {value}", requires=("localectl",), value="text", sudo=True),

    # ---------------------------------------------------------------- storage
    _c("storage.disks", "Storage", "Block devices and mount points",
       "lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,LABEL", requires=("lsblk",), reads=True, fast=True),
    _c("storage.disks", "Storage", "Disks and volumes",
       "diskutil list", platforms=("Darwin",), reads=True, fast=True),
    _c("storage.usage", "Storage", "Free space per filesystem",
       "df -h", requires=("df",), reads=True, fast=True),
    _c("storage.largest", "Storage", "Biggest directories under a path",
       "du -h -d 2 {value} 2>/dev/null | sort -rh | head -20",
       requires=("du",), value="path", reads=True, timeout=90),
    _c("storage.mount", "Storage", "Mount a block device (e.g. /dev/sdb1)",
       "udisksctl mount -b {value}", requires=("udisksctl",), value="text"),
    _c("storage.unmount", "Storage", "Unmount a block device",
       "udisksctl unmount -b {value}", requires=("udisksctl",), value="text"),
    _c("storage.eject", "Storage", "Power off a removable drive",
       "udisksctl power-off -b {value}", requires=("udisksctl",), value="text"),
    _c("storage.smart", "Storage", "Drive health (SMART)",
       "smartctl -H {value}", requires=("smartctl",), value="text", reads=True, sudo=True),
    _c("storage.trash", "Storage", "Move a file to the trash",
       "gio trash {value}", requires=("gio",), value="path"),

    # -------------------------------------------------------------- packages
    _c("packages.search", "Packages", "Search the package repositories",
       "pacman -Ss {value}", requires=("pacman",), value="text", reads=True, fast=True),
    _c("packages.search", "Packages", "Search the package repositories",
       "apt-cache search {value}", requires=("apt-cache",), value="text", reads=True, fast=True),
    _c("packages.search", "Packages", "Search the package repositories",
       "dnf search {value}", requires=("dnf",), value="text", reads=True, fast=True),
    _c("packages.search", "Packages", "Search the package repositories",
       "zypper search {value}", requires=("zypper",), value="text", reads=True),
    _c("packages.search", "Packages", "Search the package repositories",
       "brew search {value}", platforms=("Darwin",), requires=("brew",),
       value="text", reads=True, fast=True),
    _c("packages.search", "Packages", "Search the package repositories",
       "winget search {value}", platforms=("Windows",), value="text", reads=True, fast=True),
    _c("packages.install", "Packages", "Install a package",
       "pacman -S --noconfirm {value}", requires=("pacman",),
       value="text", sudo=True, timeout=600),
    _c("packages.install", "Packages", "Install a package",
       "apt-get install -y {value}", requires=("apt-get",), value="text", sudo=True, timeout=600),
    _c("packages.install", "Packages", "Install a package",
       "dnf install -y {value}", requires=("dnf",), value="text", sudo=True, timeout=600),
    _c("packages.install", "Packages", "Install a package",
       "zypper --non-interactive install {value}", requires=("zypper",),
       value="text", sudo=True, timeout=600),
    _c("packages.install", "Packages", "Install a package",
       "brew install {value}", platforms=("Darwin",), requires=("brew",),
       value="text", timeout=600),
    _c("packages.install", "Packages", "Install a package",
       "winget install --silent {value}", platforms=("Windows",), value="text", timeout=600),
    _c("packages.remove", "Packages", "Remove a package",
       "pacman -Rns --noconfirm {value}", requires=("pacman",),
       value="text", sudo=True, risky=True, timeout=300),
    _c("packages.remove", "Packages", "Remove a package",
       "apt-get remove -y {value}", requires=("apt-get",),
       value="text", sudo=True, risky=True, timeout=300),
    _c("packages.remove", "Packages", "Remove a package",
       "dnf remove -y {value}", requires=("dnf",), value="text",
       sudo=True, risky=True, timeout=300),
    _c("packages.remove", "Packages", "Remove a package",
       "brew uninstall {value}", platforms=("Darwin",), requires=("brew",),
       value="text", risky=True, timeout=300),
    _c("packages.update", "Packages", "Update every package",
       "pacman -Syu --noconfirm", requires=("pacman",), sudo=True, timeout=1800),
    _c("packages.update", "Packages", "Update every package",
       "apt-get update && apt-get upgrade -y", requires=("apt-get",), sudo=True, timeout=1800),
    _c("packages.update", "Packages", "Update every package",
       "dnf upgrade -y", requires=("dnf",), sudo=True, timeout=1800),
    _c("packages.update", "Packages", "Update every package",
       "brew update && brew upgrade", platforms=("Darwin",), requires=("brew",), timeout=1800),
    _c("packages.update", "Packages", "Update every package",
       "winget upgrade --all --silent", platforms=("Windows",), timeout=1800),
    _c("packages.installed", "Packages", "List installed packages",
       "pacman -Q", requires=("pacman",), reads=True, fast=True),
    _c("packages.installed", "Packages", "List installed packages",
       "apt list --installed", requires=("apt",), reads=True, fast=True),
    _c("packages.installed", "Packages", "List installed packages",
       "brew list", platforms=("Darwin",), requires=("brew",), reads=True, fast=True),
    _c("packages.info", "Packages", "Details about a package",
       "pacman -Si {value}", requires=("pacman",), value="text", reads=True, fast=True),
    _c("packages.owns", "Packages", "Which package owns a file",
       "pacman -Qo {value}", requires=("pacman",), value="path", reads=True, fast=True),
    _c("packages.orphans", "Packages", "Orphaned packages that can be removed",
       "pacman -Qtdq", requires=("pacman",), reads=True, fast=True),
    _c("packages.aur.install", "Packages", "Install from the AUR",
       "yay -S --noconfirm {value}", requires=("yay",), value="text", timeout=1800),
    _c("packages.aur.install", "Packages", "Install from the AUR",
       "paru -S --noconfirm {value}", requires=("paru",), value="text", timeout=1800),
    _c("flatpak.search", "Packages", "Search Flathub",
       "flatpak search {value}", requires=("flatpak",), value="text", reads=True, fast=True),
    _c("flatpak.install", "Packages", "Install a Flatpak application",
       "flatpak install -y flathub {value}", requires=("flatpak",), value="text", timeout=1800),
    _c("flatpak.list", "Packages", "Installed Flatpaks",
       "flatpak list --app", requires=("flatpak",), reads=True, fast=True),
    _c("flatpak.update", "Packages", "Update Flatpaks",
       "flatpak update -y", requires=("flatpak",), timeout=1800),
    _c("flatpak.remove", "Packages", "Remove a Flatpak application",
       "flatpak uninstall -y {value}", requires=("flatpak",),
       value="text", risky=True, timeout=300),
    _c("snap.install", "Packages", "Install a snap",
       "snap install {value}", requires=("snap",), value="text", sudo=True, timeout=1800),
    _c("snap.list", "Packages", "Installed snaps",
       "snap list", requires=("snap",), reads=True, fast=True),

    # ---------------------------------------------------------------- session
    _c("session.windows.list", "Session", "Open windows",
       "hyprctl clients", requires=("hyprctl",), reads=True, fast=True),
    _c("session.windows.list", "Session", "Open windows",
       "swaymsg -t get_tree", requires=("swaymsg",), reads=True, fast=True),
    _c("session.windows.list", "Session", "Open windows",
       "wmctrl -l", requires=("wmctrl",), session="x11", reads=True, fast=True),
    _c("session.windows.list", "Session", "Open windows",
       "xdotool search --name '.*' getwindowname %@",
       requires=("xdotool",), session="x11", reads=True),
    _c("session.window.active", "Session", "The focused window",
       "hyprctl activewindow", requires=("hyprctl",), reads=True, fast=True),
    _c("session.window.focus", "Session", "Focus a window by title match",
       "hyprctl dispatch focuswindow title:{value}", requires=("hyprctl",), value="text"),
    _c("session.window.focus", "Session", "Focus a window by title match",
       "wmctrl -a {value}", requires=("wmctrl",), session="x11", value="text"),
    _c("session.window.close", "Session", "Close the focused window",
       "hyprctl dispatch killactive", requires=("hyprctl",)),
    _c("session.window.close", "Session", "Close the focused window",
       "swaymsg kill", requires=("swaymsg",)),
    _c("session.window.fullscreen", "Session", "Toggle fullscreen on the focused window",
       "hyprctl dispatch fullscreen 1", requires=("hyprctl",), fast=True),
    _c("session.window.float", "Session", "Toggle floating on the focused window",
       "hyprctl dispatch togglefloating", requires=("hyprctl",), fast=True),
    _c("session.workspace.switch", "Session", "Switch to a workspace",
       "hyprctl dispatch workspace {value}", requires=("hyprctl",), value="number", fast=True),
    _c("session.workspace.switch", "Session", "Switch to a workspace",
       "swaymsg workspace number {value}", requires=("swaymsg",), value="number", fast=True),
    _c("session.workspace.list", "Session", "Workspaces in use",
       "hyprctl workspaces", requires=("hyprctl",), reads=True, fast=True),
    _c("session.workspace.move_window", "Session", "Move the focused window to a workspace",
       "hyprctl dispatch movetoworkspace {value}", requires=("hyprctl",), value="number"),
    _c("session.exec", "Session", "Launch a program in the desktop session",
       "hyprctl dispatch exec {value}", requires=("hyprctl",), value="text"),
    _c("session.exec", "Session", "Launch a program in the desktop session",
       "swaymsg exec {value}", requires=("swaymsg",), value="text"),
    _c("session.reload", "Session", "Reload the compositor config",
       "hyprctl reload", requires=("hyprctl",)),
    _c("session.setting", "Session", "Set a compositor option at runtime",
       "hyprctl keyword {value}", requires=("hyprctl",), value="text"),
    _c("session.screenshot.full", "Session", "Screenshot the whole screen to a file",
       "grim {value}", requires=("grim",), value="path", fast=True),
    _c("session.screenshot.full", "Session", "Screenshot the whole screen to a file",
       "maim {value}", requires=("maim",), session="x11", value="path", fast=True),
    _c("session.screenshot.full", "Session", "Screenshot the whole screen to a file",
       "scrot {value}", requires=("scrot",), session="x11", value="path", fast=True),
    _c("session.screenshot.full", "Session", "Screenshot the whole screen to a file",
       "screencapture -x {value}", platforms=("Darwin",), value="path", fast=True),
    _c("session.screenshot.region", "Session", "Screenshot a region you select",
       "grim -g \"$(slurp)\" {value}", requires=("grim", "slurp"), value="path", timeout=120),
    _c("session.screenshot.region", "Session", "Screenshot a region you select",
       "maim -s {value}", requires=("maim",), session="x11", value="path", timeout=120),
    _c("session.screenshot.clipboard", "Session", "Screenshot straight to the clipboard",
       "grim - | wl-copy", requires=("grim", "wl-copy"), fast=True),
    _c("session.record.start", "Session", "Record the screen to a file",
       "wf-recorder -f {value}", requires=("wf-recorder",), value="path", timeout=5),
    _c("session.record.stop", "Session", "Stop screen recording",
       "pkill -INT wf-recorder", requires=("wf-recorder",)),
    _c("session.clipboard.copy", "Session", "Put text on the clipboard",
       "printf %s {value} | wl-copy", requires=("wl-copy",), value="text", fast=True),
    _c("session.clipboard.copy", "Session", "Put text on the clipboard",
       "printf %s {value} | xclip -selection clipboard",
       requires=("xclip",), session="x11", value="text", fast=True),
    _c("session.clipboard.copy", "Session", "Put text on the clipboard",
       "printf %s {value} | pbcopy", platforms=("Darwin",), value="text", fast=True),
    _c("session.clipboard.paste", "Session", "Read the clipboard",
       "wl-paste", requires=("wl-paste",), reads=True, fast=True),
    _c("session.clipboard.paste", "Session", "Read the clipboard",
       "xclip -selection clipboard -o", requires=("xclip",), session="x11", reads=True, fast=True),
    _c("session.clipboard.paste", "Session", "Read the clipboard",
       "pbpaste", platforms=("Darwin",), reads=True, fast=True),
    _c("session.clipboard.clear", "Session", "Clear the clipboard",
       "wl-copy --clear", requires=("wl-copy",), fast=True),
    _c("session.notify", "Session", "Show a desktop notification",
       "notify-send JARVIS {value}", requires=("notify-send",), value="text", fast=True),
    _c("session.notify", "Session", "Show a desktop notification",
       "osascript -e 'display notification {value} with title \"JARVIS\"'",
       platforms=("Darwin",), value="text", fast=True),
    _c("session.type", "Session", "Type text into the focused window",
       "ydotool type {value}", requires=("ydotool",), value="text"),
    _c("session.type", "Session", "Type text into the focused window",
       "wtype {value}", requires=("wtype",), value="text"),
    _c("session.type", "Session", "Type text into the focused window",
       "xdotool type {value}", requires=("xdotool",), session="x11", value="text"),
    _c("session.key", "Session", "Send a key or chord to the focused window",
       "ydotool key {value}", requires=("ydotool",), value="text"),
    _c("session.key", "Session", "Send a key or chord to the focused window",
       "xdotool key {value}", requires=("xdotool",), session="x11", value="text"),
    _c("session.idle.state", "Session", "Idle inhibitors currently held",
       "systemd-inhibit --list", requires=("systemd-inhibit",), reads=True, fast=True),

    # ------------------------------------------------------------------ input
    _c("input.devices", "Input", "Keyboards, mice, and touchpads",
       "hyprctl devices", requires=("hyprctl",), reads=True, fast=True),
    _c("input.devices", "Input", "Keyboards, mice, and touchpads",
       "libinput list-devices", requires=("libinput",), reads=True, sudo=True),
    _c("input.devices", "Input", "Input devices",
       "xinput list", requires=("xinput",), session="x11", reads=True, fast=True),
    _c("input.sensitivity.set", "Input", "Pointer sensitivity (-1.0 to 1.0)",
       "hyprctl keyword input:sensitivity {value}", requires=("hyprctl",), value="number"),
    _c("input.natural_scroll", "Input", "Natural scrolling on or off",
       "hyprctl keyword input:natural_scroll {value}", requires=("hyprctl",),
       value="choice", choices=("true", "false")),
    _c("input.touchpad.tap", "Input", "Tap-to-click on or off",
       "hyprctl keyword input:touchpad:tap-to-click {value}", requires=("hyprctl",),
       value="choice", choices=("true", "false")),
    _c("input.touchpad.disable", "Input", "Disable an input device by name",
       "hyprctl keyword 'device[{value}]:enabled' false", requires=("hyprctl",), value="text"),
    _c("input.touchpad.enable", "Input", "Enable an input device by name",
       "hyprctl keyword 'device[{value}]:enabled' true", requires=("hyprctl",), value="text"),
    _c("input.repeat_rate.set", "Input", "Key repeat rate",
       "hyprctl keyword input:repeat_rate {value}", requires=("hyprctl",), value="number"),
    _c("input.layout.set", "Input", "Switch keyboard layout for this session",
       "hyprctl keyword input:kb_layout {value}", requires=("hyprctl",), value="text"),
    _c("input.layout.set", "Input", "Switch keyboard layout for this session",
       "setxkbmap {value}", requires=("setxkbmap",), session="x11", value="text"),
    _c("input.numlock", "Input", "Turn Num Lock on",
       "brightnessctl --device='input*::numlock' set 1", requires=("brightnessctl",)),

    # --------------------------------------------------------------- lighting
    _c("lighting.devices", "Lighting", "RGB controllers found",
       "openrgb --list-devices", requires=("openrgb",), reads=True, fast=True),
    _c("lighting.devices", "Lighting", "RGB devices found",
       "polychromatic-cli -k", requires=("polychromatic-cli",), reads=True, fast=True),
    _c("lighting.devices", "Lighting", "Aura lighting modes",
       "asusctl aura -h", requires=("asusctl",), reads=True, fast=True),
    _c("lighting.effect.set", "Lighting", "Set an RGB effect",
       "openrgb --mode {value}", requires=("openrgb",), value="text", fast=True),
    _c("lighting.effect.set", "Lighting", "Set an RGB effect",
       "polychromatic-cli -d all -o {value}", requires=("polychromatic-cli",),
       value="text", fast=True),
    _c("lighting.effect.set", "Lighting", "Set an Aura keyboard effect",
       "asusctl aura {value}", requires=("asusctl",), value="text", fast=True),
    _c("lighting.effect.set", "Lighting", "Set an OpenRazer effect",
       "razer-cli effect {value}", requires=("razer-cli",), value="text", fast=True),
    _c("lighting.color.set", "Lighting", "Set a static RGB colour (hex, no #)",
       "openrgb --mode static --color {value}", requires=("openrgb",), value="text", fast=True),
    _c("lighting.color.set", "Lighting", "Set a static RGB colour",
       "polychromatic-cli -d all -o static -c {value}", requires=("polychromatic-cli",),
       value="text", fast=True),
    _c("lighting.brightness.set", "Lighting", "Set RGB brightness",
       "openrgb --brightness {value}", requires=("openrgb",), value="percent", fast=True),
    _c("lighting.brightness.set", "Lighting", "Set keyboard Aura brightness",
       "asusctl aura brightness {value}", requires=("asusctl",), value="choice",
       choices=("off", "low", "med", "high"), fast=True),
    _c("lighting.off", "Lighting", "Turn RGB lighting off",
       "openrgb --mode off", requires=("openrgb",), fast=True),
    _c("lighting.off", "Lighting", "Turn RGB lighting off",
       "polychromatic-cli -d all -o none", requires=("polychromatic-cli",), fast=True),
    _c("lighting.off", "Lighting", "Turn keyboard lighting off",
       "asusctl aura brightness off", requires=("asusctl",), fast=True),
    _c("lighting.cooler", "Lighting", "Liquid cooler / AIO lighting and pump status",
       "liquidctl status", requires=("liquidctl",), reads=True),

    # --------------------------------------------------------- peripherals io
    _c("printer.list", "Peripherals", "Printers and the default",
       "lpstat -p -d", requires=("lpstat",), reads=True, fast=True),
    _c("printer.queue", "Peripherals", "Jobs waiting to print",
       "lpstat -o", requires=("lpstat",), reads=True, fast=True),
    _c("printer.print", "Peripherals", "Print a file on the default printer",
       "lp {value}", requires=("lp",), value="path"),
    _c("printer.cancel", "Peripherals", "Cancel a print job",
       "cancel {value}", requires=("cancel",), value="text"),
    _c("camera.list", "Peripherals", "Video capture devices",
       "v4l2-ctl --list-devices", requires=("v4l2-ctl",), reads=True, fast=True),
    _c("camera.snapshot", "Peripherals", "Grab one frame from the webcam",
       "ffmpeg -y -f v4l2 -i /dev/video0 -frames:v 1 {value}",
       requires=("ffmpeg",), value="path", timeout=30),
    _c("camera.snapshot", "Peripherals", "Grab one frame from the webcam",
       "imagesnap {value}", platforms=("Darwin",), requires=("imagesnap",), value="path"),
    _c("scanner.list", "Peripherals", "Scanners", "scanimage -L",
       requires=("scanimage",), reads=True, timeout=60),
    _c("scanner.scan", "Peripherals", "Scan a page to a file",
       "scanimage --format=png -o {value}", requires=("scanimage",), value="path", timeout=180),

    # -------------------------------------------------------------- container
    _c("containers.list", "Containers", "Running containers",
       "docker ps", requires=("docker",), reads=True, fast=True),
    _c("containers.list", "Containers", "Running containers",
       "podman ps", requires=("podman",), reads=True, fast=True),
    _c("containers.all", "Containers", "Every container",
       "docker ps -a", requires=("docker",), reads=True, fast=True),
    _c("containers.start", "Containers", "Start a container",
       "docker start {value}", requires=("docker",), value="text"),
    _c("containers.stop", "Containers", "Stop a container",
       "docker stop {value}", requires=("docker",), value="text", timeout=60),
    _c("containers.logs", "Containers", "Tail a container's logs",
       "docker logs --tail 100 {value}", requires=("docker",), value="text", reads=True),
    _c("containers.images", "Containers", "Local images",
       "docker images", requires=("docker",), reads=True, fast=True),
    _c("vms.list", "Containers", "Virtual machines",
       "virsh list --all", requires=("virsh",), reads=True, fast=True),
    _c("vms.start", "Containers", "Start a virtual machine",
       "virsh start {value}", requires=("virsh",), value="text"),
    _c("vms.stop", "Containers", "Shut down a virtual machine",
       "virsh shutdown {value}", requires=("virsh",), value="text"),
)


# ---------------------------------------------------------------------------
# Host probing
# ---------------------------------------------------------------------------


def _session_type() -> str:
    return (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()


def _supported(control: Control) -> bool:
    if platform.system() not in control.platforms:
        return False
    if control.session:
        session = _session_type()
        # An empty session type means we cannot tell — do not exclude on a guess.
        if session and session != control.session:
            return False
    if any(not os.environ.get(var) for var in control.env):
        return False
    return all(shutil.which(binary) for binary in control.requires)


@dataclass
class ControlCatalog:
    """Probed, deduplicated view of what this host can actually do."""

    ttl: float = 300.0
    _controls: list[Control] = field(default_factory=list)
    _by_id: dict[str, Control] = field(default_factory=dict)
    _probed_at: float = 0.0

    def refresh(self) -> list[Control]:
        seen: dict[str, Control] = {}
        for control in _CONTROLS:
            if control.id in seen:
                continue  # an earlier, higher-priority backend already won
            if _supported(control):
                seen[control.id] = control
        self._by_id = seen
        self._controls = list(seen.values())
        self._probed_at = time.time()
        return self._controls

    def all(self, *, refresh: bool = False) -> list[Control]:
        if refresh or not self._controls or time.time() - self._probed_at > self.ttl:
            self.refresh()
        return list(self._controls)

    def get(self, control_id: str) -> Control | None:
        self.all()
        return self._by_id.get(control_id)

    def ids(self) -> list[str]:
        return [c.id for c in self.all()]


_catalog = ControlCatalog()


def get_catalog() -> ControlCatalog:
    return _catalog


def available(*, refresh: bool = False) -> list[Control]:
    """Every control this host can actually perform, one backend per action."""
    return _catalog.all(refresh=refresh)


def get(control_id: str) -> Control | None:
    return _catalog.get(control_id)


def by_category(controls: Iterable[Control] | None = None) -> dict[str, list[Control]]:
    grouped: dict[str, list[Control]] = {}
    for control in controls if controls is not None else available():
        grouped.setdefault(control.category, []).append(control)
    return grouped


def unsupported_ids() -> list[str]:
    """Logical actions declared here that this host cannot perform."""
    have = set(_catalog.ids())
    return sorted({c.id for c in _CONTROLS} - have)


#: Packages that provide a control binary, so JARVIS can install its own missing
#: capabilities instead of reporting them as impossible.
_PROVIDERS: dict[str, dict[str, str]] = {
    "brightnessctl": {"pacman": "brightnessctl", "apt": "brightnessctl", "dnf": "brightnessctl"},
    "ddcutil": {"pacman": "ddcutil", "apt": "ddcutil", "dnf": "ddcutil"},
    "playerctl": {"pacman": "playerctl", "apt": "playerctl", "dnf": "playerctl"},
    "powerprofilesctl": {
        "pacman": "power-profiles-daemon",
        "apt": "power-profiles-daemon",
        "dnf": "power-profiles-daemon",
    },
    "openrgb": {"pacman": "openrgb", "apt": "openrgb", "dnf": "openrgb", "flatpak": "org.openrgb.OpenRGB"},
    "polychromatic-cli": {"pacman": "polychromatic", "apt": "polychromatic"},
    "asusctl": {"pacman": "asusctl"},
    "liquidctl": {"pacman": "liquidctl", "apt": "liquidctl", "dnf": "liquidctl"},
    "lpstat": {"pacman": "cups", "apt": "cups-client", "dnf": "cups-client"},
    "lp": {"pacman": "cups", "apt": "cups-client", "dnf": "cups-client"},
    "scanimage": {"pacman": "sane", "apt": "sane-utils", "dnf": "sane-backends"},
    "wf-recorder": {"pacman": "wf-recorder", "apt": "wf-recorder"},
    "nmap": {"pacman": "nmap", "apt": "nmap", "dnf": "nmap"},
    "traceroute": {"pacman": "traceroute", "apt": "traceroute", "dnf": "traceroute"},
    "speedtest-cli": {"pacman": "speedtest-cli", "apt": "speedtest-cli", "dnf": "speedtest-cli"},
    "smartctl": {"pacman": "smartmontools", "apt": "smartmontools", "dnf": "smartmontools"},
    "cpupower": {"pacman": "cpupower", "apt": "linux-cpupower", "dnf": "kernel-tools"},
    "ydotool": {"pacman": "ydotool", "apt": "ydotool"},
    "wtype": {"pacman": "wtype", "apt": "wtype"},
    "grim": {"pacman": "grim", "apt": "grim"},
    "slurp": {"pacman": "slurp", "apt": "slurp"},
    "maim": {"pacman": "maim", "apt": "maim"},
    "gammastep": {"pacman": "gammastep", "apt": "gammastep"},
    "hyprsunset": {"pacman": "hyprsunset"},
    "sensors": {"pacman": "lm_sensors", "apt": "lm-sensors", "dnf": "lm_sensors"},
    "v4l2-ctl": {"pacman": "v4l-utils", "apt": "v4l-utils", "dnf": "v4l-utils"},
    "udisksctl": {"pacman": "udisks2", "apt": "udisks2", "dnf": "udisks2"},
    "docker": {"pacman": "docker", "apt": "docker.io", "dnf": "docker"},
    "virsh": {"pacman": "libvirt", "apt": "libvirt-clients", "dnf": "libvirt-client"},
    "ufw": {"pacman": "ufw", "apt": "ufw", "dnf": "ufw"},
    "wmctrl": {"pacman": "wmctrl", "apt": "wmctrl", "dnf": "wmctrl"},
    "xdotool": {"pacman": "xdotool", "apt": "xdotool", "dnf": "xdotool"},
}


def _package_manager() -> tuple[str, str] | None:
    """The host's package manager and the command template to install with it."""
    for name, template in (
        ("pacman", "device_control action=control control=packages.install value={pkg}"),
        ("apt", "device_control action=control control=packages.install value={pkg}"),
        ("dnf", "device_control action=control control=packages.install value={pkg}"),
        ("flatpak", "device_control action=control control=flatpak.install value={pkg}"),
    ):
        if shutil.which(name):
            return name, template
    return None


def installable() -> list[dict[str, str]]:
    """Controls this host lacks but could gain by installing one package."""
    pm = _package_manager()
    if pm is None:
        return []
    manager, template = pm
    have = set(_catalog.ids())
    wanted: dict[str, set[str]] = {}
    for control in _CONTROLS:
        if control.id in have or platform.system() not in control.platforms:
            continue
        missing = [b for b in control.requires if not shutil.which(b)]
        if len(missing) != 1:
            continue  # ambiguous or already-satisfiable; not a one-package win
        pkg = _PROVIDERS.get(missing[0], {}).get(manager)
        if pkg:
            wanted.setdefault(pkg, set()).add(control.id)
    out: list[dict[str, str]] = []
    for pkg, ids in sorted(wanted.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out.append({
            "package": pkg,
            "manager": manager,
            "install": template.format(pkg=pkg),
            "unlocks": ", ".join(sorted(ids)),
            "count": str(len(ids)),
        })
    return out


def control_binaries() -> frozenset[str]:
    """Every binary this catalogue can drive — used to spot commands in prose."""
    out: set[str] = set()
    for control in _CONTROLS:
        out.update(control.requires)
        head = control.command.strip().split()
        if head:
            base = head[0].split("/")[-1]
            if base.isidentifier() or "-" in base or "." in base:
                out.add(base)
    out.discard("")
    return frozenset(out)


# ---------------------------------------------------------------------------
# Rendering for the prompt
# ---------------------------------------------------------------------------


#: One representative id per everyday intent. The full catalogue is ~200 actions;
#: dumping all of it into every prompt buries the user's actual request (a small model
#: then answers *about* the hardware instead of using it). These cover what gets asked
#: for out loud, and `action=controls query=…` retrieves the rest on demand.
_ESSENTIAL_IDS: tuple[str, ...] = (
    "display.brightness.set",
    "display.brightness.get",
    "display.keyboard_backlight.set",
    "display.list",
    "audio.volume.set",
    "audio.volume.get",
    "audio.mute.toggle",
    "audio.outputs.list",
    "media.playpause",
    "media.next",
    "media.now_playing",
    "power.battery",
    "power.lock",
    "power.uptime",
    "system.temperature",
    "system.load",
    "system.memory",
    "system.processes.top",
    "storage.usage",
    "network.status",
    "network.wifi.list",
    "network.wifi.connect",
    "network.ip.public",
    "bluetooth.devices",
    "packages.install",
    "packages.search",
    "session.screenshot.full",
    "session.notify",
    "session.windows.list",
    "service.status",
)


def format_controls_index(*, essentials: int = 24) -> str:
    """A compact index of what this host can do, plus how to look up the rest.

    Deliberately small. The contract with the model is "you have ~200 verified
    actions here, here are the everyday ids, search for anything else" — which
    leaves its attention on the request instead of on an inventory.
    """
    controls = available()
    if not controls:
        return ""
    grouped = by_category(controls)
    counts = " · ".join(
        f"{category} {len(items)}" for category, items in sorted(grouped.items())
    )
    by_id = {c.id: c for c in controls}
    picks = [by_id[cid] for cid in _ESSENTIAL_IDS if cid in by_id][:essentials]
    lines = [
        f"=== Device controls: {len(controls)} verified actions on this machine ===",
        "Perform one with device_control action=control, passing control= one of the ids "
        "below verbatim (and value= where the signature shows one). The id is the "
        "contract — the command, backend and OS quirks are already resolved for this "
        "host, so never compose or print shell syntax for anything in this catalogue, "
        "and never pass a placeholder in place of an id.",
        f"Categories: {counts}",
    ]
    if picks:
        lines.append("Everyday ids:")
        for control in picks:
            lines.append(f"  {control.signature()} — {control.summary}")
    lines.append(
        "That is a shortlist, not the limit. For anything else — and before concluding "
        "you cannot do something to this machine — call device_control "
        "action=controls query=<word> (e.g. query=bluetooth, query=nightlight) and it "
        "returns the matching ids; then perform one."
    )
    gaps = installable()
    if gaps:
        lines.append(
            "Missing a capability? These packages unlock more, and you may install them "
            "yourself with control=packages.install value=<pkg>: "
            + ", ".join(g["package"] for g in gaps[:8])
        )
    return "\n".join(lines)


def format_controls_context(*, max_per_category: int = 40) -> str:
    """The exhaustive command list. Opt-in via config.agent.full_device_context."""
    controls = available()
    if not controls:
        return ""
    grouped = by_category(controls)
    lines = [
        "=== Device controls available on this machine "
        f"({len(controls)} verified actions) ===",
        "Run any of these with device_control action=control, passing control= the id "
        "verbatim and value= where the signature shows one. "
        "The id is the contract; JARVIS already resolved the right backend command for "
        "this OS, session, and installed tools. Never print these commands as chat — "
        "call the tool.",
    ]
    for category in sorted(grouped):
        items = sorted(grouped[category], key=lambda c: c.id)
        acts = [c for c in items if not c.reads]
        reads = [c for c in items if c.reads]
        lines.append(f"\n{category}:")
        for control in acts[:max_per_category]:
            marks = []
            if control.risky:
                marks.append("confirm")
            if control.sudo:
                marks.append("sudo")
            suffix = f" [{', '.join(marks)}]" if marks else ""
            lines.append(f"  {control.signature()} — {control.summary}{suffix}")
        if len(acts) > max_per_category:
            lines.append(f"  … +{len(acts) - max_per_category} more in {category}")
        if reads:
            # Read-only probes are self-describing; list the ids and save the tokens.
            lines.append("  reads: " + ", ".join(c.signature() for c in reads))
    return "\n".join(lines)


def format_control_memories(hostname: str) -> list[str]:
    """One retrievable note per category, for semantic recall.

    Per-action notes would be ~200 rows and ~200 embedding calls on every host
    scan, for knowledge the system prompt already carries in full. One note per
    category is enough to answer "what can you do with the audio on this box?"
    """
    out: list[str] = []
    for category, controls in sorted(by_category().items()):
        actions = sorted(c.id for c in controls)
        summaries = "; ".join(
            f"{c.id} ({c.summary.lower()})" for c in sorted(controls, key=lambda c: c.id)[:14]
        )
        out.append(
            f"{category} controls on {hostname} ({len(actions)} actions): {summaries}. "
            f"Perform any of them with device_control action=control and the id "
            f"verbatim; list them with device_control action=controls "
            f"query={category.lower()}."
        )
    return out


# ---------------------------------------------------------------------------
# Natural-language resolution
# ---------------------------------------------------------------------------

_WORD_EDGE = r"(?<![a-z0-9])%s(?![a-z0-9])"
_PERCENT_RE = re.compile(r"(\d{1,3})\s*(?:%|percent|per cent)")
_TO_LEVEL_RE = re.compile(r"\b(?:to|at|by)\s+(\d{1,3})\b")
_BARE_NUMBER_RE = re.compile(r"\b(\d{1,3})\b")
_A_LOT = ("a lot", "way ", "much ", "significantly", "right down", "right up", "all the way")
_A_BIT = ("a bit", "a little", "slightly", "a touch", "a tad")


@dataclass(frozen=True)
class Resolution:
    control: Control
    params: dict[str, str]
    phrase: str
    confidence: float

    def render(self) -> str:
        return self.control.render(self.params)


def _phrase_hit(text: str, phrase: str) -> bool:
    return re.search(_WORD_EDGE % re.escape(phrase), text) is not None


def _amount(text: str, control: Control) -> str | None:
    """Pull a percentage or step size out of the request."""
    match = _PERCENT_RE.search(text) or _TO_LEVEL_RE.search(text)
    if match:
        return match.group(1)
    if control.value == "step":
        if any(w in text for w in _A_LOT):
            return "40"
        if any(w in text for w in _A_BIT):
            return "10"
        return _STEP_DEFAULTS.get(control.id, "10")
    if control.value in {"percent", "number"}:
        bare = _BARE_NUMBER_RE.search(text)
        if bare:
            return bare.group(1)
    return None


def resolve(text: str, *, fast_only: bool = False) -> Resolution | None:
    """Match a spoken request to one available control, with its value.

    Returns ``None`` unless the phrasing is unambiguous — this drives execution
    without an LLM round-trip, so a wrong guess is worse than no guess.
    """
    low = (text or "").strip().lower()
    if not low:
        return None
    best: tuple[int, Control, str] | None = None
    for control in available():
        if fast_only and not control.fast:
            continue
        for phrase in _PHRASES.get(control.id, ()):
            if not _phrase_hit(low, phrase):
                continue
            score = len(phrase)
            if best is None or score > best[0]:
                best = (score, control, phrase)
    if best is None:
        return None
    _, control, phrase = best

    params: dict[str, str] = {}
    if control.needs_value():
        if len(control.params) > 1:
            return None  # multi-parameter actions always go through the model
        if control.value == "choice":
            for choice in control.choices:
                if _phrase_hit(low, choice.lower()):
                    params["value"] = choice
                    break
            else:
                return None
        else:
            amount = _amount(low, control)
            if amount is None:
                return None
            params["value"] = amount

    # A long, complex sentence usually carries more intent than one action.
    confidence = 0.95 if len(low) <= 90 else 0.6
    if len(phrase) <= 4:
        confidence -= 0.2
    return Resolution(control, params, phrase, confidence)


#: How each directly-spoken action should be narrated once it has run.
#: ``{value}`` is the amount that was applied.
_SPEECH: dict[str, str] = {
    "display.brightness.set": "the screen is at {value}%",
    "display.brightness.down": "dimmed the screen by {value}%",
    "display.brightness.up": "brightened the screen by {value}%",
    "display.brightness.max": "the screen is at full brightness",
    "display.brightness.min": "the screen is as dim as it goes",
    "display.keyboard_backlight.set": "the keyboard backlight is at {value}%",
    "display.off": "the screen is off",
    "display.on": "the screen is awake",
    "display.nightlight.on": "night light is on at {value}K",
    "display.nightlight.off": "night light is off",
    "audio.volume.set": "the volume is at {value}%",
    "audio.volume.up": "turned the volume up {value}%",
    "audio.volume.down": "turned the volume down {value}%",
    "audio.mute": "the sound is muted",
    "audio.unmute": "the sound is back on",
    "audio.mute.toggle": "toggled mute",
    "audio.mic.mute": "the microphone is muted",
    "audio.mic.unmute": "the microphone is live again",
    "media.playpause": "toggled playback",
    "media.play": "playing",
    "media.pause": "paused",
    "media.stop": "stopped playback",
    "media.next": "skipped to the next track",
    "media.previous": "went back a track",
    "network.wifi.on": "Wi-Fi is on",
    "network.wifi.off": "Wi-Fi is off",
    "network.airplane.on": "airplane mode is on",
    "network.airplane.off": "airplane mode is off",
    "bluetooth.power.on": "Bluetooth is on",
    "bluetooth.power.off": "Bluetooth is off",
    "power.profile.set": "the power profile is {value}",
    "power.lock": "the session is locked",
    "power.suspend": "suspending",
    "packages.update": "updating every package",
    "session.screenshot.full": "captured the screen",
    "session.clipboard.clear": "cleared the clipboard",
}


def describe(resolution: Resolution) -> str:
    """Short spoken description of what a resolution did."""
    control = resolution.control
    value = resolution.params.get("value", "")
    template = _SPEECH.get(control.id)
    if template:
        return template.format(value=value)
    summary = control.summary[0].lower() + control.summary[1:]
    if not value:
        return summary
    return f"{summary} ({value})"

# ---------------------------------------------------------------------------
# Readouts: turn a probe's raw output into one spoken sentence.
# Only ids listed here can be answered without the model — everything else
# keeps its raw output and is handed to the model to phrase.
# ---------------------------------------------------------------------------

_TEMP_RE = re.compile(r"^([A-Za-z0-9_ .+-]+?):\s+\+?(-?\d+(?:\.\d+)?)\s*.C", re.MULTILINE)
_CPU_LABELS = ("tctl", "tdie", "package id", "core 0", "cpu", "k10temp", "coretemp")


def _read_brightness(out: str) -> str | None:
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 4 and parts[3].endswith("%"):
            return f"The screen is at {parts[3]}."
    return None


def _read_volume(out: str) -> str | None:
    match = re.search(r"Volume:\s*([0-9.]+)", out)
    if not match:
        match = re.search(r"(\d+)%", out)
        if not match:
            return None
        pct = int(match.group(1))
    else:
        pct = int(round(float(match.group(1)) * 100))
    muted = "MUTED" in out.upper()
    return f"The volume is at {pct}%" + (", though it is muted." if muted else ".")


def _read_battery(out: str) -> str | None:
    pct = re.search(r"percentage:\s*([\d.]+)%", out)
    state = re.search(r"state:\s*(\S+)", out)
    if not pct:
        pct = re.search(r"(\d+)%", out)
        if not pct:
            return None
    phrase = f"The battery is at {round(float(pct.group(1)))}%"
    if state:
        word = state.group(1).replace("-", " ")
        phrase += f", {word}" if word != "unknown" else ""
    left = re.search(r"time to empty:\s*(.+)", out)
    if left and state and "discharging" in state.group(1):
        phrase += f" — about {left.group(1).strip()} remaining"
    return phrase + "."


def _read_temperature(out: str) -> str | None:
    readings = [(label.strip(), float(value)) for label, value in _TEMP_RE.findall(out)]
    if not readings:
        return None
    cpu = [r for r in readings if any(k in r[0].lower() for k in _CPU_LABELS)]
    label, value = max(cpu or readings, key=lambda r: r[1])
    hottest = max(readings, key=lambda r: r[1])
    phrase = f"{label} is at {value:.0f}°C"
    if hottest[0] != label:
        phrase += f"; the hottest sensor is {hottest[0]} at {hottest[1]:.0f}°C"
    return phrase + "."


def _read_wifi(out: str) -> str | None:
    for line in out.splitlines():
        parts = line.strip().split(":")
        if len(parts) >= 2 and parts[0] == "yes":
            signal = f" at {parts[2]}% signal" if len(parts) > 2 and parts[2] else ""
            return f"Connected to {parts[1]}{signal}."
    return "Not connected to any Wi-Fi network." if out.strip() == "" else None


def _read_uptime(out: str) -> str | None:
    text = out.strip()
    return f"The machine has been {text}." if text.startswith("up") else None


def _read_memory(out: str) -> str | None:
    for line in out.splitlines():
        if line.lower().startswith("mem:"):
            cols = line.split()
            if len(cols) >= 4:
                return f"Using {cols[2]} of {cols[1]}, with {cols[3]} free."
    return None


def _read_disk(out: str) -> str | None:
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 6 and cols[5] == "/":
            return f"The root filesystem has {cols[3]} free of {cols[1]} ({cols[4]} used)."
    return None


def _read_playing(out: str) -> str | None:
    text = out.strip().splitlines()[0].strip() if out.strip() else ""
    return f"Now playing: {text}." if text else "Nothing is playing."


_READOUTS = {
    "display.brightness.get": _read_brightness,
    "display.keyboard_backlight.get": _read_brightness,
    "audio.volume.get": _read_volume,
    "power.battery": _read_battery,
    "power.uptime": _read_uptime,
    "system.temperature": _read_temperature,
    "system.memory": _read_memory,
    "storage.usage": _read_disk,
    "network.wifi.current": _read_wifi,
    "media.now_playing": _read_playing,
}


def has_readout(control_id: str) -> bool:
    return control_id in _READOUTS


def readout(control_id: str, output: str) -> str | None:
    """One spoken sentence for a probe's output, or None to let the model phrase it."""
    fn = _READOUTS.get(control_id)
    if fn is None:
        return None
    body = output
    if "\n(succeeded)\n" in body:
        body = body.split("\n(succeeded)\n", 1)[1]
    elif "\n(exited with code" in body:
        return None
    try:
        return fn(body)
    except Exception:  # noqa: BLE001
        return None


def _validate_tables() -> None:
    """Every id in the lookup tables must name a declared control."""
    declared = {c.id for c in _CONTROLS}
    for label, mapping in (
        ("_PHRASES", _PHRASES),
        ("_SPEECH", _SPEECH),
        ("_READOUTS", _READOUTS),
        ("_STEP_DEFAULTS", _STEP_DEFAULTS),
    ):
        unknown = sorted(set(mapping) - declared)
        if unknown:
            raise ValueError(f"controls.{label} names undeclared control ids: {unknown}")
        if label == "_READOUTS":
            not_reads = sorted(
                cid for cid in mapping
                if not any(c.id == cid and c.reads for c in _CONTROLS)
            )
            if not_reads:
                raise ValueError(f"controls._READOUTS covers non-read controls: {not_reads}")


_validate_tables()
