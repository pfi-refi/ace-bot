"""Real capture route with isolated fake providers: never calls a live service."""
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import main, brain, capture_store
from fastapi.testclient import TestClient


def extraction(**extra):
    return dict(summary='Meeting notes from Alex.', facts=[], todos=[], contacts=[], text='Alex said hello.', **extra)


class CaptureRoute(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.app.dependency_overrides[main.require_auth] = lambda: None
        self.create = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text=json.dumps(extraction()))], stop_reason='end_turn'))
        self.patches = [patch.object(main.chat, '_anthropic', return_value=SimpleNamespace(messages=SimpleNamespace(create=self.create))),
                        patch.object(capture_store, 'save', return_value='cap-test'),
                        patch.object(main.daybank, 'add_item', return_value=(True, {})),
                        patch.object(brain, 'add_memory', return_value=True),
                        patch.object(main.history, 'append'),
                        patch.object(main, '_live_convos', [[]]),
                        patch.object(main.chat, '_CTX', {'ts': 99}),
                        patch.object(main.voice, 'transcribe', new_callable=AsyncMock)]
        self.mocks = [p.start() for p in self.patches]
        self.save, self.add, self.memory, self.history = self.mocks[1:5]

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        main.app.dependency_overrides.clear()

    def send(self, filename='meeting.txt', data=b'Alex said hello.', mime='text/plain', preview=False):
        return self.client.post('/capture' + ('?dry_run=true' if preview else ''), files={'file': (filename, data, mime)}).json()

    def model(self, value, stop='end_turn'):
        self.create.return_value = SimpleNamespace(content=[SimpleNamespace(text=value)], stop_reason=stop)

    def test_plain_notes_persist_exact_text_and_context(self):
        result = self.send(data=b'Exact notes, no model rewriting.')
        self.assertTrue(result['ok'])
        self.assertEqual(self.save.call_args.kwargs['text'], 'Exact notes, no model rewriting.')
        self.assertEqual(result['capture_id'], 'cap-test')
        self.assertIn('Exact notes', main._live_convos[0][-1]['content'])
        self.assertIn('read_attachment', main._live_convos[0][-1]['content'])
        self.assertEqual(main.chat._CTX['ts'], 0)
        self.mocks[-1].assert_not_called()

    def test_markdown_octet_stream(self):
        self.assertEqual(self.send('notes.md', b'# Notes', 'application/octet-stream')['kind'], 'text')

    def test_malformed_json_is_failure_without_writes(self):
        for response in ('not json', '{}', '{"summary":"yes"}', json.dumps(dict(extraction(), facts='wrong'))):
            self.model(response)
            self.assertFalse(self.send()['ok'])
        self.save.assert_not_called(); self.add.assert_not_called(); self.memory.assert_not_called()

    def test_token_truncation_is_failure_even_with_parseable_json(self):
        self.model(json.dumps(extraction()), 'max_tokens')
        self.assertFalse(self.send()['ok']); self.save.assert_not_called()

    def test_storage_failure_prevents_autofiling(self):
        self.model(json.dumps(dict(extraction(), facts=['Known fact'], todos=['Call Alex'])))
        self.save.side_effect = RuntimeError('offline')
        self.assertFalse(self.send()['ok']); self.add.assert_not_called(); self.memory.assert_not_called()

    def test_failed_filing_is_partial_not_false_success(self):
        self.model(json.dumps(dict(extraction(), facts=['Known fact'], todos=['Call Alex'])))
        self.memory.return_value = False
        self.add.return_value = False, 'unavailable'
        result = self.send()
        self.assertTrue(result['ok']); self.assertTrue(result['partial'])
        self.assertEqual(result['facts_count'], 0); self.assertEqual(result['todos_count'], 0)
        self.assertEqual(len(result['warnings']), 2)

    def test_preview_writes_nothing(self):
        self.assertTrue(self.send(preview=True)['ok'])
        self.save.assert_not_called(); self.history.assert_not_called(); self.add.assert_not_called()

    def test_bad_utf8_or_oversized_text_never_calls_model(self):
        self.assertFalse(self.send(data=b'\xff')['ok'])
        self.assertFalse(self.send(data=b'x' * 60001)['ok'])
        self.create.assert_not_called()

    def test_docx_extracts_paragraph_text(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as z:
            z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:document>')
        result = self.send('notes.docx', data.getvalue(), 'application/octet-stream')
        self.assertTrue(result['ok']); self.assertEqual(result['text'], 'Hello')

    def test_bad_docx_never_calls_model(self):
        self.assertFalse(self.send('notes.docx', b'bad zip', 'application/octet-stream')['ok'])
        self.create.assert_not_called()

    def test_image_original_is_saved(self):
        data=b'\x89PNG\r\n\x1a\nfixture'
        self.assertTrue(self.send('photo.png', data, 'application/octet-stream')['ok'])
        self.assertEqual(self.save.call_args.kwargs['data'], data)
        self.assertEqual(self.save.call_args.kwargs['media_type'], 'image/png')

    def test_audio_transcript_is_persisted(self):
        self.mocks[-1].return_value = ('Original spoken words.', None)
        self.assertTrue(self.send('memo.m4a', b'audio', 'audio/m4a')['ok'])
        self.assertEqual(self.save.call_args.kwargs['text'], 'Original spoken words.')

if __name__ == '__main__': unittest.main()
