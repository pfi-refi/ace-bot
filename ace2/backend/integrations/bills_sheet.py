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

import logging
import re
from datetime import datetime, timedelta

from ..db import EASTERN

logger = logging.getLogger("ace2.bills")

# Brady's "Monthly Bills, Debts, Subscriptions, Income" workbook.
SHEET_ID = "1jLIskX1IYDnt4T5DuEKUxqD_LjZxqIT3NPuTusGP7nw"
BILLS_TAB = "A1:F60"          # tab 1 — Bill / Due Day / Monthly Amount / Paid? / Paid From / Notes

_MONEY = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_DAY = re.compile(r"^\s*(\d{1,2})\s*(?:st|nd|rd|th)?\s*$", re.I)


def _money(cell: str):
    m = _MONEY.search(cell or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
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


def parse_bills(rows: list, today=None) -> list:
    """Turn the raw sheet grid into bill dicts. Section headers and totals are skipped.

    Kept deliberately tolerant: a row only counts as a bill if it has a name and an amount,
    so blank spacers, merged section banners and the TOTAL block fall out on their own.
    """
    today = today or datetime.now(EASTERN).date()
    out, section = [], ""
    for row in rows or []:
        cells = [(c or "").strip() for c in (list(row) + ["", "", "", "", "", ""])[:6]]
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
            continue
        day = _day(due_cell)
        out.append({
            "name": name, "section": section, "amount": amount,
            "day": day, "due_on": _next_occurrence(day, today) if day else None,
            "paid": bool(paid_cell), "paid_from": from_cell, "notes": notes,
        })
    return out


async def fetch_bills(today=None) -> tuple:
    """(bills, error). Never raises — a money answer must fail loudly, not silently."""
    from . import mcp_client
    if not mcp_client.enabled():
        return [], "the Workspace connector is off, so I can't reach your budget sheet"
    # The MCP server's parameter spelling is not exposed anywhere we can read (/diag/mcp lists
    # tool NAMES only), and guessing it wrong would fail silently — the brief would just lose
    # its money block with no error anyone sees. So try the known conventions and take the
    # first that answers. Whichever wins is logged, so this can be pinned later.
    attempts = [
        {"spreadsheet_id": SHEET_ID, "range_name": BILLS_TAB},
        {"spreadsheet_id": SHEET_ID, "range": BILLS_TAB},
        {"spreadsheetId": SHEET_ID, "rangeName": BILLS_TAB},
        {"spreadsheetId": SHEET_ID, "range": BILLS_TAB},
    ]
    raw, last = "", ""
    for args in attempts:
        out = await mcp_client.call("mcp_read_sheet_values", args)
        if out and not out.startswith("⚠️") and "(no content" not in out:
            logger.info("bills sheet read OK with params %s", sorted(args))
            raw = out
            break
        last = out or ""
    if not raw:
        return [], (last or "no answer from the sheet").lstrip("⚠️ ").strip()
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        rows.append([c.strip() for c in line.split("\t")] if "\t" in line
                    else [c.strip() for c in line.split("|")])
    bills = parse_bills(rows, today)
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
    soon = [b for b in bills if b["due_on"] and today <= b["due_on"] <= horizon and not b["paid"]]
    soon.sort(key=lambda b: b["due_on"])
    if not soon:
        return "(nothing due in the next %d days)" % within_days
    lines = []
    for b in soon:
        when = (b["due_on"] - today).days
        label = "TODAY" if when == 0 else "tomorrow" if when == 1 else "in %dd" % when
        line = "  %s — $%.2f — %s" % (b["name"], b["amount"], label)
        if b["notes"]:
            line += "  [%s]" % b["notes"][:90]
        lines.append(line)
    return "\n".join(lines)
