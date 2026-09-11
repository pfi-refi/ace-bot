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
import threading
import uuid
from contextlib import contextmanager
import re as _re
from datetime import datetime, timedelta

import pytz

logger = logging.getLogger("ace2.db")
EASTERN = pytz.timezone("America/New_York")

# 'approval' (2026-08-26): something ACE HAS PREPARED that needs Brady to say go before it
# executes -- a drafted email, a proposed reschedule, a payment to send. It surfaces in the
# TODAY card's "waiting on your OK" lane. The seed of the approvals tray.
KINDS = ("note", "todo", "commitment", "followup", "approval")
_ready = False

# pg_trgm availability: None = not probed yet, True/False = known. Probed once per
# process in _init_search(); a managed Postgres that refuses CREATE EXTENSION just
# degrades search to full-text only instead of erroring.
_search_ready = False
_trgm_ok = None


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


# ONE HANDSHAKE, NOT ONE PER CALL (2026-09-11, ultra review finding 10).
#
# This opened a brand-new psycopg2 connection — full TCP, TLS and auth to Railway Postgres —
# on EVERY call, and there are ~31 call sites across this module. Opening Brady's board cost
# 19–26 seconds because `derive_bucket` → `area_name` → `area_renames` lands on this path once
# per row: roughly a thousand handshakes to render 515 rows.
#
# I first fixed that by caching the two settings reads. The review took that apart correctly:
# a cache bought speed for two specific callers and paid for it with a staleness window, an
# invalidation ordering problem, a check-then-use race across `asyncio.to_thread` workers, and
# a read-modify-write in `rename_area` that could silently drop a custom list. None of that is
# worth having. A pool makes every caller cheap, present and future, and needs no TTL, no
# invalidation and no staleness at all — so the cache is gone and this is what replaced it.
_POOL = {"p": None}
_POOL_LOCK = threading.Lock()


def _pool():
    if _POOL["p"] is None:
        with _POOL_LOCK:
            if _POOL["p"] is None:
                import psycopg2.pool  # lazy, so the app boots before the dep lands
                _POOL["p"] = psycopg2.pool.ThreadedConnectionPool(
                    1, 10, os.environ["DATABASE_URL"], connect_timeout=10)
    return _POOL["p"]


@contextmanager
def _conn():
    pool = _pool()
    conn = pool.getconn()
    # A pooled connection can be dead on arrival — Railway restarts, idle timeouts, a network
    # blip. Returning it to the pool and asking for another is cheaper than failing a request,
    # and closed connections are discarded rather than recycled.
    if conn.closed:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    bad = False
    try:
        yield conn
        conn.commit()
    except Exception:
        bad = True
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        # A connection that raised may be in an unusable transaction state; drop it rather
        # than hand the next caller a poisoned one.
        pool.putconn(conn, close=bad or conn.closed)


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
        # Working-tree spine (2026-07-31, board-dedup review): the same supersede pattern the
        # facts table already has, so twins can be MERGED (loser: status='dropped' +
        # superseded_by=winner) and items can hang under a parent deal — never deleted.
        cur.execute("ALTER TABLE daybank_items ADD COLUMN IF NOT EXISTS parent_id TEXT")
        cur.execute("ALTER TABLE daybank_items ADD COLUMN IF NOT EXISTS superseded_by TEXT")
        # RECORDS vs ACTIONS (2026-09-05, Phase 4). The board held one row type, so a thing with a
        # STATE and a thing with an ENDING shared a shape — which is why completing "drop off her
        # packet" deleted Feliz the client. An ACTION has a natural end and leaves when done; a
        # RECORD has a state and is updated forever.
        #   entry      'action' | 'record'      — nullable; NULL reads as 'action' everywhere.
        #   state      records only: 'active' | 'waiting' | 'settled'.
        #   waiting_on free text — WHO it is parked on. A waiting row with no name is how
        #              "waiting on approval" quietly becomes "forgotten".
        #   closed_by  'brady' | 'ace' — never recorded before, so there was no way to tell who
        #              closed the four records that vanished in one update on 5 Sept.
        # All nullable with no backfill in this statement: an un-migrated row behaves exactly as
        # it does today, so this migration cannot change behavior on its own.
        # next_step (2026-09-08): the ONE move that advances this row, in Brady's words.
        # Additive and nullable — nothing is derived from prose into it, and no existing row
        # is rewritten. An empty next_step on an undated action is exactly what puts a row in
        # the "Needs a decision" lane, which is the signal Brady asked for rather than a
        # backlog of invented dates.
        # updated_at (2026-09-09): the brief's "since the last brief" window counted only
        # rows CREATED or COMPLETED, so moving a deadline, naming who you are waiting on, or
        # writing a next step — the cleanup Brady had just finished — counted as no change at
        # all and the brief told him nothing had moved. Nullable with no backfill: an
        # un-migrated row simply reads as "not edited since this shipped", which is true.
        for _col in ("entry TEXT", "state TEXT", "waiting_on TEXT", "closed_by TEXT",
                     "bucket TEXT", "next_step TEXT", "followup TEXT",
                     "chosen_on TEXT", "updated_at TIMESTAMPTZ",
                     "reviewed_at TIMESTAMPTZ"):
            cur.execute(f"ALTER TABLE daybank_items ADD COLUMN IF NOT EXISTS {_col}")
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
        # THE RELEASE-ONE BOUNDARY (Codex, 2026-09-09). "Carried over from the old board"
        # was being computed purely from a row's current fields, so a capture made five
        # seconds ago wore "carried over · not yet reviewed" — claiming a history it did not
        # have. The boundary is stamped once, the first time this code touches the database,
        # and never rewritten; a row created before it genuinely predates release one, and a
        # row created after it never can. Stored, not inferred.
        cur.execute("SELECT 1 FROM summaries WHERE kind = %s LIMIT 1",
                    ("board_review_boundary",))
        if not cur.fetchone():
            cur.execute("INSERT INTO summaries (kind, text) "
                        "VALUES (%s, to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SSOF'))",
                        ("board_review_boundary",))
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
            # IDEMPOTENT (2026-08-24): the early user-persist runs at the top of EVERY stream_turn,
            # and the voice path is explicitly re-entrant (ElevenLabs retries a turn whose deadline
            # slipped; main.py cancels the previous task). A bare INSERT would then write the same
            # sentence twice, and sanitize_for_api merges them into "X\n\nX" — Ace reading Brady
            # twice. Skip an identical role+content written in the last 3 minutes.
            cur.execute(
                "INSERT INTO turns (source, role, content) "
                "SELECT %s, %s, %s WHERE NOT EXISTS ("
                "  SELECT 1 FROM turns WHERE role = %s AND content = %s "
                "  AND ts > now() - interval '3 minutes')",
                (source, role, content, role, content))
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
# Canonical board categories. Tags are normalized AT THE STORE (the single choke point) so
# a sweep model shouting 'DEALS' can never mint a phantom column again — every reader
# (panel, context, graph) matches these case-sensitively.
# 2026-08-10 LIFE PIVOT: Brady stepped back from GFI/PFI to personal clients + a stable job
# while he digs out financially. New priority columns lead; the old business ones back-burner.
CATEGORIES = ("Money", "Bills", "Opportunities", "Goals", "Personal",
              "Deals", "Agents", "Admin", "Networking", "Business", "Tech")
_CANON_CAT = {c.lower(): c for c in CATEGORIES}
# Legacy category names → their current home, so old rows normalize on any write instead of
# lingering as dead tags (2026-08-26: "Job Hunt" became "Opportunities").
_CANON_CAT.update({"job hunt": "Opportunities"})


