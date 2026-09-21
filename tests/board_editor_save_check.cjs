// Execute the REAL editor SAVE handler (and the real 401 helpers it leans on) against a
// synthetic editor row, and assert on the REQUEST BODY it posts. Synthetic data only.
//
// What this pins down:
//   * the status control says what it means — WAITING posts 'waiting', READY posts the
//     explicit '' that CLEARS a stored state, SETTLED posts 'settled' on a record;
//   * a refusal leaves the editor open with the typed values and names the reason;
//   * a signed-out 401 ends the session and says so instead of 'RETRY SAVE';
//   * a row stored as 'active' renders — and saves — as READY, not as a fourth state.
// NOT covered here: cmdPaintBoard and the editor's `wait:` draft snapshot both live in
// cmdRender, outside every region this file executes, so they are stubbed rather than run.
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../ace2/app.js'), 'utf8');

// The shared 401 helpers, executed as shipped — a stub here would prove nothing about them.
const hStart = source.indexOf('  function boardJson(r) {');
const hEnd = source.indexOf('  function confirmBoardCompletion(it) {', hStart);
// The save handler itself.
const sStart = source.indexOf("    Array.prototype.forEach.call(v.querySelectorAll('.cmd-esave'), function(b){ b.onclick=function(){");
const sEnd = source.indexOf("    var f=v.querySelector('#cmd-add');", sStart);
assert.ok(hStart >= 0 && hEnd > hStart, 'boardJson/boardWhy helpers not found');
assert.ok(sStart >= 0 && sEnd > sStart, 'editor SAVE handler not found');
const region = source.slice(hStart, hEnd) + '\n' + source.slice(sStart, sEnd);

// The editor's status-select value, computed by the SHIPPED lines in cmdRow — the rest of
// cmdRow builds HTML, so only these two are executed here.
const vStart = source.indexOf("      var dState = (cmd.draft && cmd.draft.state != null)");
const vEnd = source.indexOf('      var dWait  =', vStart);
assert.ok(vStart >= 0 && vEnd > vStart, 'dState computation not found');
const dStateSrc = source.slice(vStart, vEnd);
const dStateOf = (it, draft) =>
  new Function('cmd', 'it', dStateSrc + '\nreturn dState;')({ draft: draft || null }, it);

function save(typed, reply) {
  const field = value => ({ value, style: {}, focus() {} });
  const nodes = {
    '.cmd-etext': field(typed.text), '.cmd-ecat': field(typed.cat || 'Admin'),
    '.cmd-eentry': field(typed.entry), '.cmd-ebucket': field(typed.bucket || 'Inbox'),
    '.cmd-estate': field(typed.state), '.cmd-ewait': field(typed.wait || ''),
    '.cmd-edue': field(typed.due || ''), '.cmd-efup': field(typed.fup || ''),
    '.cmd-enext': field(typed.next || '')
  };
  const row = { getAttribute: () => 'row-1', querySelector: s => nodes[s] || null,
                querySelectorAll: () => [] };
  const button = { textContent: 'SAVE', disabled: false, closest: () => row };
  const out = { nodes, button, posted: null, painted: 0, refetched: 0, login: 0 };
  const cmd = { editing: 'row-1', draft: { text: typed.text }, items: [], dueToday: null, today: '' };
  out.cmd = cmd;
  const ctx = {
    cmd, API: '', headers: () => ({}),
    v: { querySelectorAll: () => [button] },
    toLogin() { out.login++; },
    cmdFetch: () => { out.refetched++; return Promise.resolve(); },
    cmdPaintBoard() { out.painted++; },
    fetch: (url, opts) => {
      out.url = url; out.posted = JSON.parse(opts.body);
      return Promise.resolve({ status: reply.status || 200, ok: (reply.status || 200) < 400, json: async () => reply.body });
    }
  };
  vm.runInNewContext(region, ctx);
  button.onclick();
  // let the fetch chain settle
  return new Promise(r => setImmediate(() => setImmediate(() => setImmediate(() => r(out)))));
}

const receipt = { body: { ok: true, items: [], due_today: null, today: '2026-09-21' } };

