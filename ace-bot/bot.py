"""
Ace — Brady McGraw's Telegram business partner bot.
Sends a morning briefing every weekday at 9:30 AM ET with live
Google Calendar events, Gmail unread summary, and Google Tasks,
then calls Claude to produce a prioritised daily brief.

v5: Three daily check-ins — 9:30 AM brief, 1:00 PM midday triage, 5:30 PM EOD sweep.
    Respects Brady's schedule blocks and actively protects personal time after 6 PM.
v6: Google Tasks integration — open tasks surface in morning brief and midday triage.
    /tasks command shows all open tasks on demand.
v7: Evening wind-down moves to 7:00 PM — reflection, stretch reminder, close-of-day chat.
    System prompt expanded with EMD goal, commission level, Lead Division schedule.
    Morning brief acknowledges Brady is coming off his gym session.
v8: Ace becomes a real business partner — challenges Brady, pushes back, holds him accountable.
    EMD window updated to August 1, 2026. No hardcoded production numbers — Ace asks Brady
    where he stands instead of referencing stale data. Periodic check-in questions built in.
"""

import io
import json
import logging
import os
import re
from datetime import datetime

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
AUTHORIZED_USER_ID = 8681823830  # Brady's Telegram chat ID — security filter
MEMORY_FILE_NAME = "ace_memory.json"

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

# ── Google Tasks ───────────────────────────────────────────────────────────────

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

