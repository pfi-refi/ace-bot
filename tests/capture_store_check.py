"""Attachment persistence and original-image reread using disposable PostgreSQL."""
import os, sys, tempfile, json
from pathlib import Path
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ace2'))
import pgserver
from backend import capture_store as store
with tempfile.TemporaryDirectory() as temp:
    server=pgserver.get_server(Path(temp)/'pg',cleanup_mode='delete')
    try:
        with patch.dict(os.environ,DATABASE_URL=server.get_uri(),ANTHROPIC_API_KEY='test-only'):
            first=store.save(filename='notes.txt',kind='text',summary='notes',text='Exact original\nwith punctuation.')
            image=store.save(filename='photo.png',kind='image',summary='image',text='partial OCR',data=b'original-pixels',media_type='image/png')
            assert store.get(first)['text']=='Exact original\nwith punctuation.'
            assert store.get()['id']==image
            assert store.get(image)['data']==b'original-pixels'
            assert store.get("' OR true --") is None
            with patch('anthropic.Anthropic') as provider:
                client=provider.return_value.__enter__.return_value
                client.messages.create.return_value=SimpleNamespace(content=[SimpleNamespace(text='Verified image detail')])
                assert json.loads(store.read_attachment(first))['text'].startswith('Exact original')
                provider.assert_not_called()
                result=json.loads(store.read_attachment(image,question='What is in this image?'))
                assert result['source_answer']=='Verified image detail'
                block=client.messages.create.call_args.kwargs['messages'][0]['content'][0]
                assert block['source']['data']=='b3JpZ2luYWwtcGl4ZWxz'
            print('PASS: durable exact text, original bytes, latest lookup, safe ID lookup, text-only no-model read, original-image question')
    finally:
        server.cleanup()
