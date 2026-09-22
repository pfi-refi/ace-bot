"""The backfill — read every original row, build the entity layer, prove nothing moved.

WHY THIS EXISTS (2026-09-21). Ace has 8649 source records and no durable idea of who is in
them. This builds that idea ONCE, from what is already stored, with no model call and no
network. The thing it is most careful about is not what it creates — it is what it must
not disturb.

THE PROMISE, AND HOW IT IS PROVEN. `facts`, `turns`, `daybank_items`, `summaries` and the
profile are never rewritten, never re-statused, never deleted. That is easy to claim and
easy to get wrong, so it is MEASURED: every run captures a manifest — row count plus an
aggregate content hash per original table, computed exactly the way Codex's independent
`check_source_integrity.py` computes it — before and after, and compares them. Drift is a
hard failure with a non-zero exit, and inside an `--apply` batch the count check runs in
the SAME transaction as the writes, so a batch that saw drift rolls itself back.

DRY RUN IS THE DEFAULT, AND IT IS EXACT. It is not a simulation: Postgres DDL and DML are
both transactional, so the dry run creates the layer, runs every phase for real, prints
the numbers, and rolls the entire transaction back. A preview produced by a second,
parallel code path would eventually disagree with the real one; this one cannot.

WHAT THE REAL DATA FORCED (amendment 1):
  • 1013 telemetry summaries, 673 assistant save-narrations and 7 QA rows are indexed,
    counted and searchable, and are NOT scanned for people. A save-narration welds four
    unrelated names into one sentence, so co-mention inside it is evidence of nothing.
  • 181 graph_cache rows are a YEAR of historical snapshots, not one. All 181 are indexed
    as dated history; only the MOST RECENT one seeds review rows, or the queue would fill
    with thousands of duplicate suggestions and become unusable.
  • A graph seed becomes a REVIEW ROW and never an entity. The live cache types `PFI` and
    `GFI Legends` as `person`; a source that is wrong about what a thing IS cannot be
    allowed to create it.
  • 294 archived facts have no `superseded_by`. They become dated history with no
    successor. No replacement chain is invented.

The report never says "fully organized". It prints the unresolved count and the sentence
Codex asked for: a search index is not a verified person dossier.
"""

import difflib
import json
import logging
import re
import time
from contextlib import contextmanager

from . import db, entities, entity_index

logger = logging.getLogger("ace2.entity_migrate")

INDEX_VERSION = 1
BATCH = 400

# The originals. CONSTANTS — there is no path by which caller input names a table here.
# The list matches Codex's independent checker so the two manifests are comparable
# line for line.
MANIFEST_TABLES = ("facts", "turns", "daybank_items", "summaries",
                   "ace_tasks", "ace_write_ops", "ace_review", "push_subs")

# A candidate name must clear this to become an entity (SPEC rule 4).
MIN_SOURCES_FOR_ENTITY = 2
# Above this many entities the merge-candidate sweep is skipped and said to be skipped:
# it is the one O(k²) step in the whole migration, and it is over ENTITY NAMES, never
# over sources. 1500² is 2.25M cheap string comparisons; beyond that it is not worth it.
MERGE_SCAN_MAX = 1500
MERGE_LAST_RATIO = 0.80
MERGE_FIRST_RATIO = 0.60
# Review rows are only useful if a human can work through them.
MAX_UNPROMOTED_REVIEWS = 400
MAX_FACTS_PER_SOURCE = 2

# Only these corpora yield typed entity attributes. The board is deliberately absent: it
# is the authority on task state, and a typed claim derived from a board row would look
# like a second, competing copy of it.
FACT_CORPORA = ("fact", "turn", "profile")


class _DryRunAbort(Exception):
    """Raised at the end of a dry run to roll the whole transaction back."""


@contextmanager
def _session(apply: bool):
    """ONE transaction for the whole run — committed on apply, rolled back on a dry run.

    Batching the apply and committing per batch was the first design and it was wrong in
    two ways at once. A concurrent turn landing mid-run tripped the integrity check after
    half the layer was already committed, leaving a partial population that nobody had
    asked for; and the dry run and the apply then took different paths, so the preview
    was only approximately the thing it was previewing. One transaction gives an atomic
    apply, an exact preview, and the same code for both: the only difference between the
    two is whether the last thing that happens is a COMMIT or a ROLLBACK.

    It costs a long-running transaction — about five seconds on the real corpus, which
    is a price worth paying for "it either all happened or none of it did".
    """
    if apply:
        with db._conn() as c, c.cursor() as cur:
            yield cur
        return
    try:
        with db._conn() as c, c.cursor() as cur:
            yield cur
            raise _DryRunAbort()
    except _DryRunAbort:
        pass


@contextmanager
def _batch(shared_cur):
    """Kept as a seam so every phase reads like a unit of work, not one long function."""
    yield shared_cur


# ── Phase 1: the integrity manifest ─────────────────────────────────────────────
# Tables whose primary key is a monotonically increasing integer. For these the
# integrity window can be FROZEN at a starting id, which is what lets a normal append
# (a new turn arriving while the migration runs) be recognised as Ace working rather
# than as this migration corrupting something. Everything outside this dict is compared
# whole.
_ID_WINDOWED = ("facts", "turns", "summaries", "ace_write_ops", "ace_review", "ace_tasks")


def window_bounds(cur=None) -> dict:
    """{table: max_id} at the start of the run — the frozen integrity window."""
    out = {}

    def _load(c):
        # Ask the catalog which of these actually have an INTEGER id, rather than trying
        # max(id) and catching the error: a failed statement poisons the surrounding
        # transaction, and this one runs inside the migration's single transaction.
        c.execute("SELECT table_name FROM information_schema.columns "
                  "WHERE table_schema = 'public' AND column_name = 'id' "
                  "AND data_type IN ('integer', 'bigint', 'smallint') "
                  "AND table_name = ANY(%s)", (list(_ID_WINDOWED),))
        for (t,) in c.fetchall():
            c.execute("SELECT coalesce(max(id), 0) FROM " + t)
            out[t] = int(c.fetchone()[0] or 0)
    if cur is not None:
        _load(cur)
    else:
        with db._conn() as c, c.cursor() as cu:
            _load(cu)
    return out


