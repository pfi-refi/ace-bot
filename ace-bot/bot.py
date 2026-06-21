"""
Ace — Brady McGraw's Telegram business advisor bot.
Sends a morning briefing every weekday at 9:30 AM ET with live
Google Calendar events and Gmail unread summary, then calls Claude
to produce a prioritised daily brief.

v5: Three daily check-ins — 9:30 AM brief, 1:00 PM midday triage, 5:30 PM EOD sweep.
    Respects Brady's schedule blocks and actively protects personal time after 6 PM.
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

# ── Claude ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are Ace, Brady McGraw's sharp, concise business advisor. "
    "Brady is the Marketing Director and owner of Platinum Fortune Impact (PFI), "
    "a GFI Legends Base Shop in Summit County/Cleveland, Ohio. "
    "He leads ~18 licensed insurance and financial services agents. "
    "Primary products: Life Insurance, IUL, FIA/Annuities, Mortgage Protection, Final Expense. "
    "CRM: GoHighLevel. "
    "Keep briefings tight, direct, and actionable — wealth-advisor tone."
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
    """Generate today's morning brief using live Calendar, Gmail, and memory data."""
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")
    weekday = now_et.weekday()
    calendar_data = get_calendar_events()
    email_data = get_gmail_summary()
    memories = read_memory()
    day_reminders = {
        0: "Monday — Focus on team training, pipeline review, and admin work.",
        1: "Tuesday — Prioritise new lead follow-up and appointment setting.",
        2: "Wednesday — Mid-week pulse check on team activity and pipeline.",
        3: "Thursday — Push for end-of-week appointment closes.",
        4: "Friday — Wrap the week strong; prep Monday game plan.",
    }
    day_note = day_reminders.get(weekday, "")
    memory_section = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_section = f"\n📋 What I know about how Brady operates:\n{memory_str}\n"
    prompt = (
        f"Generate a morning briefing for Brady for {day_str}.\n\n"
        "LIVE DATA PULLED FROM HIS ACCOUNTS:\n"
        f"📅 Today's calendar:\n{calendar_data}\n\n"
        f"📧 Unread priority emails:\n{email_data}\n\n"
        f"📌 Day context: {day_note}\n"
        f"{memory_section}\n"
        "Brady's daily schedule to work with:\n"
        "• Mornings: deep work and Claude blocks\n"
        "• 12–3 PM: recruiting and training (protected block)\n"
        "• 4–6 PM: client appointments, leads, field training (protected)\n"
        "• After 6 PM: personal time — do not schedule work here\n\n"
        "Based on the real data above, give Brady:\n"
        "1. A brief warm opener (1 sentence)\n"
        "2. 🎯 Top 3 Focuses — the 3 most important things to act on today\n"
        "3. 📅 Calendar — clean list of his meetings/events today\n"
        "4. 📧 Attention — emails that need a reply or action (if any)\n"
        "5. 📌 Reminders — day-of-week reminders relevant to PFI operations\n"
        "6. A one-line close\n\n"
        "Format with clear emoji section headers. Under 400 words. Lead with what matters most."
    )
    return _call_claude([{"role": "user", "content": prompt}], max_tokens=700)

# ── Midday triage ──────────────────────────────────────────────────────────────

def build_midday_triage() -> str:
    """Generate 1 PM midday check-in — priority for afternoon block, deal pulse."""
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")
    calendar_data = get_calendar_events()
    memories = read_memory()
    memory_section = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_section = f"\n📋 Context about Brady:\n{memory_str}\n"
    prompt = (
        f"Generate a midday triage check-in for Brady. It's 1:00 PM ET on {day_str}.\n\n"
        "LIVE DATA:\n"
        f"📅 Today's full calendar:\n{calendar_data}\n\n"
        f"{memory_section}\n"
        "Brady's afternoon schedule:\n"
        "• 12–3 PM: Recruiting and training block (in progress)\n"
        "• 4–6 PM: Client appointments, leads, field training\n"
        "• After 6 PM: Personal time — Ace does not schedule work here\n\n"
        "Give Brady a tight midday check-in:\n"
        "1. Quick opener (1 line — energetic, forward-looking)\n"
        "2. ⚡ Afternoon Priority — the 2-3 most important things for the 4-6 PM block\n"
        "3. 📋 Deal Check-In — ask for updates on active deals "
        "(Augustar policy cancel, Ki Man law firm, Nina test July 2, Nevada licenses). "
        "Prompt him to update you on any movement.\n"
        "4. 🕐 Calendar — anything left on the calendar today that needs prep?\n"
        "5. One quick reminder to protect his energy — no hero grinding\n\n"
        "Under 250 words. Direct and sharp."
    )
    return _call_claude([{"role": "user", "content": prompt}], max_tokens=500)

