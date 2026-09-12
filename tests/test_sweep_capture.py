"""The background board-keeper — the safety net for things Brady says on a call.

Two defects found on 12 September, both measured against the live board before changing
anything:

1. It read the last 40 turns. That day's midday call alone produced 70 rows, so a sweep
   running when he hung up could not see the half where the money was discussed.
2. NOTHING it filed could carry a date. The line protocol had no field for one and the
   write passed None unconditionally — which is most of why 64 open rows held 26 due dates
   and exactly one follow-up, and why the Week tab read emptier than his week was.
"""
import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import chat, daybank, db

EASTERN = pytz.timezone("America/New_York")


class SweptTasksCanCarryADate(unittest.TestCase):
    """apply_sweep is executed against a fake store; the parsing is the real parsing."""

    def run_sweep(self, triage):
        added = []

        def add_item(kind, text, due=None, tags=None, parent_id=None, bucket=None):
            added.append({"kind": kind, "text": text, "due": due, "tags": tags})
            return True, {"id": "x1"}

        with patch.object(daybank, "read_items", lambda *a, **k: []), \
             patch.object(daybank, "add_item", add_item), \
             patch.object(chat.brain, "add_memory", lambda *a, **k: None):
            asyncio.run(chat.apply_sweep(0, "NONE", triage, "NONE"))
        return added

    def test_a_day_brady_named_lands_on_the_row(self):
        got = self.run_sweep("ADD :: Bills :: Pay the electric disconnect :: DUE=2026-09-14")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["due"], "2026-09-14")
        self.assertEqual(got[0]["text"], "Pay the electric disconnect")
        self.assertEqual(got[0]["tags"], ["Bills"])

    def test_no_date_said_means_no_date_invented(self):
        got = self.run_sweep("ADD :: Money :: Buy and install new tires")
        self.assertEqual(got[0]["due"], None)
        self.assertEqual(got[0]["text"], "Buy and install new tires")

    def test_a_weekday_name_is_refused_rather_than_stored(self):
        """The sweep must resolve days from the ladder. Anything that is not an ISO day is
        not a date, and must not become one — nor leak into the title."""
        got = self.run_sweep("ADD :: Bills :: Pay the water bill :: DUE=next Friday")
        self.assertEqual(got[0]["due"], None)
        self.assertEqual(got[0]["text"], "Pay the water bill")
        self.assertNotIn("Friday", got[0]["text"])
        self.assertNotIn("DUE", got[0]["text"])

    def test_the_field_never_leaks_into_the_title(self):
        for line in ("ADD :: Money :: Send the truck payment :: DUE=2026-09-15",
                     "ADD :: Money :: Send the truck payment :: DUE=whenever"):
            got = self.run_sweep(line)
            self.assertEqual(got[0]["text"], "Send the truck payment", line)

    def test_a_title_containing_a_colon_pair_still_survives(self):
        got = self.run_sweep("ADD :: Deals :: Nigel :: phase 2 build :: DUE=2026-09-18")
        self.assertEqual(got[0]["due"], "2026-09-18")
        self.assertEqual(got[0]["text"], "Nigel::phase 2 build")

    def test_completions_are_untouched_by_the_new_field(self):
        got = self.run_sweep("DONE :: abc123")
        self.assertEqual(got, [], "a DONE line must never add a row")


class TheSweepSeesAWholeConversation(unittest.TestCase):
    def test_it_asks_for_more_than_one_call_s_worth_of_turns(self):
        """Brady's 12 Sept midday call was 70 rows on its own."""
        self.assertGreater(chat._SWEEP_TURNS, 70)

    def test_it_is_bounded_by_characters_as_well_as_count(self):
        """A count alone lets a run of 2000-char brain-dumps blow the prompt up."""
        turns = [{"role": "user", "content": "x" * 2000} for _ in range(200)]
        kept = chat._budget_turns(turns, chat._SWEEP_CHARS)
        self.assertLess(len(kept), 200)
        self.assertLessEqual(sum(len(t["content"]) + 20 for t in kept), chat._SWEEP_CHARS)

    def test_the_newest_turns_are_the_ones_kept(self):
        turns = [{"role": "user", "content": f"turn {i} " + "x" * 1000} for i in range(200)]
        kept = chat._budget_turns(turns, 20000)
        self.assertIn("turn 199", kept[-1]["content"])

    def test_compose_reads_the_widened_window(self):
        import inspect
        src = inspect.getsource(chat.compose_sweep)
        self.assertIn("db.recent_turns, _SWEEP_TURNS", src)
        self.assertIn("_budget_turns(turns, _SWEEP_CHARS)", src)


class TheBoardKeeperIsToldHowToResolveADay(unittest.TestCase):
    def prompt(self):
        # compose_sweep imports db/daybank inside the function, so patch the real modules.
        turn = {"role": "user", "content": "pay the electric by Monday",
                "ts": "2026-09-12T16:00:00+00:00"}
        with patch.object(db, "enabled", lambda: True), \
             patch.object(db, "recent_turns", lambda n: [turn] * 6), \
             patch.object(chat.brain, "read_memory", lambda: []), \
             patch.object(daybank, "read_items", lambda *a, **k: []), \
             patch.object(chat, "bridge_lease_active", lambda kind: False), \
             patch.object(chat, "_learn_state", {}):
            return asyncio.run(chat.compose_sweep(force=True))["triage_prompt"]

    def test_the_date_ladder_rides_with_it(self):
        p = self.prompt()
        self.assertIn("DATE LADDER", p)
        self.assertIn("Never count weekdays yourself", p)

    def test_the_add_format_carries_the_optional_field(self):
        p = self.prompt()
        self.assertIn("ADD :: CATEGORY :: task title :: DUE=YYYY-MM-DD", p)
        self.assertIn("never invent a date he did not give", p)


class HeStopsRepeatingBradyBackToHimself(unittest.TestCase):
    def test_the_rule_is_in_the_assembled_voice_context(self):
        from test_voice_length_and_dates import voice_context
        ctx = voice_context()
        self.assertIn("DO NOT REPLAY WHAT HE JUST SAID", ctx)
        self.assertIn("agree in four words", ctx)

    def test_it_names_what_a_reply_must_add(self):
        from test_voice_length_and_dates import voice_context
        self.assertIn("something he did not already say", voice_context())


if __name__ == "__main__":
    unittest.main()
