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
from jarvis.memory import is_self_belief  # noqa: E402
from jarvis.persona import system_prompt  # noqa: E402
from jarvis.llm import split_thinking  # noqa: E402


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
        llm = FakeLLM([FakeMessage(content="Your battery is at 38%, Sir.")])
        tool, _ = _tool(name="device_control")
        agent = _agent(llm, [tool])
        h = Harness()

        final = _run_loop(agent, h)

        self.assertEqual(final, "Your battery is at 38%, Sir.")
        appended = [
            m
            for call in llm.calls
            for m in call["messages"]
            if "[system]" in str(m.get("content") or "")
        ]
        self.assertEqual(appended, [], "no [system] nudges under trust_model")

    def test_cloud_endpoint_leaves_a_pasted_command_alone(self):
        """strict_tools=auto means no recovery layer for a cloud model."""
        llm = FakeLLM([FakeMessage(content="brightnessctl set 30%")])
        tool, tool_calls = _tool(name="device_control")
        agent = _agent(llm, [tool])
        agent.config.llm.base_url = "https://api.openai.com/v1"
        agent.config.llm.prefer = "cloud"
        agent.config.llm.fallback_base_url = "https://api.openai.com/v1"
        h = Harness()

        final = _run_loop(agent, h)

        self.assertFalse(agent._strict_tools)
        self.assertEqual(final, "brightnessctl set 30%")
        self.assertEqual(tool_calls, [])

    def test_local_endpoint_runs_a_pasted_command(self):
        """A local model handing back a command is the failure strict_tools exists for."""
        llm = FakeLLM([
            FakeMessage(content="brightnessctl set 30%"),
            FakeMessage(content="Dimmed the screen."),
        ])
        tool, tool_calls = _tool(name="device_control")
        agent = _agent(llm, [tool])
        h = Harness()

        self.assertTrue(agent._strict_tools, "localhost endpoint should enable recovery")
        final = _run_loop(agent, h)

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["command"], "brightnessctl set 30%")
        self.assertEqual(final, "Dimmed the screen.")

    def test_strict_tools_recovers_narrated_tool_call(self):
        llm = FakeLLM([
            FakeMessage(content="device_control action=control control=power.battery"),
            FakeMessage(content="Battery is at 38%."),
        ])
        tool, tool_calls = _tool(name="device_control")
        agent = _agent(llm, [tool])
        agent.config.agent.strict_tools = "on"
        h = Harness()

        final = _run_loop(agent, h)

        self.assertEqual(
            tool_calls, [{"action": "control", "control": "power.battery"}]
        )
        self.assertEqual(final, "Battery is at 38%.")

    def test_strict_tools_can_be_forced_off(self):
        llm = FakeLLM([FakeMessage(content="device_control action=control control=power.battery")])
        tool, tool_calls = _tool(name="device_control")
        agent = _agent(llm, [tool])
        agent.config.agent.strict_tools = "off"
        h = Harness()

        _run_loop(agent, h)

        self.assertEqual(tool_calls, [])

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


class SelfBeliefTests(unittest.TestCase):
    """A reply's hedging about its own abilities must never become a stored fact.

    REM once promoted "…despite browsing limitations" to long_term memory; recalling it
    taught the model the limitation was real, which produced the next hedge.
    """

    LIMITS = [
        "requests daily briefings despite browsing limitations.",
        "my browsing capabilities may have limitations",
        "JARVIS cannot access real-time information",
        "I don't have access to current news",
        "assistant can't fetch live news",
        "knowledge cutoff prevents current events",
        "no real-time access to the web",
        "I lack an integration for that",
    ]
    FACTS = [
        "user prefers Firefox over Chrome",
        "Requests daily Iran war news briefings",
        "Interest: Daily News Briefing on Middle East Conflict",
        "brightness is controlled via brightnessctl on /dev/intel_backlight",
        "user is unable to attend meetings before 10am",
        "the office printer has no network access",
        "user cannot use the trackpad, prefers the Razer mouse",
        "openrgb is not installed on this host",
        "The laptop lacks an ethernet port",
    ]

    def test_detects_assistant_limitations(self):
        for text in self.LIMITS:
            self.assertTrue(is_self_belief(text), text)

    def test_leaves_real_facts_alone(self):
        for text in self.FACTS:
            self.assertFalse(is_self_belief(text), text)

    def test_recall_never_returns_one(self):
        memory = Memory(Path(_TMP) / f"selfbelief-{self.id()}.db")
        memory.add_memory("long_term", "News briefings are limited by my browsing limitations")
        memory.add_memory("long_term", "User asks for news briefings every morning")
        hits = memory.recall("news briefing")
        self.assertEqual(hits, ["User asks for news briefings every morning"])