def _item_cat(it: dict):
    """The board column an item lives in (first canonical category tag), or None."""
    for t in (it.get("tags") or []):
        if t in CATEGORIES:
            return t
    return None


def canon_tags(tags: list) -> list:
    """Normalize category-ish tags to canonical case ('DEALS'→'Deals') and dedupe
    case-insensitively (order kept); non-category tags (e.g. 'migrated') pass through."""
    out, seen = [], set()
    for t in (tags or []):
        t = (t or "").strip()
        if not t:
            continue
        c = _CANON_CAT.get(t.lower(), t)
        if c.lower() in seen:
            continue
        seen.add(c.lower())
        out.append(c)
    return out


_DUE_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DUE_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# A day-of-month bill whose day passed THIS many days ago (or fewer) stays "current" (overdue)
# instead of rolling to next month — so a just-missed bill fires the reminder/overdue signals.
_DUE_GRACE_DAYS = 5


def _find_date(s: str, today):
    """Find the first date in a string.

    Shapes handled, in priority order: ISO ('2026-09-13'), slash ('9/4', 'Fri 9/4'),
    relative ('today', 'tomorrow', 'tonight'), weekday name ('Saturday' -> the NEXT one),
    month+day ('aug 17'), ordinal day ('the 18th').

    The first four were added 2026-08-31 after a live audit found 19 of 19 board items
    carrying a due string and only 4 resolving to a date — and those 4 matched on the item
    TEXT, not the due field. Every phrase Ace actually writes ('tomorrow', 'Wed 9/2',
    'Sat 8/29', ISO) fell through, so the DUE TODAY card was permanently empty. The card
    was fine; the parser was starving it.
    """
    import re
    from datetime import date as _date, timedelta as _td
    if not s:
        return None
    s = s.lower()

    m = re.search(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b", s)          # 2026-09-13
    if m:
        try:
            return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", s)     # 9/4, Fri 9/4, 8/29/26
    if m:
        mo, day = int(m.group(1)), int(m.group(2))
        yr = m.group(3)
        if 1 <= mo <= 12 and 1 <= day <= 31:
            if yr:
                yr = int(yr)
                yr += 2000 if yr < 100 else 0
            else:
                yr = today.year
            try:
                d = _date(yr, mo, day)
            except ValueError:
                return None
            if not m.group(3):
                # bare M/D: a date far in the past means next year, far ahead means last year
                if (today - d).days > 40:
                    d = _date(yr + 1, mo, day)
                elif (d - today).days > 320:
                    d = _date(yr - 1, mo, day)
            return d

    # AN EXPLICIT DATE ALWAYS BEATS A RELATIVE WORD (moved above the relative block
    # 2026-09-05). pin_due writes "Sep 6 (tomorrow)" to keep the row readable; if the
    # relative check ran first it would match "tomorrow" and the pinned date would drift
    # forward every single day — exactly the bug pinning exists to kill.
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", s)
    if m:
        try:
            d = _date(today.year, _DUE_MONTHS[m.group(1)], int(m.group(2)))
            if (today - d).days > 40:
                d = _date(today.year + 1, _DUE_MONTHS[m.group(1)], int(m.group(2)))
            elif (d - today).days > 320:
                # 'dec 28' read on Jan 2 parses as ~a year OUT; it means LAST year's date
                # (just missed / recent context), not 11+ months away (2026-08-23 scrub M4).
                d = _date(today.year - 1, _DUE_MONTHS[m.group(1)], int(m.group(2)))
            return d
        except ValueError:
            return None

    m = re.search(r"\bmid[-\s]?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", s)
    if m:                                # 'mid-September' -> the 15th of that month
        mo = _DUE_MONTHS[m.group(1)]
        yr = today.year + (1 if mo < today.month - 1 else 0)
        return _date(yr, mo, 15)
    if re.search(r"\bmid[-\s]?month\b", s):
        return _date(today.year, today.month, 15)

    if re.search(r"\b(today|tonight|this\s+(?:morning|afternoon|evening))\b", s):
        return today
    if re.search(r"\btomorrow\b", s):
        return today + _td(days=1)

    # Whole words only. A trailing [a-z]* here would read "monthly" as Monday and
    # "satisfied" as Saturday — and "monthly" is on half the bills.
    m = re.search(r"\b(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu"
                  r"|friday|fri|saturday|sat|sunday|sun)\b", s)
    if m:
        want = {"monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1,
                "wednesday": 2, "wed": 2, "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
                "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}[m.group(1)]
        ahead = (want - today.weekday()) % 7
        return today + _td(days=ahead or 7)   # a bare weekday name means the NEXT one

    m = re.search(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", s)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            y, mo = today.year, today.month
            # Month boundary (2026-08-23 scrub M4): a day near the END of the PREVIOUS month that
            # passed within the grace window (e.g. 'the 30th' seen on Sep 3) is still OVERDUE,
            # not next-month — the same-month grace below can't reach across months.
            if day > today.day:
                pm_y, pm_m = (y, mo - 1) if mo > 1 else (y - 1, 12)
                try:
                    prev = _date(pm_y, pm_m, day)
                except ValueError:
                    prev = None
                if prev and 0 < (today - prev).days <= _DUE_GRACE_DAYS:
                    return prev
            # Day-of-month recurs monthly. If the day already passed THIS month it's normally NEXT
            # month's — but keep a short grace window (2026-08-19 audit) so a JUST-missed bill reads
            # as OVERDUE (negative due_days) instead of ~a month out, so reminders/overdue fire.
            if day < today.day - _DUE_GRACE_DAYS:
                mo += 1
                if mo > 12:
                    mo, y = 1, y + 1
            # Roll forward to the next month that ACTUALLY HAS this day — short months have no
            # 30th/31st, and the old code returned None (a silently DATELESS bill) in that case.
            for _ in range(13):
                try:
                    return _date(y, mo, day)
                except ValueError:
                    mo += 1
                    if mo > 12:
                        mo, y = 1, y + 1
    return None


_RELATIVE_DUE = _re.compile(
    r"\b(today|tonight|tomorrow|tmrw|this (?:week|morning|afternoon|evening|weekend)"
    r"|next (?:week|month)|end of (?:the )?week|eow|later today)\b", _re.I)


def pin_due(due: str, today=None) -> str:
    """Freeze relative due wording to a real date AT WRITE TIME.

    "tomorrow" was stored verbatim and re-resolved against the CURRENT day on every read, so
    an item due "tomorrow" was due tomorrow FOREVER and could never go overdue. Proof from the
    live board 2026-09-05: three items carried due="tomorrow" and all three resolved to 09-06
    despite being written on 08-17, 08-30 and 09-03 — one of them nineteen days stale.

    Anything already absolute ("Sep 8", "the 17th", "Wed 9/9") is left exactly as typed; only
    wording that MOVES gets pinned, and the original is kept alongside so the row still reads
    like a person wrote it: "tomorrow" -> "Sep 6 (tomorrow)".

    Vague spans ("this week", "next week") pin to their END — the last day they could honestly
    mean — so they surface late rather than never.
    """
    raw = (due or "").strip()
    if not raw or not _RELATIVE_DUE.search(raw):
        return raw
    today = today or datetime.now(EASTERN).date()
    low = raw.lower()
    if _re.search(r"\bnext week\b", low):
        d = today + timedelta(days=(6 - today.weekday()) + 7)      # end of next week
    elif _re.search(r"\bnext month\b", low):
        d = (today.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    elif _re.search(r"\bthis week(end)?\b|\bend of (the )?week\b|\beow\b", low):
        d = today + timedelta(days=6 - today.weekday())            # this Sunday
    elif _re.search(r"\btomorrow\b|\btmrw\b", low):
        d = today + timedelta(days=1)
    else:                                                          # today / tonight / this morning...
        d = today
    stamp = "%s %d" % (_DUE_MONTH_NAMES[d.month - 1], d.day)
    return "%s (%s)" % (stamp, raw)


def parse_due(text: str, due: str = None, today=None):
    """Canonical DUE-DATE parser (2026-08-13) so the brief, watchdog, and the 'Due' lens all
    agree — no more the model guessing 'tomorrow' for the 18th. To avoid grabbing CONTEXTUAL
    dates ('Walter's statements Aug 6', 'Aug 4 after gym'), a date in the TEXT only counts when
    it directly follows a due/by/pay marker; the explicit `due` FIELD is always taken as-is."""
    import re
    if today is None:
        today = datetime.now(EASTERN).date()
    d = _find_date(due, today)          # the due FIELD is unambiguous — any date in it is the due
    if d:
        return d
    # A due FIELD that is SET but unparseable ("mid-September", "this week") still means Brady
    # named the due date there — do not go scavenging the text for a different one. On
    # 2026-09-01 the sewer bill read due="mid-September" with text "due mid-month, NOT 8/27
    # (that date was stale/wrong)", and the text scan pulled 8/27 out of the very sentence
    # disowning it, marking a current bill 5 days overdue.
    if (due or "").strip():
        return None
    t = (text or "").lower()            # in TEXT, only a date right after 'due' / 'by' / 'pay'
    for m in re.finditer(r"\b(?:due|by|pay(?:ment)?)\b", t):
        d = _find_date(t[m.end():m.end() + 22], today)
        if d:
            return d
    return None


def _done_et(ts) -> str:
    """A done_ts (stored UTC) as its EASTERN calendar date.

    Promoted out of read_items 2026-09-05: tools.py was doing a raw ts[:10], which is the
    exact bug the comment inside read_items warns about — anything closed 8pm-midnight ET
    reads as the next day. One definition, so the next caller cannot get it wrong.
    """
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).astimezone(EASTERN).strftime("%Y-%m-%d")
    except Exception:
        return str(ts)[:10]


# ── RECORDS vs ACTIONS — derivation (Phase 4, 2026-09-05) ──────────────────────────
# Deliberately BIASED TOWARD 'action', which is what every row is today. Calling a record an
# action just preserves current behavior; calling an action a record risks hiding real work,
# so a row is only promoted on a strong signal.
#
# The waiting signals below are lifted from Brady's ACTUAL open board — "everything submitted,
# waiting on approval", "just waiting, no push needed", "once signed back it can be issued",
# "tied up until after Sept 15", "no update expected until October". Seven open rows read like
# this, and they are precisely the ones that got closed in a single sweep on 5 Sept: a thing
# parked on somebody else has no natural end, so it is a RECORD with a state, never a to-do.
_WAIT_RE = _re.compile(
    r"\b(waiting\s+(?:on|for)|awaiting|still\s+pending|just\s+waiting|no\s+push"
    # NOT a bare "once he/she/it …": "set the meeting once he responds" is an ACTION that
    # merely mentions waiting, and promoting it to a record would HIDE the imperative next to
    # it ("send him the rollover materials"). Only a completed-by-someone-else clause counts.
    r"|once\s+(?:signed|approved|issued|processed)|tied\s+up\s+until|no\s+update\s+expected"
    r"|pending\s+(?:approval|signature|processing)|submitted,\s*waiting)\b", _re.I)
_WAIT_WHO = _re.compile(r"\bwaiting\s+(?:on|for)\s+([A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+)?"
                        r"|approval|signature|processing|[a-z]+(?:\s+[a-z]+){0,2})", _re.I)


def _waiting_on(text: str) -> str:
    m = _WAIT_WHO.search(text or "")
    return (m.group(1).strip().rstrip(".,;") if m else "")


# ── FIVE BUCKETS (Phase 4, 2026-09-05) — METADATA ONLY, NOT A RE-FILING ────────────
# Brady's framing: file an ACTION by WHOSE TIME IT TAKES, not what it is about — the haircut
# is Personal even though it is for Groundworks.
#
# ⚠ THIS DERIVATION IS A DEFAULT, NOT TRUTH, AND THE BOARD IS NOT REORGANISED BY IT. Measured
# against the 36 live open actions it got ~10% wrong, and the failures are structural, not
# tunable: "Ace Ready Mix" (a concrete supplier) read as Ace's own work, and the Morgan row —
# "how does the offer read against Groundworks + Damon by then" — read as Groundworks when it
# only COMPARES against it. Keyword matching cannot separate what a row is ABOUT from what it
# MENTIONS. Ship the field, keep the 11 categories doing their job, and let a judgment pass
# (the dedup judge pattern) or Brady correct these before anything is grouped by bucket.
# INBOX (release one, 2026-09-09). Previously there were five areas and `derive_bucket`
# defaulted anything it could not place to "Personal" — so unclassified captures quietly
# piled into a real area of Brady's life. Inbox is where genuinely unassigned work goes, and
# it is the DEFAULT for a row nothing else matches. Existing rows are untouched: a stored
# bucket always wins, and nothing is re-bucketed by this change.
INBOX = "Inbox"
BUCKETS = (INBOX, "GFI/PFI", "Groundworks", "Side Work", "Personal", "Ace")

# Custom lists Brady adds himself. Stored, not hardcoded, so a list survives a restart and
# renaming it keeps every item — the rows reference the area by NAME, so a rename is a
# single update and no row moves.
def custom_lists() -> list:
    """Extra areas Brady created. [] when the store is unavailable."""
    try:
        row = latest_summary("board_lists")
        import json as _json
        return [x for x in _json.loads(row.get("text") or "[]") if isinstance(x, str)]
    except Exception:
        return []


def review_boundary() -> str:
    """When release one first touched this database. '' when unknown.

    Written once by _init_schema and never rewritten. Everything created before it is work
    that existed under the old board; everything after it is not, whatever its fields say.
    """
    try:
        return (latest_summary("board_review_boundary").get("text") or "").strip()
    except Exception:
        return ""


def _before_boundary(ts, boundary: str) -> bool:
    """True when this row predates release one. Unknown boundary ⇒ False: a row is never
    called legacy on a guess, because that is the claim that has to be earned."""
    if not boundary or not ts:
        return False
    try:
        return datetime.fromisoformat(str(ts)) < datetime.fromisoformat(boundary)
    except Exception:
        return False


def area_renames() -> dict:
    """{canonical built-in name: what Brady calls it now}.

    The keyword filing in `derive_bucket` returns canonical names. Without this map, renaming
    Groundworks to "Concrete" would keep filing new concrete work into a "Groundworks" that no
    longer appears anywhere — the item would vanish from the board without being deleted.
    """
    try:
        row = latest_summary("board_list_renames")
        import json as _json
        got = _json.loads(row.get("text") or "{}")
        return {k: v for k, v in got.items() if k in BUCKETS and isinstance(v, str) and v}
    except Exception:
        return {}


def area_name(canonical: str) -> str:
    """The name Brady sees for a built-in slot."""
    return area_renames().get(canonical, canonical)


def all_areas() -> list:
    ren = area_renames()
    shown = [ren.get(b, b) for b in BUCKETS]
    return shown + [a for a in custom_lists() if a not in shown]


def _clean_list_names(names: list) -> list:
    """Dedupe case-insensitively, cap length and count, drop built-ins. Shared so the
    in-transaction rename and the ordinary save cannot apply different rules."""
    clean, seen = [], set()
    for n in (names or []):
        n = (n or "").strip()[:40]
        if n and n not in BUCKETS and n.lower() not in seen:
            seen.add(n.lower()); clean.append(n)
    return clean[:20]


def set_custom_lists(names: list) -> bool:
    import json as _json
    return add_summary(_json.dumps(_clean_list_names(names)), "board_lists")


def rename_area(old_name: str, new_name: str) -> tuple:
    """Rename a list. Every item and link follows it — the rows are updated in place, so
    ids, history, parents and dates are all preserved."""
    old_name = (old_name or "").strip(); new_name = (new_name or "").strip()[:40]
    if not old_name or not new_name:
        return False, "both names are required"
    if new_name in all_areas() and new_name != old_name:
        return False, f"'{new_name}' already exists"
    ren = area_renames()
    lists = custom_lists()
    # A built-in is a SLOT, not a label: renaming it records what he calls it now and leaves
    # the slot itself in place, so keyword filing keeps landing in the same area.
    canonical = next((b for b in BUCKETS if ren.get(b, b) == old_name), None)
    if canonical is None and old_name not in lists:
        return False, f"no list named '{old_name}'"
    ensure_ready()
    # ONE TRANSACTION, OR NEITHER (Codex, 2026-09-09). The rows were moved and committed
    # first, and the list metadata was written afterwards through add_summary — a separate
    # connection whose result was not even read. If that second write failed, every item had
    # already moved to a name the navigation and the keyword filing still did not know, and
    # the caller was told "renamed". Both writes now share a cursor, so the commit at the end
    # of the `with` block is the only thing that makes either of them real.
    import json as _json
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE daybank_items SET bucket = %s WHERE bucket = %s",
                        (new_name, old_name))
            moved = cur.rowcount or 0
            if canonical is not None:
                ren[canonical] = new_name
                payload = _json.dumps({k: v for k, v in ren.items() if v != k})
                kind = "board_list_renames"
            else:
                payload = _json.dumps(_clean_list_names(
                    [new_name if x == old_name else x for x in lists]))
                kind = "board_lists"
            cur.execute("INSERT INTO summaries (kind, text) VALUES (%s, %s)", (kind, payload))
    except Exception as e:
        logger.error("rename_area rolled back: %s", e)
        return False, f"rename failed, nothing was changed: {e}"
    return True, f"renamed; {moved} item(s) stayed with it"


# Personal time wins first: an errand is his own hours whoever the occasion belongs to.
# "birthday" was here and cost the Morgan row: "sitting on it until his birthday, Oct 21" is a
# DATE, not an errand. Only unambiguous errand nouns survive.
_B_ERRAND = _re.compile(r"\b(dress|haircut|gift\s+for|deed|doctor|dentist|groceries)\b", _re.I)
_B_ACE = _re.compile(r"\b(?!ace\s+ready\s*mix)(ace\b|ace's|defect|upgrade\s+session"
                     r"|railway|deploy-ready)\b", _re.I)
_B_PFI = _re.compile(r"\b(pfi|gfi|iul|annuity|carrier|paramed|rollover|fta|zenbusiness"
                     r"|commission|chris\s+stout|the\s+offer|prospect)\b", _re.I)
_B_GROUNDWORKS = _re.compile(r"\b(groundworks|tony\b|inspector)\b", _re.I)
_B_SIDE = _re.compile(r"\b(damon|woody|concrete|pour(?:s|ing)?|ready\s*mix|greenhouse|uncle"
                      r"|gantz|dns|site\s+build)\b", _re.I)


def derive_bucket(text: str, cat: str) -> str:
    """The area to SHOW for a row that has none stored. Legacy display, unchanged.

    This deliberately still falls back to Personal. Changing it to Inbox looked right in
    isolation and was wrong in practice: 28 of Brady's 63 open rows have no stored area and
    have been sitting under Personal for weeks, so the new fallback would have relocated a
    third of his board the moment this deployed — with no migration, no preview and no
    approval, which is the one thing he asked not to happen. New capture lands in Inbox via
    `derive_bucket_for_capture`; these rows move only when he says so.
    """
    return area_name(_derive_bucket_slot(text, cat) or "Personal")


def derive_bucket_for_capture(text: str, cat: str) -> str:
    """Where NEW work files itself. Unrecognised means nobody has decided yet — which is
    what Inbox is for — rather than Personal quietly absorbing it."""
    return area_name(_derive_bucket_slot(text, cat) or INBOX)


def unfiled(item: dict) -> bool:
    """True when a row's area is only a fallback: nothing stored, and nothing in the wording
    files it either. These are the genuinely unassigned rows the Inbox proposal is about.

    RECORDS ARE NOT UNFILED WORK. Run against the real board this matched 29 rows, and most
    were the bill register and Brady's goals — things that live on a shelf, are release-two
    surfaces, and would have been swept into Inbox as though nobody had placed them. Inbox is
    for captured WORK nobody has filed; a record already has a home.
    """
    if item.get("bucket_set"):
        return False
    if (item.get("entry") or "") == "record":
        return False
    return not _derive_bucket_slot(item.get("text") or "", _item_cat(item))


def _derive_bucket_slot(text: str, cat: str) -> str:
    """The canonical slot, before Brady's own naming is applied."""
    t = text or ""
    if _B_ERRAND.search(t):                      # his own hours, whoever it is for
        return "Personal"
    if cat == "Tech" or (_B_ACE.search(t) and not _re.search(r"ace\s+ready\s*mix", t, _re.I)):
        return "Ace"
    if _B_PFI.search(t) or cat in ("Deals", "Agents", "Networking"):
        return "GFI/PFI"
    if _B_GROUNDWORKS.search(t):
        return "Groundworks"
    if _B_SIDE.search(t):
        return "Side Work"
    return ""   # nothing matched; the caller decides what an unrecognised row means


def _derive_entry(it) -> str:
    """'record' only on a strong signal; everything else stays an action."""
    tags = it.get("tags") or []
    if "Goals" in tags:                       # a goal has a state, never an ending
        return "record"
    if it.get("kind") == "note":              # notes were already records in all but name
        return "record"
    if _is_recurring_bill_row(it):            # the register — updated forever, never completed
        return "record"
    _t = it.get("text") or ""
    if len(_t) <= _WAIT_MAX_CHARS and _WAIT_RE.search(_t):   # parked = a state, not a to-do
        return "record"
    return "action"


# A STATUS IS STATED BRIEFLY; A REPORT QUOTES ONE (2026-09-06). The Friday defects row —
# thousands of characters that QUOTE "submitted, waiting on approval", "just waiting, no push"
# while describing the missing WAITING state — was itself derived as a waiting record, which
# parked a work item and protected it from ever closing. Every genuine waiting row on the live
# board is under 150 characters ("Thiami — everything submitted, waiting on approval"); nothing
# that long is stating its own status. Same failure this heuristic keeps hitting elsewhere:
# what a row is ABOUT is not what it MENTIONS.
_WAIT_MAX_CHARS = 300


def _derive_state(it) -> str:
    t = it.get("text") or ""
    if len(t) <= _WAIT_MAX_CHARS and _WAIT_RE.search(t):
        return "waiting"
    return "active"


def _is_recurring_bill_row(it) -> bool:
    """Module-level twin of read_items' _is_recurring_bill, so the derivation can use it too.
    A real obligation names an amount AND a repeating due day; a one-off chore filed under
    Bills ('Call the gas company') is an action and must still disappear when done."""
    if "Bills" not in (it.get("tags") or []):
        return False
    t = it.get("text") or ""
    if "$" not in t:
        return False
    return bool(_re.search(r"due\s+(the\s+)?\d{1,2}(st|nd|rd|th)\b", t, _re.I)
                or _re.search(r"/\s*mo(nth)?\b", t, _re.I)
                or _re.search(r"\bmonthly\b", t, _re.I))


def read_items(active_only: bool = True) -> list:
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT id, ts, kind, text, status, tags, due, done_ts, "
                        "parent_id, superseded_by, entry, state, waiting_on, closed_by, "
                        "bucket, next_step, followup, chosen_on, updated_at, reviewed_at "
                        "FROM daybank_items")
            rows = cur.fetchall()
        _today = datetime.now(EASTERN).date()
        _boundary = review_boundary()
        items = []
        for r in rows:
            it = {
                "id": r[0], "ts": r[1].isoformat(), "kind": r[2], "text": r[3], "status": r[4],
                "tags": r[5] or [], "due": r[6], "done_ts": r[7].isoformat() if r[7] else None,
                "parent_id": r[8], "superseded_by": r[9],
                "closed_by": r[13],
            }
            # A stored classification always wins; otherwise DERIVE, so the model works before any
            # backfill and stays right for rows written by paths that don't set it yet.
            it["entry"] = r[10] or _derive_entry(it)
            it["state"] = r[11] or (_derive_state(it) if it["entry"] == "record" else None)
            it["waiting_on"] = r[12] or (_waiting_on(it.get("text")) if it["state"] == "waiting" else None)
            # A STORED bucket always wins: it is either the judgment pass or Brady's own
            # correction, and the keyword default exists only so an unclassified row still
            # lands somewhere sane.
            # A STORED bucket wins for EVERY row (2026-09-08). The old expression read
            # `... if entry == "action" else None`, which threw a stored bucket away on a
            # record — so Brady could set the area on a record and it silently reverted,
            # exactly what the comment above promises cannot happen. Derivation is still
            # limited to actions here, so Ace's context is unchanged; the presentation-layer
            # fallback in classify.area_of covers records without touching stored data.
            it["bucket"] = r[14] or (derive_bucket(it.get("text"), _item_cat(it))
                                     if it["entry"] == "action" else None)
            # Callers need to tell a JUDGED/corrected bucket from the keyword default — the
            # classification pass fills only empties, so it must never overwrite Brady.
            it["bucket_set"] = bool(r[14])
            # Recorded by Brady or by an explicit edit only — never inferred from the text.
            it["next_step"] = r[15]
            it["followup"] = r[16]
            # CHOSEN FOR TODAY (2026-09-08) — the date Brady picked this up, which is NOT a
            # deadline. Accepting one of Ace's suggestions must never invent a due date; the
            # obligation's own timing is `due` and belongs to the world, this belongs to him.
            it["chosen_on"] = r[17]
            # When this row was last EDITED, as opposed to created or completed. The brief's
            # change window reads it so a cleanup pass is not invisible.
            it["updated_at"] = r[18].isoformat() if r[18] else None
            # WHEN BRADY LOOKED AT IT. Durable, and the only thing that retires the
            # carried-over review flag — no fabricated date or next step required.
            it["reviewed_at"] = r[19].isoformat() if r[19] else None
            # Did this row exist before release one shipped? A stored boundary answers it, so
            # a fresh capture can never inherit the old board's history.
            it["pre_release_one"] = _before_boundary(it.get("ts"), _boundary)
            # Deterministic due date (computed once here so brief / watchdog / UI all agree).
            _d = parse_due(it["text"], it["due"], _today)
            it["due_on"] = _d.isoformat() if _d else None
            it["due_days"] = (_d - _today).days if _d else None
            items.append(it)
        if active_only:
            # Active view = open items + anything CLOSED today (visible receipt, gone tomorrow).
            # 'dropped' (archived twins/mistakes) behaves exactly like done here.
            # ⚠ done_ts is stored UTC; slicing its raw [:10] compared a UTC date to an Eastern one,
            # so anything closed 8pm–midnight ET (UTC = tomorrow) VANISHED from the view — the
            # "Ace doesn't mark some items off" bug (2026-08-19 audit). Convert to Eastern first.
            today = datetime.now(EASTERN).strftime("%Y-%m-%d")
            # BILLS STAY ON THE SHELF ALL MONTH (2026-08-26, Brady: "that should be an untouched
            # board... just say when they're actually due and how much"). A bill marked paid used
            # to VANISH until the 1st, so mid-month his register was incomplete — 11 of ~16 bills
            # were invisible and Ace couldn't state his real monthly obligations. Now a Bills item
            # paid THIS MONTH stays visible, flagged paid_this_period, and rollover_recurring_bills
            # still reopens it for the new month. Complete register, and nothing reads as overdue
            # when he's already paid it.
            ym = datetime.now(EASTERN).strftime("%Y-%m")

            def _is_recurring_bill(it) -> bool:
                """A RECURRING OBLIGATION, not a one-off task that happens to sit in Bills.
                The Bills column also holds actions ('Call the gas company', 'Check the sewer
                application') and those SHOULD disappear when done. A real bill names an amount
                AND a repeating due day ('$88.40 — due 18th', '$50/mo'). Requiring both keeps
                completed chores and old test rows off the shelf."""
                import re as _re
                if "Bills" not in (it.get("tags") or []):
                    return False
                t = (it.get("text") or "")
                if "$" not in t:
                    return False
                return bool(_re.search(r"due\s+(the\s+)?\d{1,2}(st|nd|rd|th)\b", t, _re.I)
                            or _re.search(r"/\s*mo(nth)?\b", t, _re.I)
                            or _re.search(r"\bmonthly\b", t, _re.I))

            def _keep(it):
                if it["status"] == "open":
                    return True
                d = _done_et(it["done_ts"])
                if d == today:
                    return True
                # only DONE (never 'dropped' — those are archived mistakes) recurring bills
                return (it["status"] == "done" and _is_recurring_bill(it) and d[:7] == ym)
            items = [it for it in items if _keep(it)]
            for it in items:
                if (it["status"] == "done" and _is_recurring_bill(it)
                        and _done_et(it["done_ts"])[:7] == ym):
                    it["paid_this_period"] = True
        items.sort(key=lambda it: it["ts"], reverse=True)
        return items
    except Exception as e:
        logger.warning("db read_items failed: %s", e)
        return []


_ITEM_STOP = {"the", "a", "an", "to", "for", "of", "and", "on", "in", "at", "with", "i", "my",
              "me", "he", "him", "his", "her", "we", "us", "get", "got", "need", "needs", "needto",
              "follow", "followup", "up", "w", "re", "this", "that", "is", "are", "be", "do", "by",
              # 2026-07-31 board-dedup review: connective words that made paraphrases look new
              "about", "back", "again", "later", "also", "just", "still", "touch", "base",
              "circle", "then", "than", "will", "would", "should", "them", "they", "their"}


def _stem(t: str) -> str:
    """Crude suffix strip so 'rescheduling'/'rescheduled' and 'needs'/'need' collide —
    dup detection only, both sides get the same treatment so collisions are consistent."""
    if len(t) > 5 and t.endswith("ing"):
        return t[:-3]
    if len(t) > 4 and (t.endswith("ed") or t.endswith("es")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s"):
        return t[:-1]
    return t


def _norm_item(text: str):
    """Token set for near-duplicate detection: lowercased alnum words minus stopwords."""
    import re
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return frozenset(_stem(t) for t in toks if len(t) > 1 and t not in _ITEM_STOP)


def find_items(query: str, status: str = "open") -> list:
    """Fuzzy-find board items by text (for update-by-meaning: 'mark off the Kara follow-up').
    Returns a SINGLE item when one match is clearly confident, else up to 4 candidates so the
    caller can ask which. [] when nothing plausible."""
    import difflib
    query = (query or "").strip()
    if not query:
        return []
    qn = _norm_item(query)
    scored = []
    for it in read_items(active_only=False):
        if status and it.get("status") != status:
            continue
        on = _norm_item(it.get("text", ""))
        if not on:
            continue
        inter = len(qn & on)
        r1 = inter / min(len(qn), len(on)) if qn and on else 0.0
        r2 = difflib.SequenceMatcher(None, query.lower(), (it.get("text") or "").lower()).ratio()
        score = max(r1, r2)
        if score >= 0.5 and (inter >= 1 or r2 >= 0.6):
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] >= 0.75 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.2):
        return [scored[0][1]]
    return [it for _s, it in scored[:4]]


