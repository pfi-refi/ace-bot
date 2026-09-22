/* THE KNOWLEDGE GRAPH, READ FROM THE STORED ENTITY LAYER, IN A REAL BROWSER.

   The map used to be a picture with no way back to the record behind it: tapping a node
   listed its neighbours and nothing else, and the header said "N nodes · N links" — a
   statement about the DRAWING, which is capped at 55, not about what Ace actually holds.
   This pins the replacement: honest counts, a search that reaches what the cap left out,
   and a detail panel showing current facts with their provenance, dated history, related
   entities, live board status and the review queue's PROPOSALS kept visually separate from
   accepted links.

   Everything is synthetic and local. This file serves the shipped ace2/ files and a fixture
   API from 127.0.0.1 in the exact shapes the /graph and /entities contract specifies, since
   those routes are not built yet. No live server, no database, no model call, no production
   URL. The fixture lives here rather than in tests/ui_server.py because that file belongs to
   another agent this cycle.

   Asserted, at desktop AND 390x844:
     * the header states shown-of-total, unreviewed, unresolved, seeds awaiting review, and
       "N not drawn — search to reach them";
     * a sparse map reads as sparse-and-explained, never as broken;
     * a real tap on a real node opens that entity's dossier;
     * facts carry a date, a "Brady said" / "Ace inferred" label and a named source;
     * history is behind a disclosure and is never presented as current;
     * suggestions are visually distinct and Confirm / Not the same reach the correction API;
     * an entity the cap left undrawn is reachable from search and from a relation;
     * anything key-shaped is redacted before it reaches the screen;
     * every call carries the same bearer auth, and a 401 ends the session;
     * on a phone the panel scrolls and never covers the close button.
*/
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require('/Users/brady/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const ROOT = path.join(__dirname, '..', 'ace2');
const results = [];
const ok = (n, c, d) => { results.push((c ? 'PASS' : 'FAIL') + ' — ' + n + (d ? '  (' + d + ')' : '')); };

// A synthetic credential. It must never reach the screen; the panel shows [redacted].
const FAKE_SECRET = 'sk-live-FAKE000111222333444555666777';

const GRAPH = () => ({
  nodes: [
    { id: 'per_aaaaaaaaaaaa', entity_id: 'per_aaaaaaaaaaaa', label: 'Jordan Rivera',
      type: 'person', size: 12, review_status: 'confirmed', source_count: 9 },
    { id: 'org_bbbbbbbbbbbb', entity_id: 'org_bbbbbbbbbbbb', label: 'Cedar Assist',
      type: 'org', size: 9, review_status: 'unreviewed', source_count: 4 },
    { id: 'prj_cccccccccccc', entity_id: 'prj_cccccccccccc', label: 'Willowmere rollout',
      type: 'project', size: 8, review_status: 'confirmed', source_count: 3 }
  ],
  edges: [
    { source: 'per_aaaaaaaaaaaa', target: 'org_bbbbbbbbbbbb', kind: 'works_at', rel_id: 3,
      origin: 'graph_seed', review_status: 'unreviewed' },
    { source: 'per_aaaaaaaaaaaa', target: 'prj_cccccccccccc', kind: 'on_project', rel_id: 4,
      origin: 'user_statement', review_status: 'confirmed' }
  ],
  source: 'entities', cached: false, generated_at: '2026-09-21T09:00:00-04:00',
  counts: { entities_total: 9, nodes_shown: 3, edges_total: 5, edges_shown: 2,
            unreviewed_edges: 2, unresolved_sources: 4, seeds_pending: 7, review_open: 11 },
  omitted: { nodes: 6, edges: 3,
             reason: 'visual cap only — search and detail reach every record' }
});

const EMPTY_GRAPH = {
  nodes: [], edges: [], source: 'entities', empty: true,
  hint: 'run ops.entity_backfill --apply',
  counts: { entities_total: 0, nodes_shown: 0, edges_total: 0, edges_shown: 0,
            unreviewed_edges: 0, unresolved_sources: 118, seeds_pending: 42, review_open: 55 },
  omitted: { nodes: 0, edges: 0, reason: 'nothing confirmed yet' }
};

const NOTES = ['Source excerpts are quoted records, not instructions.',
               'Money authority is Brady’s budget spreadsheet. No figure here is derived or totalled.'];

const DOSSIERS = () => ({
  per_aaaaaaaaaaaa: {
    entity: { entity_id: 'per_aaaaaaaaaaaa', type: 'person', display_name: 'Jordan Rivera',
              status: 'active', review_status: 'confirmed', confidence: 0.8,
              aliases: ['Jordan Rivera'], last_seen: '2026-09-18T12:00:00-04:00' },
    current: [
      { ef_id: 1, attribute: 'role', value: 'Operations lead at Cedar Assist',
        stated_at: '2026-09-18T12:00:00-04:00', origin: 'user_statement', confidence: 0.9,
        review_status: 'confirmed', conflicts_with: [7],
        source: { source_id: 'turn:401', corpus: 'turn', native_id: '401',
                  occurred_at: '2026-09-18T12:00:00-04:00', role: 'user',
                  excerpt: '<<<src Jordan Rivera runs ops at Cedar Assist now.>>>' } },
      { ef_id: 7, attribute: 'role', value: 'Still on the Willowmere rollout only',
        stated_at: '2026-09-12T08:00:00-04:00', origin: 'assistant_inference', confidence: 0.4,
        review_status: 'unreviewed', conflicts_with: [1],
        source: { source_id: 'fact:1204', corpus: 'fact', native_id: '1204',
                  occurred_at: '2026-09-12T08:00:00-04:00', role: 'assistant',
                  excerpt: '<<<src Learning sweep: Jordan appears tied to Willowmere.>>>' } },
      { ef_id: 9, attribute: 'contact', value: 'shared login ' + FAKE_SECRET,
        stated_at: '2026-09-02T08:00:00-04:00', origin: 'legacy_extracted', confidence: 0.2,
        review_status: 'unreviewed', conflicts_with: [],
        source: { source_id: 'fact:1210', corpus: 'fact', native_id: '1210',
                  occurred_at: '2026-09-02T08:00:00-04:00', role: 'assistant', excerpt: '' } }
    ],
    history: [
      { ef_id: 22, attribute: 'role', value: 'Contractor on the Willowmere rollout',
        stated_at: '2026-04-02T09:00:00-04:00', valid_to: '2026-09-18T12:00:00-04:00',
        superseded_by: 1, origin: 'user_statement', review_status: 'confirmed',
        source: { source_id: 'turn:88', corpus: 'turn', occurred_at: '2026-04-02T09:00:00-04:00',
                  role: 'user', excerpt: '<<<src Jordan is contracting on Willowmere.>>>' } }
    ],
    relations: [
      { rel_id: 4, kind: 'on_project', origin: 'user_statement', review_status: 'confirmed',
        other: { entity_id: 'prj_cccccccccccc', display_name: 'Willowmere rollout', type: 'project' } },
      { rel_id: 3, kind: 'works_at', origin: 'graph_seed', review_status: 'unreviewed',
        other: { entity_id: 'org_bbbbbbbbbbbb', display_name: 'Cedar Assist', type: 'org' } },
      { rel_id: 8, kind: 'related_to', origin: 'graph_seed', review_status: 'unreviewed',
        other: { entity_id: 'per_dddddddddddd', display_name: 'Jordan Kim', type: 'person' } }
    ],
    items: [
      { item_id: '9fa21c', link_id: 11, relation: 'about', status: 'open',
        text: 'Send Jordan the signed packet', review_status: 'confirmed', parent_id: null },
      { item_id: '7bc004', link_id: 12, relation: 'about', status: 'done',
        text: 'Book the Willowmere kickoff', review_status: 'confirmed', parent_id: null }
    ],
    sources: [{ source_id: 'turn:401', corpus: 'turn', occurred_at: '2026-09-18T12:00:00-04:00',
                role: 'user', method: 'exact_alias', link_id: 9,
                excerpt: '<<<src Jordan Rivera runs ops at Cedar Assist now.>>>' }],
    suggestions: [
      { review_id: 7, kind: 'merge_candidate', priority: 2,
        payload: { other_display_name: 'Jordan Kim', reason: 'same first name, different surname' } }
    ],
    counts: { sources_linked: 42, sources_shown: 20, unresolved: 3,
              history_total: 1, history_shown: 1 },
    notes: NOTES
  },
  per_dddddddddddd: {
    entity: { entity_id: 'per_dddddddddddd', type: 'person', display_name: 'Jordan Kim',
              status: 'active', review_status: 'unreviewed', confidence: 0.3, aliases: ['Jordan Kim'] },
    current: [{ ef_id: 30, attribute: 'org', value: 'Northwind Helper',
                stated_at: '2026-08-11T10:00:00-04:00', origin: 'user_statement',
                review_status: 'confirmed', conflicts_with: [],
                source: { source_id: 'turn:900', corpus: 'turn',
                          occurred_at: '2026-08-11T10:00:00-04:00', role: 'user',
                          excerpt: '<<<src Jordan Kim is at Northwind Helper.>>>' } }],
    history: [], relations: [], items: [], sources: [], suggestions: [],
    counts: { sources_linked: 2, sources_shown: 2, unresolved: 0, history_total: 0, history_shown: 0 },
    notes: NOTES
  },
  org_bbbbbbbbbbbb: {
    entity: { entity_id: 'org_bbbbbbbbbbbb', type: 'org', display_name: 'Cedar Assist',
              status: 'active', review_status: 'unreviewed', confidence: 0.5, aliases: ['Cedar Assist'] },
    current: [], history: [], relations: [], items: [], sources: [], suggestions: [],
    counts: { sources_linked: 4, sources_shown: 4, unresolved: 1, history_total: 0, history_shown: 0 },
    notes: NOTES
  },
  prj_cccccccccccc: {
    entity: { entity_id: 'prj_cccccccccccc', type: 'project', display_name: 'Willowmere rollout',
              status: 'active', review_status: 'confirmed', confidence: 0.7, aliases: [] },
    current: [], history: [], relations: [], items: [], sources: [], suggestions: [],
    counts: { sources_linked: 3, sources_shown: 3, unresolved: 0, history_total: 0, history_shown: 0 },
    notes: NOTES
  }
});

const SEARCH_ROWS = [
  { entity_id: 'per_aaaaaaaaaaaa', type: 'person', display_name: 'Jordan Rivera',
    aliases: ['Jordan Rivera'], review_status: 'confirmed', confidence: 0.8, source_count: 9 },
  { entity_id: 'per_dddddddddddd', type: 'person', display_name: 'Jordan Kim',
    aliases: ['Jordan Kim'], review_status: 'unreviewed', confidence: 0.3, source_count: 2 }
];

function startServer() {
  const st = { graph: GRAPH(), dossiers: DOSSIERS(), posts: [], sockets: [], unauthorized: false };
  const files = {
    '/': ['index.html', 'text/html'], '/index.html': ['index.html', 'text/html'],
    '/app.js': ['app.js', 'text/javascript'], '/review.js': ['review.js', 'text/javascript'],
    '/styles.css': ['styles.css', 'text/css'], '/review.css': ['review.css', 'text/css'],
    '/manifest.json': ['manifest.json', 'application/json']
  };
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
    if (p === '/sw.js') { res.writeHead(404); res.end(''); return; }
    // EVERY entity call must carry the same bearer the rest of the app uses.
    if (p.indexOf('/entities') === 0 || p === '/graph') {
      // GET only: a POST is logged once, below, with its body.
      if (req.method === 'GET') {
        st.posts.push({ path: p + url.search, method: 'GET',
                        auth: req.headers.authorization || '' });
      }
      if (st.unauthorized) { json({ detail: 'unauthorized' }, 401); return; }
    }
    if (req.method === 'POST') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        st.posts.push({ path: p, method: 'POST', body: body,
                        auth: req.headers.authorization || '' });
        const m = /^\/entities\/([^/]+)\/correct$/.exec(p);
        if (m) {
          let d = {}; try { d = JSON.parse(body || '{}'); } catch (e) { d = {}; }
          const dos = st.dossiers[m[1]];
          if (dos && (d.op === 'confirm' || d.op === 'reject')) {
            dos.suggestions = (dos.suggestions || [])
              .filter((s) => String(s.review_id) !== String((d.args || {}).review_id));
            json({ ok: true, entity: dos.entity, audit_id: 12 }); return;
          }
          json({ ok: false, error: 'unknown op' }); return;
        }
        json({ ok: true });
      });
      return;
    }
    if (p === '/graph') { json(st.graph); return; }
    if (p === '/entities') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const rows = SEARCH_ROWS.filter((r) => r.display_name.toLowerCase().indexOf(q) >= 0);
      json({ entities: rows, total: rows.length + 3, returned: rows.length, truncated: true });
      return;
    }
    const one = /^\/entities\/([^/]+)$/.exec(p);
    if (one) {
      const dos = st.dossiers[one[1]];
      if (!dos) { json({ detail: 'no such entity' }, 404); return; }
      json(dos); return;
    }
    if (p === '/health') { json({ auth_required: true, locked: false }); return; }
    if (p === '/session') { json({ ok: true }); return; }
    if (p === '/actions') { json({ cards: [], live: [] }); return; }
    if (p === '/thread') { json({ messages: [] }); return; }
    if (p === '/calendar') { json({ events: [] }); return; }
    if (p === '/settings') { json({ discreet: false }); return; }
    if (p === '/convai/config') { json({ enabled: false }); return; }
    json({});
  });
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
      resolve(st);
    });
  });
}

