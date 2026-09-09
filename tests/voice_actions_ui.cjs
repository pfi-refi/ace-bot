/* Progress cards in a real browser against the real app + disposable Postgres.
   Start tests/ui_server.py first. Fake provider; no Google, no model, no network. */
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

  const raise = (kind) => p.evaluate(k => fetch('/demo/task', {
    method: 'POST', headers: { 'Content-Type': 'application/json',
      Authorization: 'Bearer dev' }, body: JSON.stringify({ kind: k })
  }).then(r => r.json()), kind);
  const cards = () => p.locator('.task-card');
  const shot = (n) => p.screenshot({ path: `${OUT}/${n}.png` });

  // ── the chat panel must NOT open, and the page must not navigate ──────────────
  const beforeUrl = p.url();
  const chatWasOpen = await p.evaluate(() => document.body.classList.contains('mode-chat'));
  await raise('working');
  await p.waitForSelector('.task-card', { timeout: 5000 });
  ok('a card appears without opening the chat panel',
     (await p.evaluate(() => document.body.classList.contains('mode-chat'))) === chatWasOpen);
  ok('...and without navigating away', p.url() === beforeUrl);
  ok('the working card names its state',
     /WORKING/i.test(await cards().first().innerText()));
  await shot('01-desktop-working');

  // ── it must not cover the mic, the composer, or the board controls ───────────
  const overlap = await p.evaluate(() => {
    const card = document.querySelector('.task-card'); if (!card) return 'no card';
    const r = card.getBoundingClientRect();
    const hits = [];
    ['#mic-btn', '#chat-input', '.qa-cmd', '#command-btn', '.dock', '.qa'].forEach(sel => {
      document.querySelectorAll(sel).forEach(el => {
        const q = el.getBoundingClientRect();
        if (q.width === 0 || q.height === 0) return;
        if (!(r.right < q.left || r.left > q.right || r.bottom < q.top || r.top > q.bottom))
          hits.push(sel);
      });
    });
    return hits.join(',') || 'none';
  });
  ok('the card covers no mic, input or board control', overlap === 'none', overlap);

  // ── approval: sticky, no close button, and answerable ───────────────────────
  await raise('approval');
  await p.waitForSelector('.task-card[data-state="needs_approval"]', { timeout: 5000 });
  const appr = p.locator('.task-card[data-state="needs_approval"]').first();
  ok('an approval card has no dismiss control',
     await appr.locator('.tc-x').count() === 0);
  const apprText = await appr.innerText();
  ok('...and offers Review and a refusal',
     /review it/i.test(apprText) && /no,\s*don/i.test(apprText), apprText.replace(/\n/g, ' | '));
  await shot('02-desktop-approval');
  await p.waitForTimeout(10500);
  ok('an approval is still there after the success window has passed',
     await p.locator('.task-card[data-state="needs_approval"]').count() === 1,
     'a pending approval must never auto-dismiss');

  // "No, do not send it"
  await appr.locator('.tc-no').click();
  await p.waitForTimeout(900);
  ok('refusing it settles the card as not done',
     await p.locator('.task-card[data-state="failed"]').count() >= 1);

  // ── failure: sticky, no link ────────────────────────────────────────────────
  await p.evaluate(() => document.querySelectorAll('.task-card .tc-x').forEach(x => x.click()));
  await raise('failure');
  await p.waitForSelector('.task-card[data-state="failed"]', { timeout: 5000 });
  const fail = p.locator('.task-card[data-state="failed"]', { hasText: 'permission denied' }).first();
  ok('a failure card explains itself', await fail.count() === 1);
  ok('...and offers no link', await fail.locator('.tc-go').count() === 0);
  await shot('03-desktop-failure');
  await p.waitForTimeout(10500);
  ok('a failure does not self-dismiss',
     await p.locator('.task-card[data-state="failed"]').count() >= 1);
  await p.evaluate(() => document.querySelectorAll('.task-card .tc-x').forEach(x => x.click()));

  // ── success: verified link, then it clears itself ───────────────────────────
  const started = await raise('success');
  ok('dispatch does not report success', started.card.state !== 'completed', started.card.state);
  await p.waitForSelector('.task-card[data-state="completed"]', { timeout: 15000 });
  const done = p.locator('.task-card[data-state="completed"]').first();
  const link = await done.locator('.tc-go').getAttribute('href');
  ok('the success card offers Open spreadsheet',
     /Open spreadsheet/i.test(await done.innerText()));
  ok('...pointing at the verified id',
     link === 'https://docs.google.com/spreadsheets/d/1DemoSheetIdAbCdEfGhIjKlMnOpQrStUvWx/edit',
     link);
  await shot('04-desktop-success');
  const tid = await done.getAttribute('data-task');
  await p.waitForTimeout(10500);
  ok('a success dismisses itself after ~9s',
     await p.locator(`.task-card[data-task="${tid}"]`).count() === 0);

  // ── the result stays reachable in recent activity ────────────────────────────
  await p.click('#more-btn').catch(() => {});
  await p.waitForTimeout(200);
  await p.click('#activity-open');
  await p.waitForSelector('#ace-review[open]', { timeout: 5000 });
  await p.waitForTimeout(700);
  const tray = await p.locator('#ace-review').innerText();
  ok('the dismissed result is retrievable in recent activity',
     /Sample Assistant Pricing/.test(tray) && /Done/.test(tray));
  ok('...with the same verified link',
     (await p.locator('#ace-review a').first().getAttribute('href')) ===
     'https://docs.google.com/spreadsheets/d/1DemoSheetIdAbCdEfGhIjKlMnOpQrStUvWx/edit');
  await shot('05-desktop-activity');
  await p.evaluate(() => document.getElementById('ace-review').close());

  // ── a second tab shows the same task, and creates nothing ───────────────────
  const p2 = await b.newPage({ viewport: { width: 1440, height: 1000 } });
  await p2.goto(URL, { waitUntil: 'domcontentloaded' });
  await p2.evaluate(() => localStorage.setItem('ace2_token', 'dev'));
  await p2.reload({ waitUntil: 'networkidle' });
  await raise('approval');
  await p2.waitForTimeout(1200);
  const beforeCount = await p2.evaluate(() => fetch('/actions?limit=50',
      { headers: { Authorization: 'Bearer dev' } }).then(r => r.json()).then(d => d.cards.length));
  await p2.reload({ waitUntil: 'networkidle' });
  await p2.waitForTimeout(1200);
  ok('a second tab rebuilds live cards from the server',
     await p2.locator('.task-card[data-state="needs_approval"]').count() >= 1);
  const afterCount = await p2.evaluate(() => fetch('/actions?limit=50',
      { headers: { Authorization: 'Bearer dev' } }).then(r => r.json()).then(d => d.cards.length));
  ok('...and opening it created no new task', afterCount === beforeCount,
     `${beforeCount} → ${afterCount}`);
  await p2.close();

  // ── hiding the page must not cancel accepted work ────────────────────────────
  await p.evaluate(() => document.querySelectorAll('.task-card .tc-x').forEach(x => x.click()));
  const hidden = await p.evaluate(async () => {
    Object.defineProperty(document, 'visibilityState', { get: () => 'hidden', configurable: true });
    Object.defineProperty(document, 'hidden', { get: () => true, configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    const r = await fetch('/demo/task', { method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer dev' },
      body: JSON.stringify({ kind: 'success' }) }).then(x => x.json());
    return r.card.task_id;
  });
  await p.waitForTimeout(2500);
  const hiddenState = await p.evaluate(id => fetch('/actions/' + id,
      { headers: { Authorization: 'Bearer dev' } }).then(r => r.json())
      .then(d => d.card.state), hidden);
  ok('work accepted while the page is hidden still completes',
     hiddenState === 'completed', hiddenState);

  // ── phone ────────────────────────────────────────────────────────────────────
  await p.setViewportSize({ width: 390, height: 844 });
  await p.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
  });
  await p.waitForTimeout(400);
  await p.evaluate(() => document.querySelectorAll('.task-card .tc-x').forEach(x => x.click()));
  await raise('approval');
  await p.waitForSelector('.task-card[data-state="needs_approval"]', { timeout: 5000 });
  await p.waitForTimeout(400);
  const phone = await p.evaluate(() => {
    const card = document.querySelector('.task-card');
    const r = card.getBoundingClientRect();
    const hits = [];
    ['#mic-btn', '#chat-input', '.dock', '.qa'].forEach(sel => {
      document.querySelectorAll(sel).forEach(el => {
        const q = el.getBoundingClientRect();
        if (q.width === 0 || q.height === 0) return;
        if (!(r.right < q.left || r.left > q.right || r.bottom < q.top || r.top > q.bottom))
          hits.push(sel);
      });
    });
    const btn = card.querySelector('.tc-no');
    return { overlap: hits.join(',') || 'none',
             overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
             btnH: btn ? Math.round(btn.getBoundingClientRect().height) : 0,
             bottom: Math.round(window.innerHeight - r.bottom) };
  });
  ok('phone: the card covers no mic, input or dock control', phone.overlap === 'none', phone.overlap);
  ok('phone: no horizontal page overflow', phone.overflow <= 0, String(phone.overflow));
  ok('phone: controls clear a 44px touch target', phone.btnH >= 44, String(phone.btnH));
  ok('phone: the card sits above the bottom bar', phone.bottom >= 100, String(phone.bottom));
  ok('phone: it is a BOTTOM card, not a mid-screen one',
     phone.bottom <= Math.round(844 * 0.55), String(phone.bottom));
  await shot('06-phone-approval');
  await p.evaluate(() => document.querySelectorAll('.task-card .tc-x').forEach(x => x.click()));
  await raise('success');
  await p.waitForSelector('.task-card[data-state="completed"]', { timeout: 15000 });
  await shot('07-phone-success');
  await p.evaluate(() => document.querySelectorAll('.task-card .tc-x').forEach(x => x.click()));
  await raise('failure');
  await p.waitForSelector('.task-card[data-state="failed"]', { timeout: 5000 });
  await shot('08-phone-failure');
  await raise('working');
  await p.waitForTimeout(300);
  await shot('09-phone-working');

  ok('no page errors', errs.length === 0, errs.join(' / '));
  await b.close();
  console.log(results.join('\n'));
  console.log('\n' + results.filter(r => r.startsWith('PASS')).length + ' passed, '
              + results.filter(r => r.startsWith('FAIL')).length + ' failed');
  process.exit(results.some(r => r.startsWith('FAIL')) ? 1 : 0);
})();