# ── SEMANTIC DEDUP (Phase 4, 2026-09-05) ───────────────────────────────────────────
# WHY A MODEL AND NOT A THRESHOLD. Brady's live duplicate — "Sienna's aunt — signature packet
# still pending, just waiting, no push needed" vs "Sienna's aunt — signature packet sent to
# her, once signed back it can be issued" — scores 0.267 on token overlap, because the two
# rows say the same thing in almost no shared words. The pairs that must NEVER merge (truck
# payment vs truck arrears, Mission Lane minimum vs payoff) score 0.077 and 0.143. There is no
# cutoff that separates 0.267 from 0.143, so no amount of tuning fixes this class. Judgment does.
#
# The judge is INJECTED (chat.py registers a Haiku-backed one at startup) so this module keeps
# no model dependency and stays unit-testable. Unset ⇒ byte-identical behavior to before.
_DUP_JUDGE = None


def set_dup_judge(fn) -> None:
    """fn(new_text, [candidate items]) -> matching item id, or '' — see chat.py."""
    global _DUP_JUDGE
    _DUP_JUDGE = fn


_MONEY_RE = _re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_DAYNUM_RE = _re.compile(r"\bdue\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b", _re.I)


def _distinguishing(a: str, b: str) -> bool:
    """True when two texts CONTRADICT on money or due-day and must never be merged.

    Runs BEFORE the model, deliberately: this is the rule that protects the truck payment from
    its arrears and a card's minimum from its payoff, and it is the expensive mistake to get
    wrong, so it stays deterministic and testable rather than delegated. Elaboration is not
    contradiction — one text carrying an amount the other simply omits is fine.
    """
    am, bm = set(_MONEY_RE.findall(a or "")), set(_MONEY_RE.findall(b or ""))
    if am and bm and am != bm:
        return True
    ad, bd = set(_DAYNUM_RE.findall(a or "")), set(_DAYNUM_RE.findall(b or ""))
    if ad and bd and ad != bd:
        return True
    return False


