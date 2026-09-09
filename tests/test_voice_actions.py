"""Voice-to-action: the task lifecycle, and the spreadsheet acceptance case.

Mocks and fakes only — no provider, no network, no model, no paid call. The disposable
Postgres coverage (idempotency, multiple tabs, reconnect, terminal immutability) lives in
tests/voice_actions_check.py; these are the pure units.
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ace2'))
from backend import capabilities as cp                # noqa: E402

ROWS = [["Assistant", "Model", "Monthly"],
        ["Northwind Helper", "flat", "$20"],
        ["Cedar Assist", "per-request", "$0.004"]]


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class Fake:
    """A provider that behaves like the real MCP text interface: flattened strings."""

    def __init__(self, **script):
        self.script = script
        self.calls = []
        self.sheet = {}

    async def __call__(self, tool, args):
        self.calls.append((tool, args))
        if tool in self.script:
            v = self.script[tool]
            return v(args) if callable(v) else v
        if tool == "mcp_create_spreadsheet":
            return '{"spreadsheetId": "1FakeSheetIdAbCdEfGhIjKlMnOpQrStUv"}'
        if tool == "mcp_modify_sheet_values":
            self.sheet[args.get("range")] = args.get("values")
            return '{"updatedCells": 9}'
        if tool == "mcp_read_sheet_values":
            return __import__("json").dumps({"values": self.sheet.get(args.get("range"), [])})
        if tool.startswith("mcp_get_drive_file") or tool == "mcp_search_drive_files":
            return '{"owners":[{"emailAddress":"brady@example.com"}]}'
        return "(no content returned)"


class TheIdMustComeFromTheProvider(unittest.TestCase):
    """The 9 September link was composed from the model's prose. It cannot be, ever again."""

    def test_prose_yields_no_id(self):
        self.assertEqual(cp.extract_id("The sheet's built and populated with all 9 rows"), "")

    def test_a_missing_id_fails_instead_of_inventing_a_link(self):
        f = Fake(mcp_create_spreadsheet="Created the spreadsheet successfully.")
        with self.assertRaises(cp.Failed) as e:
            run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertIn("did not return a spreadsheet id", str(e.exception))
        self.assertNotIn("mcp_modify_sheet_values", [c[0] for c in f.calls])

    def test_the_link_is_built_from_the_verified_id(self):
        f = Fake()
        out = run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertEqual(out["file_id"], "1FakeSheetIdAbCdEfGhIjKlMnOpQrStUv")
        self.assertEqual(out["url"],
                         "https://docs.google.com/spreadsheets/d/1FakeSheetIdAbCdEfGhIjKlMnOpQrStUv/edit")

    def test_an_id_is_read_out_of_a_returned_url(self):
        f = Fake(mcp_create_spreadsheet="Done: https://docs.google.com/spreadsheets/d/"
                                        "1kfs_qmLhf-fD-h7VWlQAT5fX3KVtgiRacSWTRpFyFKI/edit")
        out = run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertEqual(out["file_id"], "1kfs_qmLhf-fD-h7VWlQAT5fX3KVtgiRacSWTRpFyFKI")


class ContentsAreVerifiedNotAssumed(unittest.TestCase):
    def test_a_successful_run_reads_back_and_reports_what_it_checked(self):
        f = Fake()
        out = run(cp.create_spreadsheet({"title": "Pricing", "rows": ROWS}, f))
        self.assertIn("mcp_read_sheet_values", [c[0] for c in f.calls])
        self.assertEqual(out["rows_written"], 3)
        self.assertGreaterEqual(out["cells_verified"], 3)

    def test_a_sheet_that_reads_back_empty_is_not_a_success(self):
        f = Fake(mcp_read_sheet_values='{"values": []}')
        with self.assertRaises(cp.Failed) as e:
            run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertIn("unverified", str(e.exception))

    def test_missing_contents_are_named_not_glossed(self):
        f = Fake(mcp_read_sheet_values='{"values": [["Assistant","Model","Monthly"]]}')
        with self.assertRaises(cp.Failed) as e:
            run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertIn("does not contain what was written", str(e.exception))
        self.assertIn("Northwind Helper", str(e.exception))

    def test_a_write_failure_reports_the_partial_file(self):
        f = Fake(mcp_modify_sheet_values="Error: range is invalid")
        with self.assertRaises(cp.Failed) as e:
            run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertTrue(e.exception.result.get("partial"))
        self.assertTrue(e.exception.result.get("file_id"))


