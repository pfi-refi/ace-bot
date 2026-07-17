/* ============================================================
   ACE 2.0 — frontend controller
   Holographic orb (audio-reactive) · streaming tool-use chat · voice · layers
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

  /* ============================================================ MATRIX RAIN */
  var rainTimer = null;
  (function () {
    var canvas = $('matrix'), ctx = canvas.getContext('2d');
    var chars = 'アイウエオカキクケコサシスセソ0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    var fontSize = 14, drops = [];
    function resize() {
      canvas.width = window.innerWidth; canvas.height = window.innerHeight;
      var cols = Math.floor(canvas.width / fontSize); drops = [];
      for (var i = 0; i < cols; i++) drops[i] = Math.random() * -canvas.height / fontSize;
    }
    function draw() {
      ctx.fillStyle = 'rgba(0,0,0,.06)'; ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#39FF14'; ctx.font = fontSize + 'px monospace';
      for (var i = 0; i < drops.length; i++) {
        ctx.fillText(chars.charAt(Math.floor(Math.random() * chars.length)), i * fontSize, drops[i] * fontSize);
        if (drops[i] * fontSize > canvas.height && Math.random() > .975) drops[i] = 0;
        drops[i]++;
      }
    }
    function pump() { clearInterval(rainTimer); if (document.visibilityState === 'visible') rainTimer = setInterval(draw, 60); }
    resize(); window.addEventListener('resize', resize); pump();
    document.addEventListener('visibilitychange', pump);
  })();

  /* ============================================================ HOLOGRAPHIC ORB
     Five layers (stars, sphere, rings, scanlines, pulses), eased state params.
     setState() drives idle/listening/speaking; setAmplitude() lets Ace's voice
     pulse the orb in real time (the JARVIS payoff). */
  var orb = (function () {
    var canvas = $('orb-canvas'), ctx = canvas.getContext('2d');
    var W = 240, H = 240, cx = 120, cy = 120, SPHERE_R = 84, CLIP_R = 118;
    var glowEl = $('orb-glow');
    var speed = 1, speedT = 1, core = 0, coreT = 0, rim = 0, rimT = 0;
    var pulseInterval = 3, lastPulse = 0, amp = 0;

    var stars = [];
    for (var i = 0; i < 60; i++) { var a = Math.random() * 6.283, r = Math.random() * CLIP_R;
      stars.push({ x: Math.cos(a) * r, y: Math.sin(a) * r, vx: (Math.random() - .5) * .12, vy: (Math.random() - .5) * .12, s: .5 + Math.random() * 1.3, tw: Math.random() * 6.283, ts: .6 + Math.random() * 1.4 }); }
    var rings = [
      { rx: 100, ryf: .30, tilt: 0, period: 5, dir: 1, orient: .05, sats: 3 },
      { rx: 94, ryf: .34, tilt: Math.PI / 3, period: 7, dir: -1, orient: .04, sats: 2 },
      { rx: 108, ryf: .26, tilt: 2.094, period: 9, dir: 1, orient: .03, sats: 2 } ];
    var pulses = [], last = performance.now();

    function setState(s) {
      if (s === 'speaking') { speedT = 2.6; coreT = 1; rimT = 1; pulseInterval = .7; glowEl.style.opacity = '1'; glowEl.style.transform = 'scale(1.12)'; }
      else if (s === 'listening') { speedT = 2; coreT = .6; rimT = .7; pulseInterval = 1; glowEl.style.opacity = '.9'; glowEl.style.transform = 'scale(1.06)'; }
      else { speedT = 1; coreT = 0; rimT = 0; pulseInterval = 3; glowEl.style.opacity = '.65'; glowEl.style.transform = 'scale(1)'; amp = 0; }
    }
    function setAmplitude(v) { amp = Math.max(0, Math.min(1, v)); }

    function frame(now) {
      var t = now / 1000, dt = Math.min(.05, (now - last) / 1000) || .016; last = now;
      speed += (speedT - speed) * .05; core += (coreT - core) * .06; rim += (rimT - rim) * .06;
      var C = Math.min(1.4, core + amp * .8), R = Math.min(1.4, rim + amp * .7), S = speed + amp * 1.5;
      ctx.clearRect(0, 0, W, H);
      for (var i = 0; i < stars.length; i++) { var s = stars[i]; s.x += s.vx; s.y += s.vy;
        if (Math.hypot(s.x, s.y) > CLIP_R) { s.vx = -s.vx; s.vy = -s.vy; s.x += s.vx * 2; s.y += s.vy * 2; }
        ctx.fillStyle = 'rgba(200,255,220,' + (.35 + .4 * (.5 + .5 * Math.sin(t * s.ts + s.tw))).toFixed(3) + ')';
        ctx.beginPath(); ctx.arc(cx + s.x, cy + s.y, s.s, 0, 6.283); ctx.fill(); }
      var g = ctx.createRadialGradient(cx, cy, 2, cx, cy, SPHERE_R);
      g.addColorStop(0, 'rgba(57,255,20,' + (.15 + C * .25).toFixed(3) + ')');
      g.addColorStop(.6, 'rgba(57,255,20,' + (.08 + C * .14).toFixed(3) + ')');
      g.addColorStop(1, 'rgba(0,255,150,' + (.25 + C * .15).toFixed(3) + ')');
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, SPHERE_R, 0, 6.283); ctx.fill();
      ctx.save(); ctx.shadowColor = 'rgba(57,255,20,.9)'; ctx.shadowBlur = 12 + R * 14;
      ctx.strokeStyle = 'rgba(57,255,20,' + (.6 + R * .4).toFixed(3) + ')'; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(cx, cy, SPHERE_R, 0, 6.283); ctx.stroke(); ctx.restore();
      for (var k = 0; k < rings.length; k++) { var r = rings[k];
        var orient = r.tilt + t * r.orient * r.dir * S, spin = t * (6.283 / r.period) * r.dir * S;
        ctx.save(); ctx.translate(cx, cy); ctx.rotate(orient);
        ctx.strokeStyle = 'rgba(57,255,20,' + (.35 + R * .2).toFixed(3) + ')'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.ellipse(0, 0, r.rx, r.rx * r.ryf, 0, 0, 6.283); ctx.stroke();
        ctx.save(); ctx.shadowColor = 'rgba(57,255,20,.9)'; ctx.shadowBlur = 8; ctx.fillStyle = 'rgba(160,255,140,.95)';
        for (var j = 0; j < r.sats; j++) { var aa = spin + (j / r.sats) * 6.283;
          ctx.beginPath(); ctx.arc(Math.cos(aa) * r.rx, Math.sin(aa) * r.rx * r.ryf, 2.2, 0, 6.283); ctx.fill(); }
        ctx.restore(); ctx.restore(); }
      var off = (t * 14) % 4; ctx.strokeStyle = 'rgba(57,255,20,.03)'; ctx.lineWidth = 1; ctx.beginPath();
      for (var y = off; y < H; y += 4) { ctx.moveTo(0, y); ctx.lineTo(W, y); } ctx.stroke();
      if (t - lastPulse >= pulseInterval) { lastPulse = t; pulses.push({ r: SPHERE_R * .5, a: .4 }); }
      for (var p = pulses.length - 1; p >= 0; p--) { var pu = pulses[p]; pu.r += 42 * dt;
        pu.a = .4 * (1 - (pu.r - SPHERE_R * .5) / (CLIP_R - SPHERE_R * .5));
        if (pu.a <= .01 || pu.r >= CLIP_R) { pulses.splice(p, 1); continue; }
        ctx.strokeStyle = 'rgba(57,255,20,' + pu.a.toFixed(3) + ')'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(cx, cy, pu.r, 0, 6.283); ctx.stroke(); }
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
    var months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
    function pad(n) { return n < 10 ? '0' + n : '' + n; }
    function tick() { var d = new Date(), h = d.getHours(), ap = h >= 12 ? 'PM' : 'AM', h12 = h % 12 || 12;
      $('clock').innerHTML = pad(h12) + ':' + pad(d.getMinutes()) + '<span class="sec">:' + pad(d.getSeconds()) + '</span><span class="ampm">' + ap + '</span>';
      $('topdate').textContent = months[d.getMonth()] + ' ' + pad(d.getDate()) + ' ' + d.getFullYear(); }
    tick(); setInterval(tick, 1000);
  })();

  /* ============================================================ AUTH */
  function headers() { var h = { 'Content-Type': 'application/json' }; if (state.token) h.Authorization = 'Bearer ' + state.token; return h; }
  function saveToken(t) { state.token = t || ''; if (t) localStorage.setItem('ace2_token', t); else localStorage.removeItem('ace2_token'); }
  function toLogin() { saveToken(''); $('app').classList.add('hidden'); $('login').classList.remove('hidden'); $('login-input').focus(); }

  function boot() {
    fetch(API + '/health').then(function (r) { return r.json(); }).then(function (h) {
      $('bb-version').textContent = 'ACE ' + (h.version || 'v2.0.0') + ' // OPERATIONAL';
      state.authRequired = !!h.auth_required;
      if (!state.authRequired) { startApp(); return; }
      if (!state.token) { toLogin(); return; }
      // Validate the stored token BEFORE trusting it (portal's fatal miss).
      fetch(API + '/session', { headers: headers() }).then(function (r) {
        if (r.ok) startApp(); else toLogin();
      }).catch(function () { startApp(); }); // network down: still show shell
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

  /* ============================================================ APP START */
  function startApp() {
    $('app').classList.remove('hidden');
    initLayers();
    connectWS();
    refreshData();
    greeting();
    setInterval(refreshData, 5 * 60 * 1000);
  }
  function greeting() {
    var d = new Date(), days = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    var months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    addAceMessage('Ace online. ' + days[d.getDay()] + ', ' + months[d.getMonth()] + ' ' + d.getDate() +
      '. I have your calendar, tasks, mail, and memory — and I can act on any of them. What do you want to move first?');
  }

  /* ============================================================ WEBSOCKET */
  function wsURL() { var p = location.protocol === 'https:' ? 'wss:' : 'ws:'; var q = state.token ? ('?token=' + encodeURIComponent(state.token)) : ''; return p + '//' + location.host + '/ws/chat' + q; }
  function connectWS() {
    var ws; try { ws = new WebSocket(wsURL()); } catch (e) { setLink(false); scheduleReconnect(); return; }
    state.ws = ws;
    ws.onopen = function () { state.wsReady = true; state.reconnectDelay = 1000; setLink(true); };
    ws.onclose = function (ev) {
      state.wsReady = false; setLink(false);
      if (ev && ev.code === 4401) { toLogin(); return; }   // stale token → login, not a loop
      scheduleReconnect();
    };
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
      case 'final': if (streamMsg) finalizeStream(streamMsg, msg.text); break;
      case 'confirmation': pushIntel(msg.text); break;
      case 'error': removeTyping(); discardEmptyStream(); addAceMessage(msg.text); break;
      case 'done': discardEmptyStream(); streamMsg = null; activeTool = null; state.busy = false;
        setOrbState(state.micActive ? 'listening' : 'idle'); refreshData(); break;
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
          (d.confirmations || []).forEach(pushIntel); state.busy = false; setOrbState('idle'); refreshData(); })
        .catch(function () { removeTyping(); addAceMessage('⚠️ Link to Ace failed. Retrying…'); state.busy = false; setOrbState('idle'); });
    }
  }

  /* ============================================================ CHAT RENDER */
  var messagesEl = $('messages');
  function scrollBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }
  function nowLabel() { var d = new Date(), h = d.getHours(), ap = h >= 12 ? 'PM' : 'AM', h12 = h % 12 || 12; return (h12 < 10 ? '0' : '') + h12 + ':' + (d.getMinutes() < 10 ? '0' : '') + d.getMinutes() + ' ' + ap; }

  function addUserMessage(t) { var m = document.createElement('div'); m.className = 'msg user'; m.appendChild(document.createTextNode(t)); var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel(); m.appendChild(ts); messagesEl.appendChild(m); scrollBottom(); }
  function addAceMessage(t) { var m = document.createElement('div'); m.className = 'msg ace'; m.innerHTML = '<div class="sender">ACE</div>'; m.appendChild(document.createTextNode(t)); var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel(); m.appendChild(ts); messagesEl.appendChild(m); scrollBottom(); return m; }
  function beginAceStream() { var m = document.createElement('div'); m.className = 'msg ace'; m.innerHTML = '<div class="sender">ACE</div>'; var body = document.createElement('span'); m.appendChild(body); var cur = document.createElement('span'); cur.className = 'cursor'; cur.textContent = ' '; m.appendChild(cur); messagesEl.appendChild(m); scrollBottom(); return { el: m, body: body, cursor: cur, text: '' }; }
  function appendToStream(s, t) { s.text += t; s.body.textContent = s.text; scrollBottom(); }
  function discardEmptyStream() { if (streamMsg && !streamMsg.text && streamMsg.el && streamMsg.el.parentNode) { streamMsg.el.parentNode.removeChild(streamMsg.el); streamMsg = null; } }
  function finalizeStream(s, finalText) { s.body.textContent = finalText || s.text; if (s.cursor && s.cursor.parentNode) s.cursor.parentNode.removeChild(s.cursor); var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel(); s.el.appendChild(ts); speak(finalText || s.text); scrollBottom(); }

  function renderTool(msg) {
    if (msg.status === 'running') {
      activeTool = document.createElement('div'); activeTool.className = 'tool-pill';
      activeTool.innerHTML = '<span class="spin">◈</span> ' + msg.label + '…';
      messagesEl.appendChild(activeTool); scrollBottom();
    } else if (activeTool) { activeTool.className = 'tool-pill done'; activeTool.innerHTML = '◈ ' + msg.label; activeTool = null; scrollBottom(); }
  }

  var typingEl = null;
  function showTyping() { removeTyping(); typingEl = document.createElement('div'); typingEl.className = 'msg ace'; typingEl.innerHTML = '<div class="sender">ACE</div><span class="typing"><span></span><span></span><span></span></span>'; messagesEl.appendChild(typingEl); scrollBottom(); }
  function removeTyping() { if (typingEl && typingEl.parentNode) typingEl.parentNode.removeChild(typingEl); typingEl = null; }

  /* ============================================================ INTEL FEED */
  var intelEl = $('intel-list'), intelHas = false;
  function pushIntel(text) { if (!intelHas) { intelEl.innerHTML = ''; intelHas = true; } var it = document.createElement('div'); it.className = 'intel-item'; it.innerHTML = '<span class="it-ts">' + nowLabel() + '</span>'; it.appendChild(document.createTextNode(text)); intelEl.insertBefore(it, intelEl.firstChild); while (intelEl.children.length > 12) intelEl.removeChild(intelEl.lastChild); }

  /* ============================================================ DATA (bootstrap) */
  function refreshData() {
    fetch(API + '/bootstrap?days=7', { headers: headers() })
      .then(function (r) { if (r.status === 401) { toLogin(); throw 0; } return r.json(); })
      .then(function (d) {
        renderCalendar(d.events || []); renderTasks(d.tasks || []); renderWeather(d.weather || {});
        var s = d.services || {}; setSvc('svc-calendar', s.calendar); setSvc('svc-tasks', s.tasks); setSvc('svc-drive', s.drive); setSvc('svc-gmail', s.drive);
      }).catch(function () {});
  }
  function localDateStr(d) { return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2); }
  function eventRow(e, isNext, showDay) {
    var row = document.createElement('div'); row.className = 'event' + (isNext ? ' next' : '');
    if (showDay) { var dd = document.createElement('span'); dd.className = 'day-label'; dd.textContent = e.day_label; row.appendChild(dd); }
    var t = document.createElement('span'); t.className = 'event-time'; t.textContent = e.time;
    var n = document.createElement('span'); n.className = 'event-name'; n.textContent = e.title;
    row.appendChild(t); row.appendChild(n);
    if (isNext) { var b = document.createElement('span'); b.className = 'next-badge'; b.textContent = 'NEXT'; row.appendChild(b); }
    return row;
  }
  function renderCalendar(events) {
    var todayStr = localDateStr(new Date());
    var today = events.filter(function (e) { return e.date === todayStr; });
    var upcoming = events.filter(function (e) { return e.date > todayStr; }).slice(0, 5);
    var te = $('today-list');
    if (!today.length) te.innerHTML = '<div class="empty-state">[ ALL CLEAR ]</div>';
    else { te.innerHTML = ''; var now = new Date(), nextIdx = -1; for (var i = 0; i < today.length; i++) { if (!today[i].all_day && new Date(today[i].iso) > now) { nextIdx = i; break; } } today.forEach(function (e, i) { te.appendChild(eventRow(e, i === nextIdx)); }); }
    var ue = $('upcoming-list');
    if (!upcoming.length) ue.innerHTML = '<div class="empty-state">nothing ahead</div>';
    else { ue.innerHTML = ''; upcoming.forEach(function (e) { ue.appendChild(eventRow(e, false, true)); }); }
  }
  function renderTasks(tasks) {
    var el = $('tasks-list');
    if (!tasks.length) { el.innerHTML = '<div class="empty-state">[ INBOX ZERO ]</div>'; return; }
    el.innerHTML = '';
    tasks.slice(0, 40).forEach(function (t) {
      var row = document.createElement('div'); row.className = 'task';
      var box = document.createElement('span'); box.className = 'box';
      var body = document.createElement('span'); body.className = 't-body'; body.appendChild(document.createTextNode(t.title));
      var meta = document.createElement('span'); meta.className = 't-list'; meta.textContent = t.list || '';
      if (t.due) { var due = document.createElement('span'); due.className = 't-due'; due.textContent = '  ⏱ ' + t.due; meta.appendChild(due); }
      body.appendChild(meta); row.appendChild(box); row.appendChild(body); el.appendChild(row);
    });
  }
  var WX_GLYPH = { '01': '☀', '02': '⛅', '03': '☁', '04': '☁', '09': '☂', '10': '☂', '11': '⚡', '13': '❄', '50': '≡' };
  function renderWeather(w) {
    var card = $('wx-card');
    if (!w || !w.ok) { card.innerHTML = '<div class="empty-state">' + ((w && w.condition) || 'UNAVAILABLE') + '</div>'; $('bb-weather').textContent = 'CLE --°F'; return; }
    var glyph = WX_GLYPH[(w.icon || '').slice(0, 2)] || '≡';
    var loc = (w.location || 'CLEVELAND, OH');
    var desc = (w.description || w.condition || '').toUpperCase();
    card.innerHTML =
      '<div class="wx"><div class="wx-glyph">' + glyph + '</div><div class="wx-temp">' + (w.temp != null ? w.temp + '°' : '--') + '</div>' +
      '<div class="wx-cond">' + loc + (desc ? ' · ' + desc : '') + '</div>' +
      '<div class="wx-grid">' +
        '<div class="wx-cell"><span>HIGH</span><b>' + (w.high != null ? w.high + '°' : '--') + '</b></div>' +
        '<div class="wx-cell"><span>LOW</span><b>' + (w.low != null ? w.low + '°' : '--') + '</b></div>' +
        '<div class="wx-cell"><span>HUM</span><b>' + (w.humidity != null ? w.humidity + '%' : '--') + '</b></div>' +
        '<div class="wx-cell"><span>WIND</span><b>' + (w.wind != null ? w.wind + 'mph' : '--') + '</b></div>' +
      '</div></div>';
    $('bb-weather').textContent = 'CLE ' + (w.temp != null ? w.temp + '°F' : '--');
  }
  function setSvc(id, on) { var el = $(id); if (el) el.classList.toggle('off', !on); }
  function setLink(on) { $('chip-link').classList.toggle('off', !on); $('chip-sync').classList.toggle('off', !on); }

  /* ============================================================ OVERLAYS (memory / history) */
  function openModal(title, html) { $('modal-title').textContent = title; $('modal-body').innerHTML = html; $('modal').classList.remove('hidden'); }
  $('modal-close').addEventListener('click', function () { $('modal').classList.add('hidden'); });
  $('modal').addEventListener('click', function (e) { if (e.target === $('modal')) $('modal').classList.add('hidden'); });

  $('qa-memory').addEventListener('click', function () {
    openModal('ACE MEMORY — SOURCE OF TRUTH', '<div class="loading">reading memory…</div>');
    fetch(API + '/memory', { headers: headers() }).then(function (r) { return r.json(); }).then(function (d) {
      var mem = d.memories || []; if (!mem.length) { $('modal-body').innerHTML = '<div class="empty-state">memory empty</div>'; return; }
      $('modal-body').innerHTML = mem.map(function (m) { var div = document.createElement('div'); div.className = 'm-item'; div.textContent = (typeof m === 'string') ? m : JSON.stringify(m); return div.outerHTML; }).join('');
    }).catch(function () { $('modal-body').innerHTML = '<div class="empty-state">could not read memory</div>'; });
  });

  $('qa-history').addEventListener('click', function () {
    openModal('CONVERSATION HISTORY — TELEGRAM + ACE 2.0', '<div class="loading">loading history…</div>');
    fetch(API + '/history?months=3', { headers: headers() }).then(function (r) { return r.json(); }).then(function (d) {
      var rows = [];
      (d.shared || []).forEach(function (m) { rows.push({ badge: 'tg', role: m.role, body: m.content, ts: '' }); });
      (d.own || []).forEach(function (m) { rows.push({ badge: 'ace2', role: m.role, body: m.content, ts: (m.ts || '').slice(5, 16).replace('T', ' ') }); });
      if (!rows.length) { $('modal-body').innerHTML = '<div class="empty-state">no history yet</div>'; return; }
      $('modal-body').innerHTML = rows.map(function (r) {
        var b = document.createElement('div'); b.textContent = r.body || '';
        return '<div class="hist-row"><span class="hist-badge ' + r.badge + '">' + (r.badge === 'tg' ? 'TG' : 'ACE2') + '</span>' +
          '<span class="hist-role">' + (r.role || '') + '</span>' +
          '<span class="hist-body">' + b.innerHTML + (r.ts ? ' <span class="hist-ts">' + r.ts + '</span>' : '') + '</span></div>';
      }).join('');
    }).catch(function () { $('modal-body').innerHTML = '<div class="empty-state">could not load history</div>'; });
  });

  /* ============================================================ VOICE */
  // In: Web Speech (hold-to-talk). Out: /tts (real ACE voice) with WebAudio
  // amplitude → orb; falls back to browser speechSynthesis if TTS is unconfigured.
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
  function stopMic() { state.micActive = false; $('mic-btn').classList.remove('active'); if (!state.busy) setOrbState('idle'); try { state.recognition && state.recognition.stop(); } catch (e) {} }
  $('mic-btn').addEventListener('click', function () { state.micActive ? stopMic() : startMic(); });

  $('voice-toggle').addEventListener('click', function () {
    state.voiceOut = !state.voiceOut; localStorage.setItem('ace2_voice', state.voiceOut ? 'on' : 'off');
    $('voice-toggle').classList.toggle('off', !state.voiceOut); $('voice-toggle').setAttribute('aria-pressed', String(state.voiceOut));
    if (!state.voiceOut) { try { window.speechSynthesis.cancel(); } catch (e) {} if (ttsAudio) { ttsAudio.pause(); } ttsQueue = []; ttsPlaying = false; }
  });
  $('voice-toggle').classList.toggle('off', !state.voiceOut);

  function speak(text) {
    if (!state.voiceOut || !text) return;
    ttsQueue.push(text); if (!ttsPlaying) playNextTts();
  }
  function playNextTts() {
    if (!ttsQueue.length) { ttsPlaying = false; return; }
    ttsPlaying = true;
    var text = ttsQueue.shift();
    fetch(API + '/tts', { method: 'POST', headers: headers(), body: JSON.stringify({ text: text }) })
      .then(function (r) { if (r.status === 204) throw 'fallback'; if (!r.ok) throw 'fallback'; return r.blob(); })
      .then(function (blob) { playAudioBlob(blob); })
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
        var loop = function () { if (!analyser) return; analyser.getByteFrequencyData(data);
          var sum = 0; for (var i = 0; i < data.length; i++) sum += data[i]; orb.setAmplitude((sum / data.length) / 128);
          raf = requestAnimationFrame(loop); }; loop();
      } catch (e) { analyser = null; }
    }
    var cleanup = function () { if (raf) cancelAnimationFrame(raf); orb.setAmplitude(0); ttsAudio = null; if (!state.busy) setOrbState(state.micActive ? 'listening' : 'idle'); playNextTts(); };
    ttsAudio.onended = cleanup; ttsAudio.onerror = cleanup;
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
    ttsAudio.play().catch(cleanup);
  }
  function browserSpeak(text) {
    if (!window.speechSynthesis) { ttsPlaying = false; return; }
    try {
      setOrbState('speaking');
      var u = new SpeechSynthesisUtterance(text); u.rate = 1.08; u.pitch = 1;
      u.onend = function () { if (!state.busy) setOrbState(state.micActive ? 'listening' : 'idle'); playNextTts(); };
      window.speechSynthesis.cancel(); window.speechSynthesis.speak(u);
    } catch (e) { ttsPlaying = false; }
  }

  /* ============================================================ LAYERS + DIRECTIONS */
  function initLayers() {
    var collapsed = {}; try { collapsed = JSON.parse(localStorage.getItem('ace2_collapsed') || '{}'); } catch (e) {}
    var layers = document.querySelectorAll('.layer[data-layer]');
    Array.prototype.forEach.call(layers, function (layer) {
      var key = layer.getAttribute('data-layer');
      if (collapsed[key]) layer.setAttribute('data-collapsed', '1');
      var btn = layer.querySelector('.collapse');
      if (btn) btn.addEventListener('click', function () {
        var isC = layer.getAttribute('data-collapsed') === '1';
        if (isC) layer.removeAttribute('data-collapsed'); else layer.setAttribute('data-collapsed', '1');
        collapsed[key] = !isC; localStorage.setItem('ace2_collapsed', JSON.stringify(collapsed));
      });
    });
    // Direction switch is a data attribute on #main; default 'rail'. Persist choice.
    var dir = localStorage.getItem('ace2_dir') || 'rail'; setDirection(dir);
  }
  function setDirection(dir) {
    $('main').setAttribute('data-dir', dir);
    localStorage.setItem('ace2_dir', dir);
    // Summon bar (used in focus mode; also handy elsewhere).
    var bar = $('summon-bar'); bar.innerHTML = '';
    if (dir === 'focus') {
      ['SCHEDULE','WEATHER','INTEL','TASKS','MEMORY','HISTORY'].forEach(function (name) {
        var c = document.createElement('button'); c.className = 'summon-chip'; c.textContent = name;
        c.addEventListener('click', function () {
          if (name === 'MEMORY') return $('qa-memory').click();
          if (name === 'HISTORY') return $('qa-history').click();
        });
        bar.appendChild(c);
      });
    }
  }
  window.aceSetDirection = setDirection;  // console hook: aceSetDirection('halo')

  /* ============================================================ WIRING */
  $('send-btn').addEventListener('click', function () { sendMessage(); });
  $('chat-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') sendMessage(); });
  Array.prototype.forEach.call(document.querySelectorAll('.qa[data-msg]'), function (btn) {
    btn.addEventListener('click', function () { sendMessage(btn.getAttribute('data-msg')); });
  });

  // PWA: register the service worker so Ace installs as one app.
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function () {});
    });
  }

  boot();
})();
