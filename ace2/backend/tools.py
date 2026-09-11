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
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import brain, daybank, memory_db
from . import ops
from .integrations.calendar_api import (
    create_calendar_event,
    reschedule_calendar_event,
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
    search_personal_gmail,
    read_personal_gmail,
    send_email,
)

logger = logging.getLogger("ace2.tools")

DEFAULT_TASK_LIST = "Admin List - back log"


# ── Schemas (sent to the Anthropic API) ─────────────────────────────────────────
TOOLS = [
    {
        "name": "create_calendar_event",
        "description": (
            "Create a NEW event on Brady's Google Calendar. Use when Brady asks to "
            "schedule, book, add, or block time for something. Execute immediately — "
            "do not ask for confirmation unless the date/time is genuinely ambiguous. "
            "For moving an existing event use reschedule_calendar_event with its exact "
            "calendar and event ids from get_calendar_range. Never delete/create a replacement."
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
        "name": "reschedule_calendar_event",
        "description": (
            "Move ONE existing timed event on the business calendar using exact calendar_id "
            "and event_id from get_calendar_range. Call only when Brady explicitly requests "
            "a move and gives an unambiguous new time. Both times require ISO UTC offsets. "
            "When Brady names a weekday pass expected_weekday verbatim as the full weekday "
            "name; it must match the Eastern start date. A date alone cannot verify his intent. "
            "This patches and reads back the SAME event, never delete/create. Attendee "
            "invitations, shared/personal calendars, all-day events and entire recurring "
            "series are not supported; a single recurring occurrence is supported. "
            "Report moved only when the result is verified completed; otherwise report "
            "the limitation or uncertain result, never silently create a replacement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {key: {"type": "string"} for key in
                           ("calendar_id", "event_id", "start_datetime", "end_datetime", "expected_weekday")},
            "required": ["calendar_id", "event_id", "start_datetime", "end_datetime"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_calendar_event",
        "description": (
            "Delete or cancel an event from Brady's Google Calendar. Use when Brady asks "
            "to cancel, remove, or delete a meeting. Deletes calendar events only. "
            "For rescheduling use reschedule_calendar_event; never delete an event just to move it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_title": {"type": "string", "description": "Title or keyword to match"},
                "event_date": {
                    "type": "string",
                    "description": "Date of the event, ISO YYYY-MM-DD, to narrow the search",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Leave unset the first time. After Brady says yes to the "
                                   "delete on a later turn, re-call with confirmed: true.",
                },
            },
            "required": ["event_title", "event_date"],
        },
    },
    {
        "name": "add_task",
        "description": (
            "LEGACY — add a task to Google Tasks (being retired). Do NOT use for normal "
            "tasks; those go on Brady's task board via capture_item. Only use this if "
            "Brady EXPLICITLY says to put something in Google Tasks."
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
            "LEGACY — complete a task in Google Tasks (being retired). Normal completions "
            "happen on Brady's task board via update_item with the item's id from context. "
            "Only use this if the task genuinely lives in Google and he says so."
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
                "confirmed": {
                    "type": "boolean",
                    "description": "Leave unset the first time. After Brady says yes to the "
                                   "send on a later turn, re-call with confirmed: true.",
                },
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
        "name": "search_personal_gmail",
        "description": (
            "Search Brady's PERSONAL mailbox (br80mcgraw) — his own life, separate from PFI: "
            "recruiters and job leads, personal clients, family, bills and personal accounts. "
            "READ ONLY — you can never send from this address. Use it whenever what he's asking "
            "about is personal, or when search_gmail (PFI) comes up empty. Same Gmail operators. "
            "Pass an id to read_personal_gmail for the full body."
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
        "name": "read_personal_gmail",
        "description": (
            "Read the FULL body of one PERSONAL email by its id (from search_personal_gmail). "
            "The result includes a direct link — pair it with open_url to put the actual email "
            "on Brady's screen when he asks to see it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The email id from search_personal_gmail"},
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
        "name": "recall",
        "description": (
            "TOTAL RECALL — searches by MEANING, not just exact words, across everything "
            "you and Brady have EVER discussed or recorded: all past conversations (this "
            "app, Telegram, the archive), your durable memory facts, and the task board / "
            "data bank. It matches paraphrases, related wording, partial or misspelled "
            "names, and fuzzy descriptions — so 'the guy from the port' can surface a "
            "person Brady never named here, and 'that equipment deal' can surface the "
            "machine by its actual name. Use it whenever Brady references something not "
            "in your live context — 'what did we say about…', 'that thing from last "
            "week/month', a person, client or deal detail you don't see — instead of "
            "guessing or saying you don't remember. If the first phrasing comes back "
            "thin, try a different angle (a role, a place, a topic) rather than giving "
            "up. Returns matched snippets with dates and where each came from — these "
            "are ranked CANDIDATES, so read them and, if none actually answer what "
            "Brady asked, say so plainly instead of forcing the closest one to fit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": (
                    "What to look for, in natural words — a description, paraphrase, "
                    "partial name, role, place or topic. Exact keywords not required.")},
                "max_results": {"type": "integer", "description": "How many matches (1-20, default 8)"},
            },
            "required": ["query"],
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
            "Add a NEW item to Brady's TASK BOARD — Ace's OWN store, THE task & pipeline system "
            "(what Brady sees in his Command panel). This is the ONLY place tasks go. Do this "
            "ON YOUR OWN, without being asked, whenever something worth not forgetting surfaces "
            "in conversation: a commitment he made, a follow-up, a 'don't forget to…', a loose "
            "task, or a note. BUT FIRST scan YOUR TASK BOARD in context: if this task is already "
            "there — even worded differently — do NOT capture a twin; call update_item on the "
            "existing item instead (complete it, or fold the new details into its text/due). "
            "A completion ('I did X', 'X is handled') is NEVER a capture — it's update_item "
            "status='done'. Always pick the best-fitting category. (For a hard-dated "
            "appointment, also create a calendar event.) One item per call.\n"
            "SPAWN FROM A RECORD: when the thing to do comes OUT of a record — the Rebecca "
            "record says her payout cleared and a packet needs mailing — pass that record's id "
            "as parent_id. The action then completes on its own without touching the record, "
            "which is the whole point: closing 'drop off her packet' used to delete Feliz the "
            "client. Never re-capture the record itself as a task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["note", "todo", "commitment", "followup", "approval"],
                    "description": ("What kind of item this is. Use 'approval' ONLY for something "
                                    "YOU have already prepared that needs Brady to say go before "
                                    "it executes (a drafted email, a proposed reschedule, a "
                                    "payment to send) -- it surfaces in his TODAY card under "
                                    "'waiting on your OK'. Never use it for ordinary to-dos."),
                },
                "text": {"type": "string", "description": "The item, phrased tightly (~one line)"},
                "due": {"type": "string", "description": "Optional due date/time in plain words (e.g. 'today 5pm', 'Fri')"},
                "category": {
                    "type": "string",
                    "enum": ["Money", "Bills", "Opportunities", "Goals", "Personal", "Deals", "Agents", "Admin", "Networking", "Business", "Tech"],
                    "description": "Which board column this belongs on (pick the best fit)",
                },
                "parent_id": {
                    "type": "string",
                    "description": "Optional: the RECORD id this action came out of, so completing the action leaves the record standing",
                },
            },
            "required": ["kind", "text"],
        },
    },
    {
        "name": "read_own_code",
        "description": (
            "READ YOUR OWN SOURCE. You are a running program and this is the code that runs "
            "you. Use it when Brady says something you produced was wrong — a brief that was "
            "off, a board item that behaved oddly, a number you got from somewhere — and you "
            "cannot explain WHY from what you can see. Search first, then read the range "
            "around the hit.\n"
            "ALWAYS CITE file:line when you explain your own behavior, and say plainly when "
            "the code does not show what you expected — a confident wrong reading of your own "
            "source is worse than 'I looked and I can't tell'. This is READ ONLY: you cannot "
            "change or deploy yourself. If a fix is needed, describe it and capture_item it so "
            "a person applies it.\n"
            "Real example: 'the brief was off' three times in one week; the cause was that the "
            "brief received ITEMS for Money and Bills and only a COUNT for every other "
            "category, so a session due that day was invisible to it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Regex/text to find across the app's source. Start here — returns path:line hits.",
                },
                "path": {
                    "type": "string",
                    "description": "File to read, relative to the app root (e.g. 'backend/chat.py', 'app.js')",
                },
                "start": {"type": "integer", "description": "First line to read (default 1)"},
                "lines": {"type": "integer", "description": "How many lines (default/max 200)"},
            },
        },
    },
    {
        "name": "update_item",
        "description": (
            "Update a TASK BOARD item: mark it done when Brady says he finished it (however he "
            "words it), reopen it, rewrite its text when he gives NEW INFO about it, move its "
            "category, change its due, or status='dropped' to archive a mistake/dead item "
            "(archived, never deleted). Use the item's id from YOUR TASK BOARD in context; if "
            "you're not sure which id, pass match='a few words of the task' instead and the "
            "board finds it — if it's ambiguous you'll get candidates back to choose from or "
            "ask Brady. When Brady says something is finished, ALWAYS complete the existing "
            "item — never capture_item a completion as a new task.\n"
            "CLASSIFY IT when Brady's words say what KIND of thing it is. An ACTION has a "
            "natural end and leaves when done ('mail Rebecca's packet'). A RECORD has a STATE "
            "and is updated forever — a deal, a client, a bill, a goal, a person. Set "
            "state='waiting' the moment something is parked on someone ELSE ('submitted, "
            "waiting on approval', 'just waiting, no push') and put WHO in waiting_on: a "
            "waiting record is protected from auto-closing, because nothing on Brady's side "
            "finishes it. Set state='settled' when a record is finished but must STAY — "
            "Feliz's annuity is closed and paid, that is settled, not deleted. Never close a "
            "record to mean 'settled'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The board item id (preferred when known)"},
                "match": {"type": "string", "description": "No id? A few words of the item's text — fuzzy-resolved server-side"},
                "status": {"type": "string", "enum": ["open", "done", "dropped"], "description": "New status ('dropped' archives it)"},
                "text": {"type": "string", "description": "Optional new text (fold new details into the SAME item)"},
                "category": {
                    "type": "string",
                    "enum": ["Money", "Bills", "Opportunities", "Goals", "Personal", "Deals", "Agents", "Admin", "Networking", "Business", "Tech"],
                    "description": "Optional: move the item to this board column",
                },
                "due": {"type": "string", "description": "Optional new due in plain words ('' clears it)"},
                "entry": {
                    "type": "string", "enum": ["action", "record"],
                    "description": "What KIND of row: 'action' ends and leaves; 'record' has a state and persists",
                },
                "state": {
                    "type": "string", "enum": ["active", "waiting", "settled"],
                    "description": "Records only. 'waiting' = parked on someone else (protected from auto-close); 'settled' = finished but kept",
                },
                "waiting_on": {
                    "type": "string",
                    "description": "With state='waiting': WHO or WHAT it is parked on ('Tony', 'approval')",
                },
                "next_step": {"type": "string", "description":
                    "The ONE move that advances this, in Brady's words. Set it only when he "
                    "says what the next move is — never infer it from the task text. Empty "
                    "string clears it. An action with no next_step and no date is what shows "
                    "up under Needs a decision, which is the signal he asked for."},
                "followup": {"type": "string", "description":
                    "YYYY-MM-DD when BRADY should chase this. Different from `due`, which is "
                    "the obligation's own deadline, and different from whatever the other "
                    "party is waiting on. Empty string clears it."},
            },
            "required": [],
        },
    },
    {
        "name": "set_privacy",
        "description": (
            "Turn DISCREET / PRIVATE MODE on or off. Call with on=true when Brady says something "
            "like 'private mode', 'go private', 'don't say my numbers', 'I'm around people' — while "
            "PRIVATE, you never say dollar amounts, balances, or financial specifics OUT LOUD; you "
            "offer to show them on screen or read them only if he confirms he's alone. Call with "
            "on=false when he says 'normal mode', 'off', 'I'm alone now', 'you can say numbers'. "
            "Confirm in one short line. (Typed replies are always fine to show numbers.)"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "on": {"type": "boolean", "description": "true = go private/discreet; false = normal"},
            },
            "required": ["on"],
        },
    },
    {
        "name": "update_profile",
        "description": (
            "Rewrite the ★ YOUR PROFILE block — the editable, authoritative statement of who "
            "Brady is, what's live in his world, and what your mission is. Call this when he "
            "REDEFINES things: a venture starts or ends, priorities shift, a relationship "
            "changes, or he tells you who you are to him ('you're my second brain full stop'). "
            "Pass the COMPLETE new profile text: take the current profile from your context, "
            "apply his change, keep everything still true, drop what's obsolete. 150-300 words, "
            "same section shape (WHO BRADY IS / MONEY CONTEXT / HIS PEOPLE / WHO YOU ARE TO "
            "HIM). This is a standing change — it steers every future turn, so get it right."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The complete new profile text (not a diff)"},
                "confirmed": {
                    "type": "boolean",
                    "description": "Leave unset the first time. After Brady approves the rewrite "
                                   "on a later turn, re-call with confirmed: true.",
                },
            },
            "required": ["text"],
        },
    },
]

