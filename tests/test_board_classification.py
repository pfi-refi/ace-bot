"""One row means one thing, in every view. Synthetic fixtures only."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ace2'))
from backend import classify                       # noqa: E402


def row(**kw):
    base = {'id': 'x1', 'text': 'a thing', 'status': 'open', 'tags': [],
            'entry': 'action', 'state': None, 'due_days': None, 'bucket': 'Personal'}
    base.update(kw)
    return base


class Lanes(unittest.TestCase):
    def test_every_row_lands_in_exactly_one_lane(self):
        rows = [row(id='a', due_days=-3), row(id='b', due_days=0), row(id='c', due_days=5),
                row(id='d', state='waiting'), row(id='e', entry='record'),
                row(id='f'), row(id='g', status='done'), row(id='h', state='settled')]
        s = classify.summarise(rows)
        self.assertEqual(sum(s['counts'].values()), len(rows),
                         'lane counts must account for every row, not double-count or hide')

    def test_overdue_is_distinct_from_today(self):
        self.assertEqual(classify.lane_of(row(due_days=-1)), classify.LANE_OVERDUE)
        self.assertEqual(classify.lane_of(row(due_days=0)), classify.LANE_TODAY)

    def test_undated_action_without_a_next_step_needs_a_decision(self):
        self.assertEqual(classify.lane_of(row()), classify.LANE_UNDECIDED)

    def test_a_recorded_next_step_moves_it_out_of_undecided(self):
        self.assertEqual(classify.lane_of(row(next_step='call Damon Tuesday')),
                         classify.LANE_ANYTIME)

    def test_record_is_reference_not_a_task(self):
        self.assertEqual(classify.lane_of(row(entry='record')), classify.LANE_REFERENCE)

    def test_dated_record_still_surfaces(self):
        """A bill due today genuinely needs him; hiding it repeats the bug this fixes."""
        self.assertEqual(classify.lane_of(row(entry='record', due_days=0)),
                         classify.LANE_TODAY)


class WaitingIsNeverAlsoDue(unittest.TestCase):
    """The defect: app.js dropped SETTLED from the dated lanes but not WAITING, so the same
    row could be completed in one section and protected in another."""

    def test_dated_waiting_row_is_waiting_only(self):
        for dd in (-5, -1, 0, 1, 30):
            with self.subTest(due_days=dd):
                self.assertEqual(classify.lane_of(row(state='waiting', due_days=dd)),
                                 classify.LANE_WAITING)

    def test_waiting_is_never_completable(self):
        self.assertFalse(classify.decorate(row(state='waiting', due_days=-2))['completable'])

    def test_waiting_is_visible_not_hidden(self):
        d = classify.decorate(row(state='waiting'))
        self.assertEqual(d['lane_label'], 'Waiting on someone else')
        self.assertFalse(d['actionable'], 'visible, but not Brady’s work')

    def test_reference_and_done_are_not_completable_either(self):
        self.assertFalse(classify.decorate(row(entry='record'))['completable'])
        self.assertFalse(classify.decorate(row(status='done'))['completable'])

    def test_actionable_rows_still_are(self):
        for r in (row(due_days=-1), row(due_days=0), row(due_days=3), row(),
                  row(next_step='x')):
            self.assertTrue(classify.decorate(r)['completable'])


class AreasAndShelves(unittest.TestCase):
    def test_stored_area_wins(self):
        self.assertEqual(classify.area_of(row(bucket='Groundworks')), 'Groundworks')

    def test_records_get_an_area_too(self):
        """41 of 70 open rows were arealess because db.py fills bucket only for actions."""
        a = classify.area_of(row(entry='record', bucket=None, text='Pay the electric bill'))
        self.assertNotEqual(a, 'Unassigned')

    def test_shelves_cross_areas_without_cloning(self):
        d = classify.decorate(row(bucket='Personal', tags=['Bills']))
        self.assertEqual(d['area'], 'Personal')
        self.assertIn('Bills', d['shelves'])

    def test_a_row_is_never_duplicated_to_appear_twice(self):
        rows = [row(id='only', bucket='Personal', tags=['Bills', 'Goals'])]
        s = classify.summarise(rows)
        self.assertEqual(s['total'], 1)
        self.assertEqual(sum(s['areas'].values()), 1)


class NothingIsSilentlyHidden(unittest.TestCase):
    def test_undated_work_is_counted_in_full(self):
        rows = [row(id=str(i)) for i in range(23)]
        s = classify.summarise(rows)
        self.assertEqual(s['counts'][classify.LANE_UNDECIDED], 23,
                         'the old panel showed 6 of 23 with no total')

    def test_needs_you_excludes_waiting_and_reference(self):
        rows = [row(id='a', due_days=0), row(id='b', state='waiting'),
                row(id='c', entry='record')]
        self.assertEqual(classify.summarise(rows)['needs_you'], 1)

    def test_open_count_excludes_completed_only(self):
        rows = [row(id='a'), row(id='b', status='done'), row(id='c', state='settled')]
        s = classify.summarise(rows)
        self.assertEqual(s['open'], 2, 'settled stays on the board; done does not')


class ExplicitOverridesSurvive(unittest.TestCase):
    def test_stored_state_beats_any_derivation(self):
        self.assertEqual(classify.lane_of(row(state='settled', due_days=-9)),
                         classify.LANE_SETTLED)

    def test_nothing_is_derived_from_prose_here(self):
        """classify reads decisions db.py already made; it must not re-read the text."""
        src = (ROOT / 'ace2' / 'backend' / 'classify.py').read_text()
        for token in ('_WAIT_RE', 're.compile', 'lower()'):
            self.assertNotIn(token, src, f'{token} would mean classify re-derives meaning')


if __name__ == '__main__':
    unittest.main()


class StoredAreaSurvivesOnRecords(unittest.TestCase):
    """db.py discarded a stored bucket whenever entry == 'record', so setting the area on a
    record silently reverted to a keyword guess."""

    def test_record_keeps_the_area_it_was_given(self):
        self.assertEqual(classify.area_of(row(entry='record', bucket='Side Work')), 'Side Work')

    def test_action_keeps_the_area_it_was_given(self):
        self.assertEqual(classify.area_of(row(entry='action', bucket='Groundworks')),
                         'Groundworks')

    def test_only_an_unset_area_is_derived(self):
        derived = classify.area_of(row(entry='record', bucket=None, text='pour the driveway'))
        self.assertTrue(derived)
        self.assertNotEqual(derived, 'Unassigned')