def _dup_candidates(text: str, norm: set, items: list, limit: int = 4) -> list:
    """Cheap, high-recall shortlist for the judge: open rows sharing >=2 meaningful tokens,
    minus anything the money/date guard says is a different thing. Precision is the judge's
    job; this only has to avoid sending it nonsense."""
    out = []
    for it in items:
        if it.get("status") != "open":
            continue
        other = _norm_item(it.get("text", ""))
        shared = norm & other
        if len(shared) < 2:
            continue
        if _distinguishing(text, it.get("text", "")):
            continue
        out.append((len(shared), it))
    out.sort(key=lambda p: -p[0])
    return [it for _, it in out[:limit]]


def _repeat_result(existing, text, due, parent_id=None):
    """Only verbatim repeats are silent no-ops. Preserve new details for review."""
    same_text = " ".join(text.casefold().split()) == " ".join(existing.get("text", "").casefold().split())
    same_due = not due or pin_due(due) == pin_due(existing.get("due"))
    same_parent = (parent_id or None) == (existing.get("parent_id") or None)
    if same_text and same_due and same_parent:
        return True, {**existing, "dup": True}
    # Changed detail on a near-identical row is NOT a failure and NOT a silent merge —
    # it is an unresolved question with exactly two answers, and the caller needs the
    # existing ID to act on either. Structured, not prose, so the renderer can say what
    # actually happened rather than the reader guessing from a string (2026-09-08).
    return False, {
        "needs_review": True,
        "existing_id": existing["id"],
        "existing_text": existing.get("text", ""),
        "existing_status": existing.get("status", "open"),
        "existing_due": existing.get("due"),
        "requested_text": text,
        "requested_due": due,
    }


