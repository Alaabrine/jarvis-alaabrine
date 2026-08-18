"""Detect, inspect, connect, and control peripherals (USB, Bluetooth, audio, displays, …)."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult, prop

_SAFE_ACTIONS = {"list", "scan", "inspect", "remember"}
_SAFE_COMMANDS = {
    "volume",
    "mute",
    "unmute",
    "play",
    "pause",
    "play-pause",
    "next",
    "previous",
    "stop",
    "brightness",
}


def _dangerous(args: dict) -> bool:
    action = (args.get("action") or "").strip().lower()
    if action in _SAFE_ACTIONS:
        return False
    if action in {"connect", "disconnect"}:
        # Bluetooth connect to a known device / volume-like; pairing & Wi-Fi secrets are gated.
        return False
    if action == "control":
        cmd = (args.get("command") or "").strip().lower()
        return cmd not in _SAFE_COMMANDS
    return True


def _preview(args: dict) -> str:
    action = args.get("action", "?")
    target = args.get("target") or args.get("id") or ""
    command = args.get("command") or ""
    value = args.get("value") or ""
    bits = [action]
    if target:
        bits.append(target)
    if command:
        bits.append(command)
    if value and action != "connect":
        bits.append(value)
    return "peripherals: " + " ".join(str(b) for b in bits)


def _learner():
    from ..peripherals import get_learner

    return get_learner()


def _format_one(p: dict) -> str:
    flags = []
    if p.get("connected"):
        flags.append("connected")
    if p.get("paired"):
        flags.append("paired")
    if p.get("trusted"):
        flags.append("trusted")
    if not p.get("available", True):
        flags.append("offline")
    flag_s = ", ".join(flags) or "present"
    lines = [
        f"{p.get('name')}  [{p.get('kind')}]  id={p.get('id')}  ({flag_s})",
    ]
    if p.get("address"):
        lines.append(f"  address: {p['address']}")
    if p.get("vendor") or p.get("product"):
        lines.append(f"  vendor/product: {p.get('vendor') or ''} {p.get('product') or ''}".rstrip())
    hints = p.get("control_hints") or []
    if hints:
        lines.append("  control:")
        for h in hints[:6]:
            lines.append(f"    • {h}")
    facts = p.get("facts") or []
    if facts:
        lines.append("  learned:")
        for f in facts[-8:]:
            lines.append(f"    – {f}")
    return "\n".join(lines)


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    learner = _learner()
    if learner is None:
        return ToolResult(False, "Peripheral learner is not running.")

    action = (args.get("action") or "").strip().lower()
    target = (args.get("target") or args.get("id") or args.get("name") or "").strip()
    kind = (args.get("kind") or "").strip().lower()

    if action in {"", "list"}:
        items = learner.items or ctx.memory.list_peripherals(kind or None)
        if kind:
            items = [p for p in items if p.get("kind") == kind]
        if not items:
            return ToolResult(
                True,
                "No peripherals recorded yet. Call peripherals with action=scan to inventory USB, "
                "Bluetooth, audio, displays, cameras, printers, storage, and nearby Wi-Fi/mDNS.",
            )
        from ..peripherals import format_inventory

        return ToolResult(True, format_inventory(items))

    if action == "scan":
        discover = bool(args.get("discover"))
        items = await learner.scan(llm=ctx.llm, reason="tool", discover=discover)
        if kind:
            items = [p for p in items if p.get("kind") == kind]
        from ..peripherals import format_inventory

        note = " (including Bluetooth inquiry)" if discover else ""
        return ToolResult(True, f"Scan complete{note}.\n" + format_inventory(items))

    if action == "inspect":
        if not target:
            return ToolResult(False, "inspect requires target (id, MAC, or name).")
        p = await learner.inspect(target, llm=ctx.llm)
        if p is None:
            return ToolResult(False, f"No peripheral matching '{target}'. Try action=scan first.")
        return ToolResult(True, "Inspected:\n" + _format_one(p))

    if action == "remember":
        fact = (args.get("fact") or args.get("value") or "").strip()
        if not target or not fact:
            return ToolResult(False, "remember requires target and fact (or value).")
        p = await learner.remember_fact(target, fact, llm=ctx.llm)
        if p is None:
            return ToolResult(False, f"No peripheral matching '{target}'.")
        return ToolResult(True, f"Remembered about {p.get('name')}: {fact}")

    if action in {"connect", "disconnect", "pair", "unpair", "trust"}:
        if not target:
            return ToolResult(False, f"{action} requires target (id, MAC, or name).")
        ok, output, p = await learner.act(
            target,
            action,
            password=(args.get("password") or "").strip(),
        )
        name = (p or {}).get("name") or target
        status = "succeeded" if ok else "failed"
        return ToolResult(ok, f"{action} {name}: {status}\n{output}")

    if action == "control":
        if not target:
            return ToolResult(False, "control requires target.")
        command = (args.get("command") or "").strip()
        if not command:
            return ToolResult(
                False,
                "control requires command (volume, mute, brightness, default, mount, "
                "unmount, play, enable, …).",
            )
        ok, output, p = await learner.act(
            target,
            command,
            value=(args.get("value") or "").strip(),
            password=(args.get("password") or "").strip(),
        )
        name = (p or {}).get("name") or target
        status = "succeeded" if ok else "failed"
        return ToolResult(ok, f"control {command} on {name}: {status}\n{output}")

    return ToolResult(
        False,
        f"Unknown action '{action}'. Use: list, scan, inspect, connect, disconnect, "
        "pair, unpair, control, remember.",
    )


peripherals = Tool(
    name="peripherals",
    description=(
        "Detect, learn about, connect to, and control peripherals: USB devices, Bluetooth "
        "(headphones, keyboards, mice), audio sinks/sources, displays, cameras, printers, "
        "removable storage, Wi-Fi networks, and LAN/mDNS neighbours. "
        "Actions: list (inventory), scan (refresh; set discover=true to inquire nearby Bluetooth), "
        "inspect (deep-learn one device and remember facts), "
        "connect / disconnect / pair / unpair (Bluetooth, Wi-Fi, storage, network), "
        "control (volume, mute, brightness, default, mount, play, enable, …), "
        "remember (attach a fact to a device). "
        "Match target by id, MAC, or name. Prefer this over inventing bluetoothctl/nmcli/pactl "
        "commands — fall back to device_control shell only if a control hint is missing."
    ),
    parameters={
        "action": prop(
            "string",
            "One of: list, scan, inspect, connect, disconnect, pair, unpair, control, remember.",
        ),
        "target": prop(
            "string",
            "Peripheral id, MAC address, SSID, or name (fuzzy). Required except list/scan.",
            optional=True,
        ),
        "id": prop("string", "Alias for target.", optional=True),
        "name": prop("string", "Alias for target.", optional=True),
        "kind": prop(
            "string",
            "Filter list/scan: bluetooth, usb, audio, display, hid, camera, printer, "
            "storage, wifi, network, radio.",
            optional=True,
        ),
        "discover": prop(
            "boolean",
            "For scan: run a live Bluetooth inquiry for nearby unpaired devices (slower).",
            optional=True,
        ),
        "command": prop(
            "string",
            "For control: volume, mute, unmute, brightness, default, mount, unmount, "
            "play, pause, next, previous, enable, disable, print, power.",
            optional=True,
        ),
        "value": prop(
            "string",
            "Control argument (e.g. 50% for volume, file path for print) or a fact to remember.",
            optional=True,
        ),
        "password": prop(
            "string",
            "Wi-Fi password when connecting to a new network. Never echoed back.",
            optional=True,
        ),
        "fact": prop("string", "Fact to remember about the target (action=remember).", optional=True),
    },
    run=_run,
    dangerous=_dangerous,
    preview=_preview,
)
