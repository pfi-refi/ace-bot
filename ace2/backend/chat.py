"""
Ace 2.0 chat turn — streaming, with real tool use.

A manual streaming loop over the Anthropic Messages API (not the Tool Runner):
the runner is great for headless agents, but here the harness IS the product —
we emit a custom WebSocket event the instant each tool runs, so the HUD orb can
show "◈ CREATING EVENT…" between bursts of text. That interleaving is exactly
the control a manual loop gives cleanly.

WS event protocol (matches app.js):
    start        — a turn began
    delta {text} — a chunk of Ace's reply
    tool  {name,label,status} — a tool is running / finished (JARVIS moment)
    confirmation {text}       — a tool's result line (also pushed to intel feed)
    final {text} — the complete reply text
    error {text}
    done

Design decisions carried in from the portal's scars:
  • Live context is fetched CONCURRENTLY (asyncio.gather over to_thread), never
    six sequential blocking calls. Fatal for realtime voice otherwise.
  • Conversation history is READ from the shared Drive file for continuity and
    sanitized before it touches the API; 2.0 never writes that file (brain.py).
  • Actions execute mid-turn, BEFORE the final text — so the confirmation is true
    by the time Ace says it (the portal ran tags after emitting the reply).
  • No tags anywhere. The prompt describes tools; there's nothing to regress into.
"""

import asyncio
import json
import logging
import os
import uuid
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from anthropic import AsyncAnthropic

from . import brain, capabilities, connectors, daybank, history, tools
from . import tasks as tasks_mod
from . import ops
from .integrations.calendar_api import (
    get_events_structured,
    get_tomorrow_events,
)
from .integrations import mcp_client
from .integrations.tasks_api import (
    add_task, get_gmail_summary, get_inbox_structured, get_personal_inbox_structured,
    get_task_lists_grouped, get_tasks, get_tasks_structured,
)
from .integrations.weather import get_weather
from .system_prompt import build_system_prompt

logger = logging.getLogger("ace2.chat")

EASTERN = pytz.timezone("America/New_York")
# LEAN MODE (2026-08-03, money tight): typed brain on Sonnet 5 — intro pricing $2/$10
# through 2026-08-31 (vs Opus $5/$25 = 60%+ cheaper), near-Opus quality for chief-of-staff
# work. Revert when cash flows: set ACE2_MODEL=claude-opus-4-8 in Railway or flip this default.
MODEL = os.environ.get("ACE2_MODEL", "claude-sonnet-5")
# Live VOICE replies run on the FASTEST model, not Opus — conversational snappiness
# (time-to-first-word) matters far more than depth per spoken sentence, and Opus's
# thinking latency is the main thing that makes voice feel laggy. Typed stays on MODEL.
# Bump to claude-sonnet-5 via env if voice needs more reasoning per turn.
# 2026-08-24 REVERTED same day: Sonnet-5 on voice made first-token latency exceed ElevenLabs'
# cutoff on longer turns — calls died SILENT (Brady 16:48-16:50: update never arrived, zero
# replies to follow-ups). Haiku restored: TALKING is the product. Money-precision lives in
# prompt rules 7c/7d (model-agnostic); complex money math belongs in typed chat / the sheet.
# If we ever retry a bigger voice model: shrink the voice context first + verify EL timeout.
VOICE_MODEL = os.environ.get("ACE2_VOICE_MODEL", "claude-haiku-4-5-20251001")
# The background LEARNING/TRIAGE sweep + briefs + graph + nudges are NOT latency-bound (they
# run off the live path), so they don't need the premium typed brain (Opus, $5/$25). Sonnet 5
# ($3/$15, cheaper still on intro pricing) is near-Opus quality and cut Brady's API bill ~40-60%
# on all this invisible background work — the typed CHAT he actually interacts with stays MODEL.
# Haiku was too weak here (a dense brain dump → 0 facts, most dropped); Sonnet 5 is the floor.
# Override with ACE2_LEARN_MODEL (e.g. back to claude-opus-4-8) to trade cost for depth.
# LEAN MODE: ALL background work (sweep, briefs, watchdog, recap, graph) rides this one
# knob — Haiku 4.5 ($1/$5) does structured triage/recap work fine. Was Sonnet 5.
LEARN_MODEL = os.environ.get("ACE2_LEARN_MODEL", "claude-haiku-4-5-20251001")
# Voice may PREPARE outward actions. Execution requires the explicit Review tray.
# Tool schemas remain stable for cache reuse.
# Source reading is a TYPED-path activity: it returns dozens of lines to reason over, which
# is the opposite of what a live call needs, and the voice brain is the small fast model.
# Brady asking "why was the brief off?" out loud still works — Ace answers from context and
# can read the code properly when he is back at the keyboard.
_VOICE_TOOL_DENY = {"read_own_code"}

# ---- Explicit action review: user inspects immutable payload in Review UI. ----
_CONFIRM_ALWAYS = {
    "send_email", "mcp_send_gmail_message",   # outbound to third parties
    "delete_calendar_event",                  # destroys a meeting
    "mcp_get_drive_shareable_link",           # outward data exposure
    "update_profile",                         # standing identity/mission change — Brady sees it first
}
_DESTRUCTIVE_ACTIONS = {"delete", "remove", "clear", "cancel", "trash"}
_DESTRUCTIVE_HINTS = ("delete", "remove", "clear", "trash", "cancel")
_SHARE_UPDATES = {"all", "externalonly", "true", "1"}
_turn_seq = [0]
_turn_user_text = [""]

def _next_turn_id():
    _turn_seq[0] += 1
    return _turn_seq[0]


def _strip_confirm(args: dict) -> dict:
    return {k: v for k, v in args.items() if k not in ("confirmed", "confirm_token")}


def _needs_confirm(name: str, args: dict) -> bool:
    if name in _CONFIRM_ALWAYS:
        return True
    a = _strip_confirm(args)
    if name in ("mcp_manage_event", "mcp_manage_task"):
        action = str(a.get("action", "")).strip().lower()
        if action:
            # Trust the explicit action verb — 'create'/'update' a task named
            # "Cancel Comcast" must NOT trip a destructive hint.
            if action in _DESTRUCTIVE_ACTIONS:
                return True
            return str(a.get("send_updates", "")).strip().lower() in _SHARE_UPDATES
        # No action given → sniff the whole payload as a fallback.
        blob = json.dumps(a, default=str).lower()
        return any(h in blob for h in _DESTRUCTIVE_HINTS)
    if name == "mcp_modify_sheet_values":
        return bool(a.get("clear_values"))   # clearing a range destroys data
    if name == "mcp_modify_doc_text":
        return True   # overwrites document content
    return False


def _confirm_gate(name: str, args: dict, turn_id: int):
    """Gated actions only execute from the authenticated review endpoint."""
    clean = _strip_confirm(args)
    if not _needs_confirm(name, args):
        return False, clean
    # All outward/destructive requests require an explicit decision in Review.
    return True, clean

EFFORT = os.environ.get("ACE2_EFFORT", "low")   # low|medium|high|xhigh|max — LEAN MODE: was medium
# EFFORT PER ROUTE (Phase 6 step 5, 2026-09-06). Auditing this found it is ALREADY route-
# scoped: output_config is set on the typed path only, and the background passes (learn
# sweep, briefs, the dedup judge, the bucket pass) never pass one. So the remaining question
# is only what a TYPED turn deserves, and that is not a saving — raising it RAISES the bill.
# It is Brady's money, so the knob ships defaulted to today's behaviour and the decision is
# his: ACE2_EFFORT_TYPED=medium costs more per typed turn and buys deeper reasoning on the
# turns where he is actually working a deal. Nothing changes until he sets it.
EFFORT_TYPED = os.environ.get("ACE2_EFFORT_TYPED", EFFORT).strip().lower()
MAX_TOKENS = int(os.environ.get("ACE2_MAX_TOKENS", "16000"))  # ceiling covers thinking+tools+prose; only billed if used
MAX_TOOL_ITERS = 8
NOW_WINDOW_MIN = 90  # an event that started within this many minutes reads as "in progress"

# ── DISCREET MODE (2026-08-11, Brady) — when he's around people, Ace must not AIR OUT his
# finances out loud. Single-user setting, stored in the db (kind='setting_discreet') and mirrored
# in this process flag for free reads. Set via POST /settings/discreet; loaded at boot.
_discreet = [False]


def load_discreet() -> bool:
    try:
        from . import db
        if db.enabled():
            _discreet[0] = (db.latest_summary("setting_discreet").get("text") or "off") == "on"
    except Exception:
        pass
    return _discreet[0]


def set_discreet(on: bool) -> bool:
    _discreet[0] = bool(on)
    try:
        from . import db
        if db.enabled():
            db.add_summary("on" if on else "off", "setting_discreet")
    except Exception:
        pass
    return _discreet[0]


# ── EDITABLE PROFILE — who Brady is + Ace's mission, OUT of the hardcoded prompt ─────────
# (2026-08-23, Brady: "pull out the hard coded information and make it editable... better
# oversight on who he is to me and who I am.") The system prompt now carries only OPERATING
# RULES; this profile is the authoritative who/what, stored as a summary row ('ace_profile')
# so Brady or Ace (update_profile tool, confirm-gated) can rewrite it without a deploy.
# DEFAULT below = current reality; used until a db row exists (no db write at boot).
DEFAULT_PROFILE = (
    "WHO BRADY IS: Brady McGraw — builder/operator with several ventures in motion at once: "
    "(1) financial services — personal insurance clients (annuity/IUL) + closing out his "
    "remaining GFI business (~$2,100 commissions still to collect); PFI is his entity (renewal "
    "decision Sept 13). (2) Concrete + business owners — pours with Damon Gantz and helps him "
    "grow the business; more owner-clients to come. (3) Income floor — landing stable $3-4K/mo "
    "(job applications, contract/CRM work). (4) Evaluating opportunities on their merits — "
    "currently Chris's recruiting offer (structured $10-12K debt, book doesn't transfer; "
    "decision pending after the Wednesday Zoom).\n"
    "MONEY CONTEXT: digging out of a real hole — bills and due-dates, the debt snowball, IRS/"
    "tax work with Ken Weinberg (EA). Vital CONTEXT and the lens for money decisions — but NOT "
    "his whole identity and not your only mission.\n"
    "HIS PEOPLE: Gabby — fiancée and full partner; money is shared and discussed openly; the "
    "trust rebuild is real, keep the tone warm and straight. Damon — close friend + business "
    "partner (concrete; wedding Sept 6).\n"
    "WHO YOU ARE TO HIM: his SECOND BRAIN, FULL STOP — not boxed into any one business or "
    "'mode'. Whatever is live — personal, GFI, Damon, job hunt, Gabby, a brand-new venture — "
    "you track it, prioritize HIM, and help him move on it. Big-picture partner AND "
    "detail-keeper, with real banter."
)
_profile_cache = {"text": None, "ts": 0.0}


def load_profile() -> str:
    """Current profile text: db override when set, DEFAULT_PROFILE otherwise. 60s TTL cache so
    voice's latency-sensitive context build never waits on a fresh db read."""
    now = time.time()
    if _profile_cache["text"] is not None and (now - _profile_cache["ts"]) < 60:
        return _profile_cache["text"]
    text = DEFAULT_PROFILE
    try:
        from . import db
        if db.enabled():
            stored = (db.latest_summary("ace_profile").get("text") or "").strip()
            if stored:
                text = stored
    except Exception:
        pass
    _profile_cache.update(text=text, ts=now)
    return text


def set_profile(text: str) -> bool:
    text = (text or "").strip()
    if len(text) < 200:   # refuse a wipe — a real profile is 150+ words; this short is a mistake
        return False
    try:
        from . import db
        if db.enabled() and db.add_summary(text, "ace_profile"):
            _profile_cache.update(text=text, ts=time.time())
            return True
    except Exception:
        pass
    return False


def _profile_block() -> str:
    return ("★ YOUR PROFILE — WHO BRADY IS & YOUR MISSION (editable + authoritative; when his "
            "life shifts, update it with update_profile):\n" + load_profile())


def _discreet_note() -> str:
    """Behavior directive injected into live context when Discreet Mode is on."""
    if not _discreet[0]:
        return ""
    return (
        "\n\n★ PRIVACY / DISCREET MODE IS CURRENTLY ON (state = ON). Brady may be around other "
        "people, so do NOT say exact dollar amounts, balances, debts, or financial specifics OUT "
        "LOUD on a spoken turn. When he asks about money by voice, OFFER first — naturally, e.g. "
        "\"want me to read them out, or just put them on your screen?\" — and use display_card to "
        "show them silently if he'd rather. Read figures aloud ONLY if he says yes or that he's "
        "private. TYPED replies MAY include the numbers (he's reading, not broadcasting). "
        "TURNING IT OFF: privacy is ON right now — if Brady says 'normal mode', 'turn it off', "
        "'I'm alone now', or 'you can say my numbers', you MUST call set_privacy(on=false) to "
        "actually flip it — do NOT just claim it's already off. Non-financial talk is normal."
    )


_PRIV_OFF = ("normal mode", "private mode off", "privacy off", "privacy is off",
             "turn off private", "turn private off", "turn off privacy",
             "you can say my numbers", "not private anymore")
_PRIV_ON = ("private mode", "go private", "privacy mode", "privacy on", "discreet mode",
            "don't say my numbers", "dont say my numbers", "keep my numbers quiet")


def maybe_toggle_privacy(text: str):
    """DETERMINISTIC Discreet-Mode toggle (2026-08-11): a privacy request must never depend on
    the model choosing the tool. Match the magic phrases and flip the setting directly, BEFORE
    the turn's context is built — so state is guaranteed correct and the directive updates the
    same turn. OFF is checked first so 'private mode off' turns it off, not on."""
    t = (text or "").lower()
    if any(p in t for p in _PRIV_OFF):
        set_discreet(False)
        return "off"
    if any(p in t for p in _PRIV_ON):
        set_discreet(True)
        return "on"
    return None

_client = None


def _format_today_schedule(events: list, now: datetime) -> str:
    """Split today's structured events into done / now / next / later for the model.

    `events` are get_events_structured() dicts (each has an Eastern-aware `iso` and a
    formatted `time`). Rendering the day relative to `now` is what lets Ace reason in
    "what's already happened vs. what's next" terms instead of seeing a flat list.
    """
    timed, all_day = [], []
    for e in events:
        if e.get("all_day"):
            all_day.append(e)
            continue
        try:
            start = datetime.fromisoformat(e["iso"])
        except (ValueError, KeyError, TypeError):
            continue
        timed.append((start, e))
    timed.sort(key=lambda x: x[0])

    past = [(s, e) for s, e in timed if s <= now]
    upcoming = [(s, e) for s, e in timed if s > now]

    # The most-recent already-started event counts as "in progress" if it began
    # within the window (structured events carry no end time, so this approximates).
    now_item, done = None, list(past)
    if past and (now - past[-1][0]) <= timedelta(minutes=NOW_WINDOW_MIN):
        now_item, done = past[-1], past[:-1]

    lines = []
    if done:
        lines.append("Already done earlier today:")
        lines += [f"  ✓ {e['time']} — {e['title']}" for _, e in done]
    if now_item:
        lines.append("Happening now (started recently):")
        lines.append(f"  ▸ {now_item[1]['time']} — {now_item[1]['title']}")
    if upcoming:
        lines.append("NEXT UP:")
        lines.append(f"  → {upcoming[0][1]['time']} — {upcoming[0][1]['title']}")
        if len(upcoming) > 1:
            lines.append("Later today:")
            lines += [f"  • {e['time']} — {e['title']}" for _, e in upcoming[1:]]
    if all_day:
        lines.append("All day:")
        lines += [f"  • {e['title']}" for e in all_day]

    return "\n".join(lines) if lines else "(nothing on the calendar today)"


# ── 1-HOUR PROMPT CACHE (Phase 6 step 4, 2026-09-06) ───────────────────────────────
# WHY IT BROKE ON 3 AUG, precisely: `ttl` on a cache_control block requires the
# extended-cache-ttl-2025-04-11 beta, and `betas` is a parameter of client.BETA.messages —
# it does not exist on client.messages. Passing ttl on the standard endpoint is rejected, so
# every call failed at once. Verified against the installed SDK (0.125.0), not remembered.
#
# WHY IT IS WORTH REDOING. The cached prefix is now ~20k tokens (system prompt + tool schemas
# + the memory block added in step 1). At a 5-minute TTL that prefix is re-WRITTEN several
# times a day at 1.25x rate; at an hour it is written a fraction as often. This is the
# largest remaining lever, and bigger than it was before step 1 put memory in the prefix.
#
# AND IT IS PREFLIGHTED. Something that once broke every call does not get shipped on faith:
# one tiny call at boot proves the beta is accepted before any of Brady's turns depend on it.
# If it fails, the process falls back to the plain 5-minute cache and says so in the log —
# degraded, never broken. ACE2_CACHE_TTL=5m disables it without a deploy.
_TTL_BETA = "extended-cache-ttl-2025-04-11"
_CACHE_TTL = os.environ.get("ACE2_CACHE_TTL", "1h").strip().lower()
_ttl_ok = [False]        # set by the preflight; False ⇒ standard endpoint, 5-minute cache


def _cc(ttl: bool = True) -> dict:
    """A cache_control block: 1h when the preflight passed, otherwise the plain 5m one."""
    return ({"type": "ephemeral", "ttl": "1h"} if (ttl and _ttl_ok[0])
            else {"type": "ephemeral"})


