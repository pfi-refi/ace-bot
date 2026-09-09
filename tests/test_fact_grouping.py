"""Keeping one person's facts together. Synthetic facts only — no real names, no store.

These check the CONTEXT the model receives. They do not show that replies improved; that
needs a paid evaluation, which is Brady's call.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ace2'))
from backend import chat                              # noqa: E402


class EntityDetection(unittest.TestCase):
    def test_a_mid_sentence_name_is_an_entity(self):
        self.assertIn('Marlow', chat._entities_in('Brady met Marlow about the roof'))

    def test_a_sentence_initial_verb_is_not(self):
        """Board rows begin with verbs — Find/Ask/Build/Review — and reading those as people
        put 76 unrelated facts into the window."""
        for text in ('Find a time with the supplier', 'Ask the office about it',
                     'Build the ad campaign', 'Review the paperwork'):
            first = text.split()[0]
            self.assertNotIn(first, chat._entities_in(text), text)

    def test_a_verb_is_never_a_name_whatever_its_tense(self):
        """Blanking every sentence-initial capital also lost real names that legitimately
        start a fact, so the verbs are named instead of the position being blamed."""
        got = chat._entities_in('Left a message. Called again later')
        self.assertEqual(got, [], got)

    def test_a_name_that_starts_a_fact_is_still_found(self):
        self.assertIn('Marlow', chat._entities_in('Marlow paramed exam scheduled for August'))

    def test_relationship_words_count_as_names(self):
        self.assertIn('Uncle', chat._entities_in("the pour is for his uncle's greenhouse"))
        self.assertIn('Aunt', chat._entities_in("waiting on her aunt to sign"))

    def test_brady_and_his_companies_are_not_entities(self):
        self.assertEqual(chat._entities_in('Brady and PFI and GFI'), [])

    def test_months_and_weekdays_are_not_entities(self):
        self.assertEqual(chat._entities_in('due on Tuesday in September'), [])


class GroupingIsLossless(unittest.TestCase):
    FACTS = ['note about Marlow one', 'note about Marlow two', 'note about Marlow three',
             'note about Renner one', 'note about Renner two', 'note about Renner three',
             'a fact naming nobody at all', 'another anonymous fact']

    def test_every_fact_is_printed_exactly_once(self):
        out = chat._group_facts(self.FACTS)
        import re as _re
        body = [_re.sub(r'^\[[^\]]+\] ', '', l.strip()[2:].split('   [also names:')[0])
                for l in out.splitlines() if l.startswith('  - ')]
        self.assertEqual(len(body), len(self.FACTS))
        self.assertEqual(sorted(body), sorted(self.FACTS))

    def test_unheaded_facts_still_appear(self):
        out = chat._group_facts(self.FACTS)
        self.assertIn('a fact naming nobody at all', out)
        self.assertIn('OTHER:', out)

    def test_each_person_gets_their_own_heading(self):
        out = chat._group_facts(self.FACTS)
        self.assertIn('MARLOW (3):', out)
        self.assertIn('RENNER (3):', out)

    def test_the_correction_is_the_last_dated_line(self):
        stale = 'Marlow project is on Thursday'
        fix = 'Marlow correction: project is on Friday, not Thursday'
        out = chat._group_facts([fix, stale], min_facts=2, meta={
            fix: {'ts': '2026-09-08T10:00:00', 'tier': 'core'},
            stale: {'ts': '2026-09-01T10:00:00', 'tier': 'active'}})
        lines = [l for l in out.splitlines() if l.startswith('  - ')]
        self.assertIn('2026-09-08', lines[-1])
        self.assertIn('Friday', lines[-1], 'the newest DATE must land last, not the core tier')

    def test_dates_are_printed_so_recency_is_not_inferred(self):
        f = 'Marlow one'
        out = chat._group_facts([f, 'Marlow two'], min_facts=2,
                                meta={f: {'ts': '2026-09-02T00:00:00', 'tier': 'active'}})
        self.assertIn('recorded 2026-09-02', out)
        self.assertIn('[undated]', out, 'a fact with no date must say so, not borrow one')

    def test_a_shared_fact_is_printed_once_and_cross_referenced(self):
        facts = self.FACTS + ['Marlow and Renner met about the same job']
        out = chat._group_facts(facts)
        body = [l for l in out.splitlines() if l.startswith('  - ')]
        self.assertEqual(len([b for b in body if 'met about the same job' in b]), 1,
                         'printing it under both headings doubled the block')
        self.assertIn('[also names:', out)

    def test_the_reading_instruction_is_present(self):
        out = chat._group_facts(self.FACTS)
        self.assertIn('Never carry a detail from one heading to another', out)
        self.assertIn('newest last', out)
        self.assertIn('EXPLICITLY CORRECTS AN EARLIER ONE', out)
        self.assertIn('ASK only when the disagreement is genuinely unresolved', out)


class SweepNarrativesAreNotFacts(unittest.TestCase):
    """One row naming five unrelated people is the blending vector no grouping can undo."""

    def test_learning_sweep_narrative_is_filtered(self):
        self.assertTrue(chat._is_meta_fact(
            'Learning sweep complete. Saved three updates: one deal, one client, one bill.'))

    def test_saved_n_updates_is_filtered(self):
        self.assertTrue(chat._is_meta_fact('Saved two updates: a thing and another thing'))

    def test_a_real_fact_is_not_filtered(self):
        self.assertFalse(chat._is_meta_fact(
            'Marlow paramed exam scheduled for August 4'))

    def test_filtering_is_context_only_not_deletion(self):
        """The row stays in the store; only the live window skips it."""
        src = (ROOT / 'ace2' / 'backend' / 'chat.py').read_text()
        self.assertIn('the row stays in the store and in recall', src)


class LiveEntitiesDriveTheWindow(unittest.TestCase):
    """An entity whose last note is old falls out of head+tail, and the model then fills the
    hole from whichever similar case IS present — the reproduced aunt/Thiami/Rebecca failure."""

    def test_an_open_row_puts_its_person_in_the_roster(self):
        items = [{'status': 'open', 'text': 'Follow up with Marlow on the packet'}]
        self.assertIn('Marlow', chat._live_entities(items))

    def test_a_closed_row_does_not(self):
        items = [{'status': 'done', 'text': 'Follow up with Renner on the packet'}]
        self.assertNotIn('Renner', chat._live_entities(items))

    def test_old_facts_for_a_live_person_are_carried_in(self):
        old = ['Marlow fact %d' % i for i in range(3)]
        filler = ['unrelated filler %d' % i for i in range(200)]
        window = chat._mem_slim(old + filler, head=5, tail=5, live_entities=['Marlow'])
        kept = [f for f in window if 'Marlow' in f]
        self.assertTrue(kept, 'a live case must not be invisible just because it is old')

    def test_without_a_roster_the_old_behaviour_is_unchanged(self):
        facts = ['f%d' % i for i in range(200)]
        self.assertEqual(len(chat._mem_slim(facts, head=5, tail=5)), 11)   # 5 + notice + 5


if __name__ == '__main__':
    unittest.main()


class TargetedConversationRules(unittest.TestCase):
    """Instructions aimed at the four reproduced failures. Presence only — these do NOT
    show the failures are fixed; that needs a paid retest, which is Brady's call."""

    def prompt(self):
        return chat.build_system_prompt()

    def test_two_cases_are_not_one_case(self):
        p = self.prompt()
        self.assertIn('TWO CASES ARE NOT ONE CASE', p)
        self.assertIn('because they share a word', p)

    def test_a_promise_is_not_a_completion(self):
        p = self.prompt()
        self.assertIn('A PROMISE IS NOT A COMPLETION', p)
        self.assertIn('never chain further steps off it', p)

    def test_conflicts_are_surfaced_not_smoothed(self):
        p = self.prompt()
        self.assertIn('WHEN TWO DATED LINES DISAGREE', p)
        self.assertIn('Do not pick one silently', p)

    def test_the_earlier_rules_survive(self):
        p = self.prompt()
        for rule in ('13b.', '13c.', '13d.', '13e.', '13f.', '13g.', 'PLAN MY WEEK'):
            self.assertIn(rule, p)


