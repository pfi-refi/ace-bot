"""Board REPAIR check — the state field, end to end, through the real routes.

Three defects this file exists to keep dead (2026-09-21):

  1. An ACTION could never be parked on someone else. The editor offers
     "WAITING — parked on someone else" and the route refused it outright, so the save
     came back "state applies to records, not actions" — while classify.lane_of has keyed
     the WAITING lane off `state` alone all along.
  2. READY could never clear a stored state. `state` was a plain `str = ""`, so "omitted"
     and "cleared" were the same value and both were dropped: a waiting or undecided row
     answered ok:true and stayed exactly where it was.
  3. The Drive fallback took entry/state/waiting_on/bucket/chosen_on/reviewed and threw
     them away while answering ok — the silent-write class daybank.py's own comments exist
     to kill.

Everything here goes through the ACTUAL HTTP path and is verified by READBACK from a fresh
GET, never from the POST's own echo. Real disposable PostgreSQL (pgserver), synthetic rows
only. No production URL, no Google credentials, no model calls, no network.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'ace2'))
import pgserver                                        # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402

tmp = tempfile.TemporaryDirectory(prefix='ace-repair-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    os.environ.pop('ACE2_PASSWORD', None)
    from ace2.backend import db, daybank, classify          # noqa: E402
    from ace2.backend.main import app                       # noqa: E402
    db._init_schema(); db._ready = True; db._trgm_ok = False

    import datetime as _dt
    import json as _j
    import uuid
    from datetime import datetime, timedelta, timezone

    # Synthetic rows. The first five carry no waiting phrase and no date, so they exercise the
    # write path rather than db's prose heuristics. The last two are the OPPOSITE on purpose:
    # their wording is the shape of Brady's live parked rows, which is what db._derive_state
    # reads when the column is NULL — the case a clear has to survive.
    FIX = [
        ('call the excavator about the driveway', ['Business'], 'action', None, 'Side Work'),
        ('choose a color for the trim', ['Business'], 'action', None, 'Side Work'),
        # seeded with NO state, so section 3's first transition is a real write and not a
        # restatement of what the column already said
        ('the permit file for the barn', ['Business'], 'record', None, 'Side Work'),
        ('parent record for the pour', ['Deals'], 'record', None, 'GFI/PFI'),
        ('order gravel for the pad', ['Business'], 'action', None, 'Side Work'),
        ('Thiami — everything submitted, waiting on approval', ['Deals'], 'record', None,
         'GFI/PFI'),
        ('chase the title company, still pending', ['Deals'], 'action', None, 'GFI/PFI'),
    ]
    ids = {}
    with db._conn() as c, c.cursor() as cur:
        for n, (text, tags, entry, state, bucket) in enumerate(FIX):
            i = uuid.uuid4().hex[:6]; ids[text] = i
            cur.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,entry,state,"
                        "bucket) VALUES(%s,%s,'todo',%s,'open',%s::jsonb,%s,%s,%s)",
                        (i, datetime.now(timezone.utc) - timedelta(days=n), text,
                         _j.dumps(tags), entry, state, bucket))
    A_WAIT = ids['call the excavator about the driveway']
    A_DEC = ids['choose a color for the trim']
    REC = ids['the permit file for the barn']
    PARENT = ids['parent record for the pour']
    A_DUE = ids['order gravel for the pad']
    W_REC = ids['Thiami — everything submitted, waiting on approval']
    W_ACT = ids['chase the title company, still pending']

    c = TestClient(app)

    def post(body):
        return c.post('/daybank/update', json=body).json()

    def row(item_id):
        """The row as a FRESH read of the board sees it — never the POST's own echo."""
        return next((x for x in c.get('/daybank?all=true').json()['items']
                     if x['id'] == item_id), None)

    def raw_state(item_id):
        """The stored columns, not the read-time derivation — the only way to prove what a
        clear actually wrote. READY/ACTIVE is STORED as 'active', never NULLed: a NULL state
        is re-derived from the row's wording on the next read, which is how ACTIVE used to
        turn straight back into WAITING."""
        with db._conn() as cn, cn.cursor() as cur_:
            cur_.execute("SELECT state, waiting_on FROM daybank_items WHERE id = %s", (item_id,))
            return cur_.fetchone()

    def today_agrees(note):
        """Every row Today shows carries the lane the full board gives the same id."""
        full = {x['id']: x['lane'] for x in c.get('/daybank?all=true').json()['items']}
        dt = c.get('/daybank').json()['due_today']
        for grp in ('deadlines', 'chosen', 'suggested', 'waiting'):
            for x in dt[grp]:
                assert full[x['id']] == x['lane'], \
                    f'{note}: Today and the board disagree on {x["id"]} ' \
                    f'({x["lane"]} vs {full[x["id"]]})'
        return dt

    board_ids = {x['id'] for x in c.get('/daybank?all=true').json()['items']}

    # ── 1. WAITING → READY on an ACTION ────────────────────────────────────────────
    # The defect: this POST came back ok:false, "state applies to records, not actions".
    r = post({'id': A_WAIT, 'entry': 'action', 'state': 'waiting', 'waiting_on': 'Tony'})
    assert r['ok'] is True, f'an action could not be parked on someone else: {r.get("error")!r}'
    w = row(A_WAIT)
    assert w['entry'] == 'action', f'the kind was changed: {w["entry"]}'
    assert w['state'] == 'waiting', f'waiting did not persist: {w["state"]!r}'
    assert w['waiting_on'] == 'Tony', f'the owner did not persist: {w["waiting_on"]!r}'
    assert w['lane'] == classify.LANE_WAITING, w['lane']
    assert w['completable'] is False, 'a parked action must not offer a checkbox'
    today_agrees('action parked')

    # ...and READY brings it back, which is the transition that silently did nothing.
    r = post({'id': A_WAIT, 'entry': 'action', 'state': ''})
    assert r['ok'] is True, r.get('error')
    w = row(A_WAIT)
    # READY reads as ACTIVE — the neutral end of the lifecycle, stored rather than derived.
    assert w['state'] == 'active', f'READY did not clear the state: {w["state"]!r}'
    assert not w['waiting_on'], f'a Ready row kept a stale owner: {w["waiting_on"]!r}'
    assert raw_state(A_WAIT) == ('active', None), \
        f'the clear did not reach the columns: {raw_state(A_WAIT)}'
    assert w['lane'] in classify.ACTIONABLE, f'it did not come back as work: {w["lane"]}'
    assert w['completable'] is True, 'a Ready action must be completable again'
    assert not classify.has_next_step(w), 'a cleared owner still counted as the next move'
    today_agrees('action back to ready')

    # ── 2. DECIDE → READY ──────────────────────────────────────────────────────────
    r = post({'id': A_DEC, 'state': 'decide'})
    assert r['ok'] is True, r.get('error')
    d = row(A_DEC)
    assert d['state'] == 'decide' and d['lane'] == classify.LANE_UNDECIDED, d['lane']
    assert d['needs_decision'] is True, 'the open-question mark was lost'
    r = post({'id': A_DEC, 'state': ''})
    assert r['ok'] is True, r.get('error')
    d = row(A_DEC)
    assert d['state'] == 'active', f'the decision mark survived READY: {d["state"]!r}'
    assert d['needs_decision'] is False and d['lane'] != classify.LANE_UNDECIDED, d['lane']
    assert raw_state(A_DEC)[0] == 'active'

    # ── 3. THE RECORD LIFECYCLE STILL RUNS END TO END ──────────────────────────────
    # The column starts empty, so ACTIVE here is a real write — a record reads as ACTIVE
    # either way, and only the stored column can tell the two apart.
    assert raw_state(REC)[0] is None, 'the fixture already carried a state'
    assert post({'id': REC, 'state': 'active'})['ok'] is True
    assert raw_state(REC)[0] == 'active', 'ACTIVE was not written'
    assert row(REC)['lane'] == classify.LANE_REFERENCE, row(REC)['lane']
    assert post({'id': REC, 'state': 'waiting', 'waiting_on': 'the county'})['ok'] is True
    m = row(REC)
    assert m['lane'] == classify.LANE_WAITING and m['waiting_on'] == 'the county', m
    assert m['completable'] is False
    assert post({'id': REC, 'state': 'settled'})['ok'] is True
    m = row(REC)
    assert m['lane'] == classify.LANE_SETTLED, m['lane']
    assert not m['waiting_on'], f'a settled record kept its old owner: {m["waiting_on"]!r}'
    assert post({'id': REC, 'state': ''})['ok'] is True
    assert raw_state(REC)[0] == 'active', 'the settled record did not come back to ACTIVE'
    m = row(REC)
    assert m['state'] == 'active' and m['lane'] == classify.LANE_REFERENCE, m

    # ── 4. 'settled' IS STILL REFUSED ON AN ACTION, AND NOTHING IS WRITTEN ─────────
    before = row(A_DEC)
    bad = post({'id': A_DEC, 'entry': 'action', 'state': 'settled'})
    assert bad['ok'] is False, 'an action was allowed a record lifecycle'
    assert 'settled' in bad['error'] and 'record' in bad['error'].lower(), bad['error']
    after = row(A_DEC)
    assert (after['state'], after['lane'], after['text']) == \
           (before['state'], before['lane'], before['text']), 'a refused edit still wrote'
    assert raw_state(A_DEC)[0] == 'active', 'a refused edit reached the database'
    # ...and the guard does not depend on the client volunteering `entry`: the stored kind
    # answers when the request is silent.
    bad = post({'id': A_DEC, 'state': 'settled'})
    assert bad['ok'] is False and 'settled' in bad['error'], bad
    assert raw_state(A_DEC)[0] == 'active'
    # An unrecognised state keeps the old error shape, listing what is valid.
    bad = post({'id': A_DEC, 'state': 'parked'})
    assert bad['ok'] is False and "'parked'" in bad['error'], bad
    assert 'waiting' in bad['error'] and 'decide' in bad['error'], bad['error']
    # The same refusal holds at the shared boundary Ace's own tool calls.
    ok_, msg_ = db.update_item(A_DEC, state='parked')
    assert ok_ is False and 'unknown state' in msg_, msg_

    # ── 5. REGRESSION GUARD: AN OMITTED `state` MUST NEVER CLEAR ONE ──────────────
    # The checkbox handler posts {id, status} and nothing else. If "omitted" were read as
    # "clear", every tick would quietly un-park a row.
    assert post({'id': A_WAIT, 'state': 'waiting', 'waiting_on': 'Tony'})['ok'] is True
    assert post({'id': A_WAIT, 'text': 'call the excavator about the driveway again'})['ok']
    w = row(A_WAIT)
    assert w['text'].endswith('again'), 'the text edit did not persist'
    assert (w['state'], w['waiting_on']) == ('waiting', 'Tony'), \
        f'an update with no state field changed the state: {w["state"]!r}/{w["waiting_on"]!r}'
    assert w['lane'] == classify.LANE_WAITING
    r = post({'id': A_WAIT, 'status': 'done', 'force_close': True})
    assert r['ok'] is True, r.get('error')
    w = row(A_WAIT)
    assert w['status'] == 'done' and w['state'] == 'waiting', \
        f'closing the row rewrote its state: {w["state"]!r}'
    assert post({'id': A_WAIT, 'status': 'open'})['ok'] is True
    w = row(A_WAIT)
    assert w['state'] == 'waiting' and w['waiting_on'] == 'Tony', w
    assert post({'id': A_WAIT, 'state': ''})['ok'] is True      # leave it Ready for later

    # ── 6. TEXT AND DUE THROUGH THE ROUTE, VERIFIED BY READBACK ──────────────────
    today = c.get('/daybank').json()['today']
    r = post({'id': A_DUE, 'text': 'order gravel for the pad and the apron'})
    assert r['ok'] is True and r['saved']['text'] == 'order gravel for the pad and the apron', r
    assert row(A_DUE)['text'] == 'order gravel for the pad and the apron'
    r = post({'id': A_DUE, 'due': today})
    assert r['saved']['due'] == today, r['saved']['due']
    g = row(A_DUE)
    assert g['due'] == today and g['due_days'] == 0 and g['lane'] == classify.LANE_TODAY, g
    r = post({'id': A_DUE, 'due': ''})
    assert not r['saved']['due'], r['saved']['due']
    g = row(A_DUE)
    assert not g['due'] and g['due_days'] is None, g
    assert g['lane'] == classify.LANE_ANYTIME, g['lane']

    # STATE AND CATEGORY/TAGS IN ONE SAVE. The editor never sends them apart, and the route
    # now resolves the row ONCE for both the kind check and the tag merge — so the save that
    # uses both is the one that proves the single lookup serves them both.
    r = post({'id': A_DUE, 'state': 'waiting', 'waiting_on': 'the supplier',
              'category': 'Admin', 'tags': ['Tech']})
    assert r['ok'] is True, r.get('error')
    g = row(A_DUE)
    assert g['state'] == 'waiting' and g['waiting_on'] == 'the supplier', g
    assert g['tags'][0] == 'Admin' and 'Tech' in g['tags'], g['tags']
    assert g['lane'] == classify.LANE_WAITING, g['lane']
    r = post({'id': A_DUE, 'state': '', 'category': 'Business', 'tags': ['Tech']})
    assert r['ok'] is True, r.get('error')
    g = row(A_DUE)
    assert raw_state(A_DUE) == ('active', None), raw_state(A_DUE)
    assert g['tags'][0] == 'Business' and 'Tech' in g['tags'], g['tags']
    assert g['lane'] == classify.LANE_ANYTIME, g['lane']

    # ── 7. THE COMPLETION GUARD IS UNTOUCHED ─────────────────────────────────────
    assert post({'id': REC, 'state': 'waiting', 'waiting_on': 'the county'})['ok'] is True
    r = post({'id': REC, 'status': 'done'})
    assert r['ok'] is False and r.get('blocked') is True, f'a waiting row closed by checkbox: {r}'
    assert r['error'].startswith('NOT COMPLETED'), r['error']
    assert 'the county' in r['error'], r['error']
    assert row(REC)['status'] == 'open', 'the blocked row was modified'
    r = post({'id': REC, 'status': 'done', 'force_close': True})
    assert r['ok'] is True and row(REC)['status'] == 'done', r
    assert post({'id': REC, 'status': 'open'})['ok'] is True
    # a child ACTION completing leaves its parent RECORD open and unchanged
    p_before = row(PARENT)
    add = c.post('/daybank/add', json={'text': 'sign the loader rental form'}).json()
    assert add['ok'] is True and add.get('dup') is False, add
    child = next(x for x in add['items'] if x['text'] == 'sign the loader rental form')['id']
    with db._conn() as cn, cn.cursor() as cur_c:
        cur_c.execute("UPDATE daybank_items SET parent_id=%s WHERE id=%s", (PARENT, child))
    assert post({'id': child, 'status': 'done'})['ok'] is True
    assert row(child)['status'] == 'done'
    p_after = row(PARENT)
    for f in ('status', 'state', 'entry', 'text', 'due', 'waiting_on', 'lane'):
        assert p_after[f] == p_before[f], f'completing a child changed the parent {f}'

    # ── 8. TODAY STAYS CONSISTENT ACROSS A TRANSITION ────────────────────────────
    assert post({'id': A_DUE, 'due': today})['ok'] is True
    dt = today_agrees('dated for today')
    assert A_DUE in [x['id'] for x in dt['deadlines']], 'a row due today is missing from Today'
    assert post({'id': A_DUE, 'state': 'waiting', 'waiting_on': 'the supplier'})['ok'] is True
    dt = today_agrees('parked while dated')
    assert A_DUE not in [x['id'] for x in dt['deadlines']], 'parked work still read as a deadline'
    assert A_DUE not in [x['id'] for x in dt['suggested']], 'parked work was suggested'
    assert A_DUE in [x['id'] for x in dt['waiting']], 'parked work vanished from Today'
    assert post({'id': A_DUE, 'state': ''})['ok'] is True
    dt = today_agrees('reopened')
    assert A_DUE in [x['id'] for x in dt['deadlines']], 'reopening lost the deadline'
    assert not row(A_DUE)['waiting_on'], 'the supplier outlived the waiting state'
    assert row(A_DUE)['due'] == today, 'a state change rewrote the deadline'
    # Ids are stable through every one of those transitions — nothing was cloned or replaced.
    assert board_ids <= {x['id'] for x in c.get('/daybank?all=true').json()['items']}, \
        'a row lost its id somewhere in the transitions'

    # ── 9. reviewed:true RETIRES THE CARRIED-OVER FLAG, INVENTING NOTHING ────────
    legacy = uuid.uuid4().hex[:6]
    _b = _dt.datetime.fromisoformat(db.review_boundary()) - _dt.timedelta(days=2)
    with db._conn() as cn, cn.cursor() as cur_l:
        cur_l.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,entry) "
                      "VALUES(%s,%s,'todo','a row from the old board','open','[]'::jsonb,"
                      "'action')", (legacy, _b))
    assert row(legacy)['carried_over'] is True, 'the fixture is not a legacy row'
    assert post({'id': legacy, 'reviewed': True})['ok'] is True
    lg = row(legacy)
    assert lg['carried_over'] is False and lg['reviewed_at'], lg
    assert not lg['due'] and not lg['next_step'], 'data was invented to clear the flag'
    assert not lg['state'], 'reviewing a row gave it a state it never had'

    # ── 10. A WRITE WITHOUT CREDENTIALS IS REFUSED ───────────────────────────────
    # require_auth reads the environment per request, so one app instance can be locked and
    # unlocked in place — no second process, no real password anywhere.
    os.environ['ACE2_PASSWORD'] = 'synthetic-check-password'
    os.environ.pop('ACE2_ALLOW_OPEN', None)
    try:
        resp = c.post('/daybank/update', json={'id': A_DUE, 'text': 'written without a token'})
        assert resp.status_code == 401, f'an unauthenticated write was accepted: {resp.status_code}'
    finally:
        os.environ.pop('ACE2_PASSWORD', None)
        os.environ['ACE2_ALLOW_OPEN'] = '1'
    assert row(A_DUE)['text'] != 'written without a token', 'the refused write landed anyway'

    # ── 11. THE DRIVE FALLBACK REFUSES WHAT IT CANNOT WRITE ──────────────────────
    # It applied status and text only, dropped every classification field, and answered ok.
    # db.enabled() is patched off WITHOUT touching Drive — the refusal comes before any call.
    _real = db.enabled
    db.enabled = lambda: False
    try:
        for kw in ({'entry': 'record'}, {'state': 'waiting'}, {'waiting_on': 'Tony'},
                   {'bucket': 'Side Work'}, {'chosen_on': today}, {'reviewed': True}):
            ok_, msg_ = daybank.update_item(A_DUE, **kw)
            assert ok_ is False, f'the Drive path answered ok to {kw} it cannot write'
            assert 'Postgres' in msg_, msg_
    finally:
        db.enabled = _real
    assert row(A_DUE)['state'] == 'active', 'the fallback check disturbed the row'

    # ── 12. THE CLEAR HAS TO SURVIVE THE READ-TIME DERIVATION ────────────────────
    # read_items re-derives a NULL state on a record (db._derive_state), and this row is worded
    # the way Brady's live parked rows are — so a clear that only NULLed the column came back
    # as WAITING on the very next read, with an owner re-read out of the prose. Storing his
    # correction is the fix: an explicit value always beats the derivation.
    d0 = row(W_REC)
    assert d0['lane'] == classify.LANE_WAITING, f'the fixture is not waiting-worded: {d0}'
    assert post({'id': W_REC, 'state': 'waiting', 'waiting_on': 'Thiami'})['ok'] is True
    assert row(W_REC)['waiting_on'] == 'Thiami'
    assert post({'id': W_REC, 'state': ''})['ok'] is True
    back = row(W_REC)
    assert back['state'] == 'active', f'ACTIVE did not stick on a parked-sounding row: {back}'
    assert not back['waiting_on'], f'the owner was derived straight back: {back["waiting_on"]!r}'
    assert back['lane'] == classify.LANE_REFERENCE, back['lane']
    assert raw_state(W_REC) == ('active', None), raw_state(W_REC)
    today_agrees('waiting-worded record set active')

    # ...and the same durability for decide → READY on an ACTION worded the same way.
    assert post({'id': W_ACT, 'state': 'decide'})['ok'] is True
    assert row(W_ACT)['lane'] == classify.LANE_UNDECIDED, row(W_ACT)['lane']
    assert post({'id': W_ACT, 'state': ''})['ok'] is True
    a_back = row(W_ACT)
    assert a_back['state'] == 'active' and a_back['needs_decision'] is False, a_back
    assert a_back['lane'] in classify.ACTIONABLE and a_back['completable'] is True, a_back['lane']
    assert raw_state(W_ACT) == ('active', None), raw_state(W_ACT)
    # The state derivation is gated on entry == 'record', so an action only meets it when its
    # own `entry` column is NULL and its text reads parked — this row with the column emptied.
    # The KIND still derives to a record there (a known limit, not this fix's business), so
    # the LANE is not the signal: the stored state is. It must still read ACTIVE.
    with db._conn() as cn, cn.cursor() as cur_e:
        cur_e.execute("UPDATE daybank_items SET entry = NULL WHERE id = %s", (W_ACT,))
    bare = row(W_ACT)
    assert bare['state'] == 'active', f'the stored state was derived away: {bare["state"]!r}'
    assert not bare['waiting_on'], f'an owner was derived back onto it: {bare["waiting_on"]!r}'
    with db._conn() as cn, cn.cursor() as cur_e:
        cur_e.execute("UPDATE daybank_items SET entry = 'action' WHERE id = %s", (W_ACT,))

    # ── 13. THE REFUSAL TELLS EACH KIND SOMETHING IT CAN ACT ON ──────────────────
    # 'settled' is refused on an action, so the record's way out — "set state='settled'" —
    # was advice into a second refusal once an action could be parked at all.
    assert post({'id': W_ACT, 'state': 'waiting',
                 'waiting_on': 'the title company'})['ok'] is True
    r = post({'id': W_ACT, 'status': 'done'})
    assert r['ok'] is False and r.get('blocked') is True, r
    assert r['error'].startswith('NOT COMPLETED'), r['error']
    assert 'the title company' in r['error'], r['error']
    assert 'do not tell him it is done' in r['error'].lower(), r['error']
    assert 'settled' not in r['error'], f'a parked action was sent to a dead end: {r["error"]}'
    assert 'force_close' in r['error'], r['error']
    assert row(W_ACT)['status'] == 'open', 'the blocked action was modified'
    assert post({'id': W_ACT, 'status': 'done', 'force_close': True})['ok'] is True
    assert row(W_ACT)['status'] == 'done'
    assert post({'id': W_ACT, 'status': 'open', 'state': ''})['ok'] is True
    # a waiting RECORD keeps the lifecycle advice, word for word
    assert post({'id': W_REC, 'state': 'waiting', 'waiting_on': 'Thiami'})['ok'] is True
    r = post({'id': W_REC, 'status': 'done'})
    assert r['ok'] is False and r['error'].startswith('NOT COMPLETED'), r['error']
    assert "set state='settled'" in r['error'], f'a record lost its way out: {r["error"]}'
    assert 'Thiami' in r['error'], r['error']
    assert 'do not tell him it is done' in r['error'].lower(), r['error']
    assert row(W_REC)['status'] == 'open'

    # ── 14. 'settled' ON AN ACTION IS REFUSED AT THE SHARED BOUNDARY TOO ─────────
    # Ace's own update_item tool calls db directly and never meets the route's guard — the
    # same two-tier split the completion rule was moved down here to end.
    before_raw = raw_state(A_DEC)
    ok_, msg_ = db.update_item(A_DEC, state='settled')
    assert ok_ is False, 'the shared boundary settled an action'
    assert 'settled' in msg_ and 'record' in msg_.lower(), msg_
    assert raw_state(A_DEC) == before_raw, 'a refused settle still wrote'
    # ...and the same call that says what kind it is goes through
    ok_, _ = db.update_item(A_DEC, entry='record', state='settled')
    assert ok_ is True, 'a record could not be settled at the boundary'
    assert raw_state(A_DEC)[0] == 'settled'
    assert row(A_DEC)['lane'] == classify.LANE_SETTLED, row(A_DEC)['lane']
    ok_, _ = db.update_item(A_DEC, entry='action', state='')
    assert ok_ is True and raw_state(A_DEC) == ('active', None), raw_state(A_DEC)

    # ── 15. "" IS THE PANEL'S WORD, NOT ACE'S ───────────────────────────────────
    # The two surfaces differ deliberately: on the route "" is the editor's READY option and
    # CLEARS; through the tool it is a model filling an optional it is not using, and must do
    # nothing. Pin both, because the difference is the whole safety of the parked lane.
    from ace2.backend import tools                       # noqa: E402
    assert post({'id': W_REC, 'state': 'waiting', 'waiting_on': 'Thiami'})['ok'] is True
    assert raw_state(W_REC) == ('waiting', 'Thiami'), raw_state(W_REC)
    out = tools.execute('update_item', {'id': W_REC, 'state': ''})
    assert raw_state(W_REC) == ('waiting', 'Thiami'), \
        f'the tool un-parked a row with an empty optional: {raw_state(W_REC)}'
    assert 'Updated' not in str(out), f'the tool claimed a change it did not make: {out!r}'
    # ...and an empty state riding along with a REAL edit changes only the real edit
    out = tools.execute('update_item', {'id': W_REC, 'text': 'Thiami — submitted, waiting on '
                                        'approval', 'state': '', 'waiting_on': ''})
    assert raw_state(W_REC) == ('waiting', 'Thiami'), \
        f'an empty optional erased the owner: {raw_state(W_REC)}'
    assert row(W_REC)['text'] == 'Thiami — submitted, waiting on approval'
    assert row(W_REC)['lane'] == classify.LANE_WAITING, 'the row lost its protection'
    # the panel's "" still clears, on the same row, through the route
    assert post({'id': W_REC, 'state': ''})['ok'] is True
    assert raw_state(W_REC) == ('active', None), raw_state(W_REC)

    # ── 16. UNKNOWN KIND REFUSES WITHOUT MUTATION ──────
    # Unknown evidence must not permit a lifecycle change or suggest a kind conversion.
    assert post({'id': REC, 'state': 'active'})['ok'] is True
    _real_read = db.read_items
    db.read_items = lambda *a, **k: []
    try:
        ok_, msg_ = db.update_item(REC, state='settled')
        assert ok_ is False and 'Unable to verify' in msg_, msg_
    finally:
        db.read_items = _real_read
    assert raw_state(REC)[0] == 'active', raw_state(REC)
    assert post({'id': REC, 'state': ''})['ok'] is True
    # an unknown id gets the accurate answer from the write, not a lifecycle lecture
    ok_, msg_ = db.update_item('does-not-exist', state='settled')
    assert ok_ is False and 'Unable to verify' in msg_, msg_
    assert 'RECORD lifecycle' not in msg_, msg_

    # ── 17. A REFUSAL THE STORE WROTE REACHES THE PANEL WITH ITS REASON ──────────
    # Only NOT COMPLETED carried one; everything else answered ok:false with nothing to
    # show, and the editor rendered it as a bare RETRY SAVE.
    r = post({'id': 'no-such-row', 'text': 'nowhere to write this'})
    assert r['ok'] is False, r
    assert 'no item' in (r.get('error') or ''), f'a refusal arrived with no reason: {r.get("error")!r}'
    assert r.get('blocked') is not True, 'an ordinary refusal was reported as a block'
    assert 'summary' in r and all('lane' in x for x in r['items']), 'the refusal dropped the board'
    r = post({'id': A_WAIT})
    assert r['ok'] is False and 'nothing to update' in (r.get('error') or ''), r.get('error')
    assert row(A_WAIT)['status'] == 'open', 'an empty update touched the row'

    print('PASS: an ACTION can be parked on someone else and brought back to READY (state '
          'stored ACTIVE, owner NULL, lane actionable, completable again); decide clears the '
          'same way; READY sticks even on a row worded like a parked one, where a NULL used to '
          'be re-derived to WAITING; the record lifecycle still runs active → waiting → '
          'settled → active; settled stays refused on an action by the row\'s own kind, on the '
          'route AND at the shared boundary, with no write; an update that omits state never '
          'touches a stored one; text and due edit and clear by readback; the completion guard '
          'holds, keeps NOT COMPLETED and the owner\'s name, offers a record settled and an '
          'action force_close, and leaves parent rows alone; Today and the board agree row for '
          'row through every transition; reviewed retires the carried-over flag inventing '
          'nothing; an unauthenticated write is 401; and the Drive fallback refuses the fields '
          'it used to drop.')
finally:
    server.cleanup(); tmp.cleanup()