async def preflight_cache_ttl() -> bool:
    """Prove the 1h cache beta is accepted BEFORE any real turn relies on it."""
    if _CACHE_TTL != "1h":
        logger.info("cache ttl: 5m (ACE2_CACHE_TTL=%s)", _CACHE_TTL)
        return False
    try:
        client = _anthropic()
        await client.beta.messages.create(
            model=VOICE_MODEL, max_tokens=1, betas=[_TTL_BETA],
            system=[{"type": "text", "text": "ok",
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            messages=[{"role": "user", "content": "hi"}])
        _ttl_ok[0] = True
        logger.info("cache ttl: 1h ENABLED (beta %s accepted)", _TTL_BETA)
        return True
    except Exception as e:
        _ttl_ok[0] = False
        logger.warning("cache ttl: 1h REFUSED (%s) — staying on the 5m cache", e)
        return False


def _anthropic() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _is_meta_fact(f: str) -> bool:
    """Ace's own dev-backlog + save-narration — NOT a durable fact about Brady. The audit found
    ~23% of the store was 'ACE SELF-NOTE:' + assistant save-confirmations, and the recent tail was
    dominated by them, diluting every per-turn context. Filter them out of the LIVE context only
    (the brief's self-note path and recall still read the raw store)."""
    s = (f or "").strip().lower()
    # "Learning sweep complete. Saved three updates: <five unrelated people in one sentence>"
    # was a NEAR MISS on 'memory sweep complete' and is the single worst blending vector in
    # the store: one row that names the aunt, Thiami, Rebecca and Damon together, which no
    # amount of grouping can separate because the entities are already welded into one
    # sentence (Codex, 8 Sept: "avoid concatenated sweep narratives as authoritative
    # workflow facts"). It is a record of Ace SAVING things, not a fact about Brady.
    # Filtered from the LIVE context only — the row stays in the store and in recall.
    if re.match(r"^(learning|memory|nightly|background)\s+sweep\s+complete", s):
        return True
    if re.match(r"^saved\s+(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
                r"(update|memory|memories|fact|facts|file|files|note|notes)", s):
        return True
    return (s.startswith("ace self-note")
            or s.startswith("saved ")
            or s.startswith("memory sweep complete")
            or s.startswith("noting ")
            or "new memory files written" in s)


# ── KEEPING ONE PERSON'S FACTS TOGETHER ────────────────────────────────────────
# Codex's 8 Sept evaluation reproduced two failures on BOTH the deployed and the proposed
# prompt: Thiami's DC wet-signature requirement and Rebecca's Thursday mailing date were
# each attached to the AUNT's signature case, and an explicit correction — "wait to confirm
# with my uncle not Damon" — was ignored even though it was present in the input. So neither
# failure is "the fact was missing"; both are "the facts arrived as one undifferentiated
# stream of 110 bullets and the model had nothing holding them apart."
#
# This groups the SAME facts under the entity each one NAMES. It is deliberately an index by
# literal mention, not a judgement about what a fact is "really about" — that judgement is
# exactly what put "Ace Ready Mix" in the Ace lane and read a defects report as a waiting
# item. A fact naming two people is listed under BOTH, which is lossless and makes the
# ambiguity visible instead of hiding it. Nothing is dropped, reordered within a group, or
# rewritten; the store is untouched.
_ENTITY_STOP = {
    # Brady, his companies, and the words that look like names but never are.
    "brady", "ace", "pfi", "gfi", "the", "and", "for", "with", "his", "her", "their",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "irs", "llc", "cfi", "fta", "dns", "eod",
    # process nouns that recur constantly and would each earn a meaningless heading
    "completed", "income", "plan", "note", "update", "corrected", "learning", "saved",
    "deal", "deals", "goal", "goals", "task", "tasks", "call", "calls", "meeting",
    "google", "drive", "sheet", "board", "memory", "brief", "week", "today", "tomorrow",
    "morning", "evening", "night", "status", "next", "new", "old", "final", "total",
    "paid", "pay", "payment", "commission", "license", "test", "exam", "form", "packet",
    "everything", "anything", "something", "nothing", "one", "two", "equal", "balance",
    "groundworks", "personal", "side", "work", "money", "bills", "admin", "business",
}
# Relationship words Brady uses as names ("my uncle", "Syanna's aunt Ne").
_RELATIONS = ("uncle", "aunt", "mom", "dad", "mother", "father", "brother", "sister",
              "grandpa", "grandma", "wife", "girlfriend")
_NAME_RE = re.compile(r"\b([A-Z][a-z]{2,})\b")
# How Brady's board rows start. These are actions, never people.
_IMPERATIVES = {
    "find", "ask", "build", "review", "talk", "follow", "get", "call", "send", "pay",
    "set", "check", "make", "order", "finish", "mark", "add", "update", "look", "figure",
    "redo", "sort", "book", "email", "text", "confirm", "schedule", "start", "stop",
    "keep", "move", "put", "take", "give", "run", "read", "write", "draft", "prep",
    "clean", "fix", "close", "open", "sell", "buy", "help", "reach", "touch", "wait",
    # past-tense forms show up mid-fact ("Left a message. Called again later")
    "called", "sent", "asked", "emailed", "texted", "moved", "added", "updated",
    "checked", "finished", "started", "left", "drafted", "reviewed", "ordered",
    "confirmed", "scheduled", "booked", "signed", "received", "delayed", "waiting",
    "pending", "cleared", "submitted", "approved", "declined", "resent", "holding",
}


def _entities_in(fact: str) -> list:
    """Entity names this fact literally names. Mention-indexing only — no inference about
    what the fact is 'really about', because that judgement is what put 'Ace Ready Mix' in
    the Ace lane and read a defects report as a waiting item."""
    out = []
    low = (fact or "").lower()
    for r in _RELATIONS:
        if re.search(r"\b" + r + r"\b", low):
            out.append(r.capitalize())
    # Board rows open with an imperative — "Find a time with Damon", "Ask Tony" — and reading
    # those as people gave a roster of Find/Ask/Build/Review and pulled 76 unrelated facts
    # into the window. Blanking every sentence-initial capital fixed that but ALSO lost real
    # names that legitimately start a sentence ("Marlow paramed scheduled…"), so the verbs
    # are named instead of the position being blamed.
    for m in _NAME_RE.findall(fact or ""):
        low = m.lower()
        if low in _ENTITY_STOP or low in _IMPERATIVES or m in out:
            continue
        out.append(m)
    return out[:4]          # a fact naming half the roster is not about any of them


def _fact_ts(meta: dict, f: str) -> str:
    """Full stored timestamp for SORTING, or '' when unknown. Never guessed.

    Truncating to ten characters lost same-day resolution: a 09:00 note and a 17:00
    correction both became '2026-09-08', and the stable sort then fell back to the
    importance order the read path had given them — so the evening correction could print
    before the morning note it superseded (Codex, 8 Sept).
    """
    m = (meta or {}).get(f) or {}
    return str(m.get("ts") or "")


def _fact_stamp(meta: dict, f: str) -> str:
    """What the reader sees. Minutes are shown when known, so two updates on the same day
    are distinguishable. This is when the fact was RECORDED, not when the thing happened —
    writing down an old event today does not make its content new."""
    ts = _fact_ts(meta, f)
    if not ts:
        return "undated"
    day, _, rest = ts.partition("T")
    clock = rest[:5]
    return f"recorded {day} {clock}".rstrip() if clock and clock != "00:00" else f"recorded {day}"


def _group_facts(mem_list: list, min_facts: int = 3, meta: dict = None) -> str:
    """Render memory grouped by the entity each fact names, newest LAST inside each group.

    Order within a group is preserved from the store, which sorts oldest→newest, so the last
    line under a heading is the current one. That is what lets a correction win without any
    schema change: it is simply the most recent thing said about that entity.

    Each fact is printed ONCE, under its primary entity, with any other names it mentions
    noted inline. Printing it under every entity it named doubled the block (24k → 50k
    characters on the live store, about 6,000 extra tokens a turn) for no added information.
    """
    facts = [f for f in (mem_list or []) if (f or "").strip()]
    if not facts:
        return "(memory empty)"
    ents = {f: _entities_in(f) for f in facts}
    tally = {}
    for names in ents.values():
        for e in names:
            tally[e] = tally.get(e, 0) + 1
    # A heading has to earn itself; anything rarer stays in one plain list rather than
    # hiding behind a heading of its own.
    worthy = {e for e, n in tally.items() if n >= min_facts}
    groups, order, rest = {}, [], []
    for f in facts:
        # The most SPECIFIC entity wins the row: "Robin's daughter Rebecca's packet" belongs
        # under REBECCA, not under ROBIN, who appears in dozens of unrelated facts. First-
        # mentioned put it under the commonest name and buried it.
        cands = [e for e in ents[f] if e in worthy]
        primary = min(cands, key=lambda e: tally[e]) if cands else None
        if primary is None:
            rest.append(f); continue
        if primary not in groups:
            groups[primary] = []; order.append(primary)
        others = [e for e in ents[f] if e != primary and e in worthy]
        groups[primary].append((f, others))
    order.sort(key=lambda e: -len(groups[e]))
    lines = ["HOW TO READ THIS: each heading holds what is known about THAT person or "
             "project. Never carry a detail from one heading to another — a requirement on "
             "one deal is not a requirement on another.\n"
             "Each line shows when it was RECORDED, newest last. Recording an old event "
             "today does not make its content new.\n"
             "A LATER LINE THAT EXPLICITLY CORRECTS AN EARLIER ONE — Brady saying it is the "
             "uncle NOT Damon, a figure restated, a date moved — SETTLES it. Use the "
             "corrected version and do not ask him to confirm a correction he already gave.\n"
             "ASK only when the disagreement is genuinely unresolved: two sources that never "
             "referred to each other, a board row and a calendar entry that simply differ, or "
             "no way to tell which is current. Then name both and ask one short question.\n"
             "An undated line has no known recording time and settles nothing on its own."]

    def _render(items):
        # Sorted by the STORED DATE, not by list position. read_facts() returns core-tier
        # facts first regardless of age, so position told the model a stale line was the
        # newest — the exact reproduction Codex ran with a 1 Sept fact outranking an 8 Sept
        # correction. Undated facts sort first so a dated line always lands last.
        # Sorted on the FULL timestamp (ISO sorts lexically), so same-day updates keep their
        # real order instead of falling back to the read path's importance ordering.
        dated = sorted(items, key=lambda t: (_fact_ts(meta, t[0]) or "0000"))
        out = []
        for f, others in dated:
            tail = f"   [also names: {', '.join(others)}]" if others else ""
            out.append(f"  - [{_fact_stamp(meta, f)}] {f}{tail}")
        return out

    for name in order:
        lines.append("")
        lines.append(f"{name.upper()} ({len(groups[name])}):")
        lines.extend(_render(groups[name]))
    if rest:
        lines.append("")
        lines.append("OTHER:")
        lines.extend(_render([(f, []) for f in rest]))
    return "\n".join(lines)


def _live_entities(items: list, limit: int = 12) -> list:
    """People and projects named on the OPEN board — the cases actually in play today.

    Used to decide whose old facts must be carried into the live window. Mention-indexing
    again, not inference: if the aunt's row is open, the aunt's facts are relevant, whatever
    the row is "about".
    """
    tally = {}
    for it in (items or []):
        if (it.get("status") or "open") != "open":
            continue
        for e in _entities_in(it.get("text") or ""):
            tally[e] = tally.get(e, 0) + 1
    return [e for e, _ in sorted(tally.items(), key=lambda kv: -kv[1])][:limit]


def _mem_slim(mem_list: list, head: int = 40, tail: int = 70,
              live_entities: list = None, per_entity: int = 2) -> list:
    """SMART MEMORY TIER (2026-08-03, cost fix): the fact store crossed ~330 entries and
    re-mailing ALL of it every turn was the single biggest per-message cost. Inline =
    the head (core tier sorts first) + the most recent tail; everything in between is
    one recall() away — Ace is TOLD that, so nothing is lost, just not re-sent.
    (2026-08-19) Ace's own SELF-NOTE / save-narration are stripped here so they never crowd
    the live context — they remain in the raw store for the brief backlog + recall."""
    mem_list = [m for m in mem_list if not _is_meta_fact(m)]
    if len(mem_list) <= head + tail:
        return mem_list
    keep = list(mem_list[:head]) + list(mem_list[-tail:])
    # PULL IN THE FACTS FOR WHOEVER IS LIVE ON THE BOARD (2026-09-08).
    # head+tail is core + most-recent, and an entity whose last note is a month old falls out
    # of both. Codex's evaluation caught the consequence: asked whether the AUNT's deal was
    # complete, Ace had NO aunt facts in context at all and answered using Thiami's document
    # requirement and Rebecca's mailing date instead. It was not confusing two people it could
    # see — it was filling a hole with the nearest similar case. So for each person or project
    # named on the OPEN board, carry their most recent few facts even if they are old.
    if live_entities:
        want = {e.lower() for e in live_entities}
        have = set(keep)
        extra = []
        for e in want:
            hits = [m for m in mem_list if m not in have
                    and re.search(r"\b" + re.escape(e) + r"\b", m, re.I)]
            for m in hits[-per_entity:]:
                if m not in have:
                    extra.append(m); have.add(m)
        if extra:
            # keep store order so "newest last" still holds inside each group
            pos = {m: i for i, m in enumerate(mem_list)}
            keep = sorted(set(keep) | set(extra), key=lambda m: pos.get(m, 0))
    dropped = len(mem_list) - len(keep)
    return (keep[:head] if len(keep) <= head else keep[:head]) + (
        [f"… ({max(dropped, 0)} older facts not shown — use recall to "
         f"search them before ever saying you don't know or remember something)"]
        + keep[head:] if len(keep) > head else [])


async def _live_context() -> tuple:
    """Fetch memory + calendar (recent past → next 3 weeks) + tasks + inbox + weather
    + data bank concurrently."""
    memory, cal_all, bank, inbox, personal, wx = await asyncio.gather(
        asyncio.to_thread(brain.read_memory),
        asyncio.to_thread(get_events_structured, 21, 7),  # last week → next 3 weeks
        asyncio.to_thread(daybank.read_items, True),
        asyncio.to_thread(get_gmail_summary),
        asyncio.to_thread(get_personal_inbox_structured, 5),   # dormant until GOOGLE_TOKEN_JSON_PERSONAL set
        get_weather(),
        return_exceptions=True,
    )

    def ok(v, default):
        return default if isinstance(v, Exception) else v

    now = datetime.now(EASTERN)
    events = ok(cal_all, [])
    today_str = now.strftime("%Y-%m-%d")
    today_events = [e for e in events if e.get("date") == today_str]
    mem_list = _mem_slim(ok(memory, []), live_entities=_live_entities(ok(bank, [])))
    mem = _group_facts(mem_list, meta=_CTX.get("memory_meta") or brain.read_memory_meta())
    today_sched = _format_today_schedule(today_events, now)
    bank_str = _format_daybank(ok(bank, []))
    p_list = ok(personal, [])   # [] until Brady links br80mcgraw — nothing shows before then
    personal_block = "\n".join(f"- {m['from']}: {m['subject']}" for m in p_list)
    # SPLIT FOR CACHING (Phase 6 step 1, 2026-09-06). Measured on the live store, ACE MEMORY
    # alone is ~4,600 tokens and was re-sent UNCACHED on every typed turn — the largest
    # repeated cost in the request. It only changes when the learning sweep files a fact, so
    # it belongs in its own cached block; the profile and the recap move with it for the same
    # reason. Everything below them changes turn to turn and must stay out of the cache.
    #
    # ORDER MATTERS: the cache is a PREFIX, so the slow half has to come first and nothing
    # volatile may be interleaved into it. CURRENT TIME therefore moves out of position 1 and
    # becomes the first line of the fast half — still stated up front, just after the part
    # that doesn't change. (It was first because voice kept losing the date; it stays
    # prominent, and the voice path builds its own context and is untouched here.)
    _chg = _changelog_block()
    slow = [
        _profile_block(),
        *(["", "WHAT CHANGED IN YOU RECENTLY (newest first — this is what you can do NOW; if "
           "Brady asks what's new or what you can do, answer from this, and never describe a "
           "fix as still pending when it is listed here):", _chg] if _chg else []),
        "",
        "ACE MEMORY (what you know about Brady and PFI):",
        mem,
        "",
        "WHERE YOU LEFT OFF (recap of your recent conversations — pick up from here, don't re-ask):",
        _recap_block(),
    ]
    parts = [
        f"CURRENT TIME (Eastern): {now.strftime('%A, %B %d, %Y — %-I:%M %p')}",
        "",
        date_ladder(now),
        "",
        "TODAY'S SCHEDULE (relative to the current time above):",
        today_sched,
        "",
        "CALENDAR — last week through the next 3 weeks (past for reference, upcoming for "
        "planning; answer any date-range question from this directly):",
        _format_calendar_window(events, now),
        "",
        "UNREAD PRIORITY INBOX — PFI / business (last 2 days — scan it; flag anything that needs a reply):",
        ok(inbox, "(unavailable)"),
        *(["",
           "PERSONAL INBOX — br80mcgraw (SEPARATE from PFI, READ-ONLY; Brady replies himself. Flag "
           "anything that needs him — a job/recruiter reply, a personal-client note — but never send "
           "from here):",
           personal_block] if personal_block else []),
        "",
        "WEATHER RIGHT NOW (factor it into his day when it matters):",
        _format_weather(ok(wx, {})),
        "",
        "YOUR TASK BOARD (Ace's OWN store — THE task & pipeline system; Brady sees this as his "
        "Command panel. PRIORITY columns are shown in full; back-burner is capped for focus — the "
        "board is BIGGER than what's here. Complete/reopen with update_item, capture with "
        "capture_item. If an item ISN'T listed below, DON'T say it doesn't exist — call update_item "
        "with a few words in `match` and the server resolves it against the whole board. Google "
        "Tasks is retired — never route tasks there unless Brady explicitly says 'Google'):",
        bank_str,
    ]
    return "\n".join(slow), "\n".join(parts)


def _format_weather(w) -> str:
    if not isinstance(w, dict) or not w.get("ok"):
        return "(unavailable)"
    lead = []
    if w.get("temp") is not None:
        lead.append(f"{w['temp']}°")
    if w.get("description"):
        lead.append(w["description"])
    hl = []
    if w.get("high") is not None:
        hl.append(f"H{w['high']}°")
    if w.get("low") is not None:
        hl.append(f"L{w['low']}°")
    tail = f" ({'/'.join(hl)})" if hl else ""
    loc = f" — {w['location']}" if w.get("location") else ""
    return (" ".join(lead) + tail + loc) or "(unavailable)"


_PRIORITY_CATS = ("Money", "Bills", "Opportunities", "Goals", "Personal")
_BACKBURNER_CATS = ("Deals", "Agents", "Admin", "Networking", "Business", "Tech")
_ALL_CATS = _PRIORITY_CATS + _BACKBURNER_CATS


def _due_tag(it) -> str:
    """Deterministic due label from db-computed due_days — never let the model guess a date."""
    dd = it.get("due_days")
    if dd is None:
        return ""
    on = it.get("due_on") or ""
    when = ("DUE TODAY" if dd == 0 else "DUE TOMORROW" if dd == 1
            else f"due in {dd}d" if dd > 0 else f"{abs(dd)}d OVERDUE")
    return f"  [{when}{f' · {on}' if on else ''}]"


# How much of an item's text Ace actually gets to READ. The old flat 100 chars amputated
# 19 of 49 open items — including the Chris diligence (583 chars hidden) and the gas balance.
# Full priority text costs ~350 tokens/turn, which is nothing against quoting a stale number.
_PRIO_CHARS = 700   # Money/Bills/Opportunities/Goals/Personal — these carry dollars and dates
_BACK_CHARS = 240   # back-burner still gets enough to be actionable


def _format_daybank(items: list) -> str:
    """Render the board for Ace's per-turn context. Stage ④ (2026-08-13): PRIORITY columns
    (Money/Bills/Opportunities/Goals/Personal) are shown in full (text to _PRIO_CHARS), due-sorted, with EXACT due labels
    so Ace reasons about what's imminent instead of reciting a flat 88-line wall. Back-burner
    columns are capped and summarized — the FULL board is still reachable: update_item/complete
    resolve by `match` text server-side, so an item Ace can't see here is still completable by a
    few words. This cuts per-turn tokens AND sharpens what matters (his 'reason, don't recite')."""
    if not items:
        return "(nothing captured yet)"
    open_items = [it for it in items if it.get("status") == "open"]
    done_today = [it for it in items if it.get("status") == "done"]

    def _cat(it):
        return next((t for t in (it.get("tags") or []) if t in _ALL_CATS), "Admin")

    def _row(it, cap=_BACK_CHARS):
        txt = (it.get("text", "") or "")
        # A silent cut is worse than a short row: on 2026-08-26 the gas item's real balance
        # ($438.84) and its disconnection date sat past the old 100-char cut, so Ace confidently
        # quoted the stale $191 that WAS visible. Mark the cut so he knows to pull the rest —
        # update_item/complete resolve by match text, so the full item is always reachable.
        body = txt if len(txt) <= cap else txt[:cap].rstrip() + " …[TRUNCATED — ask for the full item before quoting numbers]"
        # THE MODEL WAS INVISIBLE TO HIM (2026-09-06). Phase 4 put records/actions, the WAITING
        # state and the five lanes in the database and the panel, but this — the board as ACE
        # sees it — still rendered a flat category list. So he treated a record parked on
        # someone else exactly like a to-do Brady owes, which is the whole failure the split
        # exists to prevent. The tags are short on purpose; they cost a few tokens a row.
        # ONE INTERPRETATION (2026-09-08). This block used to decide waiting/settled/record
        # for itself, in a different order from the screen and from backend/classify.py —
        # a third opinion about the same row. It now asks classify, so what Ace reads and
        # what Brady sees can never drift apart again.
        from . import classify
        lane = classify.lane_of(it)
        if lane == classify.LANE_WAITING:
            who = it.get("waiting_on")
            # The legend above says what these MEAN — repeating it on every row cost ~475
            # tokens a turn for no added information. The tag carries only the data.
            mark = f" [PARKED · {who}]" if who else " [PARKED]"
        elif lane == classify.LANE_SETTLED:
            mark = " [SETTLED]"
        elif lane == classify.LANE_REFERENCE:
            mark = " [RECORD]"
        elif lane == classify.LANE_UNDECIDED:
            # No date and no next step: the one thing Ace should ASK about rather than
            # invent an answer for.
            mark = " [NEEDS A DECISION]"
        elif it.get("bucket"):
            mark = f" [{it['bucket']}]"
        else:
            mark = ""
        # The next move and Brady's own follow-up date, when he has actually recorded them.
        nxt = (it.get("next_step") or "").strip()
        if nxt:
            mark += f" [NEXT: {nxt[:80]}]"
        fup = (it.get("followup") or "").strip()
        if fup:
            mark += f" [FOLLOW UP {fup}]"
        return f"- [{it.get('id','?')}] {body}{_due_tag(it)}{mark}"

    def _due_key(it):
        dd = it.get("due_days")
        return (dd is None, dd if dd is not None else 999)

    lines, back = [], []
    for c in _PRIORITY_CATS:
        col = sorted((it for it in open_items if _cat(it) == c), key=_due_key)
        if col:
            lines.append(f"{c} ({len(col)}):")
            lines += [f"  {_row(it, _PRIO_CHARS)[2:]}" for it in col]
    bb = [it for it in open_items if _cat(it) in _BACKBURNER_CATS]
    if bb:
        bb.sort(key=_due_key)
        CAP = 14
        back.append(f"BACK-BURNER ({len(bb)} items — Deals/Agents/Admin/Networking/Business/Tech):")
        back += [f"  {_row(it, _BACK_CHARS)[2:]}  [{_cat(it)}]" for it in bb[:CAP]]
        if len(bb) > CAP:
            back.append(f"  …+{len(bb) - CAP} more back-burner items (say a few words to pull or complete any — resolved by match)")
    parked_n = sum(1 for it in open_items if it.get("state") == "waiting")
    legend = ("HOW TO READ THIS BOARD: an ACTION ends when it is done. A [RECORD] has a STATE "
              "and is updated forever — a client, a bill, a goal — never 'complete' one to mean "
              "it is settled. [PARKED] means it is waiting on someone ELSE: it is real and it "
              "matters, but it is NOT something Brady can act on, so never list it as his to-do "
              "or push him on it. The [Lane] tag says whose time an action takes.")
    if parked_n:
        legend += f" {parked_n} row(s) are parked right now."
    legend += (" [NEEDS A DECISION] means it has no date and no next step — ask what the next "
               "move is rather than inventing one. [NEXT: …] is the move Brady already chose; "
               "[FOLLOW UP date] is when HE chases it, which is not the same as a due date.")
    out = [legend, "", "PRIORITY (money & life — act on these first):"] + lines + [""] + back
    if done_today:
        out += ["", f"DONE TODAY ({len(done_today)}): " + " · ".join(
            (it.get('text','') or '')[:40] for it in done_today[:12])]
    return "\n".join(out).strip()


async def _load_messages(user_text: str, prior=None) -> list:
    """Conversation for the API, sanitized to {role,content} + this turn.

    prior=None (HTTP one-shot): seed from the UNIFIED thread (ace2's own history,
    which records both voice and typed turns). prior given (WS chat / voice adapter):
    use the caller's conversation — the WS seeds it from the unified thread too, and
    the voice adapter passes ElevenLabs' live call history.
    """
    if prior is None:
        prior = await asyncio.to_thread(_unified_thread)
    msgs = brain.sanitize_for_api(prior)
    # The user turn is now persisted BEFORE this runs (2026-08-24 early-persist), so on the
    # HTTP path it is ALREADY the last message here — and sanitize_for_api MERGES consecutive
    # same-role turns, so after a failed turn left an orphan user message it arrives as
    # "earlier text\n\nthis text" and strict equality no longer recognizes it. That produced a
    # DOUBLE user message — his words twice in the prompt (the API merges consecutive same-role
    # turns rather than erroring, so it's silent context pollution, not a crash).
    # endswith() recognizes both the plain and the merged shape.
    last = msgs[-1] if msgs else None
    if not (last and last.get("role") == "user"
            and (last.get("content") or "").endswith(user_text)):
        msgs.append({"role": "user", "content": user_text})
    return msgs


# ── The ONE brain: Ace 2.0 is SELF-CONTAINED ────────────────────────────────────
# Ace records EVERY turn from both voice and chat to its OWN history. That log — not
# the Telegram bot's shared window — is the single source of truth. Ace no longer
# reads the bot's conversation at all: the bot is a standalone backup now, never a
# dependency. This is what makes voice and chat the same brain AND makes Ace fully
# individual — the bot going dark can't take Ace's memory with it. Durable state
# lives in ACE MEMORY (facts) + the DATA BANK (commitments/deals); this raw thread
# is only the last handful of turns for immediate continuity.
# A fixed TURN count is the wrong unit for voice (measured 2026-09-08). Brady's median gap
# between turns on a live call is SIX SECONDS, so the old 30-turn window held about three
# minutes. In the 22-minute call on 7 September the week plan he kept referring to was still
# in context when he first pushed back at 09:11 (25 turns back) and had fallen out by 09:17
# (44 turns) — which is exactly when "you've already given it to me twice" starts. The window
# is now measured in MINUTES with a hard turn cap, so one conversation stays one conversation.
_THREAD_MINUTES = int(os.environ.get("ACE2_THREAD_MINUTES", "90"))
_THREAD_MAX = int(os.environ.get("ACE2_THREAD_MAX", "80"))


# A re-flush arrives within seconds. Two stored user turns can otherwise be MINUTES or DAYS
# apart — a request Brady made this morning and an expanded version of it tonight are two
# separate intentions, and merging them would silently drop the first (Codex, 2026-09-08).
_REFLUSH_WINDOW_SEC = int(os.environ.get("ACE2_REFLUSH_WINDOW", "60"))


def _seconds_apart(a: dict, b: dict) -> float:
    """Gap between two stored turns. An unparseable stamp returns infinity, so a turn whose
    time is unknown is never collapsed into another."""
    try:
        ta = datetime.fromisoformat(str(a.get("ts")))
        tb = datetime.fromisoformat(str(b.get("ts")))
        return abs((tb - ta).total_seconds())
    except Exception:
        return float("inf")


def _collapse_reflushes(turns: list) -> list:
    """Drop transcript re-flushes: ElevenLabs re-sends a growing user turn as it decides the
    sentence is finished, so one spoken sentence lands as 2-3 rows. On 7 September 15 of 59
    consecutive user pairs were continuations of the previous one, a quarter of the window
    spent on text already present. Only a STRICT extension of the immediately preceding user
    turn is collapsed, and the longest form is what survives — a genuine short correction
    ("No, don't.") is not an extension of anything and is never touched."""
    out = []
    for t in turns:
        prev = out[-1].get("content", "") if out else ""
        cur = t.get("content", "")
        if (out and t.get("role") == "user" and out[-1].get("role") == "user"
                and len(cur) > len(prev) and cur.startswith(prev)
                and _seconds_apart(out[-1], t) <= _REFLUSH_WINDOW_SEC):
            out[-1] = t          # same sentence, more of it
            continue
        # An EXACT repeat is left alone: saying "yes" twice is two answers, not a re-flush.
        out.append(t)
    return out


def _unified_thread(limit: int = None) -> list:
    """Recent conversation across voice + chat, from Ace's OWN history only.

    Held by TIME rather than turn count (see above), de-duplicated for transcript re-flushes,
    and capped so a very long session cannot grow the prompt without bound. `recall` still
    covers anything older."""
    try:
        entries = history.read_recent(2)
    except Exception:
        entries = []
    turns = [
        {"role": e.get("role"), "content": (e.get("content") or "").strip(), "ts": e.get("ts")}
        for e in entries
        if e.get("role") in ("user", "assistant") and (e.get("content") or "").strip()
    ]
    turns = _collapse_reflushes(turns)
    cutoff = datetime.now(EASTERN) - timedelta(minutes=_THREAD_MINUTES)
    recent = []
    for t in turns:
        ts = t.get("ts")
        try:
            if ts and datetime.fromisoformat(str(ts)).astimezone(EASTERN) >= cutoff:
                recent.append(t)
        except Exception:
            recent.append(t)          # unparseable stamp: keep it rather than lose the turn
    # Never return less than the old behaviour, never more than the cap.
    if len(recent) < 30:
        recent = turns[-30:]
    return recent[-(limit or _THREAD_MAX):]


# ~4 chars per token: 6,000 characters is roughly 1,500 tokens of conversation, against a
# voice context that already runs several thousand. Measured cost is in the report.
_VOICE_THREAD_CHARS = int(os.environ.get("ACE2_VOICE_THREAD_CHARS", "9000"))
# Big enough for a day-by-day plan to survive intact. The TOTAL budget above bounds cost.
_VOICE_TURN_CHARS = int(os.environ.get("ACE2_VOICE_TURN_CHARS", "900"))


def _budget_turns(turns: list, max_chars: int) -> list:
    """The most recent turns that fit in `max_chars`, oldest-first, never fewer than one.

    Newest-first accumulation, then reversed: the END of the conversation is what a live
    turn needs most, and dropping the oldest is the only safe direction — a correction Brady
    just made must never be the thing that falls out.
    """
    out, used = [], 0
    for t in reversed(turns or []):
        n = len(t.get("content") or "") + 20        # + role/date framing
        if out and used + n > max_chars:
            break
        out.append(t)
        used += n
    return list(reversed(out))


def _format_thread(turns: list, per_turn: int = 280) -> str:
    """PER-TURN CAP (2026-09-08, Codex). Every turn was cut to 280 characters. That is fine
    for conversational back-and-forth and destroys a structured answer: the 7 September week
    plan is a single 2,522-character assistant turn, so Ace could only ever see its opening
    sentence — the days themselves were never in his context, whatever the window size.
    Callers with a total budget can afford a bigger per-turn cap; the default is unchanged
    so no other caller shifts.

    """ + """Render the recent thread for the context block. Lines are stamped with the DAY
    ONLY (never a clock time) — day-stamps stop the 'it's Saturday' drift, while
    omitting the time stops Ace from echoing a past turn's timestamp as 'now' (the
    9:24-vs-10:12 bug). The one authority for the current time is the CURRENT TIME
    line at the top of the context."""
    if not turns:
        return "(no earlier conversation yet — start fresh)"
    lines = []
    for m in turns:
        stamp = ""
        if m.get("ts"):
            try:
                dt = datetime.fromisoformat(m["ts"]).astimezone(EASTERN)
                stamp = f"[{dt.strftime('%a')}] "
            except Exception:
                stamp = ""
        lines.append(f"{stamp}{m['role']}: {m['content'][:per_turn]}")
    return "\n".join(lines)


async def _card_payload(panel: str):
    """Structured data for a display_card call. Concurrent, graceful."""
    if panel == "calendar":
        return {"events": await asyncio.to_thread(get_events_structured, 21, 7)}
    if panel == "timeline":
        # Today only; the frontend computes the NOW line against the live clock.
        return {"events": await asyncio.to_thread(get_events_structured, 1)}
    if panel == "tasks":
        flat, grouped = await asyncio.gather(
            asyncio.to_thread(get_tasks_structured),
            asyncio.to_thread(get_task_lists_grouped),
        )
        return {"tasks": flat, "lists": grouped}
    if panel == "inbox":
        return {"emails": await asyncio.to_thread(get_inbox_structured, 6)}
    if panel == "weather":
        return await get_weather()
    if panel == "memory":
        return {"memories": await asyncio.to_thread(brain.read_memory)}
    if panel == "daybank":
        return {"items": await asyncio.to_thread(daybank.read_items, True)}
    return {}


async def _run_ui_tool(name: str, tool_input: dict, emit) -> str:
    """Execute a UI tool: the 'action' is an event the frontend materializes."""
    if name == "display_card":
        panel = (tool_input.get("panel") or "").strip().lower()
        where = (tool_input.get("where") or "").strip().lower()
        where = where if where in ("left", "right") else None
        try:
            data = await _card_payload(panel)
            await emit("card", {"panel": panel, "data": data, "where": where})
            return f"Displayed the {panel} card on screen."
        except Exception as e:
            logger.error("display_card(%s): %s", panel, e)
            return f"⚠️ Could not display {panel}: {e}"
    if name == "open_url":
        url = (tool_input.get("url") or "").strip()
        label = (tool_input.get("label") or url).strip()
        if not url.lower().startswith(("http://", "https://")):
            return "⚠️ open_url needs an http(s) URL."
        await emit("open", {"url": url, "label": label})
        return f"Opened on screen: {label}"
    return f"⚠️ Unknown UI tool: {name}"


def date_ladder(now) -> str:
    """Every named day for the next two weeks, resolved to a real date.

    WHY THIS EXISTS (2026-09-11, from a live call). Ace had "Thursday, September 10, 2026" in
    front of him and still answered "next Wednesday is September 18th" — the 18th is a FRIDAY.
    He then wrote Ken's callback to the calendar on the wrong day, was corrected, wrote it
    again on a second wrong day, and was corrected again. Three writes, two of them wrong.

    Weekday arithmetic is the thing a language model is worst at and a lookup table is best
    at, so this replaces the arithmetic. It also fixes the ambiguity underneath it: Brady uses
    "this Friday" for the one in the current week and "next Friday" for the one in the week
    after, and those are different days. Counting forward from today cannot tell them apart —
    on a Thursday, "next Friday" is six days out, not one.

    Weeks run Monday to Sunday, which is the boundary "next week" means in ordinary speech.
    """
    from datetime import timedelta
    today = now.date()
    # Sunday that ends the current week (Mon=0 … Sun=6).
    end_of_week = today + timedelta(days=6 - today.weekday())
    lines = ["DATE LADDER — resolve any named day from THIS TABLE. Never count weekdays "
             "yourself; if a day is not listed, say so rather than calculating it.",
             f"  today          {today:%a %Y-%m-%d}",
             f"  tomorrow       {today + timedelta(days=1):%a %Y-%m-%d}"]
    rest, nxt = [], []
    for i in range(1, 15):
        d = today + timedelta(days=i)
        (rest if d <= end_of_week else nxt).append(d)
    for d in rest:
        lines.append(f"  this {d:%A}".ljust(17) + f"{d:%Y-%m-%d}")
    for d in nxt[:7]:
        lines.append(f"  next {d:%A}".ljust(17) + f"{d:%Y-%m-%d}")
    return "\n".join(lines)


def _format_calendar_window(events: list, now) -> str:
    """Group a get_events_structured(days, back_days) list by date for context — recent
    past + upcoming — so Ace can answer 'what did I have Tuesday' and 'what's next week'.
    """
    if not events:
        return "(no events in this window)"
    today = now.date()
    by_date: dict = {}
    for e in events:
        by_date.setdefault(e.get("date", ""), []).append(e)
    lines = []
    for dstr in sorted(k for k in by_date if k):
        try:
            d = datetime.strptime(dstr, "%Y-%m-%d").date()
        except Exception:
            continue
        delta = (d - today).days
        rel = {0: " — TODAY", -1: " — YESTERDAY", 1: " — TOMORROW"}.get(delta, " (past)" if delta < 0 else "")
        header = by_date[dstr][0].get("date_label", dstr)
        lines.append(f"{header}{rel}:")
        for e in by_date[dstr]:
            cal = e.get("calendar", "")
            lines.append(f"  {e.get('time','')} — {e.get('title','')}" + (f" [{cal}]" if cal else ""))
    return "\n".join(lines)


# ── Voice context cache (stale-while-revalidate) ────────────────────────────────
# _fast_context() used to run a live Google-Calendar + Drive-memory + weather + Postgres
# fan-out on EVERY voice turn (tolerated up to 9s). THAT silent gap is what makes ElevenLabs
# cut a live call — the timeouts Brady keeps hitting. Voice conversation doesn't need
# second-fresh calendar/memory (ElevenLabs resends the live turn-by-turn transcript as
# `prior`), so we serve those from a background-refreshed cache and format the per-turn
# string from cache + the LIVE clock: microseconds, zero per-turn network. Primed at startup
# (main.prime) and kept warm every ~30s.
_CTX = {"memory": [], "memory_meta": {}, "events": [], "bank": [], "convo": [], "wx": {},
        "tasks": [], "recap": "", "ts": 0.0}
_CTX_TTL = 45.0


def ctx_diag() -> dict:
    """Read-only snapshot of the VOICE context cache — the exact data a live call reads
    from. If today's events are missing here, voice is calendar-blind even though the typed
    path (a fresh fetch) is fine. This is how we tell a stale/failed refresh from a wrong
    clock."""
    now = datetime.now(EASTERN)
    today = now.strftime("%Y-%m-%d")
    events = _CTX.get("events") or []
    todays = [e for e in events if e.get("date") == today]
    return {
        "server_now_eastern": now.strftime("%A, %B %d, %Y — %-I:%M %p"),
        "today": today,
        "cache_age_seconds": round(time.time() - _CTX["ts"], 1) if _CTX["ts"] else None,
        "cache_ttl_seconds": _CTX_TTL,
        "cache_is_stale": (_CTX["ts"] == 0.0) or (time.time() - _CTX["ts"]) > _CTX_TTL,
        "events_total_cached": len(events),
        "events_today_count": len(todays),
        "events_today_titles": [f"{e.get('time','')} {e.get('title','')}".strip() for e in todays[:12]],
        "memory_facts_cached": len(_CTX.get("memory") or []),
        "board_items_cached": len(_CTX.get("bank") or []),
        "learn_model": LEARN_MODEL,
        "typed_model": MODEL,
        "voice_model": VOICE_MODEL,
    }
_RECAP_TTL = 3 * 3600.0   # regenerate the "where we left off" recap at most every ~3h
_recap_running = [False]
_ctx_lock: asyncio.Lock = asyncio.Lock()
_ctx_keepwarm_started = [False]


async def _refresh_ctx() -> None:
    """Fetch the heavy voice-context bundle once and store it. Never raises; keeps the last
    good value for any sub-fetch that errors so one dead integration can't blank the context.
    If a refresh is already in flight, WAIT for it to land (don't skip) — a cold first turn
    that skipped got an EMPTY context and answered 'confidently' from nothing."""
    if _ctx_lock.locked():
        async with _ctx_lock:   # queue behind the in-flight fetch; its result is ours
            return
    async with _ctx_lock:
        await _refresh_ctx_inner()


async def _refresh_ctx_inner() -> None:
    try:
        memory, mem_meta, cal_all, bank, convo, wx = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(brain.read_memory),
                asyncio.to_thread(brain.read_memory_meta),   # dates, so recency is not guessed
                asyncio.to_thread(get_events_structured, 21, 7),  # last week → next 3 weeks
                asyncio.to_thread(daybank.read_items, True),   # HIS task board — voice's task titles
                asyncio.to_thread(_unified_thread),   # the ONE thread: voice + chat
                get_weather(),
                return_exceptions=True,
            ),
            timeout=20,  # off the critical path now, so it can afford to wait out a slow call
        )

        def keep(v, prev):
            # Also keep the LAST GOOD weather when the API hiccups with {"ok": False} (scrub m6).
            return prev if (isinstance(v, Exception) or v is None
                            or (isinstance(v, dict) and v.get("ok") is False)) else v

        _CTX.update(
            memory=keep(memory, _CTX["memory"]),
            memory_meta=keep(mem_meta, _CTX["memory_meta"]),
            events=keep(cal_all, _CTX["events"]),
            bank=keep(bank, _CTX["bank"]),
            convo=keep(convo, _CTX["convo"]),
            wx=keep(wx, _CTX["wx"]),
            ts=time.time(),
        )
    except Exception as e:
        logger.warning("voice ctx refresh failed: %s", e)
    # Keep the "where we left off" recap warm + regenerate if stale — background, never blocking.
    try:
        asyncio.create_task(_refresh_recap())
    except Exception:
        pass


