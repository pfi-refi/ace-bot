/* Release one, driven through the REAL app in a real browser against disposable Postgres.
   Start tests/ui_server.py first. Synthetic rows only; no production URL, no model calls. */
const { chromium } = require('/Users/brady/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const URL = 'http://127.0.0.1:8791/';
const OUT = process.env.ACE_UI_OUT || '/tmp/ace-release1-shots';
const results = [];
const ok = (n, c, d) => results.push((c ? 'PASS' : 'FAIL') + ' — ' + n + (d ? '  (' + d + ')' : ''));

(async () => {
  const b = await chromium.launch({ headless: true, channel: 'chrome' });
  const p = await b.newPage({ viewport: { width: 1440, height: 1100 } });
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(URL, { waitUntil: 'domcontentloaded' });
  await p.evaluate(() => localStorage.setItem('ace2_token', 'dev'));
  await p.reload({ waitUntil: 'networkidle' });

  const api = (path) => p.evaluate(u => fetch(u, {
    headers: { Authorization: 'Bearer dev' } }).then(r => r.json()), URL.replace(/\/$/, '') + path);
  const openCmd = async () => { await p.click('#command-btn'); await p.waitForSelector('.cmd-lens'); };
  const lens = async (l) => { await p.click(`.cmd-lens button[data-lens="${l}"]`); await p.waitForTimeout(250); };
  // ONE ROW NOW (2026-09-11): areas, categories and the records/completed toggles live in
  // the filter sheet. Open it before reaching for any of them.
  const openFilter = async () => {
    if (!(await p.locator('.cmd-sheet').count())) { await p.click('#cmd-filter'); await p.waitForSelector('.cmd-sheet'); }
  };
  const area = async (a) => { await openFilter(); await p.click(`.cmd-area[data-area="${a}"]`); await p.waitForTimeout(300); };
  const showRecords = async () => { await openFilter(); await p.click('#cmd-recs'); await p.waitForTimeout(300); };
  const showDoneRows = async () => { await openFilter(); await p.click('#cmd-showdone'); await p.waitForTimeout(300); };
  const rowFor = (t) => p.locator('.cmd-row', { hasText: t }).first();

  await openCmd();
  await p.screenshot({ path: `${OUT}/01-desktop-today.png` });

  // ── 1. Areas are the navigation, and Inbox is permanently visible but not the default
  await openFilter();
  const areas = await p.locator('.cmd-sheet .cmd-area[data-area]').allInnerTexts();
  ok('Inbox is permanently visible', areas.some(a => /INBOX/i.test(a)), areas.join(' | '));
  ok('Inbox is not the default view',
     (await p.locator('.cmd-area.on').innerText()).trim().toUpperCase() === 'ALL');

  // ── 2. The two Today surfaces show the SAME rows
  const cmdToday = await p.evaluate(() =>
    [...document.querySelectorAll('#command-view .cmd-row')].map(r => r.getAttribute('data-id')));
  await p.click('#command-view .cmd-hd #cmd-x');
  // ONE TODAY SURFACE (2026-09-11). The dock button used to open a separate card; it now
  // opens the board at Today, because the two were rendering the SAME server grouping and
  // the card was the one Brady found showing nine rows he could not tick.
  await p.click('.qa[data-panel="daybank"]');
  await p.waitForSelector('#command-view .cmd-lens');
  ok('Due today opens the board, not a second surface',
     await p.locator('.cmd-lens button[data-lens="today"].on').count() === 1);
  ok('...and no separate Due Today card is created',
     await p.locator('.card[data-panel="DUE TODAY"]').count() === 0);
  await p.click('#command-view .cmd-hd #cmd-x');
  // The card renderer is still reachable — Ace can push one — so its grouping is still
  // covered, just driven directly instead of through the retired button.
  await p.evaluate(async () => {
    const r = await fetch('/daybank?suggest=3', { headers: { Authorization: 'Bearer dev' } });
    const d = await r.json();
    window.aceDebug.card('daybank', { items: d.items, summary: d.summary || null,
                                      due_today: d.due_today, today: d.today });
  });
  await p.waitForSelector('.db-item');
  const cardToday = await p.evaluate(() =>
    [...document.querySelectorAll('.db-item')].length);
  const dt = (await api('/daybank?suggest=3')).due_today;
  // The board renders the server's grouping MINUS undated reference records, which are
  // hidden by default (2026-09-11). A record with a due date is an obligation and stays —
  // that is the whole point of the exemption, so the expectation states the same rule
  // rather than being loosened to make the number fit.
  const shown = rows => (rows || []).filter(r => r.entry !== 'record' || r.due_days != null);
  const count = f => f(dt.deadlines).length + f(dt.chosen).length + f(dt.suggested).length
                   + f(dt.decisions).length + f(dt.review).length + f(dt.waiting).length;
  const expected = count(shown);                 // the board honours the records preference
  const expectedCard = count(r => r || []);      // a pushed card shows the grouping as-is
  ok('Command Center Today shows exactly the server grouping', cmdToday.length === expected,
     `${cmdToday.length} vs ${expected}`);
  ok('a pushed Due Today card still shows the whole grouping',
     cardToday === expectedCard, `${cardToday} vs ${expectedCard}`);
  ok('suggestions are capped at three', dt.suggested.length === 3, String(dt.suggested.length));
  // UNREVIEWED WORK IS NOT A SUGGESTION (Codex, 2026-09-09): carried-over rows leave the
  // suggestion pool entirely and appear as their own group on BOTH surfaces.
  ok('unreviewed rows are not suggested as ready work',
     dt.suggested.every(x => x.carried_over === false),
     dt.suggested.map(x => x.text.slice(0, 24)).join(' | '));
  ok('...they are listed as review instead', (dt.review || []).length > 0,
     `${(dt.review || []).length} of ${dt.review_total}`);
  ok('explicit decisions are not suggested either',
     dt.suggested.every(x => x.needs_decision === false));
  ok('...they are listed as decisions', (dt.decisions || []).length > 0);
  ok('the card shows the review group too',
     (await p.locator('.db-sect').allInnerTexts()).some(t => /WORTH A LOOK/i.test(t)));
  ok('the card shows the decisions group too',
     (await p.locator('.db-sect').allInnerTexts()).some(t => /DECISIONS TO MAKE/i.test(t)));
  if (dt.suggested_total > dt.suggested.length) {
    ok('Show more is offered on the card', await p.locator('.db-more').count() === 1);
    await p.click('.db-more'); await p.waitForTimeout(600);
    ok('Show more actually loads more', await p.locator('.db-item').count() > cardToday);
  }
  await p.screenshot({ path: `${OUT}/02-due-today-card.png` });

  // ── 3. Handling a follow-up leaves the task open
  await openCmd(); await lens('all');
  const fupRow = rowFor('Chase Rebecca');
  await fupRow.scrollIntoViewIfNeeded();
  ok('a follow-up occurrence offers its own controls',
     await fupRow.locator('.cmd-fup[data-fup="clear"]').count() === 1);
  const before = (await api('/daybank?all=true')).items.find(i => /Chase Rebecca/.test(i.text));
  await fupRow.locator('.cmd-fup[data-fup="clear"]').click();
  await p.waitForTimeout(900);
  const after = (await api('/daybank?all=true')).items.find(i => i.id === before.id);
  ok('handling a follow-up leaves the task OPEN', after.status === 'open', after.status);
  ok('...and does not touch the deadline', after.due === before.due, `${before.due} → ${after.due}`);
  ok('...and does not touch the next step', after.next_step === before.next_step);
  ok('...and clears only the follow-up', !after.followup);

  // pushing a week moves only the follow-up
  // 'Permit sign-off' is a WAITING RECORD. Everything hides records by default now, and
  // Waiting deliberately does not — that is where a parked row belongs.
  const pr = rowFor('Permit sign-off');
  await lens('waiting'); await pr.scrollIntoViewIfNeeded();
  const pBefore = (await api('/daybank?all=true')).items.find(i => /Permit sign-off/.test(i.text));
  await pr.locator('.cmd-fup[data-fup="push"]').click();
  await p.waitForTimeout(900);
  const pAfter = (await api('/daybank?all=true')).items.find(i => i.id === pBefore.id);
  ok('push a week moves only the follow-up',
     pAfter.followup !== pBefore.followup && pAfter.due === pBefore.due
       && pAfter.status === 'open' && pAfter.waiting_on === pBefore.waiting_on,
     `${pBefore.followup} → ${pAfter.followup}`);

  // ── 4. Needs a decision is explicit, and carried-over rows say so
  await lens('today');
  const decideTxt = await p.locator('#command-view .cmd-list').innerText();
  // The Decide lens is retired: the server's own Today grouping was already carrying both
  // of these, which is why the button was a second route to a list already on screen.
  ok('marked decisions are listed on Today', /DECISIONS TO MAKE/.test(decideTxt));
  ok('carried-over rows are separated and labelled',
     /WORTH A LOOK/.test(decideTxt) && /not yet reviewed/i.test(decideTxt));
  // Same guarantee, the Today group's own words: the retired lens said "none of them is set
  // to Ready", this one says "not suggested work". Both mean it is not offered as ready work.
  ok('carried-over rows are not offered as ready work',
     /not suggested work/i.test(decideTxt));
  await p.screenshot({ path: `${OUT}/03-decide.png` });

  // ── 4b. Reviewing a carried-over row takes one tap and invents nothing
  await lens('today');
  const carried = p.locator('.cmd-row', { hasText: 'Book the dentist' }).first();
  await carried.scrollIntoViewIfNeeded();
  ok('a carried-over row offers a review control',
     await carried.locator('.cmd-rev').count() === 1);
  const cId = await carried.getAttribute('data-id');
  const cBefore = (await api('/daybank?all=true')).items.find(i => i.id === cId);
  ok('...and is not in the suggestion list while unreviewed',
     !((await api('/daybank?suggest=3')).due_today.suggested || []).some(x => x.id === cId));
  await carried.locator('.cmd-rev').click();
  await p.waitForTimeout(1000);
  const cAfter = (await api('/daybank?all=true')).items.find(i => i.id === cId);
  ok('one tap retires the review flag', cAfter.carried_over === false);
  ok('...durably', !!cAfter.reviewed_at, String(cAfter.reviewed_at));
  ok('...without inventing a deadline or a next step',
     cAfter.due === cBefore.due && cAfter.next_step === cBefore.next_step);
  ok('...and the row is otherwise untouched',
     cAfter.status === cBefore.status && cAfter.text === cBefore.text
       && cAfter.bucket === cBefore.bucket);
  ok('a reviewed row becomes ordinary suggestible work',
     ((await api('/daybank?suggest=9')).due_today.suggested || []).some(x => x.id === cId));

  // ── 5. The editor: pickers, states, tags, and the three separate dates
  await lens('all');
  const target = rowFor('Book the dentist');
  await target.scrollIntoViewIfNeeded();
  await target.locator('.cmd-pencil').click();
  await p.waitForSelector('.cmd-editing');
  const ed = p.locator('.cmd-row.cmd-editing');
  ok('the deadline uses a real date picker',
     await ed.locator('input.cmd-edue[type="date"]').count() === 1);
  ok('the follow-up has its own picker',
     await ed.locator('input.cmd-efup[type="date"]').count() === 1);
  ok('both dates can be cleared', await ed.locator('.cmd-eclear').count() === 2);
  const states = await ed.locator('.cmd-estate option').allInnerTexts();
  ok('status offers Ready / Waiting / Needs a decision',
     /READY/.test(states.join('|')) && /WAITING/.test(states.join('|'))
       && /NEEDS A DECISION/.test(states.join('|')), states.join(' | '));
  const areaOpts = await ed.locator('.cmd-ebucket option').allInnerTexts();
  ok('the area picker lists every area including Inbox',
     areaOpts.length >= 6 && areaOpts.some(a => /Inbox/i.test(a)), areaOpts.join(' | '));
  ok('secondary tags are offered', await ed.locator('.cmd-etag').count() > 5);
  await p.screenshot({ path: `${OUT}/04-editor.png` });

  const dId = await ed.getAttribute('data-id');
  await ed.locator('input.cmd-edue').fill('2026-09-18');
  await ed.locator('input.cmd-efup').fill('2026-09-15');
  await ed.locator('.cmd-enext').fill('call the office when they open');
  await ed.locator('.cmd-estate').selectOption('decide');
  await ed.locator('.cmd-etag[data-tag="Admin"]').click();
  await ed.locator('.cmd-esave').click();
  await p.waitForTimeout(1100);
  const saved = (await api('/daybank?all=true')).items.find(i => i.id === dId);
  ok('the deadline saved', saved.due_on === '2026-09-18', String(saved.due_on));
  ok('the follow-up saved as its own field', saved.followup === '2026-09-15', String(saved.followup));
  ok('the next step saved', saved.next_step === 'call the office when they open');
  // A DEADLINE AND AN OPEN QUESTION ARE BOTH TRUE. The lane belongs to the deadline — a row
  // he flagged must not drop out of Overdue and be missed — but the mark stays readable.
  ok('Needs a decision saved on an ACTION', saved.state === 'decide' && saved.needs_decision === true,
     `${saved.state}/${saved.lane}`);
  ok('a dated decision keeps its deadline lane', saved.lane === 'upcoming', saved.lane);
  ok('...and still reads as a decision on the row',
     /needs a decision/i.test(await rowFor('Book the dentist').innerText()));
  await lens('today');
  ok('...and still appears under Decide',
     (await p.locator('#command-view .cmd-list').innerText()).indexOf(saved.text) >= 0);
  await lens('all');
  ok('the secondary tag saved without a second copy',
     saved.tags.indexOf('Admin') > 0, JSON.stringify(saved.tags));
  const dupes = (await api('/daybank?all=true')).items.filter(i => i.text === saved.text).length;
  ok('tagging never duplicated the row', dupes === 1, String(dupes));

  // ── 6. Do today writes the day and never a deadline
  await lens('all');
  const wRow = rowFor('Website copy');
  await wRow.scrollIntoViewIfNeeded(); await wRow.locator('.cmd-pencil').click();
  await p.waitForSelector('.cmd-editing');
  const wId = await p.locator('.cmd-row.cmd-editing').getAttribute('data-id');
  const wBefore = (await api('/daybank?all=true')).items.find(i => i.id === wId);
  await p.locator('.cmd-etoday').click(); await p.waitForTimeout(900);
  const wAfter = (await api('/daybank?all=true')).items.find(i => i.id === wId);
  ok('un-choosing today clears the day and writes no deadline',
     wAfter.chosen_on !== wBefore.chosen_on && wAfter.due === wBefore.due,
     `${wBefore.chosen_on} → ${wAfter.chosen_on}`);

  // ── 7. Lists: add, then rename, and every link survives
  const linked = (await api('/daybank?all=true')).items;
  // Scoped, and removed at the end of this section. Left registered it dismissed EVERY later
  // dialog before the test that wanted one could see it.
  const listPrompts = async d => {
    const m = d.message();
    if (/Add a list/.test(m)) return d.accept('Greenhouse');
    if (/Rename/.test(m)) return d.accept('Greenhouse build');
    return d.dismiss();
  };
  p.on('dialog', listPrompts);
  await p.click('#cmd-lists'); await p.waitForTimeout(1000);
  let lists = await api('/board/lists');
  ok('a new list persists', lists.areas.includes('Greenhouse'), lists.areas.join(' | '));
  await p.click('#cmd-lists'); await p.waitForTimeout(1000);
  lists = await api('/board/lists');
  ok('renaming a list works', lists.areas.includes('Greenhouse build'), lists.areas.join(' | '));
  const nowRows = (await api('/daybank?all=true')).items;
  ok('renaming preserved every id', linked.every(o => nowRows.some(n => n.id === o.id)));
  ok('renaming preserved every parent link and date',
     linked.every(o => { const n = nowRows.find(x => x.id === o.id);
       return n && n.parent_id === o.parent_id && n.due === o.due && n.ts === o.ts; }));

  p.off('dialog', listPrompts);

  // ── 8. Capture lands in the area he is looking at
  await area('Side Work');
  await p.fill('#cmd-input', 'Release one: price the new mixer');
  await p.press('#cmd-input', 'Enter'); await p.waitForTimeout(1200);
  const added = (await api('/daybank?all=true')).items.find(i => /price the new mixer/.test(i.text));
  ok('capture files into the area on screen', added && added.bucket === 'Side Work',
     added ? added.bucket : 'not added');

  // ── 9. A waiting row asks before it closes, and an accidental tap still cannot close it
  // (2026-09-11, Brady: "am I not able to mark those off?"). His Due Today was nine parked
  // rows and not one of them was tickable. Something can finish without the other person
  // ever coming back — but on 5 Sept four records vanished to one stray tap, so it asks.
  await area('All');
  await lens('waiting');
  const wr = p.locator('.cmd-row', { hasText: 'Permit sign-off' }).first();
  await wr.scrollIntoViewIfNeeded();
  const before9 = (await api('/daybank?all=true')).items.find(i => /Permit sign-off/.test(i.text));

  let asked = '';
  // once(), not on(): a lingering listener double-handles the next dialog and Playwright
  // throws "Cannot dismiss dialog which is already handled".
  p.once('dialog', d => { asked = d.message(); d.dismiss(); });
  await wr.locator('.cmd-box').click(); await p.waitForTimeout(1100);
  const after9 = (await api('/daybank?all=true')).items.find(i => i.id === before9.id);
  ok('closing a parked row asks first', /close it anyway/i.test(asked), asked.replace(/\n+/g, ' '));
  ok('...and names what it is parked on', /waiting on the county/i.test(asked));
  ok('a stray tap still cannot close a waiting record', after9.status === 'open', after9.status);

  p.once('dialog', d => d.accept());
  await wr.locator('.cmd-box').click(); await p.waitForTimeout(1100);
  const forced9 = (await api('/daybank?all=true')).items.find(i => i.id === before9.id);
  ok('...but confirming does close it', forced9.status === 'done', forced9.status);
  // Put it back through the UI — reopening is not a close, so it asks nothing.
  await showDoneRows();
  await p.locator('.cmd-row', { hasText: 'Permit sign-off' }).first().locator('.cmd-box').click();
  await p.waitForTimeout(1100);
  const restored9 = (await api('/daybank?all=true')).items.find(i => i.id === before9.id);
  ok('...and reopening it needs no confirmation', restored9.status === 'open', restored9.status);
  await showDoneRows();

  // ── 9b. One filter row, and records are out of the way by default
  await lens('all');
  ok('the header is one row, not three',
     await p.locator('#command-view .cmd-lens').count() === 1
     && await p.locator('#command-view > .cmd-areas').count() === 0
     && await p.locator('#command-view > .cmd-chips').count() === 0);
  ok('...offering exactly Today, Week, Waiting and Everything',
     (await p.locator('.cmd-lens button[data-lens]').allInnerTexts())
       .map(t => t.trim().split(' ')[0].toUpperCase()).join('|')
       === 'TODAY|WEEK|WAITING|EVERYTHING');
  // A genuine entry='record' from the fixture. ('Unfiled thought from the truck' is an
  // ACTION — it reads like a note, which is exactly why the entry field exists.)
  const recordText = 'The Marlow deal';
  ok('records are hidden by default',
     await p.locator('.cmd-row', { hasText: recordText }).count() === 0);
  await showRecords();
  ok('...and the toggle brings them back',
     await p.locator('.cmd-row', { hasText: recordText }).count() === 1);
  await showRecords();
  ok('areas and categories still work, from inside the sheet',
     await p.locator('.cmd-sheet .cmd-area[data-area]').count() > 0
     && await p.locator('.cmd-sheet .cmd-chip').count() > 0);
  await openFilter();
  await p.screenshot({ path: `${OUT}/09-one-row.png` });

  // ── 9d. The Week lens: the dated grouping, findable (2026-09-11, Brady asked for one)
  await lens('week');
  const weekTxt = await p.locator('#command-view .cmd-list').innerText();
  ok('Week groups by date, not by age',
     /TODAY|TOMORROW|THIS WEEK|OVERDUE/.test(weekTxt), weekTxt.slice(0, 70));
  // Case-insensitive on purpose: .cmd-grp is text-transform: uppercase, so innerText comes
  // back shouting and a case-sensitive match fails for a reason that has nothing to do with
  // the code. That has bitten this project three times.
  ok('...and lists follow-ups separately from deadlines',
     !/FOLLOW-UPS/i.test(weekTxt) || /when you chase them/i.test(weekTxt));
  ok('...and never shows the undated pile',
     !/EVERYTHING ELSE/i.test(weekTxt));
  // Week and Everything are built by ONE function, so a dated row cannot be in one only.
  const weekIds = await p.evaluate(() =>
    [...document.querySelectorAll('#command-view .cmd-row')].map(r => r.getAttribute('data-id')));
  await lens('all');
  const allIds = await p.evaluate(() =>
    [...document.querySelectorAll('#command-view .cmd-row')].map(r => r.getAttribute('data-id')));
  ok('every row in Week is also in Everything',
     weekIds.every(id => allIds.includes(id)),
     weekIds.filter(id => !allIds.includes(id)).join(','));
  ok('...and Everything is the larger list', allIds.length >= weekIds.length,
     `${allIds.length} vs ${weekIds.length}`);
  await p.screenshot({ path: `${OUT}/09-week.png` });

  // ── 9c. Hiding records must never hide a DEADLINE (Codex, 2026-09-11)
  // A record with a due date is lane='today' and completable — an obligation, not reference.
  // Filtering Today and Everything by entry type alone made it disappear from both.
  const dueRecord = 'County permit fee due';
  await lens('today');
  ok('a dated record still reaches Today with records hidden',
     await p.locator('.cmd-row', { hasText: dueRecord }).count() === 1);
  await lens('all');
  ok('...and still reaches Everything',
     await p.locator('.cmd-row', { hasText: dueRecord }).count() === 1);
  ok('...while undated reference stays hidden',
     await p.locator('.cmd-row', { hasText: 'The Marlow deal' }).count() === 0);

  // ── 10. Phone: readable controls, no sideways page scroll
  await p.setViewportSize({ width: 390, height: 844 });
  await p.waitForTimeout(400); await lens('all');
  await area('All'); await lens('all');
  const ph = rowFor('Order concrete');
  await ph.scrollIntoViewIfNeeded(); await ph.locator('.cmd-pencil').click();
  await p.waitForSelector('.cmd-editing'); await p.waitForTimeout(300);
  const sizes = await p.evaluate(() => {
    const pick = s => { const e = document.querySelector(s); if (!e) return null;
      const r = e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; };
    return { due: pick('.cmd-edue'), state: pick('.cmd-estate'), save: pick('.cmd-esave'),
             tag: pick('.cmd-etag'), today: pick('.cmd-etoday'),
             overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
  });
  ok('phone: no horizontal page overflow', sizes.overflow <= 0, String(sizes.overflow));
  ['due', 'state', 'save', 'tag', 'today'].forEach(k =>
    ok(`phone: ${k} clears a 44px touch target`, sizes[k] && sizes[k][1] >= 44,
       sizes[k] ? sizes[k].join('x') : 'missing'));
  await p.screenshot({ path: `${OUT}/05-phone-editor.png`, fullPage: false });
  await p.click('.cmd-ecancel'); await lens('today');
  await p.screenshot({ path: `${OUT}/06-phone-today.png`, fullPage: false });

  ok('no page errors', errs.length === 0, errs.join(' / '));
  await b.close();
  console.log(results.join('\n'));
  console.log('\n' + results.filter(r => r.startsWith('PASS')).length + ' passed, '
              + results.filter(r => r.startsWith('FAIL')).length + ' failed');
  process.exit(results.some(r => r.startsWith('FAIL')) ? 1 : 0);
})();
