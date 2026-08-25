# MIRROR OF ace-portal/backend/calendar_api.py — copied verbatim, not rewritten.
# These Google integrations are battle-tested (originally ported from
# ace-bot/bot.py) and are deliberately reused rather than rebuilt.
# Railway root dirs are per-service, so a shared package would force both
# services to root at the repo root; copying is the cheaper trade.
# Delete the ace-portal copy once Ace 2.0 fully replaces the portal.
"""
Google Calendar reads + writes for the Ace Portal.

Ported directly from ace-bot/bot.py. Two shapes of data are exposed:
  • Structured (get_events_structured) — JSON for the Schedule panel.
  • Text (get_calendar_events / get_tomorrow_events / get_calendar_events_range)
    — the exact strings the bot injects into Ace's context, reused unchanged so
    the portal Ace and the Telegram Ace reason over identical calendar text.

SECURITY: parse_time_flexible() is ported verbatim — NEVER remove or modify it.
Writes only ever target PFI_CALENDAR_ID, passed explicitly on every call.
"""

import logging
from datetime import datetime, timedelta

from googleapiclient.discovery import build

from .google_client import EASTERN, PFI_CALENDAR_ID, get_google_creds

logger = logging.getLogger("ace_portal.calendar")

_PRIMARY_CAL_IDS = ("planforitpfi@gmail.com", "primary", "pfi@platinumfortuneimpact.com")


# ── Structured read (for the Schedule panel) ────────────────────────────────────
# CALENDAR FILTER (2026-08-10, Brady's pivot): Ace should surface ONLY his client + personal
# appointments — NOT the PFI team calendar, BPM/hiring interviews, or interview calendars
# shared onto his account. Those live on shared calendars others rely on, so we FILTER his
# VIEW (never delete). Two gates, both env-tunable so Brady can adjust without a code change:
#   ACE2_CAL_DENY      — calendar NAME substrings to drop whole (default: the team calendar)
#   ACE2_CAL_DENY_IDS  — calendar ID substrings to drop whole (default: the interview calendar)
#   ACE2_EVENT_DENY    — event TITLE substrings to drop even off a kept calendar (BPM etc.)
import os as _os

# MERGE, don't override (2026-08-20): these env vars were originally REPLACING the code defaults,
# so a value set in Railway silently cancelled every default added later in code (the 'Troyer
# Capital' block never applied because ACE2_CAL_DENY was pinned to 'team calendar'). Built-in
# blocks now ALWAYS apply; the env var only ADDS more on top.
def _merged(env_name: str, base: str) -> list:
    vals = [s.strip().lower() for s in base.split(",") if s.strip()]
    vals += [s.strip().lower() for s in _os.environ.get(env_name, "").split(",")
             if s.strip() and s.strip().lower() not in vals]
    return vals

# 'troyer capital' (2026-08-20): the shared calendar is literally named "Troyer Capital HI's" —
# the 'lincoln troyer' token never matched it, and removing bare 'interview' from EVENT_DENY
# (to protect Brady's OWN job interviews) un-hid its events. Block the whole calendar by name.
_CAL_DENY = _merged("ACE2_CAL_DENY", "team calendar,lincoln troyer,troyer capital")
_CAL_DENY_IDS = _merged("ACE2_CAL_DENY_IDS", "mikeywilson4mw@gmail.com")
# 2026-08-19 review: DROPPED bare 'hiring' + 'interview' from the base — they word-matched Brady's
# OWN job-hunt interviews (top-3 priority; a filtered day looks identical to an empty one). The
# recruiting calendars are dropped wholesale above, so the generic words were pure downside.
# ⚠ If Railway's ACE2_EVENT_DENY still contains 'interview'/'hiring', clear them THERE too — env
# entries merge in and would re-hide his interviews.
_EVENT_DENY = _merged("ACE2_EVENT_DENY", "bpm,hierarchy training,base shop,rblc,live calling,gfi lgnds,momentum monday")