# Voice-only handoff tool. NOT part of TOOLS (so the typed path, which already has the full
# MCP surface, never sees it) — chat.py appends it to VOICE_TOOLS only. It lets a live voice
# turn hand a Google Workspace AUTHORING task (create/edit a Doc, build/update a Sheet or
# Slides deck, mint a shareable link — the mcp_-only surface voice doesn't carry) to the
# on-screen assistant, which runs it as a normal typed turn with the full MCP toolset and the
# usual confirm flow. Keeps voice fast (native-only) without ever losing heavy action.
# SUPERSEDED 2026-09-09 by START_TASK. Kept only so an in-flight cached voice schema that
# still names it resolves to something honest instead of an unknown tool. Do not re-add it to
# VOICE_TOOLS: it required a connected screen, ran the work inside a chat turn, and reported
# whatever the model said rather than what happened.
BUILD_ON_SCREEN = {
    "name": "build_on_screen",
    "description": (
        "Hand a Google Workspace AUTHORING task to the on-screen assistant, which has the full "
        "Docs / Sheets / Slides / Drive-sharing toolset. Use this on a VOICE call when Brady asks "
        "you to CREATE or EDIT a Google Doc, build or update a Sheet or Slides deck, or make a "
        "shareable link — the things your spoken tools don't cover. Put the COMPLETE instruction "
        "in `request` (everything the screen needs: title, the full contents, which sheet/range, "
        "etc.). After calling it, tell Brady in one short sentence to watch his screen. Do NOT use "
        "this for calendar, tasks, email, Drive search, memory, recall, or the data bank — you do "
        "those yourself by voice."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": (
                    "The full authoring instruction to execute on screen, phrased as a complete "
                    "request, e.g. 'Create a Google Doc titled \"Q3 Refi Pipeline\" with a section "
                    "per active deal listing borrower, rate, and status.'"
                ),
            },
        },
        "required": ["request"],
    },
}

