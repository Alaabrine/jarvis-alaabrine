"""Application catalog: everything installed on this host that JARVIS can launch.

The old device scan probed a hardcoded list of executables on ``PATH``, which made
Flatpak/Snap/Wine/macOS-bundle applications invisible to the agent. This module builds
a real inventory from XDG desktop entries, Flatpak/Snap refs, macOS app bundles, Windows
Start Menu shortcuts, and PATH utilities — then resolves a *purpose* ("roblox studio",
"photo editor", "vector graphics") to the application that serves it, so the agent can
act without being told which program to use.
"""

from __future__ import annotations

import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Executables worth advertising even though they have no desktop entry.
_CLI_CANDIDATES = [
    "git", "node", "npm", "npx", "python3", "pip", "uv", "uvx", "cargo", "rustc", "go",
    "java", "gcc", "make", "cmake", "docker", "podman", "kubectl", "ssh", "rsync",
    "ffmpeg", "imagemagick", "convert", "yt-dlp", "curl", "wget", "jq", "gh", "tmux",
    "vim", "nvim", "emacs", "nano", "code", "codium", "flatpak", "snap", "pacman",
    "apt", "dnf", "brew", "winget", "systemctl", "journalctl", "nmcli", "bluetoothctl",
    "pactl", "wpctl", "amixer", "brightnessctl", "xrandr", "swaymsg", "hyprctl",
    "playerctl", "powerprofilesctl", "upower", "rfkill", "ip", "openrgb",
    "polychromatic-cli", "razer-cli", "grim", "maim", "xdotool", "ydotool", "wtype",
]

# Field codes in a desktop-entry Exec line (XDG spec) — stripped before launching.
_FIELD_CODES = re.compile(r"%[fFuUdDnNickvm]")

# Purpose keywords for applications whose name does not say what they do. The agent
# matches a request against these, so "make a roblox studio project" finds Vinegar.
_PURPOSE_ALIASES: dict[str, list[str]] = {
    "org.vinegarhq.vinegar": [
        "roblox studio", "roblox development", "roblox place", "luau", "rbxl",
        "build a roblox game", "roblox editor",
    ],
    "org.vinegarhq.sober": ["roblox", "roblox player", "play roblox", "roblox client"],
    "org.prismlauncher.prismlauncher": ["minecraft", "minecraft java", "modded minecraft"],
    "io.mrarm.mcpelauncher": ["minecraft bedrock", "minecraft pe", "mcpe"],
    "com.heroicgameslauncher.hgl": ["epic games", "gog", "game launcher", "amazon games"],
    "steam": ["games", "game library", "pc gaming"],
    "org.gimp.gimp": ["photo editing", "image editor", "retouch", "raster graphics"],
    "org.kde.krita": ["digital painting", "drawing", "illustration", "concept art"],
    "org.inkscape.inkscape": ["vector graphics", "svg", "logo design", "vector editor"],
    "io.gitlab.theevilskeleton.upscaler": ["upscale image", "super resolution"],
    "io.gitlab.adhami3310.impression": ["flash usb", "write iso", "bootable drive"],
    "com.obsproject.studio": ["screen recording", "streaming", "capture video"],
    "io.github.spacingbat3.webcord": ["discord", "chat", "voice chat"],
    "com.github.unrud.videodownloader": ["download video", "youtube download"],
    "com.github.rafostar.clapper": ["video player", "play video", "watch video"],
    "io.missioncenter.missioncenter": ["task manager", "system monitor", "resource usage"],
    "io.github.flattool.warehouse": ["manage flatpaks", "uninstall flatpak"],
    "com.github.tchx84.flatseal": ["flatpak permissions", "sandbox permissions"],
    "org.gnome.boxes": ["virtual machine", "vm", "virtualisation"],
    "org.mozilla.thunderbird": ["email client", "mail"],
    "org.mozilla.thunderbird_esr": ["email client", "mail"],
    "code": ["code editor", "ide", "vscode", "visual studio code", "write code"],
    "codium": ["code editor", "ide", "vscode", "write code"],
    "firefox": ["web browser", "browse the web", "internet"],
    "chromium": ["web browser", "browse the web"],
    "google-chrome": ["web browser", "browse the web"],
    "blender": ["3d modelling", "3d animation", "render 3d"],
    "audacity": ["audio editing", "record audio", "edit sound"],
    "libreoffice": ["office suite", "documents", "spreadsheet", "word processor"],
    "vlc": ["video player", "media player", "play video"],
    "btop": ["system monitor", "cpu usage", "process list"],
    "org.godotengine.godot": ["game engine", "make a game", "2d game", "3d game", "gdscript"],
    "com.blackmagicdesign.resolve": ["video editing", "colour grading", "edit a video", "davinci"],
    "io.dbeaver.dbeaver": ["database client", "sql client", "query a database"],
    "cursor": ["code editor", "ide", "ai editor", "write code"],
    "net.davidotek.pupgui2": ["proton", "wine compatibility", "steam compatibility"],
    "org.kde.dolphin": ["file manager", "browse files"],
    "nautilus": ["file manager", "browse files"],
    "thunar": ["file manager", "browse files"],
    "kitty": ["terminal", "shell", "command line"],
    "alacritty": ["terminal", "shell", "command line"],
    "org.kde.ark": ["archive", "unzip", "extract files"],
    "discord": ["discord", "chat", "voice chat"],
    "slack": ["slack", "team chat"],
    "spotify": ["music", "play music", "listen to music"],
    "obsidian": ["notes", "note taking", "markdown notes"],
    "gnome-boxes": ["virtual machine", "vm"],
    "rofi": ["app launcher", "run dialog"],
}

