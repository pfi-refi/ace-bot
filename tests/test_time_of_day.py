"""He closed a midday call with "Goodnight, Brady" — twice in three days, 15:06 and 12:04.

He had the clock in his context both times. What he did not have was a WORD for the part of
the day, and the sign-off ritual's only examples were evening ones, so the small voice model
copied the word it was shown. These execute the real context assembly at fixed times.
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import chat
from test_voice_length_and_dates import voice_context

TZ = pytz.timezone("America/New_York")


class _FixedClock:
    """A datetime stand-in whose now() is pinned. Everything else passes through."""

    def __init__(self, fixed):
        self.fixed = fixed

    def now(self, tz=None):
        return self.fixed.astimezone(tz) if tz else self.fixed

    def __getattr__(self, name):
        return getattr(datetime, name)


def context_at(hour, minute=0):
    with patch.object(chat, "datetime", _FixedClock(TZ.localize(datetime(2026, 9, 12, hour, minute)))):
        return voice_context()


class ThePartOfTheDayIsAWord(unittest.TestCase):
    def test_boundaries(self):
        for hour, part in ((5, "morning"), (11, "morning"), (12, "afternoon"), (16, "afternoon"),
                           (17, "evening"), (20, "evening"), (21, "night"), (4, "night")):
            self.assertEqual(chat.daypart(TZ.localize(datetime(2026, 9, 12, hour))), part, hour)

    def test_every_part_has_a_sign_off(self):
        self.assertEqual(set(chat.SIGN_OFFS), {"morning", "afternoon", "evening", "night"})

    def test_goodnight_is_only_offered_at_night(self):
        for part, line in chat.SIGN_OFFS.items():
            if part == "night":
                self.assertIn("Goodnight", line)
            else:
                self.assertNotIn("Goodnight", line, part)


class TheVoiceContextSaysWhichPartItIs(unittest.TestCase):
    def test_the_actual_failure_at_twelve_oh_four(self):
        """Pins the TOP anchor line specifically. The ritual repeats the word further down, so
        a looser assertion stayed green when the anchor lost it — mutation-checked."""
        ctx = context_at(12, 4)
        self.assertIn("12:04 PM (US Eastern) — it is the AFTERNOON.", ctx)
        self.assertNotIn("it is the NIGHT", ctx)

    def test_the_other_failure_at_three_in_the_afternoon(self):
        ctx = context_at(15, 6)
        self.assertIn("it is the AFTERNOON", ctx)

    def test_at_night_it_says_so(self):
        ctx = context_at(22, 30)
        self.assertIn("it is the NIGHT", ctx)
        self.assertIn("'Goodnight, Brady.'", ctx)

    def test_the_sign_off_examples_track_the_hour(self):
        """The ritual used to show only evening examples. It now shows the ones for right now."""
        noon, night = context_at(12, 4), context_at(22, 30)
        self.assertIn(chat.SIGN_OFFS["afternoon"], noon)
        self.assertNotIn(chat.SIGN_OFFS["night"], noon)
        self.assertIn(chat.SIGN_OFFS["night"], night)

    def test_the_rule_is_stated_not_implied(self):
        self.assertIn("'Goodnight' is a night word: never say it before the evening", context_at(12))


if __name__ == "__main__":
    unittest.main()
