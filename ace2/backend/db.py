"""
Ace 2.0's OWN database — a Railway Postgres compounding brain.

This is the "railway memory": Ace's conversation (turns) and his data bank
(commitments/deals/todos) live here instead of slow Google-Drive JSON files. It
makes Ace fast and fully self-hosted — his brain no longer depends on Google
Drive latency or on the Telegram bot.

DORMANT BY DEFAULT: everything is gated on DATABASE_URL. Unset → enabled() is
False and history.py / daybank.py transparently fall back to the Drive JSON
stores (the prior behavior), so this file is inert until Postgres is provisioned
and DATABASE_URL is referenced onto the service. On first use with DATABASE_URL
set, ensure_ready() creates the schema and BACKFILLS from the existing Drive
stores — so flipping it on loses nothing and continuity is seamless.

Sync + connect-per-op (wrapped by callers in asyncio.to_thread, same as the Drive
calls it replaces). Single user, internal Railway network → connect latency is a
few ms, far below Drive's. Every op is best-effort and never raises into a turn.
"""

import logging
import os
import uuid
from contextlib import contextmanager
from datetime import datetime

import pytz

logger = logging.getLogger("ace2.db")
EASTERN = pytz.timezone("America/New_York")

KINDS = ("note", "todo", "commitment", "followup")
_ready = False

# pg_trgm availability: None = not probed yet, True/False = known. Probed once per
# process in _init_search(); a managed Postgres that refuses CREATE EXTENSION just
# degrades search to full-text only instead of erroring.
_search_ready = False
_trgm_ok = None


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


@contextmanager
def _conn():
    import psycopg2  # imported lazily so the app boots even before the dep lands
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_schema():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS turns (
                id       BIGSERIAL PRIMARY KEY,
                ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
                source   TEXT,
                role     TEXT NOT NULL,
                content  TEXT NOT NULL
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daybank_items (
                id       TEXT PRIMARY KEY,
                ts       TIMESTAMPTZ NOT NULL,
                kind     TEXT,
                text     TEXT NOT NULL,
                status   TEXT NOT NULL DEFAULT 'open',
                tags     JSONB DEFAULT '[]'::jsonb,
                due      TEXT,
                done_ts  TIMESTAMPTZ
            )""")
        # Durable facts — Ace's real memory bank. Replaces the capped (60), bot-shared Drive
        # ace_memory.json. UNCAPPED (the old cap silently dropped facts). `tier` = core |
        # active | archived; a retired fact is set tier='archived' + invalid_at (kept as dated
        # history, never deleted — Brady's rule). bi-temporal fields support supersede-not-delete.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id            BIGSERIAL PRIMARY KEY,
                ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
                subject       TEXT,
                kind          TEXT,
                text          TEXT NOT NULL,
                tier          TEXT NOT NULL DEFAULT 'active',
                valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
                invalid_at    TIMESTAMPTZ,
                superseded_by BIGINT,
                source        TEXT DEFAULT 'ace2'
            )""")
        # Compaction: rolling "where we left off" recaps distilled from recent conversation, so
        # context COMPOUNDS and Ace never cold-reloads. One current recap is injected every turn.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id    BIGSERIAL PRIMARY KEY,
                ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
                kind  TEXT NOT NULL DEFAULT 'recap',
                text  TEXT NOT NULL
            )""")
        # Web-push subscriptions — how Ace reaches the PHONE when nothing is open. The push
        # service mints a unique endpoint URL per installed app, so the endpoint IS the device
        # identity: it's the primary key, and a re-subscribe upserts instead of leaving a twin.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS push_subs (
                endpoint TEXT PRIMARY KEY,
                p256dh   TEXT NOT NULL,
                auth     TEXT NOT NULL,
                ts       TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")


# ── Search infrastructure (hybrid recall: full-text + trigram) ───────────────────
# Expression GIN indexes over the text Ace already stores. to_tsvector with an
# explicit 'english' regconfig literal is IMMUTABLE, so it is indexable; the
# stemmer + stopword list is what turns keyword lookup into meaning lookup
# ("operators"→"oper" matches "operator", "retired"→"retir" matches "retires").
_FTS_DDL = (
    "CREATE INDEX IF NOT EXISTS facts_fts_idx ON facts "
    "USING gin (to_tsvector('english', text))",
    "CREATE INDEX IF NOT EXISTS turns_fts_idx ON turns "
    "USING gin (to_tsvector('english', content))",
    "CREATE INDEX IF NOT EXISTS daybank_items_fts_idx ON daybank_items "
    "USING gin (to_tsvector('english', text))",
)
# Trigram indexes power fuzzy/typo/partial-name matching (similarity / word_similarity).
_TRGM_DDL = (
    "CREATE INDEX IF NOT EXISTS facts_trgm_idx ON facts USING gin (text gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS turns_trgm_idx ON turns USING gin (content gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS daybank_items_trgm_idx ON daybank_items "
    "USING gin (text gin_trgm_ops)",
)


def _ddl_best_effort(stmt: str) -> bool:
    """Run one DDL statement in its OWN transaction. Own-transaction matters: a
    failed CREATE INDEX poisons the surrounding transaction, so batching these
    would make one failure kill the rest."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(stmt)
        return True
    except Exception as e:
        logger.warning("db search DDL skipped (%s): %s", stmt.split(" ON ")[0], e)
        return False


