/* THE 2026-09-21 PHONE DEFECT, IN A REAL BROWSER, AT THE SIZE IT HAPPENED ON.
   A live 390x844 run opened the Command Center and could not press a row's Edit button:
   months-old FAILED deep-dive cards were sitting on top of the board and took the tap. They
   also came back every time the socket reconnected or the tab was restored, because
   dismissal only ever lived in memory.

   Everything here is synthetic and local: this file serves the shipped ace2/ files and a
   fixture API from 127.0.0.1, including a real WebSocket handshake so the RECONNECT path is
   the production one (ws.onopen → syncTaskCards) rather than a stand-in. No live server, no
   database, no model call, no production URL.

   tests/ui_server.py is the usual fixture server for these checks, but it is owned by
   another agent this cycle and cannot carry historical task rows or the new /entities and
   /graph routes — so the fixture lives here instead, in the same shape those endpoints
   return.

   Asserted, at desktop AND 390x844:
     * a stale FAILED card never renders, on load, on reconnect or on visibility restore;
     * with that stale card in the data, the board's Edit button and Today checkbox both work;
     * dismissal survives reload, reconnect and visibilitychange — each asserted separately;
     * needs_approval renders in every mode, including "Off", and Activity keeps everything;
     * "results only" shows results, not progress ticks;
     * a result landing while the mic is open is delivered and does not disturb the voice UI;
     * a full all → results → off → all toggle starts no task and issues no write;
     * the visible stack is capped at three with an overflow pill;
     * Stop is reachable on a live row in Recent activity.
*/
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require('/Users/brady/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const ROOT = path.join(__dirname, '..', 'ace2');
const results = [];
const ok = (n, c, d) => { results.push((c ? 'PASS' : 'FAIL') + ' — ' + n + (d ? '  (' + d + ')' : '')); };

const ago = (ms) => new Date(Date.now() - ms).toISOString();
const MIN = 60 * 1000, DAY = 24 * 60 * MIN;
const TODAY = new Date().toISOString().slice(0, 10);

// The exact wording the live run was blocked by, so the assertion names the real failure.
const STALE_MSG = 'The deep dive response was incomplete; no finished answer is available.';

function fixtureCards() {
  return {
    live: { task_id: 'tsk-live', state: 'working', title: 'Building the sample sheet',
            detail: 'Writing the rows', created_at: ago(30 * 1000), sticky: false,
            auto_dismiss_ms: 0 },
    stale: { task_id: 'tsk-stale-deepdive', state: 'failed',
             title: 'Deep dive: sample assistant pricing', error: STALE_MSG,
             created_at: ago(45 * DAY), settled_at: ago(45 * DAY),
             sticky: true, auto_dismiss_ms: 0 },
    fresh: { task_id: 'tsk-fresh-fail', state: 'failed', title: 'Sample spreadsheet',
             error: 'The provider refused to create it: 403 permission denied. Nothing was made.',
             created_at: ago(3 * MIN), settled_at: ago(2 * MIN), sticky: true, auto_dismiss_ms: 0 },
    approval: { task_id: 'tsk-approval', state: 'needs_approval', title: 'Share with Chris',
                detail: 'Sharing this outside your account needs your OK.',
                created_at: ago(20 * DAY), review_id: 'rev-demo', sticky: true, auto_dismiss_ms: 0 }
  };
}

function boardItems() {
  return [
    { id: 'row-permit', text: 'County permit fee due (fixture)', tags: ['Bills'], status: 'open',
      entry: 'record', state: '', bucket: 'Groundworks', due: TODAY, due_on: TODAY, due_days: 0,
      lane: 'today', completable: true, ts: '2026-09-20T10:00:00' },
    { id: 'row-packet', text: 'Chase Rebecca for the signed packet (fixture)', tags: ['Deals'],
      status: 'open', entry: 'action', state: '', bucket: 'GFI/PFI', due: '', due_days: null,
      lane: 'today', chosen_on: TODAY, completable: true, ts: '2026-09-20T11:00:00' }
  ];
}

function startServer() {
  const st = { cards: fixtureCards(), items: boardItems(), sockets: [], posts: [] };
  const files = {
    '/': ['index.html', 'text/html'], '/index.html': ['index.html', 'text/html'],
    '/app.js': ['app.js', 'text/javascript'], '/review.js': ['review.js', 'text/javascript'],
    '/styles.css': ['styles.css', 'text/css'], '/review.css': ['review.css', 'text/css'],
    '/manifest.json': ['manifest.json', 'application/json']
  };
  function board() {
    const open = st.items.filter((i) => i.status === 'open');
    return { ok: true, today: TODAY, items: st.items,
             due_today: { deadlines: open.filter((i) => i.due_days === 0),
                          chosen: open.filter((i) => i.chosen_on === TODAY && i.due_days !== 0),
                          suggested: [], suggested_total: 0, decisions: [], review: [], waiting: [] } };
  }
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const p = url.pathname;
    const json = (o, code) => {
      res.writeHead(code || 200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(o));
    };
    if (req.method === 'GET' && files[p]) {
      const [name, type] = files[p];
      res.writeHead(200, { 'Content-Type': type });
      res.end(fs.readFileSync(path.join(ROOT, name)));
      return;
    }
    if (p === '/sw.js') { res.writeHead(404); res.end(''); return; }   // no worker in a check
    if (req.method === 'POST') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        st.posts.push({ path: p, body: body });
        if (p === '/daybank/update') {
          let d = {}; try { d = JSON.parse(body || '{}'); } catch (e) { d = {}; }
          const row = st.items.filter((i) => i.id === d.id)[0];
          if (row && d.status) row.status = d.status;
          if (row && d.text) row.text = d.text;
          json(board()); return;
        }
        if (/^\/actions\/[^/]+\/cancel$/.test(p)) {
          const id = p.split('/')[2];
          json({ ok: true, card: { task_id: id, state: 'cancelled', title: 'Building the sample sheet',
                                   created_at: ago(30 * 1000), settled_at: new Date().toISOString() } });
          return;
        }
        json({ ok: true });
      });
      return;
    }
    if (p === '/health') { json({ auth_required: true, locked: false }); return; }
    if (p === '/session') { json({ ok: true }); return; }
    if (p === '/actions') {
      const c = st.cards;
      const all = [c.live, c.fresh, c.stale, c.approval].filter(Boolean);
      json({ cards: all, live: all.filter((x) => x.state === 'queued' || x.state === 'working') });
      return;
    }
    if (p === '/daybank') { json(board()); return; }
    if (p === '/board/lists') { json({ areas: ['Inbox', 'Groundworks', 'GFI/PFI'], custom: [] }); return; }
    if (p === '/thread') { json({ messages: [] }); return; }
    if (p === '/calendar') { json({ events: [] }); return; }
    if (p === '/settings') { json({ discreet: false }); return; }
    if (p === '/convai/config') { json({ enabled: false }); return; }
    if (p === '/push/key') { json({}); return; }
    if (p === '/graph') { json({ nodes: [], edges: [], source: 'entities', counts: {} }); return; }
    json({});
  });
  // A REAL HANDSHAKE, so ws.onopen — and therefore syncTaskCards — is the shipped path.
  server.on('upgrade', (req, socket) => {
    const key = req.headers['sec-websocket-key'] || '';
    const accept = crypto.createHash('sha1')
      .update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
               + 'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n');
    st.sockets.push(socket);
    socket.on('error', () => {});
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      st.url = 'http://127.0.0.1:' + server.address().port + '/';
      st.close = () => { st.sockets.forEach((s) => { try { s.destroy(); } catch (e) {} });
                         server.close(); };
      st.drop = () => { const s = st.sockets.splice(0); s.forEach((x) => { try { x.destroy(); } catch (e) {} }); };
      resolve(st);
    });
  });
}

