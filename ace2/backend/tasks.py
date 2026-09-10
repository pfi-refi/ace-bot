"""Durable tasks: one record per accepted request, with an explicit lifecycle.

WHY THIS EXISTS (2026-09-09). Brady asked by voice for a spreadsheet. Voice handed the
request to the screen as a chat turn, the screen ran ungated, unjournalled MCP calls, and
Ace told him the sheet was "built and populated" and then "open". Google said the file did
not exist. Nothing in the system could say what had actually happened, because nothing
recorded it: `ace_write_ops` covers the native tools and explicitly excludes MCP.

So a task is a ROW, not a turn. It survives the panel closing, the tab closing, a reconnect
and a redelivery. Its state is only ever advanced by execution evidence, and the states a
surface may show are exactly the states this table holds.

    queued  → working → completed
                     ↘ needs_approval → working → completed
                     ↘ failed
            ↘ cancelled

Terminal states (completed / failed / cancelled) are immutable: a late duplicate delivery
cannot reopen finished work, and a cancel cannot un-happen something already done.
"""
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import db

logger = logging.getLogger("ace2.tasks")

QUEUED = "queued"
WORKING = "working"
NEEDS_APPROVAL = "needs_approval"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})
LIVE = frozenset({QUEUED, WORKING, NEEDS_APPROVAL})
STATES = TERMINAL | LIVE

# A task claimed by a worker that dies leaves a row in `working` forever. After this it is
# reclaimable — long enough that a slow provider is never stolen from.
STALE_WORKING_SECONDS = int(os.environ.get("ACE2_TASK_STALE_SECONDS", "600"))
# How long two identical requests are treated as the same request. A repeated voice
# transcript, a double-tap, or two tabs sending the same thing must not make two documents.
DEDUP_WINDOW_SECONDS = int(os.environ.get("ACE2_TASK_DEDUP_SECONDS", "900"))


def enabled() -> bool:
    return db.enabled()


def ready() -> None:
    with db._conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ace_tasks (
            id            TEXT PRIMARY KEY,
            request_key   TEXT NOT NULL,
            capability    TEXT NOT NULL,
            args          JSONB NOT NULL,
            origin        TEXT NOT NULL DEFAULT 'voice',
            state         TEXT NOT NULL DEFAULT 'queued',
            title         TEXT,
            detail        TEXT,
            result        JSONB,
            error         TEXT,
            review_id     TEXT,
            attempts      INT NOT NULL DEFAULT 0,
            -- CANCELLATION IS A REQUEST, NOT AN OUTCOME (Codex, 2026-09-09). Marking a task
            -- cancelled while its provider call was still in flight displayed "cancelled"
            -- and then wrote the file anyway, and terminal immutability then blocked the
            -- receipt — so the record said nothing happened while a document existed. The
            -- flag is what a running handler checks at each side-effect boundary; the state
            -- only becomes cancelled once that handler has stopped and handed back what it
            -- did do.
            cancel_requested BOOLEAN NOT NULL DEFAULT false,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at    TIMESTAMPTZ,
            settled_at    TIMESTAMPTZ,
            spoken_at     TIMESTAMPTZ)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ace_tasks_key_idx "
                    "ON ace_tasks(request_key, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS ace_tasks_state_idx "
                    "ON ace_tasks(state, created_at DESC)")


_TEXT_KEYS = frozenset({"title", "text", "body", "notes", "description", "request"})