def _init_search():
    """Idempotent search setup: pg_trgm + GIN indexes. Never raises — if any part
    is unavailable (permissions, old server), search degrades instead of dying.
    Runs AT MOST ONCE per process, on its own latch: if ensure_ready() keeps failing
    (e.g. a Drive backfill hiccup) this must not re-run the DDL on every db call."""
    global _search_ready, _trgm_ok
    if _search_ready:
        return
    _search_ready = True
    for stmt in _FTS_DDL:
        _ddl_best_effort(stmt)
    # CREATE EXTENSION needs elevated rights on some managed Postgres. Probe rather
    # than assume: create it, then actually call the function to confirm it works.
    _ddl_best_effort("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT word_similarity('ace', 'ace bot')")
            cur.fetchone()
        _trgm_ok = True
    except Exception as e:
        _trgm_ok = False
        logger.warning("db: pg_trgm unavailable — hybrid search falls back to FTS only (%s)", e)
    if _trgm_ok:
        for stmt in _TRGM_DDL:
            _ddl_best_effort(stmt)


def ensure_ready():
    """Create schema + backfill from Drive once per process. Best-effort."""
    global _ready
    if _ready or not enabled():
        return
    try:
        _init_schema()
        _backfill()
        _ready = True
    except Exception as e:
        logger.error("db ensure_ready failed (%s) — falling back to Drive", e)
    # Search setup is independent of the core schema/backfill: run it even if the
    # backfill hiccuped, and never let it block _ready.
    try:
        _init_search()
    except Exception as e:
        logger.warning("db _init_search failed: %s", e)


