"""The one detector that looks for what is NOT on the board.

Every other check in selfaudit asks whether what was written is right. None of them could see
the 12 September failure: Brady listed obligations out loud and only some became rows.

The fixtures are that day's real sentences, and the calibration is real too — the threshold
was set against the live board, calendar and memory, where six genuinely-tracked things score
at or above it and two invented misses score below.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import selfaudit as sa


def turn(text, minutes_ago=5, role="user"):
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {"role": role, "ts": ts.isoformat(), "content": text}


def row(text):
    return {"id": "i1", "status": "open", "entry": "action", "text": text}


def audit(turns, items=(), facts=(), events=(), prev=None):
    now = {"items": list(items), "facts": list(facts), "events": list(events),
           "turns": list(turns)}
    found = [f for f in sa.audit(now, prev) if f["class"] == "SAIDNOTCAPTURED"]
    return found, now


class WhatCountsAsHimSayingIt(unittest.TestCase):
    def test_his_real_sentences_are_picked_up(self):
        said = [o["said"] for o in sa._obligations([
            turn("I gotta get my electric bill paid."),
            turn("I still have my truck payment I want to get sent out."),
            turn("I need to call the roofer about the garage estimate."),
        ])]
        self.assertTrue(any("electric bill" in s for s in said), said)
        self.assertTrue(any("roofer" in s for s in said), said)

    def test_conversational_lookalikes_are_not_obligations(self):
        for line in ("I have to say that was great.",
                     "I need to know when it hits.",
                     "I have to be honest with you about the numbers.",
                     "I gotta go."):
            self.assertEqual(sa._obligations([turn(line)]), [], line)

    def test_only_brady_is_read(self):
        self.assertEqual(sa._obligations([turn("I need to check the calendar", role="assistant")]), [])

    def test_a_thin_fragment_is_not_checkable(self):
        """Fewer than three content words cannot be matched against anything, so a missing
        row proves nothing about it."""
        self.assertEqual(sa._obligations([turn("I need to do that.")]), [])
        self.assertEqual(sa._obligations([turn("I have to review some numbers.")]), [],
                         "a fragment that names nothing in particular is not checkable")

    def test_a_run_on_is_cut_at_the_first_clause(self):
        """Brady talks in run-ons. Judged whole, the fragment collects so many keywords that
        the row covering the first half cannot clear the threshold — this produced a false
        alarm about the greenhouse pour on the first live run."""
        said = sa._obligations([turn(
            "I have to get my uncle's greenhouse poured next week at some point, and I know "
            "I'm potentially helping Damon on Friday with a pour as well.")])
        self.assertEqual(len(said), 1)
        self.assertNotIn("Damon", said[0]["said"])
        self.assertIn("greenhouse", said[0]["said"])


class MatchingSpeechAgainstAWrittenRow(unittest.TestCase):
    def test_a_verb_tense_does_not_break_the_match(self):
        """Mutation-checked: with three keywords, two matching carries it anyway, so the
        pinning case is a two-word fragment where the tense IS the difference."""
        self.assertEqual(sa._stem("poured"), "pour")
        self.assertEqual(sa._stem("mailing"), "mail")
        self.assertEqual(sa._covered("greenhouse poured", "Uncle's greenhouse pour next week"),
                         1.0)
        self.assertTrue(sa._already_known(
            "the greenhouse poured", [row("Uncle's greenhouse pour — moved to next week")],
            [], []))

    def test_stemming_does_not_collide_short_words(self):
        for word in ("bed", "red", "king", "ring", "seed"):
            self.assertEqual(sa._stem(word), word)

    def test_everything_he_said_that_day_is_recognised_by_the_corpus(self):
        """Not one row against one sentence — the detector reads the WHOLE corpus, and it
        has to, because "Truck loan (First Bank of Ohio)" carries no word "payment" and
        "Water — $50/mo" carries no word "bill". A dropped row counts: it means the
        obligation was seen and decided, which is not a capture failure."""
        corpus = [row(t) for t in (
            "Truck loan (First Bank of Ohio) — $473/mo, due the 15th. REAFFIRMED in Ch.7",
            "Water — $50/mo — due the 16th. Usage is only ~$20; the other $30 chips at arrears",
            "Pay water bill — $50",
            "Electric — $364/mo actual usage; the Equal Payment Plan only bills $193",
            "Buy and install new tires for truck (~$100)",
            "Rebecca Hebbard — mail her packet Thursday morning",
        )]
        facts = [{"text": "Truck loan payment of $473 leaves the account on the 15th"}]
        for said in ("get my electric bill paid", "get the tires for my truck",
                     "send my truck payment", "pay the water bill",
                     "mail Rebecca's packet"):
            self.assertTrue(sa._already_known(said, corpus, [], facts), said)

    def test_unrelated_text_does_not_clear_it(self):
        for said, text in (
            ("renew my drone pilot licence", "ZZTEST delta - renew the LLC filing"),
            ("call the roofer about the garage estimate", "Zoom call with Chris and Gabby"),
        ):
            self.assertLess(sa._covered(said, text), sa._SAID_MATCH, said)


class ItChecksEveryStore(unittest.TestCase):
    """The mistake this detector exists to avoid, made first by me and then designed out."""

    SAID = [turn("I have to be in Michigan for the Groundworks training that week.")]

    def test_a_board_row_silences_it(self):
        found, _ = audit(self.SAID, items=[row("Michigan Groundworks training week — lodging covered")])
        self.assertEqual(found, [])

    def test_a_calendar_entry_silences_it(self):
        found, _ = audit(self.SAID, events=[{"title": "Groundworks training, Michigan"}])
        self.assertEqual(found, [])

    def test_a_MEMORY_fact_silences_it(self):
        """Ace routes durable context to memory and to-dos to the board, on purpose. Reading
        only the board on 12 Sept made six correctly-filed items look like misses."""
        found, _ = audit(self.SAID, facts=[
            {"text": "Groundworks Michigan training confirmed Mon 9/28-Fri 10/2, lodging covered"}])
        self.assertEqual(found, [], "memory was not consulted — the original false alarm")

    def test_nothing_anywhere_is_reported(self):
        found, _ = audit(self.SAID)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "medium")
        self.assertIn("Michigan", found[0]["text"])


class ItDoesNotNag(unittest.TestCase):
    def test_the_same_obligation_is_raised_only_once(self):
        said = [turn("I need to call the roofer about the garage estimate.")]
        first, now = audit(said)
        self.assertEqual(len(first), 1)
        again, _ = audit(said, prev={"items": [], "said_seen": now["said_seen"]})
        self.assertEqual(again, [], "an obligation Brady chose not to track became a daily nag")

    def test_what_was_raised_is_carried_into_the_snapshot(self):
        _found, now = audit([turn("I need to call the roofer about the garage estimate.")])
        self.assertTrue(now.get("said_seen"))

    def test_something_said_days_ago_is_not_reopened(self):
        old = [turn("I need to call the roofer about the garage estimate.",
                    minutes_ago=60 * (sa._SAID_LOOKBACK_HOURS + 5))]
        found, _ = audit(old)
        self.assertEqual(found, [])

    def test_a_chatty_day_is_capped(self):
        turns = [turn(f"I need to call supplier number {i} about the quarterly rebate paperwork.")
                 for i in range(sa._SAID_MAX + 6)]
        found, _ = audit(turns)
        self.assertLessEqual(len(found), sa._SAID_MAX)


class OccurrencesAreNotHistoricalMatches(unittest.TestCase):
    def test_old_completed_cycle_does_not_hide_next_month(self):
        said = turn("I need to pay the water bill next month.")
        old = {**row("Pay water bill"), "status": "done",
               "done_ts": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()}
        found, _ = audit([said], items=[old])
        self.assertEqual(len(found), 1)
        self.assertIn("Possibly untracked", found[0]["detail"])

    def test_closure_after_statement_can_cover_it(self):
        closed = {**row("Pay water bill"), "status": "done",
                  "done_ts": datetime.now(timezone.utc).isoformat()}
        found, _ = audit([turn("I need to pay the water bill.")], items=[closed])
        self.assertEqual(found, [])

    def test_undated_historical_closure_is_not_completion_evidence(self):
        found, _ = audit([turn("I need to pay the water bill.")],
                         items=[{**row("Pay water bill"), "status": "dropped"}])
        self.assertEqual(len(found), 1)

    def test_previous_date_does_not_suppress_same_words_today(self):
        statement = "I need to pay the water bill."
        yesterday = turn(statement)
        yesterday["ts"] = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        _, snapshot = audit([yesterday])
        today, _ = audit([turn(statement)], prev=snapshot)
        self.assertEqual(len(today), 1)

    def test_changed_timing_is_not_erased_from_seen_key(self):
        _, snapshot = audit([turn("I need to pay the water bill this month.")])
        found, _ = audit([turn("I need to pay the water bill next month.")], prev=snapshot)
        self.assertEqual(len(found), 1)

    def test_seen_eviction_retains_actual_newest_key(self):
        prior = {"said_seen": ["v2:2000-01-01:old" + str(i) for i in range(400)]}
        _, snapshot = audit([turn("I need to pay the water bill.")], prev=prior)
        self.assertEqual(len(snapshot["said_seen"]), 400)
        self.assertNotIn("v2:2000-01-01:old0", snapshot["said_seen"])
        self.assertIn("water", snapshot["said_seen"][-1])


class ItStaysFree(unittest.TestCase):
    def test_the_module_still_makes_no_model_call(self):
        """selfaudit's whole premise: 'a guard whose price scales with how broken things are
        will get turned off exactly when it matters.'"""
        src = Path(sa.__file__).read_text()
        for banned in ("anthropic", "messages.create", "_anthropic", "LEARN_MODEL"):
            self.assertNotIn(banned, src, f"selfaudit reached for {banned}")

    def test_turns_are_collected_for_it(self):
        import inspect
        self.assertIn("recent_turns", inspect.getsource(sa._collect))


if __name__ == "__main__":
    unittest.main()