def request_key(capability: str, args: dict) -> str:
    """Stable identity for "the same request said twice".

    Whitespace is folded everywhere and case folded only for human-language fields, for the
    same reason ops.py does it: a speech re-flush that only re-spaces a title is the same
    request, but an id or an email that differs by case may be a different target.
    """
    def norm(v, key=""):
        if isinstance(v, dict):
            return {k: norm(v[k], k) for k in sorted(v)}
        if isinstance(v, (list, tuple)):
            return [norm(x, key) for x in v]
        if isinstance(v, str):
            s = " ".join(v.split())
            return s.lower() if key in _TEXT_KEYS else s
        return v
    blob = json.dumps({"c": capability, "a": norm(args or {})}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _row(r) -> dict:
    return {
        "id": r[0], "request_key": r[1], "capability": r[2], "args": r[3], "origin": r[4],
        "state": r[5], "title": r[6], "detail": r[7], "result": r[8], "error": r[9],
        "review_id": r[10], "attempts": r[11], "cancel_requested": bool(r[12]),
        "created_at": r[13].isoformat() if r[13] else None,
        "updated_at": r[14].isoformat() if r[14] else None,
        "started_at": r[15].isoformat() if r[15] else None,
        "settled_at": r[16].isoformat() if r[16] else None,
        "spoken_at": r[17].isoformat() if r[17] else None,
    }


_COLS = ("id, request_key, capability, args, origin, state, title, detail, result, error, "
         "review_id, attempts, cancel_requested, created_at, updated_at, started_at, "
         "settled_at, spoken_at")


def accept(capability: str, args: dict, origin: str = "voice", title: str = "",
           daily_cap: int = 0, day: str = "") -> tuple:
    """(verdict, task) — 'created' | 'existing' | 'unavailable'.

    'existing' is the whole point: a repeated transcript, a second tab, or a retried
    delivery finds the task that is already running or already finished instead of starting
    a second one. A request whose earlier attempt FAILED is allowed to start again, because
    a failure Brady can see and re-ask for is not a duplicate.

    `daily_cap` admits a PAID capability in the SAME transaction that creates its row, which
    is the only way the count can be trusted: checking in one transaction and inserting in
    another lets three concurrent requests all see room and all spend. Returns 'over_cap'
    with the usage instead. The day lock is always taken BEFORE the request-key lock so two
    transactions can never grab them in opposite orders and deadlock.
    """
    if not enabled():
        return "unavailable", None
    key = request_key(capability, args)
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            # One writer at a time for this key, so two simultaneous dispatches cannot both
            # miss each other's row and both insert.
            if daily_cap and daily_cap > 0:
                cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                            (716296, f"{capability}:{day}"))
                cur.execute("SELECT count(*) FROM ace_tasks WHERE capability = %s "
                            "AND state <> %s AND created_at >= %s::date "
                            "AND created_at < (%s::date + interval '1 day')",
                            (capability, CANCELLED, day, day))
                used = int(cur.fetchone()[0] or 0)
                if used >= daily_cap:
                    return "over_cap", {"used": used, "cap": daily_cap}
            cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (716295, key))
            cur.execute(f"SELECT {_COLS} FROM ace_tasks WHERE request_key = %s "
                        f"AND created_at > now() - %s::interval ORDER BY created_at DESC LIMIT 1",
                        (key, f"{DEDUP_WINDOW_SECONDS} seconds"))
            prior = cur.fetchone()
            # AN UNRESOLVED CREATE OUTLIVES THE DEDUP WINDOW. If an earlier attempt dispatched
            # a create and never learned the outcome, that protection must not expire fifteen
            # minutes later — that is precisely when a re-ask would make the second document.
            # Searched by request_key with no time bound, newest first.
            cur.execute(f"SELECT {_COLS} FROM ace_tasks WHERE request_key = %s AND "
                        f"result->>'create_state' = 'unknown' ORDER BY created_at DESC LIMIT 1",
                        (key,))
            unresolved = cur.fetchone()
            if unresolved and not prior:
                prior = unresolved
            elif unresolved and prior and _row(unresolved)["id"] != _row(prior)["id"]:
                prior = unresolved
            carried = {}
            if prior:
                got = _row(prior)
                if got["state"] in LIVE or got["state"] == COMPLETED:
                    return "existing", got
                # A FAILED attempt may still have created something. Brady re-asking is a
                # new task — he saw it fail — but it must not create a SECOND document, so
                # the irreversible facts the old attempt checkpointed come with it and the
                # handler resumes from them. This is "check for an existing result before
                # retrying creation after a timeout", enforced in the store rather than
                # left to whoever calls it.
                for k, v in (got.get("result") or {}).items():
                    # Everything that says "something already exists out there": the id if we
                    # got one, and the create marker/ambiguity if we did not.
                    if k in ("file_id", "url", "create_state", "create_marker"):
                        carried[k] = v
            tid = uuid.uuid4().hex
            cur.execute(f"INSERT INTO ace_tasks(id, request_key, capability, args, origin, "
                        f"state, title, result) VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb) "
                        f"RETURNING {_COLS}",
                        (tid, key, capability, json.dumps(args or {}), origin, QUEUED,
                         (title or "").strip()[:120] or None, json.dumps(carried)))
            return "created", _row(cur.fetchone())
    except Exception as e:
        logger.error("tasks.accept failed: %s", e)
        return "unavailable", None


