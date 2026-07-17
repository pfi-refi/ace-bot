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
def get_events_structured(days: int = 7) -> list:
    """Return upcoming events across all calendars as a list of dicts.

    Each item: {start, iso, date, date_label, day_label, time, all_day, title, calendar}
    Spans today 00:00 through `days` days ahead, sorted by start time.
    """
    days = max(1, min(int(days), 30))
    try:
        creds = get_google_creds()
        service = build("calendar", "v3", credentials=creds)
        now_et = datetime.now(EASTERN)
        start_of_day = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        end_window = start_of_day + timedelta(days=days)

        calendars = service.calendarList().list().execute().get("items", [])
        events: list = []
        seen_ids: set = set()
        for calendar in calendars:
            cal_id = calendar["id"]
            cal_name = calendar.get("summary", cal_id)
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
        events.sort(key=lambda x: x["start"])
        return events
    except Exception as e:
        logger.error("Structured calendar fetch error: %s", e)
        return []


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
        result = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        return True, result.get("id", "created")
    except Exception as e:
        logger.error("Calendar create error: %s", e)
        return False, str(e)


def delete_calendar_event(title: str, date_str: str, calendar_id: str = PFI_CALENDAR_ID) -> tuple:
    """Delete a calendar event by title match on a given date. Returns (success, message).

    NOTE: this deletes CALENDAR events only (an explicit Ace action Brady asks for).
    It never touches the protected Drive data files (ace_memory / ace_conversation).
    """
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
