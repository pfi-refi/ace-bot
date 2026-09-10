"""Runs accepted tasks in the background, on the server, tied to a task id.

The 9 September failure was architectural before it was a bug: voice could only author by
handing the request to the screen as a chat turn, so the work lived inside a panel. If no
screen was connected it did not run at all, and Ace's account of it came from the model's
prose rather than from anything that had happened.

Execution now belongs to the server. A task is accepted, claimed once, executed here, and
settled with evidence. Closing the card, closing the panel, hiding the page or losing the
socket changes nothing about whether the work runs — those only change who is watching.
Every event carries the task id, so a reconnect or a second tab re-reads state instead of
starting anything.
"""
import asyncio
import logging

from datetime import datetime, timezone

from . import capabilities, connectors, tasks

logger = logging.getLogger("ace2.taskrunner")

# Watchers, not workers. A socket subscribes to be told; it never drives execution.
_listeners: set = set()
_running: dict = {}


def subscribe(fn) -> None:
    _listeners.add(fn)


def unsubscribe(fn) -> None:
    _listeners.discard(fn)


async def _broadcast(task: dict) -> None:
    """Tell every attached surface the new state. Failures are per-listener: a dead socket
    must not stop the task or the other tabs from hearing about it."""
    payload = tasks.card(task)
    if not payload:
        return
    for fn in list(_listeners):
        try:
            await fn(payload)
        except Exception as e:
            logger.debug("task listener dropped: %s", e)
            _listeners.discard(fn)


async def _provider(name: str, arguments: dict) -> str:
    from .integrations import mcp_client
    return await mcp_client.call(name, arguments)


async def _execute(task_id: str, call=None) -> dict:
    """One attempt. Only ever advances state on evidence."""
    t = tasks.get(task_id)
    if not t:
        return {}
    spec = capabilities.REGISTRY.get(t["capability"])
    if not spec:
        return tasks.failed(task_id, f"{t['capability']} is not a supported task")

    async def progress(detail):
        await _broadcast(tasks.working(task_id, detail))

    async def checkpoint(patch):
        """Written straight to the row, so a retry after a lost response resumes instead of
        creating a second document. Returns False when the write did NOT land, so the
        handler can refuse to dispatch something it cannot keep track of."""
        out = await asyncio.to_thread(tasks.checkpoint, task_id, patch)
        return bool(out)

    async def should_stop():
        # Read fresh each time: the request arrives from another request handler while this
        # coroutine is mid-flight, so a cached value would miss it.
        return await asyncio.to_thread(tasks.is_cancel_requested, task_id)

    try:
        result = await spec["handler"](t.get("args") or {}, call or _provider, progress,
                                       t.get("result") or {}, checkpoint, should_stop)
    except capabilities.Cancelled as e:
        # Stopped at a boundary, carrying what already happened. Settled here — with the
        # receipt — rather than by the cancel request, which is why "cancelled" can no longer
        # be displayed while a file quietly exists.
        return tasks.cancelled_with_receipt(task_id, e.result, e.message)
    except capabilities.Failed as e:
        # A named, explainable failure. Any partial artefact rides along so Brady is told
        # what DOES exist rather than left to guess.
        return tasks.failed(task_id, e.message, e.result)
    except asyncio.CancelledError:
        # The awaiting caller went away; the work itself is not cancelled by that. Leave the
        # row in `working` so it is reclaimable, and never claim it stopped.
        logger.info("task %s: awaiter cancelled; execution left to settle", task_id[:8])
        raise
    except Exception as e:
        logger.exception("task %s crashed", task_id[:8])
        return tasks.failed(task_id, f"unexpected {type(e).__name__} — nothing verified")
    detail = ""
    if result.get("warnings"):
        detail = result["warnings"][0]
    return tasks.completed(task_id, result, detail)


async def run(task_id: str, call=None) -> dict:
    """Claim and execute. Safe to call from anywhere, any number of times.

    The claim is what makes that true: two tabs, a redelivered event and a retry all land
    here, and exactly one of them executes.
    """
    if task_id in _running:
        return tasks.get(task_id)
    if not tasks.claim(task_id):
        # Somebody else has it, or it is already finished. Re-broadcast so the caller's
        # surface still gets the current state.
        cur = tasks.get(task_id)
        await _broadcast(cur)
        return cur
    _running[task_id] = True
    try:
        await _broadcast(tasks.get(task_id))
        out = await _execute(task_id, call)
        await _broadcast(out)
        return out
    finally:
        _running.pop(task_id, None)


def start(task_id: str, call=None) -> None:
    """Fire-and-forget on the server's own loop, held so it cannot be garbage collected."""
    task = asyncio.create_task(run(task_id, call))
    _bg.add(task)
    task.add_done_callback(_bg.discard)


