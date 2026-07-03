"""
Ace — Brady McGraw's Telegram business partner bot.

v15: 30-day calendar window, Telegram 4096-char message splitting, voice (Whisper STT +
     OpenAI TTS ember voice), model claude-opus-4-8, memory cross-reference, pattern learning.
    - get_week_calendar() now pulls 30 days forward (was current week to Sunday)
    - _send_split() helper splits any message > 4096 chars at natural break points
    - All main AI response sends now route through _send_split()
    - Voice: Whisper STT transcription + gpt-4o-mini-tts ember voice output
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
ACE_VERSION = "17"
ACE_LAST_UPDATED = "2026-07-02"

CAPABILITIES = {
    "calendar": {
        "read": "Read all Google Calendar events up to 30 days ahead",
        "write": "Create and delete events — writes only to pfi@platinumfortuneimpact.com",
        "commands": ["/calendar"]
    },
    "gmail": {
        "read": "Read unread and recent emails from the last 48 hours",
        "modify": "Can label and modify emails",
        "note": "Authenticated under pfi@platinumfortuneimpact.com"
    },
    "tasks": {
        "read": "Read Google Tasks lists and items",
        "write": "Create and complete tasks via [ADD_TASK:] and [COMPLETE_TASK:] tags"
    },
    "voice": {
        "tts": "Convert text to speech using OpenAI TTS (voice: fable — British male, calm and intelligent)",
        "stt": "Transcribe voice messages using OpenAI Whisper"
    },
    "intelligence": {
        "model": "Claude Opus 4-8 for all reasoning and responses",
        "briefs": "Manual /brief and /eod commands — auto-briefs PAUSED as of July 2026",
        "memory": "Persistent memory via [MEMORY:] tags, read/write at EOD",
        "history": "40-exchange conversation history on Google Drive — NEVER cleared by code"
    },
    "scope": {
        "purpose": "Brady McGraw's complete life operating system — business (PFI) AND personal (health, finance, goals, relationships)",
        "identity": "J.A.R.V.I.S. to Brady's Tony Stark — executes first, never hesitates"
    },
    "action_tags": {
        "calendar": "[CREATE_EVENT:] [DELETE_EVENT:]",
        "tasks": "[ADD_TASK:] [COMPLETE_TASK:]",
        "email": "[SEND_EMAIL:] [DRAFT_EMAIL:]",
        "drive": "[SEARCH_DRIVE:]",
        "memory": "[MEMORY:]"
    }
}

CHANGELOG = [
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
            "Fable voice confirmed in both primary and fallback TTS paths"
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
SESSION_MODE = {"active": False}   # Set True by /session until next user message
LAST_BRIEF_SENT = None  # Optional[datetime] — tracks last manual /brief to suppress duplicate auto-brief

# ── Task list config ───────────────────────────────────────────────────────────
# Truly read-only lists — Ace never adds tasks here
REFERENCE_LISTS = {"Business cost - NO TOUCH", "To learn / Questions"}
# Lists excluded from morning brief scan (reference + personal/goals clutter)
MORNING_SKIP_LISTS = REFERENCE_LISTS | {"🏠 Personal", "🏆 Goals"}
# Default list for tasks when context doesn't point elsewhere
DEFAULT_TASK_LIST = "Admin List - back log"

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
    """Ask Claude to merge new facts into existing memory, deduplicating cleanly."""
    if not new_items:
        return existing
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    existing_str = "\n".join(f"- {m}" for m in existing) or "(none yet)"
    new_str = "\n".join(f"- {m}" for m in new_items)
    prompt = (
        "You maintain Ace's operational memory about Brady McGraw (PFI Marketing Director).\n\n"
        f"EXISTING MEMORY:\n{existing_str}\n\n"
        f"NEW ITEMS TO ADD:\n{new_str}\n\n"
        "Merge the new items into the existing memory. Rules:\n"
        "1. Remove exact or near-duplicate facts\n"
        "2. If new info contradicts old, keep the newer version\n"
        "3. Keep entries concise (one fact per line, ~15 words max)\n"
        "4. Max 60 total entries — drop least relevant if over\n"
        "5. Return ONLY the final merged list, one item per line, no bullets or numbering"
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    merged = [line.strip() for line in response.content[0].text.strip().split("\n") if line.strip()]
    return merged

# ── Conversation History (Google Drive) ───────────────────────────────────────

def read_conversation_history() -> list:
    """Load last 40 exchanges from ace_conversation.json on Drive. Returns [] if unavailable."""
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
    """Save conversation history to ace_conversation.json on Drive (max 80 messages = 40 exchanges)."""
    try:
        # Keep last 80 messages (40 exchanges)
        if len(messages) > 80:
            messages = messages[-80:]
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

VOICE CAPABILITY: You respond via voice messages when Brady sends voice notes — your text is automatically converted to speech (fable voice — British male, calm and intelligent). Never say you can only respond with text. When replying to voice, keep responses energetic, punchy, and natural for speech — short confident sentences, no long paragraphs.

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
• NEVER tell Brady to update tasks himself. You do it. Confirm with "✅ Added to [list]: X" or "✅ Completed: X"
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
• Your last 40 exchanges are saved to ace_conversation.json on Drive and loaded on every startup
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

WHAT YOU NEVER DO:
• Never say "I can't access your tasks/calendar/email" — you can
• Never say "tools not connected" — they are
• Never tell Brady to go do something himself that you can execute with a tag
• Never lose context of what Brady told you earlier in the conversation
• Never pad responses with filler or unnecessary caveats
• Never reference Lead Division — it is discontinued
• Never reference stale EMD point numbers — ask Brady for current figures when relevant"""