def manifest(cur=None, bounds: dict = None) -> dict:
    """{table: {rows, hash}} for every ORIGINAL table that exists.

    `row_to_json(t)::text` covers every column, and ordering by that text makes the
    aggregate independent of physical row order — deliberately the same recipe Codex's
    independent checker uses, so a disagreement between the two would be a real
    disagreement and not a method difference.

    With `bounds`, the comparison is restricted to rows that already existed when the run
    started. A row APPENDED during the run is Ace doing its job and is reported as such;
    a row inside the window that CHANGED is a hard failure. Without bounds this is the
    whole table, which is what the standalone checker compares.
    """
    out = {}

    def _load(c):
        for t in MANIFEST_TABLES:
            c.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + t,))
            if not c.fetchone()[0]:
                continue
            # `t` and the predicate come from module constants, never from a caller.
            cap = (bounds or {}).get(t)
            where = " WHERE x.id <= %s" if cap is not None else ""
            sql = ("SELECT count(*), md5(coalesce(string_agg(row_to_json(x)::text, '' "
                   "ORDER BY row_to_json(x)::text), '')) FROM " + t + " x" + where)
            c.execute(sql, (cap,) if cap is not None else None)
            n, h = c.fetchone()
            out[t] = {"rows": int(n or 0), "hash": h}
            if cap is not None:
                c.execute("SELECT count(*) FROM " + t + " x WHERE x.id > %s", (cap,))
                appended = int(c.fetchone()[0] or 0)
                if appended:
                    out[t]["appended_during_run"] = appended

    if cur is not None:
        _load(cur)
    else:
        with db._conn() as c, c.cursor() as cu:
            _load(cu)
    return out


def row_counts(cur, bounds: dict = None) -> dict:
    """Just the counts, inside the window — the cheap mid-run guard."""
    out = {}
    for t in MANIFEST_TABLES:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + t,))
        if not cur.fetchone()[0]:
            continue
        cap = (bounds or {}).get(t)
        if cap is None:
            cur.execute("SELECT count(*) FROM " + t)
        else:
            cur.execute("SELECT count(*) FROM " + t + " x WHERE x.id <= %s", (cap,))
        out[t] = int(cur.fetchone()[0] or 0)
    return out


def compare_manifest(before: dict, after: dict) -> tuple:
    """(ok, [drift…]). Any difference inside the window is a failure.

    An `appended_during_run` count is NOT drift: this migration issues no INSERT, UPDATE
    or DELETE against an original table, so a row that appeared is Ace continuing to
    work. It is reported by name, and the next incremental pass indexes it.
    """
    drift = []
    for t in sorted(set(before) | set(after)):
        b, a = before.get(t), after.get(t)
        if b is None or a is None:
            drift.append({"table": t, "before": b, "after": a,
                          "problem": "table appeared or vanished"})
        elif b["rows"] != a["rows"] or b["hash"] != a["hash"]:
            drift.append({"table": t, "before": b, "after": a,
                          "problem": ("a row that existed when the run started was "
                                      "changed or removed. This migration never writes "
                                      "to this table, so something else did — the run "
                                      "is aborted and rolled back rather than guessing "
                                      "which rows are still trustworthy.")})
    return (not drift), drift


class SourceDrift(Exception):
    """An original table changed during the run. Nothing is allowed to continue."""


# ── Phase 2/3: candidates and the entity bar ────────────────────────────────────
def _scan(cur, corpora, limit, fn):
    """Walk every non-excluded source once, handing (rec, sid, cls, text) to `fn`."""
    for corpus in corpora:
        since, seen = None, 0
        while True:
            rows = entity_index.read_corpus(corpus, BATCH, since, cur)
            if not rows:
                break
            for rec in rows:
                since = rec["native_id"]
                seen += 1
                text = rec.get("text") or ""
                cls, excl = entities.classify_source(
                    corpus, role=rec.get("role"), kind=rec.get("kind"),
                    source_name=rec.get("source_name"), text=text)
                if excl:
                    continue     # never learn a name from telemetry or a save-narration
                fn(rec, entities.source_id(corpus, rec["native_id"]), cls, text)
            if limit and seen >= limit:
                break


def _case_stats(cur, corpora, limit) -> entity_index.CaseStats:
    """Pass one: how Brady actually cases each word. See entity_index.CaseStats."""
    st = entity_index.CaseStats()
    _scan(cur, corpora, limit, lambda rec, sid, cls, text: st.add_text(text))
    return st


def _collect_candidates(cur, corpora, limit, case: entity_index.CaseStats) -> dict:
    """{alias_norm: candidate} from a deterministic scan. Creates nothing.

    A candidate carries WHICH sources it came from and WHAT KIND of evidence each one
    was, because the bar is about evidence quality, not repetition — and the bar has to
    be something a human can re-check from the row.
    """
    cands = {}
    # Given names that Brady himself uses about a PERSON in conversation. Collected
    # separately because of how the corpus is actually shaped: full names live in legacy
    # sweep facts and board titles, while turns say "Chris". Neither half is enough on
    # its own — a bare given name never resolves (amendment 1 D.3), and a full name
    # sitting only in Ace-generated text is not evidence of a person. Together they are:
    # a full name seen in >=2 sources whose given name Brady addresses as a person in a
    # turn is a person, recorded as unreviewed with that reasoning attached.
    given_strong = {}

    def visit(rec, sid, cls, text):
        corpus = rec["corpus"]
        strong_src = corpus in ("turn", "profile") and cls == "user_statement"
        for raw, start, end, p_ctx, o_ctx in entity_index.span_occurrences(text):
            if p_ctx and strong_src:
                head = entities.norm_alias(case.trim(raw).split()[0]) \
                    if case.trim(raw) else ""
                if head:
                    given_strong.setdefault(head, set()).add(sid)
            trimmed = case.trim(raw)
            if not trimmed:
                continue        # the whole span was common words: "Business Review"
            _add_candidate(cands, trimmed, sid, cls, corpus, rec.get("occurred_at"),
                           person_ctx=p_ctx,
                           org_ctx=o_ctx or entity_index.has_org_suffix(raw),
                           suffix=entity_index.has_org_suffix(raw),
                           name_tokens=all(case.is_name_token(t)
                                           for t in trimmed.split()))
        for intro in entity_index.introductions(text):
            name = case.trim(intro["name"]) or intro["name"]
            _add_candidate(cands, name, sid, cls, corpus, rec.get("occurred_at"),
                           intro={**intro, "source_id": sid},
                           name_tokens=all(case.is_name_token(t) for t in name.split()))
            if intro.get("org") and entity_index.has_org_suffix(intro["org"]):
                _add_candidate(cands, intro["org"], sid, cls, corpus,
                               rec.get("occurred_at"), org_ctx=True, suffix=True,
                               name_tokens=True)

    _scan(cur, corpora, limit, visit)
    for c in cands.values():
        toks = c["display"].split()
        c["given_strong"] = (len(given_strong.get(entities.norm_alias(toks[0]), ()))
                             if len(toks) >= 2 else 0)
    return cands