def get(task_id: str) -> dict:
    if not enabled():
        return {}
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM ace_tasks WHERE id = %s", (task_id,))
            r = cur.fetchone()
        return _row(r) if r else {}
    except Exception as e:
        logger.warning("tasks.get failed: %s", e)
        return {}


def claim(task_id: str) -> bool:
    """Move queued → working, exactly once. False when somebody else already has it.

    This is what stops two workers — two tabs, or a retry racing the original — from both
    executing the same accepted task.
    """
    if not enabled():
        return False
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE ace_tasks SET state=%s, attempts=attempts+1, "
                        "started_at=now(), updated_at=now() WHERE id=%s AND "
                        "(state=%s OR (state=%s AND started_at < now() - %s::interval)) ",
                        (WORKING, task_id, QUEUED, WORKING,
                         f"{STALE_WORKING_SECONDS} seconds"))
            return (cur.rowcount or 0) > 0
    except Exception as e:
        logger.warning("tasks.claim failed: %s", e)
        return False


def _advance(task_id: str, state: str, **fields) -> dict:
    """Write a new state, refusing to move a task that has already finished.

    The guard is the point: a redelivered completion, a late failure from an abandoned
    attempt, or a cancel arriving after the document exists must not rewrite history.
    """
    if not enabled():
        return {}
    sets = ["state=%s", "updated_at=now()"]
    args = [state]
    for col in ("title", "detail", "error", "review_id"):
        if col in fields:
            sets.append(f"{col}=%s"); args.append(fields[col])
    if "result" in fields:
        # MERGE, never replace. A checkpointed file_id is the only thing standing between a
        # lost response and a duplicate document, and a failure used to overwrite `result`
        # with {} — destroying exactly the fact a retry needs. Later keys win; nothing
        # already recorded is dropped.
        sets.append("result = COALESCE(result,'{}'::jsonb) || %s::jsonb")
        args.append(json.dumps(fields["result"]))
    if state in TERMINAL:
        sets.append("settled_at=now()")
    args.append(task_id)
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute(f"UPDATE ace_tasks SET {', '.join(sets)} WHERE id=%s "
                        f"AND state NOT IN %s RETURNING {_COLS}",
                        tuple(args) + (tuple(TERMINAL),))
            r = cur.fetchone()
            if not r:
                cur.execute(f"SELECT {_COLS} FROM ace_tasks WHERE id=%s", (task_id,))
                cur_row = cur.fetchone()
                if cur_row:
                    logger.info("tasks: refused %s → %s on a %s task",
                                task_id[:8], state, _row(cur_row)["state"])
                return _row(cur_row) if cur_row else {}
            return _row(r)
    except Exception as e:
        logger.error("tasks._advance failed: %s", e)
        return {}


def working(task_id: str, detail: str = "") -> dict:
    return _advance(task_id, WORKING, detail=(detail or "")[:300] or None)


def checkpoint(task_id: str, patch: dict) -> dict:
    """Record an irreversible fact the moment it is true — before the step that follows it
    can time out.

    A create that succeeds and then loses its response is the dangerous case: retrying it
    makes a SECOND document. So the provider's id is written here as soon as it comes back,
    and a later attempt reads it and resumes instead of creating again. Merges into `result`
    without touching state, and never overwrites a fact already recorded.
    """
    if not enabled() or not patch:
        return {}
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute(f"UPDATE ace_tasks SET result = COALESCE(result,'{{}}'::jsonb) || "
                        f"%s::jsonb, updated_at=now() WHERE id=%s AND state NOT IN %s "
                        f"RETURNING {_COLS}",
                        (json.dumps(patch), task_id, tuple(TERMINAL)))
            r = cur.fetchone()
            return _row(r) if r else {}
    except Exception as e:
        logger.warning("tasks.checkpoint failed: %s", e)
        return {}


