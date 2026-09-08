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
