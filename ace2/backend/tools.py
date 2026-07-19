"""
Ace 2.0 tools — real actions, exposed to Claude via the Messages API.

This is the whole point of the rebuild: Ace ACTS. No inline text-tags (the
portal's legacy path); Claude calls these as first-class tools and the model
never sees a tag to regress into.

Each tool has a schema (sent to the API) and an executor (run when Claude calls
it). Executors return a short human-readable confirmation string — the same
string surfaces to Brady in the transcript and the intel feed. Executors never
raise; a failure comes back as a ⚠️ string so one bad tool call never kills the
turn.

MIRROR of the intent of ace-bot/bot.py ACE_TOOLS (create_calendar_event,
delete_calendar_event, add_task, complete_task, send_email, search_drive) plus
draft_email, which the portal's integrations have and the bot doesn't. Keep the
schemas behaviourally aligned with the bot so "one Ace" stays true.

Blocking Google I/O — every executor is wrapped in asyncio.to_thread by the
caller (chat.py). Do not call these straight from the event loop.
"""

import logging
from datetime import datetime, timedelta

from . import brain, daybank
from .integrations.calendar_api import (
    create_calendar_event,
    delete_calendar_event,
    get_calendar_range,
)
from .integrations.google_client import EASTERN
from .integrations.tasks_api import (
    add_task,
    complete_task,
    draft_email,
    read_gmail,
    search_drive,
    search_gmail,
    send_email,
)

logger = logging.getLogger("ace2.tools")

DEFAULT_TASK_LIST = "Admin List - back log"


