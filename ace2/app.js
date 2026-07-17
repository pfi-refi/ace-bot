/* ============================================================
   ACE 2.0 — the stage controller
   Breathing-galaxy orb · assistant-materialized cards · voice
   No dashboard: Ace populates the screen (card/open WS events).
   ============================================================ */
(function () {
  'use strict';

  var API = '';
  var state = {
    token: localStorage.getItem('ace2_token') || '',
    authRequired: true,
    ws: null, wsReady: false, busy: false,
    micActive: false, recognition: null,
    voiceOut: localStorage.getItem('ace2_voice') !== 'off',
    reconnectDelay: 1000,
  };
  var $ = function (id) { return document.getElementById(id); };

  /* ============================================================ DEEP SPACE
     Starfield background — slow parallax drift, twinkle, rare shooting star. */
  (function () {
    var canvas = $('matrix'), ctx = canvas.getContext('2d');
    var stars = [], meteors = [], running = true;
    function resize() {
      canvas.width = window.innerWidth; canvas.height = window.innerHeight;
      stars = [];
      var n = Math.floor(canvas.width * canvas.height / 9000);
      for (var i = 0; i < n; i++) stars.push({
        x: Math.random() * canvas.width, y: Math.random() * canvas.height,
        z: .3 + Math.random() * .7, s: .4 + Math.random() * 1.4,
        tw: Math.random() * 6.283, ts: .3 + Math.random() * 1.2,
        hue: Math.random() < .78 ? '180,255,220' : (Math.random() < .6 ? '150,230,255' : '200,190,255'),
      });
    }
    function frame(now) {
      if (!running) return;
      var t = now / 1000;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i];
        s.x += .012 * s.z; if (s.x > canvas.width + 2) s.x = -2;
        var a = (.25 + .45 * (.5 + .5 * Math.sin(t * s.ts + s.tw))) * s.z;
        ctx.fillStyle = 'rgba(' + s.hue + ',' + a.toFixed(3) + ')';
        ctx.beginPath(); ctx.arc(s.x, s.y, s.s * s.z, 0, 6.283); ctx.fill();
      }
      if (Math.random() < .0015 && meteors.length < 1) {
        meteors.push({ x: Math.random() * canvas.width * .8, y: Math.random() * canvas.height * .3, vx: 7 + Math.random() * 5, vy: 3 + Math.random() * 2, life: 1 });
      }
      for (var m = meteors.length - 1; m >= 0; m--) {
        var mt = meteors[m]; mt.x += mt.vx; mt.y += mt.vy; mt.life -= .02;
        if (mt.life <= 0) { meteors.splice(m, 1); continue; }
        var g = ctx.createLinearGradient(mt.x - mt.vx * 6, mt.y - mt.vy * 6, mt.x, mt.y);
        g.addColorStop(0, 'rgba(69,255,166,0)'); g.addColorStop(1, 'rgba(180,255,220,' + (.7 * mt.life).toFixed(3) + ')');
        ctx.strokeStyle = g; ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.moveTo(mt.x - mt.vx * 6, mt.y - mt.vy * 6); ctx.lineTo(mt.x, mt.y); ctx.stroke();
      }
      requestAnimationFrame(frame);
    }
    resize(); window.addEventListener('resize', resize);
    document.addEventListener('visibilitychange', function () {
      var was = running; running = document.visibilityState === 'visible';
      if (running && !was) requestAnimationFrame(frame);
    });
    requestAnimationFrame(frame);
  })();

  /* ============================================================ GALAXY ORB
     A breathing galaxy: nebula clouds, spiral-arm stars, luminous core.
     Backing store 480×480 (crisp at 320px CSS), logical coords 240 via scale(2).
     States ease; setAmplitude() lets Ace's voice surge the whole galaxy. */
  var orb = (function () {
    var canvas = $('orb-canvas'), ctx = canvas.getContext('2d');
    ctx.scale(2, 2);
    var W = 240, cx = 120, cy = 120, R = 112;
    var speed = 1, speedT = 1, glow = 0, glowT = 0, amp = 0;
    var last = performance.now();

    // Nebula clouds — big soft color fields that slowly orbit and morph
    var clouds = [];
    var hues = ['69,255,166', '79,227,255', '69,255,166', '143,123,255', '79,227,255', '69,255,166'];
    for (var i = 0; i < 6; i++) clouds.push({
      dist: 18 + Math.random() * 55, ang: Math.random() * 6.283,
      spd: (.02 + Math.random() * .045) * (i % 2 ? 1 : -1),
      r: 40 + Math.random() * 44, wob: Math.random() * 6.283, hue: hues[i], a: .09 + Math.random() * .07,
    });
    // Spiral arms — three arms of orbiting stars along a log spiral
    var starsG = [];
    for (var arm = 0; arm < 3; arm++) for (var j = 0; j < 52; j++) {
      var tt = j / 52, rr = 12 + tt * 96;
      starsG.push({
        r: rr * (0.92 + Math.random() * .16),
        a0: arm * 2.094 + tt * 3.6 + (Math.random() - .5) * .5,
        w: .22 / (0.35 + tt),                       // inner stars orbit faster
        s: .5 + Math.random() * 1.5 * (1 - tt * .5),
        tw: Math.random() * 6.283, ts: .5 + Math.random() * 1.6,
        cyan: Math.random() < .3,
      });
    }

    function setState(s) {
      if (s === 'speaking') { speedT = 2.2; glowT = 1; }
      else if (s === 'listening') { speedT = 1.7; glowT = .6; }
      else { speedT = 1; glowT = 0; amp = 0; }
    }
    function setAmplitude(v) { amp = Math.max(0, Math.min(1, v)); }

    function frame(now) {
      var t = now / 1000; last = now;
      speed += (speedT - speed) * .04; glow += (glowT - glow) * .05;
      // THE BREATH — slow inhale/exhale of scale and light; voice deepens it
      var breath = 1 + .045 * Math.sin(t * .55) + amp * .1;
      var light = .75 + .25 * Math.sin(t * .55 + .8) + glow * .5 + amp * .8;

      ctx.clearRect(0, 0, W, W);
      ctx.save();
      ctx.beginPath(); ctx.arc(cx, cy, R + 6, 0, 6.283); ctx.clip();
      ctx.translate(cx, cy); ctx.scale(breath, breath); ctx.translate(-cx, -cy);

      // deep vignette base
      var base = ctx.createRadialGradient(cx, cy, 10, cx, cy, R + 10);
      base.addColorStop(0, 'rgba(6,20,18,.9)'); base.addColorStop(1, 'rgba(1,4,5,.2)');
      ctx.fillStyle = base; ctx.beginPath(); ctx.arc(cx, cy, R + 8, 0, 6.283); ctx.fill();

      // nebula clouds (additive)
      ctx.globalCompositeOperation = 'lighter';
      for (var i = 0; i < clouds.length; i++) {
        var c = clouds[i];
        var ang = c.ang + t * c.spd * speed;
        var wr = c.r * (1 + .12 * Math.sin(t * .4 + c.wob));
        var x = cx + Math.cos(ang) * c.dist, y = cy + Math.sin(ang) * c.dist * .62;
        var g = ctx.createRadialGradient(x, y, 0, x, y, wr);
        g.addColorStop(0, 'rgba(' + c.hue + ',' + (c.a * light).toFixed(3) + ')');
        g.addColorStop(1, 'rgba(' + c.hue + ',0)');
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, wr, 0, 6.283); ctx.fill();
      }

      // spiral-arm stars
      for (var k = 0; k < starsG.length; k++) {
        var st = starsG[k];
        var a = st.a0 + t * st.w * speed;
        var x2 = cx + Math.cos(a) * st.r, y2 = cy + Math.sin(a) * st.r * .62;
        var tw = .35 + .55 * (.5 + .5 * Math.sin(t * st.ts + st.tw));
        ctx.fillStyle = st.cyan
          ? 'rgba(160,240,255,' + (tw * light * .9).toFixed(3) + ')'
          : 'rgba(190,255,225,' + (tw * light * .9).toFixed(3) + ')';
        ctx.beginPath(); ctx.arc(x2, y2, st.s, 0, 6.283); ctx.fill();
      }

      // luminous core
      var coreR = 26 + amp * 10;
      var core = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
      core.addColorStop(0, 'rgba(235,255,245,' + (.85 * light).toFixed(3) + ')');
      core.addColorStop(.35, 'rgba(69,255,166,' + (.5 * light).toFixed(3) + ')');
      core.addColorStop(1, 'rgba(69,255,166,0)');
      ctx.fillStyle = core; ctx.beginPath(); ctx.arc(cx, cy, coreR, 0, 6.283); ctx.fill();

      ctx.globalCompositeOperation = 'source-over';
      ctx.restore();

      // halo rim — soft, breathing with the galaxy
      ctx.save();
      ctx.shadowColor = 'rgba(69,255,166,.8)'; ctx.shadowBlur = 16 + glow * 18 + amp * 14;
      ctx.strokeStyle = 'rgba(69,255,166,' + (.22 + glow * .3 + amp * .3).toFixed(3) + ')';
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(cx, cy, (R + 4) * breath, 0, 6.283); ctx.stroke();
      ctx.restore();

      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
    return { setState: setState, setAmplitude: setAmplitude };
  })();

  function setOrbState(s) {
    orb.setState(s);
    var el = $('orb-state'); el.classList.remove('listening', 'speaking');
    if (s === 'listening') { el.classList.add('listening'); el.textContent = '[ LISTENING ]'; }
    else if (s === 'speaking') { el.classList.add('speaking'); el.textContent = '[ SPEAKING ]'; }
    else el.textContent = '[ IDLE ]';
  }

  /* ============================================================ CLOCK */
  (function () {
    var days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
    function tick() {
      var d = new Date(), h = d.getHours(), ap = h >= 12 ? 'PM' : 'AM', h12 = h % 12 || 12;
      var mm = (d.getMinutes() < 10 ? '0' : '') + d.getMinutes();
      $('clock').innerHTML = days[d.getDay()] + ' · <b>' + h12 + ':' + mm + '</b> ' + ap;
    }
    tick(); setInterval(tick, 15000);
  })();

  /* ============================================================ AUTH */
  function headers() { var h = { 'Content-Type': 'application/json' }; if (state.token) h.Authorization = 'Bearer ' + state.token; return h; }
  function saveToken(t) { state.token = t || ''; if (t) localStorage.setItem('ace2_token', t); else localStorage.removeItem('ace2_token'); }
  function toLogin(lockedMsg) {
    saveToken(''); $('app').classList.add('hidden'); $('login').classList.remove('hidden');
    if (lockedMsg) {
      $('login-err').textContent = lockedMsg;
      $('login-input').disabled = true; $('login-btn').disabled = true;
    } else { $('login-input').disabled = false; $('login-btn').disabled = false; $('login-input').focus(); }
  }

  function boot() {
    fetch(API + '/health').then(function (r) { return r.json(); }).then(function (h) {
      state.authRequired = !!h.auth_required;
      if (h.locked) { toLogin('LOCKED — SET ACE2_PASSWORD IN RAILWAY'); return; }
      if (!state.authRequired) { startApp(); return; }
      if (!state.token) { toLogin(); return; }
      fetch(API + '/session', { headers: headers() }).then(function (r) { r.ok ? startApp() : toLogin(); })
        .catch(function () { startApp(); });
    }).catch(function () { toLogin(); });
  }
  function doLogin() {
    var pw = $('login-input').value; $('login-err').textContent = '';
    fetch(API + '/auth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw }) })
      .then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .then(function (d) { saveToken(d.token || ''); $('login').classList.add('hidden'); startApp(); })
      .catch(function () { $('login-err').textContent = 'ACCESS DENIED'; $('login-input').value = ''; });
  }
  $('login-btn').addEventListener('click', doLogin);
  $('login-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });

  function startApp() {
    $('app').classList.remove('hidden');
    connectWS();
    greeting();
  }
  function greeting() {
    var d = new Date(), days = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    addAceMessage('Online. ' + days[d.getDay()] + '. Calendar, tasks, mail, memory — live, and the screen is mine to use. What are we moving first?');
  }

  /* ============================================================ WEBSOCKET */
  function wsURL() { var p = location.protocol === 'https:' ? 'wss:' : 'ws:'; var q = state.token ? ('?token=' + encodeURIComponent(state.token)) : ''; return p + '//' + location.host + '/ws/chat' + q; }
  function connectWS() {
    var ws; try { ws = new WebSocket(wsURL()); } catch (e) { setLink(false); scheduleReconnect(); return; }
    state.ws = ws;
    ws.onopen = function () { state.wsReady = true; state.reconnectDelay = 1000; setLink(true); };
    ws.onclose = function (ev) { state.wsReady = false; setLink(false); if (ev && ev.code === 4401) { toLogin(); return; } scheduleReconnect(); };
    ws.onerror = function () { setLink(false); };
    ws.onmessage = function (ev) { try { handleWSEvent(JSON.parse(ev.data)); } catch (e) {} };
  }
  function scheduleReconnect() { setTimeout(connectWS, state.reconnectDelay); state.reconnectDelay = Math.min(15000, state.reconnectDelay * 1.7); }
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible' && !state.wsReady) { state.reconnectDelay = 1000; connectWS(); } });

  var streamMsg = null, activeTool = null;
  function handleWSEvent(msg) {
    switch (msg.type) {
      case 'start': removeTyping(); setOrbState('speaking'); streamMsg = beginAceStream(); break;
      case 'delta': if (!streamMsg) streamMsg = beginAceStream(); appendToStream(streamMsg, msg.text); break;
      case 'tool': renderTool(msg); break;
      case 'card': materializeCard(msg.panel, msg.data); break;
      case 'open': openLink(msg.url, msg.label); break;
      case 'confirmation': renderConfirm(msg.text); break;
      case 'final': if (streamMsg) finalizeStream(streamMsg, msg.text); break;
      case 'error': removeTyping(); discardEmptyStream(); addAceMessage(msg.text); break;
      case 'done': discardEmptyStream(); streamMsg = null; activeTool = null; state.busy = false;
        if (!ttsPlaying) setOrbState(state.micActive ? 'listening' : 'idle'); break;
    }
  }

  function sendMessage(text) {
    text = (text || $('chat-input').value).trim();
    if (!text || state.busy) return;
    state.busy = true; $('chat-input').value = '';
    addUserMessage(text); showTyping(); setOrbState('listening');
    if (state.wsReady && state.ws) { state.ws.send(JSON.stringify({ message: text })); }
    else {
      fetch(API + '/chat', { method: 'POST', headers: headers(), body: JSON.stringify({ message: text }) })
        .then(function (r) { if (r.status === 401) { toLogin(); throw 0; } return r.json(); })
        .then(function (d) { removeTyping(); if (d.reply) { setOrbState('speaking'); addAceMessage(d.reply); speak(d.reply); }
          (d.confirmations || []).forEach(renderConfirm); state.busy = false; if (!ttsPlaying) setOrbState('idle'); })
        .catch(function () { removeTyping(); addAceMessage('⚠️ Link failed. Reconnecting…'); state.busy = false; setOrbState('idle'); });
    }
  }

  /* ============================================================ TRANSCRIPT */
  var messagesEl = $('messages');
  function scrollBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }
  function nowLabel() { var d = new Date(), h = d.getHours(), ap = h >= 12 ? 'PM' : 'AM', h12 = h % 12 || 12; return h12 + ':' + (d.getMinutes() < 10 ? '0' : '') + d.getMinutes() + ' ' + ap; }
  function addUserMessage(t) { var m = document.createElement('div'); m.className = 'msg user'; m.appendChild(document.createTextNode(t)); messagesEl.appendChild(m); scrollBottom(); }
  function addAceMessage(t) { var m = document.createElement('div'); m.className = 'msg ace'; m.innerHTML = '<div class="sender">ACE</div>'; m.appendChild(document.createTextNode(t)); var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel(); m.appendChild(ts); messagesEl.appendChild(m); scrollBottom(); return m; }
  function beginAceStream() { var m = document.createElement('div'); m.className = 'msg ace'; m.innerHTML = '<div class="sender">ACE</div>'; var b = document.createElement('span'); m.appendChild(b); var c = document.createElement('span'); c.className = 'cursor'; c.textContent = ' '; m.appendChild(c); messagesEl.appendChild(m); scrollBottom(); return { el: m, body: b, cursor: c, text: '' }; }
  function appendToStream(s, t) { s.text += t; s.body.textContent = s.text; scrollBottom(); }
  function discardEmptyStream() { if (streamMsg && !streamMsg.text && streamMsg.el.parentNode) { streamMsg.el.parentNode.removeChild(streamMsg.el); streamMsg = null; } }
  function finalizeStream(s, txt) { s.body.textContent = txt || s.text; if (s.cursor.parentNode) s.cursor.parentNode.removeChild(s.cursor); var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel(); s.el.appendChild(ts); speak(txt || s.text); scrollBottom(); }
  function renderTool(msg) {
    if (msg.status === 'running') { activeTool = document.createElement('div'); activeTool.className = 'tool-pill'; activeTool.innerHTML = '<span class="spin">◈</span> '; activeTool.appendChild(document.createTextNode(msg.label + '…')); messagesEl.appendChild(activeTool); scrollBottom(); }
    else if (activeTool) { activeTool.className = 'tool-pill done'; activeTool.innerHTML = '◈ '; activeTool.appendChild(document.createTextNode(msg.label)); activeTool = null; scrollBottom(); }
  }
  function renderConfirm(text) { var p = document.createElement('div'); p.className = 'tool-pill done'; p.appendChild(document.createTextNode(text)); messagesEl.appendChild(p); scrollBottom(); }
  var typingEl = null;
  function showTyping() { removeTyping(); typingEl = document.createElement('div'); typingEl.className = 'msg ace'; typingEl.innerHTML = '<div class="sender">ACE</div><span class="typing"><span></span><span></span><span></span></span>'; messagesEl.appendChild(typingEl); scrollBottom(); }
  function removeTyping() { if (typingEl && typingEl.parentNode) typingEl.parentNode.removeChild(typingEl); typingEl = null; }

  /* ============================================================ CARDS — Ace's projections */
  var cardsEl = $('cards');
  function cardShell(title) {
    var card = document.createElement('div'); card.className = 'card';
    var head = document.createElement('div'); head.className = 'card-head';
    head.appendChild(document.createTextNode(title));
    var x = document.createElement('button'); x.className = 'card-x'; x.textContent = '✕';
    x.addEventListener('click', function () { card.remove(); });
    head.appendChild(x);
    var body = document.createElement('div'); body.className = 'card-body';
    card.appendChild(head); card.appendChild(body);
    // replace an existing card of the same panel; cap the stack at 4
    var old = cardsEl.querySelector('.card[data-panel="' + title + '"]'); if (old) old.remove();
    card.setAttribute('data-panel', title);
    cardsEl.insertBefore(card, cardsEl.firstChild);
    while (cardsEl.children.length > 4) cardsEl.removeChild(cardsEl.lastChild);
    return body;
  }
  function empty(body, note) { var d = document.createElement('div'); d.className = 'empty-note'; d.textContent = note; body.appendChild(d); }

  function materializeCard(panel, data) {
    data = data || {};
    if (panel === 'calendar') {
      var body = cardShell('CALENDAR');
      var events = data.events || [];
      if (!events.length) return empty(body, 'All clear.');
      events.slice(0, 10).forEach(function (e) {
        var row = document.createElement('div'); row.className = 'c-event';
        var day = document.createElement('span'); day.className = 'c-day'; day.textContent = e.day_label || '';
        var tm = document.createElement('span'); tm.className = 'c-time'; tm.textContent = e.time || '';
        var nm = document.createElement('span'); nm.className = 'c-name'; nm.textContent = e.title || '';
        row.appendChild(day); row.appendChild(tm); row.appendChild(nm); body.appendChild(row);
      });
    } else if (panel === 'tasks') {
      var body2 = cardShell('TASKS');
      var tasks = data.tasks || [];
      if (!tasks.length) return empty(body2, 'Inbox zero.');
      tasks.slice(0, 12).forEach(function (t) {
        var row = document.createElement('div'); row.className = 'c-task';
        var box = document.createElement('span'); box.className = 'c-box';
        var bd = document.createElement('span'); bd.style.flex = '1'; bd.appendChild(document.createTextNode(t.title || ''));
        var meta = document.createElement('span'); meta.className = 'c-meta'; meta.textContent = t.list || '';
        if (t.due) { var due = document.createElement('span'); due.className = 'c-due'; due.textContent = '  ⏱ ' + t.due; meta.appendChild(due); }
        bd.appendChild(meta); row.appendChild(box); row.appendChild(bd); body2.appendChild(row);
      });
    } else if (panel === 'weather') {
      var body3 = cardShell('WEATHER');
      var w = data;
      if (!w.ok) return empty(body3, (w.condition || 'Unavailable'));
      var G = { '01': '☀', '02': '⛅', '03': '☁', '04': '☁', '09': '☂', '10': '☂', '11': '⚡', '13': '❄', '50': '≡' };
      var wx = document.createElement('div'); wx.className = 'wx';
      function cell(cls, txt) { var el = document.createElement('div'); el.className = cls; el.textContent = txt; return el; }
      wx.appendChild(cell('wx-glyph', G[(w.icon || '').slice(0, 2)] || '≡'));
      wx.appendChild(cell('wx-temp', w.temp != null ? w.temp + '°' : '--'));
      wx.appendChild(cell('wx-cond', (w.location || '') + (w.description ? ' · ' + w.description : '')));
      var grid = document.createElement('div'); grid.className = 'wx-grid';
      [['HIGH', w.high != null ? w.high + '°' : '--'], ['LOW', w.low != null ? w.low + '°' : '--'],
       ['HUM', w.humidity != null ? w.humidity + '%' : '--'], ['WIND', w.wind != null ? w.wind + ' mph' : '--']]
        .forEach(function (p) { var cl = document.createElement('div'); cl.className = 'wx-cell';
          var s = document.createElement('span'); s.textContent = p[0]; var b = document.createElement('b'); b.textContent = p[1];
          cl.appendChild(s); cl.appendChild(b); grid.appendChild(cl); });
      wx.appendChild(grid); body3.appendChild(wx);
    } else if (panel === 'memory') {
      var body4 = cardShell('MEMORY');
      var mems = data.memories || [];
      if (!mems.length) return empty(body4, 'Memory empty.');
      mems.slice(0, 15).forEach(function (m) { var d = document.createElement('div'); d.className = 'c-mem'; d.textContent = (typeof m === 'string') ? m : JSON.stringify(m); body4.appendChild(d); });
    }
  }

  function openLink(url, label) {
    if (!/^https?:\/\//i.test(url || '')) return;
    var win = null; try { win = window.open(url, '_blank', 'noopener'); } catch (e) {}
    // popup blocked → materialize a link card instead
    if (!win) {
      var body = cardShell('LINK'); body.className += ' link-card';
      var a = document.createElement('a'); a.href = url; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = label || url; body.appendChild(a);
    }
  }

  /* ============================================================ STATUS */
  function setLink(on) { $('link-dot').classList.toggle('off', !on); }

  /* ============================================================ VOICE */
  var audioCtx = null, ttsAudio = null, ttsQueue = [], ttsPlaying = false;
  function setupRecognition() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return null;
    var rec = new SR(); rec.continuous = false; rec.interimResults = false; rec.lang = 'en-US';
    rec.onresult = function (e) { var tr = e.results[0][0].transcript; $('chat-input').value = tr; stopMic(); sendMessage(tr); };
    rec.onend = function () { if (state.micActive) stopMic(); };
    rec.onerror = function () { stopMic(); };
    return rec;
  }
  function startMic() {
    if (!state.recognition) state.recognition = setupRecognition();
    if (!state.recognition) { addAceMessage('Voice input is not supported in this browser.'); return; }
    state.micActive = true; $('mic-btn').classList.add('active'); setOrbState('listening');
    try { state.recognition.start(); } catch (e) {}
  }
  function stopMic() { state.micActive = false; $('mic-btn').classList.remove('active'); if (!state.busy && !ttsPlaying) setOrbState('idle'); try { state.recognition && state.recognition.stop(); } catch (e) {} }
  $('mic-btn').addEventListener('click', function () { state.micActive ? stopMic() : startMic(); });

  $('voice-toggle').addEventListener('click', function () {
    state.voiceOut = !state.voiceOut; localStorage.setItem('ace2_voice', state.voiceOut ? 'on' : 'off');
    $('voice-toggle').classList.toggle('off', !state.voiceOut); $('voice-toggle').setAttribute('aria-pressed', String(state.voiceOut));
    if (!state.voiceOut) { try { window.speechSynthesis.cancel(); } catch (e) {} if (ttsAudio) ttsAudio.pause(); ttsQueue = []; ttsPlaying = false; orb.setAmplitude(0); }
  });
  $('voice-toggle').classList.toggle('off', !state.voiceOut);

  function speak(text) { if (!state.voiceOut || !text) return; ttsQueue.push(text); if (!ttsPlaying) playNextTts(); }
  function playNextTts() {
    if (!ttsQueue.length) { ttsPlaying = false; if (!state.busy) setOrbState(state.micActive ? 'listening' : 'idle'); return; }
    ttsPlaying = true;
    var text = ttsQueue.shift();
    fetch(API + '/tts', { method: 'POST', headers: headers(), body: JSON.stringify({ text: text }) })
      .then(function (r) { if (!r.ok || r.status === 204) throw 'fallback'; return r.blob(); })
      .then(playAudioBlob)
      .catch(function () { browserSpeak(text); });
  }
  function playAudioBlob(blob) {
    setOrbState('speaking');
    try { if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { audioCtx = null; }
    ttsAudio = new Audio(URL.createObjectURL(blob));
    var analyser = null, data = null, raf = null;
    if (audioCtx) {
      try {
        var src = audioCtx.createMediaElementSource(ttsAudio);
        analyser = audioCtx.createAnalyser(); analyser.fftSize = 256;
        src.connect(analyser); analyser.connect(audioCtx.destination);
        data = new Uint8Array(analyser.frequencyBinCount);
        (function loop() { if (!analyser) return; analyser.getByteFrequencyData(data);
          var sum = 0; for (var i = 0; i < data.length; i++) sum += data[i];
          orb.setAmplitude((sum / data.length) / 128); raf = requestAnimationFrame(loop); })();
      } catch (e) { analyser = null; }
    }
    var done = function () { if (raf) cancelAnimationFrame(raf); orb.setAmplitude(0); ttsAudio = null; playNextTts(); };
    ttsAudio.onended = done; ttsAudio.onerror = done;
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
    ttsAudio.play().catch(done);
  }
  function browserSpeak(text) {
    if (!window.speechSynthesis) { ttsPlaying = false; return; }
    try {
      setOrbState('speaking');
      var u = new SpeechSynthesisUtterance(text); u.rate = 1.06; u.pitch = 1;
      u.onend = function () { playNextTts(); };
      window.speechSynthesis.cancel(); window.speechSynthesis.speak(u);
    } catch (e) { ttsPlaying = false; }
  }

  /* ============================================================ WIRING */
  $('send-btn').addEventListener('click', function () { sendMessage(); });
  $('chat-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') sendMessage(); });
  Array.prototype.forEach.call(document.querySelectorAll('.qa[data-msg]'), function (btn) {
    btn.addEventListener('click', function () { sendMessage(btn.getAttribute('data-msg')); });
  });

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () { navigator.serviceWorker.register('/sw.js').catch(function () {}); });
  }

  // Debug/demo hook (also lets us drive the stage from the console).
  window.aceDebug = { card: materializeCard, open: openLink, event: handleWSEvent, orb: orb };

  boot();
})();
