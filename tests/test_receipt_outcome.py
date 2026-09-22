"""The update receipt: what it may claim, and what it may never claim.

WHY THIS FILE EXISTS (2026-09-21). In the live test three typed turns each changed exactly
the row Brady meant, and each reply ended "Outcome unconfirmed; check before retrying" — one
of them said confirmed and unconfirmed in the same breath. Nothing in the chain was lying on
purpose: `_do_update_item` returned prose, `ops.classify` can only call prose REPORTED, and
REPORTED means "claimed, nothing verified". The receipt had no evidence behind it because
nothing had read the saved row.

So these tests do not check that the happy path says something nice. They check the four ways
it must REFUSE to say it — a refusal from the store, an ambiguous `match`, a read-back that
did not come back, and a write that changed nothing — and they check that the one path which
IS allowed to say COMPLETED only gets there from a fresh snapshot of the row it names.

No database: `db.get_item` and `db.update_item` are stubbed so the REAL diff in
`db.update_item_verified` and the REAL wording in `tools._do_update_item` are what runs.
Behaviour against actual PostgreSQL lives in tests/receipt_repair_check.py.
"""
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ace2'))
from backend import chat, db, ops, tools            # noqa: E402


def row(**kw):
    """One stored board row, in the shape db.get_item returns it."""
    base = dict(id='row1', ts=None, kind='todo', text='call the excavator about the driveway',
                status='open', tags=['Business'], due=None, done_ts=None, parent_id=None,
                superseded_by=None, entry='action', state=None, waiting_on=None,
                closed_by=None, bucket='Side Work', next_step=None, followup=None,
                chosen_on=None, updated_at=None, reviewed_at=None)
    base.update(kw)
    base['area'] = base['bucket']
    base['category'] = next((t for t in (base['tags'] or []) if t in db.CATEGORIES), None)
    base['reviewed'] = bool(base['reviewed_at'])
    return base


@contextmanager
def store(before, after, write=(True, 'saved'), read_back=True, candidates=()):
    """A board of exactly one row, read twice: once before the write and once after.

    `read_back=False` is the case where the write is accepted and the row then cannot be
    read — the one that must never reach COMPLETED and must never be reported as a failure
    either, because the change may well be sitting in the table.
    """
    reads = iter([before, after if read_back else None])
    with patch.object(db, 'enabled', lambda: True), \
         patch.object(db, 'get_item', lambda i: next(reads, None)), \
         patch.object(db, 'update_item', lambda *a, **k: write), \
         patch.object(db, 'find_items', lambda *a, **k: list(candidates)):
        yield


class TheVerbNamesTheTransition(unittest.TestCase):
    """status='open' on an already-open row PRESERVES it. The old verb table read the
    argument, so every text edit answered "◆ Reopened" — a row described as raised from the
    dead when all that happened was a rename."""

    def test_the_transition_table(self):
        for before, after, verb in (('open', 'open', 'Updated'),
                                    ('open', 'done', 'Completed'),
                                    ('done', 'open', 'Reopened'),
                                    ('dropped', 'open', 'Reopened'),
                                    ('open', 'dropped', 'Archived'),
                                    ('done', 'dropped', 'Archived'),
                                    ('done', 'done', 'Updated')):
            with self.subTest(transition=f'{before}->{after}'):
                self.assertEqual(tools._update_verb(before, after), verb)

    def test_a_text_edit_on_an_open_row_says_updated_not_reopened(self):
        with store(row(), row(text='call the excavator about the culvert')):
            out = tools._do_update_item(id='row1', status='open',
                                        text='call the excavator about the culvert')
        self.assertEqual(out.state, ops.COMPLETED)
        self.assertIn('Updated', out.text)
        self.assertNotIn('Reopened', out.text)

    def test_completion_and_reopening_are_named_from_the_saved_row(self):
        with store(row(), row(status='done')):
            done = tools._do_update_item(id='row1', status='done')
        self.assertEqual(done.state, ops.COMPLETED)
        self.assertIn('Completed', done.text)
        with store(row(status='done'), row(status='open')):
            back = tools._do_update_item(id='row1', status='open')
        self.assertEqual(back.state, ops.COMPLETED)
        self.assertIn('Reopened', back.text)