# VOICE-TO-ACTION (2026-09-09). Replaces build_on_screen. The work no longer needs a screen,
# does not run inside a chat turn, and cannot be reported as done from prose: this tool hands
# the request to shared background execution and comes back with a task id and a state. Voice
# and typing call the same handler with the same approval rules, so "create a spreadsheet"
# means one thing however Brady asks for it.
START_TASK = {
    "name": "start_task",
    "description": (
        "Start a longer task that runs in the BACKGROUND while you keep talking — building a "
        "Google Sheet, or researching something on the live internet. He does not need "
        "a screen open and you must not tell him to watch one — a small progress card appears "
        "wherever he is. You get back a task id and a state, which will be 'queued'. "
        "ACKNOWLEDGE IT IN ONE SHORT SENTENCE AND SAY NOTHING ABOUT THE RESULT: it has not run "
        "yet. Never say it is built, populated, ready or open, and never invent or read out a "
        "link — the card carries the real link once the file has been read back and verified, "
        "and you will be told the outcome separately. Do not use this for calendar, tasks, "
        "email, Drive search, memory, recall or the data bank; you do those yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "capability": {
                "type": "string",
                "enum": ["create_spreadsheet", "create_doc", "create_folder", "research"],
                "description": (
                    "create_spreadsheet — build a Google Sheet. "
                    "create_doc — write a Google Doc (put the text in `blocks`, one entry "
                    "per paragraph, in order). "
                    "create_folder — make a Drive folder (`name`). "
                    "research — look something up on the live internet and come back with "
                    "sources and the date checked. Use research whenever Brady asks what "
                    "something costs, what is current, or what someone is doing now; do NOT "
                    "answer those from memory."
                ),
            },
            "question": {
                "type": "string",
                "description": ("For research: the question, in full. Include what he "
                                "actually wants to know, not a keyword."),
            },
            "blocks": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("For create_doc: the document text, one paragraph per entry, "
                                "in the order they should appear. Real content only — if he "
                                "has not said what goes in it, ask before calling this."),
            },
            "name": {
                "type": "string",
                "description": "For create_folder: the folder name.",
            },
            "folder": {
                "type": "string",
                "description": ("Optional, for create_spreadsheet and create_doc: the name of "
                                "an EXISTING Drive folder to put it in. Leave it out and the "
                                "file lands in the root of My Drive. Only pass this if Brady "
                                "said where it should go — do not choose a folder for him."),
            },
            "title": {
                "type": "string",
                "description": "The document title exactly as Brady asked for it.",
            },
            "rows": {
                "type": "array",
                "description": (
                    "The spreadsheet contents as rows of cells, header row FIRST. Include real "
                    "content — never placeholder text. If Brady has not said what goes in it, "
                    "ask him before calling this rather than inventing data."
                ),
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "bold_header": {
                "type": "boolean",
                "description": (
                    "True if he asked for header formatting. It is reported as unsupported "
                    "rather than attempted — say so only if the card says so."
                ),
            },
        },
        # Only the capability is universally required; the handler refuses a task whose
        # own fields are missing rather than inventing them.
        "required": ["capability"],
    },
}

