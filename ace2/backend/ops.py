"""Durable journal for writes, so one logical write happens exactly once.

WHY THIS EXISTS (2026-09-08). A live voice turn runs its tools with
`await asyncio.to_thread(tools.execute, ...)`. When ElevenLabs supersedes the turn,
`main.py` cancels that task — but cancelling the *await* does not stop the *thread*.
The write finishes; nobody records that it did. Two bad outcomes followed:

  • the write landed and Ace reported nothing, so Brady re-asked and it landed twice;
  • the write landed and the next turn "retried" it, double-booking the calendar.

So the fix is not to stop cancelling (barge-in is a feature, and the thread cannot be
recalled anyway). The fix is to give every write a durable identity and lifecycle, and
to treat an interrupted dispatch as UNKNOWN rather than failed — an unknown is never
replayed automatically, because replaying is exactly how the Ken call got booked four
times on 2026-08-25.

STATES
  dispatched  the write was handed to the executor; outcome not yet recorded
  completed   the executor returned; `receipt` holds what it said
  failed      the executor raised or returned a ⚠ result
  unknown     dispatched, then the turn died before an outcome was recorded.
              Surfaced for review. NEVER auto-replayed.

IDENTITY. `op_key` is a hash of tool + canonical args + session. Two calls with the
same key inside RETRY_WINDOW_SEC are treated as the same logical write: the second
returns the first's receipt instead of executing. Outside that window the same words
mean a NEW intention ("add another block on Tuesday") and execute normally. Identical
wording alone is deliberately NOT the whole key — the window and the session are what
separate a delivery retry from an intentional repeat, and the window is the knob.
"""
import hashlib
import json
import logging
import os

from . import db

logger = logging.getLogger("ace_portal.ops")

# Deliveries retried by ElevenLabs arrive within seconds; a deliberate repeat of the
# exact same action almost never does. Short enough not to swallow real intent.
RETRY_WINDOW_SEC = int(os.environ.get("ACE2_OP_RETRY_WINDOW", "180"))
# A dispatch with no outcome after this long is presumed interrupted → unknown.
STALE_SEC = int(os.environ.get("ACE2_OP_STALE", "120"))

# Writes that execute inline. Outward/destructive tools are NOT here: they go through
# review_store, which is already durable and single-use.
JOURNALLED = frozenset({
    "create_calendar_event", "add_task", "complete_task",
    "capture_item", "update_item", "save_memory", "update_profile", "draft_email",
})

_ADVISORY_LOCK = 716294   # neighbour of review_store's 716293; must not collide


def enabled() -> bool:
    return db.enabled()


def ready() -> None:
    with db._conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ace_ops (
            op_key TEXT PRIMARY KEY,
            tool TEXT NOT NULL,
            args JSONB NOT NULL,
            state TEXT NOT NULL DEFAULT 'dispatched',
            receipt TEXT,
            external_id TEXT,
            session TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            settled_at TIMESTAMPTZ)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ace_ops_created_idx ON ace_ops(created_at DESC)")


def _canonical(args: dict) -> str:
    """Stable text for hashing. Whitespace-folded so an ASR re-flush that only changes
    spacing still hashes the same; key order fixed so dict ordering never matters."""
    def norm(v):
        if isinstance(v, str):
            return " ".join(v.split()).casefold()
        if isinstance(v, dict):
            return {k: norm(v[k]) for k in sorted(v)}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return json.dumps(norm(args or {}), sort_keys=True, default=str)


def op_key(tool: str, args: dict, session: str = "") -> str:
    raw = f"{tool}\x00{_canonical(args)}\x00{session or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def external_event_id(key: str) -> str:
    """A deterministic Google Calendar event id derived from the op key.

    Google accepts a caller-supplied `id` on insert and rejects a second insert of the
    same id with 409. That turns a retry into a provider-side no-op — real idempotency,
    not the read-then-write probe in calendar_api, which two concurrent turns can both
    pass before either writes. Charset is base32hex (a-v, 0-9), length 5-1024.
    """
    trans = str.maketrans("wxyz", "0123")
    return ("ace" + key)[:40].translate(trans)


