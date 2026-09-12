"""Real SSE adapter with a synthetic turn; no network, model, or database calls."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from ace2.backend import main


class NoticeTransport(unittest.IsolatedAsyncioTestCase):
    async def drive(self, consume):
        acknowledgments = []
        queued = asyncio.Event()
        async def turn(text, emit, **kwargs):
            await emit("delta", {"text": "Hello."})
            queued.set()
            result = await emit("task_notice", {"task_id": "fixture", "text": "**Your report is ready.**"})
            acknowledgments.append(result)
            await emit("final", {})
        request = AsyncMock()
        request.json.return_value = {"messages": [{"role": "user", "content": "Hi"}]}
        with patch.object(main, "_llm_authorized", return_value=True), patch.object(main.chat, "stream_turn", turn):
            response = await main.openai_compat(request, authorization="fixture")
            iterator = response.body_iterator
            first = await anext(iterator)
            self.assertIn("Hello.", first)
            await queued.wait()
            await asyncio.sleep(0)
            self.assertEqual(acknowledgments, [], "enqueue alone must not acknowledge")
            if consume:
                notice = await anext(iterator)
                self.assertIn("Your report is ready.", notice)
                self.assertNotIn("**", notice)
                self.assertEqual(acknowledgments, [], "yield without resume is not acknowledged")
                rest = [piece async for piece in iterator]
                self.assertTrue(rest)
                self.assertEqual(acknowledgments, [True])
            else:
                await iterator.aclose()
                await asyncio.sleep(0)
                self.assertEqual(acknowledgments, [])
            task = main._active_voice_task["task"]
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_unconsumed_queue_does_not_acknowledge(self):
        await self.drive(False)

    async def test_consumed_notice_acknowledges_exact_fragment(self):
        await self.drive(True)
