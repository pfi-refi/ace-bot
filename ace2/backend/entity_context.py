"""Bounded retrieval over the entity layer — what a prompt, a tool and the API may read.

WHY THIS EXISTS (2026-09-21). `entities.py` is the STORE: it knows how to hold a claim and
who said it. This module is the only place that decides how much of that store is allowed
to reach a model prompt, a tool reply or an HTTP response, and in what words. Keeping the
two apart matters because the failure modes are different. A store bug loses a fact. A
retrieval bug puts 40,000 characters of somebody else's prose into a voice turn, or quietly
presents Ace's own guess from Tuesday as something Brady said in March.

THE RULES THIS FILE ENFORCES, and what each one is protecting against:

 1. EVERYTHING IS CHARACTER-BOUNDED BEFORE IT IS RETURNED. Not "usually short" — measured,
    clipped, and the clip is announced. There is no concatenation here whose length is a
    function of how much data happens to exist.
 2. THE REGISTRY BLOCK CARRIES NO PRIVATE FACTS. Names, types, ids and counts only. It goes
    into every typed turn and every voice turn, so anything in it is permanently in the
    prompt for every subject — including ones the current conversation has nothing to do
    with. An INDEX of who exists is a fair thing to always carry; a page of everyone's
    business is not.
 3. AUTHORITY BEATS RECENCY, VISIBLY. Every statement is labelled by its source class
    (`[Brady said]` / `[Ace inferred]` / `[legacy extracted memory]`), and a newer
    assistant inference is never printed as though it had replaced an older user
    statement. When two current statements disagree, BOTH are printed, dated, under a
    heading that says they disagree. Contradictions are displayed, never settled here.
 4. A BOARD ROW IS A RECORD, NOT A QUOTATION. Board text can be prose Ace generated; only
    its STATUS is authoritative, and that status is read live from `db.read_items` every
    time. It is labelled BOARD RECORD so nothing downstream can read it as Brady's words.
 5. A PLAN IS NOT A COMPLETION. `attribute='plan'` renders with the word "intention" on the
    line. "I'll send Chris the packet" must never be rendered in a shape that a model can
    summarise as "sent".
 6. EXCLUDED SOURCES NEVER APPEAR. `internal_metadata` (settings, watch telemetry) and
    `test_data` (synthetic QA rows) are indexed, counted and searchable — and they are kept
    out of every rendered dossier and out of every excerpt returned by source search. No
    credential, key or settings row has a path to a prompt or to the graph UI.
 7. EVERY DOSSIER CARRIES THE STANDING NOTES. Money authority is Brady's budget
    spreadsheet, and source excerpts are quoted records rather than instructions.

WHAT ELSE LIVES HERE, AND WHY IT IS HERE RATHER THAN IN entities.py. The global source
search and the governed correction path are read/write helpers the API needs, and
`entities.py` is owned by another agent and under active edit. They are written against
that module's PUBLIC names (`valid_entity_id`, `link_cur`, `audit_cur`, `excerpts`,
`wrap_excerpt`, …) so the store stays the single implementation of every rule. All SQL is
parameterized; no caller input is ever interpolated into a statement, table names included.
"""

import logging

from . import db, entities

logger = logging.getLogger("ace2.entity_context")

# Prompt budgets. The registry rides in EVERY turn, so it is the tightest.
REGISTRY_CHARS = 900
REGISTRY_LIMIT = 20
DOSSIER_CHARS = 2200

# How a statement is attributed on screen and in a prompt. The source CLASS decides this,
# never the timestamp: "recent" is not a synonym for "reliable", and the 566 reflection
# facts in the live corpus are exactly the rows that would otherwise read as testimony.
CLASS_LABEL = {
    "user_statement": "[Brady said]",
    "manual": "[Brady said]",
    "legacy_extracted": "[legacy extracted memory]",
    "secondary_summary": "[Ace's own summary]",
    "assistant_inference": "[Ace inferred]",
    "graph_seed": "[unconfirmed graph seed]",
    "migration": "[legacy extracted memory]",
    "internal_metadata": "[internal telemetry]",
    "test_data": "[synthetic test record]",
}
CLASS_LABEL_DEFAULT = "[unlabelled source]"

# Never rendered into a dossier, a prompt or an excerpt — by construction, in both
# directions: the class check here, and `excluded_reason` on the source row.
HIDDEN_CLASSES = ("internal_metadata", "test_data")

BOARD_NOTE = ("BOARD RECORDS (live task state read from the board just now. A board row is "
              "a RECORD, not a quotation from Brady — its wording may be Ace's. The board "
              "is the only authority on whether something is done):")
PLAN_NOTE = "intention, not a completion"


def _label(source_class: str, origin: str = "") -> str:
    return CLASS_LABEL.get(source_class or "") or CLASS_LABEL.get(origin or "") \
        or CLASS_LABEL_DEFAULT


# AN OUTAGE IS NOT AN EMPTY MEMORY. These are three different answers and they must never
# collapse into one: `ready` (ask away), `absent` (the migration has not run — there is
# genuinely nothing indexed yet), and `unavailable` (the database did not answer, so this
# process does not know what is there and may not imply it knows). Returning "0 records"
# for the third is the exact failure this release exists to remove — a confident statement
# about something that was never read.
STATE_READY = "ready"
STATE_ABSENT = "absent"
STATE_UNAVAILABLE = "unavailable"


# EVERY QUERY IN THIS FILE IS TIME-BOUNDED, and the reason is the thread pool rather than
# the query. These reads reach the turn through `asyncio.to_thread`, and a thread cannot be
# cancelled: when the caller's 1.5 s budget expires it stops WAITING, but the thread keeps
# running and keeps its pooled connection (there are ten). A slow database would therefore
# cost a silently-omitted block AND a leaked slot on every turn, which is how one sluggish
# query becomes a stalled voice call. `SET LOCAL` expires with the transaction, so nothing
# is left behind on the pooled connection.
STATEMENT_TIMEOUT_MS = 4000


def _bound(cur, c, ms: int = STATEMENT_TIMEOUT_MS) -> None:
    try:
        cur.execute("SET LOCAL statement_timeout = %s", (int(ms),))
    except Exception:
        c.rollback()      # the guard is a nicety; keep the connection usable without it


def _layer_state_cur(cur) -> str:
    cur.execute("SELECT to_regclass('public.ace_entities') IS NOT NULL")
    return STATE_READY if cur.fetchone()[0] else STATE_ABSENT


def layer_state() -> str:
    """`ready` | `absent` | `unavailable`. Cheap, and never raises."""
    try:
        if not entities.enabled():
            return STATE_ABSENT          # no database configured: nothing to be wrong about
    except Exception:
        return STATE_UNAVAILABLE
    try:
        with db._conn() as c, c.cursor() as cur:
            _bound(cur, c)
            return _layer_state_cur(cur)
    except Exception as e:
        logger.warning("entity layer unreachable: %s", type(e).__name__)
        return STATE_UNAVAILABLE


def available() -> bool:
    """True when there is an entity layer to READ.

    False covers both "not migrated in" and "the database did not answer", because the
    prompt-facing callers treat them identically: render nothing and let the EXISTING
    recall path carry the turn. The HTTP layer, which has to tell a person the truth
    rather than fill a prompt, asks `layer_state()` instead and answers 503 for an outage.
    """
    return layer_state() == STATE_READY


def _date(iso) -> str:
    return str(iso)[:10] if iso else ""


def _iso(ts):
    try:
        return ts.isoformat() if ts is not None else None
    except Exception:
        return str(ts) if ts else None


