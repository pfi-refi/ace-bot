"""Offline regressions. Extract actual functions without app startup or live credentials."""
import ast
import asyncio
import importlib.util
import json
import logging
from pathlib import Path
import re
import sys
import types
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'ace2'))
from backend import db, review_store, planning


def extract(path, names, env):
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),env)
    return env


class FakeCursor:
    def __init__(self): self.writes=[]; self.row=('saved',)
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def cursor(self): return self
    def execute(self,sql,args=None): self.writes.append((sql,args))
    def fetchone(self): return self.row


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.rows=[];self.cursor=FakeCursor()
        self.patches=[patch.object(db,'ensure_ready'),patch.object(db,'read_items',lambda **kw:self.rows),patch.object(db,'_conn',lambda:self.cursor),patch.object(db,'_trgm_ok',False),patch.object(db,'_DUP_JUDGE',None)]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
    def existing(self,text,**kwargs):
        self.rows[:]=[dict(id='old',text=text,status='open',tags=['Bills'],parent_id=None,**kwargs)]
    def add(self,text,**kwargs):return db.add_item('todo',text,tags=['Bills'],**kwargs)
    def test_exact_repeat(self):
        self.existing('Call Ken about filing');ok,r=self.add('Call Ken about filing');self.assertTrue(ok);self.assertTrue(r['dup']);self.assertFalse(self.cursor.writes)
    def test_amounts_never_merge(self):
        self.existing('Pay electric bill $100');ok,r=self.add('Pay electric bill $200');self.assertTrue(ok);self.assertNotIn('dup',r);self.assertIn('200',r['text'])
    def test_new_information_is_not_claimed_saved(self):
        self.existing('Call Ken about filing');ok,r=self.add('Call Ken about filing with updated documents');self.assertFalse(ok);self.assertIn('NOT saved',r);self.assertFalse(self.cursor.writes)
    def test_new_occurrence_after_completion(self):
        self.existing('Call Ken about filing',due='2026-09-08');self.rows[0]['status']='done'
        ok,r=self.add('Call Ken about filing',due='2026-10-08');self.assertTrue(ok);self.assertNotIn('dup',r)
    def test_changed_due_requires_review(self):
        self.existing('Call Ken about filing',due='2026-09-08');ok,r=self.add('Call Ken about filing',due='2026-09-09');self.assertFalse(ok);self.assertIn('REVIEW',r)
    def test_wrong_person_cannot_complete(self):
        self.existing('Call Damon about website');ok,r=db.update_item('',status='done',match='Call Ken about website');self.assertFalse(ok);self.assertIn('AMBIGUOUS',r);self.assertFalse(self.cursor.writes)
    def test_exact_text_completion_still_works(self):
        self.existing('Call Damon about website');ok,r=db.update_item('',status='done',match='Call Damon about website');self.assertTrue(ok)
    def test_direct_id_completion_still_works(self):
        ok,r=db.update_item('old',status='done');self.assertTrue(ok)
    def test_semantic_judge_cannot_discard_details(self):
        self.existing("Sienna aunt signature packet sent awaiting return")
        with patch.object(db,'_DUP_JUDGE',lambda text,cands:'old'):
            ok,r=self.add("Sienna aunt packet still pending no push needed")
        self.assertFalse(ok);self.assertIn('NOT saved',r)
    def test_parent_action_is_not_parent_record(self):
        self.existing('Mail Rebecca packet');ok,r=self.add('Mail Rebecca packet',parent_id='record');self.assertTrue(ok);self.assertNotIn('dup',r)