def begin(tool: str, args: dict, session: str = "") -> tuple:
    """Claim the right to perform this write.

    Returns (verdict, key, prior) where verdict is one of:
      "execute"    nothing equivalent is recorded — go ahead
      "duplicate"  an equivalent write already completed; `prior` is its receipt
      "in_flight"  an equivalent write is dispatched right now — do not double-send
      "unknown"    an equivalent write was interrupted; `prior` explains. Needs review.
    Never raises: if the journal is unavailable the caller still executes (degraded to
    today's behaviour) rather than losing the write entirely.
    """
    key = op_key(tool, args, session)
    if not enabled():
        return "execute", key, None
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK,))
            cur.execute(
                """SELECT state, receipt,
                          EXTRACT(EPOCH FROM (now() - created_at))
                     FROM ace_ops WHERE op_key=%s""", (key,))
            row = cur.fetchone()
            if row:
                state, receipt, age = row[0], row[1], float(row[2] or 0)
                if state == "completed" and age <= RETRY_WINDOW_SEC:
                    return "duplicate", key, receipt
                if state == "dispatched":
                    if age <= STALE_SEC:
                        return "in_flight", key, None
                    cur.execute("UPDATE ace_ops SET state='unknown', settled_at=now() "
                                "WHERE op_key=%s AND state='dispatched'", (key,))
                    return "unknown", key, (
                        "A previous attempt at this exact action was interrupted before its "
                        "outcome was recorded. It may or may not have gone through. Check "
                        "before doing it again — do not just retry.")
                if state == "unknown":
                    return "unknown", key, (
                        "This exact action was previously interrupted with an unrecorded "
                        "outcome. Verify what actually happened before retrying.")
                # completed but older than the window, or failed → a fresh intention.
                cur.execute("DELETE FROM ace_ops WHERE op_key=%s", (key,))
            cur.execute(
                "INSERT INTO ace_ops(op_key,tool,args,session) VALUES(%s,%s,%s::jsonb,%s)",
                (key, tool, json.dumps(args or {}, sort_keys=True, default=str), session or None))
        return "execute", key, None
    except Exception as e:
        logger.warning("op journal unavailable (%s) — executing unjournalled", type(e).__name__)
        return "execute", key, None


def settle(key: str, state: str, receipt: str = "", external_id: str = "") -> None:
    """Record the outcome. Best-effort: a journal write must never mask a real result."""
    if state not in ("completed", "failed", "unknown"):
        raise ValueError("invalid op state: " + str(state))
    if not enabled():
        return
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE ace_ops SET state=%s, receipt=%s, external_id=%s, settled_at=now() "
                        "WHERE op_key=%s", (state, str(receipt)[:4000], external_id or None, key))
    except Exception as e:
        logger.warning("op settle failed for %s: %s", key[:8], type(e).__name__)


def pending(limit: int = 30) -> list:
    """Dispatches with no recorded outcome, oldest first — the reconciliation queue."""
    if not enabled():
        return []
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT op_key, tool, args, state, created_at
                     FROM ace_ops
                    WHERE state IN ('dispatched','unknown')
                      AND created_at > now() - interval '7 days'
                 ORDER BY created_at ASC LIMIT %s""", (limit,))
            return [{"op_key": r[0], "tool": r[1], "args": r[2], "state": r[3],
                     "created_at": r[4].isoformat()} for r in cur.fetchall()]
    except Exception as e:
        logger.warning("op pending read failed: %s", type(e).__name__)
        return []


def sweep_stale() -> int:
    """Promote long-dispatched rows to unknown. Called at startup and by the self-audit,
    so a process that died mid-write leaves an explicit unknown rather than a row that
    looks in-flight forever. Returns how many were promoted."""
    if not enabled():
        return 0
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE ace_ops SET state='unknown', settled_at=now() "
                        "WHERE state='dispatched' AND created_at < now() - make_interval(secs => %s)",
                        (STALE_SEC,))
            return cur.rowcount or 0
    except Exception as e:
        logger.warning("op sweep failed: %s", type(e).__name__)
        return 0


def recent_writes(hours: int = 36, limit: int = 40) -> list:
    """Writes attempted recently, with their outcomes — the evidence a resumed planning
    session needs to tell a promise apart from a receipt."""
    if not enabled():
        return []
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT tool, args, state, receipt, created_at
                     FROM ace_ops
                    WHERE created_at > now() - make_interval(hours => %s)
                 ORDER BY created_at DESC LIMIT %s""", (hours, limit))
            return [{"tool": r[0], "args": r[1], "state": r[2], "receipt": r[3],
                     "created_at": r[4].isoformat()} for r in cur.fetchall()]
    except Exception as e:
        logger.warning("op recent read failed: %s", type(e).__name__)
        return []
