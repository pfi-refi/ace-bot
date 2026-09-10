"""What Ace RECEIVES. Synthetic turns only.

These are deterministic checks on the context path. They do NOT demonstrate that replies
got better — that needs a live evaluation Brady has not authorised.
"""
import asyncio
import contextlib
import itertools
import sys
import threading
import time
import types
import unittest
import unittest.mock as mock
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ace2'))
from backend import chat                             # noqa: E402
from backend import ops as ops_mod                   # noqa: E402
from backend import planning, taskrunner, tools      # noqa: E402


def t(role, content, minutes_ago=0):
    ts = chat.datetime.now(chat.EASTERN) - timedelta(minutes=minutes_ago)
    return {'role': role, 'content': content, 'ts': ts.isoformat()}


class ReflushCollapse(unittest.TestCase):
    """One spoken sentence arrives as 2-3 rows; a quarter of the window was spent on text
    already present."""

    def test_growing_transcript_collapses_to_its_longest_form(self):
        out = chat._collapse_reflushes([
            t('user', 'Yes, I won.t be in the office'),
            t('user', 'Yes, I won.t be in the office recruiting'),
            t('user', 'Yes, I won.t be in the office recruiting or anything else'),
        ])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]['content'].endswith('anything else'))

    def test_a_short_correction_is_never_swallowed(self):
        """'No, don.t.' is not an extension of anything and must survive."""
        out = chat._collapse_reflushes([
            t('user', 'Go ahead and lock it up'),
            t('user', 'No, don.t.'),
        ])
        self.assertEqual(len(out), 2)

    def test_assistant_turns_are_untouched(self):
        out = chat._collapse_reflushes([
            t('assistant', 'Here is the plan'),
            t('assistant', 'Here is the plan for the week'),
        ])
        self.assertEqual(len(out), 2)

    def test_an_exact_repeat_is_two_answers_not_a_reflush(self):
        out = chat._collapse_reflushes([t('user', 'yes'), t('user', 'yes')])
        self.assertEqual(len(out), 2)

    def test_a_repeat_that_is_not_a_prefix_is_kept(self):
        out = chat._collapse_reflushes([
            t('user', 'call Ken on Tuesday'),
            t('user', 'actually call Ken on Wednesday'),
        ])
        self.assertEqual(len(out), 2)


