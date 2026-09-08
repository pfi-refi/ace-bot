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
    if (item.get("entry") or "") == "record":
        return LANE_REFERENCE
    return LANE_ANYTIME if has_next_step(item) else LANE_UNDECIDED


def decorate(item: dict) -> dict:
    """Attach the shared interpretation. Additive only — no stored field is altered."""
    lane = lane_of(item)
    return {
        **item,
        "lane": lane,
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
