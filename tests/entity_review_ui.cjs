/* THE REVIEW QUEUE AND THE SOURCE SEARCH — THE TWO SURFACES THAT TURN AN INDEX INTO MEMORY.

   Run over the real corpus the migration promoted 33 entities and linked 993 of 8649
   sources: the identity bar working exactly as designed, because the names Brady says most
   are bare first names and a bare first name must never resolve on its own. What that
   leaves is ~1030 proposals and ~4500 unmatched sources. Before these panels none of it
   could be opened, and graph seeds — which create no entity at all — had no entrypoint
   whatsoever. A header saying "1030 in the review queue" with no way in is a number shaped
   like an answer.

   Everything here is synthetic and local: this file serves the shipped ace2/ files and a
   fixture API from 127.0.0.1 in the shapes ace2/backend/main.py really returns (read from
   entity_context.review_queue / apply_review_action / search_sources, not guessed). No live
   server, no database, no model call, no production URL. The names are invented.

   Asserted, at desktop AND 390x844:
     * the queue opens from the graph header, and the header's count is the way in;
     * it lists every kind, in the server's priority order, unchanged;
     * filtering by kind asks the server for that kind;
     * confirm / reject / dismiss each issue exactly ONE post to
       /entities/review/{id} and update their row in place, with no reload;
     * confirming a graph_seed offers the type choice and posts the human's answer, not the
       model's claim, and says that this is the only way a seed becomes a record;
     * an ambiguous name refuses to be confirmed without a record, and the server's refusal
       is shown rather than swallowed;
     * source search reaches unassigned, ambiguous AND excluded rows;
     * an excluded row shows its reason and no body;
     * proposals are visually distinct from accepted links;
     * "not indexed yet" and "the index is down" do not look the same;
     * a planted fake credential never reaches the screen.
*/
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require('/Users/brady/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const ROOT = path.join(__dirname, '..', 'ace2');
const results = [];
const ok = (n, c, d) => { results.push((c ? 'PASS' : 'FAIL') + ' — ' + n + (d ? '  (' + d + ')' : '')); };

// Planted, synthetic, and it must never be rendered anywhere.
const FAKE_SECRET = 'sk-live-FAKE000111222333444555666777';

const INDEX_NOTE = 'A search index is not a verified dossier: these are records Ace has '
                 + 'indexed, not claims he has checked.';
const PROPOSAL_NOTE = 'These are PROPOSALS. Nothing here has been accepted, and confirming '
                    + 'one is the only way a graph seed becomes a record.';

// Priorities mirror entities.review_priority: ambiguous_name/name_collision 1, a heavily
// evidenced unpromoted_name 1, merge_candidate 3, unassigned_source 5, graph_seed 6 last.
const REVIEWS = () => ([
  { review_id: 11, kind: 'ambiguous_name', subject_key: 'jordan', priority: 1, state: 'open',
    created_at: '2026-09-21T10:00:00-04:00', resolved_at: null, resolution: null,
    payload: { alias: 'Jordan', alias_norm: 'jordan',
               entity_ids: ['per_aaaaaaaaaaaa', 'per_dddddddddddd'],
               why: 'this name matches 2 known entities, or is a first name that has never '
                  + 'been confirmed; no link was written',
               example_source_id: 'turn:4102' } },
  { review_id: 12, kind: 'unpromoted_name', subject_key: 'wendell brooke', priority: 1,
    state: 'open', created_at: '2026-09-21T10:01:00-04:00', resolved_at: null, resolution: null,
    payload: { name: 'Wendell Brooke', alias_norm: 'wendell brooke', entity_ids: [],
               claimed_type: 'unknown', sources: ['turn:11', 'turn:12', 'fact:90'],
               source_count: 708, person_context_sources: 640, org_context_sources: 2,
               why: 'seen in 708 sources but only ever as a bare first name; the bar '
                  + 'refuses to promote it without a human' } },
  { review_id: 13, kind: 'name_collision', subject_key: 'brooke', priority: 1, state: 'open',
    created_at: '2026-09-21T10:02:00-04:00', resolved_at: null, resolution: null,
    payload: { alias_norm: 'brooke', entity_ids: ['per_aaaaaaaaaaaa', 'per_dddddddddddd'],
               why: '2 active entities answer to this name; it resolves to none of them '
                  + 'until a human says which' } },
  { review_id: 14, kind: 'merge_candidate', subject_key: 'per_a|per_d', priority: 3,
    state: 'open', created_at: '2026-09-21T10:03:00-04:00', resolved_at: null, resolution: null,
    payload: { entity_ids: ['per_aaaaaaaaaaaa', 'per_dddddddddddd'],
               names: ['Jordan Rivera', 'Jordan Riviera'],
               similarity: { last: 0.93, first: 1.0 },
               why: 'these two names look similar; THIS IS A SUGGESTION ONLY — nothing was '
                  + 'merged and no link was written' } },
  { review_id: 15, kind: 'unassigned_source', subject_key: 'turn:5510', priority: 5,
    state: 'open', created_at: '2026-09-21T10:04:00-04:00', resolved_at: null, resolution: null,
    payload: { source_id: 'turn:5510', corpus: 'turn', entity_ids: [],
               candidate_names: ['Willowmere Rollout', 'Cedar Assist'],
               why: 'this source names something, and nothing here matched an entity' } },
  { review_id: 16, kind: 'graph_seed', subject_key: 'cedar assist', priority: 6,
    state: 'open', created_at: '2026-09-21T10:05:00-04:00', resolved_at: null, resolution: null,
    payload: { label: 'Cedar Assist', claimed_type: 'person', claimed_type_is_accepted: false,
               entity_ids: [], source_id: 'summary:87',
               edges: [{ source: 'Cedar Assist', target: 'Jordan Rivera', kind: 'works_at' }],
               why: 'a model-generated graph node. Its type is CLAIMED, not accepted — the '
                  + 'live cache types organizations as people. It becomes an entity only '
                  + 'when a human confirms it.' } }
]);

const BY_KIND = { ambiguous_name: 26, unpromoted_name: 400, name_collision: 1,
                  merge_candidate: 9, unassigned_source: 555, graph_seed: 48 };

const SOURCES = [
  { source_id: 'turn:5510', corpus: 'turn', native_id: '5510',
    occurred_at: '2026-09-14T11:00:00-04:00', role: 'user', source_class: 'user_statement',
    status: 'unassigned', excluded_reason: null, char_len: 84,
    excerpt: '<<<src Met the Willowmere crew about the rollout schedule.>>>' },
  { source_id: 'turn:4102', corpus: 'turn', native_id: '4102',
    occurred_at: '2026-09-12T09:00:00-04:00', role: 'user', source_class: 'user_statement',
    status: 'ambiguous', excluded_reason: null, char_len: 40,
    excerpt: '<<<src Jordan called about the packet.>>>' },
  // The row that proves the withholding: a settings record whose body is never rendered.
  { source_id: 'summary:1408', corpus: 'summary', native_id: '1408',
    occurred_at: '2026-09-10T02:00:00-04:00', role: 'system',
    source_class: 'internal_metadata', status: 'excluded',
    excluded_reason: 'operational telemetry, not personal knowledge', char_len: 120,
    excerpt: '',
    excerpt_withheld: 'settings / telemetry or synthetic test text is never rendered; the '
                    + 'row is listed so nothing is hidden' },
  // …and one that tries to smuggle a credential through a normal excerpt.
  { source_id: 'fact:1210', corpus: 'fact', native_id: '1210',
    occurred_at: '2026-09-02T08:00:00-04:00', role: 'assistant',
    source_class: 'legacy_extracted', status: 'indexed', excluded_reason: null, char_len: 60,
    excerpt: '<<<src shared login ' + FAKE_SECRET + ' for the portal>>>' }
];

function startServer() {
  const st = { reviews: REVIEWS(), posts: [], gets: [], sockets: [], mode: 'ready' };
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

    const memoryRoute = p.indexOf('/entities') === 0 || p.indexOf('/sources') === 0
                     || p === '/graph';
    if (memoryRoute && req.method === 'GET') {
      st.gets.push({ path: p + url.search, auth: req.headers.authorization || '' });
      // A DB outage is a 503; "not migrated yet" is a 200 with index_state 'absent'.
      if (st.mode === 'unavailable') {
        json({ detail: 'The memory index could not be reached. This is NOT an empty '
                     + 'result — nothing is being claimed about what is or is not on '
                     + 'file.' }, 503);
        return;
      }
    }
    if (req.method === 'POST') {
      let raw = '';
      req.on('data', (c) => { raw += c; });
      req.on('end', () => {
        st.posts.push({ path: p, body: raw, auth: req.headers.authorization || '' });
        const m = /^\/entities\/review\/(\d+)$/.exec(p);
        if (m) {
          let d = {}; try { d = JSON.parse(raw || '{}'); } catch (e) { d = {}; }
          const row = st.reviews.filter((r) => String(r.review_id) === m[1])[0];
          const args = d.args || {};
          if (!row) { json({ ok: false, error: 'No review row ' + m[1] + '.' }, 400); return; }
          if (d.action !== 'confirm') {
            row.state = 'dismissed';
            json({ ok: true, review_id: row.review_id, action: d.action, state: 'dismissed',
                   applied: { kind: row.kind, state: 'dismissed', changed: true,
                              note: 'this is a permanent tombstone: a backfill replay will '
                                  + 'not re-open it' },
                   audit_id: 51, notes: [INDEX_NOTE] });
            return;
          }
          // The server's real refusals, reproduced: confirming an ambiguous name without
          // naming a record writes nothing.
          if (['ambiguous_name', 'unassigned_source', 'low_confidence_link']
                .indexOf(row.kind) >= 0 && !args.entity_id) {
            json({ ok: false, error: 'Confirming this needs args.entity_id — WHICH record '
                                   + 'the source belongs to. Nothing was guessed and '
                                   + 'nothing written.' }, 400);
            return;
          }
          row.state = 'resolved';
          const applied = { kind: row.kind, state: 'resolved', review_id: row.review_id };
          if (row.kind === 'graph_seed') {
            Object.assign(applied, { entity_id: 'org_eeeeeeeeeeee', entity_created: true,
              claimed_type: row.payload.claimed_type, claimed_type_accepted: false,
              type_used: args.type || 'person', relations_proposed: 1,
              note: 'relations from a seed are recorded as UNREVIEWED proposals; confirming '
                  + 'the node does not confirm its edges' });
          } else if (row.kind === 'unpromoted_name') {
            Object.assign(applied, { entity_id: 'per_ffffffffffff', entity_created: true,
                                     type_used: args.type || 'person', sources_linked: 3 });
          } else if (row.kind === 'merge_candidate') {
            Object.assign(applied, { loser: args.loser || 'per_dddddddddddd',
                                     into: args.into });
          } else if (row.kind === 'name_collision') {
            Object.assign(applied, { acknowledged: true, changed: false });
          } else {
            Object.assign(applied, { entity_id: args.entity_id,
                                     source_id: row.payload.source_id
                                             || row.payload.example_source_id, link_id: 77 });
          }
          json({ ok: true, review_id: row.review_id, action: 'confirm', state: 'resolved',
                 applied: applied, audit_id: 52, notes: [INDEX_NOTE] });
          return;
        }
        json({ ok: true });
      });
      return;
    }

    if (p === '/entities/review') {
      const kind = url.searchParams.get('kind') || '';
      const open = st.reviews.filter((r) => r.state === 'open');
      const rows = (kind ? open.filter((r) => r.kind === kind) : open)
        .slice().sort((a, b) => (a.priority - b.priority) || (a.review_id - b.review_id));
      json({ reviews: rows, returned: rows.length,
             total: kind ? (BY_KIND[kind] || rows.length)
                         : Object.keys(BY_KIND).reduce((s, k) => s + BY_KIND[k], 0),
             counts: { total: Object.keys(BY_KIND).reduce((s, k) => s + BY_KIND[k], 0),
                       by_kind: BY_KIND, by_state: { open: 1039, dismissed: 4 } },
             state: 'open', kind: kind || null,
             index_state: st.mode === 'absent' ? 'absent' : 'ready',
             notes: [INDEX_NOTE, PROPOSAL_NOTE] });
      return;
    }
    if (p === '/sources/search') {
      const status = url.searchParams.get('status') || '';
      const rows = status ? SOURCES.filter((s) => s.status === status) : SOURCES;
      json({ sources: rows, total: rows.length, returned: rows.length, truncated: false,
             counts: { by_status: { indexed: 993, unassigned: 4496, ambiguous: 612,
                                    excluded: 2548 },
                       by_class: { user_statement: 2349 }, matching: 8649,
                       unassigned: 4496, ambiguous: 612, excluded: 2548, indexed: 993 },
             index_state: st.mode === 'absent' ? 'absent' : 'ready',
             notes: [INDEX_NOTE].concat(st.mode === 'absent'
               ? ['The entity layer is not migrated in, so there is nothing indexed to '
                  + 'search yet.'] : []) });
      return;
    }
    if (p === '/entities') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const rows = [{ entity_id: 'per_aaaaaaaaaaaa', type: 'person',
                      display_name: 'Jordan Rivera', aliases: ['Jordan Rivera'],
                      review_status: 'confirmed', confidence: 0.8, source_count: 9 }]
        .filter((e) => e.display_name.toLowerCase().indexOf(q) >= 0);
      json({ entities: rows, total: rows.length, returned: rows.length, truncated: false,
             index_state: 'ready' });
      return;
    }
    if (p === '/graph') {
      json({ nodes: [], edges: [], source: 'entities', empty: true,
             counts: { entities_total: 33, nodes_shown: 0, edges_total: 0, edges_shown: 0,
                       unreviewed_edges: 0, unresolved_sources: 4496, seeds_pending: 48,
                       review_open: 1039 },
             omitted: { nodes: 0, edges: 0, reason: 'nothing confirmed yet' } });
      return;
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

const row = (id) => '#ace-review article[data-review="' + id + '"]';

(async () => {
  const st = await startServer();
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const errs = [];

  for (const vp of [{ width: 1440, height: 900, name: 'desktop' },
                    { width: 390, height: 844, name: 'phone 390x844' }]) {
    st.reviews = REVIEWS(); st.posts.length = 0; st.gets.length = 0; st.mode = 'ready';
    const tag = (s) => vp.name + ': ' + s;
    const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
    await context.route((u) => !String(u).startsWith(st.url.slice(0, -1)), (r) => r.abort());
    await context.addInitScript(() => { localStorage.setItem('ace2_token', 'dev'); });
    const page = await context.newPage();
    page.on('pageerror', (e) => errs.push(vp.name + ': ' + e.message));

    await page.goto(st.url, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#quick', { timeout: 8000 });

    // 1. THE COUNT IS THE DOOR. The graph header carries the number and opens the queue.
    await page.click('#more-btn');
    await page.click('#graph-btn');
    await page.waitForSelector('#gr-review', { timeout: 6000 });
    await page.waitForFunction(() => /\d/.test(
      document.getElementById('gr-review').textContent), null, { timeout: 6000 });
    ok(tag('the graph header shows how many proposals are waiting'),
       (await page.textContent('#gr-review')).indexOf('1039') >= 0,
       await page.textContent('#gr-review'));
    await page.click('#gr-review');
    await page.waitForSelector('#ace-review article.rv-prop', { timeout: 6000 });
    ok(tag('the queue opens from the graph'),
       (await page.textContent('#ace-review h2')) === 'Memory review');

    // 2. EVERY KIND, IN THE SERVER'S ORDER, WITH THE HONEST HEADLINE.
    const order = await page.evaluate(() => Array.prototype.map.call(
      document.querySelectorAll('#rv-list article'), (a) => a.dataset.kind));
    ok(tag('every kind is listed, seeds last, in the order the server sent'),
       order.join(',') === 'ambiguous_name,unpromoted_name,name_collision,merge_candidate,'
                         + 'unassigned_source,graph_seed', order.join(','));
    const head = await page.textContent('#ace-review .rv-head');
    ok(tag('the headline says how many are open and that none are accepted'),
       /1039 proposal\(s\) open/.test(head) && /nothing here has been accepted/.test(head),
       head);
    ok(tag('the proposal note from the server is shown'),
       (await page.textContent('#ace-review')).indexOf('Nothing here has been accepted') >= 0);

    // 3. A PROPOSAL IS NOT A RECORD, and it says what confirming would do.
    const look = await page.evaluate(() => {
      const a = document.querySelector('#rv-list article');
      const s = getComputedStyle(a);
      return { cls: a.className, border: s.borderLeftColor, width: s.borderLeftWidth,
               tag: a.querySelector('small').textContent };
    });
    ok(tag('proposals are visually distinct and labelled as proposals'),
       look.cls === 'rv-prop' && /^PROPOSAL/.test(look.tag)
       && look.border === 'rgb(255, 176, 32)' && look.width === '4px',
       JSON.stringify(look));

    // 4. FILTERING BY KIND ASKS THE SERVER FOR THAT KIND.
    const beforeGets = st.gets.length;
    await page.click('#ace-review .rv-filters button[data-kind="graph_seed"]');
    await page.waitForFunction(() => document.querySelectorAll('#rv-list article').length === 1,
                               null, { timeout: 6000 });
    const asked = st.gets.slice(beforeGets).filter((g) => /kind=graph_seed/.test(g.path));
    ok(tag('a kind filter is a server query, not a client-side hide'), asked.length === 1,
       st.gets.slice(beforeGets).map((g) => g.path).join(' | '));
    ok(tag('every queue read carries the same bearer auth'),
       st.gets.length > 0 && st.gets.every((g) => g.auth === 'Bearer dev'));

    // 5. CONFIRMING A SEED: the human sets the type, and the consequence is stated.
    const seed = row(16);
    ok(tag('a seed says confirming it is the only way it becomes a record'),
       /ONLY way a seed becomes a record/.test(await page.textContent(seed + ' .rv-consequence')));
    ok(tag("a seed shows the old map's type as a claim, not as the answer"),
       /claimed: person/.test(await page.textContent(seed)));
    ok(tag('the type is a choice, defaulted to the claim but changeable'),
       await page.locator(seed + ' select.rv-type').count() === 1);
    await page.selectOption(seed + ' select.rv-type', 'org');
    const beforePosts = st.posts.length;
    await page.click(seed + ' button[data-action="confirm"]');
    await page.waitForFunction(() => document.querySelector(
      'article[data-review="16"]').className === 'rv-done', null, { timeout: 6000 });
    const seedPost = st.posts[st.posts.length - 1];
    ok(tag('confirming a seed posts exactly one decision'),
       st.posts.length === beforePosts + 1
       && seedPost.path === '/entities/review/16', JSON.stringify(seedPost && seedPost.path));
    ok(tag("…carrying the human's type, not the model's claim"),
       /"action":"confirm"/.test(seedPost.body) && /"type":"org"/.test(seedPost.body),
       seedPost.body);
    ok(tag('…with the same bearer auth as everything else'), seedPost.auth === 'Bearer dev');
    const seedSaid = await page.textContent(seed + ' .rv-say');
    ok(tag('the row reports what actually happened, in place, with no reload'),
       /Created org_eeeeeeeeeeee as a org/.test(seedSaid)
       && /UNREVIEWED proposals/.test(seedSaid), seedSaid);
    ok(tag('a decided row no longer offers the buttons'),
       await page.locator(seed + ' button').count() === 0);

    // 6. AN AMBIGUOUS NAME REFUSES TO BE GUESSED — and the refusal is shown.
    await page.click('#ace-review .rv-filters button[data-kind=""]');
    await page.waitForSelector(row(11), { timeout: 6000 });
    const beforeRefuse = st.posts.length;
    await page.click(row(11) + ' button[data-action="confirm"]');
    await page.waitForFunction(() => /needs args.entity_id/.test(
      document.querySelector('article[data-review="11"] .rv-say').textContent), null,
      { timeout: 6000 });
    ok(tag('confirming without naming a record is refused, and the reason is shown'),
       st.posts.length === beforeRefuse + 1
       && /Nothing was guessed/.test(await page.textContent(row(11) + ' .rv-say')));
    ok(tag('…and the row stays open so he can answer it'),
       await page.locator(row(11) + ' button[data-action="confirm"]').count() === 1
       && await page.evaluate(() => !document.querySelector(
            'article[data-review="11"] button[data-action="confirm"]').disabled));
    // Naming one of the candidates makes it go through.
    await page.click(row(11) + ' .rv-ctl > div > button');
    ok(tag('choosing a candidate record is reflected before he commits'),
       /^Chosen: per_/.test(await page.textContent(row(11) + ' .rv-picked')),
       await page.textContent(row(11) + ' .rv-picked'));
    await page.click(row(11) + ' button[data-action="confirm"]');
    await page.waitForFunction(() => document.querySelector(
      'article[data-review="11"]').className === 'rv-done', null, { timeout: 6000 });
    const named = st.posts[st.posts.length - 1];
    ok(tag('…and the confirmation names the record he picked'),
       /"entity_id":"per_aaaaaaaaaaaa"/.test(named.body), named.body);

    // 7. REJECT AND DISMISS: one post each, and the tombstone is described honestly.
    const beforeReject = st.posts.length;
    await page.click(row(13) + ' button[data-action="reject"]');
    await page.waitForFunction(() => document.querySelector(
      'article[data-review="13"]').className === 'rv-closed', null, { timeout: 6000 });
    const rejected = st.posts[st.posts.length - 1];
    ok(tag('reject posts exactly one decision to the review endpoint'),
       st.posts.length === beforeReject + 1 && rejected.path === '/entities/review/13'
       && /"action":"reject"/.test(rejected.body), rejected.body);
    ok(tag('reject says the tombstone is permanent and forward-looking'),
       /permanent tombstone/.test(await page.textContent(row(13) + ' .rv-say'))
       && /not resolve for new records/.test(await page.textContent(row(13) + ' .rv-say')),
       await page.textContent(row(13) + ' .rv-say'));
    const beforeDismiss = st.posts.length;
    await page.click(row(15) + ' button[data-action="dismiss"]');
    await page.waitForFunction(() => document.querySelector(
      'article[data-review="15"]').className === 'rv-closed', null, { timeout: 6000 });
    ok(tag('dismiss posts exactly one decision'),
       st.posts.length === beforeDismiss + 1
       && /"action":"dismiss"/.test(st.posts[st.posts.length - 1].body));
    ok(tag('the open count comes down as he works, without a reload'),
       /103[0-9]* proposal\(s\) open/.test(await page.textContent('#ace-review .rv-head'))
       && (await page.textContent('#ace-review .rv-head')).indexOf('1039 proposal') < 0,
       await page.textContent('#ace-review .rv-head'));

    // 8. A MERGE SUGGESTION IS A SUGGESTION, and the human picks the survivor.
    ok(tag('a merge suggestion says nothing was merged'),
       /nothing was merged/.test(await page.textContent(row(14))));
    ok(tag('…and asks which record survives'),
       await page.locator(row(14) + ' select.rv-into').count() === 1);

    // 9. THE SOURCE SEARCH reaches what has no entity to be found by.
    await page.click('#ace-review .rv-jump button');
    await page.waitForSelector('#rv-src-out article', { timeout: 6000 });
    ok(tag('the source search opens from the review panel'),
       (await page.textContent('#ace-review h2')) === 'Find a source');
    const counts = await page.textContent('#rv-src-counts');
    ok(tag('it counts unassigned, ambiguous and excluded rows honestly'),
       /unassigned 4496/.test(counts) && /ambiguous 612/.test(counts)
       && /excluded 2548/.test(counts), counts);
    const statuses = await page.evaluate(() => Array.prototype.map.call(
      document.querySelectorAll('#rv-src-out article'), (a) => a.dataset.status));
    ok(tag('unassigned, ambiguous and excluded records are all reachable'),
       statuses.indexOf('unassigned') >= 0 && statuses.indexOf('ambiguous') >= 0
       && statuses.indexOf('excluded') >= 0, statuses.join(','));

    // 10. AN EXCLUDED ROW SHOWS ITS REASON, NOT AN EMPTY BOX.
    const excluded = await page.evaluate(() => {
      const a = document.querySelector('#rv-src-out article[data-status="excluded"]');
      return { text: a.textContent, hasBody: !!a.querySelector('pre'),
               withheld: !!a.querySelector('.rv-withheld'), cls: a.className };
    });
    ok(tag('an excluded row renders no body'), !excluded.hasBody && excluded.withheld);
    ok(tag('…and says why, and that it is still counted'),
       /operational telemetry/.test(excluded.text)
       && /still indexed, still counted/.test(excluded.text), excluded.text.slice(0, 120));
    ok(tag('…and looks different from an ordinary record'),
       /rv-excluded/.test(excluded.cls));

    // 11. NO CREDENTIAL REACHES THE SCREEN, from either surface.
    const page_text = await page.textContent('#ace-review');
    ok(tag('a planted credential is redacted before it is displayed'),
       page_text.indexOf('sk-live-FAKE') < 0 && /\[redacted\]/.test(page_text));

    // 12. A FILTER IS A SERVER QUERY HERE TOO.
    const beforeFilter = st.gets.length;
    await page.selectOption('#rv-status', 'excluded');
    await page.waitForFunction(() => document.querySelectorAll(
      '#rv-src-out article').length === 1, null, { timeout: 6000 });
    ok(tag('the status filter is asked of the server'),
       st.gets.slice(beforeFilter).some((g) => /status=excluded/.test(g.path)),
       st.gets.slice(beforeFilter).map((g) => g.path).join(' | '));

    if (vp.width === 390) {
      const fit = await page.evaluate(() => {
        const d = document.getElementById('ace-review');
        const r = d.getBoundingClientRect();
        const b = d.querySelector('article');
        return { right: Math.round(r.right), left: Math.round(r.left),
                 vw: window.innerWidth, scrolls: d.scrollHeight > d.clientHeight,
                 cardRight: Math.round(b.getBoundingClientRect().right) };
      });
      ok(tag('the panel fits the screen and scrolls rather than overflowing'),
         fit.left >= 0 && fit.right <= fit.vw && fit.cardRight <= fit.vw,
         JSON.stringify(fit));
      const OUT = process.env.ACE_UI_OUT || '/tmp/ace-voice-shots';
      try { fs.mkdirSync(OUT, { recursive: true }); } catch (e) {}
      await page.screenshot({ path: path.join(OUT, '22-phone-memory-review.png') });
    }

    // 13. "NOT INDEXED YET" AND "THE INDEX IS DOWN" ARE DIFFERENT ANSWERS.
    st.mode = 'absent';
    await page.evaluate(() => window.aceReview.queue(''));
    await page.waitForSelector('#rv-index-state', { timeout: 6000 });
    const absent = await page.evaluate(() => ({
      state: document.getElementById('rv-index-state').dataset.state,
      text: document.getElementById('rv-index-state').textContent }));
    ok(tag('"not indexed yet" says so, and says recall still reads the originals'),
       absent.state === 'absent' && /NOT INDEXED/.test(absent.text)
       && /original records/.test(absent.text), absent.text.slice(0, 90));

    st.mode = 'unavailable';
    await page.evaluate(() => window.aceReview.queue(''));
    await page.waitForFunction(() => {
      const e = document.getElementById('rv-index-state');
      return e && e.dataset.state === 'unavailable';
    }, null, { timeout: 6000 });
    const down = await page.textContent('#rv-index-state');
    ok(tag('an unreachable index claims nothing about what is on file'),
       /NOT an empty result/.test(down), down.slice(0, 90));
    ok(tag('…and does not look like an empty queue'),
       await page.locator('#rv-list').count() === 0);
    st.mode = 'ready';

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
