"""Write-lifecycle regressions: a superseded voice turn must not lose or duplicate a write.

These cover the logic that does not need a database. Real journal semantics against a
disposable PostgreSQL live in tests/write_reliability_check.py.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ace2'))
from backend import ops, tools                     # noqa: E402
from backend.integrations import calendar_api      # noqa: E402


class OpIdentity(unittest.TestCase):
    def test_same_request_same_key(self):
        a = ops.op_key('capture_item', {'text': 'Mail Rebecca packet'})
        b = ops.op_key('capture_item', {'text': 'Mail Rebecca packet'})
        self.assertEqual(a, b)

    def test_whitespace_and_case_reflush_is_the_same_key(self):
        """ElevenLabs re-sends a re-cased/re-spaced transcript; that is one write."""
        a = ops.op_key('capture_item', {'text': 'Mail Rebecca packet'})
        b = ops.op_key('capture_item', {'text': '  mail   Rebecca  packet '})
        self.assertEqual(a, b)

    def test_key_order_does_not_matter(self):
        a = ops.op_key('create_calendar_event', {'title': 'Ken', 'date': '2026-09-09'})
        b = ops.op_key('create_calendar_event', {'date': '2026-09-09', 'title': 'Ken'})
        self.assertEqual(a, b)

    def test_different_detail_is_a_different_write(self):
        a = ops.op_key('create_calendar_event', {'title': 'Ken', 'time': '10:00'})
        b = ops.op_key('create_calendar_event', {'title': 'Ken', 'time': '15:00'})
        self.assertNotEqual(a, b)

    def test_gated_tools_are_not_journalled_here(self):
        """send_email / delete_calendar_event are durable via the Review tray already."""
        self.assertNotIn('send_email', ops.JOURNALLED)
        self.assertNotIn('delete_calendar_event', ops.JOURNALLED)
        self.assertIn('create_calendar_event', ops.JOURNALLED)


class CalendarIdempotency(unittest.TestCase):
    def test_same_title_and_start_yields_same_id(self):
        s = {'dateTime': '2026-09-09T15:00:00-04:00'}
        self.assertEqual(calendar_api._deterministic_event_id('Ken Weinberg', s),
                         calendar_api._deterministic_event_id(' ken   weinberg ', s))

    def test_different_start_yields_different_id(self):
        a = calendar_api._deterministic_event_id('Ken', {'dateTime': '2026-09-08T10:00:00-04:00'})
        b = calendar_api._deterministic_event_id('Ken', {'dateTime': '2026-09-09T15:00:00-04:00'})
        self.assertNotEqual(a, b)

    def test_id_is_legal_for_google(self):
        """base32hex only (a-v, 0-9), length 5-1024 — an illegal id fails the insert."""
        i = calendar_api._deterministic_event_id('Grandpa coffee ☕', {'date': '2026-09-12'})
        self.assertTrue(5 <= len(i) <= 1024)
        self.assertTrue(all(c in '0123456789abcdefghijklmnopqrstuv' for c in i), i)

    def test_duplicate_insert_is_recognised(self):
        class Resp:
            status = 409
        class Err(Exception):
            resp = Resp()
        self.assertTrue(calendar_api._is_duplicate_id_error(Err('duplicate')))
        self.assertFalse(calendar_api._is_duplicate_id_error(Exception('network down')))


class CaptureReviewMessage(unittest.TestCase):
    """The 'needs review' outcome must name the item and the two ways to resolve it."""
    def render(self, **over):
        payload = {'needs_review': True, 'existing_id': 'abc123',
                   'existing_text': 'Capital One minimum $80', 'existing_status': 'open',
                   'existing_due': '2026-09-08', 'requested_text': 'Capital One minimum $106',
                   'requested_due': '2026-09-08'}
        payload.update(over)
        with patch.object(tools.daybank, 'add_item', lambda *a, **k: (False, payload)):
            self.outcome = tools._do_capture_item(text=payload['requested_text'])
        return str(self.outcome)

    def test_says_it_was_not_saved(self):
        self.assertIn('NOT SAVED', self.render())

    def test_state_is_needs_review_not_completed(self):
        """Codex's finding: this message does not start with the warning glyph, so a
        prose-prefix classifier recorded it as a COMPLETED action and the planning
        context then listed it as done work that had never been saved."""
        self.render()
        self.assertEqual(self.outcome.state, ops.NEEDS_REVIEW)
        self.assertNotEqual(self.outcome.state, ops.COMPLETED)

    def test_classify_agrees(self):
        self.render()
        self.assertEqual(ops.classify(self.outcome)[0], ops.NEEDS_REVIEW)

    def test_is_not_worded_as_a_failure(self):
        self.assertNotIn('Could not capture', self.render())

    def test_gives_the_real_id_for_both_routes(self):
        out = self.render()
        self.assertIn('abc123', out)
        self.assertIn('update_item', out)
        self.assertIn('capture_item', out)

    def test_shows_both_versions_so_the_difference_is_visible(self):
        out = self.render()
        self.assertIn('$80', out)
        self.assertIn('$106', out)

    def test_does_not_claim_success(self):
        self.assertIn('do not report this as added', self.render().lower())


class DispatchLifecycle(unittest.IsolatedAsyncioTestCase):
    """_dispatch_write with the journal mocked: verdicts, and the cancellation contract."""
    def setUp(self):
        from backend import chat
        self.chat = chat
        self.settled = []
        self.ran = []

    def _tool(self, delay=0.0, result='◆ Added to your board: x'):
        def run(name, args):
            import time
            if delay:
                time.sleep(delay)
            self.ran.append((name, args))
            return result
        return run

    async def _dispatch(self, verdict, prior=None, tool=None, name='capture_item'):
        with patch.object(self.chat.ops, 'begin', lambda n, a, s='': (verdict, 'k1', prior)), \
             patch.object(self.chat.ops, 'settle',
                          lambda k, st, r='', e='': self.settled.append((k, st, r))), \
             patch.object(self.chat.tools, 'execute', tool or self._tool()):
            return await self.chat._dispatch_write(name, {'text': 'x'})

    async def test_legacy_prose_settles_reported_not_completed(self):
        """An executor that only returns a sentence cannot prove it did anything."""
        out = await self._dispatch('execute')
        self.assertIn('Added', out)
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(self.settled[0][1], ops.REPORTED)

    async def test_structured_success_settles_completed(self):
        tool = lambda n, a: ops.Outcome(ops.COMPLETED, '◆ Added: Ken', record_id='evt_1')
        out = await self._dispatch('execute', tool=tool)
        self.assertIn('Ken', out)
        self.assertEqual(self.settled[0][1], ops.COMPLETED)

    async def test_unsaved_capture_never_settles_completed(self):
        """The release blocker, end to end through the dispatcher."""
        tool = lambda n, a: ops.Outcome(ops.NEEDS_REVIEW, '◆ NOT SAVED — needs your call')
        await self._dispatch('execute', tool=tool)
        self.assertEqual(self.settled[0][1], ops.NEEDS_REVIEW)

    async def test_unavailable_journal_refuses_to_execute(self):
        out = await self._dispatch('unavailable')
        self.assertEqual(self.ran, [], 'nothing may run without write ownership')
        self.assertIn('NOT DONE', out)
        self.assertIn('do not claim it happened', out.lower())

    async def test_retry_returns_prior_receipt_without_re_executing(self):
        """A redelivered request produces ONE write, and reports the original receipt."""
        out = await self._dispatch('duplicate', prior='◆ Added to your board: Ken 3pm')
        self.assertIn('Ken 3pm', out)
        self.assertEqual(self.ran, [])

    async def test_in_flight_tells_the_model_to_stop(self):
        out = await self._dispatch('in_flight')
        self.assertIn('STOP', out)
        self.assertEqual(self.ran, [])

    async def test_unknown_outcome_is_never_replayed(self):
        out = await self._dispatch('unknown', prior='Previous attempt was interrupted.')
        self.assertIn('interrupted', out.lower())
        self.assertEqual(self.ran, [], 'an unknown outcome must NOT re-execute')

    async def test_warning_prose_settles_failed_before_dispatch(self):
        await self._dispatch('execute', tool=self._tool(result='⚠️ Calendar create error'))
        self.assertEqual(self.settled[0][1], ops.FAILED_BEFORE_DISPATCH)

    async def test_exception_on_external_write_is_unknown_not_failed(self):
        """The provider may have committed before the response was lost, so this is not
        a safe-to-retry failure."""
        def boom(n, a):
            raise TimeoutError('connection reset')
        with self.assertRaises(TimeoutError):
            await self._dispatch('execute', tool=boom, name='create_calendar_event')
        self.assertEqual(self.settled[-1][1], ops.UNKNOWN)
        self.assertIn('may already have gone through', self.settled[-1][2])

    async def test_exception_on_local_write_is_safe_to_retry(self):
        def boom(n, a):
            raise ValueError('bad row')
        with self.assertRaises(ValueError):
            await self._dispatch('execute', tool=boom, name='capture_item')
        self.assertEqual(self.settled[-1][1], ops.FAILED_BEFORE_DISPATCH)

    async def test_cancellation_mid_dispatch_still_completes_and_settles(self):
        """THE core guarantee. Cancelling the await cannot stop the thread, so the write
        must finish and be recorded — not silently vanish into an unrecorded maybe."""
        import threading
        started = threading.Event()

        def slow(name, args):
            import time
            started.set()
            time.sleep(0.25)
            self.ran.append((name, args))
            return '◆ Added to your board: slow'

        # Patches are held HERE, not inside the cancelled coroutine: the settle lookup
        # happens after the tool returns, which is after cancellation, so a `with` block
        # inside the coroutine would unpatch before the write finished.
        patches = [patch.object(self.chat.ops, 'begin', lambda n, a, s='': ('execute', 'k9', None)),
                   patch.object(self.chat.ops, 'settle',
                                lambda k, st, r='', e='': self.settled.append((k, st, r))),
                   patch.object(self.chat.tools, 'execute', slow)]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])

        task = asyncio.create_task(self.chat._dispatch_write('capture_item', {'text': 'slow'}))
        self.assertTrue(await asyncio.to_thread(started.wait, 2), 'tool never started')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.6)                      # let the shielded write land
        self.assertEqual(len(self.ran), 1, 'the write must still complete')
        self.assertEqual(self.settled[-1][1], ops.REPORTED,
                         'the outcome must be durably recorded despite cancellation')

    async def test_unjournalled_tool_passes_straight_through(self):
        out = await self._dispatch('execute', name='search_gmail')
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(self.settled, [], 'reads are not journalled')


if __name__ == '__main__':
    unittest.main()


class BridgeLease(unittest.TestCase):
    """A bridge claim must expire if the worker never acknowledges."""
    def setUp(self):
        from backend import chat
        self.chat = chat
        chat._bridge_leases.clear()
        self.addCleanup(chat._bridge_leases.clear)

    def test_fresh_lease_holds_the_job(self):
        self.chat.bridge_lease_take('brief:morning')
        self.assertTrue(self.chat.bridge_lease_active('brief:morning'))

    def test_unrelated_job_is_unaffected(self):
        self.chat.bridge_lease_take('brief:morning')
        self.assertFalse(self.chat.bridge_lease_active('brief:eod'))

    def test_expired_lease_releases_the_job_back(self):
        """The failure that lost 52 sweeps: worker dies holding the claim forever."""
        import time
        self.chat.bridge_lease_take('sweep')
        self.chat._bridge_leases['sweep']['until'] = time.time() - 1
        self.assertFalse(self.chat.bridge_lease_active('sweep'),
                         'an unacknowledged claim must lapse so the server can take over')

    def test_lease_issues_an_owner_token(self):
        token = self.chat.bridge_lease_take('brief:morning')
        self.assertTrue(token)
        self.assertEqual(self.chat.bridge_lease_token('brief:morning'), token)

    def test_expired_lease_has_no_owner(self):
        import time
        self.chat.bridge_lease_take('brief:eod')
        self.chat._bridge_leases['brief:eod']['until'] = time.time() - 1
        self.assertEqual(self.chat.bridge_lease_token('brief:eod'), '')

    def test_release_on_acknowledgement(self):
        self.chat.bridge_lease_take('brief:eod')
        self.chat.bridge_lease_release('brief:eod')
        self.assertFalse(self.chat.bridge_lease_active('brief:eod'))


class BridgeWorkerDelivery(unittest.TestCase):
    """The model call is expensive; only the POST may be retried, and never dropped."""
    def setUp(self):
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location('bw', ROOT / 'ops' / 'bridge_worker.py')
        self.bw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bw)
        self.tmp = tempfile.mkdtemp(prefix='ace-bridge-test-')
        self.bw.PENDING = str(Path(self.tmp) / 'pending')
        self.bw.LOG = str(Path(self.tmp) / 'bridge.log')
        self.calls = []

    def test_successful_post_is_not_retried(self):
        with patch.object(self.bw, 'api', lambda cfg, path, body=None: self.calls.append(path) or {'ok': True}), \
             patch.object(self.bw.time, 'sleep', lambda s: None):
            out = self.bw.post_result({}, {'job': 'brief', 'text': 'x'})
        self.assertEqual(out, {'ok': True})
        self.assertEqual(len(self.calls), 1)

    def test_transient_failure_retries_delivery_only(self):
        state = {'n': 0}

        def flaky(cfg, path, body=None):
            state['n'] += 1
            if state['n'] < 3:
                raise TimeoutError('The read operation timed out')
            return {'ok': True}

        with patch.object(self.bw, 'api', flaky), patch.object(self.bw.time, 'sleep', lambda s: None):
            out = self.bw.post_result({}, {'job': 'sweep'})
        self.assertEqual(out, {'ok': True})
        self.assertEqual(state['n'], 3, 'should retry the POST, not give up')

    def test_undeliverable_result_is_parked_not_lost(self):
        def dead(cfg, path, body=None):
            raise TimeoutError('The read operation timed out')

        with patch.object(self.bw, 'api', dead), patch.object(self.bw.time, 'sleep', lambda s: None):
            out = self.bw.post_result({}, {'job': 'brief', 'kind': 'morning', 'text': 'done work'})
        self.assertTrue(out.get('parked'))
        files = list(Path(self.bw.PENDING).glob('*.json'))
        self.assertEqual(len(files), 1, 'finished model work must survive a failed POST')
        import json as _j
        self.assertEqual(_j.loads(files[0].read_text())['text'], 'done work')

    def test_parked_result_is_delivered_on_the_next_run(self):
        Path(self.bw.PENDING).mkdir(parents=True, exist_ok=True)
        (Path(self.bw.PENDING) / '20260908T090000-brief.json').write_text(
            '{"job": "brief", "kind": "morning", "text": "recovered"}')
        sent = []
        with patch.object(self.bw, 'api', lambda cfg, path, body=None: sent.append(body) or {'ok': True}):
            self.bw.flush_pending({})
        self.assertEqual(sent[0]['text'], 'recovered')
        self.assertEqual(list(Path(self.bw.PENDING).glob('*.json')), [])

    def test_flush_stops_when_server_is_still_down(self):
        Path(self.bw.PENDING).mkdir(parents=True, exist_ok=True)
        (Path(self.bw.PENDING) / '20260908T090000-brief.json').write_text('{"job": "brief"}')
        def dead(cfg, path, body=None):
            raise TimeoutError('down')
        with patch.object(self.bw, 'api', dead):
            self.bw.flush_pending({})
        self.assertEqual(len(list(Path(self.bw.PENDING).glob('*.json'))), 1,
                         'pending work must be preserved, not discarded')


class CalendarLogVolume(unittest.TestCase):
    """Per-event filter logging drowned the Railway window and leaked event titles."""
    def setUp(self):
        calendar_api._drop_tally.clear()

    def test_drops_are_counted_not_logged_individually(self):
        with self.assertLogs('ace_portal.calendar', level='INFO') as cm:
            for _ in range(50):
                calendar_api._event_dropped('BPM meeting')
            calendar_api._event_dropped('Base Shop huddle')
            calendar_api.log_filter_summary('3 kept')
        self.assertEqual(len(cm.output), 1, f'expected one summary line, got {len(cm.output)}')
        self.assertIn('51', cm.output[0])

    def test_summary_carries_no_event_titles(self):
        with self.assertLogs('ace_portal.calendar', level='INFO') as cm:
            calendar_api._event_dropped('Private therapy appointment bpm')
            calendar_api.log_filter_summary()
        self.assertNotIn('therapy', cm.output[0].lower())

    def test_filtering_itself_is_unchanged(self):
        self.assertTrue(calendar_api._event_dropped('BPM meeting'))
        self.assertFalse(calendar_api._event_dropped('Interview with Tyler'),
                         'the 2026-08-11 fix must hold: real interviews are not filtered')
        self.assertFalse(calendar_api._event_dropped('Ken Weinberg tax review'))


class BillsFromSheet(unittest.TestCase):
    """Money comes from the sheet. The row that nearly got a disconnection missed the
    brief entirely on 6 Sept because it has no Due Day — regression-locked here."""
    HDR = ['Bill / Expense', 'Due Day', 'Monthly Amount', 'Paid?', 'Paid From', 'Notes']

    def bills(self):
        from datetime import date
        from backend.integrations import bills_sheet as bs
        rows = [self.HDR,
                ['Electric', '14th', '$364.00', '', 'Chase', 'EPP under-collects'],
                ['Capital One', '8th', '$106.00', '', 'Chase', 'Minimum raised from $80 to $106'],
                ['ONE-TIME / NOT MONTHLY', '', '', '', '', ''],
                ['Electric - stop disconnection', '', '$247.79', '', '',
                 'MINIMUM by Sept 14. Reconnection fee is $35.']]
        return bs, rows, date(2026, 9, 8)

    def test_one_time_obligation_reaches_the_money_block(self):
        bs, rows, today = self.bills()
        block = bs.format_due_soon(bs.parse_bills(rows, today), 10, today)
        self.assertIn('247.79', block, 'a dated obligation with no Due Day must still surface')

    def test_notes_derived_date_is_labelled_not_asserted(self):
        bs, rows, today = self.bills()
        block = bs.format_due_soon(bs.parse_bills(rows, today), 10, today)
        self.assertIn('from Notes', block)

    def test_sheet_amount_is_used_verbatim(self):
        bs, rows, today = self.bills()
        block = bs.format_due_soon(bs.parse_bills(rows, today), 10, today)
        self.assertIn('$106.00', block)
        self.assertNotIn('$80', block.split('[')[0], 'the stale board figure must not appear')

    def test_unreadable_sheet_refuses_to_guess(self):
        bs, _, _ = self.bills()
        self.assertEqual(bs.parse_bills([['no', 'header', 'here']]), [])


class WorkerAcknowledgement(unittest.TestCase):
    """HTTP 200 is not an acknowledgement. {ok:false} must not destroy finished work."""
    def setUp(self):
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location('bw2', ROOT / 'ops' / 'bridge_worker.py')
        self.bw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bw)
        self.tmp = tempfile.mkdtemp(prefix='ace-ack-test-')
        self.bw.PENDING = str(Path(self.tmp) / 'pending')
        self.bw.LOG = str(Path(self.tmp) / 'bridge.log')

    def park(self, body='{"job": "brief", "kind": "morning", "text": "work"}'):
        Path(self.bw.PENDING).mkdir(parents=True, exist_ok=True)
        (Path(self.bw.PENDING) / '20260908T090000-brief.json').write_text(body)

    def parked(self):
        return list(Path(self.bw.PENDING).glob('*.json'))

    def test_negative_ack_is_retried_not_treated_as_delivered(self):
        seen = []

        def declining(cfg, path, body=None):
            seen.append(1)
            return {'ok': False, 'reason': 'this result is already being applied', 'retry': True}

        with patch.object(self.bw, 'api', declining), patch.object(self.bw.time, 'sleep', lambda s: None):
            self.bw.post_result({}, {'job': 'brief', 'text': 'work'})
        self.assertGreater(len(seen), 1, 'a declined result must be retried')
        self.assertEqual(len(self.parked()), 1, 'and then parked, not dropped')

    def test_idempotent_ack_counts_as_delivered(self):
        with patch.object(self.bw, 'api', lambda c, p, body=None: {'ok': True, 'idempotent': True}), \
             patch.object(self.bw.time, 'sleep', lambda s: None):
            out = self.bw.post_result({}, {'job': 'brief'})
        self.assertTrue(out['ok'])
        self.assertEqual(self.parked(), [])

    def test_stale_period_result_is_discarded_not_retried_forever(self):
        self.park()
        with patch.object(self.bw, 'api', lambda c, p, body=None: {'ok': False, 'stale': True}):
            self.bw.flush_pending({})
        self.assertEqual(self.parked(), [], 'a result for a passed period can never succeed')

    def test_already_delivered_result_is_discarded(self):
        self.park()
        with patch.object(self.bw, 'api', lambda c, p, body=None: {'ok': False, 'late': True}):
            self.bw.flush_pending({})
        self.assertEqual(self.parked(), [])

    def test_recoverable_decline_keeps_the_parked_result(self):
        self.park()
        with patch.object(self.bw, 'api', lambda c, p, body=None: {'ok': False, 'retry': True,
                                                                   'reason': 'busy'}):
            self.bw.flush_pending({})
        self.assertEqual(len(self.parked()), 1, 'finished work must survive a recoverable decline')


class CalendarConflictVerification(unittest.TestCase):
    """A 409 says an id exists, not that it holds what we asked for."""
    def test_changed_end_time_is_a_difference(self):
        w = {'summary': 'Ken', 'start': {'dateTime': '2026-09-09T15:00:00-04:00'},
             'end': {'dateTime': '2026-09-09T16:00:00-04:00'}}
        e = {'summary': 'Ken', 'start': {'dateTime': '2026-09-09T15:00:00-04:00'},
             'end': {'dateTime': '2026-09-09T17:00:00-04:00'}}
        self.assertIn('end', calendar_api._event_differences(w, e))

    def test_changed_description_is_a_difference(self):
        w = {'summary': 'Ken', 'start': {'dateTime': '2026-09-09T15:00:00-04:00'},
             'description': 'bring transcripts'}
        e = {'summary': 'Ken', 'start': {'dateTime': '2026-09-09T15:00:00-04:00'},
             'description': ''}
        self.assertIn('description', calendar_api._event_differences(w, e))

    def test_equivalent_timezone_spellings_converge(self):
        w = {'summary': 'Ken', 'start': {'dateTime': '2026-09-09T15:00:00-04:00'},
             'end': {'dateTime': '2026-09-09T16:00:00-04:00'}}
        e = {'summary': 'Ken', 'start': {'dateTime': '2026-09-09T19:00:00Z'},
             'end': {'dateTime': '2026-09-09T20:00:00Z'}}
        self.assertEqual(calendar_api._event_differences(w, e), {},
                         'the same instant written two ways is not a conflict')

    def test_untouched_provider_fields_are_not_conflicts(self):
        w = {'summary': 'Ken', 'start': {'dateTime': '2026-09-09T15:00:00-04:00'}}
        e = {'summary': 'Ken', 'start': {'dateTime': '2026-09-09T15:00:00-04:00'},
             'description': 'auto-added by Google', 'etag': 'x', 'htmlLink': 'y'}
        self.assertEqual(calendar_api._event_differences(w, e), {})

    def test_all_day_dates_compare(self):
        w = {'summary': 'Holiday', 'start': {'date': '2026-09-07'}}
        e = {'summary': 'Holiday', 'start': {'date': '2026-09-07'}}
        self.assertEqual(calendar_api._event_differences(w, e), {})


class WriteCoverageBoundary(unittest.TestCase):
    """Not every write is journalled. The uncovered set must be visible, not assumed empty."""
    GATED = {'send_email', 'mcp_send_gmail_message', 'delete_calendar_event'}

    def test_gated_mcp_writes_are_not_reported_as_gaps(self):
        self.assertEqual(ops.uncovered_mcp_writes(['mcp_send_gmail_message'], self.GATED), [])

    def test_reads_are_not_gaps(self):
        self.assertEqual(
            ops.uncovered_mcp_writes(['mcp_search_gmail', 'mcp_list_events', 'mcp_get_file'],
                                     self.GATED), [])

    def test_an_ungated_mcp_write_is_surfaced(self):
        gaps = ops.uncovered_mcp_writes(
            ['mcp_create_drive_file', 'mcp_update_sheet_values', 'mcp_search_drive'], self.GATED)
        self.assertEqual(gaps, ['mcp_create_drive_file', 'mcp_update_sheet_values'])

    def test_journalled_local_tools_are_not_gaps(self):
        self.assertEqual(ops.uncovered_mcp_writes(['create_calendar_event'], self.GATED), [])


class CalendarCreatorThroughTheRealFunction(unittest.TestCase):
    """Through create_calendar_event with a mocked Google service — not the helpers.
    Helper-only fixtures missed the pre-insert probe twice."""
    EXISTING = {'id': 'fixture-event', 'summary': 'Fixture meeting',
                'start': {'dateTime': '2026-09-09T15:00:00-04:00'},
                'end': {'dateTime': '2026-09-09T16:00:00-04:00'},
                'description': 'old details'}

    def service(self, items):
        from unittest.mock import Mock
        svc = Mock()
        svc.events.return_value.list.return_value.execute.return_value = {'items': items}
        svc.events.return_value.insert.return_value.execute.return_value = {'id': 'new-event'}
        return svc

    def create(self, items, **kw):
        from unittest.mock import Mock
        svc = self.service(items)
        args = dict(title='Fixture meeting', date_str='2026-09-09', time_str='15:00',
                    duration_minutes=60, description='old details')
        args.update(kw)
        with patch.object(calendar_api, 'get_google_creds'), \
             patch.object(calendar_api, 'build', return_value=svc):
            return calendar_api.create_calendar_event(**args), svc

    def test_changed_duration_is_not_reported_as_success(self):
        (ok, msg, state), svc = self.create([self.EXISTING], duration_minutes=120)
        self.assertFalse(ok)
        self.assertEqual(state, calendar_api.ADAPTER_NEEDS_REVIEW)
        self.assertIn('end', msg)
        self.assertFalse(svc.events.return_value.insert.called, 'nothing may be written')

    def test_changed_description_is_not_reported_as_success(self):
        (ok, msg, state), _ = self.create([self.EXISTING], description='changed details')
        self.assertFalse(ok)
        self.assertEqual(state, calendar_api.ADAPTER_NEEDS_REVIEW)
        self.assertIn('description', msg)

    def test_identical_request_converges_without_inserting(self):
        (ok, ident, state), svc = self.create([self.EXISTING])
        self.assertTrue(ok)
        self.assertEqual(state, calendar_api.ADAPTER_COMPLETED)
        self.assertEqual(ident, 'fixture-event')
        self.assertFalse(svc.events.return_value.insert.called)

    def test_empty_calendar_inserts_once(self):
        (ok, ident, state), svc = self.create([])
        self.assertTrue(ok)
        self.assertEqual(state, calendar_api.ADAPTER_COMPLETED)
        self.assertTrue(svc.events.return_value.insert.called)

    def test_error_after_dispatch_is_unknown_not_retryable(self):
        from unittest.mock import Mock
        svc = self.service([])
        svc.events.return_value.insert.return_value.execute.side_effect = \
            TimeoutError('response lost')
        with patch.object(calendar_api, 'get_google_creds'), \
             patch.object(calendar_api, 'build', return_value=svc):
            ok, msg, state = calendar_api.create_calendar_event(
                title='Fixture meeting', date_str='2026-09-09', time_str='15:00')
        self.assertFalse(ok)
        self.assertEqual(state, calendar_api.ADAPTER_UNKNOWN)
        self.assertIn('may exist', msg)

    def test_error_before_dispatch_is_safe_to_retry(self):
        with patch.object(calendar_api, 'get_google_creds', side_effect=RuntimeError('no creds')):
            ok, msg, state = calendar_api.create_calendar_event(
                title='Fixture meeting', date_str='2026-09-09', time_str='15:00')
        self.assertFalse(ok)
        self.assertEqual(state, calendar_api.ADAPTER_FAILED_BEFORE_DISPATCH)


class CalendarExecutorBoundary(unittest.TestCase):
    """The executor/dispatcher boundary: adapters that RETURN errors, not raise them."""
    def run_executor(self, adapter_return):
        with patch.object(tools, 'create_calendar_event', return_value=adapter_return):
            return tools._do_create_calendar_event('Fixture', '2026-09-09T15:00:00-04:00')

    def test_post_dispatch_uncertainty_is_unknown(self):
        out = self.run_executor((False, 'UNVERIFIED: response lost after dispatch', 'unknown'))
        self.assertEqual(ops.classify(out)[0], ops.UNKNOWN)

    def test_conflict_is_needs_review(self):
        out = self.run_executor((False, 'ALREADY EXISTS WITH DIFFERENT DETAILS', 'needs_review'))
        self.assertEqual(ops.classify(out)[0], ops.NEEDS_REVIEW)

    def test_pre_dispatch_failure_stays_retryable(self):
        out = self.run_executor((False, 'bad credentials', 'failed_before_dispatch'))
        self.assertEqual(ops.classify(out)[0], ops.FAILED_BEFORE_DISPATCH)

    def test_success_carries_the_provider_id(self):
        out = self.run_executor((True, 'evt_123', 'completed'))
        state, _text, record = ops.classify(out)
        self.assertEqual(state, ops.COMPLETED)
        self.assertEqual(record, 'evt_123')

    def test_unrecognised_adapter_state_defaults_to_unknown(self):
        """An adapter that has not been converted must not be assumed safe."""
        out = self.run_executor((False, 'something happened', 'not-a-state'))
        self.assertEqual(ops.classify(out)[0], ops.UNKNOWN)


class DeliveryOwnership(unittest.TestCase):
    def test_brief_delivery_requires_the_journal(self):
        """A check-then-act day-marker read is not an atomic claim, so delivery must not
        proceed without durable ownership."""
        self.assertIn('bridge_deliver', ops.REQUIRE_JOURNAL)
        self.assertEqual(ops.begin('bridge_deliver', {'job_id': 'brief:morning:2026-09-08'})[0],
                         'unavailable')

    def test_both_paths_use_the_same_claim_key(self):
        from backend import chat
        key_loop = ops.brief_claim('morning', '2026-09-08')
        self.assertEqual(ops.op_key('bridge_deliver', key_loop),
                         ops.op_key('bridge_deliver', {'job_id': 'brief:morning:2026-09-08'}))
        self.assertTrue(hasattr(chat, 'bridge_lease_token'))
