/* Review and saved planning notebook. All rendering uses textContent. */
(function () {
  'use strict';
  const dialog = document.createElement('dialog');
  dialog.id = 'ace-review';
  const heading = document.createElement('h2');
  const close = document.createElement('button'); close.textContent = 'Close';
  close.onclick = () => dialog.close();
  const body = document.createElement('div');
  dialog.append(close, heading, body); document.body.append(dialog);
  async function call(path, data) {
    const response = await fetch(path, {
      method: data === undefined ? 'GET' : 'POST',
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + (localStorage.getItem('ace2_token') || '')},
      ...(data === undefined ? {} : {body: JSON.stringify(data)})
    });
    // A DEAD SESSION ENDS THE SAME WAY EVERYWHERE (2026-09-21). This tray used to report an
    // expired token as "Could not load saved work", which reads as a server fault and hides
    // the one action that fixes it. app.js owns the sign-out, so it does the sign-out.
    if (response.status === 401 && window.__toLogin) { try { window.__toLogin(); } catch (e) {} }
    let result = null;
    try { result = await response.json(); } catch (e) { result = null; }
    return {ok: response.ok, status: response.status, body: result || {}};
  }
  async function api(path, data) {
    const r = await call(path, data);
    if (!r.ok) throw new Error(r.body.detail || r.body.error || 'Could not load saved work.');
    return r.body;
  }
  function text(tag, value, parent) {
    const el = document.createElement(tag); el.textContent = value; parent.append(el); return el;
  }
  /* BELT AND BRACES ON THE WAY TO THE SCREEN (2026-09-21). The server already withholds
     settings and telemetry bodies and redacts excerpts; this is the second pair of eyes,
     sharing app.js's one redactor so the two surfaces cannot drift apart. */
  function redact(value) {
    const s = value == null ? '' : String(value);
    try { return window.__redact ? window.__redact(s) : s; } catch (e) { return s; }
  }
  function safe(tag, value, parent) { return text(tag, redact(value), parent); }
  function button(label, parent, onclick) {
    const b = text('button', label, parent); b.type = 'button'; b.onclick = onclick; return b;
  }
  async function open(mode) {
    heading.textContent = mode === 'plan' ? 'Saved planning notebook' : 'Review actions';
    body.replaceChildren(); if (!dialog.open) dialog.showModal();
    text('p', 'Loading…', body);
    try {
      const data = await api(mode === 'plan' ? '/plan/draft' : '/reviews');
      body.replaceChildren();
      if (mode === 'plan') {
        text('p', 'Drafts and corrections are saved here. Saving does not create calendar events.', body);
        for (const entry of data.entries) {
          const card = document.createElement('article'); body.append(card);
          text('small', entry.role + ' · ' + new Date(entry.saved_at).toLocaleString(), card);
          text('pre', entry.text, card);
        }
        if (!data.entries.length) text('p', 'No saved planning notes yet. Add your starting draft below.', body);
        const label = text('label', 'Add a correction or draft', body);
        const input = document.createElement('textarea'); input.id = 'plan-note'; input.rows = 7; input.maxLength = 40000;
        label.htmlFor = input.id; body.append(input);
        const save = text('button', 'Save note', body);
        save.onclick = async () => {
          save.disabled = true;
          try { await api('/plan/draft', {text: input.value}); await open('plan'); }
          catch (e) { text('p', e.message, body); save.disabled = false; }
        };
      } else {
        text('p', 'Review the exact recipient, resource and details. Approve executes this version. Reject changed or unwanted proposals. Voice confirmations do not execute these actions.', body);
        if (!data.items.length) text('p', 'No actions waiting for review.', body);
        for (const item of data.items) {
          const card = document.createElement('article'); body.append(card);
          text('h3', item.tool.replaceAll('_', ' '), card);
          text('small', item.state + ' · expires ' + new Date(item.expires_at).toLocaleTimeString(), card);
          for (const [key, value] of Object.entries(item.args)) {
            text('strong', key.replaceAll('_', ' '), card);
            text('pre', typeof value === 'string' ? value : JSON.stringify(value, null, 2), card);
          }
          if (item.result) text('pre', item.result, card);
          if (item.state === 'unknown' || item.state === 'executing') text('p', 'Verify the destination before another attempt. This proposal cannot execute again.', card);
          if (item.state === 'pending') {
            const approve = text('button', 'Approve this exact action', card);
            const reject = text('button', 'Reject', card);
            const decide = async (yes) => {
              approve.disabled = reject.disabled = true;
              try { await api('/reviews/' + encodeURIComponent(item.id), {approve: yes}); await open('review'); }
              catch (e) { text('p', e.message + ' Refresh before trying again.', card); }
            };
            approve.onclick = () => decide(true); reject.onclick = () => decide(false);
          }
        }
      }
    } catch (e) { body.replaceChildren(); text('p', e.message, body); }
  }
  /* RECENT ACTIVITY (2026-09-09). Where a progress card's result stays reachable after the
     card dismisses itself. Reuses this tray rather than adding a surface: approvals already
     live here, and a task that needs approval is the same thing seen from the other side. */
  async function activity() {
    heading.textContent = 'Recent activity';
    body.replaceChildren(); if (!dialog.open) dialog.showModal();
    text('p', 'Loading…', body);
    try {
      const data = await api('/actions?limit=25');
      body.replaceChildren();
      const cards = data.cards || [];
      if (!cards.length) {
        text('p', 'Nothing yet. Ask Ace to build something and it will show up here.', body);
        return;
      }
      text('p', 'Everything Ace has run for you, newest first. A link here is the same '
              + 'verified link the card showed — it is only recorded once the file has been '
              + 'read back.', body);
      const LABEL = {queued: 'Queued', working: 'Working', needs_approval: 'Needs approval',
                     completed: 'Done', failed: 'Did not work', cancelled: 'Cancelled'};
      for (const c of cards) {
        const card = document.createElement('article'); body.append(card);
        text('small', (LABEL[c.state] || c.state)
                    + (c.created_at ? ' · ' + new Date(c.created_at).toLocaleString() : ''), card);
        text('h3', c.title || 'Task', card);
        if (c.detail) text('p', c.detail, card);
        if (c.error) text('p', c.error, card);
        for (const w of (c.warnings || [])) text('p', w, card);
        if (c.action && c.action.url) {
          const a = document.createElement('a');
          a.href = c.action.url; a.target = '_blank'; a.rel = 'noopener';
          a.textContent = c.action.label || 'Open';
          card.append(a);
        }
        if (c.state === 'needs_approval') {
          text('p', 'Waiting on you in Review actions. It has not run.', card);
        }
      }
    } catch (e) { body.replaceChildren(); text('p', e.message, body); }
  }
  /* CONNECTIONS (2026-09-10). What Ace is plugged into, in plain words: the account, what he
     is allowed to do with it, when each action last actually worked, and what is broken.
     Reuses this tray rather than adding a surface. Brady should not have to understand MCP
     configuration to find out whether Ace can read his calendar. */
  const KIND_WORDS = {read: 'can read', create: 'can create', edit: 'can change',
                      send: 'can send', share: 'can share', delete: 'can delete'};

  async function connections() {
    heading.textContent = 'Connections';
    body.replaceChildren(); if (!dialog.open) dialog.showModal();
    text('p', 'Checking…', body);
    try {
      const data = await api('/connectors?probe=true');
      body.replaceChildren();
      text('p', data.note || '', body);
      for (const c of data.connectors || []) {
        const card = document.createElement('article'); body.append(card);
        text('h3', c.label || c.name, card);

        const state = !c.configured ? 'Not set up'
                    : c.reachable === false ? 'Not answering'
                    : c.reachable === true ? 'Connected' : 'Set up';
        text('small', state + (c.identity ? ' · ' + c.identity : ''), card);

        if ((c.missing_config || []).length) {
          text('p', 'Missing settings: ' + c.missing_config.join(', ')
                  + ' — these are setting NAMES, not values.', card);
        }
        if (c.cost_note) text('small', c.cost_note, card);

        // What he can actually do, grouped so it reads as capability rather than plumbing.
        const can = (c.actions || []).filter(a => a.enabled);
        const off = (c.actions || []).filter(a => !a.enabled);
        const tested = can.filter(a => a.tested);
        text('p', `${can.length} actions available · ${tested.length} confirmed working here`
                + (off.length ? ` · ${off.length} unavailable` : ''), card);

        const ul = document.createElement('ul'); card.append(ul);
        for (const a of can) {
          const li = document.createElement('li'); ul.append(li);
          const what = (KIND_WORDS[a.kind] || a.kind) + ' — '
                     + a.tool.replace(/^mcp_/, '').replace(/_/g, ' ');
          const bits = [what];
          if (a.approval === 'review') bits.push('needs your approval');
          else if (a.approval_when) bits.push('approval if ' + a.approval_when);
          bits.push(a.tested ? 'last worked ' + String(a.last_tested).slice(0, 10)
                             : 'not tried yet');
          li.textContent = bits.join(' · ');
        }
        for (const a of off) {
          const li = document.createElement('li'); ul.append(li);
          li.textContent = a.tool.replace(/^mcp_/, '').replace(/_/g, ' ')
                         + ' — unavailable: ' + (a.not_enabled_because || 'not enabled');
        }
      }

      // Anything genuinely broken gets named here rather than buried in a status list.
      const inbox = await api('/inbox').catch(() => null);
      const err = inbox && inbox.errors && inbox.errors.personal;
      if (err) {
        const card = document.createElement('article'); body.append(card);
        text('h3', 'Personal Gmail', card);
        text('small', 'Needs reconnecting', card);
        text('p', 'The saved permission has expired or been revoked, so Ace cannot read this '
                + 'account. It stays read-only when you reconnect it — no new access is '
                + 'requested.', card);
        const b = text('button', 'How to reconnect', card);
        b.onclick = () => { b.replaceWith(Object.assign(document.createElement('p'), {
          textContent: 'Reconnecting runs through the connector service\u2019s Google sign-in, '
            + 'which needs its public address temporarily restored. Ask Claude or Codex to '
            + 'walk it with you \u2014 it is a two-minute job but it changes a live setting, '
            + 'so it should not happen from a button press here.' })); };
      }
    } catch (e) { body.replaceChildren(); text('p', e.message, body); }
  }
  /* ══════════════════════════════════════════════ MEMORY REVIEW (2026-09-21)
     WHY THIS SURFACE HAD TO EXIST. Run over the real corpus, the migration promoted 33
     entities and linked 993 of 8649 sources. That is the identity bar working exactly as
     designed — the names Brady says most often are bare first names, and a bare first name
     must never resolve on its own, however many thousand sources it appears in. What it
     leaves behind is roughly a thousand proposals and several thousand unmatched sources
     that, until this panel, nothing in the app could open. A header reading "1030 in the
     review queue" with no way in is a number shaped like an answer.

     Graph seeds are the sharpest case: importing the old model-built cache creates NO
     entity at all, so pressing Confirm here is the ONLY way one ever becomes a record —
     and the type it arrives with is the model's CLAIM (the live cache files two
     organizations as people), so the human sets the type rather than inheriting it.

     Everything listed here is a PROPOSAL. It is styled as one, it says so, and nothing in
     it is accepted until Brady presses a button. */
  const KIND_TITLE = {
    unpromoted_name: 'Name seen often, never promoted',
    ambiguous_name: 'Name that resolves to nobody',
    name_collision: 'Two records answer to one name',
    graph_seed: 'Suggestion from the old map',
    unassigned_source: 'Record that matched nothing',
    merge_candidate: 'Possible duplicate',
    low_confidence_link: 'Weak link',
    discrepancy: 'Disagreement worth a look'
  };
  const KIND_CONSEQUENCE = {
    unpromoted_name: 'Confirming creates a record of the type you choose and links the '
      + 'sources listed above. Nothing else is touched.',
    graph_seed: 'This is the ONLY way a seed becomes a record. The type below is the old '
      + 'map’s CLAIM — it files organizations as people — so you set it, not it. Any edges '
      + 'it came with arrive as unreviewed proposals, not as confirmed links.',
    ambiguous_name: 'Confirming attaches this mention to the record you name below. '
      + 'Without one it refuses rather than guesses.',
    unassigned_source: 'Confirming attaches this source to the record you name below. '
      + 'Without one it refuses rather than guesses.',
    low_confidence_link: 'Confirming attaches this source to the record you name below.',
    merge_candidate: 'Confirming marks the other record as merged into the survivor. No '
      + 'row is deleted and no link is rewritten.',
    name_collision: 'Confirming only acknowledges this. Two people really can answer to '
      + 'one name, so nothing is merged and nothing resolves.',
    discrepancy: 'Confirming only acknowledges this. The board stays the authority on '
      + 'task state.'
  };
  const NEEDS_ENTITY = ['ambiguous_name', 'unassigned_source', 'low_confidence_link'];
  const ENTITY_TYPES = ['person', 'org', 'project'];
  const REASON = {confirm: 'Confirmed in the memory review',
                  reject: 'Rejected in the memory review',
                  dismiss: 'Dismissed in the memory review'};

  function subjectOf(r) {
    const p = r.payload || {};
    return p.name || p.label || p.alias || (p.names || []).join('  ·  ')
        || p.source_id || p.alias_norm || r.subject_key || ('review ' + r.review_id);
  }
  function sourceCountOf(r) {
    const p = r.payload || {};
    return Number(p.source_count || (p.sources || []).length || 0);
  }
  function outcomeWords(action, body) {
    const d = (body && body.applied) || {};
    if (action !== 'confirm') {
      // A TOMBSTONE LOOKS FORWARD AS WELL AS BACK. Rejecting retracts what the name already
      // matched AND stops it resolving for sources indexed after today, so the decision is
      // not quietly undone by the next batch.
      return 'Closed. This is a permanent tombstone — re-running the migration will not '
           + 'reopen it, and this name will not resolve for new records either.';
    }
    const bits = ['Confirmed.'];
    if (d.entity_id) {
      bits.push(d.entity_created
        ? 'Created ' + d.entity_id + (d.type_used ? ' as a ' + d.type_used : '') + '.'
        : 'Attached to ' + d.entity_id + '.');
    }
    if (d.sources_linked) bits.push(d.sources_linked + ' source(s) linked.');
    if (d.relations_proposed) {
      bits.push(d.relations_proposed + ' relation(s) recorded as UNREVIEWED proposals — '
              + 'confirming the node did not confirm its edges.');
    }
    if (d.loser) bits.push(d.loser + ' marked merged; no row was deleted.');
    if (d.acknowledged) bits.push('Acknowledged only — nothing structural was written.');
    return bits.join(' ');
  }

  function reviewRow(r, host, onDecided) {
    const p = r.payload || {};
    const card = document.createElement('article');
    card.className = 'rv-prop'; card.dataset.review = String(r.review_id);
    card.dataset.kind = r.kind;
    host.append(card);
    const n = sourceCountOf(r);
    text('small', 'PROPOSAL · ' + (KIND_TITLE[r.kind] || r.kind) + ' · priority '
       + r.priority + (n ? ' · ' + n + ' source' + (n === 1 ? '' : 's') : '')
       + (r.created_at ? ' · ' + String(r.created_at).slice(0, 10) : ''), card);
    safe('h3', subjectOf(r), card);
    if (p.why) safe('p', p.why, card);

    // The evidence, quoted rather than characterised — ids so a claim can be checked.
    if ((p.sources || []).length) {
      safe('small', 'Sources: ' + p.sources.join(', ')
         + (n > p.sources.length ? ' … and ' + (n - p.sources.length) + ' more' : ''), card);
    }
    if (p.source_id) safe('small', 'Source: ' + p.source_id, card);
    if ((p.candidate_names || []).length) {
      safe('small', 'Names in it: ' + p.candidate_names.join(', '), card);
    }
    if ((p.entity_ids || []).length) {
      safe('small', 'Existing records involved: ' + p.entity_ids.join(', '), card);
    }
    if (p.person_context_sources || p.org_context_sources) {
      safe('small', 'Read as a person in ' + (p.person_context_sources || 0)
         + ' source(s), as an organization in ' + (p.org_context_sources || 0) + '.', card);
    }
    if (p.similarity) {
      safe('small', 'Name similarity — surname ' + p.similarity.last + ', first name '
         + p.similarity.first + '. A suggestion only; nothing was merged.', card);
    }
    (p.edges || []).slice(0, 8).forEach((e) => {
      safe('small', 'Claimed link: ' + (e.source || '?') + ' → ' + (e.target || '?')
         + ' (' + String(e.kind || 'related_to').replace(/_/g, ' ') + ')', card);
    });

    const controls = document.createElement('div');
    controls.className = 'rv-ctl'; card.append(controls);
    if (KIND_CONSEQUENCE[r.kind]) text('p', KIND_CONSEQUENCE[r.kind], controls)
      .className = 'rv-consequence';

    // THE HUMAN SETS THE TYPE. The old cache's type is displayed as a claim, never applied
    // silently — it calls PFI and GFI Legends people.
    let typeSel = null;
    if (r.kind === 'graph_seed' || r.kind === 'unpromoted_name') {
      const lab = text('label', 'Record type — your call, not the model’s', controls);
      typeSel = document.createElement('select');
      typeSel.className = 'rv-type';
      ENTITY_TYPES.forEach((t) => {
        const o = document.createElement('option'); o.value = t; o.textContent = t;
        typeSel.append(o);
      });
      const claimed = String(p.claimed_type || '');
      typeSel.value = ENTITY_TYPES.indexOf(claimed) >= 0 ? claimed : 'person';
      lab.append(typeSel);
      if (claimed && claimed !== 'unknown') {
        safe('small', 'The old map claimed: ' + claimed + ' — a claim, not an acceptance.',
             controls);
      }
    }

    let winnerSel = null;
    if (r.kind === 'merge_candidate') {
      const ids = p.entity_ids || [], names = p.names || [];
      const lab = text('label', 'Which record survives?', controls);
      winnerSel = document.createElement('select'); winnerSel.className = 'rv-into';
      ids.forEach((id, i) => {
        const o = document.createElement('option');
        o.value = id; o.textContent = redact(names[i] || id) + ' (' + id + ')';
        winnerSel.append(o);
      });
      lab.append(winnerSel);
    }

    // WHICH RECORD? An ambiguous name and an unmatched source have no answer of their own,
    // so the human names one. Nothing is guessed, and Confirm without a choice is refused
    // by the server rather than resolved to a best guess here.
    let chosen = '';
    let picked = null;
    if (NEEDS_ENTITY.indexOf(r.kind) >= 0) {
      picked = text('small', 'No record chosen yet — Confirm will refuse rather than guess.',
                    controls);
      picked.className = 'rv-picked';
      const offer = document.createElement('div'); controls.append(offer);
      const choose = (id, label) => {
        chosen = id;
        picked.textContent = 'Chosen: ' + redact(label || id) + ' (' + id + ')';
        picked.className = 'rv-picked on';
      };
      (p.entity_ids || []).forEach((id) => button(id, offer, () => choose(id)));
      const lab = text('label', 'Or find the record by name', controls);
      const find = document.createElement('input');
      find.type = 'search'; find.className = 'rv-find';
      find.placeholder = 'search the register…';
      lab.append(find);
      const hits = document.createElement('div'); hits.className = 'rv-hits';
      controls.append(hits);
      let timer = 0;
      find.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          hits.replaceChildren();
          const q = find.value.trim(); if (!q) return;
          const res = await call('/entities?limit=6&q=' + encodeURIComponent(q));
          const rows = (res.body || {}).entities || [];
          if (!rows.length) { text('small', 'No record on file by that name.', hits); return; }
          rows.forEach((e) => button(redact(e.display_name) + ' · ' + e.type, hits,
                                     () => choose(e.entity_id, e.display_name)));
        }, 220);
      });
    }

    const bar = document.createElement('div'); bar.className = 'rv-bar';
    controls.append(bar);
    const say = text('p', '', card);
    say.className = 'rv-say'; say.setAttribute('role', 'status');
    const btns = [];
    function decide(action) {
      const args = {};
      if (typeSel) args.type = typeSel.value;
      if (winnerSel) {
        args.into = winnerSel.value;
        const others = (p.entity_ids || []).filter((i) => i !== winnerSel.value);
        if (others.length === 1) args.loser = others[0];
      }
      if (chosen) args.entity_id = chosen;
      btns.forEach((b) => { b.disabled = true; });
      say.textContent = '…';
      call('/entities/review/' + encodeURIComponent(r.review_id),
           {action: action, args: args, reason: REASON[action]})
        .then((res) => {
          const out = res.body || {};
          if (!res.ok || !out.ok) {
            say.textContent = out.error
              || 'That decision did not go through, and nothing was written.';
            btns.forEach((b) => { b.disabled = false; });
            return;
          }
          // Reflected in place: the queue does not reload under him mid-decision.
          card.className = action === 'confirm' ? 'rv-done' : 'rv-closed';
          card.dataset.decided = action;
          controls.replaceChildren();
          say.textContent = outcomeWords(action, out);
          if (onDecided) onDecided(r, action, out);
        })
        .catch(() => {
          say.textContent = 'That decision did not go through, and nothing was written.';
          btns.forEach((b) => { b.disabled = false; });
        });
    }
    [['Confirm', 'confirm', 'Write this decision to the record.'],
     ['Not the same — reject', 'reject',
      'Permanent: the name stops resolving for new records too, and a migration replay '
      + 'will not reopen it.'],
     ['Dismiss', 'dismiss', 'Close it without deciding. Nothing structural is written.']
    ].forEach(([label, action, why]) => {
      const b = button(label, bar, () => decide(action));
      b.title = why; b.dataset.action = action;
      btns.push(b);
    });
    return card;
  }

  /* THREE DIFFERENT ANSWERS, NEVER ONE (2026-09-21). "Nothing is indexed yet" and "the
     index is unreachable" are opposite facts that would otherwise both render as an empty
     list, which is the shape of a confident lie. `absent` says what to run and reminds him
     recall still reads the original records; `unavailable` (a 503) claims nothing at all
     about what is or is not on file. */
  function indexBanner(state, host) {
    if (state === 'unavailable') {
      const b = text('p', 'The memory index could not be reached. This is NOT an empty '
        + 'result — nothing is being claimed about what is or is not on file. Try again in '
        + 'a moment.', host);
      b.className = 'rv-down'; b.id = 'rv-index-state'; b.dataset.state = 'unavailable';
      return true;
    }
    if (state === 'absent') {
      const b = text('p', 'The memory index has not been built yet. An empty answer here '
        + 'means NOT INDEXED — not "nothing exists". Ace still searches the original '
        + 'records when he recalls something.', host);
      b.className = 'rv-absent'; b.id = 'rv-index-state'; b.dataset.state = 'absent';
      return true;
    }
    return false;
  }

  let rvKind = '';
  async function memory(kind) {
    if (kind !== undefined) rvKind = kind || '';
    heading.textContent = 'Memory review';
    body.replaceChildren(); if (!dialog.open) dialog.showModal();
    text('p', 'Loading…', body);
    try {
      const res = await call('/entities/review?state=open&limit=100'
        + (rvKind ? '&kind=' + encodeURIComponent(rvKind) : ''));
      if (res.status === 503) {
        body.replaceChildren(); indexBanner('unavailable', body); return;
      }
      if (!res.ok) throw new Error((res.body || {}).detail || (res.body || {}).error
                                   || 'The review queue could not be read.');
      const data = res.body || {};
      body.replaceChildren();
      indexBanner(data.index_state, body);
      const counts = data.counts || {}, byKind = Object.assign({}, counts.by_kind || {});
      const head = text('p', '', body); head.className = 'rv-head';

      const jump = document.createElement('div'); jump.className = 'rv-jump'; body.append(jump);
      button('Find a source', jump, () => sources());

      const filters = document.createElement('div');
      filters.className = 'rv-filters'; body.append(filters);
      const chip = (k, label, n) => {
        const b = button(label + ' · ' + n, filters, () => memory(k));
        b.dataset.kind = k;
        if ((rvKind || '') === k) b.className = 'on';
      };
      chip('', 'Everything', counts.total || 0);
      Object.keys(byKind).sort((a, b) => byKind[b] - byKind[a])
        .forEach((k) => chip(k, KIND_TITLE[k] || k, byKind[k]));

      (data.notes || []).forEach((note) => safe('small', note, body));

      // SERVER ORDER, UNTOUCHED. The stored priority carries evidence strength now — a
      // name seen in hundreds of sources sorts above a single sighting, and graph seeds
      // sort last because they are the least trustworthy evidence in the system. A second
      // opinion applied here would only be able to disagree with it.
      const rows = (data.reviews || []).slice();
      const list = document.createElement('div'); list.id = 'rv-list'; body.append(list);
      if (!rows.length) {
        text('p', rvKind ? 'Nothing open of that kind.'
                         : 'Nothing waiting. Every proposal has been answered.', list);
      }
      const repaint = () => {
        const total = Object.keys(byKind).reduce((s, k) => s + byKind[k], 0);
        head.textContent = total + ' proposal(s) open · showing ' + rows.length
          + (rvKind ? ' of ' + (byKind[rvKind] || 0) + ' ' + (KIND_TITLE[rvKind] || rvKind)
                    : ' of ' + total)
          + ' · nothing here has been accepted';
      };
      rows.forEach((r) => reviewRow(r, list, (row) => {
        byKind[row.kind] = Math.max(0, (byKind[row.kind] || 1) - 1);
        repaint();
      }));
      repaint();
      if (data.total > rows.length) {
        text('small', (data.total - rows.length) + ' more are not on this page — filter by '
           + 'kind to reach them.', body);
      }
    } catch (e) { body.replaceChildren(); text('p', e.message, body); }
  }

  /* ══════════════════════════════════════════ SOURCE SEARCH (2026-09-21)
     `/entities?q=` searches ENTITIES, and a source that matched nothing has none — so
     thousands of unassigned rows were unreachable by construction. This searches the index
     itself. An excluded row (settings, watch telemetry, QA text) is listed and counted but
     its body is deliberately withheld by the server: that is where a credential would be
     if one ever reached the corpus. The row says WHY rather than showing an empty box that
     looks broken. */
  const SRC_FILTERS = [
    ['status', 'Status', ['', 'indexed', 'unassigned', 'ambiguous', 'excluded']],
    ['corpus', 'Where from', ['', 'fact', 'turn', 'item', 'profile', 'summary']],
    ['class', 'Kind of record', ['', 'user_statement', 'assistant_inference',
      'legacy_extracted', 'secondary_summary', 'internal_metadata', 'test_data']]
  ];
  const srcQ = {q: '', status: '', corpus: '', class: ''};

  async function sources(preset) {
    Object.assign(srcQ, preset || {});
    heading.textContent = 'Find a source';
    body.replaceChildren(); if (!dialog.open) dialog.showModal();

    const jump = document.createElement('div'); jump.className = 'rv-jump'; body.append(jump);
    button('Memory review', jump, () => memory());
    text('p', 'Every indexed record, including the ones that matched no entity. Nothing is '
       + 'dropped from this index — an unmatched source is listed as unassigned, not lost.',
         body);

    const form = document.createElement('div'); form.className = 'rv-srcform';
    body.append(form);
    const q = document.createElement('input');
    q.type = 'search'; q.id = 'rv-q'; q.placeholder = 'text in the record…'; q.value = srcQ.q;
    text('label', 'Search text', form).append(q);
    SRC_FILTERS.forEach(([key, label, options]) => {
      const sel = document.createElement('select');
      sel.id = 'rv-' + key;
      options.forEach((o) => {
        const opt = document.createElement('option');
        opt.value = o; opt.textContent = o ? o.replace(/_/g, ' ') : 'any';
        sel.append(opt);
      });
      sel.value = srcQ[key] || '';
      sel.onchange = () => { srcQ[key] = sel.value; run(); };
      text('label', label, form).append(sel);
    });
    const out = document.createElement('div'); out.id = 'rv-src-out'; body.append(out);

    let sourceOffset = 0, sourceRequest = 0;
    async function run(offset = 0) {
      sourceOffset = Math.max(0, offset);
      const request = ++sourceRequest;
      srcQ.q = q.value;
      out.replaceChildren(); text('p', 'Searching…', out);
      const res = await call('/sources/search?limit=40&offset=' + sourceOffset
        + '&q=' + encodeURIComponent(srcQ.q) + '&status=' + encodeURIComponent(srcQ.status)
        + '&corpus=' + encodeURIComponent(srcQ.corpus)
        + '&class=' + encodeURIComponent(srcQ.class));
      if (request !== sourceRequest || !out.isConnected) return;
      out.replaceChildren();
      if (res.status === 503) { indexBanner('unavailable', out); return; }
      if (!res.ok) {
        text('p', (res.body || {}).detail || (res.body || {}).error
           || 'The source search could not be run.', out);
        return;
      }
      const data = res.body || {};
      indexBanner(data.index_state, out);
      const c = data.counts || {};
      const line = text('p', (data.total || 0) + ' matching · showing ' + (data.returned || 0)
        + ' · unassigned ' + (c.unassigned || 0) + ' · ambiguous ' + (c.ambiguous || 0)
        + ' · excluded ' + (c.excluded || 0) + ' · linked ' + (c.indexed || 0), out);
      line.className = 'rv-head'; line.id = 'rv-src-counts';
      (data.notes || []).forEach((note) => safe('small', note, out));
      if (!(data.sources || []).length) {
        text('p', 'No indexed record matches that. This says nothing about what is in the '
           + 'original records — only about what the index reaches.', out);
      }
      (data.sources || []).forEach((s) => {
        const card = document.createElement('article');
        card.className = 'rv-src' + (s.status === 'excluded' ? ' rv-excluded' : '');
        card.dataset.status = s.status || '';
        card.dataset.source = s.source_id || '';
        out.append(card);
        text('small', [s.source_id, s.corpus, String(s.occurred_at || '').slice(0, 10),
                       s.role, s.source_class, s.status].filter(Boolean).join(' · '), card);
        if (s.excerpt) safe('pre', s.excerpt, card);
        else if (s.excerpt_withheld) {
          safe('p', 'Body withheld — ' + s.excerpt_withheld, card).className = 'rv-withheld';
        } else {
          text('p', 'No excerpt is available for this record.', card).className = 'rv-withheld';
        }
        if (s.excluded_reason) {
          safe('small', 'Excluded: ' + s.excluded_reason
             + ' — still indexed, still counted, still searchable.', card);
        }
      });
      const paging = document.createElement('div'); paging.className = 'rv-jump'; out.append(paging);
      if (sourceOffset > 0) button('Previous records', paging, () => run(Math.max(0, sourceOffset - 40)));
      if (data.truncated) button('Next records', paging, () => run(sourceOffset + 40));
    }
    q.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
    button('Search', form, () => run());
    await run();
  }

  /* One way in from anywhere: the graph header calls these, and so does More. */
  window.aceReview = {queue: memory, sources: sources};

  const connBtn = document.getElementById('connections-open');
  if (connBtn) connBtn.onclick = () => connections();

  const actBtn = document.getElementById('activity-open');
  if (actBtn) actBtn.onclick = () => activity();
  const revQueue = document.getElementById('memory-review-open');
  if (revQueue) revQueue.onclick = () => memory('');
  const srcBtn = document.getElementById('source-search-open');
  if (srcBtn) srcBtn.onclick = () => sources();
  document.getElementById('review-open').onclick = () => open('review');
  document.getElementById('draft-open').onclick = () => open('plan');
})();
