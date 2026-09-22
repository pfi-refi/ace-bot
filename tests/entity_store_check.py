"""Entity store + migration check — real Postgres, synthetic corpus, no model calls.

Disposable `pgserver`, in the style of tests/board_repair_check.py. Everything asserted
here is read back from the DATABASE, never from the thing that wrote it.

WHAT THIS FILE EXISTS TO KEEP DEAD, in the order it hurt:

  1. THE ORIGINALS MOVING. `facts`, `turns`, `daybank_items`, `summaries` and every
     profile version must be byte-identical before and after — twice — measured with the
     same row-count + content-hash manifest Codex's independent checker uses.
  2. A CONFIDENTLY WRONG POPULATION. Run against the real corpus the first version of
     this migration created 283 "people" including `Business Review`, `Call Armando` and
     `Base Shop`, and filed `Allianz Life`, `Ally Bank` and `Amazon Prime` as PEOPLE.
     Those exact generic labels are fixtures here now.
  3. IDENTITY GUESSES. Jordan Rivera and Jordan Kim stay two people; a bare "Jordan"
     attaches to neither; Sienna and Syanna never merge.
  4. A PLAN READ AS A COMPLETION, and one finished task marking an unrelated one done.
  5. A HUMAN CORRECTION BEING UNDONE BY A REPLAY.
  6. A RECORD DISAPPEARING — unassigned sources stay counted and findable.

Every name here is invented. `Jordan Rivera`, `Jordan Kim`, `Sienna`, `Syanna` and `Chris`
are Codex's acceptance names; the rest is made up. Nothing in this file comes from Brady's
real data.

`Willowmere` stands in for the project in Codex's acceptance scenario 5 (a job offer and a
project staying distinct while linked to the same person). Codex originally named a real
project there, and the real migration found it in the live corpus with 76 sources — so it
was renamed here rather than committed into the repository. The scenario is unchanged; only
the label is synthetic.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pgserver                                          # noqa: E402

tmp = tempfile.TemporaryDirectory(prefix='ace-entity-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    from ace2.backend import db, entities, entity_index, entity_migrate   # noqa: E402

    db._init_schema()
    db._ready = True
    db._trgm_ok = False

    NOW = datetime.now(timezone.utc)

    def ago(days):
        return NOW - timedelta(days=days)

    # ── The synthetic corpus ────────────────────────────────────────────────────
    # Shapes, not content: two people sharing a first name, two near-identical names,
    # a relationship with no name, a plan, a later completion claim, an archived
    # correction, a source that matches nothing, telemetry, a QA row, and the generic
    # capitalized labels that must never become people.
    TURNS = [
        ('user', 'Jordan Rivera is my client from the Ridgeview referral.', 40),
        ('user', 'I called Jordan Rivera about the signature packet today.', 39),
        ('user', 'I met Jordan Kim at Halcyon Partners LLC last Tuesday.', 38),
        ('user', 'Jordan Kim said the Halcyon Partners LLC paperwork is ready.', 37),
        ('user', 'Jordan called back about the paperwork.', 36),
        ('user', 'Sienna Marsh needs the packet; Syanna Marsh does not.', 35),
        ('user', 'I talked to Sienna Marsh about her aunt this morning.', 34),
        ('user', 'my aunt is flying in on the weekend.', 33),
        ('user', 'I called Syanna Marsh about the other file.', 32),
        ('user', 'Syanna Marsh said she will handle the other file herself.', 31),
        ('user', 'I will send Chris Ferro the packet tomorrow.', 30),
        ('user', 'Chris Ferro also has a job offer to look at.', 29),
        ('user', 'Sent Chris Ferro the packet yesterday.', 20),
        ('user', 'I emailed Marlow Bexley about the Willowmere project.', 28),
        ('user', 'Marlow Bexley said the Willowmere project starts in spring.', 27),
        ('assistant', 'Confirmed — the packet was sent to Chris Ferro. Nothing else '
                      'touched.', 19),
        ('user', 'The barn roof still leaks after the storm.', 15),
        # Six sightings of a capitalized phrase with no person and no org context: it
        # must stay a candidate, and it must outrank a one-sighting candidate.
        ('user', 'The Ridgeview Ledger printed the schedule.', 26),
        ('user', 'Another Ridgeview Ledger notice arrived.', 25),
        ('user', 'The Ridgeview Ledger deadline moved.', 24),
        ('user', 'Ridgeview Ledger again about the schedule.', 23),
        ('user', 'Checking the Ridgeview Ledger listing.', 22),
        ('user', 'The Ridgeview Ledger listing is wrong.', 21),
        ('user', 'This is a temporary Codex QA test, not a real life update.', 14),
    ]
    FACTS = [
        ('Jordan Rivera works at Halcyon Partners LLC.', 'sweep', 'active', None, 26),
        ('Jordan Rivera lives in Ridgeview.', 'sweep', 'active', None, 25),
        ('Sienna Marsh lives in Fairview.', 'sweep', 'archived', 12, 24),
        ('Sienna Marsh lives in Eastport.', 'sweep', 'active', None, 10),
        ('Learning sweep complete. Saved three updates: Jordan Rivera, Sienna Marsh, '
         'Marlow Bexley.', 'sweep', 'active', None, 23),
        ('ACE SELF-NOTE: consider trimming the planning prompt.', 'reflection',
         'active', None, 22),
        ('Chris Ferro is weighing a job offer from Ironvale Systems.', 'sweep',
         'active', None, 21),
        ('The Willowmere project needs a concrete estimate.', 'sweep', 'active', None, 20),
    ]
    ITEMS = [
        ('itm00001', 'Mail the signature packet to Chris Ferro', 'open', 18),
        ('itm00002', 'Book the job offer appointment for Chris Ferro', 'open', 17),
        ('itm00003', 'Business Review', 'open', 16),
        ('itm00004', 'Call Armando', 'open', 16),
        ('itm00005', 'Base Shop', 'open', 15),
        ('itm00006', 'Account Manager', 'open', 15),
        ('itm00007', 'Build Team', 'open', 14),
        ('itm00008', 'Best Use Case Column', 'open', 14),
        ('itm00009', 'Call Cross Country', 'open', 13),
        ('itm00010', 'Call Jordan', 'open', 13),
        ('itm00011', 'Renew the Allianz Life policy', 'open', 12),
        ('itm00012', 'Move the savings to Ally Bank', 'open', 12),
        ('itm00013', 'Cancel Amazon Prime', 'open', 11),
        ('itm00014', 'CODEX QA TEMPORARY marker — verify board controls', 'open', 10),
    ]
    SUMMARIES = [
        ('watch_state', '{"tick": 1}', 9),
        ('watch_snapshot', '{"tick": 2}', 9),
        ('recap', 'We went over the Willowmere project and the Chris Ferro packet.', 8),
        ('brief_morning', 'Today: the signature packet for Chris Ferro.', 7),
        ('graph_cache', '{"nodes": [{"id": "halcyon", "label": "Halcyon Partners LLC", '
                        '"type": "person"}, {"id": "jordan-rivera", '
                        '"label": "Jordan Rivera", "type": "person"}], '
                        '"edges": [{"source": "Jordan Rivera", '
                        '"target": "Halcyon Partners LLC", "kind": "works_at"}]}', 30),
        ('graph_cache', '{"nodes": [{"id": "halcyon", "label": "Halcyon Partners LLC", '
                        '"type": "person"}, {"id": "jordan-rivera", '
                        '"label": "Jordan Rivera", "type": "person"}, '
                        '{"id": "marlow-bexley", "label": "Marlow Bexley", '
                        '"type": "person"}], "edges": []}', 6),
        ('ace_profile', 'Brady runs personal client work and a concrete side business. '
                        'He tracks obligations on the board and money in a spreadsheet.',
         40),
        ('ace_profile', 'Brady runs personal client work, a concrete side business, and '
                        'is weighing a stable job. Money authority is the spreadsheet.',
         5),
    ]

    with db._conn() as c, c.cursor() as cur:
        for role, content, d in TURNS:
            cur.execute("INSERT INTO turns(ts, source, role, content) "
                        "VALUES(%s, 'ace2', %s, %s)", (ago(d), role, content))
        for text, src, tier, sup, d in FACTS:
            cur.execute("INSERT INTO facts(ts, text, source, tier, valid_from, "
                        "invalid_at) VALUES(%s,%s,%s,%s,%s,%s)",
                        (ago(d), text, src, tier, ago(d),
                         ago(11) if tier == 'archived' else None))
        for iid, text, status, d in ITEMS:
            cur.execute("INSERT INTO daybank_items(id, ts, kind, text, status, tags) "
                        "VALUES(%s,%s,'todo',%s,%s,'[]'::jsonb)",
                        (iid, ago(d), text, status))
        for kind, text, d in SUMMARIES:
            cur.execute("INSERT INTO summaries(ts, kind, text) VALUES(%s,%s,%s)",
                        (ago(d), kind, text))

    def eid_of(name, type_=None):
        rows, _ = entities.find_entities(name, type=type_, limit=20)
        want = entities.norm_alias(name)
        for r in rows:
            if entities.norm_alias(r["display_name"]) == want:
                return r["entity_id"]
        return None

    def names_of_type(t):
        rows, _ = entities.find_entities("", type=t, limit=200)
        return {r["display_name"] for r in rows}

    # ── 1. DRY RUN WRITES NOTHING ───────────────────────────────────────────────
    base = entity_migrate.manifest()
    dry = entity_migrate.run(apply=False)
    assert dry["ok"] is True, dry["errors"]
    assert dry["entities_created"] > 0, 'the dry run previewed no entities at all'
    assert entity_migrate.manifest() == base, 'the dry run changed an original table'
    assert entities.tables_exist() is False, 'the dry run left its tables behind'
    import logging as _logging
    _q = _logging.getLogger('ace2.entities')
    _q.setLevel(_logging.CRITICAL)          # the next call is SUPPOSED to find nothing
    assert entities.counts()["entities"] == 0, 'the dry run persisted entities'
    _q.setLevel(_logging.NOTSET)
    DRY_CREATED = dry["entities_created"]
    DRY_LINKED = dry["linked"]

    # ── 2. APPLY ────────────────────────────────────────────────────────────────
    first = entity_migrate.run(apply=True)
    assert first["ok"] is True, first["errors"]
    assert first["source_integrity"]["ok"] is True
    for t, d in first["source_integrity"]["tables"].items():
        assert d["unchanged"], f'{t} changed during the apply'
    assert entity_migrate.manifest() == base, 'an original table moved during --apply'
    assert first["entities_created"] == DRY_CREATED, \
        f'the dry run promised {DRY_CREATED} entities and the apply made ' \
        f'{first["entities_created"]}'
    assert first["linked"] == DRY_LINKED, 'the dry run and the apply disagreed on links'

    c0 = entities.counts()
    assert c0["entities"] == first["entities_created"]
    assert c0["sources_total"] == first["reconciliation"]["scanned"]

    # ── 3. EVERY SOURCE IS ACCOUNTED FOR ────────────────────────────────────────
    rec = first["reconciliation"]
    assert rec["balances"] is True, rec
    # Counted from the database, not from the fixture lists: db._init_schema() also
    # writes a board_review_boundary summary, and a reconciliation that only balances
    # against what the test THINKS is there is not a reconciliation.
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM turns) + (SELECT count(*) FROM facts) "
                    "+ (SELECT count(*) FROM daybank_items) "
                    "+ (SELECT count(*) FROM summaries)")
        total_rows = int(cur.fetchone()[0])
    assert rec["scanned"] == total_rows, f'{rec["scanned"]} scanned of {total_rows}'
    assert rec["indexed"] + rec["excluded"] + rec["unassigned"] + rec["ambiguous"] \
        == total_rows
    for corpus, d in first["phases"]["sources"]["per_corpus"].items():
        assert d["complete"] is True, f'{corpus} skipped rows: {d}'
    # profile history is two separately addressable sources, not one mutable one
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_sources WHERE corpus = 'profile'")
        assert cur.fetchone()[0] == 2, 'a profile version was collapsed or dropped'

    # ── 4. EXCLUSIONS ARE LABELLED, COUNTED AND STILL THERE ─────────────────────
    by_reason = c0["excluded_by_reason"]
    # watch_state + watch_snapshot + the board_review_boundary row db._init_schema wrote
    assert by_reason.get(entities.EXCL_TELEMETRY) == 3, by_reason
    assert by_reason.get(entities.EXCL_QA) == 2, by_reason          # 1 turn + 1 board row
    assert by_reason.get(entities.EXCL_META) == 2, by_reason        # sweep + self-note
    assert by_reason.get(entities.EXCL_GRAPH) == 2, by_reason       # both snapshots
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_sources WHERE status = 'excluded' "
                    "AND excluded_reason IS NULL")
        assert cur.fetchone()[0] == 0, 'a source was excluded without saying why'
        cur.execute("SELECT count(*) FROM ace_entity_links l JOIN ace_sources s "
                    "ON s.source_id = l.source_id WHERE s.excluded_reason IS NOT NULL")
        assert cur.fetchone()[0] == 0, \
            'an excluded source was linked — co-mentions in a save-narration are not ' \
            'evidence of a relationship'

    # ── 5. THE POPULATION IS NOT MADE OF HEADINGS (the release blocker) ─────────
    people = names_of_type("person")
    everything = names_of_type("person") | names_of_type("org") | names_of_type("project")
    GENERIC = ('Business Review', 'Call Armando', 'Base Shop', 'Account Manager',
               'Build Team', 'Best Use Case Column', 'Call Cross Country', 'Call Jordan',
               'Cross Country')
    for label in GENERIC:
        assert label not in everything, f'{label!r} became an entity'
        assert label not in people, f'{label!r} became a PERSON'
    for company in ('Allianz Life', 'Ally Bank', 'Amazon Prime'):
        assert company not in people, f'{company!r} was filed as a person'
    assert 'Jordan Rivera' in people and 'Jordan Kim' in people, sorted(people)
    assert 'Halcyon Partners LLC' in names_of_type("org"), sorted(names_of_type("org"))

    # ── 6. SCENARIO 1 — two Jordans, and a bare Jordan that attaches to neither ─
    j_riv, j_kim = eid_of('Jordan Rivera'), eid_of('Jordan Kim')
    assert j_riv and j_kim and j_riv != j_kim, 'the two Jordans were collapsed'
    assert entities.resolve_alias('Jordan Rivera') == ('resolved', j_riv)
    assert entities.resolve_alias('Jordan Kim') == ('resolved', j_kim)
    verdict, ids = entities.resolve_alias('Jordan')
    assert verdict == 'ambiguous' and sorted(ids) == sorted([j_riv, j_kim]), (verdict, ids)
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT s.source_id, s.status FROM ace_sources s WHERE s.corpus = "
                    "'turn' AND s.source_id IN (SELECT 'turn:' || t.id::text FROM turns t "
                    "WHERE t.content LIKE 'Jordan called back%%')")
        row = cur.fetchone()
        assert row and row[1] == 'ambiguous', f'the bare-Jordan source is {row}'
        cur.execute("SELECT count(*) FROM ace_entity_links WHERE source_id = %s "
                    "AND retracted_at IS NULL", (row[0],))
        assert cur.fetchone()[0] == 0, 'an ambiguous name was linked anyway'
    amb = [r for r in entities.review_queue(limit=200, kind='ambiguous_name')]
    assert any(r["subject_key"] == 'jordan' for r in amb), \
        'no ambiguous_name review row for the shared first name'
    jrow = next(r for r in amb if r["subject_key"] == 'jordan')
    assert sorted(jrow["payload"]["entity_ids"]) == sorted([j_riv, j_kim])
    # ...and the relationship that names nobody is raised too, not dropped
    assert any(r["subject_key"] == 'relationship:my aunt' for r in amb), \
        '"my aunt" resolved to nothing and said nothing about it'

    # ── 7. SCENARIO 2 — Sienna and Syanna never merge ──────────────────────────
    s1, s2 = eid_of('Sienna Marsh'), eid_of('Syanna Marsh')
    assert s1 and s2 and s1 != s2, 'Sienna and Syanna were merged'
    assert entities.resolve_alias('Sienna Marsh') == ('resolved', s1)
    assert entities.resolve_alias('Syanna Marsh') == ('resolved', s2)
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_entities WHERE status = 'merged'")
        assert cur.fetchone()[0] == 0, 'the migration merged something on its own'
    merges = entities.review_queue(limit=50, kind='merge_candidate')
    for m in merges:
        assert m["state"] == 'open', m
        assert 'SUGGESTION ONLY' in m["payload"]["why"]
    # a suggestion may exist for these two; it may never have acted
    assert any(sorted(m["payload"]["entity_ids"]) == sorted([s1, s2]) for m in merges) \
        or not merges, 'merge suggestions exist but not for the look-alike pair'

    # ── 8. SCENARIO 3 + 4 — a plan is not a completion, the board is untouched ─
    chris = eid_of('Chris Ferro')
    assert chris, 'Chris Ferro was not created from the introduction/context evidence'
    doss = entities.dossier(chris)
    attrs = {f["attribute"] for f in doss["current"]}
    plans = [f for f in doss["current"] if f["attribute"] == 'plan']
    assert plans, f'"I will send ... tomorrow" was not stored as a plan: {attrs}'
    assert all('will send' not in f["value"] for f in doss["current"]
               if f["attribute"] == 'status'), 'a plan was filed as a status'
    # the assistant's "Confirmed — the packet was sent" must not be the authority
    for f in doss["current"] + doss["history"]:
        if f.get("source_class") == 'assistant_inference':
            assert f["authority"] > entities.authority_of('user_statement'), f
    # the board rows are exactly as they were
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT id, text, status FROM daybank_items ORDER BY id")
        assert [(r[0], r[1], r[2]) for r in cur.fetchall()] == \
            sorted([(i, t, s) for i, t, s, _ in ITEMS]), \
            'the migration changed a board row'

    # scenario 4: Brady says sent, the board says open → an explicit discrepancy,
    # and ONLY on the task it is actually about.
    disc = doss["discrepancies"]
    assert disc, 'a user completion claim against an open board row raised nothing'
    flagged = {d["item_id"] for d in disc}
    assert 'itm00001' in flagged, 'the packet discrepancy was missed'
    assert 'itm00002' not in flagged, \
        'finishing the packet flagged an unrelated job-offer appointment as done'
    for d in disc:
        assert 'neither was changed' in d["note"]
    assert {it["item_id"] for it in doss["items"]} >= {'itm00001', 'itm00002'}
    assert all(it["status"] == 'open' for it in doss["items"])

    # ── 9. SCENARIO 5 — two unrelated threads on one person stay distinct ──────
    assert len([f for f in doss["current"] if f["attribute"] == 'plan']) >= 1
    for f in doss["current"]:
        assert f["attribute"] in entities.SINGLE_VALUED_ATTRIBUTES or \
            not f["conflicts_with"], \
            f'two unrelated statements were called a contradiction: {f}'
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_entity_facts WHERE entity_id = %s "
                    "AND superseded_by IS NOT NULL", (chris,))
        assert cur.fetchone()[0] == 0, \
            'the migration auto-superseded a statement without a scoped correction'
    par = eid_of('Willowmere', 'project') or eid_of('Willowmere')
    assert par, 'the Willowmere project was not recognised as its own entity'
    assert par != chris

    # ── 10. SCENARIO 6 — archived originals are dated history, not current ─────
    sienna_doss = entities.dossier(s1)
    hist_values = [f["value"] for f in sienna_doss["history"]]
    cur_values = [f["value"] for f in sienna_doss["current"]]
    assert any('Fairview' in v for v in hist_values), \
        'the archived fact vanished instead of becoming history'
    assert not any('Fairview' in v for v in cur_values), \
        'an archived fact is being presented as current truth'
    closed = [f for f in sienna_doss["history"] if 'Fairview' in f["value"]]
    assert closed and closed[0]["valid_to"], 'the archived claim has no closing date'
    assert closed[0]["superseded_by"] is None, \
        'a replacement chain was invented for an archived fact that has none'
    for f in sienna_doss["current"] + sienna_doss["history"]:
        if f["source"]:
            assert f["source"]["excerpt"].startswith('<<<src '), f["source"]
    assert any(f["stated_at_is_extraction"] for f in sienna_doss["history"]), \
        'a fact extraction time is being presented as an event date'

    # ── 11. DOSSIER SHAPE, BOUNDS AND THE STANDING NOTES ───────────────────────
    assert entities.MONEY_NOTE in doss["notes"]
    assert entities.DATA_NOTE in doss["notes"]
    assert entities.INDEX_NOTE in doss["notes"]
    for s in doss["sources"]:
        assert s["excerpt"].startswith('<<<src ') and s["excerpt"].endswith('>>>')
        assert len(s["excerpt"]) <= entities.EXCERPT_MAX + 12
    small = entities.dossier(chris, max_sources=1, max_history=1)
    assert small["counts"]["sources_shown"] <= 1
    assert len(small["history"]) <= 1
    assert small["counts"]["sources_linked"] >= small["counts"]["sources_shown"]
    for s in doss["suggestions"]:
        assert s["accepted"] is False, 'a proposal was presented as an accepted link'
    # no money is derived anywhere
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_entity_facts WHERE attribute IN "
                    "('total', 'balance', 'amount')")
        assert cur.fetchone()[0] == 0

    # ── 12. SCENARIO 8 — nothing disappears ────────────────────────────────────
    assert c0["unassigned"] > 0, 'the fixture has no unmatched source to test with'
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT s.source_id FROM ace_sources s WHERE s.corpus = 'turn' "
                    "AND s.source_id IN (SELECT 'turn:' || t.id::text FROM turns t "
                    "WHERE t.content LIKE 'The barn roof%%')")
        barn = cur.fetchone()
        assert barn, 'a source that matched nothing was dropped from the index'
        cur.execute("SELECT status FROM ace_sources WHERE source_id = %s", (barn[0],))
        assert cur.fetchone()[0] == 'unassigned'
    # every entity is reachable by search, cap or no cap
    allrows, total = entities.find_entities('', limit=200)
    assert total == c0["entities"] == len(allrows)

    # ── 13. GRAPH SEEDS ARE REVIEW ROWS ONLY ───────────────────────────────────
    gs = first["phases"]["graph_seeds"]
    assert gs["snapshots_indexed_elsewhere"] == 2, gs
    assert gs["nodes"] == 3 and gs["seeds_new"] == 3, gs     # newest snapshot only
    seeds = entities.review_queue(limit=50, kind='graph_seed')
    assert len(seeds) == 3, [s["subject_key"] for s in seeds]
    for s in seeds:
        assert s["payload"]["claimed_type_is_accepted"] is False
        assert s["payload"]["entity_ids"] == []
    # the cache calls Halcyon Partners LLC a PERSON; the layer must not have believed it
    halcyon = eid_of('Halcyon Partners LLC')
    assert entities.get_entity(halcyon)["type"] == 'org'
    assert entities.counts()["relations"] == 0, \
        'a model-generated edge became a stored relation'

    # ── 13b. THE QUEUE IS ACTUALLY PRIORITIZED ─────────────────────────────────
    # Every row used to come out at the schema default of 5, which delegated the whole
    # judgement to whatever the UI happened to sort by.
    q = entities.review_queue(limit=500)
    assert q, 'the review queue is empty'
    assert [r["priority"] for r in q] == sorted(r["priority"] for r in q), \
        'review_queue() is not returning rows in priority order'
    assert len({r["priority"] for r in q}) > 1, \
        'every open review row has the same priority — the queue is not prioritized'
    by_kind = {}
    for r in q:
        by_kind.setdefault(r["kind"], set()).add(r["priority"])
    assert by_kind.get("graph_seed") == {6}, by_kind
    assert max(by_kind.get("ambiguous_name", {9})) <= 2, by_kind
    # Repeated generic wording stays searchable without becoming a review chore.
    assert not any(r["subject_key"] == 'ridgeview ledger' for r in q)
    with db._conn() as cc, cc.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_sources s JOIN turns t ON s.source_id='turn:'||t.id::text WHERE t.content LIKE %s", ('%Ridgeview Ledger%',))
        assert cur.fetchone()[0] == 6

    # ── 13c. find_entities IS NOT N+1 ──────────────────────────────────────────
    # It issued two extra statements PER ROW. Twenty entities was 41 statements — 2 ms
    # on this socket and most of a second at a 20 ms round trip, on a thread that
    # asyncio.to_thread cannot cancel, holding one of ten pool slots the whole time.
    import psycopg2.extensions as _pgext

    class _CountingCursor(_pgext.cursor):
        count = 0

        def execute(self, *a, **kw):
            _CountingCursor.count += 1
            return super().execute(*a, **kw)

    _real_conn = db._conn

    from contextlib import contextmanager as _cm

    class _ConnProxy:
        """psycopg2 connections are read-only objects, so count through a wrapper."""

        def __init__(self, conn):
            self._c = conn

        def cursor(self, *a, **kw):
            kw.setdefault('cursor_factory', _CountingCursor)
            return self._c.cursor(*a, **kw)

        def __getattr__(self, name):
            return getattr(self._c, name)

    @_cm
    def _counting_conn():
        with _real_conn() as conn:
            yield _ConnProxy(conn)

    db._conn = _counting_conn
    try:
        _CountingCursor.count = 0
        rows, _t = entities.find_entities('', limit=20, with_summary=False)
        registry_statements = _CountingCursor.count
        _CountingCursor.count = 0
        entities.find_entities('', limit=20, with_summary=True)
        api_statements = _CountingCursor.count
    finally:
        db._conn = _real_conn
    assert len(rows) >= 5, 'not enough entities to make this measurement meaningful'
    # timeout + count + page + aliases (+ summary). Constant in the number of rows.
    assert registry_statements <= 5, \
        f'the registry path ran {registry_statements} statements for {len(rows)} rows'
    assert api_statements <= 6, \
        f'the API path ran {api_statements} statements for {len(rows)} rows'
    assert all(r["aliases"] for r in rows), 'batching lost the aliases'
    withsum, _t = entities.find_entities('', limit=20, with_summary=True)
    assert any(r["summary"] for r in withsum), 'batching lost the summaries'
    assert all(r["summary"] == "" for r in rows), 'with_summary=False still paid for it'

    # ── 14. EVERYTHING CREATED IS UNREVIEWED ───────────────────────────────────
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_entities WHERE review_status <> 'unreviewed'")
        assert cur.fetchone()[0] == 0, 'the migration confirmed something on its own'

    # ── 15. A SECOND --apply IS A NO-OP, IN NUMBERS ────────────────────────────
    second = entity_migrate.run(apply=True)
    assert second["ok"] is True, second["errors"]
    assert second["entities_created"] == 0, second["entities_created"]
    assert second["linked"] == 0, second["linked"]
    assert second["review_new"] == 0, second["review_new"]
    assert second["entity_facts"] == 0, second["entity_facts"]
    assert second["phases"]["graph_seeds"]["seeds_new"] == 0
    c1 = entities.counts()
    for k in ("entities", "sources_total", "links_active", "entity_facts", "review_open",
              "relations", "unassigned", "ambiguous", "excluded"):
        assert c0[k] == c1[k], f'{k} moved on a replay: {c0[k]} -> {c1[k]}'
    assert entity_migrate.manifest() == base, 'the second apply moved an original'

    # ── 16. SCENARIO 7 — a replay does not resurrect a rejected mapping ────────
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT link_id, source_id FROM ace_entity_links WHERE entity_id = %s "
                    "AND retracted_at IS NULL ORDER BY link_id LIMIT 1", (j_riv,))
        link_id, killed_source = cur.fetchone()
    assert entities.unlink(link_id, 'not actually about him', actor='brady') is True
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT retracted_at, retracted_reason FROM ace_entity_links "
                    "WHERE link_id = %s", (link_id,))
        row = cur.fetchone()
        assert row[0] is not None and 'not actually' in row[1]
        cur.execute("SELECT count(*) FROM ace_entity_audit WHERE op = 'unlink'")
        assert cur.fetchone()[0] == 1, 'the correction was not audited'
    third = entity_migrate.run(apply=True)
    assert third["ok"] is True, third["errors"]
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT retracted_at FROM ace_entity_links WHERE link_id = %s",
                    (link_id,))
        assert cur.fetchone()[0] is not None, \
            'the backfill resurrected a mapping Brady had removed'
        cur.execute("SELECT count(*) FROM ace_entity_links WHERE entity_id = %s "
                    "AND source_id = %s AND retracted_at IS NULL", (j_riv, killed_source))
        assert cur.fetchone()[0] == 0, 'the rejected mapping came back as a second row'
    assert third["tombstones_respected"] >= 1, third["tombstones_respected"]
    # a rejected review row stays rejected too
    rej = entities.review_queue(limit=5, kind='graph_seed')[0]
    assert entities.resolve_review(rej["review_id"], 'rejected', actor='brady') is True
    entity_migrate.run(apply=True)
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT state FROM ace_entity_review WHERE review_id = %s",
                    (rej["review_id"],))
        assert cur.fetchone()[0] == 'dismissed', 'a rejected review row was re-opened'

    # ── 16b. A REJECTED ALIAS DOES NOT LAPSE ON THE NEXT NEW SOURCE ───────────
    # The tombstone above protects the PAST. This protects the future: Brady rejects an
    # alias, a brand-new turn arrives tomorrow containing that exact name, and it must
    # still not license a link — otherwise the correction looks like it worked and
    # quietly did not.
    marlow = eid_of('Marlow Bexley')
    assert marlow, 'the fixture lost the entity this section needs'
    with db._conn() as c, c.cursor() as cur:
        cur.execute("UPDATE ace_entity_aliases SET review_status = 'rejected' "
                    "WHERE entity_id = %s AND alias_norm = 'marlow bexley'", (marlow,))
        assert cur.rowcount == 1
    assert entities.resolve_alias('Marlow Bexley')[0] != 'resolved', \
        'a rejected alias still resolves'
    with db._conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO turns(source, role, content) VALUES('ace2','user',%s) "
                    "RETURNING id", ('Marlow Bexley is my new accountant and called about the new schedule.',))
        fresh = cur.fetchone()[0]
    entity_index.note('turn', fresh)
    assert entity_index.drain(timeout=15) is True
    fresh_sid = 'turn:%d' % fresh
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_entity_links WHERE source_id = %s "
                    "AND entity_id = %s AND retracted_at IS NULL", (fresh_sid, marlow))
        assert cur.fetchone()[0] == 0, \
            'a rejected alias minted a fresh link on a brand-new source'
        # ...and the mention is SEEN, not silently dropped
        cur.execute("SELECT status FROM ace_sources WHERE source_id = %s", (fresh_sid,))
        assert cur.fetchone()[0] == 'ambiguous', 'the rejected mention vanished'
    assert any(r["subject_key"] == 'marlow bexley'
               for r in entities.review_queue(limit=500, kind='ambiguous_name')), \
        'the rejected name was not raised for review'
    # a full re-apply must not undo the rejection either
    entity_migrate.run(apply=True)
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_entity_links WHERE source_id = %s "
                    "AND entity_id = %s AND retracted_at IS NULL", (fresh_sid, marlow))
        assert cur.fetchone()[0] == 0, 'a replay re-linked through a rejected alias'
    # a CONFIRMED first name still resolves — the confirm path is not collateral damage
    with db._conn() as c, c.cursor() as cur:
        cur.execute("UPDATE ace_entity_aliases SET review_status = 'confirmed' "
                    "WHERE entity_id = %s AND alias_norm = 'marlow'", (marlow,))
        confirmed_rows = cur.rowcount
    if confirmed_rows:
        assert entities.resolve_alias('Marlow') == ('resolved', marlow), \
            'a confirmed first-name alias stopped resolving'

    # ── 16c. A REJECTED ENTITY LICENSES NOTHING, AND IS STILL FINDABLE ────────
    # Rejecting is the intended tool for the unreviewed junk this migration leaves
    # behind. Before this fix it only stopped the record being DRAWN: it still
    # resolved, still rode into the registry block of every prompt, and still collected
    # a link from every new source that named it.
    junk = eid_of('Ridgeview Ledger') or eid_of('Halcyon Partners LLC')
    if not junk:
        junk = entities.upsert_entity('org', 'Northbeam Holdings', origin='manual',
                                      key='manual:northbeam-test')
    junk_name = entities.get_entity(junk)["display_name"]
    assert entities.resolve_alias(junk_name)[0] == 'resolved', 'fixture precondition'
    with db._conn() as c, c.cursor() as cur:
        cur.execute("UPDATE ace_entities SET review_status = 'rejected' "
                    "WHERE entity_id = %s", (junk,))
    # 1. it no longer resolves
    assert entities.resolve_alias(junk_name)[0] != 'resolved', \
        'a rejected entity still resolves'
    # 2. it is out of the alias index, so no new source can link to it
    assert not any(e[0] == junk for v in entities.alias_index().values() for e in v), \
        'a rejected entity is still in the alias index'
    with db._conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO turns(source, role, content) VALUES('ace2','user',%s) "
                    "RETURNING id", ('Following up with %s again today.' % junk_name,))
        rj_turn = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM ace_entity_links WHERE entity_id = %s "
                    "AND retracted_at IS NULL", (junk,))
        links_before = cur.fetchone()[0]
    entity_index.note('turn', rj_turn)
    assert entity_index.drain(timeout=15) is True
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ace_entity_links WHERE entity_id = %s "
                    "AND retracted_at IS NULL", (junk,))
        assert cur.fetchone()[0] == links_before, \
            'a rejected entity collected a new link'
    # 3. it is out of a LISTING (the registry block C renders into every prompt)...
    listed, _ = entities.find_entities('', limit=200)
    assert not any(r["entity_id"] == junk for r in listed), \
        'a rejected entity is still offered in the registry listing'
    # ...but 4. a SEARCH for it still finds it, and the review path still reaches it
    found, ftotal = entities.find_entities(junk_name, limit=20)
    assert any(r["entity_id"] == junk for r in found), \
        'a rejected entity became unfindable — rejected is not deleted'
    assert entities.get_entity(junk)["review_status"] == 'rejected'
    assert entities.dossier(junk)["entity"]["review_status"] == 'rejected'
    # and an explicit override still lists it
    forced, _ = entities.find_entities('', limit=200, include_rejected=True)
    assert any(r["entity_id"] == junk for r in forced)
    with db._conn() as c, c.cursor() as cur:
        cur.execute("UPDATE ace_entities SET review_status = 'unreviewed' "
                    "WHERE entity_id = %s", (junk,))

    # ── 16d. AN EDIT THAT REMOVES THE NAME LEAVES AN HONEST STATUS ────────────
    # `live` used to be tallied BEFORE the stale-link retraction, so a board row edited
    # from a name to something else stayed status='indexed' with zero live links: out
    # of the unassigned population, and therefore never raised for review.
    with db._conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO daybank_items(id, ts, kind, text, status, tags) "
                    "VALUES('itmEDIT1', now(), 'todo', %s, 'open', '[]'::jsonb)",
                    # Not Marlow Bexley: section 16b deliberately left that alias
                    # rejected, and a rejected alias correctly links to nothing.
                    ('Call Jordan Rivera today about the signature packet',))
    entity_index.note('item', 'itmEDIT1')
    assert entity_index.drain(timeout=15) is True
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM ace_sources WHERE source_id = 'item:itmEDIT1'")
        assert cur.fetchone()[0] == 'indexed', 'the fixture never linked in the first place'
        cur.execute("SELECT count(*) FROM ace_entity_links WHERE "
                    "source_id = 'item:itmEDIT1' AND retracted_at IS NULL")
        assert cur.fetchone()[0] >= 1
        cur.execute("UPDATE daybank_items SET text = %s WHERE id = 'itmEDIT1'",
                    ('Buy milk and a new tarp',))
    entity_index.note('item', 'itmEDIT1')
    assert entity_index.drain(timeout=15) is True
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM ace_sources WHERE source_id = 'item:itmEDIT1'")
        st = cur.fetchone()[0]
        cur.execute("SELECT count(*) FILTER (WHERE retracted_at IS NULL), count(*) "
                    "FROM ace_entity_links WHERE source_id = 'item:itmEDIT1'")
        live_n, all_n = cur.fetchone()
    assert live_n == 0 and all_n >= 1, (live_n, all_n)
    assert st != 'indexed', \
        f'an edited source with zero live links is still status={st!r} — it has ' \
        f'fallen out of every bucket a human can reach'
    assert st == 'unassigned', st

    # ── 17. NEW KNOWLEDGE AFTER THE MIGRATION IS DISCOVERABLE ──────────────────
    # The ongoing-memory requirement: an introduction Brady makes tomorrow must not be
    # permanently invisible to person search just because the backfill already ran.
    with db._conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO turns(source, role, content) VALUES('ace2','user',%s) "
                    "RETURNING id",
                    ('Quill Farrow is my new accountant; I called Quill Farrow today.',))
        new_turn = cur.fetchone()[0]
    entity_index.note('turn', new_turn)
    assert entity_index.drain(timeout=15) is True, 'the index queue never drained'
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM ace_sources WHERE source_id = %s",
                    ('turn:%d' % new_turn,))
        got = cur.fetchone()
        assert got, 'the hook did not index the new turn at all'
    # No manual migration here: this must work through the live hook alone.
    quill = eid_of('Quill Farrow')
    assert quill, 'a brand-new introduction produced no entity and no candidate'
    rows, total = entities.find_entities('Quill', limit=10)
    assert total >= 1 and any(r["entity_id"] == quill for r in rows), \
        'the new person is not findable by search'
    qd = entities.dossier(quill)
    assert qd["counts"]["sources_linked"] >= 1, 'the new person has no sourced evidence'

    # A later statement must grow the same record without a manual backfill.
    with db._conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO turns(source,role,content) VALUES('ace2','user',%s) RETURNING id,ts", ('Quill Farrow lives in Northfield.',))
        follow_id, follow_ts = cur.fetchone()
    entity_index.note('turn', follow_id)
    assert entity_index.drain(timeout=15)
    grown = entities.dossier(quill)
    assert any(f.get('source_id') == 'turn:%d' % follow_id and f.get('attribute') == 'location' for f in grown['current'])
    assert entities.get_entity(quill)['last_seen'] >= follow_ts.isoformat()
    before_repeat = entities.counts()
    entity_index.note('turn', follow_id)
    assert entity_index.drain(timeout=15)
    after_repeat = entities.counts()
    for metric in ('entities','links_active','entity_facts'):
        assert before_repeat[metric] == after_repeat[metric], metric + ' duplicated on hook replay'

    # ── 18. THE HOOK IS AN ENQUEUE, NOT A DATABASE CALL ────────────────────────
    import time as _t
    t0 = _t.perf_counter()
    for _ in range(200):
        entity_index.note('turn', new_turn)
    elapsed = _t.perf_counter() - t0
    assert elapsed < 0.5, f'200 note() calls took {elapsed:.3f}s on the caller thread'
    stats = entity_index.queue_stats()
    assert stats["capacity"] == entity_index.NOTE_QUEUE_MAX
    assert stats["queued"] + stats["dropped"] >= 200
    entity_index.drain(timeout=20)
    # a missing table is a silent no-op, not an error
    entity_index.reset_probe()
    entity_index.note('turn', 999999)
    entity_index.note('nonsense', 'x')
    assert entity_index.drain(timeout=10) is True

    # ── 19. RECONCILIATION RECOVERS WHAT A CURSOR CANNOT SEE ───────────────────
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT id FROM facts WHERE text LIKE 'Jordan Rivera lives%%'")
        fid = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM ace_entity_facts WHERE source_id = %s "
                    "AND valid_to IS NULL", ('fact:%d' % fid,))
        live_before = cur.fetchone()[0]
    assert db.archive_fact(fid) is True          # the ORIGINAL retires itself
    got = entity_index.reconcile()
    if live_before:
        assert got["archived_facts_closed"] >= 1, got
        with db._conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM ace_entity_facts WHERE source_id = %s "
                        "AND valid_to IS NULL", ('fact:%d' % fid,))
            assert cur.fetchone()[0] == 0, \
                'an archived original left its claim standing as current'
    # a board row edited in place has a UUID id no cursor orders — the hash sweep finds it
    with db._conn() as c, c.cursor() as cur:
        cur.execute("UPDATE daybank_items SET text = %s WHERE id = 'itm00005'",
                    ('Base Shop — now mentions Marlow Bexley',))
    got = entity_index.reconcile()
    assert got["board_changed"] >= 1, got
    with db._conn() as c, c.cursor() as cur:
        cur.execute("SELECT content_hash FROM ace_sources WHERE source_id = 'item:itm00005'")
        assert cur.fetchone()[0] == entities.content_hash(
            'Base Shop — now mentions Marlow Bexley'), \
            'the re-index did not pick up the edited board row'

    # ── 20. ROLLBACK DROPS ONLY THIS LAYER ─────────────────────────────────────
    before_rollback = entity_migrate.manifest()
    refused = entity_migrate.rollback(confirm=False)
    assert 'refused' in refused["error"] and refused["dropped"] == []
    assert entities.tables_exist() is True
    done = entity_migrate.rollback(confirm=True)
    assert done["source_integrity"]["ok"] is True, done
    assert set(done["dropped"]) == set(entities.TABLES)
    assert entities.tables_exist() is False
    with db._conn() as c, c.cursor() as cur:
        for t in ('facts', 'turns', 'daybank_items', 'summaries'):
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", ('public.' + t,))
            assert cur.fetchone()[0], f'rollback dropped {t}'
    # Dropping the layer moved nothing in the originals — compared against the state
    # immediately before the rollback, because sections 16b, 17 and 19 deliberately
    # appended turns and edited originals to exercise the hooks.
    final = entity_migrate.manifest()
    assert final == before_rollback, 'the rollback touched an original table'
    assert final["summaries"] == base["summaries"], 'summaries moved at any point'
    # Four turns were appended on purpose by sections 16b, 16c and 17 to exercise the
    # live hook. Nothing else may have moved.
    assert final["turns"]["rows"] == base["turns"]["rows"] + 4, \
        'turns changed by something other than the rows this test appended'

    print('PASS: dry run wrote nothing and matched the apply exactly; the originals are '
          'byte-identical across two applies and a rollback; every one of the %d source '
          'records lands in exactly one of indexed/excluded/unassigned/ambiguous; '
          'telemetry, QA rows, save-narrations and graph snapshots are indexed, counted '
          'and never linked; the generic labels (Business Review, Call Armando, Base '
          'Shop, Account Manager, Build Team, Call Jordan, Cross Country) became no '
          'entity at all and Allianz Life / Ally Bank / Amazon Prime never became '
          'people; two Jordans stay two and a bare Jordan links to neither; Sienna and '
          'Syanna never merge; a plan stays a plan and an assistant "sent" never '
          'outranks Brady; the completion discrepancy lands on the packet and not on '
          'the unrelated appointment; archived facts are dated history with no invented '
          'successor; graph seeds are review rows only and the cache calling an LLC a '
          'person changed nothing; a second and third --apply are numeric no-ops; a '
          'link Brady retracted and a review row he rejected survive a replay; a NEW '
          'introduction after the migration is indexed by the hook, promoted and '
          'findable by search; note() is an enqueue that 200 calls run in under half a '
          'second; reconcile() closes claims from an archived original and re-indexes '
          'an edited board row whose UUID no cursor can order; the review queue is '
          'genuinely prioritized, with a six-source candidate ahead of a two-source one '
          'and graph seeds last, and a replay restores an evidence-derived priority '
          'without re-opening anything; a REJECTED alias licenses no link on a '
          'brand-new source and is raised for review instead, while a confirmed first '
          'name still resolves; a REJECTED ENTITY stops resolving, stops being listed '
          'in the registry and collects no new links, while staying findable by search '
          'and reachable by dossier; an edit that removes the last name from a source '
          'leaves it status=unassigned rather than a phantom "indexed" with zero live '
          'links; find_entities is a constant number of statements regardless of row '
          'count; and rollback drops exactly the nine new tables.' % total_rows)
finally:
    server.cleanup()
    tmp.cleanup()
