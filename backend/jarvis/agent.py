"""The JARVIS agent loop: streaming tool-calling over an emit/confirm interface."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable

from .config import Config
from .device import get_learner
from .llm import Interrupted, LLMClient, LLMError
from .memory import Memory
from .persona import system_prompt
from .tools import ToolContext, openai_schemas, registry

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]
ConfirmFn = Callable[[dict[str, Any]], Awaitable[bool]]

MAX_ITERATIONS = 12
SUBAGENT_MAX_ITERATIONS = 24

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

    def _check(self, cancel: asyncio.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise AgentInterrupted()

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
        context_parts = list(recalled)
        learner = get_learner()
        device_ctx = learner.get_context() if learner else ""
        if device_ctx:
            context_parts.insert(0, device_ctx)
        else:
            for d in devices[:3]:
                context_parts.append(f"Known device: {d['name']} ({d['profile'].get('os','?')})")
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
        cancel: asyncio.Event | None,
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
            self._check(cancel)
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
                cancel=cancel,
            )
        except Interrupted:
            await emit({"type": "thought_end", "id": thought_id})
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

        self.ctx.conversation_id = conversation_id
        self.memory.add_message(conversation_id, "user", display_text, stored_meta or None)
        multimodal = build_user_content(display_text, vision_parts)

        await emit({"type": "agent_start"})
        await emit({"type": "status", "state": "thinking"})

        vision_report = ""
        try:
            if vision_parts:
                self._check(cancel)
                vision_report = await self._vision_scan(vision_parts, display_text, emit, cancel)
                # Ground the agent loop in the fresh scan (tools + some backends drop images).
                grounded = display_text
                if vision_report:
                    grounded = (
                        f"{display_text}\n\n"
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
            self._check(cancel)
        except AgentInterrupted:
            await self._finish_interrupted(emit)
            return
        except Exception as exc:  # noqa: BLE001
            await emit({"type": "error", "message": f"Failed to prepare context: {exc}"})
            await emit({"type": "agent_end"})
            return

        final_text = ""

        try:
            delegation_guard = _wants_delegation(display_text) or self._conversation_wants_delegation(
                conversation_id
            )
            final_text = await self._tool_loop(
                messages,
                emit,
                confirm,
                cancel,
                MAX_ITERATIONS,
                delegation_guard=delegation_guard,
            )
        except AgentInterrupted:
            await self._finish_interrupted(emit)
            return
        except asyncio.CancelledError:
            await self._finish_interrupted(emit)
            return

        if final_text:
            self.memory.add_message(conversation_id, "assistant", final_text)
            summary = f"User asked: {display_text[:200]} | JARVIS answered: {final_text[:300]}"
            embedding = await self.llm.embed(summary)
            self.memory.add_memory("conversation", summary, embedding)

        await emit({"type": "status", "state": "idle"})
        await emit({"type": "agent_end"})

    async def run_task(
        self,
        goal: str,
        emit: EmitFn,
        confirm: ConfirmFn,
        cancel: asyncio.Event | None = None,
    ) -> str:
        """Run an autonomous background subagent for a single goal.

        Uses a synthetic context (no conversation history) and returns the final report.
        Raises AgentInterrupted when cancelled.
        """
        system = system_prompt(self.config.user_name, "", self.config) + SUBAGENT_ADDENDUM
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": goal},
        ]
        return await self._tool_loop(messages, emit, confirm, cancel, SUBAGENT_MAX_ITERATIONS)

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

    async def _tool_loop(
        self,
        messages: list[dict],
        emit: EmitFn,
        confirm: ConfirmFn,
        cancel: asyncio.Event | None,
        max_iterations: int,
        delegation_guard: bool = False,
    ) -> str:
        """The LLM <-> tool iteration loop. Returns the final assistant text.

        When ``delegation_guard`` is set (user explicitly asked for background/subagent
        work), the loop refuses to finalize with a plain-text answer until ``start_task``
        has actually been called at least once — forcing the call if the model only
        narrates its intent. This is the common failure mode with local tool-calling models.
        """
        schemas = openai_schemas()
        final_text = ""
        empty_rounds = 0
        llm_failed = False
        started_task = False
        force_start_task = False
        delegation_pushes = 0

        for _ in range(max_iterations):
            self._check(cancel)
            current_thought = f"t-{uuid.uuid4().hex}"
            await emit({"type": "thought_start", "id": current_thought})

            async def on_delta(delta: dict[str, Any], tid: str = current_thought) -> None:
                self._check(cancel)
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
                else None
            )
            force_start_task = False
            try:
                message = await self._chat_with_optional_force(
                    messages, schemas, on_delta, cancel, tool_choice
                )
            except Interrupted:
                await emit({"type": "thought_end", "id": current_thought})
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
            self._check(cancel)

            if not tool_calls:
                if content:
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
                            final_text = notice
                            await emit({"type": "assistant", "text": notice})
                            break
                    final_text = content
                    await emit({"type": "assistant", "text": content})
                    break
                # Reasoning-only / empty reply (common with local <think> models).
                # Ending here would silently kill the run — nudge the model to act instead.
                empty_rounds += 1
                if empty_rounds > 2:
                    if delegation_guard and not started_task:
                        notice = await self._force_spawn_subagent(messages, emit)
                        if notice:
                            started_task = True
                            final_text = notice
                            await emit({"type": "assistant", "text": notice})
                    break
                messages.append({"role": "assistant", "content": reasoning[:2000] or "(no output)"})
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
                await emit({"type": "status", "state": "thinking"})
                continue

            empty_rounds = 0
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

            for tc in tool_calls:
                self._check(cancel)
                if tc.function.name == "start_task":
                    started_task = True
                result_text = await self._execute_tool(tc, emit, confirm, cancel)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                )

            await emit({"type": "status", "state": "thinking"})

        if delegation_guard and not started_task:
            notice = await self._force_spawn_subagent(messages, emit)
            if notice:
                started_task = True
                final_text = notice
                await emit({"type": "assistant", "text": notice})

        if not final_text and not llm_failed:
            final_text = await self._wrap_up(messages, emit, cancel)

        return final_text

    async def _chat_with_optional_force(
        self,
        messages: list[dict],
        schemas: list[dict],
        on_delta: Any,
        cancel: asyncio.Event | None,
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
        cancel: asyncio.Event | None,
    ) -> str:
        """Force a final tool-free answer when the loop ended without one.

        Reached when the iteration budget is exhausted or the model kept returning
        empty replies; without this the run would end with no message at all.
        """
        self._check(cancel)
        messages.append({
            "role": "user",
            "content": (
                "[system] Stop working now. Summarise for the user, in plain text, what you "
                "have done so far, what you found, and what remains unfinished (mention any "
                "background task ids you started). Do not call any tools."
            ),
        })
        try:
            message = await self.llm.chat(messages, tools=None, cancel=cancel)
        except Interrupted:
            raise AgentInterrupted()
        except LLMError as exc:
            await emit({"type": "error", "message": str(exc)})
            return ""
        text = (message.content or "").strip()
        if text:
            await emit({"type": "assistant", "text": text})
        return text

    async def _finish_interrupted(self, emit: EmitFn) -> None:
        await emit({"type": "interrupted", "message": "Interrupted."})
        await emit({"type": "status", "state": "idle"})
        await emit({"type": "agent_end"})

    async def _execute_tool(
        self,
        tc: Any,
        emit: EmitFn,
        confirm: ConfirmFn,
        cancel: asyncio.Event | None,
    ) -> str:
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        tool = self.tools.get(name)
        if tool is None:
            await emit({
                "type": "tool_result",
                "id": tc.id,
                "name": name,
                "ok": False,
                "output": f"Unknown tool: {name}",
            })
            return f"Error: unknown tool '{name}'."

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
            self._check(cancel)
            approved = await confirm({
                "id": tc.id,
                "name": name,
                "args": args,
                "preview": tool.make_preview(args),
            })
            self._check(cancel)
            if not approved:
                await emit({
                    "type": "tool_result",
                    "id": tc.id,
                    "name": name,
                    "ok": False,
                    "output": "Denied by user.",
                })
                return f"The user declined to run {name}."
            await emit({"type": "status", "state": "running_tool"})
        elif dangerous and auto_approve:
            await emit({"type": "status", "state": "running_tool"})

        self._check(cancel)
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
            return f"Error while running {name}: {exc}"

        await emit({
            "type": "tool_result",
            "id": tc.id,
            "name": name,
            "ok": result.ok,
            "output": result.output,
        })
        return result.output
