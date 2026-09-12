"""The deep_dive capability, executed — not read.

A long voice question used to run inside the live turn, where Brady heard three holding
phrases and then up to forty seconds of nothing before the answer was thrown away. It is a
background task now. These tests drive the real handler with a scripted model so the
read-only guarantee, the budgets and the honest endings are exercised rather than asserted
about the source.
"""
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import capabilities as cp
from backend import chat, ops, tasks, taskrunner, tools


def text_block(t):
    return types.SimpleNamespace(type="text", text=t)


def tool_block(name, args=None, bid="tu_1"):
    return types.SimpleNamespace(type="tool_use", name=name, input=args or {}, id=bid)


class Model:
    """Replays scripted responses; records the tools it was OFFERED each round."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.offered = []
        self.systems = []
        self.messages = types.SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.offered.append(sorted(t["name"] for t in (kw.get("tools") or [])))
        self.systems.append(kw.get("system") or "")
        blocks = self.rounds.pop(0) if self.rounds else [text_block("done")]
        return types.SimpleNamespace(content=blocks)


class DeepDiveRuns(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executed = []

        def fake_execute(name, args):
            self.executed.append(name)
            return f"result of {name}"

        self.patches = [
            patch.object(chat, "_live_context", AsyncMock(return_value=("SLOW", "FAST"))),
            patch.object(chat, "build_system_prompt", lambda: "ACE"),
            patch.object(tools, "execute", fake_execute),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    async def run_dive(self, model, question="how does my week line up?", should_stop=None):
        return await cp.deep_dive({"question": question, "_client": model}, None,
                                  should_stop=should_stop)

    async def test_it_answers_and_reports_what_it_read(self):
        model = Model([tool_block("get_calendar_range", {"days": 7})],
                      [text_block("Chris is next. Ken is waiting on you.")])
        out = await self.run_dive(model)
        self.assertEqual(self.executed, ["get_calendar_range"])
        self.assertEqual(out["reads_run"], 1)
        self.assertIn("Chris is next", out["answer"])
        self.assertTrue(out["checked_at"])

    async def test_only_read_tools_are_ever_offered(self):
        model = Model([text_block("nothing to look up")])
        await self.run_dive(model)
        offered = set(model.offered[0])
        self.assertEqual(offered, set(cp.DEEP_DIVE_READS))
        self.assertEqual(offered & tools.NATIVE_MUTATIONS, set())
        self.assertEqual(offered & set(ops.JOURNALLED), set())

    async def test_a_write_it_asks_for_anyway_is_refused_not_executed(self):
        """Two layers on purpose. Reaching here means something changed upstream; the loop
        must still refuse rather than run it."""
        model = Model([tool_block("send_email", {"to": "x@y.z"})],
                      [text_block("I could not send anything.")])
        out = await self.run_dive(model)
        self.assertEqual(self.executed, [], "a write executed inside a read-only deep dive")
        self.assertEqual(out["reads_run"], 0)
        self.assertTrue(any("only ever reads" in w for w in out["limits"]))

    async def test_the_read_budget_is_enforced_by_the_server(self):
        rounds = [[tool_block("recall", {"query": str(i)}, f"tu_{i}")]
                  for i in range(cp.DEEP_DIVE_MAX_READS + 2)]
        rounds.append([text_block("what I have so far")])
        with patch.object(cp, "DEEP_DIVE_MAX_ROUNDS", len(rounds) + 1):
            out = await self.run_dive(Model(*rounds))
        self.assertEqual(len(self.executed), cp.DEEP_DIVE_MAX_READS)
        self.assertEqual(out["reads_run"], cp.DEEP_DIVE_MAX_READS)
        self.assertTrue(any("ran out of the reads" in w for w in out["limits"]))

    async def test_running_out_of_rounds_still_produces_an_answer(self):
        """The failure this capability replaces was work that evaporated. A deep dive that
        hits the round ceiling makes one final tool-free pass instead of returning nothing."""
        rounds = [[tool_block("recall", {"q": str(i)}, f"tu_{i}")] for i in range(3)]
        rounds.append([text_block("Here is what I got before I ran out.")])
        with patch.object(cp, "DEEP_DIVE_MAX_ROUNDS", 3):
            model = Model(*rounds)
            out = await self.run_dive(model)
        self.assertIn("ran out", out["answer"].lower())
        self.assertEqual(model.offered[-1], [], "the final pass must offer no tools")
        self.assertTrue(any("ran out of the reads" in w for w in out["limits"]))

    async def test_a_stop_between_rounds_never_claims_a_change(self):
        calls = {"n": 0}

        async def stop():
            calls["n"] += 1
            return calls["n"] > 1

        model = Model([tool_block("recall", {"q": "a"})], [text_block("late")])
        with self.assertRaises(cp.Cancelled) as caught:
            await self.run_dive(model, should_stop=stop)
        self.assertIn("only ever reads", caught.exception.message)

    async def test_no_question_is_refused_before_anything_runs(self):
        with self.assertRaises(cp.Failed):
            await cp.deep_dive({"question": "  ", "_client": Model()}, None)
        self.assertEqual(self.executed, [])

    async def test_an_empty_answer_is_a_failure_not_a_blank_card(self):
        with self.assertRaises(cp.Failed) as caught:
            await self.run_dive(Model([text_block("   ")]))
        self.assertIn("nothing to tell you", caught.exception.message)

    async def test_unreachable_data_is_refused_rather_than_answered_from_nothing(self):
        with patch.object(chat, "_live_context", AsyncMock(side_effect=RuntimeError("db"))):
            with self.assertRaises(cp.Failed) as caught:
                await self.run_dive(Model([text_block("confident prose")]))
        self.assertIn("have not written anything", caught.exception.message)

    async def test_his_own_data_is_actually_put_in_front_of_the_model(self):
        model = Model([text_block("answer")])
        await self.run_dive(model)
        self.assertIn("SLOW", model.systems[0])
        self.assertIn("FAST", model.systems[0])


class TheReadOnlyGuaranteeIsStructural(unittest.TestCase):
    def test_the_allowed_set_cannot_contain_a_write(self):
        self.assertTrue(cp.DEEP_DIVE_READS <= tools.NATIVE_READS)
        self.assertEqual(cp.DEEP_DIVE_READS & tools.NATIVE_MUTATIONS, set())
        self.assertEqual(cp.DEEP_DIVE_READS & set(ops.JOURNALLED), set())
        self.assertEqual(cp.DEEP_DIVE_READS & tools.UI_TOOLS, set())

    def test_every_allowed_name_is_a_real_tool(self):
        known = {t["name"] for t in tools.TOOLS}
        self.assertEqual(cp.DEEP_DIVE_READS - known, set())

    def test_the_schemas_offered_match_the_allowed_set(self):
        self.assertEqual({t["name"] for t in cp._deep_dive_schemas()}, set(cp.DEEP_DIVE_READS))

    def test_it_is_registered_and_reachable_from_both_mouths(self):
        self.assertIn("deep_dive", cp.REGISTRY)
        self.assertIn("deep_dive", cp.ARG_KEYS)
        self.assertIn("deep_dive",
                      tools.START_TASK["input_schema"]["properties"]["capability"]["enum"])

    def test_the_daily_cap_is_admitted_not_merely_declared(self):
        """research's cap rides on its connector. deep_dive has no connector, so the spec
        must carry one and dispatch must read it — or the number is decoration."""
        spec = cp.REGISTRY["deep_dive"]
        self.assertTrue(spec.get("costs_money"))
        self.assertGreater(int(spec.get("daily_cap") or 0), 0)
        src = Path(taskrunner.__file__).read_text()
        seg = src.split("async def dispatch(")[1].split("\nasync def ")[0]
        self.assertIn('spec.get("daily_cap")', seg)


class FinishedWorkReachesTheCall(unittest.TestCase):
    """pending_voice_notices existed with tests and NO caller since 9 September, so a
    completed background task only ever appeared on a card — useless on a phone call."""

    def test_an_answer_is_written_for_speaking_not_for_reading(self):
        out = chat._format_finished_tasks([{
            "id": "t1", "state": tasks.COMPLETED, "title": "how the week lines up",
            "capability": "deep_dive",
            "result": {"answer": "Chris is next.", "limits": ["Check the amounts."]}}])
        self.assertIn("Chris is next.", out)
        self.assertIn("how the week lines up", out)
        self.assertIn("caveat: Check the amounts.", out)
        self.assertIn("in your own words", out)

    def test_a_failure_is_reported_as_a_failure(self):
        out = chat._format_finished_tasks([{
            "id": "t2", "state": tasks.FAILED, "title": "deal review",
            "capability": "deep_dive", "error": "the model did not come back"}])
        self.assertIn("DID NOT FINISH", out)
        self.assertIn("the model did not come back", out)

    def test_nothing_finished_adds_nothing_to_the_context(self):
        self.assertEqual(chat._format_finished_tasks([]), "")

    def test_a_completed_card_carries_the_answer_without_web_sources(self):
        """The answer used to be nested under `sources`, so a capability that reads his own
        records produced a card with a tick and nothing to read."""
        card = tasks.card({"id": "t3", "state": tasks.COMPLETED, "capability": "deep_dive",
                           "result": {"answer": "Ken is waiting on you.",
                                      "checked_at": "2026-09-11"}})
        self.assertEqual(card["answer"], "Ken is waiting on you.")
        self.assertEqual(card["title"], "Deep dive")
        self.assertNotIn("sources", card)

    def test_something_to_read_does_not_vanish_in_nine_seconds(self):
        """A receipt may dismiss itself. A briefing he is meant to read may not."""
        readable = tasks.card({"id": "t4", "state": tasks.COMPLETED, "capability": "deep_dive",
                               "result": {"answer": "A long answer."}})
        self.assertEqual(readable["auto_dismiss_ms"], 0)
        self.assertTrue(readable["sticky"])
        receipt = tasks.card({"id": "t5", "state": tasks.COMPLETED,
                              "capability": "create_doc", "result": {"url": "u"}})
        self.assertEqual(receipt["auto_dismiss_ms"], 9000, "an ordinary receipt changed too")
        self.assertFalse(receipt["sticky"])


class AnnouncedOnce(unittest.IsolatedAsyncioTestCase):
    """Marked spoken only by a turn that actually said something."""

    async def drive(self, reply_text):
        import contextlib
        from backend import planning
        spoken, notice = [], {"id": "t9", "state": tasks.COMPLETED, "title": "the week",
                              "capability": "deep_dive", "result": {"answer": "Chris."}}

        class Stream:
            def __init__(self, text):
                self.text = text
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def __aiter__(self):
                async def chunks():
                    if self.text:
                        yield types.SimpleNamespace(
                            type="content_block_delta",
                            delta=types.SimpleNamespace(type="text_delta", text=self.text))
                return chunks()
            async def get_final_message(self):
                return types.SimpleNamespace(content=[], stop_reason="end_turn", usage=None)

        sent = {}

        def stream(**kw):
            sent.update(kw)
            return Stream(reply_text)

        client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream))

        async def emit(kind, payload):
            return None

        with contextlib.ExitStack() as stack:
            for target, key, value in (
                (chat, "_anthropic", lambda: client),
                (chat, "_fast_context", AsyncMock(return_value="")),
                (chat, "_load_messages", AsyncMock(return_value=[])),
                (chat, "build_system_prompt", lambda: "test"),
                (chat, "maybe_toggle_privacy", lambda text: None),
                (chat, "_ttl_ok", [False]),
                (chat.mcp_client, "is_mcp_tool", lambda name: False),
                (chat.history, "append", lambda role, text: None),
                (planning, "capture", lambda *a: None),
                (planning, "context", lambda: ""),
                (taskrunner, "pending_voice_notices", lambda limit=3: [notice]),
                (tasks, "mark_spoken", lambda tid: spoken.append(tid)),
            ):
                stack.enter_context(patch.object(target, key, value))
            reply = await chat.stream_turn("what did you find?", emit, fast=True)
        return reply, spoken, sent

    async def test_a_real_reply_marks_the_notice_spoken(self):
        reply, spoken, _ = await self.drive("Chris is next on the Nigel build.")
        self.assertEqual(spoken, ["t9"])

    async def test_the_finished_answer_actually_reaches_the_model(self):
        """Mutation-checked: formatting the notice and never putting it in the turn's context
        left every other test in this class green while Brady heard nothing."""
        _reply, _spoken, sent = await self.drive("Chris is next.")
        system = "".join(b.get("text", "") for b in (sent.get("system") or []))
        self.assertIn("FINISHED WHILE YOU WERE TALKING", system)
        self.assertIn("Chris.", system)

    async def test_the_fallback_line_does_not_burn_the_notice(self):
        reply, spoken, _ = await self.drive("")
        self.assertIn("say that again", reply)
        self.assertEqual(spoken, [], "a turn that said nothing consumed the announcement")


if __name__ == "__main__":
    unittest.main()
