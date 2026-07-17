# MIRROR OF ace-portal/backend/google_client.py — copied verbatim, not rewritten.
# These Google integrations are battle-tested (originally ported from
# ace-bot/bot.py) and are deliberately reused rather than rebuilt.
# Railway root dirs are per-service, so a shared package would force both
# services to root at the repo root; copying is the cheaper trade.
# Delete the ace-portal copy once Ace 2.0 fully replaces the portal.
"""
Shared Google auth + constants for the Ace Portal backend.

Ported directly from ace-bot/bot.py (get_google_creds) — do NOT rewrite the
credential flow. Credentials come from Railway env vars ONLY. Never hardcode
GOOGLE_TOKEN_JSON / GOOGLE_CREDENTIALS_JSON or any secret.
"""

import json
import logging
import os

import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

logger = logging.getLogger("ace_portal.google")

# ── Constants (shared with bot.py) ──────────────────────────────────────────────
EASTERN = pytz.timezone("America/New_York")

# calendar_id is ALWAYS explicit — never a bare variable default. Ace only writes
# to Brady's PFI calendar. (Matches bot.py security rule.)
PFI_CALENDAR_ID = "pfi@platinumfortuneimpact.com"

MEMORY_FILE_NAME = "ace_memory.json"
CONVERSATION_FILE_NAME = "ace_conversation.json"

# Task-list config (ported from bot.py) ─────────────────────────────────────────
REFERENCE_LISTS = {"Business cost - NO TOUCH", "To learn / Questions"}
MORNING_SKIP_LISTS = REFERENCE_LISTS | {"🏠 Personal", "🏆 Goals"}
DEFAULT_TASK_LIST = "Brain Dump"


def get_google_creds() -> Credentials:
    """Build Google OAuth credentials from Railway env vars, refreshing if expired.

    Ported verbatim from bot.py. Reads GOOGLE_TOKEN_JSON — the SAME env var the
    Telegram bot uses. Do not add a new Railway var; it is shared with bot.py.
    """
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
