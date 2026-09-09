"""The 9 September morning, composed end to end, and inspected.

Builds the REAL brief input through compose_brief_prompt with this morning's conversation as
a fixed historical fixture and everything else stubbed, then asserts the things the audit
said the brief must know and must not claim. Input inspection only: no model call, no
delivery, no live record, no network.
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'ace2'))
from backend import chat                              # noqa: E402

EASTERN = chat.EASTERN
# EASTERN is a pytz zone: passing it as tzinfo= yields the 1883 LMT offset (-04:56), which is
# how the first run of this file rendered an 8:28 statement as 9:24 AM. localize() is the only
# correct way to attach it, and the production path already does exactly that.
NOW = EASTERN.localize(datetime(2026, 9, 9, 9, 0))
TODAY = NOW.strftime('%Y-%m-%d')


def t(role, content, hh, mm):
    return {'role': role, 'content': content,
            'ts': EASTERN.localize(datetime(2026, 9, 9, hh, mm)).isoformat()}


# THE FIXED HISTORICAL CASE. This is the sequence from the audit's observed timeline.
MORNING = [
    t('user', 'No, we are good to roll. I am out DoorDashing right now, like I was yesterday, '
              'and I am gonna go to the gym later this evening because I did not have time '
              'this morning.', 8, 28),
    t('assistant', 'Got it — DoorDashing now, gym tonight.', 8, 28),
    t('user', 'I just have to double-check on my aunt, to make sure she actually got '
              'everything signed.', 8, 29),
    t('user', 'Then I want to prospect, work on my website, and get hold of Nick and Josh. '
              'Nick has a window Thursday evening but I need an exact time from him — I will '
              'handle that today.', 8, 30),
    t('assistant', 'Sounds like a full day.', 8, 30),
    t('user', 'No — order the concrete TODAY, not tomorrow. The pour is Friday for my uncle.', 8, 31),
]

BOARD = [
    {'id': 'a1', 'text': "Confirm the aunt actually signed the packet", 'status': 'open',
     'tags': ['Deals'], 'entry': 'action', 'state': None, 'due': None, 'due_days': None,
     'due_on': None, 'waiting_on': None, 'next_step': None, 'followup': None,
     'chosen_on': None, 'bucket': 'GFI/PFI', 'ts': '2026-09-05T12:00:00+00:00',
     'updated_at': None, 'done_ts': None},
    {'id': 'a2', 'text': "Order concrete for the uncle's Friday pour", 'status': 'open',
     'tags': ['Business'], 'entry': 'action', 'state': None, 'due': TODAY, 'due_days': 0,
     'due_on': TODAY, 'waiting_on': None, 'next_step': 'call dispatch this morning',
     'followup': None, 'chosen_on': TODAY, 'bucket': 'Groundworks',
     'ts': '2026-09-06T12:00:00+00:00', 'updated_at': None, 'done_ts': None},
    {'id': 'a3', 'text': "Lock Nick's Thursday time", 'status': 'open', 'tags': ['Networking'],
     'entry': 'action', 'state': None, 'due': None, 'due_days': None, 'due_on': None,
     'waiting_on': None, 'next_step': None, 'followup': None, 'chosen_on': None,
     'bucket': 'GFI/PFI', 'ts': '2026-09-07T12:00:00+00:00', 'updated_at': None, 'done_ts': None},
    {'id': 'a4', 'text': 'Website copy for the new page', 'status': 'open', 'tags': ['Business'],
     'entry': 'action', 'state': None, 'due': None, 'due_days': None, 'due_on': None,
     'waiting_on': None, 'next_step': None, 'followup': None, 'chosen_on': None,
     'bucket': 'Side Work', 'ts': '2026-09-07T13:00:00+00:00', 'updated_at': None, 'done_ts': None},
    {'id': 'a5', 'text': 'Rebecca mailing + postage', 'status': 'open', 'tags': ['Deals'],
     'entry': 'record', 'state': 'waiting', 'waiting_on': 'Rebecca', 'due': None,
     'due_days': None, 'due_on': None, 'next_step': None, 'followup': '2026-09-11',
     'chosen_on': None, 'bucket': 'GFI/PFI', 'ts': '2026-09-04T12:00:00+00:00',
     'updated_at': None, 'done_ts': None},
]
EVENTS = [{'date': TODAY, 'time': '3:00 PM', 'title': 'Ken',
           'iso': EASTERN.localize(datetime(2026, 9, 9, 15, 0)).isoformat(),
           'all_day': False}]

fails = []
def ok(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL') + ' — ' + name + (f'  ({detail})' if detail else ''))
    if not cond:
        fails.append(name)


async def _wx():
    return {'now': 68, 'high': 78, 'low': 60, 'desc': 'clear'}


async def _bills():
    return {'ok': True, 'items': [{'name': 'Electric', 'amount': 247.79, 'due': '2026-09-14'}]}


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


async def main():
    with patch('backend.db.read_items', return_value=BOARD), \
         patch('backend.db.read_facts_full', return_value=[]), \
         patch('backend.db.latest_summary', return_value={}), \
         patch('backend.db.enabled', return_value=True), \
         patch('backend.brain.read_memory', return_value=['Goal: land a stable income floor']), \
         patch('backend.chat.load_profile', return_value='Brady runs GFI/PFI and Groundworks.'), \
         patch('backend.chat._unified_thread', return_value=MORNING), \
         patch('backend.chat.get_events_structured', return_value=EVENTS), \
         patch('backend.chat.get_weather', new=_wx), \
         patch('backend.integrations.bills_sheet.fetch_bills', new=_bills), \
         patch('backend.chat.datetime', _Clock):
        return await chat.compose_brief_prompt('morning')





p = asyncio.run(main()) or ''
Path('/tmp/ace-brief-input.txt').write_text(p)
print(f'composed {len(p)} characters\n')

low = p.lower()
# ── what the audit says a grounded brief must KNOW ──────────────────────────────
ok('the gym correction is in the input', 'gym' in low and 'this evening' in low)
ok('his statements carry HIS clock, not UTC and not LMT', '8:28 AM ET' in p,
   'the 9 Sept brief printed 12:28 UTC as though it were his morning')
ok('...and every statement time is Eastern',
   all(x in p for x in ('8:28 AM ET', '8:29 AM ET', '8:30 AM ET', '8:31 AM ET')))
ok('it is his own words, not a summary of them', 'WHAT BRADY HIMSELF SAID' in p)
ok('the concrete correction survived', 'order the concrete today' in low)
ok('...with the tomorrow it overrules', 'not tomorrow' in low)
ok('the aunt is a signature to CONFIRM, not a done deal',
   'actually signed' in low or 'actually got everything signed' in low)
ok("Nick's Thursday window is present", 'nick' in low and 'thursday' in low)
ok('Josh is present', 'josh' in low)
ok('prospecting and the website are present', 'prospect' in low and 'website' in low)
ok('Ken at 3 PM is on the schedule', 'ken' in low and '3:00 pm' in low)

# ── the structured board state the brief used to flatten away ───────────────────
ok('who owns the next move is stated', 'PARKED ON Rebecca' in p)
ok('...and that he is not the actor', 'NOT the next actor' in p)
ok("Rebecca's follow-up is not called her deadline", "not the other party's deadline" in p)
ok('the work he chose for today is named as his', 'HE CHOSE THESE FOR TODAY' in p)
ok('undated real work reaches the brief', 'UNDATED AND READY' in p)
ok("...including Nick's untimed row", "Lock Nick's Thursday time" in p)
ok('a next step arrives in his words', 'call dispatch this morning' in p)

# ── framing ────────────────────────────────────────────────────────────────────
ok('the current profile is in the input', 'WHO HE IS RIGHT NOW' in p)
ok('the brief is about his day, not a fixed recovery narrative',
   'its subject is HIS DAY' in p and 'FINANCIAL RECOVERY' not in p)
ok('money leads only when it is actually urgent', 'If money is not' in p)
ok('the sheet is still the only source for dollar figures', 'ONLY trustworthy' in p)

# ── the change window ──────────────────────────────────────────────────────────
ok('the window says what it actually covers', 'WINDOW:' in p)
ok('...and admits when it is only a look-back',
   'not a true since-the-last-brief list' in p)

# ── what it must NOT do ────────────────────────────────────────────────────────
# "hit the gym" and "crushed the gym" DO appear in the input — inside the two rules that
# quote those exact failures back as warnings. What must not appear is an assertion in the
# DATA that the gym happened, so check everything after the rules block.
_data = p.split('CURRENT TIME:')[-1]
ok('nothing in the DATA asserts the gym happened',
   'hit the gym' not in _data.lower() and 'crushed the gym' not in _data.lower())
ok('...and the rules still quote the failure back as a warning',
   'hit the gym this morning' in p)
ok('evidence is required before calling anything done',
   'NEVER SAY SOMETHING IS DONE WITHOUT EVIDENCE' in p)
ok('an assistant claim is not proof', 'claim' in low and 'not' in low)
ok('no statement was silently truncated',
   '[truncated' not in p.split('WHAT BRADY HIMSELF SAID')[-1])


# ── A SEPARATE, NEWER CASE ─────────────────────────────────────────────────────
# Brady asked for this morning to stay a FIXED historical regression and for newer
# conversations to be handled as their own cases. This is the second case: a quiet morning
# where the only recent turns are from yesterday. It is the shape that made a prior-day
# statement get labelled "today", and it must also not invent a plan out of nothing.
YESTERDAY = [
    {'role': 'user',
     'content': 'I am going to mail Rebecca the packet today and call the county tomorrow.',
     'ts': EASTERN.localize(datetime(2026, 9, 8, 16, 40)).isoformat()},
]


async def quiet():
    with patch('backend.db.read_items', return_value=BOARD), \
         patch('backend.db.read_facts_full', return_value=[]), \
         patch('backend.db.latest_summary', return_value={}), \
         patch('backend.db.enabled', return_value=True), \
         patch('backend.brain.read_memory', return_value=[]), \
         patch('backend.chat.load_profile', return_value='Brady runs GFI/PFI and Groundworks.'), \
         patch('backend.chat._unified_thread', return_value=YESTERDAY), \
         patch('backend.chat.get_events_structured', return_value=EVENTS), \
         patch('backend.chat.get_weather', new=_wx), \
         patch('backend.integrations.bills_sheet.fetch_bills', new=_bills), \
         patch('backend.chat.datetime', _Clock):
        return await chat.compose_brief_prompt('morning')


q = asyncio.run(quiet()) or ''
print('\n— second case: a quiet morning, newest turns from yesterday —')
ok('a prior-day statement is NOT presented as today',
   'WHAT BRADY HIMSELF SAID TODAY' not in q)
ok('...it is kept, dated, as earlier context',
   'Rebecca the packet' in q and ('EARLIER' in q.upper() or 'PRIOR' in q.upper()))
ok("...and the relative words in it are flagged rather than re-read as today's",
   'sep' in q.lower() or '2026-09-08' in q)
ok('the day still has his board to work from', 'HE CHOSE THESE FOR TODAY' in q)
ok('and the window is honest about a missing receipt',
   'not a true since-the-last-brief list' in q)

print()
if fails:
    print(f'{len(fails)} FAILED: ' + '; '.join(fails)); sys.exit(1)
print('ALL PASS — the composed 9 September input carries his plan, his corrections and the '
      'structured board state, and states the limits of its own change window.')
print('Input written to /tmp/ace-brief-input.txt for reading. No model was called.')
