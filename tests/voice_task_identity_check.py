"""Concurrent transcript extensions reuse one persisted job, even at the daily cap."""
import os, sys, tempfile
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ace2'))
import pgserver
from backend import db,tasks
with tempfile.TemporaryDirectory(prefix='ace-origin-') as temp:
    server=pgserver.get_server(Path(temp)/'data',cleanup_mode='delete')
    try:
        os.environ['DATABASE_URL']=server.get_uri()
        os.environ.pop('ANTHROPIC_API_KEY',None)
        db._init_schema();db._ready=True;tasks.ready()
        day=datetime.now(timezone.utc).date().isoformat()
        def accept(question):
            return tasks.accept('deep_dive',{'question':question},daily_cap=1,day=day,origin_key='same-spoken-turn')
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows=list(pool.map(accept,['Full sweep','Full sweep of bills, deals and follow-ups']))
        assert {v for v,t in rows}=={'created','existing'},rows
        assert len({t['id'] for v,t in rows})==1
        tid=rows[0][1]['id']
        tasks.failed(tid,'fixture interruption')
        v,t=accept('Expanded transcript of the same request')
        assert v=='existing' and t['id']==tid and t['state']==tasks.FAILED
        v,t=tasks.accept('deep_dive',{'question':'A separate request'},daily_cap=1,day=day,origin_key='new-turn')
        assert v=='over_cap'
        # This identity must never collapse unrelated create requests.
        a=tasks.accept('create_doc',{'title':'A'},origin_key='shared')[1]
        b=tasks.accept('create_doc',{'title':'B'},origin_key='shared')[1]
        assert a['id']!=b['id']
        print('PASS: concurrent voice extensions create one paid job; reuse survives cap/failure; new requests and creates stay distinct')
    finally:server.cleanup()