def _backfill():
    """One-time: if a table is empty, populate it from the existing Drive store so
    flipping to Postgres carries all of Ace's memory forward."""
    from . import history, daybank  # lazy: avoid circular import at module load
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM turns")
        turns_empty = cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM daybank_items")
        bank_empty = cur.fetchone()[0] == 0

    if turns_empty:
        try:
            entries = history._drive_read_recent(6)  # read the raw Drive history
            rows = [
                (e.get("ts"), e.get("source", "ace2"), e.get("role"), (e.get("content") or "").strip())
                for e in entries
                if e.get("role") in ("user", "assistant") and (e.get("content") or "").strip()
            ]
            if rows:
                with _conn() as c, c.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO turns (ts, source, role, content) "
                        "VALUES (COALESCE(%s::timestamptz, now()), %s, %s, %s)", rows)
                logger.info("db backfill: %d turns from Drive", len(rows))
        except Exception as e:
            logger.warning("db backfill turns failed: %s", e)

    # facts: seed once from the Drive memory file (uncapped, all 'active'). Read Drive DIRECTLY
    # (brain._read_json), not brain.read_memory() which now dispatches back here.
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM facts")
        facts_empty = cur.fetchone()[0] == 0
    if facts_empty:
        try:
            from . import brain
            facts = brain._read_json(brain.MEMORY_FILE_NAME).get("memories", [])
            rows = [((f or "").strip(),) for f in facts if isinstance(f, str) and (f or "").strip()]
            if rows:
                with _conn() as c, c.cursor() as cur:
                    cur.executemany("INSERT INTO facts (text) VALUES (%s)", rows)
                logger.info("db backfill: %d facts from Drive", len(rows))
        except Exception as e:
            logger.warning("db backfill facts failed: %s", e)

    if bank_empty:
        try:
            items = daybank._drive_read_items(active_only=False)
            rows = [
                (it.get("id") or uuid.uuid4().hex[:8], it.get("ts"), it.get("kind", "note"),
                 (it.get("text") or "").strip(), it.get("status", "open"),
                 __import__("json").dumps(it.get("tags") or []), it.get("due"), it.get("done_ts"))
                for it in items if (it.get("text") or "").strip()
            ]
            if rows:
                with _conn() as c, c.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO daybank_items (id, ts, kind, text, status, tags, due, done_ts) "
                        "VALUES (%s, COALESCE(%s::timestamptz, now()), %s, %s, %s, %s::jsonb, %s, %s::timestamptz) "
                        "ON CONFLICT (id) DO NOTHING", rows)
                logger.info("db backfill: %d data-bank items from Drive", len(rows))
        except Exception as e:
            logger.warning("db backfill daybank failed: %s", e)


# ── Conversation turns (replaces history.py's Drive store) ───────────────────────
def append_turn(role: str, content: str, source: str = "ace2") -> bool:
    if not content or not content.strip():
        return False
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("INSERT INTO turns (source, role, content) VALUES (%s, %s, %s)",
                        (source, role, content))
        return True
    except Exception as e:
        logger.warning("db append_turn failed: %s", e)
        return False


def recent_turns(limit: int = 12) -> list:
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT ts, source, role, content FROM turns ORDER BY id DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
        rows.reverse()  # oldest-first
        return [{"ts": r[0].isoformat(), "source": r[1], "role": r[2], "content": r[3]} for r in rows]
    except Exception as e:
        logger.warning("db recent_turns failed: %s", e)
        return []


# ── Data bank (replaces daybank.py's Drive store; Ace's task/deal system) ────────
def read_items(active_only: bool = True) -> list:
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT id, ts, kind, text, status, tags, due, done_ts FROM daybank_items")
            rows = cur.fetchall()
        items = [{
            "id": r[0], "ts": r[1].isoformat(), "kind": r[2], "text": r[3], "status": r[4],
            "tags": r[5] or [], "due": r[6], "done_ts": r[7].isoformat() if r[7] else None,
        } for r in rows]
        if active_only:
            today = datetime.now(EASTERN).strftime("%Y-%m-%d")
            items = [it for it in items if it["status"] == "open"
                     or it["ts"][:10] == today or (it["done_ts"] or "")[:10] == today]
        items.sort(key=lambda it: it["ts"], reverse=True)
        return items
    except Exception as e:
        logger.warning("db read_items failed: %s", e)
        return []


_ITEM_STOP = {"the", "a", "an", "to", "for", "of", "and", "on", "in", "at", "with", "i", "my",
              "me", "he", "him", "his", "her", "we", "us", "get", "got", "need", "needs", "needto",
              "follow", "followup", "up", "w", "re", "this", "that", "is", "are", "be", "do", "by"}


def _norm_item(text: str):
    """Token set for near-duplicate detection: lowercased alnum words minus stopwords."""
    import re
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return frozenset(t for t in toks if len(t) > 1 and t not in _ITEM_STOP)