def _dedup_eligible(existing, text, due, parent_id):
    if existing.get("superseded_by") or existing.get("status") == "dropped":
        return False
    if _distinguishing(text, existing.get("text", "")):
        return False
    if (parent_id or None) != (existing.get("parent_id") or None):
        return False
    # A new explicitly dated occurrence is distinct from an earlier completed one.
    if existing.get("status") == "done" and due and pin_due(due) != pin_due(existing.get("due")):
        return False
    return True


def add_item(kind: str, text: str, due: str = None, tags: list = None, dedup: bool = True,
             parent_id: str = None, bucket: str = None) -> tuple:
    text = (text or "").strip()
    if not text:
        return False, "empty text"
    kind = (kind or "note").strip().lower()
    if kind not in KINDS:
        kind = "note"
    ensure_ready()
    tags = canon_tags(tags)
    # Dedup against OPEN *and* DONE items so nothing duplicates or resurrects (the Google Tasks
    # bug in reverse): a near-match returns the existing item flagged {"dup": True} instead of
    # inserting a twin. Hardened 2026-07-31 (Kara×2 / PFI-Hub×3 / Donna×2 leaked through the old
    # 0.8-of-min gate): stemmed tokens, a looser band for longer items, plus a pg_trgm second
    # opinion. A near-miss no longer vanishes silently — it rides back as "similar" so the
    # caller (Ace) can choose update-over-twin. Data bank is small, so the full scan is cheap.
    # CATEGORY-AWARE DEDUP (2026-08-11 review fix): dedup used to scan EVERY column, so a Bills
    # entry worded like a Money task got swallowed and never inserted (silent data loss). Now a
    # cross-column strong match is NOT a hard dup — it rides back as "similar" and still inserts;
    # only a same-column match (or when the new item has no category) collapses to a true dup.
    in_cat = next((t for t in (tags or []) if t in CATEGORIES), None)

    def _same_col(it) -> bool:
        if not in_cat:
            return True   # uncategorized capture: preserve old cross-board dedup
        return _item_cat(it) == in_cat

    similar = None
    if dedup:
        norm = _norm_item(text)
        if norm:
            # Track the best SAME-COLUMN match and the best cross-column match separately, so a
            # same-column twin always wins the dup decision even when a different column also
            # scores high (else the newer cross-column item could shadow a real same-column dup).
            best, best_score = None, 0.0           # best cross-column (different column) match
            best_sc, best_sc_score = None, 0.0     # best SAME-column match
            for it in read_items(active_only=False):
                if not _dedup_eligible(it, text, due, parent_id):
                    continue
                other = _norm_item(it.get("text", ""))
                if not other:
                    continue
                inter = len(norm & other)
                union = len(norm | other) or 1
                # SIZE GUARD (2026-08-31). The two containment rules below divide by the
                # SHORTER token set, which turns every short item into a magnet: a 252-token
                # capture was refused as a duplicate of "5 deals a month" because it contained
                # both of that item's two tokens (score 0.90, Jaccard 0.008). Six more short
                # items swallowed it the same way. Net effect: Ace writes a long, detailed item,
                # add_item answers dup:True, NOTHING is stored, and the caller reports success —
                # silent data loss, the exact failure this dedup exists to prevent.
                # Containment only means something when the two are comparably sized; otherwise
                # fall through to Jaccard, which charges for everything the longer item does not
                # share. 0.5 keeps the real paraphrase case ("Book Donna's strategy session" vs
                # "Donna — … needs a strategy session", ratio 0.8) well inside the gate.
                ratio = min(len(norm), len(other)) / max(len(norm), len(other))
                if other == norm:
                    score = 1.0
                elif ratio >= 0.5 and inter >= 2 and inter / min(len(norm), len(other)) >= 0.8:
                    score = 0.9
                elif ratio >= 0.5 and inter >= 3 and inter / min(len(norm), len(other)) >= 0.6:
                    score = 0.85   # longer paraphrase: 'Book Donna's strategy session' vs
                                   # 'Donna — … needs a strategy session'
                elif inter / union >= 0.6:
                    score = 0.85
                else:
                    score = inter / union
                if _same_col(it):
                    if score > best_sc_score:
                        best_sc_score, best_sc = score, it
                elif score > best_score:
                    best_score, best = score, it
            if best_sc and best_sc_score >= 0.85:
                return _repeat_result(best_sc, text, due, parent_id)          # true same-column twin
            # NEAR-IDENTICAL TEXT IS A TWIN, WHATEVER COLUMN IT LANDS IN (2026-08-26). The
            # cross-column allowance below exists to stop a Bills item being swallowed by a
            # differently-worded Money item — real, keep it. But it also let the SAME SENTENCE
            # in twice whenever Ace guessed a different category on the second capture (live
            # example: "Ask Robin about her personal deals…" filed under BOTH Networking and
            # Deals, 100% identical). That is the duplicate source Brady kept hitting — and the
            # reason completions "don't stick": he closes one twin and the other stays open.
            # 0.90+ means the same sentence (or a shortened version of it), so collapse it;
            # 0.85-0.90 is a genuine paraphrase about the same subject and still inserts as
            # "similar". Tested against Brady's real board: the items that MUST stay separate
            # (truck payment vs truck arrears, Mission Lane payoff vs minimum, the two Klarna
            # accounts) score 0.06-0.35 — nowhere near the line, so the data-loss fix holds.
            if best and best_score >= 0.90:
                return _repeat_result(best, text, due, parent_id)
            if best and best_score >= 0.85:                     # only a cross-column match
                similar = {"id": best["id"], "text": best["text"], "status": best["status"]}
            # Trigram second opinion: catches rewordings token overlap can't (nicknames,
            # typos, mashed words). Index already exists — this was only used by recall.
            if not similar and _trgm_ok:
                try:
                    with _conn() as c, c.cursor() as cur:
                        cur.execute(
                            "SELECT id, text, status, word_similarity(%s, text) AS s "
                            "FROM daybank_items ORDER BY s DESC LIMIT 1", (text,))
                        row = cur.fetchone()
                    if row and row[3] is not None:
                        ex = next((i for i in read_items(active_only=False)
                                   if i["id"] == row[0]), None)
                        if float(row[3]) >= 0.72 and ex and _same_col(ex) and _dedup_eligible(ex, text, due, parent_id):
                            return _repeat_result(ex, text, due, parent_id)
                        if float(row[3]) >= 0.5 and ex and not similar:
                            similar = {"id": row[0], "text": row[1], "status": row[2]}
                except Exception:
                    pass
            if best and best_score >= 0.45 and not similar:
                similar = {"id": best["id"], "text": best["text"], "status": best["status"]}
    # LAST GATE BEFORE INSERT: the lexical rules above have decided this is NOT a twin, which
    # is exactly where the Sienna duplicate got through. Only reached when a shortlist exists,
    # so a clearly-novel item still costs nothing.
    if dedup and _DUP_JUDGE is not None:
        try:
            _norm = _norm_item(text)
            _cands = _dup_candidates(text, _norm, [it for it in read_items(active_only=False) if _dedup_eligible(it, text, due, parent_id)]) if _norm else []
            if _cands:
                _hit = _DUP_JUDGE(text, _cands) or ""
                _match = next((c for c in _cands if c.get("id") == _hit), None)
                if _match:
                    logger.info("semantic dedup: %r collapsed into %s", text[:60], _match["id"])
                    return _repeat_result(_match, text, due, parent_id)
        except Exception as e:                      # a judge failure must never block a write
            logger.warning("semantic dedup skipped: %s", e)
    item = {
        "id": uuid.uuid4().hex[:8], "ts": datetime.now(EASTERN).isoformat(),
        "kind": kind, "text": text, "status": "open", "tags": tags or [],
        "due": (pin_due(due) or None), "done_ts": None,
    }
    try:
        import json
        with _conn() as c, c.cursor() as cur:
            # parent_id links a spawned ACTION back to the RECORD it came from ("mail Rebecca's
            # packet" -> the Rebecca record), so completing the action cannot take the record
            # with it. The column has existed since 2026-07-31 but nothing could ever set it.
            # bucket is the area Brady was LOOKING AT when he captured this. Storing it (as
            # opposed to leaving it to the keyword rules) is what makes "add it to this list"
            # mean what it says; an empty value still falls through to derive_bucket, which
            # lands unrecognised capture in Inbox rather than guessing Personal.
            _bkt = (bucket or "").strip() or None
            if _bkt and _bkt not in all_areas():
                return False, f"unknown area '{bucket}'"
            if not _bkt:
                _bkt = derive_bucket_for_capture(text, in_cat or "")
            item["bucket"] = _bkt
            cur.execute(
                "INSERT INTO daybank_items (id, ts, kind, text, status, tags, due, done_ts, "
                "parent_id, bucket) VALUES (%s, %s::timestamptz, %s, %s, 'open', %s::jsonb, %s, "
                "NULL, %s, %s)",
                (item["id"], item["ts"], kind, text, json.dumps(item["tags"]), item["due"],
                 (parent_id or None), _bkt))
        if similar:
            item["similar"] = similar   # heads-up, not a block: caller can merge/update
        return True, item
    except Exception as e:
        logger.error("db add_item failed: %s", e)
        return False, str(e)