def needs_approval(task_id: str, review_id: str, detail: str = "") -> dict:
    return _advance(task_id, NEEDS_APPROVAL, review_id=review_id,
                    detail=(detail or "")[:300] or None)


def completed(task_id: str, result: dict, detail: str = "") -> dict:
    """Only ever called with execution evidence. `result` is what was verified, not what
    was requested — the caller reads the artefact back before it gets here."""
    return _advance(task_id, COMPLETED, result=result or {},
                    detail=(detail or "")[:300] or None)


def failed(task_id: str, error: str, result: dict = None) -> dict:
    return _advance(task_id, FAILED, error=(error or "failed")[:600],
                    result=result or {})


def request_cancel(task_id: str) -> tuple:
    """(verdict, task) — 'cancelled' | 'requested' | 'too_late' | 'gone'.

    A task that has not started yet can be stopped outright: nothing has happened. One that is
    already running gets a FLAG, and the handler stops at its next side-effect boundary and
    reports what it had already done. Nothing here claims the work did not happen.
    """
    got = get(task_id)
    if not got:
        return "gone", {}
    if got["state"] in TERMINAL:
        return "too_late", got
    if got["state"] == QUEUED:
        out = _advance(task_id, CANCELLED, detail="Stopped before it started")
        return "cancelled", out
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE ace_tasks SET cancel_requested=true, updated_at=now() "
                        "WHERE id=%s AND state NOT IN %s", (task_id, tuple(TERMINAL)))
    except Exception as e:
        logger.warning("tasks.request_cancel failed: %s", e)
        return "gone", got
    return "requested", get(task_id)


def is_cancel_requested(task_id: str) -> bool:
    t = get(task_id)
    return bool(t and t.get("cancel_requested"))


def cancelled_with_receipt(task_id: str, result: dict, detail: str) -> dict:
    """Settle as cancelled while RECORDING what already happened.

    The receipt is the point. A create that landed before the stop request is a real file, and
    the record has to say so — "cancelled" must never be read as "nothing was made".
    """
    return _advance(task_id, CANCELLED, result=result or {}, detail=(detail or "")[:300])


def cancel(task_id: str, reason: str = "") -> tuple:
    """(ok, task). Refuses once the task is terminal.

    "Cancelled" must never be said about work that already happened, so this reports the
    truth rather than pretending: a completed task stays completed and the caller is told.
    """
    got = get(task_id)
    if not got:
        return False, {}
    if got["state"] in TERMINAL:
        return False, got
    out = _advance(task_id, CANCELLED, detail=(reason or "cancelled by Brady")[:300])
    return (out.get("state") == CANCELLED), out


def reserve_daily(capability: str, cap: int, day: str) -> tuple:
    """(ok, used, cap) — atomically take one of today's slots for a paid capability.

    A declared cap is not a cap. This is the admission gate that makes ACE2_RESEARCH_DAILY_CAP
    real: it counts today's tasks and inserts the reservation inside ONE transaction, taking a
    lock keyed to the capability and day, so two concurrent voice turns — or a restarted
    worker — cannot both look, both see room, and both spend.

    Reservations live in the tasks table itself, so a restart cannot forget them, and a task
    that FAILED still counts: the money was spent whether or not the answer was useful.

    NOTE: this is the REPORTING view. Admission itself happens inside accept(), in the same
    transaction as the insert — counting here and inserting later is exactly how a race gets
    through, which it did.
    """
    if not enabled():
        return False, 0, cap
    if cap <= 0:
        return True, 0, cap
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                        (716296, f"{capability}:{day}"))
            cur.execute("SELECT count(*) FROM ace_tasks WHERE capability = %s "
                        "AND state <> %s AND created_at >= %s::date "
                        "AND created_at < (%s::date + interval '1 day')",
                        (capability, CANCELLED, day, day))
            used = int(cur.fetchone()[0] or 0)
            return (used < cap), used, cap
    except Exception as e:
        # FAIL CLOSED on a paid capability: if the ledger cannot be read we cannot know what
        # has already been spent today, and guessing costs real money.
        logger.error("tasks.reserve_daily failed: %s", e)
        return False, 0, cap


