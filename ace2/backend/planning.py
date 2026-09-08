"""A persistent planning notebook, independent of voice connection lifetime."""
import re
from datetime import datetime, timezone
from . import ops, review_store
START = re.compile(r'\b(?:plan(?:ning)?\s+(?:my|the|our|this|next)\s+week|week(?:ly)?\s+plan|lay\s+out\s+(?:my|the)\s+week)\b', re.I)
DAYS = re.compile(r'\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', re.I)


def is_draft(text):
    return len(set(x.lower() for x in DAYS.findall(text))) >= 3 and len(text) > 350


def capture(role, text):
    entries = review_store.read_plan()
    recent = bool(entries) and (datetime.now(timezone.utc) - datetime.fromisoformat(entries[-1]['saved_at'])).total_seconds() < 5400
    if START.search(text) or is_draft(text) or recent:
        # Keep original updates, not a lossy AI summary; recent rows are context,
        # all rows remain stored for recovery. No calendar actions are implied.
        review_store.append_plan(role, text)


def context():
    entries = review_store.read_plan()
    if not entries:
        return ''
    drafts = [i for i,e in enumerate(entries) if e['role']=='assistant' and is_draft(e['text'])]
    start = drafts[-1] if drafts else max(0, len(entries)-12)
    selected = entries[start:]
    # Bound context while preserving the full latest draft and newest corrections.
    if len(selected) > 17:
        selected = selected[:1] + selected[-16:]
    rendered = '\n'.join(e['role'] + ': ' + e['text'] for e in selected)
    if len(rendered) > 18000:
        rendered = rendered[:10000] + '\n[Middle omitted from model context; consult Saved week. Do not assume missing details.]\n' + rendered[-8000:]
    return ('\nSAVED PLANNING NOTEBOOK (draft only; NOT proof of calendar creation). '
            'Resume this work. Later user corrections override older draft text. '
            'Flag conflicting dates instead of booking both. Do not repeat answered intake '
            'questions. Only describe calendar entries as created after tool receipts.\n' +
            rendered + settled_actions())


def settled_actions() -> str:
    """What the drafts above ACTUALLY resulted in, split by how well it is known.

    The first version of this listed everything that had not raised an exception under
    "ACTIONS ALREADY CARRIED OUT — these are DONE", which meant a capture that was
    deliberately NOT saved appeared as finished work. Ace's own record then said the job
    was done when it was not — the exact failure this system exists to remove. Only a
    verified COMPLETED outcome may be described as done; a legacy executor's prose is
    reported-but-unverified, and anything unresolved is called out as needing a check.
    """
    try:
        rows = ops.recent_writes()
    except Exception:
        return ''
    if not rows:
        return ''
    done = [r for r in rows if r['state'] == ops.COMPLETED]
    claimed = [r for r in rows if r['state'] == ops.REPORTED]
    review = [r for r in rows if r['state'] == ops.NEEDS_REVIEW]
    open_q = [r for r in rows if r['state'] in ('dispatched', ops.UNKNOWN, ops.UNAVAILABLE)]

    out = ['\n\nWHAT ACTUALLY HAPPENED (from the write journal, not from the draft):']
    if done:
        out.append('CONFIRMED DONE \u2014 do not recreate these:')
        for r in done[:20]:
            ident = f" [{r['external_id']}]" if r.get('external_id') else ''
            out.append('  \u2713 ' + r['tool'] + ident + ': ' + (r['receipt'] or '')[:150])
    if claimed:
        out.append('REPORTED BUT NOT VERIFIED \u2014 Ace said these ran; no receipt confirms '
                   'it. Say so if asked, and check rather than assert:')
        for r in claimed[:12]:
            out.append('  ~ ' + r['tool'] + ': ' + (r['receipt'] or '')[:150])
    if review:
        out.append('NOT SAVED \u2014 these were deliberately held back for a decision and are '
                   'still outstanding. They are NOT done:')
        for r in review[:10]:
            out.append('  \u2717 ' + r['tool'] + ': ' + (r['receipt'] or '')[:150])
    if open_q:
        out.append('UNRESOLVED \u2014 outcome not recorded. Do NOT retry these blindly; check '
                   'the calendar or board first and tell Brady they need confirming:')
        for r in open_q[:10]:
            out.append('  ? ' + r['tool'] + ': ' + str(r['args'])[:130])
    if len(out) == 1:
        return ''
    return '\n'.join(out)