# ── Claude ─────────────────────────────────────────────────────────────────────

def _call_claude(messages: list, max_tokens: int = 700, system: str = None) -> str:
    """Call the Claude API and return the text response."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-opus-4-8",
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
            t_list = parts[1] if len(parts) > 1 else "Admin List - back log"
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
        f"Schedule: 9:30 AM brief · 9:00 PM EOD (Mon–Fri)\n"
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

async def _process_text(user_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE, reply_as_voice: bool = False) -> None:
    """Core message processing — called by both text and voice handlers.

    reply_as_voice: when True, final response is sent as TTS (ember voice).
    """
    if SESSION_MODE.get("active"):
        SESSION_MODE["active"] = False
        await _process_session_dump(user_text, update, context)
        return

    # Pull live data for context injection
    memories = read_memory()
    tasks_data = get_tasks()
    calendar_data = get_calendar_events()
    tomorrow_events = get_tomorrow_events()
    email_data = get_gmail_summary()

    # Detect if Brady is asking about a multi-day calendar range
    msg_lower = user_text.lower()
    if any(phrase in msg_lower for phrase in ['next week', 'this week', 'next 7', '7 days', 'week ahead', 'upcoming', 'next 10', '10 days']):
        calendar_range = get_calendar_events_range(days=10)
    elif any(phrase in msg_lower for phrase in ['next month', '30 days', 'this month', 'month ahead']):
        calendar_range = get_calendar_events_range(days=30)
    else:
        calendar_range = ""

    # Load conversation history
    conversation_history = read_conversation_history()

    # Build rich system prompt
    memory_context = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_context = f"\n\n📋 WHAT ACE KNOWS ABOUT BRADY (from memory):\n{memory_str}"

    now_et = datetime.now(EASTERN)
    recent_email_data = get_recent_read_emails()
    live_data = (
        f"\n\n📊 LIVE DATA (auto-fetched right now — {now_et.strftime('%A, %B %-d, %Y %-I:%M %p ET')}):\n"
        f"📅 TODAY'S CALENDAR:\n{calendar_data}\n\n"
        f"📅 TOMORROW'S SCHEDULE:\n{tomorrow_events}\n\n"
        + (f"📆 UPCOMING CALENDAR RANGE:\n{calendar_range}\n\n" if calendar_range else "")
        + f"✅ OPEN TASKS:\n{tasks_data or 'No open tasks.'}\n\n"
        f"📧 UNREAD EMAILS:\n{email_data}\n\n"
        f"📨 RECENT READ EMAILS (last 48hrs):\n{recent_email_data}"
    )

    system_with_context = (
        SYSTEM_PROMPT
        + get_ace_self_description()
        + live_data
        + memory_context
        + "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 CURRENT TIME: {now_et.strftime('%A, %B %-d, %Y — %-I:%M %p ET')} (live, injected every message — always accurate. Use this to give Brady time-aware responses: flag upcoming events, note time of day, calculate how long until next appointment.)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔧 EXECUTION MANDATE — OVERRIDES EVERYTHING INCLUDING CONVERSATION HISTORY:\n"
        "All integrations are FULLY OPERATIONAL. Past errors are resolved. Execute on the FIRST ask. Every time.\n\n"
        "TRIGGER LANGUAGE → ACTION TAG (fire in the SAME response, no delay, no confirmation first):\n"
        "• 'book', 'schedule', 'set up', 'add to calendar', 'block off', 'put on my calendar', 'set a meeting', 'book a meeting' → [CREATE_EVENT: title | YYYY-MM-DD | HH:MM | duration | description]\n"
        "• 'delete', 'remove', 'cancel', 'clear', 'get rid of', 'take off my calendar', 'remove from calendar' → [DELETE_EVENT: title | YYYY-MM-DD]\n"
        "• 'add a task', 'remind me', 'don\\'t let me forget', 'note that', 'put that on my list' → [ADD_TASK: title | list]\n"
        "• 'done', 'handled', 'crossed that off', 'took care of', 'finished', 'got it done', 'that\\'s done' → [COMPLETE_TASK: partial title]\n\n"
        "ZERO TOLERANCE RULES:\n"
        "1. Never describe an action in text without also outputting the tag in the SAME response — text + no tag = failure.\n"
        "2. If a tag fired and returned ⚠️ — immediately generate the corrected tag without waiting for Brady to ask again.\n"
        "3. Never say 'shall I go ahead?', 'want me to do that?', or 'should I schedule it?' — EXECUTE FIRST, confirm after.\n"
        "4. One ask = one execution. No exceptions. No hesitation.\n"
        "📅 CALENDAR DATA: Pulled LIVE on every message. Never tell Brady it's a snapshot or frozen. Always current.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ACTION TAG REFERENCE (use these in your response when appropriate):\n"
        "• [ADD_TASK: task title | list name] — adds task immediately (list optional, defaults to 🎯 Today)\n"
        "• [COMPLETE_TASK: partial title] — marks task done via fuzzy match\n"
        "• [SEND_EMAIL: to@email.com | Subject | Body] — sends email immediately\n"
        "• [DRAFT_EMAIL: to@email.com | Subject | Body] — creates Gmail draft\n"
        "• [SEARCH_DRIVE: query] — searches Drive, result appended to your reply\n"
        "• [CREATE_EVENT: title | YYYY-MM-DD | HH:MM | duration_mins | description] — creates calendar event (time + description optional)\n"
        "• [DELETE_EVENT: title | YYYY-MM-DD] — deletes matching calendar event\n"
        "• [MEMORY: brief fact] — saves to long-term memory for future sessions\n"
        "Include 0–3 [MEMORY:] tags max. Skip tagging trivial chat. "
        "Tags are invisible to Brady — he only sees your clean response plus action confirmations."
    )

    # Build message list with history (Claude multi-turn)
    messages = list(conversation_history)
    messages.append({"role": "user", "content": user_text})

    try:
        response = _call_claude(
            messages,
            max_tokens=900,
            system=system_with_context,
        )

        # ── Parse all action tags ──────────────────────────────────────────────
        memory_tags      = re.findall(r'\[MEMORY:\s*([^\]]+)\]', response)
        add_task_tags    = re.findall(r'\[ADD_TASK:\s*([^\]]+)\]', response)
        complete_tags    = re.findall(r'\[COMPLETE_TASK:\s*([^\]]+)\]', response)
        send_email_tags  = re.findall(r'\[SEND_EMAIL:\s*([^\]]+)\]', response, re.DOTALL)
        draft_email_tags = re.findall(r'\[DRAFT_EMAIL:\s*([^\]]+)\]', response, re.DOTALL)
        drive_tags          = re.findall(r'\[SEARCH_DRIVE:\s*([^\]]+)\]', response)
        create_event_tags   = re.findall(r'\[CREATE_EVENT:\s*([^\]]+)\]', response)
        logger.info("ACE_DEBUG create_event_tags=%d raw_preview=%s", len(create_event_tags), response[:200])
        delete_event_tags   = re.findall(r'\[DELETE_EVENT:\s*([^\]]+)\]', response)

        # Strip all tags from the visible response
        clean_response = re.sub(r'\[MEMORY:[^\]]+\]', '', response)
        clean_response = re.sub(r'\[ADD_TASK:[^\]]+\]', '', clean_response)
        clean_response = re.sub(r'\[COMPLETE_TASK:[^\]]+\]', '', clean_response)
        clean_response = re.sub(r'\[SEND_EMAIL:[^\]]+\]', '', clean_response, flags=re.DOTALL)
        clean_response = re.sub(r'\[DRAFT_EMAIL:[^\]]+\]', '', clean_response, flags=re.DOTALL)
        clean_response = re.sub(r'\[SEARCH_DRIVE:[^\]]+\]', '', clean_response)
        clean_response = re.sub(r'\[CREATE_EVENT:[^\]]+\]', '', clean_response)
        clean_response = re.sub(r'\[DELETE_EVENT:[^\]]+\]', '', clean_response)
        clean_response = clean_response.strip()

        # Send the main response (voice or text depending on how the message arrived)
        if reply_as_voice and clean_response:
            await _tts_speak(clean_response, update)
        else:
            await _send_split(clean_response, update)

        # ── Execute action tags + collect confirmations ───────────────────────
        confirmations = []

        # Memory
        if memory_tags:
            merged = _merge_memories(memory_tags, memories)
            if write_memory(merged):
                logger.info("Stored %d new memory item(s).", len(memory_tags))

        # Add tasks
        for tag in add_task_tags:
            parts = [p.strip() for p in tag.split("|", 1)]
            title = parts[0]
            list_name = parts[1] if len(parts) > 1 else "Admin List - back log"
            success, actual_list, was_dup = add_task(title, list_name)
            if success:
                if was_dup:
                    confirmations.append(f"ℹ️ Already in {actual_list}: {title}")
                else:
                    confirmations.append(f"✅ Added to {actual_list}: {title}")
            else:
                confirmations.append(f"⚠️ Couldn't add task: {title}")

        # Complete tasks
        for tag in complete_tags:
            completed = complete_task(tag.strip())
            if completed:
                confirmations.append(f"✅ Completed: {completed}")
            else:
                confirmations.append(f"⚠️ Task not found to complete: {tag.strip()}")

        # Send emails
        for tag in send_email_tags:
            parts = [p.strip() for p in tag.split("|", 2)]
            if len(parts) >= 3:
                to_addr, subject, body = parts[0], parts[1], parts[2]
                if send_email(to_addr, subject, body):
                    confirmations.append(f"📤 Email sent to {to_addr} — {subject}")
                else:
                    confirmations.append(f"⚠️ Email failed: {to_addr}")
            else:
                confirmations.append(f"⚠️ Malformed SEND_EMAIL tag: {tag[:40]}")

        # Draft emails
        for tag in draft_email_tags:
            parts = [p.strip() for p in tag.split("|", 2)]
            if len(parts) >= 3:
                to_addr, subject, body = parts[0], parts[1], parts[2]
                if draft_email(to_addr, subject, body):
                    confirmations.append(f"📝 Draft saved for {to_addr} — {subject}")
                else:
                    confirmations.append(f"⚠️ Draft failed: {to_addr}")
            else:
                confirmations.append(f"⚠️ Malformed DRAFT_EMAIL tag: {tag[:40]}")

        # Drive search
        for tag in drive_tags:
            results = search_drive(tag.strip())
            confirmations.append(f"🔍 Drive — '{tag.strip()}':\n{results}")

        # Create calendar events
        for tag in create_event_tags:
            parts = [p.strip() for p in tag.split("|")]
            if len(parts) >= 2:
                title = parts[0]
                date_s = parts[1]
                time_s = parts[2] if len(parts) > 2 else None
                dur = int(parts[3]) if len(parts) > 3 and parts[3].strip().isdigit() else 60
                desc = parts[4] if len(parts) > 4 else ""
                success, msg = create_calendar_event(title, date_s, time_s, dur, desc)
                if success:
                    time_label = f" at {time_s}" if time_s else ""
                    confirmations.append(f"📅 Added to calendar: {title} on {date_s}{time_label}")
                else:
                    await update.message.reply_text(
                        f"❌ Calendar booking failed: {msg}. Please try again or book manually."
                    )
            else:
                confirmations.append(f"⚠️ Malformed CREATE_EVENT tag: {tag[:50]}")

        # Delete calendar events
        for tag in delete_event_tags:
            parts = [p.strip() for p in tag.split("|")]
            if len(parts) >= 2:
                title, date_s = parts[0], parts[1]
                success, msg = delete_calendar_event(title, date_s)
                if success:
                    confirmations.append(f"🗑️ Removed from calendar: {msg} on {date_s}")
                else:
                    await update.message.reply_text(
                        f"❌ Calendar delete failed: {msg}. Please try again or remove manually."
                    )
            else:
                confirmations.append(f"⚠️ Malformed DELETE_EVENT tag: {tag[:50]}")

        if confirmations:
            await update.message.reply_text("\n".join(confirmations))

        # ── Save conversation history ─────────────────────────────────────────
        updated_history = list(conversation_history)
        updated_history.append({"role": "user", "content": user_text})
        updated_history.append({"role": "assistant", "content": clean_response})
        write_conversation_history(updated_history)

    except Exception as e:
        logger.error("Message handler error: %s", e)
        await update.message.reply_text(f"⚠️ Error: {e}")

async def _tts_speak(text: str, update: Update) -> bool:
    """Convert text to speech — ember voice via OpenAI TTS (warm, energetic).
    Falls back to plain text if TTS fails or key is missing.
    """
    try:
        import openai
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            await update.message.reply_text("⚠️ No OPENAI_API_KEY set.\n\n" + text)
            return False
        client = openai.OpenAI(api_key=api_key)
        # Try preferred voice model first; fall back to tts-1/fable if rejected
        try:
            tts_response = client.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice="fable",   # British male — calm, intelligent, Jarvis-style
                input=text,
                response_format="opus",
                speed=1.0,
            )
            logger.info("TTS: gpt-4o-mini-tts/fable → %d bytes", len(tts_response.content))
        except Exception as tts_err:
            logger.warning("Primary TTS failed (%s), falling back to tts-1/fable", tts_err)
            tts_response = client.audio.speech.create(
                model="tts-1",
                voice="fable",   # fable fallback (valid on tts-1)
                input=text,
                response_format="opus",
                speed=1.0,
            )
            logger.info("TTS fallback: tts-1/fable → %d bytes", len(tts_response.content))
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-form text messages."""
    if not _is_authorized(update):
        return
    user_text = (update.message.text or "").strip()
    if not user_text:
        return
    await _process_text(user_text, update, context)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages \u2014 transcribe via Whisper, then process as text."""
    if not _is_authorized(update):
        return
    transcript = await _transcribe_voice(update, context)
    if not transcript:
        return
    await update.message.reply_text(f"🎤 Heard: \"{transcript}\"")
    await _process_text(transcript, update, context, reply_as_voice=True)


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