# Desktop-entry categories → plain-language purpose words the agent may search for.
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "WebBrowser": ["web browser", "browse the web"],
    "TextEditor": ["text editor", "edit text"],
    "IDE": ["ide", "code editor", "programming"],
    "Development": ["development", "programming", "code"],
    "Graphics": ["graphics", "image", "design"],
    "Photography": ["photo", "photography"],
    "RasterGraphics": ["raster image", "bitmap"],
    "VectorGraphics": ["vector graphics", "svg"],
    "Viewer": ["viewer", "view files"],
    "3DGraphics": ["3d", "modelling"],
    "AudioVideo": ["media", "audio", "video"],
    "Audio": ["audio", "sound", "music"],
    "Video": ["video", "movie"],
    "Player": ["player", "playback"],
    "Recorder": ["record", "recording"],
    "Game": ["game", "gaming", "play"],
    "Office": ["office", "document"],
    "Spreadsheet": ["spreadsheet", "excel"],
    "WordProcessor": ["word processor", "document"],
    "Presentation": ["presentation", "slides"],
    "Email": ["email", "mail"],
    "InstantMessaging": ["chat", "messaging"],
    "Chat": ["chat", "messaging"],
    "TerminalEmulator": ["terminal", "shell", "console"],
    "FileManager": ["file manager", "browse files"],
    "System": ["system"],
    "Monitor": ["monitor", "system monitor"],
    "Settings": ["settings", "preferences"],
    "Network": ["network", "internet"],
    "Utility": ["utility", "tool"],
    "Emulator": ["emulator"],
    "Archiving": ["archive", "zip", "compression"],
    "Security": ["security", "password"],
}

# How much a category-derived term counts relative to one the app states itself.
_WEAK_TERM_FACTOR = 0.55

# Words carrying no discriminating power in an app-resolution query.
_STOPWORDS = {
    "a", "an", "the", "for", "with", "on", "in", "to", "my", "me", "please", "new",
    "open", "launch", "start", "run", "use", "using", "create", "make", "build",
    "project", "app", "application", "program", "some", "and", "of", "up", "it",
    "that", "this", "can", "you", "i", "want", "need", "would", "like", "let",
    "s", "then",
}


