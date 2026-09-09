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
  async function api(path, data) {
    const response = await fetch(path, {
      method: data === undefined ? 'GET' : 'POST',
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + (localStorage.getItem('ace2_token') || '')},
      ...(data === undefined ? {} : {body: JSON.stringify(data)})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Could not load saved work.');
    return result;
  }
  function text(tag, value, parent) {
    const el = document.createElement(tag); el.textContent = value; parent.append(el); return el;
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
  const actBtn = document.getElementById('activity-open');
  if (actBtn) actBtn.onclick = () => activity();
  document.getElementById('review-open').onclick = () => open('review');
  document.getElementById('draft-open').onclick = () => open('plan');
})();