def add_item(kind: str, text: str, due: str = None, tags: list = None, dedup: bool = True) -> tuple:
    text = (text or "").strip()
    if not text:
        return False, "empty text"
    kind = (kind or "note").strip().lower()
    if kind not in KINDS:
        kind = "note"
    ensure_ready()
    # Dedup against OPEN *and* DONE items so nothing duplicates or resurrects (the Google Tasks
    # bug in reverse): a near-match returns the existing item flagged {"dup": True} instead of
    # inserting a twin. Data bank is small, so the full scan is cheap.
    if dedup:
        norm = _norm_item(text)
        if norm:
            for it in read_items(active_only=False):
                other = _norm_item(it.get("text", ""))
                if not other:
                    continue
                inter = len(norm & other)
                if other == norm or (inter >= 2 and inter / min(len(norm), len(other)) >= 0.8):
                    return True, {**it, "dup": True}
    item = {
        "id": uuid.uuid4().hex[:8], "ts": datetime.now(EASTERN).isoformat(),
        "kind": kind, "text": text, "status": "open", "tags": tags or [],
        "due": (due or None), "done_ts": None,
    }
    try:
        import json
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO daybank_items (id, ts, kind, text, status, tags, due, done_ts) "
                "VALUES (%s, %s::timestamptz, %s, %s, 'open', %s::jsonb, %s, NULL)",
                (item["id"], item["ts"], kind, text, json.dumps(item["tags"]), item["due"]))
        return True, item
    except Exception as e:
        logger.error("db add_item failed: %s", e)
        return False, str(e)


def set_item_tags(item_id: str, tags: list) -> bool:
    """Replace an item's tags (used to backfill/repair categories). True on success."""
    ensure_ready()
    try:
        import json
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE daybank_items SET tags = %s::jsonb WHERE id = %s",
                        (json.dumps(tags or []), item_id))
        return True
    except Exception as e:
        logger.warning("db set_item_tags failed: %s", e)
        return False


# ── Durable facts (replaces brain.py's capped, bot-shared Drive ace_memory.json) ─
def read_facts(include_archived: bool = False) -> list:
    """Current durable facts as plain strings, core tier first. Archived facts stay in the
    table as dated history but are excluded from context unless include_archived."""
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            if include_archived:
                cur.execute("SELECT text FROM facts ORDER BY (tier='core') DESC, id")
            else:
                cur.execute("SELECT text FROM facts WHERE tier <> 'archived' AND invalid_at IS NULL "
                            "ORDER BY (tier='core') DESC, id")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning("db read_facts failed: %s", e)
        return []


def read_facts_full() -> list:
    """Full rows (for the stale-fact review UI / archiving). [] on failure."""
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT id, text, tier, ts, invalid_at FROM facts ORDER BY (tier='core') DESC, id")
            return [{"id": r[0], "text": r[1], "tier": r[2],
                     "ts": r[3].isoformat() if r[3] else None,
                     "invalid_at": r[4].isoformat() if r[4] else None} for r in cur.fetchall()]
    except Exception as e:
        logger.warning("db read_facts_full failed: %s", e)
        return []


def add_fact(text: str, tier: str = "active", subject: str = None, kind: str = None,
             source: str = "ace2") -> bool:
    """Append a durable fact — UNCAPPED (the old 60-cap silently dropped facts). No-ops on an
    exact case-insensitive duplicate among live facts; smart reconcile/supersede comes later."""
    text = (text or "").strip()
    if not text:
        return False
    if tier not in ("core", "active", "archived"):
        tier = "active"
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1 FROM facts WHERE lower(text)=lower(%s) AND invalid_at IS NULL LIMIT 1", (text,))
            if cur.fetchone():
                return True
            cur.execute("INSERT INTO facts (subject, kind, text, tier, source) VALUES (%s,%s,%s,%s,%s)",
                        (subject, kind, text, tier, source))
        return True
    except Exception as e:
        logger.warning("db add_fact failed: %s", e)
        return False


def archive_fact(fact_id: int, superseded_by: int = None) -> bool:
    """Retire a fact WITHOUT deleting it — tier='archived', invalid_at=now() (Brady's rule:
    stale facts become searchable history, never gone)."""
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE facts SET tier='archived', invalid_at=now(), superseded_by=%s WHERE id=%s",
                        (superseded_by, fact_id))
        return True
    except Exception as e:
        logger.warning("db archive_fact failed: %s", e)
        return False


