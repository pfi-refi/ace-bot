# MIRROR OF ace-portal/backend/tasks_api.py — copied verbatim, not rewritten.
# These Google integrations are battle-tested (originally ported from
# ace-bot/bot.py) and are deliberately reused rather than rebuilt.
# Railway root dirs are per-service, so a shared package would force both
# services to root at the repo root; copying is the cheaper trade.
# Delete the ace-portal copy once Ace 2.0 fully replaces the portal.
"""
Google Tasks (+ Gmail + Drive helpers) for the Ace Portal.

Ported directly from ace-bot/bot.py. get_tasks_structured() feeds the Tasks panel;
the text helpers reproduce the exact strings Ace's context expects.
"""

import base64
import logging
from datetime import datetime
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from .google_client import (
    DEFAULT_TASK_LIST,
    EASTERN,
    MORNING_SKIP_LISTS,
    get_google_creds,
)

logger = logging.getLogger("ace_portal.tasks")


# ── Structured read (for the Tasks panel) ───────────────────────────────────────
def get_tasks_structured(skip_reference: bool = False) -> list:
    """Return open tasks across all lists as a list of dicts.

    Each item: {list, title, due}. Incomplete tasks only.
    """
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists = service.tasklists().list(maxResults=20).execute().get("items", [])
        out = []
        for tl in task_lists:
            tl_title = tl.get("title", "Tasks")
            if skip_reference and tl_title in MORNING_SKIP_LISTS:
                continue
            try:
                tasks_result = service.tasks().list(
                    tasklist=tl["id"], showCompleted=False, showHidden=False, maxResults=20,
                ).execute()
                for task in tasks_result.get("items", []):
                    if task.get("status") == "completed":
                        continue
                    title = task.get("title", "").strip()
                    if not title:
                        continue
                    due_str = ""
                    due = task.get("due", "")
                    if due:
                        try:
                            due_dt = datetime.fromisoformat(
                                due.replace("Z", "+00:00")
                            ).astimezone(EASTERN)
                            due_str = due_dt.strftime("%-m/%-d")
                        except Exception:
                            pass
                    out.append({"list": tl_title, "title": title, "due": due_str})
            except Exception as e:
                logger.warning("Error fetching tasks from list '%s': %s", tl_title, e)
        return out
    except Exception as e:
        logger.error("Structured tasks fetch error: %s", e)
        return []


