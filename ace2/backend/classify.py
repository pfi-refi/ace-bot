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


def area_of(item: dict, *, renames=None) -> str:
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
        return db.derive_bucket(item.get("text"), (item.get("tags") or [""])[0], renames=renames) or "Unassigned"
    except Exception:
        return "Unassigned"


def shelves_of(item: dict) -> list:
    """Cross-cutting shelves this row also belongs to, from its existing tags."""
    tags = item.get("tags") or []
    return [s for s in SHELVES if s in tags]


def needs_decision(item: dict) -> bool:
    """Brady marked this as an open question — whatever lane its dates put it in.

    Lanes are mutually exclusive and a real deadline has to win one, or a dated row he also
    flagged would drop out of Overdue and he would miss it. But losing the lane must not lose
    the MARK: this is the flag every surface reads, so "I haven't decided" and "this is due
    Friday" can both be true and both be visible.
    """
    return (item.get("state") or "") == "decide" and (item.get("status") or "open") == "open"


def carried_over(item: dict) -> bool:
    """A row from the OLD board that the derived rule was calling a decision, which Brady has
    not looked at yet.

    Three facts, and all three have to hold. Two of them are durable and stored, which is the
    correction (Codex, 2026-09-09): this used to be computed from a row's current fields
    alone, so a capture made five seconds ago wore "carried over · not yet reviewed" and
    claimed a history it never had, while a legacy row could shed the flag only by having a
    date or a next step invented for it.

      1. `pre_release_one` — it existed before release one shipped, per a boundary stamped
         once in the database. A row created after it is new work and never wears this.
      2. `reviewed_at` is empty — he has not looked at it. Saying "Ready" is enough to set
         that, with no fabricated date or next step required.
      3. It has the shape the OLD derivation flagged: an open action with no date and no
         next step recorded.

    This is a REVIEW flag, not a status. It never changes a row's lane and never blocks it
    from being completed — but no surface may present a row wearing it as accepted, ready
    work, because nobody has decided that yet.
    """
    if (item.get("status") or "open") != "open":
        return False
    if not item.get("pre_release_one"):
        return False
    if (item.get("reviewed_at") or "").strip():
        return False
    if (item.get("state") or "") in ("waiting", "settled", "decide"):
        return False
    if (item.get("entry") or "") == "record":
        return False
    if (item.get("chosen_on") or "").strip():
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


def open_children_index(items: list) -> dict:
    """{parent id: number of OPEN rows filed under it}, from the rows given."""
    idx = {}
    for it in (items or []):
        pid = it.get("parent_id")
        if pid and (it.get("status") or "open") == "open":
            idx[pid] = idx.get(pid, 0) + 1
    return idx


def open_children_of(item: dict) -> int:
    """How many open subtasks hang under this row. db.read_items counts it over the whole
    table; a row built elsewhere carries 0 unless the caller filled it in."""
    try:
        return int(item.get("open_children") or 0)
    except (TypeError, ValueError):
        return 0


def completion_hold(item: dict, lane: str = None) -> str:
    """WHY an ordinary tick may not close this row, or "" when it may.

    One word per reason, for a surface to name it and for the server-side guard
    (db.update_item) to agree with: 'waiting' (someone else owns the next move),
    'reference' (a record has a state, not an ending), 'children' (open subtasks are still
    filed under it), 'settled'/'done' (already closed). A parent with open subtasks is an
    ordinary actionable row in every other respect — same lane, same counts — it simply
    cannot be finished while work is still hanging under it (2026-09-22).
    """
    lane = lane or lane_of(item)
    if lane == LANE_WAITING:
        return "waiting"
    if lane == LANE_REFERENCE:
        return "reference"
    if lane == LANE_SETTLED:
        return "settled"
    if lane == LANE_DONE:
        return "done"
    if open_children_of(item) > 0:
        return "children"
    return ""


def decorate(item: dict, *, renames=None) -> dict:
    """Attach the shared interpretation. Additive only — no stored field is altered."""
    lane = lane_of(item)
    hold = completion_hold(item, lane)
    return {
        **item,
        "lane": lane,
        "carried_over": carried_over(item),
        "needs_decision": needs_decision(item),
        "lane_label": LANE_LABELS[lane],
        "area": area_of(item, renames=renames),
        "shelves": shelves_of(item),
        "actionable": lane in ACTIONABLE,
        # A checkbox may be offered only when the lane allows it AND nothing open is
        # filed under the row. `completion_hold` says which of those it was.
        "completable": lane in COMPLETABLE and not hold,
        "completion_hold": hold,
        "open_children": open_children_of(item),
    }


def with_children(items: list) -> list:
    """The same rows, each carrying `open_children` — computed from this list when the
    store did not already fill it in, so every caller decorates from the same count."""
    rows = list(items or [])
    if any("open_children" not in r for r in rows):
        idx = open_children_index(rows)
        rows = [r if "open_children" in r else {**r, "open_children": idx.get(r.get("id"), 0)}
                for r in rows]
    return rows


def summarise(items: list, *, renames=None) -> dict:
    """Counts per lane plus the totals a view needs to prove nothing is hidden."""
    rows = [decorate(i, renames=renames) for i in with_children(items)]
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


def due_today_sections(items: list, today: str, suggest: int = 3, *, renames=None) -> dict:
    """The compact groups both Today surfaces show. Pure — it writes nothing and proposes
    nothing that is not already on the board.

    THREE suggestions, not five (Brady, 2026-09-09): "Show three suggestions on both phone
    and desktop with a Show more option." `suggested_total` carries the real size so a
    surface can offer the rest without a second request, and so the count is never hidden.
    """
    rows = [decorate(i, renames=renames) for i in with_children(items)]
    live = [r for r in rows if r["lane"] != LANE_DONE]
    deadlines = [r for r in live if r["lane"] in (LANE_OVERDUE, LANE_TODAY)]
    picked = [r for r in live if chosen_today(r, today)
              and r not in deadlines and r["actionable"]]
    taken = {r["id"] for r in deadlines} | {r["id"] for r in picked}
    # A suggestion is real work Brady could reasonably finish: actionable, not already
    # here, not waiting on anyone, not a record. Oldest first — the things quietly rotting.
    #
    # UNREVIEWED WORK IS NOT A SUGGESTION (Codex, 2026-09-09). Carried-over rows were landing
    # here, so a row the old board had been calling an undecided question was offered as
    # ordinary ready work — the label said "not yet reviewed" while the surface treated it as
    # accepted. They get their own group, named for what it is: something to look at, not
    # something to pick up.
    pool = [r for r in live
            if r["actionable"] and r["id"] not in taken and r["lane"] != LANE_UPCOMING
            and not r["carried_over"] and not r["needs_decision"]]
    pool.sort(key=lambda r: (r.get("ts") or ""))
    review = [r for r in live if r["carried_over"] and r["id"] not in taken]
    review.sort(key=lambda r: (r.get("ts") or ""))
    open_q = [r for r in live if r["needs_decision"] and r["id"] not in taken]
    open_q.sort(key=lambda r: (r.get("ts") or ""))
    return {
        "deadlines": sorted(deadlines, key=lambda r: (r.get("due_days") is None,
                                                      r.get("due_days", 0))),
        "chosen": picked,
        "suggested": pool[:suggest],
        "suggested_total": len(pool),
        # Never presented as ready work on any surface.
        "review": review[:suggest],
        "review_total": len(review),
        # Questions he marked himself, which are his to answer rather than to "do".
        "decisions": open_q[:suggest],
        "decisions_total": len(open_q),
        "waiting": [r for r in live if r["lane"] == LANE_WAITING],
    }