# WHAT EACH NATIVE TOOL DOES, DECLARED (2026-09-10, Codex). An interrupted turn has to say
# which operations actually changed something, and inferring that from a tool's ANSWER is how
# a calendar read ended up listed as a completed mutation. These are stated, not guessed.
#
# THE PARTITION (pinned by tests/test_conversation_context.py, 2026-09-10). Every name in
# TOOLS belongs to EXACTLY ONE of four disjoint sets, and each set means something different
# about what an interrupted turn may say:
#
#   UI_TOOLS          paints a screen; mutates nothing, so it is never in the receipts
#   NATIVE_READS      answers a question; not a mutation, so it is never in the receipts
#   ops.JOURNALLED    a write with a durable verdict — the only route to "this DID go through"
#   NATIVE_MUTATIONS  a write with NO journal verdict available here, so it is UNKNOWN
#
# NATIVE_MUTATIONS is therefore NOT "all the writes" — it is the writes whose outcome this
# process cannot evidence, and that is exactly why it is listed separately from JOURNALLED.
# send_email and delete_calendar_event execute from the Review tray (durable and single-use
# by construction, but settled outside this turn); set_privacy flips a local flag. All three
# are reported as outcome-not-confirmed rather than as done, and chat._dispatch_write reads
# this set to decide that. A native tool in NEITHER set is treated as a possible mutation
# with an unknown outcome — the safe default — and logged as an undeclared gap.
TOOLS.append({
    "name": "read_attachment",
    "description": "Read a previously uploaded photo, PDF, meeting note, or audio transcript. Use capture_id from the conversation; omit only for the latest upload. Supply question to inspect original image/PDF details (uses a model call); omit for stored text. Do not guess unseen attachment content.",
    "input_schema": {"type":"object", "properties": {
        "capture_id":{"type":"string"}, "question":{"type":"string"}}, "additionalProperties":False}
})

