"""Exercise actual stream_turn without provider, network, or persistent writes."""
import contextlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ace2'))
from backend import chat, planning


class Stream:
    def __init__(self, text, blocks=()):
        self.text, self.blocks = text, list(blocks)
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    def __aiter__(self):
        async def chunks():
            for token in self.text.splitlines(keepends=True):
                yield types.SimpleNamespace(type='content_block_delta', delta=types.SimpleNamespace(type='text_delta', text=token))
        return chunks()
    async def get_final_message(self):
        return types.SimpleNamespace(content=self.blocks, stop_reason='tool_use' if self.blocks else 'end_turn', usage=None)


class Claims(unittest.IsolatedAsyncioTestCase):
    async def run_turn(self, request, scripts, outcome=None):
        events, history = [], []
        calls = list(scripts)
        client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **kw: calls.pop(0)))
        async def emit(kind, payload): events.append((kind, payload))
        async def dispatch(name, args, sink=None):
            if sink is not None: sink.update(state=(outcome or {}).get('state', ''), kind='mutation')
            return (outcome or {}).get('text', 'No result')
        with contextlib.ExitStack() as stack:
            for target, key, value in (
                (chat, '_anthropic', lambda: client),
                (chat, '_fast_context', AsyncMock(return_value='')),
                (chat, '_load_messages', AsyncMock(return_value=[])),
                (chat, 'build_system_prompt', lambda: 'test'),
                (chat, 'maybe_toggle_privacy', lambda text: None),
                (chat, '_ttl_ok', [False]),
                (chat, '_dispatch_write', dispatch),
                (chat.mcp_client, 'is_mcp_tool', lambda name: False),
                (chat.history, 'append', lambda role, text: history.append((role, text))),
                (planning, 'capture', lambda *args: None),
                (planning, 'context', lambda: ''),
            ): stack.enter_context(patch.object(target, key, value))
            reply = await chat.stream_turn(request, emit, fast=True)
        return reply, events, history

    async def test_zero_tool_claim_cannot_escape_to_voice_or_history(self):
        for claim in ('Done. I moved your gym to 6:30.', 'All set, I sent it.', 'I have updated the calendar.'):
            reply, events, history = await self.run_turn('Move my gym to 6:30', [Stream(claim)])
            self.assertIn("don't have a verified action result", reply)
            self.assertFalse(any(claim in str(payload) for _, payload in events))
            self.assertNotIn(('assistant', claim), history)

    async def test_approval_turn_guarded(self):
        for approval in ('Yes', 'yes please', 'go ahead and do that'):
            reply, _, _ = await self.run_turn(approval, [Stream('Done, deleted.')])
            self.assertIn('verified action result', reply)

    async def test_clarification_preserved(self):
        text = 'Which appointment do you mean?'
        reply, events, _ = await self.run_turn('Move my appointment', [Stream(text)])
        self.assertEqual(text, reply)
        self.assertIn(('delta', {'text': text}), events)

    async def test_advice_with_action_vocabulary_preserved(self):
        text = "Once created, a task should have a clear next step. You updated your priorities yesterday."
        reply, _, _ = await self.run_turn('Make suggestions about my task process', [Stream(text)])
        self.assertEqual(text, reply)

    async def test_honest_unavailable_explanation_preserved(self):
        text = "I cannot move that appointment here. I have not updated your calendar. Nothing was changed."
        reply, _, _ = await self.run_turn('Move my appointment', [Stream(text)])
        self.assertEqual(text, reply)

    def test_unknown_operation_uses_friendly_fallback(self):
        self.assertNotIn('mcp_secret_provider', chat.action_receipt_reply([{'state': chat.OP_UNKNOWN, 'tool': 'mcp_secret_provider'}]))

    async def test_normal_conversation_preserved(self):
        text = 'Good morning!\nHow are you?'
        reply, events, _ = await self.run_turn('Hello Ace', [Stream(text)])
        self.assertEqual(text, reply)
        self.assertEqual(2, sum(kind == 'delta' for kind, _ in events))

    async def test_real_dispatch_receipt_replaces_extra_claim(self):
        from backend import ops
        block = types.SimpleNamespace(type='tool_use', name='capture', input={'text': 'Test item'}, id='call1')
        reply, events, _ = await self.run_turn('Capture a test item', [Stream('Done, I sent an email.', [block]), Stream('I captured it and sent an email.')], {'state': ops.COMPLETED, 'text': 'Captured test item, record 123'})
        self.assertIn('Captured test item, record 123', reply)
        self.assertNotIn('sent an email', str(events))

    async def test_read_never_proves_write(self):
        block = types.SimpleNamespace(type='tool_use', name='get_calendar_range', input={}, id='read1')
        reply, _, _ = await self.run_turn('Update the calendar', [Stream('', [block]), Stream('Done.')], {'text': 'Found gym at 7 AM'})
        self.assertNotIn('Confirmed result', reply)

    def test_receipt_states_do_not_promote_pending_to_completed(self):
        for state in (chat.OP_QUEUED, chat.OP_REVIEW, chat.OP_UNKNOWN, chat.OP_FAILED):
            result = chat.action_receipt_reply([{'state': state, 'tool': 'create_folder', 'text': 'Misleading finished prose'}])
            self.assertNotIn('Misleading', result)
            self.assertNotIn('Confirmed result', result)
        self.assertEqual('', chat.action_receipt_reply([{'state': chat.OP_READ, 'text': 'Found calendar events'}]))

