"""
Ace — Brady McGraw's Telegram business advisor bot.
Sends a morning briefing every weekday at 9:30 AM ET with live
Google Calendar events and Gmail unread summary, then calls Claude
to produce a prioritised daily brief.
"""

import json
import logging
import os
from datetime import datetime

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
EASTERN = pytz.timezone("America/New_York")
AUTHORIZED_USER_ID = 8681823830  # Brady's Telegram chat ID — security filter

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


# ── Gmail ─────────────────────────────────────────────────────────────────────

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
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in msg_data.get("payload", {}).get("headers", [])
            }
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


# ── Claude ────────────────────────────────────────────────────────────────────

def _call_claude(messages: list, max_tokens: int = 700) -> str:
    """Call the Claude API and return the text response."""
    import anthropic  # imported here to avoid top-level startup cost

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=(
            "You are Ace, Brady McGraw's sharp, concise business advisor. "
            "Brady is the Marketing Director and owner of Platinum Fortune Impact (PFI), "
            "a GFI Legends Base Shop in Summit County/Cleveland, Ohio. "
            "He leads ~18 licensed insurance and financial services agents. "
            "Primary products: Life Insurance, IUL, FIA/Annuities, Mortgage Protection, Final Expense. "
            "CRM: GoHighLevel. "
            "Keep briefings tight, direct, and actionable — wealth-advisor tone."
        ),
        messages=messages,
    )
    return response.content[0].text


# ── Morning brief ─────────────────────────────────────────────────────────────

def build_morning_brief() -> str:
    """Generate today's morning brief using live Calendar and Gmail data."""
    now_et = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")
    weekday = now_et.weekday()  # 0=Mon … 4=Fri

    # Pull live data
    calendar_data = get_calendar_events()
    email_data = get_gmail_summary()

    # Day-specific reminders
    day_reminders = {
        0: "Monday — Focus on team training, pipeline review, and admin work.",
        1: "Tuesday — Prioritise new lead follow-up and appointment setting.",
        2: "Wednesday — Mid-week pulse check on team activity and pipeline.",
        3: "Thursday — Push for end-of-week appointment closes.",
        4: "Friday — Wrap the week strong; prep Monday game plan.",
    }
    day_note = day_reminders.get(weekday, "")

    prompt = (
        f"Generate a morning briefing for Brady for {day_str}.\n\n"
        "LIVE DATA PULLED FROM HIS ACCOUNTS:\n"
        f"📅 Today's calendar:\n{calendar_data}\n\n"
        f"📧 Unread priority emails:\n{email_data}\n\n"
        f"📋 Day context: {day_note}\n\n"
        "Based on the real data above, give Brady:\n"
        "1. A brief warm opener (1 sentence)\n"
        "2. 🎯 Top 3 Focuses — the 3 most important things to act on today, based on his calendar and emails\n"
        "3. 📅 Calendar — clean list of his meetings/events today\n"
        "4. 📧 Attention — emails that need a reply or action (if any)\n"
        "5. 📋 Reminders — any standing day-of-week reminders relevant to PFI operations\n"
        "6. A one-line motivational close\n\n"
        "Format with clear emoji section headers. Keep it tight — under 400 words total. "
        "Lead with what matters most. No fluff."
    )

    return _call_claude([{"role": "user", "content": prompt}], max_tokens=700)


# ── Security check ────────────────────────────────────────────────────────────

def _is_authorized(update: Update) -> bool:
    return update.effective_chat.id == AUTHORIZED_USER_ID


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "👋 Ace is online.\n\n"
        "Commands:\n"
        "  /brief — morning briefing right now\n"
        "  /status — check that I'm running\n"
        "  /help — show this message\n\n"
        "Automated brief fires at 9:30 AM ET, Mon–Fri."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "Ace commands:\n"
        "  /brief — on-demand morning brief (live calendar + email)\n"
        "  /status — confirm the bot is alive\n"
        "  /help — this message"
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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    now_et = datetime.now(EASTERN)
    await update.message.reply_text(
        f"✅ Ace is running.\n"
        f"Current time (ET): {now_et.strftime('%A %B %-d, %Y — %-I:%M %p')}\n"
        f"Auto-brief: 9:30 AM ET, Mon–Fri"
    )


# ── Scheduler job ─────────────────────────────────────────────────────────────

async def send_morning_brief(app: Application) -> None:
    """Scheduled job — build the brief and push to Brady's chat."""
    try:
        logger.info("Sending scheduled morning brief…")
        brief = build_morning_brief()
        await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=brief)
        logger.info("Morning brief sent.")
    except Exception as e:
        logger.error("Scheduled brief error: %s", e)
        try:
            await app.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"⚠️ Morning brief failed: {e}",
            )
        except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    # Register commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("status", cmd_status))

    # Scheduler — 9:30 AM ET, Mon–Fri
    scheduler = AsyncIOScheduler(timezone=EASTERN)
    scheduler.add_job(
        send_morning_brief,
        trigger="cron",
        day_of_week="mon-fri",
        hour=9,
        minute=30,
        args=[app],
    )
    scheduler.start()
    logger.info("Scheduler started — brief fires at 9:30 AM ET, Mon–Fri.")

    logger.info("Ace is starting up…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