NATIVE_READS = frozenset({
    "get_calendar_range", "read_gmail", "read_personal_gmail", "search_gmail",
    "search_personal_gmail", "search_drive", "recall", "read_own_code", "read_attachment",
})
NATIVE_MUTATIONS = frozenset({
    "delete_calendar_event", "send_email", "set_privacy",
})

# Short present-tense labels for the WS `tool` event (the orb shows "◈ CREATING EVENT…")
# Anthropic SERVER-side web search (2026-07-31): executed by the API itself — no executor
# here, no scraper, no extra vendor. Appended to the TYPED loop only (chat.py); voice stays
# native-fast and hands internet questions to the screen. max_uses caps cost per turn
# (searches bill ~$10/1k + tokens — 5/turn keeps a runaway loop impossible).
WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

TOOL_LABELS = {
    "read_attachment": "READING ATTACHMENT",
    "web_search": "SEARCHING THE WEB",
    "create_calendar_event": "CREATING EVENT",
    "reschedule_calendar_event": "MOVING EVENT",
    "delete_calendar_event": "REMOVING EVENT",
    "get_calendar_range": "READING CALENDAR",
    "add_task": "ADDING TASK",
    "complete_task": "COMPLETING TASK",
    "send_email": "SENDING EMAIL",
    "draft_email": "DRAFTING EMAIL",
    "search_gmail": "SEARCHING EMAIL",
    "search_personal_gmail": "SEARCHING PERSONAL EMAIL",
    "read_personal_gmail": "READING PERSONAL EMAIL",
    "read_gmail": "READING EMAIL",
    "recall": "SEARCHING MEMORY",
    "search_drive": "SEARCHING DRIVE",
    "save_memory": "SAVING TO MEMORY",
    "set_privacy": "PRIVACY",
    "update_profile": "UPDATING PROFILE",
    "capture_item": "CAPTURING",
    "update_item": "UPDATING BANK",
    "display_card": "PROJECTING",
    "open_url": "OPENING",
    "build_on_screen": "SENDING TO SCREEN",
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
        ok, info, state = create_calendar_event(
            title=title, date_str=date_str, time_str=time_str,
            duration_minutes=duration, description=description or "",
        )
        if ok:
            when = date_str if all_day else f"{date_str} {time_str}"
            return ops.Outcome(ops.COMPLETED, f"📅 Added to calendar: {title} — {when}",
                               record_id=str(info))
        # A warning sentence proves nothing about WHEN it failed. The adapter says whether
        # the request ever left the process; an outcome lost after dispatch is UNKNOWN and
        # must be reconciled, never quietly retried into a second event.
        state = {
            "needs_review": ops.NEEDS_REVIEW,
            "unknown": ops.UNKNOWN,
            "failed_before_dispatch": ops.FAILED_BEFORE_DISPATCH,
        }.get(state, ops.UNKNOWN)
        prefix = "◆" if state == ops.NEEDS_REVIEW else "⚠️"
        return ops.Outcome(state, f"{prefix} {info}")
    except Exception as e:
        logger.error("create_calendar_event: %s", e)
        # This path is reached only before the adapter returned, i.e. argument parsing.
        return ops.Outcome(ops.FAILED_BEFORE_DISPATCH, f"⚠️ Calendar create failed: {e}")


def _do_reschedule_calendar_event(calendar_id, event_id, start_datetime, end_datetime, expected_weekday="", **_):
    ok, info, state = reschedule_calendar_event(calendar_id, event_id, start_datetime, end_datetime, expected_weekday)
    if ok:
        return ops.Outcome(ops.COMPLETED,
                           f"Calendar move verified: {start_datetime} to {end_datetime}.", record_id=event_id,
                           detail={"calendar_id": calendar_id, "event_id": event_id,
                                   "start": start_datetime, "end": end_datetime,
                                   "verified": True})
    return ops.Outcome(state, str(info))


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
        r = complete_task(task_title)
        status = r.get("status") if isinstance(r, dict) else None
        if status == "done":
            return f"✅ Completed: {r['title']}"
        if status == "ambiguous":
            opts = "; ".join(r.get("candidates") or [])
            return (f"⚠️ NOT DONE — more than one open task could be '{task_title}'. Ask Brady "
                    f"which one, by name: {opts}")
        if status == "none":
            openlist = r.get("open") or []
            if not openlist:
                return "⚠️ NOT DONE — there are no open tasks to complete right now."
            return (f"⚠️ NOT DONE — nothing open matches '{task_title}'. Tell Brady what IS open "
                    f"and ask which he means: {'; '.join(openlist)}")
        return f"⚠️ Couldn't reach Google Tasks to complete '{task_title}'."
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


def _do_set_privacy(on=True, **_):
    # Lazy import avoids a chat<->tools circular import at module load.
    from . import chat
    state = chat.set_discreet(bool(on))
    return ("🔒 Private mode ON — I'll keep your money off the speaker; say 'normal mode' when you're clear."
            if state else "🔓 Private mode OFF — I can say your numbers out loud again.")


def _do_update_profile(text="", **_):
    from . import chat
    if chat.set_profile(text):
        return "◆ Profile updated — this is who we are now; it steers every turn from here."
    return ("⚠️ Profile NOT saved — the text was too short (a real profile is 150+ words) or "
            "the store is unavailable. Rewrite the FULL profile and try again.")


def _do_save_memory(fact, **_):
    try:
        # add_memory routes to the Postgres brain (uncapped append, dedup) when live, else the
        # old Drive merge. No more Haiku merge silently dropping facts at the 60-cap.
        return f"🧠 Noted: {fact}" if brain.add_memory([fact]) else "⚠️ Could not save to memory"
    except Exception as e:
        logger.error("save_memory: %s", e)
        return f"⚠️ Save memory failed: {e}"


# ── ACE READS HIS OWN SOURCE (2026-09-06) ──────────────────────────────────────────
# WHY. Brady told Ace the brief was "off" three times in one week and neither of them could
# say why, because Ace cannot see the code that builds his brief. The actual cause was that
# _board_stats handed him ITEMS for Money and Bills only and a bare COUNT for every other
# category — a fact sitting in a file he had no access to. He experienced the symptom with no
# access to the cause.
#
# READ ONLY, AND THAT IS THE LINE. Ace is the thing Brady depends on; a self-applied deploy
# could take out his own recovery path at 9am with nobody watching. This matches his existing
# tiered-autonomy doctrine — auto = read/draft/capture, queue-for-approval = writes. Proposing
# a change is a board item a human applies, never a commit.
_SRC_ROOT = Path(__file__).resolve().parent.parent          # the ace2/ directory
_SRC_OK_EXT = {".py", ".js", ".css", ".html", ".md", ".toml", ".json", ".txt"}
_SRC_SKIP = {"node_modules", ".git", "__pycache__", ".venv", "snapshots"}
_SRC_MAX_LINES = 200
_SRC_MAX_HITS = 40
# Nothing secret should live in the repo (credentials are Railway env vars), but a source
# reader must not become the one place that leaks one if that ever stops being true.
_SECRETish = re.compile(r"(?i)(api[_-]?key|secret|password|token|bearer)\s*[:=]\s*['\"][^'\"]{8,}")


def _src_files():
    for p in _SRC_ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _SRC_OK_EXT:
            continue
        if any(part in _SRC_SKIP for part in p.parts):
            continue
        yield p


def _src_redact(line: str) -> str:
    return _SECRETish.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + "= «redacted»", line)


