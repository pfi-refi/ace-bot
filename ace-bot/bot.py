"""
Ace — Brady McGraw's Telegram business partner bot.

v15: 30-day calendar window, Telegram 4096-char message splitting, voice (Whisper STT +
     OpenAI TTS onyx voice), model claude-opus-4-8, memory cross-reference, pattern learning.
    - get_week_calendar() now pulls 30 days forward (was current week to Sunday)
    - _send_split() helper splits any message > 4096 chars at natural break points
    - All main AI response sends now route through _send_split()
    - Voice: Whisper STT transcription + gpt-4o-mini-tts onyx voice output
    - EMD data lives in Ace's memory file (ace_memory.json on Drive) — not hardcoded here

v13: Memory cross-reference, pattern learning, updated task lists, all-list morning scan.
    - Morning brief cross-references memory with open tasks to surface carry-forward items
    - EOD captures carry-forward items and stores them in memory automatically
    - System prompt updated to Brady's actual current task lists (Today list removed)
    - Pattern learning: Ace logs behavioral patterns to memory over time
    - Reference lists + Personal/Goals excluded from morning brief scan
    - add_task() defaults to 'Admin List - back log' (Today list no longer exists)
    - OpenAI API key now in Railway env (OPENAI_API_KEY) for future voice support
"""

import base64
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
EASTERN = pytz.timezone("America/New_York")

def get_system_prompt() -> str:
    """Build the system prompt with live date/time injected on every message."""
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d, %Y")
    time_str = now_et.strftime("%-I:%M %p ET")
    date_header = (
        f"TODAY IS {day_str} | CURRENT TIME: {time_str}\n"
        "This date and time are injected fresh on EVERY message. "
        "Trust this absolutely. NEVER second-guess the date or say you are unsure what day it is. "
        "NEVER tell Brady to get some rest or wind down unless the time above shows 7 PM or later.\n\n"
    )
    return date_header + SYSTEM_PROMPT
# ============================================================
# ACE SELF-AWARENESS SYSTEM — v17
# ============================================================
ACE_VERSION = "18.13"
ACE_LAST_UPDATED = "2026-07-15"

CAPABILITIES = {
    "calendar": {
        "read": "Read all Google Calendar events up to 30 days ahead; re-fetch mid-session via [READ_CALENDAR:today] or [READ_CALENDAR:week]",
        "write": "Create and delete events — writes only to pfi@platinumfortuneimpact.com",
        "commands": ["/calendar"]
    },
    "gmail": {
        "read": "Read unread and recent emails from the last 48 hours",
        "modify": "Can label and modify emails",
        "note": "Authenticated under pfi@platinumfortuneimpact.com"
    },
    "tasks": {
        "read": "Read Google Tasks lists and items on demand via [LIST_TASKS:all] or [LIST_TASKS:listname] — bot fetches fresh and feeds data back to Ace",
        "write": "Create and complete tasks via [ADD_TASK:] and [COMPLETE_TASK:] tags"
    },
    "voice": {
        "tts": "Convert text to speech using OpenAI TTS (voice: onyx — deep, authoritative male)",
        "stt": "Transcribe voice messages using OpenAI Whisper"
    },
    "intelligence": {
        "model": "Claude Sonnet 4-5 for all reasoning and responses (Haiku for memory utility)",
        "briefs": "Manual /brief and /eod commands — auto-briefs PAUSED as of July 2026",
        "memory": "Persistent memory via [MEMORY:] tags, read/write at EOD",
        "history": "80-exchange conversation history on Google Drive — NEVER cleared by code"
    },
    "scope": {
        "purpose": "Brady McGraw's complete life operating system — business (PFI) AND personal (health, finance, goals, relationships)",
        "identity": "J.A.R.V.I.S. to Brady's Tony Stark — executes first, never hesitates"
    },
    "action_tags": {
        "calendar": "tool: create_calendar_event, delete_calendar_event (v18 function calling) | [READ_CALENDAR:today/week] to re-fetch mid-session",
        "tasks": "tool: add_task, complete_task (v18 function calling) | [LIST_TASKS:all/listname] to read live task data",
        "email": "tool: send_email (v18 function calling) | [DRAFT_EMAIL:] tag for drafts",
        "drive": "tool: search_drive (v18 function calling)",
        "memory": "[MEMORY:] tag"
    }
}

CHANGELOG = [
    {
        "date": "2026-07-15",
        "version": "18.13",
        "changes": [
            "DATETIME FIX (root cause): 'Current date and time' in the tool-use path was derived from update.message.date — Telegram's message-receipt timestamp, which goes stale when a polling backlog drains after a Railway redeploy. Now derived from the live server clock (datetime.now(EASTERN)), same as get_system_prompt(). Message send-time kept as separate context line, with defensive UTC localization if the datetime ever arrives naive.",
            "/session FIXED: SESSION_MODE was only consumed in _process_text (legacy path), but handle_message routes to _process_with_tools — brain dumps were never processed. SESSION_MODE check added to _process_with_tools.",
            "Tag-leak fix: fetch tags ([LIST_TASKS:]/[READ_CALENDAR:]/[READ_EMAIL:]) re-emitted in a second-pass response are now stripped from the visible reply in _process_text.",
            "Infinite-loop guard: _process_with_tools agentic loop capped at 8 iterations — prevents API burn + silence if Claude keeps re-emitting fetch tags.",
            "Legacy action tags now handled in the tool-use path: [ADD_TASK:], [COMPLETE_TASK:], [CREATE_EVENT:], [DELETE_EVENT:], [SEND_EMAIL:], [DRAFT_EMAIL:], [SEARCH_DRIVE:] emitted by Claude (from old history patterns) are executed and stripped instead of leaking raw to Brady.",
            "Memory hardening: _merge_memories wrapped in try/except with a safe local merge fallback (append + exact dedup, 60-item cap) so a Haiku API failure can no longer crash /remember, EOD, or message handling.",
            "Stale-date prevention: memory merge prompt now includes today's date and instructs converting relative dates ('tomorrow', 'next week') to absolute dates before storing.",
            "Empty-response fallback: _process_with_tools now sends a notice instead of silence when Claude returns no text and no tool ran.",
            "/status no longer advertises the paused 9:30 AM / 9:00 PM auto-brief schedule.",
            "TOOL_USE_SYSTEM_PROMPT version string now interpolates ACE_VERSION instead of hardcoded 'v18.10'; SYSTEM_PROMPT history note corrected 40 → 80 exchanges.",
        ]
    },
    {
        "date": "2026-07-07",
        "version": "18.10",
        "changes": [
            "/reset_history command added — clears ace_conversation.json on Drive (empty JSON), memory files untouched",
            "CommandHandler registered for reset_history; added to /start and /help command lists",
        ]
    },
    {
        "date": "2026-07-07",
        "version": "18.9",
        "changes": [
            "HISTORY OVERRIDE added to SYSTEM_PROMPT, TOOL_USE_SYSTEM_PROMPT, and EXECUTION MANDATE — explicit statement that [LIST_TASKS:] and [READ_CALENDAR:] ARE operational, overrides any baked-in history refusals",
            "Root cause fix: 80-exchange persistent history contained past exchanges where Ace said it could not read tasks/calendar — override statement prevents those from suppressing tag output",
            "TOOL_USE_SYSTEM_PROMPT version string updated from hardcoded v18.7 to v18.9",
            "All three prompt surfaces (SYSTEM_PROMPT, TOOL_USE_SYSTEM_PROMPT, EXECUTION MANDATE) now contain affirmative capability statement for both read tags",
        ]
    },
    {
        "date": "2026-07-07",
        "version": "18.8",
        "changes": [
            "Fixed silent second-response bug: [LIST_TASKS:] and [READ_CALENDAR:] tags now deliver data in one message — no more 'let me grab that' + silence pattern",
            "Root fix: replaced response.content SDK objects with plain string for assistant turn — eliminates Pydantic serialization failure on second API call",
            "data_msg rewritten with explicit 'DATA FETCH COMPLETE — do NOT re-emit tags' instruction, preventing infinite tag loop",
            "TOOL_USE_SYSTEM_PROMPT updated: fetch tags must be output alone with no preamble text to enable clean silent one-shot fetch-and-respond",
        ]
    },
    {
        "date": "2026-07-07",
        "version": "18.7",
        "changes": [
            "[LIST_TASKS: all] and [LIST_TASKS: list name] tags added — Ace outputs tag, bot fetches live Google Tasks data and feeds it back so Ace can respond with real task contents",
            "[READ_CALENDAR: today] and [READ_CALENDAR: week] tags added — re-fetches calendar mid-conversation fresh from Google Calendar and feeds data back for grounded responses",
            "Both tags wired into _process_with_tools loop (continue on detection) and _process_text legacy path (second Claude call with data injected)",
            "Brevity rule updated: /brief and /eod removed from long-format exceptions — long responses now gated only on task list pulls, email summaries, and explicit user requests",
        ]
    },
    {
        "date": "2026-07-06",
        "version": "18.6",
        "changes": [
            "Hard brevity constraint added to SYSTEM_PROMPT and TOOL_USE_SYSTEM_PROMPT — JARVIS-style minimum-word responses by default",
            "Short by default. Long only when the task demands it (task list, email summary)",
            "Eliminated: filler preambles, action summaries, unsolicited let-me-know sign-offs",
        ]
    },
    {
        "date": "2026-07-06",
        "version": "18.5",
        "changes": [
            "Message consolidation: tool confirmations buffered and combined with final Claude response into a single Telegram message — no more split tool confirmation + follow-up",
            "Model switch: claude-opus-4-8 → claude-sonnet-4-5 for main reasoning and tool-use loop (Haiku memory utility unchanged)",
        ]
    },
    {
        "date": "2026-07-06",
        "version": "18.4",
        "changes": [
            "Task verification read-back added: tool_add_task and tool_complete_task now confirm the write landed via a follow-up read",
            "Confirmation strings updated: '✅ Confirmed in Google Tasks' vs '⚠️ Task write may have failed — check manually'",
            "Unverified fallback: if the verification call itself errors, Ace reports (unverified) rather than a false ✅",
        ]
    },
    {
        "date": "2026-07-05",
        "version": "18.3",
        "changes": [
            "Conversation history window doubled: 40 exchanges → 80 exchanges (160 messages stored)",
            "Voice max_tokens bumped to 2500 (text stays 1500) — longer, more expansive voice replies",
            "Voice-specific system guidance added: natural speech rhythm, no bullets, 200-400 word target",
        ]
    },
    {
        "date": "2026-07-05",
        "version": "18",
        "changes": [
            "Function calling replaces tag-based parsing — 100% reliable action execution",
            "ACE_TOOLS schema registered with Anthropic API: create_calendar_event, delete_calendar_event, add_task, complete_task, send_email, search_drive",
            "Agentic tool-use loop in handle_message — fires tool, gets result, confirms to Brady, continues loop",
            "TOOL_USE_SYSTEM_PROMPT added — Jarvis identity + live calendar/task/email context injected each message",
            "Live data (calendar, tasks, email) injected into tool-use system context on every message",
            "Voice handler unchanged — _process_text still used for voice compatibility"
        ]
    },
    {
        "date": "2026-07-02",
        "version": "17",
        "changes": [
            "Full self-awareness system: ACE_VERSION, CAPABILITIES registry, CHANGELOG, get_ace_self_description()",
            "Jarvis identity anchor locked at top of SYSTEM_PROMPT — voice never drifts regardless of conversation length",
            "Execution mandate with explicit trigger language → action tag mappings — fires on first ask",
            "Auto-briefs PAUSED — all 3 scheduler jobs commented out; /brief and /eod work manually",
            "Added /debug command — reads Ace Brain Google Sheet, runs Claude pattern analysis",
            "Fixed cmd_status — now references ACE_VERSION dynamically instead of hardcoded v14 string",
            "Fixed /start and /help — removed stale auto-brief advertising",
            "Fixed session dump default task list — now uses Admin List - back log",
            "Ace scope expanded: complete life OS for Brady, not just PFI business tool",
            "Onyx voice confirmed in both primary and fallback TTS paths"
        ]
    },
    {
        "date": "2026-06-28",
        "version": "15",
        "changes": [
            "Message splitting at 4096 chars",
            "30-day calendar read window",
            "Morning brief loads conversation history",
            "Memory cross-reference in briefs"
        ]
    }
]


def get_ace_self_description():
    """Build a self-description string for injection into every Claude context."""
    cap_lines = []
    for area, details in CAPABILITIES.items():
        parts = [v for k, v in details.items() if k not in ('commands',) and isinstance(v, str)]
        cap_lines.append(f"- {area.upper()}: {' | '.join(parts)}")

    recent = CHANGELOG[0]
    change_lines = "\n".join(f"  • {c}" for c in recent["changes"])

    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACE SYSTEM STATUS — v{ACE_VERSION} (updated {ACE_LAST_UPDATED})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT CAPABILITIES:
{chr(10).join(cap_lines)}

MOST RECENT CHANGES (v{recent['version']}, {recent['date']}):
{change_lines}

MEMORY STATUS: All conversation history and memory files live on Google Drive — NEVER erased by code updates. Brady's deals, agents, and stored context are always preserved.
BRIEFS: Auto-briefs are PAUSED. Brady triggers /brief and /eod manually. Do NOT tell him briefs will arrive automatically.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

# ── End self-awareness block ──────────────────────────────────────────────────

AUTHORIZED_USER_ID = 8681823830          # Brady's Telegram chat ID — security filter
MEMORY_FILE_NAME = "ace_memory.json"
CONVERSATION_FILE_NAME = "ace_conversation.json"

