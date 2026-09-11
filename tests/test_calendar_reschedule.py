"""Provider-free behavior tests: exact identity, no invitations, safe patch/read-back."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend.integrations import calendar_api as cal
from backend import tools, ops

START = '2026-09-15T10:00:00-04:00'
END = '2026-09-15T11:00:00-04:00'

class CalendarReschedule(unittest.TestCase):
    def setUp(self):
        self.before = {'id': 'exact-event', 'etag': 'revision-1', 'organizer': {'self': True},
                       'start': {'dateTime': '2026-09-14T10:00:00-04:00'},
                       'end': {'dateTime': '2026-09-14T11:00:00-04:00'}}
        self.after = {**copy.deepcopy(self.before),
                      'start': {'dateTime': START}, 'end': {'dateTime': END}}
        self.events = Mock()
        self.events.get.return_value.execute.side_effect = [self.before, self.after]
        self.events.patch.return_value.headers = {}
        self.service = Mock()
        self.service.events.return_value = self.events
        self.build = patch.object(cal, 'build', return_value=self.service).start()
        patch.object(cal, 'get_google_creds', return_value=None).start()
        self.addCleanup(patch.stopall)

    def call(self, **kw):
        return cal.reschedule_calendar_event(**({'calendar_id': cal.PFI_CALENDAR_ID,
                   'event_id': 'exact-event', 'start_datetime': START,
                   'end_datetime': END} | kw))

    def test_verified_patch_same_event_no_insert_delete(self):
        self.assertEqual(self.call(), (True, 'exact-event', cal.ADAPTER_COMPLETED))
        args = self.events.patch.call_args.kwargs
        self.assertEqual(args['eventId'], 'exact-event')
        self.assertEqual(args['calendarId'], cal.PFI_CALENDAR_ID)
        self.assertEqual(args['sendUpdates'], 'none')
        self.assertEqual(set(args['body']), {'start', 'end'})
        self.assertEqual(self.events.patch.return_value.headers['If-Match'], 'revision-1')
        self.assertEqual(self.events.get.call_count, 2)
        self.events.insert.assert_not_called()
        self.events.delete.assert_not_called()

    def test_unknown_if_readback_wrong_or_missing_time(self):
        for start in ({'dateTime': '2026-09-16T10:00:00-04:00'}, {}):
            self.events.get.return_value.execute.side_effect = [self.before, {**self.after, 'start': start}]
            self.assertEqual(self.call()[2], cal.ADAPTER_UNKNOWN)

    def test_lost_patch_response_unknown_no_recreate(self):
        self.events.patch.return_value.execute.side_effect = TimeoutError()
        self.assertEqual(self.call()[2], cal.ADAPTER_UNKNOWN)
        self.events.insert.assert_not_called()

    def test_lost_read_response_unknown(self):
        self.events.get.return_value.execute.side_effect = [self.before, TimeoutError()]
        self.assertEqual(self.call()[2], cal.ADAPTER_UNKNOWN)

    def test_repeated_target_converges_without_patch(self):
        self.events.get.return_value.execute.side_effect = [self.after, self.after]
        self.assertEqual(self.call()[2], cal.ADAPTER_COMPLETED)
        self.events.patch.assert_not_called()

    def test_offset_equivalence_verified(self):
        self.after['start'] = {'dateTime': '2026-09-15T14:00:00Z'}
        self.after['end'] = {'dateTime': '2026-09-15T15:00:00Z'}
        self.assertEqual(self.call()[2], cal.ADAPTER_COMPLETED)

    def test_weekday_mismatch_rejected_before_read(self):
        self.assertEqual(self.call(expected_weekday="Wednesday")[2], cal.ADAPTER_FAILED_BEFORE_DISPATCH)
        self.build.assert_not_called()
        self.assertEqual(self.call(expected_weekday="Tuesday")[2], cal.ADAPTER_COMPLETED)

    def test_inputs_reject_before_network(self):
        for kw in ({'calendar_id': 'personal@gmail.com'}, {'calendar_id': 'primary'},
                   {'event_id': ''}, {'start_datetime': '2026-09-15'},
                   {'start_datetime': '2026-09-15T10:00:00'},
                   {'end_datetime': START}, {'end_datetime': '2026-09-14T10:00:00-04:00'}):
            with self.subTest(kw=kw):
                self.assertFalse(self.call(**kw)[0])
        self.build.assert_not_called()

    def test_forbidden_event_kinds_never_write(self):
        for change in ({'attendees': [{'email': 'client@example.com'}]},
                       {'attendeesOmitted': True}, {'recurrence': ['RRULE:FREQ=DAILY']},
                       {'organizer': {'self': False}}, {'etag': ''},
                       {'start': {'date': '2026-09-15'}}, {'status': 'cancelled'},
                       {'id': 'wrong-event'}, {'eventType': 'outOfOffice'}):
            with self.subTest(change=change):
                self.events.get.return_value.execute.side_effect = [{**self.before, **change}]
                self.assertEqual(self.call()[2], cal.ADAPTER_NEEDS_REVIEW)
        self.events.patch.assert_not_called()

    def test_single_recurring_instance_preserves_series_identity(self):
        for event in (self.before, self.after):
            event.update(recurringEventId='series', originalStartTime={'dateTime': '2026-09-14T10:00:00-04:00'})
        self.assertEqual(self.call()[2], cal.ADAPTER_COMPLETED)
        self.assertNotIn('recurrence', self.events.patch.call_args.kwargs['body'])

    def test_wrong_recurring_readback_never_completed(self):
        self.before['recurringEventId'] = 'series'
        self.assertEqual(self.call()[2], cal.ADAPTER_UNKNOWN)

    def test_reads_retain_identity_and_do_not_merge_same_id_across_calendars(self):
        self.service.calendarList.return_value.list.return_value.execute.return_value = {
            'items': [{'id': cal.PFI_CALENDAR_ID, 'summary': 'Business'},
                      {'id': 'other-calendar', 'summary': 'Personal visible'}]}
        event = {**self.before, 'summary': 'Workout', 'recurringEventId': 'series',
                 'originalStartTime': self.before['start']}
        self.events.list.return_value.execute.return_value = {'items': [event]}
        rows = cal.get_events_structured()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['calendar_id'] for r in rows}, {cal.PFI_CALENDAR_ID, 'other-calendar'})
        self.assertTrue(all(r['event_id'] == 'exact-event' and r['recurring_event_id'] == 'series' for r in rows))
        text = cal.get_calendar_range()
        self.assertIn('calendar_id=other-calendar; event_id=exact-event', text)
        self.assertIn('recurring_event_id=series', text)

    def test_tool_returns_journalled_receipt(self):
        with patch.object(tools, 'reschedule_calendar_event', return_value=(True, 'exact-event', 'completed')):
            result = tools._do_reschedule_calendar_event(cal.PFI_CALENDAR_ID, 'exact-event', START, END)
        self.assertEqual(result.state, ops.COMPLETED)
        self.assertEqual(result.record_id, 'exact-event')
        self.assertTrue(result.detail['verified'])
        self.assertIn('reschedule_calendar_event', ops.REQUIRE_JOURNAL)
        self.assertIn('reschedule_calendar_event', ops.JOURNALLED)

if __name__ == '__main__':
    unittest.main()
