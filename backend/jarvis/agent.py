"""The JARVIS agent loop: streaming tool-calling over an emit/confirm interface."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Awaitable, Callable

from .config import Config
from .device import get_learner
from .peripherals import get_learner as get_peripheral_learner
from .llm import Interrupted, LLMClient, LLMError
from .memory import Memory
from .persona import system_prompt
from .tools import ToolContext, openai_schemas, registry

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]
ConfirmFn = Callable[[dict[str, Any]], Awaitable[bool]]

MAX_ITERATIONS = 16
ACTION_MAX_ITERATIONS = 28
SUBAGENT_MAX_ITERATIONS = 32
FAIL_RETRY_LIMIT = 2

SUBAGENT_ADDENDUM = """

Background subagent mode:
- You are running as a BACKGROUND SUBAGENT spawned by JARVIS to complete one task autonomously.
- There is no user available to answer questions or approve actions. Tools that normally
  require confirmation will be denied automatically (unless auto-approve is enabled); if a
  denial blocks part of the task, note it in your report and continue with what you can do.
- Never call start_task; subagents may not spawn further subagents.
- When finished, end with a single final message: a clear, self-contained report of what you
  did, what you found, and anything you could not complete.
"""


_DELEGATION_HINTS = (
    "subagent",
    "sub-agent",
    "sub agent",
    "background",
    "in parallel",
    "parallel",
    "spawn",
    "delegate",
    "redeploy",
    "deploy",
    "commence",
    "run these",
    "run this in",
    "while you",
    "as many",
)

_MCP_DENIAL_PHRASES = (
    "don't have access",
    "do not have access",
    "don't have direct access",
    "do not have direct access",
    "cannot fetch external",
    "can't fetch external",
    "not available in current environment",
    "not available in my environment",
    "no access to any",
    "cannot use any mcp",
    "can't use any mcp",
    "i don't have access to any roblox",
    "i do not have access to any roblox",
    "cannot use mcp",
    "can't use mcp",
    "mcp protocol as claimed",
    "no roblox mcp",
    "roblox mcp or api",
)


def _wants_delegation(text: str) -> bool:
    """Heuristic: did the user explicitly ask for background/subagent work?"""
    low = (text or "").lower()
    if any(hint in low for hint in _DELEGATION_HINTS):
        return True
    # "create/deploy/redeploy … agents" without the word subagent.
    if "agent" in low and any(
        w in low for w in ("create", "deploy", "redeploy", "spawn", "run", "start", "commence", "many")
    ):
        return True
    return False


def _wants_mcp(text: str) -> bool:
    """Heuristic: did the user ask to use an installed MCP server/tool?"""
    low = (text or "").lower()
    if "mcp" in low:
        return True
    try:
        from .config import store

        for entry in store.get().mcp.servers:
            name = (entry.name or "").strip().lower()
            if name and name in low:
                return True
            sid = (entry.id or "").strip().lower()
            if sid and sid in low:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _denies_mcp_access(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in _MCP_DENIAL_PHRASES)


_MISSING_CAPABILITY_PHRASES = (
    "i don't have a tool",
    "i do not have a tool",
    "i don't have the ability",
    "i do not have the ability",
    "i'm not able to",
    "i am not able to",
    "i'm unable to",
    "i am unable to",
    "no integration",
    "not integrated",
    "you'll need to install",
    "you will need to install",
    "you would need to install",
    "you need to install",
    "you can install",
    "requires an mcp server",
    "no mcp server",
    "isn't supported",
    "is not supported",
    "outside my capabilities",
    "beyond my capabilities",
)


def _lacks_capability(text: str) -> bool:
    """Did the reply give up for want of an integration JARVIS could provision itself?"""
    low = (text or "").lower()
    return any(p in low for p in _MISSING_CAPABILITY_PHRASES) or _denies_mcp_access(text)


_ACTION_VERBS = (
    "dim",
    "brighten",
    "install",
    "uninstall",
    "connect",
    "disconnect",
    "pair",
    "open",
    "launch",
    "send",
    "set ",
    "make ",
    "enable",
    "disable",
    "kill",
    "start ",
    "stop ",
    "lower",
    "raise",
    "turn on",
    "turn off",
    "mute",
    "unmute",
    "volume",
    "mount",
    "unmount",
    "scan",
    "discover",
    "remember",
    "delete",
    "remove",
    "download",
    "update",
    "upgrade",
    "lock",
    "screenshot",
    "click",
    "type ",
    "scroll",
    "fill ",
    "write",
    "create",
    "fix",
    "configure",
    "set up",
    "setup",
    "change",
    "switch",
    "reboot",
    "restart",
    "pair",
    "flash",
    "rainbow",
    "backlight",
    "brightness",
    "lighting",
    "lights",
    " colour",
    " color",
    "rgb",
)

_ACTION_AFFIRM = {
    "yes",
    "yeah",
    "yep",
    "ok",
    "okay",
    "sure",
    "do it",
    "do that",
    "do them",
    "go",
    "go ahead",
    "go for it",
    "please",
    "please do",
    "just do it",
    "proceed",
    "at once",
    "install it",
    "install all this",
    "install all this stuff",
    "run it",
    "run them",
    "execute",
    "execute it",
    "alright",
    "all right",
    "sure thing",
    "yes please",
    "ok go",
    "okay go",
}

_INSTRUCTION_MARKERS = (
    "would you like me to",
    "shall i install",
    "shall i run",
    "do you want me to",
    "i can walk you through",
    "i can help you",
    "here are the steps",
    "here is how",
    "here's how",
    "run the following",
    "run this command",
    "run these commands",
    "you'll need to",
    "you will need to",
    "you can run",
    "you should run",
    "execute the following",
    "try running",
    "paste this",
    "copy and paste",
    "manually install",
    "follow these steps",
)

_SHELL_IN_FENCE = re.compile(
    r"```(?:\w+)?[^\n]*\n[\s\S]{0,2500}?\b(?:sudo|pacman|apt(?:-get)?|dnf|yum|systemctl|"
    r"lsmod|modprobe|insmod|openrazer|razer|hidraw|/dev/input|bluetoothctl|pactl|wpctl|"
    r"brightnessctl|polychromatic|razer-cli|openrgb|npm|npx|yarn|pnpm|vite|mkdir)\b",
    re.IGNORECASE,
)

def _command_heads() -> frozenset[str]:
    """Every executable name a reply might be pasting instead of running."""
    from . import controls as _ctl

    extras = {
        "modprobe", "insmod", "lsmod", "echo", "tee", "cat", "chmod", "chown",
        "openrazer", "razer", "npm", "npx", "yarn", "pnpm", "mkdir", "vite",
        "xdg-open", "xset", "setxkbmap", "wpctl", "pw-cli", "swaymsg", "hyprctl",
        "systemctl", "journalctl", "nmcli", "bluetoothctl", "brightnessctl",
    }
    return frozenset(extras | _ctl.control_binaries())


_COMMAND_HEADS = _command_heads()

_SHELL_LINE = re.compile(
    r"^(?:\$\s*)?(?:sudo\s+)?(?:"
    + "|".join(sorted((re.escape(h) for h in _COMMAND_HEADS), key=len, reverse=True))
    + r")\b",
    re.IGNORECASE | re.MULTILINE,
)

_CMD_PREFIX = re.compile(r"^\s*(?:[$#>]\s+|`)?")
_FENCE_LINE = re.compile(r"^\s*```")


def _is_command_line(line: str) -> bool:
    stripped = _CMD_PREFIX.sub("", line).strip().strip("`").strip()
    if not stripped:
        return False
    head = stripped.split()[0]
    if head.lower() == "sudo" and len(stripped.split()) > 1:
        head = stripped.split()[1]
    return head.split("/")[-1].lower() in _COMMAND_HEADS


_ASKED_FOR_TEXT = re.compile(
    r"\b(?:write|draft|compose|generate|show me|give me|print|explain|teach|"
    r"what(?:'s| is) the command|which command|how do i|how to|how can i|"
    r"script|snippet|example|sample|template|boilerplate|pseudocode)\b",
    re.IGNORECASE,
)


def _asked_for_text(user_text: str) -> bool:
    """Did the user ask FOR a command or script? Then never auto-run what comes back."""
    return bool(_ASKED_FOR_TEXT.search(user_text or ""))


def pasted_commands(text: str, limit: int = 3) -> list[str]:
    """Commands handed to the user to run, whether bare or inside a fence."""
    fenced: list[str] = []
    for block in _CODE_FENCE_BODY.findall(text or ""):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if lines and all(_is_command_line(ln) for ln in lines):
            for line in lines:
                cleaned = _CMD_PREFIX.sub("", line).strip().strip("`").strip()
                if cleaned and cleaned not in fenced:
                    fenced.append(cleaned)
    if fenced:
        return fenced[:limit]
    return bare_commands(text, limit)


def bare_commands(text: str, limit: int = 3) -> list[str]:
    """Commands a reply pasted *instead of* running them.

    Only fires when the message is essentially nothing but commands — a sentence
    that merely mentions one is left alone.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 1200:
        return []
    commands: list[str] = []
    prose = 0
    for line in raw.splitlines():
        if not line.strip() or _FENCE_LINE.match(line):
            continue
        if _is_command_line(line):
            cleaned = _CMD_PREFIX.sub("", line).strip().strip("`").strip().rstrip(".;")
            if cleaned and cleaned not in commands:
                commands.append(cleaned)
        else:
            prose += 1
    if not commands or prose > 1:
        return []
    return commands[:limit]

_SCAFFOLD_RE = re.compile(
    r"\b(?:npm\s+(?:init|install|create|run)|npx\s+|yarn\s+create|pnpm\s+create|"
    r"vite\b|create-react-app|mkdir\s+-p|cargo\s+new|django-admin|"
    r"pip\s+install|python\s+-m\s+venv|composer\s+create-project)\b",
    re.IGNORECASE,
)

_CODE_FENCE = re.compile(r"```[\w+-]*\n[\s\S]{20,}?```")
_CODE_FENCE_BODY = re.compile(r"```[\w+-]*\n([\s\S]*?)```")

_HARDWARE_WORDS = (
    "keyboard",
    "keyboards",
    "mouse",
    "mice",
    "headset",
    "headphone",
    "headphones",
    "backlight",
    "backlights",
    "rgb",
    "rainbow",
    "lighting",
    "brightness",
    "volume",
    "speaker",
    "display",
    "monitor",
    "wifi",
    "bluetooth",
    "webcam",
    "camera",
    "printer",
)

_MUTATION_VERBS = (
    "dim",
    "brighten",
    "install",
    "uninstall",
    "connect",
    "disconnect",
    "pair",
    "open",
    "launch",
    "send",
    "set ",
    "make ",
    "enable",
    "disable",
    "kill",
    "start ",
    "stop ",
    "lower",
    "raise",
    "turn on",
    "turn off",
    "mute",
    "unmute",
    "volume",
    "mount",
    "unmount",
    "delete",
    "remove",
    "download",
    "update",
    "upgrade",
    "lock",
    "screenshot",
    "click",
    "type ",
    "scroll",
    "drag",
    "fill ",
    "submit",
    "gui",
    "desktop",
    "on screen",
    "on-screen",
    "write",
    "create",
    "fix",
    "configure",
    "set up",
    "setup",
    "change",
    "switch",
    "reboot",
    "restart",
    "flash",
    "rainbow",
    "backlight",
    "brightness",
    "lighting",
)

_EFFECTING_PERIPHERAL = {
    "connect",
    "disconnect",
    "pair",
    "unpair",
    "trust",
    "control",
}

_INERT_DEVICE_ACTIONS = {"read", "list"}


def _utterance_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (text or "").strip().lower())).strip()


