"""Real disposable local Postgres. No production URL or Google backfill."""
import os,sys,tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ace2'))
import pgserver
from backend import db,review_store,planning
with tempfile.TemporaryDirectory(prefix='ace-pg-') as temp:
    server=pgserver.get_server(Path(temp)/'data',cleanup_mode='delete')
    try:
        with patch.dict(os.environ,{'DATABASE_URL':server.get_uri()}):
            db._init_schema();db._ready=True;db._trgm_ok=False
            ident=review_store.propose('send_email',{'to':'test@example.invalid','body':'reviewed'})
            assert review_store.propose('send_email',{'to':'test@example.invalid','body':'reviewed'})==ident
            def claim(_):
                try:return review_store.claim(ident,True)
                except ValueError:return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(claim,range(2)))
            assert sum(r is not None for r in results)==1,results
            review_store.finish(ident,'unknown','synthetic response')
            assert review_store.list_approvals()[0]['state']=='unknown'
            reject=review_store.propose('delete_calendar_event',{'event_id':'fake'})
            review_store.claim(reject,False)
            try:review_store.claim(reject,True);raise AssertionError('rejected replay')
            except ValueError:pass
            expired=review_store.propose('send_email',{'to':'expired@example.invalid'})
            with db._conn() as c,c.cursor() as cur:cur.execute("UPDATE ace_review SET expires_at=now()-interval '1 second' WHERE id=%s",(expired,))
            try:review_store.claim(expired,True);raise AssertionError('expired replay')
            except ValueError:pass
            review_store.append_plan('user','Plan my week, with family time on Monday.')
            assert 'family' in planning.context()
            ok,item=db.add_item('todo','Pay test bill $100',tags=['Bills']);assert ok
            ok,item2=db.add_item('todo','Pay test bill $200',tags=['Bills']);assert ok and item2['id']!=item['id']
            ok,result=db.update_item('',status='done',match='Pay other bill $100');assert not ok
            assert db.update_item(item['id'],status='done')[0]
            assert len(db.read_items(active_only=False))==2
            print('PASS: real Postgres schema, atomic competing claims, replay, rejection, expiry, notebook persistence, distinct obligations, guarded completion.')
    finally:server.cleanup()