class RecoveryParsingTests(unittest.TestCase):
    """strict_tools must parse what a local model actually narrates.

    Every string here was produced verbatim by qwen3.5 through Ollama when the system
    prompt was long enough that it stopped emitting structured tool_calls.
    """

    KNOWN = {"device_control", "browse", "peripherals", "computer_use"}

    def _recovered(self, text):
        from jarvis.agent import recover_text_tool_calls
        import json as _json

        return [
            (c.function.name, _json.loads(c.function.arguments))
            for c in recover_text_tool_calls(text, "", self.KNOWN)
        ]

    def test_control_id_survives(self):
        """The `control=` argument is the whole point of the call."""
        self.assertEqual(
            self._recovered("device_control action=control control=display.brightness.set value=35"),
            [("device_control", {"action": "control", "control": "display.brightness.set", "value": 35})],
        )

    def test_read_only_control(self):
        self.assertEqual(
            self._recovered("device_control action=control control=power.battery"),
            [("device_control", {"action": "control", "control": "power.battery"})],
        )

    def test_multi_word_values_are_not_truncated(self):
        self.assertEqual(
            self._recovered("browse mode=search query=world news today"),
            [("browse", {"mode": "search", "query": "world news today"})],
        )
        self.assertEqual(
            self._recovered("peripherals action=connect target=Alaa AirPods Pro"),
            [("peripherals", {"action": "connect", "target": "Alaa AirPods Pro"})],
        )

    def test_url_is_normalised(self):
        self.assertEqual(
            self._recovered("device_control action=open url=example.com"),
            [("device_control", {"action": "open", "url": "https://example.com"})],
        )

    def test_prose_is_not_mistaken_for_a_call(self):
        for text in (
            "I will look into that for you shortly.",
            "The build finished; total=42 items were copied.",
            "Your battery is at 38%.",
        ):
            self.assertEqual(self._recovered(text), [], text)

    def test_unknown_tool_name_is_ignored(self):
        self.assertEqual(self._recovered("frobnicate action=control value=1"), [])


class ToolRoutingTests(unittest.TestCase):
    """A capability reached for through the wrong tool must be redirected, not denied.

    Observed: qwen3.5 tries `device_control action=browse`, gets a bare "unknown action",
    and tells the user web access is unavailable — inventing the very limitation the
    self-belief filter then has to keep out of memory.
    """

    def _run(self, args):
        import asyncio as _asyncio

        from jarvis.llm import LLMClient
        from jarvis.tools import device_control as dc
        from jarvis.tools.base import ToolContext

        cfg = Config()
        ctx = ToolContext(
            config=cfg,
            memory=Memory(Path(_TMP) / "routing.db"),
            llm=LLMClient(cfg.llm),
        )
        return _asyncio.run(dc.device_control.run(args, ctx))

    def test_browse_action_points_at_the_browse_tool(self):
        result = self._run({"action": "browse", "query": "world news"})
        self.assertFalse(result.ok)
        self.assertIn("browse", result.output)
        self.assertIn("mode=search", result.output)
        self.assertIn("You are online", result.output)

    def test_screenshot_action_points_at_computer_use(self):
        result = self._run({"action": "screenshot"})
        self.assertFalse(result.ok)
        self.assertIn("computer_use", result.output)

    def test_connect_action_points_at_peripherals(self):
        result = self._run({"action": "connect", "target": "airpods"})
        self.assertFalse(result.ok)
        self.assertIn("peripherals", result.output)

    def test_unknown_action_still_names_the_other_tools(self):
        result = self._run({"action": "frobnicate"})
        self.assertFalse(result.ok)
        for tool in ("browse", "computer_use", "peripherals"):
            self.assertIn(tool, result.output)

    def test_a_controls_listing_says_it_is_not_an_answer(self):
        result = self._run({"action": "controls", "query": "brightness"})
        self.assertTrue(result.ok)
        self.assertIn("not an answer", result.output)

    def test_prompts_carry_no_placeholder_argument_syntax(self):
        """The model copies `control=<id>` literally, so it must not appear."""
        from jarvis import controls as ctl
        from jarvis.persona import system_prompt as sp
        from jarvis.tools import device_control as dc

        blobs = [
            sp("Alaa", "", Config()),
            ctl.format_controls_index(),
            dc.device_control.description,
        ]
        for blob in blobs:
            self.assertNotIn("control=<id>", blob)
            self.assertNotIn("value=<v>", blob)


class ScratchpadLeakTests(unittest.TestCase):
    """A model that answers with its own planning notes must not reach the user."""

    def test_strips_quoted_constraints_and_headers(self):
        from jarvis.agent import _clean_final

        leaked = (
            "Thinking Process:\n"
            "1. Analyze the Request\n"
            '   Constraint 2: "Answer the user in plain spoken text"\n'
            "   Constraint 4: Do not call any tools.\n"
            "Your battery is at 88% and charging."
        )
        cleaned = _clean_final(leaked)
        self.assertEqual(cleaned, "Your battery is at 88% and charging.")

    def test_leaves_a_normal_answer_intact(self):
        from jarvis.agent import _clean_final

        text = "Dimmed the screen to 30%, Sir. Anything else?"
        self.assertEqual(_clean_final(text), text)