def _is_affirmation(text: str) -> bool:
    """Short confirmations that mean 'do the last thing you proposed'."""
    key = _utterance_key(text)
    if not key:
        return False
    if key in _ACTION_AFFIRM:
        return True
    words = key.split()
    if len(words) <= 6 and words[0] in {"yes", "ok", "okay", "sure", "alright"}:
        return True
    return False


def _outstanding_request(memory: Any, conversation_id: int) -> str | None:
    """Most recent real user ask in this conversation (skip the current affirmation)."""
    skipped_current = False
    for m in reversed(memory.get_messages(conversation_id)[-24:]):
        if m.get("role") != "user":
            continue
        text = (m.get("content") or "").strip()
        if not text or text.startswith("[system]"):
            continue
        if not skipped_current:
            skipped_current = True
            continue
        if _is_affirmation(text):
            continue
        return text[:600]
    return None


def _is_clarification(text: str) -> bool:
    """Short corrections / bare names that refine the previous ask."""
    raw = (text or "").strip()
    if not raw or len(raw) > 120:
        return False
    low = raw.lower()
    if _OPEN_VERB_RE.search(low) and not re.match(r"^(i meant|meant|no|actually)\b", low):
        return False
    if re.match(r"^(i meant|meant|no|actually|sorry|wait)\b", low):
        return True
    words = re.findall(r"[a-z0-9]+", low)
    if 1 <= len(words) <= 4 and not _wants_action(raw) and not _is_affirmation(raw):
        return True
    return False


def _outstanding_open_request(memory: Any, conversation_id: int) -> str | None:
    """Prior open/URL ask, skipping the current turn and intervening clarifications."""
    skipped_current = False
    for m in reversed(memory.get_messages(conversation_id)[-24:]):
        if m.get("role") != "user":
            continue
        text = (m.get("content") or "").strip()
        if not text or text.startswith("[system]"):
            continue
        if not skipped_current:
            skipped_current = True
            continue
        if _is_affirmation(text) or _is_clarification(text):
            continue
        low = text.lower()
        if _parse_open_intent_direct(text) or (
            _OPEN_VERB_RE.search(low)
            and any(
                w in low
                for w in ("youtube", "browser", "website", "url", "channel", "http")
            )
        ):
            return text[:600]
        return None
    return None


def _follow_through_prompt(memory: Any, conversation_id: int, display_text: str) -> str | None:
    if not _is_affirmation(display_text):
        return None
    goal = _outstanding_request(memory, conversation_id)
    if goal:
        return (
            f"{display_text}\n\n"
            f"[system] That is confirmation to EXECUTE this outstanding request now:\n"
            f"«{goal}»\n"
            "Call device_control and/or peripherals and carry it out. "
            "Use the device class they named (keyboard vs mouse vs headphones vs display). "
            "Do not start a different task from the inventory. Do not paste a script — run it. "
            "For lights: peripherals action=control command=lighting value=rainbow|spectrum|off."
        )
    return (
        f"{display_text}\n\n"
        "[system] Confirmation to act. Execute the most recent unfinished request in this "
        "conversation with device_control/peripherals. Do not start an unrelated task."
    )


def _wants_action(text: str) -> bool:
    """Heuristic: did the user ask JARVIS to *do* something on this machine?"""
    low = (text or "").strip().lower()
    if not low:
        return False
    if _is_affirmation(text):
        return True
    return any(v in low for v in _ACTION_VERBS)


def _wants_mutation(text: str) -> bool:
    """True when the user asked to change something, not merely list/scan it."""
    if _is_affirmation(text):
        return True
    low = (text or "").strip().lower()
    return any(v in low for v in _MUTATION_VERBS)


def _action_directive(display_text: str, prior: str | None = None) -> str | None:
    """Steer the model toward tools on the first turn of a do-this request."""
    if not (
        _wants_action(display_text)
        or _is_affirmation(display_text)
        or parse_open_intent(display_text, prior)
    ):
        return None
    if _is_affirmation(display_text) and not parse_open_intent(display_text, prior):
        return None
    extra = ""
    low = display_text.lower()
    if any(w in low for w in ("keyboard", "backlight", "rgb", "rainbow", "lighting", "led")):
        extra = (
            " For lights, call peripherals with action=control, command=lighting "
            "(or brightness), target the keyboard, value=rainbow|spectrum|wave|off|50%. "
            "If that fails, device_control shell with openrgb/polychromatic-cli/"
            "brightnessctl, then retry. "
        )
    open_extra = ""
    gui_extra = ""
    if parse_open_intent(display_text, prior):
        open_extra = (
            " Launch with device_control action=open (url=... or app=...). "
            "Do NOT use computer_use / screenshots to open a URL or app — call open now. "
            "Never print the tool call as chat text — invoke the tool. "
        )
    elif any(
        w in low
        for w in (
            "click",
            "type ",
            "fill",
            "scroll",
            "dialog",
            "gui",
            "desktop",
            "on screen",
            "on-screen",
            "in the app",
        )
    ):
        gui_extra = (
            " For on-screen UI already open, call computer_use: screenshot first, "
            "look at the image, then click/type/key/scroll; screenshot again to verify. "
        )
    return (
        f"{display_text}\n\n"
        "[system] Execute this on THIS machine now. Call peripherals, "
        "device_control, and/or computer_use in this turn and actually run the change. "
        "Do not paste a script, npm/vite scaffold, or numbered how-to. 'Make X …' "
        "means change X, not create a new project."
        f"{extra}{open_extra}{gui_extra}"
        "The spoken reply is only what actually ran."
    )


_NARRATED_INTENT = re.compile(
    r"\b(?:i(?:'m| am) (?:going to|about to)|i(?:'ll| will)|let me|allow me to|"
    r"i shall|one moment while i)\s+"
    r"(?:just |now |quickly |first )?"
    r"(?:read|check|run|look|see|get|fetch|grab|take|query|inspect|probe|pull|"
    r"retrieve|open|launch|start|set|adjust|change|dim|brighten|turn|mute|connect|"
    r"pair|install|update|scan|search|find|measure|verify|confirm)\b",
    re.IGNORECASE,
)