def _do_read_own_code(search=None, path=None, start=None, lines=None, **_):
    try:
        if search:
            rx = re.compile(search, re.I)
            hits, scanned = [], 0
            for f in _src_files():
                scanned += 1
                try:
                    for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                        if rx.search(line):
                            rel = f.relative_to(_SRC_ROOT)
                            hits.append(f"{rel}:{n}: {_src_redact(line.strip())[:190]}")
                            if len(hits) >= _SRC_MAX_HITS:
                                break
                except Exception:
                    continue
                if len(hits) >= _SRC_MAX_HITS:
                    break
            if not hits:
                return f"No match for {search!r} across {scanned} source files."
            more = " (stopped at the cap — narrow the search)" if len(hits) >= _SRC_MAX_HITS else ""
            return f"{len(hits)} match(es){more}:\n" + "\n".join(hits)

        if not path:
            return "⚠️ Pass search='pattern' to find code, or path='backend/chat.py' to read it."
        target = (_SRC_ROOT / path).resolve()
        if not str(target).startswith(str(_SRC_ROOT)) or not target.is_file():
            return f"⚠️ No such file inside the app: {path}"
        if target.suffix.lower() not in _SRC_OK_EXT:
            return f"⚠️ Not a readable source file: {path}"
        all_lines = target.read_text(errors="replace").splitlines()
        s0 = max(1, int(start or 1))
        n = min(int(lines or _SRC_MAX_LINES), _SRC_MAX_LINES)
        chunk = all_lines[s0 - 1:s0 - 1 + n]
        if not chunk:
            return f"{path} has {len(all_lines)} lines; {s0} is past the end."
        body = "\n".join(f"{s0 + i}: {_src_redact(l)}" for i, l in enumerate(chunk))
        tail = f"\n… {len(all_lines) - (s0 - 1 + len(chunk))} more lines" if s0 - 1 + len(chunk) < len(all_lines) else ""
        return f"{path} (lines {s0}-{s0 + len(chunk) - 1} of {len(all_lines)}):\n{body}{tail}"
    except Exception as e:
        return f"⚠️ Could not read source: {e}"


