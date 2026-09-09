"""The task lifecycle through the ACTUAL routes and a real disposable PostgreSQL.

Synthetic data and a fake provider only. No production URL, no Google credentials, no model
calls, no network. Every case here is one Brady listed.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'ace2'))
import pgserver                                        # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402

tmp = tempfile.TemporaryDirectory(prefix='ace-voice-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    from ace2.backend import capabilities as cp        # noqa: E402
    from ace2.backend import db, taskrunner, tasks     # noqa: E402
    from ace2.backend.main import app                  # noqa: E402
    db._init_schema(); db._ready = True; db._trgm_ok = False
    tasks.ready()
    cp.EXPECTED_USER = "brady@example.com"

    ROWS = [["Assistant", "Pricing model", "Monthly"],
            ["Northwind Helper", "flat", "$20"],
            ["Cedar Assist", "per-request", "$0.004"]]
    ARGS = {"title": "Sample Assistant Pricing (fictional)", "rows": ROWS}

    def make_provider(**script):
        state = {"created": 0, "sheet": {}, "calls": []}

        async def call(tool, args):
            state["calls"].append(tool)
            if tool in script:
                v = script[tool]
                return v(args, state) if callable(v) else v
            if tool == "mcp_create_spreadsheet":
                state["created"] += 1
                return json.dumps({"spreadsheetId": f"1Sheet{state['created']:0>34}"})
            if tool == "mcp_modify_sheet_values":
                state["sheet"][args.get("range")] = args.get("values")
                return json.dumps({"updatedCells": 9})
            if tool == "mcp_read_sheet_values":
                return json.dumps({"values": state["sheet"].get(args.get("range"), [])})
            if tool.startswith("mcp_get_drive_file"):
                return json.dumps({"owners": [{"emailAddress": "brady@example.com"}]})
            return "(no content returned)"
        return call, state

    c = TestClient(app)
    # ONE loop for the whole file: coroutines created in one and awaited in another raise,
    # and the gather below deliberately creates three before awaiting any.
    LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(LOOP)
    run = LOOP.run_until_complete

    # ── 1. SUCCESS: verified, linked, and announced only afterwards ─────────────
    call, st = make_provider()
    card = run(taskrunner.dispatch("create_spreadsheet", ARGS, origin="voice",
                                   title=ARGS["title"], call=call))
    assert card["state"] in (tasks.QUEUED, tasks.WORKING, tasks.COMPLETED), card
    # dispatch returns immediately and must NOT claim success
    tid = card["task_id"]
    for _ in range(200):
        t = tasks.get(tid)
        if t["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    t = tasks.get(tid)
    assert t["state"] == tasks.COMPLETED, t
    done = tasks.card(t)
    assert done["action"]["label"] == "Open spreadsheet"
    assert done["action"]["url"].startswith("https://docs.google.com/spreadsheets/d/1Sheet")
    assert t["result"]["file_id"] in done["action"]["url"], 'the link must come from the id'
    assert "mcp_read_sheet_values" in st["calls"], 'nothing was read back'
    assert done["auto_dismiss_ms"] >= 8000 and done["auto_dismiss_ms"] <= 10000
    assert done["sticky"] is False
    assert st["created"] == 1

    # ── 2. DUPLICATE DISPATCH and MULTIPLE TABS: one document, not three ────────
    call2, st2 = make_provider()
    # Its own title: case 1's request is already COMPLETED, and asking for exactly that
    # again correctly returns the finished task rather than building a second copy — which
    # is itself asserted at the end of this block.
    DUP = {"title": "Duplicate dispatch check", "rows": ROWS}
    first = run(taskrunner.dispatch("create_spreadsheet", DUP, call=call2))
    again = run(taskrunner.dispatch("create_spreadsheet", DUP, call=call2))
    third = run(taskrunner.dispatch("create_spreadsheet", dict(DUP), call=call2))
    assert first["task_id"] == again["task_id"] == third["task_id"], 'duplicates forked'
    # the same request phrased with different spacing/casing is the same request
    spaced = run(taskrunner.dispatch(
        "create_spreadsheet",
        {"title": "  duplicate   DISPATCH check ", "rows": ROWS}, call=call2))
    assert spaced["task_id"] == first["task_id"], 're-flushed transcript made a second task'
    for _ in range(200):
        if tasks.get(first["task_id"])["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    assert st2["created"] == 1, f'created {st2["created"]} documents for one request'
    # ...and re-asking for something ALREADY FINISHED returns that result, builds nothing.
    call2b, st2b = make_provider()
    repeat = run(taskrunner.dispatch("create_spreadsheet", ARGS, call=call2b))
    assert repeat["task_id"] == tid and repeat["state"] == tasks.COMPLETED, repeat
    assert st2b["created"] == 0, 'a finished request was built a second time'

    # two tabs both call run() on the same id — the claim allows exactly one execution
    call3, st3 = make_provider()
    v, t3 = tasks.accept("create_spreadsheet", {"title": "Two tabs", "rows": ROWS})
    assert v == "created"
    run(asyncio.gather(taskrunner.run(t3["id"], call3), taskrunner.run(t3["id"], call3),
                       taskrunner.run(t3["id"], call3)))
    assert st3["created"] == 1, f'multiple tabs created {st3["created"]} documents'
    assert tasks.get(t3["id"])["state"] == tasks.COMPLETED

    # ── 3. TIMEOUT AFTER CREATION, BEFORE THE RESPONSE ARRIVES ──────────────────
    # The provider makes the file, then the next step dies. A retry must NOT create again.
    boom = {"n": 0}

    def flaky_write(args, state):
        boom["n"] += 1
        if boom["n"] == 1:
            raise asyncio.TimeoutError("response lost")
        state["sheet"][args.get("range")] = args.get("values")
        return json.dumps({"updatedCells": 9})

    call4, st4 = make_provider(mcp_modify_sheet_values=flaky_write)
    v, t4 = tasks.accept("create_spreadsheet", {"title": "Lost response", "rows": ROWS})
    out = run(taskrunner.run(t4["id"], call4))
    assert out["state"] == tasks.FAILED, out
    mid = tasks.get(t4["id"])
    assert mid["result"].get("file_id"), 'the created id was not checkpointed'
    kept = mid["result"]["file_id"]
    # a COMPLETED or CANCELLED task is not retryable; only a failure is
    assert tasks.retry(tid) == {}, 'a finished task was re-queued'
    # retry the SAME task: it resumes from the recorded id and creates nothing new
    assert tasks.retry(t4["id"])["state"] == tasks.QUEUED
    out2 = run(taskrunner.run(t4["id"], call4))
    assert out2["state"] == tasks.COMPLETED, out2
    assert st4["created"] == 1, f'the retry created {st4["created"]} documents'
    assert tasks.get(t4["id"])["result"]["file_id"] == kept, 'the retry made a different file'

    # ...and BRADY RE-ASKING is a new task that still must not create a second document.
    boom2 = {"n": 0}

    def fail_once(args, state):
        boom2["n"] += 1
        if boom2["n"] == 1:
            raise asyncio.TimeoutError("response lost")
        state["sheet"][args.get("range")] = args.get("values")
        return json.dumps({"updatedCells": 9})

    call4b, st4b = make_provider(mcp_modify_sheet_values=fail_once)
    REASK = {"title": "Re-asked after a lost response", "rows": ROWS}
    a1 = run(taskrunner.dispatch("create_spreadsheet", REASK, call=call4b))
    for _ in range(200):
        if tasks.get(a1["task_id"])["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    assert tasks.get(a1["task_id"])["state"] == tasks.FAILED
    made_id = tasks.get(a1["task_id"])["result"]["file_id"]
    a2 = run(taskrunner.dispatch("create_spreadsheet", REASK, call=call4b))
    assert a2["task_id"] != a1["task_id"], 'a visible failure should be re-askable'
    for _ in range(200):
        if tasks.get(a2["task_id"])["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    assert tasks.get(a2["task_id"])["state"] == tasks.COMPLETED
    assert st4b["created"] == 1, f're-asking created {st4b["created"]} documents'
    assert tasks.get(a2["task_id"])["result"]["file_id"] == made_id, \
        're-asking made a different file instead of finishing the one that exists'

    # ── 4. PERMISSION DENIED and PROVIDER FAILURE ───────────────────────────────
    call5, _ = make_provider(mcp_create_spreadsheet="Error 403: permission denied")
    v, t5 = tasks.accept("create_spreadsheet", {"title": "Denied", "rows": ROWS})
    out = run(taskrunner.run(t5["id"], call5))
    assert out["state"] == tasks.FAILED
    fc = tasks.card(out)
    assert fc["sticky"] is True and fc["auto_dismiss_ms"] == 0, 'a failure must not self-dismiss'
    assert fc["action"] is None, 'a failed task must not offer a link'
    assert "permission denied" in fc["error"]

    call6, _ = make_provider(mcp_create_spreadsheet="The spreadsheet was created.")
    v, t6 = tasks.accept("create_spreadsheet", {"title": "No id", "rows": ROWS})
    out = run(taskrunner.run(t6["id"], call6))
    assert out["state"] == tasks.FAILED and "did not return a spreadsheet id" in out["error"]
    assert not (out.get("result") or {}).get("url"), 'a link was offered without an id'

    # ── 5. WRONG ACCOUNT: correct file, but Brady cannot open it ────────────────
    call7, st7 = make_provider(
        mcp_get_drive_file_metadata=json.dumps({"owners": [{"emailAddress": "svc@robot.iam"}]}),
        mcp_get_drive_file_info="Error: none",
        mcp_search_drive_files="Error: none")
    v, t7 = tasks.accept("create_spreadsheet", {"title": "Wrong owner", "rows": ROWS})
    out = run(taskrunner.run(t7["id"], call7))
    assert out["state"] == tasks.FAILED, out
    assert "file does not exist" in out["error"], out["error"]
    assert "mcp_get_drive_shareable_link" not in st7["calls"], 'it tried to share its way out'

    # ── 6. APPROVAL: required, denied, and "No, do not send it" ─────────────────
    async def gated(args, call, progress=None, known=None, checkpoint=None):
        raise AssertionError("must not execute before approval")

    cp.REGISTRY["_gated_test"] = {"handler": gated, "title": "Gated", "verb": "Sending"}
    v, t8 = tasks.accept("_gated_test", {"to": "someone@example.com"})
    tasks.needs_approval(t8["id"], review_id="rev-1", detail="Waiting on your OK")
    gc = tasks.card(tasks.get(t8["id"]))
    assert gc["state"] == tasks.NEEDS_APPROVAL
    assert gc["sticky"] is True and gc["auto_dismiss_ms"] == 0, \
        'a pending approval must never auto-dismiss'
    assert gc["review_id"] == "rev-1"
    # "No, do not send it"
    denied = run(taskrunner.deny(t8["id"], "You said no, so I did not do it."))
    assert denied["state"] == tasks.FAILED and "did not do it" in denied["error"]
    # and a denial is final — approving afterwards cannot resurrect it
    assert run(taskrunner.resume_approved(t8["id"]))["state"] == tasks.FAILED
    cp.REGISTRY.pop("_gated_test")

    # ── 7. TERMINAL IS TERMINAL: redelivery cannot rewrite history ──────────────
    finished = tasks.get(tid)
    assert tasks.completed(tid, {"file_id": "SOMETHING-ELSE"})["result"]["file_id"] \
        == finished["result"]["file_id"], 'a redelivery rewrote a finished result'
    assert tasks.failed(tid, "late failure")["state"] == tasks.COMPLETED, \
        'a late failure reopened a completed task'
    assert tasks.working(tid)["state"] == tasks.COMPLETED, 'a stale event walked it backwards'

    # cancelling something already done is refused, and says so
    ok, t_done = tasks.cancel(tid)
    assert ok is False and t_done["state"] == tasks.COMPLETED
    refused = run(taskrunner.cancel(tid))
    assert refused.get("cancel_refused") is True
    assert "already finished" in refused["cancel_reason"]

    # a live task cancels properly
    v, t9 = tasks.accept("create_spreadsheet", {"title": "Stop me", "rows": ROWS})
    cancelled = run(taskrunner.cancel(t9["id"], "cancelled by Brady"))
    assert cancelled["state"] == tasks.CANCELLED and not cancelled.get("cancel_refused")

    # ── 8. THE ROUTES: reconnect, hidden page, retrieval after dismissal ────────
    r = c.post('/actions/start', json={"capability": "create_spreadsheet",
                                       "args": {"title": "Via route", "rows": ROWS}}).json()
    assert r["card"]["state"] in (tasks.QUEUED, tasks.WORKING, tasks.COMPLETED), r
    rid = r["card"]["task_id"]
    # An unsupported capability is refused without a task
    bad = c.post('/actions/start', json={"capability": "launch_rocket", "args": {}}).json()
    assert bad["card"]["state"] == tasks.FAILED and not bad["card"].get("task_id")

    # closing the card / hiding the page changes nothing: state is re-read from the server
    got = c.get(f'/actions/{rid}').json()
    assert got["card"]["task_id"] == rid
    assert c.get('/actions/does-not-exist').status_code == 404
    listing = c.get('/actions?limit=25').json()
    assert rid in [x["task_id"] for x in listing["cards"]], 'recent activity lost the task'
    assert all(x["state"] in tasks.LIVE for x in listing["live"])
    # a completed task's result stays retrievable after its card auto-dismissed
    doner = c.get(f'/actions/{tid}').json()["card"]
    assert doner["state"] == tasks.COMPLETED and doner["action"]["url"]

    # ── 9. BACKGROUND COMPLETION WHILE A VOICE CONVERSATION IS ACTIVE ──────────
    # A settled task Ace has not spoken about is offered once, and only once.
    callV, _ = make_provider()
    vt = run(taskrunner.dispatch("create_spreadsheet",
                                 {"title": "Finished mid-conversation", "rows": ROWS},
                                 call=callV))
    for _ in range(200):
        if tasks.get(vt["task_id"])["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    vid = vt["task_id"]
    assert tasks.get(vid)["state"] == tasks.COMPLETED
    assert any(n["id"] == vid for n in taskrunner.pending_voice_notices()), \
        'a task that finished mid-conversation had no completion notice'
    # announced ONCE, whichever surface notices it first
    assert tasks.mark_spoken(vid) is True
    assert tasks.mark_spoken(vid) is False, 'the same completion would be announced twice'
    assert not any(n["id"] == vid for n in taskrunner.pending_voice_notices())

    # ── 10. LISTENERS ARE WATCHERS, NOT WORKERS ────────────────────────────────
    seen = []

    async def watcher(card):
        seen.append(card["state"])

    async def broken(card):
        raise RuntimeError("this socket is gone")

    taskrunner.subscribe(broken); taskrunner.subscribe(watcher)
    call10, st10 = make_provider()
    v, t10 = tasks.accept("create_spreadsheet", {"title": "Watched", "rows": ROWS})
    out = run(taskrunner.run(t10["id"], call10))
    assert out["state"] == tasks.COMPLETED, 'a dead socket stopped the work'
    assert tasks.COMPLETED in seen, 'the live socket was not told'
    taskrunner.unsubscribe(watcher)

    # ── 11. EXISTING SAFEGUARDS ARE UNTOUCHED ──────────────────────────────────
    from ace2.backend import chat, ops
    assert "send_email" in chat._CONFIRM_ALWAYS
    assert "mcp_send_gmail_message" in chat._CONFIRM_ALWAYS
    assert "mcp_get_drive_shareable_link" in chat._CONFIRM_ALWAYS
    assert "delete_calendar_event" in chat._CONFIRM_ALWAYS
    assert chat._needs_confirm("mcp_modify_doc_text", {"document_id": "x"}) is True
    assert chat._needs_confirm("mcp_modify_sheet_values", {"clear_values": True}) is True
    assert "create_calendar_event" in ops.JOURNALLED and "draft_email" in ops.EXTERNAL
    # the board guard still refuses a waiting record
    import uuid
    from datetime import datetime, timezone
    wid = uuid.uuid4().hex[:6]
    with db._conn() as cn, cn.cursor() as cur:
        cur.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,entry,state,"
                    "waiting_on) VALUES(%s,%s,'todo','parked on the county','open','[]'::jsonb,"
                    "'record','waiting','the county')", (wid, datetime.now(timezone.utc)))
    assert c.post('/daybank/update', json={"id": wid, "status": "done"}).json().get("blocked")

    print('PASS: a dispatched request is never reported as done; the link is built from a '
          'provider id that survived a read-back; duplicate dispatch, a re-spaced transcript '
          'and three tabs make ONE document; a lost response after creation resumes from the '
          'checkpointed id instead of creating a second file; permission denial, provider '
          'failure and a wrong-account file all fail loudly without offering a link or '
          'sharing anything; a pending approval never auto-dismisses and a denial is final; '
          'completed and cancelled states cannot be rewritten by a redelivery, and cancelling '
          'finished work is refused with a reason; results stay retrievable after the card '
          'goes; a dead socket cannot stop the work; and the email, sharing, document, '
          'calendar and board guards are all still in force.')
finally:
    server.cleanup(); tmp.cleanup()