class ThreadWindow(unittest.TestCase):
    """A fixed turn count is the wrong unit: Brady's median voice gap is six seconds, so
    30 turns held about three minutes and one conversation split into several."""

    def fake(self, entries):
        return unittest.mock.patch.object(chat.history, 'read_recent', lambda *a, **k: entries)

    def test_a_whole_live_session_stays_in_one_window(self):
        import unittest.mock  # noqa: F401
        # 60 turns at 6s intervals = the 7 Sept cadence; all inside 90 minutes.
        entries = [t('user' if i % 2 else 'assistant', 'turn %d' % i, minutes_ago=(60 - i) // 10)
                   for i in range(60)]
        with unittest.mock.patch.object(chat.history, 'read_recent', lambda *a, **k: entries):
            got = chat._unified_thread()
        self.assertGreater(len(got), 30,
                           'the old 30-turn cap dropped the first half of a 22-minute call')
        self.assertLessEqual(len(got), chat._THREAD_MAX, 'must stay bounded')

    def test_old_turns_fall_out(self):
        import unittest.mock
        entries = [t('user', 'ancient', minutes_ago=600) for _ in range(40)] + \
                  [t('user', 'recent %d' % i, minutes_ago=1) for i in range(5)]
        with unittest.mock.patch.object(chat.history, 'read_recent', lambda *a, **k: entries):
            got = chat._unified_thread()
        self.assertTrue(any('recent' in g['content'] for g in got))

    def test_never_returns_less_than_the_old_behaviour(self):
        import unittest.mock
        entries = [t('user', 'old %d' % i, minutes_ago=999) for i in range(40)]
        with unittest.mock.patch.object(chat.history, 'read_recent', lambda *a, **k: entries):
            got = chat._unified_thread()
        self.assertGreaterEqual(len(got), 30, 'a quiet day must not lose its history')

    def test_unparseable_timestamps_are_kept_not_dropped(self):
        import unittest.mock
        entries = [{'role': 'user', 'content': 'no stamp %d' % i, 'ts': None}
                   for i in range(35)]
        with unittest.mock.patch.object(chat.history, 'read_recent', lambda *a, **k: entries):
            got = chat._unified_thread()
        self.assertGreaterEqual(len(got), 30)


class InstructionsArePresent(unittest.TestCase):
    """A weak check: it proves the guidance ships, not that behaviour changed."""

    def test_the_six_conversational_rules_are_in_the_built_prompt(self):
        p = chat.build_system_prompt()
        for rule in ('13b.', '13c.', '13d.', '13e.', '13f.', '13g.'):
            self.assertIn(rule, p)

    def test_retrieval_promise_is_named_explicitly(self):
        self.assertIn('NEVER PROMISE RETRIEVAL YOU DO NOT PERFORM', chat.build_system_prompt())

    def test_personality_guidance_was_not_removed(self):
        p = chat.build_system_prompt()
        self.assertIn('PLAN MY WEEK', p, 'existing rituals must survive')

    def test_runtime_profile_is_not_touched_by_this_change(self):
        src = (ROOT / 'ace2' / 'backend' / 'system_prompt.py').read_text()
        self.assertNotIn('set_profile', src)


if __name__ == '__main__':
    import unittest.mock
    unittest.main()


class ReflushNeedsToBeTheSameBreath(unittest.TestCase):
    """Adjacent stored user turns can be from different sessions. An expansion hours later
    is a separate intention, not a transcript re-flush."""

    def test_turns_seconds_apart_collapse(self):
        out = chat._collapse_reflushes([
            t('user', 'call Ken', minutes_ago=10),
            t('user', 'call Ken about the filing', minutes_ago=10),
        ])
        self.assertEqual(len(out), 1)

    def test_turns_an_hour_apart_do_not_collapse(self):
        out = chat._collapse_reflushes([
            t('user', 'call Ken', minutes_ago=120),
            t('user', 'call Ken about the filing', minutes_ago=5),
        ])
        self.assertEqual(len(out), 2, 'a later, fuller request is a new intention')

    def test_a_turn_with_no_timestamp_is_never_collapsed(self):
        out = chat._collapse_reflushes([
            {'role': 'user', 'content': 'call Ken', 'ts': None},
            {'role': 'user', 'content': 'call Ken about the filing', 'ts': None},
        ])
        self.assertEqual(len(out), 2)


class VoiceContextBudget(unittest.TestCase):
    """The widened window has to reach the voice path; it was cut back to 24 turns."""

    def test_budget_keeps_the_newest_turns(self):
        turns = [t('user', 'x' * 100, minutes_ago=i) for i in range(50, 0, -1)]
        got = chat._budget_turns(turns, 1000)
        self.assertLess(len(got), len(turns))
        self.assertEqual(got[-1]['content'], turns[-1]['content'], 'the newest must survive')

    def test_budget_always_returns_at_least_one_turn(self):
        got = chat._budget_turns([t('user', 'y' * 5000)], 100)
        self.assertEqual(len(got), 1)

    def test_a_long_structured_answer_is_not_cut_to_280_chars(self):
        """The 7 Sept week plan is one 2,522-char turn; at 280 only its opening survived."""
        plan = 'Monday do A. ' * 60 + 'Thursday do B.'
        out = chat._format_thread([t('assistant', plan)], per_turn=chat._VOICE_TURN_CHARS)
        self.assertIn('Thursday', out)
        self.assertNotIn('Thursday', chat._format_thread([t('assistant', plan)]))

    def test_default_per_turn_cap_is_unchanged_for_other_callers(self):
        self.assertIn('per_turn: int = 280', (ROOT / 'ace2' / 'backend' / 'chat.py').read_text())


class AnInterruptedVoiceTurnLeavesAnHonestRecord(unittest.TestCase):
    """10 September: Brady talked for a minute, finished with "take those down first", and Ace
    went silent. The writes landed but no assistant turn was ever saved.

    These EXECUTE the classifier and the note builder on the codebase's real tool output.
    The previous versions grepped the source, which passed while the filter was letting
    refusals through as completions (Codex, 2026-09-10)."""

    CAPTURED = "\u25c6 Captured: Pick up prescription"
    NOT_AVAIL = ("NOT AVAILABLE — mcp_create_doc is deliberately not enabled. Nothing ran. "
                 "Tell Brady plainly and do not describe this as done.")
    REDIRECT = ("USE start_task INSTEAD. mcp_create_spreadsheet creates something without "
                "checking it afterwards. NOTHING HAS BEEN CREATED by this call.")
    BLOCKED = ("NOT COMPLETED — that row is waiting on the county; do not tell him it is done.")
    GLYPH = "\u26a0\ufe0f MCP create_spreadsheet reported an error: quota exceeded"
    QUEUED = "ACCEPTED as task ab12cd34 and now queued. NOTHING HAS BEEN CREATED YET."

    def ops(self, *pairs):
        return [{"tool": t, "text": x, "state": chat.classify_result(t, x)} for t, x in pairs]

    def op(self, tool, text, **kw):
        return [{"tool": tool, "text": text,
                 "state": chat.classify_result(tool, text, **kw)}]

    def test_a_real_completion_is_reported(self):
        from backend import ops as _ops
        note = chat.interrupted_note(
            self.op("capture_item", self.CAPTURED, outcome=_ops.COMPLETED))
        self.assertIn("DID go through", note)
        self.assertIn("Pick up prescription", note)

    def test_a_refusal_is_never_reported_as_done(self):
        """STRENGTHENED 2026-09-10 (adversarial review). This used to assert the note was
        EMPTY, which conflated two different guarantees and only enforced one of them:

          (a) a refusal must never be reported as a completion — the real requirement, and
          (b) a refusal must leave no record at all — an accident of (a)'s implementation.

        (b) was itself a defect: an interrupted turn whose only operation was refused left NO
        assistant row, so the next turn read Brady's words with silence beneath them. The
        failure is now NAMED, by tool, with no refusal prose replayed. Every part of (a) is
        asserted here explicitly rather than being implied by emptiness, so this test is
        strictly harder to pass than the version it replaces."""
        for text in (self.NOT_AVAIL, self.REDIRECT, self.BLOCKED, self.GLYPH):
            note = chat.interrupted_note(self.ops(("mcp_create_doc", text)))
            self.assertNotIn("DID go through", note,
                             f"a refusal was reported as a completion: {text[:40]}")
            self.assertNotIn("still running", note,
                             f"a refusal was reported as under way: {text[:40]}")
            self.assertNotIn("may or may not", note,
                             f"a refusal was reported as possibly done: {text[:40]}")
            # The refusal's own words are addressed to the MODEL, and this string is replayed
            # into the recent thread. The FACT is recorded; the instructions are not.
            self.assertNotIn(text, note,
                             f"model-directed refusal prose was replayed: {text[:40]}")
            self.assertIn("failed or were refused", note,
                          f"the refusal left no record at all: {text[:40]}")
            self.assertIn("mcp_create_doc", note,
                          f"the refused tool was not named: {text[:40]}")

    def test_a_refusal_beside_a_success_does_not_ride_along(self):
        from backend import ops as _ops
        note = chat.interrupted_note(
            self.op("capture_item", self.CAPTURED, outcome=_ops.COMPLETED)
            + self.ops(("mcp_create_doc", self.NOT_AVAIL),
                       ("mcp_create_spreadsheet", self.REDIRECT)))
        self.assertIn("Pick up prescription", note)
        self.assertNotIn("NOT AVAILABLE", note)
        self.assertNotIn("USE start_task", note)

    def test_a_dispatched_task_is_running_not_finished(self):
        note = chat.interrupted_note(
            self.op("start_task", self.QUEUED, card_state="queued"))
        self.assertIn("still running", note)
        self.assertNotIn("DID go through", note)

    def test_nothing_done_means_nothing_written(self):
        """NOTHING ASKED means nothing written. A turn that only READ, or only painted a
        screen, still leaves no assistant row — nothing was asked of the world, so there is
        nothing to report about it. Widened 2026-09-10: it now pins the read-only and
        render-only cases too, which were never asserted and are the ones that actually
        have to stay silent."""
        self.assertEqual(chat.interrupted_note([]), "")
        self.assertEqual(chat.interrupted_note(None), "")
        self.assertEqual(chat.interrupted_note(self.ops(("get_calendar_range", "Mon 3pm"))), "")
        self.assertEqual(chat.interrupted_note(self.ops(("display_card", "Displayed."))), "")
        self.assertEqual(chat.interrupted_note(
            [{"tool": "mcp_read_sheet_values", "text": "rows", "state": chat.OP_READ}]), "")

    def test_something_asked_for_and_refused_is_not_nothing(self):
        """The other half of the rule above, and the defect it used to hide: a FAILURE is an
        event. It is recorded as one — never as a completion, and never in the refusal's own
        model-directed words."""
        note = chat.interrupted_note(self.ops(("send_email", self.GLYPH)))
        self.assertNotEqual(note, "", "a failed send left no trace of having been asked for")
        self.assertIn("failed or were refused", note)
        self.assertIn("send_email", note)
        self.assertNotIn("DID go through", note)
        self.assertNotIn(self.GLYPH, note)

    def test_it_does_not_claim_the_rest_was_skipped(self):
        """A shielded write can settle after the turn ends, so this list is not a complete
        account of it and must not read as one (Codex, 2026-09-10)."""
        from backend import ops as _ops
        note = chat.interrupted_note(
            self.op("capture_item", self.CAPTURED, outcome=_ops.COMPLETED))
        self.assertNotIn("was not done", note)
        self.assertIn("can finish after that", note)

    def test_a_native_read_is_not_a_completed_mutation(self):
        """get_calendar_range is not in the MCP registry, so the call-site lookup returned
        is_read=False and it was listed under "these DID go through"."""
        self.assertEqual(chat.classify_result("get_calendar_range", "Mon 3pm Ken"),
                         chat.OP_READ)
        self.assertEqual(chat.interrupted_note(self.op("get_calendar_range", "Mon 3pm")), "")

    def test_an_unrecognised_answer_is_unknown_not_done(self):
        """The old classifier defaulted to success, so anything it did not recognise became
        a completion claim."""
        self.assertEqual(chat.classify_result("mystery_tool", "It seems fine."),
                         chat.OP_UNKNOWN)
        note = chat.interrupted_note(self.op("mystery_tool", "It seems fine."))
        self.assertIn("NOT confirmed", note)
        self.assertNotIn("DID go through", note)

    def test_a_claimed_but_unverified_write_is_not_reported_as_done(self):
        from backend import ops as _ops
        # ops.REPORTED means the executor returned prose — claimed, nothing verified.
        self.assertEqual(
            chat.classify_result("add_task", "Added it", outcome=_ops.REPORTED),
            chat.OP_UNKNOWN)

    def test_journal_failures_are_failures(self):
        from backend import ops as _ops
        for st in (_ops.FAILED_BEFORE_DISPATCH, _ops.UNAVAILABLE, _ops.NEEDS_REVIEW):
            self.assertEqual(chat.classify_result("add_task", "x", outcome=st),
                             chat.OP_FAILED)

    def test_a_finished_task_card_is_done_and_a_failed_one_is_not(self):
        self.assertEqual(chat.classify_result("start_task", "already made",
                                              card_state="completed"), chat.OP_DONE)
        for st in ("failed", "cancelled"):
            self.assertEqual(chat.classify_result("start_task", "x", card_state=st),
                             chat.OP_FAILED)

    def test_an_mcp_read_is_not_a_completed_mutation(self):
        self.assertEqual(
            chat.classify_result("mcp_read_sheet_values", "Row  1: ['a']", is_read=True),
            chat.OP_READ)
        self.assertEqual(chat.interrupted_note([{"tool": "mcp_read_sheet_values",
                                                 "text": "rows", "state": chat.OP_READ}]), "",
                         'a read is not something that "went through"')

    def test_start_task_is_recorded_before_its_branch_returns(self):
        """That branch appends its tool_result and continues, so it never reached the
        classification below and a dispatched task was invisible to the record."""
        src = Path(chat.__file__).read_text()
        # Split on the STATEMENT, not the word — the comment above it says "before the
        # continue", which a naive split cuts at.
        branch = src.split('if block.name == "start_task":')[1].split("\n                    continue")[0]
        self.assertIn("turn_ops.append", branch)
        self.assertIn("card_state", branch)

    def test_an_empty_result_is_a_failure_not_a_success(self):
        self.assertEqual(chat.classify_result("capture_item", ""), chat.OP_FAILED)

    def test_the_handler_uses_the_builder_and_shields_it(self):
        src = Path(chat.__file__).read_text()
        seg = src.rsplit("except asyncio.CancelledError:", 1)[1]
        self.assertIn("interrupted_note(", seg)
        self.assertIn("asyncio.shield(asyncio.to_thread(history.append", seg)

    def test_a_working_turn_is_given_time_to_finish_before_cancelling(self):
        from backend import main
        self.assertGreaterEqual(main._TURN_FINISH_GRACE, 15)

    def test_the_user_half_is_still_saved_up_front(self):
        self.assertIn("_persist_user", Path(chat.__file__).read_text())


# ══════════════════════════════════════════════════════════════════════════════
# DRIVING THE REAL TURN (2026-09-10)
#
# Everything above this line calls classify_result / interrupted_note directly. Codex's
# acceptance criterion was "targeted behavioural tests through actual stream control flow",
# and he was right to insist: every helper test above passed while the start_task branch
# raised NameError before the helper was ever reached, while a shielded write cancelled
# mid-flight was omitted from turn_ops entirely, and while a screen render was reported to
# Brady as "may or may not have taken effect".
#
# These drive chat.stream_turn itself — the same loop production runs — against a faked
# Anthropic client, emit, taskrunner, tools.execute, ops journal and MCP client, cancel the
# turn the way a barge-in does, and assert on the text that actually reached history.append.
# Nothing here touches a network, a provider or a database.
# ══════════════════════════════════════════════════════════════════════════════

class _Blk:
    """One `tool_use` content block, shaped the way the SDK's is."""

    def __init__(self, name, args=None, bid=None):
        self.type, self.name = "tool_use", name
        self.input = dict(args or {})
        self.id = bid or ("tu_" + name)


class _Reply:
    def __init__(self, *blocks, stop_reason="tool_use"):
        self.content, self.stop_reason, self.usage = list(blocks), stop_reason, None


_BARGE_IN = "BARGE_IN"      # script item that hangs the model call so the test can cancel


class _FakeStream:
    def __init__(self, owner, item):
        self._owner, self._item = owner, item

    async def __aenter__(self):
        if self._item == _BARGE_IN:
            self._owner.waiting.set()
            await asyncio.Event().wait()     # cancelled from the test, exactly like a barge-in
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _no_deltas():
            return
            yield                            # pragma: no cover - makes this an async generator
        return _no_deltas()

    async def get_final_message(self):
        return self._item


class _FakeMessages:
    def __init__(self, owner):
        self._owner = owner

    def stream(self, **kwargs):
        self._owner.calls.append(kwargs)
        item = (self._owner.script.pop(0) if self._owner.script
                else _Reply(stop_reason="end_turn"))
        return _FakeStream(self._owner, item)


class _FakeAnthropic:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.waiting = asyncio.Event()
        self.messages = _FakeMessages(self)
        self.beta = types.SimpleNamespace(messages=_FakeMessages(self))


def _aret(value):
    async def _f(*a, **k):
        return value
    return _f


def _card(**fields):
    """A fake taskrunner.dispatch returning one fixed card."""
    async def _dispatch(capability, args, origin="voice", title=""):
        return dict(fields)
    return _dispatch


class _Record:
    """What one interrupted turn left behind."""

    def __init__(self, rows, events, client, settled):
        self.rows, self.events, self.client, self.settled = rows, events, client, settled

    @property
    def note(self):
        """The text actually written to conversation history as an assistant turn."""
        return "\n".join(text for role, text in self.rows if role == "assistant")

    @property
    def model_calls(self):
        return len(self.client.calls)

    def emitted(self, kind):
        return [p for k, p in self.events if k == kind]


class AnInterruptedVoiceTurnDrivenThroughStreamTurn(unittest.IsolatedAsyncioTestCase):
    """The 10 September failure, reproduced through the real control flow and asserted on
    the history text — not on internal state and not on the helpers."""

    CAPTURED = "◆ Captured: Pick up prescription"

    async def turn(self, *script, execute=None, dispatch=None, journalled=(),
                   begin=None, mcp=(), mcp_call=None, ready=None, release=None,
                   user_text="do that for me"):
        client = _FakeAnthropic(list(script) + [_BARGE_IN])
        rows, events, settled = [], [], []

        async def emit(kind, payload=None):
            events.append((kind, payload))
            return 1

        def _settle(attempt_id, state, receipt="", external_id=""):
            settled.append((attempt_id, state, receipt))

        with contextlib.ExitStack() as patches:
            for target, attr, value in (
                (chat, "_anthropic", lambda: client),
                (chat, "_fast_context", _aret("")),
                (chat, "_load_messages", _aret([])),
                (chat, "_card_payload", _aret({})),
                (chat.history, "append", lambda role, text: rows.append((role, text))),
                (planning, "capture", lambda *a, **k: None),
                (planning, "context", lambda *a, **k: ""),
                (chat.tools, "execute", execute or (lambda n, a: "ok")),
                (taskrunner, "dispatch",
                 dispatch or _card(state="queued", task_id="deadbeef0123")),
                (chat.mcp_client, "is_mcp_tool", lambda n: n in mcp),
                (chat.mcp_client, "call", mcp_call or _aret("mcp answered")),
                (ops_mod, "JOURNALLED", frozenset(journalled)),
                (ops_mod, "begin", begin or (lambda n, a: ("execute", "opkey123456", None))),
                (ops_mod, "settle", _settle),
            ):
                patches.enter_context(mock.patch.object(target, attr, value))

            task = asyncio.create_task(chat.stream_turn(user_text, emit, fast=True))
            deadline = time.monotonic() + 5.0
            pred = ready or client.waiting.is_set
            while not pred() and not task.done():
                if time.monotonic() > deadline:
                    task.cancel()
                    raise AssertionError(
                        "the turn never reached the point this test wanted to interrupt")
                await asyncio.sleep(0.005)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if release is not None:
                release.set()                      # let a mid-flight write finish on its own
                for _ in range(200):
                    await asyncio.sleep(0.005)
                    if settled:
                        break
        return _Record(rows, events, client, settled)

    # ── start_task ────────────────────────────────────────────────────────────
    async def test_a_dispatched_task_is_started_not_finished(self):
        rec = await self.turn(_Blk_reply("start_task", {"capability": "create_folder",
                                                        "name": "Deals"}))
        self.assertIn("Started and still running (NOT finished):", rec.note)
        self.assertIn("ACCEPTED as task deadbeef", rec.note)
        self.assertNotIn("DID go through", rec.note)

    async def test_an_already_completed_task_is_reported_as_already_done(self):
        rec = await self.turn(
            _Blk_reply("start_task", {"capability": "create_folder", "name": "Deals"}),
            dispatch=_card(state="completed", task_id="oldtask00001"))
        self.assertIn("These DID go through:", rec.note)
        self.assertIn("already asked for exactly this and it is DONE", rec.note)
        self.assertNotIn("still running", rec.note)

    async def test_a_task_that_never_started_is_never_reported_as_running(self):
        """FAILED with no task_id — nothing was accepted, so nothing may be narrated as
        under way. The stale-`card` bug reported exactly this as queued."""
        rec = await self.turn(
            _Blk_reply("start_task", {"capability": "create_folder", "name": "Deals"}),
            dispatch=_card(state="failed", error="the queue refused it."))
        self.assertNotIn("still running", rec.note)
        self.assertNotIn("DID go through", rec.note)
        self.assertNotIn("may or may not", rec.note)
        # STRENGTHENED 2026-09-10: this asserted the note was EMPTY, which meant Brady's
        # request vanished from history entirely — he asked for a folder, the dispatch was
        # refused, the line dropped, and the next turn saw his words with silence beneath
        # them. "Nothing true to report" was wrong: that it was asked for and did NOT happen
        # is true, and it is the only thing the next turn needs.
        self.assertIn("failed or were refused", rec.note,
                      "a task that never started left no record of having been asked for")
        self.assertIn("start_task", rec.note)
        self.assertNotIn("the queue refused it", rec.note,
                         "the refusal's own prose was replayed into the recent thread")

    async def test_a_failed_or_cancelled_task_with_an_id_is_not_running_either(self):
        for state in ("failed", "cancelled"):
            with self.subTest(state=state):
                rec = await self.turn(
                    _Blk_reply("start_task", {"capability": "create_folder", "name": "Deals"}),
                    dispatch=_card(state=state, task_id="abc123456789"))
                self.assertNotIn("still running", rec.note)
                self.assertNotIn("DID go through", rec.note)

    async def test_a_rejected_start_task_does_not_kill_the_turn(self):
        """`card_state=(card.get("state") if "card" in dir() or card else "")` raised
        NameError on exactly these branches — dir() inside a function lists BOUND locals, so
        the guard was False and `or card` evaluated an unbound name. The turn died: no tool
        result went back, an error event fired, and Ace said nothing at all."""
        cases = [
            ({}, "no capability"),
            ({"capability": "create_folder"}, "missing folder name"),
            ({"capability": "create_doc", "title": "Notes"}, "missing blocks"),
            ({"capability": "create_spreadsheet", "rows": [["a"]]}, "missing title"),
            ({"capability": "research"}, "missing question"),
            ({"capability": "not_a_capability_ace_has"}, "unknown capability"),
        ]
        for args, why in cases:
            with self.subTest(why=why):
                rec = await self.turn(
                    _Blk_reply("start_task", args),
                    dispatch=_card(state="failed", error="not a supported task"))
                self.assertEqual(rec.emitted("error"), [],
                                 "the turn crashed instead of answering the model")
                self.assertEqual(rec.model_calls, 2,
                                 "the tool result never went back — the turn died here")
                self.assertNotIn("still running", rec.note)
                self.assertNotIn("DID go through", rec.note)

    async def test_a_rejected_task_does_not_inherit_the_previous_ones_card(self):
        """`card` was a loop-scoped local, so a second start_task whose arguments were
        refused was classified from the FIRST block's card and read back to Brady as
        started-and-running."""
        rec = await self.turn(_Reply(
            _Blk("start_task", {"capability": "create_folder", "name": "Deals"}, "t1"),
            _Blk("start_task", {"capability": "create_folder"}, "t2")))
        self.assertIn("ACCEPTED as task deadbeef", rec.note)
        self.assertNotIn("No folder name came through", rec.note)
        running = rec.note.split("Started and still running (NOT finished):")[1]
        self.assertEqual(running.count("•"), 1,
                         "the refused block rode in on the dispatched one's card")

    # ── reads, renders and mutations ──────────────────────────────────────────
    async def test_a_native_read_is_never_listed_as_something_that_went_through(self):
        def _exec(name, args):
            if name == "get_calendar_range":
                return "Mon 3pm — Ken at the title office"
            return ops_mod.Outcome(ops_mod.COMPLETED, self.CAPTURED)

        rec = await self.turn(_Reply(
            _Blk("get_calendar_range", {"days": 1}, "t1"),
            _Blk("capture_item", {"text": "Pick up prescription"}, "t2")),
            execute=_exec, journalled={"capture_item"})
        self.assertIn("These DID go through:", rec.note)
        self.assertIn("Pick up prescription", rec.note)
        self.assertNotIn("Ken at the title office", rec.note,
                         "a calendar read was listed as a completed mutation")
        self.assertNotIn("get_calendar_range", rec.note)

    async def test_an_mcp_read_cut_off_mid_flight_is_not_a_possible_mutation(self):
        """2026-09-10, adversarial review. The provisional turn_ops entry — the one written
        BEFORE the await so an in-flight write cannot vanish — decided "is this a read?" from
        tools.NATIVE_READS alone. That set holds only the NATIVE reads, so every MCP read
        (Drive search, doc content, sheet values) fell through to OP_UNKNOWN, and a barge-in
        during a Drive search wrote "may or may not have taken effect" about a SEARCH. That
        is the false alarm this note exists to prevent, aimed the wrong way: it sends Brady to
        check a record a read could not have touched. connectors.py declares the kind
        statically, before the call, so there is no excuse for guessing."""
        started, release = threading.Event(), threading.Event()

        async def _slow_read(name, args):
            started.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 5)
            return "Found 3 files"

        try:
            rec = await self.turn(
                _Blk_reply("mcp_search_drive_files", {"query": "Deals"}),
                mcp={"mcp_search_drive_files"}, mcp_call=_slow_read, ready=started.is_set)
        finally:
            release.set()
        self.assertNotIn("may or may not have taken effect", rec.note,
                         "a Drive SEARCH was reported as a possible mutation")
        self.assertNotIn("Check the record", rec.note)
        self.assertEqual(rec.note, "", "a read cut off mid-flight is still only a read")

    async def test_a_write_cut_off_mid_flight_is_still_named(self):
        """The guard on the fix above: narrowing the provisional state to reads must not
        quietly stop naming an unobserved MCP WRITE, which is the case the entry exists for."""
        started, release = threading.Event(), threading.Event()

        async def _slow_write(name, args):
            started.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 5)
            return "done"

        try:
            rec = await self.turn(
                _Blk_reply("mcp_modify_sheet_values",
                           {"spreadsheet_id": "x", "range_name": "A1", "values": [["y"]]}),
                mcp={"mcp_modify_sheet_values"}, mcp_call=_slow_write, ready=started.is_set)
        finally:
            release.set()
        self.assertIn("Outcome NOT confirmed", rec.note)
        self.assertIn("mcp_modify_sheet_values", rec.note)
        self.assertNotIn("DID go through", rec.note)

    async def test_a_ui_render_is_not_an_operation_at_all(self):
        def _exec(name, args):
            return ops_mod.Outcome(ops_mod.COMPLETED, self.CAPTURED)

        rec = await self.turn(_Reply(
            _Blk("display_card", {"panel": "daybank"}, "t1"),
            _Blk("capture_item", {"text": "Pick up prescription"}, "t2")),
            execute=_exec, journalled={"capture_item"})
        self.assertIn("Pick up prescription", rec.note)
        self.assertNotIn("Displayed the", rec.note)
        self.assertNotIn("may or may not have taken effect", rec.note,
                         "a screen render was reported as a possible mutation")

    async def test_a_failed_mutation_is_dropped_and_a_verified_one_is_kept(self):
        def _exec(name, args):
            if name == "set_privacy":
                return "⚠️ Could not reach the settings store. Nothing changed."
            return ops_mod.Outcome(ops_mod.COMPLETED, self.CAPTURED)

        rec = await self.turn(_Reply(
            _Blk("set_privacy", {"discreet": True}, "t1"),
            _Blk("capture_item", {"text": "Pick up prescription"}, "t2")),
            execute=_exec, journalled={"capture_item"})
        self.assertIn("These DID go through:", rec.note)
        self.assertIn("Pick up prescription", rec.note)
        self.assertNotIn("Could not reach the settings store", rec.note)

    async def test_prose_alone_never_reaches_did_go_through(self):
        """A journalled write whose executor returns plain prose is REPORTED, not verified."""
        rec = await self.turn(
            _Blk_reply("capture_item", {"text": "Pick up prescription"}),
            execute=lambda n, a: "Saved it.", journalled={"capture_item"})
        self.assertNotIn("DID go through", rec.note)
        self.assertIn("Outcome NOT confirmed", rec.note)

    # ── shielded writes ───────────────────────────────────────────────────────
    async def test_a_shielded_write_with_an_unknown_outcome_is_not_confirmed(self):
        """ops.begin says a previous identical attempt was interrupted. _dispatch_write
        answers with a ⚠️-prefixed sentence and sinks ops.UNKNOWN — and the prose check used
        to run first, so the glyph won, the op was filed as a clean failure and the note
        dropped it. A write that may have gone through was reported as one that did not."""
        rec = await self.turn(
            _Blk_reply("capture_item", {"text": "Pick up prescription"}),
            journalled={"capture_item"},
            begin=lambda n, a: ("unknown", "opkey123456",
                                "A previous attempt at this exact action was interrupted "
                                "before its outcome was recorded."))
        self.assertIn("Outcome NOT confirmed", rec.note)
        self.assertIn("interrupted", rec.note)
        self.assertNotIn("DID go through", rec.note)

    async def test_a_write_cancelled_in_flight_is_named_never_absent(self):
        """The shield re-raises CancelledError, so control never reached turn_ops.append and
        the operation vanished from the record entirely. "Unobserved" is not "did not
        happen" — it has to be named as unconfirmed."""
        release, started = threading.Event(), threading.Event()

        def _slow(name, args):
            started.set()
            release.wait(5)
            return ops_mod.Outcome(ops_mod.COMPLETED, self.CAPTURED)

        rec = await self.turn(
            _Blk_reply("capture_item", {"text": "Pick up prescription"}),
            execute=_slow, journalled={"capture_item"},
            ready=started.is_set, release=release)

        self.assertIn("Outcome NOT confirmed", rec.note)
        self.assertIn("capture_item", rec.note,
                      "the in-flight write was not named in the interrupted record")
        self.assertNotIn("DID go through", rec.note,
                         "an unobserved write must never be claimed as done")
        # Barge-in is still barge-in: the write kept its own lifecycle and settled itself.
        self.assertEqual([s[1] for s in rec.settled], [ops_mod.COMPLETED],
                         "the shielded write no longer settles independently")

    async def test_the_note_never_claims_the_rest_was_skipped(self):
        rec = await self.turn(
            _Blk_reply("capture_item", {"text": "Pick up prescription"}),
            execute=lambda n, a: ops_mod.Outcome(ops_mod.COMPLETED, self.CAPTURED),
            journalled={"capture_item"})
        self.assertIn("can finish after that", rec.note)
        self.assertNotIn("was not done", rec.note)

    async def test_a_turn_that_did_nothing_writes_no_assistant_row(self):
        rec = await self.turn()
        self.assertEqual(rec.note, "")
        self.assertEqual([r for r, _ in rec.rows], ["user"],
                         "only Brady's half should have been persisted")