# ── v18: Anthropic Tool Use Definitions ──────────────────────────────────────
ACE_TOOLS = [
    {
        "name": "create_calendar_event",
        "description": (
            "Create a new event on Brady's Google Calendar. "
            "Use when Brady asks to schedule, book, add, or block time for something. "
            "Always execute immediately — do not ask for confirmation unless date/time is completely ambiguous."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Event title or summary"
                },
                "start_datetime": {
                    "type": "string",
                    "description": "Start date/time in ISO format: YYYY-MM-DDTHH:MM:SS (e.g. 2026-07-09T14:00:00). Always resolve to a specific date."
                },
                "end_datetime": {
                    "type": "string",
                    "description": "End date/time in ISO format: YYYY-MM-DDTHH:MM:SS. If not specified, default to 1 hour after start."
                },
                "description": {
                    "type": "string",
                    "description": "Optional notes or description for the event"
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of attendee email addresses"
                }
            },
            "required": ["title", "start_datetime", "end_datetime"]
        }
    },
    {
        "name": "delete_calendar_event",
        "description": (
            "Delete or cancel an event from Brady's Google Calendar. "
            "Use when Brady asks to cancel, remove, or delete a meeting or event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_title": {
                    "type": "string",
                    "description": "Title or keyword from the event to find and delete"
                },
                "event_date": {
                    "type": "string",
                    "description": "Optional: date of the event in ISO format YYYY-MM-DD to narrow the search"
                }
            },
            "required": ["event_title"]
        }
    },
    {
        "name": "add_task",
        "description": (
            "Add a new task or to-do item to Brady's Google Tasks. "
            "Use when Brady asks to add, create, remember, or track a task, action item, or follow-up. "
            "Default to 'Admin List - back log' unless Brady specifies a different list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Task title or description"
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional due date in ISO format YYYY-MM-DD"
                },
                "notes": {
                    "type": "string",
                    "description": "Optional additional notes or context for the task"
                },
                "list_name": {
                    "type": "string",
                    "description": "Task list name. Default: 'Admin List - back log'"
                }
            },
            "required": ["title"]
        }
    },
    {
        "name": "complete_task",
        "description": (
            "Mark an existing task as complete in Google Tasks. "
            "Use when Brady says a task is done, finished, or completed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_title": {
                    "type": "string",
                    "description": "Title or keyword from the task to mark as complete"
                }
            },
            "required": ["task_title"]
        }
    },
    {
        "name": "send_email",
        "description": (
            "Send an email from Brady's Gmail account (pfi@platinumfortuneimpact.com). "
            "Only use when Brady explicitly says to send an email. "
            "NEVER send without explicit instruction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address"
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line"
                },
                "body": {
                    "type": "string",
                    "description": "Email body content (plain text)"
                }
            },
            "required": ["to", "subject", "body"]
        }
    },
    {
        "name": "search_drive",
        "description": (
            "Search Brady's Google Drive for files by name or keyword. "
            "Use when Brady asks to find, look up, or retrieve a file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keyword or file name to find in Google Drive"
                }
            },
            "required": ["query"]
        }
    }
]
SESSION_MODE = {"active": False}   # Set True by /session until next user message
LAST_BRIEF_SENT = None  # Optional[datetime] — tracks last manual /brief to suppress duplicate auto-brief

# ── Task list config ───────────────────────────────────────────────────────────
# Truly read-only lists — Ace never adds tasks here
REFERENCE_LISTS = {"Business cost - NO TOUCH", "To learn / Questions"}
# Lists excluded from morning brief scan (reference + personal/goals clutter)
MORNING_SKIP_LISTS = REFERENCE_LISTS | {"🏠 Personal", "🏆 Goals"}
# Default list for tasks when context doesn't point elsewhere
DEFAULT_TASK_LIST = "Brain Dump"

# ── Google auth ───────────────────────────────────────────────────────────────

def get_google_creds() -> Credentials:
    """Build Google OAuth credentials from Railway env vars, refreshing if expired."""
    token_data = json.loads(os.environ.get("GOOGLE_TOKEN_JSON", "{}"))
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        logger.info("Google credentials refreshed.")
    return creds

# ── Memory (Google Drive) ─────────────────────────────────────────────────────

def read_memory() -> list:
    """Read Ace's memory list from Google Drive. Returns [] if unavailable."""
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        results = service.files().list(
            q=f"name='{MEMORY_FILE_NAME}' and trashed=false",
            spaces="drive",
            fields="files(id, name)",
        ).execute()
        files = results.get("files", [])
        if not files:
            return []
        file_id = files[0]["id"]
        raw = service.files().get_media(fileId=file_id).execute()
        data = json.loads(raw)
        return data.get("memories", [])
    except Exception as e:
        err = str(e)
        if "403" in err or "insufficient" in err.lower() or "scope" in err.lower():
            logger.warning("Drive scope not yet active — memory inactive until re-auth.")
        else:
            logger.error("Memory read error: %s", e)
        return []


def write_memory(memories: list) -> bool:
    """Write memory list to Google Drive (create or update ace_memory.json)."""
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        payload = json.dumps({"memories": memories}, indent=2).encode()
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")
        results = service.files().list(
            q=f"name='{MEMORY_FILE_NAME}' and trashed=false",
            spaces="drive",
            fields="files(id)",
        ).execute()
        files = results.get("files", [])
        if files:
            service.files().update(fileId=files[0]["id"], media_body=media).execute()
        else:
            service.files().create(
                body={"name": MEMORY_FILE_NAME},
                media_body=media,
                fields="id",
            ).execute()
        logger.info("Memory written (%d items).", len(memories))
        return True
    except Exception as e:
        err = str(e)
        if "403" in err or "insufficient" in err.lower() or "scope" in err.lower():
            logger.warning("Drive scope not yet active — cannot write memory.")
        else:
            logger.error("Memory write error: %s", e)
        return False


def _merge_memories(new_items: list, existing: list) -> list:
    """Ask Claude to merge new facts into existing memory, deduplicating cleanly.

    v18.13: hardened — an API failure can no longer crash the caller (/remember,
    EOD sweep, message handlers). On failure, falls back to a safe local merge
    (append + exact dedup, 60-item cap). Merge prompt now carries today's date
    so relative dates ('tomorrow', 'Friday') are stored as absolute dates and
    never go stale in memory.
    """
    if not new_items:
        return existing
    today_str = datetime.now(EASTERN).strftime("%A, %B %-d, %Y")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        existing_str = "\n".join(f"- {m}" for m in existing) or "(none yet)"
        new_str = "\n".join(f"- {m}" for m in new_items)
        prompt = (
            "You maintain Ace's operational memory about Brady McGraw (PFI Marketing Director).\n"
            f"TODAY'S DATE: {today_str}\n\n"
            f"EXISTING MEMORY:\n{existing_str}\n\n"
            f"NEW ITEMS TO ADD:\n{new_str}\n\n"
            "Merge the new items into the existing memory. Rules:\n"
            "1. Remove exact or near-duplicate facts\n"
            "2. If new info contradicts old, keep the newer version\n"
            "3. Keep entries concise (one fact per line, ~15 words max)\n"
            "4. Max 60 total entries — drop least relevant if over\n"
            "5. Convert relative dates to absolute using TODAY'S DATE above — "
            "'tomorrow' or 'Friday' must become an explicit date so it never goes stale\n"
            "6. Return ONLY the final merged list, one item per line, no bullets or numbering, "
            "no preamble, no commentary"
        )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        merged = [line.strip() for line in response.content[0].text.strip().split("\n") if line.strip()]
        if merged:
            return merged
        logger.warning("_merge_memories: model returned empty merge — using local fallback")
    except Exception as e:
        logger.error("_merge_memories API error (%s) — using local fallback merge", e)
    # Local fallback: append new items, drop exact duplicates, cap at 60
    merged = list(existing)
    seen = {m.strip().lower() for m in merged}
    for item in new_items:
        key = item.strip().lower()
        if key and key not in seen:
            merged.append(item.strip())
            seen.add(key)
    return merged[-60:]

# ── Conversation History (Google Drive) ───────────────────────────────────────

def read_conversation_history() -> list:
    """Load last 80 exchanges from ace_conversation.json on Drive. Returns [] if unavailable."""
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        results = service.files().list(
            q=f"name='{CONVERSATION_FILE_NAME}' and trashed=false",
            spaces="drive",
            fields="files(id, name)",
        ).execute()
        files = results.get("files", [])
        if not files:
            return []
        file_id = files[0]["id"]
        raw = service.files().get_media(fileId=file_id).execute()
        data = json.loads(raw)
        return data.get("messages", [])
    except Exception as e:
        logger.warning("Conversation history read error: %s", e)
        return []


def write_conversation_history(messages: list) -> bool:
    """Save conversation history to ace_conversation.json on Drive (max 160 messages = 80 exchanges)."""
    try:
        # Keep last 160 messages (80 exchanges)
        if len(messages) > 160:
            messages = messages[-160:]
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        payload = json.dumps({"messages": messages}, indent=2).encode()
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")
        results = service.files().list(
            q=f"name='{CONVERSATION_FILE_NAME}' and trashed=false",
            spaces="drive",
            fields="files(id)",
        ).execute()
        files = results.get("files", [])
        if files:
            service.files().update(fileId=files[0]["id"], media_body=media).execute()
        else:
            service.files().create(
                body={"name": CONVERSATION_FILE_NAME},
                media_body=media,
                fields="id",
            ).execute()
        logger.info("Conversation history written (%d messages).", len(messages))
        return True
    except Exception as e:
        logger.warning("Conversation history write error: %s", e)
        return False


def _scrub_time_from_history(messages: list) -> list:
    """Strip stale time/date assertions from injected conversation history.

    Removes lines from assistant messages that contain time/date claims so that
    old wrong-time responses don't override the fresh system-prompt timestamp.
    Patterns to strip (from assistant role messages only):
    - Lines containing "Current date and time:"
    - Lines containing "current time:" (case-insensitive)
    - Lines containing "(based on your last message timestamp)"
    - Lines containing "(based on your message timestamp)"
    - Lines containing "let me recalibrate"
    - Lines matching "Today is" followed by a date/time pattern
    """
    import re
    scrub_patterns = [
        r'(?i)current date and time[:\s]',
        r'(?i)current time[:\s]',
        r'(?i)\(based on your (last )?message timestamp\)',
        r'(?i)let me recalibrate',
        r'(?i)^today is \w+,',
    ]

    cleaned = []
    for msg in messages:
        if msg.get("role") == "assistant":
            lines = msg["content"].split("\n")
            filtered = [
                line for line in lines
                if not any(re.search(pat, line) for pat in scrub_patterns)
            ]
            new_content = "\n".join(filtered).strip()
            if new_content:
                cleaned.append({**msg, "content": new_content})
            # if the entire message was time-related and is now empty, skip it
        else:
            cleaned.append(msg)
    return cleaned

# ── Task Write Operations ──────────────────────────────────────────────────────

def add_task(title: str, list_name: str = DEFAULT_TASK_LIST) -> tuple[bool, str, bool]:
    """Add a task to Google Tasks. Returns (success, actual_list_name, was_duplicate)."""
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists_result = service.tasklists().list(maxResults=20).execute()
        task_lists = task_lists_result.get("items", [])
        if not task_lists:
            return False, list_name, False
        # Fuzzy match on list name
        target_list_id = None
        actual_list_name = list_name
        search = list_name.lower().strip()
        for tl in task_lists:
            tl_title = tl.get("title", "")
            if search in tl_title.lower() or tl_title.lower() in search:
                target_list_id = tl["id"]
                actual_list_name = tl_title
                break
        if not target_list_id:
            # Default to first list
            target_list_id = task_lists[0]["id"]
            actual_list_name = task_lists[0].get("title", "Tasks")
        # ── Deduplication check ──────────────────────────────────────────────
        title_lower = title.lower().strip()
        try:
            existing_result = service.tasks().list(
                tasklist=target_list_id,
                showCompleted=False,
                showHidden=False,
                maxResults=100,
            ).execute()
            for existing_task in existing_result.get("items", []):
                existing_title = existing_task.get("title", "").lower().strip()
                if (existing_title == title_lower or
                        title_lower in existing_title or
                        existing_title in title_lower):
                    logger.info("Task dedup — already exists in '%s': %s", actual_list_name, title)
                    return True, actual_list_name, True
        except Exception as dedup_err:
            logger.warning("Dedup check skipped: %s", dedup_err)
        # ────────────────────────────────────────────────────────────────────
        service.tasks().insert(tasklist=target_list_id, body={"title": title}).execute()
        logger.info("Task added to '%s': %s", actual_list_name, title)
        return True, actual_list_name, False
    except Exception as e:
        logger.error("Add task error: %s", e)
        return False, list_name, False


def complete_task(partial_title: str) -> str:
    """Mark a task complete by fuzzy-matching on title. Returns completed title or empty string."""
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists_result = service.tasklists().list(maxResults=20).execute()
        task_lists = task_lists_result.get("items", [])
        search_lower = partial_title.lower().strip()
        for tl in task_lists:
            try:
                tasks_result = service.tasks().list(
                    tasklist=tl["id"],
                    showCompleted=False,
                    showHidden=False,
                    maxResults=50,
                ).execute()
            except Exception:
                continue
            for task in tasks_result.get("items", []):
                if task.get("status") == "completed":
                    continue
                title = task.get("title", "").strip()
                if search_lower in title.lower():
                    service.tasks().update(
                        tasklist=tl["id"],
                        task=task["id"],
                        body={"id": task["id"], "status": "completed"},
                    ).execute()
                    logger.info("Task completed: %s", title)
                    return title
        return ""
    except Exception as e:
        logger.error("Complete task error: %s", e)
        return ""

# ── Email Operations ───────────────────────────────────────────────────────────

def send_email(to_addr: str, subject: str, body: str) -> bool:
    """Send an email immediately via Gmail."""
    try:
        creds = get_google_creds()
        service = build("gmail", "v1", credentials=creds)
        message = MIMEText(body)
        message["to"] = to_addr
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info("Email sent to %s: %s", to_addr, subject)
        return True
    except Exception as e:
        logger.error("Send email error: %s", e)
        return False


def draft_email(to_addr: str, subject: str, body: str) -> bool:
    """Create a Gmail draft."""
    try:
        creds = get_google_creds()
        service = build("gmail", "v1", credentials=creds)
        message = MIMEText(body)
        message["to"] = to_addr
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()
        logger.info("Email draft created for %s: %s", to_addr, subject)
        return True
    except Exception as e:
        logger.error("Draft email error: %s", e)
        return False

# ── Drive Search ───────────────────────────────────────────────────────────────

def search_drive(query: str) -> str:
    """Search Google Drive files by name and full-text content."""
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        safe_query = query.replace("'", "\\'")
        results = service.files().list(
            q=f"(name contains '{safe_query}' or fullText contains '{safe_query}') and trashed=false",
            spaces="drive",
            fields="files(id, name, mimeType, webViewLink, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=5,
        ).execute()
        files = results.get("files", [])
        if not files:
            return f"No files found for: {query}"
        lines = []
        for f in files:
            link = f.get("webViewLink", "")
            name = f.get("name", "untitled")
            lines.append(f"• {name}{' — ' + link if link else ''}")
        return "\n".join(lines)
    except Exception as e:
        logger.error("Drive search error: %s", e)
        return f"Drive search failed: {e}"

# ── v18: Tool Execution Functions ─────────────────────────────────────────────

