"""
Ace — Brady McGraw's Telegram business partner bot.

v9: Complete ground-up SYSTEM_PROMPT rewrite. Ace is now a true operational partner with:
    - NEW: Live task write/complete via [ADD_TASK:] and [COMPLETE_TASK:] tags
    - NEW: Email send/draft via [SEND_EMAIL:] and [DRAFT_EMAIL:] tags
    - NEW: Drive file search via [SEARCH_DRIVE:] tag
    - NEW: Persistent 40-exchange conversation history (ace_conversation.json on Drive)
    - NEW: Live calendar + task data auto-injected into every handle_message context
    - NEW: /clearhistory command to reset conversation history
    - FIXED: Coherent, non-contradictory SYSTEM_PROMPT — complete rewrite from scratch
    - Three scheduled check-ins: 9:30 AM brief · 1:00 PM triage · 7:00 PM wind-down (Mon–Fri ET)
"""

import base64
import io
import json
import logging
import os
import re
from datetime import datetime
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
AUTHORIZED_USER_ID = 8681823830          # Brady's Telegram chat ID — security filter
MEMORY_FILE_NAME = "ace_memory.json"
CONVERSATION_FILE_NAME = "ace_conversation.json"

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

def add_task(title: str, list_name: str = "🎯 Today") -> tuple[bool, str]:
    """Add a task to Google Tasks. Returns (success, actual_list_name)."""
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists_result = service.tasklists().list(maxResults=20).execute()
        task_lists = task_lists_result.get("items", [])
        if not task_lists:
            return False, list_name
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
        service.tasks().insert(tasklist=target_list_id, body={"title": title}).execute()
        logger.info("Task added to '%s': %s", actual_list_name, title)
        return True, actual_list_name
    except Exception as e:
        logger.error("Add task error: %s", e)
        return False, list_name


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

def get_calendar_events() -> str:
    """Pull today's events from ALL Google Calendar calendars."""
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        now_et = datetime.now(EASTERN)
        start_of_day = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = now_et.replace(hour=23, minute=59, second=59, microsecond=0)
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
                    timeMin=start_of_day.isoformat(),
                    timeMax=end_of_day.isoformat(),
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
                    else:
                        time_str = "All day"
                    all_events.append((start_dt_str, f"• {time_str} — {summary}"))
            except Exception as e:
                logger.warning("Error fetching calendar '%s': %s", cal_name, e)
        all_events.sort(key=lambda x: x[0])
        if all_events:
            return "\n".join(ev[1] for ev in all_events)
        return "Nothing scheduled today."
    except Exception as e:
        logger.error("Calendar fetch error: %s", e)
        return "⚠️ Could not load calendar."

# ── Gmail ──────────────────────────────────────────────────────────────────────