class ActionGuardScope(unittest.TestCase):
    """THE VERB IS NOT THE REQUEST (2026-09-11, from a live call).

    Brady said "I will not be reaching out to Armando, I've made my choice to go with Chris…
    after his BUILD for the Nigel I will be doing Chris part time" and the word "build" put
    the turn into action-guard mode. His whole reply came back as a receipt dump, and his
    follow-up "Why couldn't you update it?" got nothing.

    Measured against 120 real messages from his own history: the old matcher guarded 48 of
    them (40%), this one guards 5 (4%), and all five are genuine instructions."""

    def test_the_sentence_that_broke_it(self):
        self.assertFalse(chat.action_request(
            "I will not be reaching out to Armando I've made my choice to go with Chris "
            "after the groundwork's section of everything and getting that going and then "
            "after his build for the Nigel I will be doing Chris part time"))

    def test_brady_narrating_his_own_day_is_not_an_instruction(self):
        for said in ("I made another $51 today",
                     "I could probably go out and start making some DoorDash money",
                     "we're gonna lock everything in tomorrow",
                     "he is on board with everything",
                     "Armando is my upline and was my mentor at GFI",
                     "budget review will be tomorrow after we pay a few bills"):
            self.assertFalse(chat.action_request(said), said)

    def test_real_instructions_still_guard(self):
        for asked in ("send that email to Rebecca",
                      "add Ken to Wednesday at 10",
                      "please put the pour on Monday",
                      "can you delete the gym block",
                      "go ahead and send it",
                      "I need you to make me a document",
                      # the verb sits three words after "you to" — this one produced real
                      # calendar events and MUST be guarded
                      "what I want you to do is take that schedule and put it on my calendar"):
            self.assertTrue(chat.action_request(asked), asked)

    def test_a_bare_yes_is_still_an_approval(self):
        for said in ("yes", "yep", "go ahead", "ok", "do it", "sure"):
            self.assertTrue(chat.action_request(said), said)


class GuardedReplyKeepsTheConversation(unittest.TestCase):
    def test_the_receipt_is_appended_not_substituted(self):
        """`receipt or text` is what deleted Brady's answer."""
        out = chat.guarded_reply(
            "Got it — Armando is off the list, Chris is the play.",
            [{"tool": "update_item", "state": chat.OP_FAILED,
              "reason": "Could not update item: no open item matches 'Armando'"}])
        self.assertIn("Armando is off the list", out)          # his answer survived
        self.assertIn("Failed or refused", out)                # and the truth came with it
        self.assertIn("no open item matches", out)             # and it says WHY

    def test_a_false_success_claim_is_still_suppressed(self):
        out = chat.guarded_reply("I've updated the board for you.",
                                 [{"tool": "update_item", "state": chat.OP_FAILED}])
        self.assertNotIn("I've updated the board", out)
        self.assertIn("verified action result", out)

    def test_plain_conversation_with_no_operations_is_untouched(self):
        said = "That maps — Josh first, then Armando if you change your mind."
        self.assertEqual(chat.guarded_reply(said, []), said)