# ── Schemas (sent to the Anthropic API) ─────────────────────────────────────────
TOOLS = [
    {
        "name": "create_calendar_event",
        "description": (
            "Create a new event on Brady's Google Calendar. Use when Brady asks to "
            "schedule, book, add, or block time for something. Execute immediately — "
            "do not ask for confirmation unless the date/time is genuinely ambiguous."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start_datetime": {
                    "type": "string",
                    "description": "Start in ISO format YYYY-MM-DDTHH:MM:SS (e.g. 2026-07-17T14:00:00). Resolve to a concrete date.",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "End in ISO format YYYY-MM-DDTHH:MM:SS. If unspecified, default to 1 hour after start.",
                },
                "description": {"type": "string", "description": "Optional notes"},
            },
            "required": ["title", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": (
            "Delete or cancel an event from Brady's Google Calendar. Use when Brady asks "
            "to cancel, remove, or delete a meeting. Deletes calendar events only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_title": {"type": "string", "description": "Title or keyword to match"},
                "event_date": {
                    "type": "string",
                    "description": "Date of the event, ISO YYYY-MM-DD, to narrow the search",
                },
            },
            "required": ["event_title", "event_date"],
        },
    },
    {
        "name": "add_task",
        "description": (
            "Add a task to Brady's Google Tasks. Use when Brady asks to add, create, "
            "remember, or track a task, action item, or follow-up. "
            f"Default list: '{DEFAULT_TASK_LIST}' unless Brady names another."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "list_name": {"type": "string", "description": f"Task list. Default '{DEFAULT_TASK_LIST}'"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "complete_task",
        "description": (
            "Mark an existing task complete in Google Tasks. Use when Brady says a task "
            "is done, finished, or handled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_title": {"type": "string", "description": "Title or keyword to match"},
            },
            "required": ["task_title"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "Send an email from Brady's Gmail (pfi@platinumfortuneimpact.com). "
            "Only use when Brady EXPLICITLY says to send. Never send unprompted — if in "
            "doubt, use draft_email instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Subject line"},
                "body": {"type": "string", "description": "Plain-text body"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "draft_email",
        "description": (
            "Create a Gmail draft (does NOT send). Use when Brady wants an email written "
            "for review, or when a send isn't explicitly authorized."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Subject line"},
                "body": {"type": "string", "description": "Plain-text body"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "search_gmail",
        "description": (
            "Search Brady's WHOLE mailbox (not just unread) and get back matching emails "
            "with an id + a snippet. Use whenever he asks to find, look up, or check an email "
            "— 'did X email me', 'find the email about Y', 'what did the lender say'. Supports "
            "Gmail search operators: from:, to:, subject:, keywords, newer_than:7d, older_than:, "
            "has:attachment, is:unread. To read one in full, pass its id to read_gmail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query (operators supported)"},
                "max_results": {"type": "integer", "description": "How many to return (1-20, default 8)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_gmail",
        "description": (
            "Read the FULL body of one email by its id (from search_gmail). Use when Brady "
            "wants the details of a specific email, or before you summarize/answer/draft a reply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The email id from search_gmail"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "get_calendar_range",
        "description": (
            "Read Brady's calendar for ANY window — past or future — beyond what's already in "
            "context. Use for 'what did I have last month', 'what's on my calendar the week of "
            "the 20th', 'am I free in three weeks'. start_offset_days shifts the start from "
            "today (negative = past, e.g. -30 ≈ a month ago; positive = future); num_days = "
            "window length."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_offset_days": {"type": "integer", "description": "Days from today to start (negative = past)"},
                "num_days": {"type": "integer", "description": "Window length in days (1-120)"},
            },
            "required": ["start_offset_days", "num_days"],
        },
    },
    {
        "name": "search_drive",
        "description": (
            "Search Brady's Google Drive by name or full-text. Use when Brady asks to "
            "find, look up, or retrieve a file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword or file name"},
            },
            "required": ["query"],
        },
    },
    # ── UI tools — executed by the harness (chat.py), not here. These are what
    # make Ace an assistant instead of a dashboard: HE decides what appears. ──
    {
        "name": "display_card",
        "description": (
            "Materialize a visual card on Brady's screen. Use whenever you talk about "
            "his schedule, tasks, weather, or memory — and after any action that "
            "changes them — so he sees the data, not just hears it. The screen is "
            "yours to populate; there are no menus."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "panel": {
                    "type": "string",
                    "enum": ["calendar", "tasks", "weather", "memory", "timeline", "daybank", "inbox"],
                    "description": (
                        "Which card to display. Use 'timeline' for today's schedule "
                        "laid out against the current time (a live NOW line with what's "
                        "done, now, and next) — prefer it over 'calendar' when Brady "
                        "asks about today or what's next. Use 'daybank' to show his data "
                        "bank (captured to-dos, commitments, notes) — show it after you "
                        "capture something, or when he asks what he's got open."
                    ),
                },
                "where": {
                    "type": "string",
                    "enum": ["left", "right"],
                    "description": (
                        "Optional: which side of the screen to place the card. Use it when "
                        "Brady asks (e.g. 'put my timeline on the left'), or to keep two "
                        "cards side by side. Defaults to a sensible side per card."
                    ),
                },
            },
            "required": ["panel"],
        },
    },
    {
        "name": "open_url",
        "description": (
            "Open a link on Brady's screen (a Drive file, doc, or site). Use when he "
            "asks to open, show, or pull up something that lives at a URL — e.g. right "
            "after search_drive returns a file link."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to open"},
                "label": {"type": "string", "description": "Short human label for the link"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save a durable fact about Brady, PFI, a deal, a person, or how he wants you "
            "to work — so you remember it in future sessions (this is shared with Ace on "
            "Telegram too). Use when Brady tells you something worth keeping, or when you "
            "learn a lasting preference. One concise fact per call. Convert relative dates "
            "to absolute. Don't save trivia or one-off chatter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "One concise fact to remember (~15 words)"},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "capture_item",
        "description": (
            "Capture something into Brady's DATA BANK — his running second brain and "
            "to-do surface. Do this ON YOUR OWN, without being asked, whenever something "
            "worth not forgetting surfaces in conversation: a commitment he made, a "
            "follow-up, a 'don't forget to…', a loose task, or a note. This is how you "
            "manage his to-dos so he doesn't have to live in Google Tasks — capture it "
            "here, and he'll see it on screen. (For a hard-dated appointment, also create "
            "a calendar event; for a note-to-self, just capture it.) One item per call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["note", "todo", "commitment", "followup"],
                    "description": "What kind of item this is",
                },
                "text": {"type": "string", "description": "The item, phrased tightly (~one line)"},
                "due": {"type": "string", "description": "Optional due date/time in plain words (e.g. 'today 5pm', 'Fri')"},
            },
            "required": ["kind", "text"],
        },
    },
    {
        "name": "update_item",
        "description": (
            "Update a DATA BANK item: mark it done (status='done') when Brady says he "
            "finished it or you complete it for him, reopen it, or edit its text. Use the "
            "item's id from the data bank."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The data bank item id"},
                "status": {"type": "string", "enum": ["open", "done"], "description": "New status"},
                "text": {"type": "string", "description": "Optional new text"},
            },
            "required": ["id"],
        },
    },
]

# Short present-tense labels for the WS `tool` event (the orb shows "◈ CREATING EVENT…")
TOOL_LABELS = {
    "create_calendar_event": "CREATING EVENT",
    "delete_calendar_event": "REMOVING EVENT",
    "get_calendar_range": "READING CALENDAR",
    "add_task": "ADDING TASK",
    "complete_task": "COMPLETING TASK",
    "send_email": "SENDING EMAIL",
    "draft_email": "DRAFTING EMAIL",
    "search_gmail": "SEARCHING EMAIL",
    "read_gmail": "READING EMAIL",
    "search_drive": "SEARCHING DRIVE",
    "save_memory": "SAVING TO MEMORY",
    "capture_item": "CAPTURING",
    "update_item": "UPDATING BANK",
    "display_card": "PROJECTING",
    "open_url": "OPENING",
}

# Tools the harness (chat.py) executes itself — they touch the UI, not Google.
UI_TOOLS = {"display_card", "open_url"}


def _parse_iso(dt_str: str):
    """ISO string → (date 'YYYY-MM-DD', time 'HH:MM'|None, is_all_day). Eastern-aware.

    A value with no 'T' (date only, e.g. '2026-07-17') is an all-day event —
    note fromisoformat happily parses that to midnight, so we must check for the
    time component explicitly rather than rely on a parse failure.
    """
    dt_str = (dt_str or "").strip()
    if "T" not in dt_str:
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d")
            return dt.strftime("%Y-%m-%d"), None, True
        except ValueError:
            raise ValueError(f"Cannot parse datetime: {dt_str!r}")
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is not None:
            dt = dt.astimezone(EASTERN)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), False
    except ValueError:
        raise ValueError(f"Cannot parse datetime: {dt_str!r}")