def rollover_recurring_bills() -> int:
    """RECURRING BILLS (2026-08-11, Brady chose 'roll over monthly'): a Bills item marked paid
    (done) in a PRIOR month reopens for the new month, so the register stays a live picture of
    what's due — instead of a paid bill vanishing forever. Idempotent; safe to call daily."""
    ensure_ready()
    try:
        now = datetime.now(EASTERN)
        ym = (now.year, now.month)
        reopened = 0
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT id, tags, done_ts FROM daybank_items "
                        "WHERE status = 'done' AND done_ts IS NOT NULL")
            for iid, tags, done_ts in cur.fetchall():
                if not tags or "Bills" not in tags:
                    continue
                dts = done_ts.astimezone(EASTERN) if getattr(done_ts, "tzinfo", None) else done_ts
                if (dts.year, dts.month) < ym:
                    cur.execute("UPDATE daybank_items SET status = 'open', done_ts = NULL "
                                "WHERE id = %s", (iid,))
                    reopened += 1
        if reopened:
            logger.info("rollover: reopened %d recurring bill(s) for the new month", reopened)
        return reopened
    except Exception as e:
        logger.warning("rollover_recurring_bills failed: %s", e)
        return 0


def set_item_tags(item_id: str, tags: list) -> bool:
    """Replace an item's tags (used to backfill/repair categories). True on success."""
    ensure_ready()
    try:
        import json
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE daybank_items SET tags = %s::jsonb WHERE id = %s",
                        (json.dumps(canon_tags(tags)), item_id))
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