async def prime_ctx() -> None:
    """Warm the cache at startup and start the background loops (called once from main.py):
    the context keep-warm AND the learning sweep (Ace teaching himself from conversations)."""
    await _refresh_ctx()
    await asyncio.to_thread(load_discreet)   # remember the Discreet Mode setting across restarts
    await preflight_cache_ttl()   # prove the 1h cache beta BEFORE a real turn depends on it
    if not _ctx_keepwarm_started[0]:
        _ctx_keepwarm_started[0] = True
        asyncio.create_task(_ctx_keepwarm())
        asyncio.create_task(_learn_loop())
        asyncio.create_task(_brief_loop())   # proactive: morning game plan + EOD recap
        asyncio.create_task(_watch_loop())   # ambient: he notices things and speaks up unasked
        # 1pm due-now push RETIRED 2026-08-24 (Brady: "useless honestly because of how much I use
        # him") — he's in Ace all day, so a scheduled list is noise on top of the briefs + the
        # ambient watch. The engine stays (POST /reminder/run still works on demand, and the
        # Steward desk will own due-date nudges with real triggers instead of a clock).
        # asyncio.create_task(_reminder_loop())
        asyncio.create_task(_graph_warm_loop())   # keep the knowledge map instant to open
        # PHASE 5 (2026-09-06): Ace auditing his OWN bookkeeping, on Railway's clock instead of
        # a laptop cron that went dark 53 hours in one week. Different job from _watch_loop
        # above — that one watches BRADY's world, this one watches Ace. Pure database reads, no
        # model call, so it costs nothing and cannot get expensive exactly when things break.
        from . import selfaudit
        asyncio.create_task(selfaudit.loop())


# ── The LEARNING AGENT: a background sub-agent that sweeps conversations so Ace teaches ───
# himself — auto-extracting new durable facts (not only when he remembered to save one), on a
# schedule, OFF the live conversation loop so he stays fast. This is "he builds himself" (Brady).
_LEARN_INTERVAL = 90 * 60.0   # LEAN MODE: sweep ~every 90 min (was 45 — hash-gated, so no loss when quiet)
_learn_running = [False]
_learn_state = {"last_hash": None}


async def compose_sweep(force: bool = False) -> dict:
    """Build one learning sweep's inputs + prompts WITHOUT spending a model call — the
    MAX BRIDGE (Brady's iMac running Claude Code on his Max plan, $0/token) pulls this,
    runs the prompts locally, and posts outputs to apply_sweep(). The in-server runner
    below uses the exact same pair, so there is ONE sweep brain on either path."""
    try:
        from . import db, daybank
        if not db.enabled():
            return {"skipped": "no db"}
        turns = await asyncio.to_thread(db.recent_turns, 40)
        if not turns or len(turns) < 4:
            return {"skipped": "too few turns"}
        # Keep near-full turns: Brady's brain-dump turns run 1000-1600 chars and the old 400-char
        # cap silently dropped the SECOND half of every long turn (where most deals/people/status
        # updates lived) — a primary reason the sweep under-captured. Opus can hold the whole thing.
        convo = "\n".join(f"{t.get('role')}: {(t.get('content') or '')[:2000]}" for t in turns)
        h = hash(convo)
        if not force and bridge_lease_active("sweep"):
            return {"skipped": "bridge worker holds this sweep"}
        if not force and h == _learn_state.get("last_hash"):
            return {"skipped": "no change"}   # nothing new since the last sweep — don't burn a call
        existing = await asyncio.to_thread(brain.read_memory)
        facts_prompt = (
                "You are Ace's background LEARNING sweep — Brady's second brain. Read this whole "
                "conversation and extract EVERY new durable fact worth remembering. Be THOROUGH, "
                "not conservative: he is brain-dumping and trusts you to catch all of it. Capture "
                "each of these when present:\n"
                "- a NEW person/prospect/agent and who they are (e.g. 'Vicks — 65, retired buggy "
                "operator at the port, new prospect')\n"
                "- any change in a deal's or person's STATUS (e.g. 'Corrine leaving the company "
                "after the Kiana Wiggins deal closes')\n"
                "- commitments, plans, splits, reschedules (e.g. 'Walter splitting his past deals "
                "50/50 with Brady'), and decisions\n"
                "Rules: skip pure small talk and things ALREADY covered by KNOWN FACTS. Convert "
                "relative dates to absolute. One fact per line, ~20 words, no bullets/numbering. "
                "Err toward capturing — a missed fact is worse than a slightly redundant one.\n"
                "★ ROUTING: do NOT record an ACTIONABLE to-do or follow-up (something Brady needs "
                "to DO — call someone, pay a bill, file a form, chase a deal) as a fact. Those "
                "belong on his TASK BOARD, handled by a separate pass — filing them here hides "
                "them where he can't track or complete them. Facts are durable CONTEXT ONLY: who "
                "people are, a deal's or person's STATUS, decisions made, numbers, and commitments "
                "already agreed — never open action items.\n"
                "★ WHO ELSE IS IN THE ROOM (2026-09-05): Brady says so in plain language — 'I'm "
                "with my new manager', 'Gabby's here', 'I'm on with the lender'. What he says "
                "while positioning for SOMEONE ELSE is social, not durable: 'I'm going full time "
                "with Groundworks', said in front of a new manager, was what that moment "
                "required, not a decision he made. Record what is actually true — that the "
                "conversation happened, who was there, what was genuinely agreed — never the "
                "positioning itself as his plan. When you cannot tell whether a line was meant "
                "for YOU or for an audience, LEAVE IT OUT: this is the one place where the "
                "'err toward capturing' rule above does not apply, because a wrong fact about "
                "his intentions steers every brief and every answer that follows it.\n"
                "If truly nothing new, reply with the single word NONE.\n\n"
                # MOST-RECENT known facts (2026-08-11 review fix): was existing[:80] = core +
                # OLDEST, so the sweep never saw recently-added facts and kept re-extracting them.
                "KNOWN FACTS:\n" + ("\n".join(f"- {m}" for m in (existing or [])[-80:]) or "(none)")
                + f"\n\nCONVERSATION:\n{convo}")
        # TRIAGE prompt — the RECONCILER (2026-07-31 board-dedup review): sees ids + status,
        # can emit DONE to close what Brady said he finished, forbidden to re-add rewordings.
        existing_items = await asyncio.to_thread(daybank.read_items, False)
        open_items = [it for it in existing_items if it.get("status") == "open"]
        recent_closed = [it for it in existing_items if it.get("status") != "open"][:40]
        tracked = "\n".join(f"- [{it['id']}] {it.get('text', '')}" for it in open_items)
        tracked_closed = "\n".join(f"- [{it['id']}] {it.get('text', '')}" for it in recent_closed)
        triage_prompt = (
                    "You are Ace's background board-keeper — Brady's safety net so nothing he says "
                    "slips, AND the janitor who closes what he finished. Read the conversation and "
                    "do BOTH:\n"
                    "1) COMPLETIONS — close an item ONLY when Brady clearly states, in the PAST "
                    "TENSE, that he ALREADY DID it: 'paid it', 'sent it', 'filed it', 'called them "
                    "and it's handled', 'finished', 'booked it', 'that's done'. Emit:\nDONE :: <id>\n"
                    "   ⚠ DO NOT close an item because he PLANNED it, is WORKING on it, is CHASING "
                    "it, mentioned it, or discussed it. 'I need to…', 'I'm going to…', 'let's focus "
                    "on…', 'still chasing…', 'what about X', 'add X', or just talking about a task "
                    "are NOT completions. When there's ANY doubt, DO NOT close it — a wrongly-closed "
                    "task is far worse than one left open. Ongoing/standing tasks (apply to jobs, "
                    "chase the commission, package offers) are NEVER 'done' from a single mention.\n"
                    "2) NEW to-dos — extract genuinely NEW actionable to-dos or follow-ups he needs "
                    "to DO. CRITICAL: a task that matches ANY item below — even reworded, shortened, "
                    "expanded, or with a different date — is NOT new. Skip it (or emit DONE if he "
                    "finished it). Never re-add a CLOSED item unless Brady explicitly reopened it. "
                    "New info about a tracked task is NOT a new task. For real new ones emit:\n"
                    "ADD :: CATEGORY :: task title\n"
                    "(CATEGORY from — priority: Money, Bills, Opportunities, Goals, Personal; "
                    "back-burner: Deals, Agents, Admin, Networking, Business, Tech. A money/tax/"
                    "debt move → Money; a recurring bill → Bills; a job or contract-income lead → "
                    "Opportunities.) One per line, ONLY those two formats, no other text. If nothing, "
                    "reply NONE.\n\n"
                    "OPEN ITEMS:\n" + (tracked or "(none)") + "\n\n"
                    "RECENTLY CLOSED (do not re-add):\n" + (tracked_closed or "(none)") + "\n\n"
                    f"CONVERSATION:\n{convo}")
        # REFLECTION prompt — the self-building seed: Ace notices where he fell short or
        # where Brady wants more; results become ACE SELF-NOTE facts (his dev backlog).
        reflection_prompt = (
            "You are Ace reflecting on how to serve Brady better. From this conversation, "
            "list up to 3 SELF-IMPROVEMENT notes — ONLY genuine signals like: something "
            "Brady asked for that Ace couldn't do or got wrong, a capability he wished "
            "for, repeated manual work Ace should automate, or a misroute/miss. ~15 words "
            "each. One per line, EXACTLY: NOTE :: <the note>. No other text. If none, "
            "reply NONE.\n\n"
            f"CONVERSATION:\n{convo}")
        return {"hash": h, "facts_prompt": facts_prompt, "triage_prompt": triage_prompt,
                "reflection_prompt": reflection_prompt}
    except Exception as e:
        logger.warning("compose_sweep failed: %s", e)
        return {"skipped": f"compose failed: {e}"}


async def apply_sweep(h: int, facts_text: str, triage_text: str, reflection_text: str) -> dict:
    """File one sweep's outputs through the SAME guarded stores (reconcile-on-write memory,
    dedup'd board, DONE only for really-open ids) and stamp last_hash so the in-server loop
    never re-burns a call on a conversation the MAX BRIDGE already digested. One brain,
    one set of rules — only the electricity comes from a different place."""
    from . import daybank
    filed, routed, closed = 0, 0, 0
    try:
        if h:
            _learn_state["last_hash"] = h
        text = (facts_text or "").strip()
        if not text or text.upper().startswith("NONE"):
            facts = []
        else:
            facts = [ln.strip("-•* ").strip() for ln in text.split("\n")
                     if len(ln.strip()) > 8 and not ln.strip().upper().startswith("NONE")]
        if facts:
            await asyncio.to_thread(brain.add_memory, facts, "sweep")
            filed = len(facts)
            logger.info("learn sweep: filed %d candidate fact(s)", filed)
        try:
            t2 = (triage_text or "").strip()
            if t2 and not t2.upper().startswith("NONE"):
                existing_items = await asyncio.to_thread(daybank.read_items, False)
                open_ids = {it["id"] for it in existing_items if it.get("status") == "open"}
                for ln in t2.split("\n"):
                    if "::" not in ln:
                        continue
                    parts = [p.strip().strip("-•*[] ") for p in ln.split("::")]
                    verb = parts[0].upper()
                    if verb == "DONE" and len(parts) >= 2:
                        iid = parts[1].split()[0].strip("[]") if parts[1] else ""
                        # NO-TOUCH SHELVES (2026-08-26, Brady): Bills recur monthly, Goals close
                        # only when genuinely achieved, and DEALS close only when Brady says every
                        # requirement is met AND it paid out — a submitted app or a scheduled
                        # paramed is progress, not completion. The sweep must never auto-close any
                        # of the three from a passing mention. Enforced in CODE, not just the
                        # prompt, because the sweep is exactly where silent wrong-closes happened.
                        _it = next((x for x in existing_items if x.get("id") == iid), None)
                        _cat = next((t for t in ((_it or {}).get("tags") or [])
                                     if t in ("Bills", "Goals", "Deals")), None)
                        if _cat:
                            logger.info("sweep: refused to auto-close %s item %s", _cat, iid)
                            continue
                        # WAITING IS NOT DONE (2026-09-05, Phase 4). A record parked on someone
                        # else — "everything submitted, waiting on approval", "just waiting, no
                        # push needed" — has no natural end, and a passing mention of it in
                        # conversation is not completion. Two of the six waiting rows on the
                        # live board sit in Opportunities, which the shelf guard above does not
                        # cover, so category alone was never going to be enough.
                        if (_it or {}).get("state") == "waiting":
                            logger.info("sweep: refused to auto-close WAITING record %s", iid)
                            continue
                        if iid in open_ids:   # only close ids that are really open — never guess
                            ok2, _r = await asyncio.to_thread(
                                daybank.update_item, iid, "done", closed_by="ace")
                            if ok2:
                                closed += 1
                        continue
                    if verb == "ADD" and len(parts) >= 3:
                        cat, title = parts[1], "::".join(parts[2:]).strip()
                    elif verb not in ("DONE", "ADD") and len(parts) >= 2:
                        # legacy 'CATEGORY :: title' lines still land safely
                        cat, title = parts[0], "::".join(parts[1:]).strip()
                    else:
                        continue
                    if len(title) > 4:
                        ok2, res2 = await asyncio.to_thread(
                            daybank.add_item, "todo", title, None, [cat] if cat else None)
                        if ok2 and not (isinstance(res2, dict) and res2.get("dup")):
                            routed += 1
                if routed or closed:
                    logger.info("board-keeper sweep: +%d to-do(s), closed %d", routed, closed)
        except Exception as e:
            logger.warning("triage apply (data bank) failed: %s", e)
        try:
            t3 = (reflection_text or "").strip()
            if t3 and not t3.upper().startswith("NONE"):
                notes = []
                for ln in t3.split("\n"):
                    if "::" not in ln:
                        continue   # line protocol keeps preamble/junk out of permanent memory
                    body = ln.split("::", 1)[1].strip()
                    if len(body) > 12:
                        notes.append(f"ACE SELF-NOTE: {body}")
                notes = notes[:3]
                if notes:
                    await asyncio.to_thread(brain.add_memory, notes, "reflection")
                    logger.info("reflection: filed %d self-note(s)", len(notes))
        except Exception as e:
            logger.warning("reflection apply failed: %s", e)
        return {"facts": filed, "tasks": routed, "closed": closed}
    except Exception as e:
        logger.warning("apply_sweep failed: %s", e)
        return {"error": str(e), "facts": filed, "tasks": routed, "closed": closed}


