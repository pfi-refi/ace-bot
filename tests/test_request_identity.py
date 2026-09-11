import unittest
from unittest.mock import patch, AsyncMock
from backend import main
from starlette.websockets import WebSocketDisconnect

class RequestIdentity(unittest.IsolatedAsyncioTestCase):
    async def test_ws_echoes_identity_for_each_turn_including_error(self):
        sent=[]
        ws=AsyncMock()
        ws.query_params={'token':'fixture'}
        ws.receive_json.side_effect=[{'message':'first','request_id':'A'}, {'message':'second','request_id':'B'},WebSocketDisconnect()]
        async def send(d):sent.append(d)
        ws.send_json.side_effect=send
        async def turn(text,emit,**kwargs):
            await emit('delta',{'text':text})
            if text=='second':await emit('error',{'text':'fixture error'})
            return text
        with patch.object(main,'token_valid',return_value=True),patch.object(main.chat,'_unified_thread',return_value=[]),patch.object(main.chat,'stream_turn',side_effect=turn):
            await main.ws_chat(ws)
        self.assertEqual([d['request_id'] for d in sent if d['type']=='done'],['A','B'])
        self.assertEqual([d['request_id'] for d in sent if d['type']=='error'],['B'])
        self.assertTrue(all(d.get('request_id') in ('A','B') for d in sent))
    async def test_http_echoes_identity(self):
        async def turn(text,emit):await emit('final',{'text':'fixture'})
        with patch.object(main.chat,'stream_turn',side_effect=turn):
            result=await main.chat_http(main.ChatReq(message='test',request_id='C'))
        self.assertEqual(result['request_id'],'C')