def retry(task_id: str) -> dict:
    """Re-queue a FAILED task, keeping everything it already achieved.

    Deliberately narrow: only a failure is retryable. A completed task is finished and a
    cancelled one was stopped on purpose — reopening either would be how "cancelled" or
    "done" stops meaning anything. The result is kept, so a retry resumes from the
    checkpointed file rather than creating a new one.
    """
    if not enabled():
        return {}
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute(f"UPDATE ace_tasks SET state=%s, error=NULL, settled_at=NULL, "
                        f"updated_at=now() WHERE id=%s AND state=%s RETURNING {_COLS}",
                        (QUEUED, task_id, FAILED))
            r = cur.fetchone()
            return _row(r) if r else {}
    except Exception as e:
        logger.warning("tasks.retry failed: %s", e)
        return {}


def mark_spoken(task_id: str) -> bool:
    """Record that Ace has already told Brady about this outcome out loud, so a completion
    is announced once even if several surfaces notice it."""
    if not enabled():
        return False
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE ace_tasks SET spoken_at=now() WHERE id=%s AND spoken_at IS NULL",
                        (task_id,))
            return (cur.rowcount or 0) > 0
    except Exception:
        return False


def recent(limit: int = 20, live_only: bool = False) -> list:
    if not enabled():
        return []
    try:
        ready()
        with db._conn() as c, c.cursor() as cur:
            if live_only:
                cur.execute(f"SELECT {_COLS} FROM ace_tasks WHERE state IN %s "
                            f"ORDER BY created_at DESC LIMIT %s",
                            (tuple(LIVE), max(1, min(limit, 100))))
            else:
                cur.execute(f"SELECT {_COLS} FROM ace_tasks ORDER BY created_at DESC LIMIT %s",
                            (max(1, min(limit, 100)),))
            return [_row(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning("tasks.recent failed: %s", e)
        return []


def card(t: dict) -> dict:
    """The task as a surface should render it. One shape for the toast, the bottom card and
    the recent-activity list, so they cannot disagree about a state."""
    if not t:
        return {}
    state = t.get("state")
    res = t.get("result") or {}
    out = {
        "task_id": t.get("id"),
        "state": state,
        # A stop was asked for and the handler has not reached a safe point yet. The surface
        # says "stopping", never "cancelled" — the work may still be mid-step.
        "stopping": bool(t.get("cancel_requested")) and state in LIVE,
        "capability": t.get("capability"),
        # Name it the way Brady asked for it. A card headed "Spreadsheet" tells him nothing
        # when two are in flight, so the request's own title is the fallback before the
        # generic one.
        "title": (t.get("title") or (t.get("args") or {}).get("title")
                  or _DEFAULT_TITLES.get(t.get("capability"), "Working")),
        "detail": t.get("detail") or "",
        "error": t.get("error") or "",
        "review_id": t.get("review_id") or "",
        "created_at": t.get("created_at"),
        "settled_at": t.get("settled_at"),
        # Only a completed task may carry an action, and only from a verified result.
        "action": None,
        # Success dismisses itself; an approval or a failure waits for Brady.
        "auto_dismiss_ms": 9000 if state == COMPLETED else 0,
        "sticky": state in (NEEDS_APPROVAL, FAILED),
    }
    if state == COMPLETED and res.get("url"):
        out["action"] = {"label": res.get("action_label") or "Open", "url": res["url"]}
    if res.get("warnings"):
        # The lead warning is already the card's `detail`; repeating it underneath made the
        # card say the same caveat twice.
        out["warnings"] = [w for w in res["warnings"] if w != out["detail"]]
    # RESEARCH SHOWS ITS WORKING. The sources and the date checked ride on the card itself,
    # so an answer can never appear without the evidence for it and without saying how old
    # that evidence is.
    if res.get("sources"):
        out["sources"] = res["sources"][:6]
        out["checked_at"] = res.get("checked_at", "")
        out["support"] = res.get("support", "")
        out["answer"] = (res.get("answer") or "")[:1200]
    return out


_DEFAULT_TITLES = {"create_spreadsheet": "Spreadsheet", "research": "Research"}
