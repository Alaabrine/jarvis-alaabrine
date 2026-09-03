"""Agent-loop and memory-isolation tests.

Stdlib only (``python -m unittest discover backend/tests``) so they run without adding
a test dependency. Every test points JARVIS_DATA_DIR at a temp directory before the
package is imported — importing jarvis.config creates ~/.jarvis otherwise.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="jarvis-test-")
os.environ["JARVIS_DATA_DIR"] = _TMP

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.agent import Agent, RunControl  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.tools.base import Tool, ToolResult, prop  # noqa: E402


class FakeMessage:
    def __init__(self, content="", tool_calls=None, reasoning=""):
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning = reasoning


class FakeToolCall:
    def __init__(self, name, arguments="{}", call_id="call-1"):
        self.id = call_id
        self.function = type("Fn", (), {"name": name, "arguments": arguments})()


class FakeLLM:
    """Replays queued replies and records how it was called."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], "kwargs": kwargs})
        if not self.replies:
            return FakeMessage(content="(exhausted)")
        return self.replies.pop(0)

    async def embed(self, text):
        return None


def _tool(name="probe", *, dangerous=False, output="ran"):
    calls: list[dict] = []

    async def _run(args, ctx):
        calls.append(args)
        return ToolResult(True, output)

    tool = Tool(
        name=name,
        description="test tool",
        parameters={"x": prop("string", "anything", optional=True)},
        run=_run,
        dangerous=dangerous,
    )
    return tool, calls


class Harness:
    """Collects emitted events and answers confirmation prompts."""

    def __init__(self, approve=True):
        self.events: list[dict] = []
        self.approve = approve
        self.confirms: list[dict] = []

    async def emit(self, event):
        self.events.append(event)

    async def confirm(self, payload):
        self.confirms.append(payload)
        return self.approve

    def texts(self, kind):
        return [e.get("text") for e in self.events if e.get("type") == kind]


def _agent(llm, tools):
    memory = Memory(Path(_TMP) / f"agent-{len(tools)}-{id(llm)}.db")
    agent = Agent(Config(), memory)
    agent.llm = llm
    agent.ctx.llm = llm
    agent.tools = {t.name: t for t in tools}
    return agent


def _run_loop(agent, harness, messages=None, budget=8):
    return asyncio.run(
        agent._tool_loop(
            messages or [{"role": "user", "content": "do the thing"}],
            harness.emit,
            harness.confirm,
            RunControl(),
            budget,
            None,
        )
    )


