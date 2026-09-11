"""Verify real voice/chat context assembly without network or model calls."""
import asyncio, contextlib, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ace2'))
from backend import chat
from backend.system_prompt import build_system_prompt

class UpgradeAwareness(unittest.TestCase):
    def test_both_real_context_paths_receive_same_release_block(self):
        with contextlib.ExitStack() as stack:
            for name,value in [('_profile_block',''),('_recap_block',''),('_group_facts',''),('_format_daybank',''),('_format_calendar_window',''),('_format_today_schedule',''),('_format_thread','')]:
                stack.enter_context(patch.object(chat,name,return_value=value))
            for obj,name,value in [(chat.brain,'read_memory',[]),(chat.brain,'read_memory_meta',{}),(chat.daybank,'read_items',[]),(chat,'get_events_structured',[]),(chat,'get_gmail_summary',''),(chat,'get_personal_inbox_structured',[])]:
                stack.enter_context(patch.object(obj,name,return_value=value))
            stack.enter_context(patch.object(chat,'get_weather',new=AsyncMock(return_value={})))
            ctx=dict(chat._CTX,ts=time.time(),events=[],memory=[],bank=[],wx={},convo=[],memory_meta={})
            stack.enter_context(patch.object(chat,'_CTX',ctx))
            block=chat._upgrade_awareness()
            slow,_=asyncio.run(chat._live_context())
            voice=asyncio.run(chat._fast_context())
            self.assertIn(block,slow); self.assertIn(block,voice)
            self.assertIn('Paperclip continuity',block)
            self.assertIn('More menu organized',block)
            self.assertIn('physical iPhone',block)
            self.assertIn('spoken yes alone',voice)
            self.assertNotIn('confirmed true',voice)

    def test_long_entry_retains_ending_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'CHANGELOG.md'
            p.write_text('# Releases\n---\n\n## New\n'+'Description. '*55+'NEVER AUTO-SEND.\n')
            with patch.object(chat,'_CHANGELOG',p),patch.object(chat,'_changelog_cache',{'mtime':0,'text':''}):
                self.assertIn('NEVER AUTO-SEND.',chat._changelog_block())

    def test_oversize_is_omitted_whole_and_missing_is_honest(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'CHANGELOG.md';p.write_text('---\n## Huge\n'+'x'*7000+'\nNEVER AUTO-SEND.')
            with patch.object(chat,'_CHANGELOG',p),patch.object(chat,'_changelog_cache',{'mtime':0,'text':''}):
                text=chat._changelog_block()
                self.assertIn('omitted',text);self.assertNotIn('xxx',text)
                p.unlink()
                self.assertIn('Release notes unavailable',chat._upgrade_awareness())

    def test_shipping_notes_fit_without_dropping_limits(self):
        block=chat._changelog_block()
        self.assertNotIn('omitted for size',block)
        self.assertLess(len(block),chat._CHANGELOG_MAX_CHARS)
        self.assertIn('deferred',block)
        prompt=build_system_prompt()
        self.assertIn('read_attachment',prompt)
        self.assertNotIn('pull every number',prompt)
        self.assertNotIn('interface has no menus',prompt)