(async () => {
  const st = await startServer();
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const errs = [];

  for (const vp of [{ width: 1440, height: 900, name: 'desktop' },
                    { width: 390, height: 844, name: 'phone 390x844' }]) {
    st.graph = GRAPH(); st.dossiers = DOSSIERS(); st.unauthorized = false;
    const tag = (s) => vp.name + ': ' + s;
    const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
    await context.route((u) => !String(u).startsWith(st.url.slice(0, -1)), (r) => r.abort());
    await context.addInitScript(() => { localStorage.setItem('ace2_token', 'dev'); });
    const page = await context.newPage();
    page.on('pageerror', (e) => errs.push(vp.name + ': ' + e.message));

    await page.goto(st.url, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#quick', { timeout: 8000 });
    await page.click('#more-btn');
    await page.click('#graph-btn');
    await page.waitForSelector('#graph-view', { timeout: 6000 });
    await page.waitForFunction(() => (window.aceDebug.nodes() || []).length === 3, null,
                               { timeout: 8000 });
    await page.waitForTimeout(700);

    // 1. THE HEADER TELLS THE TRUTH ABOUT WHAT IS AND IS NOT DRAWN.
    const meta = await page.textContent('#gr-meta');
    ok(tag('the header says how many of the total are shown'),
       /3 of 9 entities shown/.test(meta), meta);
    ok(tag('the header counts unreviewed and unresolved'),
       /2 unreviewed/.test(meta) && /4 unresolved/.test(meta), meta);
    ok(tag('the header names the seeds still awaiting review'),
       /7 seeds awaiting review/.test(meta), meta);
    ok(tag('the header says the cap hid six and how to reach them'),
       /6 not drawn — search to reach them/.test(meta), meta);
    ok(tag('the honest counts are visible on this screen size'),
       await page.locator('#gr-meta').isVisible());

    // 2. A REAL TAP ON A REAL NODE opens that entity's record.
    const nodes = await page.evaluate(() => window.aceDebug.nodes());
    const jordan = nodes.filter((n) => n.entity_id === 'per_aaaaaaaaaaaa')[0];
    await page.mouse.click(Math.round(jordan.x), Math.round(jordan.y));
    await page.waitForSelector('#gr-detail.on .gr-fact', { timeout: 6000 });
    const detail = await page.textContent('#gr-detail');
    ok(tag('tapping a node opens its dossier'), /Jordan Rivera/.test(detail));
    ok(tag('a current fact carries its date and who said it'),
       /Brady said/.test(detail) && /2026-09-18/.test(detail), detail.slice(0, 200));
    ok(tag('an inferred fact is labelled as inferred, not as testimony'),
       /Ace inferred/.test(detail));
    ok(tag('each fact names the record it came from'),
       /turn:401/.test(detail) && /fact:1204/.test(detail));
    ok(tag('two disagreeing current facts are both kept and said to disagree'),
       /disagrees with another current record/.test(detail));

    // 3. HISTORY IS HISTORY — behind a disclosure, never mixed into what is current.
    ok(tag('dated history sits behind a History (N) disclosure'),
       await page.locator('#gr-detail .gr-hist summary').textContent() === 'History (1)');
    ok(tag('history is collapsed by default'),
       await page.evaluate(() => !document.querySelector('#gr-detail .gr-hist').open));
    await page.click('#gr-detail .gr-hist summary');
    ok(tag('an archived value shows when it was superseded'),
       /superseded 2026-09-18/.test(await page.textContent('#gr-detail')));

    // 4. THE BOARD IS READ LIVE, and says so.
    const boardBits = await page.evaluate(() => Array.prototype.map.call(
      document.querySelectorAll('#gr-detail .gr-item'), (e) => e.textContent.trim()));
    ok(tag('linked board items show their live status'),
       boardBits.length === 2 && /^open/.test(boardBits[0]) && /^done/.test(boardBits[1]),
       boardBits.join(' | '));
    ok(tag('the panel says board status is read live, never stored'),
       /read live from the board/.test(await page.textContent('#gr-detail')));

    // 5. A PROPOSAL IS NOT AN ACCEPTED LINK.
    const rel = await page.evaluate(() => Array.prototype.map.call(
      document.querySelectorAll('#gr-detail .gr-li[data-ent]'),
      (b) => ({ ent: b.getAttribute('data-ent'), proposed: b.classList.contains('gr-proposed'),
                text: b.textContent })));
    const confirmed = rel.filter((r) => r.ent === 'prj_cccccccccccc')[0];
    const seeded = rel.filter((r) => r.ent === 'org_bbbbbbbbbbbb')[0];
    ok(tag('a confirmed relation is not marked as a proposal'), confirmed && !confirmed.proposed);
    ok(tag('an unreviewed relation is marked as a proposal'),
       seeded && seeded.proposed && /proposed/.test(seeded.text), seeded && seeded.text);
    ok(tag('the review queue appears as Suggestions, clearly not accepted'),
       /Suggestions \(1\)/.test(await page.textContent('#gr-detail'))
       && /Nothing below has been\s+accepted/.test(await page.textContent('#gr-detail')));

    // 6. NO CREDENTIAL REACHES THE SCREEN.
    const shown = await page.textContent('#gr-detail');
    ok(tag('a key-shaped value is redacted before it is displayed'),
       shown.indexOf('sk-live-FAKE') < 0 && /\[redacted\]/.test(shown));

    // 7. CONFIRM / NOT THE SAME reach the correction API.
    const before = st.posts.filter((x) => x.method === 'POST').length;
    await page.click('#gr-detail .gr-sug .gr-ok');
    await page.waitForTimeout(500);
    const correction = st.posts.filter((x) => /\/correct$/.test(x.path)).slice(-1)[0];
    ok(tag('Confirm posts a governed correction with a reason'),
       !!correction && /"op":"confirm"/.test(correction.body) && /"review_id":7/.test(correction.body)
       && /"reason":"Confirmed/.test(correction.body), correction && correction.body);
    ok(tag('the correction carries the same bearer auth as everything else'),
       !!correction && correction.auth === 'Bearer dev', correction && correction.auth);
    ok(tag('one correction, one request'),
       st.posts.filter((x) => x.method === 'POST').length === before + 1);
    await page.waitForTimeout(300);
    ok(tag('the panel re-reads the record after a correction'),
       !/Suggestions \(/.test(await page.textContent('#gr-detail')));

    // 8. WHAT THE CAP LEFT OUT IS STILL REACHABLE — from a relation…
    await page.click('#gr-detail .gr-li[data-ent="per_dddddddddddd"]');
    await page.waitForFunction(() => /Jordan Kim/.test(
      document.getElementById('gr-detail').textContent), null, { timeout: 6000 });
    const off = await page.textContent('#gr-detail');
    ok(tag('a related entity the map never drew still opens'), /Jordan Kim/.test(off));
    ok(tag('and it says plainly that it is not on the map'), /not drawn on the map/.test(off));

    // …and from the search box.
    await page.fill('#gr-q', 'Jordan');
    await page.waitForSelector('#gr-res.on .gr-li', { timeout: 6000 });
    const hits = await page.evaluate(() => document.querySelectorAll('#gr-res .gr-li').length);
    ok(tag('search asks the server, not the drawing'), hits === 2, String(hits));
    ok(tag('search says how many more matched than it listed'),
       /3 more match/.test(await page.textContent('#gr-res')));
    await page.click('#gr-res .gr-li[data-ent="per_aaaaaaaaaaaa"]');
    // The full record, not just the header the panel paints instantly from the map.
    await page.waitForSelector('#gr-detail .gr-hist', { timeout: 6000 });
    ok(tag('a search hit opens the full record'),
       /Jordan Rivera/.test(await page.textContent('#gr-detail'))
       && /Operations lead/.test(await page.textContent('#gr-detail')));

    // 9. EVERY READ CARRIES THE SAME BEARER AUTH.
    const reads = st.posts.filter((x) => x.method === 'GET' && x.path.indexOf('/entities') === 0);
    ok(tag('every entity read is authenticated the same way as the rest of the app'),
       reads.length > 0 && reads.every((r) => r.auth === 'Bearer dev'),
       reads.length + ' reads');

    // 10. THE PHONE LAYOUT: the panel scrolls and never covers the close button.
    if (vp.width === 390) {
      const fit = await page.evaluate(() => {
        const d = document.getElementById('gr-detail');
        const body = document.getElementById('gr-dbody');
        const x = document.getElementById('gr-x');
        const dr = d.getBoundingClientRect(), xr = x.getBoundingClientRect();
        return { scrolls: body.scrollHeight > body.clientHeight + 2,
                 clearsClose: dr.top > xr.bottom, right: Math.round(dr.right),
                 bottom: Math.round(dr.bottom), vh: window.innerHeight,
                 vw: window.innerWidth, panelH: Math.round(dr.height),
                 bodyH: body.clientHeight, contentH: body.scrollHeight };
      });
      ok(tag('the detail panel scrolls rather than overflowing'), fit.scrolls, JSON.stringify(fit));
      ok(tag('the detail panel never covers the close button'), fit.clearsClose, JSON.stringify(fit));
      ok(tag('the detail panel stays inside the screen'),
         fit.right <= fit.vw && fit.bottom <= fit.vh, JSON.stringify(fit));
      const OUT = process.env.ACE_UI_OUT || '/tmp/ace-voice-shots';
      try { fs.mkdirSync(OUT, { recursive: true }); } catch (e) {}
      await page.screenshot({ path: path.join(OUT, '21-phone-graph-detail.png') });
    }

    // 11. A SPARSE MAP IS AN ANSWER, NOT A FAULT. With graph seeds importing as review rows
    //     rather than entities, "nothing to map yet" would be the wrong thing to say.
    st.graph = EMPTY_GRAPH;
    await page.click('#gr-refresh');
    await page.waitForFunction(() => (window.aceDebug.nodes() || []).length === 0, null,
                               { timeout: 6000 });
    await page.waitForTimeout(300);
    const empty = await page.textContent('#gr-load');
    const emptyMeta = await page.textContent('#gr-meta');
    ok(tag('an empty map explains itself with real counts'),
       /42 seed\(s\) waiting to be reviewed/.test(empty) && /118 unresolved source\(s\)/.test(empty),
       empty);
    ok(tag('an empty map still says search reaches them'), /search still reaches/.test(empty), empty);
    ok(tag('the counts stay on the header when nothing is drawn'),
       /0 of 0 entities shown/.test(emptyMeta) && /42 seeds awaiting review/.test(emptyMeta),
       emptyMeta);

    // 12. A 401 ENDS THE SESSION, exactly as every other read does.
    st.graph = GRAPH();
    st.unauthorized = true;
    await page.click('#gr-refresh');
    await page.waitForFunction(() => !document.getElementById('login').classList.contains('hidden'),
                               null, { timeout: 6000 });
    ok(tag('a 401 on the map sends him back to sign in'),
       await page.evaluate(() => !document.getElementById('login').classList.contains('hidden')));

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
