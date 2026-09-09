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
    import uvicorn                                     # noqa: E402
    from ace2.backend.main import app                  # noqa: E402
    print('READY http://127.0.0.1:%d/' % PORT, flush=True)
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='error')
finally:
    server.cleanup(); tmp.cleanup()