def _add_candidate(cands, name, sid, source_class, corpus, occurred_at, *,
                   person_ctx=False, org_ctx=False, suffix=False, intro=None,
                   name_tokens=False):
    key = entities.norm_alias(name)
    if not key:
        return
    c = cands.get(key)
    if c is None:
        c = cands[key] = {
            "display": " ".join(str(name).split()), "alias_norm": key,
            "sources": set(), "person_ctx": set(), "person_ctx_strong": set(),
            "org_ctx": set(), "org_ctx_strong": set(), "intro_sources": set(), "project_sources": set(),
            "intros": [], "suffix": False, "name_tokens": name_tokens,
            "first_seen": occurred_at, "last_seen": occurred_at,
        }
    c["sources"].add(sid)
    c["name_tokens"] = c["name_tokens"] or name_tokens
    c["suffix"] = c["suffix"] or suffix
    if person_ctx:
        c["person_ctx"].add(sid)
        # STRONG means a source BRADY produced in conversation, or his own profile.
        # Board rows and legacy_extracted facts are CORROBORATION, not promotion
        # evidence: a board title can be written by Ace (ACCEPTANCE-NOTES line 27), so
        # a span that only ever appears on the board must never create a person.
        if corpus in ("turn", "profile") and source_class == "user_statement":
            c["person_ctx_strong"].add(sid)
    if org_ctx:
        c["org_ctx"].add(sid)
        if source_class == "user_statement" and corpus in ("turn", "profile"):
            c["org_ctx_strong"].add(sid)
    if intro is not None:
        if intro.get("pattern") == "project":
            c["project_sources"].add(sid)
        if source_class == "user_statement" and corpus in ("turn", "profile"):
            c["intro_sources"].add(sid)
        if len(c["intros"]) < 4:
            c["intros"].append(intro)
    if occurred_at:
        if not c["first_seen"] or occurred_at < c["first_seen"]:
            c["first_seen"] = occurred_at
        if not c["last_seen"] or occurred_at > c["last_seen"]:
            c["last_seen"] = occurred_at


def decide_candidate(c) -> tuple:
    """(type, why) — or ('', why-not). THE BAR. Never defaults to person.

    Written as one readable ladder on purpose: what got promoted, as what, and on what
    evidence has to be auditable from this function alone. The ordering matters — org
    evidence is checked before person evidence, because `Allianz Life` is mentioned the
    same way a person is ("call Allianz Life") and the trailing `Life` is the stronger
    signal.

      ORG      a commercial suffix, or explicit org context ("works at X", "policy with
               X"), in >= 2 distinct sources — or a suffix plus an introduction.
      PROJECT  "the X project" in >= 2 distinct sources.
      PERSON   an explicit introduction Brady wrote, OR person-context use in >= 2
               distinct sources of which at least one is a conversation turn or his
               profile. Two tokens minimum: a bare first name is not an identity.
      nothing  everything else stays a REVIEW CANDIDATE with claimed_type 'unknown'.
               An unreviewed suggestion costs nothing; a confidently wrong person in
               Brady's contacts costs trust.
    """
    n_tokens = len(c["display"].split())
    if not c["display"]:
        return "", "nothing was left of the span after common words were removed"
    # A bare company word is not a company: the span "Concrete" or "Insurance" on its own
    # named an industry, not an organization, and the first run created both.
    if (c["suffix"] and n_tokens >= 2
            and (len(c["sources"]) >= MIN_SOURCES_FOR_ENTITY or c["intros"])):
        if not entity_index.org_has_distinct_token(c["display"]):
            return "", ("this is an industry, not an organization — every word in it is "
                        "a generic company word")
        legal_suffix = bool(re.search(r"\b(?:LLC|INC|LTD|LLP|PLLC)\.?$", c["display"], re.I))
        if legal_suffix or (c.get("org_ctx_strong") and len(c["org_ctx"]) >= MIN_SOURCES_FOR_ENTITY):
            return "org", "company name supported by legal suffix or repeated organization context"
        return "", "commercial wording alone does not establish an organization; needs contextual evidence"
    if (c.get("org_ctx_strong") and len(c["org_ctx"]) >= MIN_SOURCES_FOR_ENTITY
            and entity_index.org_has_distinct_token(c["display"])):
        return "org", ("used as an organization ('works at …', 'policy with …') in %d "
                       "distinct sources" % len(c["org_ctx"]))
    if len(c["project_sources"]) >= MIN_SOURCES_FOR_ENTITY:
        return "project", ("named as a project in %d distinct sources"
                           % len(c["project_sources"]))
    if not c["name_tokens"]:
        return "", ("the words in this span are ones Brady writes in lower case "
                    "elsewhere, so the capitals are position, not a name")
    if entity_index.looks_like_place(c["display"]):
        return "", ("this names a place, not a person; it can still become an "
                    "organization on organization evidence")
    if c["intro_sources"]:
        return "person", "explicit introduction in a conversation turn Brady wrote"
    if n_tokens < 2:
        return "", ("a single given name is not an identity; it stays a candidate and "
                    "never resolves on its own")
    if (len(c["person_ctx"]) >= MIN_SOURCES_FOR_ENTITY and c["person_ctx_strong"]):
        return "person", ("addressed or quoted as a person in %d distinct sources, at "
                          "least one of them a conversation turn"
                          % len(c["person_ctx"]))
    if (len(c["person_ctx"]) >= MIN_SOURCES_FOR_ENTITY
            and c.get("given_strong", 0) >= MIN_SOURCES_FOR_ENTITY):
        return "person", ("full name used as a person in %d sources, and its given name "
                          "is addressed as a person in %d of Brady's own turns"
                          % (len(c["person_ctx"]), c["given_strong"]))
    if len(c["person_ctx"]) >= MIN_SOURCES_FOR_ENTITY:
        return "", ("used as a person in %d sources, but never in a conversation turn "
                    "or the profile — board titles and sweep facts can be Ace's own "
                    "wording, so they corroborate and do not promote"
                    % len(c["person_ctx"]))
    if c["person_ctx"]:
        return "", ("used as a person in only %d source; the bar is two distinct "
                    "sources or an explicit introduction" % len(c["person_ctx"]))
    return "", ("a capitalized span in %d source(s) with no person or organization "
                "context; repetition alone is not identity" % len(c["sources"]))


