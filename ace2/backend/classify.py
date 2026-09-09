"""One interpretation of a board row, shared by Ace's context and every screen.

WHY THIS EXISTS (2026-09-08). The command centre and the Due Today panel each derived
their own answer to "what is this row and does it need Brady". `app.js` dropped settled
rows from the dated lanes but NOT waiting ones, so a dated WAITING record could appear in
Late/Today/Tomorrow with a working "Mark done" checkbox — while the Parked section three
lines below deliberately disabled that same checkbox, because one stray tap is how four
records vanished on 5 September. Two views, two rulesets, one row.

Nothing here derives NEW meaning from prose. `db.py` already decides entry/state/bucket and
those decisions, including Brady's explicit overrides, are taken as given. This module only
answers the presentation question — which single lane does a row belong in, and may it be
completed from there — so that the answer is computed once and everyone reads the same one.

LANES are mutually exclusive and ordered. A row appears in exactly one, which is what makes
counts add up to the total instead of double-counting.
"""

# Ordered: the first match wins, so a waiting row can never also be a dated row.
LANE_SETTLED = "settled"        # finished, kept on the board (closed and paid)
LANE_WAITING = "waiting"        # someone else owns the next move
LANE_OVERDUE = "overdue"        # a real deadline that has passed
LANE_TODAY = "today"            # due today
LANE_UPCOMING = "upcoming"      # dated, still ahead
LANE_UNDECIDED = "undecided"    # actionable, no date and no next step — needs a decision
LANE_ANYTIME = "anytime"        # actionable, undated, but a next step is recorded
LANE_REFERENCE = "reference"    # a record Brady tracks; has a state, not an ending
LANE_DONE = "done"              # status closed

# Lanes whose rows are Brady's to act on. Waiting and reference are deliberately absent:
# they are visible, they are not work.
ACTIONABLE = frozenset({LANE_OVERDUE, LANE_TODAY, LANE_UPCOMING, LANE_UNDECIDED, LANE_ANYTIME})

# Only these may show a completion control. A waiting row finishes when the OTHER person
# acts; a reference row has a lifecycle, not a checkbox; a done row is already done.
COMPLETABLE = ACTIONABLE

LANE_ORDER = (LANE_OVERDUE, LANE_TODAY, LANE_UPCOMING, LANE_UNDECIDED, LANE_ANYTIME,
              LANE_WAITING, LANE_REFERENCE, LANE_SETTLED, LANE_DONE)

LANE_LABELS = {
    LANE_OVERDUE: "Overdue",
    LANE_TODAY: "Today",
    LANE_UPCOMING: "Upcoming",
    LANE_UNDECIDED: "Needs a decision",
    LANE_ANYTIME: "Anytime",
    LANE_WAITING: "Waiting on someone else",
    LANE_REFERENCE: "Records & reference",
    LANE_SETTLED: "Settled",
    LANE_DONE: "Completed",
}

# Cross-cutting shelves. These are TAGS, not containers: a bill in the Personal area stays
# one row in one area and simply also answers to Bills. Nothing is cloned to appear twice.
SHELVES = ("Bills", "Goals")


def area_of(item: dict) -> str:
    """The life/work area for this row.

    A STORED bucket always wins — it is either the judgment pass or Brady's own correction.
    Records carry none, because db.py only fills bucket for actions, which left 41 of 70 open
    rows arealess and the area filter useless for most of the board. Rather than migrate rows,
    fall back to the SAME `derive_bucket` already used for actions. This is presentation only:
    nothing is written, and `bucket_set` still tells a caller whether the area was decided or
    merely derived.
    """
    stored = (item.get("bucket") or "").strip()
    if stored:
        return stored
    try:
        from . import db
        return db.derive_bucket(item.get("text"), (item.get("tags") or [""])[0]) or "Unassigned"
    except Exception:
        return "Unassigned"


def shelves_of(item: dict) -> list:
    """Cross-cutting shelves this row also belongs to, from its existing tags."""
    tags = item.get("tags") or []
    return [s for s in SHELVES if s in tags]


def carried_over(item: dict) -> bool:
    """True for a row that WOULD have been Needs a decision under the old derivation but was
    never marked by Brady. Used by the migration preview and shown as "carried over — not yet
    reviewed", so nothing silently becomes Ready and nothing claims he chose it."""
    if (item.get("status") or "open") != "open":
        return False
    if (item.get("state") or "") in ("waiting", "settled", "decide"):
        return False
    if (item.get("entry") or "") == "record":
        return False
    return not has_next_step(item)