def _do_capture_item(kind="note", text="", due=None, category=None, parent_id=None, **_):
    tags = [category] if category else None
    ok, res = daybank.add_item(kind, text, due=due, tags=tags, parent_id=parent_id)
    if ok:
        if isinstance(res, dict) and res.get("dup"):
            # Actionable receipt (2026-07-31): id + status so the model can pivot to
            # update_item instead of dead-ending (the old receipt had neither).
            st = res.get("status", "open")
            # done_ts is UTC; a raw [:10] compares a UTC date to an Eastern one, so anything
            # closed 8pm-midnight ET reads one day late. Same bug db.py:524 documents.
            when = db._done_et(res.get("done_ts")) or ""
            state = "open" if st == "open" else f"already {st}{f' {when}' if when else ''}"
            return ops.Outcome(ops.COMPLETED, (
                    f"◆ Already on your board as [{res.get('id')}] ({state}): "
                    f"{res.get('text', text)} — to change or complete it, use update_item "
                    f"with that id."), record_id=res.get("id", ""))
        tail = f" (due {due})" if due else ""
        cat = f" [{category}]" if category else ""
        out = f"◆ Added to your board{cat}: {res['text']}{tail}"
        row_id = res.get("id", "") if isinstance(res, dict) else ""
        sim = res.get("similar") if isinstance(res, dict) else None
        if sim:
            out += (f"\n⚠ Similar existing item [{sim['id']}] ({sim['status']}): {sim['text']} — "
                    f"if that's the same task, keep ONE: update_item the existing one and drop "
                    f"this new one (status='dropped').")
        return ops.Outcome(ops.COMPLETED, out, record_id=row_id)
    if isinstance(res, dict) and res.get("needs_review"):
        # NOT an error: the board already holds a row that looks like this one but the
        # details differ, so saving either silently would lose information. Say precisely
        # what is on the board, what was asked for, and the two moves that resolve it —
        # both keyed to the real item id so the next call can actually succeed.
        ex_due = f" (due {res['existing_due']})" if res.get("existing_due") else ""
        want_due = f" (due {res['requested_due']})" if res.get("requested_due") else ""
        return ops.Outcome(ops.NEEDS_REVIEW, (
                f"◆ NOT SAVED — needs your call. The board already has "
                f"[{res['existing_id']}] ({res.get('existing_status', 'open')}): "
                f"{res['existing_text']}{ex_due}\n"
                f"You asked to add: {res['requested_text']}{want_due}\n"
                f"If it is the SAME obligation with new details, call update_item with "
                f"id='{res['existing_id']}' and the new text/due — that amends the existing "
                f"row and I will confirm the saved result. If it is genuinely DIFFERENT, "
                f"call capture_item again with wording that names what makes it different "
                f"(the person, the purpose, or which occurrence). Tell Brady which one you "
                f"are doing; do not report this as added."),
            detail={"existing_id": res["existing_id"]})
    return ops.Outcome(ops.FAILED_BEFORE_DISPATCH, f"⚠️ Could not capture: {res}")


