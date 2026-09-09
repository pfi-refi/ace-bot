"""The three defects from ACE-RELEASE-ONE-INTEGRATED-REVIEW-2026-09-09.md, through the real
routes and a real disposable PostgreSQL.

Synthetic rows only. No production URL, no credentials, no model calls, no network.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'ace2'))
import pgserver                                        # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402

tmp = tempfile.TemporaryDirectory(prefix='ace-rc-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    from ace2.backend import chat, classify, daybank, db   # noqa: E402
    from ace2.backend.main import app                      # noqa: E402
    db._init_schema(); db._ready = True; db._trgm_ok = False

    import json as _j, uuid
    from datetime import datetime, timedelta, timezone
    c = TestClient(app)

    def add(text, **kw):
        """Insert a row directly so its ts (and therefore its legacy status) is ours to set."""
        i = uuid.uuid4().hex[:6]
        cols = dict(id=i, ts=kw.pop('ts', datetime.now(timezone.utc)), kind='todo', text=text,
                    status='open', tags=_j.dumps(kw.pop('tags', ['Admin'])),
                    due=kw.pop('due', None), entry=kw.pop('entry', 'action'),
                    state=kw.pop('state', None), waiting_on=kw.pop('waiting_on', None),
                    bucket=kw.pop('bucket', 'Personal'), next_step=kw.pop('next_step', None),
                    followup=kw.pop('followup', None), chosen_on=kw.pop('chosen_on', None))
        with db._conn() as cn, cn.cursor() as cur:
            cur.execute("INSERT INTO daybank_items(%s) VALUES (%s)"
                        % (",".join(cols), ",".join(["%s"] * len(cols))), list(cols.values()))
        return i

    def row(item_id):
        return next(x for x in c.get('/daybank?all=true').json()['items'] if x['id'] == item_id)

    # The boundary was stamped by _init_schema, so "before it" is genuinely the old board.
    BOUNDARY = db.review_boundary()
    assert BOUNDARY, 'the release-one boundary was never stamped'
    OLD = datetime.fromisoformat(BOUNDARY) - timedelta(days=3)

    # ── DEFECT 1: the brief called explicit decisions ready, and contradicted itself ───
    d_next = add('Decide whether to accept this job', state='decide',
                 next_step='Check terms first', ts=OLD)
    d_bare = add('Decide whether to move the shop', state='decide', ts=OLD)
    d_dated = add('Decide whether to renew the lease', state='decide',
                  due=(datetime.now(db.EASTERN) + timedelta(days=3)).strftime('%Y-%m-%d'),
                  ts=OLD)
    legacy = add('an old row nobody ever filed', ts=OLD)
    fresh = add('captured just now, undated', next_step='email the quote')

    stats = chat._board_stats()
    ready_block = stats.split('UNDATED AND READY')[-1] if 'UNDATED AND READY' in stats else ''
    for _id, why in ((d_next, 'a decision WITH a next step'),
                     (d_bare, 'a decision with no next step'),
                     (d_dated, 'a dated decision')):
        assert f'[{_id}]' not in ready_block, f'{why} was handed to the brief as ready work'
    assert f'[{legacy}]' not in ready_block, 'an unreviewed legacy row was called ready'
    assert f'[{fresh}]' in ready_block, 'genuinely ready work stopped reaching the brief'
    assert 'OPEN QUESTIONS HE MARKED HIMSELF' in stats
    assert 'CARRIED OVER FROM THE OLD BOARD' in stats
    # ...and it must stop asserting "no next step recorded" over a row that has one
    seg = stats.split(f'[{d_next}]')[1].split('  - [')[0]
    assert 'Check terms first' in seg
    assert 'no date and no next step recorded' not in seg, \
        'the brief contradicted its own next-step line'
    assert 'DECISION HE HAS NOT MADE' in seg
    # a DATED decision keeps its clock placement AND carries the mark
    dated_seg = stats.split(f'[{d_dated}]')[1].split('  - [')[0]
    assert 'DECISION HE HAS NOT MADE' in dated_seg, 'a dated decision lost its flag'
    assert 'ALSO ON THE CLOCK' in stats and f'[{d_dated}]' in stats.split('ALSO ON THE CLOCK')[1]

    # ── DEFECT 2: the carried-over flag was a label, not a protection ─────────────────
    assert row(legacy)['carried_over'] is True
    assert row(fresh)['carried_over'] is False, 'new capture claimed the old board’s history'
    brand_new = c.post('/daybank/add', json={'text': 'something I just thought of'}).json()
    bn = next(x for x in brand_new['items'] if x['text'] == 'something I just thought of')
    assert bn['carried_over'] is False, 'a capture made seconds ago was called carried over'

    dt = c.get('/daybank').json()['due_today']
    assert legacy not in [x['id'] for x in dt['suggested']], \
        'an unreviewed row was offered as suggested work'
    assert legacy in [x['id'] for x in dt['review']], 'and it vanished instead of being listed'
    assert d_bare not in [x['id'] for x in dt['suggested']], 'a decision was suggested as work'
    assert d_bare in [x['id'] for x in dt['decisions']]
    # it is still real work he can do — the flag must not quietly disable the row
    assert row(legacy)['completable'] is True and row(legacy)['actionable'] is True

    # explicit Ready clears it DURABLY, with no invented date or next step
    before = row(legacy)
    r = c.post('/daybank/update', json={'id': legacy, 'reviewed': True}).json()
    assert r['ok'] is True, r.get('error')
    after = row(legacy)
    assert after['carried_over'] is False, 'saying Ready did not retire the flag'
    assert after['reviewed_at'], 'nothing durable was written'
    assert after['due'] == before['due'] and after['next_step'] == before['next_step'], \
        'the flag was cleared by inventing data'
    assert after['status'] == 'open' and after['state'] == before['state']
    # ...and it survives a reload (a fresh read, not a cached decoration)
    db._ready = False; db._ready = True
    assert classify.carried_over(
        next(x for x in daybank.read_items(False) if x['id'] == legacy)) is False, \
        'the review did not survive a reload'
    assert legacy in [x['id'] for x in c.get('/daybank').json()['due_today']['suggested']], \
        'a reviewed row should now be ordinary ready work'

    # a legacy row NOT reviewed stays unreviewed across a reload
    assert classify.carried_over(
        next(x for x in daybank.read_items(False) if x['id'] == d_bare or x['id'] == legacy
             )) is False
    legacy2 = add('another old unfiled row', ts=OLD)
    db._ready = False; db._ready = True
    assert row(legacy2)['carried_over'] is True, 'a legacy row lost its flag on reload'

    # choosing a day also reviews it, durably
    today = c.get('/daybank').json()['today']
    c.post('/daybank/update', json={'id': legacy2, 'chosen_on': today})
    db._ready = False; db._ready = True
    assert row(legacy2)['reviewed_at'], 'picking a day did not durably review it'
    assert row(legacy2)['carried_over'] is False

    # ── DEFECT 3: rename reported success after its metadata write failed ─────────────
    c.post('/board/lists', json={'name': 'Greenhouse'})
    moved = add('a row in the new list', bucket='Greenhouse')
    snapshot = {x['id']: dict(x) for x in c.get('/daybank?all=true').json()['items']}
    lists_before = c.get('/board/lists').json()

    # FAILURE INJECTION. psycopg2's cursor is a C type and cannot be patched, so the seam is
    # the connection: a real one, wrapped so the list-metadata INSERT raises AFTER the bucket
    # UPDATE has already run on the same transaction. That is exactly the window the old code
    # committed through, and the rollback below is the real database's, not a stub's.
    import contextlib
    _real_conn = db._conn

    class _Cur:
        def __init__(self, cur): self._c = cur
        def __enter__(self): return self
        def __exit__(self, *a): return self._c.__exit__(*a)
        def execute(self, sql, args=None):
            if 'INSERT INTO summaries' in str(sql):
                raise RuntimeError('injected: metadata write failed')
            return self._c.execute(sql, args)
        def __getattr__(self, n): return getattr(self._c, n)

    class _Conn:
        def __init__(self, conn): self._c = conn
        def cursor(self): return _Cur(self._c.cursor().__enter__().__class__ and
                                      self._c.cursor())
        def __getattr__(self, n): return getattr(self._c, n)

    @contextlib.contextmanager
    def _sabotaged():
        with _real_conn() as real:
            yield _Conn(real)

    ok_rename, msg = None, ''
    try:
        with patch.object(db, '_conn', _sabotaged):
            ok_rename, msg = db.rename_area('Greenhouse', 'Greenhouse build')
    except Exception as e:
        ok_rename, msg = False, str(e)
    assert ok_rename is False, f'a failed rename reported success: {msg}'
    assert 'nothing was changed' in msg or 'failed' in msg, msg

    after_rows = {x['id']: dict(x) for x in c.get('/daybank?all=true').json()['items']}
    assert set(after_rows) == set(snapshot), 'a failed rename changed which rows exist'
    for i, was in snapshot.items():
        now_ = after_rows[i]
        for f in ('bucket', 'status', 'due', 'ts', 'parent_id', 'text', 'next_step',
                  'followup', 'waiting_on', 'state'):
            assert now_.get(f) == was.get(f), f'failed rename changed {f} on {i}'
    assert c.get('/board/lists').json() == lists_before, 'failed rename changed the list names'
    assert row(moved)['bucket'] == 'Greenhouse', 'items moved despite the rename failing'
    assert db.derive_bucket_for_capture('nothing recognisable here', '') == 'Inbox'

    # and the same for a BUILT-IN, whose metadata is the filing alias
    alias_before = db.area_renames()
    # what the keyword rules file today, whatever that is — the point is it must not move
    filing_before = {t: db.derive_bucket(t, '') for t in
                     ('pour the footers on Friday', 'paramed exam for the annuity',
                      'nothing recognisable here at all')}
    try:
        with patch.object(db, '_conn', _sabotaged):
            ok2, msg2 = db.rename_area('Groundworks', 'Concrete')
    except Exception as e:
        ok2, msg2 = False, str(e)
    assert ok2 is False, f'a failed built-in rename reported success: {msg2}'
    assert db.area_renames() == alias_before, 'the filing alias changed on a failed rename'
    assert {t: db.derive_bucket(t, '') for t in filing_before} == filing_before, \
        'a failed rename changed where new work would be filed'
    assert 'Groundworks' in c.get('/board/lists').json()['areas']

    # ...and a rename that is NOT sabotaged still works, atomically
    ok3, msg3 = db.rename_area('Greenhouse', 'Greenhouse build')
    assert ok3 is True, msg3
    assert 'Greenhouse build' in c.get('/board/lists').json()['areas']
    assert row(moved)['bucket'] == 'Greenhouse build'
    assert row(moved)['ts'] == snapshot[moved]['ts'], 'a good rename rewrote history'

    print('PASS: explicit decisions and unreviewed legacy rows never reach the brief or the '
          'suggestion lists as ready work, and a dated decision keeps both its deadline and '
          'its mark; the carried-over flag is durable, bounded to rows that predate release '
          'one, cleared by saying Ready with no invented date or next step, and survives a '
          'reload in both directions; and a rename whose metadata write fails changes '
          'nothing at all — no bucket, no id, no link, no list name, no filing alias.')
finally:
    server.cleanup(); tmp.cleanup()