def _cal_dropped(cal_id: str, cal_name: str) -> bool:
    nid, nm = (cal_id or "").lower(), (cal_name or "").lower()
    if any(d in nid for d in _CAL_DENY_IDS) or any(d in nm for d in _CAL_DENY):
        logger.info("cal filter: dropped whole calendar '%s'", cal_name)
        return True
    return False


def _event_dropped(title: str) -> bool:
    # WORD-BOUNDARY match (2026-08-11 review fix): a bare substring test would hide a real
    # meeting whose title merely CONTAINS a deny word (e.g. 'bpm' inside another word). Match
    # whole words/phrases only, and LOG every drop so over-filtering is diagnosable (a filtered
    # day used to look identical to an empty one — that was the 'missing appointment' symptom).
    import re
    t = (title or "").lower()
    for d in _EVENT_DENY:
        if re.search(r"(?<!\w)" + re.escape(d) + r"(?!\w)", t):
            logger.info("cal filter: dropped event '%s' (matched '%s')", title, d)
            return True
    return False


def get_events_structured(days: int = 7, back_days: int = 0) -> list:
    """Return events across all calendars as a list of dicts.

    Each item: {start, iso, date, date_label, day_label, time, all_day, title, calendar}
    Spans (today - `back_days`) 00:00 through `days` days ahead, sorted by start time.
    back_days=0 keeps the original today-forward behaviour; pass a positive value to
    include recent past events (e.g. back_days=7 for the last week + `days` ahead).
    """
    days = max(1, min(int(days), 60))
    back_days = max(0, min(int(back_days), 60))
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        now_et = datetime.now(EASTERN)
        today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_day = today_start - timedelta(days=back_days)
        end_window = today_start + timedelta(days=days)

        calendars = service.calendarList().list().execute().get("items", [])
        events: list = []
        seen_ids: set = set()
        for calendar in calendars:
            cal_id = calendar["id"]
            cal_name = calendar.get("summary", cal_id)
            if _cal_dropped(cal_id, cal_name):
                continue   # team / interview calendar — never surfaced to Ace
            try:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=start_of_day.isoformat(),
                    timeMax=end_window.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()
                for event in result.get("items", []):
                    event_id = event.get("id", "")
                    if event_id in seen_ids:
                        continue
                    seen_ids.add(event_id)
                    summary = event.get("summary", "No title")
                    if _event_dropped(summary):
                        continue   # BPM / interview / training block — filtered from his view
                    start = event.get("start", {})
                    start_dt_str = start.get("dateTime", start.get("date", ""))
                    if "T" in start_dt_str:
                        dt = datetime.fromisoformat(start_dt_str)
                        if dt.tzinfo:
                            dt = dt.astimezone(EASTERN)
                        time_str = dt.strftime("%-I:%M %p")
                        all_day = False
                    else:
                        dt = datetime.strptime(start_dt_str, "%Y-%m-%d")
                        dt = EASTERN.localize(dt)
                        time_str = "All day"
                        all_day = True
                    is_primary = cal_id in _PRIMARY_CAL_IDS
                    events.append({
                        "start": start_dt_str,
                        "iso": dt.isoformat(),
                        "date": dt.strftime("%Y-%m-%d"),
                        "date_label": dt.strftime("%A, %B %-d"),
                        "day_label": dt.strftime("%a").upper(),
                        "time": time_str,
                        "all_day": all_day,
                        "title": summary,
                        "calendar": "" if is_primary else cal_name,
                    })
            except Exception as e:
                logger.warning("Error fetching calendar '%s': %s", cal_name, e)
        events.sort(key=lambda x: x["iso"])   # sort by tz-normalized time, not the raw start string
        return events
    except Exception as e:
        logger.error("Structured calendar fetch error: %s", e)
        return []