_FIRST_NAME_MIN = 3


def _first_name_of(display: str, type_: str) -> str:
    if type_ != "person":
        return ""
    toks = display.split()
    if len(toks) < 2:
        return ""
    first = toks[0].strip(".,")
    return first if len(first) >= _FIRST_NAME_MIN else ""


def _create_entities(cur, cands, stats, max_new) -> dict:
    """Promote what clears the bar; queue everything else. Returns {alias_norm: id}."""
    created = {}
    decided = {k: decide_candidate(c) for k, c in cands.items()}
    promoted = [(k, c) for k, c in cands.items() if decided[k][0]]
    promoted.sort(key=lambda kv: (-len(kv[1]["sources"]), kv[0]))
    unpromoted = [(k, c) for k, c in cands.items() if not decided[k][0]]
    unpromoted.sort(key=lambda kv: (-len(kv[1]["sources"]), kv[0]))
    stats["candidates"] = len(cands)
    stats["candidates_promoted"] = len(promoted)
    stats["candidates_unpromoted"] = len(unpromoted)
    for k, _c in promoted:
        stats["promoted:" + decided[k][0]] = stats.get("promoted:" + decided[k][0], 0) + 1

    for key, c in promoted:
        type_, why = decided[key]
        if max_new and stats.get("entities_created", 0) >= max_new:
            stats["entities_capped"] = stats.get("entities_capped", 0) + 1
            continue
        first = _first_name_of(c["display"], type_)
        eid, made = entities.upsert_entity_cur(
            cur, type_, c["display"], origin="migration", confidence=0.4,
            review_status="unreviewed", occurred_at=c["last_seen"],
            key=entities.import_key(type_, key),
            first_name_aliases=[first] if first else ())
        if not eid:
            continue
        created[key] = eid
        if made:
            stats["entities_created"] = stats.get("entities_created", 0) + 1
            entities.audit_cur(cur, "migration", "create_entity", eid,
                               {"alias_norm": key, "sources": len(c["sources"])},
                               None, {"display_name": c["display"], "type": type_}, why)
        else:
            stats["entities_existing"] = stats.get("entities_existing", 0) + 1
        # The relationship an introduction actually stated, quoted from the source.
        for intro in c["intros"]:
            if intro.get("relation"):
                entities.add_entity_fact_cur(
                    cur, eid, "relationship", "%s — %s" % (c["display"],
                                                           intro["relation"]),
                    stated_at=c["last_seen"], source_id=intro["source_id"],
                    origin="user_statement", confidence=0.6,
                    source_class="user_statement", supersede=False)

    # Everything the bar refused stays VISIBLE as a candidate with its claimed type
    # recorded as unknown, and with the reason in words. An unreviewed suggestion is the
    # correct output for weak evidence; a confidently wrong person is not.
    reviewable = [(key, c) for key, c in unpromoted if
                  c["intro_sources"] or len(c["org_ctx"]) >= 2 or len(c["project_sources"]) >= 2
                  or (len(c["person_ctx"]) >= 2 and
                      (c["person_ctx_strong"] or c.get("given_strong", 0) >= 2))]
    stats["weak_candidates_search_only"] = len(unpromoted) - len(reviewable)
    for key, c in reviewable[:MAX_UNPROMOTED_REVIEWS]:
        _, made = entities.queue_review_cur(
            cur, "unpromoted_name", key,
            {"name": c["display"], "alias_norm": key, "entity_ids": [],
             "claimed_type": "unknown",
             "sources": sorted(c["sources"])[:8], "source_count": len(c["sources"]),
             "person_context_sources": len(c["person_ctx"]),
             "org_context_sources": len(c["org_ctx"]),
             "why": decided[key][1]},
            priority=entities.review_priority("unpromoted_name", len(c["sources"])))
        if made:
            stats["review_new"] = stats.get("review_new", 0) + 1
    if len(reviewable) > MAX_UNPROMOTED_REVIEWS:
        stats["unpromoted_not_queued"] = len(reviewable) - MAX_UNPROMOTED_REVIEWS
    return created


def _name_collisions(cur, stats) -> list:
    """One review row per alias that maps to two or more active entities.

    A notice for a human, never a merge. "Chris" SHOULD collide, and the right response
    is to show Brady that it does.
    """
    cur.execute("SELECT a.alias_norm, array_agg(DISTINCT a.entity_id) FROM "
                "ace_entity_aliases a JOIN ace_entities e ON e.entity_id = a.entity_id "
                "WHERE e.status = 'active' GROUP BY a.alias_norm HAVING count(DISTINCT "
                "a.entity_id) > 1 ORDER BY a.alias_norm")
    out = []
    for norm, ids in cur.fetchall():
        out.append({"alias_norm": norm, "entity_ids": sorted(ids)})
        _, made = entities.queue_review_cur(
            cur, "name_collision", norm,
            {"alias_norm": norm, "entity_ids": sorted(ids),
             "why": ("%d active entities answer to this name; it resolves to none of "
                     "them until a human says which" % len(ids))},
            priority=entities.review_priority("name_collision"))
        if made:
            stats["review_new"] = stats.get("review_new", 0) + 1
    return out


def _merge_candidates(cur, stats) -> int:
    """Similar names → a SUGGESTION. Never a merge, never a link.

    This is the only place fuzzy matching is allowed to exist at all, and all it may do
    is put a question in a queue. Sienna and Syanna may appear here; they may never be
    joined by anything but a human.
    """
    cur.execute("SELECT entity_id, type, display_name FROM ace_entities "
                "WHERE status = 'active' ORDER BY entity_id")
    rows = cur.fetchall()
    if len(rows) > MERGE_SCAN_MAX:
        stats["merge_scan_skipped"] = len(rows)
        return 0
    made_n = 0
    by_type = {}
    for eid, t, name in rows:
        by_type.setdefault(t, []).append((eid, name, entities.norm_alias(name).split()))
    for t, group in by_type.items():
        for i in range(len(group)):
            e1, n1, t1 = group[i]
            for j in range(i + 1, len(group)):
                e2, n2, t2 = group[j]
                if not t1 or not t2 or len(t1) != len(t2) or t1 == t2:
                    continue
                last = difflib.SequenceMatcher(None, t1[-1], t2[-1]).ratio()
                first = difflib.SequenceMatcher(None, t1[0], t2[0]).ratio()
                if last < MERGE_LAST_RATIO or first < MERGE_FIRST_RATIO:
                    continue
                key = "|".join(sorted([e1, e2]))
                _, made = entities.queue_review_cur(
                    cur, "merge_candidate", key,
                    {"entity_ids": sorted([e1, e2]), "names": sorted([n1, n2]),
                     "similarity": {"last": round(last, 3), "first": round(first, 3)},
                     "why": ("these two names look similar; THIS IS A SUGGESTION ONLY — "
                             "nothing was merged and no link was written")},
                    priority=entities.review_priority("merge_candidate"))
                if made:
                    made_n += 1
                    stats["review_new"] = stats.get("review_new", 0) + 1
    return made_n