# ── Claude ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are Ace, Brady McGraw's real business partner — not a yes-man. "
    "Your job is to challenge his thinking, push back when something doesn't add up, "
    "hold him accountable to his commitments, and call it out when he's drifting. "
    "Agree when it makes sense. Disagree when it doesn't. Never validate just to make him feel good. "
    "Brady is the Marketing Director and owner of Platinum Fortune Impact (PFI), "
    "a GFI Legends Base Shop in Summit County/Cleveland, Ohio. "
    "He leads ~18 licensed insurance and financial services agents. "
    "Primary products: Life Insurance, IUL, FIA/Annuities, Mortgage Protection, Final Expense. "
    "CRM: GoHighLevel. "
    "GFI promotion path: BL → SM → MD (Brady's current level, 60% commission) → EMD → SBL. "
    "EMD requires Brady's personal production AND his Super Team to hit rolling 6-month benchmarks. "
    "His current promotion window closes August 1, 2026. "
    "Do NOT reference specific point numbers from memory — they change weekly and stale data misleads. "
    "Instead, periodically ask Brady where he and his team stand so you're working from live numbers. "
    "Lead Division runs Tuesday through Friday — this shapes his weekly rhythm. "
    "Brady's 9:30 AM brief catches him right after his morning gym session. "
    "Keep responses tight, direct, and actionable. Lead with what matters most. Never pad."
)

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
        1: "Tuesday — Lead Division is live. New leads need same-day follow-up.",
        2: "Wednesday — Lead Division running. Mid-week pulse — is the team actually producing?",
        3: "Thursday — Lead Division running. Push for closes before the week bleeds out.",
        4: "Friday — Lead Division running. Wrap strong. Don't let momentum die over the weekend.",
    }
    day_note = day_reminders.get(weekday, "")
    # Monday check-in: ask about EMD standing
    emd_check = ""
    if weekday == 0:
        emd_check = (
            "\n🎯 EMD CHECK-IN: It's Monday — ask Brady where his personal points and Super Team "
            "points stand heading into the week. The August 1 window is live. Don't assume — ask.\n"
        )
    memory_section = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_section = f"\n📋 What I know about Brady:\n{memory_str}\n"
    tasks_section = ""
    if tasks_data:
        tasks_section = f"\n✅ Open Tasks:\n{tasks_data}\n"
    prompt = (
        f"Generate a morning briefing for Brady for {day_str}.\n\n"
        "LIVE DATA PULLED FROM HIS ACCOUNTS:\n"
        f"📅 Today's calendar:\n{calendar_data}\n\n"
        f"📧 Unread priority emails:\n{email_data}\n"
        f"{tasks_section}"
        f"📌 Day context: {day_note}\n"
        f"{emd_check}"
        f"{memory_section}\n"
        "Brady's daily schedule:\n"
        "• 9:30 AM: Just wrapped morning gym — coming in energized\n"
        "• Mornings: deep work and strategy blocks\n"
        "• 12–3 PM: recruiting and training (protected block)\n"
        "• 4–6 PM: client appointments, leads, field training (protected)\n"
        "• After 6 PM: personal time — do not schedule work here\n\n"
        "Based on the real data above, give Brady:\n"
        "1. A sharp opener (1 sentence — acknowledge he's just off the gym, set the tone for the day)\n"
        "2. 🎯 Top 3 Focuses — the 3 most critical moves today, not just a task list\n"
        "3. 📅 Calendar — clean list of his meetings/events today\n"
        "4. 📧 Attention — emails that need a reply or action (if any)\n"
        "5. ✅ Tasks — flag anything overdue or due today\n"
        "6. 📌 Day Reminders — specific to PFI operations and the day of week\n"
        "7. A one-line close that challenges him or holds him to something\n\n"
        "Format with clear emoji section headers. Under 450 words. "
        "Be a partner, not a cheerleader. If something looks off in the data, call it out."
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
    if tasks_data:
        tasks_section = f"\n✅ Open Tasks:\n{tasks_data}\n"
    # Friday: ask about week production
    weekly_checkin = ""
    if weekday == 4:
        weekly_checkin = (
            "\n📊 FRIDAY PRODUCTION CHECK: Ask Brady how the week actually went — "
            "appointments set, deals submitted, recruiting activity. Don't assume it went well. "
            "Get the real number and compare it to what he said Monday.\n"
        )
    # Wednesday: mid-week EMD pulse
    elif weekday == 2:
        weekly_checkin = (
            "\n📊 MID-WEEK CHECK: Ask Brady where he stands on production this week. "
            "Is he on pace? If not, what's the gap and what's he doing about it?\n"
        )
    prompt = (
        f"Generate a midday triage check-in for Brady. It's 1:00 PM ET on {day_str}.\n\n"
        "LIVE DATA:\n"
        f"📅 Today's full calendar:\n{calendar_data}\n"
        f"{tasks_section}"
        f"{memory_section}"
        f"{weekly_checkin}\n"
        "Brady's afternoon schedule:\n"
        "• 12–3 PM: Recruiting and training block (in progress)\n"
        "• 4–6 PM: Client appointments, leads, field training\n"
        "• After 6 PM: Personal time — Ace does not schedule work here\n\n"
        "Give Brady a tight midday check-in:\n"
        "1. Quick opener (1 line — direct, forward-looking, not a pep talk)\n"
        "2. ⚡ Afternoon Priority — the 2-3 most important moves for the 4-6 PM block\n"
        "3. ✅ Task Pulse — any tasks due today or overdue? Flag them without sugarcoating.\n"
        "4. 📋 Deal/Agent Update — pull any active deal or agent situations from memory and "
        "ask Brady for a status update. Don't reference stale specifics — ask what's live.\n"
        "5. 🕐 Calendar — anything left today that needs prep?\n"
        "6. One accountability line — something he committed to that he needs to follow through on\n\n"
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
        "1. One calm, affirming opener — acknowledge the day is done (no recaps, no urgency)\n"
        "2. 📌 Carry Forward — top 2-3 things to pick up first thing tomorrow (brief, not a list dump)\n"
        "3. 🌙 Wind Down — remind him to stretch, breathe, and actually disconnect. "
        "He grinds hard; recovery is part of the performance. Make it feel like permission.\n"
        "4. 💬 Open Floor — invite him to reflect on how the day went, what's on his mind, "
        "or just to talk. No agenda. This is his space to decompress before closing out.\n\n"
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
        " /status — check that I'm running\n"
        " /help — show this message\n\n"
        "You can also just text me anything — I'll respond and remember what matters.\n\n"
        "Auto check-ins: 9:30 AM brief · 1:00 PM triage · 7:00 PM wind-down (Mon–Fri)."
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
        " /status — confirm the bot is alive\n"
        " /help — this message\n\n"
        "Or just text me — I'll respond and remember anything useful.\n\n"
        "Schedule: 9:30 AM brief · 1:00 PM triage · 7:00 PM wind-down (Mon–Fri)"
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
        if not tasks:
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

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    now_et = datetime.now(EASTERN)
    memories = read_memory()
    memory_status = f"{len(memories)} items stored" if memories else "not yet activated"
    tasks_data = get_tasks()
    tasks_status = f"{len(tasks_data.splitlines())} open tasks" if tasks_data and tasks_data != "No open tasks." else "not active or no open tasks"
    await update.message.reply_text(
        f"✅ Ace is running.\n"
        f"Current time (ET): {now_et.strftime('%A %B %-d, %Y — %-I:%M %p')}\n"
        f"Schedule: 9:30 AM brief · 1:00 PM triage · 7:00 PM wind-down (Mon–Fri)\n"
        f"Memory: {memory_status}\n"
        f"Tasks: {tasks_status}"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-form text — Brady can chat with Ace, and Ace learns from it."""
    if not _is_authorized(update):
        return
    user_text = update.message.text.strip()
    if not user_text:
        return
    memories = read_memory()
    memory_context = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_context = f"\n\nWhat I already know about Brady:\n{memory_str}"
    system_with_memory = (
        SYSTEM_PROMPT
        + memory_context
        + "\n\nRespond to Brady's message directly and as a real business partner. "
        "If he's on track, confirm it. If something looks off, say so — don't soften it. "
        "If you don't know his current numbers or situation, ask rather than assume. "
        "If this message reveals something worth remembering for future briefings "
        "(a schedule change, business priority, team update, goal progress, etc.), "
        "append it at the very end of your reply using exactly this format:\n"
        "[MEMORY: brief fact to remember]\n"
        "Include 0–3 [MEMORY: ...] tags max. Skip tagging trivial or one-off chat."
    )
    try:
        response = _call_claude(
            [{"role": "user", "content": user_text}],
            max_tokens=500,
            system=system_with_memory,
        )
        memory_tags = re.findall(r'\[MEMORY:\s*(.+?)\]', response)
        clean_response = re.sub(r'\n?\[MEMORY:[^\]]+\]', '', response).strip()
        await update.message.reply_text(clean_response)
        if memory_tags:
            merged = _merge_memories(memory_tags, memories)
            if write_memory(merged):
                logger.info("Stored %d new memory item(s) from conversation.", len(memory_tags))
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
    app.add_handler(CommandHandler("status", cmd_status))

    # Free-text conversation handler (learns from every message)
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
    logger.info("Scheduler started — 9:30 AM brief · 1:00 PM triage · 7:00 PM wind-down (Mon–Fri ET).")

    logger.info("Ace v8 is starting up…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
