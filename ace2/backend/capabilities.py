"""What a task can actually do — the ONE implementation voice and typing both use.

Two rules run through this file, both bought with the 9 September spreadsheet.

RESULTS ARE READ BACK, NOT ASSUMED. `mcp_client.call` flattens a tool result to text and
never inspects `isError`, so a provider-level failure arrives looking exactly like content.
On 9 September the model narrated "the sheet's built and populated", composed a link out of
that prose, and Google told Brady the file did not exist. Nothing here reports success
without having re-read the artefact from the provider and matched what it wrote.

A LINK IS BUILT FROM A VERIFIED ID. Never from model text, and never from an id that has not
survived a read-back. And being able to read a file over Ace's own connection is NOT the same
as Brady being able to open it in his browser — that difference is the likeliest reason he
saw "does not exist" — so ownership is checked separately and, when it cannot be established,
said plainly instead of dressed up as ready.
"""
import json
import logging
import os
import re

logger = logging.getLogger("ace2.capabilities")

# The Google identity Brady is signed in as. Set it and a created file whose owner does not
# match is reported as inaccessible rather than announced as ready.
EXPECTED_USER = os.environ.get("ACE2_GOOGLE_USER", "").strip().lower()

_ID_KEYS = ("spreadsheetId", "spreadsheet_id", "fileId", "file_id", "documentId",
            "document_id", "id")
# A Google file id: 25+ of the URL-safe alphabet. Tight enough not to match ordinary prose.
_ID_RE = re.compile(r"\b([A-Za-z0-9_-]{25,80})\b")
_URL_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{25,80})")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class Failed(Exception):
    """Execution failed with an explanation fit to show Brady."""

    def __init__(self, message: str, result: dict = None):
        super().__init__(message)
        self.message = message
        self.result = result or {}


def _looks_like_error(text: str) -> bool:
    """Provider text that means failure.

    mcp_client returns the flattened text of a tool result and does not look at the
    protocol's isError flag, so this is the only place a tool-level error can be caught
    before it is mistaken for a success.
    """
    t = (text or "").strip()
    if not t or t == "(no content returned)":
        return True
    low = t.lower()
    if t.startswith("⚠️"):
        return True
    return any(m in low for m in (
        "error", "failed", "denied", "not found", "does not exist", "forbidden",
        "unauthorized", "permission", "invalid_grant", "quota", "insufficient",
        "traceback", "exception",
    ))


def extract_id(text: str) -> str:
    """The provider's own id for the thing it just made, or '' — never a guess.

    Prefers structured JSON, then an id embedded in a returned URL, then a bare token. If
    none of those produce something, the caller must fail: composing a plausible-looking id
    is precisely how Brady got a link to a file that was not there.
    """
    t = (text or "").strip()
    if not t:
        return ""
    try:
        blob = json.loads(t)
        stack = [blob]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k in _ID_KEYS:
                    v = cur.get(k)
                    if isinstance(v, str) and len(v) >= 25:
                        return v
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    except Exception:
        pass
    m = _URL_ID_RE.search(t)
    if m:
        return m.group(1)
    for cand in _ID_RE.findall(t):
        if not cand.isalpha() or len(cand) >= 30:   # skip long ordinary words
            return cand
    return ""


def sheet_url(file_id: str) -> str:
    """The ordinary owner-access URL. This grants nothing: it is the same address the owner
    would use themselves, which is why creating one is not a sharing decision."""
    if not file_id:
        raise Failed("no spreadsheet id, so there is no link to give")
    return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"


def a1(rows: list) -> str:
    n = max(1, len(rows))
    width = max((len(r) for r in rows), default=1)
    last = ""
    w = max(1, width)
    while w:
        w, rem = divmod(w - 1, 26)
        last = chr(65 + rem) + last
    return f"A1:{last}{n}"


def _cells(text: str) -> list:
    """Rows out of a read-back, whatever shape the provider used to say them."""
    t = (text or "").strip()
    try:
        blob = json.loads(t)
        if isinstance(blob, dict):
            for k in ("values", "rows", "data"):
                if isinstance(blob.get(k), list):
                    return blob[k]
        if isinstance(blob, list):
            return blob
    except Exception:
        pass
    return [[c.strip() for c in ln.split("\t")] for ln in t.splitlines() if ln.strip()]


def _flat(rows) -> set:
    out = set()
    for r in rows or []:
        if isinstance(r, (list, tuple)):
            out |= {str(c).strip() for c in r if str(c).strip()}
        elif str(r).strip():
            out.add(str(r).strip())
    return out