def _do_update_item(id="", match=None, status=None, text=None, category=None, due=None,
                    entry=None, state=None, waiting_on=None, next_step=None, followup=None,
                    **_):
    tags = None
    if category:
        _CATS = {"Money", "Bills", "Opportunities", "Goals", "Personal", "Deals", "Agents", "Admin", "Networking", "Business", "Tech"}
        it = None
        if id:
            it = next((x for x in daybank.read_items(False) if x.get("id") == id), None)
        elif match:   # resolve by match too, else re-categorizing wipes non-category tags (audit #7)
            from . import db
            cands = db.find_items(match, status="open")
            if len(cands) == 1:
                it = cands[0]
        keep = [t for t in ((it.get("tags") if it else None) or []) if t not in _CATS]
        tags = [category] + keep
    # Ace's own hand. The Command panel stamps 'brady'; the split is what makes "who closed
    # this?" answerable at all (2026-09-05).
    ok, res = daybank.update_item(id, status=status, text=text, tags=tags, due=due, match=match,
                                  next_step=next_step, followup=followup,
                                  closed_by="ace", entry=entry, state=state,
                                  waiting_on=waiting_on)
    if ok:
        verb = {"done": "Completed", "open": "Reopened", "dropped": "Archived"}.get(status, "Updated")
        moved = f" → [{category}]" if category else ""
        return f"◆ {verb}{moved}: {res}"
    # AMBIGUOUS / not-found comes back as guidance, not a dead end — the model can ask Brady
    # or pick from the candidates and call again with the id.
    return f"⚠️ Could not update item: {res}"


def _do_recall(query, max_results=8, **_):
    return memory_db.recall(query, max_results)


def _do_search_gmail(query, max_results=8, **_):
    return search_gmail(query, max_results)


def _do_search_personal_gmail(query, max_results=8, **_):
    return search_personal_gmail(query, max_results)


def _do_read_personal_gmail(message_id, **_):
    return read_personal_gmail(message_id)


def _do_read_gmail(message_id, **_):
    return read_gmail(message_id)


def _do_get_calendar_range(start_offset_days=0, num_days=7, **_):
    return get_calendar_range(start_offset_days, num_days)


from .capture_store import read_attachment

_DISPATCH = {
    "read_attachment": read_attachment,
    "create_calendar_event": _do_create_calendar_event,
    "reschedule_calendar_event": _do_reschedule_calendar_event,
    "delete_calendar_event": _do_delete_calendar_event,
    "get_calendar_range": _do_get_calendar_range,
    "add_task": _do_add_task,
    "complete_task": _do_complete_task,
    "send_email": _do_send_email,
    "draft_email": _do_draft_email,
    "search_gmail": _do_search_gmail,
    "search_personal_gmail": _do_search_personal_gmail,
    "read_personal_gmail": _do_read_personal_gmail,
    "read_gmail": _do_read_gmail,
    "recall": _do_recall,
    "search_drive": _do_search_drive,
    "save_memory": _do_save_memory,
    "capture_item": _do_capture_item,
    "update_item": _do_update_item,
    "set_privacy": _do_set_privacy,
    "update_profile": _do_update_profile,
    "read_own_code": _do_read_own_code,
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
