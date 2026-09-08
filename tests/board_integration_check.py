"""Board integration through the ACTUAL FastAPI routes and a real disposable PostgreSQL.

Synthetic rows only. No production URL, no Google credentials, no model calls, no network.
Unit tests missed every one of these because they exercised helpers, not routes.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'ace2'))
import pgserver                                        # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402

tmp = tempfile.TemporaryDirectory(prefix='ace-board-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    from ace2.backend import db, daybank, chat, classify   # noqa: E402
    from ace2.backend.main import app                       # noqa: E402
    db._init_schema(); db._ready = True; db._trgm_ok = False

    import json as _j
    import uuid
    from datetime import datetime, timedelta, timezone
    FIX = [
        ('undated action with no next step', ['Business'], None, 'action', None, None, 'Side Work'),
        ('waiting on the county for the permit', ['Business'], '2026-09-20', 'record', 'waiting', 'the county', 'Side Work'),
        ('a record that is settled', ['Money'], None, 'record', 'settled', None, 'GFI/PFI'),
        ('parent record for the deal', ['Deals'], None, 'record', None, None, 'GFI/PFI'),
    ]
    ids = {}
    with db._conn() as c, c.cursor() as cur:
        for n, (text, tags, due, entry, state, wait, bucket) in enumerate(FIX):
            i = uuid.uuid4().hex[:6]; ids[text] = i
            cur.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,due,entry,state,"
                        "waiting_on,bucket) VALUES(%s,%s,'todo',%s,'open',%s::jsonb,%s,%s,%s,%s,%s)",
                        (i, datetime.now(timezone.utc) - timedelta(days=n), text,
                         _j.dumps(tags), due, entry, state, wait, bucket))
    c = TestClient(app)

    def lanes(payload):
        return {r['id']: r['lane'] for r in payload['items']}

    # 1. GET carries the contract, and `all` is a real parameter on the real route.
    g = c.get('/daybank').json()
    assert 'summary' in g and all('lane' in r for r in g['items']), 'GET lost the contract'
    g_all = c.get('/daybank?all=true').json()
    assert g_all['summary']['total'] >= g['summary']['total'], 'all=true must not shrink the set'

    # 2. A SUCCESSFUL update answers with the same contract — this is what went blank before.
    target = ids['undated action with no next step']
    r = c.post('/daybank/update', json={'id': target, 'text': 'undated action, reworded'}).json()
    assert r['ok'] is True, r
    assert 'summary' in r, 'successful update dropped the summary'
    assert all('lane' in x for x in r['items']), 'successful update dropped lanes'
    assert any(x['text'] == 'undated action, reworded' for x in r['items']), 'edit did not persist'

    # 3. A SUCCESSFUL add answers with the same contract.
    r = c.post('/daybank/add', json={'text': 'brand new captured thing'}).json()
    assert r['ok'] is True and 'summary' in r and all('lane' in x for x in r['items']), r

    # 4. next_step: create, correct, clear — through the route, verified by readback.
    r = c.post('/daybank/update', json={'id': target, 'next_step': 'call two shops'}).json()
    assert r['ok'] and r['next_step'] == 'call two shops', r
    assert r['lane'] == classify.LANE_ANYTIME, f"a recorded next step must leave Needs a decision: {r['lane']}"
    r = c.post('/daybank/update', json={'id': target, 'next_step': 'call three shops'}).json()
    assert r['next_step'] == 'call three shops', 'correction did not persist'
    r = c.post('/daybank/update', json={'id': target, 'next_step': ''}).json()
    assert r['next_step'] is None, f'clearing must null it, got {r["next_step"]!r}'
    assert r['lane'] == classify.LANE_UNDECIDED, 'clearing returns it to Needs a decision'

    # 5. followup is Brady's date and is NOT the obligation's due date.
    r = c.post('/daybank/update', json={'id': target, 'followup': '2026-09-19'}).json()
    assert r['followup'] == '2026-09-19', r
    row = next(x for x in r['items'] if x['id'] == target)
    assert row.get('due') in (None, ''), 'a follow-up must not become a due date'

    # 6. Completion is enforced at the BOUNDARY, not just by a disabled checkbox.
    wait_id = ids['waiting on the county for the permit']
    r = c.post('/daybank/update', json={'id': wait_id, 'status': 'done'}).json()
    assert r['ok'] is False and r.get('blocked'), f'a waiting row must not close by checkbox: {r}'
    assert 'the county' in r['error'], r['error']
    still = next(x for x in c.get('/daybank').json()['items'] if x['id'] == wait_id)
    assert still['status'] == 'open', 'the blocked row must be untouched'

    # ...but a deliberate, explicit change is still possible.
    r = c.post('/daybank/update', json={'id': wait_id, 'status': 'done', 'force_close': True}).json()
    assert r['ok'] is True, r
    r = c.post('/daybank/update', json={'id': wait_id, 'status': 'open'}).json()   # restore

    # 7. A child action completing must not touch its parent record.
    parent = ids['parent record for the deal']
    add = c.post('/daybank/add', json={'text': 'child action under the deal'}).json()
    child = next(x for x in add['items'] if x['text'] == 'child action under the deal')
    with db._conn() as cn, cn.cursor() as cur:
        cur.execute("UPDATE daybank_items SET parent_id=%s WHERE id=%s", (parent, child['id']))
    r = c.post('/daybank/update', json={'id': child['id'], 'status': 'done'}).json()
    assert r['ok'] is True, r
    p_now = next(x for x in c.get('/daybank?all=true').json()['items'] if x['id'] == parent)
    assert p_now['status'] == 'open', 'completing a child must not close its parent'

    # 8. The SAME interpretation reaches Ace's context, not just the screen.
    rows = daybank.read_items(False)
    ctx = chat._format_daybank(rows)
    assert '[PARKED · the county]' in ctx, 'context must show who owns the next move'
    assert '[NEEDS A DECISION]' in ctx, 'context must flag undated work with no next step'
    for r_ in rows:
        if r_['id'] == wait_id:
            assert classify.lane_of(r_) == classify.LANE_WAITING

    # 9. Cross-view consistency: one row, one lane, everywhere.
    api = lanes(c.get('/daybank?all=true').json())
    direct = {r_['id']: classify.lane_of(r_) for r_ in daybank.read_items(False)}
    for k, v in direct.items():
        assert api.get(k) == v, f'view disagreement on {k}: api={api.get(k)} direct={v}'

    print('PASS: contract survives add/update, all= is a real route parameter, next_step '
          'creates/corrects/clears, follow-up is not a due date, completion is enforced at '
          'the boundary with an explicit override, child completion preserves its parent, '
          'and Ace reads the same lanes the screen does.')
finally:
    server.cleanup(); tmp.cleanup()