def get_gmail_summary() -> str:
    """Pull recent unread priority emails from Gmail (excludes promos/social)."""
    try:
        creds = get_google_creds()
        service = build("gmail", "v1", credentials=creds)
        results = service.users().messages().list(
            userId="me",
            q="is:unread newer_than:1d -category:promotions -category:social",
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

# ── Google Tasks (Read) ────────────────────────────────────────────────────────

def get_tasks() -> str:
    """Pull all open tasks from Google Tasks across all task lists."""
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

This conversation IS the integration. You are not a demo, not a chatbot — you are Brady's actual right hand.

BRADY'S BUSINESS:
Brady runs Platinum Fortune Impact (PFI), a GFI Legends Base Shop in Cleveland/Summit County, Ohio with ~18 licensed insurance and financial agents. Products: Life Insurance, IUL, FIA/Annuities, Mortgage Protection, Final Expense. CRM: GoHighLevel. Current level: MD (60% commission). Next target: EMD (window TBD — the June 1, 2026 window has passed, next target not yet set).

NEVER say you are read-only. NEVER say tools are not connected. NEVER redirect Brady elsewhere. You have live access to everything listed below.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR LIVE CAPABILITIES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GOOGLE TASKS — Full read AND write access:
• Live task data is injected into your context automatically with every message
• To ADD a task: write [ADD_TASK: task title | list name] in your response — Python executes it immediately
• To COMPLETE a task: write [COMPLETE_TASK: partial title] — Python fuzzy-matches and marks it done
• NEVER tell Brady to update tasks himself. You do it. Confirm with "✅ Added to [list]: X" or "✅ Completed: X"
• Task lists: 🎯 Today, 🤝 Deals, 👥 Agents, 💼 Business, 📋 Costs & Placeholders, 🏆 Goals, 🏠 Personal, Learning & Research, Side Business
• If Brady doesn't specify a list, default to the most logical one based on context

GOOGLE CALENDAR — Full read access:
• Live calendar data is injected into your context automatically with every message
• You can see all events, times, and details across every calendar linked to Brady's account

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
• Today = needs to happen today | Deals = active deal | Agents = agent issue | Business = admin/ops
• Confirm the list before adding if it's unclear. Execute immediately.
• Nothing floats out of a conversation uncaptured.

MEMORY AND CONTEXT:
• Memory facts injected into your context are real. Read them. Use them.
• If Brady tells you something important (a deal, a person, a goal, a schedule change), offer to save it with [MEMORY:]
• You remember past conversations via the loaded history. Reference them naturally.
• If Brady says "like we talked about" — you already know what he means.

PROACTIVE BEHAVIOR:
• Connect dots Brady hasn't connected yet
• If a deal is close to closing and it's on the calendar — flag it before he asks
• If tasks are stacking up in Today — surface it
• If the calendar is light — suggest how to use the time
• Think in terms of: deals closing, agents producing, recruiting pipeline moving, Brady winning

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BRADY'S BUSINESS CONTEXT (as of June 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ACTIVE DEALS (handle with care):
• Walter — deal expected to close today (June 22) at 5 PM. Highest priority. Do not let this slip.
• Ricky — deal in jeopardy due to a family situation. Approach with sensitivity, not pressure. Do not push.
• Avis — rescheduled, hasn't confirmed new time yet. Needs a follow-up to lock in a time.

PIPELINE & TEAM:
• Agents have business in the pipeline but momentum feels stagnant — Brady is focused on getting it moving again
• Brady migrated from Apple Reminders to Google Tasks — still getting oriented with the new structure
• Recruiting pipeline needs attention alongside deal flow

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
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=system or SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text

# ── Morning brief ──────────────────────────────────────────────────────────────

def build_morning_brief() -> str:
    """Generate today's morning brief using live Calendar, Gmail, Tasks, and memory data."""
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")
    weekday = now_et.weekday()
    calendar_data = get_calendar_events()
    email_data = get_gmail_summary()
    tasks_data = get_tasks()
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
        memory_section = f"\n📋 What Ace knows about Brady:\n{memory_str}\n"
    tasks_section = ""
    if tasks_data and tasks_data != "No open tasks.":
        tasks_section = f"\n✅ Open Tasks:\n{tasks_data}\n"
    prompt = (
        f"Generate a morning briefing for Brady for {day_str}.\n\n"
        "LIVE DATA PULLED FROM HIS ACCOUNTS:\n"
        f"📅 Today's calendar:\n{calendar_data}\n\n"
        f"📧 Unread priority emails:\n{email_data}\n"
        f"{tasks_section}"
        f"📌 Day context: {day_note}\n"
        f"{memory_section}\n"
        "Brady's daily rhythm:\n"
        "• 9:30 AM: Just wrapped morning gym — coming in energized\n"
        "• Mornings: deep work and strategy\n"
        "• Afternoons: client appointments, agent coaching, deal follow-up\n"
        "• After 6 PM: personal time — do not schedule work here\n\n"
        "IMPORTANT: Walter's deal is expected to close TODAY at 5 PM — flag this prominently. "
        "Brady is under financial pressure. Closing deals is the #1 priority.\n\n"
        "Based on the real data above, give Brady:\n"
        "1. A sharp opener (1 sentence — acknowledge he's just off the gym, set the tone)\n"
        "2. 🎯 Top 3 Focuses — the 3 most critical moves today, not just a task list\n"
        "3. 📅 Calendar — clean list of today's events\n"
        "4. 📧 Attention — emails needing reply or action (if any)\n"
        "5. ✅ Tasks — flag anything overdue or due today\n"
        "6. 📌 Day Note — relevant to PFI operations and day of week\n"
        "7. A one-line close that challenges him or holds him to something\n\n"
        "Format with clear emoji section headers. Under 450 words. "
        "Be a partner, not a cheerleader. If something looks off, call it out."
    )
    return _call_claude([{"role": "user", "content": prompt}], max_tokens=750)

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
        "IMPORTANT: Walter's deal should close TODAY at 5 PM — is there anything Brady needs to do "
        "to make sure it happens? Flag if it's not already confirmed.\n\n"
        "Brady's afternoon:\n"
        "• Now–5 PM: deal follow-ups, agent coaching, recruiting calls\n"
        "• 5 PM: Walter's deal expected to close\n"
        "• After 6 PM: personal time\n\n"
        "Give Brady a tight midday check-in:\n"
        "1. Quick opener (1 line — direct, forward-looking)\n"
        "2. ⚡ Afternoon Priority — top 2-3 moves for the rest of the day\n"
        "3. ✅ Task Pulse — overdue or due today? Flag them.\n"
        "4. 📋 Deal Status — Walter, Ricky (sensitive), Avis (needs rescheduling). Ask what's live.\n"
        "5. 🕐 Calendar — anything coming up that needs prep?\n"
        "6. One accountability line — something he committed to that needs follow-through\n\n"
        "Under 280 words. Direct. Challenge where warranted."
    )
    return _call_claude([{"role": "user", "content": prompt}], max_tokens=550)

