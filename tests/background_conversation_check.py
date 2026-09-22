"""Real task lifecycle + real voice SSE adapter while work runs; disposable Postgres.
Only the model/provider responses are fictional. No production or paid calls.
"""
import asyncio,os,sys,tempfile
from pathlib import Path
from unittest.mock import AsyncMock,patch
import pgserver
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ace2'))

async def check():
    from backend import db,tasks,taskrunner,capabilities,main
    db._init_schema();db._ready=True;tasks.ready()
    started,release=asyncio.Event(),asyncio.Event()
    executions=[]
    async def handler(args,call,progress,known,checkpoint,should_stop):
        executions.append(args['question'])
        started.set()
        await release.wait()
        await progress('Final verification')
        return {'answer':'Verified fictional report','reads_run':1}
    async def conversation(text,emit,**kwargs):
        assert not release.is_set()
        await emit('delta',{'text':'We can keep talking while the report runs.'})
        await emit('final',{})
    with patch.dict(capabilities.REGISTRY,{'deep_dive':{'handler':handler,'title':'Fixture report'}}), \
         patch.object(main,'_llm_authorized',return_value=True), \
         patch.object(main.chat,'stream_turn',side_effect=conversation):
        card=await taskrunner.dispatch('deep_dive',{'question':'Fictional report'},origin='voice',origin_key='fictional-turn')
        tid=card['task_id'] if 'task_id' in card else card['id']
        await asyncio.wait_for(started.wait(),2)
        assert tasks.get(tid)['state']==tasks.WORKING
        # Actual voice adapter yields a full second turn while the task is deliberately blocked.
        request=AsyncMock();request.json.return_value={'messages':[{'role':'user','content':'Can we keep talking?'}]}
        response=await main.openai_compat(request,authorization='fixture')
        chunks=await asyncio.wait_for(collect(response.body_iterator),2)
        assert 'We can keep talking' in ''.join(chunks)
        assert tasks.get(tid)['state']==tasks.WORKING
        # The voice response has ended; the server-owned job still exists and settles.
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(taskrunner._bg)),2)
        result=tasks.get(tid)
        assert result['state']==tasks.COMPLETED,result
        assert result['result']['answer']=='Verified fictional report'
        assert executions==['Fictional report']
        await taskrunner.recover_once()
        assert executions==['Fictional report']
        print('PASS: voice turn completes during background work; task survives voice end; verified result persists; recovery does not replay it.')

async def collect(iterator):return [part async for part in iterator]
with tempfile.TemporaryDirectory(prefix='ace-concurrent-') as tmp:
    server=pgserver.get_server(Path(tmp)/'data',cleanup_mode='delete')
    os.environ['DATABASE_URL']=server.get_uri();os.environ.pop('ANTHROPIC_API_KEY',None)
    try:asyncio.run(check())
    finally:server.cleanup()
