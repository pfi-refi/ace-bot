"""Command Center backend contract — four findings, end to end, through the real routes.

What this file keeps dead (2026-09-22):

  1. CLEARING A TEXT-DERIVED DUE DID NOT STICK. `due` is only the stored field; when it is
     empty the store derives a date from the wording ("pay by Oct 2"), so an explicit clear
     wrote NULL over NULL and the date was back on the next read. Now the clear remembers
     the exact date it removed (`due_cleared`) and holds only that date back — reword the
     row to a different date and the new one shows; omit `due` and nothing is touched, and
     a derived date is never frozen into the field by an edit that did not mention it.
  2. TWO EDITORS, LAST WRITER WINS. `expected_updated_at` is an optional optimistic lock,
     checked under a row lock in the same transaction as the write. Stale → ok:false,
     conflict:true, nothing written, the current row handed back. Omitted → no check, so
     Ace's tool and the checkbox are exactly as they were.
  3. A PARENT WITH OPEN SUBTASKS COULD BE TICKED DONE. Refused as NOT COMPLETED (the shape
     the panel already renders as a block), force_close remains the deliberate override,
     nothing cascades either way, closing a child leaves the parent open, and the board
     payload — Today included — says completable:false with completion_hold:'children'.
  4. QUICK-ADD REFUSALS SAID NOTHING. A duplicate or a near-twin now names the existing row
     (id, text, status, due) and, for a near-twin, the requested values and a reason.

Real disposable PostgreSQL (pgserver), synthetic rows only, verified by READBACK from a
fresh GET and from the raw columns — never from a POST's own echo. No production URL, no
Google credentials, no model calls, no network.
"""
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'ace2'))
import pgserver                                        # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402