class ProviderFailuresAreNotContent(unittest.TestCase):
    def test_permission_denied_is_a_failure(self):
        f = Fake(mcp_create_spreadsheet="Error 403: permission denied for this account")
        with self.assertRaises(cp.Failed) as e:
            run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertIn("refused to create", str(e.exception))

    def test_the_mcp_error_marker_is_recognised(self):
        f = Fake(mcp_create_spreadsheet="⚠️ MCP create_spreadsheet reported an error: nope")
        with self.assertRaises(cp.Failed):
            run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))

    def test_an_empty_result_is_a_failure_not_a_blank_success(self):
        f = Fake(mcp_create_spreadsheet="(no content returned)")
        with self.assertRaises(cp.Failed):
            run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))


class AccessIsCheckedNotAssumed(unittest.TestCase):
    """Brady's "file does not exist" is what a wrong-account file looks like."""

    def setUp(self):
        self._was = cp.EXPECTED_USER

    def tearDown(self):
        cp.EXPECTED_USER = self._was

    def test_a_file_owned_by_another_account_is_not_announced_as_ready(self):
        cp.EXPECTED_USER = "brady@example.com"
        f = Fake(mcp_get_drive_file_metadata='{"owners":[{"emailAddress":"robot@svc.iam"}]}')
        with self.assertRaises(cp.Failed) as e:
            run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertIn("file does not exist", str(e.exception))
        self.assertIn("robot@svc.iam", str(e.exception))

    def test_and_no_sharing_is_created_to_paper_over_it(self):
        cp.EXPECTED_USER = "brady@example.com"
        f = Fake(mcp_get_drive_file_metadata='{"owners":[{"emailAddress":"robot@svc.iam"}]}')
        with self.assertRaises(cp.Failed):
            run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertNotIn("mcp_get_drive_shareable_link", [c[0] for c in f.calls])

    def test_the_owners_own_link_is_not_a_sharing_change(self):
        cp.EXPECTED_USER = "brady@example.com"
        f = Fake()
        out = run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertEqual(out["access"], "owner")
        self.assertIn("no sharing permission", out["link_kind"])

    def test_unknown_ownership_is_flagged_rather_than_promised(self):
        cp.EXPECTED_USER = "brady@example.com"
        f = Fake(mcp_get_drive_file_metadata="Error: not available",
                 mcp_get_drive_file_info="Error: not available",
                 mcp_search_drive_files="Error: not available")
        out = run(cp.create_spreadsheet({"title": "T", "rows": ROWS}, f))
        self.assertEqual(out["access"], "unknown")
        self.assertTrue(any("cannot promise" in w for w in out["warnings"]))


class UnsupportedFormattingIsReportedNotAttempted(unittest.TestCase):
    def test_bold_header_is_declared_unsupported(self):
        f = Fake()
        out = run(cp.create_spreadsheet(
            {"title": "T", "rows": ROWS, "bold_header": True}, f))
        self.assertTrue(any("bolding" in w for w in out["warnings"]))

    def test_and_no_docs_tool_is_aimed_at_a_spreadsheet(self):
        # On 9 September mcp_modify_doc_text — a Docs tool — was called with a spreadsheet
        # id to bold a header, and sat unapproved while Ace said the sheet was done.
        f = Fake()
        run(cp.create_spreadsheet({"title": "T", "rows": ROWS, "bold_header": True}, f))
        self.assertNotIn("mcp_modify_doc_text", [c[0] for c in f.calls])


class NothingIsInvented(unittest.TestCase):
    def test_no_title_no_file(self):
        f = Fake()
        with self.assertRaises(cp.Failed):
            run(cp.create_spreadsheet({"rows": ROWS}, f))
        self.assertEqual(f.calls, [])

    def test_no_contents_no_file(self):
        f = Fake()
        with self.assertRaises(cp.Failed):
            run(cp.create_spreadsheet({"title": "T", "rows": []}, f))
        self.assertEqual(f.calls, [])


class TheVoiceToolCannotClaimAResult(unittest.TestCase):
    def test_the_schema_forbids_announcing_completion(self):
        from backend import tools
        d = tools.START_TASK["description"]
        for phrase in ("has not run yet", "never invent", "Never say it is built"):
            self.assertIn(phrase, d)

    def test_the_retired_handoff_is_not_offered_to_voice(self):
        from backend import chat
        names = [t["name"] for t in chat.VOICE_TOOLS]
        self.assertIn("start_task", names)
        self.assertNotIn("build_on_screen", names)

    def test_typed_and_voice_share_the_capability(self):
        from backend import tools
        # One registry, one handler. The typed loop is handed the same tool.
        self.assertIn("create_spreadsheet", cp.REGISTRY)
        self.assertEqual(cp.REGISTRY["create_spreadsheet"]["handler"], cp.create_spreadsheet)
        self.assertEqual(tools.START_TASK["input_schema"]["properties"]["capability"]["enum"],
                         ["create_spreadsheet"])
