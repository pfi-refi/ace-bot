"""What Ace RECEIVES. Synthetic turns only.

These are deterministic checks on the context path. They do NOT demonstrate that replies
got better — that needs a live evaluation Brady has not authorised.
"""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ace2'))
from backend import chat                             # noqa: E402


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
        for text in (self.NOT_AVAIL, self.REDIRECT, self.BLOCKED, self.GLYPH):
            note = chat.interrupted_note(self.ops(("mcp_create_doc", text)))
            self.assertEqual(note, "", f"a refusal was written to history: {text[:40]}")

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
        self.assertEqual(chat.interrupted_note([]), "")
        self.assertEqual(chat.interrupted_note(self.ops(("send_email", self.GLYPH))), "")

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