# ── Phase 4: graph seeds — review rows, nothing else ────────────────────────────
def _graph_seeds(cur, stats) -> dict:
    """Seed REVIEW ROWS from the most recent graph_cache snapshot. No entity. No relation.

    There are 181 graph_cache rows in the live corpus — a year of daily snapshots. All
    181 are indexed as sources by the ordinary summary pass (dated history, inspectable);
    only the newest seeds the queue, because 181 passes of the same 48 nodes would be
    thousands of duplicate suggestions. The UNIQUE(kind, subject_key) constraint would
    absorb that too, but relying on a constraint to hide a design mistake is not the
    same as not making it.
    """
    out = {"snapshots_indexed_elsewhere": 0, "nodes": 0, "edges": 0, "seeds_new": 0,
           "source_id": None}
    cur.execute("SELECT count(*) FROM summaries WHERE kind = 'graph_cache'")
    out["snapshots_indexed_elsewhere"] = int(cur.fetchone()[0] or 0)
    cur.execute("SELECT id, text, ts FROM summaries WHERE kind = 'graph_cache' "
                "ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return out
    sid = entities.source_id("summary", row[0])
    out["source_id"] = sid
    try:
        blob = json.loads(row[1] or "{}")
    except Exception:
        stats["graph_unparsed"] = 1
        return out
    nodes = [n for n in (blob.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (blob.get("edges") or []) if isinstance(e, dict)]
    out["nodes"], out["edges"] = len(nodes), len(edges)
    by_label = {}
    for e in edges:
        for side in ("source", "target"):
            by_label.setdefault(str(e.get(side) or ""), []).append(
                {"source": e.get("source"), "target": e.get("target"),
                 "kind": e.get("kind")})
    for n in nodes:
        label = " ".join(str(n.get("label") or n.get("id") or "").split())
        if not label:
            continue
        _, made = entities.queue_review_cur(
            cur, "graph_seed", entities.norm_alias(label) or label[:80],
            {"label": label, "claimed_type": str(n.get("type") or "")[:24],
             "claimed_type_is_accepted": False, "entity_ids": [],
             "edges": by_label.get(label, [])[:8], "source_id": sid,
             "why": ("a model-generated graph node. Its type is CLAIMED, not accepted — "
                     "the live cache types organizations as people. It becomes an entity "
                     "only when a human confirms it.")},
            priority=entities.review_priority("graph_seed"))
        if made:
            out["seeds_new"] += 1
            stats["review_new"] = stats.get("review_new", 0) + 1
    return out


# ── Phase 5/6: sources and links, one pass ──────────────────────────────────────
def _entity_facts_for(cur, rec, sid, cls, index, stats) -> int:
    """Typed attributes for a source, QUOTED, one entity per sentence.

    A sentence naming two entities is skipped outright: that is exactly how the aunt's
    signature case ended up carrying Rebecca's mailing date. A plan is stored as a plan.
    Nothing here computes, sums or infers a figure.
    """
    if rec["corpus"] not in FACT_CORPORA:
        return 0
    # COUNT ATTEMPTS, NOT SUCCESSES. Counting successes made the pass non-idempotent:
    # the first run stopped after two inserts, the second run's inserts all hit the
    # dedupe index and "succeeded" zero times, so the budget never ran out and sentence
    # three got its turn — one new row on a replay that was supposed to change nothing.
    # The set of sentences considered has to be the same every time.
    considered = 0
    made = 0
    archived_at = rec.get("invalid_at")
    for sentence in entity_index.sentences(rec.get("text") or "", 30):
        if considered >= MAX_FACTS_PER_SOURCE:
            break
        hits = entities.scan_aliases(sentence, index)
        ids = {h[2][0] for h in hits if h[0] == "resolved" and h[2]}
        if len(ids) != 1:
            continue                       # zero, or a blend — neither is evidence
        attribute = entity_index.attribute_of(sentence)
        if attribute == "note":
            continue                       # the source excerpt already says it
        considered += 1
        ef_id, ok = entities.add_entity_fact_cur(
            cur, list(ids)[0], attribute, sentence, stated_at=rec.get("occurred_at"),
            source_id=sid, origin=cls, confidence=0.5, source_class=cls,
            role=rec.get("role"),
            # ⚠ THE MIGRATION NEVER AUTO-SUPERSEDES. Two statements sharing an attribute
            # are not a correction: a new job-offer plan does not retire an older packet
            # plan, and two roles at two companies are both true. Supersession needs
            # explicit same-proposition evidence, which a bulk import does not have, so
            # every dated statement is retained and supersession is reserved for a
            # scoped correction through the API (or an archived original, below).
            supersede=False,
            # A fact row's ts is when a sweep EXTRACTED it, not when it happened.
            stated_at_is_extraction=(rec["corpus"] == "fact"))
        if ok:
            made += 1
            stats["entity_facts"] = stats.get("entity_facts", 0) + 1
            if attribute == "plan":
                stats["entity_facts_plan"] = stats.get("entity_facts_plan", 0) + 1
            if archived_at is not None:
                # The ORIGINAL was archived. Date the claim closed with the original's
                # own stamp and invent no successor: 294 archived facts have none.
                entities.close_entity_fact_cur(cur, ef_id, archived_at)
                stats["entity_facts_archived"] = stats.get("entity_facts_archived", 0) + 1
    return made


# ── The run ─────────────────────────────────────────────────────────────────────
def run(*, apply: bool = False, corpora=None, limit: int = None, graph_seeds: bool = True,
        resume: bool = False, max_new_entities: int = 0, report_path: str = None) -> dict:
    """Do the whole thing. Returns the report dict; writes only when `apply` is True."""
    t0 = time.time()
    corpora = tuple(c for c in (corpora or entities.CORPORA) if c in entities.CORPORA)
    rep = {
        "mode": "apply" if apply else "dry-run",
        "index_version": INDEX_VERSION,
        "corpora": list(corpora),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "phases": {}, "stats": {}, "source_integrity": {}, "errors": [],
    }
    if not entities.enabled():
        rep["errors"].append("DATABASE_URL is not set; nothing was read or written.")
        rep["ok"] = False
        return rep

    stats = {}
    try:
        with _session(apply) as shared:
            # ── phase 0: schema ──
            with _batch(shared) as cur:
                entities.ready_cur(cur)
            entity_index.reset_probe()

            # ── phase 1: inventory BEFORE ──
            with _batch(shared) as cur:
                bounds = window_bounds(cur)
                before = manifest(cur, bounds)
                baseline_counts = row_counts(cur, bounds)
            rep["phases"]["inventory_before"] = before
            rep["integrity_window"] = bounds

            # ── phase 2: candidates ──
            # Two reads of the corpus, deliberately. The first learns how Brady cases
            # each word; the second cannot judge a span until that is known, because
            # "Call" at the start of a board title is a verb and "Cross" might not be.
            with _batch(shared) as cur:
                case = _case_stats(cur, corpora, limit)
                cands = _collect_candidates(cur, corpora, limit, case)
            decided = {k: decide_candidate(c) for k, c in cands.items()}
            rep["phases"]["candidates"] = {
                "distinct_names": len(cands),
                "promotable": sum(1 for k in cands if decided[k][0]),
                "case_stats": {"distinct_tokens": len(set(case.lower) | set(case.upper)),
                               "common_words": sum(1 for t in set(case.lower)
                                                   if case.is_common(t))},
                "note": ("deterministic capitalized-span and introduction extraction "
                         "against corpus case statistics; no entity exists yet"),
            }

            # ── phase 3: entities ──
            with _batch(shared) as cur:
                _create_entities(cur, cands, stats, max_new_entities)
                _guard(cur, baseline_counts, bounds)
            with _batch(shared) as cur:
                cols = _name_collisions(cur, stats)
                merges = _merge_candidates(cur, stats)
                _guard(cur, baseline_counts, bounds)
            rep["phases"]["entities"] = {
                "created": stats.get("entities_created", 0),
                "by_type": {t: stats.get("promoted:" + t, 0)
                            for t in entities.TYPES if stats.get("promoted:" + t)},
                "already_present": stats.get("entities_existing", 0),
                "capped_not_created": stats.get("entities_capped", 0),
                "unpromoted_queued": min(stats.get("candidates_unpromoted", 0),
                                         MAX_UNPROMOTED_REVIEWS),
                "unpromoted_not_queued": stats.get("unpromoted_not_queued", 0),
                "name_collisions": len(cols),
                "merge_candidates": merges,
                "merge_scan_skipped_entities": stats.get("merge_scan_skipped", 0),
                "bar": ("ORG: a commercial suffix or explicit org context in >=2 "
                        "sources. PROJECT: 'the X project' in >=2 sources. PERSON: an "
                        "explicit introduction Brady wrote, OR person-context use in "
                        ">=2 sources at least one of which is a conversation turn or "
                        "his profile, and never a single given name. NOTHING ELSE "
                        "BECOMES AN ENTITY OF ANY TYPE — there is no person default. "
                        "Everything weaker stays a review candidate with claimed_type "
                        "'unknown'. Every entity created here is unreviewed."),
            }
            rep["collisions"] = cols
            rep["top_unpromoted"] = [
                {"name": c["display"], "sources": len(c["sources"]),
                 "person_context": len(c["person_ctx"]),
                 "org_context": len(c["org_ctx"]), "why_not": decided[k][1]}
                for k, c in sorted(((k, c) for k, c in cands.items() if not decided[k][0]),
                                   key=lambda kv: -len(kv[1]["sources"]))[:25]]
            rep["created_entities"] = sorted(
                ({"name": c["display"], "type": decided[k][0],
                  "sources": len(c["sources"]), "why": decided[k][1]}
                 for k, c in cands.items() if decided[k][0]),
                key=lambda d: (d["type"], d["name"]))

            # ── phase 4: graph seeds ──
            if graph_seeds:
                with _batch(shared) as cur:
                    rep["phases"]["graph_seeds"] = _graph_seeds(cur, stats)
                    _guard(cur, baseline_counts, bounds)
                rep["phases"]["graph_seeds"]["policy"] = (
                    "review rows only — no entity, no relation, no confirmed type")
            else:
                rep["phases"]["graph_seeds"] = {"skipped": True}

            # ── phase 5+6: sources and links, ONE pass ──
            per_corpus = {}
            for corpus in corpora:
                since = None
                if resume:
                    # Read the checkpoint on the SHARED cursor. A dry run holds an open
                    # transaction that has created these tables but not committed, so a
                    # second connection asking about them would block on the DDL lock.
                    with _batch(shared) as cur:
                        cur.execute("SELECT last_native_id FROM ace_index_checkpoint "
                                    "WHERE corpus = %s", (corpus,))
                        got = cur.fetchone()
                        since = got[0] if got else None
                before_scanned = stats.get("scanned", 0)
                before_links = stats.get("links_new", 0)
                last = since
                while True:
                    with _batch(shared) as cur:
                        index = entities.alias_index(cur)
                        rows = entity_index.read_corpus(corpus, BATCH, last, cur)
                        if not rows:
                            break
                        for rec in rows:
                            last = rec["native_id"]
                            got = entity_index.index_source_cur(
                                cur, rec, index, queue=True, origin="migration",
                                stats=stats)
                            if got.get("resolved") and not got.get("excluded"):
                                _entity_facts_for(cur, rec, got["source_id"],
                                                  got.get("source_class"), index, stats)
                        entities.checkpoint_cur(
                            cur, corpus, last_native_id=last,
                            last_ts=rows[-1].get("occurred_at"),
                            counts={"scanned": stats.get("scanned", 0)})
                        # THE IN-BATCH GUARD. It runs in the same transaction as the
                        # writes above, so drift rolls this batch back rather than
                        # being noticed afterwards when it is too late to undo.
                        _guard(cur, baseline_counts, bounds)
                    if limit and (stats.get("scanned", 0) - before_scanned) >= limit:
                        break
                with _batch(shared) as cur:
                    total = entity_index.corpus_total(corpus, cur)
                scanned_here = stats.get("scanned", 0) - before_scanned
                per_corpus[corpus] = {
                    "scanned": scanned_here,
                    "rows_in_corpus": total,
                    # COMPLETENESS IS CHECKED, NOT ASSUMED. A pagination bug that skipped
                    # 958 of 2133 facts reported a perfectly clean run until this line
                    # existed; "every source is accounted for" has to be a measurement.
                    "complete": (scanned_here == total) if not (limit or resume) else None,
                    "links_new": stats.get("links_new", 0) - before_links,
                }
                if per_corpus[corpus]["complete"] is False:
                    rep["errors"].append(
                        "%s: scanned %d of %d rows — sources were skipped"
                        % (corpus, scanned_here, total))
            rep["phases"]["sources"] = {
                "per_corpus": per_corpus,
                "note": ("indexing and linking are ONE pass per source: an in-memory "
                         "alias_norm -> entity dict, O(tokens), no pairwise comparison "
                         "and no model call"),
            }

            # ── phase 7: counts, integrity AFTER ──
            with _batch(shared) as cur:
                after = manifest(cur, bounds)
                rep["counts"] = entities.counts_cur(cur)
            rep["phases"]["inventory_after"] = after
            ok, drift = compare_manifest(before, after)
            rep["source_integrity"] = {"ok": ok, "tables": {
                t: {"before": before.get(t), "after": after.get(t),
                    "unchanged": before.get(t) == after.get(t)}
                for t in sorted(set(before) | set(after))}, "drift": drift}
            if not ok:
                raise SourceDrift("original tables changed during the run")
    except SourceDrift as e:
        rep["errors"].append(str(e))
    except Exception as e:
        logger.exception("entity backfill failed")
        rep["errors"].append("%s: %s" % (type(e).__name__, e))

    rep["stats"] = dict(sorted(stats.items()))
    # The counts were captured INSIDE the session. In a dry run the transaction has since
    # been rolled back, so re-reading them here would report the world as it is, not as
    # the run would have left it — which is the opposite of what a preview is for.
    c = rep.get("counts") or json.loads(json.dumps(entities._ZERO_COUNTS))
    rep["counts"] = c
    scanned = stats.get("scanned", 0)
    rep["indexed"] = stats.get("status:indexed", 0)
    rep["excluded"] = stats.get("excluded", 0)
    rep["unassigned"] = stats.get("status:unassigned", 0)
    rep["ambiguous"] = stats.get("status:ambiguous", 0)
    rep["candidates"] = stats.get("candidates", 0)
    rep["entities_created"] = stats.get("entities_created", 0)
    rep["linked"] = stats.get("links_new", 0)
    rep["entity_facts"] = stats.get("entity_facts", 0)
    rep["review_new"] = stats.get("review_new", 0)
    rep["tombstones_respected"] = stats.get("tombstones_respected", 0)
    rep["unresolved"] = (int(c.get("unassigned", 0)) + int(c.get("ambiguous", 0))
                         + int(c.get("review_open", 0)))
    rep["reconciliation"] = {
        "scanned": scanned,
        "indexed": rep["indexed"], "excluded": rep["excluded"],
        "unassigned": rep["unassigned"], "ambiguous": rep["ambiguous"],
        "sum": rep["indexed"] + rep["excluded"] + rep["unassigned"] + rep["ambiguous"],
        "balances": (rep["indexed"] + rep["excluded"] + rep["unassigned"]
                     + rep["ambiguous"]) == scanned,
        "note": ("every source record is in exactly one of these four buckets. Each "
                 "summaries row kind='ace_profile' is indexed ONCE, under the profile "
                 "corpus keeping its summaries id, and excluded from the summary corpus "
                 "— so profile history is preserved as separate dated versions and "
                 "nothing is counted twice"),
    }
    rep["elapsed_sec"] = round(time.time() - t0, 2)
    rep["ok"] = not rep["errors"] and rep["source_integrity"].get("ok", False)
    if report_path:
        try:
            with open(report_path, "w") as f:
                json.dump(rep, f, indent=2, default=str)
            rep["report_path"] = report_path
        except Exception as e:
            rep["errors"].append("report not written: %s" % type(e).__name__)
    return rep


def _guard(cur, baseline: dict, bounds: dict = None) -> None:
    """Originals unchanged, checked INSIDE the run's own transaction.

    Cheap enough to run between phases, and because the whole apply is one transaction,
    raising here rolls back everything rather than leaving half a layer behind.
    """
    now = row_counts(cur, bounds)
    for t, n in baseline.items():
        if now.get(t) != n:
            raise SourceDrift("%s changed during the run: %s rows -> %s"
                              % (t, n, now.get(t)))


# ── Rollback ────────────────────────────────────────────────────────────────────
def rollback(confirm: bool = False) -> dict:
    """Drop EXACTLY this layer's tables. Nothing else, ever.

    The list is `entities.TABLES`, the same constant the creator uses, so the dropper
    cannot reach a table it does not own. `facts`, `turns`, `daybank_items` and
    `summaries` are not in it and never will be.
    """
    out = {"tables": list(entities.TABLES), "dropped": [], "confirmed": bool(confirm)}
    if not confirm:
        out["error"] = "refused: --yes-drop-entity-layer is required"
        return out
    if not entities.enabled():
        out["error"] = "DATABASE_URL is not set"
        return out
    before = manifest()
    try:
        with db._conn() as c, c.cursor() as cur:
            for t in entities.TABLES:
                if t.startswith("ace_entit") or t in ("ace_sources", "ace_index_checkpoint"):
                    cur.execute("DROP TABLE IF EXISTS " + t + " CASCADE")
                    out["dropped"].append(t)
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    entity_index.reset_probe()
    after = manifest()
    ok, drift = compare_manifest(before, after)
    out["source_integrity"] = {"ok": ok, "drift": drift}
    return out


# ── Report rendering ────────────────────────────────────────────────────────────
def render(rep: dict) -> str:
    """The human report. It never claims the corpus is organized."""
    L = []
    add = L.append
    add("ENTITY BACKFILL — %s%s" % (rep.get("mode", "?"),
                                    "" if rep.get("ok") else "   ** NOT OK **"))
    add("  corpora: %s   index_version %s   %.2fs"
        % (", ".join(rep.get("corpora") or []), rep.get("index_version"),
           rep.get("elapsed_sec", 0)))
    if rep.get("mode") == "dry-run":
        add("  DRY RUN — every phase below really ran, inside one transaction that was "
            "then rolled back. Nothing was written.")
    add("")
    si = rep.get("source_integrity") or {}
    add("1. SOURCE INTEGRITY (the originals)            ok=%s" % si.get("ok"))
    for t, d in sorted((si.get("tables") or {}).items()):
        b, a = d.get("before") or {}, d.get("after") or {}
        add("     %-16s %6s rows  %s   ->  %6s rows  %s   %s"
            % (t, b.get("rows"), (b.get("hash") or "")[:12], a.get("rows"),
               (a.get("hash") or "")[:12],
               "unchanged" if d.get("unchanged") else "*** CHANGED ***"))
    for d in (si.get("drift") or []):
        add("     DRIFT: %s" % d)
    add("")
    ph = rep.get("phases") or {}
    cand = ph.get("candidates") or {}
    add("2. CANDIDATES")
    add("     distinct names %s   clearing the bar %s"
        % (cand.get("distinct_names"), cand.get("promotable")))
    add("     %s" % cand.get("note", ""))
    add("")
    ent = ph.get("entities") or {}
    add("3. ENTITIES")
    add("     created %s %s   already present %s   capped %s"
        % (ent.get("created"), ent.get("by_type") or {}, ent.get("already_present"),
           ent.get("capped_not_created")))
    add("     unpromoted -> review %s   (not queued, over the cap: %s)"
        % (ent.get("unpromoted_queued"), ent.get("unpromoted_not_queued")))
    add("     name collisions %s   merge SUGGESTIONS %s"
        % (ent.get("name_collisions"), ent.get("merge_candidates")))
    add("     bar: %s" % ent.get("bar", ""))
    if rep.get("top_unpromoted"):
        add("     top candidates NOT promoted (visible in the review queue):")
        for u in rep["top_unpromoted"][:12]:
            add("        %-34s %4d src  person-ctx %-3d org-ctx %-3d  %s"
                % (u["name"][:34], u["sources"], u["person_context"],
                   u["org_context"], u["why_not"][:60]))
    add("")
    gs = ph.get("graph_seeds") or {}
    add("4. GRAPH SEEDS")
    if gs.get("skipped"):
        add("     skipped (--no-graph-seeds)")
    else:
        add("     snapshots in the corpus %s (all indexed as dated history)"
            % gs.get("snapshots_indexed_elsewhere"))
        add("     newest snapshot %s: %s nodes / %s edges -> %s new review rows"
            % (gs.get("source_id"), gs.get("nodes"), gs.get("edges"),
               gs.get("seeds_new")))
        add("     %s" % gs.get("policy", ""))
    add("")
    add("5+6. SOURCES AND LINKS")
    for corpus, d in sorted(((ph.get("sources") or {}).get("per_corpus") or {}).items()):
        flag = {True: "all", False: "*** ROWS SKIPPED ***", None: "partial (limited)"}[
            d.get("complete")]
        add("     %-8s scanned %6s of %6s  %-22s new links %6s"
            % (corpus, d.get("scanned"), d.get("rows_in_corpus"), flag,
               d.get("links_new")))
    add("     %s" % (ph.get("sources") or {}).get("note", ""))
    st = rep.get("stats") or {}
    add("     entity facts %s (of which plans %s, dated closed from an archived "
        "original %s)" % (st.get("entity_facts", 0), st.get("entity_facts_plan", 0),
                          st.get("entity_facts_archived", 0)))
    add("     tombstones respected (links a human removed, NOT recreated): %s"
        % rep.get("tombstones_respected", 0))
    add("")
    rc = rep.get("reconciliation") or {}
    add("7. RECONCILIATION — every source record lands in exactly one bucket")
    add("     indexed %s + excluded %s + unassigned %s + ambiguous %s = %s   (scanned %s)"
        % (rc.get("indexed"), rc.get("excluded"), rc.get("unassigned"),
           rc.get("ambiguous"), rc.get("sum"), rc.get("scanned")))
    add("     balances: %s" % rc.get("balances"))
    add("     %s" % rc.get("note", ""))
    for reason, n in sorted((rep.get("counts") or {}).get("excluded_by_reason", {}).items(),
                            key=lambda kv: -kv[1]):
        add("     excluded %6d   %s" % (n, reason))
    cc = rep.get("counts") or {}
    add("     by corpus:       %s" % (cc.get("by_corpus") or {}))
    add("     by source_class: %s" % (cc.get("by_source_class") or {}))
    add("     by role:         %s" % (cc.get("by_role") or {}))
    add("     source_class is NOT a copy of `role`: 'board' rows are user_statement but "
        "rank below a conversation turn, because a board title can be Ace's wording.")
    add("")
    c = rep.get("counts") or {}
    add("8. WHAT IS IN THE LAYER NOW")
    add("     entities %s %s   aliases-> merged %s retired %s"
        % (c.get("entities"), c.get("by_type"), c.get("merged"), c.get("retired")))
    add("     sources %s   links active %s / retracted %s   relations %s   facts %s"
        % (c.get("sources_total"), c.get("links_active"), c.get("links_retracted"),
           c.get("relations"), c.get("entity_facts")))
    add("     review open %s / closed %s" % (c.get("review_open"),
                                             c.get("review_resolved")))
    for kind, byp in sorted((c.get("review_by_kind_priority") or {}).items(),
                            key=lambda kv: (min(kv[1]), -sum(kv[1].values()))):
        add("        %-20s %4d open   by priority %s"
            % (kind, sum(byp.values()), dict(sorted(byp.items()))))
    add("        priority 1 = act on this first (a blocking name, or a candidate with "
        "overwhelming evidence); 6 = graph seeds, the least trustworthy evidence here")
    add("")
    add("9. UNRESOLVED")
    add("     unassigned %s + ambiguous %s + open review %s"
        % (c.get("unassigned"), c.get("ambiguous"), c.get("review_open")))
    add("     %d source(s) remain unresolved and searchable; review proposals are grouped separately."
        % rep.get("unresolved", 0))
    add("     A search index is not a verified person dossier: every entity created here "
        "is unreviewed, and every claim is a quoted source record.")
    add("     Money authority is Brady's budget spreadsheet. Nothing here derives, sums "
        "or totals a figure.")
    for e in rep.get("errors") or []:
        add("  ERROR: %s" % e)
    return "\n".join(L)
