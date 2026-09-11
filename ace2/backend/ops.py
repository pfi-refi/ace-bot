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
import uuid
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
    "create_calendar_event", "reschedule_calendar_event", "add_task", "complete_task",
    "capture_item", "update_item", "save_memory", "update_profile", "draft_email",
})

# Writes that leave this process and land in someone else's system. Two consequences:
# an unexpected exception is UNKNOWN rather than a safe-to-retry failure (the provider may
# have committed before the response was lost), and the journal is MANDATORY — without it
# we cannot tell a redelivery from a new request, and the failure mode is a silent double
# booking. Local writes are excluded so a Postgres outage still falls back to Drive.
EXTERNAL = frozenset({"create_calendar_event", "reschedule_calendar_event", "add_task", "complete_task", "draft_email"})
# Brief delivery joins this set (2026-09-08, Codex rev2). A check-then-act read of the
# day marker is not atomic: two overlapping completions during a journal outage both read
# "not delivered" and both push. Requiring the journal means an outage DEFERS delivery
# instead of risking a duplicate — and since the journal and the brief marker share the
# same database, an outage that hides the journal would have broken delivery anyway.
REQUIRE_JOURNAL = EXTERNAL | {"bridge_deliver"}

# The one claim key both the bridge and the in-server fallback compete for.
def brief_claim(kind: str, day: str) -> dict:
    return {"job_id": f"brief:{kind}:{day}"}

# Writable paths that do NOT pass through this journal, stated rather than implied:
#   • MCP tools (mcp_client.call). Outward/destructive ones are already durable and
#     single-use via review_store; the rest are reads. Anything else that LOOKS like a
#     write is reported by uncovered_mcp_writes() rather than silently assumed safe.
#   • The Review tray executor in main.py — durable and single-use by construction.
NOT_JOURNALLED_NOTE = "MCP tool calls and Review-tray execution do not use this journal."

# Verbs that mean "this changes someone else's system".
_WRITE_VERBS = ("create", "send", "delete", "update", "insert", "add", "remove", "move",
                "trash", "modify", "write", "post", "reply", "forward", "label", "archive")


def uncovered_mcp_writes(tool_names, gated) -> list:
    """MCP tools that look like writes but are neither gated nor journalled.

    The honest answer to "is every write covered?" is no, and this makes the remaining
    boundary visible instead of leaving it to a claim in a report. Called by the diagnostic
    endpoint and asserted in tests, so a newly-enabled MCP write shows up as a gap rather
    than as a silent hole.
    """
    out = []
    for name in tool_names or []:
        if name in (gated or set()) or name in JOURNALLED:
            continue
        stem = name[4:] if name.startswith("mcp_") else name
        if any(stem.startswith(v) or ("_" + v) in stem for v in _WRITE_VERBS):
            out.append(name)
    return sorted(out)

_ADVISORY_LOCK = 716294   # neighbour of review_store's 716293; must not collide


def enabled() -> bool:
    return db.enabled()


def ready() -> None:
    """Append-only attempt log. One ROW PER ATTEMPT, never overwritten, so the history of
    an action survives a later attempt with the same identity (Codex: deleting the old row
    to reuse an identity destroyed exactly the evidence a reconciliation needs)."""
    with db._conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ace_write_ops (
            attempt_id TEXT PRIMARY KEY,
            op_key TEXT NOT NULL,
            tool TEXT NOT NULL,
            args JSONB NOT NULL,
            state TEXT NOT NULL DEFAULT 'dispatched',
            receipt TEXT,
            external_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            settled_at TIMESTAMPTZ)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ace_write_ops_key_idx "
                    "ON ace_write_ops(op_key, created_at DESC)")


# Free text a speech transcript may re-case or re-space between deliveries. Everything
# else — ids, emails, URLs, match strings, calendar ids — is compared EXACTLY, because
# case-folding an identifier can merge two genuinely different targets.
_TEXT_KEYS = frozenset({"text", "title", "summary", "description", "body", "notes",
                        "message", "content", "subject", "category", "kind"})


def _canonical(args: dict) -> str:
    """Stable text for hashing. Whitespace is folded everywhere (an ASR re-flush that only
    re-spaces is the same request); case is folded ONLY for human-language fields."""
    def norm(key, v):
        if isinstance(v, str):
            v = " ".join(v.split())
            return v.casefold() if key in _TEXT_KEYS else v
        if isinstance(v, dict):
            return {k: norm(k, v[k]) for k in sorted(v)}
        if isinstance(v, list):
            return [norm(key, x) for x in v]
        return v
    return json.dumps(norm("", args or {}), sort_keys=True, default=str)


