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
        # release one: the decision lane is a CHOICE Brady makes, never derived from
        # "undated and no next step". Only this row should ever read Needs a decision.
        ('pick a direction on the trailer', ['Business'], None, 'action', 'decide', None, 'Side Work'),
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
    assert r['lane'] == classify.LANE_ANYTIME, r['lane']
    r = c.post('/daybank/update', json={'id': target, 'next_step': 'call three shops'}).json()
    assert r['next_step'] == 'call three shops', 'correction did not persist'
    r = c.post('/daybank/update', json={'id': target, 'next_step': ''}).json()
    assert r['next_step'] is None, f'clearing must null it, got {r["next_step"]!r}'
    # RELEASE ONE: clearing a next step must NOT push the row into Needs a decision. That
    # lane is now something Brady chooses; deleting a sentence is not him choosing it.
    assert r['lane'] == classify.LANE_ANYTIME, f'clearing re-derived a decision: {r["lane"]}'
    assert r['saved']['carried_over'] is True, 'it should still be flagged for review, though'
    dec = ids['pick a direction on the trailer']
    d = next(x for x in c.get('/daybank').json()['items'] if x['id'] == dec)
    assert d['lane'] == classify.LANE_UNDECIDED, f'an explicit mark must hold: {d["lane"]}'
    assert d['carried_over'] is False, 'a row he marked himself is not "carried over"'

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

    # 10. ACCEPTING A SUGGESTION MUST NOT CREATE A DEADLINE (2026-09-08, Brady).
    sug = c.get('/daybank').json()['due_today']['suggested']
    assert sug, 'expected at least one suggestion in the fixture'
    s_id = sug[0]['id']
    before = next(x for x in c.get('/daybank?all=true').json()['items'] if x['id'] == s_id)
    today = c.get('/daybank').json()['today']
    r = c.post('/daybank/update', json={'id': s_id, 'chosen_on': today}).json()
    assert r['ok'] is True, r
    after = next(x for x in r['items'] if x['id'] == s_id)
    assert after.get('chosen_on', '')[:10] == today, after
    assert after.get('due') == before.get('due'), 'accepting a suggestion changed the due date'
    assert after.get('due_days') == before.get('due_days'), 'a suggestion silently scheduled itself'
    # it now shows under CHOSEN, not under SUGGESTED
    dt = c.get('/daybank').json()['due_today']
    assert s_id in [x['id'] for x in dt['chosen']], 'accepted work must move to today'
    assert s_id not in [x['id'] for x in dt['suggested']], 'and must leave the suggestion list'
    # declining puts it back and still writes no date
    r = c.post('/daybank/update', json={'id': s_id, 'chosen_on': ''}).json()
    back = next(x for x in r['items'] if x['id'] == s_id)
    assert not back.get('chosen_on'), back
    assert back.get('due') == before.get('due'), 'declining changed the due date'

    # 11. The Command Center and Due Today never disagree about the same row.
    full = {x['id']: x['lane'] for x in c.get('/daybank?all=true').json()['items']}
    dt = c.get('/daybank').json()['due_today']
    for grp in ('deadlines', 'chosen', 'suggested', 'waiting'):
        for x in dt[grp]:
            assert full[x['id']] == x['lane'], f'surface disagreement on {x["id"]}'

    # 12. THE COMPLETION RULE HOLDS AT THE SHARED BOUNDARY, not just on the HTTP route.
    #     Ace's update_item tool calls daybank/db directly and bypassed the route guard
    #     entirely — he closed a record parked on someone else and said "◆ Completed".
    from ace2.backend import tools                       # noqa: E402
    w2 = ids['waiting on the county for the permit']
    out = tools._do_update_item(id=w2, status='done')
    assert 'NOT COMPLETED' in str(out), f'the tool still closed a waiting record: {out!r}'
    assert 'the county' in str(out), out
    assert 'do not tell him it is done' in str(out).lower(), 'the tool must not report success'
    after = next(x for x in daybank.read_items(False) if x['id'] == w2)
    assert after['status'] == 'open', 'the waiting record was modified'

    # a reference record is refused the same way, through the same path
    rec = ids['a record that is settled']
    out = tools._do_update_item(id=rec, status='done')
    assert 'NOT COMPLETED' in str(out), out

    # resolving by TEXT rather than id must not slip past it either
    out = tools._do_update_item(match='waiting on the county for the permit', status='done')
    assert 'NOT COMPLETED' in str(out), f'match-resolution bypassed the rule: {out!r}'
    assert next(x for x in daybank.read_items(False) if x['id'] == w2)['status'] == 'open'

    # an ordinary action still completes normally through the tool
    act = c.post('/daybank/add', json={'text': 'an ordinary action to finish'}).json()
    act_id = next(x for x in act['items'] if x['text'] == 'an ordinary action to finish')['id']
    out = tools._do_update_item(id=act_id, status='done')
    assert 'NOT COMPLETED' not in str(out), out
    assert next(x for x in daybank.read_items(False) if x['id'] == act_id)['status'] == 'done'

    # and a deliberate close is still possible at the same boundary
    ok2, msg2 = daybank.update_item(w2, status='done', force_close=True)
    assert ok2 is True, msg2
    daybank.update_item(w2, status='open')

    print('PASS: Ace.s tool cannot close a waiting record or a reference record, by id or by '
          'match; ordinary actions still complete; force_close still works; and: '
          'accepting a suggestion sets chosen_on and NEVER a due date, both surfaces '
          'agree row for row, and: contract survives add/update, all= is a real route parameter, next_step '
          'creates/corrects/clears, follow-up is not a due date, completion is enforced at '
          'the boundary with an explicit override, child completion preserves its parent, '
          'and Ace reads the same lanes the screen does.')
finally:
    server.cleanup(); tmp.cleanup()