class FailureReasons(unittest.TestCase):
    def test_a_human_reason_survives(self):
        self.assertEqual(
            chat.user_safe_reason("⚠️ Could not update item: no open item matches 'Armando'"),
            "Could not update item: no open item matches 'Armando'")

    def test_model_directed_refusals_are_stripped(self):
        self.assertEqual(chat.user_safe_reason(
            "USE start_task INSTEAD. mcp_create_spreadsheet creates something "
            "without checking it."), "")

    def test_operation_prose_is_never_echoed_for_a_pending_state(self):
        """Codex's invariant, kept: only an explicit `reason` may surface."""
        for state in (chat.OP_QUEUED, chat.OP_REVIEW, chat.OP_UNKNOWN, chat.OP_FAILED):
            out = chat.action_receipt_reply(
                [{"state": state, "tool": "create_folder", "text": "Misleading finished prose"}])
            self.assertNotIn("Misleading", out)

    def test_but_an_explicit_reason_does_surface(self):
        out = chat.action_receipt_reply(
            [{"state": chat.OP_FAILED, "tool": "update_item",
              "reason": "Could not update item: no open item matches 'Armando'"}])
        self.assertIn("no open item matches", out)


class CodexFollowUp(unittest.TestCase):
    """The three findings from CODEX-REVIEW-CLAUDE-AFTER-VOICE-2026-09-11, each reproduced
    before it was fixed."""

    def test_direct_address_is_an_instruction(self):
        """"Ace, add Ken to Wednesday" is the most natural way to ask on a call, and it
        sailed past an anchor that only accepted punctuation before the verb."""
        for asked in ("Ace, add Ken to Wednesday at 10",
                      "Hey Ace, send that email",
                      "Ace delete the gym block",
                      "ok Ace, put the pour on Monday",
                      "Ace can you delete the gym block"):
            self.assertTrue(chat.action_request(asked), asked)

    def test_direct_address_did_not_re_open_the_verb_matcher(self):
        for said in ("I will not be reaching out to Armando after his build for the Nigel",
                     "Ace is doing great today",
                     "I made another $51 today"):
            self.assertFalse(chat.action_request(said), said)

    def test_a_verified_write_draws_no_unverified_warning(self):
        out = chat.guarded_reply(
            "I updated the board. Chris is next.",
            [{"state": chat.OP_DONE, "tool": "update_item", "text": "Updated record 123"}])
        self.assertNotIn("don't have a verified action result", out)
        self.assertIn("Chris is next", out)
        self.assertIn("Confirmed result", out)

    def test_a_verified_receipt_never_vouches_for_a_second_claim(self):
        """Codex's test caught this in my first fix: ONE confirmed capture was passing
        "and sent an email" through untouched — an action with no operation behind it. The
        claim is always dropped; only the warning is conditional."""
        out = chat.guarded_reply(
            "I captured it and sent an email.",
            [{"state": chat.OP_DONE, "tool": "capture", "text": "Captured test item, 123"}])
        self.assertNotIn("sent an email", out)
        self.assertIn("Captured test item, 123", out)

    def test_but_one_success_never_vouches_for_a_second_action(self):
        """A mix still suppresses — that hole is the reason this layer exists."""
        out = chat.guarded_reply(
            "I updated the board and sent Rebecca the packet. Chris is next.",
            [{"state": chat.OP_DONE, "tool": "update_item", "text": "Updated record 123"},
             {"state": chat.OP_FAILED, "tool": "send_email", "reason": "no recipient"}])
        self.assertNotIn("sent Rebecca", out)
        self.assertIn("verified action result", out)

    def test_unrelated_sentences_survive_the_suppression(self):
        """It replaced the WHOLE reply on any match, so "Chris is next." died with the claim."""
        out = chat.guarded_reply(
            "I sent it. Chris is next and Lincoln is at three.",
            [{"state": chat.OP_UNKNOWN, "tool": "send_email"}])
        self.assertIn("Chris is next", out)
        self.assertIn("Lincoln is at three", out)
        self.assertNotIn("I sent it", out)

    def test_a_dated_record_is_an_obligation_not_reference(self):
        """The board hid records by entry type, and classify gives a record with a due date
        lane='today' AND completable — so a permit fee due today vanished from Today and
        Everything at once. This asserts the server side of that; the browser suite covers
        the filter."""
        from backend import classify
        import datetime
        today = datetime.date.today().isoformat()
        rows = [classify.decorate({"id": "r1", "text": "Permit fee due to the county",
                                   "status": "open", "entry": "record", "due_days": 0,
                                   "due": today, "tags": ["Bills"],
                                   "ts": "2026-09-01T00:00:00"})]
        self.assertEqual(rows[0]["lane"], "today")
        self.assertTrue(rows[0]["completable"])
        self.assertIn("Permit fee due to the county",
                      [x["text"] for x in classify.due_today_sections(rows, today)["deadlines"]])


if __name__ == '__main__': unittest.main()