def op_key(tool: str, args: dict) -> str:
    """Logical identity of one write.

    There is deliberately no session component: `_dispatch_write` never had a session to
    supply, and a parameter that is always empty implies a guarantee that does not exist.
    If ElevenLabs turns out to expose a stable turn or session id, THAT is what belongs
    here — it would express intent, which a content hash cannot.
    """
    return hashlib.sha256(f"{tool}\x00{_canonical(args)}".encode("utf-8")).hexdigest()[:40]


def begin(tool: str, args: dict, window: int = None) -> tuple:
    """Claim the right to perform this write.

    Returns (verdict, attempt_id, prior) where verdict is one of:
      "execute"      nothing equivalent is live — go ahead
      "duplicate"    an equivalent write already completed; `prior` is its receipt
      "in_flight"    an equivalent write is running right now — do not double-send
      "unknown"      an equivalent write was interrupted; needs confirmation, not a retry
      "unavailable"  ownership could not be established; NOTHING was attempted

    `window` overrides RETRY_WINDOW_SEC for callers that need durable-for-longer identity
    (the bridge's completion delivery uses a full day).

    FAILS CLOSED for EXTERNAL tools. Returning "execute" on a journal outage — which is
    what this did before — removed the protection precisely when duplicates are most
    likely, and for a calendar write that means a real double booking.
    """
    key = op_key(tool, args)
    win = RETRY_WINDOW_SEC if window is None else window
    if not enabled():
        return (("unavailable", key, None) if tool in REQUIRE_JOURNAL
                else ("execute", key, None))
    try:
        ready()
        attempt = uuid.uuid4().hex
        with db._conn() as c, c.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK,))
            cur.execute(
                """SELECT state, receipt, EXTRACT(EPOCH FROM (now() - created_at))
                     FROM ace_write_ops WHERE op_key=%s
                 ORDER BY created_at DESC LIMIT 1""", (key,))
            row = cur.fetchone()
            if row:
                state, receipt, age = row[0], row[1], float(row[2] or 0)
                if state in (COMPLETED, REPORTED) and age <= win:
                    return "duplicate", key, receipt
                if state == NEEDS_REVIEW and age <= win:
                    # Not saved and awaiting a human decision — repeating the identical
                    # request must not quietly turn into a second attempt.
                    return "duplicate", key, receipt
                if state == "dispatched":
                    if age <= STALE_SEC:
                        return "in_flight", key, None
                    cur.execute("UPDATE ace_write_ops SET state=%s, settled_at=now() "
                                "WHERE op_key=%s AND state='dispatched'", (UNKNOWN, key))
                    return "unknown", key, (
                        "A previous attempt at this exact action was interrupted before its "
                        "outcome was recorded. It may or may not have gone through. Check "
                        "before doing it again — do not just retry.")
                if state == UNKNOWN:
                    return "unknown", key, (
                        "This exact action was previously interrupted with an unrecorded "
                        "outcome. Verify what actually happened before retrying.")
                # completed/reported beyond the window, or a clean failure → new intention.
            cur.execute(
                "INSERT INTO ace_write_ops(attempt_id,op_key,tool,args) "
                "VALUES(%s,%s,%s,%s::jsonb)",
                (attempt, key, tool, json.dumps(args or {}, sort_keys=True, default=str)))
        return "execute", attempt, None
    except Exception as e:
        logger.warning("op journal unavailable (%s)", type(e).__name__)
        if tool in REQUIRE_JOURNAL:
            return "unavailable", key, None
        return "execute", key, None


def settle(attempt_id: str, state: str, receipt: str = "", external_id: str = "") -> None:
    """Record the outcome against THIS attempt. Best-effort: a journal write must never
    mask a real result."""
    if state not in _STATES and state != "dispatched":
        raise ValueError("invalid op state: " + str(state))
    if not enabled():
        return
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE ace_write_ops SET state=%s, receipt=%s, external_id=%s, "
                        "settled_at=now() WHERE attempt_id=%s",
                        (state, str(receipt)[:4000], external_id or None, attempt_id))
    except Exception as e:
        logger.warning("op settle failed for %s: %s", str(attempt_id)[:8], type(e).__name__)