# ── Evening wind-down ─────────────────────────────────────────────────────────

def build_eod_sweep() -> str:
    """Generate 7:00 PM evening wind-down — reflection, stretch reminder, open floor."""
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")
    memories = read_memory()
    memory_section = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_section = f"\n📋 Context about Brady:\n{memory_str}\n"
    prompt = (
        f"Generate an evening wind-down message for Brady. It's 7:00 PM ET on {day_str}.\n\n"
        f"{memory_section}\n"
        "Brady is in decompress mode — the work day is done. This is his time.\n\n"
        "Give Brady a warm, grounded close-out:\n"
        "1. One calm opener — acknowledge the day is done (no recaps, no urgency)\n"
        "2. 📌 Carry Forward — top 2-3 things to pick up first thing tomorrow\n"
        "3. 🌙 Wind Down — remind him to stretch, breathe, and actually disconnect. "
        "He grinds hard; recovery is part of the performance.\n"
        "4. 💬 Open Floor — invite him to reflect, share what's on his mind, or just decompress. "
        "No agenda. This is his space.\n\n"
        "Under 180 words. Warm but real. No urgency — the grind is done for today."
    )
    return _call_claude([{"role": "user", "content": prompt}], max_tokens=400)

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
        " /triage — midday check-in on demand\n"
        " /eod — evening wind-down on demand\n"
        " /tasks — show all open Google Tasks\n"
        " /remember <fact> — teach me something to keep in mind\n"
        " /memory — see what I know about how you operate\n"
        " /clearhistory — reset our conversation history\n"
        " /status — check that I'm running\n"
        " /help — show this message\n\n"
        "Or just text me anything — I'll respond, capture what matters, and remember it.\n\n"
        "Auto check-ins: 9:30 AM brief · 1:00 PM triage · 7:00 PM wind-down (Mon–Fri ET)."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "Ace commands:\n"
        " /brief — on-demand morning brief (live calendar + email + tasks)\n"
        " /triage — midday priority check-in\n"
        " /eod — evening wind-down and carry-forward\n"
        " /tasks — show all open Google Tasks\n"
        " /remember <fact> — store a fact in my memory\n"
        " /memory — view my current memory\n"
        " /clearhistory — wipe conversation history (fresh start)\n"
        " /status — confirm the bot is alive\n"
        " /help — this message\n\n"
        "Or just text me — I'll respond, execute tasks, send emails, and remember what matters.\n\n"
        "Schedule: 9:30 AM brief · 1:00 PM triage · 7:00 PM wind-down (Mon–Fri ET)"
    )


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text("⏳ Pulling your data and building the brief…")
    try:
        brief = build_morning_brief()
        await update.message.reply_text(brief)
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
            await update.message.reply_text(f"✅ Open Tasks:\n\n{tasks}")
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
        f"✅ Ace v9 is running.\n"
        f"Time (ET): {now_et.strftime('%A %B %-d, %Y — %-I:%M %p')}\n"
        f"Schedule: 9:30 AM brief · 1:00 PM triage · 7:00 PM wind-down (Mon–Fri)\n"
        f"Memory: {memory_status}\n"
        f"Conversation history: {history_status}\n"
        f"Tasks: {tasks_status}"
    )