const SEL = {
  stale: '.task-card[data-task="tsk-stale-deepdive"]',
  fresh: '.task-card[data-task="tsk-fresh-fail"]',
  approval: '.task-card[data-task="tsk-approval"]'
};

(async () => {
  const st = await startServer();
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const errs = [];

  for (const vp of [{ width: 1440, height: 900, name: 'desktop' },
                    { width: 390, height: 844, name: 'phone 390x844' }]) {
    st.cards = fixtureCards();
    st.items = boardItems();
    const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
    // Nothing leaves the machine: the shell's optional CDN import is refused, not fetched.
    await context.route((u) => !String(u).startsWith(st.url.slice(0, -1)), (r) => r.abort());
    await context.addInitScript(() => { localStorage.setItem('ace2_token', 'dev'); });
    const page = await context.newPage();
    page.on('pageerror', (e) => errs.push(vp.name + ': ' + e.message));
    const reqs = [];
    page.on('request', (r) => reqs.push({ m: r.method(), u: r.url(),
                                          auth: (r.headers() || {}).authorization || '' }));
    const tag = (s) => vp.name + ': ' + s;

    await page.goto(st.url, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector(SEL.approval, { timeout: 8000 });
    await page.waitForTimeout(400);

    // 1. THE LIVE DEFECT. A card that failed 45 days ago is history, not news.
    ok(tag('a 45-day-old FAILED card does not render on load'),
       await page.locator(SEL.stale).count() === 0);
    ok(tag('a failure from two minutes ago still renders'),
       await page.locator(SEL.fresh).count() === 1);
    ok(tag('a 20-day-old approval renders anyway — it is a gate, not a notification'),
       await page.locator(SEL.approval).count() === 1);
    ok(tag('the default preference keeps a WORKING card off the screen'),
       await page.locator('.task-card[data-task="tsk-live"]').count() === 0);
    ok(tag('the saved preference defaults to results only'),
       await page.evaluate(() => window.aceDebug.notify()) === 'results');

    // 2. DISMISSAL, AND THE THREE WAYS IT USED TO COME UNDONE.
    await page.click(SEL.fresh + ' .tc-x');
    await page.waitForTimeout(300);
    ok(tag('dismissing removes the card'), await page.locator(SEL.fresh).count() === 0);
    const saved = await page.evaluate(() => localStorage.getItem('ace.notify.dismissed') || '');
    ok(tag('dismissal is written to this device'), saved.indexOf('tsk-fresh-fail@') >= 0, saved);

    st.drop();                                   // a real socket drop → a real reconnect
    await page.waitForTimeout(2200);
    ok(tag('dismissal survives a RECONNECT'), await page.locator(SEL.fresh).count() === 0);
    ok(tag('the stale failure does not return on a RECONNECT'),
       await page.locator(SEL.stale).count() === 0);

    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForTimeout(500);
    ok(tag('dismissal survives VISIBILITYCHANGE'), await page.locator(SEL.fresh).count() === 0);
    ok(tag('the stale failure does not return on VISIBILITYCHANGE'),
       await page.locator(SEL.stale).count() === 0);

    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector(SEL.approval, { timeout: 8000 });
    await page.waitForTimeout(500);
    ok(tag('dismissal survives a RELOAD'), await page.locator(SEL.fresh).count() === 0);
    ok(tag('the stale failure does not return after a RELOAD'),
       await page.locator(SEL.stale).count() === 0);
    ok(tag('the approval is still there after a reload — never suppressed'),
       await page.locator(SEL.approval).count() === 1);

    // 3. THE NAMED ACCEPTANCE ITEM: pre-existing FAILED cards must not block the board.
    await page.click('#command-btn');
    await page.waitForSelector('.cmd-row', { timeout: 8000 });
    await page.waitForTimeout(300);
    ok(tag('an open panel pushes the card layer below it'),
       await page.evaluate(() => document.body.classList.contains('panel-open')
         && getComputedStyle(document.getElementById('task-layer')).zIndex === '58'));
    const hitPencil = await page.evaluate(() => {
      const b = document.querySelector('.cmd-row .cmd-pencil');
      const r = b.getBoundingClientRect();
      const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return { same: el === b || b.contains(el), got: el ? (el.className || el.tagName) : 'nothing' };
    });
    ok(tag('PRE-EXISTING FAILED CARDS DO NOT COVER THE EDIT BUTTON'), hitPencil.same, hitPencil.got);
    await page.click('.cmd-row .cmd-pencil', { timeout: 5000 });
    await page.waitForSelector('.cmd-row.cmd-editing .cmd-etext', { timeout: 5000 });
    ok(tag('EDIT OPENS with historical failed cards in the data'), true);
    await page.click('.cmd-row.cmd-editing .cmd-ecancel');
    await page.waitForTimeout(200);

    const before = st.posts.length;
    await page.click('.cmd-row[data-id="row-permit"] .cmd-box', { timeout: 5000 });
    await page.waitForTimeout(600);
    const ticked = st.posts.slice(before).filter((x) => x.path === '/daybank/update'
                                                     && x.body.indexOf('"status":"done"') >= 0);
    ok(tag("THE TODAY CHECKBOX REACHES THE SERVER"), ticked.length === 1,
       JSON.stringify(st.posts.slice(before).map((x) => x.path)));

    // ...and neither a reconnect nor a visibility restore may drop a card over the open board.
    st.drop();
    await page.waitForTimeout(2200);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForTimeout(500);
    ok(tag('the stale card stays gone while the board is open'),
       await page.locator(SEL.stale).count() === 0);
    await page.click('#cmd-x');
    await page.waitForTimeout(200);
    ok(tag('closing the board clears panel-open'),
       await page.evaluate(() => !document.body.classList.contains('panel-open')));

    // 4. THE THREE MODES, SET THROUGH THE REAL SETTINGS CONTROL.
    async function setMode(mode) {
      await page.click('#more-btn');
      await page.waitForSelector('[data-notify="' + mode + '"]', { state: 'visible', timeout: 4000 });
      // A plain click, never `force`: if a card is covering the drawer this must fail, which
      // is how the same obstruction was caught here as on the board.
      await page.click('[data-notify="' + mode + '"]', { timeout: 6000 });
      await page.waitForTimeout(150);
      const lit = await page.evaluate((m) => Array.prototype.map.call(
        document.querySelectorAll('[data-notify]'),
        (b) => b.getAttribute('data-notify') + (b.classList.contains('on') ? '*' : '')
             + (b.getAttribute('aria-pressed') === 'true' ? '!' : '')).join(' '), mode);
      ok(tag('the ' + mode + ' segment shows as the chosen one'),
         lit.indexOf(mode + '*!') >= 0 && lit.split('*').length === 2, lit);
      await page.keyboard.press('Escape');
      await page.waitForTimeout(150);
    }
    const push = (card) => page.evaluate((c) => window.aceDebug.event(
      Object.assign({ type: 'task' }, c)), card);

    await setMode('all');
    ok(tag('a task card does not cover the More tools drawer either'), true);
    ok(tag('the settings control writes the preference to this device'),
       await page.evaluate(() => localStorage.getItem('ace.notify.mode')) === 'all');
    await push({ task_id: 'tsk-a', state: 'working', title: 'Working under ALL',
                 created_at: new Date().toISOString() });
    await page.waitForTimeout(200);
    ok(tag('ALL shows work in progress'),
       await page.locator('.task-card[data-task="tsk-a"]').count() === 1);
    await setMode('results');
    await page.waitForTimeout(250);
    ok(tag('switching to RESULTS hides existing optional progress'),
       await page.locator('.task-card[data-task="tsk-a"]').count() === 0);

    await setMode('results');
    await push({ task_id: 'tsk-b', state: 'working', title: 'Progress tick',
                 created_at: new Date().toISOString() });
    await page.waitForTimeout(200);
    ok(tag('RESULTS ONLY ignores a progress tick'),
       await page.locator('.task-card[data-task="tsk-b"]').count() === 0);
    await push({ task_id: 'tsk-b', state: 'completed', title: 'Progress tick',
                 detail: 'Verified and ready.', created_at: new Date().toISOString(),
                 settled_at: new Date().toISOString(), auto_dismiss_ms: 0 });
    await page.waitForTimeout(200);
    ok(tag('RESULTS ONLY shows the actual result'),
       await page.locator('.task-card[data-task="tsk-b"]').count() === 1);
    await setMode('off');
    await page.waitForTimeout(250);
    ok(tag('switching OFF hides an already-visible optional result'),
       await page.locator('.task-card[data-task="tsk-b"]').count() === 0);
    await push({ task_id: 'tsk-b', state: 'completed', title: 'Later update while OFF',
                 settled_at: new Date().toISOString() });
    await page.waitForTimeout(200);
    ok(tag('an update to that result stays hidden while OFF'),
       await page.locator('.task-card[data-task="tsk-b"]').count() === 0);

    await setMode('off');
    await push({ task_id: 'tsk-c', state: 'completed', title: 'Silenced result',
                 created_at: new Date().toISOString(), settled_at: new Date().toISOString() });
    await push({ task_id: 'tsk-d', state: 'failed', title: 'Silenced failure',
                 error: 'nope', created_at: new Date().toISOString(),
                 settled_at: new Date().toISOString() });
    await page.waitForTimeout(200);
    ok(tag('OFF suppresses results'),
       await page.locator('.task-card[data-task="tsk-c"]').count() === 0
       && await page.locator('.task-card[data-task="tsk-d"]').count() === 0);
    await push({ task_id: 'tsk-e', state: 'needs_approval', title: 'Approval under OFF',
                 detail: 'Needs your OK.', created_at: new Date().toISOString() });
    await page.waitForTimeout(200);
    ok(tag('OFF never suppresses an approval'),
       await page.locator('.task-card[data-task="tsk-e"]').count() === 1);
    ok(tag('an approval carries no dismiss button — closing one would look like answering it'),
       await page.locator('.task-card[data-task="tsk-e"] .tc-x').count() === 0);
    await push({ task_id: 'tsk-mode-gate', state: 'needs_approval', title: 'Temporary gate',
                 created_at: new Date().toISOString() });
    await page.waitForTimeout(100);
    await push({ task_id: 'tsk-mode-gate', state: 'completed', title: 'Gate settled while OFF',
                 settled_at: new Date().toISOString() });
    await page.waitForTimeout(250);
    ok(tag('a settled approval does not leave a stale gate or optional result while OFF'),
       await page.locator('.task-card[data-task="tsk-mode-gate"]').count() === 0);

    // OFF is about popups. The server-side history is untouched.
    await page.click('#more-btn');
    await page.click('#activity-open');
    await page.waitForSelector('#ace-review article', { timeout: 6000 });
    await page.waitForTimeout(600);
    const activity = await page.evaluate(() => Array.prototype.map.call(
      document.querySelectorAll('#ace-review article h3'), (h) => h.textContent));
    ok(tag('OFF leaves Recent activity complete'),
       activity.length === 4 && activity.join('|').indexOf('Deep dive') >= 0,
       activity.join(' | '));

    // 5. STOP IS STILL REACHABLE on the live row, now that its popup is hidden.
    const stopN = st.posts.filter((x) => x.path === '/actions/tsk-live/cancel').length;
    ok(tag('a live row in Recent activity carries Stop'),
       await page.locator('#ace-review article button[data-stop="tsk-live"]').count() === 1);
    await page.click('#ace-review article button[data-stop="tsk-live"]');
    await page.waitForTimeout(400);
    ok(tag('Stop posts to the existing cancel endpoint'),
       st.posts.filter((x) => x.path === '/actions/tsk-live/cancel').length === stopN + 1);
    await page.evaluate(() => document.getElementById('ace-review').close());
    await page.waitForTimeout(150);

    // 6. CHANGING THE PREFERENCE NEVER STARTS, RETRIES OR RESTARTS WORK.
    const mark = reqs.length;
    await setMode('all'); await setMode('results'); await setMode('off'); await setMode('all');
    const during = reqs.slice(mark);
    const writes = during.filter((r) => r.m !== 'GET');
    const taskCalls = during.filter((r) => /\/actions|\/chat|\/capture|\/business\/report/.test(r.u));
    ok(tag('a full three-way toggle issues no write at all'), writes.length === 0,
       writes.map((w) => w.m + ' ' + w.u).join(' | '));
    ok(tag('a full three-way toggle touches no task endpoint'), taskCalls.length === 0,
       taskCalls.map((w) => w.u).join(' | '));

    // 7. A RESULT LANDING WHILE THE MIC IS OPEN.
    await setMode('results');
    await page.evaluate(() => window.aceDebug.listening(true));
    await page.waitForTimeout(150);
    await push({ task_id: 'tsk-voice', state: 'completed', title: 'Finished while listening',
                 detail: 'Verified and ready.', created_at: new Date().toISOString(),
                 settled_at: new Date().toISOString(), auto_dismiss_ms: 0 });
    await page.waitForTimeout(250);
    ok(tag('a result still arrives while voice is active'),
       await page.locator('.task-card[data-task="tsk-voice"]').count() === 1);
    const voice = await page.evaluate(() => {
      const mic = document.getElementById('mic-btn'), r = mic.getBoundingClientRect();
      const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return { listening: mic.classList.contains('active'),
               orb: document.getElementById('orb-state').textContent,
               micFree: el === mic || mic.contains(el) };
    });
    ok(tag('the card does not interrupt the voice UI'),
       voice.listening && voice.orb === '[ LISTENING ]' && voice.micFree, JSON.stringify(voice));
    await page.evaluate(() => window.aceDebug.listening(false));

    // 8. THE VISIBLE STACK IS CAPPED, and the rest is one tap into Activity.
    await setMode('all');
    for (const n of [1, 2, 3, 4]) {
      await push({ task_id: 'tsk-cap-' + n, state: 'completed', title: 'Capped ' + n,
                   created_at: new Date().toISOString(), settled_at: new Date().toISOString(),
                   auto_dismiss_ms: 0 });
      await page.waitForTimeout(60);
    }
    await page.waitForTimeout(250);
    const stack = await page.evaluate(() => ({
      cards: document.querySelectorAll('#task-layer .task-card[data-state="completed"]').length,
      pill: (document.getElementById('task-more') || {}).textContent || ''
    }));
    ok(tag('no more than three results are on screen at once'), stack.cards <= 3, String(stack.cards));
    ok(tag('the rest collapse into one Activity pill'),
       /more in Activity/.test(stack.pill), stack.pill);
    // A CONTROL YOU CANNOT REACH IS NOT A CONTROL. The whole slice exists to stop a card
    // layer putting something out of reach, so the pill's own box is measured against the
    // viewport rather than trusting that a click happened to resolve.
    const pillBox = await page.evaluate(() => {
      const p = document.getElementById('task-more'), r = p.getBoundingClientRect();
      const layer = document.getElementById('task-layer').getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left),
               right: Math.round(r.right), layerTop: Math.round(layer.top),
               layerBottom: Math.round(layer.bottom),
               vw: window.innerWidth, vh: window.innerHeight };
    });
    ok(tag('the overflow pill is inside the viewport'),
       pillBox.top >= 0 && pillBox.left >= 0 && pillBox.bottom <= pillBox.vh
       && pillBox.right <= pillBox.vw, JSON.stringify(pillBox));
    ok(tag('the whole card stack stays inside the viewport'),
       pillBox.layerTop >= 0 && pillBox.layerBottom <= pillBox.vh, JSON.stringify(pillBox));
    await page.click('#task-more', { timeout: 6000 });
    await page.waitForSelector('#ace-review[open]', { timeout: 6000 });
    ok(tag('the pill opens Recent activity, where the overflow lives'),
       (await page.textContent('#ace-review h2')) === 'Recent activity'
       && await page.locator('#task-more').count() === 0);
    await page.evaluate(() => document.getElementById('ace-review').close());

    if (vp.width === 390) {
      const OUT = process.env.ACE_UI_OUT || '/tmp/ace-voice-shots';
      try { fs.mkdirSync(OUT, { recursive: true }); } catch (e) {}
      await page.screenshot({ path: path.join(OUT, '20-phone-notification-cards.png') });
    }
    await context.close();
  }

  ok('no page errors in any run', errs.length === 0, errs.join(' / '));
  await browser.close();
  st.close();
  console.log(results.join('\n'));
  const failed = results.filter((r) => r.startsWith('FAIL')).length;
  console.log('\n' + (results.length - failed) + ' passed, ' + failed + ' failed');
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