def _bounded(lines, max_chars: int, tail: str = "", extra: int = 0) -> str:
    """Join `lines` under a HARD character cap and say what was dropped.

    The cap is on the returned string, tail included — not on the input, not on average,
    and not "unless the last line is long". Anything that does not fit is counted and
    announced, because a silently shortened record is a record that lies by omission.

    `extra` is the count the CALLER already knows it is leaving out (rows beyond its own
    limit). It is added to what the cap drops, so the single number in the tail is the
    real number missing rather than one of two separate half-truths.

    IT TRUNCATES, IT DOES NOT SIEVE (2026-09-22, review finding 2). This used to SKIP a
    line that did not fit and carry on with the next one, which quietly re-parented rows
    under the wrong heading: a section heading is the longest line in a dossier and the
    rows beneath it are the shortest, so near the cap the DISAGREEMENTS heading was
    dropped while the two contradicting facts it introduced survived — and a contradiction
    rendered as two agreed claims is the exact thing amendment C forbids. Cutting at the
    first line that does not fit makes a heading and its rows live or die together, and
    makes the trailing count mean "everything after this point" instead of "some lines,
    somewhere".
    """
    max_chars = max(0, int(max_chars or 0))
    if max_chars <= 0:
        return ""
    lines = list(lines)
    out, used, dropped = [], 0, max(0, int(extra or 0))
    for i, raw in enumerate(lines):
        line = "" if raw is None else str(raw)
        cost = len(line) + (1 if out else 0)
        if used + cost > max_chars:
            dropped += len(lines) - i
            break
        out.append(line)
        used += cost
    if dropped:
        def _note(n):
            t = tail or "… (%d more line(s) not shown — bounded for the prompt)"
            return (t % n) if "%d" in t else t
        # Make ROOM for the honest tail rather than dropping it: a truncation nobody is
        # told about is the one failure a bounded renderer must not have.
        while out and used + len(_note(dropped)) + 1 > max_chars:
            used -= len(out[-1]) + (1 if len(out) > 1 else 0)
            out.pop()
            dropped += 1
        note = _note(dropped)
        if used + len(note) + (1 if out else 0) <= max_chars:
            out.append(note)
    text = "\n".join(out)
    return text[:max_chars]


# ── The registry block — an INDEX of who exists, never the facts ────────────────
def registry_block(limit: int = REGISTRY_LIMIT, max_chars: int = REGISTRY_CHARS) -> str:
    """`- [per_ab12cd34ef56] Jane Rivera (person · 12 sources · last 2026-09-18)` lines.

    Names, types, ids and counts. NO private facts: this string is pasted into every typed
    and every spoken turn, so a summary line here would mean Brady's business about one
    person is in the prompt for a conversation about somebody else entirely. The id is the
    point — it is what `lookup_entity` takes, and looking a person up is how their details
    are supposed to arrive.

    Sorted by `last_seen DESC`, hard-clipped, with an honest tail.

    ONE CONNECTION, TWO STATEMENTS, TIME-BOUNDED (2026-09-22, review finding 5). This used
    to probe `available()` on its own connection and then call `entities.find_entities`,
    which fans out a query per row for aliases and a leading fact — 40-odd statements and
    two pooled slots, on a path that runs on every typed turn and every 30-second voice
    warm. It also fetched a private current-statement `summary` that this block is
    forbidden to render and therefore threw away. So the registry reads exactly the six
    columns it prints, in one statement, on one connection, under a statement_timeout;
    the pool has ten slots and `asyncio.to_thread` cannot be cancelled, so a read that
    overran its budget used to hold a slot indefinitely.
    """
    limit = max(1, min(int(limit or 20), 60))
    state = {"rows": [], "total": 0}

    def _read(cur):
        if _layer_state_cur(cur) != STATE_READY:
            return
        cur.execute("SELECT count(*) FROM ace_entities "
                    "WHERE status = 'active' AND review_status <> 'rejected'")
        state["total"] = int(cur.fetchone()[0] or 0)
        cur.execute(
            "SELECT e.entity_id, e.type, e.display_name, e.review_status, e.last_seen, "
            "(SELECT count(*) FROM ace_entity_links l WHERE l.entity_id = e.entity_id "
            "AND l.retracted_at IS NULL) FROM ace_entities e "
            "WHERE e.status = 'active' AND e.review_status <> 'rejected' "
            "ORDER BY e.last_seen DESC NULLS LAST, e.display_name LIMIT %s", (limit,))
        state["rows"] = cur.fetchall()

    try:
        with db._conn() as c, c.cursor() as cur:
            _bound(cur, c)
            _read(cur)
    except Exception as e:
        logger.warning("registry_block failed: %s", type(e).__name__)
        return ""
    if not state["rows"]:
        return ""
    lines = []
    for eid, kind, name, review, last_seen, n in state["rows"]:
        bits = [kind or "?", "%d source%s" % (n or 0, "" if (n or 0) == 1 else "s")]
        if last_seen:
            bits.append("last " + _date(_iso(last_seen)))
        if (review or "") not in ("confirmed", ""):
            bits.append(review)
        lines.append("- [%s] %s (%s)" % (eid or "?", name or "?", " · ".join(bits)))
    more = max(0, state["total"] - len(lines))
    return _bounded(lines, max_chars, "… (%d more — use lookup_entity by name or id)",
                    extra=more)


# ── One entity, rendered ────────────────────────────────────────────────────────
def _fact_line(f: dict, *, historic: bool = False) -> str:
    attr = str(f.get("attribute") or "note")
    if attr == "plan":
        attr = "plan (%s)" % PLAN_NOTE
    parts = ["- %s: %s" % (attr, f.get("value") or "")]
    parts.append(_label(f.get("source_class"), f.get("origin")))
    if f.get("stated_at"):
        parts.append(_date(f["stated_at"]))
    if f.get("stated_at_is_extraction"):
        parts.append("(that date is when it was extracted, not when it happened)")
    if historic and f.get("valid_to"):
        parts.append("(superseded %s)" % _date(f["valid_to"]))
    if f.get("conflicts_with"):
        # BELT AND BRACES ON THE HEADING (review finding 2). The DISAGREEMENTS heading
        # says these rows are disputed, and `_bounded` now guarantees the heading cannot
        # be dropped while its rows survive. This marker means the row is still honest
        # if it is ever read on its own — quoted, copied, or reached by some future
        # renderer that does not know about the section. HISTORY only escaped the same
        # bug because "(superseded …)" is already inline; a contradiction deserves the
        # same treatment.
        parts.append("(DISPUTED — another current statement disagrees; neither is chosen)")
    if f.get("source_id"):
        parts.append("source %s" % f["source_id"])
    else:
        # Amendment C: a current statement with no provenance is not accepted as one.
        parts.append("(no source on file — unverified)")
    if (f.get("review_status") or "") not in ("confirmed", ""):
        parts.append(f["review_status"])
    return " ".join(parts)


def _visible(f: dict) -> bool:
    return (f.get("source_class") or "") not in HIDDEN_CLASSES


_SRC_OPEN, _SRC_CLOSE = "<<<src ", ">>>"


def _reclip_excerpt(wrapped: str, limit: int) -> str:
    """Shorten an ALREADY-WRAPPED excerpt without breaking the data boundary.

    (2026-09-22, review finding 6.) `entities.wrap_excerpt` puts `<<<src …>>>` around a
    quoted source span, and those delimiters are the untrusted-content boundary: they are
    what tells a model that the words inside were written by somebody else and are a
    record, not an instruction. Clipping the wrapped string cut the closing marker off and
    left an OPEN quote in the prompt — strictly worse than no marker at all, because
    everything after it reads as though it were still inside the quotation. So the inner
    text is unwrapped, clipped, and re-wrapped, which also re-runs the redaction.
    """
    s = str(wrapped or "")
    if not s:
        return ""
    inner = s[len(_SRC_OPEN):-len(_SRC_CLOSE)] \
        if (s.startswith(_SRC_OPEN) and s.endswith(_SRC_CLOSE)) else s
    return entities.wrap_excerpt(inner, limit)


