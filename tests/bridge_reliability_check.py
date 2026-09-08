"""Bridge completion + planning-context semantics against a real disposable PostgreSQL.

Delivery is MOCKED throughout — no push, no HUD, no notification of any kind is produced.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
import pgserver                                       # noqa: E402
from backend import chat, db, ops, planning, review_store   # noqa: E402
from backend import main as srv                       # noqa: E402


class FakeReq:
    headers = {}
    client = None


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


with tempfile.TemporaryDirectory(prefix='ace-bridge-pg-') as temp:
    server = pgserver.get_server(Path(temp) / 'data', cleanup_mode='delete')
    try:
        with patch.dict(os.environ, {'DATABASE_URL': server.get_uri()}):
            db._init_schema(); db._ready = True; db._trgm_ok = False
            ops.ready(); review_store.ready()
            today = chat.datetime.now(chat.EASTERN).strftime('%Y-%m-%d')
            delivered = []

            async def fake_deliver(kind, text):
                delivered.append((kind, text))
                return True

            with patch.object(srv, '_bridge_check', lambda r: None), \
                 patch.object(chat, 'deliver_brief', fake_deliver):

                # 1. A successful POST whose RESPONSE IS LOST: the worker retries the same
                #    completion. It must not push the brief to Brady twice.
                body = srv.BridgeResultReq(job='brief', kind='morning', text='Good morning.',
                                           job_id=f'brief:morning:{today}', job_date=today)
                first = run(srv.bridge_complete(body, FakeReq()))
                assert first['ok'] is True, first
                assert len(delivered) == 1, delivered
                second = run(srv.bridge_complete(body, FakeReq()))
                assert second.get('idempotent') is True, second
                assert len(delivered) == 1, f'retry delivered again: {delivered}'

                # 2. Yesterday's parked result must not consume today's slot.
                stale = srv.BridgeResultReq(job='brief', kind='eod', text='Old recap.',
                                            job_id='brief:eod:2026-09-07',
                                            job_date='2026-09-07')
                res = run(srv.bridge_complete(stale, FakeReq()))
                assert res['ok'] is False and res.get('stale'), res
                assert len(delivered) == 1, 'a stale period must not deliver'

                # 3. Lease expiry → the in-server loop delivers → a LATE worker result
                #    arrives. It must be discarded, not pushed as a second copy.
                db.add_summary(today, 'brief_eod')          # the fallback already sent it
                late = srv.BridgeResultReq(job='brief', kind='eod', text='Late recap.',
                                           job_id=f'brief:eod:{today}', job_date=today)
                res = run(srv.bridge_complete(late, FakeReq()))
                assert res['ok'] is False and res.get('late'), res
                assert len(delivered) == 1, f'late result delivered a duplicate: {delivered}'

            # 4. An UNSAVED capture must never read as completed work in a resumed plan.
            #    This is the release blocker Codex found, checked end to end.
            key_verdict, attempt, _ = ops.begin('capture_item', {'text': 'Capital One $106'})
            assert key_verdict == 'execute'
            ops.settle(attempt, ops.NEEDS_REVIEW, '◆ NOT SAVED — needs your call. [abc123]')
            v2, a2, _ = ops.begin('create_calendar_event', {'title': 'Ken', 'date': today})
            ops.settle(a2, ops.COMPLETED, '◆ Added: Ken Wed 3 PM', external_id='evt_1')
            review_store.append_plan('user', 'Plan my week please, lay out the week.')
            ctx = planning.context()
            assert 'CONFIRMED DONE' in ctx, ctx[-800:]
            assert 'Ken' in ctx.split('NOT SAVED')[0], 'the completed event must be listed done'
            assert 'NOT SAVED' in ctx, 'the unsaved capture must be listed as outstanding'
            done_block = ctx.split('NOT SAVED')[0]
            assert 'Capital One' not in done_block, \
                'an unsaved capture must NOT appear as an action already carried out'

            # 5. Repeating the identical unsaved capture must not become a second attempt.
            v3, _, prior = ops.begin('capture_item', {'text': 'Capital One $106'})
            assert v3 == 'duplicate' and 'NOT SAVED' in (prior or ''), (v3, prior)

            # 6. Append-only history: reusing an identity after the window keeps the old row.
            with db._conn() as c, c.cursor() as cur:
                cur.execute("UPDATE ace_write_ops SET created_at=now()-make_interval(secs=>%s)",
                            (ops.RETRY_WINDOW_SEC + 60,))
            v4, a4, _ = ops.begin('capture_item', {'text': 'Capital One $106'})
            assert v4 == 'execute'
            with db._conn() as c, c.cursor() as cur:
                cur.execute("SELECT count(*) FROM ace_write_ops WHERE tool='capture_item'")
                assert cur.fetchone()[0] == 2, 'the earlier attempt must still be on record'

            # 7. Journal outage during completion: the day-marker must still stop a second
            #    push. Idempotency degrades from two guards to one, never to none.
            delivered2 = []

            async def fake_deliver2(kind, text):
                delivered2.append((kind, text))
                return True

            with patch.object(srv, '_bridge_check', lambda r: None), \
                 patch.object(chat, 'deliver_brief', fake_deliver2), \
                 patch.object(ops, 'ready', side_effect=RuntimeError('journal outage')):
                b2 = srv.BridgeResultReq(job='brief', kind='morning', text='x',
                                         job_id=f'brief:morning:{today}', job_date=today)
                run(srv.bridge_complete(b2, FakeReq()))
                run(srv.bridge_complete(b2, FakeReq()))
            assert len(delivered2) == 0, (
                'the day marker from scenario 1 must still block re-delivery: %r' % delivered2)

            print('PASS: lost-response retry is idempotent, stale period rejected, late result '
                  'discarded, unsaved capture never reads as done, repeat of an unsaved capture '
                  'converges, attempt history is append-only, journal outage still blocked by the day marker.')
    finally:
        server.cleanup()