(async () => {
  // 1. An ACTION parked on someone else posts the state AND the owner.
  let r = await save({ text: 'Chase the permit', entry: 'action', state: 'waiting', wait: '  Tony  ' },
                     receipt);
  assert.equal(r.posted.state, 'waiting', 'an action must be allowed to be WAITING');
  assert.equal(r.posted.waiting_on, 'Tony', 'the typed owner rides along with the waiting state');
  assert.equal(r.posted.entry, 'action');
  assert.equal(r.cmd.editing, null, 'a saved row closes the editor');
  assert.equal(r.cmd.draft, null);
  assert.equal(r.refetched, 1);
  assert.equal(r.painted, 1, 'both Today surfaces repaint from one place, once');

  // 2. READY is an explicit clear, not an omission — this is what lets him UNpark a row.
  r = await save({ text: 'Chase the permit', entry: 'action', state: '', wait: 'Tony' }, receipt);
  assert.ok('state' in r.posted, 'READY must SEND the field — omitting it leaves the old state');
  assert.equal(r.posted.state, '', 'READY clears the stored waiting/decide state');
  assert.equal(r.posted.waiting_on, '', 'clearing the state clears the owner with it');

  // 3. A RECORD can be finished-but-kept.
  r = await save({ text: 'Feliz annuity', entry: 'record', state: 'settled' }, receipt);
  assert.equal(r.posted.state, 'settled');
  assert.equal(r.posted.entry, 'record');
  assert.equal(r.posted.bucket, '', 'a record has no lane');

  // 4. A refusal is never eaten: the editor stays open, typed values intact, reason on the button.
  r = await save({ text: 'Chase the permit', entry: 'action', state: 'waiting', wait: 'Tony' },
                 { body: { ok: false, error: "unknown state 'waiting' — use active, waiting, settled or decide" } });
  assert.equal(r.button.textContent, "RETRY — unknown state 'waiting'", r.button.textContent);
  assert.equal(r.button.disabled, false, 'he has to be able to try again');
  assert.equal(r.cmd.editing, 'row-1', 'a refused save keeps the editor open');
  assert.equal(r.nodes['.cmd-ewait'].value, 'Tony', 'the typed values are still on screen');
  assert.equal(r.painted, 0, 'nothing repainted from a refusal');

  // 5. Signed out is a NAMED failure, not a generic one.
  r = await save({ text: 'Chase the permit', entry: 'action', state: 'waiting', wait: 'Tony' },
                 { status: 401, body: null });
  assert.equal(r.login, 1, 'a 401 must send him back to the login screen');
  assert.equal(r.button.textContent, 'RETRY — Sign in to save.', r.button.textContent);
  assert.equal(r.button.disabled, false);
  assert.equal(r.cmd.editing, 'row-1', 'a signed-out save keeps the edit');
  assert.equal(r.painted, 0);

  // HTTP error JSON is not a successful board receipt, even if its body looks like one.
  for (const status of [422, 500]) {
    r = await save({text:'Keep this edit',entry:'action',state:''},
                   {status,body:{ok:true,items:[]}});
    assert.equal(r.cmd.editing, 'row-1');
    assert.equal(r.painted, 0);
    assert.equal(r.button.disabled, false);
    assert.equal(r.button.textContent, 'RETRY SAVE');
  }

  // 7. Clearing a row STORES 'active', which is not one of the select's options. It has to
  // fold to READY/ACTIVE ('') by rule, not by the browser happening to pick the first option.
  assert.equal(dStateOf({ state: 'active' }), '', "a stored 'active' is the select's READY/ACTIVE");
  assert.equal(dStateOf({ state: 'waiting' }), 'waiting', 'a real state is left alone');
  assert.equal(dStateOf({ state: '' }), '');
  assert.equal(dStateOf({ state: 'active' }, { state: 'decide' }), 'decide',
               'an unsaved draft still wins over the stored value');
  // ...and what the select carries is exactly what the handler posts: the explicit clear.
  r = await save({ text: 'Cleared row', entry: 'action', state: dStateOf({ state: 'active' }) },
                 receipt);
  assert.equal(r.posted.state, '', "a row stored as 'active' saves as the clear, never as 'active'");

  console.log('PASS: the editor posts the state he picked (waiting/clear/settled) with its owner; '
            + "a stored 'active' still reads and saves as READY; "
            + 'refused and signed-out saves keep the edit and say why.');
})().catch(e => { console.error(e); process.exit(1); });