def _Blk_reply(name, args=None):
    return _Reply(_Blk(name, args))


class TheNativeToolSurfaceIsFullyDeclared(unittest.TestCase):
    """An interrupted turn decides what to say from these sets. A native tool in none of
    them is silently treated as a possible mutation with an unknown outcome — safe, but only
    by accident, and it is one edit away from being treated as a completion."""

    SETS = ("UI_TOOLS", "NATIVE_READS", "NATIVE_MUTATIONS", "ops.JOURNALLED")

    def sets(self):
        return {"UI_TOOLS": set(tools.UI_TOOLS),
                "NATIVE_READS": set(tools.NATIVE_READS),
                "NATIVE_MUTATIONS": set(tools.NATIVE_MUTATIONS),
                "ops.JOURNALLED": set(ops_mod.JOURNALLED)}

    def test_every_native_tool_is_declared_exactly_once(self):
        declared = self.sets()
        names = {t["name"] for t in tools.TOOLS}
        union = set().union(*declared.values())
        missing = sorted(names - union)
        self.assertEqual(
            missing, [],
            "undeclared native tool(s) %s — add each to exactly one of %s, or an interrupted "
            "turn has no stated basis for what it says about them" % (missing, list(self.SETS)))

    def test_the_four_declarations_do_not_overlap(self):
        declared = self.sets()
        for a, b in itertools.combinations(declared, 2):
            overlap = sorted(declared[a] & declared[b])
            self.assertEqual(overlap, [],
                             "%s and %s both claim %s — they mean different things about "
                             "what may be said happened" % (a, b, overlap))

    def test_nothing_is_declared_that_is_not_a_native_tool(self):
        names = {t["name"] for t in tools.TOOLS}
        for label in ("UI_TOOLS", "NATIVE_READS", "NATIVE_MUTATIONS"):
            stray = sorted(self.sets()[label] - names)
            self.assertEqual(stray, [], "%s names %s, which is not in tools.TOOLS" % (label, stray))

    def test_a_native_name_never_raises_in_the_connector_registry(self):
        """chat.py looks every tool up in connectors, native ones included. connector_of
        returns '' for an unregistered name and action('', name) must answer {}, not blow up
        the turn."""
        from backend import connectors
        for name in sorted({t["name"] for t in tools.TOOLS}
                           | {"", "not_a_tool", "start_task", "build_on_screen"}):
            with self.subTest(tool=name):
                got = connectors.action(connectors.connector_of(name), name)
                self.assertEqual(got, {},
                                 "no native tool is registered on a connector, so this lookup "
                                 "must answer with nothing rather than a kind chat.py trusts")


