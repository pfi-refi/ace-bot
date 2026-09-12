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
    assert out["state"] == tasks.FAILED and "without a spreadsheet id" in out["error"]
    assert not (out.get("result") or {}).get("url"), 'a link was offered without an id'

    # ── 5. WRONG ACCOUNT: correct file, but Brady cannot open it ────────────────
    # A permission list that excludes him IS evidence he cannot open it.
    call7, st7 = make_provider(
        mcp_get_drive_file_metadata=json.dumps({
            "owners": [{"emailAddress": "svc@robot.iam"}],
            "permissions": [{"emailAddress": "svc@robot.iam"}]}),
        mcp_get_drive_file_info="Error: none")
    v, t7 = tasks.accept("create_spreadsheet", {"title": "Wrong owner", "rows": ROWS})
    out = run(taskrunner.run(t7["id"], call7))
    assert out["state"] == tasks.FAILED, out
    assert "file does not exist" in out["error"], out["error"]
    assert "mcp_get_drive_shareable_link" not in st7["calls"], 'it tried to share its way out'

    # A different owner with NO permission list proves nothing either way (Codex): report
    # unknown and warn, rather than calling a shared file inaccessible.
    call7b, _ = make_provider(
        mcp_get_drive_file_metadata=json.dumps({"owners": [{"emailAddress": "svc@robot.iam"}]}),
        mcp_get_drive_file_info="Error: none")
    v, t7b = tasks.accept("create_spreadsheet", {"title": "Owner unknown", "rows": ROWS})
    out7b = run(taskrunner.run(t7b["id"], call7b))
    assert out7b["state"] == tasks.COMPLETED, out7b
    assert out7b["result"]["access"] == "unknown"
    assert any("cannot promise" in w for w in out7b["result"]["warnings"])

    # ...and a file OWNED by someone else but shared with him is simply fine.
    call7c, _ = make_provider(
        mcp_get_drive_file_metadata=json.dumps({
            "owners": [{"emailAddress": "svc@robot.iam"}],
            "permissions": [{"emailAddress": "svc@robot.iam"},
                            {"emailAddress": "brady@example.com"}]}),
        mcp_get_drive_file_info="Error: none")
    v, t7c = tasks.accept("create_spreadsheet", {"title": "Shared with me", "rows": ROWS})
    out7c = run(taskrunner.run(t7c["id"], call7c))
    assert out7c["state"] == tasks.COMPLETED and out7c["result"]["access"] == "ok"

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

    # ── 12. THE TRUE LOST-CREATE-RESPONSE CASE (Codex defect 2) ────────────────
    # The provider CREATES the file and then the response is lost. The old checkpoint only
    # existed after a response came back, so the next attempt created a second document.
    made_files = {"n": 0}

    def create_then_vanish(args, state):
        made_files["n"] += 1
        raise asyncio.TimeoutError("response lost after the remote create")

    callL, _ = make_provider(mcp_create_spreadsheet=create_then_vanish)
    LOST = {"title": "Lost create response", "rows": ROWS}
    l1 = run(taskrunner.dispatch("create_spreadsheet", LOST, call=callL))
    for _ in range(200):
        if tasks.get(l1["task_id"])["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    t_l1 = tasks.get(l1["task_id"])
    assert t_l1["state"] == tasks.FAILED
    assert t_l1["result"].get("create_state") == "unknown", t_l1["result"]
    assert "may or may not" in t_l1["error"], t_l1["error"]
    # Re-asking must NOT create again — it must stop and ask.
    l2 = run(taskrunner.dispatch("create_spreadsheet", LOST, call=callL))
    for _ in range(200):
        if tasks.get(l2["task_id"])["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    assert made_files["n"] == 1, f'a re-ask created {made_files["n"]} documents'
    assert tasks.get(l2["task_id"])["state"] == tasks.FAILED
    assert "will not create another" in tasks.get(l2["task_id"])["error"].lower() \
        or "not created another" in tasks.get(l2["task_id"])["error"].lower(), \
        tasks.get(l2["task_id"])["error"]

    # ...and the protection SURVIVES the dedup window closing.
    with db._conn() as cn, cn.cursor() as cur:
        cur.execute("UPDATE ace_tasks SET created_at = now() - interval '3 hours' "
                    "WHERE id IN %s", ((l1["task_id"], l2["task_id"]),))
    l3 = run(taskrunner.dispatch("create_spreadsheet", LOST, call=callL))
    for _ in range(200):
        if tasks.get(l3["task_id"])["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    assert made_files["n"] == 1, \
        f'the unresolved create was forgotten once the dedup window closed ({made_files["n"]})'

    # A title match alone is a CANDIDATE, not identity — it must still refuse to guess.
    callR, _ = make_provider(
        mcp_create_spreadsheet=create_then_vanish,
        mcp_search_drive_files=json.dumps({"files": [
            {"id": "1CandidateAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
            {"id": "1CandidateBBBBBBBBBBBBBBBBBBBBBBBBBBB"}]}))
    AMB = {"title": "Ambiguous name", "rows": ROWS}
    r1 = run(taskrunner.dispatch("create_spreadsheet", AMB, call=callR))
    for _ in range(200):
        if tasks.get(r1["task_id"])["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    r2 = run(taskrunner.dispatch("create_spreadsheet", AMB, call=callR))
    for _ in range(200):
        if tasks.get(r2["task_id"])["state"] in tasks.TERMINAL:
            break
        run(asyncio.sleep(0.02))
    err2 = tasks.get(r2["task_id"])["error"]
    assert "guess" in err2.lower() or "tell me" in err2.lower(), err2

    # ── 13. A CHECKPOINT THAT WILL NOT WRITE STOPS THE CREATE (fail closed) ────
    import ace2.backend.tasks as _tmod
    real_cp = _tmod.checkpoint
    _tmod.checkpoint = lambda *a, **k: {}          # every checkpoint write fails
    try:
        callC, stC = make_provider()
        v, tC = tasks.accept("create_spreadsheet", {"title": "No journal", "rows": ROWS})
        outC = run(taskrunner.run(tC["id"], callC))
        assert outC["state"] == tasks.FAILED
        assert "could not record" in outC["error"], outC["error"]
        assert stC["created"] == 0, 'it created something it could not keep track of'
    finally:
        _tmod.checkpoint = real_cp

    # ── 14. CANCELLATION AT EVERY BOUNDARY (Codex defect 3) ────────────────────
    # (a) before dispatch — nothing exists, and nothing is created
    callX, stX = make_provider()
    v, tX = tasks.accept("create_spreadsheet", {"title": "Stop before", "rows": ROWS})
    # claim first so the row is WORKING, then ask it to stop — this exercises the handler's
    # own boundary check rather than the queued short-circuit. run() would refuse a second
    # claim, so the executor is called directly.
    tasks.claim(tX["id"]); tasks.request_cancel(tX["id"])
    outX = run(taskrunner._execute(tX["id"], callX))
    assert outX["state"] == tasks.CANCELLED and stX["created"] == 0

    # (b) DURING the create — the file lands, the write must NOT, and the receipt is kept
    gate = {"stop_after_create": None}

    def slow_create(args, state):
        state["created"] += 1
        gate["stop_after_create"]()               # the stop arrives mid-call
        return json.dumps({"spreadsheetId": "1MidCreateAAAAAAAAAAAAAAAAAAAAAAAAAA"})

    callY, stY = make_provider(mcp_create_spreadsheet=slow_create)
    v, tY = tasks.accept("create_spreadsheet", {"title": "Stop during", "rows": ROWS})
    gate["stop_after_create"] = lambda: tasks.request_cancel(tY["id"])
    tasks.claim(tY["id"])
    outY = run(taskrunner._execute(tY["id"], callY))
    assert outY["state"] == tasks.CANCELLED, outY
    assert "mcp_modify_sheet_values" not in stY["calls"], \
        'it kept writing after being told to stop'
    assert outY["result"].get("file_id") == "1MidCreateAAAAAAAAAAAAAAAAAAAAAAAAAA", \
        f'cancellation lost the receipt for a file that exists: {outY["result"]}'
    cardY = tasks.card(outY)
    assert "exists" in (cardY["detail"] or "").lower(), cardY["detail"]
    assert stY["created"] == 1

    # (c) a running task reports STOPPING, never cancelled, until it settles
    v, tZ = tasks.accept("create_spreadsheet", {"title": "Stopping label", "rows": ROWS})
    tasks.claim(tZ["id"])
    verdict, tz = tasks.request_cancel(tZ["id"])
    assert verdict == "requested"
    cz = tasks.card(tasks.get(tZ["id"]))
    assert cz["state"] == tasks.WORKING and cz["stopping"] is True, cz
    pend = run(taskrunner.cancel(tZ["id"]))
    assert pend.get("cancel_pending") is True and pend["state"] != tasks.CANCELLED

    # (d) a queued task stops outright, because nothing has happened
    v, tQ = tasks.accept("create_spreadsheet", {"title": "Stop queued", "rows": ROWS})
    assert tasks.request_cancel(tQ["id"])[0] == "cancelled"

    # ── 15. WRONG VALUES DO NOT PASS VERIFICATION ─────────────────────────────
    callW, _ = make_provider(
        mcp_read_sheet_values=json.dumps({"values": [["Assistant", "Pricing model", "Monthly"],
                                                     ["Northwind Helper", "flat", "$999"],
                                                     ["Cedar Assist", "per-request", "$0.004"]]}))
    v, tW = tasks.accept("create_spreadsheet", {"title": "Wrong figure", "rows": ROWS})
    outW = run(taskrunner.run(tW["id"], callW))
    assert outW["state"] == tasks.FAILED, 'a wrong money figure verified clean'
    assert "C2" in outW["error"], outW["error"]
    assert tasks.card(outW)["action"] is None, 'a wrong sheet still offered a link'

    # ── 16. THE PAID DAILY CAP IS ENFORCED, NOT ADVERTISED ─────────────────────
    # It used to be a number in the inventory that no execution path read.
    from ace2.backend import connectors as _cn
    _was_cap = _cn.CONNECTORS["web_research"]["daily_task_cap"]
    _cn.CONNECTORS["web_research"]["daily_task_cap"] = 2

    class _FakeClient:
        def __init__(self): self.messages, self.n = self, 0
        async def create(self, **kw):
            self.n += 1
            cite = lambda u: type("X", (), {"url": u, "title": u})()
            blk = type("B", (), {"text": "an answer", "citations": [cite("https://a.example")]})()
            return type("R", (), {"content": [blk], "usage": type("U", (), {
                "server_tool_use": type("S", (), {"web_search_requests": 1})()})()})()

    _client = _FakeClient()
    import ace2.backend.capabilities as _capmod
    _real_research = _capmod.REGISTRY["research"]["handler"]

    async def _research_with_fake(a, call, progress=None, known=None, checkpoint=None,
                                  should_stop=None):
        return await _real_research({**a, "_client": _client}, call, progress, known,
                                    checkpoint, should_stop)

    _capmod.REGISTRY["research"]["handler"] = _research_with_fake
    try:
        cards = []
        for i in range(4):
            cards.append(run(taskrunner.dispatch("research", {"question": f"q{i}?"})))
            for _ in range(200):
                tid_ = cards[-1].get("task_id")
                if not tid_ or tasks.get(tid_)["state"] in tasks.TERMINAL:
                    break
                run(asyncio.sleep(0.02))
        admitted = [c for c in cards if c.get("task_id")]
        refused = [c for c in cards if not c.get("task_id")]
        assert len(admitted) == 2, f'the cap admitted {len(admitted)}, expected 2'
        assert len(refused) == 2, f'{len(refused)} refusals, expected 2'
        assert "daily limit" in refused[0]["error"], refused[0]["error"]
        assert refused[0]["sticky"] is True and refused[0]["auto_dismiss_ms"] == 0
        assert _client.n == 2, f'the provider was called {_client.n} times past a cap of 2'

        # ...and a REFUSAL leaves no task row pretending to be work
        rows_now = [t for t in tasks.recent(50) if t["capability"] == "research"]
        assert len(rows_now) == 2, f'{len(rows_now)} research rows for 2 admitted tasks'

        # RACE: three simultaneous requests at a cap of 3 with 2 already used must admit ONE.
        _cn.CONNECTORS["web_research"]["daily_task_cap"] = 3
        raced = run(asyncio.gather(
            taskrunner.dispatch("research", {"question": "race a?"}),
            taskrunner.dispatch("research", {"question": "race b?"}),
            taskrunner.dispatch("research", {"question": "race c?"})))
        got = [c for c in raced if c.get("task_id")]
        assert len(got) == 1, f'{len(got)} admitted in a race with one slot left'
    finally:
        _capmod.REGISTRY["research"]["handler"] = _real_research
        _cn.CONNECTORS["web_research"]["daily_task_cap"] = _was_cap

    # ── 14. A DEEP DIVE IS A TASK, END TO END ─────────────────────────────────
    # The long-voice-turn fix. A wide question must reach a real row, complete off the turn,
    # produce something to READ, and be offered to the conversation exactly once.
    import types as _types

    class _ScriptedModel:
        """Two rounds: one read, then the answer. No network, no key, no spend."""

        def __init__(self):
            self.rounds = [
                [_types.SimpleNamespace(type="tool_use", name="recall",
                                        input={"query": "chris"}, id="tu_1")],
                [_types.SimpleNamespace(type="text",
                                        text="Chris is next. Ken is still waiting on you.")],
            ]
            self.messages = _types.SimpleNamespace(create=self._create)

        async def _create(self, **kw):
            blocks = self.rounds.pop(0) if self.rounds else [
                _types.SimpleNamespace(type="text", text="done")]
            return _types.SimpleNamespace(content=blocks)

    _real_dive = _capmod.REGISTRY["deep_dive"]["handler"]

    async def _scripted_dive(args, call, progress=None, known=None,
                             checkpoint=None, should_stop=None):
        return await _real_dive({**args, "_client": _ScriptedModel()}, call, progress,
                                known, checkpoint, should_stop)

    _capmod.REGISTRY["deep_dive"]["handler"] = _scripted_dive
    _was_dive_cap = _capmod.REGISTRY["deep_dive"]["daily_cap"]
    try:
        dv = run(taskrunner.dispatch("deep_dive", {"question": "how does my week line up?"}))
        did = dv["task_id"]
        for _ in range(300):
            if tasks.get(did)["state"] in tasks.TERMINAL:
                break
            run(asyncio.sleep(0.02))
        row = tasks.get(did)
        assert row["state"] == tasks.COMPLETED, f'deep dive ended {row["state"]}: {row["error"]}'
        assert "Chris is next" in (row["result"] or {}).get("answer", ""), \
            'the deep dive completed without an answer to show him'
        dcard = tasks.card(row)
        assert dcard["answer"] and dcard["title"] == "Deep dive"
        assert dcard["auto_dismiss_ms"] == 0 and dcard["sticky"], \
            'a briefing he is meant to read would have vanished in nine seconds'
        # ...and it is offered to the live conversation once, then never again.
        assert any(n["id"] == did for n in taskrunner.pending_voice_notices()), \
            'a finished deep dive never reached the conversation'
        assert tasks.mark_spoken(did) is True
        assert not any(n["id"] == did for n in taskrunner.pending_voice_notices())

        # THE CAP IS ADMITTED, NOT DECLARED. deep_dive has no connector, so its limit lives on
        # the registry entry — the whole point of the taskrunner change. Prove dispatch reads it.
        _capmod.REGISTRY["deep_dive"]["daily_cap"] = 1
        blocked = run(taskrunner.dispatch("deep_dive", {"question": "a different question?"}))
        assert not blocked.get("task_id"), 'the deep dive daily cap admitted one too many'
        assert "ACE2_DEEP_DIVE_DAILY_CAP" in blocked["error"], \
            f'the refusal names the wrong environment variable: {blocked["error"]}'
    finally:
        _capmod.REGISTRY["deep_dive"]["handler"] = _real_dive
        _capmod.REGISTRY["deep_dive"]["daily_cap"] = _was_dive_cap

    print('PASS: a dispatched request is never reported as done; the link is built from a '
          'provider id that survived a read-back; duplicate dispatch, a re-spaced transcript '
          'and three tabs make ONE document; a lost response after creation resumes from the '
          'checkpointed id instead of creating a second file; permission denial, provider '
          'failure and a wrong-account file all fail loudly without offering a link or '
          'sharing anything; a pending approval never auto-dismisses and a denial is final; '
          'completed and cancelled states cannot be rewritten by a redelivery, and cancelling '
          'finished work is refused with a reason; results stay retrievable after the card '
          'goes; a dead socket cannot stop the work; and the email, sharing, document, '
          'calendar and board guards are all still in force; a lost create response is '
          'never retried into a second document and survives the dedup window; a checkpoint '
          'that will not write stops the create; cancellation before, during and after the '
          'create reports stopping rather than cancelled and keeps the receipt for anything '
          'that already existed; a wrong cell value fails verification by coordinate; and the '
          'paid daily cap admits exactly its limit, refuses the rest without creating a task, '
          'and holds under three simultaneous requests for one remaining slot; and a deep '
          'dive runs off the turn to a real row, produces an answer that stays on screen, '
          'reaches the conversation exactly once, and is refused past its own daily cap.')
finally:
    server.cleanup(); tmp.cleanup()