def _narrates_intent(text: str) -> bool:
    """Did the reply promise an action ("I'll check the temperature") without doing it?

    Local models frequently answer with the sentence they should have spoken *while*
    calling a tool, and then stop. Nothing ran, so the promise is the whole answer.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 600:
        return False
    return bool(_NARRATED_INTENT.search(raw))


def _looks_like_instructions(text: str) -> bool:
    """True when the model wrote a how-to instead of calling tools."""
    raw = text or ""
    low = raw.lower()
    if any(m in low for m in _INSTRUCTION_MARKERS):
        return True
    if _SHELL_IN_FENCE.search(raw):
        return True
    if len(_SHELL_LINE.findall(raw)) >= 2:
        return True
    if bare_commands(raw):
        return True
    if _SCAFFOLD_RE.search(raw):
        return True
    fences = _CODE_FENCE.findall(raw)
    if fences:
        fenced_len = sum(len(f) for f in fences)
        if fenced_len >= max(80, int(len(raw) * 0.4)):
            return True
        blob = "\n".join(fences).lower()
        if any(
            tok in blob
            for tok in ("sudo", "npm", "npx", "mkdir", "pacman", "apt ", "systemctl", "&&")
        ):
            return True
    if re.search(r"(?:^|\n)\s*(?:step\s*)?\d+[\.\):]\s+\S.{8,}", raw, re.IGNORECASE) and any(
        w in low
        for w in (
            "install",
            "sudo",
            "driver",
            "package",
            "module",
            "run ",
            "command",
            "razer",
            "keyboard",
            "backlight",
        )
    ):
        return True
    return False


_SUGGEST_RE = re.compile(r"\[\[suggest:\s*(.+?)\s*\|\s*(.+?)\]\]", re.IGNORECASE)
_SUDO_MISS_MARKERS = (
    "no sudo password is configured",
    "needs sudo, but no sudo password",
)
_EMAIL_MISS_MARKERS = (
    "smtp is not configured",
    "no email profiles configured",
    "add smtp settings",
)
_CONFIG_SUGGESTIONS: dict[str, dict[str, str]] = {
    "sudo": {
        "label": "Save my sudo password",
        "prompt": (
            "Save my sudo password so you can install packages and run privileged "
            "commands. Ask me for it and put it in Systems → Authorisation."
        ),
    },
    "email": {
        "label": "Set up email",
        "prompt": (
            "Walk me through setting up email so you can send and read messages for me."
        ),
    },
}


def _extract_suggestions(text: str) -> tuple[str, list[dict[str, str]]]:
    """Strip [[suggest: label | prompt]] trailers from a reply."""
    items: list[dict[str, str]] = []
    raw = text or ""
    for match in _SUGGEST_RE.finditer(raw):
        label = (match.group(1) or "").strip()
        prompt = (match.group(2) or "").strip()
        if not label or not prompt:
            continue
        if _looks_like_instructions(f"{label}\n{prompt}"):
            continue
        items.append({"label": label[:80], "prompt": prompt[:400]})
    cleaned = _SUGGEST_RE.sub("", raw)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, items[:3]


def _dedupe_suggestions(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in items:
        key = (item.get("prompt") or item.get("label") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= 3:
            break
    return out


def _config_miss_kind(output: str) -> str | None:
    low = (output or "").lower()
    if any(m in low for m in _SUDO_MISS_MARKERS):
        return "sudo"
    if any(m in low for m in _EMAIL_MISS_MARKERS):
        return "email"
    return None


def _looks_like_tool_failure(output: str) -> bool:
    text = output or ""
    low = text.lower()
    if text.startswith(("Error", "Refused:", "The user declined")):
        return True
    if "denied by user" in low:
        return True
    if re.search(r"\(exited with code [1-9]", low):
        return True
    if _config_miss_kind(text):
        return True
    if low.startswith("failed to") or "could not " in low[:240]:
        return True
    return False


def _hard_gate_risk(name: str, args: dict, preview: str) -> str:
    action = str(args.get("action") or args.get("mode") or "").strip().lower()
    command = str(args.get("command") or "")
    blob = f"{name} {action} {command} {preview}".lower()
    if name == "communicate" or action == "send" or "email" in blob:
        return "This will send email."
    if action == "delete" or re.search(r"\b(rm|rmdir|unlink)\b", blob):
        return "This will delete files."
    if action == "write" or "overwrite" in blob or ">" in command:
        return "This will overwrite a file."
    if action == "move" or re.search(r"\bmv\b", blob):
        return "This will move or rename files."
    if re.search(r"\b(shutdown|reboot|poweroff|halt)\b", blob):
        return "This will shut down or reboot the machine."
    if re.search(r"\b(dd|mkfs)\b", blob):
        return "This can destroy disk data."
    return "This is irreversible."


def _is_effecting_call(name: str, args: dict) -> bool:
    """True when the tool call itself changes the machine (not list/inspect/browse)."""
    if name == "peripherals":
        action = (args.get("action") or "").strip().lower()
        return action in _EFFECTING_PERIPHERAL
    if name == "device_control":
        action = (args.get("action") or "").strip().lower()
        return action not in _INERT_DEVICE_ACTIONS and bool(action)
    if name == "computer_use":
        action = (args.get("action") or "").strip().lower()
        if not action or action in {"windows", "status", "info", "capabilities"}:
            return False
        if action == "clipboard":
            mode = (args.get("mode") or ("set" if args.get("text") else "get")).lower()
            return mode == "set"
        # screenshot / click / type / key / scroll / drag / move / focus all count
        return True
    if name in {"communicate", "start_task", "run_background_shell", "mcp_invoke"}:
        return True
    if name.startswith("mcp_") and name != "mcp_discover":
        return True
    return False


def _scaffold_mismatches_request(user_text: str, command: str) -> bool:
    low = (user_text or "").lower()
    if not any(w in low for w in _HARDWARE_WORDS):
        return False
    return bool(_SCAFFOLD_RE.search(command or ""))


_LIGHTING_FILLER = {
    "make", "my", "the", "a", "an", "to", "please", "jarvis", "set", "put",
    "into", "on", "of", "for", "now", "just", "can", "you", "me", "do", "turn",
    "its", "them", "those", "this", "your", "hey", "ok", "okay", "sir",
}

_LIGHTING_NOUNS = {
    "rainbow", "spectrum", "wave", "breathing", "breathe", "static", "keyboard",
    "keyboards", "kbd", "backlight", "backlights", "razer", "rgb", "led", "leds",
    "lighting", "lights", "light", "brightness", "brighten", "dim", "colour",
    "color", "off", "max", "full", "percent", "mouse", "deathadder", "asus",
    "aura", "nkey", "keys", "key",
}

_SYSTEM_ECHO = re.compile(
    r"(do not mention these system instructions|stop working now|"
    r"\[system\]|these system instructions)",
    re.I,
)

_OPEN_VERB_RE = re.compile(
    r"\b(?:open|launch|start|go to|navigate to|visit|bring up|pull up)\b",
    re.I,
)
_URL_RE = re.compile(
    r"(https?://[^\s\"'<>]+|(?:www\.)[a-z0-9\-]+(?:\.[a-z0-9\-]+)+(?:/[^\s\"'<>]*)?)",
    re.I,
)
_KNOWN_SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "twitter": "https://x.com",
    "x.com": "https://x.com",
    "reddit": "https://www.reddit.com",
    "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
}
_KNOWN_APPS = {
    "firefox",
    "chrome",
    "chromium",
    "brave",
    "edge",
    "code",
    "cursor",
    "discord",
    "slack",
    "spotify",
    "steam",
    "kitty",
    "alacritty",
    "wezterm",
    "nautilus",
    "dolphin",
    "thunar",
}
_GUI_ONLY_OPEN = re.compile(
    r"\b(?:settings|preferences|dialog|menu|button|form|checkbox|dropdown)\b",
    re.I,
)
# Narrated / pseudo tool calls local models write instead of structured tool_calls.
_BRACKET_TOOL_RE = re.compile(
    r"\[\s*([a-zA-Z_][\w]*)\s*\]\s+([a-zA-Z_][\w]*)"
    r"(?:\s*(?:[:=\-–—]|with)\s*([^\n\[]+))?",
    re.I,
)
_FUNC_TOOL_RE = re.compile(
    r"\b([a-zA-Z_][\w]*)\s*\(\s*((?:action|url|app|target|command|path|query|text|"
    r"x|y|keys|key|mode|value)\s*=[^)]{1,400})\)",
    re.I,
)
# Prose form: ``device_control action=open url=https://…`` (no brackets/parens).
_PROSE_TOOL_RE = re.compile(
    r"(?m)^(?:let me (?:just )?|i(?:'| a)?m going to |i(?:'| wi)ll |"
    r"calling |use |using |run |running |execute |executing )?"
    r"([a-zA-Z_][\w]*)\s+"
    r"((?:action|url|app|target|command|path|query|text|keys|key|mode|value|"
    r"x|y)\s*=\S+(?:\s+(?:action|url|app|target|command|path|query|text|keys|"
    r"key|mode|value|x|y)\s*=\S+)*)",
    re.I,
)
_KV_PAIR_RE = re.compile(
    r"([a-zA-Z_][\w]*)\s*=\s*(\"([^\"]*)\"|'([^']*)'|(\S+))"
)
_COMPUTER_ACTIONS = {
    "screenshot",
    "screen",
    "capture",
    "click",
    "move",
    "drag",
    "scroll",
    "type",
    "key",
    "clipboard",
    "windows",
    "focus",
    "status",
}
_YT_HANDLE_FIXES = {
    "pewdipie": "PewDiePie",
    "pewdipies": "PewDiePie",
    "peediepie": "PewDiePie",
    "pewdiepie": "PewDiePie",
    "pew die pie": "PewDiePie",
}


def _normalise_url(raw: str) -> str:
    url = (raw or "").strip().rstrip(".,);]`")
    if not url:
        return ""
    if not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url
    return url


def _catalog_match(query: str, min_score: float) -> bool:
    """Does the installed-application catalog resolve this confidently?"""
    try:
        from .appcatalog import get_catalog

        hits = get_catalog().resolve(query, limit=1)
    except Exception:  # noqa: BLE001
        return False
    return bool(hits and hits[0][1] >= min_score)


def _yt_handle(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", (name or "").strip())
    fixed = _YT_HANDLE_FIXES.get((name or "").strip().lower()) or _YT_HANDLE_FIXES.get(
        cleaned.lower()
    )
    return fixed or cleaned


def parse_open_intent(text: str, prior: str | None = None) -> dict[str, str] | None:
    """If this is clearly 'open a URL/app', return device_control open args."""
    direct = _parse_open_intent_direct(text)
    if direct:
        return direct
    if prior:
        return _parse_open_followup(text, prior)
    return None


def _parse_open_intent_direct(text: str) -> dict[str, str] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()

    url_m = _URL_RE.search(raw)
    if url_m and (len(raw) < 220 or _OPEN_VERB_RE.search(raw)):
        return {"action": "open", "url": _normalise_url(url_m.group(1))}

    if not _OPEN_VERB_RE.search(low):
        return None
    if _GUI_ONLY_OPEN.search(low) and not url_m:
        return None

    # "open the pewdiepie youtube channel …"
    yt = re.search(
        r"(?:open|launch|go to|visit|bring up|pull up)\s+(?:the\s+)?(.+?)\s+"
        r"youtube\s+channel",
        low,
    )
    if not yt:
        yt = re.search(
            r"youtube\s+channel\s+(?:of\s+|for\s+)?([a-z0-9_.\-]+(?:\s+[a-z0-9_.\-]+)?)",
            low,
        )
    if yt:
        name = re.sub(r"^(?:the|a|an)\s+", "", yt.group(1).strip(" '\""))
        name = re.sub(r"\s+(?:on|in)\s+.*$", "", name).strip()
        name = name.rstrip("'s")
        if name and len(name) < 80:
            handle = _yt_handle(name)
            if handle and " " not in name:
                return {"action": "open", "url": f"https://www.youtube.com/@{handle}"}
            from urllib.parse import quote_plus

            return {
                "action": "open",
                "url": (
                    "https://www.youtube.com/results?search_query="
                    + quote_plus((handle or name) + " channel")
                ),
            }

    for site, base in _KNOWN_SITES.items():
        if re.search(rf"\b{re.escape(site)}\b", low):
            return {"action": "open", "url": base}

    # Domain-ish: open example.com / open foo.io/bar
    dom = re.search(
        r"(?:open|launch|go to|visit)\s+([a-z0-9\-]+(?:\.[a-z0-9\-]+)+(?:/[^\s]*)?)",
        low,
    )
    if dom:
        return {"action": "open", "url": _normalise_url(dom.group(1))}

    # "open roblox studio" — a multi-word app name that resolves to one installed app.
    # The high threshold keeps richer asks ("open a new roblox studio project") on the
    # full agent loop, where the work beyond launching actually happens.
    phrase_m = re.search(
        r"(?:open|launch|start)\s+(?:the\s+)?([a-z0-9_.\- ]{3,48}?)"
        r"(?:\s+(?:app|application|for\s+me|please|now))?\s*$",
        low,
    )
    if phrase_m:
        phrase = phrase_m.group(1).strip()
        if phrase and " " in phrase and _catalog_match(phrase, 85.0):
            return {"action": "open", "app": phrase}

    app_m = re.search(
        r"(?:open|launch|start)\s+(?:the\s+)?([a-z0-9_.\-]+)(?:\s|$)",
        low,
    )
    if app_m:
        app = app_m.group(1)
        if app in _KNOWN_APPS or app.endswith("-browser"):
            return {"action": "open", "app": app}
        # The application catalog knows Flatpak/Snap/Wine apps the whitelist never did.
        if _catalog_match(app, 90.0):
            return {"action": "open", "app": app}

    return None


def _parse_open_followup(text: str, prior: str) -> dict[str, str] | None:
    """Short clarifications after an open/youtube ask → reopen with the name."""
    prior_low = (prior or "").lower()
    if not _OPEN_VERB_RE.search(prior_low):
        return None
    youtubeish = "youtube" in prior_low or "channel" in prior_low
    siteish = any(s in prior_low for s in _KNOWN_SITES) or bool(_URL_RE.search(prior))
    if not youtubeish and not siteish:
        return None

    raw = (text or "").strip()
    if not raw or len(raw) > 120:
        return None
    low = raw.lower()
    if _OPEN_VERB_RE.search(low):
        return None  # handled by direct parser
    if _is_affirmation(raw):
        return _parse_open_intent_direct(prior)

    # "I meant …" / "no, …" / bare name
    m = re.match(
        r"^(?:i\s+meant|meant|no|actually|sorry|wait)[,:]?\s*(.+)$",
        low,
    )
    name = (m.group(1) if m else low).strip(" .\"'")
    name = re.sub(
        r"^(?:the\s+)?(?:famous\s+)?(?:youtuber'?s?\s+)?(?:channel\s+)?",
        "",
        name,
    ).strip(" .\"'")
    vague = {
        "",
        "the famous one",
        "that one",
        "youtuber",
        "the youtuber",
        "channel",
        "his channel",
        "her channel",
        "their channel",
        "famous youtuber",
        "the famous youtuber",
        "famous youtubers channel",
    }
    if name in vague or name.endswith("youtuber's channel") or name.endswith("youtubers channel"):
        # Pull a creator name out of the prior open ask.
        prior_yt = re.search(
            r"(?:open|launch|go to|visit)\s+(?:the\s+)?(.+?)\s+youtube\s+channel",
            prior_low,
        )
        if not prior_yt:
            return None
        name = prior_yt.group(1).strip(" '\"").rstrip("'s")
        name = re.sub(r"^(?:the|a|an)\s+", "", name)

    words = name.split()
    if not name or len(words) > 5 or len(name) > 60:
        return None
    if not re.search(r"[a-z0-9]", name):
        return None

    if youtubeish:
        handle = _yt_handle(name)
        if handle:
            return {"action": "open", "url": f"https://www.youtube.com/@{handle}"}
    return None


def _parse_kwarg_blob(blob: str) -> dict[str, Any]:
    """Parse ``action=screenshot, x=10`` or space-separated kwargs into a dict."""
    text = (blob or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    out: dict[str, Any] = {}
    for m in _KV_PAIR_RE.finditer(text):
        key = m.group(1)
        val = m.group(3) if m.group(3) is not None else (
            m.group(4) if m.group(4) is not None else (m.group(5) or "")
        )
        val = val.strip().rstrip(".,);]")
        if not key:
            continue
        if re.fullmatch(r"-?\d+", val):
            out[key] = int(val)
        elif val.lower() in {"true", "false"}:
            out[key] = val.lower() == "true"
        else:
            out[key] = val
    return out


def _looks_like_tool_prose(text: str, known_tools: set[str] | None = None) -> bool:
    """True when the model dumped a raw tool invocation as the chat reply."""
    raw = (text or "").strip()
    if not raw or len(raw) > 500:
        return False
    # Single-line or short dump of tool syntax.
    compact = re.sub(r"\s+", " ", raw)
    if known_tools:
        for name in known_tools:
            if re.match(rf"^{re.escape(name)}\b", compact, re.I) and "=" in compact:
                return True
    if _BRACKET_TOOL_RE.search(raw) or _FUNC_TOOL_RE.search(raw) or _PROSE_TOOL_RE.search(raw):
        # Dominant content is the tool line (not a long explanation that mentions a tool).
        return len(compact) < 280 or compact.count(" ") < 25
    return False


def recover_text_tool_calls(
    content: str,
    reasoning: str,
    known_tools: set[str],
) -> list[Any]:
    """Turn narrated tool text into real tool calls.

    Local models often write intended calls into reasoning/content instead of the
    structured ``tool_calls`` field — without this, nothing is executed.
    """
    if not known_tools:
        return []
    # Prefer the chat reply when it IS the tool dump (must not be shown as speech).
    if content and _looks_like_tool_prose(content, known_tools):
        found = _extract_text_tool_args(content, known_tools)
        if found:
            return _as_recovered_tool_calls(found)
    blob = f"{reasoning or ''}\n{content or ''}".strip()
    if not blob:
        return []
    return _as_recovered_tool_calls(_extract_text_tool_args(blob, known_tools))


def _extract_text_tool_args(
    blob: str,
    known_tools: set[str],
) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()

    def _push(name: str, args: dict[str, Any]) -> None:
        if not name or name not in known_tools or not args:
            return
        key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if key in seen:
            return
        seen.add(key)
        found.append((name, args))

    for m in _BRACKET_TOOL_RE.finditer(blob):
        name = m.group(1).strip()
        action = (m.group(2) or "").strip().lower()
        rest = (m.group(3) or "").strip()
        args: dict[str, Any] = {}
        if name == "computer_use" and action in _COMPUTER_ACTIONS:
            args["action"] = "screenshot" if action in {"screen", "capture"} else action
            if rest and "=" in rest:
                args.update(_parse_kwarg_blob(rest))
        elif name == "device_control":
            args["action"] = action
            if action == "open":
                url_m = _URL_RE.search(rest) if rest else None
                if url_m:
                    args["url"] = _normalise_url(url_m.group(1))
                elif rest:
                    token = rest.split()[0].strip(".,;\"'")
                    if "." in token or token.startswith("http"):
                        args["url"] = _normalise_url(token)
                    else:
                        args["app"] = token
            elif action == "shell" and rest:
                args["command"] = rest
            elif rest and "=" in rest:
                args.update(_parse_kwarg_blob(rest))
        elif name == "peripherals":
            args["action"] = action
            if rest and "=" in rest:
                args.update(_parse_kwarg_blob(rest))
        elif rest and "=" in rest:
            args = _parse_kwarg_blob(rest)
            if action and "action" not in args:
                args["action"] = action
        _push(name, args)

    for m in _FUNC_TOOL_RE.finditer(blob):
        _push(m.group(1).strip(), _parse_kwarg_blob(m.group(2)))

    for m in _PROSE_TOOL_RE.finditer(blob):
        name = m.group(1).strip()
        args = _parse_kwarg_blob(m.group(2))
        if name == "device_control" and args.get("action") == "open":
            url = args.get("url")
            if isinstance(url, str):
                args["url"] = _normalise_url(url)
        _push(name, args)

    return found[:4]


def _as_recovered_tool_calls(found: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    out = []
    for i, (name, args) in enumerate(found):
        out.append(
            type(
                "ToolCall",
                (),
                {
                    "id": f"recovered_{i}_{uuid.uuid4().hex[:8]}",
                    "function": type(
                        "Fn",
                        (),
                        {"name": name, "arguments": json.dumps(args)},
                    )(),
                },
            )()
        )
    return out


def parse_lighting_intent(text: str, prior: str | None = None) -> dict[str, str] | None:
    """If this utterance is a lighting request, return peripherals control args."""
    low = (text or "").strip().lower()
    if not low:
        return None
    words = re.findall(r"[a-z0-9]+", low)
    leftover = [w for w in words if w not in _LIGHTING_FILLER and w not in _LIGHTING_NOUNS]
    lightingish = (
        "backlight" in low
        or "kbd" in low
        or (("keyboard" in low or "asus" in low) and any(
            w in low for w in ("light", "led", "rgb", "rainbow", "dim", "bright", "aura")
        ))
        or (
            any(w in low for w in ("rainbow", "spectrum"))
            and any(w in low for w in ("keyboard", "kbd", "keys", "backlight", "razer", "asus"))
        )
    )
    leftover_ok = all(
        w in {
            "actually", "still", "want", "need", "mode", "effect", "change",
            "switch", "asus", "zephyrus", "laptop", "keys", "key", "please",
            "lightss", "backlit", "backlightss", "for", "from", "its",
        }
        or w.startswith("light")
        or w.startswith("kbd")
        or w.startswith("asus")
        for w in leftover
    )
    if not lightingish and leftover and not leftover_ok:
        return None
    if not lightingish and leftover:
        return None
    if not any(
        w in low
        for w in (
            "keyboard", "kbd", "backlight", "razer", "rgb", "lighting", "lights",
            "led", "brightness", "asus", "lightss", "keys",
        )
    ) and not any(w.startswith("light") for w in words):
        return None
    if not any(
        w in low
        for w in (
            "rainbow", "spectrum", "wave", "breath", "static", "rgb", "dim",
            "brighten", "brightness", "backlight", "lights", "lighting", "led",
            "off", "on", "make", "turn", "light",
        )
    ) and not any(w.startswith("light") for w in words):
        # Clarification of device only — inherit effect from the prior ask.
        if prior:
            inherited = parse_lighting_intent(prior)
            if inherited:
                inherited = dict(inherited)
                inherited["target"] = "keyboard"
                return inherited
        return None

    if "rainbow" in low or (prior and "rainbow" in prior.lower() and not re.search(r"\b(on|off|dim)\b", low)):
        command, value = "lighting", "rainbow"
    elif "spectrum" in low:
        command, value = "lighting", "spectrum"
    elif "wave" in low:
        command, value = "lighting", "wave"
    elif "breath" in low:
        command, value = "lighting", "breathing"
    elif "dim" in low:
        command, value = "brightness", "25%"
    elif "brighten" in low or "max" in low or "full" in low:
        command, value = "brightness", "100%"
    elif re.search(r"\boff\b", low):
        command, value = "lighting", "off"
    else:
        m = re.search(r"(\d{1,3})\s*%", low)
        command, value = ("brightness", f"{m.group(1)}%") if m else ("lighting", "on")

    if prior and value in {"on"} and "rainbow" in prior.lower() and not re.search(r"\b(on|off|dim|turn)\b", low):
        command, value = "lighting", "rainbow"

    if "mouse" in low or "deathadder" in low:
        target = "razer"
    elif "razer" in low and not any(w in low for w in ("keyboard", "kbd", "backlight", "asus")):
        target = "razer"
    else:
        target = "keyboard"
    return {"action": "control", "target": target, "command": command, "value": value}


def _clean_final(text: str) -> str:
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if not _SYSTEM_ECHO.search(ln)]
    return "\n".join(lines).strip()


def lighting_speech(request: str, ok: bool, output: str, name: str | None) -> str:
    body = (output or "").strip()
    low = (request or "").lower()
    label = name or "the lights"
    if "razer" in low and "keyboard" in low and (
        "no razer keyboard" in body.lower() or "deathadder" in body.lower() or "mouse" in body.lower()
    ):
        if ok:
            return (
                "There isn't a Razer keyboard attached — OpenRazer only sees the "
                "DeathAdder mouse, which has no rainbow keyboard matrix. I set the "
                "laptop key backlight instead."
            )
        return (
            "There isn't a Razer keyboard on this machine, and I couldn't change "
            "the laptop backlight either."
        )
    if ok:
        extra = ""
        for ln in body.splitlines():
            s = ln.strip()
            if s.lower().startswith(("this led", "this razer", "openrazer", "no razer", "aura ")):
                extra = " " + s
                break
        if "aura" in body.lower():
            return f"Very good. The ROG Aura keyboard lighting is on."
        return f"Very good. I applied that to {label}.{extra}".strip()
    if "rgb did not change" in body.lower() or "hidraw" in body.lower():
        return (
            "I turned the key-brightness layer up, but the visible RGB is on the "
            "ASUS Aura keyboard and I couldn't open its hidraw device. Save a sudo "
            "password in Systems so I can write the Aura packets, then ask me again."
        )
    tail = body.splitlines()[-1].strip() if body else "it didn't take."
    return f"I tried to change {label}, but {tail[:240]}"


def _has_imported_mcp_tools() -> bool:
    try:
        from .mcp_client import get_manager

        return bool(get_manager().proxy_tools())
    except Exception:  # noqa: BLE001
        return False


def _goal_from_messages(messages: list[dict]) -> tuple[str, str]:
    """Build a subagent goal and title from recent user turns."""
    user_bits: list[str] = []
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        text = m.get("content")
        if isinstance(text, str) and text.strip() and not text.strip().startswith("[system]"):
            user_bits.append(text.strip())
        if len(user_bits) >= 3:
            break
    user_bits.reverse()
    goal = "\n\n".join(user_bits) if user_bits else "Complete the delegated background work."
    title_source = user_bits[-1] if user_bits else goal
    title = title_source.replace("\n", " ").strip()[:80] or "Background task"
    return goal, title


class AgentInterrupted(Interrupted):
    """Raised when the user interrupts the current agent run."""


class AgentSteered(Exception):
    """The current model stream was cut so a mid-run user utterance can be applied."""


_CANCEL_UTTERANCES = frozenset({
    "stop",
    "cancel",
    "abort",
    "never mind",
    "nevermind",
    "forget it",
    "that's enough",
    "thats enough",
    "stop it",
    "stop that",
    "cancel that",
    "cancel it",
    "please stop",
    "please cancel",
    "enough",
    "cut it out",
    "jarvis stop",
    "stop jarvis",
    "stop please",
    "cancel please",
})


def is_cancel_utterance(text: str) -> bool:
    """True when the user is aborting the current turn, not issuing a new task."""
    raw = (text or "").strip().lower()
    raw = re.sub(r"[.!?…]+$", "", raw).strip()
    raw = re.sub(r"^(hey |ok |okay |please )+", "", raw).strip()
    raw = re.sub(r"^(jarvis[,:]?\s+)", "", raw).strip()
    raw = re.sub(r"[.!?…]+$", "", raw).strip()
    return raw in _CANCEL_UTTERANCES


def _speech_excerpt(text: str, limit: int = 1200) -> str:
    """Trim a working narration so chat/TTS stay barge-in friendly."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned.rfind(". ", 0, limit)
    if cut >= 60:
        return cleaned[: cut + 1]
    cut = cleaned.rfind(" ", 0, limit)
    if cut >= 60:
        return cleaned[:cut] + "…"
    return cleaned[:limit].rstrip() + "…"