class TheReceiptNamesOnlyWhatMoved(unittest.TestCase):
    def test_an_unchanged_field_is_omitted_and_listed_as_unchanged(self):
        with store(row(state='waiting', waiting_on='Tony'),
                   row(state='waiting', waiting_on='Tony', text='a new wording')):
            ok, detail = db.update_item_verified('row1', text='a new wording')
        self.assertTrue(ok)
        self.assertEqual(list(detail['changed']), ['text'])
        self.assertEqual(detail['changed']['text']['to'], 'a new wording')
        for field in ('state', 'waiting_on', 'status', 'due', 'next_step', 'followup', 'area'):
            self.assertIn(field, detail['unchanged'])

    def test_the_sentence_lists_the_changed_fields_and_no_others(self):
        with store(row(), row(text='new words', due='2026-10-01', next_step='call first')):
            out = tools._do_update_item(id='row1', text='new words', due='2026-10-01',
                                        next_step='call first')
        self.assertEqual(out.state, ops.COMPLETED)
        self.assertIn('(text, due, next_step)', out.text)
        self.assertNotIn('waiting_on', out.text)
        self.assertNotIn('state', out.text)

    def test_status_is_spoken_as_the_verb_and_not_again_as_a_field(self):
        with store(row(), row(status='done', done_ts='2026-09-21T10:00:00')):
            out = tools._do_update_item(id='row1', status='done')
        self.assertIn('Completed', out.text)
        self.assertNotIn('status', out.text)
        self.assertIn('status', out.detail['changed'])

    def test_bookkeeping_columns_are_not_reported_as_changes(self):
        """done_ts / closed_by / updated_at are stamped by the store, not asked for. Naming
        them would put words in Brady's mouth about fields he never touched."""
        with store(row(), row(status='done', done_ts='2026-09-21T10:00:00',
                              closed_by='ace', updated_at='2026-09-21T10:00:00')):
            ok, detail = db.update_item_verified('row1', status='done', closed_by='ace')
        self.assertTrue(ok)
        self.assertEqual(list(detail['changed']), ['status'])

    def test_the_receipt_names_the_row_it_actually_wrote(self):
        with store(row(id='abc123'), row(id='abc123', text='new words')):
            out = tools._do_update_item(id='abc123', text='new words')
        self.assertEqual(out.record_id, 'abc123')
        self.assertIn('[abc123]', out.text)


class NoSuccessWithoutEvidence(unittest.TestCase):
    """The four negatives, each asserted separately. None of them may be COMPLETED."""

    def test_a_refusal_is_not_a_success_and_keeps_its_exact_words(self):
        refusal = ("NOT COMPLETED — this is waiting on Tony, who owns the next move. "
                   "Nothing on Brady's side finishes it; it finishes when they act. "
                   "Only pass force_close if Brady says to close it anyway. Do NOT "
                   "tell him it is done.")
        with store(row(state='waiting', waiting_on='Tony'),
                   row(state='waiting', waiting_on='Tony'), write=(False, refusal)):
            out = tools._do_update_item(id='row1', status='done')
        self.assertNotEqual(out.state, ops.COMPLETED)
        self.assertEqual(out.state, ops.FAILED_BEFORE_DISPATCH)
        self.assertIn(refusal, out.text)

    def test_an_ambiguous_match_is_not_a_success_and_keeps_its_candidates(self):
        cands = [{'id': 'aaa111', 'text': 'call Damon about the website'},
                 {'id': 'bbb222', 'text': 'call Ken about the website'}]
        with store(row(), row(), candidates=cands):
            out = tools._do_update_item(match='call about the website', status='done')
        self.assertNotEqual(out.state, ops.COMPLETED)
        self.assertIn('AMBIGUOUS', out.text)
        self.assertIn('aaa111', out.text)
        self.assertIn('bbb222', out.text)

    def test_a_read_back_failure_is_never_completed_and_never_a_clean_failure(self):
        with store(row(), row(text='new words'), read_back=False):
            out = tools._do_update_item(id='row1', text='new words')
        self.assertNotEqual(out.state, ops.COMPLETED)
        self.assertEqual(out.state, ops.REPORTED)
        self.assertIn('not established', out.text)

    def test_a_write_that_changed_nothing_is_not_reported_as_a_change(self):
        same = row()
        with store(same, row()):
            out = tools._do_update_item(id='row1', text=same['text'])
        self.assertEqual(out.state, ops.COMPLETED)
        self.assertIn('Already satisfied', out.text)
        self.assertFalse(out.detail['changed'])

    def test_a_missing_row_is_not_a_success(self):
        with store(None, None):
            out = tools._do_update_item(id='does-not-exist', status='done')
        self.assertNotEqual(out.state, ops.COMPLETED)
        self.assertIn('no item does-not-exist', out.text)

    def test_the_store_is_never_asked_to_write_when_the_target_is_unknown(self):
        wrote = []
        cands = [{'id': 'aaa111', 'text': 'one thing'}, {'id': 'bbb222', 'text': 'another'}]
        with patch.object(db, 'enabled', lambda: True), \
             patch.object(db, 'find_items', lambda *a, **k: cands), \
             patch.object(db, 'update_item', lambda *a, **k: wrote.append(a) or (True, 'x')):
            out = tools._do_update_item(match='thing', status='done')
        self.assertEqual(wrote, [], 'an unresolved target must not reach the store')
        self.assertNotEqual(out.state, ops.COMPLETED)

    def test_the_offline_store_claims_nothing_it_cannot_read_back(self):
        """The Drive fallback is a read-modify-write of one JSON blob; there is no saved row
        to re-read, so REPORTED is the ceiling."""
        with patch.object(db, 'enabled', lambda: False), \
             patch.object(tools.daybank, 'update_item', lambda *a, **k: (True, 'saved text')):
            out = tools._do_update_item(id='row1', text='saved text')
        self.assertEqual(out.state, ops.REPORTED)


