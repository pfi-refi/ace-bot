"""A persistent planning notebook, independent of voice connection lifetime."""
import re
from datetime import datetime, timezone
from . import review_store
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
            rendered)