class ApprovalTests(unittest.TestCase):
    def test_conversation_cannot_authorize(self):
        env=extract(ROOT/'ace2/backend/chat.py',{'_strip_confirm','_needs_confirm','_confirm_gate'},dict(json=json,_CONFIRM_ALWAYS={'send_email','delete_calendar_event'},_DESTRUCTIVE_ACTIONS={'delete','clear'},_DESTRUCTIVE_HINTS=('delete','clear'),_SHARE_UPDATES={'all'}))
        for tool in ('send_email','delete_calendar_event'):
            for words in ('Yes','No, do not send it','No, do not delete it','Go ahead','What time is it?'):
                env['_turn_user_text']=[words]
                blocked,args=env['_confirm_gate'](tool,{'confirmed':True,'target':'other'},2)
                self.assertTrue(blocked);self.assertNotIn('confirmed',args)
        self.assertFalse(env['_confirm_gate']('read_gmail',{},2)[0])
    def test_claim_replay_rejected(self):
        c=FakeCursor();c.row=None
        with patch.object(review_store,'ready'),patch.object(db,'_conn',lambda:c):
            with self.assertRaises(ValueError):review_store.claim('old',True)
        sql=c.writes[0][0];self.assertIn("state='pending'",sql);self.assertIn('expires_at>now()',sql)
    def test_claim_uses_persisted_payload(self):
        c=FakeCursor();c.row=({'tool':'send_email','args':{'to':'reviewed@example.invalid'}},)
        with patch.object(review_store,'ready'),patch.object(db,'_conn',lambda:c):
            self.assertEqual(review_store.claim('id',True)['args']['to'],'reviewed@example.invalid')
    def test_store_unavailable_fails_closed(self):
        with patch.object(db,'enabled',lambda:False):
            with self.assertRaises(RuntimeError):review_store.propose('send_email',{})


class StaticTests(unittest.TestCase):
    def test_public_assets_and_private_paths(self):
        from backend.public_assets import PublicAssets
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.testclient import TestClient
        app=Starlette(routes=[Mount('/',app=PublicAssets(directory=str(ROOT/'ace2')))])
        with TestClient(app) as client:
            for path in ('app.js','styles.css','sw.js','manifest.json','review.js','review.css','icon-192.png'):
                self.assertEqual(client.get('/'+path).status_code,200,path)
            for path in ('backend/chat.py','backend/main.py','.env.example','requirements.txt','README.md','../backend/db.py','%2e%2e/backend/db.py'):
                self.assertEqual(client.get('/'+path).status_code,404,path)


class PlanningTests(unittest.TestCase):
    def test_full_draft_and_later_correction_survive(self):
        draft='Monday: family. Tuesday: call Ken. Friday: tentative pour. '+('Detailed work. '*40)
        entries=[dict(role='assistant',text=draft),dict(role='user',text='Correction: Ken is Wednesday, not Tuesday.')]
        with patch.object(review_store,'read_plan',lambda:entries):
            context=planning.context()
        self.assertIn(draft,context);self.assertIn('Correction: Ken is Wednesday',context);self.assertIn('NOT proof',context)
    def test_start_captured_without_model(self):
        with patch.object(review_store,'read_plan',lambda:[]),patch.object(review_store,'append_plan') as append:
            planning.capture('user','Please plan my week')
            append.assert_called_once_with('user','Please plan my week')


