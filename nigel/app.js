/* NIGEL prototype — application shell.
   Navigation: StarCloud → Systems → Sectors → Records, driven by the URL hash so every
   view is linkable and the browser back button works. All AI output and every
   "submission" is simulated in this file; nothing is sent anywhere. */
(function () {
  const D = window.NIGEL_DATA;
  const SC = window.StarCloud;
  const $ = (s, r) => (r || document).querySelector(s);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (n) => '$' + Number(n).toLocaleString('en-US');
  const STAGES = ['Intake', 'Suitability', 'Illustration', 'Submission', 'Issued'];

  /* ---------- state ---------- */
  const state = {
    reviews: {},          // id -> {status, outcome}
    cases: {},            // household id -> {stage, memo, illustration, submission}
    activity: D.activity.slice(),
    convo: []
  };
  try {
    const saved = JSON.parse(sessionStorage.getItem('nigel-proto') || 'null');
    if (saved && saved.v === 1) Object.assign(state, saved.state);
  } catch (e) { /* storage unavailable, run in memory */ }
  function persist() {
    try { sessionStorage.setItem('nigel-proto', JSON.stringify({ v: 1, state: { reviews: state.reviews, cases: state.cases, activity: state.activity } })); } catch (e) { /* ignore */ }
  }
  function reviewStatus(r) { return (state.reviews[r.id] && state.reviews[r.id].status) || 'open'; }
  function openReviews(systemId) { return D.reviews.filter(r => reviewStatus(r) === 'open' && (!systemId || r.system === systemId)); }
  function caseOf(id) {
    if (!state.cases[id]) state.cases[id] = { stage: D.households[id].stage, memo: null, illustration: null, submission: null, notes: [] };
    return state.cases[id];
  }
  function logActivity(what, system) {
    state.activity.unshift({ when: 'Just now', what, system: system || 'nigel' });
    persist();
  }

  /* ---------- labels on the cloud ---------- */
  const labelsRoot = $('#labels');
  function buildLabels() {
    labelsRoot.innerHTML = '';
    SC.setNodes(D.systems.map(s => ({ id: s.id, pos: s.pos, posPortrait: s.posPortrait, attention: openReviews(s.id).length > 0 })));
    for (const s of D.systems) {
      const b = document.createElement('button');
      b.className = 'label' + (s.pos[0] > 0.6 ? ' flip' : '');
      b.dataset.system = s.id;
      b.innerHTML = `<span class="label-core"></span><span class="label-text"><span class="label-name">${esc(s.name)}</span><span class="label-sub"></span></span>`;
      b.addEventListener('click', () => go(`/systems/${s.id}`));
      labelsRoot.appendChild(b);
      SC.bindLabel(s.id, b);
    }
    refreshLabels();
  }
  function refreshLabels() {
    for (const s of D.systems) {
      const el = labelsRoot.querySelector(`[data-system="${s.id}"]`);
      if (!el) continue;
      const n = openReviews(s.id).length;
      el.classList.toggle('is-amber', n > 0);
      const sub = $('.label-sub', el);
      sub.textContent = n > 0 ? `${n} need${n === 1 ? 's' : ''} attention` : s.tagline;
      SC.setAttention(s.id, n > 0);
      // portrait: stack the text under the node so the two columns never collide
      const portrait = SC.isPortrait();
      el.classList.toggle('stack', portrait);
      el.classList.toggle('flip', !portrait && s.pos[0] > 0.6);
    }
    const total = openReviews().length;
    const badge = $('#attention-badge');
    badge.textContent = total; badge.classList.toggle('is-zero', total === 0);
    $('#topbar-attention-text').textContent = total ? `${total} need${total === 1 ? 's' : ''} attention` : 'All clear';
    $('#topbar-attention-n').textContent = total ? String(total) : '✓';
    $('#topbar-attention').classList.toggle('is-clear', total === 0);
  }

  /* ---------- routing ---------- */
  function go(path) { location.hash = '#' + path; }
  function parse() {
    const parts = (location.hash.replace(/^#\/?/, '') || '').split('/').filter(Boolean);
    const view = parts[0] || 'starcloud';
    if (view === 'systems') {
      return { view: parts.length === 1 ? 'systems' : parts.length === 2 ? 'sectors' : parts.length === 3 ? 'records' : 'record', sid: parts[1], sec: parts[2], rid: parts[3] };
    }
    return { view, sid: null, sec: null, rid: null };
  }
  let current = null;
  function render() {
    const r = parse();
    current = r;
    const sys = r.sid && D.systems.find(s => s.id === r.sid);
    const sec = sys && r.sec && sys.sectors.find(x => x.id === r.sec);
    const hh = r.rid && D.households[r.rid];
    if (r.view !== 'starcloud' && r.view !== 'attention' && r.view !== 'systems' && r.view !== 'records' && r.view !== 'activity' && !sys) { go('/'); return; }
    if ((r.view === 'records' && !sec) || (r.view === 'record' && !hh)) { go(sys ? `/systems/${sys.id}` : '/'); return; }

    document.body.dataset.view = r.view;
    if (r.view === 'starcloud') delete document.body.dataset.panel; else document.body.dataset.panel = '1';
    document.body.classList.remove('menu-open'); $('#menu-toggle').setAttribute('aria-expanded', 'false');
    for (const b of document.querySelectorAll('.nav-item')) {
      const key = b.dataset.nav;
      b.classList.toggle('is-active', key === r.view || (key === 'systems' && ['sectors', 'records', 'record'].includes(r.view) && !(r.view === 'record')) || (key === 'records' && r.view === 'record'));
    }
    for (const el of labelsRoot.children) el.classList.toggle('is-focus', !!sys && el.dataset.system === sys.id);

    // camera
    if (r.view === 'starcloud') { SC.reset(); SC.veil(false); }
    else if (sys) { SC.focus(sys.id, r.view === 'sectors' ? 1.45 : 1.7); SC.veil(true); }
    else { SC.focus(null, 1.12); SC.veil(true); }

    renderCrumbs(r, sys, sec, hh);
    const stage = $('#stage');
    stage.scrollTop = 0;
    stage.innerHTML = ({
      starcloud: viewStarCloud, attention: viewAttention, systems: viewSystems, records: sec ? () => viewRecords(sys, sec) : viewAllRecords,
      sectors: () => viewSectors(sys), record: () => viewRecord(sys, sec, hh), activity: viewActivity
    }[r.view] || viewStarCloud)();
    if (r.view === 'records' && !sec) stage.innerHTML = viewAllRecords();
    bindStage();
  }

  function renderCrumbs(r, sys, sec, hh) {
    const items = [['StarCloud', '/']];
    if (r.view === 'attention') items.push(['Attention', '/attention']);
    if (r.view === 'activity') items.push(['Activity', '/activity']);
    if (r.view === 'records' && !sys) items.push(['Records', '/records']);
    if (['systems', 'sectors', 'records', 'record'].includes(r.view) && (sys || r.view === 'systems')) items.push(['Systems', '/systems']);
    if (sys) items.push([sys.name, `/systems/${sys.id}`]);
    if (sec) items.push([sec.name, `/systems/${sys.id}/${sec.id}`]);
    if (hh) items.push([hh.name, `/systems/${sys.id}/${sec.id}/${hh.id}`]);
    const c = $('#crumbs');
    c.innerHTML = items.map(([name, path], i) => {
      const last = i === items.length - 1;
      const back = i === items.length - 2;
      return `${i ? `<span class="crumb-sep${back ? ' keep' : ''}">›</span>` : ''}<button class="crumb${last ? ' is-current' : ''}${back ? ' crumb-back' : ''}" data-go="${path}">${back ? '<span class="back-arrow">‹ </span>' : ''}${esc(name)}</button>`;
    }).join('');
  }

  /* ---------- views ---------- */
  function viewStarCloud() {
    const total = openReviews().length;
    const pending = D.systems[0].sectors[0].count;
    return `<div class="orient">
      <div class="stats">
        <button class="chip" data-go="/systems"><span class="chip-dot"></span>${D.systems.length} systems</button>
        <button class="chip" data-go="/systems/paraclete/pending"><span class="chip-dot"></span>${pending} pending annuity clients</button>
        <button class="chip chip-amber${total ? '' : ' is-clear'}" data-go="/attention"><span class="chip-dot"></span>${total ? total + ' need attention' : 'All clear'}</button>
      </div>
      <div class="hint">Select a system on the cloud, or ask NIGEL below. Amber means something needs attention.</div>
    </div>`;
  }

  function viewSystems() {
    return `<section class="panel"><div class="panel-head"><div><h1><small>StarCloud</small>Systems</h1><p class="lede">Each system is a region of the cloud. Amber marks a system with open reviews.</p></div></div>
    <div class="panel-body"><div class="grid">${D.systems.map(s => {
      const n = openReviews(s.id).length;
      return `<button class="tile${n ? ' is-amber' : ''}" data-go="/systems/${s.id}"><span class="tile-top"><span class="tile-core"></span><span class="tile-name">${esc(s.name)}</span><span class="tile-count">${s.sectors.length} sector${s.sectors.length === 1 ? '' : 's'}</span></span><span class="tile-sub">${esc(s.tagline)}</span>${n ? `<span class="tile-flag">${n} need${n === 1 ? 's' : ''} attention</span>` : ''}</button>`;
    }).join('')}</div></div></section>`;
  }

  function viewSectors(sys) {
    const n = openReviews(sys.id).length;
    return `<section class="panel"><div class="panel-head"><div><h1><small>System</small>${esc(sys.name)}</h1><p class="lede">${esc(sys.tagline)}. ${n ? `<strong style="color:var(--amber)">${n} review${n === 1 ? '' : 's'} open.</strong>` : 'Nothing needs attention here.'}</p></div>
      ${n ? `<button class="btn btn-amber" data-go="/attention">Open attention</button>` : ''}</div>
    <div class="panel-body"><div class="grid">${sys.sectors.map(sec => {
      const open = sec.attention ? openReviews(sys.id).length : 0;
      const hasRecords = sec.records.length > 0;
      const target = sec.attention ? '/attention' : hasRecords ? `/systems/${sys.id}/${sec.id}` : '';
      return `<button class="tile${open ? ' is-amber' : ''}${target ? '' : ' is-disabled'}" ${target ? `data-go="${target}"` : 'disabled'}><span class="tile-top"><span class="tile-core"></span><span class="tile-name">${esc(sec.name)}</span><span class="tile-count">${sec.attention ? open : sec.count}</span></span><span class="tile-sub">${esc(sec.note)}</span>${open ? `<span class="tile-flag">${open} open review${open === 1 ? '' : 's'}</span>` : (target ? '' : '<span class="tile-sub">Not in this prototype</span>')}</button>`;
    }).join('')}</div></div></section>`;
  }

  function viewRecords(sys, sec) {
    return `<section class="panel"><div class="panel-head"><div><h1><small>${esc(sys.name)}</small>${esc(sec.name)}</h1><p class="lede">${esc(sec.note)}. Select a household to open its case.</p></div></div>
    <div class="panel-body"><div class="list">${sec.records.map(id => {
      const h = D.households[id]; const c = caseOf(id);
      return `<button class="row" data-go="/systems/${sys.id}/${sec.id}/${id}"><span><span class="row-title">${esc(h.name)}</span><span class="row-sub">${esc(h.caseTitle)} · ${money(h.premium)}</span></span><span class="row-meta"><span class="stage-tag${c.stage >= 3 ? ' is-live' : ''}">${STAGES[c.stage]}</span></span></button>`;
    }).join('')}</div></div></section>`;
  }

  function viewAllRecords() {
    const rows = Object.values(D.households).map(h => {
      const c = caseOf(h.id);
      return `<button class="row" data-go="/systems/paraclete/pending/${h.id}"><span><span class="row-title">${esc(h.name)}</span><span class="row-sub">Paraclete 1 · ${esc(h.caseTitle)}</span></span><span class="row-meta"><span class="stage-tag${c.stage >= 3 ? ' is-live' : ''}">${STAGES[c.stage]}</span></span></button>`;
    }).join('');
    return `<section class="panel"><div class="panel-head"><div><h1><small>StarCloud</small>Records</h1><p class="lede">Households with an open case. In this prototype all records live under Paraclete 1 → Pending annuity clients.</p></div></div><div class="panel-body"><div class="list">${rows}</div></div></section>`;
  }

  function viewAttention() {
    const groups = D.systems.filter(s => D.reviews.some(r => r.system === s.id));
    const total = openReviews().length;
    return `<section class="panel"><div class="panel-head"><div><h1><small>StarCloud</small>Attention</h1><p class="lede">${total ? `${total} item${total === 1 ? '' : 's'} waiting on you. Each action below is recorded in Activity and nothing is sent anywhere.` : 'Everything is resolved. New items would appear here as NIGEL finds them.'}</p></div>
      ${Object.keys(state.reviews).length ? '<button class="btn btn-ghost" data-act="reset-reviews">Reset sample reviews</button>' : ''}</div>
    <div class="panel-body">${groups.map(s => {
      const list = D.reviews.filter(r => r.system === s.id);
      const n = list.filter(r => reviewStatus(r) === 'open').length;
      return `<div class="group-title"><span>${esc(s.name)}</span><span class="n">${n} open</span></div><div class="list">${list.map(reviewCard).join('')}</div>`;
    }).join('')}</div></section>`;
  }
  function reviewCard(r) {
    const st = state.reviews[r.id];
    const done = st && st.status !== 'open';
    return `<article class="review${done ? ' is-done' : ''}" data-review="${r.id}">
      <div><div class="review-kind">${esc(r.kind)}</div><div class="review-title">${esc(r.title)}</div><div class="review-summary">${esc(r.summary)}</div><div class="review-detail">${esc(r.detail)}</div></div>
      <div class="review-due">Due ${esc(r.due)}</div>
      ${done ? `<div class="review-outcome">${esc(st.outcome)}</div>` : `<div class="review-actions">
        <button class="btn btn-primary" data-act="review" data-id="${r.id}" data-outcome="approve">Approve</button>
        <button class="btn" data-act="review" data-id="${r.id}" data-outcome="changes">Request changes</button>
        <button class="btn btn-ghost" data-act="review" data-id="${r.id}" data-outcome="defer">Defer a week</button>
      </div>`}
    </article>`;
  }

  function viewActivity() {
    return `<section class="panel"><div class="panel-head"><div><h1><small>StarCloud</small>Activity</h1><p class="lede">What NIGEL and you have done. Entries created in this session are simulated.</p></div></div>
    <div class="panel-body"><div class="timeline">${state.activity.map(a => `<div class="tl${a.when === 'Just now' ? ' is-new' : ''}"><span class="w">${esc(a.when)}</span><span class="x">${esc(a.what)}</span></div>`).join('')}</div></div></section>`;
  }

  function viewRecord(sys, sec, h) {
    const c = caseOf(h.id);
    const stepper = STAGES.map((s, i) => `<span class="step${i < c.stage ? ' is-done' : i === c.stage ? ' is-current' : ''}"><span class="k">${i < c.stage ? '✓' : i + 1}</span>${s}</span>`).join('');
    const initials = (n) => n.split(' ').map(w => w[0]).join('').slice(0, 2);
    const docs = h.documents.map(d => {
      let status = d.status;
      if (d.name === 'Suitability memo' && c.memo) status = c.stage > 1 ? 'Reviewed' : 'Drafted';
      const amber = /needs|not started/i.test(status);
      return `<div class="doc"><span>${esc(d.name)}</span><span class="st${amber ? ' is-amber' : ''}">${esc(status)}</span></div>`;
    }).join('');
    const timeline = h.timeline.concat(c.notes).map(t => `<div class="tl${t.isNew ? ' is-new' : ''}"><span class="w">${esc(t.when)}</span><span class="x">${esc(t.what)}</span></div>`).join('');

    const act = (i, title, desc, btn) => `<div class="action${i === c.stage ? ' is-current' : i < c.stage ? ' is-done' : ''}"><span class="t">${title}</span><span class="d">${desc}</span><span class="act-ctl">${btn}</span></div>`;
    const suitBtn = c.stage === 1
      ? (c.memo ? `<button class="btn btn-primary" data-act="advance" data-id="${h.id}" data-to="2">Mark reviewed</button>` : `<button class="btn btn-primary" data-act="memo" data-id="${h.id}">Draft memo <span class="sim">sim AI</span></button>`)
      : c.stage > 1 ? '<span class="sim-tag">done</span>' : '<button class="btn" disabled>Draft memo</button>';
    const illusBtn = c.stage === 2
      ? (c.illustration ? `<button class="btn btn-primary" data-act="advance" data-id="${h.id}" data-to="3">Client signed</button>` : `<button class="btn btn-primary" data-act="illustrate" data-id="${h.id}">Generate <span class="sim">sim</span></button>`)
      : c.stage > 2 ? '<span class="sim-tag">done</span>' : '<button class="btn" disabled>Generate</button>';
    const subBtn = c.stage === 3
      ? (c.submission && c.submission.done ? '<span class="sim-tag">transmitted</span>' : `<button class="btn btn-primary" data-act="submit" data-id="${h.id}">Submit <span class="sim">sim</span></button>`)
      : c.stage > 3 ? '<span class="sim-tag">done</span>' : '<button class="btn" disabled>Submit</button>';

    return `<section class="panel"><div class="panel-head"><div><h1><small>${esc(sys.name)} · ${esc(sec.name)}</small>${esc(h.name)}</h1><p class="lede">${esc(h.caseTitle)} · ${money(h.premium)} · ${esc(h.proposedCarrier)}</p><div class="stepper">${stepper}</div></div>
      <div><button class="btn btn-ghost" data-act="reset-case" data-id="${h.id}">Reset case</button></div></div>
    <div class="panel-body"><div class="two-col">
      <div class="card"><h3>Household</h3>
        <div class="members">${h.members.map(m => `<div class="member"><span class="avatar">${esc(initials(m.name))}</span><span><div class="who">${esc(m.name)}, ${m.age}</div><div class="what">${esc(m.role)}</div></span></div>`).join('')}</div>
        <dl class="kv"><dt>Advisor</dt><dd>${esc(h.advisor)}</dd><dt>Contact</dt><dd>${esc(h.contact)}</dd><dt>Risk profile</dt><dd>${esc(h.riskProfile)}</dd><dt>Objective</dt><dd>${esc(h.objective)}</dd><dt>Source of funds</dt><dd>${esc(h.sourceOfFunds)}</dd><dt>Product</dt><dd>${esc(h.product)}</dd></dl>
        <h3 style="margin-top:18px">Documents</h3><div class="docs">${docs}</div>
      </div>
      <div class="card"><h3>Case workflow</h3>
        <div class="actions">
          ${act(0, 'Intake', 'Questionnaire and existing contract on file.', c.stage > 0 ? '<span class="sim-tag">done</span>' : `<button class="btn btn-primary" data-act="advance" data-id="${h.id}" data-to="1">Complete intake</button>`)}
          ${act(1, 'Suitability', 'NIGEL drafts the memo from intake; you review and sign off.', suitBtn)}
          ${c.memo ? `<div class="memo${c.memoTyping ? ' is-typing' : ''}" id="memo-${h.id}">${esc(c.memo)}</div><div style="margin-top:6px"><span class="sim-tag">simulated AI draft · review before use</span></div>` : ''}
          ${act(2, 'Illustration', 'Product illustration for the proposed premium.', illusBtn)}
          ${c.illustration ? illustrationTable(c.illustration) : ''}
          ${act(3, 'Carrier submission', 'Package the case and transmit to the carrier.', subBtn)}
          ${c.submission ? submissionBlock(c.submission) : ''}
          ${act(4, 'Issued', 'Contract issued and delivered.', c.stage === 4 ? '<span class="sim-tag">issued</span>' : '<span class="sim-tag" style="opacity:.5">pending</span>')}
        </div>
        ${c.stage === 3 && c.submission && c.submission.done ? '<div class="callout"><strong>Waiting on carrier.</strong> In a live system NIGEL would poll for the acknowledgement. Here you can <button class="btn btn-ghost" style="padding:2px 8px" data-act="advance" data-id="' + h.id + '" data-to="4">simulate issue</button>.</div>' : ''}
        <h3 style="margin-top:18px">Timeline</h3><div class="timeline">${timeline}</div>
      </div>
    </div></div></section>`;
  }
  function illustrationTable(il) {
    return `<table class="illus"><thead><tr><th>Year</th><th>Assumed credit</th><th>Value</th><th>Surrender charge</th></tr></thead><tbody>${il.rows.map(r => `<tr><td>${r.year}</td><td class="num">${r.credit}</td><td class="num">${money(r.value)}</td><td class="num">${r.surrender}</td></tr>`).join('')}</tbody></table><div style="margin-top:6px"><span class="sim-tag">simulated illustration · ${esc(il.product)}</span></div>`;
  }
  function submissionBlock(s) {
    const steps = ['Packaging documents', 'Transmitting to carrier', 'Acknowledgement received'];
    return `<div class="progress">${steps.map((t, i) => `<div class="pstep${i < s.step ? ' is-done' : i === s.step ? ' is-active' : ''}"><span class="pd"></span>${t}</div>`).join('')}</div>
    ${s.done ? `<div class="receipt">SIMULATED RECEIPT<br>Ref ${esc(s.ref)}<br>${esc(s.carrier)} · ${esc(s.when)}<br>No data left this page.</div>` : ''}`;
  }

  /* ---------- actions ---------- */
  function bindStage() {
    for (const el of document.querySelectorAll('[data-go]')) el.addEventListener('click', () => go(el.dataset.go));
    for (const el of document.querySelectorAll('[data-act]')) el.addEventListener('click', () => action(el.dataset));
  }
  function action(d) {
    const id = d.id;
    if (d.act === 'review') {
      const r = D.reviews.find(x => x.id === id);
      const text = { approve: 'Approved. NIGEL will carry out the proposed action (simulated).', changes: 'Changes requested. NIGEL will revise the proposal (simulated).', defer: 'Deferred one week. It will return to Attention (simulated).' }[d.outcome];
      state.reviews[id] = { status: d.outcome, outcome: text };
      logActivity(`${d.outcome === 'approve' ? 'Approved' : d.outcome === 'changes' ? 'Requested changes on' : 'Deferred'}: ${r.title}`, r.system);
      refreshLabels(); render(); toast(text);
    }
    if (d.act === 'reset-reviews') { state.reviews = {}; logActivity('Sample reviews reset.'); refreshLabels(); render(); }
    if (d.act === 'reset-case') { delete state.cases[id]; persist(); render(); toast('Case reset to its starting point.'); }
    if (d.act === 'advance') {
      const c = caseOf(id), to = Number(d.to), h = D.households[id];
      c.stage = to;
      c.notes.push({ when: 'Just now', what: { 1: 'Intake completed.', 2: 'Suitability memo reviewed and signed off.', 3: 'Illustration signed by the client.', 4: 'Contract issued (simulated).' }[to], isNew: true });
      logActivity(`${h.name}: moved to ${STAGES[to]}.`, 'paraclete'); persist(); render();
    }
    if (d.act === 'memo') draftMemo(id);
    if (d.act === 'illustrate') illustrate(id);
    if (d.act === 'submit') confirmSubmit(id);
  }

  function draftMemo(id) {
    const h = D.households[id], c = caseOf(id);
    const full = `Suitability memo — ${h.name}\n\nRecommendation: ${h.product} from ${h.proposedCarrier}, funded by ${h.sourceOfFunds.replace(/\.$/, '')}.\n\nRationale: The household's stated objective is "${h.objective}" The risk profile is ${h.riskProfile.toLowerCase()}. A fixed index design keeps principal protected while offering credited growth, and the 10-year surrender schedule ends before the planned income start date.\n\nReplacement analysis: The existing contract's surrender charge expires Nov 2026. Waiting until then avoids a $4,100 charge; the memo recommends a conditional application dated for the surrender-free window.\n\nOpen items: signed replacement disclosure; confirm beneficiary designations.\n\nThis text was generated by a simulated assistant for prototype purposes.`;
    c.memo = ''; c.memoTyping = true; render();
    let i = 0;
    const el = () => document.getElementById('memo-' + id);
    const tick = () => {
      if (!c.memoTyping) return;
      i = Math.min(full.length, i + 6);
      c.memo = full.slice(0, i);
      const m = el(); if (m) m.textContent = c.memo;
      if (i < full.length) setTimeout(tick, 18);
      else { c.memoTyping = false; c.notes.push({ when: 'Just now', what: 'Suitability memo drafted by NIGEL (simulated).', isNew: true }); logActivity(`${h.name}: suitability memo drafted (simulated).`, 'paraclete'); persist(); render(); }
    };
    setTimeout(tick, 400);
  }

  function illustrate(id) {
    const h = D.households[id], c = caseOf(id);
    const rows = []; let v = h.premium;
    for (let y = 1; y <= 10; y++) {
      const credit = [4.2, 0, 6.1, 3.5, 0, 5.4, 4.8, 2.1, 5.9, 3.3][y - 1];
      v = Math.round(v * (1 + credit / 100));
      rows.push({ year: y, credit: credit.toFixed(1) + '%', value: v, surrender: Math.max(0, 10 - y) + '%' });
    }
    c.illustration = { product: h.product, rows };
    c.notes.push({ when: 'Just now', what: 'Illustration generated (simulated).', isNew: true });
    logActivity(`${h.name}: illustration generated (simulated).`, 'paraclete'); persist(); render();
  }

  function confirmSubmit(id) {
    const h = D.households[id];
    modal('Submit to carrier?', `<p>This is a <strong>simulated</strong> submission for <strong>${esc(h.name)}</strong>. The prototype will animate a transmission and produce a fictional reference number. Nothing is sent to ${esc(h.proposedCarrier)} or anywhere else.</p>`, [
      ['Cancel', 'btn btn-ghost', null],
      ['Simulate submission', 'btn btn-primary', () => runSubmit(id)]
    ]);
  }
  function runSubmit(id) {
    const h = D.households[id], c = caseOf(id);
    c.submission = { step: 0, done: false }; render();
    const steps = [900, 1400, 900];
    let s = 0;
    const next = () => {
      s++; c.submission.step = s;
      if (s >= 3) {
        const ref = 'SIM-' + new Date().toISOString().slice(0, 10).replace(/-/g, '') + '-' + Math.random().toString(36).slice(2, 7).toUpperCase();
        c.submission = { step: 3, done: true, ref, carrier: h.proposedCarrier, when: new Date().toLocaleString() };
        c.notes.push({ when: 'Just now', what: `Submitted to ${h.proposedCarrier} (simulated). Ref ${ref}.`, isNew: true });
        logActivity(`${h.name}: submitted to carrier (simulated), ref ${ref}.`, 'paraclete'); persist(); render(); toast('Simulated submission complete.');
        return;
      }
      render(); setTimeout(next, steps[s]);
    };
    setTimeout(next, steps[0]);
  }

  /* ---------- modal + toast ---------- */
  function modal(title, body, actions) {
    const m = $('#modal'); $('#modal-title').textContent = title; $('#modal-body').innerHTML = body;
    const a = $('#modal-actions'); a.innerHTML = '';
    for (const [label, cls, fn] of actions) {
      const b = document.createElement('button'); b.className = cls; b.textContent = label;
      b.addEventListener('click', () => { m.hidden = true; if (fn) fn(); });
      a.appendChild(b);
    }
    m.hidden = false; a.lastChild.focus();
  }
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') $('#modal').hidden = true; });
  let toastTimer;
  function toast(text) {
    let t = $('.toast'); if (!t) { t = document.createElement('div'); t.className = 'toast'; document.body.appendChild(t); }
    t.textContent = text; clearTimeout(toastTimer); toastTimer = setTimeout(() => t.remove(), 2600);
  }

  /* ---------- conversation (simulated) ---------- */
  const log = $('#convo-log');
  function say(role, html, chips) {
    const m = document.createElement('div'); m.className = 'msg ' + role;
    m.innerHTML = (role === 'nigel' ? '<span class="from">NIGEL · simulated</span>' : '') + html;
    if (chips && chips.length) {
      const c = document.createElement('div'); c.className = 'msg-chips';
      for (const [label, path, fn] of chips) { const b = document.createElement('button'); b.className = 'btn'; b.textContent = label; b.addEventListener('click', () => { if (fn) fn(); else go(path); }); c.appendChild(b); }
      m.appendChild(c);
    }
    log.appendChild(m); log.hidden = false; log.scrollTop = log.scrollHeight;
    return m;
  }
  function reply(q) {
    const t = q.toLowerCase();
    const open = openReviews();
    const sysByName = D.systems.find(s => t.includes(s.name.toLowerCase()) || t.includes(s.id));
    if (/attention|review|needs|what.*(do|urgent)|flag/.test(t)) {
      if (!open.length) return ['Nothing needs attention right now. All sample reviews are resolved.', [['Open attention', '/attention']]];
      return [`${open.length} item${open.length === 1 ? '' : 's'} need attention: ` + open.map(r => `<strong>${esc(r.title)}</strong> (${D.systems.find(s => s.id === r.system).name}, due ${r.due})`).join('; ') + '.', [['Open attention', '/attention'], ...open.slice(0, 1).map(r => [`Go to ${D.systems.find(s => s.id === r.system).name}`, `/systems/${r.system}`])]];
    }
    if (/okonkwo|reyes|household|1035|exchange/.test(t)) {
      const c = caseOf('okonkwo-reyes');
      return [`The Okonkwo-Reyes case is at <strong>${STAGES[c.stage]}</strong>. Fixed index annuity funded by a 1035 exchange, ${money(250000)}. ${c.stage === 1 && !c.memo ? 'The next step is the suitability memo, which I can draft.' : 'You can continue the workflow from the record.'}`, [['Open record', '/systems/paraclete/pending/okonkwo-reyes']]];
    }
    if (/pending|annuity client|pipeline/.test(t)) return ['Four households are pending in Paraclete 1: Okonkwo-Reyes (suitability), Thornbury (illustration), Halvorsen (intake) and Adeyemi-Park (submitted).', [['Open pending clients', '/systems/paraclete/pending']]];
    if (sysByName && /open|go|show|take|jump|navigate/.test(t)) return [`Opening ${esc(sysByName.name)}.`, [], () => go(`/systems/${sysByName.id}`)];
    if (sysByName) { const n = openReviews(sysByName.id).length; return [`${esc(sysByName.name)}: ${esc(sysByName.tagline)}. ${sysByName.sectors.length} sectors, ${n ? n + ' open review' + (n === 1 ? '' : 's') : 'nothing flagged'}.`, [[`Open ${sysByName.name}`, `/systems/${sysByName.id}`]]]; }
    if (/submit|send|transmit/.test(t)) return ['Submissions in this prototype are simulated: the record view animates a transmission and produces a fictional reference. Nothing is sent to a carrier.', [['Open Okonkwo-Reyes', '/systems/paraclete/pending/okonkwo-reyes']]];
    if (/home|star ?cloud|back|overview|reset view/.test(t)) return ['Back to the StarCloud.', [], () => go('/')];
    if (/activity|history|what happened|log/.test(t)) return ['The Activity view lists what NIGEL and you have done, including simulated actions from this session.', [['Open activity', '/activity']]];
    if (/help|what can|how do/.test(t)) return ['I can navigate the cloud, summarise what needs attention, and walk a pending annuity case through suitability, illustration and a simulated submission. Try “what needs attention”, “open AUM”, or “where is the Okonkwo-Reyes case”.', [['What needs attention', '', () => ask('what needs attention')], ['Open Systems', '/systems']]];
    return ['This prototype has no model connected, so I only answer a fixed set of questions. Try “what needs attention”, “open Paraclete 1”, or “where is the Okonkwo-Reyes case”.', [['What needs attention', '', () => ask('what needs attention')], ['Show systems', '/systems']]];
  }
  function ask(q) {
    say('user', esc(q));
    const th = say('nigel', '<span class="dots"><span></span><span></span><span></span></span>'); th.classList.add('thinking');
    setTimeout(() => {
      th.remove();
      const [html, chips, fn] = reply(q);
      say('nigel', html, chips);
      if (fn) setTimeout(fn, 250);
    }, 550 + Math.random() * 350);
  }
  $('#convo-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const inp = $('#convo-input'); const q = inp.value.trim(); if (!q) return;
    inp.value = ''; ask(q);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!$('#modal').hidden) { $('#modal').hidden = true; return; }
      if (!log.hidden && document.activeElement === $('#convo-input')) { log.hidden = true; return; }
      const back = document.querySelector('.crumb-back'); if (back) back.click(); else if (location.hash && location.hash !== '#/') go('/');
    }
  });
  $('#convo-input').addEventListener('focus', () => { if (log.children.length) log.hidden = false; });

  /* ---------- shell wiring ---------- */
  for (const b of document.querySelectorAll('.nav-item')) b.addEventListener('click', () => go(b.dataset.nav === 'starcloud' ? '/' : '/' + b.dataset.nav));
  $('#topbar-attention').addEventListener('click', () => go('/attention'));
  $('#menu-toggle').addEventListener('click', () => { const open = document.body.classList.toggle('menu-open'); $('#menu-toggle').setAttribute('aria-expanded', String(open)); });
  $('#scrim').addEventListener('click', () => { document.body.classList.remove('menu-open'); $('#menu-toggle').setAttribute('aria-expanded', 'false'); });
  window.addEventListener('hashchange', render);
  window.addEventListener('resize', () => { clearTimeout(window.__nigelResize); window.__nigelResize = setTimeout(refreshLabels, 160); });

  SC.start();
  buildLabels();
  render();
  window.NIGEL = { go, state, ask };
})();
