"""Two things the 12 September call exposed, both executed rather than asserted about.

1. The DATE LADDER was added on 10 Sept to stop Ace counting weekdays and went into the
   TYPED context only. Voice is the small fast model and voice is where Brady says a day out
   loud, so he was told Friday was the 20th and had to correct it mid-call. It was the 18th.
2. REPLY LENGTH. 17 of 29 replies on that call ran past 60 words, the longest 169 — over a
   minute of talking — and every long one was a list read aloud, markdown and all.
"""
import asyncio
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import chat, main

EASTERN = pytz.timezone("America/New_York")


def voice_context():
    """The REAL assembled voice context, with the heavy fetches stubbed."""
    import contextlib
    with contextlib.ExitStack() as st:
        for name in ('_profile_block', '_recap_block', '_group_facts', '_format_daybank',
                     '_format_calendar_window', '_format_today_schedule', '_format_thread'):
            st.enter_context(patch.object(chat, name, return_value=''))
        for obj, name, val in ((chat.brain, 'read_memory', []), (chat.brain, 'read_memory_meta', {}),
                               (chat.daybank, 'read_items', []), (chat, 'get_events_structured', []),
                               (chat, 'get_gmail_summary', ''), (chat, 'get_personal_inbox_structured', [])):
            st.enter_context(patch.object(obj, name, return_value=val))
        st.enter_context(patch.object(chat, 'get_weather', new=AsyncMock(return_value={})))
        st.enter_context(patch.object(chat, '_CTX', dict(chat._CTX, ts=time.time(), events=[],
                                                         memory=[], bank=[], wx={}, convo=[],
                                                         memory_meta={})))
        return asyncio.run(chat._fast_context())


class TheLadderReachesTheMouthThatNeedsIt(unittest.TestCase):
    def test_voice_carries_the_date_ladder(self):
        self.assertIn("DATE LADDER", voice_context(),
                      "voice is the path that gets days wrong and it had no ladder")

    def test_brady_s_actual_correction_is_answerable_from_it(self):
        """12 Sept was a Saturday. Ace said Friday was the 20th; Brady said the 18th."""
        sat = EASTERN.localize(datetime(2026, 9, 12, 11, 50))
        # Split on the COLUMN gap, not the last space: "today" pairs with "Sat 2026-09-12".
        import re as _re
        rows = dict(_re.findall(r"^\s+(\S.*?)\s{2,}(\S.*)$", chat.date_ladder(sat), _re.M))
        self.assertEqual(rows["next Friday"], "2026-09-18")
        self.assertEqual(rows["today"], "Sat 2026-09-12")
        self.assertEqual(rows["next Sunday"], "2026-09-20", "the day Ace actually said")

    def test_the_ladder_forbids_counting_rather_than_merely_listing(self):
        ladder = chat.date_ladder(EASTERN.localize(datetime(2026, 9, 12, 11, 50)))
        self.assertIn("Never count weekdays yourself", ladder)

    def test_both_paths_have_it_now(self):
        import inspect
        for fn in (chat._live_context, chat._fast_context):
            self.assertIn("date_ladder", inspect.getsource(fn), fn.__name__)


class HowLongToTalk(unittest.TestCase):
    def test_the_length_rule_is_stated_before_the_tool_instructions(self):
        """It used to sit at the end of a 600-word parenthetical. Position was the bug."""
        ctx = voice_context()
        rule = ctx.find("HOW LONG TO TALK")
        self.assertGreater(rule, -1, "the length rule is not in the assembled voice context")
        for later in ("display_card", "create_calendar_event", "CALL RITUALS"):
            self.assertLess(rule, ctx.find(later), f"the length rule comes after {later}")

    def test_it_names_a_concrete_limit_and_the_actual_failure(self):
        ctx = voice_context()
        self.assertIn("two or three sentences", ctx.lower())
        self.assertIn("NEVER READ A LIST OUT LOUD", ctx)


class NothingWearsMarkdownToTheSpeaker(unittest.TestCase):
    def test_emphasis_and_bullets_are_removed(self):
        self.assertEqual(main._speakable("**Money in motion:** $3k"), "Money in motion: $3k")
        self.assertEqual(main._speakable("- tires $100"), "tires $100")
        self.assertEqual(main._speakable("• electric"), "electric")
        self.assertEqual(main._speakable("`code`"), "code")

    def test_ordinary_speech_is_untouched(self):
        for plain in ("#1 priority", "a_b_c", "Pay $247.79 by Monday", "5 - 3 is 2"):
            self.assertEqual(main._speakable(plain), plain)

    def test_a_marker_split_across_fragments_never_leaks(self):
        """'**' routinely arrives as two fragments. Each half must lose its marker on its
        own — this is why no cross-fragment buffer is needed."""
        out = "".join(main._speakable(f) for f in
                      ["Here is ", "*", "*Money", " in motion", "*", "*", " — $3k"])
        self.assertNotIn("*", out)
        self.assertEqual(out, "Here is Money in motion — $3k")

    def test_a_pure_markup_fragment_says_nothing(self):
        self.assertEqual(main._speakable("**"), "")

    def test_say_actually_routes_through_it(self):
        """A module-level scrubber nothing calls is the failure mode this file exists for."""
        import inspect
        src = inspect.getsource(main.openai_compat)
        self.assertIn("text = _speakable(text)", src)


if __name__ == "__main__":
    unittest.main()
