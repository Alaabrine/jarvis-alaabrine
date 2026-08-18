"""Tools for spawning and managing background tasks (subagents and detached processes)."""

from __future__ import annotations

import time

from .base import Tool, ToolContext, ToolResult, prop
from .shell import _is_dangerous as _shell_is_dangerous

_LOG_TAIL = 15


def _fmt_age(created_at: float) -> str:
    secs = max(0, int(time.time() - created_at))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _fmt_task_line(t: dict) -> str:
    return (
        f"- {t['id']} [{t['status']}] ({t['kind']}, {_fmt_age(t['created_at'])} ago): "
        f"{t['title']}"
    )


async def _start_task(args: dict, ctx: ToolContext) -> ToolResult:
    if ctx.tasks is None:
        return ToolResult(False, "The background task manager is not available.")
    if ctx.depth >= 1:
        return ToolResult(False, "Subagents may not spawn further subagents. Do the work directly.")
    goal = (args.get("goal") or "").strip()
    if not goal:
        return ToolResult(False, "A non-empty goal is required.")
    title = (args.get("title") or "").strip() or None
    task_id = await ctx.tasks.start_agent_task(goal, title, ctx.conversation_id)
    return ToolResult(
        True,
        f"Background subagent started: {task_id}. It runs independently of this conversation; "
        f"its final report will be posted here automatically when it finishes. "
        f"Tell the user it is underway. Use check_task('{task_id}') to inspect progress, "
        f"or cancel_task('{task_id}') to stop it.",
    )


async def _run_background_shell(args: dict, ctx: ToolContext) -> ToolResult:
    if ctx.tasks is None:
        return ToolResult(False, "The background task manager is not available.")
    command = (args.get("command") or "").strip()
    if not command:
        return ToolResult(False, "A non-empty command is required.")
    cwd = args.get("cwd") or None
    task_id = await ctx.tasks.start_shell_task(command, cwd, ctx.conversation_id)
    return ToolResult(
        True,
        f"Background process started: {task_id} ($ {command}). It keeps running with no timeout; "
        f"output is captured in the task log. Use check_task('{task_id}') for status and output, "
        f"or cancel_task('{task_id}') to kill it.",
    )


async def _check_task(args: dict, ctx: ToolContext) -> ToolResult:
    if ctx.tasks is None:
        return ToolResult(False, "The background task manager is not available.")
    task_id = (args.get("task_id") or "").strip()
    task = ctx.tasks.get(task_id)
    if task is None:
        return ToolResult(False, f"No task found with id '{task_id}'.")
    lines = [
        f"Task {task['id']} ({task['kind']}): {task['title']}",
        f"Status: {task['status']} (started {_fmt_age(task['created_at'])} ago)",
    ]
    if task.get("result"):
        lines.append(f"Result: {task['result']}")
    log = task.get("log") or []
    if log:
        lines.append(f"Log (last {min(len(log), _LOG_TAIL)} of {len(log)} entries):")
        for entry in log[-_LOG_TAIL:]:
            lines.append(f"  [{entry.get('type', '?')}] {entry.get('text', '')}")
    return ToolResult(True, "\n".join(lines))


async def _list_tasks(args: dict, ctx: ToolContext) -> ToolResult:
    if ctx.tasks is None:
        return ToolResult(False, "The background task manager is not available.")
    status = (args.get("status") or "").strip() or None
    tasks = ctx.tasks.list_tasks(status)
    if not tasks:
        return ToolResult(True, "No background tasks" + (f" with status '{status}'." if status else "."))
    return ToolResult(True, "\n".join(_fmt_task_line(t) for t in tasks))


async def _cancel_task(args: dict, ctx: ToolContext) -> ToolResult:
    if ctx.tasks is None:
        return ToolResult(False, "The background task manager is not available.")
    task_id = (args.get("task_id") or "").strip()
    if ctx.tasks.cancel(task_id):
        return ToolResult(True, f"Cancellation requested for {task_id}.")
    task = ctx.tasks.get(task_id)
    if task is None:
        return ToolResult(False, f"No task found with id '{task_id}'.")
    return ToolResult(False, f"Task {task_id} is not running (status: {task['status']}).")


start_task = Tool(
    name="start_task",
    description=(
        "Spawn an autonomous background subagent to work on a long-running goal (multi-step "
        "research, large analyses, anything likely to take more than a minute). It runs "
        "independently with its own tool access and posts a final report into this conversation "
        "when done, so you can keep chatting meanwhile. Returns a task id immediately."
    ),
    parameters={
        "goal": prop("string", "Complete, self-contained instructions for the subagent."),
        "title": prop("string", "Short human-readable title for the task.", optional=True),
    },
    run=_start_task,
)

run_background_shell = Tool(
    name="run_background_shell",
    description=(
        "Run a shell command as a detached background process with no timeout (builds, "
        "downloads, servers, long jobs). Output is captured to the task log. Returns a task id "
        "immediately; use check_task for output and cancel_task to kill it."
    ),
    parameters={
        "command": prop("string", "The shell command to execute in the background."),
        "cwd": prop("string", "Working directory (optional).", optional=True),
    },
    run=_run_background_shell,
    dangerous=_shell_is_dangerous,
    preview=lambda a: f"Run in background: {a.get('command', '')}",
)

check_task = Tool(
    name="check_task",
    description="Check a background task: status, result, and the tail of its log.",
    parameters={
        "task_id": prop("string", "The id of the task to inspect."),
    },
    run=_check_task,
)

list_tasks = Tool(
    name="list_tasks",
    description="List recent background tasks and their statuses.",
    parameters={
        "status": prop(
            "string",
            "Filter by status: running, completed, failed, or cancelled (optional).",
            optional=True,
        ),
    },
    run=_list_tasks,
)

cancel_task = Tool(
    name="cancel_task",
    description="Cancel a running background task (stops the subagent or kills the process).",
    parameters={
        "task_id": prop("string", "The id of the task to cancel."),
    },
    run=_cancel_task,
)
