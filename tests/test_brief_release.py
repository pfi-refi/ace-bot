import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock
from test_reliability import extract, ROOT
from backend import db, ops

class BriefRelease(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, response):
        sleeps=0
        async def tick(_):
            nonlocal sleeps
            sleeps+=1
            if sleeps>1: raise asyncio.CancelledError
        now=datetime.now(timezone.utc)
        env={'__name__':'backend.chat','asyncio':asyncio,'datetime':datetime,
             'EASTERN':timezone.utc,'_BRIEF_TIMES':{'morning':(now.hour,now.minute)},
             '_brief_sent':{},'bridge_lease_active':lambda k:False,
             'generate_brief':AsyncMock(return_value=response)}
        fn=extract(ROOT/'ace2/backend/chat.py',['_brief_loop'],env)['_brief_loop']
        with patch.object(asyncio,'sleep',tick),patch.object(db,'enabled',return_value=True), \
             patch.object(db,'latest_summary',return_value={}),patch.object(db,'add_summary') as marker, \
             patch.object(ops,'begin',return_value=('execute','fixture',None)),patch.object(ops,'settle') as settle:
            await fn()
            marker.assert_not_called()
            return settle.call_args.args[1]
    async def test_empty_generation_is_not_delivery(self):
        self.assertEqual(await self.exercise(''),ops.UNKNOWN)
    async def test_delivery_receipt_is_completed(self):
        self.assertEqual(await self.exercise('fixture receipt'),ops.COMPLETED)
