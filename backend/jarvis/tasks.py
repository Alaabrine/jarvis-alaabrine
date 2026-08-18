"""Background task manager: autonomous subagent runs and detached shell processes.

Tasks are owned by this manager (not the per-connection WebSocket run registry), so they
survive chat interrupts and disconnects. State is persisted in the SQLite `tasks` table and
progress is broadcast to every connected UI client as `task_update` / `task_log` events.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import store
from .memory import Memory

EventCb = Callable[[dict], Awaitable[None]]

_LOG_TEXT_CAP = 2000
_SHELL_FLUSH_LINES = 20
_SHELL_FLUSH_SECONDS = 1.0


@dataclass
class RunningTask:
    id: str
    kind: str  # 'agent' | 'shell'
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    proc: asyncio.subprocess.Process | None = None


class TaskManager:
    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self._running: dict[str, RunningTask] = {}
        self._emit_cb: EventCb | None = None

    # --- Wiring ---------------------------------------------------------
    def on_event(self, cb: EventCb) -> None:
        self._emit_cb = cb

    def active_count(self) -> int:
        return len(self._running)

    def fail_orphans(self) -> int:
        """Mark tasks left 'running' by a previous server process as failed."""
        return self.memory.fail_orphaned_tasks("Server restarted while the task was running.")

    # --- Queries ----------------------------------------------------------
    def get(self, task_id: str) -> dict[str, Any] | None:
        return self.memory.get_task(task_id)

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.memory.list_tasks(status, limit)

    def is_running(self, task_id: str) -> bool:
        return task_id in self._running

    # --- Control ----------------------------------------------------------
    def cancel(self, task_id: str) -> bool:
        rt = self._running.get(task_id)
        if rt is None:
            return False
        rt.cancel.set()
        if rt.proc is not None and rt.proc.returncode is None:
            try:
                rt.proc.kill()
            except ProcessLookupError:
                pass
        elif rt.kind == "agent" and rt.task is not None:
            rt.task.cancel()
        return True

    def delete(self, task_id: str) -> bool:
        if task_id in self._running:
            return False
        self.memory.delete_task(task_id)
        return True

    # --- Spawning -----------------------------------------------------------
    async def start_agent_task(
        self, goal: str, title: str | None = None, conversation_id: int | None = None
    ) -> str:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        title = (title or goal).strip()[:80] or "Background task"
        self.memory.create_task(task_id, "agent", title, goal, conversation_id)
        rt = RunningTask(id=task_id, kind="agent")
        rt.task = asyncio.create_task(self._run_agent_task(rt, goal, title, conversation_id))
        self._running[task_id] = rt
        await self._broadcast_update(task_id)
        return task_id

    async def start_shell_task(
        self, command: str, cwd: str | None = None, conversation_id: int | None = None
    ) -> str:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        title = command.strip()[:80] or "Shell task"
        self.memory.create_task(task_id, "shell", title, command, conversation_id)
        rt = RunningTask(id=task_id, kind="shell")
        rt.task = asyncio.create_task(self._run_shell_task(rt, command, cwd))
        self._running[task_id] = rt
        await self._broadcast_update(task_id)
        return task_id

    # --- Agent (subagent) runner --------------------------------------------
    async def _run_agent_task(
        self, rt: RunningTask, goal: str, title: str, conversation_id: int | None
    ) -> None:
        from .agent import Agent, AgentInterrupted

        agent = Agent(store.get(), self.memory, depth=1)

        async def emit(event: dict) -> None:
            entry = self._log_entry_from_event(event)
            if entry is not None:
                await self._log(rt.id, [entry])

        async def confirm(_call: dict) -> bool:
            # No user is attached to a background run: deny gated tools outright.
            return False

        try:
            report = await agent.run_task(goal, emit, confirm, cancel=rt.cancel)
        except (AgentInterrupted, asyncio.CancelledError):
            await self._finish(rt.id, "cancelled", "Cancelled by user.")
            return
        except Exception as exc:  # noqa: BLE001
            await self._finish(rt.id, "failed", f"Task failed: {exc}")
            return

        report = (report or "").strip() or "(The subagent produced no final report.)"
        if conversation_id is not None:
            self.memory.add_message(
                conversation_id,
                "assistant",
                f"[Background task completed: {title}]\n\n{report}",
            )
        await self._finish(rt.id, "completed", report)

    @staticmethod
    def _log_entry_from_event(event: dict) -> dict[str, Any] | None:
        etype = event.get("type")
        now = time.time()
        if etype == "thought_end":
            text = (event.get("content") or event.get("reasoning") or "").strip()
            if not text:
                return None
            return {"t": now, "type": "thought", "text": text[:_LOG_TEXT_CAP]}
        if etype == "tool_call":
            args = json.dumps(event.get("args") or {}, ensure_ascii=False)
            return {
                "t": now,
                "type": "tool_call",
                "text": f"{event.get('name')} {args}"[:_LOG_TEXT_CAP],
            }
        if etype == "tool_result":
            prefix = "ok" if event.get("ok") else "error"
            return {
                "t": now,
                "type": "tool_result",
                "text": f"[{prefix}] {event.get('output') or ''}"[:_LOG_TEXT_CAP],
            }
        if etype == "error":
            return {"t": now, "type": "error", "text": (event.get("message") or "")[:_LOG_TEXT_CAP]}
        if etype == "assistant":
            return {"t": now, "type": "report", "text": (event.get("text") or "")[:_LOG_TEXT_CAP]}
        return None

    # --- Shell runner ------------------------------------------------------
    async def _run_shell_task(self, rt: RunningTask, command: str, cwd: str | None) -> None:
        from .tools.shell import _scrub_secrets, _uses_sudo, _with_sudo_stdin

        cfg = store.get()
        password = (cfg.permissions.sudo_password or "").strip()
        display_cmd = command
        stdin_data: bytes | None = None

        if _uses_sudo(command):
            if not password:
                await self._finish(
                    rt.id,
                    "failed",
                    "This command needs sudo, but no sudo password is configured.",
                )
                return
            command = _with_sudo_stdin(command)
            stdin_data = (password + "\n").encode()

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd or None,
            )
        except OSError as exc:
            await self._finish(rt.id, "failed", f"Failed to start command: {exc}")
            return

        rt.proc = proc
        if stdin_data is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        buffer: list[dict[str, Any]] = []
        last_flush = time.monotonic()

        async def flush() -> None:
            nonlocal buffer, last_flush
            if buffer:
                await self._log(rt.id, buffer)
                buffer = []
            last_flush = time.monotonic()

        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = _scrub_secrets(line.decode(errors="replace").rstrip("\n"), password)
            buffer.append({"t": time.time(), "type": "output", "text": text[:_LOG_TEXT_CAP]})
            if len(buffer) >= _SHELL_FLUSH_LINES or time.monotonic() - last_flush >= _SHELL_FLUSH_SECONDS:
                await flush()

        await proc.wait()
        await flush()

        if rt.cancel.is_set():
            await self._finish(rt.id, "cancelled", f"Killed by user: {display_cmd}")
        elif proc.returncode == 0:
            await self._finish(rt.id, "completed", f"Command finished successfully (exit 0): {display_cmd}")
        else:
            await self._finish(
                rt.id, "failed", f"Command exited with code {proc.returncode}: {display_cmd}"
            )

    # --- Internals ------------------------------------------------------------
    async def _log(self, task_id: str, entries: list[dict[str, Any]]) -> None:
        self.memory.append_task_log(task_id, entries)
        await self._broadcast({"type": "task_log", "task_id": task_id, "entries": entries})

    async def _finish(self, task_id: str, status: str, result: str) -> None:
        self.memory.update_task(task_id, status=status, result=result, finished=True)
        self._running.pop(task_id, None)
        snapshot = self.memory.get_task(task_id)
        await self._broadcast({"type": "task_update", "task": snapshot})
        if status == "completed":
            await self._broadcast({"type": "task_completed", "task": snapshot})

    async def _broadcast_update(self, task_id: str) -> None:
        await self._broadcast({"type": "task_update", "task": self.memory.get_task(task_id)})

    async def _broadcast(self, event: dict) -> None:
        if self._emit_cb is None:
            return
        try:
            await self._emit_cb(event)
        except Exception:  # noqa: BLE001
            pass


_manager: TaskManager | None = None


def init_manager(memory: Memory) -> TaskManager:
    global _manager
    _manager = TaskManager(memory)
    return _manager


def get_manager() -> TaskManager | None:
    return _manager