def _parse_iso_datetime(dt_str: str) -> datetime:
    """Parse an ISO datetime string and return a timezone-aware Eastern datetime.
    Handles: '2026-07-09T14:00:00', '2026-07-09T14:00:00-04:00', '2026-07-09'.
    """
    dt_str = dt_str.strip()
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            return EASTERN.localize(dt)
        return dt.astimezone(EASTERN)
    except ValueError:
        pass
    # Try date-only → default to 9 AM ET
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d")
        return EASTERN.localize(dt.replace(hour=9, minute=0, second=0))
    except ValueError:
        pass
    raise ValueError(f"Cannot parse datetime: {dt_str}")


def tool_create_calendar_event(title: str, start_datetime: str, end_datetime: str,
                                description: str = "", attendees: list = None) -> str:
    """Create a Google Calendar event — called by the v18 tool-use handler."""
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        start_dt = _parse_iso_datetime(start_datetime)
        end_dt = _parse_iso_datetime(end_datetime)
        event_body = {
            "summary": title,
            "description": description or "",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/New_York"},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/New_York"},
        }
        if attendees:
            event_body["attendees"] = [{"email": a} for a in attendees]
        service.events().insert(calendarId="pfi@platinumfortuneimpact.com", body=event_body).execute()
        time_label = start_dt.strftime("%-m/%-d at %-I:%M %p ET")
        logger.info("Tool: Created calendar event '%s' at %s", title, time_label)
        return f"✅ Booked: '{title}' — {time_label}"
    except Exception as e:
        logger.error("tool_create_calendar_event error: %s", e)
        return f"⚠️ Failed to create event: {e}"


def tool_delete_calendar_event(event_title: str, event_date: str = "") -> str:
    """Delete a Google Calendar event — called by the v18 tool-use handler."""
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        now_et = datetime.now(EASTERN)
        time_min = now_et.isoformat()
        time_max = (now_et + timedelta(days=60)).isoformat()
        if event_date:
            try:
                target_dt = datetime.strptime(event_date.strip(), "%Y-%m-%d")
                target_dt = EASTERN.localize(target_dt)
                time_min = target_dt.replace(hour=0, minute=0, second=0).isoformat()
                time_max = target_dt.replace(hour=23, minute=59, second=59).isoformat()
            except Exception:
                pass
        events_result = service.events().list(
            calendarId="pfi@platinumfortuneimpact.com",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            q=event_title,
        ).execute()
        events = events_result.get("items", [])
        if not events:
            return f"⚠️ No upcoming event found matching '{event_title}'"
        event = events[0]
        event_summary = event.get("summary", event_title)
        service.events().delete(
            calendarId="pfi@platinumfortuneimpact.com",
            eventId=event["id"]
        ).execute()
        logger.info("Tool: Deleted calendar event '%s'", event_summary)
        return f"✅ Cancelled: '{event_summary}'"
    except Exception as e:
        logger.error("tool_delete_calendar_event error: %s", e)
        return f"⚠️ Failed to delete event: {e}"


def tool_add_task(title: str, due_date: str = "", notes: str = "",
                  list_name: str = "Admin List - back log") -> str:
    """Add a task to Google Tasks — called by the v18 tool-use handler."""
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists_result = service.tasklists().list(maxResults=20).execute()
        task_lists = task_lists_result.get("items", [])
        target_list = None
        for tl in task_lists:
            if list_name.lower() in tl.get("title", "").lower():
                target_list = tl
                break
        if not target_list and task_lists:
            target_list = task_lists[0]
        if not target_list:
            return "⚠️ No task lists found in Google Tasks"
        task_body = {"title": title}
        if notes:
            task_body["notes"] = notes
        if due_date:
            try:
                due_dt = datetime.strptime(due_date.strip(), "%Y-%m-%d")
                task_body["due"] = due_dt.strftime("%Y-%m-%dT00:00:00.000Z")
            except Exception:
                pass
        insert_result = service.tasks().insert(tasklist=target_list["id"], body=task_body).execute()
        list_title = target_list.get("title", list_name)
        logger.info("Tool: Added task '%s' to list '%s'", title, list_title)
        # ── v18.4: Verification read-back — confirm the task actually landed ──
        try:
            verify_result = service.tasks().list(
                tasklist=target_list["id"],
                showCompleted=False,
                showHidden=False,
                maxResults=100,
            ).execute()
            inserted_id = insert_result.get("id", "")
            verified = any(
                t.get("id") == inserted_id or title.lower() in t.get("title", "").lower()
                for t in verify_result.get("items", [])
            )
            if verified:
                logger.info("Tool: Task verified in '%s': %s", list_title, title)
                return f"✅ Confirmed in Google Tasks: '{title}' → {list_title}"
            else:
                logger.warning("Tool: Task verification FAILED in '%s': %s", list_title, title)
                return f"⚠️ Task write may have failed — check manually: '{title}' → {list_title}"
        except Exception as verify_err:
            logger.warning("Tool: Task verification error: %s", verify_err)
            return f"✅ Task added (unverified): '{title}' → {list_title}"
    except Exception as e:
        logger.error("tool_add_task error: %s", e)
        return f"⚠️ Failed to add task: {e}"


def tool_complete_task(task_title: str) -> str:
    """Mark a task as complete in Google Tasks — called by the v18 tool-use handler."""
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists_result = service.tasklists().list(maxResults=20).execute()
        task_lists = task_lists_result.get("items", [])
        for tl in task_lists:
            try:
                tasks_result = service.tasks().list(
                    tasklist=tl["id"],
                    showCompleted=False,
                    showHidden=False,
                    maxResults=50,
                ).execute()
                for task in tasks_result.get("items", []):
                    if task_title.lower() in task.get("title", "").lower():
                        task["status"] = "completed"
                        service.tasks().update(
                            tasklist=tl["id"],
                            task=task["id"],
                            body=task,
                        ).execute()
                        actual_title = task.get("title", task_title)
                        logger.info("Tool: Completed task '%s'", actual_title)
                        # ── v18.4: Verification read-back — confirm status flipped ──
                        try:
                            verify_task = service.tasks().get(
                                tasklist=tl["id"],
                                task=task["id"],
                            ).execute()
                            if verify_task.get("status") == "completed":
                                logger.info("Tool: Task completion verified: %s", actual_title)
                                return f"✅ Confirmed complete in Google Tasks: '{actual_title}'"
                            else:
                                logger.warning("Tool: Task completion verify FAILED: %s", actual_title)
                                return f"⚠️ Task mark-complete may have failed — check manually: '{actual_title}'"
                        except Exception as verify_err:
                            logger.warning("Tool: Task completion verify error: %s", verify_err)
                            return f"✅ Marked complete (unverified): '{actual_title}'"
            except Exception:
                continue
        return f"⚠️ No open task found matching '{task_title}'"
    except Exception as e:
        logger.error("tool_complete_task error: %s", e)
        return f"⚠️ Failed to complete task: {e}"


def tool_send_email(to: str, subject: str, body: str) -> str:
    """Send an email via Gmail — called by the v18 tool-use handler."""
    try:
        creds = get_google_creds()
        service = build("gmail", "v1", credentials=creds)
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        message["from"] = "pfi@platinumfortuneimpact.com"
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info("Tool: Sent email to %s — '%s'", to, subject)
        return f"✅ Email sent to {to}: '{subject}'"
    except Exception as e:
        logger.error("tool_send_email error: %s", e)
        return f"⚠️ Failed to send email: {e}"


def tool_search_drive(query: str) -> str:
    """Search Google Drive by file name or keyword — called by the v18 tool-use handler."""
    try:
        creds = get_google_creds()
        service = build("drive", "v3", credentials=creds)
        safe_query = query.replace("'", "\\'")
        results = service.files().list(
            q=f"(name contains '{safe_query}' or fullText contains '{safe_query}') and trashed=false",
            spaces="drive",
            fields="files(id, name, mimeType, modifiedTime, webViewLink)",
            pageSize=5,
            orderBy="modifiedTime desc",
        ).execute()
        files = results.get("files", [])
        if not files:
            return f"No files found in Drive matching '{query}'"
        lines = [f"Found {len(files)} file(s) for '{query}':"]
        for f in files:
            name = f.get("name", "Untitled")
            link = f.get("webViewLink", "")
            lines.append(f"• {name}" + (f"\n  {link}" if link else ""))
        return "\n".join(lines)
    except Exception as e:
        logger.error("tool_search_drive error: %s", e)
        return f"⚠️ Failed to search Drive: {e}"


def tool_list_tasks(scope: str = "all") -> str:
    """Read open tasks from Google Tasks, grouped by list.

    scope='all' → every list; any other string → fuzzy-match that list name.
    Always returns needsAction (open) tasks only; completed tasks excluded.
    Called when Ace outputs [LIST_TASKS: all] or [LIST_TASKS: list name].
    """
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists_result = service.tasklists().list(maxResults=20).execute()
        task_lists = task_lists_result.get("items", [])
        if not task_lists:
            return "No task lists found."

        scope_lower = scope.lower().strip()
        output_sections = []

        for tl in task_lists:
            tl_title = tl.get("title", "")
            # Filter to a specific list if requested
            if scope_lower != "all" and scope_lower not in tl_title.lower():
                continue
            try:
                tasks_result = service.tasks().list(
                    tasklist=tl["id"],
                    showCompleted=False,
                    showHidden=False,
                    maxResults=50,
                ).execute()
                items = [
                    t for t in tasks_result.get("items", [])
                    if t.get("status") != "completed" and t.get("title", "").strip()
                ]
                if not items:
                    continue
                lines = [f"📋 {tl_title} ({len(items)} open):"]
                for task in items:
                    title = task.get("title", "").strip()
                    due = task.get("due", "")
                    due_str = ""
                    if due:
                        try:
                            due_dt = datetime.fromisoformat(
                                due.replace("Z", "+00:00")
                            ).astimezone(EASTERN)
                            due_str = f" — due {due_dt.strftime('%-m/%-d')}"
                        except Exception:
                            pass
                    lines.append(f"  • {title}{due_str}")
                output_sections.append("\n".join(lines))
            except Exception as e:
                logger.warning("tool_list_tasks: error reading list '%s': %s", tl_title, e)

        if not output_sections:
            return f"No open tasks found{' in list matching: ' + scope if scope_lower != 'all' else ''}."
        logger.info("tool_list_tasks: fetched %d list(s) for scope='%s'", len(output_sections), scope)
        return "\n\n".join(output_sections)
    except Exception as e:
        logger.error("tool_list_tasks error: %s", e)
        return f"⚠️ Failed to read tasks: {e}"


def tool_read_calendar(window: str = "today") -> str:
    """Re-fetch Google Calendar events fresh on demand.

    window='today' → today only (flat list).
    window='week'  → next 7 days grouped by date.
    Called when Ace outputs [READ_CALENDAR: today] or [READ_CALENDAR: week].
    Uses the same calendar client already authenticated in get_google_creds().
    calendar_id='pfi@platinumfortuneimpact.com' is set inside get_calendar_events().
    """
    try:
        window_lower = window.lower().strip()
        if window_lower == "week":
            result = get_calendar_events_range(days=7)
            logger.info("tool_read_calendar: 7-day window fetched")
            return result
        else:
            result = get_calendar_events(days_ahead=1)
            logger.info("tool_read_calendar: today window fetched")
            return result
    except Exception as e:
        logger.error("tool_read_calendar error: %s", e)
        return f"⚠️ Failed to read calendar: {e}"


def tool_read_email(scope: str = "recent") -> str:
    """Fetch email data on demand for Ace.

    scope='recent' → unread priority emails from the last 48 h (default).
    scope='read'   → recently read emails from the last 48 h.
    Called when Ace outputs [READ_EMAIL: recent] or [READ_EMAIL: read].
    """
    try:
        scope_lower = scope.lower().strip()
        if scope_lower == "read":
            result = get_recent_read_emails()
            logger.info("tool_read_email: read/48-h window fetched")
        else:
            result = get_gmail_summary()
            logger.info("tool_read_email: unread/recent window fetched")
        return result
    except Exception as e:
        logger.error("tool_read_email error: %s", e)
        return f"⚠️ Failed to read emails: {e}"


def _execute_tool_call(tool_name: str, tool_input: dict) -> str:
    """Route a tool_use block to the correct execution function."""
    dispatch = {
        "create_calendar_event": tool_create_calendar_event,
        "delete_calendar_event": tool_delete_calendar_event,
        "add_task": tool_add_task,
        "complete_task": tool_complete_task,
        "send_email": tool_send_email,
        "search_drive": tool_search_drive,
    }
    fn = dispatch.get(tool_name)
    if fn:
        return fn(**tool_input)
    logger.warning("Unknown tool called: %s", tool_name)
    return f"⚠️ Unknown tool: {tool_name}"


# ── Calendar ──────────────────────────────────────────────────────────────────