def has_next_step(item: dict) -> bool:
    """Is the next move recorded anywhere? `next_step` is the additive field; a due date or
    an explicit waiting_on also answers 'what happens next', so neither counts as undecided."""
    if (item.get("next_step") or "").strip():
        return True
    if item.get("due_days") is not None or (item.get("due") or "").strip():
        return True
    return bool((item.get("waiting_on") or "").strip())


def lane_of(item: dict) -> str:
    """The ONE lane this row belongs in. Order matters — see the module docstring."""
    if (item.get("status") or "open") != "open":
        return LANE_DONE
    state = (item.get("state") or "").strip()
    if state == "settled":
        return LANE_SETTLED
    # BEFORE the dated lanes, deliberately. This single line is the waiting/due overlap fix:
    # a waiting row is never also an overdue row, so it can never be completed from one view
    # while being protected in another.
    if state == "waiting":
        return LANE_WAITING
    dd = item.get("due_days")
    if dd is not None:
        if dd < 0:
            return LANE_OVERDUE
        if dd == 0:
            return LANE_TODAY
        return LANE_UPCOMING
    # EXPLICIT, NOT DERIVED (release one, 2026-09-09). This used to read "undated action with
    # no next step" as Needs a decision. Brady asked for one authoritative meaning: he marks
    # it. Rows that were only ever in this lane BY DERIVATION are not silently reclassified —
    # the migration proposal carries them over with a marker (see carried_over below), and
    # until that is applied and confirmed they keep showing as Needs a decision.
    if (item.get("state") or "") == "decide":
        return LANE_UNDECIDED
    if (item.get("entry") or "") == "record":
        return LANE_REFERENCE
    return LANE_ANYTIME


def decorate(item: dict) -> dict:
    """Attach the shared interpretation. Additive only — no stored field is altered."""
    lane = lane_of(item)
    return {
        **item,
        "lane": lane,
        "carried_over": carried_over(item),
        "lane_label": LANE_LABELS[lane],
        "area": area_of(item),
        "shelves": shelves_of(item),
        "actionable": lane in ACTIONABLE,
        "completable": lane in COMPLETABLE,
    }


def summarise(items: list) -> dict:
    """Counts per lane plus the totals a view needs to prove nothing is hidden."""
    rows = [decorate(i) for i in (items or [])]
    counts = {lane: 0 for lane in LANE_ORDER}
    for r in rows:
        counts[r["lane"]] += 1
    areas = {}
    for r in rows:
        if r["lane"] not in (LANE_DONE, LANE_SETTLED):
            areas[r["area"]] = areas.get(r["area"], 0) + 1
    return {
        "counts": counts,
        "areas": areas,
        "total": len(rows),
        "open": sum(c for lane, c in counts.items() if lane != LANE_DONE),
        "needs_you": sum(counts[lane] for lane in ACTIONABLE),
    }


# ── DUE TODAY vs THE COMMAND CENTER ────────────────────────────────────────────
# Two surfaces, one set of records. The Command Center is the full board and stays that
# way. Due Today answers a narrower question and must never grow into a second board:
#
#   DEADLINES  — the world's timing. Overdue and due-today rows.
#   CHOSEN     — Brady's timing. Rows he picked up today (`chosen_on`), which is a
#                DIFFERENT column from `due` precisely so that choosing something cannot
#                invent a deadline for it.
#   SUGGESTED  — Ace's opinion, clearly labelled as such. Never written anywhere until
#                Brady accepts it, and accepting it sets `chosen_on`, never `due`.


def chosen_today(item: dict, today: str) -> bool:
    return (item.get("chosen_on") or "")[:10] == today


def due_today_sections(items: list, today: str, suggest: int = 5) -> dict:
    """The three compact groups Due Today shows. Pure — it writes nothing and proposes
    nothing that is not already on the board."""
    rows = [decorate(i) for i in (items or [])]
    live = [r for r in rows if r["lane"] != LANE_DONE]
    deadlines = [r for r in live if r["lane"] in (LANE_OVERDUE, LANE_TODAY)]
    picked = [r for r in live if chosen_today(r, today)
              and r not in deadlines and r["actionable"]]
    taken = {r["id"] for r in deadlines} | {r["id"] for r in picked}
    # A suggestion is real work Brady could reasonably finish: actionable, not already
    # here, not waiting on anyone, not a record. Oldest first — the things quietly rotting.
    pool = [r for r in live
            if r["actionable"] and r["id"] not in taken and r["lane"] != LANE_UPCOMING]
    pool.sort(key=lambda r: (r.get("ts") or ""))
    return {
        "deadlines": sorted(deadlines, key=lambda r: (r.get("due_days") is None,
                                                      r.get("due_days", 0))),
        "chosen": picked,
        "suggested": pool[:suggest],
        "suggested_total": len(pool),
        "waiting": [r for r in live if r["lane"] == LANE_WAITING],
    }
