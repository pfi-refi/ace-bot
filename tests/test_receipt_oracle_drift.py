"""The receipt oracle must keep agreeing with the writer it is checking.

WHY THIS FILE EXISTS (2026-09-21). `db.update_item_verified` no longer accepts "something in
the row moved" as proof that the requested edit landed — Codex reproduced two ways that lie:
an accepted write whose saved title differs from the requested one was reported COMPLETED, and
an already-satisfied request was reported as a failure. The repair was
`db._expected_item_values`, which re-derives what each field SHOULD read after the write and
compares that against the row actually read back.

That repair is right, and it carries one hazard worth pinning down. The oracle re-implements
`update_item`'s normalisation — `pin_due`, `canon_tags`, `.strip()`, the 300-character
`next_step` cap, `""`→`'active'` for state, the rule that leaving WAITING drops the owner. Two
copies of one rule set drift. And the drift is silent in the worst direction: if the oracle
disagrees with the writer, every ordinary board edit starts failing verification, falls to
ops.REPORTED, and Brady is told "Outcome unconfirmed" for a write that landed perfectly —
which is precisely the live defect this release exists to remove, reintroduced through its own
fix.

So this is not a test of the oracle's opinion. It runs the REAL writer against a REAL
Postgres, reads the row back, and asserts the oracle predicted that row exactly. If someone
changes a normalisation rule in `update_item` and not in `_expected_item_values`, this fails
here — loudly, at the source — instead of degrading production receipts into warnings.

Disposable pgserver, synthetic rows, no network, no model calls, no production database.
"""
import json
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ace2"))


def _server():
    import pgserver
    tmp = tempfile.TemporaryDirectory(prefix="ace-oracle-pg-")
    srv = pgserver.get_server(Path(tmp.name) / "data", cleanup_mode="delete")
    return tmp, srv


