"""Journal semantics against a real disposable PostgreSQL. Offline; no production URL.

Covers the acceptance scenarios that need true transactional behaviour: retry
convergence, intentional repetition, interrupted dispatch, restart recovery, and two
turns racing for the same write.
"""
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
import pgserver                                    # noqa: E402
from backend import db, ops                        # noqa: E402

CAL = 'create_calendar_event'
ARGS = {'title': 'Ken Weinberg', 'date': '2026-09-09', 'time': '15:00'}

with tempfile.TemporaryDirectory(prefix='ace-ops-pg-') as temp:
    server = pgserver.get_server(Path(temp) / 'data', cleanup_mode='delete')
    try:
        with patch.dict(os.environ, {'DATABASE_URL': server.get_uri()}):
            db._init_schema(); db._ready = True; db._trgm_ok = False
            ops.ready()

            # 1. A retry of the same logical request produces ONE write.
            v, k, _ = ops.begin(CAL, ARGS)
            assert v == 'execute', v
            ops.settle(k, ops.COMPLETED, '◆ Added: Ken Weinberg Wed 3:00 PM', external_id='evt_1')
            v2, k2, prior = ops.begin(CAL, ARGS)
            assert v2 == 'duplicate', v2
            assert k2 == ops.op_key(CAL, ARGS), 'a duplicate reports the logical op key'
            assert 'Ken Weinberg' in prior, prior

            # 2. An intentional second request is still possible once the retry window
            #    has passed — same words, new intention.
            with db._conn() as c, c.cursor() as cur:
                cur.execute("UPDATE ace_write_ops SET created_at=now()-make_interval(secs=>%s) "
                            "WHERE attempt_id=%s", (ops.RETRY_WINDOW_SEC + 60, k))
            v3, _, _ = ops.begin(CAL, ARGS)
            assert v3 == 'execute', f'an intentional repeat must not be swallowed: {v3}'

            # 3. Two turns racing for the same write: exactly one executes.
            other = {'title': 'Power Hour', 'date': '2026-09-08', 'time': '15:00'}
            def race(_):
                return ops.begin(CAL, other)[0]
            with ThreadPoolExecutor(max_workers=2) as pool:
                verdicts = list(pool.map(race, range(2)))
            assert sorted(verdicts) == ['execute', 'in_flight'], verdicts

            # 4. Cancellation during dispatch, never settled → stale → unknown, and an
            #    unknown is reported for review rather than silently retried.
            interrupted = {'title': 'Josh follow-up', 'date': '2026-09-08'}
            v4, k4, _ = ops.begin(CAL, interrupted)
            assert v4 == 'execute'
            with db._conn() as c, c.cursor() as cur:
                cur.execute("UPDATE ace_write_ops SET created_at=now()-make_interval(secs=>%s) "
                            "WHERE attempt_id=%s", (ops.STALE_SEC + 30, k4))
            v5, _, note = ops.begin(CAL, interrupted)
            assert v5 == 'unknown', v5
            assert 'may or may not' in note, note
            v6, _, _ = ops.begin(CAL, interrupted)
            assert v6 == 'unknown', 'an unknown must stay unknown until reconciled'

            # 5. Restart recovery: a process that died mid-write leaves an explicit
            #    unknown, listed for reconciliation, never auto-replayed.
            crashed = {'title': 'Rebecca packet', 'date': '2026-09-10'}
            _, k7, _ = ops.begin(CAL, crashed)
            with db._conn() as c, c.cursor() as cur:
                cur.execute("UPDATE ace_write_ops SET created_at=now()-make_interval(secs=>%s) "
                            "WHERE attempt_id=%s", (ops.STALE_SEC + 5, k7))
            promoted = ops.sweep_stale()
            assert promoted >= 1, promoted
            queue = ops.pending()
            assert any(p['attempt_id'] == k7 and p['state'] == ops.UNKNOWN for p in queue), queue

            # 6. A write that succeeded externally but timed out locally reconciles
            #    without a duplicate: settling it completed makes the retry converge.
            v7b, k7b, _ = ops.begin(CAL, interrupted)   # unknown; reconcile it explicitly
            ops.settle(k4, ops.COMPLETED, '◆ Added: Josh follow-up', external_id='evt_9')
            v8, _, prior8 = ops.begin(CAL, interrupted)
            assert v8 == 'duplicate', v8
            assert 'Josh follow-up' in prior8

            # 7. A failed write is a fresh intention next time, not a permanent block.
            bad = {'title': 'Broken', 'date': '2026-09-11'}
            _, k9, _ = ops.begin(CAL, bad)
            ops.settle(k9, ops.FAILED_BEFORE_DISPATCH, '⚠️ Calendar create error')
            v10, _, _ = ops.begin(CAL, bad)
            assert v10 == 'execute', v10

            print('PASS: retry convergence, intentional repeat, concurrent race, '
                  'interrupted dispatch, restart recovery, post-success reconciliation, '
                  'failure retryability.')
    finally:
        server.cleanup()