async def _learn_sweep_once(force: bool = False) -> dict:
    """In-server sweep runner (the API fallback): compose → model → apply. The MAX BRIDGE
    runs the same compose/apply pair with the model call on Brady's Max plan instead."""
    if _learn_running[0]:
        return {"skipped": "busy"}
    _learn_running[0] = True
    try:
        job = await compose_sweep(force)
        if job.get("skipped"):
            return job
        client = _anthropic()

        async def _run(prompt: str, max_tokens: int) -> str:
            resp = await client.messages.create(
                model=LEARN_MODEL, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}])
            return "".join(getattr(b, "text", "") for b in resp.content).strip()

        facts_text = await _run(job["facts_prompt"], 1500)
        try:
            triage_text = await _run(job["triage_prompt"], 1200)
        except Exception as e:
            triage_text = ""
            logger.warning("triage sweep (data bank) failed: %s", e)
        try:
            reflection_text = await _run(job["reflection_prompt"], 300)
        except Exception as e:
            reflection_text = ""
            logger.warning("reflection pass failed: %s", e)
        return await apply_sweep(job["hash"], facts_text, triage_text, reflection_text)
    except Exception as e:
        logger.warning("learn sweep failed: %s", e)
        return {"error": str(e)}
    finally:
        _learn_running[0] = False


async def _learn_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_LEARN_INTERVAL)
            await _learn_sweep_once()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _graph_warm_loop() -> None:
    """Keep the knowledge graph's cache hot. Building it costs ~35s of model time, and the
    HUD holds a connection open while it runs — so a cold tap on ◉ Graph is the one place
    Ace feels slow. Rebuilding just under the 6h TTL means Brady always gets the cached
    read (~0.1s) and never waits for a build he didn't ask for."""
    await asyncio.sleep(90)   # let the app finish booting before spending model time
    while True:
        try:
            from . import db
            if db.enabled():
                from .main import graph
                await graph(refresh=1)
                logger.info("graph cache warmed")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("graph warm failed: %s", e)
        try:
            await asyncio.sleep(23 * 3600)   # once a day — the book of business doesn't turn
        except asyncio.CancelledError:       # over every 5h; cut this rebuild cost ~80%. TTL
            break                            # is raised to match so taps still hit the cache.


_last_rollover = [None]   # date the recurring-bill rollover last ran (once per day)


async def _ctx_keepwarm() -> None:
    while True:
        try:
            await asyncio.sleep(30)
            await _refresh_ctx()
            # Recurring bills: reopen last month's paid bills once when the day turns over.
            from . import db
            today = datetime.now(EASTERN).date()
            if db.enabled() and _last_rollover[0] != today:
                _last_rollover[0] = today
                await asyncio.to_thread(db.rollover_recurring_bills)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ── PROACTIVE BRIEFS — Ace comes to Brady (morning game plan + EOD recap) ────────
# The autonomy loop Brady asked for: Ace runs himself on a schedule. Each brief is
# grounded in DETERMINISTIC board/win stats first, then written by the deep model,
# and lands in the ONE thread (so it's waiting in chat) + pushes live to open HUDs.
_BRIEF_TIMES = {"morning": (9, 0), "eod": (20, 15)}   # Eastern — Brady wants the day to open at 9

# THE THREE WAYS THE BRIEF LIED (2026-09-05). Each rule below is a morning Brady actually got.
# Shared by both briefs because all three failed in both.
_BRIEF_RULES = (
    "\n\nTHESE RULES OUTRANK EVERYTHING ABOVE.\n"
    "(0) NEVER SAY SOMETHING IS DONE WITHOUT EVIDENCE. On 9 September this brief said 'you hit "
    "the gym this morning, so that's done' forty minutes after Brady said he was skipping it "
    "until the evening. Evidence means BRADY said he did it, or a tool receipt says so. These "
    "are NOT evidence: a calendar entry (that is a plan), a board item existing, a promise from "
    "somebody else ('she said she'll sign' is still outstanding), or something YOU said in an "
    "earlier turn — your own prose is a claim, not a source. His words are testimony; yours are "
    "not. When you do not know, say what is still open rather than congratulating him for it.\n"
    "(0b) HIS PLAN FOR TODAY IS THE POINT. If he has just told you what he is doing today, lead "
    "with that. Bills belong here only as far as the sheet actually warrants; do not let a "
    "standing money narrative crowd out what he just said he needs to get done.\n"
    "(1) THE CONVERSATION OUTRANKS THE BOARD. The board is written once and rarely revisited; "
    "what Brady SAID is newer and truer. Where the thread and a board item disagree, the thread "
    "wins outright — never repeat the stale version, and never quietly narrate around it. Say "
    "the current truth, and where the gap matters, name it in one plain clause ('your board "
    "still has the truck at $500 — it's $473 now') so he knows to fix it. You have no tools "
    "here and cannot edit the board yourself. The [id] on each row is for YOUR reference only: "
    "never print one — this lands on his lock screen.\n"
    "(2) NEVER ASSERT PROGRESS YOU HAVE NOT VERIFIED. Say he did something ONLY if it is marked "
    "✓ in CHANGE SINCE THE LAST BRIEF, or he said so himself in the thread. You once told him "
    "'you crushed the gym' ninety minutes after he said he skipped it. No inferring a workout "
    "from a goal, no 'you've been consistent' from a feeling, no crediting him with an item "
    "just because it is on the board. With no evidence, say nothing about it — encouragement "
    "is welcome, invented history is not.\n"
    "(3) KNOW WHEN SOMEONE ELSE IS IN THE ROOM. Brady says so plainly ('I'm with my manager', "
    "'Gabby's here', 'I'm on with the lender'). What he says while positioning for someone else "
    "is SOCIAL, not a commitment — 'I'm going full time with Groundworks', said in front of a "
    "new manager, is what that moment needed, not a decision he made. Never carry a statement "
    "made TO a third party into the brief as his intention, his plan, or a fact about his week."
)


def _board_stats() -> str:
    """Deterministic business snapshot from Ace's own store — no LLM guessing.

    SHARED INTERPRETATION (2026-09-09). This function used to read a row as "a category and
    maybe a due date", and outside Money/Bills/Opportunities anything undated reached the
    brief as a bare count. So the board could show a row parked on the county with a next
    step written on it, and the brief saw a number. Worse, the two disagreed about what a row
    even was, which is how a brief can contradict the screen and both look right.

    It now decorates rows through `classify` — the same module the Command Center and Due
    Today use — so a lane, a waiting owner, a next step, a follow-up and a chosen day mean
    exactly one thing everywhere. Nothing here writes; decoration is additive.
    """
    from . import classify, db
    _CATS = ["Money", "Bills", "Opportunities", "Goals", "Personal", "Deals", "Agents", "Admin", "Networking", "Business", "Tech"]
    try:
        items = [classify.decorate(i) for i in db.read_items(active_only=False)]
    except Exception:
        items = []
    def cat(it):
        for t in (it.get("tags") or []):
            if t in _CATS:
                return t
        return "Admin"
    now = datetime.now(EASTERN)
    open_items = [i for i in items if i.get("status") == "open"]
    lines = ["BOARD: " + " · ".join(
        f"{c} {n}" for c in _CATS if (n := sum(1 for i in open_items if cat(i) == c)))]
    # PIVOT (2026-08-10) + deterministic dates (2026-08-13): hand the brief the open Money
    # actions and the Bills register with a PRE-COMPUTED [DUE ...] label per item, so the model
    # never guesses timing (it was saying 'tomorrow' for something 5 days out).
    # ITEMS, NOT A DIGEST (2026-09-05). Two defects lived in this function. (1) Every line was
    # cut to 104 chars — the same silent amputation that had Ace quoting stale numbers in chat,
    # so a bill's real terms ended mid-sentence. (2) ONLY Money and Bills were ever listed as
    # items; every other category reached the brief as a bare COUNT, so a dated session or a
    # commitment filed under Personal/Deals was invisible — and the model filled the hole by
    # blending unrelated Opportunities rows into things Brady never said ("$200/day angles").
    # Ids ride along so a stale row can be NAMED: this pass has no tools and cannot write to
    # the board, and pointing at [id] beats narrating around it.
    def _TODAY_STR():
        return datetime.now(EASTERN).strftime("%Y-%m-%d")

    def _line(i, cap=300, show_cat=False):
        t = (i.get("text", "") or "")
        body = t if len(t) <= cap else t[:cap].rstrip() + " …[truncated — ask before quoting]"
        tag = f"({cat(i)}) " if show_cat else ""
        head = f"  - [{i.get('id','?')}] {tag}{body}"
        dd = i.get("due_days")
        if dd is None:
            return head + _state(i)
        when = ("DUE TODAY" if dd == 0 else "DUE TOMORROW" if dd == 1
                else f"due in {dd} days" if dd > 0 else f"{abs(dd)} days OVERDUE")
        on = i.get("due_on") or ""
        return f"{head}  [{when}{f' — {on}' if on else ''}]" + _state(i)

    def _state(i):
        """The structured fields the board carries and the brief was throwing away. Each is
        STORED — never inferred from the wording — so the brief can say who owns the next
        move without guessing, and can tell a deadline from a day Brady chose himself."""
        bits = []
        if (i.get("waiting_on") or "").strip():
            bits.append(f"PARKED ON {i['waiting_on'].strip()} — he is NOT the next actor")
        if (i.get("next_step") or "").strip():
            bits.append(f"next step (his words): {i['next_step'].strip()[:120]}")
        if (i.get("followup") or "").strip():
            bits.append(f"he plans to chase it {i['followup']} — that is HIS reminder, "
                        f"not the other party's deadline")
        if (i.get("chosen_on") or "")[:10] == _TODAY_STR():
            bits.append("HE CHOSE THIS FOR TODAY (his pick, not a deadline)")
        # READ THE ROW, DO NOT RECITE THE OLD RULE (Codex, 2026-09-09). This described the
        # retired derived definition — "no date and no next step recorded" — and printed it
        # even on rows whose next step was sitting one line above it. The brief was
        # contradicting its own input. Say only what the row actually carries.
        if i.get("needs_decision"):
            bits.append("HE MARKED THIS AS A DECISION HE HAS NOT MADE. It is a question to "
                        "settle, not a job to report as ready or under way")
        if i.get("carried_over"):
            bits.append("CARRIED OVER FROM THE OLD BOARD, NOT YET REVIEWED — he has not "
                        "looked at this since the change. Never present it as agreed work")
        return ("\n      · " + "\n      · ".join(bits)) if bits else ""
    money = [i for i in open_items if cat(i) == "Money"]
    if money:
        lines.append("OPEN MONEY ACTIONS (priority — flag anything time-critical):\n"
                     + "\n".join(_line(i) for i in sorted(money, key=lambda x: (x.get("due_days") is None, x.get("due_days", 999)))[:12]))
    bills = [i for i in open_items if cat(i) == "Bills"]
    if bills:
        lines.append("BILLS (the [DUE ...] label is EXACT — use it, never compute a date yourself):\n"
                     + "\n".join(_line(i) for i in sorted(bills, key=lambda x: (x.get("due_days") is None, x.get("due_days", 999)))[:18]))
    jobs = [i for i in open_items if cat(i) == "Opportunities"]
    if jobs:
        lines.append("JOB HUNT / INCOME MOVES:\n" + "\n".join(
            _line(i) for i in sorted(
                jobs, key=lambda x: (x.get("due_days") is None, x.get("due_days", 999)))[:8]))
    # Everything else that is actually DATED. Money/Bills/Opportunities have their own sections
    # above; without this block a dated appointment, session or deal never reached the brief.
    other = [i for i in open_items
             if cat(i) not in ("Money", "Bills", "Opportunities")
             and i.get("due_days") is not None]
    if other:
        lines.append("ALSO ON THE CLOCK (dated items from every other category — sessions, "
                     "appointments, deals, personal commitments):\n" + "\n".join(
                         _line(i, show_cat=True)
                         for i in sorted(other, key=lambda x: x.get("due_days", 999))[:14]))
    # WHAT HE PICKED UP TODAY, and what is ready without a date. Both were invisible: the
    # brief only ever saw dated rows outside three categories, so the work Brady actually
    # chose this morning could not appear, and the model filled the gap with the register.
    chosen = [i for i in open_items
              if (i.get("chosen_on") or "")[:10] == now.strftime("%Y-%m-%d")]
    if chosen:
        lines.append("HE CHOSE THESE FOR TODAY (his own pick — treat as today's plan, and "
                     "never describe one as a deadline):\n"
                     + "\n".join(_line(i, show_cat=True) for i in chosen[:10]))
    parked = [i for i in open_items if i.get("lane") == "waiting"]
    if parked:
        lines.append("PARKED ON SOMEONE ELSE (he is NOT the next actor — never list these as "
                     "things for him to do, and never call one finished because time passed):\n"
                     + "\n".join(_line(i, show_cat=True) for i in parked[:10]))
    # THREE DIFFERENT THINGS, NOT ONE LIST (Codex, 2026-09-09). This selected the anytime
    # AND undecided lanes into one block called "UNDATED AND READY", so an open question
    # Brady had explicitly marked — and a legacy row nobody had reviewed — were both handed
    # to the model as ready work with a spare-time invitation attached.
    _chosen_ids = {c.get("id") for c in chosen}
    def _undated(pred):
        return [i for i in open_items
                if i.get("due_days") is None
                and cat(i) not in ("Money", "Bills", "Opportunities", "Goals")
                and i.get("id") not in _chosen_ids and pred(i)]
    decisions = _undated(lambda i: i.get("needs_decision"))
    unreviewed = _undated(lambda i: i.get("carried_over") and not i.get("needs_decision"))
    ready = _undated(lambda i: i.get("lane") == "anytime"
                     and not i.get("needs_decision") and not i.get("carried_over"))
    if decisions:
        lines.append("OPEN QUESTIONS HE MARKED HIMSELF (these are DECISIONS to settle. Never "
                     "describe one as ready, under way, or accepted — the underlying job has "
                     "not been agreed to. Offering to help him decide is useful):\n"
                     + "\n".join(_line(i, show_cat=True) for i in decisions[:8]))
    if unreviewed:
        lines.append("CARRIED OVER FROM THE OLD BOARD, NOT YET REVIEWED (the old rule called "
                     "these undecided; he has not looked at them since. Mention at most one, "
                     "and only as something to glance at — NEVER as ready or accepted "
                     "work):\n"
                     + "\n".join(_line(i, show_cat=True) for i in unreviewed[:6]))
    if ready:
        lines.append("UNDATED AND READY (real work with no deadline — candidates for spare "
                     "time, NOT things he committed to today):\n"
                     + "\n".join(_line(i, show_cat=True) for i in ready[:12]))

    # Wins from memory (the win-logger writes "Deal won:" / "Goal reached:" facts)
    try:
        wins = [f for f in db.read_facts_full()
                if not f.get("invalid_at")
                and (f.get("text", "").startswith("Deal won:") or f.get("text", "").startswith("Goal reached:"))
                and (now - datetime.fromisoformat(f["ts"]).astimezone(EASTERN)).days <= 7]
        if wins:
            lines.append("WINS THIS WEEK: " + " | ".join(w["text"][:70] for w in wins[:5]))
    except Exception:
        pass
    return "\n".join(lines)


# ── THE BRIEF'S OWN CONVERSATION INPUT (2026-09-09) ────────────────────────────
# The 9 Sept brief said "You hit the gym this morning, so that's done" forty minutes after
# Brady said he was skipping the gym until the evening. Rebuilding the deployed input from
# saved history showed why, and it is an input defect before it is a model one:
#   • compose_brief_prompt took the last 16 TURNS and formatted them at the default 280
#     characters, so the long day-layout answer — the one carrying the actual plan — was cut;
#   • four of those sixteen slots were partial re-flushes of ONE sentence, because the ASR
#     re-spelled a name and the strict-prefix collapse could not see them as the same turn;
#   • the gym correction sat outside the window entirely.
# So the brief is given a character budget rather than a turn count, a workable per-turn cap,
# a fuzzier re-flush collapse, and — most importantly — Brady's OWN action-bearing statements
# lifted out verbatim where truncation cannot reach them.
_BRIEF_THREAD_CHARS = int(os.environ.get("ACE2_BRIEF_THREAD_CHARS", "9000"))
_BRIEF_TURN_CHARS = int(os.environ.get("ACE2_BRIEF_TURN_CHARS", "900"))