class ReceiptOracleDrift(unittest.TestCase):
    """Every edit shape the tool can emit, predicted then verified against storage."""

    @classmethod
    def setUpClass(cls):
        cls._tmp, cls._srv = _server()
        os.environ["DATABASE_URL"] = cls._srv.get_uri()
        os.environ["ACE2_ALLOW_OPEN"] = "1"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        from ace2.backend import db
        cls.db = db
        db._init_schema()
        db._ready = True
        db._trgm_ok = False

    @classmethod
    def tearDownClass(cls):
        try:
            cls._srv.cleanup()
        except Exception:
            pass
        cls._tmp.cleanup()

    def _row(self, **cols):
        """One synthetic board row. Returns its id."""
        db = self.db
        item_id = uuid.uuid4().hex[:8]
        base = {"kind": "todo", "text": "seeded row " + item_id, "status": "open",
                "tags": ["Business"], "entry": "action", "state": None, "waiting_on": None,
                "due": None, "next_step": None, "followup": None, "bucket": None}
        base.update(cols)
        with db._conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO daybank_items(id, ts, kind, text, status, tags, due, entry, "
                "state, waiting_on, next_step, followup, bucket) "
                "VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)",
                (item_id, datetime.now(timezone.utc) - timedelta(days=1), base["kind"],
                 base["text"], base["status"], json.dumps(base["tags"]), base["due"],
                 base["entry"], base["state"], base["waiting_on"], base["next_step"],
                 base["followup"], base["bucket"]))
        return item_id

    def _agree(self, label, seed, requested):
        """The writer writes, the oracle predicts, and the two must describe one row.

        Asserted through `update_item_verified` itself so the production decision — did this
        edit verify — is what is being checked, not a reimplementation of it.
        """
        db = self.db
        item_id = self._row(**seed)
        before = db.get_item(item_id)
        self.assertIsNotNone(before, label + ": seeded row did not read back")
        expected = db._expected_item_values(before, requested)
        ok, detail = db.update_item_verified(item_id, **requested)
        after = detail.get("after") or {}
        self.assertTrue(detail.get("accepted"), f"{label}: store refused — {detail.get('reason')}")
        self.assertTrue(detail.get("read_back"), label + ": row did not read back")
        drifted = {k: {"oracle_predicted": v, "postgres_stored": after.get(k)}
                   for k, v in expected.items() if after.get(k) != v}
        self.assertEqual(
            {}, drifted,
            f"{label}: db._expected_item_values disagrees with what update_item actually "
            f"stored. The oracle and the writer have drifted, so real edits will now be "
            f"reported as unverified. Fix the oracle to match the writer (or the writer, if "
            f"the writer is what changed wrongly): {drifted}")
        self.assertTrue(ok, f"{label}: verification failed — {detail.get('reason')}")
        self.assertTrue(detail.get("verified"), label + ": not marked verified")
        return detail

    # ── one case per normalisation rule the oracle re-implements ────────────────

    def test_plain_text_edit_is_predicted(self):
        d = self._agree("text edit", {}, {"text": "a renamed obligation"})
        self.assertIn("text", d["changed"])
        self.assertNotIn("status", d["changed"], "a text edit must not move status")

    def test_text_is_stripped_the_same_way_by_both(self):
        d = self._agree("whitespace", {}, {"text": "   padded title   "})
        self.assertEqual("padded title", (d["after"] or {}).get("text"))

    def test_due_goes_through_pin_due_in_both(self):
        self._agree("due set", {}, {"due": "2026-12-01"})

    def test_clearing_due_is_predicted(self):
        self._agree("due cleared", {"due": "2026-12-01"}, {"due": ""})

    def test_tags_go_through_canon_tags_in_both(self):
        self._agree("tags", {}, {"tags": ["deals", "Money"]})

    def test_empty_state_stores_active_in_both(self):
        d = self._agree("state cleared", {"state": "waiting", "waiting_on": "Tony"},
                        {"state": ""})
        self.assertEqual("active", (d["after"] or {}).get("state"))

    def test_leaving_waiting_drops_the_owner_in_both(self):
        """The rule that makes a stale owner impossible — and the oracle has to know it."""
        d = self._agree("unpark", {"state": "waiting", "waiting_on": "Tony"},
                        {"state": "active"})
        self.assertIsNone((d["after"] or {}).get("waiting_on"),
                          "leaving WAITING must clear waiting_on")

    def test_explicit_owner_survives_a_state_change_in_both(self):
        d = self._agree("unpark with owner", {"state": "waiting", "waiting_on": "Tony"},
                        {"state": "waiting", "waiting_on": "Dana"})
        self.assertEqual("Dana", (d["after"] or {}).get("waiting_on"))

    def test_next_step_cap_matches(self):
        self._agree("next_step cap", {}, {"next_step": "x" * 400})

    def test_clearing_next_step_is_predicted(self):
        self._agree("next_step cleared", {"next_step": "chase the title company"},
                    {"next_step": ""})

    def test_followup_goes_through_pin_due_in_both(self):
        self._agree("followup", {}, {"followup": "2026-11-05"})

    def test_completion_is_predicted(self):
        d = self._agree("completion", {}, {"status": "done"})
        self.assertEqual("done", (d["after"] or {}).get("status"))

    def test_reopen_is_predicted(self):
        d = self._agree("reopen", {"status": "done"}, {"status": "open"})
        self.assertEqual("open", (d["after"] or {}).get("status"))

    def test_multi_field_edit_is_predicted(self):
        self._agree("multi", {}, {"text": "renamed and dated", "due": "2026-10-09",
                                  "next_step": "send the packet"})

    def test_omitted_fields_are_predicted_as_preserved(self):
        """The third of Codex's reproductions: a title edit silently moving waiting state."""
        d = self._agree("omission", {"state": "waiting", "waiting_on": "Tony"},
                        {"text": "retitled while parked"})
        after = d["after"] or {}
        self.assertEqual("waiting", after.get("state"),
                         "a title edit must not disturb an omitted waiting state")
        self.assertEqual("Tony", after.get("waiting_on"),
                         "a title edit must not disturb an omitted owner")

    def test_an_already_satisfied_request_verifies_with_no_changes(self):
        """Verified and unchanged is a real, honest outcome — not a failure."""
        db = self.db
        item_id = self._row(text="already exactly this")
        ok, detail = db.update_item_verified(item_id, text="already exactly this")
        self.assertTrue(ok, "an already-satisfied request must verify")
        self.assertTrue(detail.get("verified"))
        self.assertEqual({}, detail.get("changed"),
                         "nothing moved, so nothing may be reported as moved")

    def test_a_mismatch_against_the_requested_value_does_not_verify(self):
        """The guard itself: if storage disagrees with the request, this must not pass.

        Simulated by asking the oracle about a value the writer was never given, which is the
        shape of Codex's first reproduction (accepted write, different persisted title).
        """
        db = self.db
        item_id = self._row(text="the stored title")
        before = db.get_item(item_id)
        expected = db._expected_item_values(before, {"text": "a title nobody saved"})
        self.assertNotEqual(expected["text"], before["text"],
                            "the oracle must demand the REQUESTED value, not the stored one")


if __name__ == "__main__":
    unittest.main(verbosity=2)
