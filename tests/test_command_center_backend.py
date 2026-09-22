"""Pure-function contract behind the Command Center backend fixes (2026-09-22).

No database. The end-to-end behaviour — routes, row locks, readback — lives in
tests/command_center_backend_check.py against a disposable PostgreSQL. These tests pin the
parts that hold with a plain dict: the completion hold a parent wears, the fallback count
when a caller decorates rows the store did not count, and the due-clear read rule.
"""
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ace2'))
from backend import classify, db            # noqa: E402


def item(**kw):
    base = dict(id='p1', status='open', entry='action', state=None, text='get the permit',
                tags=['Business'], bucket='Side Work', due=None, due_days=None)
    base.update(kw)
    return base


class ParentCompletionHold(unittest.TestCase):
    def test_open_children_make_an_actionable_row_not_completable(self):
        d = classify.decorate(item(open_children=2))
        self.assertEqual(classify.LANE_ANYTIME, d['lane'], 'the hold must not change the lane')
        self.assertTrue(d['actionable'], 'a parent is still work')
        self.assertFalse(d['completable'])
        self.assertEqual('children', d['completion_hold'])
        self.assertEqual(2, d['open_children'])

    def test_no_children_leaves_the_row_completable(self):
        d = classify.decorate(item(open_children=0))
        self.assertTrue(d['completable'])
        self.assertEqual('', d['completion_hold'])
        d = classify.decorate(item())            # key absent: a row the store did not count
        self.assertTrue(d['completable'])
        self.assertEqual(0, d['open_children'])

    def test_the_lane_hold_is_named_before_children(self):
        d = classify.decorate(item(state='waiting', waiting_on='Tony', open_children=1))
        self.assertEqual('waiting', d['completion_hold'])
        self.assertFalse(d['completable'])
        d = classify.decorate(item(entry='record', open_children=1))
        self.assertEqual('reference', d['completion_hold'])
        d = classify.decorate(item(status='done', open_children=1))
        self.assertEqual('done', d['completion_hold'])
        d = classify.decorate(item(entry='record', state='settled'))
        self.assertEqual('settled', d['completion_hold'])

    def test_a_dated_parent_stays_in_its_dated_lane_but_holds(self):
        d = classify.decorate(item(due_days=0, open_children=1))
        self.assertEqual(classify.LANE_TODAY, d['lane'])
        self.assertFalse(d['completable'])
        self.assertEqual('children', d['completion_hold'])

    def test_with_children_counts_open_rows_only_and_keeps_store_counts(self):
        rows = [item(id='p'), item(id='c1', parent_id='p'), item(id='c2', parent_id='p'),
                item(id='c3', parent_id='p', status='done'), item(id='q', open_children=7)]
        out = {r['id']: r for r in classify.with_children(rows)}
        self.assertEqual(2, out['p']['open_children'])
        self.assertEqual(0, out['c1']['open_children'])
        self.assertEqual(7, out['q']['open_children'], 'a count the store filled in must win')
        self.assertNotIn('open_children', rows[0], 'the input rows must not be mutated')

    def test_summaries_and_today_carry_the_hold(self):
        rows = [item(id='p', due_days=0), item(id='c', parent_id='p')]
        s = classify.summarise(rows)
        self.assertEqual(2, s['needs_you'], 'the hold is not a lane and removes nothing')
        dt = classify.due_today_sections(rows, date.today().isoformat())
        parent = next(r for r in dt['deadlines'] if r['id'] == 'p')
        self.assertFalse(parent['completable'])
        self.assertEqual('children', parent['completion_hold'])
        self.assertEqual(1, parent['open_children'])


class DueClearReadRule(unittest.TestCase):
    TODAY = date(2026, 9, 22)

    def test_clear_suppresses_derived_dates_until_an_explicit_date_edit(self):
        self.assertIsNone(db.effective_due('pay by 2026-10-02', None, '2026-10-02', self.TODAY))
        self.assertIsNone(db.effective_due('pay by 2026-10-12', None, '2026-10-02', self.TODAY))
        self.assertEqual(date(2026, 10, 2),
                         db.effective_due('pay by 2026-10-02', None, None, self.TODAY))

    def test_relative_clear_survives_month_rollover_and_unrelated_text_edit(self):
        self.assertIsNone(db.effective_due('Mock permit due 25th', None, '2026-09-25', date(2026, 10, 2)))
        before = {'text': 'Mock permit due 25th', 'due': None, 'due_cleared': '2026-08-25'}
        result = db._expected_item_values(before, {'text': 'Mock permit (county) due 25th'})
        self.assertEqual('2026-08-25', result['due_cleared'])

    def test_a_stored_field_is_never_suppressed(self):
        self.assertEqual(date(2026, 10, 2),
                         db.effective_due('pay by 2026-10-02', '2026-10-02', '2026-10-02', self.TODAY))

    def test_text_due_reads_only_the_wording(self):
        self.assertEqual(date(2026, 10, 2), db.text_due('pay by 2026-10-02', self.TODAY))
        self.assertIsNone(db.text_due('pay the bill', self.TODAY))

    def test_oracle_predicts_the_marker_the_way_the_writer_writes_it(self):
        before = {'text': 'pay by 2026-10-02', 'due': None, 'due_cleared': None}
        exp = db._expected_item_values(before, {'due': ''})
        self.assertIsNone(exp['due'])
        self.assertEqual('2026-10-02', exp['due_cleared'])
        exp = db._expected_item_values({**before, 'due_cleared': '2026-10-02'}, {'due': '2026-11-01'})
        self.assertIsNone(exp['due_cleared'], 'a set date drops the marker')
        exp = db._expected_item_values({**before, 'due_cleared': '2026-10-02'},
                                       {'text': 'pay by 2026-10-12'})
        self.assertIsNone(exp['due_cleared'], 'a new date in the wording lapses the clear')
        exp = db._expected_item_values({**before, 'due_cleared': '2026-10-02'},
                                       {'text': 'pay (township) by 2026-10-02'})
        self.assertEqual('2026-10-02', exp['due_cleared'], 'same date reworded keeps the clear')
        exp = db._expected_item_values({**before, 'due_cleared': '2026-10-02'}, {'next_step': 'x'})
        self.assertEqual('2026-10-02', exp['due_cleared'], 'an unrelated edit leaves it alone')


if __name__ == '__main__':
    unittest.main(verbosity=2)