def set_fact_tier(fact_id: int, tier: str) -> bool:
    if tier not in ("core", "active", "archived"):
        return False
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            inv = "invalid_at = now()" if tier == "archived" else "invalid_at = NULL"
            cur.execute(f"UPDATE facts SET tier=%s, {inv} WHERE id=%s", (tier, fact_id))
        return True
    except Exception as e:
        logger.warning("db set_fact_tier failed: %s", e)
        return False


# ── Compaction recaps (the "where we left off" memory) ───────────────────────────
def latest_summary(kind: str = "recap") -> dict:
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT ts, text FROM summaries WHERE kind=%s ORDER BY id DESC LIMIT 1", (kind,))
            r = cur.fetchone()
        return {"ts": r[0].isoformat(), "text": r[1]} if r else {}
    except Exception as e:
        logger.warning("db latest_summary failed: %s", e)
        return {}


def add_summary(text: str, kind: str = "recap") -> bool:
    text = (text or "").strip()
    if not text:
        return False
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("INSERT INTO summaries (kind, text) VALUES (%s, %s)", (kind, text))
        return True
    except Exception as e:
        logger.warning("db add_summary failed: %s", e)
        return False


# ── Web-push subscriptions (Ace reaches the phone when the app is CLOSED) ────────
def add_push_sub(sub: dict) -> bool:
    """Store one browser PushSubscription — {endpoint, keys:{p256dh, auth}}. Upserts on
    endpoint, so re-subscribing the same phone refreshes its keys instead of leaving a
    dead twin that every future push has to time out against."""
    try:
        endpoint = ((sub or {}).get("endpoint") or "").strip()
        keys = (sub or {}).get("keys") or {}
        p256dh = (keys.get("p256dh") or "").strip()
        auth = (keys.get("auth") or "").strip()
    except Exception:
        return False
    if not (endpoint and p256dh and auth):
        return False
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO push_subs (endpoint, p256dh, auth) VALUES (%s, %s, %s) "
                "ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, "
                "auth = EXCLUDED.auth, ts = now()", (endpoint, p256dh, auth))
        return True
    except Exception as e:
        logger.warning("db add_push_sub failed: %s", e)
        return False


def list_push_subs() -> list:
    """Every stored device in pywebpush's own shape, oldest first. [] on any failure —
    no subscriptions simply means no phone alert, never a broken caller."""
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT endpoint, p256dh, auth FROM push_subs ORDER BY ts")
            return [{"endpoint": r[0], "keys": {"p256dh": r[1], "auth": r[2]}}
                    for r in cur.fetchall()]
    except Exception as e:
        logger.warning("db list_push_subs failed: %s", e)
        return []


def remove_push_sub(endpoint: str) -> bool:
    """Drop one device — on an explicit unsubscribe, or when the push service answers
    404/410 (app deleted / endpoint rotated) so the table self-heals."""
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return False
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM push_subs WHERE endpoint = %s", (endpoint,))
        return True
    except Exception as e:
        logger.warning("db remove_push_sub failed: %s", e)
        return False


def update_item(item_id: str, status: str = None, text: str = None,
                tags: list = None, due: str = None) -> tuple:
    """Edit a board item: status, text, tags (full replace), and/or due. due='' clears it."""
    item_id = (item_id or "").strip()
    if not item_id:
        return False, "no id"
    ensure_ready()
    try:
        import json
        with _conn() as c, c.cursor() as cur:
            sets, args = [], []
            if status in ("open", "done"):
                sets.append("status = %s"); args.append(status)
                sets.append("done_ts = %s")
                args.append(datetime.now(EASTERN).isoformat() if status == "done" else None)
            if text and text.strip():
                sets.append("text = %s"); args.append(text.strip())
            if tags is not None:
                sets.append("tags = %s::jsonb"); args.append(json.dumps(tags))
            if due is not None:
                sets.append("due = %s"); args.append(due.strip() or None)
            if not sets:
                return False, "nothing to update"
            args.append(item_id)
            cur.execute(f"UPDATE daybank_items SET {', '.join(sets)} WHERE id = %s RETURNING text", args)
            row = cur.fetchone()
        return (True, row[0]) if row else (False, f"no item {item_id}")
    except Exception as e:
        logger.error("db update_item failed: %s", e)
        return False, str(e)


