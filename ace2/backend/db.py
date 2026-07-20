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


def add_item(kind: str, text: str, due: str = None, tags: list = None) -> tuple:
    text = (text or "").strip()
    if not text:
        return False, "empty text"
    kind = (kind or "note").strip().lower()
    if kind not in KINDS:
        kind = "note"
    ensure_ready()
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


def update_item(item_id: str, status: str = None, text: str = None) -> tuple:
    item_id = (item_id or "").strip()
    if not item_id:
        return False, "no id"
    ensure_ready()
    try:
        with _conn() as c, c.cursor() as cur:
            sets, args = [], []
            if status in ("open", "done"):
                sets.append("status = %s"); args.append(status)
                sets.append("done_ts = %s")
                args.append(datetime.now(EASTERN).isoformat() if status == "done" else None)
            if text and text.strip():
                sets.append("text = %s"); args.append(text.strip())
            if not sets:
                return False, "nothing to update"
            args.append(item_id)
            cur.execute(f"UPDATE daybank_items SET {', '.join(sets)} WHERE id = %s RETURNING text", args)
            row = cur.fetchone()
        return (True, row[0]) if row else (False, f"no item {item_id}")
    except Exception as e:
        logger.error("db update_item failed: %s", e)
        return False, str(e)