class RunControl:
    """Hard-cancel vs barge-in for a live agent turn.

    ``interrupt`` stops the run. ``inject`` queues a user utterance and aborts the
    current LLM stream so the loop can incorporate it without discarding prior work.
    """

    def __init__(self, cancel: asyncio.Event | None = None) -> None:
        self.cancel = cancel if cancel is not None else asyncio.Event()
        self.conversation_id: int | None = None
        self._steer: asyncio.Queue[str] = asyncio.Queue()
        self._pending = asyncio.Event()

    def interrupt(self) -> None:
        self.cancel.set()

    def inject(self, text: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        self._steer.put_nowait(cleaned)
        self._pending.set()

    def has_steer(self) -> bool:
        return self._pending.is_set()

    def drain_steer(self) -> str:
        bits: list[str] = []
        while True:
            try:
                bits.append(self._steer.get_nowait())
            except asyncio.QueueEmpty:
                break
        if self._steer.empty():
            self._pending.clear()
        return "\n".join(bits).strip()

    @property
    def stream_flag(self) -> "_StreamFlag":
        return _StreamFlag(self)


class _StreamFlag:
    """Duck-types asyncio.Event for the LLM client (only ``is_set`` is used)."""

    def __init__(self, control: RunControl) -> None:
        self._control = control

    def is_set(self) -> bool:
        return self._control.cancel.is_set() or self._control.has_steer()


class Agent:
    def __init__(self, config: Config, memory: Memory, depth: int = 0) -> None:
        from .tasks import get_manager

        self.config = config
        self.memory = memory
        self.llm = LLMClient(config.llm)
        self.tools = registry()
        self.depth = depth
        self.ctx = ToolContext(
            config=config,
            memory=memory,
            llm=self.llm,
            tasks=get_manager(),
            depth=depth,
        )
        self._last_user_text = ""
        self._run_suggestions: list[dict[str, str]] = []

    def _as_control(
        self,
        cancel: asyncio.Event | None,
        control: RunControl | None,
    ) -> RunControl:
        if control is not None:
            return control
        return RunControl(cancel=cancel)

    def _check(self, control: RunControl | None) -> None:
        if control is None:
            return
        if control.cancel.is_set():
            raise AgentInterrupted()
        if control.has_steer():
            raise AgentSteered()

    async def _build_messages(
        self,
        conversation_id: int,
        user_text: str,
        user_content: str | list[dict] | None = None,
        *,
        fresh_media: bool = False,
    ) -> list[dict]:
        query_embedding = None if fresh_media else await self.llm.embed(user_text)
        recalled: list[str] = []
        if not fresh_media:
            recalled = self.memory.recall(user_text, query_embedding)
        devices = self.memory.list_devices()
        context_parts: list[str] = []
        learner = get_learner()
        device_ctx = learner.get_context() if learner else ""
        if device_ctx:
            context_parts.append(device_ctx)
        else:
            for d in devices[:3]:
                context_parts.append(f"Known device: {d['name']} ({d['profile'].get('os','?')})")
        peri = get_peripheral_learner()
        peri_ctx = peri.get_context() if peri else ""
        if peri_ctx:
            context_parts.append(peri_ctx)
        from .mcp_client import format_mcp_context

        mcp_ctx = format_mcp_context()
        if mcp_ctx:
            context_parts.insert(0, mcp_ctx)
        if recalled:
            context_parts.append(
                "=== Recalled memory (background only) ===\n"
                "These notes may help IF they clearly relate to the CURRENT user request. "
                "Ignore any that are about a different topic, person, site, or task. "
                "Never change the subject to match recalled memory."
            )
            context_parts.extend(recalled)
        memory_context = "\n".join(
            c if c.startswith("===") else f"- {c}" for c in context_parts
        )

        system = system_prompt(self.config.user_name, memory_context, self.config)
        if fresh_media:
            system += (
                "\n\nVision override: The latest user turn includes NEW media. "
                "A fresh vision scan of that media is provided below. "
                "Answer from that scan (and the attached pixels if present). "
                "Never reuse descriptions of earlier attachments."
            )

        messages: list[dict] = [{"role": "system", "content": system}]
        history = self.memory.get_messages(conversation_id)
        # Exclude the message we just stored (last user turn) — we append multimodal content below.
        if history and history[-1]["role"] == "user":
            history = history[:-1]

        prev_had_attachment = False
        for m in history:
            if m["role"] not in {"user", "assistant"}:
                continue
            text = m["content"] or ""
            atts = m.get("attachments") or []
            if m["role"] == "user" and atts:
                names = ", ".join(
                    a.get("name", "file") for a in atts if a.get("kind") != "frame"
                )
                if fresh_media:
                    # Keep the question, but make clear that prior media is not the current one.
                    text = (
                        f"{text}\n\n"
                        f"[Earlier attachment ({names or 'media'}) — not the current image. "
                        f"Do not reuse that description.]"
                    )
                elif names:
                    text = f"{text}\n\n[Previously attached: {names}]"
                prev_had_attachment = True
                messages.append({"role": "user", "content": text})
                continue

            if m["role"] == "assistant" and fresh_media and prev_had_attachment:
                messages.append(
                    {
                        "role": "assistant",
                        "content": "[Prior reply about an earlier attachment omitted.]",
                    }
                )
                prev_had_attachment = False
                continue

            prev_had_attachment = False
            messages.append({"role": m["role"], "content": text})

        messages.append(
            {"role": "user", "content": user_content if user_content is not None else user_text}
        )
        return messages

    async def _vision_scan(
        self,
        vision_parts: list[dict],
        user_text: str,
        emit: EmitFn,
        control: RunControl,
    ) -> str:
        """Tool-free multimodal pass so the model actually looks at the pixels."""
        from .media import vision_scan_content

        content = vision_scan_content(vision_parts, user_text)
        thought_id = f"t-{uuid.uuid4().hex}"
        await emit({"type": "status", "state": "thinking"})
        await emit({"type": "thought_start", "id": thought_id})
        await emit({
            "type": "thought_delta",
            "id": thought_id,
            "channel": "content",
            "text": "Scanning attached media…\n",
        })

        async def on_delta(delta: dict[str, Any], tid: str = thought_id) -> None:
            self._check(control)
            kind = delta.get("kind")
            text = delta.get("text") or ""
            if not text:
                return
            if kind == "reasoning":
                await emit({"type": "thought_delta", "id": tid, "channel": "reasoning", "text": text})
            elif kind == "content":
                await emit({"type": "thought_delta", "id": tid, "channel": "content", "text": text})

        try:
            message = await self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are JARVIS vision. Describe only what is visible in the "
                            "attached image(s)/frames. Do not mention prior chat topics, "
                            "SMTP, email, or earlier screenshots unless they are clearly "
                            "visible in THESE pixels."
                        ),
                    },
                    {"role": "user", "content": content},
                ],
                tools=None,
                temperature=0.2,
                on_delta=on_delta,
                cancel=control.stream_flag,
            )
        except Interrupted:
            await emit({"type": "thought_end", "id": thought_id})
            self._check(control)
            raise AgentInterrupted()
        except LLMError as exc:
            await emit({"type": "thought_end", "id": thought_id})
            await emit({
                "type": "error",
                "message": (
                    f"Vision scan failed: {exc}. "
                    "Use a vision-capable model (e.g. llava, qwen2.5-vl, llama3.2-vision)."
                ),
            })
            return ""

        description = (message.content or "").strip()
        reasoning = (message.reasoning or "").strip()
        await emit({
            "type": "thought_end",
            "id": thought_id,
            "reasoning": reasoning,
            "content": description or "(no visual description returned)",
        })
        return description

    async def run(
        self,
        conversation_id: int,
        user_text: str,
        emit: EmitFn,
        confirm: ConfirmFn,
        cancel: asyncio.Event | None = None,
        attachments: list[dict] | None = None,
        control: RunControl | None = None,
    ) -> None:
        from .media import MediaError, build_user_content, ingest_attachments

        display_text = (user_text or "").strip() or (
            "Please analyse the attached media." if attachments else ""
        )
        stored_meta: list[dict] = []
        vision_parts: list[dict] = []
        if attachments:
            try:
                stored, vision_parts = ingest_attachments(attachments)
                stored_meta = [
                    a.to_meta()
                    for a in stored
                    if a.kind in {"image", "video"}  # frames are for the model only
                ]
                await emit({
                    "type": "attachments_ready",
                    "attachments": stored_meta,
                })
            except MediaError as exc:
                await emit({"type": "error", "message": str(exc)})
                await emit({"type": "agent_end"})
                return

        ctl = self._as_control(cancel, control)
        ctl.conversation_id = conversation_id
        self.ctx.conversation_id = conversation_id
        self.memory.add_message(conversation_id, "user", display_text, stored_meta or None)
        self._last_user_text = display_text
        self._run_suggestions = []
        open_prior = _outstanding_open_request(self.memory, conversation_id)
        follow = _follow_through_prompt(self.memory, conversation_id, display_text)
        steered_text = (
            follow
            or _action_directive(display_text, open_prior)
            or display_text
        )
        multimodal = build_user_content(steered_text, vision_parts)

        await emit({"type": "agent_start"})
        await emit({"type": "status", "state": "thinking"})

        lighting_text = display_text
        if _is_affirmation(display_text):
            prev = _outstanding_request(self.memory, conversation_id)
            if prev:
                lighting_text = prev
        if not vision_parts:
            try:
                fast_early = await self._try_fast_lighting(lighting_text, emit, ctl)
                if not fast_early:
                    # Use the raw utterance so clarifications ("Pewdipie") resolve
                    # against the prior open/youtube ask via parse_open_intent(prior=…).
                    fast_early = await self._try_fast_open(display_text, emit, ctl)
            except AgentInterrupted:
                await self._finish_interrupted(emit)
                return
            except AgentSteered:
                fast_early = None
            else:
                if fast_early:
                    self.memory.add_message(conversation_id, "assistant", fast_early)
                    await emit({"type": "status", "state": "idle"})
                    await emit({"type": "agent_end"})
                    return

        vision_report = ""
        messages: list[dict] = []
        try:
            if vision_parts:
                self._check(ctl)
                vision_report = await self._vision_scan(vision_parts, display_text, emit, ctl)
                # Ground the agent loop in the fresh scan (tools + some backends drop images).
                grounded = follow or _action_directive(display_text, open_prior) or display_text
                if vision_report:
                    grounded = (
                        f"{grounded}\n\n"
                        f"[Fresh vision scan of media attached THIS turn — "
                        f"treat this as authoritative; ignore earlier image talk]\n"
                        f"{vision_report}"
                    )
                # Prefer multimodal user turn when possible, but always include the scan text.
                if isinstance(multimodal, list):
                    user_content: str | list[dict] = [
                        {
                            "type": "text",
                            "text": grounded
                            + "\n\n(Images/frames for this turn also follow — verify against the scan.)",
                        },
                        *[p for p in multimodal if p.get("type") != "text"],
                    ]
                else:
                    user_content = grounded
                messages = await self._build_messages(
                    conversation_id, display_text, user_content, fresh_media=True
                )
            else:
                messages = await self._build_messages(
                    conversation_id, display_text, multimodal, fresh_media=False
                )
            self._check(ctl)
        except AgentInterrupted:
            await self._finish_interrupted(emit)
            return
        except AgentSteered:
            if not messages:
                messages = await self._build_messages(
                    conversation_id, display_text, multimodal, fresh_media=bool(vision_parts)
                )
            try:
                await self._apply_steer(messages, conversation_id, emit, ctl)
            except AgentInterrupted:
                await self._finish_interrupted(emit)
                return
        except Exception as exc:  # noqa: BLE001
            await emit({"type": "error", "message": f"Failed to prepare context: {exc}"})
            await emit({"type": "agent_end"})
            return

        final_text = ""
        persist_final = True

        try:
            delegation_guard = _wants_delegation(display_text) or self._conversation_wants_delegation(
                conversation_id
            )
            mcp_guard = _wants_mcp(display_text) and _has_imported_mcp_tools()
            open_prior = _outstanding_open_request(self.memory, conversation_id)
            open_follow = bool(parse_open_intent(display_text, open_prior))
            action_guard = _wants_action(display_text) or bool(follow) or open_follow
            mutation_guard = _wants_mutation(display_text) or bool(follow) or open_follow

            fast = None
            if not vision_parts and not delegation_guard:
                fast = await self._try_fast_lighting(lighting_text, emit, ctl)
                if not fast:
                    fast = await self._try_fast_open(display_text, emit, ctl)
                if not fast:
                    fast = await self._try_fast_control(display_text, emit, ctl)
            if fast:
                final_text = fast
            else:
                budget = (
                    ACTION_MAX_ITERATIONS
                    if action_guard and not delegation_guard
                    else MAX_ITERATIONS
                )
                final_text = await self._tool_loop(
                    messages,
                    emit,
                    confirm,
                    ctl,
                    budget,
                    delegation_guard=delegation_guard,
                    mcp_guard=mcp_guard,
                    action_guard=action_guard,
                    mutation_guard=mutation_guard,
                    conversation_id=conversation_id,
                )
            if ctl.has_steer():
                if final_text:
                    self.memory.add_message(conversation_id, "assistant", final_text)
                    persist_final = False
                await self._apply_steer(messages, conversation_id, emit, ctl)
                more = await self._tool_loop(
                    messages,
                    emit,
                    confirm,
                    ctl,
                    8,
                    delegation_guard=delegation_guard,
                    mcp_guard=mcp_guard,
                    action_guard=action_guard,
                    mutation_guard=mutation_guard,
                    conversation_id=conversation_id,
                )
                if more:
                    final_text = more
                    persist_final = True
        except AgentInterrupted:
            await self._finish_interrupted(emit)
            return
        except asyncio.CancelledError:
            await self._finish_interrupted(emit)
            return

        if final_text and persist_final:
            self.memory.add_message(conversation_id, "assistant", final_text)
            summary = f"User asked: {display_text[:200]} | JARVIS answered: {final_text[:300]}"
            embedding = await self.llm.embed(summary)
            self.memory.add_memory("conversation", summary, embedding)

        suggestions = _dedupe_suggestions(self._run_suggestions)
        if suggestions:
            await emit({"type": "suggestions", "items": suggestions})

        await emit({"type": "status", "state": "idle"})
        await emit({"type": "agent_end"})

    async def run_task(
        self,
        goal: str,
        emit: EmitFn,
        confirm: ConfirmFn,
        cancel: asyncio.Event | None = None,
        control: RunControl | None = None,
    ) -> str:
        """Run an autonomous background subagent for a single goal.

        Uses a synthetic context (no conversation history) and returns the final report.
        Raises AgentInterrupted when cancelled.
        """
        ctl = self._as_control(cancel, control)
        from .mcp_client import format_mcp_context

        extra = format_mcp_context()
        memory_context = extra or ""
        system = system_prompt(self.config.user_name, memory_context, self.config) + SUBAGENT_ADDENDUM
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": goal},
        ]
        self._run_suggestions = []
        return await self._tool_loop(
            messages,
            emit,
            confirm,
            ctl,
            SUBAGENT_MAX_ITERATIONS,
            mcp_guard=_wants_mcp(goal) and _has_imported_mcp_tools(),
            action_guard=_wants_action(goal),
            conversation_id=None,
        )

    def _conversation_wants_delegation(self, conversation_id: int) -> bool:
        """True if a recent user turn in this conversation asked for subagents."""
        for m in reversed(self.memory.get_messages(conversation_id)[-12:]):
            if m.get("role") == "user" and _wants_delegation(m.get("content") or ""):
                return True
        return False

    async def _force_spawn_subagent(
        self,
        messages: list[dict],
        emit: EmitFn,
    ) -> str | None:
        """Programmatically start a background subagent when the model won't call start_task.

        Returns a user-facing notice including the task id, or None if spawn failed.
        """
        manager = self.ctx.tasks
        if manager is None:
            return None
        goal, title = _goal_from_messages(messages)
        call_id = f"call-{uuid.uuid4().hex[:12]}"
        args = {"goal": goal, "title": title}
        await emit({
            "type": "tool_call",
            "id": call_id,
            "name": "start_task",
            "args": args,
            "dangerous": False,
            "auto_approved": True,
        })
        try:
            task_id = await manager.start_agent_task(
                goal, title, self.ctx.conversation_id
            )
        except Exception as exc:  # noqa: BLE001
            output = f"Failed to start background subagent: {exc}"
            await emit({
                "type": "tool_result",
                "id": call_id,
                "name": "start_task",
                "ok": False,
                "output": output,
            })
            return None
        output = (
            f"Background subagent started: {task_id}. It runs independently; its report "
            f"will be posted here when finished."
        )
        await emit({
            "type": "tool_result",
            "id": call_id,
            "name": "start_task",
            "ok": True,
            "output": output,
        })
        return (
            f"Very good, Sir. I have deployed background subagent **{task_id}** "
            f"({title}). It is working now; I shall notify you when its report arrives."
        )

    async def _try_fast_lighting(
        self,
        display_text: str,
        emit: EmitFn,
        control: RunControl,
    ) -> str | None:
        """Run keyboard/RGB lighting immediately — no LLM round-trip."""
        prior = None
        cid = self.ctx.conversation_id
        if cid is not None:
            prior = _outstanding_request(self.memory, cid)
        intent = parse_lighting_intent(display_text, prior)
        if not intent:
            return None
        from .peripherals import control_lighting_direct

        self._check(control)
        call_id = f"call-{uuid.uuid4().hex[:12]}"
        args = {
            "action": "control",
            "target": intent["target"],
            "command": intent["command"],
            "value": intent["value"],
        }
        await emit({"type": "say", "text": "At once.", "working": True})
        await emit({
            "type": "tool_call",
            "id": call_id,
            "name": "peripherals",
            "args": args,
            "dangerous": False,
            "auto_approved": True,
        })
        await emit({"type": "status", "state": "running_tool"})
        try:
            ok, output, p = await asyncio.to_thread(
                control_lighting_direct,
                intent["target"],
                intent["command"],
                intent["value"],
            )
        except Exception as exc:  # noqa: BLE001
            ok, output, p = False, str(exc), None
        await emit({
            "type": "tool_result",
            "id": call_id,
            "name": "peripherals",
            "ok": ok,
            "output": output,
        })
        speech = lighting_speech(display_text, ok, output, (p or {}).get("name"))
        await emit({"type": "assistant", "text": speech})
        return speech

    #: Words that mean "act on that peripheral", which the peripherals tool owns.
    _PERIPHERAL_SCOPE = {
        "keyboard": ("keyboard_backlight",),
        "keypad": ("keyboard_backlight",),
        "mouse": (),
        "headset": (),
        "headphone": (),
        "headphones": (),
        "earbuds": (),
        "controller": (),
        "printer": ("printer.",),
        "webcam": ("camera.",),
    }

    async def _try_fast_control(
        self,
        display_text: str,
        emit: EmitFn,
        control: RunControl,
    ) -> str | None:
        """Run a catalogued device control straight from the request.

        "dim my screen" is not a question; it is an instruction with exactly one
        sensible execution. Resolving it here means it always runs, instead of
        depending on the model choosing to call a tool.
        """
        from . import controls as ctl
        from .tools import device_control as dc

        text = (display_text or "").strip()
        low = text.lower()
        if not text or low.startswith(("how do i", "how to", "how can i", "how would")):
            return None
        res = ctl.resolve(text, fast_only=True)
        if res is None or res.confidence < 0.8:
            return None
        if res.control.risky or res.control.sudo:
            return None
        if res.control.reads and not ctl.has_readout(res.control.id):
            # No way to phrase the output in one sentence — let the model do it.
            return None
        for word, allowed in self._PERIPHERAL_SCOPE.items():
            if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low):
                if not any(token in res.control.id for token in allowed):
                    return None  # belongs to the peripherals tool, not the host
        try:
            command = res.render()
        except ValueError:
            return None

        self._check(control)
        call_id = f"call-{uuid.uuid4().hex[:12]}"
        args = {"action": "control", "control": res.control.id, **res.params}
        await emit({"type": "say", "text": "At once.", "working": True})
        await emit({
            "type": "tool_call",
            "id": call_id,
            "name": "device_control",
            "args": args,
            "dangerous": False,
            "auto_approved": True,
        })
        await emit({"type": "status", "state": "running_tool"})
        try:
            result = await dc._run(args, self.ctx)
            ok, output = result.ok, result.output
        except Exception as exc:  # noqa: BLE001
            ok, output = False, str(exc)
        await emit({
            "type": "tool_result",
            "id": call_id,
            "name": "device_control",
            "ok": ok,
            "output": output,
        })
        if ok:
            speech = ctl.readout(res.control.id, output) if res.control.reads else None
            if not speech and res.control.reads:
                # The probe ran but its output did not parse — report it plainly rather
                # than leaving the call orphaned for the model to repeat.
                body = output.split("\n(succeeded)\n", 1)[-1]
                trimmed = " ".join(ln.strip() for ln in body.splitlines() if ln.strip())
                speech = f"{res.control.summary}: {_speech_excerpt(trimmed, 300)}"
            if not speech:
                speech = f"Very good — {ctl.describe(res)}."
        else:
            speech = (
                f"I couldn't manage that — {ctl.describe(res)} failed: "
                f"{_speech_excerpt(output, 240)}"
            )
        await emit({"type": "assistant", "text": speech})
        return speech

    async def _try_fast_open(
        self,
        display_text: str,
        emit: EmitFn,
        control: RunControl,
    ) -> str | None:
        """Open a URL/app immediately when the ask is unambiguous — no LLM round-trip."""
        prior = None
        cid = self.ctx.conversation_id
        if cid is not None:
            prior = _outstanding_open_request(self.memory, cid)
        intent = parse_open_intent(display_text, prior)
        if not intent:
            return None
        from .tools import apps as apps_mod

        self._check(control)
        call_id = f"call-{uuid.uuid4().hex[:12]}"
        await emit({"type": "say", "text": "At once.", "working": True})
        await emit({
            "type": "tool_call",
            "id": call_id,
            "name": "device_control",
            "args": intent,
            "dangerous": False,
            "auto_approved": True,
        })
        await emit({"type": "status", "state": "running_tool"})
        try:
            if intent.get("url"):
                result = await apps_mod._open_url({"url": intent["url"]}, self.ctx)
            else:
                result = await apps_mod._open_app(
                    {"app": intent.get("app") or intent.get("target") or ""},
                    self.ctx,
                )
            ok, output = result.ok, result.output
        except Exception as exc:  # noqa: BLE001
            ok, output = False, str(exc)
        await emit({
            "type": "tool_result",
            "id": call_id,
            "name": "device_control",
            "ok": ok,
            "output": output,
        })
        target = intent.get("url") or intent.get("app") or "that"
        if ok:
            speech = f"Very good — opened {target}."
        else:
            speech = f"I couldn't open {target}: {output}"
        await emit({"type": "assistant", "text": speech})
        return speech

    async def _tool_loop(
        self,
        messages: list[dict],
        emit: EmitFn,
        confirm: ConfirmFn,
        control: RunControl,
        max_iterations: int,
        delegation_guard: bool = False,
        mcp_guard: bool = False,
        action_guard: bool = False,
        mutation_guard: bool = False,
        conversation_id: int | None = None,
    ) -> str:
        """The LLM <-> tool iteration loop. Returns the final assistant text.

        When ``delegation_guard`` is set (user explicitly asked for background/subagent
        work), the loop refuses to finalize with a plain-text answer until ``start_task``
        has actually been called at least once — forcing the call if the model only
        narrates its intent. This is the common failure mode with local tool-calling models.

        When ``mcp_guard`` is set, refuse plain-text answers that deny MCP access or
        ignore an explicit request to use installed MCP tools — force ``mcp_invoke`` instead.

        When ``action_guard`` is set (user asked to *do* something on this machine),
        refuse how-to dumps and permission-asking replies until tools have actually run,
        and run any command the model pasted as its answer instead of speaking it back.
        ``mutation_guard`` additionally refuses to stop after a mere list/inspect —
        a control/shell/open/connect call must happen.
        """
        schemas = openai_schemas()
        final_text = ""
        empty_rounds = 0
        llm_failed = False
        started_task = False
        force_start_task = False
        used_mcp = False
        force_mcp_invoke = False
        mcp_pushes = 0
        provision_pushes = 0
        command_pushes = 0
        intent_pushes = 0
        delegation_pushes = 0
        action_pushes = 0
        force_action = False
        used_tools = False
        acted = False
        fail_retries = 0
        config_block_nudges = 0
        budget = max_iterations
        i = 0

        while i < budget:
            i += 1
            try:
                self._check(control)
            except AgentSteered:
                await self._apply_steer(messages, conversation_id, emit, control)
                budget = min(budget + 4, max_iterations + 16)
                continue

            current_thought = f"t-{uuid.uuid4().hex}"
            await emit({"type": "thought_start", "id": current_thought})

            async def on_delta(delta: dict[str, Any], tid: str = current_thought) -> None:
                self._check(control)
                kind = delta.get("kind")
                text = delta.get("text") or ""
                if not text:
                    return
                if kind == "reasoning":
                    await emit({"type": "thought_delta", "id": tid, "channel": "reasoning", "text": text})
                elif kind == "content":
                    await emit({"type": "thought_delta", "id": tid, "channel": "content", "text": text})

            tool_choice = (
                {"type": "function", "function": {"name": "start_task"}}
                if force_start_task
                else {"type": "function", "function": {"name": "mcp_invoke"}}
                if force_mcp_invoke
                else "required"
                if force_action or (
                    action_guard
                    and action_pushes < 1
                    and (not used_tools or (mutation_guard and not acted))
                )
                else None
            )
            force_start_task = False
            force_mcp_invoke = False
            force_action = False
            try:
                message = await self._chat_with_optional_force(
                    messages, schemas, on_delta, control.stream_flag, tool_choice
                )
            except Interrupted:
                await emit({"type": "thought_end", "id": current_thought})
                try:
                    self._check(control)
                except AgentSteered:
                    await self._apply_steer(messages, conversation_id, emit, control)
                    budget = min(budget + 4, max_iterations + 16)
                    continue
                raise AgentInterrupted()
            except LLMError as exc:
                await emit({"type": "thought_end", "id": current_thought})
                await emit({"type": "error", "message": str(exc)})
                llm_failed = True
                break

            tool_calls = message.tool_calls or []
            content = message.content or ""
            reasoning = message.reasoning or ""

            await emit({
                "type": "thought_end",
                "id": current_thought,
                "reasoning": reasoning,
                "content": content,
            })
            try:
                self._check(control)
            except AgentSteered:
                if content.strip():
                    await self._emit_working_speech(content, conversation_id, emit)
                await self._apply_steer(messages, conversation_id, emit, control)
                budget = min(budget + 4, max_iterations + 16)
                continue

            if not tool_calls:
                # Local models often narrate ``[computer_use] screenshot`` in reasoning
                # instead of emitting structured tool_calls — recover and execute them.
                recovered = recover_text_tool_calls(
                    content, reasoning, set(self.tools.keys())
                )
                if recovered:
                    tool_calls = recovered
                    content = ""

            if (
                not tool_calls
                and command_pushes < 2
                and (action_guard or not used_tools)
                and not _asked_for_text(self._last_user_text)
            ):
                # The reply *is* the command ("brightnessctl set 30%", or a fenced
                # "run this"). Handing back a command the user must run themselves is
                # the failure this guard exists to stop: run it, through the normal
                # gates, and let the model answer from real output.
                pasted = pasted_commands(content)
                if pasted:
                    command_pushes += 1
                    tool_calls = _as_recovered_tool_calls([
                        ("device_control", {"action": "shell", "command": cmd})
                        for cmd in pasted
                    ])
                    content = ""
                    await emit({"type": "status", "state": "running_tool"})

            if not tool_calls:
                if content:
                    if mcp_guard and not used_mcp and mcp_pushes < 2:
                        should_push = _denies_mcp_access(content) or not used_tools
                        if should_push:
                            mcp_pushes += 1
                            force_mcp_invoke = True
                            from .mcp_client import format_mcp_context

                            hint = format_mcp_context(max_tools_per_server=12)
                            messages.append({"role": "assistant", "content": content})
                            messages.append({
                                "role": "user",
                                "content": (
                                    "[system] You incorrectly answered without calling MCP tools. "
                                    "Installed MCP servers ARE available. Call the matching imported "
                                    "tool directly (mcp_<server>_<tool>) or mcp_invoke "
                                    "(server, tool, arguments). Do not claim you lack access.\n\n"
                                    f"{hint}"
                                ),
                            })
                            await emit({"type": "status", "state": "thinking"})
                            continue
                    if (
                        intent_pushes < 1
                        and not used_tools
                        and _narrates_intent(content)
                        and not _lacks_capability(content)
                    ):
                        # "I'll read your CPU temperature" — and then nothing ran.
                        # That sentence is working speech, not an answer.
                        intent_pushes += 1
                        messages.append({"role": "assistant", "content": content})
                        messages.append({
                            "role": "user",
                            "content": (
                                "[system] You said what you were about to do but called "
                                "no tool, so nothing happened and the user has no answer. "
                                "Call the tool now and answer from its output. For "
                                "anything about this machine, device_control "
                                "action=control control=<id> performs it — the available "
                                "ids are listed in your context; device_control "
                                "action=controls query=<word> searches them."
                            ),
                        })
                        await emit({"type": "status", "state": "thinking"})
                        continue
                    if (
                        provision_pushes < 1
                        and not used_tools
                        and _lacks_capability(content)
                    ):
                        # Nothing ran and the reply pleads a missing integration —
                        # JARVIS can install one itself instead of handing back homework.
                        provision_pushes += 1
                        messages.append({"role": "assistant", "content": content})
                        messages.append({
                            "role": "user",
                            "content": (
                                "[system] Do not hand back a missing capability. You can "
                                "give yourself one: call mcp_discover with mode=ensure and "
                                "goal=<the capability this request needs> to search the "
                                "public registries and install a server (remote, or local "
                                "via npx/uvx/docker), then use the tools it imports. If the "
                                "task needs a desktop application instead, call "
                                "device_control action=apps query=<purpose> to find it or "
                                "get an install command, install it, and continue. Act now; "
                                "only report back if a required API key is genuinely missing."
                            ),
                        })
                        await emit({"type": "status", "state": "thinking"})
                        continue
                    if delegation_guard and not started_task:
                        if delegation_pushes < 2:
                            delegation_pushes += 1
                            force_start_task = True
                            messages.append({"role": "assistant", "content": content})
                            messages.append({
                                "role": "user",
                                "content": (
                                    "[system] You described delegating work but did NOT call "
                                    "start_task, so no subagent exists yet. Call start_task now "
                                    "with a complete, self-contained goal (make one call per "
                                    "independent line of work). Do this before replying."
                                ),
                            })
                            await emit({"type": "status", "state": "thinking"})
                            continue
                        notice = await self._force_spawn_subagent(messages, emit)
                        if notice:
                            started_task = True
                            final_text = await self._emit_assistant(notice, emit)
                            break
                    if (
                        action_guard
                        and action_pushes < 1
                        and (
                            not used_tools
                            or (mutation_guard and not acted)
                            or _looks_like_instructions(content)
                        )
                    ):
                        action_pushes += 1
                        forced = await self._try_fast_lighting(
                            self._last_user_text, emit, control
                        )
                        if not forced:
                            forced = await self._try_fast_open(
                                self._last_user_text, emit, control
                            )
                        if forced:
                            final_text = forced
                            break
                        force_action = True
                        messages.append({"role": "assistant", "content": content})
                        messages.append({
                            "role": "user",
                            "content": (
                                "[system] You wrote a plan or script instead of running it, "
                                "or you only listed devices. Call peripherals (action=control "
                                "for lighting/volume/brightness/connect), device_control "
                                "(action=shell or action=open for URLs/apps), and/or "
                                "computer_use NOW and execute the user's last request. "
                                "Stay on that request — do not switch to a different device "
                                "and do not scaffold a website or paste npm/vite/mkdir commands."
                            ),
                        })
                        await emit({"type": "status", "state": "thinking"})
                        continue
                    cleaned = _clean_final(content)
                    if _looks_like_tool_prose(cleaned, set(self.tools.keys())):
                        # Last resort: never show raw tool syntax as the spoken reply.
                        forced = await self._try_fast_open(
                            self._last_user_text, emit, control
                        )
                        if forced:
                            final_text = forced
                            break
                        final_text = await self._emit_assistant(
                            self._synthesize_reply(messages), emit
                        )
                        break
                    if action_guard and _looks_like_instructions(cleaned):
                        final_text = await self._emit_assistant(
                            self._synthesize_reply(messages), emit
                        )
                        break
                    final_text = await self._emit_assistant(cleaned or content, emit)
                    break
                # Reasoning-only / empty reply (common with local <think> models).
                # After tools have already run, the "[system] no visible text" nudge
                # makes models narrate that message and stall with nothing in chat.
                empty_rounds += 1
                messages.append({"role": "assistant", "content": reasoning[:2000] or "(no output)"})
                if used_tools or empty_rounds > 1:
                    if delegation_guard and not started_task:
                        notice = await self._force_spawn_subagent(messages, emit)
                        if notice:
                            started_task = True
                            final_text = await self._emit_assistant(notice, emit)
                    if mutation_guard and not acted:
                        forced = await self._try_fast_lighting(
                            self._last_user_text, emit, control
                        )
                        if not forced:
                            forced = await self._try_fast_open(
                                self._last_user_text, emit, control
                            )
                        if forced:
                            final_text = forced
                            break
                    break
                messages.append({
                    "role": "user",
                    "content": (
                        "[system] Your previous reply contained no visible text and no tool "
                        "calls, so nothing happened. Continue the task NOW: either call the "
                        "appropriate tools (use start_task to delegate long or parallel work "
                        "to background subagents) or state your final answer as plain text."
                    ),
                })
                if delegation_guard and not started_task:
                    force_start_task = True
                if action_guard and not used_tools:
                    force_action = True
                await emit({"type": "status", "state": "thinking"})
                continue

            empty_rounds = 0
            used_tools = True
            if content.strip() and not _looks_like_instructions(content):
                await self._emit_working_speech(content, conversation_id, emit)
            messages.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            steered_mid_tools = False
            round_effecting_failed = False
            round_config_miss: str | None = None
            for ti, tc in enumerate(tool_calls):
                if control.has_steer() and not control.cancel.is_set():
                    for skip in tool_calls[ti:]:
                        skipped = (
                            "Not run — the user spoke before this tool started."
                        )
                        await emit({
                            "type": "tool_result",
                            "id": skip.id,
                            "name": skip.function.name,
                            "ok": False,
                            "output": skipped,
                        })
                        messages.append(
                            {"role": "tool", "tool_call_id": skip.id, "content": skipped}
                        )
                    await self._apply_steer(messages, conversation_id, emit, control)
                    steered_mid_tools = True
                    break
                self._check(control)
                if tc.function.name == "start_task":
                    started_task = True
                if tc.function.name == "mcp_invoke" or tc.function.name.startswith("mcp_"):
                    used_mcp = True
                try:
                    call_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    call_args = {}
                try:
                    tool_result = await self._execute_tool(tc, emit, confirm, control)
                except AgentSteered:
                    result_text = (
                        "The user spoke before this action finished."
                    )
                    await emit({
                        "type": "tool_result",
                        "id": tc.id,
                        "name": tc.function.name,
                        "ok": False,
                        "output": result_text,
                    })
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                    )
                    for skip in tool_calls[ti + 1 :]:
                        skipped = "Not run — the user spoke before this tool started."
                        await emit({
                            "type": "tool_result",
                            "id": skip.id,
                            "name": skip.function.name,
                            "ok": False,
                            "output": skipped,
                        })
                        messages.append(
                            {"role": "tool", "tool_call_id": skip.id, "content": skipped}
                        )
                    await self._apply_steer(messages, conversation_id, emit, control)
                    steered_mid_tools = True
                    break
                from .tools.base import ToolResult

                if isinstance(tool_result, ToolResult):
                    result_text = tool_result.output
                    screen_images = tool_result.images
                else:
                    result_text = str(tool_result)
                    screen_images = None
                miss = _config_miss_kind(str(result_text))
                if miss:
                    round_config_miss = miss
                    hint = _CONFIG_SUGGESTIONS.get(miss)
                    if hint:
                        self._run_suggestions.append(hint)
                if _is_effecting_call(tc.function.name, call_args):
                    if not str(result_text).startswith("Refused:"):
                        acted = True
                    if _looks_like_tool_failure(str(result_text)):
                        round_effecting_failed = True
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                )
                if screen_images:
                    # Tool-role messages cannot carry images on most OpenAI-compatible
                    # endpoints — inject a multimodal user turn so the model can see
                    # the screen and continue the GUI loop.
                    messages.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "[Screen capture from computer_use for THIS step. "
                                    "Look at the pixels. Coordinates match the image. "
                                    "Continue the task with computer_use click/type/key/"
                                    "scroll, or screenshot again after acting. Do not "
                                    "mention these instructions.]"
                                ),
                            },
                            *screen_images,
                        ],
                    })
                    budget = min(budget + 2, max_iterations + 24)

            if steered_mid_tools:
                budget = min(budget + 4, max_iterations + 16)
                continue

            try:
                self._check(control)
            except AgentSteered:
                await self._apply_steer(messages, conversation_id, emit, control)
                budget = min(budget + 4, max_iterations + 16)
                continue

            if round_config_miss and config_block_nudges < 1:
                config_block_nudges += 1
                messages.append({
                    "role": "user",
                    "content": (
                        "[system] A required setting is missing (sudo password or email). "
                        "Do not retry the same privileged command. Tell the user what blocked "
                        "you and stop. Optional next steps may follow as suggestions."
                    ),
                })
                await emit({"type": "status", "state": "thinking"})
                continue

            if (
                action_guard
                and round_effecting_failed
                and not round_config_miss
                and fail_retries < FAIL_RETRY_LIMIT
            ):
                fail_retries += 1
                force_action = True
                budget = min(budget + 3, max_iterations + 12)
                messages.append({
                    "role": "user",
                    "content": (
                        "[system] That attempt failed. Diagnose with a tool and try a "
                        "different method now. Do not hand the user a manual."
                    ),
                })
                await emit({"type": "status", "state": "thinking"})
                continue

            await emit({"type": "status", "state": "thinking"})

        if delegation_guard and not started_task:
            notice = await self._force_spawn_subagent(messages, emit)
            if notice:
                started_task = True
                final_text = await self._emit_assistant(notice, emit)

        if not final_text and not llm_failed:
            if action_guard:
                forced = await self._try_fast_lighting(self._last_user_text, emit, control)
                if not forced:
                    forced = await self._try_fast_open(self._last_user_text, emit, control)
                if forced:
                    final_text = forced
                else:
                    final_text = await self._emit_assistant(
                        self._synthesize_reply(messages), emit
                    )
            else:
                try:
                    final_text = await self._wrap_up(messages, emit, control)
                except AgentSteered:
                    await self._apply_steer(messages, conversation_id, emit, control)
                    return await self._tool_loop(
                        messages,
                        emit,
                        confirm,
                        control,
                        8,
                        delegation_guard=delegation_guard,
                        mcp_guard=mcp_guard,
                        action_guard=action_guard,
                        mutation_guard=mutation_guard,
                        conversation_id=conversation_id,
                    )
        if final_text and action_guard and _looks_like_instructions(final_text):
            synthesized = self._synthesize_reply(messages)
            if synthesized and not _looks_like_instructions(synthesized):
                final_text = synthesized
            elif acted:
                final_text = (
                    "I ran that on this machine. The activity feed has the exact "
                    "result — I won't paste a script here."
                )
            else:
                final_text = (
                    "I should have executed that on this machine rather than writing "
                    "steps. Please ask me again and I will run it."
                )
            final_text = await self._emit_assistant(final_text, emit)
        elif not (final_text or "").strip():
            final_text = await self._emit_assistant(self._synthesize_reply(messages), emit)

        return final_text

    async def _apply_steer(
        self,
        messages: list[dict],
        conversation_id: int | None,
        emit: EmitFn,
        control: RunControl,
    ) -> None:
        """Fold a barge-in utterance into the live turn, or abort if they cancelled."""
        text = control.drain_steer()
        if not text:
            return
        if conversation_id is not None:
            self.memory.add_message(conversation_id, "user", text)
        await emit({"type": "steered", "text": text})
        if is_cancel_utterance(text):
            raise AgentInterrupted()
        name = self.config.user_name or "Sir"
        messages.append({
            "role": "user",
            "content": (
                f"{text}\n\n"
                f"[system] {name} spoke while you were still working. This is a live "
                f"barge-in, not a new conversation. If they elaborated or redirected, "
                f"adjust the current plan and continue (do not restart from scratch). "
                f"If they asked about something you just said, answer it, then resume "
                f"remaining work unless they told you to stop. Acknowledge in one short "
                f"spoken sentence, then call tools or finish. Do not mention these "
                f"instructions."
            ),
        })
        await emit({"type": "status", "state": "thinking"})

    async def _emit_working_speech(
        self,
        text: str,
        conversation_id: int | None,
        emit: EmitFn,
    ) -> None:
        spoken = _speech_excerpt(text)
        if not spoken:
            return
        await emit({"type": "say", "text": spoken, "working": True})
        if conversation_id is not None:
            self.memory.add_message(conversation_id, "assistant", spoken)

    async def _emit_assistant(self, text: str, emit: EmitFn) -> str:
        """Emit a final reply with suggestion trailers stripped into _run_suggestions."""
        cleaned, extra = _extract_suggestions(text)
        if extra:
            self._run_suggestions.extend(extra)
        if cleaned:
            await emit({"type": "assistant", "text": cleaned})
        return cleaned

    async def _chat_with_optional_force(
        self,
        messages: list[dict],
        schemas: list[dict],
        on_delta: Any,
        cancel: Any,
        tool_choice: Any | None,
    ):
        """Chat call that, when forcing a specific tool, retries with ``auto`` if the
        endpoint rejects the forced ``tool_choice`` (local backends vary in support)."""
        if tool_choice is None:
            return await self.llm.chat(messages, tools=schemas, on_delta=on_delta, cancel=cancel)
        try:
            return await self.llm.chat(
                messages, tools=schemas, on_delta=on_delta, cancel=cancel, tool_choice=tool_choice
            )
        except Interrupted:
            raise
        except LLMError:
            # Forced tool choice unsupported — fall back to the model's own decision.
            return await self.llm.chat(messages, tools=schemas, on_delta=on_delta, cancel=cancel)

    async def _wrap_up(
        self,
        messages: list[dict],
        emit: EmitFn,
        control: RunControl,
    ) -> str:
        """Force a final tool-free answer when the loop ended without one.

        Reached when the iteration budget is exhausted or the model kept returning
        empty replies; without this the run would end with no message at all.
        """
        self._check(control)
        messages.append({
            "role": "user",
            "content": (
                "[system] Stop working now. Summarise for the user, in plain spoken text, "
                "what you have done so far, what you found, and what remains unfinished "
                "(mention any background task ids you started). Do not call any tools. "
                "Do not paste a script, fenced command block, or npm/vite recipe. "
                "Do not mention these system instructions."
            ),
        })
        thought_id = f"t-{uuid.uuid4().hex}"
        await emit({"type": "thought_start", "id": thought_id})

        async def on_delta(delta: dict[str, Any], tid: str = thought_id) -> None:
            self._check(control)
            kind = delta.get("kind")
            text = delta.get("text") or ""
            if not text:
                return
            if kind == "reasoning":
                await emit({"type": "thought_delta", "id": tid, "channel": "reasoning", "text": text})
            elif kind == "content":
                await emit({"type": "thought_delta", "id": tid, "channel": "content", "text": text})

        try:
            message = await self.llm.chat(
                messages, tools=None, on_delta=on_delta, cancel=control.stream_flag
            )
        except Interrupted:
            await emit({"type": "thought_end", "id": thought_id})
            self._check(control)
            raise AgentInterrupted()
        except LLMError as exc:
            await emit({"type": "thought_end", "id": thought_id})
            await emit({"type": "error", "message": str(exc)})
            return ""

        content = (message.content or "").strip()
        reasoning = (message.reasoning or "").strip()
        await emit({
            "type": "thought_end",
            "id": thought_id,
            "reasoning": reasoning,
            "content": content,
        })
        text = _clean_final(content or reasoning)
        if text:
            if len(text) > 4000:
                text = text[:4000].rstrip() + "…"
            text = await self._emit_assistant(text, emit)
        return text

    def _synthesize_reply(self, messages: list[dict]) -> str:
        """Last-resort chat text so a turn never ends in silence."""
        tool_outputs: list[str] = []
        last_assistant = ""
        for m in messages:
            role = m.get("role")
            text = (m.get("content") or "").strip()
            if role == "tool" and text:
                tool_outputs.append(text[:1200])
            elif role == "assistant" and text and text not in {"(no output)"}:
                cleaned = _clean_final(text)
                if cleaned and not _looks_like_instructions(cleaned):
                    last_assistant = cleaned
        if tool_outputs:
            last = tool_outputs[-1]
            compact = re.sub(r"\s+", " ", last).strip()
            return f"Done. {compact[:400]}"
        if last_assistant:
            return _speech_excerpt(last_assistant, 600)
        return (
            "I wasn't able to finish that on the machine just then. "
            "Ask me again and I'll execute it directly."
        )

    async def _finish_interrupted(self, emit: EmitFn) -> None:
        msg = "Very well — I'll stop there."
        cid = self.ctx.conversation_id
        if cid:
            self.memory.add_message(cid, "assistant", msg)
        await emit({"type": "interrupted", "message": msg})
        await emit({"type": "status", "state": "idle"})
        await emit({"type": "agent_end"})

    async def _execute_tool(
        self,
        tc: Any,
        emit: EmitFn,
        confirm: ConfirmFn,
        control: RunControl | None,
    ):
        from .tools.base import ToolResult

        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        if (
            name == "device_control"
            and (args.get("action") or "").strip().lower() == "shell"
            and _scaffold_mismatches_request(self._last_user_text, args.get("command") or "")
        ):
            output = (
                "Refused: that command scaffolds a software project. The user asked to "
                "control hardware on this machine. Use peripherals (action=control, "
                "command=lighting|brightness|volume|connect) or a matching utility "
                "(openrgb, polychromatic-cli, razer-cli, brightnessctl, wpctl)."
            )
            await emit({
                "type": "tool_result",
                "id": tc.id,
                "name": name,
                "ok": False,
                "output": output,
            })
            return ToolResult(False, output)

        tool = self.tools.get(name)
        if tool is None:
            await emit({
                "type": "tool_result",
                "id": tc.id,
                "name": name,
                "ok": False,
                "output": f"Unknown tool: {name}",
            })
            return ToolResult(False, f"Error: unknown tool '{name}'.")

        dangerous = tool.is_dangerous(args)
        auto_approve = bool(self.config.permissions.auto_approve)
        await emit({
            "type": "tool_call",
            "id": tc.id,
            "name": name,
            "args": args,
            "dangerous": dangerous,
            "auto_approved": bool(dangerous and auto_approve),
        })

        if dangerous and not auto_approve:
            await emit({"type": "status", "state": "awaiting_confirmation"})
            self._check(control)
            preview = tool.make_preview(args)
            approved = await confirm({
                "id": tc.id,
                "name": name,
                "args": args,
                "preview": preview,
                "risk": _hard_gate_risk(name, args, preview),
            })
            self._check(control)
            if not approved:
                await emit({
                    "type": "tool_result",
                    "id": tc.id,
                    "name": name,
                    "ok": False,
                    "output": "Denied by user.",
                })
                return ToolResult(False, f"The user declined to run {name}.")
            await emit({"type": "status", "state": "running_tool"})
        elif dangerous and auto_approve:
            await emit({"type": "status", "state": "running_tool"})

        self._check(control)
        try:
            result = await tool.run(args, self.ctx)
        except Exception as exc:  # noqa: BLE001
            await emit({
                "type": "tool_result",
                "id": tc.id,
                "name": name,
                "ok": False,
                "output": f"Tool raised an error: {exc}",
            })
            return ToolResult(False, f"Error while running {name}: {exc}")

        await emit({
            "type": "tool_result",
            "id": tc.id,
            "name": name,
            "ok": result.ok,
            "output": result.output,
        })
        return result
