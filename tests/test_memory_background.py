"""No paid providers: exercise real retrieval, recap assembly and async delivery."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from backend import capabilities, chat, db, entities, entity_context, main, taskrunner


class MemorySelection(unittest.TestCase):
    def test_only_direct_unambiguous_mentions_select_dossiers(self):
        index = {'nigel': [('pro_aaaaaaaaaaaa', 'name', 'confirmed')],
                 'chris': [('per_bbbbbbbbbbbb', 'first_name', 'unreviewed')],
                 'other': [('per_cccccccccccc', 'name', 'confirmed')]}
        conn = MagicMock()
        with patch.object(entity_context, 'layer_state', return_value='ready'), \
             patch.object(entity_context.db, '_conn', return_value=conn), \
             patch.object(entities, 'alias_index', return_value=index), \
             patch.object(entity_context, 'dossier_text', return_value='DATED NIGEL EVIDENCE') as dossier:
            result = entity_context.recap_context([
                {'role':'assistant','content':'Other completed everything'},
                {'role':'user','content':'Nigel needs a review. Chris may be interested.'}])
        dossier.assert_called_once_with('pro_aaaaaaaaaaaa')
        self.assertIn('DATED NIGEL EVIDENCE', result)
        self.assertIn('Unresolved names', result)
        self.assertIn(entities.MONEY_NOTE, result)

    def test_deep_dive_exposes_organized_memory_but_stays_read_only(self):
        names = {x['name'] for x in capabilities._deep_dive_schemas()}
        self.assertIn('lookup_entity', names)
        from backend import tools
        self.assertTrue(names <= tools.NATIVE_READS)
        self.assertNotIn('update_item', names)


class AsyncMemoryAndTasks(unittest.IsolatedAsyncioTestCase):
    async def test_recap_keeps_tail_correction_and_reads_relevant_memory(self):
        turns = [{'role':'user','ts':'2026-09-22T09:00:00-04:00',
                  'content':'Some earlier context. ' * 90 + 'Nigel is waiting on requirements; not complete.'}]
        turns += [{'role':'assistant','content':'Everything completed!'} for _ in range(3)]
        client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(content=[SimpleNamespace(text='Waiting on requirements.')]))))
        with patch.dict(chat._CTX, {}, clear=False), \
             patch.object(chat, '_recap_running', [False]), \
             patch.object(chat, '_recap_retry', {'after':0,'failures':0}), \
             patch.object(db, 'enabled', return_value=True), \
             patch.object(db, 'latest_summary', return_value={}), \
             patch.object(db, 'recent_turns', return_value=turns), \
             patch.object(db, 'add_summary') as save, \
             patch.object(entity_context, 'recap_context', return_value='DATED RELATED RECORD'), \
             patch.object(chat, '_anthropic', return_value=client):
            await chat._refresh_recap()
        prompt = client.messages.create.call_args.kwargs['messages'][0]['content']
        self.assertIn('not complete.', prompt)
        self.assertIn('2026-09-22T09:00:00-04:00', prompt)
        self.assertIn('DATED RELATED RECORD', prompt)
        self.assertNotIn('Everything completed!', prompt)
        save.assert_called_once_with('Waiting on requirements.', 'recap')

    async def test_slow_dashboard_does_not_block_healthy_dashboard(self):
        healthy_sent = asyncio.Event()
        release = asyncio.Event()
        slow = SimpleNamespace(send_json=AsyncMock(side_effect=lambda _: None))
        # Hashable socket doubles.
        class Socket:
            async def send_json(self, data):
                if self.slow: await release.wait()
                else: healthy_sent.set()
        slow, healthy = Socket(), Socket()
        slow.slow, healthy.slow = True, False
        with patch.object(main, '_stage_clients', {slow, healthy}), \
             patch.object(main, 'STAGE_SEND_TIMEOUT', .06):
            operation = asyncio.create_task(main.publish_stage_event('task', {'id':'fixture'}))
            await asyncio.wait_for(healthy_sent.wait(), .03)
            self.assertFalse(operation.done())
            self.assertEqual(await asyncio.wait_for(operation, .2), 1)
            self.assertNotIn(slow, main._stage_clients)
            self.assertIn(healthy, main._stage_clients)

    async def test_stalled_listener_cannot_hold_background_execution(self):
        entered = asyncio.Event()
        never = asyncio.Event()
        received = []
        async def slow(card):
            entered.set()
            await never.wait()
        async def healthy(card): received.append(card['state'])
        row={'id':'fixture','capability':'fixture','attempts':1,'state':'working'}
        async def execute(*args, **kwargs):
            return {**row,'state':'completed'}
        with patch.object(taskrunner, '_listeners', {slow,healthy}), \
             patch.object(taskrunner, '_running', {}), \
             patch.object(taskrunner, 'LISTENER_TIMEOUT', .04), \
             patch.object(taskrunner.tasks, 'claim', return_value=row), \
             patch.object(taskrunner.tasks, 'card', side_effect=lambda x:x), \
             patch.object(taskrunner, '_execute', side_effect=execute):
            worker = asyncio.create_task(taskrunner.run('fixture'))
            await entered.wait()
            # Another conversational coroutine progresses while the worker is active.
            await asyncio.sleep(0)
            self.assertFalse(worker.done())
            result = await asyncio.wait_for(worker, .2)
        self.assertEqual(result['state'],'completed')
        self.assertEqual(received,['working','completed'])
