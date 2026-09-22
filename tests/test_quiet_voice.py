"""Drive the actual voice SSE adapter: quiet progress must not silence results/errors."""
import asyncio,json,unittest
from unittest.mock import AsyncMock,patch
from ace2.backend import main
from backend import chat

class QuietVoice(unittest.IsolatedAsyncioTestCase):
    async def drive(self, delayed=False, error=False, late=False):
        ready=asyncio.Event()
        if not delayed:ready.set()
        ticks=[];hud=[];late_ticks=[];late_ready=asyncio.Event()
        original_wait=asyncio.wait_for
        async def wait(awaitable, timeout):
            # Accelerate only the SSE queue's initial timeout ticks, not task completion.
            if delayed and getattr(getattr(awaitable,'cr_code',None),'co_name','')=='get' and len(ticks)<2:
                ticks.append(timeout);awaitable.close()
                if len(ticks)==2:ready.set()
                await asyncio.sleep(0)
                raise asyncio.TimeoutError()
            if late and timeout==4.0 and getattr(getattr(awaitable,'cr_code',None),'co_name','')=='get' and len(late_ticks)<2:
                late_ticks.append(timeout);awaitable.close()
                if len(late_ticks)==2:late_ready.set()
                await asyncio.sleep(0)
                raise asyncio.TimeoutError()
            return await original_wait(awaitable,timeout)
        async def turn(text,emit,**kwargs):
            await ready.wait()
            if late:
                await emit('delta',{'text':'I can check that. '})
                await late_ready.wait()
            for tool in ('recall','search_drive','get_calendar_range'):
                await emit('tool',{'name':tool,'status':'running','label':'checking a private source'})
                await emit('tool',{'name':tool,'status':'done'})
            if error:
                await emit('error',{'code':'billing','text':'The API balance needs attention.'})
            else:
                await emit('delta',{'text':'Your appointment is at three.'})
        async def stage(kind,payload):hud.append((kind,payload))
        req=AsyncMock();req.json.return_value={'messages':[{'role':'user','content':'Check my appointment'}]}
        with patch.object(main,'_llm_authorized',return_value=True), \
             patch.object(main.chat,'stream_turn',turn), \
             patch.object(main,'publish_stage_event',stage), \
             patch.object(main.asyncio,'wait_for',wait):
            response=await main.openai_compat(req,authorization='fixture')
            pieces=[p async for p in response.body_iterator]
        content=[]
        for piece in pieces:
            if piece.startswith('data: {'):
                content.append(json.loads(piece[6:])['choices'][0]['delta'].get('content',''))
        return content,hud,ticks

    async def test_three_tools_produce_one_ack_then_outcome_and_keep_visual_receipts(self):
        content,hud,_=await self.drive()
        text=''.join(content)
        self.assertEqual(text.count('One moment'),1)
        self.assertIn('Your appointment is at three.',text)
        self.assertNotIn('private source',text)
        self.assertEqual(len(hud),6)

    async def test_slow_start_and_tools_share_one_ack_with_silent_keepalive(self):
        content,hud,ticks=await self.drive(delayed=True)
        self.assertEqual(len(ticks),2)
        self.assertEqual(''.join(content).count('One moment'),1)
        self.assertIn('',content)
        self.assertIn('Your appointment is at three.',''.join(content))

    async def test_wait_after_real_speech_stays_silent_and_still_delivers_outcome(self):
        content,_,_=await self.drive(late=True)
        self.assertNotIn('One moment',''.join(content))
        self.assertGreaterEqual(content.count(''),2)
        self.assertIn('Your appointment is at three.',''.join(content))

    async def test_quiet_mode_does_not_hide_provider_error(self):
        content,_,_=await self.drive(error=True)
        self.assertIn('API balance needs attention',''.join(content))

    def test_ack_does_not_repeat_after_real_speech_and_is_scrubbed_from_history(self):
        self.assertEqual(main._waiting_ack({'any':True}),'')
        self.assertEqual(main._strip_voice_noise('One moment… Your appointment is at three.'),'Your appointment is at three.')

    def test_board_batch_shortens_only_successes_and_preserves_failures(self):
        operations=[{'state':chat.OP_DONE,'tool':'update_item','text':f'Updated board record {i}'} for i in range(4)]
        operations.append({'state':chat.OP_UNKNOWN,'tool':'send_email','reason':'Delivery was not confirmed'})
        spoken=chat.action_receipt_reply(operations,spoken_mode=True)
        written=chat.action_receipt_reply(operations)
        self.assertIn('Completed 4 board updates',spoken)
        self.assertIn('Delivery was not confirmed',spoken)
        self.assertNotIn('These results cover',spoken)
        for i in range(4):self.assertIn(f'Updated board record {i}',written)
        self.assertNotIn('Completed 5',spoken)
