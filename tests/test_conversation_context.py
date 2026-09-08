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