# ── Executors (each returns a confirmation string; never raises) ─────────────────
def _do_create_calendar_event(title, start_datetime, end_datetime="", description="", **_):
    try:
        date_str, time_str, all_day = _parse_iso(start_datetime)
        duration = 60
        if end_datetime and not all_day:
            try:
                s = datetime.fromisoformat(start_datetime)
                e = datetime.fromisoformat(end_datetime)
                mins = int((e - s).total_seconds() // 60)
                if mins > 0:
                    duration = mins
            except (ValueError, TypeError):
                # TypeError guards a mixed aware/naive start/end — just fall back
                # to the 60-min default instead of failing the whole event.
                pass
        ok, info = create_calendar_event(
            title=title, date_str=date_str, time_str=time_str,
            duration_minutes=duration, description=description or "",
        )
        if ok:
            when = date_str if all_day else f"{date_str} {time_str}"
            return f"📅 Added to calendar: {title} — {when}"
        return f"⚠️ Could not create event: {info}"
    except Exception as e:
        logger.error("create_calendar_event: %s", e)
        return f"⚠️ Calendar create failed: {e}"


def _do_delete_calendar_event(event_title, event_date, **_):
    try:
        ok, msg = delete_calendar_event(title=event_title, date_str=event_date)
        return f"🗑️ Removed from calendar: {event_title}" if ok else f"⚠️ {msg}"
    except Exception as e:
        logger.error("delete_calendar_event: %s", e)
        return f"⚠️ Calendar delete failed: {e}"


def _do_add_task(title, list_name=DEFAULT_TASK_LIST, **_):
    try:
        ok, actual_list, dup = add_task(title=title, list_name=list_name or DEFAULT_TASK_LIST)
        if ok and dup:
            return f"✓ Already on {actual_list}: {title}"
        if ok:
            return f"✅ Added to {actual_list}: {title}"
        return f"⚠️ Could not add task: {title}"
    except Exception as e:
        logger.error("add_task: %s", e)
        return f"⚠️ Add task failed: {e}"


def _do_complete_task(task_title, **_):
    try:
        matched = complete_task(task_title)
        return f"✅ Completed: {matched}" if matched else f"⚠️ No task matching '{task_title}'"
    except Exception as e:
        logger.error("complete_task: %s", e)
        return f"⚠️ Complete task failed: {e}"


def _do_send_email(to, subject, body, **_):
    try:
        return f"📤 Email sent to {to}" if send_email(to, subject, body) else f"⚠️ Email to {to} failed"
    except Exception as e:
        logger.error("send_email: %s", e)
        return f"⚠️ Send email failed: {e}"


def _do_draft_email(to, subject, body, **_):
    try:
        return f"📝 Draft saved for {to}" if draft_email(to, subject, body) else f"⚠️ Draft for {to} failed"
    except Exception as e:
        logger.error("draft_email: %s", e)
        return f"⚠️ Draft email failed: {e}"


def _do_search_drive(query, **_):
    try:
        return f"🔍 Drive — '{query}':\n{search_drive(query)}"
    except Exception as e:
        logger.error("search_drive: %s", e)
        return f"⚠️ Drive search failed: {e}"


def _do_save_memory(fact, **_):
    try:
        existing = brain.read_memory()
        merged = brain.merge_memories([fact], existing)
        return f"🧠 Noted: {fact}" if brain.write_memory(merged) else "⚠️ Could not save to memory"
    except Exception as e:
        logger.error("save_memory: %s", e)
        return f"⚠️ Save memory failed: {e}"


def _do_capture_item(kind="note", text="", due=None, **_):
    ok, res = daybank.add_item(kind, text, due=due)
    if ok:
        tail = f" (due {due})" if due else ""
        return f"◆ Captured to your data bank: {res['text']}{tail}"
    return f"⚠️ Could not capture: {res}"


def _do_update_item(id="", status=None, text=None, **_):
    ok, res = daybank.update_item(id, status=status, text=text)
    if ok:
        verb = "Completed" if status == "done" else ("Reopened" if status == "open" else "Updated")
        return f"◆ {verb}: {res}"
    return f"⚠️ Could not update item: {res}"


def _do_search_gmail(query, max_results=8, **_):
    return search_gmail(query, max_results)


def _do_read_gmail(message_id, **_):
    return read_gmail(message_id)


def _do_get_calendar_range(start_offset_days=0, num_days=7, **_):
    return get_calendar_range(start_offset_days, num_days)


_DISPATCH = {
    "create_calendar_event": _do_create_calendar_event,
    "delete_calendar_event": _do_delete_calendar_event,
    "get_calendar_range": _do_get_calendar_range,
    "add_task": _do_add_task,
    "complete_task": _do_complete_task,
    "send_email": _do_send_email,
    "draft_email": _do_draft_email,
    "search_gmail": _do_search_gmail,
    "read_gmail": _do_read_gmail,
    "search_drive": _do_search_drive,
    "save_memory": _do_save_memory,
    "capture_item": _do_capture_item,
    "update_item": _do_update_item,
}


def execute(tool_name: str, tool_input: dict) -> str:
    """Run a tool by name. Blocking — caller wraps in asyncio.to_thread."""
    fn = _DISPATCH.get(tool_name)
    if not fn:
        return f"⚠️ Unknown tool: {tool_name}"
    try:
        return fn(**(tool_input or {}))
    except TypeError as e:
        # Missing/extra args from the model — report, don't crash the turn.
        logger.error("tool %s bad args (%s): %s", tool_name, e, tool_input)
        return f"⚠️ {tool_name}: bad arguments ({e})"
