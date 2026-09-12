"""Budget currency parsing must preserve cents and never invent a partial amount."""
import sys
import unittest
from pathlib import Path
from decimal import Decimal
from datetime import date
from unittest.mock import patch, AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ace2"))
from backend.integrations import bills_sheet as bs

class ExactCurrency(unittest.TestCase):
    def test_whole_currency_and_plain_sheet_numbers(self):
        for cell, expected in [("$1,234.56", "1234.56"), (473, "473.00"),
                               ("247.79", "247.79"), ("$-50", "-50.00"),
                               ("-$50", "-50.00"), ("($50.20)", "-50.20"),
                               ("0", "0.00")]:
            with self.subTest(cell=cell):
                self.assertEqual(bs._money(cell), Decimal(expected))

    def test_ambiguous_or_overprecision_amount_is_not_partially_extracted(self):
        for value in ("$1,234.567", "$50 or $75", "about $50", "$1,23", "NaN", "inf"):
            self.assertIsNone(bs._money(value), value)

    def test_cents_sum_exactly_and_format_without_binary_float_conversion(self):
        bills = bs.parse_bills([
            ["Bill / Expense", "Due Day", "Monthly Amount"],
            ["Small A", "12", "0.10"], ["Small B", "12", "$0.20"],
        ], date(2026, 9, 12))
        self.assertEqual(sum(b["amount"] for b in bills), Decimal("0.30"))
        block = bs.format_due_soon(bills, today=date(2026, 9, 12))
        self.assertIn("$0.10", block)
        self.assertIn("$0.20", block)


    def test_unpaid_notes_deadline_does_not_disappear_after_it_passes(self):
        rows = [["Bill / Expense", "Due Day", "Monthly Amount", "Paid?", "Notes"],
                ["Stop disconnection", "", "$247.79", "", "MINIMUM by Sept 14, 2026"]]
        today = date(2026, 9, 15)
        block = bs.format_due_soon(bs.parse_bills(rows, today), today=today)
        self.assertIn("$247.79", block)
        self.assertIn("sheet row 2", block)
        self.assertIn("date from Notes", block)
        self.assertIn("date passed 1d ago; not marked paid", block)
        rows[1][3] = "paid"
        block = bs.format_due_soon(bs.parse_bills(rows, today), today=today)
        self.assertNotIn("247.79", block)

class SheetReadFailures(unittest.IsolatedAsyncioTestCase):
    async def test_unresolved_amount_preserves_verified_rows_with_incomplete_warning(self):
        for unresolved in ("$50 or $75", "-"):
            with self.subTest(unresolved=unresolved):
                rows = [["Bill / Expense", "Due Day", "Monthly Amount", "Notes"],
                        ["Valid bill", "12", "$50", ""],
                        ["Unclear bill", "12", unresolved, "Future move to $106"]]
                with patch.object(bs.asyncio, "to_thread", AsyncMock(return_value=rows)):
                    bills, error = await bs.fetch_bills(date(2026, 9, 12))
                self.assertEqual(len(bills), 1)
                self.assertEqual(bills[0]["name"], "Valid bill")
                self.assertEqual(bills[0]["amount"], Decimal("50.00"))
                self.assertIn("row(s) 3", error)
                self.assertIn("INCOMPLETE BILL LIST", error)
                self.assertIn("excluded, not zero", error)
                self.assertIn("Do not infer", error)

    async def test_missing_source_never_substitutes_board_figures(self):
        with patch.object(bs.asyncio, "to_thread", AsyncMock(side_effect=RuntimeError("offline"))):
            bills, error = await bs.fetch_bills()
        self.assertEqual(bills, [])
        self.assertIn("Do not substitute board figures", error)