def get_tasks(skip_reference: bool = False) -> str:
    """Text form of open tasks (ported from bot.py) — feeds Ace's context."""
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists = service.tasklists().list(maxResults=20).execute().get("items", [])
        if not task_lists:
            return ""
        all_tasks = []
        for tl in task_lists:
            tl_title = tl.get("title", "Tasks")
            if skip_reference and tl_title in MORNING_SKIP_LISTS:
                continue
            try:
                tasks_result = service.tasks().list(
                    tasklist=tl["id"], showCompleted=False, showHidden=False, maxResults=20,
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
        logger.error("Tasks fetch error: %s", e)
        return ""


def get_task_lists_grouped() -> list:
    """EVERY task list (including empty ones) with its open tasks, in Google's order.

    Returns [{list, count, tasks:[{title, due}]}] — powers the click-between-lists
    card so Brady can see all his lists (Deals, Admin, Brain Dump, Personal, Goals…),
    not just whichever has items. Nothing is skipped here. [] on any failure.
    """
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists = service.tasklists().list(maxResults=30).execute().get("items", [])
        out = []
        for tl in task_lists:
            tl_title = tl.get("title", "Tasks")
            tasks = []
            try:
                r = service.tasks().list(
                    tasklist=tl["id"], showCompleted=False, showHidden=False, maxResults=100,
                ).execute()
                for task in r.get("items", []):
                    if task.get("status") == "completed":
                        continue
                    title = (task.get("title") or "").strip()
                    if not title:
                        continue
                    due_str = ""
                    due = task.get("due", "")
                    if due:
                        try:
                            due_str = datetime.fromisoformat(
                                due.replace("Z", "+00:00")).astimezone(EASTERN).strftime("%-m/%-d")
                        except Exception:
                            pass
                    tasks.append({"title": title, "due": due_str})
            except Exception as e:
                logger.warning("tasks fetch (grouped) for '%s': %s", tl_title, e)
            out.append({"list": tl_title, "count": len(tasks), "tasks": tasks})
        return out
    except Exception as e:
        logger.error("grouped task lists fetch error: %s", e)
        return []


# ── Task writes (ported from bot.py) ─────────────────────────────────────────────
def add_task(title: str, list_name: str = DEFAULT_TASK_LIST) -> tuple:
    """Add a task to Google Tasks. Returns (success, actual_list_name, was_duplicate)."""
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists = service.tasklists().list(maxResults=20).execute().get("items", [])
        if not task_lists:
            return False, list_name, False
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
            target_list_id = task_lists[0]["id"]
            actual_list_name = task_lists[0].get("title", "Tasks")
        # Dedup check
        title_lower = title.lower().strip()
        try:
            existing_result = service.tasks().list(
                tasklist=target_list_id, showCompleted=False, showHidden=False, maxResults=100,
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
        service.tasks().insert(tasklist=target_list_id, body={"title": title}).execute()
        logger.info("Task added to '%s': %s", actual_list_name, title)
        return True, actual_list_name, False
    except Exception as e:
        logger.error("Add task error: %s", e)
        return False, list_name, False


def complete_task(partial_title: str) -> str:
    """Mark a task complete by fuzzy-matching on title. Returns title or empty string."""
    try:
        creds = get_google_creds()
        service = build("tasks", "v1", credentials=creds)
        task_lists = service.tasklists().list(maxResults=20).execute().get("items", [])
        search_lower = partial_title.lower().strip()
        for tl in task_lists:
            try:
                tasks_result = service.tasks().list(
                    tasklist=tl["id"], showCompleted=False, showHidden=False, maxResults=50,
                ).execute()
            except Exception:
                continue
            for task in tasks_result.get("items", []):
                if task.get("status") == "completed":
                    continue
                title = task.get("title", "").strip()
                if search_lower in title.lower():
                    service.tasks().update(
                        tasklist=tl["id"], task=task["id"],
                        body={"id": task["id"], "status": "completed"},
                    ).execute()
                    logger.info("Task completed: %s", title)
                    return title
        return ""
    except Exception as e:
        logger.error("Complete task error: %s", e)
        return ""


# ── Gmail (ported from bot.py) ───────────────────────────────────────────────────
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
    """Pull recently read emails from Gmail in the last 48 hours."""
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


def get_inbox_structured(max_results: int = 6) -> list:
    """Priority unread emails (last 2d, no promos/social) as dicts for the inbox card:
    [{id, from, subject, snippet}]."""
    try:
        creds = get_google_creds()
        service = build("gmail", "v1", credentials=creds)
        n = max(1, min(int(max_results), 15))
        results = service.users().messages().list(
            userId="me", q="is:unread newer_than:2d -category:promotions -category:social",
            maxResults=n,
        ).execute()
        out = []
        for msg in results.get("messages", []):
            md = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"] for h in md.get("payload", {}).get("headers", [])}
            sender = headers.get("From", "Unknown")
            if "<" in sender:
                sender = sender.split("<")[0].strip().strip('"')
            out.append({
                "id": msg["id"],
                "from": sender[:40],
                "subject": headers.get("Subject", "No subject")[:90],
                "snippet": md.get("snippet", "")[:120],
            })
        return out
    except Exception as e:
        logger.error("inbox structured error: %s", e)
        return []


def _decode_b64url(data: str) -> str:
    import base64
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", "replace")
    except Exception:
        return ""


def _extract_email_body(payload: dict) -> str:
    """Walk a Gmail message payload for readable text — prefer text/plain, fall back to
    stripped text/html, recursing into multipart parts."""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if data and mime.startswith("text/plain"):
        return _decode_b64url(data)
    text_plain, text_html = "", ""
    for part in payload.get("parts", []) or []:
        pm = part.get("mimeType", "")
        if pm.startswith("multipart/"):
            nested = _extract_email_body(part)
            if nested:
                return nested
        d = part.get("body", {}).get("data")
        if not d:
            continue
        if pm.startswith("text/plain") and not text_plain:
            text_plain = _decode_b64url(d)
        elif pm.startswith("text/html") and not text_html:
            text_html = _decode_b64url(d)
    if text_plain:
        return text_plain
    if text_html:
        import re
        return re.sub(r"<[^>]+>", " ", text_html)
    return ""


def search_gmail(query: str, max_results: int = 8) -> str:
    """Search the whole mailbox with a Gmail query (supports from:, to:, subject:, keywords,
    newer_than:/older_than:, has:attachment, etc.) → matching emails with their id + snippet.
    Pass an id to read_gmail to open the full email."""
    try:
        creds = get_google_creds()
        service = build("gmail", "v1", credentials=creds)
        n = max(1, min(int(max_results), 20))
        results = service.users().messages().list(userId="me", q=query, maxResults=n).execute()
        messages = results.get("messages", [])
        if not messages:
            return f"No emails found for: {query}"
        lines = []
        for msg in messages:
            md = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in md.get("payload", {}).get("headers", [])}
            sender = headers.get("From", "Unknown")
            if "<" in sender:
                sender = sender.split("<")[0].strip().strip('"')
            subject = headers.get("Subject", "No subject")
            date = headers.get("Date", "")[:31]
            snippet = md.get("snippet", "")[:150]
            lines.append(f"[{msg['id']}] {sender[:32]} — {subject[:75]}\n    {date}\n    {snippet}")
        return "\n".join(lines)
    except Exception as e:
        logger.error("Gmail search error: %s", e)
        return f"⚠️ Could not search email: {e}"


def read_gmail(message_id: str) -> str:
    """Read the full body of one email by its message id (from search_gmail)."""
    try:
        creds = get_google_creds()
        service = build("gmail", "v1", credentials=creds)
        md = service.users().messages().get(userId="me", id=message_id.strip(), format="full").execute()
        headers = {h["name"]: h["value"] for h in md.get("payload", {}).get("headers", [])}
        body = (_extract_email_body(md.get("payload", {})) or "").strip()[:4000] or "(no readable text body)"
        return (f"From: {headers.get('From','Unknown')}\n"
                f"Subject: {headers.get('Subject','No subject')}\n"
                f"Date: {headers.get('Date','')}\n\n{body}")
    except Exception as e:
        logger.error("Gmail read error: %s", e)
        return f"⚠️ Could not read email: {e}"


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
            userId="me", body={"message": {"raw": raw}},
        ).execute()
        logger.info("Email draft created for %s: %s", to_addr, subject)
        return True
    except Exception as e:
        logger.error("Draft email error: %s", e)
        return False


# ── Drive search (ported from bot.py) ────────────────────────────────────────────
def search_drive(query: str) -> str:
    """Search Google Drive files by name and full-text content (read-only)."""
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
