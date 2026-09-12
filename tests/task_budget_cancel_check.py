"""Cancelled paid work retains its admission; paid retries cannot bypass the cap."""
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
import pgserver
from backend import db, tasks

with tempfile.TemporaryDirectory(prefix='ace-budget-cancel-') as temp:
    server = pgserver.get_server(Path(temp) / 'data', cleanup_mode='delete')
    try:
        os.environ['DATABASE_URL'] = server.get_uri()
        os.environ.pop('ANTHROPIC_API_KEY',None)
        db._init_schema(); db._ready=True; tasks.ready()
        day = datetime.now(timezone.utc).date().isoformat()
        v,t = tasks.accept('deep_dive', {'question':'never started'}, daily_cap=1, day=day)
        assert v=='created'
        assert tasks.request_cancel(t['id'])[0]=='cancelled'
        v,t = tasks.accept('deep_dive', {'question':'started'}, daily_cap=1, day=day)
        assert v=='created', 'unstarted cancellation should free capacity'
        assert tasks.claim(t['id'])
        tasks.cancelled_with_receipt(t['id'], {'reads_run':1}, 'Stopped after paid work')
        assert tasks.reserve_daily('deep_dive',1,day)==(False,1,1)
        v,_ = tasks.accept('deep_dive', {'question':'another'}, daily_cap=1, day=day)
        assert v=='over_cap', 'started cancellation must not refund spent capacity'
        _,failed = tasks.accept('research', {'query':'failed read'}, daily_cap=1, day=day)
        tasks.failed(failed['id'],'provider failure')
        assert tasks.retry(failed['id'])=={}, 'paid retries must be newly admitted'
        assert tasks.get(failed['id'])['state']==tasks.FAILED
        _,free = tasks.accept('create_doc', {'title':'free fixture'})
        tasks.failed(free['id'],'interrupted')
        assert tasks.retry(free['id'])['state']==tasks.QUEUED
        print('PASS: started cancellations remain budgeted, unstarted cancellations release capacity, paid retries require admission')
    finally:
        server.cleanup()