def get_calendar_range(start_offset_days: int = 0, num_days: int = 7) -> str:
    """Text calendar for an ARBITRARY window, grouped by date. start_offset_days shifts the
    start from today (negative = into the past, e.g. -30 ≈ a month ago; positive = future,
    e.g. 30 ≈ starting a month out); num_days = how many days the window spans. Use this for
    any 'what did I have' / 'what's on my calendar around <date>' beyond the default context."""
    start_offset_days = max(-365, min(int(start_offset_days), 365))
    num_days = max(1, min(int(num_days), 120))
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        today = datetime.now(EASTERN).replace(hour=0, minute=0, second=0, microsecond=0)
        win_start = today + timedelta(days=start_offset_days)
        win_end = win_start + timedelta(days=num_days)
        events, seen = [], set()
        for cal in service.calendarList().list().execute().get("items", []):
            if _cal_dropped(cal["id"], cal.get("summary", cal["id"])):
                continue   # team / interview calendar — filtered from Ace's view
            try:
                res = service.events().list(
                    calendarId=cal["id"], timeMin=win_start.isoformat(), timeMax=win_end.isoformat(),
                    singleEvents=True, orderBy="startTime",
                ).execute()
                for ev in res.get("items", []):
                    eid = ev.get("id", "")
                    if eid in seen:
                        continue
                    seen.add(eid)
                    if _event_dropped(ev.get("summary", "")):
                        continue   # BPM / interview / training block
                    start = ev.get("start", {})
                    s = start.get("dateTime", start.get("date", ""))
                    if "T" in s:
                        dt = datetime.fromisoformat(s)
                        if dt.tzinfo:
                            dt = dt.astimezone(EASTERN)
                        tstr = dt.strftime("%-I:%M %p")
                    else:
                        dt = EASTERN.localize(datetime.strptime(s, "%Y-%m-%d"))
                        tstr = "All day"
                    events.append((dt, tstr, ev.get("summary", "No title")))
            except Exception as e:
                logger.warning("range cal '%s': %s", cal.get("summary"), e)
        span = f"{win_start.strftime('%b %-d')} – {win_end.strftime('%b %-d, %Y')}"
        if not events:
            return f"No events on the calendar for {span}."
        events.sort(key=lambda x: x[0])
        lines, cur = [f"📅 {span}:"], None
        for dt, tstr, title in events:
            dstr = dt.strftime("%A, %B %-d")
            if dstr != cur:
                lines.append(f"\n{dstr}:")
                cur = dstr
            lines.append(f"  {tstr} — {title}")
        return "\n".join(lines)
    except Exception as e:
        logger.error("get_calendar_range error: %s", e)
        return f"⚠️ Could not read that calendar window: {e}"


# ── Text reads (ported verbatim — feed Ace's context) ───────────────────────────
def get_calendar_events(days_ahead: int = 1) -> str:
    """Pull calendar events from today through `days_ahead` days from ALL calendars."""
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        now_et = datetime.now(EASTERN)
        start_of_day = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        end_window = start_of_day + timedelta(days=days_ahead)
        calendars = service.calendarList().list().execute().get("items", [])
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
                    is_primary_cal = cal_id in _PRIMARY_CAL_IDS
                    cal_label = f" [{cal_name}]" if not is_primary_cal else ""
                    all_events.append((start_dt_str, date_str, date_label, time_str, summary + cal_label))
            except Exception as e:
                logger.warning("Error fetching calendar '%s': %s", cal_name, e)
        all_events.sort(key=lambda x: x[0])

        if not all_events:
            return "Nothing scheduled today." if days_ahead == 1 else f"No events in the next {days_ahead} days."

        if days_ahead == 1:
            return "\n".join(f"• {ev[3]} — {ev[4]}" for ev in all_events)

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
        now_et = datetime.now(EASTERN)
        tomorrow = (now_et + timedelta(days=1)).date()
        start = EASTERN.localize(datetime.combine(tomorrow, datetime.min.time()))
        end = EASTERN.localize(datetime.combine(tomorrow, datetime.max.time()))
        calendars = service.calendarList().list().execute().get("items", [])
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
                    is_primary_cal = cal_id in _PRIMARY_CAL_IDS
                    cal_label = f" [{cal_name}]" if not is_primary_cal else ""
                    all_events.append((start_dt_str, f"• {time_str} — {summary}{cal_label}"))
            except Exception as e:
                logger.warning("Error fetching tomorrow calendar '%s': %s", cal_name, e)
        all_events.sort(key=lambda x: x[0])
        tomorrow_str = tomorrow.strftime("%A, %B %-d")
        if all_events:
            lines = [f"\U0001f4c5 Tomorrow — {tomorrow_str}:"] + [ev[1] for ev in all_events]
            return "\n".join(lines)
        return f"Nothing scheduled tomorrow ({tomorrow_str})."
    except Exception as e:
        logger.error("Tomorrow calendar fetch error: %s", e)
        return "⚠️ Could not load tomorrow's calendar."