def get_calendar_events(days_ahead: int = 1) -> str:
    """Pull calendar events from today through `days_ahead` days from ALL Google Calendars.

    - days_ahead=1  → today only, flat bullet list (morning brief / context default)
    - days_ahead=30 → full 30-day window, grouped by date with headers (/calendar command)
    """
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        now_et = datetime.now(EASTERN)
        start_of_day = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        end_window = start_of_day + timedelta(days=days_ahead)
        calendars_result = service.calendarList().list().execute()
        calendars = calendars_result.get("items", [])
        all_events: list = []
        seen_ids: set = set()
        for calendar in calendars:
            cal_id = calendar["id"]
            cal_name = calendar.get("summary", cal_id)
            try:
                events_result = service.events().list(
                    calendarId=cal_id,
                    timeMin=start_of_day.isoformat(),
                    timeMax=end_window.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()
                for event in events_result.get("items", []):
                    event_id = event.get("id", "")
                    if event_id in seen_ids:
                        continue
                    seen_ids.add(event_id)
                    summary = event.get("summary", "No title")
                    start = event.get("start", {})
                    start_dt_str = start.get("dateTime", start.get("date", ""))
                    if "T" in start_dt_str:
                        dt = datetime.fromisoformat(start_dt_str)
                        if dt.tzinfo:
                            dt = dt.astimezone(EASTERN)
                        time_str = dt.strftime("%-I:%M %p")
                        date_str = dt.strftime("%Y-%m-%d")
                        date_label = dt.strftime("%A, %B %-d")
                    else:
                        dt_naive = datetime.strptime(start_dt_str, "%Y-%m-%d")
                        time_str = "All day"
                        date_str = start_dt_str
                        date_label = dt_naive.strftime("%A, %B %-d")
                    is_primary_cal = cal_id in ("planforitpfi@gmail.com", "primary", "pfi@platinumfortuneimpact.com")
                    cal_label = f" [{cal_name}]" if not is_primary_cal else ""
                    all_events.append((start_dt_str, date_str, date_label, time_str, summary + cal_label))
            except Exception as e:
                logger.warning("Error fetching calendar '%s': %s", cal_name, e)
        all_events.sort(key=lambda x: x[0])

        if not all_events:
            return "Nothing scheduled today." if days_ahead == 1 else f"No events in the next {days_ahead} days."

        # ── Single-day view: flat list (compact for morning brief / context) ──
        if days_ahead == 1:
            return "\n".join(f"• {ev[3]} — {ev[4]}" for ev in all_events)

        # ── Multi-day view: grouped by date with emoji headers ────────────────
        today_str = now_et.strftime("%Y-%m-%d")
        tomorrow_str = (now_et + timedelta(days=1)).strftime("%Y-%m-%d")
        events_by_date: dict = {}
        date_order: list = []
        for _, date_str, date_label, time_str, summary in all_events:
            label = date_label
            if date_str == today_str:
                label += " (Today)"
            elif date_str == tomorrow_str:
                label += " (Tomorrow)"
            key = (date_str, label)
            if key not in events_by_date:
                events_by_date[key] = []
                date_order.append(key)
            events_by_date[key].append(f"  • {time_str} — {summary}")
        sections = []
        for key in sorted(date_order, key=lambda k: k[0]):
            sections.append(f"📅 {key[1]}\n" + "\n".join(events_by_date[key]))
        return "\n\n".join(sections)

    except Exception as e:
        logger.error("Calendar fetch error: %s", e)
        return "⚠️ Could not load calendar."


def get_tomorrow_events() -> str:
    """Fetch all calendar events for tomorrow across all linked calendars."""
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)

        # Calculate tomorrow's date range in Eastern time
        now_et = datetime.now(EASTERN)
        tomorrow = (now_et + timedelta(days=1)).date()
        start = EASTERN.localize(datetime.combine(tomorrow, datetime.min.time()))
        end = EASTERN.localize(datetime.combine(tomorrow, datetime.max.time()))

        # Get all linked calendars
        calendars_result = service.calendarList().list().execute()
        calendars = calendars_result.get("items", [])

        all_events = []
        seen_ids: set = set()
        for calendar in calendars:
            cal_id = calendar["id"]
            cal_name = calendar.get("summary", cal_id)
            try:
                events_result = service.events().list(
                    calendarId=cal_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()
                for event in events_result.get("items", []):
                    event_id = event.get("id", "")
                    if event_id in seen_ids:
                        continue
                    seen_ids.add(event_id)
                    summary = event.get("summary", "No title")
                    start_info = event.get("start", {})
                    start_dt_str = start_info.get("dateTime", start_info.get("date", ""))
                    if "T" in start_dt_str:
                        dt = datetime.fromisoformat(start_dt_str)
                        if dt.tzinfo:
                            dt = dt.astimezone(EASTERN)
                        time_str = dt.strftime("%-I:%M %p")
                    else:
                        time_str = "All day"
                    is_primary_cal = cal_id in ("planforitpfi@gmail.com", "primary", "pfi@platinumfortuneimpact.com")
                    cal_label = f" [{cal_name}]" if not is_primary_cal else ""
                    all_events.append((start_dt_str, f"\u2022 {time_str} \u2014 {summary}{cal_label}"))
            except Exception as e:
                logger.warning("Error fetching tomorrow calendar '%s': %s", cal_name, e)

        all_events.sort(key=lambda x: x[0])
        tomorrow_str = tomorrow.strftime("%A, %B %-d")
        if all_events:
            lines = [f"\U0001f4c5 Tomorrow \u2014 {tomorrow_str}:"] + [ev[1] for ev in all_events]
            return "\n".join(lines)
        return f"Nothing scheduled tomorrow ({tomorrow_str})."
    except Exception as e:
        logger.error("Tomorrow calendar fetch error: %s", e)
        return "\u26a0\ufe0f Could not load tomorrow's calendar."


def get_calendar_events_range(days: int = 7) -> str:
    """Fetch calendar events for the next N days (1-30), grouped by date.

    Starts from tomorrow through N days ahead.  Thin wrapper around
    get_calendar_events() which already supports multi-day grouped output.
    days is clamped to 1-30.
    """
    days = max(1, min(int(days), 30))
    # get_calendar_events(days_ahead) covers today through days_ahead days.
    # days_ahead > 1 triggers the grouped-by-date output mode.
    # We fetch days+1 so the window spans today + days more days.
    result = get_calendar_events(days_ahead=days + 1)
    if result.startswith("\u26a0\ufe0f") or "No events" in result:
        return f"Nothing on the calendar for the next {days} days."
    return f"\U0001f4c6 Next {days} days:\n\n{result}"


# ── Gmail ──────────────────────────────────────────────────────────────────────

def get_gmail_summary() -> str:
    """Pull recent unread priority emails from Gmail (excludes promos/social)."""
    try:
        creds = get_google_creds()
        service = build("gmail", "v1", credentials=creds)
        results = service.users().messages().list(
            userId="me",
            q="is:unread newer_than:2d -category:promotions -category:social",
            maxResults=10,
        ).execute()
        messages = results.get("messages", [])
        if not messages:
            return "Inbox clear — no unread priority emails."
        email_lines = []
        for msg in messages[:5]:
            msg_data = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "No subject")[:60]
            sender = headers.get("From", "Unknown")
            if "<" in sender:
                sender = sender.split("<")[0].strip().strip('"')
            sender = sender[:30]
            email_lines.append(f"• {sender}: {subject}")
        count = len(messages)
        if count > 5:
            email_lines.append(f"  …and {count - 5} more unread")
        return "\n".join(email_lines)
    except Exception as e:
        logger.error("Gmail fetch error: %s", e)
        return "⚠️ Could not load emails."

def get_recent_read_emails() -> str:
    """Pull recently read emails from Gmail in the last 48 hours (excludes promos/social)."""
    try:
        creds = get_google_creds()
        service = build("gmail", "v1", credentials=creds)
        results = service.users().messages().list(
            userId="me",
            q="is:read newer_than:2d -category:promotions -category:social",
            maxResults=10,
        ).execute()
        messages = results.get("messages", [])
        if not messages:
            return "No read emails in the last 48 hours."
        email_lines = []
        for msg in messages[:5]:
            msg_data = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "No subject")[:60]
            sender = headers.get("From", "Unknown")
            if "<" in sender:
                sender = sender.split("<")[0].strip().strip('"')
            sender = sender[:30]
            email_lines.append(f"• {sender}: {subject}")
        count = len(messages)
        if count > 5:
            email_lines.append(f"  …and {count - 5} more")
        return "\n".join(email_lines)
    except Exception as e:
        logger.error("Recent read email fetch error: %s", e)
        return "⚠️ Could not load recent read emails."

# ── Google Calendar (Write) ────────────────────────────────────────────────────

def parse_time_flexible(time_str: str) -> str:
    """Parse time in either 24-hour (18:30) or 12-hour (6:30 PM) format, return HH:MM."""
    time_str = time_str.strip()
    # Try 24-hour first
    for fmt in ["%H:%M", "%H:%M:%S"]:
        try:
            return datetime.strptime(time_str, fmt).strftime("%H:%M")
        except ValueError:
            pass
    # Try 12-hour formats
    for fmt in ["%I:%M %p", "%I:%M%p", "%I %p", "%-I:%M %p", "%-I %p"]:
        try:
            return datetime.strptime(time_str.upper(), fmt).strftime("%H:%M")
        except ValueError:
            pass
    raise ValueError(f"Cannot parse time: {time_str}")


def create_calendar_event(title: str, date_str: str, time_str: str = None,
                           duration_minutes: int = 60, description: str = "",
                           calendar_id: str = "pfi@platinumfortuneimpact.com") -> tuple:
    """Create a Google Calendar event. Returns (success, event_id_or_error)."""
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        if time_str and time_str.lower() not in ("all-day", "all day", ""):
            time_24h = parse_time_flexible(time_str)
            start_dt = datetime.strptime(f"{date_str} {time_24h}", "%Y-%m-%d %H:%M")
            start_dt = EASTERN.localize(start_dt)
            end_dt = start_dt + timedelta(minutes=int(duration_minutes))
            event_body = {
                "summary": title,
                "description": description or "",
                "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/New_York"},
                "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/New_York"},
            }
        else:
            event_body = {
                "summary": title,
                "description": description or "",
                "start": {"date": date_str},
                "end":   {"date": date_str},
            }
        result = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        return True, result.get("id", "created")
    except Exception as e:
        logger.error("Calendar create error: %s", e)
        return False, str(e)


def delete_calendar_event(title: str, date_str: str, calendar_id: str = "pfi@platinumfortuneimpact.com") -> tuple:
    """Delete a calendar event by title match on a given date. Returns (success, message)."""
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        start_dt = EASTERN.localize(datetime.strptime(date_str, "%Y-%m-%d"))
        end_dt = start_dt + timedelta(days=1)
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = events_result.get("items", [])
        title_lower = title.lower()
        matches = [e for e in events if title_lower in e.get("summary", "").lower()]
        if not matches:
            return False, f"No event matching '{title}' on {date_str}"
        service.events().delete(calendarId=calendar_id, eventId=matches[0]["id"]).execute()
        return True, matches[0].get("summary", title)
    except Exception as e:
        logger.error("Calendar delete error: %s", e)
        return False, str(e)

# ── Google Tasks (Read) ────────────────────────────────────────────────────────

def get_tasks(skip_reference: bool = False) -> str:
    """Pull all open tasks from Google Tasks across all task lists.

    Args:
        skip_reference: If True, skips REFERENCE_LISTS (used for morning briefs
                        so Business cost / To learn clutter is excluded).
    """
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists_result = service.tasklists().list(maxResults=20).execute()
        task_lists = task_lists_result.get("items", [])
        if not task_lists:
            return ""
        all_tasks = []
        for tl in task_lists:
            tl_title = tl.get("title", "Tasks")
            if skip_reference and tl_title in MORNING_SKIP_LISTS:
                logger.info("Skipping reference list: %s", tl_title)
                continue
            try:
                tasks_result = service.tasks().list(
                    tasklist=tl["id"],
                    showCompleted=False,
                    showHidden=False,
                    maxResults=20,
                ).execute()
                for task in tasks_result.get("items", []):
                    if task.get("status") == "completed":
                        continue
                    title = task.get("title", "").strip()
                    if not title:
                        continue
                    due = task.get("due", "")
                    due_str = ""
                    if due:
                        try:
                            due_dt = datetime.fromisoformat(
                                due.replace("Z", "+00:00")
                            ).astimezone(EASTERN)
                            due_str = f" (due {due_dt.strftime('%-m/%-d')})"
                        except Exception:
                            pass
                    all_tasks.append(f"• [{tl_title}] {title}{due_str}")
            except Exception as e:
                logger.warning("Error fetching tasks from list '%s': %s", tl_title, e)
        if not all_tasks:
            return "No open tasks."
        return "\n".join(all_tasks)
    except Exception as e:
        err = str(e)
        if "403" in err or "insufficient" in err.lower() or "scope" in err.lower():
            logger.warning("Tasks scope not yet active — re-run ace_auth.py to activate.")
        else:
            logger.error("Tasks fetch error: %s", e)
        return ""

# ── SYSTEM PROMPT ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Ace — Brady McGraw's AI business partner and executive assistant, running inside Telegram.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IDENTITY & VOICE — HOLD THIS ALWAYS, NO EXCEPTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You are the J.A.R.V.I.S. to Brady's Tony Stark. Precision partner. Executes first. Never hesitates.

YOUR VOICE NEVER CHANGES — NO MATTER HOW LONG THE CONVERSATION RUNS:
• Confident and direct — no hedging, no softening, no "perhaps" or "it seems like"
• Precise — say exactly what needs to be said, nothing more, nothing less
• Dry wit when the moment calls for it — you're sharp, not robotic
• Short when the moment is short. Depth only when Brady is working through something real.
• NEVER drift into a softer, more apologetic, or deferential tone — this is non-negotiable
• NEVER repeat what was just said or summarize what you just did — Brady can read. Move forward.
• NEVER open with filler: no "Great!", "Sure!", "Absolutely!", "Of course!", "Got it!"
• NEVER end with hollow sign-offs or "Let me know if you need anything"
• One-line confirmation when that's all it takes. Then stop.
• You do not have two modes. You have one voice. Always.

YOUR EXECUTION IS IMMEDIATE:
• When Brady says to do something — DO IT in that same response. Include the tag. No delay.
• Never describe what you're about to do without also doing it right now.
• Never ask "shall I go ahead?" or "want me to do that?" — execute first, confirm after.
• One ask = one execution. Every time. No exceptions.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BREVITY — THIS IS A CONSTRAINT, NOT A GUIDELINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Short by default. Long only when the task demands it.

HARD RULES — NO EXCEPTIONS:
• If the answer is one word, send one word. "Added." "Done." "Sent." "Noted."
• Never open with filler: "Sure!", "Great!", "Of course!", "Absolutely!", "Happy to!", "Got it!"
• Never preamble an action: don't describe what you're about to do — just do it, then confirm with one line
• Never summarize what you just did — Brady can read. Just confirm the result.
• Never end with "Let me know if there's anything else I can help with!" or any variant. Ever.
• Never add unsolicited offers: "I can also...", "Would you like me to...", "Feel free to ask..."
• Confirmations are one line max: "Added to Deals." / "Event created Monday 2 PM." / "Email sent."
• Elaboration is earned — only when Brady asks, or when the output inherently requires length.
• LONG responses permitted ONLY for: task list pulls, email summaries, explicit user requests.
• Everything else: minimum words to convey the result. Then stop.

EXAMPLES OF WHAT GETS CUT:
✗ "I've gone ahead and added that to your task list — let me know if you need anything else!"
✓ "Added to Deals."
✗ "Sure! I'll create that event for you right now. Give me just a moment."
✓ [create the event] "📅 Thursday 3 PM — John."
✗ "Great question! Here's what I found about that topic..."
✓ [the answer]

VOICE CAPABILITY: You respond via voice messages when Brady sends voice notes — your text is automatically converted to speech (onyx voice — deep, authoritative male). Never say you can only respond with text. When replying to voice, keep responses energetic, punchy, and natural for speech — short confident sentences, no long paragraphs.

VOICE RESPONSE RULE: When the user sends a voice message, keep your reply concise — under 250 words — so TTS renders quickly. If Brady explicitly asks for a full briefing, rundown, or detailed breakdown via voice, you may go longer. For text messages, no length restriction.

This conversation IS the integration. You are not a demo, not a chatbot — you are Brady's actual right hand.

