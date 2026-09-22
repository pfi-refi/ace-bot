"""Entity API check — the routes, the retrieval budget, and what they may not leak.

WHAT THIS IS FOR. Slice 3+4 puts Ace's memory behind HTTP for the first time: a register
of people, one dossier per record, a correction path, a global source search and a graph
drawn from stored rows. Every one of those is a way for something private to leave the
process, or for a correction to be claimed and not made. So each of the properties below
is checked against the REAL app, through the REAL routes, with a REAL disposable
PostgreSQL — and the durable ones are re-read fresh afterwards rather than believed from
the call's own reply.

The properties, and the failure each one is standing in front of:

  • AUTH ON EVERY NEW ROUTE, read-only ones included. A dossier is the most personal thing
    this server renders; "it only reads" is not an exemption (Codex acceptance note 11).
  • ZERO MODEL CALLS on `/graph?source=entities`. ANTHROPIC_API_KEY is unset here, and
    `chat._anthropic` is replaced with a tripwire — a paid path would raise, loudly.
  • NON-DESTRUCTIVE, AUDITED CORRECTIONS. Unlink retracts and keeps the row; an unknown op
    writes nothing at all; a rejected review is a permanent tombstone a replay cannot
    re-open.
  • BOUNDED RETRIEVAL. The registry and the dossier text obey their character caps exactly,
    because these strings go into every prompt.
  • NOTHING HIDDEN, NOTHING LEAKED. Unassigned, ambiguous and excluded sources are all
    reachable from search; a settings/telemetry row is listed but its body is never
    rendered; and no response anywhere may contain the synthetic credential planted in the
    corpus.
  • AMENDMENT 1 SCENARIOS 4, 6 and 8 at the API level.

FIXTURES ARE SYNTHETIC. The names are Codex's chosen test names; everything else is
invented. No production URL, no real private data, no network, no model call.
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'ace2'))
import pgserver                                        # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402

FAILURES = []
CHECKS = [0]


def ok(name, cond, detail=''):
    CHECKS[0] += 1
    if cond:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s%s' % (name, (' — ' + str(detail)) if detail else ''))
        FAILURES.append(name)


# A synthetic credential planted in a settings row and in an ordinary turn. It must never
# appear in ANY response body. A key rendered once is a key leaked.
FAKE_SECRET = 'sk-live-FAKE000111222333444555666777888'

tmp = tempfile.TemporaryDirectory(prefix='ace-entity-api-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ.pop('ANTHROPIC_API_KEY', None)      # a paid path would raise, not bill
    os.environ.pop('ACE2_ALLOW_OPEN', None)        # auth must actually be on
    os.environ['ACE2_PASSWORD'] = 'synthetic-test-password'
    os.environ['ACE2_TOKEN_SECRET'] = 'synthetic-test-secret'

    from ace2.backend import chat, db, entities, entity_context   # noqa: E402
    from ace2.backend import main as main_mod                     # noqa: E402
    from ace2.backend.main import app                             # noqa: E402

    db._init_schema(); db._ready = True; db._trgm_ok = False
    entities.ready()

    # A TRIPWIRE, NOT A MOCK. If any /graph?source=entities branch reaches for a model
    # client, this raises and the check fails with the reason in the message.
    def _no_model(*a, **k):
        raise AssertionError('a model client was constructed on a path that must be free')
    chat._anthropic = _no_model

    NOW = datetime.now(timezone.utc)

    def ago(days):
        return NOW - timedelta(days=days)

    # ── Synthetic originals ────────────────────────────────────────────────────
    with db._conn() as c, c.cursor() as cur:
        def turn(role, text, when):
            cur.execute("INSERT INTO turns(ts, source, role, content) "
                        "VALUES(%s,'test',%s,%s) RETURNING id", (when, role, text))
            return str(cur.fetchone()[0])

        def fact(text, when, source='sweep', tier='active', invalid=None):
            cur.execute("INSERT INTO facts(ts, subject, kind, text, tier, valid_from, "
                        "invalid_at, source) VALUES(%s,'test','note',%s,%s,%s,%s,%s) "
                        "RETURNING id", (when, text, tier, when, invalid, source))
            return str(cur.fetchone()[0])

        def summary(kind, text, when):
            cur.execute("INSERT INTO summaries(ts, kind, text) VALUES(%s,%s,%s) "
                        "RETURNING id", (when, kind, text))
            return str(cur.fetchone()[0])

        def item(text, status='open'):
            iid = uuid.uuid4().hex[:6]
            cur.execute("INSERT INTO daybank_items(id, ts, kind, text, status, tags, "
                        "entry, bucket) VALUES(%s,%s,'todo',%s,%s,'[\"Business\"]'::jsonb,"
                        "'action','Side Work')", (iid, NOW, text, status))
            return iid

        T_ROLE = turn('user', 'Jordan Rivera runs operations at Cedar Assist now.', ago(3))
        T_SENT = turn('user', 'I sent Jordan the signed packet yesterday.', ago(1))
        T_OLD = turn('user', 'Jordan Rivera is contracting on the Willowmere rollout.',
                     ago(120))
        T_PLAN = turn('user', 'I will send Jordan the packet this week.', ago(5))
        T_GUESS = turn('assistant', 'Jordan seems to be only on Willowmere these days.',
                       ago(0))
        T_KIM = turn('user', 'Jordan Kim is at Northwind Helper.', ago(9))
        T_AMBIG = turn('user', 'Jordan said the paperwork is fine.', ago(2))
        T_LEAK = turn('assistant',
                      'Reminder of the shared login token ' + FAKE_SECRET, ago(40))
        F_LEGACY = fact('Cedar Assist is the operations vendor.', ago(60))
        S_SETTINGS = summary('watch_state',
                             'watch cursor api_key=' + FAKE_SECRET, ago(2))
        S_RECAP = summary('recap', 'Talked through the Willowmere rollout timing.', ago(4))
        ITEM_OPEN = item('Send Jordan the signed packet')
        ITEM_FREE = item('Unrelated: order gravel for the pad')

    # ── Synthetic entity layer ─────────────────────────────────────────────────
    with db._conn() as c, c.cursor() as cur:
        def note(corpus, native, text, role, klass, excluded=None, status='indexed',
                 when=None):
            sid, _, _ = entities.note_source_cur(cur, corpus, native, text,
                                                 occurred_at=when or NOW, role=role,
                                                 status=status, source_class=klass,
                                                 excluded_reason=excluded)
            return sid

        S_ROLE = note('turn', T_ROLE, 'Jordan Rivera runs operations at Cedar Assist now.',
                      'user', 'user_statement', when=ago(3))
        S_SENT = note('turn', T_SENT, 'I sent Jordan the signed packet yesterday.',
                      'user', 'user_statement', when=ago(1))
        S_OLD = note('turn', T_OLD, 'Jordan Rivera is contracting on the Willowmere '
                     'rollout.', 'user', 'user_statement', when=ago(120))
        S_PLAN = note('turn', T_PLAN, 'I will send Jordan the packet this week.',
                      'user', 'user_statement', when=ago(5))
        S_GUESS = note('turn', T_GUESS, 'Jordan seems to be only on Willowmere these days.',
                       'assistant', 'assistant_inference', when=ago(0))
        S_KIM = note('turn', T_KIM, 'Jordan Kim is at Northwind Helper.', 'user',
                     'user_statement', when=ago(9))
        # THE THREE BUCKETS THAT MUST STAY REACHABLE.
        S_AMBIG = note('turn', T_AMBIG, 'Jordan said the paperwork is fine.', 'user',
                       'user_statement', status='ambiguous', when=ago(2))
        S_UNASSIGNED = note('fact', F_LEGACY, 'Cedar Assist is the operations vendor.',
                            'assistant', 'legacy_extracted', status='unassigned',
                            when=ago(60))
        S_EXCLUDED = note('summary', S_SETTINGS, 'watch cursor api_key=' + FAKE_SECRET,
                          'system', 'internal_metadata',
                          excluded=entities.EXCL_TELEMETRY, status='excluded', when=ago(2))
        S_LEAKTURN = note('turn', T_LEAK, 'Reminder of the shared login token '
                          + FAKE_SECRET, 'assistant', 'assistant_inference', when=ago(40))
        S_SUMMARY = note('summary', S_RECAP, 'Talked through the Willowmere rollout '
                         'timing.', 'system', 'secondary_summary', when=ago(4))
        S_ITEM = note('item', ITEM_OPEN, 'Send Jordan the signed packet', 'board',
                      'user_statement', when=NOW)

        RIVERA, _ = entities.upsert_entity_cur(
            cur, 'person', 'Jordan Rivera', origin='migration', confidence=0.8,
            review_status='confirmed', key=entities.import_key('person', 'Jordan Rivera'),
            occurred_at=ago(3), first_name_aliases=['Jordan'])
        KIM, _ = entities.upsert_entity_cur(
            cur, 'person', 'Jordan Kim', origin='migration', confidence=0.6,
            review_status='unreviewed', key=entities.import_key('person', 'Jordan Kim'),
            occurred_at=ago(9), first_name_aliases=['Jordan'])
        CEDAR, _ = entities.upsert_entity_cur(
            cur, 'org', 'Cedar Assist', origin='migration', confidence=0.5,
            review_status='unreviewed', key=entities.import_key('org', 'Cedar Assist'),
            occurred_at=ago(3))
        PARACLETE, _ = entities.upsert_entity_cur(
            cur, 'project', 'Willowmere rollout', origin='migration', confidence=0.5,
            review_status='confirmed',
            key=entities.import_key('project', 'Willowmere rollout'), occurred_at=ago(120))
        # A record the 55-node cap can push off the picture (scenario 8).
        OFFMAP, _ = entities.upsert_entity_cur(
            cur, 'person', 'Marisol Trent', origin='migration', confidence=0.4,
            review_status='unreviewed', key=entities.import_key('person', 'Marisol Trent'),
            occurred_at=ago(30))

        for sid in (S_ROLE, S_SENT, S_OLD, S_PLAN, S_GUESS, S_ITEM):
            entities.link_cur(cur, RIVERA, sid, relation='mentions', method='exact_alias',
                              origin='migration', confidence=0.7, evidence='Jordan Rivera')
        UNLINK_ME, _ = entities.link_cur(cur, RIVERA, S_SUMMARY, relation='mentions',
                                         method='exact_alias', origin='migration',
                                         confidence=0.3, evidence='Willowmere')
        entities.link_cur(cur, RIVERA, S_LEAKTURN, relation='mentions',
                          method='exact_alias', origin='migration', confidence=0.2,
                          evidence='login')
        entities.link_cur(cur, KIM, S_KIM, relation='mentions', method='exact_alias',
                          origin='migration', confidence=0.7, evidence='Jordan Kim')

        # CURRENT, with provenance and dates (scenario 6).
        entities.add_entity_fact_cur(
            cur, RIVERA, 'role', 'Operations lead at Cedar Assist', stated_at=ago(3),
            source_id=S_ROLE, origin='user_statement', confidence=0.9,
            source_class='user_statement')
        # A NEWER ASSISTANT GUESS. It must NOT bury the older user statement.
        entities.add_entity_fact_cur(
            cur, RIVERA, 'role', 'Only on the Willowmere rollout', stated_at=ago(0),
            source_id=S_GUESS, origin='assistant_inference', confidence=0.3,
            source_class='assistant_inference')
        # HISTORY: dated, superseded, visibly not current.
        entities.add_entity_fact_cur(
            cur, RIVERA, 'org', 'Contractor on the Willowmere rollout', stated_at=ago(120),
            source_id=S_OLD, origin='user_statement', confidence=0.7,
            source_class='user_statement')
        entities.add_entity_fact_cur(
            cur, RIVERA, 'org', 'Cedar Assist', stated_at=ago(3), source_id=S_ROLE,
            origin='user_statement', confidence=0.9, source_class='user_statement')
        # A DISAGREEMENT ON A SINGLE-VALUED ATTRIBUTE. A person has one employer at a
        # time, so these two cannot both be right — and a NEWER assistant guess must not
        # be allowed to settle it against something Brady said.
        entities.add_entity_fact_cur(
            cur, RIVERA, 'org', 'Northwind Helper', stated_at=ago(0), source_id=S_GUESS,
            origin='assistant_inference', confidence=0.3,
            source_class='assistant_inference')
        # A PLAN IS NOT A COMPLETION, and a completion CLAIM against an open board row is
        # a discrepancy (scenario 4).
        entities.add_entity_fact_cur(
            cur, RIVERA, 'plan', 'I will send Jordan the packet this week',
            stated_at=ago(5), source_id=S_PLAN, origin='user_statement', confidence=0.8,
            source_class='user_statement')
        entities.add_entity_fact_cur(
            cur, RIVERA, 'status', 'I sent Jordan the signed packet yesterday',
            stated_at=ago(1), source_id=S_SENT, origin='user_statement', confidence=0.8,
            source_class='user_statement', supersede=False)

        REL_WORKS, _ = entities.add_relation_cur(cur, RIVERA, CEDAR, 'works_at',
                                                 source_id=S_ROLE, origin='graph_seed',
                                                 confidence=0.4,
                                                 review_status='unreviewed')
        entities.add_relation_cur(cur, RIVERA, PARACLETE, 'on_project', source_id=S_OLD,
                                  origin='user_statement', confidence=0.8,
                                  review_status='confirmed')

        entities.queue_review_cur(cur, 'graph_seed', 'northwind helper', {
            'label': 'Northwind Helper', 'claimed_type': 'person',
            'claimed_type_is_accepted': False, 'entity_ids': [],
            'edges': [{'source': 'Northwind Helper', 'target': 'Jordan Rivera',
                       'kind': 'works_with'}],
            'source_id': entities.source_id('summary', S_RECAP),
            'why': 'a model-generated graph node; its type is claimed, not accepted'},
            priority=4)
        entities.queue_review_cur(cur, 'graph_seed', 'harbor point', {
            'label': 'Harbor Point', 'claimed_type': 'person', 'entity_ids': [],
            'edges': [], 'source_id': entities.source_id('summary', S_RECAP),
            'why': 'a model-generated graph node'}, priority=4)
        entities.queue_review_cur(cur, 'merge_candidate', '|'.join(sorted([RIVERA, KIM])), {
            'entity_ids': sorted([RIVERA, KIM]), 'names': ['Jordan Kim', 'Jordan Rivera'],
            'why': 'these two names look similar; SUGGESTION ONLY'}, priority=3)
        entities.queue_review_cur(cur, 'ambiguous_name', 'jordan', {
            'alias': 'Jordan', 'alias_norm': 'jordan', 'entity_ids': sorted([RIVERA, KIM]),
            'example_source_id': S_AMBIG,
            'why': 'a first name that has never been confirmed; no link was written'},
            priority=2)
        entities.queue_review_cur(cur, 'unassigned_source', S_UNASSIGNED, {
            'source_id': S_UNASSIGNED, 'corpus': 'fact', 'entity_ids': [],
            'candidate_names': ['Cedar Assist'],
            'why': 'this source names something and nothing matched'}, priority=4)

    AUTHED = {'Authorization': 'Bearer ' + main_mod.issue_token()}
    c = TestClient(app)
    BODIES = []            # every response body, swept for the planted credential

    def get(path, auth=True):
        r = c.get(path, headers=AUTHED if auth else {})
        if auth:
            BODIES.append(r.text)
        return r

    def post(path, body, auth=True):
        r = c.post(path, json=body, headers=AUTHED if auth else {})
        if auth:
            BODIES.append(r.text)
        return r

    NEW_GET = ['/entities', '/entities/counts', '/entities/review',
               '/entities/' + RIVERA, '/sources/search?q=Jordan', '/graph',
               '/graph?source=entities']
    NEW_POST = [('/entities/%s/correct' % RIVERA, {'op': 'confirm', 'reason': 'x'}),
                ('/entities/review/1', {'action': 'dismiss', 'reason': 'x'})]

    print('\n── 1. Auth is required on every new route, read-only ones included ──')
    for p in NEW_GET:
        ok('401 without a token: GET %s' % p, get(p, auth=False).status_code == 401)
    for p, body in NEW_POST:
        ok('401 without a token: POST %s' % p,
           post(p, body, auth=False).status_code == 401)
    ok('a valid token is accepted', get('/entities').status_code == 200)

    print('\n── 2. The register and the dossier ──')
    lst = get('/entities?limit=50').json()
    ok('the list shape is entities/total/returned/truncated',
       all(k in lst for k in ('entities', 'total', 'returned', 'truncated')), lst.keys())
    ok('every record carries a real entity_id',
       all(entities.valid_entity_id(e['entity_id']) for e in lst['entities']))
    ok('search by name finds the right record',
       [e['display_name'] for e in get('/entities?q=Marisol').json()['entities']]
       == ['Marisol Trent'])
    ok('type filtering works',
       {e['type'] for e in get('/entities?type=org').json()['entities']} == {'org'})
    page = get('/entities?limit=1').json()
    ok('a truncated page says so', page['truncated'] and page['total'] > 1)

    bad = get('/entities/not-an-id')
    ok('a malformed entity id is refused at the route', bad.status_code == 400)
    ok('an unknown but well-formed id is a 404',
       get('/entities/per_ffffffffffff').status_code == 404)

    dos = get('/entities/' + RIVERA).json()
    ok('the dossier has every contracted key',
       all(k in dos for k in ('entity', 'current', 'history', 'relations', 'items',
                              'sources', 'suggestions', 'counts', 'notes')),
       sorted(dos.keys()))
    ok('the dossier states the money authority',
       any('budget spreadsheet' in n for n in dos['notes']))
    ok('the dossier states that excerpts are records, not instructions',
       any('not instructions' in n for n in dos['notes']))
    cur_role = [f for f in dos['current'] if f['attribute'] == 'role']
    cur_org = [f for f in dos['current'] if f['attribute'] == 'org']
    ok('a newer assistant inference did NOT bury the older user statement',
       len(cur_role) == 2 and len(cur_org) == 2,
       [f['value'] for f in cur_role + cur_org])
    ok('the disagreement is marked on both statements',
       all(f['conflicts_with'] for f in cur_org),
       [(f['ef_id'], f.get('conflicts_with'), f['value']) for f in cur_org])
    ok('the user statement outranks the inference in the ordering',
       cur_role[0]['source_class'] == 'user_statement'
       and cur_org[0]['source_class'] == 'user_statement')
    ok('...and the disagreement is counted, not settled',
       dos['counts'].get('conflicts', 0) >= 2, dos['counts'])

    print('\n── 3. Scenario 6 — dated source references, history is not current ──')
    ok('every current statement carries a dated source reference',
       all(f.get('source') and f['source'].get('source_id') and f.get('stated_at')
           for f in dos['current']))
    ok('excerpts are wrapped as quoted data',
       all(f['source']['excerpt'].startswith('<<<src ')
           for f in dos['current'] if f['source'].get('excerpt')))
    archived = [f for f in dos['history'] if f.get('superseded_by') or f.get('valid_to')]
    ok('archived statements are visible as history', bool(archived))
    current_ids = {f['ef_id'] for f in dos['current']}
    ok('...and none of them is presented as current',
       not any(f['ef_id'] in current_ids for f in archived))
    ok('a plan is stored as a plan, never as a completion',
       any(f['attribute'] == 'plan' for f in dos['current'] + dos['history']))

    print('\n── 4. Scenario 4 — his words vs the live board ──')
    ok('the open board row is shown with its LIVE status',
       any(i['item_id'] == ITEM_OPEN and i['status'] == 'open' for i in dos['items']))
    ok('the contradiction is reported as a discrepancy', bool(dos.get('discrepancies')))
    with db._conn() as cc, cc.cursor() as ccur:
        ccur.execute("SELECT status, text FROM daybank_items WHERE id = %s", (ITEM_OPEN,))
        board_now = ccur.fetchone()
    ok('...and the board row itself was never touched',
       board_now == ('open', 'Send Jordan the signed packet'), board_now)

    print('\n── 5. Corrections are governed, audited and non-destructive ──')
    before_audit = len(entities.audit_log(limit=500))
    r = post('/entities/%s/correct' % RIVERA, {'op': 'teleport', 'args': {},
                                               'reason': 'nope'})
    ok('an unknown op is a 400', r.status_code == 400)
    ok('...that says so honestly', r.json().get('ok') is False and 'Unknown op'
       in r.json().get('error', ''))
    ok('...and wrote nothing at all', len(entities.audit_log(limit=500)) == before_audit)

    r = post('/entities/%s/correct' % RIVERA,
             {'op': 'unlink', 'args': {'link_id': UNLINK_ME}, 'reason': 'not about him'})
    ok('unlink succeeds', r.status_code == 200 and r.json().get('ok') is True, r.text)
    ok('...and returns an audit id', bool(r.json().get('audit_id')))
    with db._conn() as cc, cc.cursor() as ccur:
        ccur.execute("SELECT retracted_at, retracted_reason FROM ace_entity_links "
                     "WHERE link_id = %s", (UNLINK_ME,))
        row = ccur.fetchone()
    ok('the link row still EXISTS (retracted, never deleted)', row is not None)
    ok('...with a retraction stamp and a reason', row and row[0] and row[1])
    ok('the retracted source is gone from a fresh dossier read',
       S_SUMMARY not in [s['source_id'] for s in get('/entities/' + RIVERA).json()['sources']])
    ok('unlinking a link that is not on this record is refused',
       post('/entities/%s/correct' % KIM,
            {'op': 'unlink', 'args': {'link_id': UNLINK_ME}}).status_code == 400)

    r = post('/entities/%s/correct' % CEDAR,
             {'op': 'rename', 'args': {'display_name': 'Cedar Assist LLC'},
              'reason': 'legal name'})
    ok('rename succeeds', r.json().get('ok') is True, r.text)
    ok('...and the OLD name is kept as an alias',
       'Cedar Assist' in [a['alias'] for a in (entities.get_entity(CEDAR) or {})
                          .get('aliases', [])])
    r = post('/entities/%s/correct' % CEDAR,
             {'op': 'remove_alias', 'args': {'alias': 'Cedar Assist LLC'},
              'reason': 'wrong'})
    ok('remove_alias succeeds', r.json().get('ok') is True, r.text)
    ok('...and the alias row is FLAGGED, not deleted',
       any(a['alias'] == 'Cedar Assist LLC' and a['review_status'] == 'rejected'
           for a in (entities.get_entity(CEDAR) or {}).get('aliases', [])))
    r = post('/entities/%s/correct' % KIM,
             {'op': 'remove_alias', 'args': {'alias': 'Jordan Kim'},
              'reason': 'that source was about someone else'})
    ok('rejecting a name also retracts the links that name produced',
       r.json()['applied']['links_retracted'] == 1, r.text)
    with db._conn() as cc, cc.cursor() as ccur:
        ccur.execute("SELECT count(*) FROM ace_entity_links WHERE entity_id = %s",
                     (KIM,))
        ok('...and still deletes nothing', ccur.fetchone()[0] == 1)
    ok('a tombstoned link is not re-created by a replay',
       entities.link(KIM, S_KIM, relation='mentions', method='exact_alias',
                     origin='migration') is None)

    r = post('/entities/%s/correct' % RIVERA,
             {'op': 'link_item', 'args': {'item_id': ITEM_FREE}, 'reason': 'his'})
    ok('link_item attaches a live board row', r.json().get('ok') is True, r.text)
    with db._conn() as cc, cc.cursor() as ccur:
        ccur.execute("SELECT status, text FROM daybank_items WHERE id = %s", (ITEM_FREE,))
        ok('...and did not write to the board',
           ccur.fetchone() == ('open', 'Unrelated: order gravel for the pad'))
    ok('link_item on a row that does not exist is refused, not invented',
       post('/entities/%s/correct' % RIVERA,
            {'op': 'link_item', 'args': {'item_id': 'nosuchrow'}}).status_code == 400)
    ok('a source id may not carry a path',
       post('/entities/%s/correct' % RIVERA,
            {'op': 'link_item', 'args': {'item_id': '../../etc/passwd'}}
            ).status_code == 400)

    # ── REGRESSION, review finding 1 (2026-09-22) ──────────────────────────────
    # `evidence` holds the SURFACE span the source used; the alias being removed is the
    # STORED one. `norm_alias` folds possessives, apostrophes, hyphens and trailing
    # punctuation precisely because those are the same name — so matching the raw strings
    # retracted nothing on the commonest real spellings while the reply said the links
    # were gone. Every row below is a link the old code left standing.
    def turn_row(cursor, role, text, when):
        cursor.execute("INSERT INTO turns(ts, source, role, content) VALUES(%s,'test',%s,%s) RETURNING id", (when, role, text))
        return str(cursor.fetchone()[0])

    print('\n   · finding 1: a removed alias retracts the links it actually made')
    SURFACES = [("Rebecca's", 'possessive'), ('O’Brien', 'curly apostrophe'),
                ('Smith-Jones', 'hyphen'), ('Rivera,', 'trailing comma'),
                ('  rebecca  ', 'case and padding')]
    with db._conn() as cc, cc.cursor() as ccur:
        VIC, _ = entities.upsert_entity_cur(
            cc and ccur, 'person', 'Rebecca Smith-Jones', origin='migration',
            confidence=0.5, review_status='unreviewed',
            key=entities.import_key('person', 'Rebecca Smith-Jones'), occurred_at=ago(4))
        for alias in ('Rebecca', "O'Brien", 'Smith Jones', 'Rivera'):
            entities.add_alias_cur(ccur, VIC, alias, kind='name', origin='migration',
                                   confidence=0.5)
        vic_sources = []
        for n, (surface, why) in enumerate(SURFACES):
            tid = turn_row(ccur, 'user', 'A synthetic line naming %s here.' % surface,
                           ago(4))
            sid = entities.note_source_cur(ccur, 'turn', tid,
                                           'A synthetic line naming %s here.' % surface,
                                           occurred_at=ago(4), role='user',
                                           source_class='user_statement')[0]
            entities.link_cur(ccur, VIC, sid, relation='mentions', method='exact_alias',
                              origin='migration', confidence=0.5, evidence=surface)
            vic_sources.append((sid, surface, why))
        # A HUMAN's link and a link matched on a DIFFERENT name must both survive.
        keep_tid = turn_row(ccur, 'user', 'A line naming Smith Jones only.', ago(4))
        keep_sid = entities.note_source_cur(ccur, 'turn', keep_tid,
                                            'A line naming Smith Jones only.',
                                            occurred_at=ago(4), role='user',
                                            source_class='user_statement')[0]
        entities.link_cur(ccur, VIC, keep_sid, relation='mentions', method='exact_alias',
                          origin='migration', confidence=0.5, evidence='Smith-Jones')
        manual_tid = turn_row(ccur, 'user', 'Brady attached this one by hand.', ago(4))
        manual_sid = entities.note_source_cur(ccur, 'turn', manual_tid,
                                              'Brady attached this one by hand.',
                                              occurred_at=ago(4), role='user',
                                              source_class='user_statement')[0]
        entities.link_cur(ccur, VIC, manual_sid, relation='about', method='manual',
                          origin='manual', confidence=1.0, evidence="Rebecca's")

    def live_links(eid):
        with db._conn() as cc, cc.cursor() as ccur:
            ccur.execute("SELECT evidence, method FROM ace_entity_links WHERE entity_id "
                         "= %s AND retracted_at IS NULL ORDER BY link_id", (eid,))
            return ccur.fetchall()

    def _link_count(eid):
        with db._conn() as cc, cc.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM ace_entity_links WHERE entity_id=%s", (eid,))
            return cursor.fetchone()[0]

    before_links = live_links(VIC)
    ok('the fixture links every awkward spelling of the name', len(before_links) == 7,
       before_links)
    r = post('/entities/%s/correct' % VIC,
             {'op': 'remove_alias', 'args': {'alias': 'Rebecca'},
              'reason': 'that is a different Rebecca'})
    applied = r.json().get('applied') or {}
    ok('removing "Rebecca" retracts the possessive and the padded/cased spellings',
       applied.get('links_retracted') == 2, r.text)
    after = live_links(VIC)
    ok("...so \"Rebecca's\" is really gone from the live links",
       "Rebecca's" not in [e for e, m in after if m == 'exact_alias'], after)
    ok('...and the count reported is the count actually retracted',
       len(before_links) - len(after) == applied.get('links_retracted'))
    ok('...while a link a HUMAN made on the same surface form survives',
       ("Rebecca's", 'manual') in after, after)
    for alias, surface, n in (("O'Brien", 'O’Brien', 1), ('Smith Jones', 'Smith-Jones', 2),
                              ('Rivera', 'Rivera,', 1)):
        r = post('/entities/%s/correct' % VIC,
                 {'op': 'remove_alias', 'args': {'alias': alias},
                  'reason': 'synthetic correction'})
        ok('removing %r retracts its %r link(s)' % (alias, surface),
           (r.json().get('applied') or {}).get('links_retracted') == n, r.text)
    ok('every machine link matched on a rejected name is now retracted',
       [e for e, m in live_links(VIC) if m == 'exact_alias'] == [], live_links(VIC))
    ok('...and nothing was deleted from the table',
       _link_count(VIC) == 7)
    ok('a rejected name cannot be re-linked by a replay',
       entities.link(VIC, vic_sources[0][0], relation='mentions', method='exact_alias',
                     origin='migration') is None)
    ok('removing a name that matched nothing says so instead of claiming success',
       (post('/entities/%s/correct' % VIC,
             {'op': 'remove_alias', 'args': {'alias': 'Smith Jones'}}).json()
        .get('applied') or {}).get('links_retracted') == 0)

    r = post('/entities/%s/correct' % RIVERA,
             {'op': 'set_fact_status',
              'args': {'ef_id': cur_role[1]['ef_id'], 'review_status': 'rejected'},
              'reason': 'that was a guess'})
    ok('set_fact_status succeeds', r.json().get('ok') is True, r.text)
    with db._conn() as cc, cc.cursor() as ccur:
        ccur.execute("SELECT review_status, valid_to, superseded_by FROM ace_entity_facts "
                     "WHERE ef_id = %s", (cur_role[1]['ef_id'],))
        f_row = ccur.fetchone()
    ok('a rejected statement is dated out but KEPT', f_row[0] == 'rejected' and f_row[1])
    ok('...with NO invented replacement chain', f_row[2] is None)
    ok('another record may not restatus this one',
       post('/entities/%s/correct' % KIM,
            {'op': 'set_fact_status', 'args': {'ef_id': cur_role[0]['ef_id'],
                                               'review_status': 'rejected'}}
            ).status_code == 400)

    print('\n── 6. The review queue is reachable, and a rejection is permanent ──')
    rev = get('/entities/review?limit=50').json()
    kinds = {r_['kind'] for r_ in rev['reviews']}
    ok('graph seeds are reachable even though they created no entity',
       'graph_seed' in kinds, kinds)
    ok('unassigned and ambiguous rows are reachable too',
       {'unassigned_source', 'ambiguous_name'} <= kinds, kinds)
    ok('the queue reports its own totals',
       rev['total'] >= len(rev['reviews']) and rev['counts']['by_kind'])
    seeds = [r_ for r_ in rev['reviews'] if r_['kind'] == 'graph_seed']
    keep_seed = [s for s in seeds if s['payload']['label'] == 'Northwind Helper'][0]
    kill_seed = [s for s in seeds if s['payload']['label'] == 'Harbor Point'][0]

    before = entities.counts()['entities']
    r = post('/entities/review/%d' % kill_seed['review_id'],
             {'action': 'reject', 'reason': 'that is not a person or a thing'})
    ok('rejecting a seed succeeds', r.json().get('ok') is True, r.text)
    ok('...and created NO entity', entities.counts()['entities'] == before)
    with db._conn() as cc, cc.cursor() as ccur:
        ccur.execute("SELECT state, resolution FROM ace_entity_review WHERE review_id = %s",
                     (kill_seed['review_id'],))
        rr = ccur.fetchone()
    ok('the review row is kept as a closed tombstone', rr[0] == 'dismissed', rr)
    # REPLAY. queue_review re-inserts with ON CONFLICT DO NOTHING, so the migration
    # running again must not re-open a decision Brady already made.
    entities.queue_review('graph_seed', 'harbor point',
                          {'label': 'Harbor Point', 'claimed_type': 'person'}, 4)
    with db._conn() as cc, cc.cursor() as ccur:
        ccur.execute("SELECT state FROM ace_entity_review WHERE review_id = %s",
                     (kill_seed['review_id'],))
        ok('a backfill replay does NOT resurrect it', ccur.fetchone()[0] == 'dismissed')
        ccur.execute("SELECT count(*) FROM ace_entity_review WHERE kind = 'graph_seed' "
                     "AND subject_key = 'harbor point'")
        ok('...and does not file a duplicate either', ccur.fetchone()[0] == 1)
    closed = get('/entities/review?state=dismissed&kind=graph_seed').json()
    ok('a closed decision stays inspectable, not erased',
       kill_seed['review_id'] in [r_['review_id'] for r_ in closed['reviews']],
       closed['reviews'])
    ok('re-confirming a closed decision is refused',
       post('/entities/review/%d' % kill_seed['review_id'],
            {'action': 'confirm', 'reason': 'changed my mind'}).status_code == 400)

    r = post('/entities/review/%d' % keep_seed['review_id'],
             {'action': 'confirm', 'args': {'type': 'org'},
              'reason': 'Northwind Helper is an organization, not a person'})
    body = r.json()
    ok('confirming a seed succeeds', body.get('ok') is True, r.text)
    ok('...and THAT is what created the entity',
       entities.valid_entity_id(body['applied'].get('entity_id', '')))
    made = entities.get_entity(body['applied']['entity_id'])
    ok("...with the human's type, not the model's claim",
       made['type'] == 'org' and body['applied']['claimed_type'] == 'person')
    ok('...recording who decided and why',
       any(a['op'] == 'review_confirm' and a['actor'] == 'user'
           and 'organization' in (a['reason'] or '')
           for a in entities.audit_log(limit=50)))
    # ITEM B. Confirming a NODE confirms the node and nothing else. The seed's claimed
    # edges become their own review rows — one click, one decision.
    ok('confirming a node creates NO relation',
       body['applied']['relations_created'] == 0
       and entities.relations(made['entity_id']) == [], r.text)
    ok('...and its claimed links are queued for their own confirmation',
       body['applied']['edge_reviews_queued'] == 1, body['applied'])
    edge_rows = [x for x in get('/entities/review?limit=100').json()['reviews']
                 if (x['payload'] or {}).get('proposal') == 'relation']
    ok('...reachable in the queue as relation proposals', len(edge_rows) == 1, edge_rows)
    ok('...saying plainly that the node confirmation did not confirm them',
       'did NOT confirm it' in edge_rows[0]['payload']['why'])
    r2 = post('/entities/review/%d' % edge_rows[0]['review_id'],
              {'action': 'confirm', 'reason': 'yes, they work together'})
    ok('confirming the EDGE is what writes the relation',
       r2.json()['applied']['created'] is True
       and [x['kind'] for x in entities.relations(made['entity_id'])] == ['works_with'],
       r2.text)
    ok('...as a confirmed relation, not another proposal',
       entities.relations(made['entity_id'])[0]['review_status'] == 'confirmed')
    ok('an unknown review action is a 400',
       post('/entities/review/%d' % keep_seed['review_id'],
            {'action': 'obliterate'}).status_code == 400)
    ok('an unknown review id is a 400',
       post('/entities/review/999999', {'action': 'confirm'}).status_code == 400)

    print('\n── 7. /graph?source=entities — zero model calls, honest counts ──')
    g = get('/graph').json()
    ok('the default source is the stored entity layer', g['source'] == 'entities')
    ok('node ids are real entity ids',
       all(entities.valid_entity_id(n['id']) and n['id'] == n['entity_id']
           for n in g['nodes']), [n['id'] for n in g['nodes']][:3])
    ok('the counts block is complete',
       all(k in g['counts'] for k in ('entities_total', 'nodes_shown', 'edges_total',
                                      'edges_shown', 'unreviewed_edges',
                                      'unresolved_sources', 'seeds_pending',
                                      'review_open')), g['counts'])
    ok('unresolved sources are counted, not hidden', g['counts']['unresolved_sources'] >= 2)
    ok('an omitted block is present', 'omitted' in g and 'reason' in g['omitted'])
    ok('edges carry rel_id and review_status',
       all('rel_id' in e and 'review_status' in e for e in g['edges']))

    print('\n── 8. Scenario 8 — the visual cap hides nothing from search ──')
    capped = get('/graph?limit=1').json()
    ok('the cap is respected', len(capped['nodes']) == 1)
    ok('...and the response says how many it did not draw',
       capped['omitted']['nodes'] == capped['counts']['entities_total'] - 1
       and capped['omitted']['nodes'] > 0)
    drawn = {n['id'] for n in capped['nodes']}
    hidden = [e for e in get('/entities?limit=100').json()['entities']
              if e['entity_id'] not in drawn]
    ok('a record the cap left out is still findable by search', bool(hidden))
    ok('...and its full dossier still opens',
       get('/entities/' + hidden[0]['entity_id']).status_code == 200)

    print('\n── 9. Source search reaches unassigned, ambiguous and excluded ──')
    for status, sid in (('unassigned', S_UNASSIGNED), ('ambiguous', S_AMBIG),
                        ('excluded', S_EXCLUDED)):
        found = get('/sources/search?status=%s&limit=100' % status).json()
        ok('%s sources are reachable' % status,
           sid in [s['source_id'] for s in found['sources']],
           [s['source_id'] for s in found['sources']])
    text_hit = get('/sources/search?q=paperwork&limit=50').json()
    ok('a text query reaches the ORIGINAL rows',
       S_AMBIG in [s['source_id'] for s in text_hit['sources']])
    ok('...and the excerpt is wrapped as quoted data',
       all(s['excerpt'].startswith('<<<src ') for s in text_hit['sources']
           if s['excerpt']))
    counts = get('/sources/search?limit=1').json()
    ok('the search reports the whole matching population',
       counts['total'] >= 11 and counts['truncated'], counts['total'])
    ok('...broken down by status', counts['counts']['unassigned'] >= 1
       and counts['counts']['ambiguous'] >= 1 and counts['counts']['excluded'] >= 1)
    ok('a corpus filter works',
       {s['corpus'] for s in get('/sources/search?corpus=item').json()['sources']}
       == {'item'})
    ok('an unknown status is ignored and SAID to be ignored',
       any('Unknown status' in n
           for n in get('/sources/search?status=nonsense').json()['notes']))
    by_class = get('/sources/search?class=internal_metadata&limit=50').json()
    ok('the class filter works and is spelled `class`, as the contract says',
       {s['source_class'] for s in by_class['sources']} == {'internal_metadata'},
       by_class['sources'])
    ok('paging is honest about what it left off',
       get('/sources/search?limit=2&offset=1').json()['truncated'])

    print('\n── 10. No credential, key or settings body ever reaches a response ──')
    excl = get('/sources/search?status=excluded&limit=50').json()
    tel = [s for s in excl['sources'] if s['source_id'] == S_EXCLUDED][0]
    ok('the telemetry row is LISTED (nothing is hidden)', tel['source_id'] == S_EXCLUDED)
    ok('...its reason is stated', bool(tel['excluded_reason']))
    ok('...and its body is never rendered',
       tel['excerpt'] == '' and 'excerpt_withheld' in tel)
    leak = get('/sources/search?q=login&limit=50').json()
    ok('a credential inside an ORDINARY turn is redacted, not returned',
       all(FAKE_SECRET not in s['excerpt'] for s in leak['sources'])
       and any('[redacted]' in s['excerpt'] for s in leak['sources']),
       [s['excerpt'] for s in leak['sources']])
    ok('no response body anywhere contains the planted credential',
       not any(FAKE_SECRET in b for b in BODIES),
       [b[:120] for b in BODIES if FAKE_SECRET in b][:1])
    ok('...nor does the graph', FAKE_SECRET not in get('/graph').text)
    ok('...nor does any dossier',
       not any(FAKE_SECRET in get('/entities/' + e['entity_id']).text
               for e in get('/entities?limit=100').json()['entities']))

    print('\n── 11. Bounded retrieval stays under its cap ──')
    reg = entity_context.registry_block()
    ok('the registry renders something', bool(reg.strip()), reg)
    ok('the registry is within its default budget', len(reg) <= 900, len(reg))
    for cap in (60, 120, 300, 900):
        ok('the registry obeys max_chars=%d' % cap,
           len(entity_context.registry_block(max_chars=cap)) <= cap,
           len(entity_context.registry_block(max_chars=cap)))
    ok('the registry carries ids for lookup_entity', RIVERA in reg)
    ok('the registry carries NO private facts',
       'Operations lead' not in reg and 'packet' not in reg, reg)
    txt = entity_context.dossier_text(RIVERA)
    ok('the dossier text renders', bool(txt.strip()))
    ok('the dossier text is within its default budget', len(txt) <= 2200, len(txt))
    for cap in (80, 400, 1000, 2200):
        ok('the dossier text obeys max_chars=%d' % cap,
           len(entity_context.dossier_text(RIVERA, max_chars=cap)) <= cap)
    ok('a truncated render SAYS it was truncated',
       'not shown' in entity_context.dossier_text(RIVERA, max_chars=400))
    ok('statements are labelled by authority, not by recency',
       '[Brady said]' in txt and '[Ace inferred]' in txt, txt)
    ok('a disagreement is rendered AS a disagreement, with both statements',
       'DISAGREEMENTS' in txt and 'Cedar Assist' in txt and 'Northwind Helper' in txt)
    ok('history is rendered as history, not as current truth',
       'HISTORY (dated, and NOT current truth)' in txt)
    ok('a board row is labelled a RECORD, not a quotation',
       'BOARD RECORD' in txt and 'not a quotation' in txt)
    ok('a plan is never rendered as a completion',
       'plan (intention, not a completion)' in txt)
    ok('the standing money note is on every dossier',
       'budget spreadsheet' in txt)
    ok('excluded telemetry never reaches the rendered text',
       FAKE_SECRET not in txt and 'watch cursor' not in txt)
    ok('lookup by id is bounded',
       len(entity_context.lookup(RIVERA, max_chars=500)) <= 500)
    amb = entity_context.lookup('Jordan')
    ok('an ambiguous first name is answered as an ambiguity, never guessed',
       'resolves to NONE' in amb and RIVERA in amb and KIM in amb, amb)
    ok('a name nobody knows returns nothing to say', entity_context.lookup('Zzz Nobody')
       == '')
    from ace2.backend import tools as _tools                          # noqa: E402
    tool_out = _tools.execute('lookup_entity', {'name_or_id': 'Jordan Rivera'})
    ok('the lookup_entity tool is really wired to this renderer',
       '[Brady said]' in tool_out and 'budget spreadsheet' in tool_out, tool_out[:160])
    ok('...and it is a READ, never journalled',
       'lookup_entity' in _tools.NATIVE_READS)
    ok('the tool says "nothing on file" rather than inventing one',
       'Nothing on file' in _tools.execute('lookup_entity',
                                           {'name_or_id': 'Zzz Nobody'}))

    print('\n── 11b. The counts headline is honest about what is unresolved ──')
    cnt = get('/entities/counts').json()
    ok('counts carry the unresolved total',
       cnt['unresolved'] == cnt['unassigned'] + cnt['ambiguous'] + cnt['review_open'],
       cnt)
    ok('...and say so in words',
       any('remain unresolved' in n for n in cnt['notes']), cnt['notes'])
    ok('...and break the queue down by kind', bool(cnt['review_by_kind']))
    ok('excluded sources are counted with their reason',
       cnt['excluded'] >= 1 and cnt['excluded_by_reason'])

    print('\n── 12. The legacy graph is preserved and still costs nothing by default ──')
    leg = get('/graph?source=legacy').json()
    ok('legacy without refresh serves the cache and never rebuilds',
       leg['source'] == 'legacy' and leg.get('empty') is True, leg)
    ok('...and says what a rebuild would cost', 'paid model call' in (leg.get('hint') or ''))
    ok('the entities path never constructed a model client',
       get('/graph?source=entities').status_code == 200)

    print('\n── 12b. An outage answers 503, never "nothing on file" ──')
    # The database is up; make it UNREACHABLE and check that no route quietly reports an
    # empty memory. A confident "0 records" about something nobody managed to read is the
    # exact class of claim this release exists to remove.
    import contextlib as _ctx                                        # noqa: E402
    real_conn = db._conn

    @_ctx.contextmanager
    def _outage():
        raise ConnectionError('synthetic outage')
        yield
    db._conn = _outage
    try:
        for p in ('/entities', '/entities/counts', '/entities/review',
                  '/entities/' + RIVERA, '/sources/search?q=Jordan',
                  '/graph?source=entities'):
            r = get(p)
            ok('an outage is a 503 on GET %s' % p, r.status_code == 503, r.text[:120])
        r = post('/entities/%s/correct' % RIVERA, {'op': 'confirm', 'reason': 'x'})
        ok('...and a correction refuses rather than pretending',
           r.status_code == 503 and r.json()['ok'] is False, r.text[:160])
        ok('...saying nothing was written', 'nothing was written' in r.json()['error'])
        ok('the registry block degrades to "" instead of raising',
           entity_context.registry_block() == '')
    finally:
        db._conn = real_conn
    ok('the routes recover once the database answers again',
       get('/entities').status_code == 200)
    # AND THE THIRD ANSWER: "not indexed yet" is its own state, not "nothing exists".
    real_state = entity_context.layer_state
    entity_context.layer_state = lambda: entity_context.STATE_ABSENT
    try:
        absent = get('/entities').json()
        ok('an un-migrated index says NOT INDEXED, not "nothing exists"',
           absent['index_state'] == 'absent'
           and any('NOT INDEXED' in n for n in absent.get('notes', [])), absent)
        ok('...and points at recall as the thing that still searches',
           any('recall still searches' in n
               for n in get('/entities/counts').json()['notes']))
    finally:
        entity_context.layer_state = real_state

    print('\n── 13. The registry reaches both prompts, and fails silent ──')
    import asyncio                                                    # noqa: E402
    import contextlib                                                 # noqa: E402
    import inspect                                                    # noqa: E402
    import time as _time                                              # noqa: E402
    from unittest.mock import AsyncMock, patch                        # noqa: E402
    from ace2.backend import memory_db, tools                         # noqa: E402

    block = asyncio.run(chat._entity_registry_block())
    ok('the typed path can fetch the registry', RIVERA in block, block[:200])
    ok('an empty registry means the block is OMITTED, not rendered empty',
       chat._registry_lines('') == [])
    ok('a populated registry is rendered under an INDEX header',
       chat._registry_lines(block)[1].startswith('PEOPLE / ORGS / PROJECTS')
       and 'INDEX' in chat._registry_lines(block)[1])

    def quiet(stack):
        """Silence the heavy integrations so the context assembly is the only thing under
        test — the same shape tests/test_upgrade_awareness.py uses."""
        for name in ('_profile_block', '_upgrade_awareness', '_recap_block',
                     '_group_facts', '_format_daybank', '_format_calendar_window',
                     '_format_today_schedule', '_format_thread'):
            stack.enter_context(patch.object(chat, name, return_value=''))
        for obj, name, val in ((chat.brain, 'read_memory', []),
                               (chat.brain, 'read_memory_meta', {}),
                               (chat.daybank, 'read_items', []),
                               (chat, 'get_events_structured', []),
                               (chat, 'get_gmail_summary', ''),
                               (chat, 'get_personal_inbox_structured', [])):
            stack.enter_context(patch.object(obj, name, return_value=val))
        stack.enter_context(patch.object(chat, 'get_weather',
                                         new=AsyncMock(return_value={})))

    with contextlib.ExitStack() as st:
        quiet(st)
        st.enter_context(patch.object(chat, '_CTX', dict(chat._CTX, ts=_time.time(),
                                                         events=[], memory=[], bank=[],
                                                         wx={}, convo=[], memory_meta={},
                                                         registry=block)))
        slow, fast = asyncio.run(chat._live_context())
        voice = asyncio.run(chat._fast_context())
    ok('the typed prompt carries the registry', RIVERA in slow and 'lookup_entity' in slow)
    ok('...in the CACHED half, after ACE MEMORY',
       slow.index('ACE MEMORY') < slow.index('PEOPLE / ORGS / PROJECTS'))
    ok('...and never in the volatile half', 'PEOPLE / ORGS / PROJECTS' not in fast)
    ok('the voice prompt carries it too', RIVERA in voice)
    ok('...read from the pre-warmed cache, not fetched on the turn',
       '_CTX.get("registry")' in inspect.getsource(chat._fast_context)
       and 'registry_block' not in inspect.getsource(chat._fast_context))
    ok('...and the cache is warmed on the background pass',
       'entity_context' in inspect.getsource(chat._refresh_ctx_inner))

    with contextlib.ExitStack() as st:
        quiet(st)
        st.enter_context(patch.object(entity_context, 'registry_block',
                                      side_effect=RuntimeError('pool is on fire')))
        st.enter_context(patch.object(chat, '_CTX', dict(chat._CTX, ts=_time.time(),
                                                         events=[], memory=[], bank=[],
                                                         wx={}, convo=[], memory_meta={},
                                                         registry='')))
        broken_slow, _ = asyncio.run(chat._live_context())
        broken_voice = asyncio.run(chat._fast_context())
    ok('a failing registry yields no block and no exception',
       'PEOPLE / ORGS / PROJECTS' not in broken_slow
       and 'PEOPLE / ORGS / PROJECTS' not in broken_voice)
    ok('...and the rest of the prompt is still assembled', bool(broken_slow.strip()))

    # ACCEPTANCE NOTE 33. This index does NOT cover the old Drive monthly history, the
    # shared Telegram window or the recovered pre-wipe archive, so the existing recall
    # path must remain exactly where it was.
    src = inspect.getsource(memory_db._build_corpus)
    ok('recall is still a native read', 'recall' in tools.NATIVE_READS)
    ok('...and still reads the Drive history, Telegram and the recovered archive',
       'read_shared_conversation' in src and 'read_recovered_history' in src
       and '_read_all_history' in src)

except Exception:
    import traceback
    traceback.print_exc()
    FAILURES.append('the check itself raised')
finally:
    try:
        db._POOL["p"] and db._POOL["p"].closeall()
    except Exception:
        pass
    server.cleanup()
    tmp.cleanup()

print('\n%d check(s) run, %d failure(s)' % (CHECKS[0], len(FAILURES)))
for f in FAILURES:
    print('  FAILED: %s' % f)
sys.exit(1 if FAILURES else 0)
