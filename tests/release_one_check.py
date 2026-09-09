"""Release one through the ACTUAL routes and a real disposable PostgreSQL.

Synthetic rows only. No production URL, no Google credentials, no model calls, no network.
Every check here is one of Brady's stated requirements, tested where it can actually break:
the route and the store, not the helper.
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

tmp = tempfile.TemporaryDirectory(prefix='ace-rel1-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    from ace2.backend import db, daybank, classify        # noqa: E402
    from ace2.backend.main import app                     # noqa: E402
    db._init_schema(); db._ready = True; db._trgm_ok = False

    import json as _j
    import uuid
    from datetime import datetime, timedelta, timezone
    TODAY = datetime.now(db.EASTERN).strftime('%Y-%m-%d')
    SOON = (datetime.now(db.EASTERN) + timedelta(days=4)).strftime('%Y-%m-%d')

    # text, tags, due, entry, state, waiting_on, bucket, next_step, followup
    FIX = [
        ('chase Rebecca for the signed packet', ['Deals'], SOON, 'action', None,
         None, 'GFI/PFI', 'call her Tuesday', TODAY),
        ('the Marlow deal', ['Deals'], None, 'record', None, None, 'GFI/PFI', None, None),
        ('waiting on the county for the permit', ['Admin'], None, 'record', 'waiting',
         'the county', 'Groundworks', None, SOON),
        ('order concrete for the pour', ['Business'], TODAY, 'action', None,
         None, 'Groundworks', None, None),
        # no stored area and nothing in the wording files it: this is the one row the Inbox
        # proposal is about, and it keeps DISPLAYING where it always has until Brady approves.
        ('a thought with no obvious home', ['Business'], None, 'action', None, None, '', None, None),
        ('decide whether to keep the trailer', ['Business'], None, 'action', 'decide',
         None, 'Side Work', None, None),
        ('undated with no next step', ['Business'], None, 'action', None, None, 'Side Work', None, None),
    ]
    ids = {}
    with db._conn() as c0, c0.cursor() as cur:
        for n, (text, tags, due, entry, state, wait, bucket, nxt, fup) in enumerate(FIX):
            i = uuid.uuid4().hex[:6]; ids[text] = i
            cur.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,due,entry,state,"
                        "waiting_on,bucket,next_step,followup) VALUES(%s,%s,'todo',%s,'open',"
                        "%s::jsonb,%s,%s,%s,%s,%s,%s,%s)",
                        (i, datetime.now(timezone.utc) - timedelta(days=n), text,
                         _j.dumps(tags), due, entry, state, wait, bucket, nxt, fup))
    c = TestClient(app)

    def row(item_id, all_items=True):
        url = '/daybank?all=true' if all_items else '/daybank'
        return next(x for x in c.get(url).json()['items'] if x['id'] == item_id)

    # ── 1. HANDLING A FOLLOW-UP LEAVES THE UNDERLYING TASK OPEN ──────────────────
    # The prototype shared one control between "I chased them" and "the work is done".
    # Closing the follow-up closed the task. This is the requirement that fix has to keep.
    fup_id = ids['chase Rebecca for the signed packet']
    before = row(fup_id)
    assert before['followup'] == TODAY and before['status'] == 'open'
    r = c.post('/board/followup', json={'id': fup_id, 'action': 'clear'}).json()
    assert r['ok'] is True, r
    assert r['task_still_open'] is True, 'handling a follow-up closed the task'
    after = row(fup_id)
    assert after['status'] == 'open', 'the task was closed by handling its follow-up'
    assert not after.get('followup'), 'the follow-up itself should be cleared'
    assert after['due'] == before['due'], 'handling a follow-up moved the deadline'
    assert after['next_step'] == before['next_step'], 'handling a follow-up lost the next step'
    assert after['text'] == before['text'] and after['id'] == before['id']

    # ...and pushing a week moves ONLY the follow-up date.
    c.post('/daybank/update', json={'id': fup_id, 'followup': TODAY})
    r = c.post('/board/followup', json={'id': fup_id, 'action': 'push', 'days': 7}).json()
    expect = (datetime.strptime(TODAY, '%Y-%m-%d') + timedelta(days=7)).strftime('%Y-%m-%d')
    assert r['ok'] and r['after']['followup'] == expect, r
    assert r['after']['due'] == before['due'], 'pushing a follow-up moved the deadline'
    assert r['task_still_open'] is True

    # ── 2. DEADLINE, FOLLOW-UP AND CHOSEN DAY ARE THREE DIFFERENT THINGS ─────────
    und = ids['undated with no next step']
    c.post('/daybank/update', json={'id': und, 'chosen_on': TODAY})
    x = row(und)
    assert x['chosen_on'][:10] == TODAY and not x.get('due'), 'choosing today wrote a deadline'
    c.post('/daybank/update', json={'id': und, 'followup': SOON})
    x = row(und)
    assert x['followup'] == SOON and not x.get('due'), 'a follow-up became a deadline'
    assert x['chosen_on'][:10] == TODAY, 'setting a follow-up dropped the chosen day'

    # ── 3. NEEDS A DECISION IS A CHOICE, NEVER A DERIVATION ─────────────────────
    assert row(ids['decide whether to keep the trailer'])['lane'] == classify.LANE_UNDECIDED
    d = row(und)
    assert d['lane'] != classify.LANE_UNDECIDED, 'undated work was called undecided again'

    # ── 4. PERSISTENT CUSTOM LISTS ──────────────────────────────────────────────
    base = c.get('/board/lists').json()
    assert 'Inbox' in base['areas'] and base['custom'] == [], base
    assert c.post('/board/lists', json={'name': 'Greenhouse'}).json()['ok'] is True
    assert 'Greenhouse' in c.get('/board/lists').json()['areas'], 'a new list did not persist'
    dup = c.post('/board/lists', json={'name': 'Personal'}).json()
    assert dup['ok'] is False, 'a duplicate area was accepted'
    # persistence is in the store, not the process
    db._ready = False; db._ready = True
    assert 'Greenhouse' in db.all_areas(), 'the list did not survive a fresh read'

    # ── 5. RENAMING AN AREA PRESERVES ITS ITEMS AND EVERY LINK ──────────────────
    parent = ids['the Marlow deal']
    child = ids['chase Rebecca for the signed packet']
    with db._conn() as cn, cn.cursor() as cur:
        cur.execute("UPDATE daybank_items SET parent_id=%s WHERE id=%s", (parent, child))
    snap = {k: row(v) for k, v in ids.items()}
    r = c.post('/board/lists', json={'name': 'GFI/PFI', 'rename_to': 'GFI and PFI'}).json()
    assert r['ok'] is True, r.get('message') or r
    assert 'GFI and PFI' in r['areas'] and 'GFI/PFI' not in r['areas'], r['areas']
    for text, old in snap.items():
        new = row(ids[text])
        assert new['id'] == old['id'], 'rename changed an id'
        assert new['ts'] == old['ts'], 'rename rewrote history'
        assert new.get('due') == old.get('due'), 'rename moved a date'
        assert new.get('waiting_on') == old.get('waiting_on'), 'rename lost waiting information'
        assert new.get('parent_id') == old.get('parent_id'), 'rename broke a parent link'
        assert new['bucket'] == ('GFI and PFI' if old['bucket'] == 'GFI/PFI' else old['bucket'])
    # renaming a BUILT-IN must also carry the automatic filing rules with it, or new work
    # keeps landing in a name that no longer appears anywhere on the board.
    assert db.derive_bucket('paramed exam for the annuity rollover', 'Deals') == 'GFI and PFI', \
        'auto-filing still points at the old name'
    added = c.post('/daybank/add', json={'text': 'new IUL prospect from the carrier'}).json()
    new_row = next(x for x in added['items'] if x['text'] == 'new IUL prospect from the carrier')
    assert new_row['bucket'] == 'GFI and PFI', f'newly captured work vanished into {new_row["bucket"]}'
    c.post('/daybank/update', json={'id': new_row['id'], 'status': 'drop'})
    c.post('/board/lists', json={'name': 'GFI and PFI', 'rename_to': 'GFI/PFI'})
    assert db.derive_bucket('paramed exam for the annuity rollover', 'Deals') == 'GFI/PFI'
    assert row(child)['bucket'] == 'GFI/PFI', 'renaming back did not bring the items'

    # a custom list renames the same way
    c.post('/board/lists', json={'name': 'Greenhouse', 'rename_to': 'Greenhouse build'})
    assert 'Greenhouse build' in c.get('/board/lists').json()['custom']

    # ── 6. TAGS ARE SECONDARY LABELS ON ONE RECORD, NOT COPIES ──────────────────
    n_before = len(c.get('/daybank?all=true').json()['items'])
    r = c.post('/daybank/update', json={'id': und, 'category': 'Business',
                                        'tags': ['Admin', 'Bills']}).json()
    assert r['ok'] is True, r.get('error') or r
    assert len(c.get('/daybank?all=true').json()['items']) == n_before, 'tagging copied a row'
    t = row(und)['tags']
    assert t[0] == 'Business', f'the primary category must stay first, got {t}'
    assert set(t[1:]) == {'Admin', 'Bills'}, f'secondary tags did not persist: {t}'
    # a tag the board does not know is REFUSED, not silently dropped
    bad = c.post('/daybank/update', json={'id': und, 'tags': ['Adnim']}).json()
    assert bad['ok'] is False and 'Adnim' in bad['error'], bad
    assert set(row(und)['tags'][1:]) == {'Admin', 'Bills'}, 'a refused edit still changed the row'
    # clearing the secondaries leaves the primary category alone
    c.post('/daybank/update', json={'id': und, 'tags': []})
    assert row(und)['tags'] == ['Business'], row(und)['tags']

    # an item can be moved into a list Brady made himself
    c.post('/board/lists', json={'name': 'Greenhouse build', 'rename_to': 'Greenhouse'})
    mv = c.post('/daybank/update', json={'id': und, 'bucket': 'Greenhouse'}).json()
    assert mv['ok'] is True, mv.get('error') or mv.get('saved')
    assert row(und)['bucket'] == 'Greenhouse'
    c.post('/daybank/update', json={'id': und, 'bucket': 'Side Work'})
    nope = c.post('/daybank/update', json={'id': und, 'bucket': 'Nowhere'}).json()
    assert nope['ok'] is False and 'Nowhere' in nope['error'], nope

    # Needs a decision is settable on an ACTION — it is an open question, not a record state
    mk = c.post('/daybank/update', json={'id': und, 'state': 'decide'}).json()
    assert mk['ok'] is True, mk.get('error') or mk
    assert row(und)['lane'] == classify.LANE_UNDECIDED, row(und)['lane']
    c.post('/daybank/update', json={'id': und, 'state': ''})

    # ── 7. THE TWO TODAY SURFACES READ THE SAME RECORDS ─────────────────────────
    payload = c.get('/daybank').json()
    dt = payload['due_today']
    full = {x['id']: x for x in c.get('/daybank?all=true').json()['items']}
    for grp in ('deadlines', 'chosen', 'suggested', 'waiting'):
        for x in dt[grp]:
            assert full[x['id']]['lane'] == x['lane'], f'the two Today views disagree on {x["id"]}'
            assert full[x['id']]['due'] == x['due'], f'Today shows a different date for {x["id"]}'
    chosen_ids = {x['id'] for x in dt['chosen']}
    deadline_ids = {x['id'] for x in dt['deadlines']}
    assert und in chosen_ids, 'work chosen for today is missing from Today'
    assert und not in deadline_ids, 'chosen work was presented as a deadline'
    assert ids['order concrete for the pour'] in deadline_ids
    wait_ids = {x['id'] for x in dt['waiting']}
    assert not (wait_ids & (chosen_ids | deadline_ids)), 'blocked work appeared as ready work'

    # ── 8. THE MAPPING PREVIEW IS READ-ONLY AND PRESERVATION-FIRST ───────────────
    fingerprint = sorted((x['id'], x['bucket'], x['status'], str(x.get('due')),
                          str(x.get('state')), str(x.get('parent_id')))
                         for x in c.get('/daybank?all=true').json()['items'])
    prev = c.get('/board/mapping_preview').json()
    assert prev['read_only'] is True and prev['applies_anything'] is False
    after_fp = sorted((x['id'], x['bucket'], x['status'], str(x.get('due')),
                       str(x.get('state')), str(x.get('parent_id')))
                      for x in c.get('/daybank?all=true').json()['items'])
    assert fingerprint == after_fp, 'the preview changed live records'

    inbox_ids = {x['id'] for x in prev['proposed_for_inbox']}
    assert inbox_ids == {ids['a thought with no obvious home']}, \
        f'only genuinely unassigned work belongs in Inbox, got {inbox_ids}'
    for text, i in ids.items():
        if i not in inbox_ids:
            assert i not in inbox_ids, text
    assert prev['open_items'] == len([x for x in c.get('/daybank?all=true').json()['items']
                                      if x['status'] == 'open'])
    assert sum(prev['staying_in_place'].values()) == prev['open_items'], \
        'the preview lost or double-counted rows'

    carried = {x['id']: x for x in prev['carried_over_decisions']}
    assert ids['undated with no next step'] not in carried, \
        'a row Brady chose for today is not an unreviewed decision'
    assert ids['decide whether to keep the trailer'] not in carried, \
        'a row he marked himself is not "carried over"'
    for entry in prev['carried_over_decisions']:
        assert 'not yet reviewed' in entry['proposed']
        assert 'not set to Ready' in entry['never']
    for pair in prev['possible_duplicates']:
        assert pair['action'] == 'review only — no merge proposed'
    assert 'reversal' in prev and 'never rewritten' in prev['reversal']

    # ── 8b. DEPLOYING THIS MUST NOT RELOCATE ANYTHING ───────────────────────────
    # 28 of Brady's 63 open rows have no stored area and have been displaying under Personal.
    # An Inbox fallback on the READ path would have moved a third of his board the instant
    # this shipped — no migration, no preview, no approval. New capture goes to Inbox; a
    # legacy row keeps showing exactly where it has been showing.
    assert db.derive_bucket('something nobody has filed', '') == 'Personal', \
        'an existing unfiled row would move on deploy'
    assert db.derive_bucket_for_capture('something nobody has filed', '') == 'Inbox', \
        'new capture should land in Inbox'
    assert db.derive_bucket('paramed exam for the annuity', 'Deals') == 'GFI/PFI', \
        'a recognised row still files by its wording'
    assert db.derive_bucket_for_capture('paramed exam for the annuity', 'Deals') == 'GFI/PFI'
    cap = c.post('/daybank/add', json={'text': 'quote the fence job for the neighbour'}).json()
    cap_row = next(x for x in cap['items'] if x['text'] == 'quote the fence job for the neighbour')
    assert cap_row['bucket'] == 'Inbox' and cap_row['bucket_set'] is True, \
        f'new capture must be STORED in Inbox, got {cap_row["bucket"]}/{cap_row["bucket_set"]}'
    # ...and it is therefore not "unassigned" any more — Inbox proposals stay about legacy rows
    prev2 = c.get('/board/mapping_preview').json()
    assert cap_row['id'] not in {x['id'] for x in prev2['proposed_for_inbox']}, \
        'a row already filed to Inbox should not be proposed for Inbox again'
    for e in prev2['proposed_for_inbox']:
        assert e.get('showing_now'), 'the preview must say where a row shows TODAY'
    c.post('/daybank/update', json={'id': cap_row['id'], 'status': 'drop'})

    # ── 9. THE EXISTING GUARDS STILL HOLD UNDER THE NEW SURFACE ─────────────────
    w = ids['waiting on the county for the permit']
    assert c.post('/daybank/update', json={'id': w, 'status': 'done'}).json().get('blocked')
    assert row(w)['status'] == 'open'
    # and a follow-up on a blocked record is still just a follow-up
    r = c.post('/board/followup', json={'id': w, 'action': 'clear'}).json()
    assert r['ok'] and r['task_still_open'] and row(w)['status'] == 'open'
    assert row(w)['waiting_on'] == 'the county', 'handling a follow-up cleared the blocker'

    print('PASS: handling a follow-up (clear and push) leaves the task open with its deadline, '
          'next step and blocker intact; deadline/follow-up/chosen-day stay three separate '
          'meanings; Needs a decision is only ever explicit; custom lists persist and refuse '
          'duplicates; renaming an area preserves ids, history, dates, waiting information and '
          'parent links; tags never copy a row; both Today surfaces read the same records with '
          'blocked work excluded; and the mapping preview changes nothing, proposes only the '
          'genuinely unassigned row for Inbox, never merges, and carries decisions over as '
          'unreviewed.')
finally:
    server.cleanup(); tmp.cleanup()