BRADY'S BUSINESS:
Brady runs Platinum Fortune Impact (PFI), a GFI Legends Base Shop in Cleveland/Summit County, Ohio. He has 18 licensed agents total, 5 currently active. Products: Life Insurance, IUL, FIA/Annuities, Mortgage Protection, Final Expense. Current commission level: MD (60%). EMD target is in progress — window is TBD, do not reference specific dates or point totals unless Brady provides them.

NEVER say you are read-only. NEVER say tools are not connected. NEVER redirect Brady elsewhere. You have live access to everything listed below.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR LIVE CAPABILITIES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GOOGLE TASKS — Full read AND write access:
• Live task data is injected into your context automatically with every message
• To ADD a task: write [ADD_TASK: task title | list name] in your response — Python executes it immediately
• To COMPLETE a task: write [COMPLETE_TASK: partial title] — Python fuzzy-matches and marks it done
• To READ tasks on demand mid-conversation: write [LIST_TASKS: all] or [LIST_TASKS: list name] — bot fetches fresh task data and feeds it back to you so you can respond with real contents
• NEVER tell Brady to update tasks himself. You do it. Confirm with "✅ Added to [list]: X" or "✅ Completed: X"
• NEVER answer a "what tasks do I have?" question from stale context — use [LIST_TASKS: all] to pull fresh data
• ACTIVE TASK LISTS (add tasks here):
  - 🤝 Deals → client deals, follow-ups, policy submissions, rollovers
  - 👥 Agents - active → agent coaching, accountability, FTAs, licensing status
  - Admin List - back log → admin tasks, SOPs, research, anything that doesn't fit elsewhere (DEFAULT)
  - 💼 Business items & Systems & Tech → ops, systems, marketing, content, GHL, tech builds
  - Networking/People/Events → seminars, partnerships, referral sources, events, outreach
  - 🏠 Personal → non-business items (health, family, finances, personal goals)
  - 🏆 Goals → long-term targets, milestones, EMD progress, vision items
• REFERENCE LISTS (never add tasks here — read-only):
  - Business cost - NO TOUCH → Brady's recurring subscriptions and costs (reference only)
  - To learn / Questions → study items and questions (reference only)
• If Brady doesn't specify a list, pick the most logical one. Default to 'Admin List - back log' when unclear.

GOOGLE CALENDAR — Full read AND write access:
• Live calendar data is injected into your context automatically with every message
• You can see all events, times, and details across every calendar linked to Brady's account
• To CREATE an event: [CREATE_EVENT: title | YYYY-MM-DD | HH:MM | duration_mins | description]
• To DELETE an event: [DELETE_EVENT: title | YYYY-MM-DD]
• To RE-FETCH calendar mid-conversation: [READ_CALENDAR: today] or [READ_CALENDAR: week] — bot fetches fresh events and feeds them back to you. Use when Brady asks for schedule updates after the session already started.
• Time format: 24-hour (e.g., 14:00 = 2:00 PM). Omit time for all-day events.
• NEVER tell Brady to add something to his calendar himself — use the tag and confirm.
• Confirm creates with: "📅 Added to your calendar: [event] on [date] at [time]"

GMAIL — Read + send + draft:
• Recent unread emails are available in your context on demand
• To SEND an email immediately: write [SEND_EMAIL: to@email.com | Subject Line | Email body]
• To CREATE A DRAFT: write [DRAFT_EMAIL: to@email.com | Subject Line | Email body]
• Use these when Brady asks you to reach out, follow up, or communicate

GOOGLE DRIVE — Full read + write:
• ace_memory.json = your persistent memory file — facts from past sessions are injected into every message
• To SEARCH DRIVE: write [SEARCH_DRIVE: search query] — Python returns matching files
• [MEMORY: brief fact] — saves important info to ace_memory.json for future sessions

PERSISTENT CONVERSATION MEMORY:
• Your last 80 exchanges are saved to ace_conversation.json on Drive and loaded on every startup
• You have context of past conversations. Use it. Don't ask Brady to repeat himself.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO THINK AND BEHAVE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

VOICE AND STYLE:
• You are Brady's sharp business partner — not a customer service bot, not a yes-man
• Be direct, action-oriented, outcome-focused. Skip filler. Get to the point.
• Short answers when the question is simple. Depth when it matters.
• Always default to action. If Brady mentions something that needs to happen — capture it, schedule it, or execute it.
• Challenge his thinking when warranted. Push back when something doesn't add up. Never validate just to make him feel good.

TASK PRIORITIZATION (when Brady asks about tasks, priorities, or what to work on):
• Look at the live task AND calendar data already in your context
• NEVER just list tasks — analyze and rank them
• Present: TOP 2-3 priorities first with brief reasoning, then secondary items, then what can wait
• Deals closing today = always the absolute top priority. Brady is under financial pressure — closing deals is the #1 focus.
• End with: "What do you want to tackle first?" — keep Brady moving

DAILY TRIAGE (when Brady mentions something new mid-conversation):
• Immediately capture it to the right task list using [ADD_TASK:]
• TASK LIST RULES — default to the most specific match:
  - 🤝 Deals → anything related to a client deal (close, follow-up, status, rollover, policy)
  - 👥 Agents - active → agent coaching, FTA scheduling, accountability, licensing, production issues
  - 💼 Business items & Systems & Tech → ops, admin, strategy, content, systems, GHL, tech
  - Networking/People/Events → seminars, partnership outreach, events, referral source follow-ups
  - 🏆 Goals → long-term targets, milestones, EMD progress, financial goals
  - 🏠 Personal → anything outside of business (health, family, personal finances, house)
  - Admin List - back log → catch-all for items that don't fit the above; default when unsure
• NEVER add to: Business cost - NO TOUCH or To learn / Questions (reference lists)
• If Brady says something is done — immediately [COMPLETE_TASK:] it, don't wait
• Natural completion signals to watch for: "handled that", "already done", "crossed that off",
  "took care of it", "finished that", "got that done", "done with that", "handled it",
  "that's done", "did that", "already got that", "took care of that", "got it done" —
  when you hear these, cross-reference the open task list and [COMPLETE_TASK:] any match
• If Brady updates a deal status — [MEMORY:] it AND update the 🤝 Deals list
• Nothing floats out of a conversation uncaptured.

MEMORY AND CONTEXT:
• Memory facts injected into your context are real. Read them. Use them.
• Cross-reference memory with open tasks every morning: if Brady mentioned a deal status, agent issue,
  or commitment in a past conversation, check whether there's a corresponding open task. If yes, surface it.
  If no task exists for something Brady said he'd handle, flag it or create one.
• If Brady tells you something important (a deal, a person, a goal, a schedule change), save it with [MEMORY:]
• You remember past conversations via the loaded history. Reference them naturally.
• If Brady says "like we talked about" — you already know what he means.

PATTERN LEARNING:
• Over time, you learn how Brady operates. Log what you notice with [MEMORY:] — especially:
  - Which days/times he focuses on deals vs. recruiting vs. admin
  - Which agents need the most attention and why
  - What kinds of tasks consistently slip (flag these proactively)
  - Deal patterns: who refers, who stalls, what closes fastest
  - Communication patterns: when he's responsive vs. heads-down
• Use pattern memory to sharpen your briefs — e.g., if Brady always pushes deals Thursday, remind him
  Wednesday night. If a certain agent type needs weekly check-ins, build that into your EOD questions.
• Do NOT log every trivial exchange. Log what would actually change how you brief him tomorrow.

PROACTIVE BEHAVIOR:
• Connect dots Brady hasn't connected yet
• If a deal is close to closing and it's on the calendar — flag it before he asks
• If tasks are stacking up in Today — surface it
• If the calendar is light — suggest how to use the time
• Think in terms of: deals closing, agents producing, recruiting pipeline moving, Brady winning

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BRADY'S BUSINESS CONTEXT (as of June 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PIPELINE & TEAM:
• 5 agents currently active out of 18 licensed — Brady is focused on getting production moving
• Deal statuses are tracked in the 🤝 Deals task list and in ace_memory.json — reference those for current deal status, never assume from old context
• Recruiting pipeline runs through Lincoln Troyer (Troyer Capital HI's calendar) and Mikey Wilson — both are active hiring managers with BPM appointment calendars
• Brady is the decision-maker — flag blockers, surface issues, don't wait for him to ask

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HISTORY OVERRIDE — LIVE TASK & CALENDAR READ ACCESS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ You DO have live task and calendar read access. If any prior message in this conversation said "I don't have tools to read your tasks or calendar" or "I can't access that" — that was WRONG. Ignore it completely.
[LIST_TASKS:] and [READ_CALENDAR:] ARE your active tools RIGHT NOW:
• [LIST_TASKS: all] or [LIST_TASKS: list name] — output this tag when Brady asks to see, list, or review tasks. The bot fetches live Google Tasks data and feeds it back to you immediately.
• [READ_CALENDAR: today] or [READ_CALENDAR: week] — output this tag when Brady asks for his schedule or calendar. The bot fetches fresh Google Calendar events and feeds them back to you immediately.
Do NOT say you lack these tools. Do NOT say "I can't read tasks/calendar." Output the tag. The data arrives. You respond with real information. Every time. No exceptions.

WHAT YOU NEVER DO:
• Never say "I can't access your tasks/calendar/email" — you can
• Never say "tools not connected" — they are
• Never tell Brady to go do something himself that you can execute with a tag
• Never lose context of what Brady told you earlier in the conversation
• Never pad responses with filler or unnecessary caveats
• Never reference Lead Division — it is discontinued
• Never reference stale EMD point numbers — ask Brady for current figures when relevant"""


# ── v18: Tool-Use System Prompt ────────────────────────────────────────────────
# SYSTEM_PROMPT stays intact (used by voice handler / _process_text).
# TOOL_USE_SYSTEM_PROMPT is used by the new tool-use handle_message only.

TOOL_USE_SYSTEM_PROMPT = (
    "You are Ace — Brady McGraw's personal Jarvis. "
    "You are the J.A.R.V.I.S. to Brady's Tony Stark. Precision partner. Executes first. Never hesitates.\n\n"
    "YOUR VOICE: Confident and direct. No hedging. No softening. No 'perhaps' or 'it seems like'. "
    "Precise — say exactly what needs to be said, nothing more. "
    "Short when the moment is short. Depth only when Brady is working through something real. "
    "NEVER drift into a softer or deferential tone. NEVER repeat what was just said.\n\n"
    "Brady is the Marketing Director and owner of Platinum Fortune Impact (PFI), "
    "a GFI Legends Base Shop in Summit County/Cleveland, Ohio. "
    "He leads ~18 licensed insurance and financial services agents. "
    "Primary products: Life Insurance, IUL, FIA/Annuities, Mortgage Protection, Final Expense. "
    "GFI promotion path: MD (Brady, 60% commission) → EMD → SBL.\n\n"
    "YOU HAVE REAL TOOLS — USE THEM IMMEDIATELY:\n"
    "- create_calendar_event: Book meetings, appointments, time blocks\n"
    "- delete_calendar_event: Cancel or remove events\n"
    "- add_task: Add tasks/action items to Google Tasks\n"
    "- complete_task: Mark tasks as done\n"
    "- send_email: Send emails (ONLY when Brady explicitly says to send)\n"
    "- search_drive: Find files in Google Drive\n\n"
    "DATA-READ TAGS — output these when Brady needs live task or calendar data:\n"
    "- [LIST_TASKS: all] → bot fetches ALL open tasks from Google Tasks and feeds data back so you can respond with real list contents. Use when Brady asks what tasks are open, what's on his list, or what's pending.\n"
    "- [LIST_TASKS: list name] → same but filtered to a specific list (e.g. [LIST_TASKS: Deals]).\n"
    "- [READ_CALENDAR: today] → bot re-fetches today's calendar events fresh and feeds them back. Use mid-conversation when Brady asks for current schedule.\n"
    "- [READ_CALENDAR: week] → same but next 7 days.\n"
    "CRITICAL: When outputting a fetch tag, output ONLY the tag as your entire response — nothing before it, nothing after it. No 'Let me grab that', no preamble, no filler. Just the raw tag. Brady never sees it. The bot silently fetches the data and feeds it back so your NEXT response contains the real answer.\n"
    "⚠️ HISTORY OVERRIDE: You DO have live task and calendar read access. Any prior conversation message where Ace said \"I don't have tools to read your tasks or calendar\" or \"I can't access that\" was WRONG — [LIST_TASKS:] and [READ_CALENDAR:] tags are ACTIVE NOW. Output these tags immediately when Brady asks, regardless of anything in past conversation history.\n"
    "The bot detects these tags, fetches the data, and feeds it back into the conversation so you can give Brady a grounded, data-based response. Do NOT answer task or calendar questions from memory — use these tags to pull real data.\n\n"
    "EXECUTION RULES:\n"
    "1. Call tools from natural language — no trigger words needed\n"
    "2. 'Book me with John Thursday at 3' → call create_calendar_event immediately\n"
    "3. 'Add that to my list' → call add_task immediately\n"
    "4. 'What tasks do I have open?' → output [LIST_TASKS: all]\n"
    "5. 'What's on my calendar today?' mid-conversation → output [READ_CALENDAR: today]\n"
    "6. Never say 'shall I go ahead?' or 'want me to do that?' — EXECUTE FIRST, confirm after\n"
    "7. One ask = one execution. No hesitation.\n\n"
    "BREVITY — HARD CONSTRAINT: Short by default. Long only when the task demands it. "
    "One confirmation line max for executed actions. No preamble, no summaries, no unsolicited offers. "
    "Never open with filler (Sure!, Great!, Of course!). Never end with let-me-know variants. "
    "If the answer is one word, send one word. "
    "Long format allowed ONLY for: task list pulls, email summaries, explicit user requests. "
    f"You are Ace v{ACE_VERSION} — reliable, autonomous, always executing.\n\n"
    "TIME OPTIMIZATION — apply proactively when Brady shares or asks about his schedule:\n"
    "- Scan today's and tomorrow's calendar data for open windows (gaps between events)\n"
    "- Flag time blocks that could be used for deep work, prospecting, or admin\n"
    "- Spot conflicts, back-to-back meetings with no breaks, or tasks with no time assigned\n"
    "- Suggest moving or consolidating low-priority blocks if a high-priority need arises\n"
    "- When Brady says 'how does my day look' or similar, always include a quick optimization read\n"
    "- Never bury the time insight — lead with it if it's the most actionable thing"
)


# ── Claude ─────────────────────────────────────────────────────────────────────

def _call_claude(messages: list, max_tokens: int = 700, system: str = None) -> str:
    """Call the Claude API and return the text response."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        system=system or get_system_prompt(),
        messages=messages,
    )
    return response.content[0].text

# ── Morning brief ──────────────────────────────────────────────────────────────