class TheSinkNeverInventsAVerdict(unittest.TestCase):
    """_dispatch_write's non-journalled branch recorded ops.COMPLETED for a native read. The
    journal never said that. Only classify_result short-circuiting reads first kept it from
    being spoken as a completion — one reorder away from a false receipt."""

    def run_write(self, name, result):
        sink = {}
        with mock.patch.object(chat.tools, "execute", lambda n, a: result), \
             mock.patch.object(ops_mod, "JOURNALLED", frozenset()):
            out = asyncio.run(chat._dispatch_write(name, {}, sink))
        return out, sink

    def test_a_read_is_not_sunk_as_completed(self):
        out, sink = self.run_write("get_calendar_range", "Mon 3pm Ken")
        self.assertEqual(out, "Mon 3pm Ken")
        self.assertNotEqual(sink.get("state"), ops_mod.COMPLETED,
                            "the journal never verified this; it is a read")
        self.assertEqual(sink.get("kind"), "read")

    def test_an_unjournalled_mutation_is_unverified_not_done(self):
        _out, sink = self.run_write("set_privacy", "Discreet mode on.")
        self.assertEqual(sink.get("kind"), "mutation")
        self.assertNotEqual(sink.get("state"), ops_mod.COMPLETED)
        self.assertEqual(chat.classify_result("set_privacy", "Discreet mode on.",
                                              outcome=sink.get("state", "")),
                         chat.OP_UNKNOWN)

    def test_only_a_structured_outcome_can_say_completed(self):
        _out, sink = self.run_write(
            "set_privacy", ops_mod.Outcome(ops_mod.COMPLETED, "Discreet mode on."))
        self.assertEqual(sink.get("state"), ops_mod.COMPLETED)


class AScreenRenderIsNotAMutation(unittest.TestCase):
    def test_ui_tools_classify_as_ui_not_unknown(self):
        for name in sorted(tools.UI_TOOLS):
            self.assertEqual(chat.classify_result(name, "Displayed it."), chat.OP_UI)

    def test_a_ui_op_is_absent_from_the_note(self):
        self.assertEqual(
            chat.interrupted_note([{"tool": "display_card", "text": "Displayed the daybank "
                                    "card on screen.", "state": chat.OP_UI}]), "")

    def test_structured_evidence_outranks_the_prose(self):
        """ops.UNKNOWN beside a ⚠️ sentence means "may have happened", not "did not"."""
        self.assertEqual(
            chat.classify_result("capture_item",
                                 "⚠️ Previous attempt's outcome is unknown.",
                                 outcome=ops_mod.UNKNOWN), chat.OP_UNKNOWN)
