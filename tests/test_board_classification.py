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

    def test_needs_a_decision_is_explicit_and_never_derived(self):
        # RELEASE ONE (2026-09-09, Brady): "Make Needs a decision an explicit choice with one
        # authoritative meaning — undated should not automatically mean undecided." Undated
        # work with nothing written down is not a decision he made; it is work he has not
        # looked at, which the review flag below reports without changing its lane.
        self.assertEqual(classify.lane_of(row()), classify.LANE_ANYTIME)
        self.assertEqual(classify.lane_of(row(state='decide')), classify.LANE_UNDECIDED)

    def test_a_deadline_wins_the_lane_but_never_erases_the_mark(self):
        # Both things are true at once: it is due Friday AND he has not decided. Lanes are
        # mutually exclusive, so the deadline takes the lane — losing it would hide a dated
        # row from Overdue. The mark rides alongside instead of competing with it.
        dated = row(state='decide', due_days=3)
        self.assertEqual(classify.lane_of(dated), classify.LANE_UPCOMING)
        self.assertTrue(classify.needs_decision(dated))
        self.assertTrue(classify.decorate(dated)['needs_decision'])
        overdue = row(state='decide', due_days=-2)
        self.assertEqual(classify.lane_of(overdue), classify.LANE_OVERDUE,
                         'a flagged row must still be visibly overdue')
        self.assertTrue(classify.needs_decision(overdue))
        self.assertFalse(classify.needs_decision(row(state='decide', status='done')))
        self.assertFalse(classify.needs_decision(row()))

    def test_the_review_flag_is_separate_from_the_lane(self):
        self.assertTrue(classify.carried_over(row()))
        self.assertFalse(classify.carried_over(row(state='decide')),
                         'a row he marked himself was never "carried over"')
        self.assertFalse(classify.carried_over(row(next_step='call Damon Tuesday')))
        self.assertFalse(classify.carried_over(row(entry='record')))
        self.assertFalse(classify.carried_over(row(status='done')))

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
        self.assertEqual(s['counts'][classify.LANE_ANYTIME], 23,
                         'the old panel showed 6 of 23 with no total')
        self.assertEqual(sum(s['counts'].values()), 23, 'undated work must not go missing')

    def test_marked_decisions_are_counted_on_their_own(self):
        rows = [row(id='a', state='decide'), row(id='b'), row(id='c')]
        s = classify.summarise(rows)
        self.assertEqual(s['counts'][classify.LANE_UNDECIDED], 1)
        self.assertEqual(s['counts'][classify.LANE_ANYTIME], 2)

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


class TwoSurfacesOneRecord(unittest.TestCase):
    """Due Today is compact and narrow; the Command Center is the full board. They read the
    same rows by the same rules, and a suggestion never becomes a schedule."""
    TODAY = '2026-09-08'

    def rows(self):
        return [
            row(id='late', due_days=-2),
            row(id='now', due_days=0),
            row(id='soon', due_days=4),
            row(id='picked', chosen_on=self.TODAY),
            row(id='old1', ts='2026-08-01'),
            row(id='old2', ts='2026-08-02'),
            row(id='parked', state='waiting', waiting_on='the county'),
            row(id='rec', entry='record'),
            row(id='shut', status='done'),
        ]

    def sections(self):
        return classify.due_today_sections(self.rows(), self.TODAY)

    def test_deadlines_are_only_real_deadlines(self):
        ids = [r['id'] for r in self.sections()['deadlines']]
        self.assertEqual(sorted(ids), ['late', 'now'])
        self.assertNotIn('soon', ids, 'a future date is not a deadline for today')

    def test_chosen_is_bradys_own_pick_not_a_due_date(self):
        picked = self.sections()['chosen']
        self.assertEqual([r['id'] for r in picked], ['picked'])
        self.assertIsNone(picked[0].get('due_days'), 'choosing must not create a deadline')

    def test_suggestions_exclude_everything_already_shown(self):
        s = self.sections()
        shown = {r['id'] for r in s['deadlines']} | {r['id'] for r in s['chosen']}
        for r in s['suggested']:
            self.assertNotIn(r['id'], shown, 'a row must not appear twice on one card')

    def test_suggestions_never_include_waiting_or_records(self):
        for r in self.sections()['suggested']:
            self.assertNotEqual(r['lane'], classify.LANE_WAITING)
            self.assertNotEqual(r['lane'], classify.LANE_REFERENCE)

    def test_suggestions_are_capped_but_the_total_is_reported(self):
        many = [row(id='n%d' % i, ts='2026-08-%02d' % (i + 1)) for i in range(12)]
        s = classify.due_today_sections(many, self.TODAY, suggest=5)
        self.assertEqual(len(s['suggested']), 5)
        self.assertEqual(s['suggested_total'], 12, 'the card must say what it is not showing')

    def test_the_card_never_shows_completed_work(self):
        s = self.sections()
        every = s['deadlines'] + s['chosen'] + s['suggested'] + s['waiting']
        self.assertNotIn('shut', [r['id'] for r in every])

    def test_chosen_on_a_different_day_is_not_today(self):
        s = classify.due_today_sections([row(id='x', chosen_on='2026-09-01')], self.TODAY)
        self.assertEqual(s['chosen'], [])

    def test_both_surfaces_agree_on_every_row(self):
        """The Command Center filters on `lane`; Due Today groups from the same decoration."""
        s = self.sections()
        for group in ('deadlines', 'chosen', 'suggested', 'waiting'):
            for r in s[group]:
                self.assertEqual(r['lane'], classify.lane_of(r),
                                 'a card group must not relabel the row it came from')
