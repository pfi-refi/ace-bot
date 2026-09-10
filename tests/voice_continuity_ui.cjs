/* WHAT BRADY SAID MID-TURN MUST NOT VANISH.

   The iPad silence had two halves, and both are exercised here against the real app.js in a
   real browser — not a helper, not a source grep:

     1. sendMessage began `if (!text || state.busy) return;`. The voice coalescer had already
        emptied utterBuf by then, so a sentence spoken while Ace was working left no bubble,
        no error and no record.
     2. state.busy latched true if a turn never reported done/error, and the mic never
        reopened — silence until a reload.

   Start tests/ui_server.py first. No Google, no model, no provider. */
const { chromium } = require('/Users/brady/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const URL = 'http://127.0.0.1:8791/';
const OUT = process.env.ACE_UI_OUT || '/tmp/ace-voice-shots';
const results = [];
const ok = (n, c, d) => results.push((c ? 'PASS' : 'FAIL') + ' — ' + n + (d ? '  (' + d + ')' : ''));

(async () => {
  const b = await chromium.launch({ headless: true, channel: 'chrome' });
  const p = await b.newPage({ viewport: { width: 1440, height: 1000 } });
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(URL, { waitUntil: 'domcontentloaded' });
  await p.evaluate(() => localStorage.setItem('ace2_token', 'dev'));
  await p.reload({ waitUntil: 'networkidle' });
  await p.waitForFunction(() => window.aceDebug && window.aceDebug.hold);

  const D = (fn, ...a) => p.evaluate(fn, ...a);
  const bubbles = (t) => p.evaluate(x => Array.from(document.querySelectorAll('.msg.user'))
    .filter(e => e.textContent.indexOf(x) === 0 || e.textContent.indexOf(x) > -1).length, t);

  /* ── 1. a sentence spoken mid-turn survives ─────────────────────────────────── */
  await D(() => { window.aceDebug.hold(); window.aceDebug.say('take those down first'); });
  ok('speech during a busy turn is not discarded',
     (await D(() => window.aceDebug.pending())).join('|') === 'take those down first');
  ok('...and he can see it was heard',
     await p.locator('.msg.user.held').count() === 1);
  ok('...marked as waiting, not as sent',
     /HEARD/i.test(await p.locator('.msg.user.held').first().innerText()));
  await p.screenshot({ path: `${OUT}/20-held-utterance.png` });

  /* ── 2. it goes out when the turn settles, exactly once ─────────────────────── */
  await D(() => window.aceDebug.event({ type: 'done' }));
  await p.waitForTimeout(300);
  ok('the held sentence is sent when the turn settles',
     (await D(() => window.aceDebug.pending())).length === 0);
  ok('...and is not drawn a second time', await bubbles('take those down first') === 1);
  ok('...and no longer reads as waiting',
     await p.locator('.msg.user.held').count() === 0);

  /* ── 3. the hold limit drops the OLDEST and says so ─────────────────────────── */
  await D(() => {
    window.aceDebug.hold();
    ['one', 'two', 'three', 'four', 'five'].forEach(t => window.aceDebug.say('say ' + t));
  });
  const held = await D(() => window.aceDebug.pending());
  ok('the queue is capped', held.length === 4, held.join('|'));
  ok('...keeping the most recent, dropping the oldest',
     held[held.length - 1] === 'say five' && held.indexOf('say one') === -1);
  ok('...and what fell off stays readable, marked not-sent',
     await p.locator('.msg.user.dropped').count() === 1);
  ok('...naming it as unsent rather than deleting it',
     /NOT SENT/i.test(await p.locator('.msg.user.dropped').first().innerText()));
  await p.screenshot({ path: `${OUT}/21-hold-limit.png` });
  await D(() => window.aceDebug.event({ type: 'done' }));
  await p.waitForTimeout(400);

  /* ── 4. a turn that goes silent releases the loop instead of latching ───────── */
  await D(() => { window.aceDebug.quiet(700); window.aceDebug.hold();
                  window.aceDebug.say('did you get that'); });
  ok('a silent turn still holds the mic shut at first', await D(() => window.aceDebug.busy()));
  await p.waitForTimeout(1500);
  ok('a turn that never reports back does not latch busy forever',
     await D(() => window.aceDebug.busy()) === false);
  const quietSaid = await p.evaluate(() => Array.from(document.querySelectorAll('.msg.ace'))
    .map(e => e.textContent).join(' ~ '));
  ok('...and says so honestly, without claiming it failed',
     /went quiet/i.test(quietSaid) && /do not know whether/i.test(quietSaid)
     && !/failed/i.test(quietSaid.split('went quiet')[1] || ''));
  ok('...and the held sentence is not stranded behind it',
     (await D(() => window.aceDebug.pending())).length === 0);
  await p.screenshot({ path: `${OUT}/22-quiet-turn.png` });

  /* ── 5. a SLOW turn is not a DEAD turn ─────────────────────────────────────── */
  await p.waitForTimeout(300);
  await D(() => { window.aceDebug.quiet(900); window.aceDebug.hold(); });
  await p.waitForTimeout(600);
  await D(() => window.aceDebug.event({ type: 'tool', name: 'mcp_search_drive_files' }));
  await p.waitForTimeout(600);
  ok('a tool event proves life and resets the watchdog',
     await D(() => window.aceDebug.busy()) === true);
  await p.waitForTimeout(1100);
  ok('...but real silence after it still releases', await D(() => window.aceDebug.busy()) === false);

  /* ── 6. typed input while busy is held too ─────────────────────────────────── */
  await D(() => { window.aceDebug.quiet(120000); window.aceDebug.hold(); });
  await p.fill('#chat-input', 'and put that on the calendar');
  await D(() => window.aceDebug.say());
  ok('typed text mid-turn is held, not swallowed',
     (await D(() => window.aceDebug.pending())).join('|') === 'and put that on the calendar');
  ok('...and the composer is cleared so it is not sent twice',
     (await p.inputValue('#chat-input')) === '');
  await D(() => window.aceDebug.event({ type: 'done' }));

  ok('no page errors', errs.length === 0, errs.join(' / '));
  await b.close();
  console.log(results.join('\n'));
  console.log('\n' + results.filter(r => r.startsWith('PASS')).length + ' passed, '
              + results.filter(r => r.startsWith('FAIL')).length + ' failed');
  process.exit(results.some(r => r.startsWith('FAIL')) ? 1 : 0);
})();
