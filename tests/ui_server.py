"""Start the real app on a disposable Postgres with synthetic rows, for the browser check.

Prints the base URL, then serves until killed. No production URL, no credentials, no model
calls. Every row here is invented.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'ace2'))
import pgserver                                        # noqa: E402

PORT = int(os.environ.get('ACE_UI_PORT', '8791'))
tmp = tempfile.TemporaryDirectory(prefix='ace-ui-pg-')
server = pgserver.get_server(Path(tmp.name) / 'data', cleanup_mode='delete')
try:
    os.environ['DATABASE_URL'] = server.get_uri()
    os.environ['ACE2_ALLOW_OPEN'] = '1'
    os.environ.pop('ANTHROPIC_API_KEY', None)
    from ace2.backend import db                        # noqa: E402
    db._init_schema(); db._ready = True; db._trgm_ok = False

    import json as _j, uuid
    from datetime import datetime, timedelta, timezone
    T = datetime.now(db.EASTERN)
    today = T.strftime('%Y-%m-%d')
    soon = (T + timedelta(days=3)).strftime('%Y-%m-%d')
    late = (T - timedelta(days=2)).strftime('%Y-%m-%d')
    FIX = [
        ('Order concrete for the uncle’s Friday pour', ['Business'], today, 'action', None, None, 'Groundworks', None, None, None),
        ('Chase Rebecca for the signed packet', ['Deals'], soon, 'action', None, None, 'GFI/PFI', 'call her Tuesday', today, None),
        ('Send Thiami the wet-signature copy', ['Deals'], late, 'action', None, None, 'GFI/PFI', None, None, None),
        ('The Marlow deal', ['Deals'], None, 'record', None, None, 'GFI/PFI', None, None, None),
        ('Permit sign-off', ['Admin'], None, 'record', 'waiting', 'the county', 'Groundworks', None, soon, None),
        ('Website copy for the new page', ['Business'], None, 'action', None, None, 'Side Work', None, None, today),
        ('Unfiled thought from the truck', ['Business'], None, 'action', None, None, '', None, None, None),
        ('Sort the trailer registration', ['Admin'], None, 'action', None, None, 'Side Work', None, None, None),
        ('Draft the Groundworks flyer', ['Business'], None, 'action', None, None, 'Groundworks', None, None, None),
        ('Email Robin about her own deals', ['Deals'], None, 'action', None, None, 'GFI/PFI', None, None, None),
        ('Whether to keep the trailer', ['Business'], None, 'action', 'decide', None, 'Side Work', None, None, None),
        ('Book the dentist', ['Personal'], None, 'action', None, None, 'Personal', None, None, None),
        ('Ace: fix the voice write path', ['Tech'], None, 'action', None, None, 'Ace', None, None, None),
    ]
    # WHICH SIDE OF THE BOUNDARY (2026-09-09). _init_schema stamps the release-one boundary
    # when this database is created, so a row's ts decides whether it counts as old-board work.
    # The fixture needs BOTH: rows that predate it (which show up under "worth a look" and are
    # never suggested until reviewed) and rows that do not (ordinary ready work). Dating every
    # row in the past made the whole board legacy and the suggestion list empty.
    BOUND = datetime.fromisoformat(db.review_boundary())
    LEGACY = {'Unfiled thought from the truck', 'Book the dentist'}
    with db._conn() as c, c.cursor() as cur:
        for n, (text, tags, due, entry, state, wait, bucket, nxt, fup, chosen) in enumerate(FIX):
            ts = (BOUND - timedelta(days=2 + n) if text in LEGACY
                  else BOUND + timedelta(seconds=60 - n))
            cur.execute("INSERT INTO daybank_items(id,ts,kind,text,status,tags,due,entry,state,"
                        "waiting_on,bucket,next_step,followup,chosen_on) VALUES(%s,%s,'todo',%s,"
                        "'open',%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (uuid.uuid4().hex[:6], ts,
                         text, _j.dumps(tags), due, entry, state, wait, bucket, nxt, fup, chosen))
    # VOICE-TO-ACTION fixtures: a fake provider so the progress cards can be exercised end
    # to end without a Google connection, plus one card of each state to photograph.
    from ace2.backend import capabilities as _cp, tasks as _tasks, taskrunner as _tr
    _tasks.ready()
    _cp.EXPECTED_USER = "brady@example.com"
    _sheet = {}

    async def _fake(tool, args):
        import asyncio as _a, json as _jj
        await _a.sleep(0.4)          # visible "working" state
        if tool == "mcp_create_spreadsheet":
            return _jj.dumps({"spreadsheetId": "1DemoSheetIdAbCdEfGhIjKlMnOpQrStUvWx"})
        if tool == "mcp_modify_sheet_values":
            _sheet[args.get("range")] = args.get("values"); return '{"updatedCells": 9}'
        if tool == "mcp_read_sheet_values":
            return _jj.dumps({"values": _sheet.get(args.get("range"), [])})
        if tool.startswith("mcp_get_drive_file"):
            return _jj.dumps({"owners": [{"emailAddress": "brady@example.com"}]})
        return "(no content returned)"

    import ace2.backend.taskrunner as _trm
    _trm._provider = _fake            # the demo server never reaches a real provider

    from fastapi import Body          # noqa: E402
    from ace2.backend.main import app as _app

    _demo_n = [0]

    @_app.post("/demo/task")
    async def _demo_task(body: dict = Body(default={})):
        """Fixture-only: raise one card in a chosen state so both layouts can be seen."""
        kind = (body or {}).get("kind", "success")
        rows = [["Assistant", "Pricing model", "Monthly"],
                ["Northwind Helper", "flat", "$20"],
                ["Cedar Assist", "per-request", "$0.004"]]
        if kind == "success":
            # A distinct title per call: an identical request correctly returns the finished
            # task instead of building a second copy, which is the right behaviour and the
            # wrong fixture for photographing a fresh card.
            _demo_n[0] += 1
            name = ("Sample Assistant Pricing (fictional)" if _demo_n[0] == 1
                    else f"Sample Assistant Pricing (fictional) {_demo_n[0]}")
            return {"card": await _tr.dispatch(
                "create_spreadsheet", {"title": name, "rows": rows},
                origin="voice", title=name, call=_fake)}
        if kind == "failure":
            v, t = _tasks.accept("create_spreadsheet", {"title": "Q4 figures", "rows": rows})
            out = _tasks.failed(t["id"], "The provider refused to create it: 403 permission "
                                         "denied. Nothing was made.")
            await _tr._broadcast(out)
            return {"card": _tasks.card(out)}
        if kind == "approval":
            v, t = _tasks.accept("create_spreadsheet",
                                 {"title": "Share with Chris", "rows": rows})
            out = _tasks.needs_approval(t["id"], "rev-demo",
                                        "Sharing this outside your account needs your OK.")
            await _tr._broadcast(out)
            return {"card": _tasks.card(out)}
        v, t = _tasks.accept("create_spreadsheet", {"title": "Working demo", "rows": rows})
        out = _tasks.working(t["id"], "Writing the rows")
        await _tr._broadcast(out)
        return {"card": _tasks.card(out)}

    # main.py mounts the frontend at "/", and a mount swallows every path registered after
    # it — so a route added here lands behind the catch-all and 404s. Move it in front.
    _app.router.routes.insert(0, _app.router.routes.pop())

    import uvicorn                                     # noqa: E402
    from ace2.backend.main import app                  # noqa: E402
    print('READY http://127.0.0.1:%d/' % PORT, flush=True)
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='error')
finally:
    server.cleanup(); tmp.cleanup()