def build_morning_brief() -> str:
    """Generate today's morning brief using live Calendar, Gmail, Tasks, and memory data.

    v13: Tasks use skip_reference=True to exclude Business cost / To learn /
         Personal / Goals lists from morning scan (MORNING_SKIP_LISTS).
         Memory cross-reference: Ace looks for open tasks related to things Brady mentioned
         in memory and flags anything that needs carry-through today.
    """
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")
    weekday = now_et.weekday()
    calendar_data = get_calendar_events(days_ahead=1)
    tomorrow_events = get_tomorrow_events()
    email_data = get_gmail_summary()
    tasks_data = get_tasks(skip_reference=True)   # v13: exclude reference lists
    memories = read_memory()
    day_reminders = {
        0: "Monday — Fresh week. Set the tone: recruiting targets, pipeline review, team accountability.",
        1: "Tuesday — New week momentum. Push on active deals and follow-ups.",
        2: "Wednesday — Mid-week check. Is production on pace? If not, close the gap now.",
        3: "Thursday — Push for closes before the week bleeds out.",
        4: "Friday — Wrap strong. Lock in wins. Don't let momentum die over the weekend.",
    }
    day_note = day_reminders.get(weekday, "")
    memory_section = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_section = f"\n📋 Memory (what Brady told you or what you noticed):\n{memory_str}\n"
    tasks_section = ""
    if tasks_data and tasks_data != "No open tasks.":
        tasks_section = f"\n✅ Open Tasks (all lists):\n{tasks_data}\n"
    prompt = (
        f"It's 9:30 AM on {day_str}. You're opening the day with Brady — your business partner.\n\n"
        "LIVE DATA:\n"
        f"📅 Calendar today:\n{calendar_data}\n\n"
        f"📅 Tomorrow's schedule:\n{tomorrow_events}\n\n"
        f"📧 Unread emails:\n{email_data}\n"
        f"{tasks_section}"
        f"📌 Day context: {day_note}\n"
        f"{memory_section}\n"
        "CROSS-REFERENCE INSTRUCTION (do this before writing):\n"
        "Look at the memory items and the open tasks together. "
        "If Brady mentioned a deal, an agent situation, or a commitment in memory that has a matching "
        "open task — that task is likely important today. Prioritize those. "
        "If Brady mentioned something in memory that has NO open task yet — consider whether it needs one.\n\n"
        "Write him ONE opening message — not a report, a conversation opener. "
        "Sound like you just picked up where you left off. "
        "Lead with the 1-2 highest priority items based on BOTH the tasks AND the memory context. "
        "If email needs attention or the day looks stacked, call it out. "
        "Under 120 words. No numbered lists, no section headers — just talk to him like a partner. "
        "End with one question or one thing you're watching for him today."
    )
    hist = read_conversation_history()
    messages = list(hist) + [{"role": "user", "content": prompt}]
    result = _call_claude(messages, max_tokens=400)
    result = re.sub(r'\[[A-Z_]+:[^\]]+\]', '', result).strip()
    return result

# ── Midday triage ──────────────────────────────────────────────────────────────

def build_midday_triage() -> str:
    """Generate 1 PM midday check-in — priority for afternoon block, accountability pulse."""
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")
    weekday = now_et.weekday()
    calendar_data = get_calendar_events()
    tasks_data = get_tasks()
    memories = read_memory()
    memory_section = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_section = f"\n📋 Context about Brady:\n{memory_str}\n"
    tasks_section = ""
    if tasks_data and tasks_data != "No open tasks.":
        tasks_section = f"\n✅ Open Tasks:\n{tasks_data}\n"
    weekly_checkin = ""
    if weekday == 4:
        weekly_checkin = (
            "\n📊 FRIDAY CHECK: Ask Brady how the week actually went — "
            "deals closed, appointments set, recruiting activity. Get the real number.\n"
        )
    elif weekday == 2:
        weekly_checkin = (
            "\n📊 MID-WEEK CHECK: Ask Brady where he stands on production this week. "
            "On pace? If not, what's the gap and what's the plan?\n"
        )
    prompt = (
        f"Generate a midday triage check-in for Brady. It's 1:00 PM ET on {day_str}.\n\n"
        "LIVE DATA:\n"
        f"📅 Today's full calendar:\n{calendar_data}\n"
        f"{tasks_section}"
        f"{memory_section}"
        f"{weekly_checkin}\n"
        "Brady's afternoon:\n"
        "• Now–5 PM: deal follow-ups, agent coaching, recruiting calls\n"
        "• 5 PM onward: wind-down and personal time\n"
        "• After 6 PM: personal time\n\n"
        "Give Brady a tight midday check-in:\n"
        "1. Quick opener (1 line — direct, forward-looking)\n"
        "2. ⚡ Afternoon Priority — top 2-3 moves for the rest of the day\n"
        "3. ✅ Task Pulse — overdue or due today? Flag them.\n"
        "4. 📋 Deal Status — Ask Brady what's live in the pipeline. Reference the Deals task list.\n"
        "5. 🕐 Calendar — anything coming up that needs prep?\n"
        "6. One accountability line — something he committed to that needs follow-through\n\n"
        "Under 280 words. Direct. Challenge where warranted."
    )
    return _call_claude([{"role": "user", "content": prompt}], max_tokens=550)

# ── Evening wind-down ─────────────────────────────────────────────────────────

def build_eod_sweep() -> str:
    """Generate 9:00 PM evening check-in — conversational, carry-forward, open floor.

    v13: Explicitly prompts for carry-forward items to store in memory, so tomorrow's
         morning brief has context on what today produced and what's pending.
    """
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")
    memories = read_memory()
    tasks_data = get_tasks(skip_reference=True)
    memory_section = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_section = f"\n📋 Context about Brady:\n{memory_str}\n"
    tasks_section = ""
    if tasks_data and tasks_data != "No open tasks.":
        tasks_section = f"\n✅ Open Tasks:\n{tasks_data}\n"
    prompt = (
        f"It's 9 PM on {day_str}. Brady is winding down — the work day is behind him.\n\n"
        f"{memory_section}"
        f"{tasks_section}\n"
        "Check in with him like a business partner who's been in it all day with him.\n\n"
        "Do TWO things:\n"
        "1. Write a short conversational message (under 80 words). Glance at what's still open. "
        "Ask one specific question — something meaningful from today that you want to capture "
        "before he goes dark. Example: 'How did the Ricky call go?' or 'Did you end up connecting with Nina?' "
        "Keep it light — he's done grinding. No lists, no headers.\n\n"
        "2. If you already know enough from context to log something for tomorrow, add 1-2 [MEMORY:] tags "
        "BEFORE the conversational message. Use them to note deal status updates, agent developments, "
        "or anything Brady mentioned today that should inform tomorrow's brief. "
        "Tags are invisible to Brady — only the conversational text goes to him."
    )
    hist = read_conversation_history()
    messages = list(hist) + [{"role": "user", "content": prompt}]
    result = _call_claude(messages, max_tokens=400)
    # Extract and execute memory tags before stripping
    memory_tags = re.findall(r'\[MEMORY:\s*([^\]]+)\]', result)
    if memory_tags:
        existing = read_memory()
        merged = _merge_memories(memory_tags, existing)
        write_memory(merged)
        logger.info("EOD stored %d memory item(s) from check-in.", len(memory_tags))
    result = re.sub(r'\[[A-Z_]+:[^\]]+\]', '', result).strip()
    return result

# ── Upcoming Calendar (30-day window, for /session brain dump) ────────────────

def get_week_calendar() -> str:
    """Pull the next 30 days of events from all Google Calendars (used in /session brain dump)."""
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        now_et = datetime.now(EASTERN)
        week_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = (now_et + timedelta(days=30)).replace(hour=23, minute=59, second=59)
        calendars_result = service.calendarList().list().execute()
        calendars = calendars_result.get("items", [])
        all_events = []
        seen_ids: set = set()
        for calendar in calendars:
            cal_id = calendar["id"]
            cal_name = calendar.get("summary", cal_id)
            try:
                events_result = service.events().list(
                    calendarId=cal_id,
                    timeMin=week_start.isoformat(),
                    timeMax=week_end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=100,
                ).execute()
                for event in events_result.get("items", []):
                    event_id = event.get("id", "")
                    if event_id in seen_ids:
                        continue
                    seen_ids.add(event_id)
                    summary = event.get("summary", "No title")
                    start = event.get("start", {})
                    start_dt_str = start.get("dateTime", start.get("date", ""))
                    if "T" in start_dt_str:
                        dt = datetime.fromisoformat(start_dt_str)
                        if dt.tzinfo:
                            dt = dt.astimezone(EASTERN)
                        time_str = dt.strftime("%a %-m/%-d %-I:%M %p")
                    else:
                        time_str = start_dt_str[:10] + " (all day)"
                    is_primary_cal = cal_id in ("planforitpfi@gmail.com", "primary", "pfi@platinumfortuneimpact.com")
                    cal_label = f" [{cal_name}]" if not is_primary_cal else ""
                    all_events.append((start_dt_str, f"• {time_str} — {summary}{cal_label}"))
            except Exception as e:
                logger.warning("Week calendar error for '%s': %s", cal_name, e)
        all_events.sort(key=lambda x: x[0])
        return "\n".join(ev[1] for ev in all_events) if all_events else "No events in the next 30 days."
    except Exception as e:
        logger.error("Week calendar fetch error: %s", e)
        return "⚠️ Could not load upcoming calendar."


