"""Store durable facts about the device or the user's preferences for future recall."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult, prop

#: Durable kinds only. Nothing stored here is scoped to a conversation, so a task
#: brief saved as a "fact" would be recalled into unrelated future chats.
KINDS = ("device_control", "device", "peripheral", "preference", "long_term")


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    fact = (args.get("fact") or "").strip()
    if not fact:
        return ToolResult(False, "fact is required.")
    kind = (args.get("kind") or "device_control").strip()
    if kind not in KINDS:
        kind = "device_control"
    embedding = await ctx.llm.embed(fact)
    ctx.memory.add_memory(kind, fact, embedding)
    return ToolResult(True, f"Remembered ({kind}): {fact[:500]}")


remember = Tool(
    name="remember",
    description=(
        "Save something durable: a stable preference of the user, or a fact about this "
        "device or a peripheral that will still be true next month. Examples: "
        "'brightness is controlled via brightnessctl on /dev/intel_backlight', "
        "'Sony WH-1000XM4 connect via bluetoothctl', 'user prefers Firefox over Chrome'. "
        "These notes are recalled in every later conversation, so store only standing "
        "facts — never the current task, a pending step, a to-do, a one-off command, or "
        "anything phrased as a request. Write it as a statement of fact, not an "
        "instruction. If the user says 'remember to …', that is a task: use start_task or "
        "simply do it, do not store it here."
    ),
    parameters={
        "fact": prop("string", "The durable fact or preference, phrased as a statement."),
        "kind": prop(
            "string",
            "Memory kind: device_control (default), device, peripheral, preference, or long_term.",
            optional=True,
        ),
    },
    run=_run,
)
