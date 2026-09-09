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
        body = [l.strip()[2:].split('   [also names:')[0]
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

    def test_newest_is_last_within_a_group(self):
        out = chat._group_facts(self.FACTS)
        block = out.split('MARLOW (3):')[1].split('\n\n')[0]
        self.assertLess(block.index('Marlow one'), block.index('Marlow three'),
                        'store order is oldest-first, so a correction must land last')

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
        self.assertIn('the LAST line is the most recent', out)


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
