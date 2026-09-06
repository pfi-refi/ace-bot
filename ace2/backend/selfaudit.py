"""
Ace auditing his OWN bookkeeping, on Railway's clock.

WHY THIS MOVED (Phase 5, 2026-09-06). These checks lived in ~/ace-ops as a laptop cron and
went dark 53 hours in one week, because a session on Brady's machine is not infrastructure.
Everything Phase 4 added — the WAITING guard, STALE, WAITINGCLOSED — is only worth anything
if something is actually watching, so the watcher now runs where Ace runs.

WHAT IT IS NOT. This is not the Noticer (`chat._watch_loop`), which watches BRADY's world —
inbox, calendar, deals going cold — and speaks up. This watches ACE, and every check is a
regression that genuinely happened. Confusing the two has cost real time; they are different
jobs with different failure modes.

COSTS NOTHING. Pure database reads and deterministic comparisons — no model call anywhere in
this file. That is deliberate: a guard whose price scales with how broken things are will get
turned off exactly when it matters.

The detector bodies below are ported VERBATIM from the ace-ops script, which carries 56
offline assertions against them (including the false positives found on the first live tick).
Only collection and delivery are new. Keep the two in sync: if you change a rule here, change
it there and re-run test_watch.py — the tests are the reason these rules are trustworthy.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from . import daybank, db
from .db import EASTERN

logger = logging.getLogger("ace2.selfaudit")

# Categories Ace is allowed to use. Anything else is drift.
CATEGORIES = {"Money", "Bills", "Opportunities", "Goals", "Personal", "Deals",
              "Agents", "Admin", "Networking", "Business", "Tech"}

_STOP = {"the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "at", "is", "are",
         "be", "with", "his", "her", "my", "it", "that", "this", "about", "from", "by"}

# Distinguishing detail: if two items disagree on a number or a date, they are
# different obligations no matter how alike the words are. This is what keeps the
# truck payment separate from the truck arrears, and the two Klarna accounts apart.
_NUM = re.compile(r"\$[\d,]+(?:\.\d\d)?|\b\d{1,2}(?:st|nd|rd|th)\b|\b\d{1,2}/\d{1,2}\b")


# Shelves that must never be auto-closed. Brady closes these by hand, out loud.
PROTECTED = {"Bills", "Goals", "Deals"}
# The register as verified on 2026-08-25. A drop means bills are being eaten again.
BILLS_EXPECTED = 16
DUP_THRESHOLD = 0.84      # below add_item's 0.90 so we see them coming. Verified on the live
                          # board: 0.84 surfaces exactly the real dup, 0.82 finds nothing more.
_FACTDUP_MAX = 40         # enough to prove memory is twinning; counting them all is wasted work
_FACTDUP_BUDGET = 120_000 # hard comparison ceiling — a pathological memory must not hang the audit
MASSCLOSE_N = 6           # this many closures...
MASSCLOSE_MINUTES = 10    # ...inside this window is not a human working


def _norm(text: str) -> frozenset:
    """Token set for near-duplicate detection — mirrors db._norm_item."""
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return frozenset(t.rstrip("s") for t in toks if len(t) > 1 and t not in _STOP)


def _score(a: str, b: str) -> float:
    """Same shape as add_item's dedup: token overlap, backed by sequence ratio.

    Containment (overlap / shorter set) is the right measure for a shortened restatement
    of the same item, but it reads 100% for ANY short item whose words happen to sit
    inside a longer one — "5 deals a month" scores 100% against "Lauren Crimines IUL
    deal, follow up in ~1 month" purely on {deal, month}. So containment only counts
    when the two are comparably sized; otherwise fall back to Jaccard, which charges
    for the words the longer item does not share.
    """
    an, bn = _norm(a), _norm(b)
    if not an or not bn:
        return 0.0
    inter = len(an & bn)
    ratio = difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    size_ratio = min(len(an), len(bn)) / max(len(an), len(bn))
    if size_ratio < 0.6:
        return max(inter / len(an | bn), ratio)
    return max(inter / min(len(an), len(bn)), ratio)


def _distinguishing(text: str) -> frozenset:
    return frozenset(m.group().lower().replace(",", "") for m in _NUM.finditer(text or ""))


def _conflicting_numbers(a: frozenset, b: frozenset) -> bool:
    """True only when two number-sets genuinely CONTRADICT each other.

    Numbers prove difference only when BOTH sides carry one the other lacks — truck payment
    ($500/17th) vs truck arrears ($665). When one set merely CONTAINS the other, that is the
    same fact elaborated, not a different obligation: "keep $165 for taxes" and "keep ~20% for
    taxes" on the same $826 payout are one task written twice, and the old strict inequality
    silently skipped the pair (real miss, 2026-08-27).
    """
    return bool(a - b) and bool(b - a)


def _is_obligation(it) -> bool:
    """True for a recurring bill, False for a one-off task filed under Bills.

    The Bills column holds both — "Mortgage (Cross Country) - $1,176.84 - due 14th" is a
    standing obligation Brady closes deliberately; "Call Cross Country to confirm payment
    isn't due until October" is a task that SHOULD get ticked off. Flagging the second as a
    shelf violation is exactly the noise that teaches someone to ignore a real alarm. Same
    test Ace uses in db._is_recurring_bill: money AND a repeating due signal.
    """
    t = it.get("text") or ""
    if "$" not in t:
        return False
    return bool(re.search(r"due\s+(the\s+)?\d{1,2}(st|nd|rd|th)\b", t, re.I)
                or re.search(r"/\s*mo(nth)?\b", t, re.I)
                or re.search(r"\bmonthly\b", t, re.I))


def _cat(item) -> str | None:
    for t in (item.get("tags") or []):
        if t in CATEGORIES:
            return t
    return None


def _parse(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def audit(now: dict, prev: dict | None) -> list:
    """Compare current state against the previous snapshot. Returns findings, worst first."""
    findings = []
    items = now["items"]
    open_items = [i for i in items if i.get("status") == "open"]
    prev_status = {i["id"]: i.get("status") for i in (prev or {}).get("items", [])}

    # SHELF and MASSCLOSE are CHANGE detectors — they need a prior snapshot to mean
    # anything. Without one, every historically-closed item looks like a fresh event
    # and the report drowns in months of old news. Stay quiet and let this run become
    # the baseline instead.
    have_baseline = prev is not None

    # --- WAITINGCLOSED: the Phase 4 guard failed --------------------------------
    # A record parked on someone else must never close by itself. On 5 Sept four of them
    # went in a single update — Feliz, Thiami, Rebecca, Sienna's aunt — because WAITING did
    # not exist as a state. It does now, and the sweep refuses to touch it, so this detector
    # exists to prove that holds in production rather than assume it.
    # It also reports if the model fields vanish from the API, which would mean a deploy
    # regressed the schema and every protection above it is silently off.
    if have_baseline:
        prev_state = {i["id"]: (i.get("state") or "") for i in (prev or {}).get("items", [])}
        broke = [it for it in items
                 if it.get("status") != "open"
                 and prev_state.get(it["id"]) == "waiting"
                 and prev_status.get(it["id"]) == "open"]
        for it in broke:
            findings.append({
                "class": "WAITINGCLOSED", "severity": "high", "id": it["id"],
                "detail": "a WAITING record was closed — it is parked on someone else, so "
                          "nothing on Brady's side completes it",
                "text": (it.get("text") or "")[:110],
            })
    if items and not any("entry" in i for i in items):
        findings.append({
            "class": "WAITINGCLOSED", "severity": "high",
            "detail": "the board is not returning `entry`/`state` — the records-vs-actions "
                      "model is not live, so the WAITING guard and the PARKED lane are off",
            "texts": [],
        })

    # --- SHELF: a protected item closed since we last looked -------------------
    for it in (items if have_baseline else []):
        if it.get("status") == "open":
            continue
        cat = _cat(it)
        if cat not in PROTECTED:
            continue
        if prev_status.get(it["id"]) != "open":
            continue          # already closed last time, or brand new — not a fresh event
        if cat == "Bills" and not _is_obligation(it):
            continue          # a task filed under Bills is meant to be completed
        findings.append({
            "class": "SHELF", "severity": "high", "id": it["id"], "category": cat,
            "detail": f"{cat} item was closed — that shelf is no-touch",
            "text": (it.get("text") or "")[:110],
        })

    # --- MASSCLOSE: a burst of closures, the Aug-16 signature -------------------
    fresh = []
    for it in (items if have_baseline else []):
        if it.get("status") == "open" or not it.get("done_ts"):
            continue
        if prev_status.get(it["id"]) != "open":
            continue
        t = _parse(it["done_ts"])
        if t:
            fresh.append((t, it))
    fresh.sort(key=lambda p: p[0])
    for i in range(len(fresh)):
        window = [p for p in fresh[i:] if (p[0] - fresh[i][0]).total_seconds() <= MASSCLOSE_MINUTES * 60]
        if len(window) >= MASSCLOSE_N:
            findings.append({
                "class": "MASSCLOSE", "severity": "high",
                "detail": (f"{len(window)} items closed inside {MASSCLOSE_MINUTES} min "
                           f"starting {window[0][0].astimezone(EASTERN):%b %d %-I:%M %p} ET "
                           f"— check these were all real"),
                "ids": [p[1]["id"] for p in window],
                "texts": [(p[1].get("text") or "")[:70] for p in window[:8]],
            })
            break

    # --- DUPES: near-identical open items --------------------------------------
    seen = set()
    for a in range(len(open_items)):
        for b in range(a + 1, len(open_items)):
            x, y = open_items[a], open_items[b]
            key = tuple(sorted((x["id"], y["id"])))
            if key in seen:
                continue
            # different amounts or dates => different obligations, never a dup
            if _conflicting_numbers(_distinguishing(x.get("text")),
                                    _distinguishing(y.get("text"))):
                continue
            s = _score(x.get("text"), y.get("text"))
            if s >= DUP_THRESHOLD:
                seen.add(key)
                findings.append({
                    "class": "DUPES", "severity": "high" if s >= 0.95 else "medium",
                    "score": round(s, 2), "ids": [x["id"], y["id"]],
                    "detail": f"{int(s*100)}% match across {_cat(x)} / {_cat(y)}",
                    "texts": [(x.get("text") or "")[:90], (y.get("text") or "")[:90]],
                })

    # --- BILLS: register integrity ---------------------------------------------
    bills = [i for i in open_items if _cat(i) == "Bills"]
    paid = [i for i in items if _cat(i) == "Bills" and i.get("status") != "open"]
    # EASTERN month, not UTC. Brady lives in Eastern; between 8pm and midnight on the last
    # day of a month UTC has already rolled over, so a UTC comparison finds nothing paid
    # "this month" and the register reads as collapsed. Fired a false HIGH at 8:18pm ET on
    # 2026-08-31 claiming 3 obligations of an expected 16 — the real count was 21.
    this_month = datetime.now(EASTERN).month
    total = len(bills) + len([p for p in paid
                              if _parse(p.get("done_ts"))
                              and _parse(p["done_ts"]).astimezone(EASTERN).month == this_month])
    if total < BILLS_EXPECTED:
        findings.append({
            "class": "BILLS", "severity": "high",
            "detail": (f"register shows {total} obligations, expected {BILLS_EXPECTED} "
                       f"— bills may be getting hidden again"),
        })

    # --- CALDUP: the same event booked more than once --------------------------
    slots = {}
    for e in now.get("events", []):
        title = (e.get("title") or e.get("summary") or "").strip().lower()
        start = str(e.get("start") or "")
        if not title or not start:
            continue
        slots.setdefault((title, start), []).append(e)
    for (title, start), group in slots.items():
        if len(group) > 1:
            findings.append({
                "class": "CALDUP", "severity": "high",
                "detail": f"'{title[:60]}' booked {len(group)}x at the same time ({start})",
                "count": len(group),
            })

    # --- UNCAT: items that fall out of every view ------------------------------
    uncat = [i for i in open_items if _cat(i) is None]
    if uncat:
        findings.append({
            "class": "UNCAT", "severity": "low",
            "detail": f"{len(uncat)} open item(s) with no category — invisible on the board",
            "ids": [i["id"] for i in uncat[:10]],
            "texts": [(i.get("text") or "")[:70] for i in uncat[:5]],
        })

    # --- OVERDUE: aging without movement ---------------------------------------
    stale = []
    for i in open_items:
        dd = i.get("due_days")
        if isinstance(dd, (int, float)) and dd <= -3:
            stale.append((dd, i))
    stale.sort(key=lambda p: p[0])
    if stale:
        findings.append({
            "class": "OVERDUE", "severity": "medium" if stale[0][0] <= -7 else "low",
            "detail": f"{len(stale)} item(s) 3+ days overdue, oldest {abs(int(stale[0][0]))}d",
            "texts": [f"{abs(int(d))}d late — {(i.get('text') or '')[:64]}" for d, i in stale[:6]],
        })

    # --- STALE: a row that contradicts itself, or names a date that has passed -----
    # THE TRUCK ROW (2026-09-05). Its header said "$500/mo, due 17th" while its own last
    # sentence said "$473 resuming 9/15", and that dead $500 leaked into Brady's recaps four
    # times over three days. Nobody re-reads a row once it is written, so a row has to be able
    # to report that it disagrees with itself.
    #
    # PRECISION MATTERS MORE THAN RECALL HERE. Seven live rows carry 2+ dollar amounts and six
    # of them are legitimate breakdowns ($826 -> $661 + $165; $56.66/mo on a $1,359.83 plan;
    # $77.94/mo of ~$468 owed). Counting amounts would cry wolf six times out of seven. A real
    # contradiction is two amounts competing for the SAME recurring slot: one carries /mo, and
    # the other sits next to a correction word.
    _CUE = re.compile(r"\b(resum\w+|now|actually|chang\w+\s+to|updat\w+\s+to|corrected"
                      r"|instead|no longer|not\s+\$)\b", re.I)
    _AMT = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
    _PERMO = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)\s*(?:/\s*mo|per\s+month|monthly)", re.I)
    # TWO CORRECTIONS FROM THE FIRST LIVE TICK (2026-09-05 22:08), both false positives:
    #
    # (a) ONLY OBLIGATIONS CAN CONTRADICT AN OBLIGATION. The Friday upgrade note was flagged
    #     because it costs out "~$9/mo", "$12/month", "$10/month" — estimates in a planning
    #     row, not a bill. Restricting to the columns where recurring obligations actually
    #     live removes the whole class.
    # (b) A ROW THAT DOCUMENTS ITS OWN HISTORY IS THE FIXED STATE, NOT THE BROKEN ONE. The
    #     truck row now reads "$473/mo, due the 15th … the old $500-due-the-17th were the
    #     PRE-REPO terms and are dead" — exactly what a corrected row should look like, and
    #     the detector flagged it anyway. Left alone it would nag Brady forever about the one
    #     row he already fixed, which is how a watchdog teaches you to ignore it. An amount
    #     sitting in a clause marked dead is documentation; only a LIVE competing amount is a
    #     contradiction.
    _DEAD = re.compile(r"\b(old|dead|was|were|pre-|prior|previous\w*|history|no longer|former"
                       r"|repossess\w*|expired|replaced)\b", re.I)
    _OBLIGATION_CATS = {"Bills", "Money"}
    contradictions = []
    for i in open_items:
        if _cat(i) not in _OBLIGATION_CATS:
            continue
        t = i.get("text") or ""
        recurring = {a.replace(",", "") for a in _PERMO.findall(t)}
        if not recurring:
            continue
        # THIRD CORRECTION (2026-09-06 10:32), from two false positives on rows I had just
        # IMPROVED. A well-written bill row legitimately carries several monthly figures:
        #   "$106/mo minimum ... ~$70/mo is interest, so only ~$36 hits principal"
        #   "$27/mo minimum ... needs ~$220/mo to clear before the rate jumps"
        # Those are DECOMPOSITIONS and TARGETS, not competing claims. `len(recurring) > 1`
        # treated them as contradictions and shouted HIGH at the two best rows on the board.
        #
        # A real contradiction is two amounts that each claim to BE the obligation — the truck
        # row's "$500/mo, due 17th" against its own "$473 resuming 9/15". So an amount only
        # counts as competing when nothing right before it marks it as a part, a target, or
        # the past.
        _NOTCLAIM = re.compile(
            r"\b(interest|principal|of which|needs?|takes?|requires?|to\s+clear|toward|"
            r"raised\s+from|up\s+from|down\s+from|was|were|old|previously|instead\s+of)\b",
            re.I)
        # A WINDOW, not adjacency: "Interest is about $57/mo" puts the marker 18 characters
        # back, which a tight \W{0,14} anchor missed. 30 chars is wide enough for the phrasings
        # that actually occur and still narrow enough that the truck row's "…due 17th. Payments
        # $473 resuming" contains no marker and stays a real contradiction.
        _LOOKBACK = 30

        # The marker can sit on EITHER side: "needs ~$220/mo" puts it before, "~$70/mo is
        # interest" puts it after. Checking only backwards left Capital One flagged.
        _NOTCLAIM_AFTER = re.compile(
            r"^\s*(?:/\s*mo|per\s+month|monthly)?\s*(?:is|goes|of)?\s*"
            r"(interest|principal|toward|to\s+principal)", re.I)

        def _claims(text_, amounts):
            out = set()
            for m in re.finditer(r"\$\s*~?([\d,]+(?:\.\d{1,2})?)", text_):
                a = m.group(1).replace(",", "")
                if a not in amounts:
                    continue
                if _NOTCLAIM.search(text_[max(0, m.start() - _LOOKBACK):m.start()]):
                    continue
                if _NOTCLAIM_AFTER.search(text_[m.end():m.end() + 34]):
                    continue
                out.add(a)
            return out

        claiming = _claims(t, recurring)
        live_others = set()
        for seg in re.split(r"[.;]|\s—\s", t):
            if _DEAD.search(seg):
                continue                       # this clause is explaining the past, not asserting
            for a in _claims(seg, {x.replace(",", "") for x in _AMT.findall(seg)}):
                if a not in recurring:
                    live_others.add(a)
        if len(claiming) > 1:
            contradictions.append(i)
        elif live_others and _CUE.search(t):
            contradictions.append(i)
    if contradictions:
        findings.append({
            # Downgraded from high 2026-09-06: even a true hit here is "a row has a stale
            # number", not something broken. HIGH pushes to Brady's lock screen.
            "class": "STALE", "severity": "medium",
            "detail": f"{len(contradictions)} row(s) state two different amounts for the same "
                      f"recurring obligation — the truck-row failure",
            "texts": [(x.get("text") or "")[:96] for x in contradictions[:5]],
        })

    # A date in the row's own words that has already passed, on a row with no due date — so
    # OVERDUE (which reads due_days) is blind to it. Live example: "Meet with Tony at
    # Groundworks office Friday 9/4 at 1PM", still open the day after.
    # HISTORY IS NOT STALENESS: "call happened Wed 9/2", "crisis from 8/26 is resolved" and
    # "decided 8/30" are records of the past and must not be flagged, so the history cue is
    # checked in the SAME CLAUSE as the date, not anywhere in the row.
    # Two conditions, because "absence of a history word" was not enough: "tried 8/3, broke
    # every call" and "(confirmed 8/28)" are TIMESTAMPS, not deadlines, and both slipped through.
    #   (a) the clause must carry FORWARD INTENT — something Brady still has to do;
    #   (b) the date must not be stamped by a history verb sitting right in front of it.
    # ("24/7" and "50/50" are excluded for free by the 1-12 month check below.)
    _DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
    _FWD = re.compile(r"\b(meet|meeting|call|due|by|deadline|add\s+to|send|finish|start|submit"
                      r"|appointment|pay|file|mail|book)\b", re.I)
    # A day name may sit between the verb and the date — "call happened Wed 9/2" — so allow one.
    _STAMP = re.compile(r"\b(confirmed|decided|tried|measured|happened|resolved|moved|updated"
                        r"|reverted|brought\s+in|as\s+of|since|was|were)\s*"
                        r"(?:(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\s*)?\W{0,3}$", re.I)
    today = datetime.now(EASTERN).date()
    past_dated = []
    for i in open_items:
        if i.get("due_days") is not None:
            continue
        t = i.get("text") or ""
        for seg in re.split(r"[.;:]|\s—\s", t):
            m = _DATE.search(seg)
            if not m or not _FWD.search(seg):
                continue
            if _STAMP.search(seg[:m.start()]):        # "confirmed 8/28" is a stamp, not a due date
                continue
            try:
                mo, dy = int(m.group(1)), int(m.group(2))
                if not (1 <= mo <= 12 and 1 <= dy <= 31):
                    continue
                when = datetime(today.year, mo, dy).date()
            except ValueError:
                continue
            if (today - when).days > 0 and (today - when).days < 180:
                past_dated.append((when, i))
                break
    if past_dated:
        past_dated.sort(key=lambda p: p[0])
        findings.append({
            "class": "STALE", "severity": "medium",
            "detail": f"{len(past_dated)} open row(s) name a date that has already passed and "
                      f"carry no due date, so OVERDUE cannot see them",
            "texts": [f"{w.strftime('%b %-d')} — {(x.get('text') or '')[:76]}" for w, x in past_dated[:5]],
        })

    # --- FACTDUP: memory growing twins -----------------------------------------
    # /memory/facts returns archived rows too (archive-not-delete keeps them searchable).
    # Comparing those against live ones re-reports every pair that was ALREADY cleaned,
    # so a tidy memory would alarm forever. Only active facts can be duplicates.
    facts = [f for f in now.get("facts", [])
             if isinstance(f, dict) and not f.get("invalid_at") and f.get("tier") != "archived"]
    rows = [(f.get("id"), f.get("text") or "") for f in facts][-400:]   # newest slice only
    # Precompute per FACT, not per PAIR. The naive loop re-ran the regex scan and the token
    # split inside the comparison, which is 160k redundant computations for 400 facts — 31s,
    # growing quadratically as memory fills. Prep once, then compare cheap sets.
    prep = [(t, _norm(t), _distinguishing(t)) for _, t in rows]
    # The token gate collapses ordinary memory to near-zero work, but it filters NOTHING
    # when facts are genuinely all alike — and "Ace is mass-producing twins" is precisely
    # when this detector matters. 400 such facts ran 531s unbounded. We only need to know
    # THAT memory is duplicating, never the exact count, so stop early and stay honest
    # about having stopped.
    dup_pairs = 0
    budget = _FACTDUP_BUDGET
    capped = False
    for a in range(len(prep)):
        if dup_pairs >= _FACTDUP_MAX or budget <= 0:
            capped = True
            break
        ta, na, da = prep[a]
        if not na:
            continue
        for b in range(a + 1, len(prep)):
            budget -= 1
            if budget <= 0:
                capped = True
                break
            tb, nb, db = prep[b]
            if not nb or _conflicting_numbers(da, db):
                continue        # contradicting amounts/dates => different facts, never a dup
            # Token gate before the expensive SequenceMatcher: nothing sharing under half
            # its words with the other survives a 0.90 score, so this is safe to skip.
            if len(na & nb) / max(len(na), len(nb)) < 0.5:
                continue
            if _score(ta, tb) >= 0.90:
                dup_pairs += 1
                if dup_pairs >= _FACTDUP_MAX:
                    capped = True
                    break
    if dup_pairs >= 3:
        findings.append({
            "class": "FACTDUP", "severity": "low",
            "detail": (f"{dup_pairs}{'+' if capped else ''} near-identical fact pair(s) "
                       f"in recent memory" + (" (stopped counting)" if capped else "")),
        })

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    return findings


# ── Collection: direct reads, no HTTP, no token ────────────────────────────────────
def _collect() -> dict:
    """Everything the audit needs. Each source is optional — one failure must not blind
    the others, which is why this is three try blocks and not one."""
    out = {"at": datetime.now(timezone.utc).isoformat(), "errors": []}
    try:
        out["items"] = daybank.read_items(False)
    except Exception as e:
        out["items"] = []; out["errors"].append(f"board: {e}")
    try:
        from .integrations.calendar_api import get_events_structured
        out["events"] = get_events_structured(14)
    except Exception as e:
        out["events"] = []; out["errors"].append(f"calendar: {e}")
    try:
        out["facts"] = db.read_facts_full()
    except Exception as e:
        out["facts"] = []; out["errors"].append(f"facts: {e}")
    return out


_SNAP_KIND = "watch_snapshot"


def _load_prev():
    """The previous snapshot, or None on the very first run.

    None is meaningful: the change-detectors (SHELF, MASSCLOSE, WAITINGCLOSED) stay SILENT
    without a baseline. Without that guard the first run reported 40+ historical closures and
    a July burst as fresh emergencies — the bug that made the original unusable.
    """
    try:
        row = db.latest_summary(_SNAP_KIND) or {}
        return json.loads(row["text"]) if row.get("text") else None
    except Exception:
        return None


def _save_snapshot(now: dict) -> None:
    """Store ONLY what audit() reads back: id, status, state. The full board is ~240KB and
    this runs several times a day; the diff needs three fields per row."""
    try:
        slim = {"at": now.get("at"),
                "items": [{"id": i.get("id"), "status": i.get("status"), "state": i.get("state")}
                          for i in now.get("items") or []]}
        db.add_summary(json.dumps(slim), _SNAP_KIND)
    except Exception as e:
        logger.warning("selfaudit snapshot save failed: %s", e)


def run_once(push: bool = True) -> list:
    """One tick. Returns findings, worst first. Never raises — a broken guard must not take
    the app down with it."""
    try:
        now = _collect()
        prev = _load_prev()
        findings = audit(now, prev)
        _save_snapshot(now)
        if findings:
            logger.info("selfaudit: %d finding(s): %s", len(findings),
                        ", ".join(f["class"] for f in findings[:8]))
        high = [f for f in findings if f.get("severity") == "high"]
        if push and high:
            try:
                from .main import send_push
                lead = high[0]
                send_push("Ace caught itself",
                          f"{lead['class']}: {lead.get('detail', '')[:120]}",
                          url="/", tag="selfaudit")
            except Exception as e:
                logger.warning("selfaudit push failed: %s", e)
        return findings
    except Exception as e:
        logger.warning("selfaudit tick failed: %s", e)
        return []


# Deliberately infrequent. The checks are free, but a PUSH is not — this reaches Brady's lock
# screen, and a guard that cries three times a day is a guard he learns to swipe away.
_TICK_SECONDS = 4 * 60 * 60


async def loop() -> None:
    await asyncio.sleep(90)          # let the first boot settle before auditing it
    while True:
        try:
            await asyncio.to_thread(run_once, True)
        except Exception as e:
            logger.warning("selfaudit loop error: %s", e)
        await asyncio.sleep(_TICK_SECONDS)