# ── Hybrid search — recall by MEANING, not by literal keyword ────────────────────
# Two independent retrievers over the same three stores (facts, turns, board items):
#
#   1. FULL-TEXT (ts_rank_cd over to_tsvector('english', …)). The english config
#      stems and drops stopwords, so "retired buggy operators" finds "retires the
#      buggy operator". Terms are OR-ed (see _TSQ) so a partial/paraphrased query
#      still matches — ts_rank_cd then ranks by how many terms hit and how densely.
#   2. TRIGRAM (word_similarity from pg_trgm). Catches what the dictionary can't:
#      typos, nicknames, partial names, mashed-together words — "Vicks"/"Vick",
#      "buggie"/"buggy". word_similarity (not plain similarity) scores the BEST
#      matching window inside a long document, so a 4-word query still scores well
#      against a 400-word conversation turn.
#
# The two produce scores on incomparable scales, so they are fused with Reciprocal
# Rank Fusion (k=60) — rank-based, needs no normalization, and is the standard
# hybrid-retrieval merge. Each (source × retriever) is its own ranked list, so a
# strong hit in a small store isn't buried by a large one.
_RRF_K = 60
_TRGM_MIN = 0.30       # word_similarity floor — below this it's noise
_PER_LIST = 25         # rows pulled per (source × retriever) list before fusion
_TRGM_SCAN = 20000     # newest turns considered by the (unindexable-by-plan) trgm scan
_SEARCH_TIMEOUT_MS = 8000

# plainto_tsquery AND-s every term; we rewrite '&' to '|' so a paraphrase that only
# partially overlaps still returns rows (recall over precision — ranking sorts it out).
# Safe by construction: the text being rewritten is Postgres' own tsquery output, not
# user input, and plainto_tsquery emits no negation operators.
_TSQ = "replace(plainto_tsquery('english', %(q)s)::text, '&', '|')::tsquery"

_FTS_SQL = f"""
WITH q AS (SELECT {_TSQ} AS tsq)
(SELECT 'fact' AS source, f.id::text AS ref, f.text AS text, f.ts AS ts,
        COALESCE(f.tier, '') AS label,
        ts_rank_cd(to_tsvector('english', f.text), (SELECT tsq FROM q)) AS score
   FROM facts f
  WHERE to_tsvector('english', f.text) @@ (SELECT tsq FROM q)
  ORDER BY score DESC LIMIT %(n)s)
UNION ALL
(SELECT 'turn' AS source, t.id::text AS ref, t.content AS text, t.ts AS ts,
        COALESCE(t.role, '') AS label,
        ts_rank_cd(to_tsvector('english', t.content), (SELECT tsq FROM q)) AS score
   FROM turns t
  WHERE to_tsvector('english', t.content) @@ (SELECT tsq FROM q)
  ORDER BY score DESC LIMIT %(n)s)
UNION ALL
(SELECT 'item' AS source, d.id::text AS ref, d.text AS text, d.ts AS ts,
        COALESCE(d.status, '') AS label,
        ts_rank_cd(to_tsvector('english', d.text), (SELECT tsq FROM q)) AS score
   FROM daybank_items d
  WHERE to_tsvector('english', d.text) @@ (SELECT tsq FROM q)
  ORDER BY score DESC LIMIT %(n)s)
"""

# `HAS_CONTENT` gates trigram on the query containing at least one real lexeme. Without
# it a query of pure stopwords ("the a of") returns nothing from FTS but a pile of
# trigram noise — "the … of" is a character pattern that occurs everywhere. Empty
# tsquery ⇒ nothing meaningful was asked ⇒ return nothing and let recall() say so.
_HAS_CONTENT = "(SELECT tsq FROM q)::text <> ''"