@dataclass
class AppEntry:
    """One launchable application or command-line tool."""

    id: str
    name: str
    kind: str  # flatpak | snap | desktop | cli | macos | windows
    launcher: list[str] = field(default_factory=list)
    generic_name: str = ""
    comment: str = ""
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    mime_types: list[str] = field(default_factory=list)
    desktop_file: str = ""
    exec_line: str = ""
    terminal: bool = False

    @property
    def gui(self) -> bool:
        return self.kind != "cli"

    def launch_argv(self, target: str = "") -> list[str]:
        """Full argv to start this app, optionally opening ``target`` (file or URL)."""
        argv = list(self.launcher)
        if target:
            argv.append(target)
        return argv

    def purpose(self) -> str:
        """Short human description used in the prompt and in tool output."""
        for candidate in (self.generic_name, self.comment):
            text = (candidate or "").strip()
            if text and text.lower() != self.name.lower():
                return text[:90]
        if self.categories:
            return "/".join(self.categories[:2])
        return ""

    def strong_terms(self) -> list[str]:
        """Terms the application says about itself, plus curated purpose aliases."""
        terms = [self.name, self.id, self.generic_name, self.comment]
        terms.extend(self.keywords)
        terms.extend(self.aliases)
        if self.launcher:
            terms.append(Path(self.launcher[-1]).name)
        return [t.lower() for t in terms if t]

    def weak_terms(self) -> list[str]:
        """Terms inferred from desktop-entry categories.

        A category says what an app *handles*, not what it does to it — an image
        viewer and an image editor share 'Graphics' — so these count for less.
        """
        terms: list[str] = []
        for cat in self.categories:
            terms.extend(_CATEGORY_KEYWORDS.get(cat, []))
        return [t.lower() for t in terms if t]

    def search_terms(self) -> list[str]:
        return self.strong_terms() + self.weak_terms()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "purpose": self.purpose(),
            "launch": " ".join(shlex.quote(a) for a in self.launcher),
            "categories": self.categories,
            "keywords": (self.keywords + self.aliases)[:12],
            "desktop_file": self.desktop_file,
        }


def _tokens(text: str) -> list[str]:
    words = re.split(r"[^a-z0-9+#]+", (text or "").lower())
    return [w for w in words if w and w not in _STOPWORDS]


def _phrase_in(phrase: str, text: str) -> bool:
    """Whole-word containment, so 'ide' does not match 'video'."""
    phrase = (phrase or "").strip().lower()
    if not phrase:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", (text or "").lower()) is not None


def _which(binary: str) -> str:
    try:
        return shutil.which(binary) or ""
    except OSError:
        return ""


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (out.stdout or "").strip()


# --------------------------------------------------------------------------- XDG