class BridgeTests(unittest.TestCase):
    def test_failed_auth_never_claims(self):
        spec=importlib.util.spec_from_file_location('bridge_test',ROOT/'ops/bridge_worker.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            cfg=Path(temp)/'config.json';cfg.write_text('{}');m.CONFIG=str(cfg)
            with patch.object(m,'find_claude',lambda cfg:'/fake/claude'),patch.object(m,'log'),patch.object(m.subprocess,'run',return_value=types.SimpleNamespace(returncode=1,stdout='{"loggedIn":false}')),patch.object(m,'api') as api:
                self.assertEqual(m.main(),1);api.assert_not_called()

if __name__=='__main__':unittest.main()

class BillsTests(unittest.TestCase):
    def test_emergency_notes_and_explicit_unpaid(self):
        from backend.integrations.bills_sheet import parse_bills, format_due_soon
        from datetime import date
        rows=[['Bill / Expense','Due Day','Monthly Amount','Paid?','Paid From','Notes'],['Electric stop disconnection','','$247.79','No','','Due September 14; reconnect $35']]
        bills=parse_bills(rows,date(2026,9,7));self.assertFalse(bills[0]['paid']);self.assertEqual(bills[0]['due_on'],date(2026,9,14));self.assertIn('$247.79',format_due_soon(bills,today=date(2026,9,7)))
    def test_reordered_columns(self):
        from backend.integrations.bills_sheet import parse_bills
        rows=[['Monthly Amount','Paid?','Bill / Expense','Due Day'],['$106','false','Capital One','8th']]
        self.assertEqual(parse_bills(rows)[0]['amount'],106);self.assertFalse(parse_bills(rows)[0]['paid'])

class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from backend import main
        from fastapi.testclient import TestClient
        cls.main=main;cls.client=TestClient(main.app) # no context => no background lifespan
    def test_review_requires_auth(self):
        with patch.object(self.main,'auth_enabled',lambda:True),patch.object(self.main,'locked',lambda:False):
            self.assertEqual(self.client.get('/reviews').status_code,401)
    def test_reject_never_calls_executor(self):
        from backend import tools
        self.main.app.dependency_overrides[self.main.require_auth]=lambda:None
        try:
            with patch.object(review_store,'claim',return_value={'tool':'send_email','args':{}}),patch.object(tools,'execute') as execute:
                self.assertEqual(self.client.post('/reviews/id',json={'approve':False}).json()['state'],'rejected');execute.assert_not_called()
        finally:self.main.app.dependency_overrides.clear()
    def test_execute_uses_saved_payload_and_preserves_failure(self):
        from backend import tools
        self.main.app.dependency_overrides[self.main.require_auth]=lambda:None
        try:
            with patch.object(review_store,'claim',return_value={'tool':'send_email','args':{'to':'reviewed@example.invalid'}}),patch.object(review_store,'finish') as finish,patch.object(tools,'execute',return_value='⚠️ Mail failed') as execute:
                r=self.client.post('/reviews/id',json={'approve':True,'args':{'to':'wrong@example.invalid'}})
                execute.assert_called_once_with('send_email',{'to':'reviewed@example.invalid'});self.assertEqual(r.json()['state'],'failed');finish.assert_called_once()
        finally:self.main.app.dependency_overrides.clear()

class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_survives_new_voice_session(self):
        from backend import chat
        from unittest.mock import AsyncMock
        text='Monday family. Tuesday follow up. Friday tentative work. '+('Detailed plan. '*35).strip()
        entries=[]
        class Stream:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            def __aiter__(self):
                async def events():
                    yield types.SimpleNamespace(type='content_block_delta',delta=types.SimpleNamespace(type='text_delta',text=text))
                return events()
            async def get_final_message(self):return types.SimpleNamespace(content=[],stop_reason='end_turn',usage=None)
        received=[]
        client=types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **kw:(received.append(kw) or Stream())))
        def append(role,text):entries.append(dict(role=role,text=text,saved_at=datetime.now(timezone.utc).isoformat()))
        async def emit(*args):pass
        with patch.object(chat,'_anthropic',lambda:client),patch.object(chat,'_fast_context',AsyncMock(return_value='test context')),patch.object(chat,'_load_messages',AsyncMock(return_value=[{'role':'user','content':'plan my week'}])),patch.object(chat,'build_system_prompt',lambda:'Test system'),patch.object(chat,'maybe_toggle_privacy'),patch.object(chat.history,'append'),patch.object(chat,'_ttl_ok',[False]),patch.object(review_store,'read_plan',lambda:entries.copy()),patch.object(review_store,'append_plan',append):
            await chat.stream_turn('Plan my week',emit,prior=[],fast=True)
            self.assertTrue(any(e['text']==text for e in entries),repr(entries))
            await chat.stream_turn('Continue where we stopped',emit,prior=[],fast=True)
            self.assertIn(text,received[-1]['system'][-1]['text'])