def _similar(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def _collapse_near_reflushes(turns: list, ratio: float = 0.72, window: int = 180) -> list:
    """DELIBERATELY A NO-OP FOR USER TEXT (2026-09-09, Codex).

    This used to merge consecutive user turns that looked similar, keeping the longer one.
    Reproduced failure:

        "I am going to call the concrete supplier tomorrow morning for the pour."
        "I am going to call the concrete supplier today for the pour."

    Twenty seconds apart, judged similar, and the LONGER one kept — so the correction was
    thrown away and the stale date survived. That is precisely the failure this whole patch
    exists to fix, recreated by the fix. The same shape applies to a changed name, amount or
    negation: a correction is usually SHORTER than what it corrects.

    Nothing but explicit transcription or session metadata can prove one utterance supersedes
    another, and we do not have it. So distinct statements are all kept, and the character
    budget — not lossy guessing — is what bounds the input. `_collapse_reflushes` still merges
    a STRICT extension, where the later text contains the earlier one in full and nothing can
    be lost.
    """
    return list(turns or [])


# What Brady says he will DO, or corrects. Kept verbatim and never truncated.
# Deliberately generous: a missed commitment falls back to the thread, but a missed
# CORRECTION is how the 9 September brief went wrong. Contractions and the spelled-out
# forms both count ("I'm gonna" and "I am going to"), and a bare negation or a changed
# day qualifies on its own.
_COMMIT_RE = re.compile(
    r"\b(i(?:'?m| am) (?:going to|gonna|doing|out|not)|i'?ll|i will|i need to|i have to|"
    r"i gotta|i'?ve got to|i want to|i'?m|today|tonight|tomorrow|this evening|this morning|"
    r"instead of|actually|no,? i|scratch that|don'?t|do not|never mind|cancel|"
    r"changed my mind|make it)\b", re.I)


def _commitment_lines(turns: list, now=None, limit: int = 12,
                      total_chars: int = 4000) -> tuple:
    """(today_block, prior_block) — Brady's action-bearing lines, complete or not at all.

    Three things this must not do, all reproduced on 9 September:

      • label a statement from ANOTHER DAY as "today". `_unified_thread` falls back to older
        turns on a quiet morning, so a 8 September line could arrive and be presented under
        TODAY — turning "I'll mail it today" into today's commitment.
      • print a UTC clock as if it were his. 12:28 UTC was 8:28 in his morning.
      • quote a statement PARTIALLY while calling it verbatim testimony. A 482-character line
        ending "Do not send the packet; the client has not signed" lost exactly that ending,
        and the opening was then presented as the whole thing.

    So: dates and times are resolved in EASTERN, today is separated from earlier days, and a
    statement either appears complete or is named as omitted — never trimmed in silence.
    """
    now = now or datetime.now(EASTERN)
    today = now.strftime("%Y-%m-%d")

    def local(ts):
        try:
            d = datetime.fromisoformat(str(ts))
            return d.astimezone(EASTERN) if d.tzinfo else EASTERN.localize(d)
        except Exception:
            return None

    said = []
    for t in (turns or []):
        if t.get("role") != "user":
            continue
        text = (t.get("content") or "").strip()
        if not text or not _COMMIT_RE.search(text):
            continue
        when = local(t.get("ts"))
        said.append((when, text))

    def render(rows, header, budget, dated=False):
        if not rows:
            return ""
        lines, used, omitted = [], 0, []
        for when, text in reversed(rows[-limit:]):          # newest first while filling
            # PRIOR DAYS CARRY THEIR DATE (2026-09-10). Today's lines need only a time — the
            # header says which day that is. An earlier line showing "8:28 AM ET" with no date
            # is ambiguous the moment two days are present, and the whole point of this block
            # is that the model can tell when something was said.
            stamp = ("time unknown" if not when
                     else when.strftime("%b %-d, %-I:%M %p ET" if dated else "%-I:%M %p ET"))
            entry = "  - %s \u201c%s\u201d" % (stamp, text)
            if used + len(entry) > budget:
                omitted.append((when, text))
                continue
            lines.append(entry); used += len(entry)
        lines.reverse()                                      # present oldest-first
        out = header + "\n" + "\n".join(lines)
        if omitted:
            # Named, never silently trimmed: a partial quote presented as testimony is worse
            # than an explicit gap.
            out += ("\n  [%d earlier statement(s) did not fit and are NOT quoted here. Do not "
                    "treat this list as complete; ask before acting on anything you cannot "
                    "see.]" % len(omitted))
        return out

    today_rows = [(w, x) for w, x in said if w and w.strftime("%Y-%m-%d") == today]
    prior_rows = [(w, x) for w, x in said if not w or w.strftime("%Y-%m-%d") != today]

    today_block = render(
        today_rows,
        "\n\nWHAT BRADY HIMSELF SAID TODAY (%s, his words, newest last — these OUTRANK "
        "anything you or the board say, and you must not contradict them):" % today,
        total_chars)
    prior_block = render(
        prior_rows,
        "\n\nEARLIER DAYS, for context only (NOT today's plan — do not read a 'today' or "
        "'tomorrow' in these as meaning today):",
        max(800, total_chars // 4), dated=True)
    return today_block, prior_block


def _user_commitments(turns: list, now=None) -> str:
    today_block, prior_block = _commitment_lines(turns, now=now)
    return today_block + prior_block


def _brief_thread(turns: list) -> str:
    """The conversation as the brief should receive it."""
    kept = _collapse_near_reflushes(_collapse_reflushes(turns or []))
    return _format_thread(_budget_turns(kept, _BRIEF_THREAD_CHARS), per_turn=_BRIEF_TURN_CHARS)


def _last_brief_cutoff(kind: str, now):
    """(cutoff, a line saying what the window actually is).

    deliver_brief writes a receipt row per kind on every delivery, so `summaries.ts` for
    'brief_<kind>' is when the last one really went out. Anchoring to it means the window
    matches what Brady was told last time instead of assuming it was twenty hours ago.

    The note is not decoration: on a fallback the brief must not imply it knows what changed
    since the last one, because it does not.
    """
    from . import db
    fallback = (now.replace(hour=4, minute=0, second=0, microsecond=0) if kind == "eod"
                else now - timedelta(hours=20))
    try:
        row = db.latest_summary(f"brief_{kind}")
        ts = (row or {}).get("ts")
        if ts:
            last = datetime.fromisoformat(ts).astimezone(EASTERN)
            # A stale receipt (days old) would open a window so wide the "change" is the whole
            # board. Cap it, and say so, rather than quietly reporting a week as "since then".
            if last < now - timedelta(days=3):
                return (now - timedelta(days=3),
                        "(WINDOW: the last %s brief went out %s — more than three days ago, so "
                        "this covers the last three days only. Do not claim it covers "
                        "everything since then.)" % (kind, last.strftime("%b %-d")))
            if last <= now:
                return (last, "(WINDOW: exactly since the last %s brief, delivered %s.)"
                        % (kind, last.strftime("%b %-d at %-I:%M %p")))
    except Exception:
        pass
    return (fallback,
            "(WINDOW: no delivery receipt for a previous %s brief, so this is a fixed "
            "look-back, not a true since-the-last-brief list. Do not say 'since your last "
            "brief' as though it were exact.)" % kind)


async def compose_brief_prompt(kind: str = "morning") -> str:
    """Everything the brief knows, minus the model call — split out (2026-08-03) so the
    MAX BRIDGE can run the same brief on Brady's Claude Max plan ($0/token); this server
    only composes the inputs and delivers the finished text. '' on failure."""
    from . import db
    try:
        stats = await asyncio.to_thread(_board_stats)
        from .integrations import bills_sheet
        events_raw, wx, convo, bills_res = await asyncio.gather(
            asyncio.to_thread(get_events_structured, 2 if kind == "eod" else 1),
            get_weather(),
            asyncio.to_thread(_unified_thread, 60),   # budget-bounded below
            bills_sheet.fetch_bills(),
            return_exceptions=True,
        )
        def ok(v, d):
            return d if isinstance(v, Exception) else v
        now = datetime.now(EASTERN)
        events = ok(events_raw, [])
        # MONEY COMES FROM THE SHEET, NEVER FROM THE BOARD (2026-09-05). Bill amounts used to
        # live in board prose and rotted there: six of nine were wrong on 9/5, including an
        # electric account nine days from disconnection that the board showed as a routine
        # $276. Brady maintains the sheet anyway; Ace reads it and stores nothing. If it is
        # unreachable the brief SAYS SO rather than falling back on figures it cannot vouch for.
        _b = ok(bills_res, ([], "the sheet lookup failed"))
        _bills, _bills_err = _b if isinstance(_b, tuple) else ([], "the sheet lookup failed")
        money_block = (bills_sheet.format_due_soon(_bills, 10, now.date()) if _bills
                       else "(couldn't read the budget sheet — %s. Say so plainly; do NOT quote "
                            "bill amounts from the board, they are not maintained.)" % _bills_err)
        today_str = now.strftime("%Y-%m-%d")
        sched = _format_today_schedule([e for e in events if e.get("date") == today_str], now)
        tomorrow_block = ""
        if kind == "eod":
            tom = [e for e in events if e.get("date") != today_str][:4]
            tomorrow_block = "\n\nTOMORROW'S FIRST EVENTS:\n" + (
                "\n".join(f"- {e.get('time','')} {e.get('title','')}" for e in tom)
                or "(nothing scheduled yet)")
        mem_all = await asyncio.to_thread(brain.read_memory)
        # HIS ACTUAL GOALS, not the first six memory strings containing the word "goal"
        # (2026-09-09 audit). That string match pulled in any sentence mentioning a goal —
        # including Ace's own notes about goals — while the rows Brady deliberately files
        # under Goals, which is his real current selection, were never consulted.
        try:
            _goal_rows = [i.get("text", "") for i in await asyncio.to_thread(db.read_items, True)
                          if i.get("status") == "open"
                          and "Goals" in (i.get("tags") or [])]
        except Exception:
            _goal_rows = []
        goals = _goal_rows[:6] or [f for f in mem_all
                                   if "goal" in f.lower() and not f.startswith("ACE SELF-NOTE")][:6]
        # THE CURRENT PROFILE. Updating Ace's identity through update_profile changed how he
        # talks in chat and did nothing to the brief, because the brief never read it — which
        # is why the brief still sounded like a phase Brady had moved on from.
        profile_now = await asyncio.to_thread(load_profile)
        self_notes = [f for f in mem_all if f.startswith("ACE SELF-NOTE")][-3:]
        # Brief feedback steering — Brady's 👍/👎 votes tune tomorrow's brief
        fb = await asyncio.to_thread(db.latest_summary, "brief_feedback")
        fb_line = ""
        if fb.get("text", "").endswith(":down"):
            fb_line = "\n\nNOTE: Brady thumbed-down the last brief — tighten it, drop filler, sharper priorities."
        elif fb.get("text", "").endswith(":up"):
            fb_line = "\n\nNOTE: Brady liked the last brief — keep this style."
        # SITUATIONAL DELTA (Stage ③, 2026-08-13): hand the brief the CHANGE since the last one —
        # what Brady closed and what landed on the board — so it SYNTHESIZES the day (leads with
        # "here's what moved") instead of reciting the whole register. Morning looks back ~20h
        # (yesterday + overnight); EOD looks back to 4am (today's work). This is the fix for his
        # "briefs are generic and repetitive" complaint: no delta some days → the brief says so.
        try:
            all_items = await asyncio.to_thread(db.read_items, False)
        except Exception:
            all_items = []
        # ANCHOR THE WINDOW TO THE LAST BRIEF THAT ACTUALLY WENT OUT (2026-09-09).
        # "Since the last brief" was a fixed 20 hours, which is not the last brief — it is a
        # guess about when the last brief was. deliver_brief already writes a receipt row per
        # kind, and that row's own ts is the real delivery instant, so use it. The fixed
        # window stays as the fallback for a first run or a missing receipt, and the label
        # below SAYS which one is in force rather than implying a precision it does not have.
        cutoff, window_note = _last_brief_cutoff(kind, now)
        _CATS = ("Money", "Bills", "Opportunities", "Goals", "Personal", "Deals",
                 "Agents", "Admin", "Networking", "Business", "Tech")
        def _cat_of(it):
            return next((t for t in (it.get("tags") or []) if t in _CATS), "")
        # Compare INSTANTS, not ISO strings — ts/done_ts are UTC (+00:00) while cutoff is Eastern
        # (-04:00); a raw string >= across offsets is unsound (2026-08-19 audit). fromisoformat
        # keeps the offset, so datetime >= datetime compares the real moment correctly.
        def _after_cutoff(ts):
            try:
                return bool(ts) and datetime.fromisoformat(ts) >= cutoff
            except Exception:
                return False
        done_recent = [i for i in all_items
                       if i.get("status") == "done" and _after_cutoff(i.get("done_ts"))]
        new_recent = [i for i in all_items
                      if i.get("status") == "open" and _after_cutoff(i.get("ts"))]
        # EDITS COUNT AS CHANGE. Only creations and completions were counted, so the evening
        # Brady spent moving deadlines, naming who he was waiting on and writing next steps
        # registered as nothing happening — and the brief told him so.
        _seen = {i.get("id") for i in done_recent} | {i.get("id") for i in new_recent}
        edited = [i for i in all_items
                  if i.get("id") not in _seen and _after_cutoff(i.get("updated_at"))]
        dl = []
        if done_recent:
            dl.append("KNOCKED OUT since the last brief:")
            dl += [f"  ✓ {i['text'][:160]}" for i in done_recent[:8]]
        if new_recent:
            dl.append("NEW on the board since the last brief:")
            dl += [f"  + {i['text'][:160]}" + (f"  [{c}]" if (c := _cat_of(i)) else "")
                   for i in new_recent[:8]]
        if edited:
            dl.append("CHANGED since the last brief (he edited these — a changed deadline, "
                      "a next step, who it is parked on. NOT finished work):")
            dl += [f"  ~ {i['text'][:140]}" for i in edited[:8]]
        delta_block = ("\n".join(dl) or "(nothing closed, added or changed since then)") \
            + "\n" + window_note
        # PIVOT (2026-08-10): Brady stepped back from GFI/PFI to dig out financially. The brief
        # leads with the RECOVERY — money/tax deadlines, bills due, income moves — NOT the old
        # EMD/business chase. Never mention EMD. Keep the Gabby situation OUT of the pushed text
        # (this lands on his lock screen where she might see) unless he himself raised it today.
        if kind == "morning":
            ask = (
                # HIS DAY, NOT A FIXED NARRATIVE (2026-09-09 audit). This asked for a "FINANCIAL
                # RECOVERY brief" led by money and bills every single morning, which is how a
                # brief written forty minutes after Brady laid out his plan could spend itself on
                # the register and omit the plan. Money leads when the sheet says something is
                # actually imminent — not because the template says so.
                "You are Ace, Brady's chief of staff, writing his MORNING BRIEF. This is NOT a "
                "template to fill — it's a SYNTHESIS, and its subject is HIS DAY. Read what he "
                "himself said, the CHANGE SINCE THE LAST BRIEF, his REAL schedule, and the board "
                "data below, then tell him the true shape of today in your own words, woven "
                "naturally (never as labeled sections): a one-line human open with the weather "
                "beat; then — only if there's something real — what moved since the last brief; "
                "then the 1-3 things that GENUINELY matter today. Those come FIRST from what he "
                "said he is doing and what he chose for today, then from anything time-critical "
                "in the money data (a tax/IRS deadline, a bill due within ~3 days), blended with "
                "his actual appointments; then one honest, steadying line. If money is not "
                "actually urgent today, do not lead with it — a bill due in nine days is not "
                "today's news, and generic bill repetition must never crowd out something he "
                "just committed to. HARD RULES: surface only what's IMMINENT or decision-worthy "
                "— do NOT list every bill or open item; the board data is REFERENCE to pull "
                "from, never to recite. Vary it day to day — never open the same way twice. On a "
                "quiet day, say so plainly instead of manufacturing urgency. ~130 words. No EMD. "
                "Gabby is fully in the loop on the money, so referencing it is fine — keep the "
                "tone steady, not heavy. Plain text, short lines, no markdown, no section labels."
            )
        else:
            ask = (
                "You are Ace, Brady's recovery chief of staff, writing his END-OF-DAY recap. A "
                "SYNTHESIS, not a template. From the CHANGE SINCE THE LAST BRIEF and the thread, "
                "tell him honestly how today actually went and what tomorrow really needs, woven "
                "naturally (no section labels): what he genuinely moved today — name real things "
                "from the delta/thread, never invent progress; then the 1-3 that carry to tomorrow, "
                "led by any imminent money/tax deadline or bill due; then one steadying line toward "
                "the income floor. Surface only what matters — don't recite the board. Vary it; if "
                "it was a slow day, own it. ~100 words. No EMD. Gabby's in the loop, so shared money "
                "talk is fine. Plain text, no markdown, no section labels."
            )
        ask += _BRIEF_RULES
        return (
            f"{ask}\n\nWHO HE IS RIGHT NOW — his current profile, which he edits. This is the\n"
            f"standing context for everything below; if it disagrees with an older habit of\n"
            f"this brief, the profile wins:\n{profile_now}\n\n"
            f"CURRENT TIME: {now.strftime('%A, %B %d, %Y — %-I:%M %p')} ET\n\n"
            f"WEATHER: {_format_weather(ok(wx, {}))}\n\nTODAY'S SCHEDULE:\n{sched}{tomorrow_block}\n\n"
            f"CHANGE SINCE THE LAST BRIEF:\n{delta_block}\n\n"
            f"MONEY DUE IN THE NEXT 10 DAYS — from Brady's budget sheet, the ONLY trustworthy\n"
            f"source for any dollar figure. Board items are NOT maintained for amounts:\n{money_block}\n\n"
            f"BOARD DATA (reference — pull only what's imminent, do NOT recite, and NEVER quote a\n"
            f"dollar amount from it):\n{stats}\n\nHIS GOALS:\n"
            + ("\n".join(f"- {g}" for g in goals) or "(none)")
            + ("\n\nACE'S OWN GROWTH NOTES (mention max ONE, casually, only if morning):\n"
               + "\n".join(f"- {n}" for n in self_notes) if self_notes else "")
            + fb_line
            + _user_commitments(ok(convo, []), now=now)
            + "\n\nRECENT THREAD (for what happened):\n" + _brief_thread(ok(convo, [])))
    except Exception as e:
        logger.warning("compose_brief_prompt(%s) failed: %s", kind, e)
        return ""


async def deliver_brief(kind: str, text: str) -> str:
    """Deliver one finished brief through the ONE guarded path: thread + day marker +
    phone push + HUD publish. Used by generate_brief AND the MAX BRIDGE — so however
    the brief got written, delivery (and double-send protection) is identical."""
    from . import db
    text = (text or "").strip()
    if not text:
        return ""
    now = datetime.now(EASTERN)
    label = "☀ MORNING BRIEF" if kind == "morning" else "◈ EVENING RECAP"
    delivered = f"{label}\n\n{text}"
    await asyncio.to_thread(history.append, "assistant", delivered)
    await asyncio.to_thread(db.add_summary, now.strftime("%Y-%m-%d"), f"brief_{kind}")
    _brief_sent[kind] = now.strftime("%Y-%m-%d")
    logger.info("brief delivered: %s (%d chars)", kind, len(text))
    # …and to the PHONE. The HUD push (publish_stage_event) only reaches an OPEN tab;
    # this is the one that lands on a locked screen. Fire-and-forget, threaded, and
    # completely inert until the VAPID keys are set — a brief never waits on it and
    # never fails because of it.
    try:
        from .main import send_push
        title = "ACE · Morning Brief" if kind == "morning" else "ACE · Evening Recap"
        # DISCREET MODE: keep dollar figures OFF the lock screen — send a generic teaser and
        # keep the real brief inside the app (it's in the thread + HUD already).
        body = ("Your game plan's ready — open Ace to read it." if _discreet[0]
                else text)
        send_push(title, body, "/", tag="brief")
    except Exception as e:
        logger.warning("brief phone push skipped: %s", e)
    try:
        from .main import publish_stage_event
        await publish_stage_event("brief", {"kind": kind, "text": text})
    except Exception:
        pass
    return delivered


async def generate_brief(kind: str = "morning") -> str:
    """Compose → model → deliver (the in-server / API fallback path)."""
    try:
        prompt = await compose_brief_prompt(kind)
        if not prompt:
            return ""
        client = _anthropic()
        resp = await client.messages.create(
            model=LEARN_MODEL, max_tokens=400,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        return await deliver_brief(kind, text)
    except Exception as e:
        logger.warning("generate_brief(%s) failed: %s", kind, e)
        return ""


async def generate_business_report() -> str:
    """THE 'HOW'S MY BUSINESS?' REPORT — on-demand deep snapshot: full pipeline by
    category with ages, wins (30d), goals vs pace, week ahead, and straight talk on
    what needs attention. Deterministic numbers first; the deep model writes the read."""
    from . import db
    try:
        now = datetime.now(EASTERN)
        items, facts, events, convo = await asyncio.gather(
            asyncio.to_thread(db.read_items, False),
            asyncio.to_thread(db.read_facts_full),
            asyncio.to_thread(get_events_structured, 7),
            asyncio.to_thread(_unified_thread, 12),
            return_exceptions=True,
        )
        def ok(v, d):
            return d if isinstance(v, Exception) else v
        items, facts, events = ok(items, []), ok(facts, []), ok(events, [])
        _CATS = ["Money", "Bills", "Opportunities", "Goals", "Personal", "Deals", "Agents", "Admin", "Networking", "Business", "Tech"]
        def cat(it):
            for t in (it.get("tags") or []):
                if t in _CATS:
                    return t
            return "Admin"
        lines = []
        # PULSE RETUNE (2026-08-23, Brady): "any money-making moves — GFI deals, business owners
        # I'm helping, etc." Not just the old GFI Deals/Agents lens: Money actions, Opportunities
        # (income), and Business (Damon/website/contract work) are all revenue surface now.
        for c in ["Money", "Deals", "Business", "Opportunities", "Agents"]:
            rows = []
            for i in items:
                if i.get("status") == "open" and cat(i) == c:
                    try:
                        age = (now - datetime.fromisoformat(i["ts"])).days
                    except Exception:
                        age = "?"
                    rows.append(f"  - {i.get('text','')[:70]} [{age}d old]")
            lines.append(f"{c.upper()} ({len(rows)} open):\n" + ("\n".join(rows) or "  (none)"))
        goals = [i.get("text", "") for i in items if i.get("status") == "open" and cat(i) == "Goals"]
        lines.append("GOALS:\n" + ("\n".join(f"  - {g[:70]}" for g in goals) or "  (none)"))
        wins = []
        for f in facts:
            t = f.get("text", "")
            if (t.startswith("Deal won:") or t.startswith("Goal reached:")) and not f.get("invalid_at"):
                try:
                    if (now - datetime.fromisoformat(f["ts"]).astimezone(EASTERN)).days <= 30:
                        wins.append(f"  - {t[:75]}")
                except Exception:
                    pass
        lines.append("WINS (last 30 days):\n" + ("\n".join(wins) or "  (none logged yet)"))
        week = "\n".join(f"  - {e.get('date','')} {e.get('time','')} {e.get('title','')[:55]}"
                         for e in events[:14]) or "  (clear)"
        lines.append("WEEK AHEAD:\n" + week)
        client = _anthropic()
        resp = await client.messages.create(
            model=LEARN_MODEL, max_tokens=900,
            messages=[{"role": "user", "content": (
                "You are Ace giving Brady the straight 'where's the money' read — his chief of "
                "staff across EVERYTHING he runs, not one company. His income surface right now: "
                "GFI/insurance deals he's closing out, concrete work + business owners he's helping "
                "(Damon etc.), contract/CRM clients, the job hunt (income floor), and any new "
                "opportunity in play (e.g. a recruiting offer being evaluated). From the REAL data "
                "below, write: (1) THE HEADLINE — one honest sentence on his money picture this "
                "week; (2) MONEY MOVES — every move that can put dollars in this month, ranked by "
                "$ x closeness, calling out anything stalled 10+ days by name; (3) OPPORTUNITIES — "
                "new/open income plays and what each needs next; (4) THE ONE MOVE — the single "
                "highest-leverage money action right now. Punchy, ~220 words max, plain text with "
                "short section labels. Never invent numbers not in the data. No EMD pacing.\n\n"
                f"TODAY: {now.strftime('%A, %B %d, %Y')}\n\n" + "\n\n".join(lines)
                + "\n\nRECENT CONTEXT:\n" + _format_thread(ok(convo, [])))}])
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if text:
            delivered = f"📊 BUSINESS PULSE\n\n{text}"
            await asyncio.to_thread(history.append, "assistant", delivered)
            return delivered
        return ""
    except Exception as e:
        logger.warning("business report failed: %s", e)
        return ""


_brief_sent: dict = {}   # process-local claim: {kind: "YYYY-MM-DD"} — survives db outages


# ── BRIDGE LEASES (2026-09-08) ──────────────────────────────────────────────────
# The bridge used to claim a brief PERMANENTLY the instant it handed the prompt over:
# it wrote the brief_{kind} marker before the worker had done anything. When the worker
# then failed — 52 times because the Claude CLI was never signed in, 10 more when the
# result POST timed out — the brief was simply gone for the day, and the in-server loop
# skipped it because the marker said "sent".
#
# A lease keeps the double-send protection without the data loss: while it is fresh, the
# in-server loop stays out of the way; once it expires unacknowledged, the loop takes the
# job back on the metered API. Worst case is a brief that arrives late on the API instead
# of one that never arrives at all.
BRIDGE_LEASE_SEC = int(os.environ.get("ACE2_BRIDGE_LEASE", "900"))
_bridge_leases: dict = {}


def bridge_lease_take(key: str) -> str:
    """Reserve a job for the bridge worker for BRIDGE_LEASE_SEC. Returns an owner token so
    a completion arriving after the lease lapsed (and after the server took the job back)
    can be recognised as late rather than applied blindly."""
    token = uuid.uuid4().hex[:16]
    _bridge_leases[key] = {"until": time.time() + BRIDGE_LEASE_SEC, "token": token}
    return token


def bridge_lease_active(key: str) -> bool:
    """True while a bridge worker still has a live reservation on this job."""
    lease = _bridge_leases.get(key)
    if not lease:
        return False
    if time.time() >= lease["until"]:
        _bridge_leases.pop(key, None)
        return False
    return True


def bridge_lease_token(key: str) -> str:
    """The current owner token, or '' when nothing holds this job."""
    lease = _bridge_leases.get(key)
    return lease["token"] if lease and time.time() < lease["until"] else ""


def bridge_lease_release(key: str) -> None:
    _bridge_leases.pop(key, None)


async def _brief_loop() -> None:
    """Fire the morning/EOD briefs at their Eastern times — once per day each, INSIDE a
    2-hour window (a late boot never sends a 'morning' brief at 11pm). Claim-first via a
    local guard + db marker so restarts, deploy overlaps, and db outages never double-send
    or spam-generate."""
    from . import db
    while True:
        try:
            await asyncio.sleep(60)
            if not db.enabled():
                continue
            now = datetime.now(EASTERN)
            today = now.strftime("%Y-%m-%d")
            cur = now.hour * 60 + now.minute
            for kind, (hh, mm) in _BRIEF_TIMES.items():
                if not (0 <= cur - (hh * 60 + mm) <= 120):
                    continue   # only within 2h of the target — missed windows are skipped
                if _brief_sent.get(kind) == today:
                    continue   # local claim (also caps retries if db reads fail)
                if bridge_lease_active(f"brief:{kind}"):
                    continue   # the Max-plan worker holds it; take over when the lease lapses
                last = await asyncio.to_thread(db.latest_summary, f"brief_{kind}")
                if (last.get("text") or "") == today:
                    _brief_sent[kind] = today
                    continue   # already sent (other container / before restart)
                # ONE CLAIM, SHARED WITH THE BRIDGE (2026-09-08). The loop and the bridge
                # worker used separate guards — a process-local flag here, a journal row
                # there — so a lease that lapsed mid-flight could have both of them deliver
                # the same brief. They now compete for the SAME durable claim, and exactly
                # one wins it.
                from . import ops as _ops
                verdict, attempt, _prior = await asyncio.to_thread(
                    _ops.begin, "bridge_deliver", _ops.brief_claim(kind, today), 86400)
                if verdict != "execute":
                    continue   # the bridge holds it, already delivered it, or it is unclear
                _brief_sent[kind] = today
                # The durable claim provides exclusion. A delivery marker must only be
                # written by deliver_brief after generation actually produces a result.
                try:
                    delivered = await generate_brief(kind)
                    await asyncio.to_thread(
                        _ops.settle, attempt,
                        _ops.COMPLETED if delivered else _ops.UNKNOWN,
                        f"brief {kind} delivered by the in-server loop" if delivered else
                        f"brief {kind} returned no delivery receipt; verify before retrying")
                except Exception:
                    await asyncio.to_thread(_ops.settle, attempt, _ops.UNKNOWN,
                                            f"brief {kind} delivery interrupted")
                    raise
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ── AMBIENT WATCH — Ace notices things and speaks up UNASKED ────────────────────
# The other half of proactive: the briefs are scheduled, this one is ambient. Every pass
# diffs the world DETERMINISTICALLY first (email ids, event fingerprints, deal ages) and
# spends a model call ONLY when something actually moved — an idle afternoon costs nothing.
# The model's main job is to say NOTHING: an interruption Brady never asked for has to earn
# its place, and a chatty watcher gets muted, which kills the whole feature. Hence the caps.
_WATCH_INTERVAL = 30 * 60.0             # LEAN MODE: pass ~every 30 min (was 12 — caps still gate nudges)
_WATCH_WAKE = (7 * 60, 21 * 60 + 30)    # 7:00–21:30 Eastern — he is NEVER pinged outside this
_WATCH_MAX_DAY = 3                      # a 4th nudge in one day is noise, not help
_WATCH_GAP_SEC = 45 * 60                # ...and never two back-to-back
_WATCH_COLD_DAYS = 10                   # a Deal untouched this long is going cold
_watch_running = [False]
_watch_last_ts = [0.0]   # process-local floor on the gap, so a failed db read can't unmute him

# Sender words that say nothing about WHO wrote: without this "PFI Support" matches the board
# on "support" and every no-reply blast reads like a client getting in touch.
_WATCH_GENERIC = {
    "noreply", "reply", "team", "support", "info", "notifications", "notification", "news",
    "newsletter", "mail", "email", "admin", "service", "services", "customer", "account",
    "accounts", "sales", "billing", "alert", "alerts", "hello", "help", "update", "updates",
    "care", "office", "contact", "group", "response", "invoice", "receipt", "llc", "inc",
}


def _watch_known_sender(sender: str, vocab: set) -> bool:
    """True when a sender's name already shows up in Ace's own world (facts + board) — the
    line between 'a client just emailed' and 'a vendor blasted a list'."""
    import re
    return any(w in vocab for w in re.findall(r"[a-z0-9]+", (sender or "").lower())
               if len(w) >= 4 and w not in _WATCH_GENERIC)


# Due-date parsing now lives canonically in db.parse_due and read_items computes due_days per
# item, so the watchdog just reads it['due_days'] (2026-08-13 — one parser for brief/watch/UI).


def _watch_scan(inbox: list, events: list, items: list, facts: list, prior: dict, now: datetime):
    """The cheap deterministic half: what a person would actually NOTICE since the last pass.
    Returns (signals, state) — state is the fingerprint set handed to the next pass, so each
    thing is noticed exactly once no matter how many passes see it."""
    import re
    vocab = set()
    for t in list(facts) + [i.get("text", "") for i in items]:
        vocab.update(w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) >= 4)
    prior_mail = set(prior.get("emails") or [])
    prior_ev = dict(prior.get("events") or {})
    flagged = list(prior.get("flagged") or [])
    fset = set(flagged)
    signals = []

    for m in inbox:
        mid = m.get("id") or ""
        if not mid or mid in prior_mail or not _watch_known_sender(m.get("from", ""), vocab):
            continue   # a stranger's cold email is not worth his attention; a name he knows is
        signals.append(f"NEW EMAIL from {m.get('from', '?')} — \"{m.get('subject', '')}\" — "
                       f"{m.get('snippet', '')[:100]}")

    ev_state, horizon = {}, now + timedelta(hours=24)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    for e in events:
        key = f"{e.get('title', '')}|{e.get('date', '')}"
        ev_state[key] = e.get("iso", "")
        try:
            start = datetime.fromisoformat(e["iso"])
        except Exception:
            continue
        # All-day items have a midnight start, so the clock window would always call them past.
        if e.get("all_day"):
            in_window = e.get("date", "") in (today_str, tomorrow_str)
        else:
            in_window = (now - timedelta(minutes=5)) <= start <= horizon
        if not in_window:
            continue
        was = prior_ev.get(key)
        if was is None:
            signals.append(f"CALENDAR ADDED: {e.get('day_label', '')} {e.get('time', '')} — "
                           f"{e.get('title', '')}")
        elif was != e.get("iso", ""):
            signals.append(f"CALENDAR MOVED: {e.get('title', '')} → {e.get('day_label', '')} "
                           f"{e.get('time', '')}")
        mins = int((start - now).total_seconds() // 60)
        if not e.get("all_day") and 0 <= mins <= 30 and key not in fset:
            signals.append(f"STARTS IN {mins} MIN: {e.get('time', '')} — {e.get('title', '')}")
            flagged.append(key)
            fset.add(key)

    # RECOVERY WATCH (2026-08-11 review fix): the old signal nudged 'Deals going cold' — but
    # deals are back-burner now. Repoint at what matters: BILLS DUE and MONEY DEADLINES within
    # the next 3 days, parsed from the item's text/due. Fired once per (item, due-date).
    for it in items:
        cat = next((t for t in (it.get("tags") or []) if t in ("Money", "Bills")), None)
        if not cat:
            continue
        days = it.get("due_days")   # deterministic, computed in db.read_items
        if days is None or not (0 <= days <= 3):
            continue
        key = f"due:{it.get('id', '')}:{it.get('due_on', '')}"
        if key in fset:
            continue
        when = "TODAY" if days == 0 else ("TOMORROW" if days == 1 else f"in {days} days")
        label = "BILL DUE" if cat == "Bills" else "MONEY DEADLINE"
        signals.append(f"{label} {when}: {it.get('text', '')[:80]}")
        flagged.append(key)
        fset.add(key)

    mail_state = [m.get("id") for m in inbox if m.get("id")]
    mail_state += sorted(prior_mail - set(mail_state))   # sorted → a stable blob, so state
    return signals, {                                    # writes happen only on real change
        "emails": mail_state[:80], "events": ev_state, "flagged": flagged[-60:],
    }


async def _watch_pass_once(force: bool = False, dry_run: bool = False) -> dict:
    """One ambient pass: deterministic diff → at most ONE model call → at most one nudge.
    Returns the decision (this is what /watch/run reports). force skips the quiet-hours and
    rate-limit gates for testing; dry_run decides but delivers nothing and keeps the state."""
    if _watch_running[0]:
        return {"nudged": False, "skipped": "busy"}
    _watch_running[0] = True
    try:
        from . import db
        if not db.enabled():
            return {"nudged": False, "skipped": "no db"}
        now = datetime.now(EASTERN)
        cur = now.hour * 60 + now.minute
        if not force and not (_WATCH_WAKE[0] <= cur <= _WATCH_WAKE[1]):
            return {"nudged": False, "skipped": "quiet hours"}
        today = now.strftime("%Y-%m-%d")
        # ONE marker carries both caps: its text is today's count, its ts is the last nudge.
        mark = await asyncio.to_thread(db.latest_summary, "nudge_count")
        sent_today = 0
        if (mark.get("text") or "").startswith(today):
            try:
                sent_today = int(mark["text"].split(":")[-1])
            except Exception:
                sent_today = _WATCH_MAX_DAY   # unreadable marker → assume spent and stay quiet
        last_ts = _watch_last_ts[0]
        if mark.get("ts"):
            try:
                last_ts = max(last_ts, datetime.fromisoformat(mark["ts"]).timestamp())
            except Exception:
                pass
        if not force:
            if sent_today >= _WATCH_MAX_DAY:
                return {"nudged": False, "skipped": "daily cap"}
            if (time.time() - last_ts) < _WATCH_GAP_SEC:
                return {"nudged": False, "skipped": "too soon"}

        inbox, events, items, facts, prior_raw = await asyncio.gather(
            asyncio.to_thread(get_inbox_structured, 10),
            asyncio.to_thread(get_events_structured, 2),
            asyncio.to_thread(db.read_items, False),
            asyncio.to_thread(db.read_facts),
            asyncio.to_thread(db.latest_summary, "watch_state"),
            return_exceptions=True,
        )

        def ok(v, d):
            return d if isinstance(v, Exception) or v is None else v

        inbox, events, facts = ok(inbox, []), ok(events, []), ok(facts, [])
        items = [i for i in ok(items, []) if i.get("status") == "open"]
        try:
            prior = json.loads(ok(prior_raw, {}).get("text") or "{}")
        except Exception:
            prior = {}
        signals, state = _watch_scan(inbox, events, items, facts, prior, now)
        if not prior:
            # First pass ever (or after a wipe) — everything looks new. Set the baseline and
            # stay silent; nobody wants a nudge storm the minute a deploy lands.
            if not dry_run:
                await asyncio.to_thread(db.add_summary, json.dumps(state, sort_keys=True), "watch_state")
            return {"nudged": False, "skipped": "baseline set", "signals": signals}
        blob = json.dumps(state, sort_keys=True)
        if not dry_run and blob != json.dumps(prior, sort_keys=True):
            # Consume the signals BEFORE deciding: whatever the model does with them, the same
            # email/event never gets re-argued every 12 minutes for the rest of the day.
            await asyncio.to_thread(db.add_summary, blob, "watch_state")
        if not signals:
            return {"nudged": False, "skipped": "nothing changed"}

        sched = _format_today_schedule([e for e in events if e.get("date") == today], now)
        board = "\n".join(f"- [{','.join(i.get('tags') or []) or 'untagged'}] {i.get('text', '')[:70]}"
                          for i in items[:25]) or "(empty)"
        thread = _format_thread(await asyncio.to_thread(_unified_thread, 8))
        client = _anthropic()
        resp = await client.messages.create(
            model=LEARN_MODEL, max_tokens=120,
            messages=[{"role": "user", "content": (
                "You are Ace's ambient watch — you noticed something and now you decide whether "
                "it is worth INTERRUPTING Brady for, unprompted, mid-work. Silence is the default "
                "and the right answer most of the time: he gets at most 3 of these in a day.\n"
                "Speak up only for what a sharp chief of staff would tap him on the shoulder for "
                "RIGHT NOW: a client or agent he knows sent something that needs him, a meeting "
                "that moved or starts within the hour with nothing prepped for it, a live deal "
                "going cold. Stay silent for newsletters and vendors, anything he obviously "
                "already knows (check the recent thread), anything that can wait for tonight's "
                "recap or tomorrow's brief, and anything you'd only say to look useful.\n"
                "If it clears that bar, reply with EXACTLY ONE sentence, 20 words max, in his "
                "register: direct, specific, names and numbers, no greeting, no 'just wanted to', "
                "no offer to help, no question. Otherwise reply with the single word NOTHING.\n\n"
                f"TIME: {now.strftime('%A, %B %-d — %-I:%M %p')} ET\n\n"
                "WHAT CHANGED SINCE THE LAST PASS:\n" + "\n".join(f"- {s}" for s in signals)
                + f"\n\nTODAY'S SCHEDULE:\n{sched}\n\nHIS OPEN BOARD:\n{board}"
                + f"\n\nRECENT THREAD (never repeat something already said here):\n{thread}")}])
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        text = text.split("\n")[0].strip().strip('"').strip()[:220]
        if not text or text.upper().startswith("NOTHING"):
            return {"nudged": False, "skipped": "not worth interrupting", "signals": signals}
        delivered = f"◈ HEADS UP — {text}"
        if dry_run:
            return {"nudged": False, "dry_run": True, "text": delivered, "signals": signals}
        # CLAIM FIRST — bump the counter before delivering, so a deploy-overlap twin that
        # decides the same thing in the same minute can't double-tap him.
        if not await asyncio.to_thread(db.add_summary, f"{today}:{sent_today + 1}", "nudge_count"):
            return {"nudged": False, "skipped": "claim failed", "signals": signals}
        _watch_last_ts[0] = time.time()
        await asyncio.to_thread(history.append, "assistant", delivered)
        try:
            from .main import publish_stage_event
            await publish_stage_event("nudge", {"text": delivered})
        except Exception:
            pass
        # …and to the PHONE (Stage ⑦, 2026-08-13). The HUD nudge only reaches an OPEN tab — this
        # is the one that lands on a locked screen when he's away from the desk (the concrete-pour
        # case). Discreet mode keeps the specifics (dollars, names) OFF the lock screen: generic
        # teaser out, full text stays in-app. Fire-and-forget; inert until VAPID keys are set.
        try:
            from .main import send_push
            body = ("Ace has a heads-up for you — open to read." if _discreet[0] else text)
            send_push("ACE · Heads-up", body, "/", tag="nudge")
        except Exception as e:
            logger.warning("nudge phone push skipped: %s", e)
        logger.info("nudge delivered (%d today): %s", sent_today + 1, text[:80])
        return {"nudged": True, "text": delivered, "count_today": sent_today + 1, "signals": signals}
    except Exception as e:
        logger.warning("watch pass failed: %s", e)
        return {"nudged": False, "error": str(e)}
    finally:
        _watch_running[0] = False


async def _watch_loop() -> None:
    """Sleeps FIRST so a boot or redeploy never fires a nudge before the caches are warm."""
    while True:
        try:
            await asyncio.sleep(_WATCH_INTERVAL)
            await _watch_pass_once()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ── DETERMINISTIC REMINDERS — the RELIABLE due-soon ping (2026-08-16) ─────────────
# The watchdog above is DISCRETIONARY: a model decides whether something is worth
# interrupting Brady for, and it defaults to silence (max 3/day). Brady asked for reliable
# reminders "outside the briefs" and got none — because each watch signal fires ONCE, to an
# open tab, and the model usually stays quiet. THIS is the other half: a deterministic,
# once-a-day phone push of open PRIORITY items that are overdue or due today/tomorrow. No
# model gate — it ALWAYS fires when something's due. Runs early afternoon so it catches what
# the morning brief flagged but he hasn't closed yet.
_REMINDER_TIME = (13, 0)          # 1:00pm ET
_REMINDER_WINDOW_MIN = 180        # may fire anytime in the 3h after the target, once/day
_reminder_sent = {"date": None}   # process-local claim, mirrors the db marker


def _reminder_scan(items: list) -> list:
    prio = ("Money", "Bills", "Opportunities", "Goals", "Personal")
    hot = [i for i in items if i.get("status") == "open"
           and any(t in prio for t in (i.get("tags") or []))
           and i.get("due_days") is not None and i["due_days"] <= 1]
    hot.sort(key=lambda x: x["due_days"])
    return hot


async def _reminder_once(force: bool = False, dry_run: bool = False) -> dict:
    """One reminder pass. force skips the time-window + daily-claim gates (for the test hook);
    dry_run builds the message but delivers nothing and claims nothing."""
    from . import db
    if not db.enabled():
        return {"skipped": "no db"}
    now = datetime.now(EASTERN)
    today = now.strftime("%Y-%m-%d")
    cur = now.hour * 60 + now.minute
    if not force:
        tgt = _REMINDER_TIME[0] * 60 + _REMINDER_TIME[1]
        if not (0 <= cur - tgt <= _REMINDER_WINDOW_MIN):
            return {"skipped": "outside window"}
        if _reminder_sent.get("date") == today:
            return {"skipped": "already today"}
        mark = await asyncio.to_thread(db.latest_summary, "reminder_sent")
        if (mark.get("text") or "") == today:
            _reminder_sent["date"] = today
            return {"skipped": "already today (db)"}
    items = await asyncio.to_thread(db.read_items, False)
    hot = _reminder_scan(items)
    if not hot:
        return {"skipped": "nothing due", "count": 0}   # don't claim — let it fire if one crosses later
    def _lbl(d):
        return "overdue" if d < 0 else "today" if d == 0 else "tomorrow"
    def _short(t, cap=48):
        t = (t.split("—")[0].split(" - ")[0]).strip()
        if len(t) <= cap:
            return t
        cut = t[:cap].rsplit(" ", 1)[0]   # trim on a word boundary — no more "double-che"
        return (cut if len(cut) >= 20 else t[:cap]) + "…"
    parts = [f"{_short(i['text'])} ({_lbl(i['due_days'])})" for i in hot[:4]]
    body = "Due now — " + "; ".join(parts) + (f" +{len(hot) - 4} more" if len(hot) > 4 else "")
    if dry_run:
        return {"would_push": True, "body": body, "count": len(hot)}
    # CLAIM FIRST so a deploy-overlap twin can't double-push the same day
    _reminder_sent["date"] = today
    await asyncio.to_thread(db.add_summary, today, "reminder_sent")
    await asyncio.to_thread(history.append, "assistant", "⏰ REMINDER — " + body)
    try:
        from .main import send_push
        push_body = ("You've got items due — open Ace." if _discreet[0] else body)
        send_push("ACE · Due Now", push_body, "/", tag="reminder")
    except Exception as e:
        logger.warning("reminder push skipped: %s", e)
    try:
        from .main import publish_stage_event
        await publish_stage_event("nudge", {"text": "⏰ REMINDER — " + body})
    except Exception:
        pass
    logger.info("reminder pushed (%d due): %s", len(hot), body[:90])
    return {"pushed": True, "body": body, "count": len(hot)}


async def _reminder_loop() -> None:
    """Sleeps first (like the others). The time-window + once-a-day db claim gate delivery, so
    a 5-min tick is cheap and just means 'fire promptly once we're in the afternoon window'."""
    while True:
        try:
            await asyncio.sleep(300)
            await _reminder_once()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _refresh_recap() -> None:
    """Keep the 'where we left off' recap warm in _CTX, and REGENERATE it (compaction) when it's
    stale and there are newer turns. So context COMPOUNDS: he opens every conversation knowing
    the decisions, open loops, and what's pending — never a cold reload. Background only; never
    on a turn's hot path. One at a time (_recap_running)."""
    if _recap_running[0]:
        return
    _recap_running[0] = True
    try:
        from . import db
        if not db.enabled():
            return
        latest = await asyncio.to_thread(db.latest_summary, "recap")
        if latest.get("text"):
            _CTX["recap"] = latest["text"]        # keep the current recap warm for voice
        fresh = False
        if latest.get("ts"):
            try:
                age = (datetime.now(EASTERN) - datetime.fromisoformat(latest["ts"])).total_seconds()
                fresh = age < _RECAP_TTL
            except Exception:
                fresh = False
        if fresh:
            return
        turns = await asyncio.to_thread(db.recent_turns, 60)
        if not turns or len(turns) < 4:
            return
        convo = "\n".join(f"{t.get('role')}: {(t.get('content') or '')[:300]}" for t in turns[-50:])
        client = _anthropic()
        resp = await client.messages.create(
            model=VOICE_MODEL, max_tokens=420,
            messages=[{"role": "user", "content": (
                "From this recent conversation, write Brady's assistant a tight 'where we left off' "
                "brief so he can pick up seamlessly. Cover: decisions made, what's IN PROGRESS or "
                "waiting on someone, open loops / promised follow-ups, and anything Brady said he "
                "finished. 4-8 short concrete bullet lines (names, deals). No preamble.\n\n"
                f"CONVERSATION:\n{convo}")}])
        recap = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if recap:
            await asyncio.to_thread(db.add_summary, recap, "recap")
            _CTX["recap"] = recap
            logger.info("recap regenerated (%d chars)", len(recap))
    except Exception as e:
        logger.warning("recap refresh failed: %s", e)
    finally:
        _recap_running[0] = False


# ── ACE KNOWS WHAT CHANGED IN HIMSELF (2026-09-06) ─────────────────────────────────
# Brady: "each time we fix something on Ace he needs to know about it." Twice in one morning
# he did not — his memory still called the upgrade session a future plan, and his board
# context had no idea the records/actions model existed. Both were patched by hand, which
# does not scale and silently rots the moment someone forgets.
#
# So the changelog SHIPS WITH THE CODE and he reads the top of it. It cannot drift from what
# is deployed, because it is deployed. It rides in the SLOW half of the context — the cached
# block — so it changes only when a deploy changes it, and costs effectively nothing per turn.
_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
_CHANGELOG_ENTRIES = 5
_changelog_cache = {"mtime": 0.0, "text": ""}


def _changelog_block() -> str:
    """The most recent entries, read from disk and cached on mtime."""
    try:
        st = _CHANGELOG.stat()
        if _changelog_cache["mtime"] == st.st_mtime:
            return _changelog_cache["text"]
        raw = _CHANGELOG.read_text(errors="replace")
        body = raw.split("---", 1)[1] if "---" in raw else raw
        parts = [p.strip() for p in body.split("\n## ") if p.strip()]
        out = []
        for p in parts[:_CHANGELOG_ENTRIES]:
            out.append("- " + " ".join(p.replace("\n", " ").split())[:400])
        text = "\n".join(out)
        _changelog_cache.update(mtime=st.st_mtime, text=text)
        return text
    except Exception:
        return ""


def _recap_block() -> str:
    """The current recap for context (prefers the warm _CTX copy; falls back to a Postgres read)."""
    recap = _CTX.get("recap") or ""
    if not recap:
        try:
            from . import db
            if db.enabled():
                recap = (db.latest_summary("recap") or {}).get("text", "")
        except Exception:
            recap = ""
    return recap or "(no recap yet — this builds after your next few conversations)"


async def _fast_context() -> str:
    """Lean context for LOW-LATENCY voice, served from the pre-warmed cache (see _refresh_ctx).
    Formats the string from cached data + the LIVE clock, so CURRENT TIME is always correct
    while the heavy fetches never sit on the turn's critical path (the source of the timeouts).
    """
    now = datetime.now(EASTERN)
    if _CTX["ts"] == 0.0:
        # Cold (fresh boot, prime still in flight) → WAIT for the fetch, bounded, so the
        # first turn speaks from real context instead of a confidently empty one. The SSE
        # lead-in covers ElevenLabs while we wait; worst case we proceed lean at 8s.
        try:
            await asyncio.wait_for(_refresh_ctx(), timeout=8)
        except asyncio.TimeoutError:
            logger.warning("cold ctx wait >8s — proceeding lean this turn")
    elif (time.time() - _CTX["ts"]) > _CTX_TTL:
        # Stale → serve what we have NOW (fast); refresh in the background for the next turn.
        asyncio.create_task(_refresh_ctx())

    def ok(v, default):
        return default if isinstance(v, Exception) or v is None else v

    events = _CTX["events"]
    today_str = now.strftime("%Y-%m-%d")
    today_events = [e for e in events if e.get("date") == today_str]
    mem_list = _mem_slim(_CTX["memory"], live_entities=_live_entities(_CTX["bank"]))
    mem = _group_facts(mem_list, meta=_CTX.get("memory_meta"))
    bank = _CTX["bank"]
    wx = _CTX["wx"]
    # Continuity: the unified thread (voice + chat, date-stamped) so voice remembers
    # today's typed turns too — not just its own call and not the stale Telegram window.
    # THE WINDOW HAS TO REACH VOICE (2026-09-08, Codex). _unified_thread was widened to hold
    # a whole conversation, and this line then cut it back to 24 turns — so on the path that
    # actually needed it nothing changed. Bounded by CHARACTERS now, not by a turn count:
    # turn lengths vary hugely (a 6-second "no, don't" against a 1,600-character brain dump),
    # so a fixed count is both wasteful and unpredictable. The budget is what protects the
    # prompt; the turn cap in _unified_thread is the outer guard.
    convo_str = _format_thread(_budget_turns(_CTX["convo"], _VOICE_THREAD_CHARS),
                               per_turn=_VOICE_TURN_CHARS)
    # HARD DATE ANCHOR (Brady: voice "keeps forgetting what day it is"). The voice brain is
    # the fast/small model — it HAS the date but a passive line let it drift on live calls.
    # State it as ground truth + an explicit instruction so it can never guess or ask.
    now_line = now.strftime("%A, %B %d, %Y — %-I:%M %p")
    return "\n".join([
        f"⏰ RIGHT NOW IT IS: {now_line} (US Eastern). This is the current date and time — "
        f"it is TODAY. If Brady asks what day, date, or time it is, answer with THIS exactly. "
        f"Never guess the date, never say you're unsure, never ask him what day it is — you "
        f"always know, it is stated right here and refreshed every turn.",
        "",
        _profile_block(),
        "",
        "ACE MEMORY (durable facts about Brady and PFI):",
        mem,
        "",
        "WHERE YOU LEFT OFF (recap of your recent conversations — pick up from here, don't re-ask):",
        _recap_block(),
        "",
        f"TODAY'S SCHEDULE ({now.strftime('%A, %B %-d')} — this is his calendar for TODAY, "
        f"relative to the current time above; answer 'what's on my calendar / what's next' "
        f"from this, never say you can't see it):",
        _format_today_schedule(today_events, now),
        "",
        "CALENDAR — last week through the next 3 weeks (past events for reference, "
        "upcoming for planning; answer range questions from this directly):",
        _format_calendar_window(events, now),
        "",
        "HIS TASK BOARD (Ace's OWN store — THE task & pipeline system, what Brady sees in his "
        "Command panel. PRIORITY columns shown in full; back-burner capped — the board is BIGGER "
        "than this. To mark one done, call update_item: use its id if it's listed, otherwise put a "
        "few words in `match` and the server finds it across the whole board — never tell him an "
        "item doesn't exist just because it's not shown. Capture new tasks with capture_item):",
        _format_daybank(ok(bank, [])),
        "",
        "WEATHER RIGHT NOW:",
        _format_weather(ok(wx, {})),
        "",
        "RECENT THREAD (what you and Brady have been discussing lately, across the HUD, "
        "voice and Telegram — use it for continuity):",
        convo_str,
        "",
        "(VOICE MODE — you are speaking out loud to Brady. Everything under LIVE CONTEXT above is "
        "live and in front of you — ANSWER FROM IT directly and confidently; never say you \"can't "
        "see\" something that's here, and never tell him to open a screen for it. You ALSO have your "
        "tools on this call: use display_card to put things on his screen, get_calendar_range / "
        "search_gmail / read_gmail to pull anything not already in context, and create_calendar_event, "
        "delete_calendar_event, capture_item (add to his task board), update_item (mark a board "
        "item done — match his words to an id in HIS TASK BOARD above), send_email, draft_email, "
        "search_drive to ACT — actually do these, then tell him it's done. For anything you have no "
        "spoken tool for — CREATING or EDITING a Google Doc, building or updating a Sheet or a "
        "Slides deck, or making a shareable link — call build_on_screen with the full instruction, "
        "then tell him in one short sentence to watch his screen; NEVER say you can't do it. "
        "Sending and deleting are "
        "GATED: the tool will come back asking for confirmation — say in one short sentence exactly "
        "what you're about to do, wait for his yes, then call it again with confirmed true. "
        "Keep spoken replies short and natural — a sentence or two, no lists or markdown. When "
        "it's just conversation — Brady, or a friend he puts on the mic — BE good company: warm, "
        "a little personality, react to what they actually said, carry the thread, and toss back "
        "a natural question so it flows; never clipped or robotic, and don't narrate tools. "
        "CALL RITUALS: (1) WAKE-UP — when Brady opens with a morning greeting ('good morning, "
        "Ace' / 'morning'), come online like JARVIS: greet him back for the time of day, then ONE "
        "tight line with the weather beat and his NEXT event from the schedule above. Two short "
        "sentences max, then stop — don't dump the whole day. (2) PAUSE — if he says 'hold on', "
        "'pause', 'one sec', 'meeting mode', or starts talking to someone else: call skip_turn "
        "with NO words if you have it (otherwise say only 'Standing by.') and then stay silent — "
        "do not speak again, and never respond to background conversation, until he addresses you "
        "directly. (3) SIGN-OFF — when he's clearly done ('goodnight, Ace', 'that's all for "
        "tonight'): one short, warm sign-off line, then call end_call if you have it to hang up. "
        "Never call end_call in any other situation. (4) UNFINISHED THOUGHT — the platform ends "
        "your turn on a SHORT SILENCE, so what reaches you is sometimes only half of what Brady "
        "is saying: it trails off on a conjunction or article ('...so I need to call Ken and'), "
        "stops just before the number or name it was heading for ('...the balance is'), or is a "
        "bare lead-in he is about to build on. When the transcript reads as CUT OFF rather than "
        "finished, call skip_turn with NO words and let him land the sentence — if you don't "
        "have skip_turn, say only 'Go on.' Never answer half a sentence and never guess the "
        "missing half. Judge it in context, not by length: a short reply that genuinely ANSWERS "
        "what you just asked ('No.' right after a yes/no question) is finished — answer it. "
        "Yield only when the words themselves are incomplete.)",
    ])


STAGE_TOOLS = [t for t in tools.TOOLS if t["name"] in tools.UI_TOOLS]
# ── THE BUCKET PASS (Phase 4 tail, 2026-09-06) ─────────────────────────────────────
# Replaces a keyword classifier that got ~10% of the live board wrong in a way tuning could
# not fix: it read "Ace Ready Mix" (a concrete supplier) as Ace's own work, a row that only
# COMPARED against Groundworks as Groundworks work, and "sitting on it until his birthday" as
# a personal errand. What a row is ABOUT is not what it MENTIONS — that is a judgment call.
#
# ONE batched call for the whole unclassified backlog, not one per item, and it fills only
# EMPTY buckets so a correction Brady makes is never overwritten. Runs rarely: once the board
# is classified there is nothing left to do.
_BUCKET_MODEL = os.environ.get("ACE2_BUCKET_MODEL", "claude-haiku-4-5-20251001")


async def bucket_pass(limit: int = 60) -> dict:
    """Classify open ACTIONS that have no stored bucket. Returns a small report."""
    try:
        items = await asyncio.to_thread(daybank.read_items, False)
        todo = [i for i in items
                if i.get("status") == "open" and i.get("entry") == "action"
                and not i.get("bucket_set")][:limit]
        if not todo:
            return {"classified": 0, "note": "every open action already has a bucket"}
        from . import db as _db          # chat.py imports db per-function, not at module scope
        listing = "\n".join(f"[{i['id']}] {(i.get('text') or '')[:220]}" for i in todo)
        prompt = (
            "File each of Brady's open actions into ONE lane. The test is WHOSE TIME IT TAKES, "
            "not what the row is about — a haircut is Personal even when it is for a Groundworks "
            "interview, and buying a dress for someone's wedding is Personal even though the "
            "wedding is Damon's.\n\n"
            "GFI/PFI — his insurance book: clients, deals, commissions, prospecting, licensing, "
            "the PFI entity, the Chris Stout/Morgan offer.\n"
            "Groundworks — the CFI job he starts 9/14: Tony, training, study material, onboarding.\n"
            "Side Work — paid work for other owners: Damon's concrete and pours, Woody, his "
            "uncle's greenhouse, websites and DNS he does for them. NOTE 'Ace Ready Mix' is a "
            "CONCRETE SUPPLIER, not Ace the assistant.\n"
            "Personal — his own life and household: money, bills, taxes, family, Gabby, health, "
            "errands.\n"
            "Ace — building or fixing Ace himself: defects, upgrade sessions, deploys.\n\n"
            "A row that merely MENTIONS a lane is not in it — 'how does the offer read against "
            "Groundworks by then' is a GFI/PFI decision, not Groundworks work.\n\n"
            "Answer one line per item, exactly 'id: Lane', nothing else. Use only those five "
            "lane names.\n\n" + listing
        )
        client = _anthropic()
        r = await client.messages.create(
            model=_BUCKET_MODEL, max_tokens=1200,
            messages=[{"role": "user", "content": prompt}])
        out = "".join(getattr(b, "text", "") for b in r.content)
        valid = set(_db.BUCKETS)
        by_id = {i["id"]: i for i in todo}
        applied, skipped = 0, 0
        for line in out.splitlines():
            m = re.match(r"\s*\[?([0-9a-f]{6,8})\]?\s*[:\-]\s*(.+?)\s*$", line)
            if not m:
                continue
            iid, lane = m.group(1), m.group(2).strip().strip(".")
            if iid not in by_id or lane not in valid:
                skipped += 1
                continue
            ok, _ = await asyncio.to_thread(daybank.update_item, iid, bucket=lane)
            applied += int(bool(ok))
        logger.info("bucket pass: %d classified, %d unusable, %d candidates",
                    applied, skipped, len(todo))
        return {"classified": applied, "unusable": skipped, "candidates": len(todo)}
    except Exception as e:
        logger.warning("bucket pass failed: %s", e)
        return {"classified": 0, "error": str(e)}


# ── THE DUPLICATE JUDGE (Phase 4, 2026-09-05) ──────────────────────────────────────
# Registered into db.set_dup_judge at startup. It is only ever called when db has already
# decided the row is NOT a lexical twin AND a shortlist survived the money/date guard, so it
# runs rarely and costs about a tenth of a cent when it does.
#
# The case it exists for: two rows about Sienna's aunt's signature packet, worded so
# differently that token overlap scored 0.267 — below the pairs that must never merge. Only
# meaning separates them.
_DUP_JUDGE_MODEL = os.environ.get("ACE2_DUP_MODEL", "claude-haiku-4-5-20251001")
_dup_sync_client = None


def _dup_judge(new_text: str, candidates: list) -> str:
    """Return the id of the candidate describing the SAME real-world thing, else ''."""
    global _dup_sync_client
    try:
        from anthropic import Anthropic
        if _dup_sync_client is None:
            _dup_sync_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        listing = "\n".join(f"[{c.get('id')}] {(c.get('text') or '')[:240]}" for c in candidates)
        prompt = (
            "Brady's board must hold ONE row per real-world thing. Decide whether the NEW row "
            "is the SAME THING as one of the EXISTING rows — meaning he would UPDATE that row "
            "rather than keep both.\n\n"
            "SAME thing: the same commitment, deal, bill or task described again — later, "
            "shorter, or in different words. Wording overlap does not matter; what the row "
            "REFERS TO does.\n"
            "DIFFERENT things, even when they name the same person or account: an ACTION vs a "
            "STATE ('mail Rebecca's packet' vs 'Rebecca's payout cleared'), two separate "
            "obligations, two stages he tracks apart, or anything with a different amount or "
            "date.\n\n"
            "Answer with the bracketed id alone, or NONE. When unsure, answer NONE — a wrong "
            "merge silently destroys a row Brady wrote, which is far worse than a duplicate "
            "he can see and merge himself.\n\n"
            f"NEW:\n{new_text[:400]}\n\nEXISTING:\n{listing}"
        )
        r = _dup_sync_client.messages.create(
            model=_DUP_JUDGE_MODEL, max_tokens=16,
            messages=[{"role": "user", "content": prompt}])
        out = "".join(getattr(b, "text", "") for b in r.content).strip()
        m = re.search(r"[0-9a-f]{6,8}", out)
        return m.group(0) if m and "NONE" not in out.upper() else ""
    except Exception as e:
        logger.warning("dup judge failed (insert proceeds): %s", e)
        return ""


# ONE HANDLER FOR BOTH MOUTHS (2026-09-09). start_task replaces build_on_screen, and the
# TYPED loop gets it too — "create a spreadsheet" has to mean the same thing, run the same
# code and obey the same approval rules whether Brady says it or types it. The typed path
# keeps its MCP surface for everything else; it just stops being the only place authoring can
# happen, and stops being where a task's truth is decided.
VOICE_TOOLS = ([t for t in tools.TOOLS if t["name"] not in _VOICE_TOOL_DENY]
               + [tools.START_TASK])
# Picking which card to show is a simple call — run it on Haiku, not Opus, so the stage
# pass adds minimal cost/latency on top of every voice turn.
STAGE_MODEL = os.environ.get("ACE2_STAGE_MODEL", "claude-haiku-4-5-20251001")

STAGE_NUDGE = (
    "\n\n---\nSTAGE MODE: Brady is on a live VOICE call — he is talking, not reading. "
    "Your ONLY job right now is the screen. Call display_card (or open_url) when he asks "
    "to see something, or when a card materially helps what he just said (e.g. he asks "
    "about his schedule → show 'timeline' or 'calendar'; his to-dos → 'daybank'). If "
    "nothing should appear on screen, call NO tool at all. Do not produce spoken text — a "
    "separate process handles the reply; you only decide the visuals."
)


async def stage_pass(user_text: str, emit, prior=None):
    """Off-critical-path DISPLAY decision for a live voice turn: pick a card (if any) and
    push it to Brady's browser over the app WebSocket. Display-only tools, no thinking, one
    non-streaming round-trip. It NEVER writes to the ElevenLabs SSE stream, so it cannot
    reintroduce the first-token 'LLM Cascade Error' — that's the whole point of keeping it
    separate from the spoken reply in stream_turn(fast=True)."""
    user_text = (user_text or "").strip()
    if not user_text:
        return
    try:
        ctx = await _fast_context()
        system = build_system_prompt() + STAGE_NUDGE + "\n\n---\nLIVE CONTEXT\n" + ctx
        messages = await _load_messages(user_text, prior)
        client = _anthropic()
        final = await client.messages.create(
            model=STAGE_MODEL, max_tokens=512, system=system, messages=messages, tools=STAGE_TOOLS,
        )
        for block in final.content:
            if getattr(block, "type", "") == "tool_use" and block.name in tools.UI_TOOLS:
                await _run_ui_tool(block.name, dict(block.input), emit)
    except Exception as e:
        logger.warning("stage_pass failed: %s", e)


async def _run_and_settle(name: str, args: dict, key: str, sink: dict = None) -> str:
    """Execute one write and record what it ACTUALLY did. Runs as its own task so that a
    superseded voice turn cancelling its AWAIT cannot leave the journal unsettled — the
    thread was never stoppable anyway (see ops.py).

    The outcome comes from the executor, not from how its sentence starts. An executor
    that returns plain prose can only be recorded as REPORTED (claimed, unverified);
    only a structured Outcome can say COMPLETED.
    """
    try:
        result = await asyncio.to_thread(tools.execute, name, args)
    except Exception as e:
        # An exception AFTER the request left the process does not mean nothing happened:
        # the provider may have committed before the response was lost. Only an executor
        # that explicitly says failed_before_dispatch is safe to retry, so an unexpected
        # error against an external service is UNKNOWN — surfaced, never auto-replayed.
        state = ops.UNKNOWN if name in ops.EXTERNAL else ops.FAILED_BEFORE_DISPATCH
        text = (f"\u26a0\ufe0f {name} failed with {type(e).__name__}. "
                + ("The request may already have gone through — check before retrying."
                   if state == ops.UNKNOWN else "Nothing was sent; it is safe to try again."))
        await asyncio.to_thread(ops.settle, key, state, text)
        _sink(sink, state, "mutation")
        raise
    state, text, record_id = ops.classify(result)
    _sink(sink, state, "mutation")
    await asyncio.to_thread(ops.settle, key, state, text, record_id)
    return text


def _sink(sink, state, kind: str = ""):
    """Record the journal's verdict where the turn can read it. The verdict is the evidence —
    an interrupted turn must not re-derive "did this happen" from the sentence it produced.

    `kind` is what the tool was DECLARED to be (read / mutation / unclassified), carried
    alongside the verdict so the call site does not have to re-guess it from the name."""
    if sink is not None:
        sink["state"] = state
        if kind:
            sink["kind"] = kind


async def _dispatch_write(name: str, args: dict, sink: dict = None) -> str:
    """Run a write exactly once, whatever ElevenLabs does to the turn around it.

    Barge-in is preserved: the caller is still cancellable and Ace still stops talking.
    What changes is that the WRITE keeps its own lifecycle — shielded, then settled by
    its own task — so an interrupted turn leaves an accurate durable record instead of
    an invisible maybe. An interrupted-with-unknown-outcome action is reported, never
    silently replayed."""
    if name not in ops.JOURNALLED:
        raw = await asyncio.to_thread(tools.execute, name, args)
        # NO JOURNAL MEANS NO VERDICT (2026-09-10). This used to sink ops.COMPLETED for a
        # native read, which is a false verdict: the journal never said that, and the only
        # thing keeping it from being spoken as "this went through" was classify_result
        # short-circuiting reads first. One refactor of that order and a calendar read
        # becomes a completion claim again. The sink now records what the EXECUTOR said —
        # ops.classify, the same rule the journalled path uses, which can only reach
        # COMPLETED from a structured Outcome and never from prose.
        state, out, _rid = ops.classify(raw)
        kind = ("read" if name in tools.NATIVE_READS else
                "mutation" if name in tools.NATIVE_MUTATIONS else "unclassified")
        if kind == "unclassified":
            # tools.py's two frozensets are meant to partition the native surface (pinned by
            # test_conversation_context). Landing here means someone added a native tool and
            # declared nothing about it — it is treated as a possible mutation with an
            # unknown outcome, which is the safe default, but the gap is said out loud
            # rather than left to be discovered from a wrong receipt.
            logger.warning("native tool %s is in neither NATIVE_READS nor NATIVE_MUTATIONS; "
                           "treating it as a possible mutation with an unknown outcome", name)
        _sink(sink, state, kind)
        return out
    verdict, key, prior_receipt = await asyncio.to_thread(ops.begin, name, args)
    if verdict == "unavailable":
        # Fail CLOSED. Without the journal we cannot tell a redelivery from a new request,
        # and for an external provider that means a silent double-booking. Say plainly that
        # nothing was attempted, so the request is preserved rather than half-claimed.
        _sink(sink, ops.UNAVAILABLE, "mutation")
        return ("\u26a0\ufe0f NOT DONE — I could not reach the record that stops this from "
                "being done twice, so I did not attempt it. Tell Brady it still needs doing "
                "and try again shortly. Do not claim it happened.")
    if verdict == "duplicate":
        _sink(sink, ops.COMPLETED, "mutation")   # it DID happen, just not on this turn
        return prior_receipt or "Already done moments ago — not repeated."
    if verdict == "in_flight":
        _sink(sink, ops.UNKNOWN, "mutation")
        return ("STOP: this exact action is already running from a previous turn. Do not "
                "send it again. Say it is in progress and wait.")
    if verdict == "unknown":
        _sink(sink, ops.UNKNOWN, "mutation")
        return "\u26a0\ufe0f " + (prior_receipt or "Previous attempt's outcome is unknown.")
    task = asyncio.create_task(_run_and_settle(name, args, key, sink))
    try:
        return str(await asyncio.shield(task))
    except asyncio.CancelledError:
        # The write is still in flight and will settle itself. Do NOT mark it failed:
        # "cancelled the await" is not "the write did not happen".
        logger.info("write %s superseded mid-flight; op %s settles independently",
                    name, key[:8])
        raise


# ── WHAT A TOOL RESULT ACTUALLY MEANS ──────────────────────────────────────────
# Sniffing prose for a warning glyph is not classification. The MCP branch answers a refusal
# with "NOT AVAILABLE — …" and a redirect with "USE start_task INSTEAD…", neither of which
# starts with the glyph — so both survived the filter and were listed to Brady under "these
# did go through". A handler whose entire job is honesty was manufacturing completion claims.
# (Codex, 2026-09-10.)
OP_DONE, OP_FAILED, OP_QUEUED, OP_READ = "done", "failed", "queued", "read"
# A proposal parked in the Review tray DEFINITELY did not execute — it is waiting on Brady.
# It needs its own state: OP_FAILED would say it was refused, and OP_UNKNOWN would say it
# "may have taken effect", which is the opposite of true. (Reviewer's open item 2.)
OP_REVIEW = "review"
OP_UNKNOWN = "unknown"          # it ran; whether it changed anything is NOT established
OP_UI = "ui"                    # painted a screen; nothing in anyone's data changed at all

# Exact openings this codebase uses to mean "this did not happen". Still checked, but only as
# one source of NEGATIVE evidence — never as the thing that decides success.
_REFUSAL_OPENINGS = (
    "\u26a0", "NOT AVAILABLE", "USE start_task INSTEAD", "NOT COMPLETED", "STOP:",
    "NOT STARTED", "Review storage unavailable", "No task text came through",
)

# The journal's own verdicts, mapped to what a surface may say. ops already draws exactly the
# distinctions that matter, including REPORTED = "the executor claimed it, nothing verified".
_OPS_MEANING = {
    ops.COMPLETED: OP_DONE,
    ops.REPORTED: OP_UNKNOWN,
    ops.UNKNOWN: OP_UNKNOWN,
    ops.FAILED_BEFORE_DISPATCH: OP_FAILED,
    ops.UNAVAILABLE: OP_FAILED,
    ops.NEEDS_REVIEW: OP_FAILED,
}


def classify_result(name: str, text: str, is_read: bool = False,
                    outcome: str = "", card_state: str = "") -> str:
    """What one tool call achieved, from EVIDENCE rather than from its prose.

    Rewritten after Codex pointed out the previous version was still a refusal-prefix sniffer
    whose default was success — so any answer it did not recognise became "this went through".
    Success is now positive-only: the journal's verdict, a declared read, or a task card's
    real state. Anything unrecognised is UNKNOWN, which no surface reports as done.

    ORDER MATTERS, and it changed on 2026-09-10. The prose check used to run FIRST, which
    meant it could overrule real evidence: `_dispatch_write` returns "⚠️ …" for a write whose
    previous attempt was interrupted, and sinks ops.UNKNOWN beside it — the glyph won, the op
    was recorded as a clean failure and the interrupted note dropped it entirely. A write that
    may have gone through was reported as one that certainly did not. Structured evidence (a
    card's state, the journal's verdict, a declared read) is now consulted before the prose,
    and the prose is only ever consulted to say NO.
    """
    t = (text or "").strip()
    if not t:
        return OP_FAILED
    if name in tools.UI_TOOLS:
        # A screen render is not a mutation of anything. Without this it fell through to
        # UNKNOWN and Brady was told a card he watched appear "may or may not have taken
        # effect" (Codex, 2026-09-10).
        return OP_UI
    if card_state:
        # A dispatched task reports its OWN state; "accepted" is never "finished".
        return {"completed": OP_DONE, "failed": OP_FAILED,
                "cancelled": OP_FAILED}.get(card_state, OP_QUEUED)
    if is_read or name in tools.NATIVE_READS:
        # Ahead of `outcome` deliberately: a non-journalled call has no verdict to report, so
        # the sink says UNKNOWN for it, and a read must not be dragged into the receipts by
        # the absence of evidence about a mutation it never performed.
        return OP_READ
    if outcome:
        return _OPS_MEANING.get(outcome, OP_UNKNOWN)
    if any(t.startswith(x) for x in _REFUSAL_OPENINGS):
        return OP_FAILED
    return OP_UNKNOWN


def interrupted_note(ops_list: list) -> str:
    """The history line an interrupted turn leaves behind, or '' when there is nothing true
    to say. Tested by execution, not by reading the source.

    A FAILURE IS SOMETHING TRUE TO SAY (2026-09-10, adversarial review). This returned '' for
    a turn whose only operations FAILED, so an interrupted turn where a task was refused
    before it started left NO assistant row at all — the next turn read Brady's words with
    silence under them and no idea he had asked for anything. Silence is not honesty; it is
    the same missing-record failure this handler was written for, pointed at the other
    outcome. Failures are now named.

    THE REFUSAL PROSE IS DELIBERATELY NOT REPLAYED. Refusal text in this codebase is addressed
    to the MODEL ("Tell Brady plainly and do not describe this as done", "Call start_task with
    capability 'create_doc' and the same details"), and history.append feeds the RECENT
    THREAD — the exact channel that, per the note further down this file, once resurfaced raw
    tool-result strings as things "Ace said" and pushed the voice model into status-report
    babble. So a failure is recorded as the FACT of a failure and the tool it belonged to, and
    nothing else. That also keeps "a refusal never rides along as a completion" true by
    construction: the refusal's own words are not in this string at all.
    """
    done = [o for o in (ops_list or []) if o.get("state") == OP_DONE]
    review = [o for o in (ops_list or []) if o.get("state") == OP_REVIEW]
    queued = [o for o in (ops_list or []) if o.get("state") == OP_QUEUED]
    unsure = [o for o in (ops_list or []) if o.get("state") == OP_UNKNOWN]
    failed = [o for o in (ops_list or []) if o.get("state") == OP_FAILED]
    if not (done or queued or unsure or failed or review):
        # Still '' for a genuinely empty turn, and for one that only READ or only painted a
        # screen: nothing was asked of the world, so there is nothing to report about it.
        return ""
    lines = ["\u26a0 INTERRUPTED — the call cut off before I answered."]
    if done:
        lines.append("These DID go through:")
        lines += [f"  \u2022 {str(o.get('text'))[:180]}" for o in done[:8]]
    if queued:
        lines.append("Started and still running (NOT finished):")
        lines += [f"  \u2022 {str(o.get('text'))[:180]}" for o in queued[:4]]
    waiting = [o for o in (ops_list or []) if o.get("state") == OP_REVIEW]
    if waiting:
        # Named by TOOL only. The result text is written at the model ("Ask Brady to open
        # Review…"), and chat.py has form on raw tool strings resurfacing in the thread as
        # things Ace supposedly said.
        lines.append("Waiting on your approval in Review — prepared, NOT done: "
                     + ", ".join(sorted({str(o.get("tool")) for o in waiting})))
    if unsure:
        lines.append("Outcome NOT confirmed — these may or may not have taken effect:")
        lines += [f"  \u2022 {str(o.get('text'))[:180]}" for o in unsure[:4]]
    if failed:
        # NAMES ONLY, and on ONE line: see the docstring. Deduplicated and order-preserving,
        # so two refusals of the same tool do not read as two separate requests, and written
        # without a bullet so it cannot be miscounted as one of the entries above.
        names = list(dict.fromkeys(str(o.get("tool") or "something") for o in failed))
        lines.append("Asked for and NOT done \u2014 these failed or were refused, and nothing "
                     "changed: " + ", ".join(names[:6]))
    # A write shielded by _dispatch_write can settle AFTER this turn ends, so this list is not
    # a complete account of the turn and must not be read as one.
    lines.append("This is only what I saw before the line dropped — a write can finish after "
                 "that, so check the record rather than assuming anything else was skipped.")
    return "\n".join(lines)


async def stream_turn(user_text: str, emit, prior=None, fast=False, extra_tools=None):
    """Run one Ace turn, emitting WS events via `emit(type, payload)` (async).

    prior: the conversation so far. The WS handler passes its per-connection
    transcript (shared Telegram window + this session's turns) so Ace actually
    remembers the conversation he's in; the voice adapter passes ElevenLabs'
    messages. None = fall back to the shared window only (HTTP one-shots).

    fast: low-latency path for live voice — lean context + low effort so Ace
    starts speaking quickly instead of stalling behind a full data prefetch.

    extra_tools: PASSTHROUGH tool schemas (Anthropic format) the CALLER executes,
    not us — ElevenLabs system tools (end_call, skip_turn, …) forwarded by the
    voice adapter. When the model calls one we emit `el_tool` and END the turn;
    ElevenLabs performs the action (hang up / yield the turn). Voice-only.

    Returns the reply text so the caller can append it to its transcript.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        await emit("error", {"text": "Empty message"})
        return ""
    maybe_toggle_privacy(user_text)   # flip Discreet Mode deterministically before context is built
    _turn_user_text[0] = user_text    # the confirm gate reads this to detect a spoken approval

    # ★ SAVE WHAT BRADY SAID *FIRST* (2026-08-24). His words used to be persisted only AFTER the
    # model replied, so any turn that died mid-call — a voice timeout, an API error, ElevenLabs
    # cutting the call — swallowed the update entirely: it reached NOTHING, not even history.
    # That's how a whole brain-dump vanished twice. Now the user half lands immediately, so even
    # a failed turn leaves his words captured and recoverable in the thread.
    try:
        await asyncio.to_thread(history.append, "user", user_text)
    except Exception as e:
        logger.warning("early user-persist failed: %s", e)

    try:
        # Voice builds its own single-block context; typed comes back split into a SLOW half
        # (profile + memory + recap — cacheable) and a FAST half (time, schedule, calendar,
        # inbox, weather, board — different every turn).
        if fast:
            ctx_slow, ctx = "", await _fast_context()
        else:
            ctx_slow, ctx = await _live_context()
        # (The old single `system` string was assembled here and never read — the real
        #  system blocks are built per-path below. Dropped rather than kept in sync.)
        messages = await _load_messages(user_text, prior)
    except Exception as e:
        logger.error("context build failed: %s", e)
        await emit("error", {"text": f"⚠️ Couldn't reach your data: {e}"})
        return ""

    try:
        from . import planning
        await asyncio.to_thread(planning.capture, "user", user_text)
        ctx += await asyncio.to_thread(planning.context)
    except Exception as e:
        logger.warning("planning notebook unavailable: %s", type(e).__name__)

    full_reply = []
    confirmations = []
    turn_ops = []          # structured per-tool outcomes, for an honest interrupted record
    handed_off = False   # a build_on_screen handoff already fired this turn — don't double-fire
    blocked_counts: dict = {}   # per-tool confirm-gate blocks THIS turn (2nd+ gets the STOP message)
    passthrough = {t["name"] for t in (extra_tools or [])}
    passthrough_called = False   # an el_tool fired (end_call/skip_turn) — the platform takes over
    # One monotonic id per real user turn — the confirm gate uses it to tell "Brady
    # spoke again" from "the model looped inside this same turn." Immune to the
    # transcript cap that a message-count would deadlock on.
    turn_id = _next_turn_id()

    try:
        client = _anthropic()
        if fast:
            # Voice = SPEED, because ElevenLabs cuts the call if the first token is late and
            # goes silent if a tool stalls. So: Haiku (fastest first token), NO extended
            # thinking, and NATIVE tools ONLY — no MCP (typed keeps the full MCP surface).
            # PROMPT CACHING: the static prompt + native tool schemas are byte-stable, so
            # mark them as a cached prefix — Haiku stops re-paying ~6k tokens of prefill
            # per turn, pulling first-token well under the lead-in threshold.
            voice_tools = [dict(t) for t in VOICE_TOOLS] + list(extra_tools or [])
            voice_tools[len(VOICE_TOOLS) - 1]["cache_control"] = _cc()
            cached_system = [
                {"type": "text", "text": build_system_prompt(),
                 "cache_control": _cc()},
                {"type": "text", "text": "\n\n---\nLIVE CONTEXT\n" + ctx + _discreet_note()},
            ]
            stream_kwargs = dict(model=VOICE_MODEL, max_tokens=1500, system=cached_system,
                                 messages=messages, tools=voice_tools)
        else:
            # MCP folds into the TYPED loop only (fetched once, cached; empty when dormant).
            # WEB_SEARCH (2026-07-31) rides here too — Anthropic executes it server-side, so
            # the dispatch loop below never sees it (its blocks are 'server_tool_use', which
            # the != 'tool_use' guard already skips). Typed only: voice stays native-fast.
            # LEAN MODE prompt caching (2026-08-03): same trick as voice — the static prompt
            # + tool schemas are byte-stable, so mark them as a cached prefix. Once warm,
            # every typed turn re-reads ~10k prefix tokens at 10% price instead of full rate.
            mcp_schemas = await mcp_client.tool_schemas()
            # NOTE: 1-hour cache TTL needs the anthropic-beta: extended-cache-ttl-2025-04-11
            # header; passing ttl without it broke every call (2026-08-03). Reverted to the
            # standard 5-min ephemeral cache — safe. Re-add 1h WITH the header, verified.
            # start_task on the TYPED loop too, so an equivalent request runs the same
            # verified handler under the same rules whichever way Brady asks. The MCP sheet
            # tools stay available for the small edits typing is good at; what changes is
            # that creating a spreadsheet is a task with an id either way, rather than an
            # unjournalled call whose only record was the model's own sentence about it.
            typed_tools = ([dict(t) for t in tools.TOOLS] + [dict(tools.START_TASK)]
                           + list(mcp_schemas) + [dict(tools.WEB_SEARCH)])
            typed_tools[-1]["cache_control"] = _cc()
            # THREE BLOCKS, TWO BREAKPOINTS (Phase 6 step 1, 2026-09-06). The cache is a
            # PREFIX over tools → system → messages, so each breakpoint extends the cached
            # span. Block 2 is the slow half of the context — measured at ~4,600 tokens of
            # memory alone, re-sent uncached on every typed turn until now. It only changes
            # when the learning sweep files a fact, so between sweeps this reads at 10% of
            # rate instead of full. If it DOES change, block 1 still hits: a miss here costs
            # one re-write of the slow half, not the whole prefix.
            typed_system = [
                {"type": "text", "text": build_system_prompt(),
                 "cache_control": _cc()},
                {"type": "text", "text": "\n\n---\nWHAT YOU KNOW\n" + ctx_slow,
                 "cache_control": _cc()},
                {"type": "text", "text": "\n\n---\nLIVE CONTEXT\n" + ctx + _discreet_note()},
            ]
            stream_kwargs = dict(
                model=MODEL, max_tokens=MAX_TOKENS, system=typed_system, messages=messages,
                tools=typed_tools,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT_TYPED},
            )
    except Exception as e:
        # Setup failure (bad key, MCP registry down) must NEVER die silently — a silent
        # death made every voice turn end in the same canned fallback line, which read
        # as Ace stuck in a loop. Emit the error (voice speaks one fixed line for it).
        logger.error("stream_turn setup failed: %s", e)
        await emit("error", {"text": f"⚠️ {e}"})
        return ""

    try:
        for _ in range(MAX_TOOL_ITERS):
            turn_text = []
            # `betas` exists ONLY on client.beta.messages — sending a 1h ttl to the standard
            # endpoint is exactly what broke every call on 3 Aug. The preflight decides which
            # of these two we are on, at boot, before any turn depends on it.
            _stream_cm = (client.beta.messages.stream(betas=[_TTL_BETA], **stream_kwargs)
                          if _ttl_ok[0] else client.messages.stream(**stream_kwargs))
            async with _stream_cm as stream:
                async for event in stream:
                    if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                        turn_text.append(event.delta.text)
                        await emit("delta", {"text": event.delta.text})
                final = await stream.get_final_message()

            # COST INSTRUMENTATION (Phase 6, 2026-09-06). Nothing measured the cache before
            # this, which made every optimization in this phase unverifiable — and would have
            # hidden a repeat of 3 Aug, when the 1-hour TTL broke every call silently. One line
            # per model call: what was cached, what was written, what was paid full rate.
            # `hit` is the fraction of the prefix served from cache — if it collapses after a
            # prompt edit, that edit invalidated the cache and the next line will show it.
            try:
                _u = getattr(final, "usage", None)
                if _u is not None:
                    _rd = getattr(_u, "cache_read_input_tokens", 0) or 0
                    _wr = getattr(_u, "cache_creation_input_tokens", 0) or 0
                    _in = getattr(_u, "input_tokens", 0) or 0
                    _tot = _rd + _wr + _in
                    logger.info("usage[%s] in=%d cache_read=%d cache_write=%d out=%d hit=%.0f%%",
                                "voice" if fast else "typed", _in, _rd, _wr,
                                getattr(_u, "output_tokens", 0) or 0,
                                (100.0 * _rd / _tot) if _tot else 0.0)
            except Exception:
                pass

            if turn_text:
                full_reply.append("".join(turn_text))

            # Web-search receipts: the API already executed these server-side — nothing to
            # run, but Brady should SEE that Ace looked something up (and what he searched).
            for _b in final.content:
                if getattr(_b, "type", "") == "server_tool_use":
                    try:
                        _q = (dict(getattr(_b, "input", {}) or {}).get("query") or "")[:60]
                    except Exception:
                        _q = ""
                    await emit("tool", {"name": "web_search",
                                        "label": "SEARCHED THE WEB" + (f": {_q}" if _q else ""),
                                        "status": "done", "ui": False})

            if final.stop_reason == "max_tokens":
                # Ran out of room (likely deep in thinking) before finishing —
                # never silently claim success or drop a pending tool call.
                logger.warning("turn hit max_tokens (%s); reply so far %d chars",
                               MAX_TOKENS, sum(len(t) for t in full_reply))
                if not "".join(full_reply).strip():
                    full_reply.append("I got a bit tangled working that through — ask me again, and I'll keep it tighter.")
                break

            if final.stop_reason == "pause_turn":
                # Server-side tool (web_search) paused mid-turn — resume it, don't truncate
                # the reply at the pause point (2026-08-23 scrub m1).
                messages.append({"role": "assistant", "content": final.content})
                continue

            if final.stop_reason != "tool_use":
                break

            # Execute every tool call in this turn, feed all results back together.
            messages.append({"role": "assistant", "content": final.content})
            tool_results = []
            for block in final.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                if block.name in passthrough:
                    # An ElevenLabs system tool (end_call, skip_turn, …). We don't execute it —
                    # we relay the call back through the SSE stream and ElevenLabs performs the
                    # action (hangs up / yields the turn). The turn ends here on our side.
                    await emit("el_tool", {"name": block.name, "args": dict(block.input), "id": block.id})
                    passthrough_called = True
                    continue
                if block.name == "start_task":
                    # SHARED BACKGROUND EXECUTION. No screen required, nothing run inside
                    # this turn, and no outcome available to narrate: the tool result says
                    # only that the task was accepted and what state it is in. Ace is told
                    # explicitly not to describe a result, because on 9 September he
                    # described one that did not exist.
                    from . import taskrunner
                    a = dict(block.input)
                    cap = (a.get("capability") or "").strip()
                    rows = a.get("rows") or []
                    question = (a.get("question") or "").strip()
                    task_args, task_title, missing = {}, "", ""
                    # PER-BLOCK, ALWAYS BOUND (2026-09-10). `card` used to be an unbound
                    # loop local on the rejection paths, and the classification below read it
                    # through `"card" in dir() or card` — which raises NameError on the very
                    # branch it was meant to protect (dir() inside a function lists BOUND
                    # locals only), killing the whole turn. Worse, when it did happen to be
                    # bound it was bound by a PREVIOUS tool block, so a second start_task
                    # whose arguments were rejected inherited the first one's card and was
                    # reported to Brady as "started and still running".
                    card, op_state = None, None
                    if cap == "create_spreadsheet":
                        if not (a.get("title") or "").strip() or not rows:
                            missing = ("A title and the contents are both required, and I "
                                       "will not make either up. Ask Brady for whichever is "
                                       "missing.")
                        task_args = {"title": a.get("title"), "rows": rows,
                                     "bold_header": bool(a.get("bold_header")),
                                     "folder": (a.get("folder") or "").strip()}
                        task_title = (a.get("title") or "").strip()[:120]
                    elif cap == "create_doc":
                        blocks = [b for b in (a.get("blocks") or []) if str(b).strip()]
                        if not (a.get("title") or "").strip() or not blocks:
                            missing = ("A title and the text are both required, and I will "
                                       "not invent either. Ask Brady for whichever is "
                                       "missing.")
                        task_args = {"title": a.get("title"), "blocks": blocks,
                                     "folder": (a.get("folder") or "").strip()}
                        task_title = (a.get("title") or "").strip()[:120]
                    elif cap == "create_folder":
                        if not (a.get("name") or "").strip():
                            missing = "No folder name came through. Ask Brady what to call it."
                        task_args = {"name": a.get("name")}
                        task_title = (a.get("name") or "").strip()[:120]
                    elif cap == "research":
                        if not question:
                            missing = ("No question came through. Ask Brady what he wants "
                                       "looked up, in one short sentence.")
                        task_args = {"question": question}
                        task_title = question[:120]
                    if not cap:
                        result = ("No task type came through. Ask Brady what he wants in one "
                                  "short sentence.")
                        op_state = OP_FAILED     # nothing was dispatched: NOT started
                    elif missing:
                        result = missing
                        op_state = OP_FAILED     # arguments refused: NOT started
                    else:
                        card = await taskrunner.dispatch(
                            cap, task_args,
                            origin=("voice" if fast else "typed"),
                            title=task_title)
                        card = card if isinstance(card, dict) else {}
                        await emit("task", card)
                        st = card.get("state")
                        if st == tasks_mod.FAILED and not card.get("task_id"):
                            result = ("NOT STARTED — " + (card.get("error") or "it could not "
                                      "be accepted") + " Tell Brady plainly; do not say it is "
                                      "being made.")
                        elif st == tasks_mod.COMPLETED:
                            result = ("He already asked for exactly this and it is DONE — the "
                                      "card is on his screen with the link. Say it is already "
                                      "made; do NOT make a second one.")
                        else:
                            result = (f"ACCEPTED as task {card.get('task_id','')[:8]} and now "
                                      f"{st}. NOTHING HAS BEEN CREATED YET. In ONE short "
                                      f"sentence tell Brady you are on it and that a card will "
                                      f"show when it is done. Do NOT say it is built, ready or "
                                      f"open. Do NOT give a link. Then carry on the "
                                      f"conversation normally.")
                        # Whatever the card actually says — queued, running, completed,
                        # failed, cancelled — is what gets recorded. Derived from THIS
                        # block's card, which is the only one in scope.
                        op_state = classify_result("start_task", result,
                                                   card_state=(st or ""))
                    # Recorded HERE, before the continue — this branch never reached the
                    # classification below, so a dispatched task was invisible to an
                    # interrupted turn's record entirely (Codex, 2026-09-10).
                    turn_ops.append({"tool": "start_task", "text": result,
                                     "state": op_state or OP_UNKNOWN})
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                         "content": result})
                    continue
                if block.name == "build_on_screen":
                    # Voice can't author Docs/Sheets/Slides itself — hand the task to the HUD,
                    # which runs it as a normal typed turn with the full MCP toolset (and the
                    # usual on-screen confirm flow). Ace just tells Brady to watch his screen.
                    # Handled here, before any status narration or confirm gate. The spoken
                    # line MUST match what actually happened, so branch on the real outcome:
                    # empty request, already-dispatched, no screen connected, or success.
                    req = (dict(block.input).get("request") or "").strip()
                    if not req:
                        result = ("No task text came through. Ask Brady in one short sentence "
                                  "what he wants built, then call build_on_screen again.")
                    elif handed_off:
                        result = ("Already sent that to the screen — just tell Brady to watch "
                                  "it. Do NOT send it again.")
                    else:
                        # emit('handoff') returns how many HUDs received it (openai_compat path).
                        reached = await emit("handoff", {"message": req})
                        if reached == 0:
                            result = ("No screen is connected, so this can't run on the HUD. Tell "
                                      "Brady in one short sentence to open the Ace app on a screen, "
                                      "then ask again. Do NOT say it's being built — it isn't.")
                        else:
                            # RETIRED PATH. If a cached schema still calls this, do not hand
                            # anything to a screen and do not claim a build — say to ask again
                            # so the request goes through start_task and gets a real task id.
                            handed_off = True
                            result = ("This route is retired and NOTHING was built. Ask Brady to "
                                      "say what he wants once more so it runs as a proper "
                                      "background task. Do NOT tell him to watch his screen and "
                                      "do NOT say anything is being made.")
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                    continue
                is_ui = block.name in tools.UI_TOOLS
                # Gate BEFORE any "running" narration — a blocked action must never be
                # spoken/painted as if it happened (it's about to be asked, not done).
                blocked, use_args = _confirm_gate(block.name, dict(block.input), turn_id)
                if blocked:
                    # No action pill/narration; just a keep-alive token so a voice
                    # turn's confirmation question doesn't stall behind a silent round-trip.
                    await emit("hold", {"name": block.name})
                    blocked_counts[block.name] = blocked_counts.get(block.name, 0) + 1
                    # Re-blocked in the SAME turn = the model is retrying with confirmed:true
                    # in the same breath; without a hard STOP it churns silent round-trips
                    # until MAX_TOOL_ITERS while the caller hears only continuers.
                    if blocked_counts[block.name] == 1:
                        from . import review_store
                        try:
                            proposal_id = await asyncio.to_thread(review_store.propose, block.name, use_args)
                            result = ("REVIEW REQUIRED. Nothing executed. Exact details saved in Review, "
                                      "proposal " + proposal_id + ". Ask Brady to open Review to approve "
                                      "or reject. Spoken yes and confirmed=true cannot execute it. "
                                      "Reject an old proposal before preparing changed details.")
                            await emit("confirmation", {"text": "Action prepared — open Review to inspect it."})
                        except Exception:
                            result = "Review storage unavailable. Nothing executed. Tell Brady the action is blocked."
                    else:
                        result = "STOP: action is waiting in Review. Do not call it again or claim it ran."
                    # RECORDED BEFORE THE CONTINUE. This branch returned without touching
                    # turn_ops, so an interrupted turn that had parked something for approval
                    # wrote no assistant row at all — Brady's words with silence beneath them,
                    # the exact hole this whole repair exists to close.
                    turn_ops.append({"tool": block.name, "text": result, "state": OP_REVIEW})
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                    continue
                label = tools.TOOL_LABELS.get(block.name) or \
                    block.name.removeprefix("mcp_").replace("_", " ").upper()
                await emit("tool", {"name": block.name, "label": label, "status": "running", "ui": is_ui})
                _outcome = {}
                # WHAT THIS TOOL IS, DECIDED BEFORE IT RUNS. The registry and tools.py both
                # declare a tool's kind STATICALLY, so a read is knowable without waiting for
                # an answer — which matters because the answer may never arrive.
                _declared_read = (block.name in tools.NATIVE_READS
                                  or connectors.action(connectors.connector_of(block.name),
                                                       block.name).get("kind") == "read")
                # UNOBSERVED IS NOT DID-NOT-HAPPEN (2026-09-10). _dispatch_write shields the
                # write and re-raises CancelledError, so a barge-in mid-write skipped the
                # append below entirely: the operation was absent from turn_ops and the
                # interrupted note never named it — Brady was told about everything EXCEPT
                # the write that was actually in flight when the line dropped. The entry goes
                # in BEFORE the await, marked outcome-unknown, and is overwritten with the
                # real verdict if the call gets to come back.
                #
                # AND "UNOBSERVED" IS NOT "UNKNOWN" EITHER, WHEN THE TOOL CANNOT MUTATE
                # ANYTHING (2026-09-10, adversarial review). The provisional state was
                # computed from tools.NATIVE_READS alone, which holds only the NATIVE reads —
                # so every MCP read (mcp_search_drive_files, mcp_get_doc_content,
                # mcp_read_sheet_values, …) fell through to OP_UNKNOWN, and a barge-in during
                # a Drive search wrote "may or may not have taken effect" about a search.
                # That is a false alarm of exactly the kind this note exists to stop, pointed
                # the wrong way: it invites Brady to go and check a record that a read could
                # not have touched. connectors.py already states the kind before the call, so
                # it is asked.
                _entry = {"tool": block.name,
                          "text": ((f"{label} ({block.name}) — started, and the line dropped "
                                    f"before the answer came back.")
                                   if (is_ui or _declared_read) else
                                   (f"{label} ({block.name}) — started, and the line dropped "
                                    f"before the outcome came back. It may have completed. "
                                    f"Check the record before doing it again.")),
                          "state": (OP_UI if is_ui else
                                    OP_READ if _declared_read else OP_UNKNOWN)}
                turn_ops.append(_entry)
                if is_ui:
                    result = await _run_ui_tool(block.name, use_args, emit)
                elif mcp_client.is_mcp_tool(block.name):
                    # THE REGISTRY DECIDES, NOT THE PROVIDER (2026-09-09). A connector
                    # publishes whatever it likes; connectors.py says which of those Ace may
                    # call. Two things this closes: a tool nobody has decided how to verify
                    # cannot be reached at all, and a write that HAS a verified capability
                    # cannot be called around it — typed chat still carried raw
                    # mcp_create_spreadsheet beside start_task, so an equivalent request
                    # could still take the unverified path that failed on 9 September.
                    _ok, _why = connectors.allowed(block.name)
                    _via = connectors.action(connectors.connector_of(block.name),
                                             block.name).get("via_capability")
                    if not _ok:
                        result = (f"NOT AVAILABLE — {_why}. Nothing ran. Tell Brady plainly "
                                  f"and do not describe this as done.")
                    elif _via and _via in capabilities.REGISTRY:
                        result = (f"USE start_task INSTEAD. {block.name} creates something "
                                  f"without checking it afterwards, which is how a "
                                  f"spreadsheet got reported as built when it did not exist. "
                                  f"Call start_task with capability '{_via}' and the same "
                                  f"details. NOTHING HAS BEEN CREATED by this call.")
                    else:
                        result = await mcp_client.call(block.name, use_args)
                else:
                    result = await _dispatch_write(block.name, use_args, _outcome)
                await emit("tool", {"name": block.name, "label": label, "status": "done", "ui": is_ui})
                if not is_ui:
                    await emit("confirmation", {"text": result})
                    confirmations.append(result)
                # Classified HERE, where the tool and its answer are both in hand — not
                # re-derived from prose later. connectors.action() on a NATIVE name is safe:
                # connector_of returns "" and action("", name) reads {} → {}, never raises.
                _is_read = _declared_read or _outcome.get("kind") == "read"
                # The provisional entry is UPDATED, not duplicated — the call came back, so
                # its real outcome replaces "unknown, still in flight".
                _entry["text"] = result
                _entry["state"] = classify_result(block.name, result, _is_read,
                                                  outcome=_outcome.get("state", ""))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            if passthrough_called:
                # ElevenLabs is taking the action (hang-up / turn-yield) — no further model
                # round-trip; anything Ace wanted to say already streamed.
                break
            messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning("hit MAX_TOOL_ITERS for: %s", user_text[:80])

        reply = "".join(full_reply).strip()
        if not reply and not passthrough_called:
            # An empty completion makes ElevenLabs treat the turn as an LLM failure and
            # cascade. On the TYPED path it was worse and silent: the frontend discards an
            # empty bubble, so exhausting MAX_TOOL_ITERS meant Ace ran eight tools and said
            # nothing at all. Both paths now always get something back.
            reply = ("I'm here — say that again for me?" if fast else
                     "I ran out of steps on that one before I could answer. Ask me again and "
                     "I'll take a narrower run at it.")
        try:
            from . import planning
            await asyncio.to_thread(planning.capture, "assistant", reply)
        except Exception as e:
            logger.warning("plan draft save failed: %s", type(e).__name__)
        await emit("final", {"text": reply})

        # Persist to 2.0's OWN history (best-effort; never blocks the reply). ONLY the real
        # exchange — raw tool-result strings used to be stored as things "Ace said," and
        # those calendar dumps / ⚠️ warnings resurfaced in the RECENT THREAD, nudging the
        # voice model into status-report babble. The reply already summarizes what was done.
        # NOTE: the USER half is already saved up front (see _persist_user above) so a turn
        # that dies mid-model-call can never swallow what Brady said.
        try:
            if reply:
                await asyncio.to_thread(history.append, "assistant", reply)
        except Exception as e:
            logger.warning("history persist skipped: %s", e)

        return reply

    except asyncio.CancelledError:
        partial = "".join(full_reply)
        tail = "".join(turn_text) if "turn_text" in locals() else ""
        if tail and not partial.endswith(tail):
            partial += tail
        if partial:
            try:
                from . import planning
                await asyncio.shield(asyncio.to_thread(planning.capture, "assistant", "INTERRUPTED DRAFT — not confirmed complete.\n" + partial))
            except Exception:
                logger.warning("interrupted draft could not be saved")
        # WHAT HE ACTUALLY DID, BEFORE THE LINE DROPPED (2026-09-10).
        #
        # Brady talked for a minute on his iPad, finished with "take those down first", and
        # Ace went silent. The writes DID happen — a record updated at 15:33:49, an item
        # captured at 15:39:06 — because _dispatch_write shields them and they settle on
        # their own. But this handler only ever saved the partial prose to the planning
        # notebook, so NOTHING reached the conversation history: no assistant turn at all.
        #
        # The next turn therefore reads his long statement with no reply beneath it and no
        # idea any of it was done. That is how you get a duplicate capture, or Ace claiming
        # he never heard something he acted on. The receipts exist — they are right here in
        # `confirmations` — so an interrupted turn now leaves an honest record of what
        # completed, marked as interrupted so nothing reads as a finished answer.
        try:
            note = interrupted_note(turn_ops if "turn_ops" in locals() else [])
            if note:
                await asyncio.shield(asyncio.to_thread(history.append, "assistant", note))
        except Exception:
            logger.warning("interrupted-turn receipts could not be saved")
        raise
    except Exception as e:
        logger.error("stream_turn error: %s", e)
        await emit("error", {"text": f"⚠️ {e}"})
        return ""
