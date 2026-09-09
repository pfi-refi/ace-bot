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

    def test_re_transcriptions_are_kept_rather_than_guessed_away(self):
        """Changed 9 Sept: fuzzy merging was removed because it discarded corrections — a
        correction is usually SHORTER than what it corrects. Slot pressure is now handled by
        the character budget, so every distinct statement survives and the fullest form is
        present among them."""
        kept = chat._collapse_near_reflushes(chat._collapse_reflushes(MORNING))
        sianas = [x for x in kept if 'Siana' in x['content'] or 'Sianazan' in x['content']]
        self.assertGreaterEqual(len(sianas), 1)
        self.assertTrue(any('everything signed' in x['content'] for x in sianas),
                        'the fullest form must still be present')
        self.assertIn('everything signed', chat._brief_thread(MORNING),
                      'and it must reach the brief')

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


# ── Codex's three reproduced failures, 9 September ────────────────────────────
import pytz  # noqa: E402
NOW = chat.EASTERN.localize(datetime(2026, 9, 9, 9, 0))


def et(role, text, mins, day=9):
    return {'role': role, 'content': text,
            'ts': chat.EASTERN.localize(
                datetime(2026, 9, day, 8, 0) + timedelta(minutes=mins)).isoformat()}


class CorrectionsAreNeverDiscarded(unittest.TestCase):
    """A correction is usually SHORTER than what it corrects. Keeping "the longer one"
    threw the correction away — the exact bug this patch exists to fix."""

    def block(self, rows):
        return chat._user_commitments(rows, now=NOW)

    def test_a_shorter_date_correction_survives(self):
        rows = [et('user', 'I am going to call the concrete supplier tomorrow morning '
                           'for the pour.', 0),
                et('user', 'I am going to call the concrete supplier today for the pour.', 0.3)]
        b = self.block(rows)
        self.assertIn('today for the pour', b)
        self.assertGreater(b.rindex('today for the pour'), b.rindex('tomorrow morning'),
                           'the correction must be the newest line')

    def test_a_negation_correction_survives(self):
        rows = [et('user', 'I am going to send the packet to the title company today.', 0),
                et('user', 'Do not send the packet.', 0.3)]
        self.assertIn('Do not send the packet', self.block(rows))

    def test_a_changed_person_survives(self):
        rows = [et('user', 'I am going to call Damon about the pour this afternoon.', 0),
                et('user', 'I am going to call my uncle instead.', 0.3)]
        b = self.block(rows)
        self.assertIn('call my uncle instead', b)
        self.assertIn('call Damon', b, 'both statements are kept; neither is guessed away')

    def test_a_changed_amount_survives(self):
        rows = [et('user', 'I am going to pay the minimum of eighty dollars on it.', 0),
                et('user', 'Actually it is a hundred and six.', 0.3)]
        self.assertIn('hundred and six', self.block(rows))

    def test_nothing_is_merged_on_similarity(self):
        rows = [et('user', 'I am going to call the plant tomorrow.', 0),
                et('user', 'I am going to call the plant today.', 0.3)]
        self.assertEqual(len(chat._collapse_near_reflushes(rows)), 2)

    def test_a_strict_extension_still_merges(self):
        """Nothing is lost when the later text contains the earlier one in full."""
        rows = [et('user', 'I am going to call the plant', 0),
                et('user', 'I am going to call the plant today about Friday', 0.2)]
        self.assertEqual(len(chat._collapse_reflushes(rows)), 1)


class TodayMeansToday(unittest.TestCase):
    """_unified_thread falls back to older turns on a quiet morning, and a prior-day
    statement was then presented under TODAY with a UTC clock."""

    def test_a_prior_day_statement_is_not_labelled_today(self):
        rows = [et('user', 'I am going to mail the packet today.', 0, day=8)]
        today_block, prior_block = chat._commitment_lines(rows, now=NOW)
        self.assertNotIn('mail the packet', today_block)
        self.assertIn('mail the packet', prior_block)

    def test_prior_context_warns_about_relative_words(self):
        rows = [et('user', 'I am going to mail the packet today.', 0, day=8)]
        _, prior = chat._commitment_lines(rows, now=NOW)
        self.assertIn('do not read a', prior.lower())

    def test_times_are_rendered_in_eastern(self):
        rows = [et('user', 'I am going to call the plant today.', 30)]
        today, _ = chat._commitment_lines(rows, now=NOW)
        self.assertIn('8:30 AM ET', today, today)

    def test_a_quiet_morning_yields_an_empty_today_block(self):
        """The fallback case: only older turns available. Nothing may be claimed as today."""
        rows = [et('user', 'I am going to do it today.', 0, day=7),
                et('user', 'I am going to do it today.', 0, day=8)]
        today, prior = chat._commitment_lines(rows, now=NOW)
        self.assertEqual(today, '')
        self.assertIn('EARLIER DAYS', prior)

    def test_just_after_midnight_is_still_today(self):
        row = {'role': 'user', 'content': 'I am going to head out early today.',
               'ts': chat.EASTERN.localize(datetime(2026, 9, 9, 0, 5)).isoformat()}
        today, _ = chat._commitment_lines([row], now=NOW)
        self.assertIn('head out early', today)

    def test_just_before_midnight_is_not_today(self):
        row = {'role': 'user', 'content': 'I am going to head out early tomorrow.',
               'ts': chat.EASTERN.localize(datetime(2026, 9, 8, 23, 55)).isoformat()}
        today, prior = chat._commitment_lines([row], now=NOW)
        self.assertEqual(today, '')
        self.assertIn('head out early', prior)


class VerbatimMeansComplete(unittest.TestCase):
    """A 482-character statement lost its ending — the operative constraint — and the
    opening was presented as authoritative testimony."""

    LONG = ('I am going to run the whole plan for the day. ' * 10
            + 'Do not send the packet; the client has not signed.')

    def test_a_long_statement_keeps_its_ending(self):
        b = chat._user_commitments([et('user', self.LONG, 0)], now=NOW)
        self.assertIn('the client has not signed', b)

    def test_an_unfittable_statement_is_named_not_trimmed(self):
        today, _ = chat._commitment_lines([et('user', self.LONG, 0)], now=NOW, total_chars=120)
        self.assertIn('NOT quoted here', today)
        self.assertNotIn('whole plan for the day', today,
                         'a partial quote must never be presented as testimony')

    def test_the_omission_warns_against_acting_on_it(self):
        today, _ = chat._commitment_lines([et('user', self.LONG, 0)], now=NOW, total_chars=120)
        self.assertIn('ask before acting', today)

    def test_newer_statements_win_the_budget(self):
        rows = [et('user', 'I am going to do the old thing today. ' * 20, 0),
                et('user', 'Actually, do the new thing instead.', 30)]
        today, _ = chat._commitment_lines(rows, now=NOW, total_chars=200)
        self.assertIn('do the new thing instead', today)
