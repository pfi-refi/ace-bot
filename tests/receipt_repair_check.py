"""Receipt REPAIR check — what an update is allowed to claim, against real PostgreSQL.

THE DEFECT THIS KEEPS DEAD (reproduced live 2026-09-21). Three typed turns each changed
exactly the row Brady meant. All three replies ended "Outcome unconfirmed; check before
retrying", and the completion reply managed to say confirmed and unconfirmed at once. Every
link was honest: `_do_update_item` returned prose, `ops.classify` can only call prose
REPORTED, `chat._OPS_MEANING` maps REPORTED to OP_UNKNOWN, and `guarded_reply` then suppresses
the correct sentence and appends the warning. The receipt had no evidence behind it because
nothing had read the saved row back.

"Reopened" was wrong in the same turn for a different reason: `status='open'` during a text
edit merely PRESERVES an already-open row, and the verb came from the argument rather than
from the transition.

Everything below goes through the ACTUAL tool and the actual dispatch pipeline, and every
claim is checked against a FRESH read — a new `GET /daybank?all=true`, or a fresh single-row
SELECT — never against the call's own echo. Real disposable PostgreSQL (pgserver), synthetic
rows only. No production URL, no Google credentials, no model calls, no network.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'ace2'))
import pgserver                                        # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402

tmp = tempfile.TemporaryDirectory(prefix='ace-receipt-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    os.environ.pop('ACE2_PASSWORD', None)
    from ace2.backend import chat, db, ops, tools          # noqa: E402
    from ace2.backend.main import app                      # noqa: E402
    db._init_schema(); db._ready = True; db._trgm_ok = False

    import json as _j
    import uuid
    from datetime import datetime, timedelta, timezone

    # Synthetic rows only. NEIGHBOUR exists to be ignored: every write below must leave it
    # byte-identical, because the board is the sole authority on task state and no repair,
    # index or receipt in this release may touch a row nobody named.
    FIX = [
        ('call the excavator about the driveway', ['Business'], 'action', None, 'Side Work'),
        ('order gravel for the pad', ['Business'], 'action', None, 'Side Work'),
        ('chase the title company about the payoff letter', ['Deals'], 'action', None,
         'GFI/PFI'),
        ('confirm the survey appointment', ['Business'], 'action', None, 'Side Work'),
        ('the permit file for the barn', ['Business'], 'record', None, 'Side Work'),
        ('a neighbour row nothing here may touch', ['Personal'], 'action', None, 'Side Work'),
    ]
    ids = {}
    with db._conn() as c, c.cursor() as cur:
        for n, (text, tags, entry, state, bucket) in enumerate(FIX):
            i = uuid.uuid4().hex[:6]; ids[text] = i
            cur.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,entry,state,"
                        "bucket) VALUES(%s,%s,'todo',%s,'open',%s::jsonb,%s,%s,%s)",
                        (i, datetime.now(timezone.utc) - timedelta(days=n), text,
                         _j.dumps(tags), entry, state, bucket))
    EDIT = ids['call the excavator about the driveway']
    DONE = ids['order gravel for the pad']
    MATCH = ids['chase the title company about the payoff letter']
    PARKED = ids['confirm the survey appointment']
    READBACK = ids['the permit file for the barn']
    NEIGHBOUR = ids['a neighbour row nothing here may touch']

    c = TestClient(app)

    def row(item_id):
        """The row as a FRESH GET of the board sees it — never the call's own echo."""
        return next((x for x in c.get('/daybank?all=true').json()['items']
                     if x['id'] == item_id), None)

    def raw(item_id):
        """Every stored column of one row, as JSON. Used for byte-identity comparison, so a
        write that quietly touched a column nobody named cannot hide behind a nicer read."""
        with db._conn() as cn, cn.cursor() as cur_:
            cur_.execute("SELECT to_jsonb(t) FROM daybank_items t WHERE id = %s", (item_id,))
            r = cur_.fetchone()
        return _j.dumps(r[0], sort_keys=True) if r else None

    def untouched(note, before):
        after = raw(NEIGHBOUR)
        assert after == before, f'{note}: a row nobody named was modified\n{before}\n{after}'

    # ── 1. A TEXT EDIT ON AN OPEN ROW IS "UPDATED", NEVER "REOPENED" ──────────────
    n0 = raw(NEIGHBOUR)
    out = tools._do_update_item(id=EDIT, status='open',
                                text='call the excavator about the culvert')
    assert isinstance(out, ops.Outcome), f'the tool still returns prose: {out!r}'
    assert out.state == ops.COMPLETED, f'a verified write was not COMPLETED: {out!r}'
    assert 'Updated' in out.text, out.text
    assert 'Reopened' not in out.text, f'an already-open row was described as reopened: {out.text}'
    assert out.record_id == EDIT, out.record_id
    assert list(out.detail['changed']) == ['text'], out.detail['changed']
    assert out.detail['changed']['text']['to'] == 'call the excavator about the culvert'
    for f in ('state', 'waiting_on', 'status', 'due'):
        assert f in out.detail['unchanged'], f'{f} was not reported as unchanged'
    assert row(EDIT)['text'] == 'call the excavator about the culvert', 'the edit did not land'
    assert row(EDIT)['status'] == 'open'
    untouched('a text edit', n0)

    # the same edit again changes nothing, and says so instead of claiming an update
    again = tools._do_update_item(id=EDIT, text='call the excavator about the culvert')
    assert again.state == ops.COMPLETED, f'already-satisfied request was not verified: {again!r}'
    assert 'Already satisfied' in again.text, again.text
    assert not again.detail['changed'], 'a no-op claimed changed task fields'

    # ── 2. COMPLETION IS NAMED FROM THE SAVED ROW, AND THE BOARD AGREES ───────────
    n0 = raw(NEIGHBOUR)
    out = tools._do_update_item(id=DONE, status='done')
    assert out.state == ops.COMPLETED, f'{out!r}'
    assert 'Completed' in out.text and out.record_id == DONE, out.text
    assert out.detail['changed']['status'] == {'from': 'open', 'to': 'done'}
    assert row(DONE)['status'] == 'done', 'the board does not agree the row is done'
    untouched('a completion', n0)

    # ── 3. REOPENING IS REOPENING, AND ONLY THEN ─────────────────────────────────
    out = tools._do_update_item(id=DONE, status='open')
    assert out.state == ops.COMPLETED, f'{out!r}'
    assert 'Reopened' in out.text, out.text
    assert out.detail['changed']['status'] == {'from': 'done', 'to': 'open'}
    assert row(DONE)['status'] == 'open'

    # ── 4. A REFUSAL IS NOT A SUCCESS, KEEPS ITS WORDS, AND WRITES NOTHING ───────
    assert c.post('/daybank/update', json={'id': PARKED, 'state': 'waiting',
                                           'waiting_on': 'Tony'}).json()['ok'] is True
    before, n0 = raw(PARKED), raw(NEIGHBOUR)
    out = tools._do_update_item(id=PARKED, status='done')
    assert out.state != ops.COMPLETED, f'the completion guard was bypassed: {out!r}'
    assert out.state == ops.FAILED_BEFORE_DISPATCH, out.state
    assert 'NOT COMPLETED' in out.text, out.text
    assert 'Tony' in out.text, 'the refusal lost the name of whoever owns the next move'
    assert 'do not tell him it is done' in out.text.lower(), out.text
    assert 'force_close' in out.text, 'the refusal lost its way out'
    assert raw(PARKED) == before, 'a refused completion still wrote'
    untouched('a refusal', n0)

    # an unknown state and an unknown area keep their own reasons, and also write nothing
    out = tools._do_update_item(id=PARKED, state='parked')
    assert out.state != ops.COMPLETED and "unknown state 'parked'" in out.text, out.text
    assert raw(PARKED) == before, 'an unknown state still wrote'

    # ── 5. A ROW THAT IS NOT THERE IS NOT A SUCCESS ──────────────────────────────
    out = tools._do_update_item(id='does-not-exist', status='done')
    assert out.state != ops.COMPLETED, f'{out!r}'
    assert 'no item does-not-exist' in out.text, out.text
    out = tools._do_update_item(match='nothing on this board says this', status='done')
    assert out.state != ops.COMPLETED, f'{out!r}'
    assert 'no open item matching' in out.text, out.text

    # ── 6. RESOLVE BY MATCH NAMES THE ROW IT WROTE; AMBIGUITY WRITES NOTHING ─────
    n0 = raw(NEIGHBOUR)
    out = tools._do_update_item(match='chase the title company about the payoff letter',
                                next_step='email Rebecca for the figure')
    assert out.state == ops.COMPLETED, f'{out!r}'
    assert out.record_id == MATCH, f'the receipt named {out.record_id}, not the row it wrote'
    assert out.detail['match_used'] is True, out.detail
    assert list(out.detail['changed']) == ['next_step'], out.detail['changed']
    assert row(MATCH)['next_step'] == 'email Rebecca for the figure'
    untouched('a match-resolved write', n0)

    # two open rows worded alike resolve to neither, and both survive untouched
    twins = {}
    with db._conn() as cn, cn.cursor() as cur_:
        for who in ('Damon', 'Ken'):
            i = uuid.uuid4().hex[:6]; twins[who] = i
            cur_.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,entry,bucket) "
                         "VALUES(%s,now(),'todo',%s,'open','[\"Business\"]'::jsonb,'action',"
                         "'Side Work')", (i, f'call {who} about the website'))
    snap = {w: raw(i) for w, i in twins.items()}
    out = tools._do_update_item(match='call about the website', status='done')
    assert out.state != ops.COMPLETED, f'an ambiguous match completed something: {out!r}'
    assert 'AMBIGUOUS' in out.text, out.text
    for who, i in twins.items():
        assert i in out.text, f'the candidate list lost {who}'
        assert raw(i) == snap[who], f'an ambiguous match still wrote to {who}'

    # ── 7. A READ-BACK THAT DOES NOT COME BACK IS NEVER A COMPLETION ─────────────
    # Simulated by failing the SECOND get_item of the call — the write itself lands, which is
    # the point: "it may well have happened" is exactly what REPORTED means, and it is the one
    # honest answer here. Claiming success would be the original defect with better plumbing;
    # claiming failure would tell Brady to redo work that is already done.
    _real_get_item = db.get_item
    _seen = []

    def _fail_the_readback(item_id):
        _seen.append(item_id)
        return None if len(_seen) > 1 else _real_get_item(item_id)

    db.get_item = _fail_the_readback
    try:
        out = tools._do_update_item(id=READBACK, text='the permit file for the barn, updated')
    finally:
        db.get_item = _real_get_item
    assert out.state != ops.COMPLETED, f'an unread row was reported as verified: {out!r}'
    assert out.state == ops.REPORTED, out.state
    assert 'not established' in out.text, out.text
    assert chat.classify_result('update_item', out.text,
                                outcome=out.state) == chat.OP_UNKNOWN
    # and the write really did land, which is why it is REPORTED and not FAILED
    assert row(READBACK)['text'] == 'the permit file for the barn, updated'

    # ── 8. TOOL → DISPATCH → CLASSIFY → REPLY, THE WHOLE LIVE PATH ───────────────
    # This is the pipeline the live test failed on. Nothing here is mocked except the absence
    # of a model: the tool runs, the journal settles, the classifier reads the journal's
    # verdict, and guarded_reply composes what Brady would actually have seen.
    sink = {}
    text = asyncio.run(chat._dispatch_write(
        'update_item', {'id': EDIT, 'text': 'call the excavator about the second culvert'},
        sink))
    assert sink.get('state') == ops.COMPLETED, f'the journal recorded {sink!r}'
    state = chat.classify_result('update_item', text, outcome=sink.get('state', ''))
    assert state == chat.OP_DONE, f'a verified write reached the reply as {state}'
    reply = chat.guarded_reply('I updated that on your board. That was the last one.',
                               [{'tool': 'update_item', 'state': state, 'text': text}])
    low = reply.lower()
    assert 'outcome unconfirmed' not in low, f'the false warning came back:\n{reply}'
    assert 'unconfirmed' not in low, f'the false warning came back:\n{reply}'
    assert "don't have a verified action result" not in low, \
        f'a verified write still drew the unverified warning:\n{reply}'
    assert not ('confirmed result' in low and 'unconfirmed' in low), \
        f'the reply says both at once:\n{reply}'
    # The claim sentence is still dropped — a verified receipt proves what IT did and never
    # what else the sentence claimed, and that guard is not being loosened here. What must
    # survive is the rest of what Ace said, and a receipt that reads as a confirmed result.
    assert 'that was the last one' in low, f'the non-claim sentence was lost:\n{reply}'
    assert 'confirmed result' in low, f'a verified write was not presented as one:\n{reply}'
    assert 'Updated' in reply and 'Reopened' not in reply, reply
    assert row(EDIT)['text'] == 'call the excavator about the second culvert'

    # the refusal travels the same path and still warns — the guard is not being loosened
    sink = {}
    text = asyncio.run(chat._dispatch_write('update_item', {'id': PARKED, 'status': 'done'},
                                            sink))
    assert sink.get('state') != ops.COMPLETED, sink
    state = chat.classify_result('update_item', text, outcome=sink.get('state', ''))
    assert state == chat.OP_FAILED, state
    assert raw(PARKED) == before, 'the dispatch path wrote to a refused row'

    # ── 9. THE NEIGHBOUR SURVIVED EVERYTHING ────────────────────────────────────
    with db._conn() as cn, cn.cursor() as cur_:
        cur_.execute("SELECT to_jsonb(t) FROM daybank_items t WHERE id = %s", (NEIGHBOUR,))
        final = _j.dumps(cur_.fetchone()[0], sort_keys=True)
    assert final == raw(NEIGHBOUR)
    assert 'a neighbour row nothing here may touch' in final
    assert row(NEIGHBOUR)['status'] == 'open'

    print('PASS: an update returns a structured ops.Outcome and reaches COMPLETED only from a '
          'fresh read of the exact saved row; the verb is the real transition, so a text edit '
          'on an open row says Updated and never Reopened; the receipt names the resolved id '
          'and only the fields that actually moved, omitting the unchanged ones and the '
          "store's own bookkeeping; a completion guard refusal, an unknown state, an unknown "
          'id, an ambiguous match and a failed read-back are each NOT completed, each keep '
          'their exact wording and candidates, and each leave the rows byte-identical; a '
          'no-op write says No change; and the real tool → _dispatch_write → classify_result '
          '→ guarded_reply path now produces OP_DONE with no "Outcome unconfirmed" warning, '
          'while a refusal on the same path still fails — with a neighbouring board row '
          'byte-identical from first write to last.')
finally:
    server.cleanup(); tmp.cleanup()
