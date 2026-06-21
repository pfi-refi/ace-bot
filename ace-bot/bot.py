"""
Ace — Brady McGraw's Telegram business advisor bot.
Sends a morning briefing every weekday at 9:30 AM ET and responds to messages all day.
"""

import asyncio
import logging
import os
import signal
from collections import deque
from datetime import datetime

import anthropic
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from system_prompt import BRADY_SYSTEM_PROMPT

# ── Load env ──────────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
BRADY_CHAT_ID      = int(os.environ["BRADY_CHAT_ID"])

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Claude client ─────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
MODEL       = "claude-sonnet-4-6"   # falls back to claude-sonnet-4-5 on NotFoundError
MAX_HISTORY = 10                    # conversation pairs kept in memory

# ── Conversation history (in-memory, per session) ─────────────────────────────
# List of {"role": "user"/"assistant", "content": "..."}
# deque auto-drops oldest when exceeding MAX_HISTORY * 2 messages
conversation_history: deque = deque(maxlen=MAX_HISTORY * 2)

# ── Timezone ──────────────────────────────────────────────────────────────────
EASTERN = pytz.timezone("US/Eastern")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    """Only respond to Brady's chat ID."""
    return update.effective_chat.id == BRADY_CHAT_ID


def build_messages() -> list[dict]:
    """Return conversation history as a list for the Claude API."""
    return list(conversation_history)


def _call_claude(messages: list[dict], max_tokens: int = 1024) -> str:
    """Call Claude with automatic model fallback. Returns reply text."""
    try:
        response = claude.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=BRADY_SYSTEM_PROMPT,
            messages=messages,
        )
        return response.content[0].text
    except anthropic.NotFoundError:
        logger.warning("%s not found — falling back to claude-sonnet-4-5", MODEL)
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=max_tokens,
            system=BRADY_SYSTEM_PROMPT,
            messages=messages,
        )
        return response.content[0].text


def ask_claude(user_message: str) -> str:
    """Send a message to Claude with full conversation history. Returns reply."""
    conversation_history.append({"role": "user", "content": user_message})

    try:
        reply = _call_claude(build_messages())
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        reply = "⚠️ API error — check logs. Try again in a moment."

    conversation_history.append({"role": "assistant", "content": reply})
    return reply


def build_morning_brief() -> str:
    """Generate today's morning brief via Claude."""
    now_et  = datetime.now(EASTERN)
    day_str = now_et.strftime("%A, %B %-d")   # e.g. "Monday, June 23"
    weekday = now_et.weekday()                 # 0=Mon … 4=Fri

    # Day-specific standing reminders
    reminders: list[str] = []
    if weekday == 1:   # Tuesday
        reminders.append("📲 Lead Division starts today — runs Tue–Fri")
    elif weekday == 2:  # Wednesday
        reminders.append("📲 Lead Division running")
        reminders.append("📞 Team training call tonight at 8 PM ET — prep recognition + skill topic")
    elif weekday == 3:  # Thursday
        reminders.append("📲 Lead Division running")
    elif weekday == 4:  # Friday
        reminders.append("📲 Lead Division — last day of the week")
        reminders.append("📊 Good day to review weekly numbers before the weekend")

    reminder_block = "\n".join(reminders) if reminders else "No standing reminders for today."

    prompt = (
        f"Generate a morning briefing for Brady for {day_str}. "
        "Give him his top 3 most important focuses for TODAY — concrete and action-oriented, "
        "based on his current priorities (1-on-1s with Caleb/Lincoln/Walter, Nina training, "
        "Eli re-engagement, Indeed audit, recruit outreach, local event commitment, workshop planning). "
        "Keep it short and scannable — this is a Telegram message, not a report. "
        "Output EXACTLY this format, filling in the sections:\n\n"
        f"☀️ Good morning Brady — here's your Ace brief for {day_str}\n\n"
        "🎯 Top 3 focuses for today:\n"
        "1. [focus]\n"
        "2. [focus]\n"
        "3. [focus]\n\n"
        f"📋 Reminders:\n{reminder_block}\n\n"
        "💬 Message me anything — deals, ideas, team questions, prioritization. I've got you."
    )

    try:
        return _call_claude([{"role": "user", "content": prompt}], max_tokens=512)
    except anthropic.APIError as e:
        logger.error("Claude API error in morning brief: %s", e)
        return (
            f"☀️ Good morning Brady — here's your Ace brief for {day_str}\n\n"
            "⚠️ Couldn't reach Claude for today's brief. Check API key / connectivity.\n\n"
            f"📋 Reminders:\n{reminder_block}\n\n"
            "💬 Message me anything — I've got you."
        )


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED JOB
# ─────────────────────────────────────────────────────────────────────────────

async def send_morning_brief(application: Application) -> None:
    """APScheduler job: send morning brief to Brady at 9:30 AM ET Mon–Fri."""
    logger.info("Generating morning brief…")
    brief = build_morning_brief()
    try:
        await application.bot.send_message(chat_id=BRADY_CHAT_ID, text=brief)
        logger.info("Morning brief sent.")
    except Exception as e:
        logger.error("Failed to send morning brief: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — welcome message."""
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "👋 Hey Brady — Ace is online.\n\n"
        "I'll send you a morning brief every weekday at 9:30 AM ET. "
        "Message me anytime for prioritization, deal questions, team stuff, or strategy.\n\n"
        "Commands:\n"
        "/brief — trigger a manual brief right now\n"
        "/reset — clear conversation history\n\n"
        "What do you need?"
    )


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/brief — manually trigger a morning brief."""
    if not is_authorized(update):
        return
    await update.message.reply_chat_action(ChatAction.TYPING)
    brief = build_morning_brief()
    await update.message.reply_text(brief)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset — clear conversation history."""
    if not is_authorized(update):
        return
    conversation_history.clear()
    await update.message.reply_text("🔄 Conversation history cleared. Fresh start.")


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any text message from Brady — send to Claude, return response."""
    if not is_authorized(update):
        logger.warning(
            "Unauthorized message from chat_id=%s — ignored.",
            update.effective_chat.id,
        )
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    logger.info("Message from Brady: %.80s…", user_text)
    await update.message.reply_chat_action(ChatAction.TYPING)

    reply = ask_claude(user_text)
    await update.message.reply_text(reply)


# ─────────────────────────────────────────────────────────────────────────────
# ERROR HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors from the dispatcher."""
    logger.error(
        "Update %s caused error: %s", update, context.error, exc_info=context.error
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN — async so AsyncIOScheduler starts on the live event loop
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("Starting Ace bot…")

    # Build the Telegram application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("brief", cmd_brief))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_error_handler(error_handler)

    # ── Scheduler (started here so it binds to the running asyncio event loop) ─
    scheduler = AsyncIOScheduler(timezone=EASTERN)
    scheduler.add_job(
        send_morning_brief,
        trigger="cron",
        day_of_week="mon-fri",
        hour=9,
        minute=30,
        kwargs={"application": application},
        id="morning_brief",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — morning brief runs Mon–Fri 9:30 AM ET.")

    # ── Start polling ──────────────────────────────────────────────────────────
    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,   # ignore messages sent while bot was offline
    )
    logger.info("Ace is running. Send /start in Telegram to begin.")

    # ── Block until SIGINT or SIGTERM (Railway sends SIGTERM to stop) ──────────
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    # ── Graceful shutdown ──────────────────────────────────────────────────────
    logger.info("Shutting down Ace…")
    scheduler.shutdown(wait=False)
    await application.updater.stop()
    await application.stop()
    await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
