"""Real row-lock cancellation/claim race; no external calls or production data."""
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
import pgserver
from backend import db, tasks

with tempfile.TemporaryDirectory(prefix='ace-cancel-race-') as temp:
    server = pgserver.get_server(Path(temp) / 'data', cleanup_mode='delete')
    try:
        os.environ['DATABASE_URL'] = server.get_uri()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        db._init_schema(); db._ready = True; tasks.ready()
        task = tasks.accept('fixture', {'title':'claim wins'})[1]
        with ThreadPoolExecutor(max_workers=1) as workers:
            with db._conn() as conn, conn.cursor() as cur:
                cur.execute('SELECT id FROM ace_tasks WHERE id=%s FOR UPDATE', (task['id'],))
                waiting = workers.submit(tasks.request_cancel, task['id'])
                time.sleep(.1)
                assert not waiting.done(), 'cancel should wait for owner of row lock'
                # The worker claims while cancellation is waiting on that very row.
                cur.execute("UPDATE ace_tasks SET state=%s, attempts=1 WHERE id=%s", (tasks.WORKING,task['id']))
            verdict, row = waiting.result(timeout=3)
        assert verdict == 'requested', (verdict,row)
        assert row['state'] == tasks.WORKING and row['cancel_requested']
        # Any side effect already made must remain recordable after the stop request.
        tasks.cancelled_with_receipt(task['id'], {'file_id':'created-before-stop'}, 'Stopped after create')
        assert tasks.get(task['id'])['result']['file_id'] == 'created-before-stop'
        task2 = tasks.accept('fixture', {'title':'cancel wins'})[1]
        verdict, row = tasks.request_cancel(task2['id'])
        assert verdict == 'cancelled' and row['state'] == tasks.CANCELLED
        assert not tasks.claim(task2['id'])
        assert tasks.request_cancel(task2['id'])[0] == 'too_late'
        print('PASS: cancel racing a claim flags running work, preserves receipts, and prevents unstarted work')
    finally:
        server.cleanup()