_bg: set = set()


async def dispatch(capability: str, args: dict, origin: str = "voice",
                   title: str = "", call=None) -> dict:
    """Accept a request and start it. Returns the card to show immediately.

    A DISPATCHED REQUEST IS NOT COMPLETED WORK, so the card returned here is `queued` or
    whatever an identical earlier request has genuinely reached — never a success.
    """
    if not capabilities.supported(capability):
        return {"state": tasks.FAILED, "error": f"{capability} is not a supported task",
                "sticky": True, "title": "Not supported", "auto_dismiss_ms": 0}
    # PAID CAPABILITIES ARE ADMITTED, NOT JUST ADVERTISED (2026-09-10). The daily cap was a
    # number in a table that no execution path read. Admission now happens INSIDE accept(),
    # in the same transaction that creates the row — checking here and inserting there let
    # three concurrent requests all see room and all spend.
    spec = capabilities.REGISTRY.get(capability) or {}
    daily_cap, day = 0, ""
    if spec.get("costs_money"):
        conn = connectors.get(spec.get("connector") or "")
        daily_cap = int(conn.get("daily_task_cap") or 0)
        day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    verdict, t = await asyncio.to_thread(tasks.accept, capability, args, origin, title,
                                         daily_cap, day)
    if verdict == "over_cap":
        # Refused BEFORE a row exists, so a declined request never looks like work.
        return {"state": tasks.FAILED, "sticky": True, "auto_dismiss_ms": 0,
                "title": spec.get("title") or capability,
                "error": (f"That would be paid lookup number {t['used'] + 1} today and the "
                          f"daily limit is {t['cap']}. I have not run it. Raise "
                          f"ACE2_RESEARCH_DAILY_CAP if you want more.")}
    if verdict == "unavailable":
        # Fail closed, like ops.py: without the record that makes a task single-use we
        # cannot tell a redelivery from a new request, and the failure mode is two documents.
        return {"state": tasks.FAILED, "sticky": True, "title": "Not started",
                "auto_dismiss_ms": 0,
                "error": "I could not reach the record that keeps this from running twice, "
                         "so I did not start it. Nothing was created."}
    if verdict == "existing":
        # An identical request already in flight or already done. Show ITS state.
        if t["state"] in tasks.LIVE:
            start(t["id"], call)
        return tasks.card(t)
    start(t["id"], call)
    return tasks.card(t)


async def resume_approved(task_id: str, call=None) -> dict:
    """Continue a task Brady approved in the Review tray."""
    t = tasks.get(task_id)
    if not t or t["state"] != tasks.NEEDS_APPROVAL:
        return tasks.card(t) if t else {}
    tasks.working(task_id, "Approved — continuing")
    return await run(task_id, call)


async def deny(task_id: str, reason: str = "") -> dict:
    """Brady said no. That is a finished task with an honest ending, not a failure to retry."""
    t = tasks.get(task_id)
    if not t:
        return {}
    if t["state"] in tasks.TERMINAL:
        return tasks.card(t)
    out = tasks.failed(task_id, reason or "You said no, so I did not do it.")
    await _broadcast(out)
    return tasks.card(out)


async def cancel(task_id: str, reason: str = "") -> dict:
    """Ask it to stop. Reports honestly what that achieved.

    A queued task stops outright — nothing has happened yet. A RUNNING one is flagged, and the
    handler stops at its next side-effect boundary and hands back what it had already done;
    until then the card says "stopping", not "cancelled". Displaying a cancellation while a
    provider call is still in flight is what let Ace report a stopped task and then write the
    file anyway.
    """
    verdict, t = tasks.request_cancel(task_id)
    if t:
        await _broadcast(t)
    card = tasks.card(t)
    if verdict == "too_late":
        card["cancel_refused"] = True
        card["cancel_reason"] = (
            "That had already finished before you asked me to stop, so there was nothing to "
            "cancel." if t["state"] == tasks.COMPLETED else
            "That was already finished, so there was nothing to cancel.")
    elif verdict == "requested":
        card["cancel_pending"] = True
        card["cancel_reason"] = (
            "Asked it to stop. It is mid-step, so I will stop at the next safe point and tell "
            "you exactly what had already been done.")
    return card


def pending_voice_notices(limit: int = 3) -> list:
    """Settled tasks Ace has not told Brady about out loud yet.

    Read by the voice path so a background completion is announced in the conversation he is
    already having, once, whichever surface noticed it first.
    """
    out = []
    for t in tasks.recent(limit=12):
        if t["state"] in (tasks.COMPLETED, tasks.FAILED) and not t.get("spoken_at"):
            out.append(t)
        if len(out) >= limit:
            break
    return out