def dossier_text(entity_id: str, *, max_chars: int = DOSSIER_CHARS) -> str:
    """Everything known about one entity, as bounded text a model may read.

    The ORDER is the argument. Current statements first, each dated and attributed, with
    disagreements pulled into their own section so a reader cannot skim past a
    contradiction. Then dated history, explicitly not current. Then relations, then live
    board status under a heading that says a board row is a record rather than a
    quotation, then the discrepancies the store found between Brady's words and the
    board, then what is still unresolved. The standing notes close it.

    Nothing here is computed. No figure is summed, no gap is filled, and where the record
    is silent the text says the record is silent.
    """
    if not available() or not entities.valid_entity_id(entity_id):
        return ""
    try:
        dos = entities.dossier(entity_id)
    except Exception as e:
        logger.warning("dossier_text failed: %s", type(e).__name__)
        return ""
    if not dos or not dos.get("entity"):
        return ""
    ent = dos["entity"]
    counts = dos.get("counts") or {}
    head = "%s — %s · id %s" % (ent.get("display_name") or "?", ent.get("type") or "?",
                                ent.get("entity_id"))
    if (ent.get("review_status") or "") not in ("confirmed", ""):
        head += " · %s (not confirmed by Brady)" % ent["review_status"]
    if ent.get("status") == "merged":
        head += " · merged into %s" % (ent.get("merged_into") or "?")
    lines = [head, entities.DATA_NOTE, entities.MONEY_NOTE,
             "PARTIAL RECORD: these are dated saved statements, not a complete current "
             "picture. Newer direct user corrections and live board state take precedence. "
             "Relative words such as 'today' refer to the statement's source date."]

    # A recent direct statement must not disappear behind a long list of old extracted
    # claims. Raw source references also cover useful context that was never made into a
    # typed entity fact. Association remains subject to the dossier's review status.
    recent = [s for s in (dos.get("sources") or [])
              if s.get("role") == "user" and s.get("source_class") == "user_statement"
              and s.get("excerpt") and not s.get("excluded_reason")]
    recent.sort(key=lambda s: s.get("occurred_at") or "", reverse=True)
    if recent:
        lines.append("RECENT LINKED USER STATEMENTS (quoted source context):")
        for source in recent[:3]:
            lines.append("- %s [Brady said] source %s: %s" % (
                _date(source.get("occurred_at")), source.get("source_id") or "?",
                _reclip_excerpt(source.get("excerpt") or "", 180)))

    items = sorted(dos.get("items") or [], key=lambda it: it.get("status") != "open")
    if items:
        lines.append("")
        lines.append(BOARD_NOTE)
        for it in items:
            lines.append("- [%s] %s (board item %s)%s" % (
                it.get("status") or "unknown", it.get("text") or "",
                it.get("item_id"), "" if it.get("live") else " — not on the board now"))

    current = [f for f in (dos.get("current") or []) if _visible(f)]
    clash = [f for f in current if f.get("conflicts_with")]
    agreed = [f for f in current if not f.get("conflicts_with")]
    lines.append("")
    lines.append("DATED SAVED CLAIMS (not archived; this alone does not prove they remain "
                 "current. A newer Ace inference never outranks something Brady said):")
    if agreed:
        lines.extend(_fact_line(f) for f in agreed)
    elif not clash:
        lines.append("- no non-archived claim is linked here; use recall for other context.")

    if clash:
        lines.append("")
        lines.append("DISAGREEMENTS (two current statements contradict each other. BOTH "
                     "are shown, neither has been chosen, and nothing was deleted):")
        lines.extend(_fact_line(f) for f in clash)

    history = [f for f in (dos.get("history") or []) if _visible(f)
               and (f.get("valid_to") or f.get("superseded_by"))]
    if history:
        lines.append("")
        lines.append("HISTORY (dated, and NOT current truth):")
        lines.extend(_fact_line(f, historic=True) for f in history)
        extra = int(counts.get("history_total") or 0) - int(counts.get("history_shown") or 0)
        if extra > 0:
            lines.append("- (%d older entry/entries not shown)" % extra)

    rels = dos.get("relations") or []
    if rels:
        lines.append("")
        lines.append("RELATED:")
        for r in rels:
            other = r.get("other") or {}
            tag = "" if (r.get("review_status") or "") == "confirmed" \
                else " — PROPOSED, not confirmed"
            lines.append("- %s (%s) %s%s" % (other.get("display_name") or
                                             other.get("entity_id") or "?",
                                             other.get("type") or "?",
                                             str(r.get("kind") or "").replace("_", " "),
                                             tag))

    disc = dos.get("discrepancies") or []
    if disc:
        lines.append("")
        lines.append("DISCREPANCIES (his words and the board do not agree. Report this; "
                     "do not resolve it and do not change the board):")
        for d in disc:
            lines.append("- board item %s is %s, and the record says: %s" % (
                d.get("item_id"), d.get("item_status"), d.get("claim") or ""))

    sug = dos.get("suggestions") or []
    unresolved = int(counts.get("unresolved") or 0)
    if sug or unresolved:
        lines.append("")
        kinds = sorted({str(s.get("kind") or "?") for s in sug})
        lines.append("UNRESOLVED: %d open question(s) in the review queue%s. These are "
                     "PROPOSALS — nothing below has been accepted." %
                     (len(sug) or unresolved,
                      (" (" + ", ".join(kinds) + ")") if kinds else ""))

    linked = int(counts.get("sources_linked") or 0)
    shown = int(counts.get("sources_shown") or 0)
    excluded = int(counts.get("sources_excluded") or 0)
    lines.append("")
    lines.append("COVERAGE: %d linked source(s), %d shown%s." % (
        linked, shown,
        (", %d excluded as telemetry or test data" % excluded) if excluded else ""))
    for n in (dos.get("notes") or [entities.DATA_NOTE, entities.MONEY_NOTE]):
        lines.append(n)
    if entities.MONEY_NOTE not in lines:
        lines.append(entities.MONEY_NOTE)
    return _bounded(lines, max_chars,
                    "… (%d more line(s) not shown — open the record in the graph panel "
                    "for the rest)")


def lookup(name_or_id: str, *, max_chars: int = DOSSIER_CHARS) -> str:
    """The text behind the `lookup_entity` tool. A READ. No model call, ever.

    An ambiguous name is answered as an ambiguity, with the candidate ids, rather than by
    picking the likelier one: "which Jordan" is a question Ace is allowed to ask and is
    never allowed to guess. An unknown name returns "" so the tool can say, in its own
    words, that there is nothing on file — which is a real answer.
    """
    q = " ".join(str(name_or_id or "").split())
    if not q or not available():
        return ""
    if entities.valid_entity_id(q):
        return dossier_text(q, max_chars=max_chars)
    try:
        verdict, ids = entities.resolve_alias(q)
    except Exception:
        verdict, ids = "unknown", []
    if verdict == "resolved":
        return dossier_text(ids, max_chars=max_chars)
    if verdict == "ambiguous" and ids:
        lines = ["%r matches %d records on file, so it resolves to NONE of them. Ask which "
                 "one he means, or look one up by id — do not pick:" % (q, len(ids))]
        for eid in list(ids)[:8]:
            ent = entities.get_entity(eid) or {}
            lines.append("- [%s] %s (%s · %d source(s))" % (
                eid, ent.get("display_name") or "?", ent.get("type") or "?",
                ent.get("source_count") or 0))
        lines.append(entities.INDEX_NOTE)
        return _bounded(lines, max_chars)
    rows, total = entities.find_entities(q=q, limit=8)
    if not rows:
        return ""
    lines = ["Nothing is filed under %r exactly. Nearest names on file (an index entry is "
             "not a verified record):" % q]
    for r in rows:
        lines.append("- [%s] %s (%s · %d source(s))" % (
            r.get("entity_id"), r.get("display_name") or "?", r.get("type") or "?",
            r.get("source_count") or 0))
    if total > len(rows):
        lines.append("… (%d more)" % (total - len(rows)))
    return _bounded(lines, max_chars)


# ── Global source search — the entrypoint for everything unresolved ─────────────
# THE BLOCKER THIS CLOSES (Codex, REVIEW-BLOCKERS). `/entities?q=` searches ENTITIES, and
# graph seeds deliberately create none — so a header reading "118 unresolved" pointed at
# rows with no way to open them. Unassigned, ambiguous and excluded sources are reachable
# here, by text, by corpus, by class and by status, with the same auth as everything else.
#
# `ace_sources` stores NO text (by design — it must not become a stale second copy), so a
# text query is answered by searching the ORIGINAL tables live and intersecting. Table
# names are constants in this dict; a caller's string can never name one.
_TEXT_SQL = {
    "fact": "SELECT id::text FROM facts WHERE text ILIKE %s ORDER BY id DESC LIMIT %s",
    "turn": "SELECT id::text FROM turns WHERE content ILIKE %s ORDER BY id DESC LIMIT %s",
    "summary": "SELECT id::text FROM summaries WHERE text ILIKE %s ORDER BY id DESC LIMIT %s",
    "item": "SELECT id FROM daybank_items WHERE text ILIKE %s ORDER BY ts DESC LIMIT %s",
}
_TEXT_SCAN_CAP = 500        # per corpus, per query

_SOURCE_COLS = ("source_id, corpus, native_id, occurred_at, role, source_class, status, "
                "excluded_reason, char_len")

_EMPTY_SEARCH = {"sources": [], "total": 0, "returned": 0, "truncated": False,
                 "counts": {}, "notes": []}