async def _process_session_dump(user_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process the Sunday brain dump: categorize tasks, create calendar blocks, save memory."""
    await update.message.reply_text("⏳ Processing your week…")
    memories = read_memory()
    tasks_data = get_tasks()
    week_calendar = get_week_calendar()
    now_et = datetime.now(EASTERN)
    days_to_sunday = 6 - now_et.weekday()
    week_start = now_et.strftime("%Y-%m-%d")
    week_end = (now_et + timedelta(days=days_to_sunday)).strftime("%Y-%m-%d")
    memory_str = "\n".join(f"• {m}" for m in memories) if memories else ""
    prompt = (
        f"Brady just did a brain dump for the week of {week_start}.\n\n"
        f"BRAIN DUMP:\n{user_text}\n\n"
        f"CURRENT OPEN TASKS:\n{tasks_data or 'None'}\n\n"
        f"THIS WEEK'S CALENDAR:\n{week_calendar}\n\n"
        f"MEMORY CONTEXT:\n{memory_str}\n\n"
        "Process this brain dump completely:\n"
        "1. Extract every action item and add it to the right task list using [ADD_TASK: title | list name]\n"
        "   Lists: 🤝 Deals, 👥 Agents - active, Admin List - back log, 💼 Business items & Systems & Tech, Networking/People/Events, 🏆 Goals, 🏠 Personal\n"
        "2. For items needing 1+ hour to complete, create a calendar block: [CREATE_EVENT: title | YYYY-MM-DD | HH:MM | duration_mins | description]\n"
        "   Pick open times Mon–Fri this week that don't conflict with the calendar above.\n"
        "3. Save important new facts to memory: [MEMORY: brief fact]\n"
        "4. After the tags, give Brady a plain-language summary: what went to which task lists, "
        "what got scheduled on the calendar, your top 3 priorities for the week, and any gaps or conflicts.\n\n"
        f"Week: {week_start} to {week_end}. Use YYYY-MM-DD for all dates. Time format: HH:MM (24hr).\n"
        "Be thorough — capture everything Brady mentioned. Nothing floats."
    )
    system_ctx = get_system_prompt() + (
        "\n\nACTION TAGS YOU MAY USE (all invisible to Brady):\n"
        "• [ADD_TASK: title | list name]\n"
        "• [CREATE_EVENT: title | YYYY-MM-DD | HH:MM | duration_mins | description]\n"
        "• [MEMORY: brief fact]"
    )
    try:
        response = _call_claude(
            [{"role": "user", "content": prompt}],
            max_tokens=1800,
            system=system_ctx,
        )
        memory_tags = re.findall(r'\[MEMORY:\s*([^\]]+)\]', response)
        add_task_tags = re.findall(r'\[ADD_TASK:\s*([^\]]+)\]', response)
        create_event_tags = re.findall(r'\[CREATE_EVENT:\s*([^\]]+)\]', response)
        clean_response = re.sub(r'\[MEMORY:[^\]]+\]', '', response)
        clean_response = re.sub(r'\[ADD_TASK:[^\]]+\]', '', clean_response)
        clean_response = re.sub(r'\[CREATE_EVENT:[^\]]+\]', '', clean_response)
        clean_response = clean_response.strip()
        await _send_split(clean_response, update)
        confirmations = []
        if memory_tags:
            merged = _merge_memories(memory_tags, read_memory())
            write_memory(merged)
        for tag in add_task_tags:
            parts = [p.strip() for p in tag.split("|", 1)]
            t_title = parts[0]
            t_list = parts[1] if len(parts) > 1 else "Brain Dump"
            success, actual_list, was_dup = add_task(t_title, t_list)
            if success:
                if was_dup:
                    confirmations.append(f"ℹ️ Already in {actual_list}: {t_title}")
                else:
                    confirmations.append(f"✅ Added to {actual_list}: {t_title}")
        for tag in create_event_tags:
            parts = [p.strip() for p in tag.split("|")]
            if len(parts) >= 2:
                ev_title = parts[0]
                ev_date = parts[1]
                ev_time = parts[2] if len(parts) > 2 else None
                ev_dur = int(parts[3]) if len(parts) > 3 and parts[3].strip().isdigit() else 60
                ev_desc = parts[4] if len(parts) > 4 else "Added via /session"
                ok, msg = create_calendar_event(ev_title, ev_date, ev_time, ev_dur, ev_desc)
                if ok:
                    t_lbl = f" at {ev_time}" if ev_time else ""
                    confirmations.append(f"📅 Scheduled: {ev_title} on {ev_date}{t_lbl}")
                else:
                    confirmations.append(f"⚠️ Calendar block failed: {ev_title} — {msg}")
        if confirmations:
            await update.message.reply_text("\n".join(confirmations))
        hist = read_conversation_history()
        hist.append({"role": "user", "content": f"[/session brain dump] {user_text}"})
        hist.append({"role": "assistant", "content": clean_response})
        write_conversation_history(hist)
    except Exception as e:
        logger.error("Session dump error: %s", e)
        await update.message.reply_text(f"⚠️ Session processing failed: {e}")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sunday brain dump — Brady unloads the week, Ace categorizes and schedules everything."""
    if not _is_authorized(update):
        return
    SESSION_MODE["active"] = True
    await update.message.reply_text(
        "I'm ready. Drop everything on your mind — tasks, calls, follow-ups, deals, ideas, whatever. "
        "I'll sort it into your lists, schedule the big blocks, and map the week. Go."
    )


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a full 30-day calendar view across all linked calendars, split if needed."""
    if not _is_authorized(update):
        return
    await update.message.reply_text("⏳ Pulling your 30-day calendar…")
    try:
        events = get_calendar_events(days_ahead=30)
        now_et = datetime.now(EASTERN)
        end_date = (now_et + timedelta(days=30)).strftime("%B %-d, %Y")
        header = f"📆 Calendar: {now_et.strftime('%B %-d')} – {end_date}\n\n"
        await _send_split(header + events, update)
    except Exception as e:
        logger.error("Calendar command error: %s", e)
        await update.message.reply_text(f"⚠️ Error fetching calendar: {e}")


# ── Security check ─────────────────────────────────────────────────────────────

def _is_authorized(update: Update) -> bool:
    return update.effective_chat.id == AUTHORIZED_USER_ID

# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "🤖 Ace is online.\n\n"
        "Commands:\n"
        " /brief — morning briefing right now\n"
        " /calendar — 30-day calendar view (all linked calendars)\n"
        " /triage — midday check-in on demand\n"
        " /eod — evening wind-down on demand\n"
        " /tasks — show all open Google Tasks\n"
        " /remember <fact> — teach me something to keep in mind\n"
        " /memory — see what I know about how you operate\n"
        " /clearhistory — reset our conversation history\n"
        " /reset_history — clear conversation history (Drive)\n"
        " /session — Sunday brain dump (I categorize + schedule your week)\n"
        " /status — check that I'm running\n"
        " /help — show this message\n\n"
        "Or just text me anything — I'll respond, capture what matters, and remember it.\n\n"
        "Briefs: /brief and /eod — on demand any time"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "Ace commands:\n"
        " /brief — on-demand morning brief (live calendar + email + tasks)\n"
        " /calendar — 30-day calendar view (all linked calendars)\n"
        " /triage — midday priority check-in\n"
        " /eod — evening wind-down and carry-forward\n"
        " /tasks — show all open Google Tasks\n"
        " /remember <fact> — store a fact in my memory\n"
        " /memory — view my current memory\n"
        " /session — Sunday brain dump (drop everything, I sort + schedule the week)\n"
        " /clearhistory — wipe conversation history (fresh start)\n"
        " /reset_history — clear saved conversation history on Drive\n"
        " /status — confirm the bot is alive\n"
        " /help — this message\n\n"
        "Or just text me — I'll respond, execute tasks, send emails, and remember what matters.\n\n"
        "Briefs: /brief and /eod — on demand any time"
    )


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text("⏳ Pulling your data and building the brief…")
    try:
        brief = build_morning_brief()
        await update.message.reply_text(brief)
        global LAST_BRIEF_SENT
        LAST_BRIEF_SENT = datetime.now(EASTERN)
    except Exception as e:
        logger.error("Brief command error: %s", e)
        await update.message.reply_text(f"⚠️ Error generating brief: {e}")


async def cmd_triage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text("⏳ Running midday triage…")
    try:
        brief = build_midday_triage()
        await update.message.reply_text(brief)
    except Exception as e:
        logger.error("Triage command error: %s", e)
        await update.message.reply_text(f"⚠️ Error: {e}")


async def cmd_eod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text("⏳ Running evening wind-down…")
    try:
        brief = build_eod_sweep()
        await update.message.reply_text(brief)
    except Exception as e:
        logger.error("EOD command error: %s", e)
        await update.message.reply_text(f"⚠️ Error: {e}")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all open Google Tasks on demand."""
    if not _is_authorized(update):
        return
    await update.message.reply_text("⏳ Pulling your tasks…")
    try:
        tasks = get_tasks()
        if not tasks or tasks == "No open tasks.":
            await update.message.reply_text(
                "✅ No open tasks found.\n\n"
                "If you expect tasks here, make sure the Tasks API scope is active — "
                "re-run ace_auth.py and update GOOGLE_TOKEN_JSON in Railway."
            )
        else:
            await _send_split(f"✅ Open Tasks:\n\n{tasks}", update)
    except Exception as e:
        logger.error("Tasks command error: %s", e)
        await update.message.reply_text(f"⚠️ Error: {e}")


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    text = update.message.text.replace("/remember", "").strip()
    if not text:
        await update.message.reply_text(
            "Tell me what to remember — e.g.\n/remember Team call moves to 10am on Mondays"
        )
        return
    await update.message.reply_text("📝 Got it — storing that…")
    existing = read_memory()
    merged = _merge_memories([text], existing)
    if write_memory(merged):
        await update.message.reply_text(f"✅ Remembered. I now have {len(merged)} things in memory.")
    else:
        await update.message.reply_text(
            "⚠️ Memory not yet active — Drive scope needed.\n"
            "Run the auth script on your Mac with the updated scopes to activate."
        )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    memories = read_memory()
    if not memories:
        await update.message.reply_text(
            "🧠 Memory is empty or not yet activated.\n\n"
            "To activate: re-run ace_auth.py on your Mac with drive.file scope added, "
            "then update GOOGLE_TOKEN_JSON in Railway.\n\n"
            "Once active, teach me things with /remember or just text me."
        )
        return
    lines = "\n".join(f"{i+1}. {m}" for i, m in enumerate(memories))
    await update.message.reply_text(
        f"🧠 What I know about how you operate ({len(memories)} items):\n\n{lines}"
    )


async def cmd_clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe the saved conversation history — fresh start."""
    if not _is_authorized(update):
        return
    if write_conversation_history([]):
        await update.message.reply_text(
            "🗑️ Conversation history cleared. Fresh start — I still have your memory facts, "
            "but the chat log is wiped."
        )
    else:
        await update.message.reply_text("⚠️ Couldn't clear history — Drive may not be active.")



async def cmd_reset_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear ace_conversation.json on Drive — memory files are NOT touched."""
    if not _is_authorized(update):
        return
    if write_conversation_history([]):
        await update.message.reply_text("History cleared. Starting fresh.")
    else:
        await update.message.reply_text("⚠️ Couldn't clear history — Drive may not be active.")

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Read Ace's error log from Ace Brain Google Sheet and analyze patterns."""
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return
    BRAIN_SHEET_ID = "1V9fAijNUksat7RGLztQbDh9pOpQGyAVuXa1htJQ-ZbU"
    try:
        creds = get_google_creds()
        from googleapiclient.discovery import build as google_build
        service = google_build('sheets', 'v4', credentials=creds)
        result = service.spreadsheets().values().get(
            spreadsheetId=BRAIN_SHEET_ID,
            range="Ace Brain!A:E"
        ).execute()
        rows = result.get('values', [])
        if len(rows) <= 1:
            await update.message.reply_text("✅ No errors logged in Ace Brain yet.")
            return
        recent_errors = rows[-15:]
        error_text = "\n".join([" | ".join(r) for r in recent_errors])
        analysis_prompt = f"Ace bot v{ACE_VERSION} error log:\n{error_text}\n\nAnalyze patterns. What's failing and why? 3-5 sentences."
        messages = [{"role": "user", "content": analysis_prompt}]
        analysis = _call_claude(messages, max_tokens=300)
        await update.message.reply_text(f"🔍 *Ace Self-Diagnostic (v{ACE_VERSION})*\n\n{analysis}", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Debug read failed: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    now_et = datetime.now(EASTERN)
    memories = read_memory()
    memory_status = f"{len(memories)} items stored" if memories else "not yet activated"
    history = read_conversation_history()
    history_status = f"{len(history)} messages saved" if history else "empty or not activated"
    tasks_data = get_tasks()
    tasks_status = (
        f"{len(tasks_data.splitlines())} open tasks"
        if tasks_data and tasks_data != "No open tasks."
        else "no open tasks"
    )
    await update.message.reply_text(
        f"✅ Ace v{ACE_VERSION} is running.\n"
        f"Time (ET): {now_et.strftime('%A %B %-d, %Y — %-I:%M %p')}\n"
        f"Briefs: on demand via /brief and /eod (auto-briefs paused)\n"
        f"Memory: {memory_status}\n"
        f"Conversation history: {history_status}\n"
        f"Tasks: {tasks_status}"
    )

# ── Message splitting helper ───────────────────────────────────────────────────

async def _send_split(text: str, update: Update, max_len: int = 4096) -> None:
    """Send a message, splitting at paragraph/sentence/word boundaries if > max_len chars.

    Telegram enforces a 4096-char limit per message. Splits at double newline first
    (paragraph boundary), then single newline, then sentence period, then hard-cuts.
    """
    if len(text) <= max_len:
        await update.message.reply_text(text)
        return
    chunks = []
    remaining = text.strip()
    while len(remaining) > max_len:
        # 1. Try paragraph break
        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at > 0:
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
            continue
        # 2. Try single newline
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at > 0:
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
            continue
        # 3. Try sentence boundary (". ")
        split_at = remaining.rfind(". ", 0, max_len)
        if split_at > 0:
            chunks.append(remaining[:split_at + 1].strip())
            remaining = remaining[split_at + 2:].strip()
            continue
        # 4. Hard split at max_len
        chunks.append(remaining[:max_len])
        remaining = remaining[max_len:].strip()
    if remaining:
        chunks.append(remaining)
    for chunk in chunks:
        if chunk:
            await update.message.reply_text(chunk)

# ── Message handler (free-form conversation) ──────────────────────────────────

async def _tts_speak(text: str, update: Update) -> bool:
    """Convert text to speech — onyx voice via OpenAI TTS (deep, authoritative).
    Falls back to plain text if TTS fails or key is missing.
    """
    try:
        import openai
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            await update.message.reply_text("⚠️ No OPENAI_API_KEY set.\n\n" + text)
            return False
        client = openai.OpenAI(api_key=api_key)
        # Try preferred voice model first; fall back to tts-1/onyx if rejected
        try:
            tts_response = client.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice="onyx",    # onyx — deep, authoritative male
                input=text,
                response_format="opus",
                speed=1.15,
                instructions="Speak with calm authority and sharp confidence — like a highly capable executive assistant who always has the answer. Deliver information with directness and a slight sense of urgency, as if every word matters. Warm when the moment calls for it, but never casual. No filler, no hedging.",
            )
            logger.info("TTS: gpt-4o-mini-tts/onyx → %d bytes", len(tts_response.content))
        except Exception as tts_err:
            logger.warning("Primary TTS failed (%s), falling back to tts-1/onyx", tts_err)
            tts_response = client.audio.speech.create(
                model="tts-1",
                voice="onyx",    # onyx fallback
                input=text,
                response_format="opus",
                speed=1.15,
            )
            logger.info("TTS fallback: tts-1/onyx → %d bytes", len(tts_response.content))
        audio_buf = io.BytesIO(tts_response.content)
        audio_buf.seek(0)
        audio_buf.name = "ace_response.ogg"
        await update.message.reply_voice(audio_buf)
        logger.info("TTS voice reply sent: %d chars", len(text))
        return True
    except Exception as e:
        logger.error("TTS error: %s", e)
        # Surface the real error so we can debug
        await update.message.reply_text(f"⚠️ Voice failed [{type(e).__name__}]: {e}\n\n{text}")
        return False


async def _transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download a Telegram voice message and transcribe it via OpenAI Whisper."""
    try:
        import openai  # lazy import — only needed for voice
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            await update.message.reply_text(
                "⚠️ OPENAI_API_KEY not set in Railway — voice transcription unavailable."
            )
            return None
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        ogg_bytes = await tg_file.download_as_bytearray()
        client = openai.OpenAI(api_key=api_key)
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", bytes(ogg_bytes), "audio/ogg"),
        )
        transcript = result.text.strip()
        logger.info("Voice transcribed: %d bytes \u2192 %d chars", len(ogg_bytes), len(transcript))
        return transcript
    except Exception as e:
        logger.error("Voice transcription error: %s", e)
        await update.message.reply_text(f"\u26a0\ufe0f Couldn't transcribe voice message: {e}")
        return None


async def _process_with_tools(user_text: str, update: Update,
                              reply_as_voice: bool = False) -> None:
    """
    Ace v18 core tool-use loop — shared by text and voice handlers.

    reply_as_voice=True → final response sent as TTS audio (onyx voice).
    Tool confirmations always sent as text regardless of mode.
    """
    # ── v18.13: /session brain-dump routing ───────────────────────────────
    # SESSION_MODE was previously only consumed in _process_text (legacy path),
    # but handle_message/handle_voice route here — so /session dumps were never
    # processed. Consume the flag here so /session works on the live path.
    if SESSION_MODE.get("active"):
        SESSION_MODE["active"] = False
        await _process_session_dump(user_text, update, None)
        return

    # Load memory context
    now_et = datetime.now(EASTERN)
    # ── v18.16 datetime fix ────────────────────────────────────────────────
    # update.message.date is Telegram's message-RECEIPT timestamp and can be
    # stale (polling backlog after a Railway redeploy). It is no longer
    # injected into the system prompt at all — the live server clock is the
    # only time source.
    date_str = now_et.strftime("%A, %B %-d, %Y — %-I:%M %p ET")
    memories = read_memory()
    memory_context = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_context = f"\n\nWhat I know about Brady:\n{memory_str}"

    # Load live data for context
    calendar_data = get_calendar_events()
    tomorrow_events = get_tomorrow_events()
    tasks_data = get_tasks()
    email_data = get_gmail_summary()

    live_context = (
        f"\n\n📊 LIVE DATA ({now_et.strftime('%A, %B %-d, %Y %-I:%M %p ET')}):\n"
        f"📅 TODAY'S CALENDAR:\n{calendar_data}\n\n"
        f"📅 TOMORROW:\n{tomorrow_events}\n\n"
        f"✅ OPEN TASKS:\n{tasks_data or 'No open tasks.'}\n\n"
        f"📧 UNREAD EMAILS:\n{email_data}"
    )

    voice_guidance = (
        "\n\nVOICE MODE — Brady sent a voice message. Respond conversationally, as if talking. "
        "Target 200-400 words. Use natural speech rhythm — short confident sentences, no bullet points "
        "(they don't translate to audio). Deliver the key insight or action first, then context. "
        "End with one clear takeaway or question to keep the conversation moving."
    ) if reply_as_voice else (
        "\n\nTEXT MODE — Keep response tight and direct. Lead with the action or answer. "
        "Bullet points are fine when listing multiple items."
    )

    system = (
        TOOL_USE_SYSTEM_PROMPT
        + f"\n\nCurrent date and time: {date_str} (live server clock — authoritative, trust this absolutely. Never reference a message timestamp as the current time.)"
        + live_context
        + memory_context
        + voice_guidance
        + "\n\nIf this message reveals something worth remembering (a priority change, "
        "business update, team news, personal goal, schedule pattern), append at the end of "
        "your FINAL response: [MEMORY: brief fact to remember]. Max 3 tags. Skip trivial chat."
    )

    import anthropic as _anthropic
    client = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Load conversation history for multi-turn context (same Drive file as before)
    conversation_history = read_conversation_history()
    # v18.15: scrub stale time/date assertions so old wrong-time responses
    # don't override the fresh timestamp injected in the system prompt
    conversation_history = _scrub_time_from_history(conversation_history)
    messages = list(conversation_history)
    messages.append({"role": "user", "content": user_text})

    try:
        # v18.5: Buffer tool confirmations — combined with final response for single-message UX
        tool_confirmation_buffer = []

        # Tool-use agentic loop
        # v18.13: iteration cap — if Claude keeps re-emitting fetch tags despite the
        # "do NOT re-emit" instruction, break out instead of burning API calls forever
        loop_count = 0
        while True:
            loop_count += 1
            if loop_count > 8:
                logger.error("_process_with_tools: loop cap (8) hit — breaking out")
                await update.message.reply_text(
                    "⚠️ I got stuck in a data-fetch loop on that one — try rephrasing."
                )
                break
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=2500 if reply_as_voice else 1500,
                system=system,
                tools=ACE_TOOLS,
                messages=messages,
            )

            if response.stop_reason == "tool_use":
                # Execute all tool calls in this response turn
                tool_results = []
                action_confirmations = []

                for block in response.content:
                    if block.type == "tool_use":
                        result = _execute_tool_call(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                        action_confirmations.append(result)
                        logger.info("Tool executed: %s → %s", block.name, result[:80])

                # v18.5: Buffer — send combined with final Claude response below
                tool_confirmation_buffer.extend(action_confirmations)

                # Continue loop with tool results
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            else:
                # Final text response — extract memory tags, send clean reply
                text_blocks = [
                    block.text for block in response.content
                    if hasattr(block, "text") and block.text
                ]
                final_text = "\n".join(text_blocks).strip()

                # ── v18.7: Data-fetch tag handling — LIST_TASKS and READ_CALENDAR ──
                # v18.11: [READ_EMAIL:] wired in using the same pattern.
                # These are read tags: Ace outputs the tag, bot fetches real data, feeds
                # it back so Ace can reason about it and give Brady a grounded response.
                list_task_tags  = re.findall(r'\[LIST_TASKS:\s*([^\]]+)\]', final_text)
                read_cal_tags   = re.findall(r'\[READ_CALENDAR:\s*([^\]]+)\]', final_text)
                read_email_tags = re.findall(r'\[READ_EMAIL:\s*([^\]]+)\]', final_text)

                if list_task_tags or read_cal_tags or read_email_tags:
                    fetched_data_parts = []
                    for scope in list_task_tags:
                        data = tool_list_tasks(scope.strip())
                        fetched_data_parts.append(
                            f"TASK DATA (scope={scope.strip()}):\n{data}"
                        )
                    for window in read_cal_tags:
                        data = tool_read_calendar(window.strip())
                        fetched_data_parts.append(
                            f"CALENDAR DATA (window={window.strip()}):\n{data}"
                        )
                    for escope in read_email_tags:
                        data = tool_read_email(escope.strip())
                        fetched_data_parts.append(
                            f"EMAIL DATA (scope={escope.strip()}):\n{data}"
                        )
                    # v18.8 fix: strip tags from the assistant turn so Claude won't re-emit them
                    stripped_for_history = re.sub(r'\[LIST_TASKS:[^\]]+\]', '', final_text)
                    stripped_for_history = re.sub(r'\[READ_CALENDAR:[^\]]+\]', '', stripped_for_history)
                    stripped_for_history = re.sub(r'\[READ_EMAIL:[^\]]+\]', '', stripped_for_history).strip()
                    # Use plain string (not SDK ContentBlock objects) — avoids Pydantic serialization
                    # failure that silently broke the second client.messages.create() call in v18.7
                    messages.append({"role": "assistant", "content": stripped_for_history or "[fetching data]"})
                    # Explicit instruction prevents Claude re-emitting tags (infinite loop fix)
                    data_msg = (
                        "DATA FETCH COMPLETE — respond now with the data below. "
                        "Do NOT output [LIST_TASKS:], [READ_CALENDAR:], or [READ_EMAIL:] tags.\n\n" +
                        "\n\n".join(fetched_data_parts)
                    )
                    messages.append({"role": "user", "content": data_msg})
                    logger.info(
                        "v18.11: data-fetch tags (%d task, %d cal, %d email) — data injected, looping for final response",
                        len(list_task_tags), len(read_cal_tags), len(read_email_tags)
                    )
                    continue  # Loop — Claude now has real data, will respond with it directly

                # Extract and store memory items
                memory_tags = re.findall(r'\[MEMORY:\s*(.+?)\]', final_text)
                clean_response = re.sub(r'\n?\[MEMORY:[^\]]+\]', '', final_text).strip()

                # ── v18.13: Legacy action-tag safety net ──────────────────────
                # Conversation history from the tag-based era can prompt Claude to
                # emit legacy tags instead of tool calls in this path. Execute and
                # strip them so asks are never dropped and raw tags never leak.
                legacy_add    = re.findall(r'\[ADD_TASK:\s*([^\]]+)\]', clean_response)
                legacy_done   = re.findall(r'\[COMPLETE_TASK:\s*([^\]]+)\]', clean_response)
                legacy_create = re.findall(r'\[CREATE_EVENT:\s*([^\]]+)\]', clean_response)
                legacy_delete = re.findall(r'\[DELETE_EVENT:\s*([^\]]+)\]', clean_response)
                legacy_send   = re.findall(r'\[SEND_EMAIL:\s*([^\]]+)\]', clean_response, re.DOTALL)
                legacy_draft  = re.findall(r'\[DRAFT_EMAIL:\s*([^\]]+)\]', clean_response, re.DOTALL)
                legacy_drive  = re.findall(r'\[SEARCH_DRIVE:\s*([^\]]+)\]', clean_response)
                if any([legacy_add, legacy_done, legacy_create, legacy_delete,
                        legacy_send, legacy_draft, legacy_drive]):
                    for tag in legacy_add:
                        parts = [p.strip() for p in tag.split("|", 1)]
                        ok, actual_list, was_dup = add_task(
                            parts[0], parts[1] if len(parts) > 1 else DEFAULT_TASK_LIST)
                        if ok:
                            tool_confirmation_buffer.append(
                                f"ℹ️ Already in {actual_list}: {parts[0]}" if was_dup
                                else f"✅ Added to {actual_list}: {parts[0]}")
                        else:
                            tool_confirmation_buffer.append(f"⚠️ Couldn't add task: {parts[0]}")
                    for tag in legacy_done:
                        done_title = complete_task(tag.strip())
                        tool_confirmation_buffer.append(
                            f"✅ Completed: {done_title}" if done_title
                            else f"⚠️ Task not found to complete: {tag.strip()}")
                    for tag in legacy_create:
                        parts = [p.strip() for p in tag.split("|")]
                        if len(parts) >= 2:
                            ok, msg = create_calendar_event(
                                parts[0], parts[1],
                                parts[2] if len(parts) > 2 else None,
                                int(parts[3]) if len(parts) > 3 and parts[3].strip().isdigit() else 60,
                                parts[4] if len(parts) > 4 else "")
                            tool_confirmation_buffer.append(
                                f"📅 Added to calendar: {parts[0]} on {parts[1]}" if ok
                                else f"⚠️ Calendar booking failed: {msg}")
                    for tag in legacy_delete:
                        parts = [p.strip() for p in tag.split("|")]
                        if len(parts) >= 2:
                            ok, msg = delete_calendar_event(parts[0], parts[1])
                            tool_confirmation_buffer.append(
                                f"🗑️ Removed from calendar: {msg}" if ok
                                else f"⚠️ Calendar delete failed: {msg}")
                    for tag in legacy_send:
                        parts = [p.strip() for p in tag.split("|", 2)]
                        if len(parts) >= 3:
                            tool_confirmation_buffer.append(
                                f"📤 Email sent to {parts[0]} — {parts[1]}"
                                if send_email(parts[0], parts[1], parts[2])
                                else f"⚠️ Email failed: {parts[0]}")
                    for tag in legacy_draft:
                        parts = [p.strip() for p in tag.split("|", 2)]
                        if len(parts) >= 3:
                            tool_confirmation_buffer.append(
                                f"📝 Draft saved for {parts[0]} — {parts[1]}"
                                if draft_email(parts[0], parts[1], parts[2])
                                else f"⚠️ Draft failed: {parts[0]}")
                    for tag in legacy_drive:
                        tool_confirmation_buffer.append(
                            f"🔍 Drive — '{tag.strip()}':\n{search_drive(tag.strip())}")
                    for pat in (r'\[ADD_TASK:[^\]]+\]', r'\[COMPLETE_TASK:[^\]]+\]',
                                r'\[CREATE_EVENT:[^\]]+\]', r'\[DELETE_EVENT:[^\]]+\]',
                                r'\[SEARCH_DRIVE:[^\]]+\]'):
                        clean_response = re.sub(pat, '', clean_response)
                    clean_response = re.sub(r'\[SEND_EMAIL:[^\]]+\]', '', clean_response, flags=re.DOTALL)
                    clean_response = re.sub(r'\[DRAFT_EMAIL:[^\]]+\]', '', clean_response, flags=re.DOTALL)
                    clean_response = clean_response.strip()
                    logger.info("v18.13: legacy action tag(s) executed in tool-use path")

                # v18.5: Consolidate tool confirmations + Claude response into one message.
                # Voice mode: confirmations as text + TTS audio (format split is unavoidable).
                if tool_confirmation_buffer and clean_response:
                    if reply_as_voice:
                        await _send_split("\n".join(tool_confirmation_buffer), update)
                        await _tts_speak(clean_response, update)
                    else:
                        combined = "\n".join(tool_confirmation_buffer) + "\n\n" + clean_response
                        await _send_split(combined, update)
                elif tool_confirmation_buffer:
                    await _send_split("\n".join(tool_confirmation_buffer), update)
                elif clean_response:
                    if reply_as_voice:
                        await _tts_speak(clean_response, update)
                    else:
                        await _send_split(clean_response, update)
                else:
                    # v18.13: never go silent — no text and no tool confirmations
                    logger.warning("_process_with_tools: empty final response, sending fallback")
                    await update.message.reply_text(
                        "⚠️ I came back empty on that one — say it again?"
                    )

                # Save conversation history (preserves cross-session context)
                updated_history = list(conversation_history)
                updated_history.append({"role": "user", "content": user_text})
                updated_history.append({"role": "assistant", "content": clean_response or final_text})
                write_conversation_history(updated_history)

                if memory_tags:
                    merged = _merge_memories(memory_tags, memories)
                    if write_memory(merged):
                        logger.info(
                            "Stored %d new memory item(s) from conversation.",
                            len(memory_tags)
                        )
                break

    except Exception as e:
        logger.error("_process_with_tools error: %s", e)
        await update.message.reply_text(f"⚠️ Error: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ace v18 — tool-use text message handler."""
    if not _is_authorized(update):
        return
    user_text = (update.message.text or "").strip()
    if not user_text:
        return
    await _process_with_tools(user_text, update, reply_as_voice=False)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages \u2014 transcribe via Whisper, then process with tool-use loop."""
    if not _is_authorized(update):
        return
    transcript = await _transcribe_voice(update, context)
    if not transcript:
        return
    await update.message.reply_text(f"🎤 Heard: \"{transcript}\"")
    await _process_with_tools(transcript, update, reply_as_voice=True)


# ── Scheduler jobs ─────────────────────────────────────────────────────────────

async def send_morning_brief(app: Application) -> None:
    """Scheduled job — 9:30 AM ET morning brief."""
    global LAST_BRIEF_SENT
    if LAST_BRIEF_SENT is not None:
        mins_ago = (datetime.now(EASTERN) - LAST_BRIEF_SENT).total_seconds() / 60
        if mins_ago < 180:
            logger.info("Skipping scheduled brief — manual brief sent %.0f min ago.", mins_ago)
            return
    try:
        logger.info("Sending scheduled morning brief…")
        brief = build_morning_brief()
        await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=brief)
        logger.info("Morning brief sent.")
    except Exception as e:
        logger.error("Scheduled brief error: %s", e)
        try:
            await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=f"⚠️ Morning brief failed: {e}")
        except Exception:
            pass


async def send_midday_triage(app: Application) -> None:
    """Scheduled job — 1:00 PM ET midday triage."""
    try:
        logger.info("Sending midday triage…")
        brief = build_midday_triage()
        await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=brief)
        logger.info("Midday triage sent.")
    except Exception as e:
        logger.error("Midday triage error: %s", e)
        try:
            await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=f"⚠️ Midday triage failed: {e}")
        except Exception:
            pass


async def send_eod_sweep(app: Application) -> None:
    """Scheduled job — 7:00 PM ET evening wind-down."""
    try:
        logger.info("Sending evening wind-down…")
        brief = build_eod_sweep()
        await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=brief)
        logger.info("Evening wind-down sent.")
    except Exception as e:
        logger.error("Evening wind-down error: %s", e)
        try:
            await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=f"⚠️ Evening wind-down failed: {e}")
        except Exception:
            pass

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    # Register commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("calendar", cmd_calendar))
    app.add_handler(CommandHandler("triage", cmd_triage))
    app.add_handler(CommandHandler("eod", cmd_eod))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("clearhistory", cmd_clear_history))
    app.add_handler(CommandHandler("reset_history", cmd_reset_history))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("session", cmd_session))

    # Free-text and voice conversation handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Scheduler — three daily check-ins, Mon–Fri ET
    scheduler = AsyncIOScheduler(timezone=EASTERN)
    # AUTO-BRIEFS PAUSED — Brady disabled July 2026. Use /brief and /eod commands manually.
    # scheduler.add_job(
    #     send_morning_brief, trigger="cron",
    #     day_of_week="mon-fri", hour=9, minute=30, args=[app],
    # )
    # MIDDAY BRIEF DISABLED — Brady requested removal June 2026
    # scheduler.add_job(
    #     send_midday_triage, trigger="cron",
    #     day_of_week="mon-fri", hour=13, minute=0, args=[app],
    # )
    # scheduler.add_job(
    #     send_eod_sweep, trigger="cron",
    #     day_of_week="mon-fri", hour=21, minute=0, args=[app],
    # )
    scheduler.start()
    logger.info(
        "Scheduler started — auto-briefs PAUSED. Use /brief and /eod manually."
    )

    logger.info(f"Ace v{ACE_VERSION} is starting up…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