async def create_spreadsheet(args: dict, call, progress=None, known=None,
                             checkpoint=None) -> dict:
    """Create a spreadsheet, write it, READ IT BACK, and only then return a link.

    `call(tool, arguments) -> str` is the provider (mcp_client.call in production, a fake in
    tests). `progress(detail)` is optional and only ever reports what is being attempted.

    Returns a verified result. Raises Failed with a shovel-ready explanation otherwise.
    """
    title = (args.get("title") or "").strip()
    rows = args.get("rows") or []
    if not title:
        raise Failed("no title was given, so nothing was created")
    if not rows:
        raise Failed("no contents were given, so nothing was created")

    async def say(msg):
        if progress:
            await progress(msg)

    # 1 — create, and demand a real id back.
    #
    # NEVER CREATE TWICE. A create that succeeds and then loses its response is the one
    # failure that costs a duplicate document, so an id recorded by an earlier attempt is
    # reused and the create is skipped. `known` comes from the task row, which was written
    # the instant the provider answered — before anything downstream could time out.
    file_id = str((known or {}).get("file_id") or "").strip()
    if file_id:
        await say("Picking up the spreadsheet that was already created")
    else:
        await say("Creating the spreadsheet")
        made = await call("mcp_create_spreadsheet", {"title": title})
        if _looks_like_error(made):
            raise Failed(f"the provider refused to create it: {str(made)[:200]}")
        file_id = extract_id(made)
        if file_id and checkpoint:
            # Durable BEFORE the next call. This is the line that turns a lost response
            # from a duplicate file into a resumable task.
            await checkpoint({"file_id": file_id, "url": sheet_url(file_id)})
    if not file_id:
        # This is the 9 September failure caught at its source: without an id from the
        # provider there is nothing to link to, and a link invented here would 404.
        raise Failed("the provider did not return a spreadsheet id, so nothing can be "
                     "linked; it is not safe to say this was created")

    # 2 — write the contents
    await say("Writing the rows")
    rng = a1(rows)
    wrote = await call("mcp_modify_sheet_values",
                       {"spreadsheet_id": file_id, "range": rng, "values": rows})
    if _looks_like_error(wrote):
        raise Failed(f"the spreadsheet was created but the rows would not write: "
                     f"{str(wrote)[:160]}",
                     {"file_id": file_id, "url": sheet_url(file_id), "partial": True})

    # 3 — read it back. This is the step whose absence let Ace claim a populated sheet.
    await say("Reading it back to check")
    got = await call("mcp_read_sheet_values", {"spreadsheet_id": file_id, "range": rng})
    if _looks_like_error(got):
        raise Failed(f"the spreadsheet could not be read back, so it is not verified: "
                     f"{str(got)[:160]}",
                     {"file_id": file_id, "url": sheet_url(file_id), "partial": True})
    back = _flat(_cells(got))
    if not back:
        raise Failed("reading it back returned nothing, so the contents are unverified",
                     {"file_id": file_id, "url": sheet_url(file_id), "partial": True})
    # Representative cells: the header row and the first cell of each written row.
    expected = _flat([rows[0]]) | {str(r[0]).strip() for r in rows[1:] if r and str(r[0]).strip()}
    missing = sorted(x for x in expected if x not in back)
    if missing:
        raise Failed("the spreadsheet does not contain what was written — missing "
                     + ", ".join(repr(m) for m in missing[:4])
                     + (f" and {len(missing) - 4} more" if len(missing) > 4 else ""),
                     {"file_id": file_id, "url": sheet_url(file_id), "partial": True})

    # 4 — can the person who asked actually open it? Reading it over Ace's connection only
    #     proves ACE can. Brady's "file does not exist" is what the difference looks like.
    warnings = []
    owner, access = await _owner_of(file_id, call)
    if access == "mismatch":
        raise Failed(
            f"the spreadsheet exists and is correct, but it belongs to {owner} — not the "
            f"account you are signed into, so the link will read as 'file does not exist' "
            f"for you. Nothing was shared to work around that; connect the Google account "
            f"you use and ask again.",
            {"file_id": file_id, "url": sheet_url(file_id), "owner": owner})
    if access == "unknown":
        warnings.append("I could not confirm from the provider which account owns this, so I "
                        "cannot promise it opens for you until you try it.")

    if args.get("bold_header") or args.get("formatting"):
        # Said plainly rather than attempted with a Docs tool, which is what happened on
        # 9 September: mcp_modify_doc_text was aimed at a spreadsheet and sat unapproved.
        warnings.append("Header bolding and column widths are not something this connection "
                        "can do — the values are all in, the formatting is not.")

    return {
        "file_id": file_id,
        "url": sheet_url(file_id),
        "action_label": "Open spreadsheet",
        "title": title,
        "rows_written": len(rows),
        "cells_verified": len(expected),
        "owner": owner or "",
        "access": access,
        "link_kind": "owner-access URL — no sharing permission was created or changed",
        "warnings": warnings,
    }


async def _owner_of(file_id: str, call) -> tuple:
    """(owner, 'owner' | 'mismatch' | 'unknown') — never granted, only observed.

    Deliberately read-only. The 9 September transcript shows Ace reaching for a shareable
    link when Brady could not open the file; minting a link is a permission change and is
    not a diagnosis.
    """
    for tool, key in (("mcp_get_drive_file_metadata", "file_id"),
                      ("mcp_get_drive_file_info", "file_id"),
                      ("mcp_search_drive_files", "query")):
        try:
            out = await call(tool, {key: file_id if key == "file_id" else f"'{file_id}'"})
        except Exception:
            continue
        if not out or _looks_like_error(out):
            continue
        found = _EMAIL_RE.findall(out)
        if found:
            owner = found[0].lower()
            if not EXPECTED_USER:
                return owner, "unknown"
            return owner, ("owner" if owner == EXPECTED_USER else "mismatch")
    return "", "unknown"


# capability name → (handler, human title, whether it may run unattended)
REGISTRY = {
    "create_spreadsheet": {
        "handler": create_spreadsheet,
        "title": "Spreadsheet",
        "verb": "Creating a spreadsheet",
    },
}


def supported(name: str) -> bool:
    return name in REGISTRY