def _completion_blocked(item_id: str) -> tuple:
    """(blocked, reason) for closing this row with an ordinary completion.

    Reads the SAME classification the screens and Ace's context read, so there is one
    answer to "may this be completed" rather than one per caller. Fails OPEN on an error:
    a lookup problem must not make the board unusable, and the row is left as it was.
    """
    try:
        from . import classify
        it = next((x for x in read_items(active_only=False) if x.get("id") == item_id), None)
        if not it:
            return False, ""
        lane = classify.lane_of(it)
        if lane in classify.COMPLETABLE:
            return False, ""
        if lane == classify.LANE_WAITING:
            who = (it.get("waiting_on") or "").strip() or "someone else"
            return True, ("NOT COMPLETED — this is waiting on %s, who owns the next move. "
                          "Nothing on Brady's side finishes it. If it really is finished, "
                          "set state='settled'; only pass force_close if Brady says to close "
                          "it anyway. Do NOT tell him it is done." % who)
        if lane == classify.LANE_REFERENCE:
            return True, ("NOT COMPLETED — this is a RECORD Brady tracks, which has a state "
                          "rather than an ending. Update it, or set state='settled' when it "
                          "is genuinely finished. Do NOT tell him it is done.")
        return True, ("NOT COMPLETED — this row is already closed (%s)." % lane)
    except Exception as e:                       # never block the board on a lookup failure
        logger.warning("completion check skipped for %s: %s", item_id, type(e).__name__)
        return False, ""


