"""Detect, inspect, connect, and control peripherals (USB, Bluetooth, audio, displays, …)."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult, prop

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


async def _connect_autonomously(
    learner,
    target: str,
    password: str,
    ctx: ToolContext,
) -> ToolResult:
    """Get a device connected and actually in use, without step-by-step instructions.

    Discovers the device if it is not in the inventory yet, pairs and trusts Bluetooth
    devices that have never been paired, connects, and routes audio to a headset once
    it is up — so "connect my headphones" is one call, not four.
    """
    steps: list[str] = []
    p = learner.resolve(target)

    if p is None:
        steps.append("not in inventory — scanning, including a Bluetooth inquiry")
        await learner.scan(llm=ctx.llm, reason="connect-discovery", discover=True)
        p = learner.resolve(target)
    if p is None:
        items = learner.items or ctx.memory.list_peripherals()
        names = ", ".join(str(i.get("name")) for i in items[:12]) or "none"
        return ToolResult(
            False,
            f"No peripheral matching '{target}' even after a discovery scan. "
            f"Known devices: {names}",
        )

    name = p.get("name") or target
    if p.get("connected"):
        steps.append("already connected")

    # A Bluetooth device that was never paired cannot simply be connected.
    if p.get("kind") == "bluetooth" and not p.get("paired"):
        ok, output, p2 = await learner.act(p["id"], "pair", password=password)
        steps.append(f"pair: {'ok' if ok else 'failed'}")
        if not ok:
            return ToolResult(False, f"Could not pair {name}.\n" + output + "\nSteps: " + "; ".join(steps))
        p = p2 or p
        ok_trust, _, p3 = await learner.act(p["id"], "trust")
        steps.append(f"trust: {'ok' if ok_trust else 'skipped'}")
        p = p3 or p

    ok, output, p = await learner.act(p["id"], "connect", password=password)
    steps.append(f"connect: {'ok' if ok else 'failed'}")
    if not ok:
        return ToolResult(False, f"Could not connect {name}.\n{output}\nSteps: " + "; ".join(steps))

    # Connecting a headset is only useful if audio actually goes there.
    p = learner.resolve(p.get("id") or target) or p
    audio_words = ("headphone", "headset", "earbud", "speaker", "airpod", "buds")
    if any(w in (p.get("name") or "").lower() for w in audio_words):
        sink = learner.resolve(p.get("name") or target)
        for candidate in (sink, p):
            if candidate and candidate.get("kind") == "audio":
                ok_def, _, _ = await learner.act(candidate["id"], "default")
                steps.append(f"set as default audio output: {'ok' if ok_def else 'failed'}")
                break

    return ToolResult(True, f"{name} is connected.\nSteps: " + "; ".join(steps) + f"\n{output}".rstrip())


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

    if action in {"connect", "ensure", "use"}:
        if not target:
            return ToolResult(False, "connect requires target (id, MAC, name, or SSID).")
        return await _connect_autonomously(
            learner,
            target,
            (args.get("password") or "").strip(),
            ctx,
        )

    if action in {"disconnect", "pair", "unpair", "trust"}:
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
        command = (args.get("command") or "").strip()
        if not target:
            cmd = command.lower()
            if cmd in {
                "lighting", "light", "lights", "rgb", "led", "backlight",
                "rainbow", "spectrum", "wave", "brightness",
            }:
                target = "keyboard"
            else:
                return ToolResult(False, "control requires target.")
        if not command:
            return ToolResult(
                False,
                "control requires command (volume, mute, brightness, lighting, rainbow, "
                "default, mount, unmount, play, enable, …).",
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
        "connect (autonomous: discovers the device if unknown, pairs and trusts it if "
        "never paired, connects, and routes audio to it if it is a headset — one call, "
        "no need to list or scan first), "
        "disconnect / pair / unpair (Bluetooth, Wi-Fi, storage, network), "
        "control (volume, mute, brightness, lighting/rgb/rainbow/backlight, default, "
        "mount, play, enable, …), "
        "remember (attach a fact to a device). "
        "For keyboard lights use action=control command=lighting value=rainbow|spectrum|"
        "wave|static|off|<percent> (target the keyboard or a lighting device). "
        "Match target by id, MAC, or name. Prefer this over inventing bluetoothctl/nmcli/pactl "
        "commands — fall back to device_control shell only if a control hint is missing. "
        "Call this tool to act on hardware; do not describe commands for the user to run."
    ),
    parameters={
        "action": prop(
            "string",
            "One of: list, scan, inspect, connect, disconnect, pair, unpair, control, "
            "remember.",
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
            "For control: volume, mute, unmute, brightness, lighting, rainbow, rgb, "
            "backlight, default, mount, unmount, play, pause, next, previous, enable, "
            "disable, print, power.",
            optional=True,
        ),
        "value": prop(
            "string",
            "Control argument (e.g. 50% for volume, rainbow/spectrum/off for lighting, "
            "file path for print) or a fact to remember.",
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
    preview=_preview,
)