tmp = tempfile.TemporaryDirectory(prefix='ace-cc-backend-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    os.environ.pop('ACE2_PASSWORD', None)
    from ace2.backend import db, classify                   # noqa: E402
    from ace2.backend.main import app                       # noqa: E402
    app.router.on_startup.clear()      # no integration warm-up, no recurring model work
    db._init_schema(); db._ready = True; db._trgm_ok = False

    import json as _j
    import uuid
    from datetime import date, datetime, timedelta, timezone

    c = TestClient(app)

    def post(body):
        return c.post('/daybank/update', json=body).json()

    def add(body):
        return c.post('/daybank/add', json=body).json()

    def board():
        return c.get('/daybank?all=true').json()

    def row(item_id):
        """The row as a FRESH read of the board sees it — never the POST's own echo."""
        return next((x for x in board()['items'] if x['id'] == item_id), None)

    def raw(item_id, cols='due, due_cleared, updated_at, status, parent_id'):
        with db._conn() as cn, cn.cursor() as cur_:
            cur_.execute(f"SELECT {cols} FROM daybank_items WHERE id = %s", (item_id,))
            return cur_.fetchone()

    def seed(text, **cols):
        """One synthetic row straight into the table, bypassing add's dedup."""
        i = uuid.uuid4().hex[:6]
        base = {'tags': ['Business'], 'entry': 'action', 'bucket': 'Side Work', 'due': None,
                'parent_id': None, 'status': 'open'}
        base.update(cols)
        with db._conn() as cn, cn.cursor() as cur_:
            cur_.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,entry,bucket,"
                         "due,parent_id) VALUES(%s,%s,'todo',%s,%s,%s::jsonb,%s,%s,%s,%s)",
                         (i, datetime.now(timezone.utc) - timedelta(days=1), text,
                          base['status'], _j.dumps(base['tags']), base['entry'],
                          base['bucket'], base['due'], base['parent_id']))
        return i

    today = date.fromisoformat(c.get('/daybank').json()['today'])
    d10, d20 = (today + timedelta(days=10)).isoformat(), (today + timedelta(days=20)).isoformat()

    # ══ 1. AN EXPLICIT CLEAR OF A TEXT-DERIVED DUE PERSISTS ═══════════════════════════
    S = seed(f'Pay the sewer bill by {d10}')
    s = row(S)
    assert s['due'] is None and s['due_on'] == d10 and s['due_days'] == 10, s
    assert s['lane'] == classify.LANE_UPCOMING, s['lane']
    assert s['due_cleared'] is None, 'an un-migrated row must carry no marker'

    # An edit that OMITS due touches neither the field nor the derived date — and does not
    # freeze the derived date into the field.
    assert post({'id': S, 'next_step': 'call the township'})['ok'] is True
    assert raw(S)[0] is None, f'an omitted due froze the derived date: {raw(S)}'
    assert row(S)['due_on'] == d10, 'an omitted due lost the derived date'

    # The clear. Field stays NULL, the marker names the date, the read holds it back.
    r = post({'id': S, 'due': ''})
    assert r['ok'] is True, r.get('error')
    assert r['saved']['due_on'] is None, f"the POST echo still derived a date: {r['saved']}"
    s = row(S)
    assert s['due'] is None and s['due_on'] is None and s['due_days'] is None, \
        f'the clear did not persist against the wording: {s["due_on"]!r}'
    assert s['due_cleared'] == d10, s['due_cleared']
    assert s['lane'] == classify.LANE_ANYTIME, s['lane']
    assert not classify.has_next_step({**s, 'next_step': None}), \
        'a cleared derived date still counted as a recorded next move'
    assert raw(S)[:2] == (None, d10), raw(S)

    # Rewording that keeps the same date keeps the clear.
    assert post({'id': S, 'text': f'Pay the sewer bill (township) by {d10}'})['ok'] is True
    s = row(S)
    assert s['due_on'] is None and s['due_cleared'] == d10, \
        f'a rewording with the same date brought it back: {s["due_on"]!r}'

    # A DIFFERENT date in the wording shows, and the old clear lapses.
    assert post({'id': S, 'text': f'Pay the sewer bill by {d20}'})['ok'] is True
    s = row(S)
    assert s['due_on'] == d20 and s['due_days'] == 20, f'a new date in the text was hidden: {s}'
    assert s['due_cleared'] is None, f'a stale clear outlived the date it cleared: {s["due_cleared"]!r}'
    assert s['lane'] == classify.LANE_UPCOMING

    # Clear that one too, remove the date from the wording, then type it back on purpose:
    # the clear must not swallow a date Brady deliberately re-entered later.
    assert post({'id': S, 'due': ''})['ok'] is True
    assert raw(S)[:2] == (None, d20), raw(S)
    assert post({'id': S, 'text': 'Pay the sewer bill'})['ok'] is True
    assert raw(S)[:2] == (None, None), f'clearing the wording left a marker: {raw(S)}'
    assert post({'id': S, 'text': f'Pay the sewer bill by {d20}'})['ok'] is True
    assert row(S)['due_on'] == d20, 'a date typed back in on purpose was swallowed'

    # An explicit date in the FIELD wins outright and drops the marker.
    assert post({'id': S, 'due': ''})['ok'] is True
    assert row(S)['due_on'] is None
    assert post({'id': S, 'due': '2026-11-01'})['ok'] is True
    s = row(S)
    assert s['due'] == '2026-11-01' and s['due_on'] == '2026-11-01', s
    assert s['due_cleared'] is None, 'setting a date left the clear marker behind'
    # ...and clearing a stored field on a row whose wording ALSO carries a date clears both:
    # Brady cleared the due; the row must not immediately show a different one.
    assert post({'id': S, 'due': ''})['ok'] is True
    s = row(S)
    assert s['due'] is None and s['due_on'] is None, f'a clear surfaced the text date: {s["due_on"]!r}'
    assert s['due_cleared'] == d20, s['due_cleared']

    # A clear on a row with NO date anywhere writes no marker (the editor sends '' for an
    # empty date box on every save), so a date typed into the wording later is not hidden.
    N = seed('order gravel for the pad')
    assert post({'id': N, 'due': ''})['ok'] is True
    assert raw(N)[:2] == (None, None), f'a clear of nothing left a marker: {raw(N)}'
    assert post({'id': N, 'text': f'order gravel for the pad by {d10}'})['ok'] is True
    assert row(N)['due_on'] == d10

    # The verified tool path agrees with the writer (oracle drift) and reports the clear.
    T = seed(f'send the packet by {d10}')
    ok_, detail = db.update_item_verified(T, due='')
    assert ok_ is True and detail['verified'] is True, detail.get('reason')
    assert detail['changed'].get('due_cleared', {}).get('to') == d10, detail['changed']
    assert row(T)['due_on'] is None
    ok_, detail = db.update_item_verified(T, text=f'send the packet by {d20}')
    assert ok_ is True and detail['verified'] is True, detail.get('reason')
    assert detail['changed'].get('due_cleared', {}).get('to') is None, detail['changed']
    assert row(T)['due_on'] == d20

    # Drive-fallback rule is unchanged: a due edit is refused there, not silently dropped.
    from ace2.backend import daybank                        # noqa: E402
    _real = db.enabled
    db.enabled = lambda: False
    try:
        ok_, msg_ = daybank.update_item(S, due='')
        assert ok_ is False and 'Postgres' in msg_, msg_
    finally:
        db.enabled = _real

    # ══ 2. OPTIMISTIC EDITOR CONCURRENCY ═════════════════════════════════════════════
    E = seed('choose a color for the trim')
    assert row(E)['updated_at'] is None, 'a fresh seed must load with no updated_at'
    # "" = "I loaded a row with NULL updated_at". First save goes through.
    r = post({'id': E, 'text': 'choose a color for the trim — v1', 'expected_updated_at': ''})
    assert r['ok'] is True, r.get('error')
    t1 = row(E)['updated_at']
    assert t1, 'the first save did not stamp updated_at'
    # The same stale "" again: refused, nothing written, the current row handed back.
    r = post({'id': E, 'text': 'choose a color for the trim — v2', 'expected_updated_at': ''})
    assert r['ok'] is False and r.get('conflict') is True, r
    assert r['error'].startswith('CONFLICT'), r['error']
    assert 'Nothing was saved' in r['error'], r['error']
    assert r.get('blocked') is not True, 'a stale edit was reported as a completion block'
    assert r['current_updated_at'] == t1 and r['saved']['updated_at'] == t1, r['current_updated_at']
    assert r['saved']['text'].endswith('v1'), r['saved']['text']
    assert 'summary' in r and all('lane' in x for x in r['items']), 'the conflict dropped the board'
    assert row(E)['text'].endswith('v1') and row(E)['updated_at'] == t1, 'a stale edit wrote'
    # The loaded value, exactly as the payload gave it: accepted.
    r = post({'id': E, 'text': 'choose a color for the trim — v2', 'expected_updated_at': t1})
    assert r['ok'] is True, r.get('error')
    t2 = row(E)['updated_at']
    assert t2 and t2 != t1
    # Another surface (Ace's tool, no lock) saves in between; the editor's copy is now stale.
    ok_, _ = db.update_item(E, text='choose a color for the trim — from the phone')
    assert ok_ is True
    t3 = row(E)['updated_at']
    r = post({'id': E, 'text': 'choose a color for the trim — v3', 'expected_updated_at': t2})
    assert r['ok'] is False and r.get('conflict') is True, r
    assert row(E)['text'].endswith('from the phone'), 'the stale editor overwrote the phone'
    assert r['current_updated_at'] == t3
    # Reload, resend with the fresh stamp: accepted.
    assert post({'id': E, 'text': 'choose a color for the trim — v3',
                 'expected_updated_at': t3})['ok'] is True
    assert row(E)['text'].endswith('v3')
    # A 'Z' suffix (a client that normalised the stamp to UTC) still compares equal.
    t4 = row(E)['updated_at']
    z = datetime.fromisoformat(t4).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    assert post({'id': E, 'next_step': 'ask Dana', 'expected_updated_at': z})['ok'] is True, \
        'a UTC-normalised stamp was refused'
    # Omitted: no check at all — every existing caller keeps working (tool compatibility).
    assert post({'id': E, 'next_step': 'ask Dana again'})['ok'] is True
    ok_, _ = db.update_item(E, next_step='ask Dana a third time')
    assert ok_ is True
    # A stale lock refuses a completion too, with the conflict reason, not the block one.
    t5 = row(E)['updated_at']
    r = post({'id': E, 'status': 'done', 'expected_updated_at': t1})
    assert r['ok'] is False and r.get('conflict') is True and row(E)['status'] == 'open', r
    # Malformed: a client bug, refused as such — never reported as "someone else edited it".
    r = post({'id': E, 'text': 'garbage stamp', 'expected_updated_at': 'yesterday'})
    assert r['ok'] is False and r.get('conflict') is not True, r
    assert 'timestamp' in r['error'] and 'Nothing changed' in r['error'], r['error']
    assert row(E)['updated_at'] == t5 and not row(E)['text'].startswith('garbage')
    # Whitespace around the stamp is not a conflict.
    assert post({'id': E, 'text': 'padded stamp', 'expected_updated_at': f'  {t5}  '})['ok'] is True
    # An unknown id with a lock still says "no item", not CONFLICT.
    r = post({'id': 'no-such-row', 'text': 'x', 'expected_updated_at': ''})
    assert r['ok'] is False and 'no item' in r['error'] and r.get('conflict') is not True, r

    # THE RACE ITSELF: two editors loaded the same stamp and save at the same instant.
    # The row lock serialises them; exactly one wins and the loser sees CONFLICT.
    t6 = row(E)['updated_at']
    barrier = threading.Barrier(2)

    def racer(label):
        barrier.wait()
        return db.update_item(E, text=f'trim colour chosen by {label}', expected_updated_at=t6)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(racer, ['A', 'B']))
    wins = [r_ for r_ in results if r_[0]]
    losses = [r_ for r_ in results if not r_[0]]
    assert len(wins) == 1 and len(losses) == 1, results
    assert losses[0][1].startswith('CONFLICT'), losses[0][1]
    assert row(E)['text'] == wins[0][1], 'the winner is not what the board shows'
    # Without the lock the same race is last-writer-wins, as before (no behaviour change).
    barrier = threading.Barrier(2)

    def unlocked(label):
        barrier.wait()
        return db.update_item(E, next_step=f'unlocked {label}')

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(r_[0] for r_ in pool.map(unlocked, ['A', 'B']))
    # The Drive fallback cannot check a lock, and says so rather than skipping the check.
    # The board read is stubbed as well so the refusal's payload never reaches for Drive.
    db.enabled = lambda: False
    _real_read = daybank.read_items
    daybank.read_items = lambda *a, **k: []
    try:
        r = post({'id': E, 'text': 'offline save', 'expected_updated_at': t6})
        assert r['ok'] is False and 'Postgres' in r['error'], r
    finally:
        db.enabled = _real
        daybank.read_items = _real_read
    assert row(E)['text'] != 'offline save', 'the refused offline save landed anyway'

    # ══ 3. A PARENT WITH OPEN SUBTASKS IS NOT COMPLETABLE — AND NOTHING CASCADES ═════
    a = add({'text': 'get the barn permit through', 'bucket': 'Side Work'})
    assert a['ok'] is True and a.get('dup') is False and a.get('id'), a
    P = a['id']
    p = row(P)
    assert p['open_children'] == 0 and p['completable'] is True and p['completion_hold'] == '', p
    a = add({'text': 'submit the drainage drawing', 'parent_id': P})
    assert a['ok'] is True and a.get('id'), a
    C1 = a['id']
    c1 = row(C1)
    assert c1['parent_id'] == P, c1
    assert c1['area'] == 'Side Work' and c1['bucket'] == 'Side Work', \
        f'a subtask filed itself away from its parent: {c1["area"]}'
    p = row(P)
    assert p['open_children'] == 1, p['open_children']
    assert p['completable'] is False and p['completion_hold'] == 'children', p
    assert p['lane'] == classify.LANE_ANYTIME and p['actionable'] is True, \
        'a parent with subtasks changed lane or stopped being work'
    # Today agrees: dated for today, the parent is a deadline that still cannot be ticked.
    assert post({'id': P, 'due': today.isoformat()})['ok'] is True
    dt = c.get('/daybank').json()['due_today']
    dl = next((x for x in dt['deadlines'] if x['id'] == P), None)
    assert dl is not None, 'a parent due today is missing from Today'
    assert dl['completable'] is False and dl['completion_hold'] == 'children', dl
    assert dl['open_children'] == 1
    # The board summary still counts the parent as work — the hold is not a lane.
    full = board()
    assert full['summary']['counts'][classify.LANE_TODAY] >= 1
    # An ordinary tick is refused, in the shape the panel renders as a block, naming the child.
    r = post({'id': P, 'status': 'done'})
    assert r['ok'] is False and r.get('blocked') is True, f'a parent with open work closed: {r}'
    assert r['error'].startswith('NOT COMPLETED') and C1 in r['error'], r['error']
    assert '1 open subtask ' in r['error'] and 'force_close' in r['error'], r['error']
    assert 'do not tell him it is done' in r['error'].lower(), r['error']
    assert row(P)['status'] == 'open', 'the refused parent was modified'
    # The same refusal at the shared boundary Ace's tool calls, and through the verified path.
    ok_, msg_ = db.update_item(P, status='done')
    assert ok_ is False and msg_.startswith('NOT COMPLETED') and C1 in msg_, msg_
    ok_, detail = db.update_item_verified(P, status='done')
    assert ok_ is False and detail['accepted'] is False and 'subtask' in detail['reason'], detail
    assert raw(P)[3] == 'open'
    # Closing the child leaves the parent open — and makes it completable again.
    assert post({'id': C1, 'status': 'done'})['ok'] is True
    assert row(C1)['status'] == 'done'
    p = row(P)
    assert p['status'] == 'open', 'closing a child closed the parent'
    assert p['open_children'] == 0 and p['completable'] is True and p['completion_hold'] == '', p
    assert p['due_on'] == today.isoformat(), 'closing a child rewrote the parent deadline'
    # Reopen the child, add a second; force_close is the deliberate override and nothing
    # cascades: both children stay open and stay attached.
    assert post({'id': C1, 'status': 'open'})['ok'] is True
    a = add({'text': 'pay the permit fee', 'parent_id': P})
    assert a['ok'] is True, a
    C2 = a['id']
    assert row(P)['open_children'] == 2
    r = post({'id': P, 'status': 'done'})
    assert r['ok'] is False and '2 open subtasks' in r['error'] and C2 in r['error'], r
    r = post({'id': P, 'status': 'done', 'force_close': True})
    assert r['ok'] is True, r.get('error')
    assert row(P)['status'] == 'done'
    for kid in (C1, C2):
        k = row(kid)
        assert k['status'] == 'open' and k['parent_id'] == P, f'force_close cascaded to {kid}: {k}'
    # Reopening the parent brings the hold straight back, from the same still-open children.
    assert post({'id': P, 'status': 'open'})['ok'] is True
    p = row(P)
    assert p['open_children'] == 2 and p['completable'] is False, p
    # A parent that is itself waiting reports the waiting hold first — the panel's existing
    # confirm flow for parked rows is unchanged.
    assert post({'id': P, 'state': 'waiting', 'waiting_on': 'the county'})['ok'] is True
    p = row(P)
    assert p['completion_hold'] == 'waiting' and p['completable'] is False, p
    assert post({'id': P, 'state': ''})['ok'] is True
    assert row(P)['completion_hold'] == 'children'
    # Children can be moved off the parent explicitly (no automatic moves happen anywhere).
    with db._conn() as cn, cn.cursor() as cur_:
        cur_.execute("UPDATE daybank_items SET parent_id = NULL WHERE id = %s", (C2,))
    assert row(P)['open_children'] == 1
    # A record parent keeps its own hold and can take subtasks.
    R = seed('the permit file for the barn', entry='record')
    a = add({'text': 'scan the signed permit', 'parent_id': R})
    assert a['ok'] is True, a
    rr = row(R)
    assert rr['open_children'] == 1 and rr['completable'] is False, rr
    assert rr['completion_hold'] == 'reference', rr['completion_hold']
    # Explicit list wins over inheritance.
    a = add({'text': 'book the inspector', 'parent_id': P, 'bucket': 'Personal'})
    assert a['ok'] is True and row(a['id'])['bucket'] == 'Personal', a

    # PARENT ADD SAFETY — every wrong parent is refused with a reason, on the route and at
    # the shared boundary, and nothing is inserted.
    D = seed('a finished job', status='done')
    X = seed('an archived twin', status='dropped')
    n_before = len(board()['items'])
    a = add({'text': 'a subtask of nothing', 'parent_id': 'nope42'})
    assert a['ok'] is False and 'nope42' in a['error'] and 'open parent' in a['error'], a
    a = add({'text': 'a subtask of finished work', 'parent_id': D})
    assert a['ok'] is False and 'done' in a['error'] and D in a['error'], a
    a = add({'text': 'a subtask of an archived row', 'parent_id': X})
    assert a['ok'] is False and 'dropped' in a['error'], a
    with db._conn() as cn, cn.cursor() as cur_:
        cur_.execute("UPDATE daybank_items SET superseded_by = %s WHERE id = %s", (P, X))
    a = add({'text': 'a subtask of a merged row', 'parent_id': X})
    assert a['ok'] is False and 'merged' in a['error'], a
    assert len(board()['items']) == n_before, 'a refused subtask was inserted anyway'
    ok_, msg_ = db.add_item('todo', 'tool subtask of nothing', parent_id='nope42')
    assert ok_ is False and 'nope42' in msg_, msg_
    ok_, msg_ = db.add_item('todo', 'tool subtask of finished work', parent_id=D)
    assert ok_ is False and 'done' in msg_, msg_
    assert len(board()['items']) == n_before

    # ══ 4. QUICK-ADD REFUSALS NAME THE EXISTING ROW ══════════════════════════════════
    a = add({'text': 'Call the county about the permit', 'bucket': 'Side Work'})
    assert a['ok'] is True and a['dup'] is False and a.get('id'), a
    Q = a['id']
    # Verbatim repeat: not inserted, and the answer says which row it already is.
    a = add({'text': 'call the county about the permit', 'bucket': 'Side Work'})
    assert a['ok'] is True and a['dup'] is True, a
    assert a['existing']['id'] == Q and a['existing']['status'] == 'open', a['existing']
    assert a['existing']['text'] == 'Call the county about the permit'
    assert Q in a['message'] and 'Already on your board' in a['message'], a['message']
    assert 'error' not in a, 'a dup is a refusal to insert, not a failure'
    # Near-twin with different detail: NOT saved, and the reply carries both sides.
    n_before = len(board()['items'])
    a = add({'text': 'Call the county about the permit!', 'bucket': 'Side Work'})
    assert a['ok'] is False and a.get('needs_review') is True and a['dup'] is False, a
    assert a['existing']['id'] == Q and a['existing']['text'] == 'Call the county about the permit'
    assert a['requested']['text'] == 'Call the county about the permit!', a['requested']
    assert Q in a['error'] and a['error'].startswith('Not saved'), a['error']
    assert 'Edit that row' in a['error'], a['error']
    assert 'summary' in a and all('lane' in x for x in a['items']), 'the refusal dropped the board'
    assert len(board()['items']) == n_before, 'a needs_review add was inserted'
    # Empty text and an unknown list are refused out loud too.
    a = add({'text': '   '})
    assert a['ok'] is False and a['error'], a
    a = add({'text': 'file it somewhere that does not exist', 'bucket': 'Nowhere'})
    assert a['ok'] is False and 'Nowhere' in a['error'], a
    # A subtask that repeats its sibling names the sibling; the same words under a
    # DIFFERENT parent are different work and insert.
    a = add({'text': 'submit the drainage drawing', 'parent_id': P})
    assert a['ok'] is True and a['dup'] is True and a['existing']['id'] == C1, a
    assert a['existing']['parent_id'] == P
    a = add({'text': 'submit the drainage drawing', 'parent_id': R})
    assert a['ok'] is True and a['dup'] is False and row(a['id'])['parent_id'] == R, a

    print('PASS: clearing a text-derived due sticks (marker names the cleared date; a '
          'different date in the wording shows; omitted due neither touches nor freezes; '
          'a set date drops the marker; verified path agrees); expected_updated_at is an '
          'atomic optimistic lock ("" for NULL, omitted = no check, stale → ok:false '
          'conflict:true with the current row and nothing written, one winner in a real '
          'race, malformed refused as a client error); a parent with open subtasks is '
          'completable:false / completion_hold:children on the board and in Today, an '
          'ordinary tick is NOT COMPLETED naming the children, force_close still closes it '
          'with no cascade, closing a child leaves the parent open; wrong parents are '
          'refused with a reason on the route and at the shared boundary; quick-add dup and '
          'near-twin refusals name the existing row and the requested values.')
finally:
    server.cleanup(); tmp.cleanup()
