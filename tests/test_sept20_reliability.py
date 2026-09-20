"""Regression scenarios for cut-off briefings, voice extensions, and provider failures."""
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch
from backend import capabilities as cp, chat, main, provider_status, db
from backend.integrations import bills_sheet


def response(text, stop='end_turn'):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type='text', text=text)],
        stop_reason=stop, usage=types.SimpleNamespace(input_tokens=120, output_tokens=50))


class DeepDiveRecovery(unittest.IsolatedAsyncioTestCase):
    async def dive(self, responses, checkpoint=None):
        create=AsyncMock(side_effect=responses)
        client=types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
        with patch.object(chat, '_live_context', AsyncMock(return_value=('Facts', 'Board'))), \
             patch.object(chat, '_unified_thread', return_value=[]),              patch.object(chat, 'build_system_prompt', return_value='test'), \
             patch.object(bills_sheet, 'fetch_bills', AsyncMock(return_value=([], 'Unverified'))):
            out=await cp.deep_dive({'question':'Review bills, income, stuck deals and follow-ups', '_client':client},
                                   None, checkpoint=checkpoint)
        return out,create

    async def test_cutoff_rewrites_once_and_only_returns_complete_answer(self):
        saved=AsyncMock(return_value=True)
        out,create=await self.dive([response('Bills checked; income is', 'max_tokens'),
            response('Bills: unverified. Income: unverified. Deals: waiting. Follow-ups: call the named owner.')], saved)
        self.assertEqual(create.await_count,2)
        self.assertNotIn('tools', create.call_args_list[1].kwargs)
        self.assertEqual(create.call_args_list[1].kwargs['max_tokens'],5000)
        self.assertEqual(out['model_rounds'][0]['stop_reason'],'max_tokens')
        self.assertEqual(out['model_rounds'][1]['stop_reason'],'end_turn')
        self.assertTrue(out['truncation_recovered'])
        self.assertNotIn('partial_answer',out)
        self.assertTrue(saved.call_args.args[0]['partial_is_incomplete'])

    async def test_repeated_cutoff_fails_with_partial_receipt_not_success(self):
        with self.assertRaises(cp.Failed) as caught:
            await self.dive([response('First fragment','max_tokens'), response('Still unfinished','max_tokens')])
        self.assertNotIn('answer',caught.exception.result)
        self.assertTrue(caught.exception.result['partial_is_incomplete'])
        self.assertEqual(len(caught.exception.result['model_rounds']),2)

    async def test_provider_failure_records_kind_without_leaking_raw_error(self):
        with self.assertRaises(cp.Failed) as caught:
            await self.dive([RuntimeError('Your credit balance is too low secret=NEVERPRINT')])
        self.assertEqual(caught.exception.result['model_rounds'][0]['error_kind'],'billing')
        self.assertNotIn('NEVERPRINT',str(caught.exception.result))


class VoiceIdentity(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(main,'_voice_identity',{'prefix':'','text':'','key':'','at':0.0})
        self.patch.start()
    def tearDown(self): self.patch.stop()
    def test_growing_transcript_reuses_identity_but_new_question_does_not(self):
        prefix=[{'role':'assistant','content':'What should we work on?'}]
        a=main._voice_origin_key(prefix+[{'role':'user','content':'Run a deep dive.'}])
        b=main._voice_origin_key(prefix+[{'role':'user','content':'Run a deep dive on bills and deals'}])
        c=main._voice_origin_key(prefix+[{'role':'user','content':'Research different CRM options'}])
        self.assertEqual(a,b);self.assertNotEqual(b,c)
    def test_new_assistant_turn_and_elapsed_window_do_not_reuse(self):
        prior=[{'role':'user','content':'Run a deep dive.'}]
        a=main._voice_origin_key(prior)
        b=main._voice_origin_key(prior+[{'role':'assistant','content':'It failed'},prior[0]])
        self.assertNotEqual(a,b)
        main._voice_identity['at']-=31
        self.assertNotEqual(b,main._voice_origin_key(prior+[{'role':'assistant','content':'It failed'},prior[0]]))


class ProviderAndContext(unittest.IsolatedAsyncioTestCase):
    async def test_billing_failure_stops_rapid_recap_retries_and_keeps_old_recap(self):
        create=AsyncMock(side_effect=RuntimeError('Your credit balance is too low'))
        client=types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
        with patch.object(chat,'_recap_running',[False]), \
             patch.object(chat,'_recap_retry',{'after':0.0,'failures':0}), \
             patch.object(db,'enabled',return_value=True), \
             patch.object(db,'latest_summary',return_value={'text':'existing recap'}), \
             patch.object(db,'recent_turns',return_value=[{'role':'user','content':'Pending reply'}]*5), \
             patch.object(chat,'_anthropic',return_value=client), \
             patch.object(chat,'_CTX',{'recap':'existing recap'}):
            await chat._refresh_recap();await chat._refresh_recap()
            self.assertEqual(create.await_count,1)
            self.assertEqual(chat._CTX['recap'],'existing recap')
            self.assertEqual(chat._recap_retry['failures'],1)

    async def test_slow_read_is_bounded(self):
        with self.assertRaises(asyncio.TimeoutError):
            await chat._context_read('fixture',asyncio.sleep(1),timeout=.01)

    def test_user_message_classification_never_exposes_raw_error(self):
        kind,message=provider_status.classify(RuntimeError('credit balance is too low token=secret'))
        self.assertEqual(kind,'billing');self.assertNotIn('secret',message)
        self.assertEqual(provider_status.retry_delay(RuntimeError('credit balance is too low'),100),900)