class TheOutcomeIsStructuredNotProse(unittest.TestCase):
    """ops.classify can only reach COMPLETED from an Outcome. This is the join that was
    missing: the executor must hand the classifier something to classify."""

    def test_the_executor_returns_an_outcome(self):
        with store(row(), row(text='new words')):
            out = tools._do_update_item(id='row1', text='new words')
        self.assertIsInstance(out, ops.Outcome)
        state, text, record_id = ops.classify(out)
        self.assertEqual(state, ops.COMPLETED)
        self.assertEqual(record_id, 'row1')
        self.assertIn('Updated', text)

    def test_a_verified_update_reaches_the_reply_as_a_confirmed_result(self):
        with store(row(), row(text='new words')):
            out = tools._do_update_item(id='row1', text='new words')
        state = chat.classify_result('update_item', out.text, outcome=out.state)
        self.assertEqual(state, chat.OP_DONE)
        reply = chat.guarded_reply(
            'I updated that on your board.',
            [{'tool': 'update_item', 'state': state, 'text': out.text}])
        self.assertNotIn('unconfirmed', reply.lower())
        self.assertIn('updated', reply.lower())

    def test_an_unverified_update_still_warns(self):
        """The warning is not being removed — it is being made true. A read-back failure must
        still reach Brady as an unconfirmed outcome."""
        with store(row(), row(text='new words'), read_back=False):
            out = tools._do_update_item(id='row1', text='new words')
        state = chat.classify_result('update_item', out.text, outcome=out.state)
        self.assertEqual(state, chat.OP_UNKNOWN)
        reply = chat.guarded_reply(
            'I updated that on your board.',
            [{'tool': 'update_item', 'state': state, 'text': out.text}])
        self.assertIn('unconfirmed', reply.lower())


class TheWiringForTheEntityLookup(unittest.TestCase):
    """Slice 3's read-only tool. A read: it must never join the write journal, because a
    journalled identity would answer the second identical question from a stored receipt."""

    def test_it_is_declared_as_a_read_and_not_as_a_write(self):
        self.assertIn('lookup_entity', tools.NATIVE_READS)
        self.assertNotIn('lookup_entity', ops.JOURNALLED)
        self.assertNotIn('lookup_entity', tools.NATIVE_MUTATIONS)
        self.assertIn('lookup_entity', tools.TOOL_LABELS)
        self.assertIn('lookup_entity', tools._DISPATCH)
        self.assertIn('lookup_entity', {t['name'] for t in tools.TOOLS})

    def test_a_missing_renderer_is_answered_honestly_rather_than_crashing_the_turn(self):
        out = tools.execute('lookup_entity', {'name_or_id': 'Jordan Rivera'})
        self.assertIsInstance(out, str)
        self.assertNotEqual(out.strip(), '')

    def test_it_asks_for_a_subject_instead_of_guessing_one(self):
        self.assertIn('name_or_id', tools.execute('lookup_entity', {'name_or_id': '  '}))


if __name__ == '__main__':
    unittest.main()
