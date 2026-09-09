"""What the morning brief RECEIVES. Synthetic turns modelled on 9 September.

These test the input contract. They do NOT show the brief's wording improved — that needs a
paid draft evaluation, which is Brady's call.
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ace2'))
from backend import chat                              # noqa: E402


def t(role, content, mins):
    ts = datetime(2026, 9, 9, 8, 0) + timedelta(minutes=mins)
    return {'role': role, 'content': content, 'ts': ts.isoformat()}


# The 9 September sequence, including the ASR re-flushes that ate four of sixteen slots.
MORNING = [
    t('user', 'No, we are good to roll. I am out DoorDashing right now, like I was '
              'yesterday, and I am gonna go to the gym later this evening because I did not '
              'have time this morning.', 28),
    t('assistant', 'Got it — DoorDashing now, gym tonight.', 28),
    t('user', 'Uh, no, I just have to double-check on Sianazan.', 29),
    t('user', 'Uh, no, I just have to double-check on Siana Zahn.', 29),
    t('user', 'Uh, no, I just have to double-check on Siana Zahn, to make sure she got '
              'everything signed, and then I have some other stuff I wanted to do.', 29),
    t('assistant', 'Got it — confirm she signed, then prospecting and website work.', 29),
    t('user', 'I am gonna handle it today while I am out, but go ahead and give me a full '
              'layout of what I need to get completed today.', 30),
    t('assistant', 'Here is your full day: ' + ('detail ' * 60)
                   + 'FINALLY lock Nick to a Thursday time and chase Josh.', 30),
    t('user', 'No, I am gonna call them.', 31),
    t('user', 'No, I am gonna call them today, not tomorrow.', 31),
    t('assistant', 'Call them today then.', 31),
]


class TheDeployedDefect(unittest.TestCase):
    """Reproduced from saved history before anything was changed."""

    def test_the_old_window_truncated_the_day_layout(self):
        old = chat._format_thread(MORNING[-16:])          # deployed: default 280-char cap
        self.assertNotIn('detail detail detail detail detail detail detail detail detail '
                         'detail detail detail detail detail detail detail detail detail '
                         'detail detail detail detail detail detail detail detail detail '
                         'detail detail detail detail detail detail detail detail detail '
                         'detail detail detail detail', old,
                         'the plan-bearing turn used to be cut at 280 characters')


class TheBriefsInput(unittest.TestCase):
    def thread(self):
        return chat._brief_thread(MORNING)

    def commitments(self):
        return chat._user_commitments(MORNING)

    def test_the_gym_correction_survives(self):
        """The brief said 'you hit the gym this morning' 40 minutes after he said otherwise."""
        both = self.thread() + self.commitments()
        self.assertIn('gym later this evening', both)

    def test_the_concrete_correction_survives(self):
        self.assertIn('today, not tomorrow', self.thread() + self.commitments())

    def test_the_day_layout_is_no_longer_cut_at_280(self):
        """The plan-bearing turn is 460+ characters; at the old cap its tail — the Nick and
        Josh actions — was cut off, which is exactly what the brief then omitted."""
        tail = 'lock Nick to a Thursday time and chase Josh'
        self.assertNotIn(tail, chat._format_thread(MORNING[-16:]), 'old cap should cut it')
        self.assertIn(tail, self.thread(), 'the brief must now see the whole answer')

    def test_re_transcriptions_of_one_sentence_collapse(self):
        """Four rows for one spoken sentence used four of sixteen slots."""
        kept = chat._collapse_near_reflushes(chat._collapse_reflushes(MORNING))
        sianas = [x for x in kept if 'Siana' in x['content'] or 'Sianazan' in x['content']]
        self.assertEqual(len(sianas), 1, [x['content'][:40] for x in sianas])
        self.assertIn('everything signed', sianas[0]['content'], 'the fullest form must win')

    def test_a_genuine_short_correction_is_not_collapsed_away(self):
        pair = [t('user', 'Go ahead and book it', 10), t('user', 'No, do not.', 11)]
        self.assertEqual(len(chat._collapse_near_reflushes(pair)), 2)

    def test_his_own_words_are_verbatim_and_outrank(self):
        c = self.commitments()
        self.assertIn('OUTRANK', c)
        self.assertIn('his words', c)
        self.assertIn('DoorDashing', c)

    def test_assistant_lines_are_not_in_the_testimony_block(self):
        self.assertNotIn('Here is your full day', self.commitments())

    def test_the_block_is_bounded(self):
        self.assertLess(len(self.thread()), chat._BRIEF_THREAD_CHARS + 1200)


class TheEvidenceRule(unittest.TestCase):
    def test_completion_needs_evidence(self):
        self.assertIn('NEVER SAY SOMETHING IS DONE WITHOUT EVIDENCE', chat._BRIEF_RULES)

    def test_a_calendar_entry_is_not_evidence(self):
        self.assertIn('a calendar entry (that is a plan)', chat._BRIEF_RULES)

    def test_a_promise_from_someone_else_is_not_evidence(self):
        self.assertIn("she said she'll sign", chat._BRIEF_RULES)

    def test_aces_own_earlier_prose_is_not_evidence(self):
        self.assertIn('your own prose is a claim, not a source', chat._BRIEF_RULES)

    def test_his_stated_plan_leads(self):
        self.assertIn('HIS PLAN FOR TODAY IS THE POINT', chat._BRIEF_RULES)

    def test_the_money_rule_is_preserved(self):
        self.assertIn('THE CONVERSATION OUTRANKS THE BOARD', chat._BRIEF_RULES)


if __name__ == '__main__':
    unittest.main()