def update_item(item_id: str, status: str = None, text: str = None,
                tags: list = None, due: str = None, match: str = None,
                superseded_by: str = None, closed_by: str = None,
                entry: str = None, state: str = None, waiting_on: str = None,
                bucket: str = None, next_step: str = None, followup: str = None,
                chosen_on: str = None, force_close: bool = False, reviewed: bool = None) -> tuple:
    """Edit a board item: status ('open'|'done'|'dropped'), text, tags (full replace),
    due (''=clear), superseded_by (merge link). Resolve by `match` text when the caller
    doesn't have the id — one confident hit applies, several return AMBIGUOUS candidates
    so the model can ask instead of guessing (the 'said it's done → new twin' fix)."""
    item_id = (item_id or "").strip()
    if not item_id and (match or "").strip():
        cands = find_items(match, status="open")
        if len(cands) == 1 and " ".join((match or "").casefold().split()) == " ".join(cands[0].get("text", "").casefold().split()):
            item_id = cands[0]["id"]
        elif not cands:
            return False, f"no open item matching '{match}'"
        else:
            return False, ("AMBIGUOUS — did you mean: "
                           + " | ".join(f"[{c['id']}] {(c.get('text') or '')[:60]}" for c in cands))
    if not item_id:
        return False, "no id"
    ensure_ready()
    # ── THE COMPLETION RULE LIVES HERE ─────────────────────────────────────────────
    # It was in the HTTP route, which protected the panel and nothing else: Ace's own
    # update_item tool calls straight through to this function, so he could close a record
    # that is parked on somebody else and report "◆ Completed" for work nobody had done.
    # Every path — panel, overlay, tool, sweep — passes through here, so the rule does too.
    # A waiting row finishes when the OTHER person acts; a reference record has a lifecycle,
    # not an ending. Changing one deliberately is still possible, but the caller has to say
    # force_close and mean it.
    if status == "done" and not force_close:
        blocked, why = _completion_blocked(item_id)
        if blocked:
            return False, why
    try:
        import json
        with _conn() as c, c.cursor() as cur:
            sets, args = [], []
            if status in ("open", "done", "dropped"):
                sets.append("status = %s"); args.append(status)
                # done_ts doubles as "closed at" for dropped, so the active view can show
                # today's archives once then let them fall away — never deleted.
                sets.append("done_ts = %s")
                args.append(datetime.now(EASTERN).isoformat() if status in ("done", "dropped") else None)
            if text and text.strip():
                sets.append("text = %s"); args.append(text.strip())
            if tags is not None:
                sets.append("tags = %s::jsonb"); args.append(json.dumps(canon_tags(tags)))
            if due is not None:
                sets.append("due = %s"); args.append(pin_due(due) or None)
            if (superseded_by or "").strip():
                sets.append("superseded_by = %s"); args.append(superseded_by.strip())
            # WHO CLOSED IT (2026-09-05). Never recorded, so when four records vanished in one
            # update there was no way to tell whether Brady did it or a sweep did.
            if closed_by and status in ("done", "dropped"):
                sets.append("closed_by = %s"); args.append(closed_by.strip()[:20])
            if entry in ("action", "record"):
                sets.append("entry = %s"); args.append(entry)
            # 'decide' joins the stored states (release one). It is set by Brady, never
            # derived — see classify.lane_of, where the old "undated and no next step"
            # inference was removed.
            if state in ("active", "waiting", "settled", "decide"):
                sets.append("state = %s"); args.append(state)
            if waiting_on is not None:
                sets.append("waiting_on = %s"); args.append((waiting_on.strip() or None))
            # next_step / followup follow the SAME contract as waiting_on and due:
            # None = leave alone, "" = clear to NULL, text = set. Nothing is ever inferred
            # from prose into either of them — they hold only what Brady or an explicit
            # edit put there. followup is BRADY's date to chase, which is a different thing
            # from `due` (the obligation's own deadline) and from the other party's timing.
            if next_step is not None:
                sets.append("next_step = %s"); args.append((next_step.strip()[:300] or None))
            if followup is not None:
                sets.append("followup = %s"); args.append((pin_due(followup) or None))
            if chosen_on is not None:
                sets.append("chosen_on = %s"); args.append((pin_due(chosen_on) or None))
                # Picking a day IS looking at the row, so it retires the review flag for
                # good rather than only until the next read.
                if (pin_due(chosen_on) or None):
                    reviewed = True if reviewed is None else reviewed
            if reviewed is not None:
                # Brady saying "this is Ready" has to be enough on its own. The old flag could
                # only be shaken off by inventing a due date or a next step, which is exactly
                # the fabricated-data pressure the derived lane created in the first place.
                sets.append("reviewed_at = " + ("now()" if reviewed else "NULL"))
            # all_areas(), not BUCKETS: a list Brady made himself is a real destination, and
            # a renamed built-in answers to its new name. Membership was checked against the
            # frozen tuple, so a move to any other area was dropped without a word — and when
            # it was the only field, the caller got "nothing to update" instead of a reason.
            if bucket is not None and str(bucket).strip():
                if str(bucket).strip() not in all_areas():
                    return False, f"unknown area '{bucket}'"
                sets.append("bucket = %s"); args.append(str(bucket).strip())
            if not sets:
                return False, "nothing to update"
            sets.append("updated_at = now()")   # a receipt for the edit, not a new field to set
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
