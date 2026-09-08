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
    """What the drafts above ACTUALLY resulted in.

    A draft is an intention; the operation journal is the record. Without this, a resumed
    session re-proposes work that already landed (how the Ken call reached the calendar
    four times), or re-promises work whose outcome nobody knows. Completed rows say
    'already done, do not redo'; unknown rows say 'check before touching'."""
    try:
        rows = ops.recent_writes()
    except Exception:
        return ''
    if not rows:
        return ''
    done = [r for r in rows if r['state'] == 'completed']
    open_q = [r for r in rows if r['state'] in ('dispatched', 'unknown')]
    out = ['\n\nACTIONS ALREADY CARRIED OUT (from the write journal, not the draft). '
           'These are DONE — resume only what is missing, and never recreate these:']
    for r in done[:20]:
        out.append('  \u2713 ' + r['tool'] + ': ' + (r['receipt'] or '')[:160])
    if not done:
        out.append('  (none yet \u2014 nothing in this plan has actually been written)')
    if open_q:
        out.append('UNRESOLVED \u2014 outcome not recorded. Do NOT retry these blindly; '
                   'check the calendar or board first and tell Brady they need confirming:')
        for r in open_q[:10]:
            out.append('  ? ' + r['tool'] + ': ' + str(r['args'])[:140])
    return '\n'.join(out)