def get_calendar_events_range(days: int = 7) -> str:
    """Fetch calendar events for the next N days (1-30), grouped by date."""
    days = max(1, min(int(days), 30))
    result = get_calendar_events(days_ahead=days + 1)
    if result.startswith("⚠️") or "No events" in result:
        return f"Nothing on the calendar for the next {days} days."
    return f"\U0001f4c6 Next {days} days:\n\n{result}"


# ── Time parsing — PORTED VERBATIM. NEVER REMOVE OR MODIFY. ──────────────────────
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


# ── Calendar writes (explicit calendar_id, ported from bot.py) ───────────────────
def create_calendar_event(title: str, date_str: str, time_str: str = None,
                          duration_minutes: int = 60, description: str = "",
                          calendar_id: str = PFI_CALENDAR_ID) -> tuple:
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
                "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/New_York"},
            }
        else:
            event_body = {
                "summary": title,
                "description": description or "",
                "start": {"date": date_str},
                "end": {"date": date_str},
            }
        # IDEMPOTENT CREATE (2026-08-25) — the root cause of Brady's double-booked Grandpa coffee
        # and QUADRUPLE-booked Ken call. A voice turn can execute its tools and then die before
        # replying (ElevenLabs retries a slipped deadline; main.py cancel-and-replace only stops
        # the OLD task, it can't un-create what already hit Google). Each retry inserted the event
        # again. Now an identical event (same title + same start, already on the calendar) is a
        # no-op that reports success — retries converge instead of stacking.
        try:
            probe_start = (start_dt if event_body.get("start", {}).get("dateTime")
                           else EASTERN.localize(datetime.strptime(date_str, "%Y-%m-%d")))
            existing = service.events().list(
                calendarId=calendar_id,
                timeMin=(probe_start - timedelta(minutes=1)).isoformat(),
                timeMax=(probe_start + timedelta(days=1) if not event_body.get("start", {}).get("dateTime")
                         else probe_start + timedelta(minutes=1)).isoformat(),
                singleEvents=True,
            ).execute().get("items", [])
            want = (title or "").strip().lower()
            for e in existing:
                if (e.get("summary") or "").strip().lower() != want:
                    continue
                s = e.get("start", {})
                same = (s.get("dateTime", "")[:16] == event_body["start"].get("dateTime", "")[:16]
                        if event_body["start"].get("dateTime")
                        else s.get("date") == event_body["start"].get("date"))
                if same:
                    logger.info("calendar: '%s' already exists at that time — skipping duplicate insert", title)
                    return True, e.get("id", "already-exists")
        except Exception as e:
            logger.warning("calendar dup-probe failed (%s) — creating anyway", e)

        result = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        return True, result.get("id", "created")
    except Exception as e:
        logger.error("Calendar create error: %s", e)
        return False, str(e)