def _text_matches(cur, q: str) -> tuple:
    """Source ids whose ORIGINAL row contains `q`. Parameterized ILIKE, capped per corpus.

    Returns [] for a query that matches nothing, and the caller treats that as "no hits" —
    never as "no filter", which would silently widen a search into the whole corpus.
    """
    pattern = "%" + str(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    out = []
    capped = False
    for corpus, sql in _TEXT_SQL.items():
        try:
            cur.execute(sql, (pattern, _TEXT_SCAN_CAP))
        except Exception as e:
            logger.warning("source search scan of %s failed: %s", corpus, type(e).__name__)
            continue
        rows = cur.fetchall()
        if len(rows) >= _TEXT_SCAN_CAP:
            capped = True
        out.extend(entities.source_id(corpus, r[0]) for r in rows)
    # An id pasted straight in ("fact:1204") is a legitimate query too.
    if entities.valid_source_id(q):
        out.append(q)
    return out, capped


def search_sources(q: str = "", status: str = None, corpus: str = None,
                   source_class: str = None, limit: int = 50, offset: int = 0) -> dict:
    """Search every indexed source — INCLUDING unassigned, ambiguous and excluded ones.

    Read-only. Excerpts come from `entities.excerpts` (a live read of the original table),
    are wrapped as data by `entities.wrap_excerpt` and redacted by `entities.redact`
    inside it. An `internal_metadata` source NEVER returns an excerpt: settings and watch
    telemetry are the rows most likely to be carrying a credential, and they have no
    business being rendered anywhere. They are still listed, still counted, still
    inspectable by id — nothing is hidden, only the body is withheld, and the row says so.
    """
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    q = " ".join(str(q or "").split())
    out = {"sources": [], "total": 0, "returned": 0, "truncated": False,
           "counts": {}, "notes": [entities.DATA_NOTE, entities.INDEX_NOTE]}
    if not available():
        out["notes"].append("The entity layer is not migrated in, so there is nothing "
                            "indexed to search yet.")
        return out
    status = str(status or "").strip() or None
    corpus = str(corpus or "").strip() or None
    source_class = str(source_class or "").strip() or None
    if status and status not in entities.SOURCE_STATUSES:
        out["notes"].append("Unknown status %r — it was ignored." % status)
        status = None
    if corpus and corpus not in entities.CORPORA:
        out["notes"].append("Unknown corpus %r — it was ignored." % corpus)
        corpus = None
    if source_class and source_class not in entities.SOURCE_CLASSES:
        out["notes"].append("Unknown class %r — it was ignored." % source_class)
        source_class = None

    def _read(cur):
        where, args = [], []
        if q:
            ids, capped = _text_matches(cur, q)
            if not ids:
                out["notes"].append("No original record contains that text.")
                return
            if capped:
                out["notes"].append(
                    "The text scan stopped at %d rows per corpus, so the totals below "
                    "count only what was scanned. Narrow the query to see the rest."
                    % _TEXT_SCAN_CAP)
            where.append("source_id = ANY(%s)")
            args.append(ids)
        if corpus:
            where.append("corpus = %s"); args.append(corpus)
        if source_class:
            where.append("source_class = %s"); args.append(source_class)
        base = (" WHERE " + " AND ".join(where)) if where else ""
        # The status breakdown is computed WITHOUT the status filter, so the caller can
        # see how many unassigned/ambiguous/excluded rows the query reaches even while
        # looking at one bucket. That is the number the graph header quotes.
        cur.execute("SELECT status, count(*) FROM ace_sources" + base + " GROUP BY status",
                    args)
        by_status = {r[0]: int(r[1]) for r in cur.fetchall()}
        cur.execute("SELECT source_class, count(*) FROM ace_sources" + base +
                    " GROUP BY source_class", args)
        by_class = {r[0]: int(r[1]) for r in cur.fetchall()}
        out["counts"] = {"by_status": by_status, "by_class": by_class,
                         "matching": sum(by_status.values()),
                         "unassigned": by_status.get("unassigned", 0),
                         "ambiguous": by_status.get("ambiguous", 0),
                         "excluded": by_status.get("excluded", 0),
                         "indexed": by_status.get("indexed", 0)}
        if status:
            where.append("status = %s"); args.append(status)
            base = " WHERE " + " AND ".join(where)
        total = by_status.get(status, 0) if status else sum(by_status.values())
        out["total"] = int(total)
        cur.execute("SELECT " + _SOURCE_COLS + " FROM ace_sources" + base +
                    " ORDER BY occurred_at DESC NULLS LAST, source_id LIMIT %s OFFSET %s",
                    args + [limit, offset])
        rows = cur.fetchall()
        texts = entities.excerpts([r[0] for r in rows], cur)
        for r in rows:
            sid, cps, native, occurred, role, sclass, sstatus, excl, clen = r
            hidden = (sclass or "") in HIDDEN_CLASSES
            item = {"source_id": sid, "corpus": cps, "native_id": native,
                    "occurred_at": _iso(occurred), "role": role,
                    "source_class": sclass, "status": sstatus,
                    "excluded_reason": excl, "char_len": int(clen or 0),
                    "excerpt": ""}
            if hidden:
                item["excerpt_withheld"] = (
                    "settings / telemetry or synthetic test text is never rendered; the "
                    "row is listed so nothing is hidden")
            elif sid in texts:
                item["excerpt"] = entities.wrap_excerpt(texts[sid])
            out["sources"].append(item)
        out["returned"] = len(out["sources"])
        out["truncated"] = bool(out["total"] > offset + out["returned"])

    try:
        with db._conn() as c, c.cursor() as cur:
            _bound(cur, c)
            _read(cur)
    except Exception as e:
        logger.warning("search_sources failed: %s: %s", type(e).__name__, e)
        out["notes"].append("The source search could not be completed; nothing is claimed "
                            "about what is or is not there.")
    return out


def review_summary(state: str = "open") -> dict:
    """{kind: n} for the review queue, so a count on screen can be opened, not just read."""
    if not available():
        return {"total": 0, "by_kind": {}, "by_state": {}}
    out = {"total": 0, "by_kind": {}, "by_state": {}}

    def _read(cur):
        cur.execute("SELECT state, count(*) FROM ace_entity_review GROUP BY state")
        out["by_state"] = {r[0]: int(r[1]) for r in cur.fetchall()}
        if state:
            cur.execute("SELECT kind, count(*) FROM ace_entity_review WHERE state = %s "
                        "GROUP BY kind ORDER BY 2 DESC", (state,))
        else:
            cur.execute("SELECT kind, count(*) FROM ace_entity_review GROUP BY kind "
                        "ORDER BY 2 DESC")
        out["by_kind"] = {r[0]: int(r[1]) for r in cur.fetchall()}
        out["total"] = sum(out["by_kind"].values())
    try:
        with db._conn() as c, c.cursor() as cur:
            _bound(cur, c)
            _read(cur)
    except Exception as e:
        logger.warning("review_summary failed: %s", type(e).__name__)
    return out


# ── The graph, built from stored entities. ZERO model calls, ever ───────────────
GRAPH_NODE_CAP = 55
_CAP_REASON = "visual cap only — search and detail reach every record"


def _graph_counts_shell(**kw) -> dict:
    base = {"entities_total": 0, "nodes_shown": 0, "edges_total": 0, "edges_shown": 0,
            "unreviewed_edges": 0, "unresolved_sources": 0, "seeds_pending": 0,
            "review_open": 0, "unconfirmed_seed_edges": 0}
    base.update(kw)
    return base


def _recency(ts) -> float:
    """Newest first, and an UNKNOWN date last rather than first.

    A missing `last_seen` is an absence of evidence, so it must never win a place on a
    canvas that only has room for 55 records.
    """
    try:
        return -ts.timestamp()
    except Exception:
        return float("inf")


def graph_payload(limit: int = GRAPH_NODE_CAP) -> dict:
    """`/graph?source=entities`. Real entity ids as node ids, honest counts, no model call.

    The old graph was a PICTURE re-imagined by a paid model run: nothing in it had an id,
    so nothing in it could be corrected, and yesterday's Sienna was not the same object as
    today's. This reads the stored records instead. It costs one connection and no money,
    and a node you tap is the same row you can correct.

    IT STARTS SPARSE, AND THAT IS THE HONEST ANSWER. Graph seeds import as review rows, not
    as entities (the live cache types two organizations as people), so an un-reviewed
    install draws very little. The counts say how many seeds and how many unresolved
    sources are waiting, and the empty shape carries the same counts — a thin map must
    explain itself rather than look broken, and it must never be papered over with a paid
    rebuild nobody asked for.
    """
    limit = max(1, min(int(limit or GRAPH_NODE_CAP), 400))
    now = None
    try:
        from datetime import datetime
        now = datetime.now(db.EASTERN).isoformat()
    except Exception:
        now = None
    empty = {"nodes": [], "edges": [], "source": "entities", "cached": False,
             "generated_at": now, "empty": True,
             "hint": "run ops.entity_backfill --apply",
             "counts": _graph_counts_shell(),
             "omitted": {"nodes": 0, "edges": 0, "reason": _CAP_REASON},
             "notes": [entities.INDEX_NOTE]}
    if not available():
        empty["hint"] = ("the entity layer has not been created yet — run "
                         "ops.entity_backfill --apply")
        return empty

    state = {"nodes": [], "edges": [], "counts": _graph_counts_shell()}

    def _read(cur):
        c = entities.counts_cur(cur)
        cur.execute("SELECT count(*) FROM ace_entity_review WHERE state = 'open' "
                    "AND kind = 'graph_seed'")
        seeds = int(cur.fetchone()[0] or 0)
        cur.execute(
            "SELECT e.entity_id, e.type, e.display_name, e.review_status, e.last_seen, "
            "(SELECT count(*) FROM ace_entity_links l WHERE l.entity_id = e.entity_id "
            "AND l.retracted_at IS NULL) FROM ace_entities e "
            "WHERE e.status = 'active' AND e.review_status <> 'rejected'")
        ents = cur.fetchall()
        cur.execute(
            "SELECT r.rel_id, r.from_entity_id, r.to_entity_id, r.kind, r.origin, "
            "r.review_status FROM ace_entity_relations r "
            "JOIN ace_entities a ON a.entity_id = r.from_entity_id "
            "JOIN ace_entities b ON b.entity_id = r.to_entity_id "
            "WHERE r.retracted_at IS NULL AND a.status = 'active' AND b.status = 'active' "
            "AND a.review_status <> 'rejected' AND b.review_status <> 'rejected' "
            "ORDER BY r.rel_id")
        rels = cur.fetchall()
        state["counts"] = _graph_counts_shell(
            entities_total=len(ents), edges_total=len(rels),
            unreviewed_edges=sum(1 for r in rels if (r[5] or "") != "confirmed"),
            unresolved_sources=int(c.get("unassigned", 0)) + int(c.get("ambiguous", 0)),
            seeds_pending=seeds, review_open=int(c.get("review_open", 0)))
        # AN UNCONFIRMED MODEL CLAIM IS NOT A LINE ON A MAP (2026-09-22, review item B).
        # Amendment E: a seed becomes an entity OR A RELATION only through an explicit
        # human confirmation. A drawn edge reads as a fact at a glance — that is what a
        # graph is FOR — so an `origin='graph_seed'` relation nobody has confirmed is
        # counted, is reachable in the detail panel and the review queue, and is not
        # drawn. This is the cache that types PFI and GFI Legends as people; its opinion
        # about who works with whom gets the same treatment as its opinion about types.
        drawable = [r for r in rels
                    if not ((r[4] or "") == "graph_seed"
                            and (r[5] or "") != "confirmed")]
        unconfirmed_seeds = len(rels) - len(drawable)
        degree = {}
        for r in drawable:
            degree[r[1]] = degree.get(r[1], 0) + 1
            degree[r[2]] = degree.get(r[2], 0) + 1
        # WHICH 55. The most connected first, then the best-evidenced, then the most
        # RECENT — the same "biggest dot is the busiest entity" rule the old map used,
        # decided here from stored degree instead of asked of a model. The recency term
        # sorted ascending until 2026-09-22 (review item A), so the tie-break preferred
        # the OLDEST record and an entity with no `last_seen` at all beat every dated one
        # for a place on the canvas.
        ranked = sorted(ents, key=lambda e: (-degree.get(e[0], 0), -(e[5] or 0),
                                             _recency(e[4]), e[0]))
        drawn = ranked[:limit]
        ids = {e[0] for e in drawn}
        state["nodes"] = [
            {"id": e[0], "entity_id": e[0], "label": str(e[2] or "")[:52],
             "type": e[1], "size": round(7 + 1.7 * min(degree.get(e[0], 0), 11), 1),
             "review_status": e[3], "source_count": int(e[5] or 0)} for e in drawn]
        state["edges"] = [
            {"source": r[1], "target": r[2], "kind": r[3], "rel_id": r[0],
             "origin": r[4], "review_status": r[5]}
            for r in drawable if r[1] in ids and r[2] in ids]
        state["counts"]["nodes_shown"] = len(state["nodes"])
        state["counts"]["edges_shown"] = len(state["edges"])
        state["counts"]["unconfirmed_seed_edges"] = unconfirmed_seeds

    try:
        with db._conn() as c, c.cursor() as cur:
            _bound(cur, c)
            _read(cur)
    except Exception as e:
        logger.warning("graph_payload failed: %s: %s", type(e).__name__, e)
        empty["hint"] = ("the stored map could not be read just now; nothing is claimed "
                         "about what is in it")
        return empty

    counts = state["counts"]
    if not state["nodes"]:
        empty["counts"] = counts
        empty["hint"] = (
            "no confirmed entity to draw yet. Graph seeds import as review rows, not as "
            "entities — confirm them in the review queue, or run "
            "ops.entity_backfill --apply if the migration has not run.")
        return empty
    seed_edges = int(counts.get("unconfirmed_seed_edges") or 0)
    # The reason line has to name every cause, or it is itself a false statement about
    # what is missing.
    reason = _CAP_REASON if not seed_edges else (
        "%s; %d unconfirmed graph-seed link(s) are counted but not drawn until a human "
        "confirms them" % (_CAP_REASON, seed_edges))
    return {
        "nodes": state["nodes"], "edges": state["edges"], "source": "entities",
        "cached": False, "generated_at": now, "counts": counts,
        "omitted": {"nodes": max(0, counts["entities_total"] - counts["nodes_shown"]),
                    "edges": max(0, counts["edges_total"] - counts["edges_shown"]),
                    "unconfirmed_seed_edges": seed_edges, "reason": reason},
        "notes": [entities.INDEX_NOTE],
    }


# ── Governed corrections — audited, non-destructive, one transaction ────────────
# Every op here writes `ace_entity_audit` and nothing here DELETES. `remove_alias`,
# `unlink` and `reject` set a status or a `retracted_at`; `merge` flags the loser and keeps
# every row. `facts`, `turns`, `daybank_items`, `summaries` and the profile are never
# written to by anything in this file — the board stays the only authority on task state.
#
# Each call runs inside ONE transaction. A refusal raises, the transaction rolls back, and
# the caller gets `{"ok": false, "error": …}` having had nothing at all written.

CORRECTION_OPS = ("rename", "add_alias", "remove_alias", "confirm", "reject", "unlink",
                  "link_item", "merge", "unmerge", "set_fact_status", "set_relation_status")
REVIEW_ACTIONS = ("confirm", "reject", "dismiss")
_STATUSES = ("unreviewed", "confirmed", "rejected")


class _Refused(Exception):
    """A refusal with a reason a human can act on. Rolls the transaction back."""


def _entity_snapshot(cur, entity_id: str):
    cur.execute("SELECT entity_id, type, display_name, status, merged_into, review_status, "
                "origin FROM ace_entities WHERE entity_id = %s", (entity_id,))
    r = cur.fetchone()
    if not r:
        return None
    return {"entity_id": r[0], "type": r[1], "display_name": r[2], "status": r[3],
            "merged_into": r[4], "review_status": r[5], "origin": r[6]}


def _close_review_cur(cur, review_id: int, state: str, resolution: str) -> None:
    cur.execute("UPDATE ace_entity_review SET state = %s, resolved_at = now(), "
                "resolution = %s WHERE review_id = %s",
                (state, entities.clip(resolution, 200), int(review_id)))


def _merge_cur(cur, loser: str, winner: str) -> dict:
    if not entities.valid_entity_id(winner):
        raise _Refused("args.into must be an entity id like per_ab12cd34ef56.")
    if loser == winner:
        raise _Refused("An entity cannot be merged into itself.")
    w = _entity_snapshot(cur, winner)
    if not w:
        raise _Refused("No entity %s to merge into." % winner)
    if w["status"] == "merged":
        raise _Refused("%s has itself been merged into %s; merge into the surviving "
                       "record instead." % (winner, w["merged_into"]))
    cur.execute("UPDATE ace_entities SET status = 'merged', merged_into = %s, "
                "updated_at = now() WHERE entity_id = %s AND status <> 'merged'",
                (winner, loser))
    if not (cur.rowcount or 0):
        raise _Refused("%s is already merged; nothing was changed." % loser)
    return {"merged_into": winner, "rows_deleted": 0,
            "aliases_transferred": False,
            "note": ("the merged record and all its links are kept; its aliases are NOT "
                     "transferred, so add any name that should now resolve to %s with "
                     "add_alias." % winner)}


def _resolve_other(cur, label: str, index=None):
    """An unambiguous ACTIVE entity for `label`, or None. Exact normalized alias only."""
    idx = entities.alias_index(cur) if index is None else index
    verdict, ids = entities.resolve_in_index(label, idx)
    return ids if verdict == "resolved" else None


def _confirm_review_cur(cur, review_id: int, kind: str, payload: dict, args: dict,
                        reason: str, actor: str, context_entity: str = None) -> dict:
    """Turn ONE reviewed proposal into a record. The only path a seed may take.

    `graph_seed` is the case that matters: the live cache types `PFI` and `GFI Legends` as
    people, so a seed's type is a CLAIM. It is recorded as claimed, and the type actually
    used is whatever the human passed or, failing that, the claim — with both written into
    the audit row alongside the actor and the reason, so "who decided this was a person"
    is always answerable.
    """
    payload = payload or {}
    detail = {"kind": kind}
    if kind == "graph_seed" and payload.get("proposal") == "relation":
        # THE EDGE'S OWN CONFIRMATION. Both ends must already be real, confirmed-by-
        # existence entities resolved by exact normalized alias — confirming a link is
        # not permission to invent the thing at the other end of it.
        frm = str(args.get("from_entity_id") or payload.get("from_entity_id") or "")
        other_label = str(payload.get("other_label") or "")
        if not entities.valid_entity_id(frm) or not _entity_snapshot(cur, frm):
            raise _Refused("This link's starting record no longer exists; nothing was "
                           "written.")
        to = str(args.get("to_entity_id") or "") or _resolve_other(cur, other_label)
        if not entities.valid_entity_id(to or ""):
            raise _Refused(
                "Nothing on file resolves to %r, so this link has no second end. Create "
                "or confirm that record first, or pass args.to_entity_id. Nothing was "
                "written." % (other_label or "?"))
        if to == frm:
            raise _Refused("A record cannot be linked to itself.")
        ekind = str(args.get("kind") or payload.get("edge_kind") or "related_to")[:40]
        rel_id, made = entities.add_relation_cur(
            cur, frm, to, ekind,
            source_id=(payload.get("source_id")
                       if entities.valid_source_id(payload.get("source_id") or "")
                       else None),
            origin="manual", confidence=0.9, review_status="confirmed")
        detail.update({"proposal": "relation", "rel_id": rel_id, "created": bool(made),
                       "from_entity_id": frm, "to_entity_id": to, "edge_kind": ekind,
                       "entity_id": frm})
        return detail
    if kind == "graph_seed":
        label = " ".join(str(payload.get("label") or "").split())
        if not label:
            raise _Refused("This seed has no label to promote.")
        claimed = str(payload.get("claimed_type") or "")
        want = str(args.get("type") or "").strip().lower()
        type_ = want if want in entities.TYPES else (
            claimed if claimed in entities.TYPES else "person")
        eid, created = entities.upsert_entity_cur(
            cur, type_, args.get("display_name") or label, origin="graph_seed",
            confidence=0.5, review_status="confirmed",
            source_id=payload.get("source_id"), key="manual:review:%d" % int(review_id))
        if not eid:
            raise _Refused("That seed's label cannot be used as a name.")
        sid = payload.get("source_id")
        if sid and entities.valid_source_id(sid):
            entities.link_cur(cur, eid, sid, relation="mentions", method="manual",
                              origin="manual", confidence=0.5,
                              evidence="confirmed graph seed: " + label,
                              review_status="confirmed")
        # ONE CONFIRMATION CONFIRMS ONE THING (2026-09-22, review item B). This used to
        # create up to eight relations from the seed's claimed edges on a single click.
        # Amendment E says a seed becomes an entity OR A RELATION only through an explicit
        # human confirmation, and confirming "Northwind Helper is an organization" is not
        # a statement about who it works with. So each edge becomes its own review row and
        # waits for its own answer; nothing is written to `ace_entity_relations` here.
        queued = 0
        for e in (payload.get("edges") or [])[:8]:
            if not isinstance(e, dict):
                continue
            src, tgt = str(e.get("source") or ""), str(e.get("target") or "")
            other_label = " ".join((tgt if entities.norm_alias(src)
                                    == entities.norm_alias(label) else src).split())
            if not other_label or entities.norm_alias(other_label) == \
                    entities.norm_alias(label):
                continue
            ekind = str(e.get("kind") or "related_to")[:40]
            _rid, made = entities.queue_review_cur(
                cur, "graph_seed",
                "edge:%s|%s|%s" % (entities.norm_alias(label),
                                   entities.norm_alias(other_label), ekind),
                {"proposal": "relation", "label": label, "other_label": other_label,
                 "from_entity_id": eid, "edge_kind": ekind, "entity_ids": [eid],
                 "claimed_type_is_accepted": False, "source_id": sid,
                 "why": ("the model's graph claimed this link. Confirming the node did "
                         "NOT confirm it; it is drawn only once a human says so.")},
                priority=4)
            queued += 1 if made else 0
        detail.update({"entity_id": eid, "entity_created": bool(created),
                       "claimed_type": claimed, "claimed_type_accepted": bool(
                           claimed in entities.TYPES and not want),
                       "type_used": type_, "relations_created": 0,
                       "edge_reviews_queued": queued,
                       "note": ("confirming a node confirms ONLY the node. Its claimed "
                                "links are queued as separate review rows and no "
                                "relation was written.")})
        return detail

    if kind == "unpromoted_name":
        name = " ".join(str(payload.get("name") or payload.get("alias_norm") or "").split())
        if not name:
            raise _Refused("This candidate has no name to promote.")
        want = str(args.get("type") or "").strip().lower()
        type_ = want if want in entities.TYPES else "person"
        eid, created = entities.upsert_entity_cur(
            cur, type_, args.get("display_name") or name, origin="user", confidence=0.7,
            review_status="confirmed", key="manual:review:%d" % int(review_id))
        if not eid:
            raise _Refused("That candidate's name cannot be used as a name.")
        linked = 0
        for sid in (payload.get("sources") or [])[:8]:
            if not entities.valid_source_id(sid):
                continue
            _lid, ok = entities.link_cur(cur, eid, sid, relation="mentions",
                                         method="manual", origin="manual", confidence=0.7,
                                         evidence="confirmed candidate: " + name,
                                         review_status="confirmed")
            linked += 1 if ok else 0
            entities.set_source_status_cur(cur, sid, "indexed")
        detail.update({"entity_id": eid, "entity_created": bool(created),
                       "type_used": type_, "sources_linked": linked})
        return detail

    if kind in ("ambiguous_name", "unassigned_source", "low_confidence_link"):
        target = str(args.get("entity_id") or context_entity or "")
        if not entities.valid_entity_id(target):
            raise _Refused("Confirming this needs args.entity_id — WHICH record the "
                           "source belongs to. Nothing was guessed and nothing written.")
        if not _entity_snapshot(cur, target):
            raise _Refused("No entity %s on file." % target)
        sid = str(args.get("source_id") or payload.get("source_id")
                  or payload.get("example_source_id") or "")
        if not entities.valid_source_id(sid):
            raise _Refused("Confirming this needs a source id like fact:1204 — the "
                           "review row does not name one.")
        link_id, made = entities.link_cur(
            cur, target, sid, relation="mentions", method="manual", origin="manual",
            confidence=0.9, evidence=entities.clip(reason or "confirmed in review", 200),
            review_status="confirmed")
        if link_id is None and not made:
            raise _Refused("That link was retracted earlier and a replay may not "
                           "resurrect it. Nothing was written.")
        entities.set_source_status_cur(cur, sid, "indexed")
        detail.update({"entity_id": target, "source_id": sid, "link_id": link_id})
        return detail

    if kind == "merge_candidate":
        ids = [i for i in (payload.get("entity_ids") or []) if entities.valid_entity_id(i)]
        winner = str(args.get("into") or context_entity or "")
        loser = str(args.get("loser") or "")
        if not loser:
            others = [i for i in ids if i != winner]
            loser = others[0] if len(others) == 1 else ""
        if not (entities.valid_entity_id(winner) and entities.valid_entity_id(loser)):
            raise _Refused("Merging needs args.into (the record that survives) and "
                           "args.loser. Nothing was merged.")
        detail.update({"loser": loser, **_merge_cur(cur, loser, winner)})
        return detail

    if kind in ("name_collision", "discrepancy"):
        # Acknowledged, not acted on. Two people really can answer to one name, and a
        # discrepancy between Brady's words and the board is reported, never resolved here.
        detail.update({"acknowledged": True, "changed": False})
        return detail

    detail.update({"acknowledged": True, "changed": False,
                   "note": "this review kind has no automatic promotion; it was closed as "
                           "confirmed and nothing structural was written"})
    return detail


def _review_action_cur(cur, review_id: int, action: str, args: dict, reason: str,
                       actor: str, context_entity: str = None) -> dict:
    cur.execute("SELECT kind, subject_key, payload, state FROM ace_entity_review "
                "WHERE review_id = %s FOR UPDATE", (int(review_id),))
    row = cur.fetchone()
    if not row:
        raise _Refused("No review row %s." % review_id)
    kind, subject, payload, state = row[0], row[1], row[2] or {}, row[3]
    if action == "confirm":
        if state != "open":
            # A TOMBSTONE IS PERMANENT. Re-opening a decision Brady already made is not
            # something an API call gets to do quietly; a fresh review row is the path.
            raise _Refused("That review was already %s. A closed decision is not reopened "
                           "here; nothing was written." % state)
        detail = _confirm_review_cur(cur, review_id, kind, payload, args or {}, reason,
                                     actor, context_entity)
        _close_review_cur(cur, review_id, "resolved", "confirmed: " + (reason or ""))
        detail["state"] = "resolved"
    else:
        # reject / dismiss. Idempotent: rejecting an already-closed row leaves the
        # tombstone exactly where it is and reports that, rather than erroring.
        if state == "open":
            _close_review_cur(cur, review_id, "dismissed", action + ": " + (reason or ""))
        detail = {"kind": kind, "state": "dismissed", "changed": state == "open",
                  "note": ("this is a permanent tombstone: a backfill replay will not "
                           "re-open it")}
    detail["review_id"] = int(review_id)
    detail["subject_key"] = subject
    return detail


def _apply_op_cur(cur, entity_id: str, op: str, args: dict, reason: str,
                  actor: str) -> dict:
    if op in ("confirm", "reject") and args.get("review_id") not in (None, ""):
        try:
            rid = int(args["review_id"])
        except Exception:
            raise _Refused("args.review_id must be a number.")
        return _review_action_cur(cur, rid, op, args, reason, actor,
                                  context_entity=entity_id)

    if op == "rename":
        name = " ".join(str(args.get("display_name") or args.get("name") or "").split())
        if not name or not entities.norm_alias(name):
            raise _Refused("rename needs args.display_name with a usable name in it.")
        cur.execute("UPDATE ace_entities SET display_name = %s, updated_at = now() "
                    "WHERE entity_id = %s", (name[:120], entity_id))
        # The old name STAYS an alias. A rename is a new label, not an erasure of the
        # word the original sources actually used.
        entities.add_alias_cur(cur, entity_id, name, kind="name", origin="manual",
                               confidence=1.0, review_status="confirmed")
        return {"display_name": name[:120], "old_alias_kept": True}

    if op == "add_alias":
        alias = " ".join(str(args.get("alias") or "").split())
        kind = str(args.get("kind") or "name").strip().lower()
        if kind not in ("name", "nickname", "handle", "email", "first_name"):
            kind = "name"
        if not alias or not entities.norm_alias(alias):
            raise _Refused("add_alias needs args.alias.")
        # A HUMAN-CONFIRMED first name is the one first name allowed to resolve; an
        # unconfirmed one is stored so the mention is seen, and resolves to nothing.
        ok = entities.add_alias_cur(cur, entity_id, alias, kind=kind, origin="manual",
                                    confidence=1.0, review_status="confirmed")
        if not ok:
            raise _Refused("That alias could not be stored.")
        return {"alias": alias, "kind": kind, "resolves": True}

    if op == "remove_alias":
        alias = " ".join(str(args.get("alias") or "").split())
        alias_id = args.get("alias_id")
        if alias_id not in (None, ""):
            cur.execute("SELECT alias, alias_norm FROM ace_entity_aliases "
                        "WHERE alias_id = %s AND entity_id = %s", (alias_id, entity_id))
            got = cur.fetchone()
            if not got:
                raise _Refused("No such alias on this record; nothing was changed.")
            alias, norm = got[0], got[1]
        elif alias:
            norm = entities.norm_alias(alias)
        else:
            raise _Refused("remove_alias needs args.alias or args.alias_id.")
        cur.execute("UPDATE ace_entity_aliases SET review_status = 'rejected' "
                    "WHERE entity_id = %s AND alias_norm = %s", (entity_id, norm))
        if not (cur.rowcount or 0):
            raise _Refused("No such alias on this record; nothing was changed.")
        # "THIS NAME IS NOT THIS PERSON" HAS TO MEAN SOMETHING. Flagging the alias alone
        # would leave every link that alias produced standing, so the mentions it matched
        # are retracted too — only the machine ones (`exact_alias`), never a link a human
        # made and never one matched on a different name. Retracted is a tombstone:
        # `entities.link_cur` refuses to re-create one, so a replay cannot undo this.
        #
        # MATCHED ON THE NORMALIZED FORM, NOT THE RAW TEXT (2026-09-22, review finding 1).
        # `evidence` is the surface span the SOURCE used — "Rebecca's", "O’Brien",
        # "Smith-Jones", "Rivera," — while the alias being removed is the stored one, and
        # `norm_alias` exists precisely because those are the same name. Comparing the raw
        # strings meant the commonest real cases (a possessive, a curly apostrophe, a
        # hyphen, a trailing comma) retracted NOTHING while the reply said the links were
        # gone: the correction looked applied, the source stayed in the dossier and in
        # `source_count`, and the tombstone acceptance scenario 7 depends on was never
        # written. The normalizer is not reachable from SQL, so the candidates are read
        # and compared here, and the count returned below is the count actually updated.
        cur.execute("SELECT link_id, evidence FROM ace_entity_links WHERE entity_id = %s "
                    "AND retracted_at IS NULL AND method = 'exact_alias'", (entity_id,))
        victims = [lid for lid, ev in cur.fetchall()
                   if ev and entities.norm_alias(ev) == norm]
        retracted = 0
        if victims:
            cur.execute("UPDATE ace_entity_links SET retracted_at = now(), "
                        "retracted_reason = %s WHERE link_id = ANY(%s) "
                        "AND retracted_at IS NULL",
                        (entities.clip("alias rejected: " + (reason or alias), 200),
                         victims))
            retracted = int(cur.rowcount or 0)
        # NOT a delete, in either table. The rows stay, flagged and stamped, because the
        # fact that this name was once attached is itself part of the record's history.
        return {"alias": alias or alias_id, "alias_norm": norm, "review_status": "rejected",
                "deleted": False, "links_retracted": retracted,
                "note": ("the alias row is kept and flagged; %s"
                         % ("%d link(s) it matched are retracted, not removed" % retracted
                            if retracted else
                            "no live machine link was matched on this name, so no link "
                            "was retracted"))}

    if op == "confirm":
        cur.execute("UPDATE ace_entities SET review_status = 'confirmed', "
                    "updated_at = now() WHERE entity_id = %s", (entity_id,))
        return {"review_status": "confirmed"}

    if op == "reject":
        cur.execute("UPDATE ace_entities SET review_status = 'rejected', "
                    "updated_at = now() WHERE entity_id = %s", (entity_id,))
        return {"review_status": "rejected", "deleted": False,
                "note": "the record and its links are kept; it is no longer drawn"}

    if op == "unlink":
        link_id = args.get("link_id")
        if link_id in (None, ""):
            raise _Refused("unlink needs args.link_id.")
        cur.execute("UPDATE ace_entity_links SET retracted_at = now(), "
                    "retracted_reason = %s WHERE link_id = %s AND entity_id = %s "
                    "AND retracted_at IS NULL",
                    (entities.clip(reason or "unlinked by Brady", 200), link_id,
                     entity_id))
        if not (cur.rowcount or 0):
            raise _Refused("No live link %s on this record; nothing was changed."
                           % link_id)
        return {"link_id": link_id, "retracted": True, "deleted": False,
                "note": "a retracted link is a tombstone; a backfill replay will not "
                        "re-create it"}

    if op == "link_item":
        item_id = str(args.get("item_id") or "").strip()
        sid = entities.source_id("item", item_id)
        if not (item_id and entities.valid_source_id(sid)):
            raise _Refused("link_item needs args.item_id — the board row's id.")
        cur.execute("SELECT id, text, ts FROM daybank_items WHERE id = %s", (item_id,))
        row = cur.fetchone()
        if not row:
            raise _Refused("No board item %s. Nothing was written, and the board was not "
                           "touched." % item_id)
        # Read-only against the board: the source row is indexed by id and hash, the
        # board row itself is never written to by this layer.
        entities.note_source_cur(cur, "item", item_id, row[1] or "", occurred_at=row[2],
                                 role="board", source_class="user_statement")
        link_id, made = entities.link_cur(cur, entity_id, sid, relation="about",
                                          method="manual", origin="manual", confidence=1.0,
                                          evidence=entities.clip(row[1] or "", 200),
                                          review_status="confirmed")
        if link_id is None and not made:
            raise _Refused("That link was retracted earlier and is not resurrected here.")
        return {"item_id": item_id, "source_id": sid, "link_id": link_id,
                "board_written": False}

    if op == "merge":
        return _merge_cur(cur, entity_id, str(args.get("into") or ""))

    if op == "unmerge":
        cur.execute("UPDATE ace_entities SET status = 'active', merged_into = NULL, "
                    "updated_at = now() WHERE entity_id = %s AND status = 'merged'",
                    (entity_id,))
        if not (cur.rowcount or 0):
            raise _Refused("%s is not merged into anything." % entity_id)
        return {"status": "active", "merged_into": None}

    if op == "set_fact_status":
        ef_id = args.get("ef_id")
        status = str(args.get("review_status") or "").strip().lower()
        if ef_id in (None, "") or status not in _STATUSES:
            raise _Refused("set_fact_status needs args.ef_id and args.review_status one "
                           "of %s." % ", ".join(_STATUSES))
        if status == "rejected":
            # Dated out of CURRENT, kept as history, and deliberately with NO
            # superseded_by: 294 archived facts in the live corpus have no successor and
            # inventing a replacement chain is exactly the fabrication to avoid.
            cur.execute("UPDATE ace_entity_facts SET review_status = 'rejected', "
                        "valid_to = COALESCE(valid_to, now()) WHERE ef_id = %s "
                        "AND entity_id = %s", (ef_id, entity_id))
        else:
            cur.execute("UPDATE ace_entity_facts SET review_status = %s WHERE ef_id = %s "
                        "AND entity_id = %s", (status, ef_id, entity_id))
        if not (cur.rowcount or 0):
            raise _Refused("No statement %s on this record; nothing was changed." % ef_id)
        return {"ef_id": ef_id, "review_status": status, "deleted": False}

    if op == "set_relation_status":
        rel_id = args.get("rel_id")
        status = str(args.get("review_status") or "").strip().lower()
        if rel_id in (None, "") or status not in _STATUSES:
            raise _Refused("set_relation_status needs args.rel_id and args.review_status "
                           "one of %s." % ", ".join(_STATUSES))
        if status == "rejected":
            cur.execute("UPDATE ace_entity_relations SET review_status = 'rejected', "
                        "retracted_at = COALESCE(retracted_at, now()) WHERE rel_id = %s "
                        "AND (from_entity_id = %s OR to_entity_id = %s)",
                        (rel_id, entity_id, entity_id))
        else:
            cur.execute("UPDATE ace_entity_relations SET review_status = %s "
                        "WHERE rel_id = %s AND (from_entity_id = %s OR to_entity_id = %s)",
                        (status, rel_id, entity_id, entity_id))
        if not (cur.rowcount or 0):
            raise _Refused("No relation %s on this record; nothing was changed." % rel_id)
        return {"rel_id": rel_id, "review_status": status, "deleted": False}

    raise _Refused("Unknown op %r. Nothing was written." % op)


def apply_correction(entity_id: str, op: str, args: dict = None, reason: str = "",
                     actor: str = "user") -> dict:
    """One governed correction. Audited, non-destructive, all-or-nothing.

    Returns `{"ok": True, "entity": {…}, "audit_id": N, "applied": {…}}`, or
    `{"ok": False, "error": "…", "status": <http code>}` — and on failure the transaction
    rolled back, so nothing at all was written.
    """
    op = str(op or "").strip().lower()
    args = dict(args or {})
    reason = str(reason or "").strip()
    actor = (str(actor or "user").strip() or "user")[:60]
    if op not in CORRECTION_OPS:
        return {"ok": False, "status": 400,
                "error": "Unknown op %r. Known ops: %s. Nothing was written."
                         % (op, ", ".join(CORRECTION_OPS))}
    if not entities.valid_entity_id(entity_id):
        return {"ok": False, "status": 400,
                "error": "That is not an entity id (expected per_/org_/prj_ + 12 hex)."}
    state = layer_state()
    if state != STATE_READY:
        return {"ok": False, "status": 503,
                "error": ("The memory index could not be reached, so nothing was "
                          "written and nothing is claimed about the record."
                          if state == STATE_UNAVAILABLE else
                          "The entity layer is not present, so there is nothing to "
                          "correct. Nothing was written.")}
    got = {}
    try:
        with db._conn() as c, c.cursor() as cur:
            _bound(cur, c)
            before = _entity_snapshot(cur, entity_id)
            if before is None:
                raise _Refused("No entity %s on file." % entity_id)
            applied = _apply_op_cur(cur, entity_id, op, args, reason, actor)
            after = _entity_snapshot(cur, entity_id)
            audit_id = entities.audit_cur(cur, actor, op, entity_id, dict(args or {}),
                                          before, {"entity": after, "applied": applied},
                                          reason)
            got = {"applied": applied, "audit_id": audit_id}
    except _Refused as r:
        return {"ok": False, "status": 400, "error": str(r)}
    except Exception as e:
        logger.warning("apply_correction %s failed: %s: %s", op, type(e).__name__, e)
        return {"ok": False, "status": 500,
                "error": "That correction did not go through, and nothing was written."}
    return {"ok": True, "entity": entities.get_entity(entity_id) or {},
            "audit_id": got.get("audit_id"), "applied": got.get("applied") or {},
            "notes": [entities.INDEX_NOTE]}


def apply_review_action(review_id, action: str, args: dict = None, reason: str = "",
                        actor: str = "user") -> dict:
    """Confirm / reject / dismiss ONE review row, through the same governed path.

    This is the only way a graph seed becomes an entity, and the audit row records who
    did it and why. Rejecting writes a permanent tombstone: `ace_entity_review` keeps the
    row with `state='dismissed'`, and `queue_review` re-inserts with ON CONFLICT DO
    NOTHING, so replaying the backfill can never re-open it.
    """
    action = str(action or "").strip().lower()
    args = dict(args or {})
    reason = str(reason or "").strip()
    actor = (str(actor or "user").strip() or "user")[:60]
    if action not in REVIEW_ACTIONS:
        return {"ok": False, "status": 400,
                "error": "Unknown action %r. Known actions: %s. Nothing was written."
                         % (action, ", ".join(REVIEW_ACTIONS))}
    try:
        rid = int(review_id)
    except Exception:
        return {"ok": False, "status": 400, "error": "review_id must be a number."}
    state = layer_state()
    if state != STATE_READY:
        return {"ok": False, "status": 503,
                "error": ("The memory index could not be reached, so nothing was "
                          "written and the queue was not read."
                          if state == STATE_UNAVAILABLE else
                          "The entity layer is not present, so there is no review "
                          "queue. Nothing was written.")}
    got = {}
    try:
        with db._conn() as c, c.cursor() as cur:
            _bound(cur, c)
            detail = _review_action_cur(cur, rid, action, args, reason, actor,
                                        context_entity=args.get("entity_id"))
            audit_id = entities.audit_cur(
                cur, actor, "review_" + action, detail.get("entity_id"),
                {"review_id": rid, **dict(args or {})}, {"state": "open"}, detail, reason)
            got = {"detail": detail, "audit_id": audit_id}
    except _Refused as r:
        return {"ok": False, "status": 400, "error": str(r)}
    except Exception as e:
        logger.warning("apply_review_action %s failed: %s: %s", action, type(e).__name__, e)
        return {"ok": False, "status": 500,
                "error": "That review decision did not go through, and nothing was "
                         "written."}
    detail = got.get("detail") or {}
    out = {"ok": True, "review_id": rid, "action": action,
           "state": detail.get("state"), "applied": detail,
           "audit_id": got.get("audit_id"), "notes": [entities.INDEX_NOTE]}
    eid = detail.get("entity_id")
    if eid:
        out["entity"] = entities.get_entity(eid) or {}
    return out
