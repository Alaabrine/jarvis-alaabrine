"""Store learned facts about the device or user preferences for future recall."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult, prop


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    fact = (args.get("fact") or "").strip()
    if not fact:
        return ToolResult(False, "fact is required.")
    kind = (args.get("kind") or "device_control").strip()
    if kind not in {"device_control", "device", "long_term", "preference"}:
        kind = "device_control"
    embedding = await ctx.llm.embed(fact)
    ctx.memory.add_memory(kind, fact, embedding)
    return ToolResult(True, f"Remembered ({kind}): {fact[:500]}")


remember = Tool(
    name="remember",
    description=(
        "Save a fact you discovered about this device or the user's preferences "
        "(e.g. 'brightness is controlled via brightnessctl on /dev/intel_backlight', "
        "'user prefers Firefox over Chrome'). Recalled automatically in future conversations."
    ),
    parameters={
        "fact": prop("string", "The fact to remember."),
        "kind": prop(
            "string",
            "Memory kind: device_control (default), device, preference, or long_term.",
            optional=True,
        ),
    },
    run=_run,
)