def _xdg_application_dirs() -> list[Path]:
    dirs: list[Path] = []
    home_data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    raw = [home_data, *(os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]
    # Flatpak and Snap export their entries outside XDG_DATA_DIRS on some setups.
    raw.extend(
        [
            str(Path.home() / ".local/share/flatpak/exports/share"),
            "/var/lib/flatpak/exports/share",
            "/var/lib/snapd/desktop",
        ]
    )
    seen: set[str] = set()
    for base in raw:
        base = (base or "").strip()
        if not base:
            continue
        path = Path(base) / "applications"
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir():
            dirs.append(path)
    return dirs


def _parse_desktop_entry(path: Path) -> dict[str, str] | None:
    """Read the ``[Desktop Entry]`` group of a .desktop file (unlocalised keys only)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    data: dict[str, str] = {}
    in_group = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if in_group:
                break  # first group only
            in_group = line.lower() == "[desktop entry]"
            continue
        if not in_group or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if "[" in key:  # localised variant, e.g. Name[fr]
            continue
        data[key] = value.strip()
    return data or None


def _exec_argv(exec_line: str) -> list[str]:
    cleaned = _FIELD_CODES.sub("", exec_line or "")
    # Flatpak wraps optional args as `@@u %U @@` / `@@ %f @@`.
    cleaned = re.sub(r"@@u?\s*|\s*@@", " ", cleaned)
    try:
        argv = shlex.split(cleaned)
    except ValueError:
        argv = cleaned.split()
    return [a for a in argv if a]


def _flatpak_id_from_entry(path: Path, data: dict[str, str]) -> str:
    ref = (data.get("X-Flatpak") or "").strip()
    if ref:
        return ref.split("/")[0]
    if "flatpak/exports" in str(path) or "/var/lib/flatpak" in str(path):
        return path.stem
    return ""


def _snap_name_from_entry(path: Path, data: dict[str, str]) -> str:
    if "/var/lib/snapd/desktop" not in str(path):
        return ""
    exec_line = data.get("Exec") or ""
    match = re.search(r"/snap/bin/([a-z0-9._-]+)", exec_line)
    if match:
        return match.group(1)
    return path.stem.split("_")[0]


def _scan_desktop_entries() -> list[AppEntry]:
    entries: dict[str, AppEntry] = {}
    gio = _which("gio")
    for directory in _xdg_application_dirs():
        try:
            files = sorted(directory.rglob("*.desktop"))
        except OSError:
            continue
        for path in files:
            try:
                entry_id = str(path.relative_to(directory)).replace("/", "-")
            except ValueError:
                entry_id = path.name
            if entry_id in entries:
                continue  # earlier directory wins (XDG precedence)
            data = _parse_desktop_entry(path)
            if not data:
                continue
            if (data.get("Type") or "Application") != "Application":
                continue
            if (data.get("NoDisplay") or "").lower() == "true":
                continue
            if (data.get("Hidden") or "").lower() == "true":
                continue
            name = (data.get("Name") or path.stem).strip()
            if not name:
                continue
            try_exec = (data.get("TryExec") or "").strip()
            if try_exec and not Path(try_exec).is_absolute() and not _which(try_exec):
                continue

            flatpak_id = _flatpak_id_from_entry(path, data)
            snap_name = _snap_name_from_entry(path, data)
            exec_line = data.get("Exec") or ""

            if flatpak_id:
                kind, ident = "flatpak", flatpak_id
                launcher = ["flatpak", "run", flatpak_id]
            elif snap_name:
                kind, ident = "snap", snap_name
                launcher = ["snap", "run", snap_name]
            else:
                kind = "desktop"
                ident = path.stem
                argv = _exec_argv(exec_line)
                if not argv:
                    continue
                # gio launch honours DBus activation, Terminal=true and field codes.
                launcher = [gio, "launch", str(path)] if gio else argv

            keywords = [k for k in re.split(r"[;,]", data.get("Keywords") or "") if k.strip()]
            categories = [c for c in (data.get("Categories") or "").split(";") if c.strip()]
            mimes = [m for m in (data.get("MimeType") or "").split(";") if m.strip()]
            entries[entry_id] = AppEntry(
                id=ident,
                name=name,
                kind=kind,
                launcher=launcher,
                generic_name=(data.get("GenericName") or "").strip(),
                comment=(data.get("Comment") or "").strip(),
                categories=categories,
                keywords=[k.strip() for k in keywords],
                mime_types=mimes[:8],
                desktop_file=str(path),
                exec_line=exec_line,
                terminal=(data.get("Terminal") or "").lower() == "true",
            )
    return list(entries.values())


def _scan_flatpak_refs(known: set[str]) -> list[AppEntry]:
    """Flatpak apps with no exported desktop entry are still runnable."""
    if not _which("flatpak"):
        return []
    out = _run(["flatpak", "list", "--app", "--columns=application,name,description"], timeout=8.0)
    apps: list[AppEntry] = []
    for line in out.splitlines():
        parts = line.split("\t")
        app_id = parts[0].strip() if parts else ""
        if not app_id or app_id.lower() in known:
            continue
        name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else app_id.split(".")[-1]
        comment = parts[2].strip() if len(parts) > 2 else ""
        apps.append(
            AppEntry(
                id=app_id,
                name=name,
                kind="flatpak",
                launcher=["flatpak", "run", app_id],
                comment=comment,
            )
        )
    return apps


def _scan_snap_refs(known: set[str]) -> list[AppEntry]:
    if not _which("snap"):
        return []
    out = _run(["snap", "list"], timeout=8.0)
    apps: list[AppEntry] = []
    for line in out.splitlines()[1:]:
        name = line.split()[0].strip() if line.split() else ""
        if not name or name.lower() in known:
            continue
        apps.append(
            AppEntry(id=name, name=name, kind="snap", launcher=["snap", "run", name])
        )
    return apps


def _scan_macos_bundles() -> list[AppEntry]:
    apps: list[AppEntry] = []
    for base in (Path("/Applications"), Path.home() / "Applications", Path("/System/Applications")):
        if not base.is_dir():
            continue
        try:
            bundles = sorted(base.glob("*.app"))
        except OSError:
            continue
        for bundle in bundles:
            name = bundle.stem
            apps.append(
                AppEntry(
                    id=name,
                    name=name,
                    kind="macos",
                    launcher=["open", "-a", str(bundle)],
                )
            )
    return apps


def _scan_windows_shortcuts() -> list[AppEntry]:
    apps: list[AppEntry] = []
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    for base in roots:
        if not str(base) or not base.is_dir():
            continue
        try:
            links = sorted(base.rglob("*.lnk"))
        except OSError:
            continue
        for link in links:
            apps.append(
                AppEntry(
                    id=link.stem,
                    name=link.stem,
                    kind="windows",
                    launcher=["cmd", "/c", "start", "", str(link)],
                )
            )
    return apps


def _scan_cli_tools(known: set[str]) -> list[AppEntry]:
    apps: list[AppEntry] = []
    for binary in _CLI_CANDIDATES:
        path = _which(binary)
        if not path or binary.lower() in known:
            continue
        apps.append(AppEntry(id=binary, name=binary, kind="cli", launcher=[path]))
    return apps


def _apply_aliases(apps: Iterable[AppEntry]) -> None:
    for app in apps:
        keys = {app.id.lower(), app.name.lower()}
        if app.launcher:
            keys.add(Path(app.launcher[-1]).name.lower())
        extra: list[str] = []
        for key in keys:
            extra.extend(_PURPOSE_ALIASES.get(key, []))
            # Loose match so 'org.mozilla.firefox' picks up the 'firefox' aliases.
            for alias_key, words in _PURPOSE_ALIASES.items():
                if "." not in alias_key and len(alias_key) > 3 and _phrase_in(alias_key, key):
                    extra.extend(words)
        app.aliases = sorted({e for e in extra})


def scan_applications() -> list[AppEntry]:
    """Inventory every launchable application and notable CLI tool on this host."""
    system = platform.system()
    apps: list[AppEntry] = []
    if system == "Linux":
        apps.extend(_scan_desktop_entries())
    elif system == "Darwin":
        apps.extend(_scan_macos_bundles())
    elif system == "Windows":
        apps.extend(_scan_windows_shortcuts())

    known = {a.id.lower() for a in apps} | {a.name.lower() for a in apps}
    if system == "Linux":
        apps.extend(_scan_flatpak_refs(known))
        known |= {a.id.lower() for a in apps}
        apps.extend(_scan_snap_refs(known))
        known |= {a.id.lower() for a in apps}
    apps.extend(_scan_cli_tools(known))

    _apply_aliases(apps)
    apps.sort(key=lambda a: (a.kind == "cli", a.name.lower()))
    return apps


# ----------------------------------------------------------------- resolution


def _document_frequency(apps: list[AppEntry]) -> dict[str, int]:
    """How many applications mention each term token.

    Used to weight query tokens: 'manager' or 'tool' appears everywhere and should
    barely count, while 'roblox' or 'inkscape' is decisive.
    """
    df: dict[str, int] = {}
    for app in apps:
        seen: set[str] = set()
        for term in app.search_terms():
            seen.update(_tokens(term))
        for token in seen:
            df[token] = df.get(token, 0) + 1
    return df


def _token_weight(token: str, df: dict[str, int] | None, total: int) -> float:
    if not df or total <= 1:
        return 1.0
    count = df.get(token, 0)
    if count <= 0:
        return 1.0
    # Inverse document frequency, normalised into (0, 1].
    return math.log(1.0 + total / count) / math.log(1.0 + total)


def score_app(
    app: AppEntry,
    query: str,
    *,
    df: dict[str, int] | None = None,
    total: int = 0,
) -> float:
    """How well ``app`` serves ``query``. 0 means no match."""
    needle = (query or "").strip().lower()
    if not needle:
        return 0.0

    if needle == app.id.lower() or needle == app.name.lower():
        return 100.0
    if app.launcher and needle == Path(app.launcher[-1]).name.lower():
        return 95.0

    terms = app.search_terms()
    score = 0.0

    for alias in app.aliases:
        if needle == alias:
            score = max(score, 90.0)
        elif _phrase_in(alias, needle):
            # A longer alias phrase found in the request is a stronger signal:
            # "roblox studio" must beat "roblox" when both appear.
            score = max(score, 62.0 + 6.0 * min(len(_tokens(alias)), 4))
        elif _phrase_in(needle, alias):
            score = max(score, 66.0)

    if _phrase_in(needle, app.name) or _phrase_in(app.name, needle):
        score = max(score, 60.0)
    if app.id.lower().endswith("." + needle.replace(" ", "")):
        score = max(score, 65.0)

    query_tokens = _tokens(needle)
    if query_tokens:
        strong_tokens: set[str] = set()
        for term in app.strong_terms():
            strong_tokens.update(_tokens(term))
        weak_tokens: set[str] = set()
        for term in app.weak_terms():
            weak_tokens.update(_tokens(term))
        weights = {t: _token_weight(t, df, total) for t in query_tokens}
        wanted = sum(weights.values()) or 1.0
        matched = 0.0
        for token, weight in weights.items():
            if token in strong_tokens:
                matched += weight
            elif token in weak_tokens:
                matched += weight * _WEAK_TERM_FACTOR
        if matched > 0:
            score = max(score, 20.0 + 30.0 * (matched / wanted))
        # Multi-word purpose phrases ("roblox studio") matching verbatim rank higher.
        for term in terms:
            if len(term) > 6 and _phrase_in(term, needle):
                score = max(score, 55.0)

    if score and app.kind == "cli":
        score -= 6.0  # prefer a real application over a same-named CLI
    if score and app.kind == "flatpak":
        score += 1.0  # exported flatpaks are usually the user-facing app
    return max(score, 0.0)


def resolve_apps(
    query: str,
    apps: list[AppEntry],
    *,
    limit: int = 5,
    floor: float = 22.0,
) -> list[tuple[AppEntry, float]]:
    df = _document_frequency(apps)
    total = len(apps)
    scored = [(app, score_app(app, query, df=df, total=total)) for app in apps]
    hits = [(a, s) for a, s in scored if s >= floor]
    hits.sort(key=lambda pair: (-pair[1], pair[0].name.lower()))
    return hits[:limit]


def _context_rank(app: AppEntry) -> tuple:
    """Order the prompt listing by usefulness: curated purposes and user-installed
    applications first, system plumbing last."""
    user_installed = app.kind in {"flatpak", "snap"} or str(Path.home()) in app.desktop_file
    plumbing = any(
        c in app.categories for c in ("Settings", "Screensaver")
    ) or app.name.lower().startswith(("avahi", "qt ", "qv4l2", "cmake"))
    return (
        0 if app.aliases else 1,
        0 if user_installed else 1,
        1 if plumbing else 0,
        app.name.lower(),
    )


def format_apps_index(apps: list[AppEntry], *, examples: int = 10) -> str:
    """One line: how many applications exist and which call finds the right one.

    Listing them is actively harmful. With ~120 apps and their purposes in the prompt —
    browsers among them — qwen3.5 answered "what's the weather in Casablanca?" by
    launching Firefox, and "who won the last F1 race?" with "I'll check using your
    installed applications". Removing the listing took web questions from 0/6 to 4/6 on
    the same prompt. ``action=apps query=<purpose>`` resolves the catalogue on demand,
    including the names that give no clue ("Vinegar" is Roblox Studio), and
    ``action=open app=<purpose>`` resolves against it directly.
    """
    if not apps:
        return ""
    gui = sum(1 for a in apps if a.gui)
    return (
        f"=== Installed applications: {len(apps)} entries ({gui} GUI), not listed here ===\n"
        "For a task an application would do, call device_control action=apps "
        "query=<purpose> to see what is installed (it knows what each one is FOR, "
        "including names that give no clue), then action=open app=<name, id or purpose> "
        "to launch it and carry on with the task inside it. Do not open a browser or an "
        "application to look something up — the browse tool reads the web directly."
    )


def _name_implies_purpose(app: AppEntry) -> bool:
    """True when the app's own name already contains one of its purpose words."""
    name = (app.name or "").lower()
    return any(alias.split()[0] in name for alias in app.aliases if alias.strip())


def format_apps_context(apps: list[AppEntry], *, gui_limit: int = 55, cli_limit: int = 40) -> str:
    """The exhaustive listing. Opt-in via config.agent.full_device_context."""
    if not apps:
        return ""
    gui = sorted((a for a in apps if a.gui), key=_context_rank)
    cli = [a for a in apps if not a.gui]
    lines = [
        "=== Installed applications (launch with device_control action=open app=<name or id>) ===",
    ]
    for app in gui[:gui_limit]:
        bits = [f"{app.name} [{app.kind}:{app.id}]"]
        purpose = app.purpose()
        if purpose:
            bits.append(purpose)
        if app.aliases:
            bits.append("use for: " + ", ".join(app.aliases[:4]))
        lines.append("  - " + " — ".join(bits))
    if len(gui) > gui_limit:
        lines.append(f"  - … +{len(gui) - gui_limit} more GUI applications (device_control action=apps to search)")
    if cli:
        names = ", ".join(a.name for a in cli[:cli_limit])
        more = f" (+{len(cli) - cli_limit} more)" if len(cli) > cli_limit else ""
        lines.append(f"Command-line tools on PATH: {names}{more}")
    lines.append(
        "Pick the installed application that fits the request and open it yourself. "
        "If nothing installed fits, use device_control action=apps to search install "
        "candidates, install it, then continue."
    )
    return "\n".join(lines)


def install_suggestions(query: str, *, limit: int = 6) -> list[str]:
    """Package candidates for a purpose nothing installed serves."""
    needle = (query or "").strip()
    if not needle:
        return []
    out: list[str] = []
    if _which("flatpak"):
        raw = _run(["flatpak", "search", "--columns=application,name,description", needle], timeout=15.0)
        for line in raw.splitlines()[:limit]:
            parts = [p.strip() for p in line.split("\t")]
            if parts and parts[0] and parts[0] != "No matches found":
                label = parts[1] if len(parts) > 1 and parts[1] else parts[0]
                out.append(f"flatpak install -y flathub {parts[0]}   # {label}")
    if len(out) < limit:
        if _which("pacman"):
            raw = _run(["pacman", "-Ss", needle], timeout=15.0)
            for line in raw.splitlines():
                if line.startswith(" ") or "/" not in line:
                    continue
                pkg = line.split()[0].split("/")[-1]
                out.append(f"pacman -S --noconfirm {pkg}")
                if len(out) >= limit:
                    break
        elif _which("apt"):
            raw = _run(["apt-cache", "search", needle], timeout=15.0)
            for line in raw.splitlines()[: limit - len(out)]:
                pkg = line.split(" - ")[0].strip()
                if pkg:
                    out.append(f"apt install -y {pkg}")
        elif _which("brew"):
            raw = _run(["brew", "search", needle], timeout=20.0)
            for token in raw.split()[: limit - len(out)]:
                out.append(f"brew install {token}")
    return out[:limit]


class AppCatalog:
    """Cached application inventory, refreshed when the desktop-entry dirs change."""

    TTL_SECONDS = 300.0

    def __init__(self) -> None:
        self._apps: list[AppEntry] = []
        self._scanned_at = 0.0
        self._signature = ""

    @staticmethod
    def _dir_signature() -> str:
        stamps: list[str] = []
        for directory in _xdg_application_dirs():
            try:
                stamps.append(f"{directory}:{directory.stat().st_mtime_ns}")
            except OSError:
                continue
        return "|".join(stamps)

    def apps(self, *, refresh: bool = False) -> list[AppEntry]:
        now = time.time()
        stale = refresh or not self._apps or (now - self._scanned_at) > self.TTL_SECONDS
        if stale:
            signature = self._dir_signature()
            if refresh or not self._apps or signature != self._signature:
                self._apps = scan_applications()
                self._signature = signature
            self._scanned_at = now
        return list(self._apps)

    def resolve(self, query: str, *, limit: int = 5, floor: float = 22.0) -> list[tuple[AppEntry, float]]:
        return resolve_apps(query, self.apps(), limit=limit, floor=floor)

    def best(self, query: str) -> AppEntry | None:
        hits = self.resolve(query, limit=1)
        return hits[0][0] if hits and hits[0][1] >= 20.0 else None

    def context(self, *, full: bool = False) -> str:
        apps = self.apps()
        return format_apps_context(apps) if full else format_apps_index(apps)

    def summary(self) -> str:
        apps = self.apps()
        gui = sum(1 for a in apps if a.gui)
        by_kind: dict[str, int] = {}
        for a in apps:
            by_kind[a.kind] = by_kind.get(a.kind, 0) + 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
        return f"{len(apps)} launchable entries ({gui} GUI apps; {detail})"


_catalog = AppCatalog()


def get_catalog() -> AppCatalog:
    return _catalog