def pending(limit: int = 30) -> list:
    """Attempts with no recorded outcome, oldest first — the reconciliation queue."""
    if not enabled():
        return []
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT attempt_id, tool, args, state, created_at
                     FROM ace_write_ops
                    WHERE state IN ('dispatched', %s)
                      AND created_at > now() - interval '7 days'
                 ORDER BY created_at ASC LIMIT %s""", (UNKNOWN, limit))
            return [{"attempt_id": r[0], "tool": r[1], "args": r[2], "state": r[3],
                     "created_at": r[4].isoformat()} for r in cur.fetchall()]
    except Exception as e:
        logger.warning("op pending read failed: %s", type(e).__name__)
        return []


def sweep_stale() -> int:
    """Promote long-dispatched attempts to unknown. Runs at startup so a process that died
    mid-write leaves an explicit unknown rather than a row that looks in-flight forever."""
    if not enabled():
        return 0
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE ace_write_ops SET state=%s, settled_at=now() "
                        "WHERE state='dispatched' "
                        "AND created_at < now() - make_interval(secs => %s)",
                        (UNKNOWN, STALE_SEC))
            return cur.rowcount or 0
    except Exception as e:
        logger.warning("op sweep failed: %s", type(e).__name__)
        return 0


def recent_writes(hours: int = 36, limit: int = 40) -> list:
    """Attempts made recently, with their outcomes — the evidence a resumed planning
    session needs to tell a promise apart from a receipt."""
    if not enabled():
        return []
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT tool, args, state, receipt, created_at, external_id
                     FROM ace_write_ops
                    WHERE created_at > now() - make_interval(hours => %s)
                 ORDER BY created_at DESC LIMIT %s""", (hours, limit))
            return [{"tool": r[0], "args": r[1], "state": r[2], "receipt": r[3],
                     "created_at": r[4].isoformat(), "external_id": r[5]}
                    for r in cur.fetchall()]
    except Exception as e:
        logger.warning("op recent read failed: %s", type(e).__name__)
        return []


# ── EXECUTOR OUTCOMES ───────────────────────────────────────────────────────────
# A prose prefix is NOT a success contract. Classifying "anything not starting with ⚠"
# as completed meant the capture message "◆ NOT SAVED — needs your call" was journalled
# as a finished action and then shown to Ace under "ACTIONS ALREADY CARRIED OUT" — the
# assistant's record claiming work happened when it had not. That is the exact failure
# this whole effort exists to remove, so executors that know their outcome now say so.
COMPLETED = "completed"                       # verified done; `record_id` where a provider gave one
NEEDS_REVIEW = "needs_review"                 # deliberately not saved; waiting on a human decision
FAILED_BEFORE_DISPATCH = "failed_before_dispatch"   # never reached the external system; safe to retry
UNKNOWN = "unknown"                           # dispatched, outcome unrecorded; NEVER auto-replay
REPORTED = "reported"                         # legacy executor returned prose; claimed, unverified
UNAVAILABLE = "unavailable"                   # could not establish ownership; nothing was attempted

_STATES = (COMPLETED, NEEDS_REVIEW, FAILED_BEFORE_DISPATCH, UNKNOWN, REPORTED, UNAVAILABLE)


class Outcome:
    """What an executor actually did. `text` is what the model reads; `state` is the truth."""

    __slots__ = ("state", "text", "record_id", "detail")

    def __init__(self, state: str, text: str, record_id: str = "", detail: dict = None):
        if state not in _STATES:
            raise ValueError("unknown outcome state: " + str(state))
        self.state = state
        self.text = text
        self.record_id = record_id or ""
        self.detail = detail or {}

    def __str__(self) -> str:      # legacy call sites treat results as strings
        return self.text

    def __repr__(self) -> str:
        return f"Outcome({self.state}, {self.text[:40]!r})"


def classify(result) -> tuple:
    """(state, text, record_id) for any executor result, structured or legacy prose.

    Legacy prose can only ever be REPORTED, never COMPLETED: nothing verified it. The
    distinction is carried through to the planning context, where REPORTED work is listed
    as claimed-but-unverified rather than as done.
    """
    if isinstance(result, Outcome):
        return result.state, result.text, result.record_id
    text = str(result)
    if text.lstrip().startswith("\u26a0"):
        return FAILED_BEFORE_DISPATCH, text, ""
    return REPORTED, text, ""
