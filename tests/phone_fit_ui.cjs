/* Card fit on narrow screens. Asserts REAL bounding rectangles, not document.scrollWidth —
   the page hides overflow, so scrollWidth reported 375 while the card's right edge was at
   403. Start tests/ui_server.py first. */
const { chromium } = require('/Users/brady/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const URL = 'http://127.0.0.1:8791/';
const OUT = process.env.ACE_UI_OUT || '/tmp/ace-voice-shots';
const results = [];
const ok = (n, c, d) => results.push((c ? 'PASS' : 'FAIL') + ' — ' + n + (d ? '  (' + d + ')' : ''));
const PAD = 12;   // #app padding on narrow screens

(async () => {
  const b = await chromium.launch({ headless: true, channel: 'chrome' });
  const errs = [];

  async function measure(p, label) {
    return p.evaluate((lbl) => {
      const out = [];
      document.querySelectorAll('.card, .card-slot, #stage, .task-card').forEach(e => {
        const r = e.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        out.push({ what: (e.id ? '#' + e.id : '.' + e.className.split(' ')[0]),
                   left: Math.round(r.left), right: Math.round(r.right),
                   width: Math.round(r.width) });
      });
      return { vw: window.innerWidth, boxes: out, scrollW: document.documentElement.scrollWidth };
    }, label);
  }

  for (const w of [375, 390, 430]) {
    const p = await b.newPage({ viewport: { width: w, height: 844 } });
    p.on('pageerror', e => errs.push(e.message));
    await p.goto(URL, { waitUntil: 'domcontentloaded' });
    await p.evaluate(() => localStorage.setItem('ace2_token', 'dev'));
    await p.reload({ waitUntil: 'networkidle' });

    // Every dashboard card Brady can summon, normal and wide.
    for (const panel of ['daybank', 'timeline', 'inbox']) {
      await p.click(`.qa[data-panel="${panel}"]`).catch(() => {});
      await p.waitForTimeout(500);
    }
    await p.waitForSelector('.card', { timeout: 8000 });
    await p.waitForTimeout(500);

    let m = await measure(p, 'cards');
    let bad = m.boxes.filter(x => x.right > w - PAD + 1 || x.left < PAD - 1);
    ok(`${w}px: every card sits inside the usable width`, bad.length === 0,
       bad.map(x => `${x.what} ${x.left}..${x.right}`).join(' | ') || `${m.boxes.length} boxes`);

    // A wide/pinned card must respect its parent, not the viewport.
    await p.evaluate(() => document.querySelectorAll('.card').forEach(c => {
      c.classList.add('card-wide'); }));
    await p.waitForTimeout(300);
    m = await measure(p, 'wide');
    bad = m.boxes.filter(x => x.right > w - PAD + 1);
    ok(`${w}px: a wide card still respects the parent`, bad.length === 0,
       bad.map(x => `${x.what} → ${x.right}`).join(' | '));

    // Long unbroken content must wrap rather than force the track wide.
    await p.evaluate(() => {
      const t = document.querySelector('.card-body') || document.querySelector('.card');
      if (t) { const d = document.createElement('div');
        d.textContent = 'https://example.com/' + 'x'.repeat(160); t.appendChild(d); }
    });
    await p.waitForTimeout(300);
    m = await measure(p, 'longword');
    bad = m.boxes.filter(x => x.right > w - PAD + 1);
    ok(`${w}px: a long unbroken URL does not push the card out`, bad.length === 0,
       bad.map(x => `${x.what} → ${x.right}`).join(' | '));

    // The task progress card, at the same widths.
    await p.evaluate(() => fetch('/demo/task', { method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer dev' },
      body: JSON.stringify({ kind: 'approval' }) }));
    await p.waitForSelector('.task-card', { timeout: 6000 });
    await p.waitForTimeout(400);
    m = await measure(p, 'taskcard');
    bad = m.boxes.filter(x => x.what === '.task-card' && (x.right > w - PAD + 1 || x.left < PAD - 1));
    ok(`${w}px: the progress card sits inside too`, bad.length === 0,
       bad.map(x => `${x.left}..${x.right}`).join(' | '));

    if (w === 390) await p.screenshot({ path: `${OUT}/12-phone-card-fit.png` });
    await p.close();
  }

  // Desktop floating behaviour must be unchanged.
  const d = await b.newPage({ viewport: { width: 1440, height: 1000 } });
  await d.goto(URL, { waitUntil: 'domcontentloaded' });
  await d.evaluate(() => localStorage.setItem('ace2_token', 'dev'));
  await d.reload({ waitUntil: 'networkidle' });
  await d.click('.qa[data-panel="daybank"]');
  await d.waitForSelector('.card', { timeout: 8000 });
  await d.waitForTimeout(500);
  const desk = await d.evaluate(() => {
    const c = document.querySelector('.card'); const r = c.getBoundingClientRect();
    return { w: Math.round(r.width), pos: getComputedStyle(document.querySelector('.card-slot')).position };
  });
  ok('desktop: the card keeps its floating slot and its own width',
     desk.pos === 'absolute' && desk.w > 250 && desk.w < 600, JSON.stringify(desk));
  await d.close();

  ok('no page errors', errs.length === 0, errs.join(' / '));
  await b.close();
  console.log(results.join('\n'));
  console.log('\n' + results.filter(r => r.startsWith('PASS')).length + ' passed, '
              + results.filter(r => r.startsWith('FAIL')).length + ' failed');
  process.exit(results.some(r => r.startsWith('FAIL')) ? 1 : 0);
})();