class SameDayPrecision(unittest.TestCase):
    """A day string could not separate a 09:00 note from a 17:00 correction, and the stable
    sort then fell back to the read path's importance order."""

    PAIR = {'Renner pour is booked for Thursday': '2026-09-08T09:00:00',
            'Renner correction: moved to Friday, not Thursday': '2026-09-08T17:00:00'}

    def rendered(self):
        facts = list(self.PAIR)[::-1]          # importance order, as the read path returns
        meta = {k: {'ts': v, 'tier': 'core'} for k, v in self.PAIR.items()}
        out = chat._group_facts(facts, min_facts=2, meta=meta)
        return [l for l in out.splitlines() if l.startswith('  - ')]

    def test_the_later_time_lands_last(self):
        self.assertIn('Friday', self.rendered()[-1])

    def test_minutes_are_shown_so_same_day_updates_are_distinguishable(self):
        rows = self.rendered()
        self.assertIn('09:00', rows[0])
        self.assertIn('17:00', rows[-1])

    def test_the_stamp_says_recorded_not_happened(self):
        """Writing down an old event today does not make its content a new correction."""
        self.assertIn('recorded', self.rendered()[0])

    def test_midnight_timestamps_do_not_print_a_misleading_time(self):
        meta = {'a fact': {'ts': '2026-09-08T00:00:00', 'tier': 'active'}}
        self.assertEqual(chat._fact_stamp(meta, 'a fact'), 'recorded 2026-09-08')

    def test_an_unknown_time_is_never_guessed(self):
        self.assertEqual(chat._fact_stamp({}, 'anything'), 'undated')


class CorrectionVersusConflict(unittest.TestCase):
    """An explicit correction RESOLVES; only a genuine conflict earns a question. Otherwise
    Ace makes Brady re-confirm corrections he already gave."""

    def test_an_explicit_correction_settles_it(self):
        out = chat._group_facts(['x', 'y'], min_facts=99)
        self.assertIn('SETTLES it', out)
        self.assertIn('do not ask him to confirm a correction he already gave', out)

    def test_a_genuine_conflict_asks_one_question(self):
        out = chat._group_facts(['x', 'y'], min_facts=99)
        self.assertIn('two sources that never referred to each other', out)
        self.assertIn('ask one short question', out)