def delete_calendar_event(title: str, date_str: str, calendar_id: str = PFI_CALENDAR_ID) -> tuple:
    """Delete a calendar event by title match on a given date. Returns (success, message).

    Checks the PFI calendar first, then falls back to Brady's other calendars —
    booked meetings (FTAs, scheduler invites) often live on the primary calendar,
    not the PFI one, and "not found" there used to dead-end the request.

    NOTE: this deletes CALENDAR events only (an explicit Ace action Brady asks for).
    It never touches the protected Drive data files (ace_memory / ace_conversation).
    """
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        start_dt = EASTERN.localize(datetime.strptime(date_str, "%Y-%m-%d"))
        end_dt = start_dt + timedelta(days=1)
        title_lower = title.lower()

        cal_ids = [calendar_id]
        try:
            for c in service.calendarList().list().execute().get("items", []):
                # Never delete off a filtered (shared team/interview) calendar — Ace doesn't
                # even show those, so a loose title match must not hard-delete another's event.
                if _cal_dropped(c["id"], c.get("summary", c["id"])):
                    continue
                if c["id"] not in cal_ids:
                    cal_ids.append(c["id"])
        except Exception as e:
            logger.warning("delete: calendarList failed (%s) — PFI only", e)

        # Collect matches ACROSS all calendars FIRST, then act — deleting matches[0] blind (old
        # behavior) silently removed the wrong event when two same-day titles shared a keyword
        # (2026-08-19 audit). More than one hit → refuse and hand back candidates to disambiguate.
        all_matches = []
        for cid in cal_ids:
            try:
                events = service.events().list(
                    calendarId=cid,
                    timeMin=start_dt.isoformat(),
                    timeMax=end_dt.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                ).execute().get("items", [])
            except Exception as e:
                logger.warning("delete: list failed on %s: %s", cid, e)
                continue
            all_matches += [(cid, e) for e in events if title_lower in e.get("summary", "").lower()]
        if not all_matches:
            return False, f"No event matching '{title}' on {date_str} on any calendar"

        def _when(e):
            s = e.get("start", {})
            return (s.get("dateTime", "") or s.get("date", ""))

        # TRUE DUPLICATES vs GENUINE AMBIGUITY (2026-08-25). The ambiguity guard added on 8/19
        # refused ANY multi-match — which made deleting duplicates IMPOSSIBLE, exactly when Brady
        # needed it (4 identical Ken calls). Events sharing the same title AND start time are
        # duplicates, not a choice: remove the extras and keep ONE. Only genuinely DIFFERENT
        # events still refuse and hand back candidates.
        groups = {}
        for cid, e in all_matches:
            groups.setdefault(((e.get("summary") or "").strip().lower(), _when(e)), []).append((cid, e))
        if len(groups) > 1:
            opts = " | ".join(f"'{e.get('summary', '')}' ({_when(e)[11:16] or 'all-day'})"
                              for _, e in all_matches[:6])
            return False, ("AMBIGUOUS — more than one DIFFERENT event matches '" + title + "' on "
                           + date_str + ": " + opts + ". Ask Brady which one before deleting.")

        dupes = list(groups.values())[0]
        if len(dupes) == 1:
            cid, ev = dupes[0]
            service.events().delete(calendarId=cid, eventId=ev["id"]).execute()
            return True, ev.get("summary", title)
        # Several copies of the SAME event → delete all but one, report what happened.
        removed = 0
        for cid, ev in dupes[1:]:
            try:
                service.events().delete(calendarId=cid, eventId=ev["id"]).execute()
                removed += 1
            except Exception as e:
                logger.warning("delete dup failed on %s: %s", cid, e)
        name = dupes[0][1].get("summary", title)
        return True, (f"{name} — removed {removed} duplicate(s), one left on the calendar")
    except Exception as e:
        logger.error("Calendar delete error: %s", e)
        return False, str(e)