class ActionNormalisationTests(unittest.TestCase):
    """`action="control power.battery"` is a real thing models send."""

    def _norm(self, args):
        from jarvis.tools.device_control import _normalise_action

        return _normalise_action(args)

    def test_fused_control_id(self):
        action, args = self._norm({"action": "control system.processes.top"})
        self.assertEqual(action, "control")
        self.assertEqual(args["control"], "system.processes.top")

    def test_fused_query(self):
        action, args = self._norm({"action": "controls network"})
        self.assertEqual((action, args["query"]), ("controls", "network"))

    def test_fused_open_target(self):
        _, args = self._norm({"action": "open firefox"})
        self.assertEqual(args["app"], "firefox")
        _, args = self._norm({"action": "open https://example.com"})
        self.assertEqual(args["url"], "https://example.com")

    def test_a_well_formed_call_is_untouched(self):
        args_in = {"action": "control", "control": "power.battery"}
        action, args = self._norm(args_in)
        self.assertEqual(action, "control")
        self.assertEqual(args, args_in)

    def test_an_explicit_argument_is_not_overwritten(self):
        _, args = self._norm({"action": "control display.brightness.set", "control": "audio.mute"})
        self.assertEqual(args["control"], "audio.mute")


class ThinkTagTests(unittest.TestCase):
    """Chain-of-thought must never be spoken as the answer."""

    def test_matched_pair(self):
        self.assertEqual(
            split_thinking("<think>weighing it up</think>The answer is 4."),
            ("weighing it up", "The answer is 4."),
        )

    def test_unterminated_open_tag(self):
        self.assertEqual(split_thinking("<think>still going"), ("still going", ""))

    def test_template_opened_block_has_only_a_closing_tag(self):
        """Qwen3 opens <think> in the chat template, so only </think> comes back."""
        reasoning, visible = split_thinking(
            "I will check the F1 results using a web search.\n</think>\n\nVerstappen won."
        )
        self.assertEqual(visible, "Verstappen won.")
        self.assertEqual(reasoning, "I will check the F1 results using a web search.")
        self.assertNotIn("think", visible)

    def test_plain_text_is_untouched(self):
        self.assertEqual(split_thinking("plain answer"), ("", "plain answer"))


class PromptBudgetTests(unittest.TestCase):
    """The injected catalogues must stay an index, not a recitation.

    A ~6k-token machine inventory ahead of a six-word request made qwen3.5 answer
    *about* the hardware instead of using it, so size here is behaviour, not tidiness.
    """

    def test_control_index_is_far_smaller_than_the_full_dump(self):
        from jarvis import controls as ctl

        if not ctl.available():
            self.skipTest("no controls available on this host")
        index = ctl.format_controls_index()
        full = ctl.format_controls_context()
        self.assertLess(len(index), len(full) * 0.45)
        self.assertIn("action=controls query=", index)

    def test_app_index_lists_nothing_and_points_at_the_search_action(self):
        """Listing installed apps made the model launch a browser to answer a question."""
        from jarvis.appcatalog import format_apps_index, get_catalog

        apps = get_catalog().apps()
        if not apps:
            self.skipTest("no applications catalogued on this host")
        index = format_apps_index(apps)
        self.assertIn("action=apps query=", index)
        self.assertLess(len(index), 800, "the catalogue must not be listed in the prompt")
        for app in apps[:40]:
            self.assertNotIn(app.id, index)

    def test_reach_block_asserts_live_internet(self):
        from jarvis.persona import reach_context

        reach = reach_context(0)
        self.assertIn("browse mode=search", reach)
        self.assertIn("online", reach)
        self.assertIn("never open a browser", reach)

    def test_peripheral_index_omits_scanned_wifi_networks(self):
        from jarvis.peripherals import format_inventory_index

        items = [
            {"id": "a", "kind": "audio", "name": "AirPods", "connected": True},
            {"id": "w", "kind": "wifi", "name": "Neighbour-5G", "available": True},
            {"id": "u", "kind": "usb", "name": "Webcam", "available": True},
        ]
        index = format_inventory_index(items)
        self.assertNotIn("Neighbour-5G", index)
        self.assertIn("AirPods", index)
        self.assertIn("Webcam", index)

    def test_closing_directive_comes_after_the_context(self):
        prompt = system_prompt("Alaa", "=== This device ===\nHostname: box", Config())
        marker = "=== Answering the latest message ==="
        self.assertIn(marker, prompt)
        self.assertGreater(
            prompt.index(marker),
            prompt.index("Hostname: box"),
            "the directive must be the last thing the model reads",
        )
        self.assertIn("Alaa", prompt.split(marker)[1])

    def test_full_device_context_is_opt_in(self):
        cfg = Config()
        self.assertFalse(cfg.agent.full_device_context)


if __name__ == "__main__":
    unittest.main()
