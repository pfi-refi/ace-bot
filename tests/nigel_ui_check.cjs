/* NIGEL prototype — browser check. Opens nigel/index.html from disk, walks
   StarCloud → Systems → Sectors → Records → Record, runs the simulated case
   workflow and the attention actions, and screenshots desktop + phone sizes.
   Usage: node tests/nigel_ui_check.cjs   (env NIGEL_SHOTS=/dir for screenshots) */
const path = require('path');
let chromium;
try { ({ chromium } = require('playwright')); }
catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }
const URL = 'file://' + path.resolve(__dirname, '..', 'nigel', 'index.html');
const OUT = process.env.NIGEL_SHOTS || '/tmp/nigel-shots';
require('fs').mkdirSync(OUT, { recursive: true });
const results = [];
const ok = (n, c, d) => results.push((c ? 'PASS' : 'FAIL') + ' — ' + n + (d ? '  (' + d + ')' : ''));
const shot = (p, name) => p.screenshot({ path: path.join(OUT, name + '.png') });
const settle = (p, ms) => p.waitForTimeout(ms || 700);

(async () => {
  const proxy = process.env.NIGEL_PROXY ? { server: process.env.NIGEL_PROXY } : undefined;
  const b = await chromium.launch({ headless: true, proxy });
  const errs = [];
  async function overflow(p) { return p.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1); }

  for (const [label, vp] of [['desktop', { width: 1440, height: 900 }], ['phone', { width: 390, height: 844 }]]) {
    const p = await b.newPage({ viewport: vp, deviceScaleFactor: 1, ignoreHTTPSErrors: true });
    p.on('pageerror', e => errs.push(label + ': ' + e.message));
    p.on('console', m => { if (m.type() === 'error' && !/ERR_CONNECTION|ERR_NAME_NOT_RESOLVED|ERR_INTERNET|ERR_PROXY|ERR_TUNNEL|Failed to load resource/.test(m.text())) errs.push(label + ' console: ' + m.text()); });
    await p.goto(URL); await settle(p, 1200);
    await shot(p, label + '-1-starcloud');
    ok(label + ' labels rendered', await p.locator('.label').count() === 9);
    ok(label + ' amber labels for Investments + Servicing', await p.locator('.label.is-amber').count() === 2);
    ok(label + ' no horizontal overflow on StarCloud', !(await overflow(p)));

    // labels visible inside viewport
    const boxes = await p.evaluate(() => [...document.querySelectorAll('.label')].map(l => { const r = l.getBoundingClientRect(); return { id: l.dataset.system, l: r.left, r: r.right, t: r.top, b: r.bottom }; }));
    const inside = boxes.every(bx => bx.l >= 0 && bx.r <= vp.width && bx.t >= 0 && bx.b <= vp.height);
    ok(label + ' all labels inside viewport', inside, JSON.stringify(boxes.filter(bx => !(bx.l >= 0 && bx.r <= vp.width && bx.t >= 0 && bx.b <= vp.height))));

    if (label === 'phone') {
      await p.click('#menu-toggle'); await settle(p, 400); await shot(p, label + '-1b-menu');
      ok('phone menu opens', await p.evaluate(() => document.body.classList.contains('menu-open')));
      await p.click('#scrim', { position: { x: 370, y: 500 } }); await settle(p, 300);
    }

    // StarCloud → Systems via label
    await p.click('.label[data-system="paraclete"]'); await settle(p, 900);
    ok(label + ' label click opens Paraclete 1 sectors', location(p) === '#/systems/paraclete');
    await shot(p, label + '-2-sectors');
    ok(label + ' no overflow on sectors', !(await overflow(p)));

    await p.click('[data-go="/systems/paraclete/pending"]'); await settle(p, 700);
    ok(label + ' pending records list shown', await p.locator('.row').count() === 4);
    await shot(p, label + '-3-records');

    await p.click('[data-go="/systems/paraclete/pending/okonkwo-reyes"]'); await settle(p, 700);
    ok(label + ' household record opens', await p.locator('.panel-head h1').innerText().then(t => t.includes('Okonkwo-Reyes')));
    ok(label + ' no overflow on record', !(await overflow(p)));
    await shot(p, label + '-4-record');

    // workflow: draft memo → mark reviewed → illustration → signed → submit
    await p.click('[data-act="memo"]'); await p.waitForSelector('[data-act="advance"][data-to="2"]', { timeout: 15000 });
    ok(label + ' memo drafted (simulated AI)', (await p.locator('.memo').innerText()).includes('Suitability memo'));
    await shot(p, label + '-5-memo');
    await p.click('[data-act="advance"][data-to="2"]'); await settle(p, 400);
    await p.click('[data-act="illustrate"]'); await settle(p, 400);
    ok(label + ' illustration table rendered', await p.locator('.illus tbody tr').count() === 10);
    await p.click('[data-act="advance"][data-to="3"]'); await settle(p, 400);
    await p.click('[data-act="submit"]'); await settle(p, 300);
    ok(label + ' submission asks for confirmation', !(await p.locator('#modal').isHidden()));
    await shot(p, label + '-6-confirm');
    await p.click('#modal-actions .btn-primary');
    await p.waitForSelector('.receipt', { timeout: 10000 });
    ok(label + ' simulated receipt shown', (await p.locator('.receipt').innerText()).includes('SIM-'));
    await shot(p, label + '-7-submitted');
    await p.click('[data-act="advance"][data-to="4"]'); await settle(p, 400);
    ok(label + ' case reaches Issued', (await p.locator('.step.is-current').textContent()).includes('Issued'));

    // breadcrumb back
    await p.click('.crumb-back'); await settle(p, 500);
    ok(label + ' breadcrumb back to records', location(p) === '#/systems/paraclete/pending');

    // today (attention) view
    await p.click('[data-nav="/today"]').catch(async () => { await p.click('#menu-toggle'); await p.click('[data-nav="/today"]'); });
    await settle(p, 700);
    ok(label + ' today shows 3 reviews', await p.locator('.review:not(.is-done)').count() === 3);
    ok(label + ' grouped AUM 2 + Insurance 1', await p.evaluate(() => [...document.querySelectorAll('.group-title')].map(g => g.textContent.replace(/\s+/g, ' ').trim()).join('|')).then(t => /AUM\s*2 open.*Insurance\s*1 open/.test(t)));
    await shot(p, label + '-8-attention');
    await p.click('[data-id="rv-ins-1"][data-outcome="approve"]'); await settle(p, 500);
    ok(label + ' badge drops to 2', (await p.locator('#attention-badge').innerText()) === '2');
    await p.click('[data-nav="/"]').catch(async () => { await p.click('#menu-toggle'); await p.click('[data-nav="/"]'); }); await settle(p, 900);
    ok(label + ' servicing label no longer amber', !(await p.locator('.label[data-system="servicing"]').evaluate(e => e.classList.contains('is-amber'))));
    await shot(p, label + '-9-after-approve');

    // conversation
    await p.fill('#convo-input', 'what needs attention'); await p.press('#convo-input', 'Enter');
    await p.waitForSelector('.msg.nigel:not(.thinking)', { timeout: 5000 }); await settle(p, 400);
    ok(label + ' convo answers attention query', (await p.locator('.msg.nigel').last().innerText()).includes('2 items need attention'));
    await shot(p, label + '-10-convo');
    await p.fill('#convo-input', 'open AUM'); await p.press('#convo-input', 'Enter'); await settle(p, 1800);
    ok(label + ' convo can navigate (AUM → Investments)', location(p) === '#/systems/investments');
    ok(label + ' no overflow with convo open', !(await overflow(p)));

    // escape closes the conversation log first, then goes up one level
    await p.keyboard.press('Escape'); await settle(p, 200);
    ok(label + ' Escape closes convo log', await p.locator('#convo-log').isHidden());
    await p.keyboard.press('Escape'); await settle(p, 400);
    ok(label + ' Escape goes up one level', location(p) === '#/');
    await p.close();
  }
  await b.close();
  function location(p) { return p.url().slice(p.url().indexOf('#')); }
  ok('no page errors', errs.length === 0, errs.join(' | '));
  console.log(results.join('\n'));
  console.log('screenshots in ' + OUT);
  process.exit(results.some(r => r.startsWith('FAIL')) ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