_TRGM_SQL = f"""
WITH q AS (SELECT {_TSQ} AS tsq)
(SELECT 'fact' AS source, f.id::text AS ref, f.text AS text, f.ts AS ts,
        COALESCE(f.tier, '') AS label,
        word_similarity(%(q)s, f.text) AS score
   FROM facts f
  WHERE {_HAS_CONTENT} AND word_similarity(%(q)s, f.text) >= %(t)s
  ORDER BY score DESC LIMIT %(n)s)
UNION ALL
(SELECT 'turn' AS source, t.id::text AS ref, t.content AS text, t.ts AS ts,
        COALESCE(t.role, '') AS label,
        word_similarity(%(q)s, t.content) AS score
   FROM (SELECT id, ts, role, content FROM turns ORDER BY id DESC LIMIT %(scan)s) t
  WHERE {_HAS_CONTENT} AND word_similarity(%(q)s, t.content) >= %(t)s
  ORDER BY score DESC LIMIT %(n)s)
UNION ALL
(SELECT 'item' AS source, d.id::text AS ref, d.text AS text, d.ts AS ts,
        COALESCE(d.status, '') AS label,
        word_similarity(%(q)s, d.text) AS score
   FROM daybank_items d
  WHERE {_HAS_CONTENT} AND word_similarity(%(q)s, d.text) >= %(t)s
  ORDER BY score DESC LIMIT %(n)s)
"""


def _run_search(sql: str, args: dict) -> list:
    """Execute one retriever. Returns [] on any failure (never raises into recall)."""
    try:
        with _conn() as c, c.cursor() as cur:
            try:
                cur.execute("SET LOCAL statement_timeout = %s", (_SEARCH_TIMEOUT_MS,))
            except Exception:
                c.rollback()  # guard is a nicety; keep the connection usable without it
            cur.execute(sql, args)
            return cur.fetchall()
    except Exception as e:
        logger.warning("db search retriever failed: %s", e)
        return []


def hybrid_search(query: str, limit: int = 10) -> list:
    """Meaning-first search across facts, conversation turns, and the task board.

    Returns [{source: 'fact'|'turn'|'item', ref, text, ts, label, score, retrievers}]
    ranked best-first, where `score` is the fused RRF score. Best-effort: any
    retriever (or the whole thing) failing yields fewer rows / [], never an exception.
    """
    query = (query or "").strip()
    if not query or not enabled():
        return []
    # Total guard: recall() AND the /memory/search endpoint call this directly, so a
    # surprise here must degrade to "no hits", never a 500 or a broken turn.
    try:
        ensure_ready()
        n = max(1, min(int(limit or 10), 50))
        per_list = max(n, _PER_LIST)

        lists = []  # each: (retriever_name, [rows...]) already score-ordered by SQL
        fts = _run_search(_FTS_SQL, {"q": query, "n": per_list})
        if fts:
            lists.append(("fts", fts))
        if _trgm_ok is not False:
            trgm = _run_search(_TRGM_SQL, {"q": query, "n": per_list,
                                           "t": _TRGM_MIN, "scan": _TRGM_SCAN})
            if trgm:
                lists.append(("trgm", trgm))

        # RRF: rank WITHIN each (source × retriever) list, then sum 1/(k + rank).
        merged = {}
        for name, rows in lists:
            by_source = {}
            for r in rows:
                by_source.setdefault(r[0], []).append(r)
            for src_rows in by_source.values():
                src_rows.sort(key=lambda r: float(r[5] or 0.0), reverse=True)
                for rank, r in enumerate(src_rows, start=1):
                    source, ref, text, ts, label, raw = r
                    key = (source, ref)
                    hit = merged.get(key)
                    if hit is None:
                        hit = merged[key] = {
                            "source": source, "ref": ref, "text": text,
                            "ts": ts.isoformat() if ts else "", "label": label or "",
                            "score": 0.0, "raw": 0.0, "retrievers": [],
                        }
                    hit["score"] += 1.0 / (_RRF_K + rank)
                    hit["raw"] = max(hit["raw"], float(raw or 0.0))
                    if name not in hit["retrievers"]:
                        hit["retrievers"].append(name)

        out = sorted(merged.values(),
                     key=lambda h: (round(h["score"], 6), h["raw"], h["ts"]), reverse=True)
        for h in out:
            h["score"] = round(h["score"], 6)
            h["raw"] = round(h["raw"], 6)
        return out[:n]
    except Exception as e:
        logger.warning("db hybrid_search failed: %s", e)
        return []