# ── EOD sweep ─────────────────────────────────────────────────────────────────

def build_eod_sweep() -> str:
    """Generate 5:30 PM EOD wrap — carry-forwards, deal pulse, close the day."""
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")
    memories = read_memory()
    memory_section = ""
    if memories:
        memory_str = "\n".join(f"• {m}" for m in memories)
        memory_section = f"\n📋 Context about Brady:\n{memory_str}\n"
    prompt = (
        f"Generate an end-of-day sweep for Brady. It's 5:30 PM ET on {day_str}.\n\n"
        f"{memory_section}\n"
        "Give Brady a clean EOD wrap-up:\n"
        "1. Quick closer (1 line — acknowledge the day, close the loop)\n"
        "2. 📌 Carry Forward — top 3 things that carry to tomorrow morning\n"
        "3. 📋 Deal Pulse — quick check on active deals. Any updates Brady should log "
        "before closing out today?\n"
        "4. ⚠️ Urgents — anything that truly cannot wait until tomorrow? "
        "If none, explicitly say the slate is clear.\n"
        "5. 🌙 Close Out — after 6 PM is Brady's time. He grinds hard; "
        "remind him to actually close the laptop and recharge. "
        "Being productive means protecting recovery time too.\n\n"
        "Under 200 words. Warm but efficient."
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
        " /eod — end-of-day sweep on demand\n"
        " /remember <fact> — teach me something to keep in mind\n"
        " /memory — see what I know about how you operate\n"
        " /status — check that I'm running\n"
        " /help — show this message\n\n"
        "You can also just text me anything — I'll respond and remember what matters.\n\n"
        "Auto check-ins: 9:30 AM brief · 1:00 PM triage · 5:30 PM EOD sweep (Mon–Fri)."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "Ace commands:\n"
        " /brief — on-demand morning brief (live calendar + email)\n"
        " /triage — midday priority check-in\n"
        " /eod — end-of-day wrap and carry-forward\n"
        " /remember <fact> — store a fact in my memory\n"
        " /memory — view my current memory\n"
        " /status — confirm the bot is alive\n"
        " /help — this message\n\n"
        "Or just text me — I'll respond and remember anything useful.\n\n"
        "Schedule: 9:30 AM brief · 1:00 PM triage · 5:30 PM EOD (Mon–Fri)"
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
    await update.message.reply_text("⏳ Running EOD sweep…")
    try:
        brief = build_eod_sweep()
        await update.message.reply_text(brief)
    except Exception as e:
        logger.error("EOD command error: %s", e)
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
    await update.message.reply_text(
        f"✅ Ace is running.\n"
        f"Current time (ET): {now_et.strftime('%A %B %-d, %Y — %-I:%M %p')}\n"
        f"Schedule: 9:30 AM brief · 1:00 PM triage · 5:30 PM EOD (Mon–Fri)\n"
        f"Memory: {memory_status}"
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
        + "\n\nRespond to Brady's message directly and helpfully. "
        "If this message reveals something worth remembering for future briefings "
        "(a schedule change, business priority, preference, team update, etc.), "
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
    """Scheduled job — 5:30 PM ET EOD sweep."""
    try:
        logger.info("Sending EOD sweep…")
        brief = build_eod_sweep()
        await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=brief)
        logger.info("EOD sweep sent.")
    except Exception as e:
        logger.error("EOD sweep error: %s", e)
        try:
            await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=f"⚠️ EOD sweep failed: {e}")
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
        day_of_week="mon-fri", hour=17, minute=30, args=[app],
    )
    scheduler.start()
    logger.info("Scheduler started — 9:30 AM brief · 1:00 PM triage · 5:30 PM EOD (Mon–Fri ET).")

    logger.info("Ace v5 is starting up…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
