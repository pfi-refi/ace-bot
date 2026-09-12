"""
Brady's budget spreadsheet as the SINGLE SOURCE OF TRUTH for money figures.

WHY THIS EXISTS (2026-09-05). Bill amounts lived in board-item prose, where nobody re-read
them, so they rotted. The truck row said "$500/mo, due 17th" in its header while its own last
sentence said "$473 resuming 9/15" — and that stale $500 leaked into Brady's recaps FOUR times
over three days. Each time the prose got patched; the row never did, and five memory rows kept
asserting the dead figure underneath. A comparison against the real sheet found SIX of nine
bills wrong on the board, including an electric account nine days from disconnection that the
board showed as a routine $276.

Brady's call: "i dont think we can do the hard code because thats how the bills are and
sometimes its better giving that to him and him store that." He is right. He maintains this
sheet anyway, in a tool built for it. So Ace reads it and stores nothing.

LIVE, NOT SYNCED. A nightly copy would just recreate a 24-hour staleness window — the exact
bug this replaces. If Drive is unreachable Ace says so; an honest "I can't reach the sheet"
beats a confident wrong number, which is what the last three weeks were.

The sheet is Brady's to restructure. Nothing here assumes a fixed cell layout: rows are found
by their header names, so moving a column or inserting a section does not break the reader.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from ..db import EASTERN

logger = logging.getLogger("ace2.bills")

# Brady's "Monthly Bills, Debts, Subscriptions, Income" workbook.
SHEET_ID = "1jLIskX1IYDnt4T5DuEKUxqD_LjZxqIT3NPuTusGP7nw"
BILLS_TAB = "'1. Bills & Expenses'!A1:L1000"          # tab 1 — Bill / Due Day / Monthly Amount / Paid? / Paid From / Notes

_MONEY = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?")
_DAY = re.compile(r"^\s*(\d{1,2})\s*(?:st|nd|rd|th)?\s*$", re.I)


def _money(cell):
    """Exact cents from a whole currency cell; never extract part of ambiguous prose."""
    value = str(cell).strip() if cell is not None else ""
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1].strip()
    if value.startswith("-"):
        if negative:
            return None
        negative, value = True, value[1:].strip()
    if value.startswith("$"):
        value = value[1:].strip()
    if value.startswith("-"):
        if negative:
            return None
        negative, value = True, value[1:].strip()
    if not _MONEY.fullmatch(value):
        return None
    try:
        amount = Decimal(value.replace(",", ""))
        return (-amount if negative else amount).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _day(cell: str):
    """'14th' -> 14. Returns None for '-' and anything that is not a day of the month."""
    m = _DAY.match((cell or "").strip())
    if not m:
        return None
    d = int(m.group(1))
    return d if 1 <= d <= 31 else None


def _next_occurrence(day: int, today):
    """The next date this day-of-month falls on, honouring short months.

    A day that has ALREADY passed this month is next month's — with no grace window, unlike
    the board's own parser. The sheet says what is owed on a cycle; whether Brady paid it is
    the "Paid?" column's job, not a date heuristic's.
    """
    y, m = today.year, today.month
    for _ in range(13):
        try:
            d = today.replace(year=y, month=m, day=day)
        except ValueError:                     # e.g. the 31st in a 30-day month
            m, y = (m + 1, y) if m < 12 else (1, y + 1)
            continue
        if d >= today:
            return d
        m, y = (m + 1, y) if m < 12 else (1, y + 1)
    return None


def parse_bills(rows: list, today=None, amount_errors=None) -> list:
    """Turn the raw sheet grid into bill dicts. Section headers and totals are skipped.

    Kept deliberately tolerant: a row only counts as a bill if it has a name and an amount,
    so blank spacers, merged section banners and the TOTAL block fall out on their own.
    """
    today = today or datetime.now(EASTERN).date()
    out, section = [], ""
    columns = None
    for row_number, row in enumerate(rows or [], 1):
        raw = [str(c).strip() if c is not None else "" for c in row]
        if "Bill / Expense" in raw and "Monthly Amount" in raw:
            columns = {label: i for i, label in enumerate(raw)}
            continue
        if columns is None:
            continue
        labels = ("Bill / Expense", "Due Day", "Monthly Amount", "Paid?", "Paid From", "Notes")
        cells = [raw[columns[label]] if label in columns and columns[label] < len(raw) else "" for label in labels]
        name, due_cell, amount_cell, paid_cell, from_cell, notes = cells
        if not name:
            continue
        low = name.lower()
        # section banners repeat across every column; totals are not bills
        if len(set(c for c in cells if c)) == 1 and len(name) > 3:
            section = name
            continue
        if low.startswith(("total", "needed per", "by category", "one-time", "held in")):
            continue
        if low in ("bill / expense", "bill/expense"):
            continue
        amount = _money(amount_cell)
        if amount is None:
            if amount_cell and amount_errors is not None:
                amount_errors.append(row_number)
            continue
        day = _day(due_cell)
        note_due = None
        if not day:
            # A date in Notes is evidence to display, never silently discarded.
            hit = re.search(r"(?:due|by|before)\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?", notes, re.I)
            if hit:
                month = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"].index(hit[1][:3].lower()) + 1
                try:
                    note_due = today.replace(year=int(hit[3] or today.year), month=month, day=int(hit[2]))
                except ValueError:
                    pass
        out.append({
            "source_row": row_number, "date_from_notes": bool(note_due),
            "name": name, "section": section, "amount": amount,
            "source_row": row_number, "date_from_notes": bool(note_due),
            "day": day, "due_on": _next_occurrence(day, today) if day else note_due,
            "paid": paid_cell.casefold() in ("yes", "paid", "true", "✓", "✔", "✅"), "paid_from": from_cell, "notes": notes,
        })
    return out


async def fetch_bills(today=None) -> tuple:
    """(bills, error). Never raises — a money answer must fail loudly, not silently."""
    # Structured Sheets values avoid guessed MCP argument names and parsing prose.
    # Reuse Ace's existing Google credential flow; missing access fails explicitly.
    def read():
        from googleapiclient.discovery import build
        from .google_client import get_google_creds
        service = build("sheets", "v4", credentials=get_google_creds(), cache_discovery=False)
        return service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=BILLS_TAB,
            valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
    try:
        rows = await asyncio.to_thread(read)
    except Exception as e:
        logger.warning("budget sheet read unavailable: %s", type(e).__name__)
        return [], "I could not verify the budget spreadsheet. Do not substitute board figures."
    amount_errors = []
    bills = parse_bills(rows, today, amount_errors)
    if amount_errors:
        return bills, ("INCOMPLETE BILL LIST: I verified the amounts shown below, but could "
                       "not verify the amount in spreadsheet row(s) " +
                       ", ".join(map(str, amount_errors)) +
                       ". Those rows are excluded, not zero. Do not infer their amounts "
                       "from notes or board text, or claim a complete total until clarified.")
    if not bills:
        return [], "I reached the sheet but could not read any bill rows out of it"
    return bills, ""


def format_due_soon(bills: list, within_days: int = 10, today=None) -> str:
    """The money block for a brief: only what lands inside the window, cheapest formatting.

    Deliberately NOT the whole register. Reciting thirty bills is what made the old briefs
    unreadable; what Brady needs at 9am is the two or three that bite this week.
    """
    today = today or datetime.now(EASTERN).date()
    horizon = today + timedelta(days=within_days)
    soon = [b for b in bills if b["due_on"] and not b["paid"] and
            (today <= b["due_on"] <= horizon or
             (b.get("date_from_notes") and b["due_on"] < today))]
    soon.sort(key=lambda b: b["due_on"])
    if not soon:
        return "(nothing due in the next %d days)" % within_days
    lines = []
    for b in soon:
        when = (b["due_on"] - today).days
        label = (f"date passed {-when}d ago; not marked paid" if when < 0 else
                 "TODAY" if when == 0 else "tomorrow" if when == 1 else "in %dd" % when)
        line = (f"  {b['name']} — ${b['amount']:.2f} — {label} "
                f"(sheet row {b.get('source_row', '?')})")
        if b.get("date_from_notes"):
            line += " [date from Notes; verify year if omitted]"
        if b["notes"]:
            line += "  [%s]" % b["notes"][:90]
        lines.append(line)
    return "\n".join(lines)