class ThinLoopTests(unittest.TestCase):
    def test_text_reply_ends_the_turn(self):
        llm = FakeLLM([FakeMessage(content="Very good, Sir. It is 12 degrees.")])
        tool, _ = _tool()
        agent = _agent(llm, [tool])
        h = Harness()

        final = _run_loop(agent, h)

        self.assertEqual(final, "Very good, Sir. It is 12 degrees.")
        self.assertEqual(len(llm.calls), 1, "no extra round trips for a plain answer")
        self.assertNotIn("tool_choice", llm.calls[0]["kwargs"])

    def test_tool_call_then_text(self):
        llm = FakeLLM([
            FakeMessage(content="Checking now.", tool_calls=[FakeToolCall("probe")]),
            FakeMessage(content="Done — the probe reports 12 degrees."),
        ])
        tool, tool_calls = _tool()
        agent = _agent(llm, [tool])
        h = Harness()

        final = _run_loop(agent, h)

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(final, "Done — the probe reports 12 degrees.")
        self.assertIn("Checking now.", h.texts("say"))
        results = [e for e in h.events if e["type"] == "tool_result"]
        self.assertEqual([r["ok"] for r in results], [True])

    def test_no_system_nudges_are_appended(self):
        """A reply with a pasted command and no tool call is left alone by default."""
        llm = FakeLLM([FakeMessage(content="brightnessctl set 30%")])
        tool, tool_calls = _tool(name="device_control")
        agent = _agent(llm, [tool])
        h = Harness()

        final = _run_loop(agent, h)

        self.assertEqual(final, "brightnessctl set 30%")
        self.assertEqual(tool_calls, [], "trust_model must not run the pasted command")
        appended = [
            m
            for call in llm.calls
            for m in call["messages"]
            if "[system]" in str(m.get("content") or "")
        ]
        self.assertEqual(appended, [], "no [system] nudges under trust_model")

    def test_strict_tools_runs_a_pasted_command(self):
        llm = FakeLLM([
            FakeMessage(content="brightnessctl set 30%"),
            FakeMessage(content="Dimmed the screen."),
        ])
        tool, tool_calls = _tool(name="device_control")
        agent = _agent(llm, [tool])
        agent.config.agent.strict_tools = True
        h = Harness()

        final = _run_loop(agent, h)

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["command"], "brightnessctl set 30%")
        self.assertEqual(final, "Dimmed the screen.")

    def test_strict_tools_recovers_narrated_tool_call(self):
        llm = FakeLLM([
            FakeMessage(content="device_control action=open url=https://example.com"),
            FakeMessage(content="Opened it."),
        ])
        tool, tool_calls = _tool(name="device_control")
        agent = _agent(llm, [tool])
        agent.config.agent.strict_tools = True
        h = Harness()

        _run_loop(agent, h)

        self.assertEqual(tool_calls, [{"action": "open", "url": "https://example.com"}])

    def test_dangerous_tool_waits_for_confirmation(self):
        llm = FakeLLM([
            FakeMessage(tool_calls=[FakeToolCall("wipe")]),
            FakeMessage(content="I stopped there."),
        ])
        tool, tool_calls = _tool(name="wipe", dangerous=True)
        agent = _agent(llm, [tool])
        h = Harness(approve=False)

        _run_loop(agent, h)

        self.assertEqual(len(h.confirms), 1)
        self.assertEqual(tool_calls, [], "denied call must not run")
        tool_msgs = [
            m
            for call in llm.calls
            for m in call["messages"]
            if m.get("role") == "tool"
        ]
        self.assertTrue(any("declined" in (m["content"] or "") for m in tool_msgs))

    def test_dangerous_tool_runs_when_auto_approved(self):
        llm = FakeLLM([
            FakeMessage(tool_calls=[FakeToolCall("wipe")]),
            FakeMessage(content="Removed."),
        ])
        tool, tool_calls = _tool(name="wipe", dangerous=True)
        agent = _agent(llm, [tool])
        agent.config.permissions.auto_approve = True
        h = Harness()

        _run_loop(agent, h)

        self.assertEqual(len(h.confirms), 0)
        self.assertEqual(len(tool_calls), 1)

    def test_unknown_tool_reports_an_error(self):
        llm = FakeLLM([
            FakeMessage(tool_calls=[FakeToolCall("nope")]),
            FakeMessage(content="That tool does not exist."),
        ])
        tool, _ = _tool()
        agent = _agent(llm, [tool])
        h = Harness()

        _run_loop(agent, h)

        results = [e for e in h.events if e["type"] == "tool_result"]
        self.assertEqual(results[0]["ok"], False)
        self.assertIn("Unknown tool", results[0]["output"])

    def test_budget_exhaustion_wraps_up_without_tools(self):
        # Always asks for a tool; the loop must stop and force a tool-free summary.
        llm = FakeLLM([FakeMessage(tool_calls=[FakeToolCall("probe")]) for _ in range(3)])
        llm.replies.append(FakeMessage(content="I ran out of runway, Sir."))
        tool, tool_calls = _tool()
        agent = _agent(llm, [tool])
        h = Harness()

        final = _run_loop(agent, h, budget=3)

        self.assertEqual(len(tool_calls), 3)
        self.assertEqual(final, "I ran out of runway, Sir.")
        self.assertIsNone(llm.calls[-1]["kwargs"].get("tools"), "wrap-up must be tool-free")


class SessionMemoryTests(unittest.TestCase):
    def setUp(self):
        self.memory = Memory(Path(_TMP) / f"memory-{self.id()}.db")

    def test_recall_skips_episodic_kinds(self):
        self.memory.add_memory("conversation", "User asked: connect my headphones now")
        self.memory.add_memory("staged", "User interest: connect my headphones now")
        self.memory.add_memory("short_term", "headphones pairing in progress")
        self.memory.add_memory("preference", "The user prefers Firefox over Chrome")

        headphones = self.memory.recall("headphones")
        self.assertEqual(headphones, [], "a previous chat's task must not cross sessions")

        browser = self.memory.recall("is firefox my preferred browser")
        self.assertEqual(browser, ["The user prefers Firefox over Chrome"])

    def test_recall_can_opt_into_episodic(self):
        self.memory.add_memory("conversation", "User asked: connect my headphones now")
        hits = self.memory.recall("headphones connect", include_episodic=True)
        self.assertEqual(len(hits), 1)

    def test_turn_summaries_are_not_stored_by_default(self):
        llm = FakeLLM([])
        agent = Agent(Config(), self.memory)
        agent.llm = llm

        asyncio.run(agent._store_turn_memory("dim my screen", "Dimmed the screen."))
        self.assertEqual(self.memory.memory_stats()["total"], 0)

        agent.config.agent.store_conversation_memories = True
        asyncio.run(agent._store_turn_memory("dim my screen", "Dimmed the screen."))
        self.assertEqual(self.memory.memory_stats()["by_kind"], {"conversation": 1})

    def test_new_conversation_starts_with_no_history(self):
        first = self.memory.create_conversation("A")
        self.memory.add_message(first, "user", "connect my headphones")
        second = self.memory.create_conversation("B")
        self.assertEqual(self.memory.get_messages(second), [])


if __name__ == "__main__":
    unittest.main()