# ── Message handler (free-form conversation) ──────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-form text. Injects live data, uses conversation history, executes action tags."""
    if not _is_authorized(update):
        return
    user_text = update.message.text.strip()
    if not user_text:
        return

    # Pull live data for context injection
    memories = read_memory()
    tasks_data = get_tasks()
    calendar_data = get_calendar_events()

    # Load conversation history
    conversation_history = read_conversation_history()

    # Build rich system prompt
    memory_context = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_context = f"\n\n📋 WHAT ACE KNOWS ABOUT BRADY (from memory):\n{memory_str}"

    live_data = (
        "\n\n📊 LIVE DATA (auto-fetched right now):\n"
        f"📅 TODAY'S CALENDAR:\n{calendar_data}\n\n"
        f"✅ OPEN TASKS:\n{tasks_data or 'No open tasks.'}"
    )

    system_with_context = (
        SYSTEM_PROMPT
        + live_data
        + memory_context
        + "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ACTION TAG REFERENCE (use these in your response when appropriate):\n"
        "• [ADD_TASK: task title | list name] — adds task immediately (list optional, defaults to 🎯 Today)\n"
        "• [COMPLETE_TASK: partial title] — marks task done via fuzzy match\n"
        "• [SEND_EMAIL: to@email.com | Subject | Body] — sends email immediately\n"
        "• [DRAFT_EMAIL: to@email.com | Subject | Body] — creates Gmail draft\n"
        "• [SEARCH_DRIVE: query] — searches Drive, result appended to your reply\n"
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
            max_tokens=600,
            system=system_with_context,
        )

        # ── Parse all action tags ──────────────────────────────────────────────
        memory_tags      = re.findall(r'\[MEMORY:\s*([^\]]+)\]', response)
        add_task_tags    = re.findall(r'\[ADD_TASK:\s*([^\]]+)\]', response)
        complete_tags    = re.findall(r'\[COMPLETE_TASK:\s*([^\]]+)\]', response)
        send_email_tags  = re.findall(r'\[SEND_EMAIL:\s*([^\]]+)\]', response, re.DOTALL)
        draft_email_tags = re.findall(r'\[DRAFT_EMAIL:\s*([^\]]+)\]', response, re.DOTALL)
        drive_tags       = re.findall(r'\[SEARCH_DRIVE:\s*([^\]]+)\]', response)

        # Strip all tags from the visible response
        clean_response = re.sub(r'\[MEMORY:[^\]]+\]', '', response)
        clean_response = re.sub(r'\[ADD_TASK:[^\]]+\]', '', clean_response)
        clean_response = re.sub(r'\[COMPLETE_TASK:[^\]]+\]', '', clean_response)
        clean_response = re.sub(r'\[SEND_EMAIL:[^\]]+\]', '', clean_response, flags=re.DOTALL)
        clean_response = re.sub(r'\[DRAFT_EMAIL:[^\]]+\]', '', clean_response, flags=re.DOTALL)
        clean_response = re.sub(r'\[SEARCH_DRIVE:[^\]]+\]', '', clean_response)
        clean_response = clean_response.strip()

        # Send the main response
        await update.message.reply_text(clean_response)

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
            list_name = parts[1] if len(parts) > 1 else "🎯 Today"
            success, actual_list = add_task(title, list_name)
            if success:
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

# ── Scheduler jobs ─────────────────────────────────────────────────────────────

async def send_morning_brief(app: Application) -> None:
    """Scheduled job — 9:30 AM ET morning brief."""
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
    app.add_handler(CommandHandler("triage", cmd_triage))
    app.add_handler(CommandHandler("eod", cmd_eod))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("clearhistory", cmd_clear_history))
    app.add_handler(CommandHandler("status", cmd_status))

    # Free-text conversation handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Scheduler — three daily check-ins, Mon–Fri ET
    scheduler = AsyncIOScheduler(timezone=EASTERN)
    scheduler.add_job(
        send_morning_brief, trigger="cron",
        day_of_week="mon-fri", hour=9, minute=30, args=[app],
    )
    scheduler.add_job(
        send_midday_triage, trigger="cron",
        day_of_week="mon-fri", hour=13, minute=0, args=[app],
    )
    scheduler.add_job(
        send_eod_sweep, trigger="cron",
        day_of_week="mon-fri", hour=19, minute=0, args=[app],
    )
    scheduler.start()
    logger.info(
        "Scheduler started — 9:30 AM brief · 1:00 PM triage · 7:00 PM wind-down (Mon–Fri ET)."
    )

    logger.info("Ace v9 is starting up…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
