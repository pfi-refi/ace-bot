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
    micActive: false, recognition: null, handsFree: false,
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
    var W = 240, cx = 120, cy = 120, R = 112;
    ctx.scale(canvas.width / W, canvas.height / W);   // logical 240 coords → any backing size (now 640, crisp when large)
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
    // Free-drifting outer motes — wander independently, fade at the soft edge (cosmic wisp)
    var motes = [];
    for (var d = 0; d < 30; d++) motes.push({
      r: 46 + Math.random() * 62, a0: Math.random() * 6.283,
      w: (.035 + Math.random() * .07) * (Math.random() < .5 ? 1 : -1),
      s: .4 + Math.random() * 1.1, tw: Math.random() * 6.283, ts: .4 + Math.random() * 1.5,
      cyan: Math.random() < .35,
    });

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

      // Autonomous life: the whole cloud slowly drifts, rotates, and tilts on its own.
      var dx = 4 * Math.sin(t * 0.11), dy = 3.2 * Math.cos(t * 0.14);   // wander
      var ox = cx + dx, oy = cy + dy;
      var flat = 0.56 + 0.13 * Math.sin(t * 0.17);                      // disc tilt over time
      var gRot = t * 0.06 * speed;                                      // whole-galaxy rotation

      ctx.clearRect(0, 0, W, W);
      ctx.save();
      ctx.translate(ox, oy); ctx.scale(breath, breath); ctx.translate(-ox, -oy);
      ctx.globalCompositeOperation = 'lighter';   // pure additive glow on transparent — it floats in space

      // edgeless glow bed — replaces the old hard vignette/rim; fades fully out, no circle
      var halo = ctx.createRadialGradient(ox, oy, 6, ox, oy, R * 1.02);
      var ha = (.14 + glow * .11 + amp * .14) * light;
      halo.addColorStop(0, 'rgba(69,255,166,' + ha.toFixed(3) + ')');
      halo.addColorStop(.5, 'rgba(69,255,166,' + (ha * .4).toFixed(3) + ')');
      halo.addColorStop(1, 'rgba(69,255,166,0)');
      ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(ox, oy, R * 1.02, 0, 6.283); ctx.fill();

      // nebula clouds — drift with the galaxy, morph, tilt
      for (var i = 0; i < clouds.length; i++) {
        var c = clouds[i];
        var ang = c.ang + gRot + t * c.spd * speed;
        var wr = c.r * (1 + .14 * Math.sin(t * .4 + c.wob));
        var x = ox + Math.cos(ang) * c.dist, y = oy + Math.sin(ang) * c.dist * flat;
        var g = ctx.createRadialGradient(x, y, 0, x, y, wr);
        g.addColorStop(0, 'rgba(' + c.hue + ',' + (c.a * light * 1.25).toFixed(3) + ')');
        g.addColorStop(1, 'rgba(' + c.hue + ',0)');
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, wr, 0, 6.283); ctx.fill();
      }

      // spiral-arm stars — orbit + global rotation, fading softly at the rim
      for (var k = 0; k < starsG.length; k++) {
        var st = starsG[k];
        var a = st.a0 + gRot + t * st.w * speed;
        var x2 = ox + Math.cos(a) * st.r, y2 = oy + Math.sin(a) * st.r * flat;
        var tw = .35 + .55 * (.5 + .5 * Math.sin(t * st.ts + st.tw));
        var al = tw * light * .95;
        ctx.fillStyle = st.cyan
          ? 'rgba(160,240,255,' + al.toFixed(3) + ')'
          : 'rgba(190,255,225,' + al.toFixed(3) + ')';
        ctx.beginPath(); ctx.arc(x2, y2, st.s, 0, 6.283); ctx.fill();
      }

      // free-drifting outer motes — wander on their own, wispy, fade at the edge
      for (var d2 = 0; d2 < motes.length; d2++) {
        var mo = motes[d2];
        var ma = mo.a0 + gRot * .6 + t * mo.w;
        var mr = mo.r * (1 + .07 * Math.sin(t * .3 + mo.tw));
        var mx = ox + Math.cos(ma) * mr, my = oy + Math.sin(ma) * mr * flat;
        var mal = (.3 + .6 * (.5 + .5 * Math.sin(t * mo.ts + mo.tw))) * light * .8;
        ctx.fillStyle = mo.cyan
          ? 'rgba(150,235,255,' + mal.toFixed(3) + ')'
          : 'rgba(200,255,230,' + mal.toFixed(3) + ')';
        ctx.beginPath(); ctx.arc(mx, my, mo.s, 0, 6.283); ctx.fill();
      }

      // luminous core — the galaxy's heart, drifting with it
      var coreR = 30 + amp * 12;
      var core = ctx.createRadialGradient(ox, oy, 0, ox, oy, coreR);
      core.addColorStop(0, 'rgba(240,255,248,' + (.92 * light).toFixed(3) + ')');
      core.addColorStop(.32, 'rgba(69,255,166,' + (.55 * light).toFixed(3) + ')');
      core.addColorStop(1, 'rgba(69,255,166,0)');
      ctx.fillStyle = core; ctx.beginPath(); ctx.arc(ox, oy, coreR, 0, 6.283); ctx.fill();

      // Feather EVERYTHING into a soft round edge — guarantees the square canvas
      // boundary never shows, no matter how far the nebula/glow reaches.
      ctx.globalCompositeOperation = 'destination-in';
      var mask = ctx.createRadialGradient(ox, oy, R * 0.55, ox, oy, R * 1.04);
      mask.addColorStop(0, 'rgba(0,0,0,1)');
      mask.addColorStop(0.7, 'rgba(0,0,0,1)');
      mask.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = mask; ctx.fillRect(cx - W, cy - W, W * 2, W * 2);

      ctx.globalCompositeOperation = 'source-over';
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

  /* ============================================================ CHAT PANEL (toggle) */
  var chatOpen = localStorage.getItem('ace2_chat') === 'on';
  function applyChatState() {
    var panel = $('chat-panel'), btn = $('chat-toggle');
    $('app').classList.toggle('chat-open', chatOpen);   // shifts content left of the panel (desktop)
    if (chatOpen) {
      panel.classList.add('open'); btn.classList.add('active'); btn.classList.remove('has-new');
      btn.setAttribute('aria-pressed', 'true'); scrollBottom();
    } else {
      panel.classList.remove('open'); btn.classList.remove('active'); btn.setAttribute('aria-pressed', 'false');
    }
  }
  function setChat(open) { chatOpen = open; localStorage.setItem('ace2_chat', open ? 'on' : 'off'); applyChatState(); }
  function markUnread() { if (!chatOpen) $('chat-toggle').classList.add('has-new'); }   // glow the toggle when a reply lands while closed
  $('chat-toggle').addEventListener('click', function () { setChat(!chatOpen); });
  $('chat-close').addEventListener('click', function () { setChat(false); });

  /* ============================================================ DAY / UP-NEXT */
  var todayEvents = [], todayLoaded = false;
  function fmtClock(d) { var h = d.getHours(), ap = h >= 12 ? 'PM' : 'AM', h12 = h % 12 || 12; return h12 + ':' + (d.getMinutes() < 10 ? '0' : '') + d.getMinutes() + ' ' + ap; }
  // Split today's events into done / now / next / later, computed against `now` (a Date).
  function daySplit(events, now) {
    var timed = [], allday = [];
    (events || []).forEach(function (e) {
      if (e.all_day) { allday.push(e); return; }
      var s = new Date(e.iso); if (isNaN(s)) return;
      timed.push({ s: s, e: e });
    });
    timed.sort(function (a, b) { return a.s - b.s; });
    var past = [], up = [];
    timed.forEach(function (t) { (t.s <= now ? past : up).push(t); });
    var nowItem = null, done = past.slice();
    if (past.length && (now - past[past.length - 1].s) <= 90 * 60000) { nowItem = past[past.length - 1]; done = past.slice(0, -1); }
    return { done: done, now: nowItem, next: up[0] || null, later: up.slice(1), allday: allday };
  }
  function renderUpNext() {
    var box = $('upnext'); if (!box || !todayLoaded) return;
    function sep() { var x = document.createElement('span'); x.className = 'un-sep'; x.textContent = '·'; return x; }
    function span(cls, txt) { var s = document.createElement('span'); s.className = cls; s.textContent = txt; return s; }
    var now = new Date(), sp = daySplit(todayEvents, now);
    box.textContent = '';
    box.appendChild(span('un-now', 'NOW ' + fmtClock(now)));
    if (sp.now) { box.appendChild(sep()); box.appendChild(span('un-next', '▸ ' + (sp.now.e.title || ''))); }
    if (sp.next) {
      box.appendChild(sep());
      box.appendChild(span('un-next', 'NEXT ' + sp.next.e.time + ' — ' + (sp.next.e.title || '')));
      if (sp.later[0]) box.appendChild(span('un-then', '  then ' + sp.later[0].e.time + ' ' + (sp.later[0].e.title || '')));
    } else if (!sp.now) {
      box.appendChild(sep());
      box.appendChild(span('un-then', todayEvents.length ? 'clear the rest of today' : 'nothing on the calendar today'));
    }
    box.classList.remove('hidden');
  }
  function loadToday() {
    fetch(API + '/calendar?days=1', { headers: headers() })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { todayEvents = (d && d.events) || []; todayLoaded = true; renderUpNext(); })
      .catch(function () {});
  }

  /* ============================================================ CLOCK */
  (function () {
    var days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
    function tick() {
      var d = new Date(), h = d.getHours(), ap = h >= 12 ? 'PM' : 'AM', h12 = h % 12 || 12;
      var mm = (d.getMinutes() < 10 ? '0' : '') + d.getMinutes();
      $('clock').innerHTML = days[d.getDay()] + ' · <b>' + h12 + ':' + mm + '</b> ' + ap;
      renderUpNext();
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
    applyChatState();
    connectWS();
    loadToday();
    loadDashboard();
    checkConvai();
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
      case 'card': materializeCard(msg.panel, msg.data, msg.where); break;
      case 'open': openLink(msg.url, msg.label); break;
      case 'confirmation': renderConfirm(msg.text); break;
      case 'final': if (streamMsg) finalizeStream(streamMsg, msg.text); break;
      case 'error': removeTyping(); discardEmptyStream(); addAceMessage(msg.text); break;
      case 'done': discardEmptyStream(); streamMsg = null; activeTool = null; state.busy = false;
        if (!ttsPlaying) { setOrbState(state.micActive ? 'listening' : 'idle'); maybeResumeMic(); } break;
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
  function addAceMessage(t) { var m = document.createElement('div'); m.className = 'msg ace'; m.innerHTML = '<div class="sender">ACE</div>'; m.appendChild(document.createTextNode(t)); var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel(); m.appendChild(ts); messagesEl.appendChild(m); scrollBottom(); markUnread(); return m; }
  function beginAceStream() { var m = document.createElement('div'); m.className = 'msg ace'; m.innerHTML = '<div class="sender">ACE</div>'; var b = document.createElement('span'); m.appendChild(b); var c = document.createElement('span'); c.className = 'cursor'; c.textContent = ' '; m.appendChild(c); messagesEl.appendChild(m); scrollBottom(); return { el: m, body: b, cursor: c, text: '' }; }
  function appendToStream(s, t) { s.text += t; s.body.textContent = s.text; scrollBottom(); }
  function discardEmptyStream() { if (streamMsg && !streamMsg.text && streamMsg.el.parentNode) { streamMsg.el.parentNode.removeChild(streamMsg.el); streamMsg = null; } }
  function finalizeStream(s, txt) { s.body.textContent = txt || s.text; if (s.cursor.parentNode) s.cursor.parentNode.removeChild(s.cursor); var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel(); s.el.appendChild(ts); speak(txt || s.text); scrollBottom(); markUnread(); }
  function renderTool(msg) {
    if (msg.status === 'running') { activeTool = document.createElement('div'); activeTool.className = 'tool-pill'; activeTool.innerHTML = '<span class="spin">◈</span> '; activeTool.appendChild(document.createTextNode(msg.label + '…')); messagesEl.appendChild(activeTool); scrollBottom(); }
    else if (activeTool) { activeTool.className = 'tool-pill done'; activeTool.innerHTML = '◈ '; activeTool.appendChild(document.createTextNode(msg.label)); activeTool = null; scrollBottom(); }
  }
  function renderConfirm(text) { var p = document.createElement('div'); p.className = 'tool-pill done'; p.appendChild(document.createTextNode(text)); messagesEl.appendChild(p); scrollBottom(); }
  var typingEl = null;
  function showTyping() { removeTyping(); typingEl = document.createElement('div'); typingEl.className = 'msg ace'; typingEl.innerHTML = '<div class="sender">ACE</div><span class="typing"><span></span><span></span><span></span></span>'; messagesEl.appendChild(typingEl); scrollBottom(); }
  function removeTyping() { if (typingEl && typingEl.parentNode) typingEl.parentNode.removeChild(typingEl); typingEl = null; }

  /* ============================================================ CARDS — Ace's projections */
  // Default screen side per card; Ace can override with a `where` on display_card.
  var CARD_SLOTS = { timeline: 'left', daybank: 'right', calendar: 'left', tasks: 'left',
                     weather: 'right', memory: 'right', inbox: 'right' };
  function slotEl(where) { return (where === 'left') ? $('cards-left') : $('cards'); }
  function cardShell(title, where) {
    var host = slotEl(where);
    var card = document.createElement('div'); card.className = 'card';
    var head = document.createElement('div'); head.className = 'card-head';
    head.appendChild(document.createTextNode(title));
    var x = document.createElement('button'); x.className = 'card-x'; x.textContent = '✕';
    x.addEventListener('click', function () { card.remove(); });
    head.appendChild(x);
    var body = document.createElement('div'); body.className = 'card-body';
    card.appendChild(head); card.appendChild(body);
    // one card per panel across BOTH slots (so re-placing moves it); cap each slot at 4
    var dup = document.querySelector('.card[data-panel="' + title + '"]'); if (dup) dup.remove();
    card.setAttribute('data-panel', title);
    host.insertBefore(card, host.firstChild);
    while (host.children.length > 4) host.removeChild(host.lastChild);
    return body;
  }
  function empty(body, note) { var d = document.createElement('div'); d.className = 'empty-note'; d.textContent = note; body.appendChild(d); }

  function materializeCard(panel, data, where) {
    data = data || {};
    var slot = where || CARD_SLOTS[panel] || 'right';
    if (panel === 'calendar') {
      var body = cardShell('CALENDAR', slot);
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
      var body2 = cardShell('TASKS', slot);
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
      var body3 = cardShell('WEATHER', slot);
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
      var body4 = cardShell('MEMORY', slot);
      var mems = data.memories || [];
      if (!mems.length) return empty(body4, 'Memory empty.');
      mems.slice(0, 15).forEach(function (m) { var d = document.createElement('div'); d.className = 'c-mem'; d.textContent = (typeof m === 'string') ? m : JSON.stringify(m); body4.appendChild(d); });
    } else if (panel === 'timeline') {
      // Fresh today data → also refresh the always-visible up-next strip.
      todayEvents = data.events || []; todayLoaded = true; renderUpNext();
      var body5 = cardShell('TODAY', slot);
      if (!todayEvents.length) return empty(body5, 'Nothing on the calendar today.');
      var sp = daySplit(todayEvents, new Date()), placedNow = false;
      function tlRow(item, cls, mark) {
        var row = document.createElement('div'); row.className = 'tl-row ' + cls;
        var mk = document.createElement('span'); mk.className = 'tl-mark'; mk.textContent = mark || '';
        var tm = document.createElement('span'); tm.className = 'tl-time'; tm.textContent = item.e.time || '';
        var nm = document.createElement('span'); nm.className = 'tl-title'; nm.textContent = item.e.title || '';
        row.appendChild(mk); row.appendChild(tm); row.appendChild(nm); body5.appendChild(row);
      }
      function nowLine() {
        var d = document.createElement('div'); d.className = 'tl-nowline';
        var s = document.createElement('span'); s.textContent = 'NOW ' + fmtClock(new Date());
        d.appendChild(s); body5.appendChild(d); placedNow = true;
      }
      sp.done.forEach(function (it) { tlRow(it, 'tl-past', '✓'); });
      if (sp.now) { nowLine(); tlRow(sp.now, 'tl-now', '▸'); }
      if (!placedNow) nowLine();
      if (sp.next) tlRow(sp.next, 'tl-next', '→');
      sp.later.forEach(function (it) { tlRow(it, 'tl-later', '•'); });
      sp.allday.forEach(function (e) { tlRow({ e: { time: 'All day', title: e.title } }, 'tl-allday', '•'); });
    } else if (panel === 'daybank') {
      var body6 = cardShell('DATA BANK', slot);
      var items = data.items || [];
      if (!items.length) return empty(body6, 'Nothing captured yet.');
      items.forEach(function (it) {
        var row = document.createElement('div'); row.className = 'db-item' + (it.status === 'done' ? ' db-done' : '');
        var box = document.createElement('button'); box.className = 'db-box' + (it.status === 'done' ? ' done' : '');
        box.title = it.status === 'done' ? 'Reopen' : 'Mark done'; box.textContent = it.status === 'done' ? '✓' : '';
        box.addEventListener('click', function () { toggleBankItem(it.id, it.status === 'done' ? 'open' : 'done'); });
        var mid = document.createElement('div'); mid.className = 'db-mid';
        var txt = document.createElement('div'); txt.className = 'db-text'; txt.textContent = it.text || '';
        var meta = document.createElement('div'); meta.className = 'db-meta';
        meta.appendChild(tag('db-kind', it.kind || 'note'));
        if (it.due) meta.appendChild(tag('db-due', '⏱ ' + it.due));
        mid.appendChild(txt); mid.appendChild(meta);
        row.appendChild(box); row.appendChild(mid); body6.appendChild(row);
      });
      function tag(cls, t) { var s = document.createElement('span'); s.className = cls; s.textContent = t; return s; }
    } else if (panel === 'inbox') {
      var body7 = cardShell('PRIORITY INBOX', slot);
      var emails = data.emails || [];
      if (!emails.length) return empty(body7, 'Inbox clear — nothing unread.');
      emails.forEach(function (m) {
        var row = document.createElement('div'); row.className = 'in-row';
        var from = document.createElement('div'); from.className = 'in-from'; from.textContent = m.from || '';
        var subj = document.createElement('div'); subj.className = 'in-subj'; subj.textContent = m.subject || '';
        var snip = document.createElement('div'); snip.className = 'in-snip'; snip.textContent = m.snippet || '';
        row.appendChild(from); row.appendChild(subj); row.appendChild(snip); body7.appendChild(row);
      });
    }
  }
  // Dashboard: auto-load the always-on panels on startup so the HUD is a live command
  // center at a glance (orb stays the centerpiece). Each is best-effort and independent.
  function loadDashboard() {
    var g = function (url) { return fetch(API + url, { headers: headers() }).then(function (r) { return r.ok ? r.json() : null; }); };
    g('/calendar?days=1').then(function (d) { if (d) materializeCard('timeline', { events: d.events || [] }, 'left'); }).catch(function () {});
    g('/tasks').then(function (d) { if (d) materializeCard('tasks', { tasks: d.tasks || [] }, 'left'); }).catch(function () {});
    g('/inbox').then(function (d) { if (d) materializeCard('inbox', { emails: d.emails || [] }, 'right'); }).catch(function () {});
    g('/weather').then(function (d) { if (d) materializeCard('weather', d, 'right'); }).catch(function () {});
  }
  function toggleBankItem(id, status) {
    fetch(API + '/daybank/update', { method: 'POST', headers: headers(), body: JSON.stringify({ id: id, status: status }) })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d && d.items) materializeCard('daybank', { items: d.items }); })
      .catch(function () {});
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

  /* ============================================================ VOICE
     Hands-free conversation: one click on the mic opens the loop — Ace
     listens, you speak (auto-sends), he answers aloud, the mic reopens.
     Recognition pauses while his voice plays so he doesn't hear himself.
     Click the mic again to close the loop. */
  var audioCtx = null, ttsAudio = null, ttsQueue = [], ttsPlaying = false;
  // Record the mic, let a short silence end the utterance, transcribe it server-side
  // (real STT) and send. Reused stream across a hands-free session; released on stop.
  var micStream = null, micRec = null, sttCtx = null, sttAnalyser = null, vadRAF = null, segmentEmpty = false;

  function ensureStream() {
    if (micStream) return Promise.resolve(micStream);
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) return Promise.reject('unsupported');
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      .then(function (s) { micStream = s; return s; });
  }
  function pickMime() {
    var opts = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
    for (var i = 0; i < opts.length; i++) { try { if (MediaRecorder.isTypeSupported(opts[i])) return opts[i]; } catch (e) {} }
    return '';
  }
  function beginSegment() {
    if (!micStream || !state.micActive || state.busy || ttsPlaying) return;
    setOrbState('listening');
    var mime = pickMime(), chunks = [];
    try { micRec = mime ? new MediaRecorder(micStream, { mimeType: mime }) : new MediaRecorder(micStream); }
    catch (e) { micRec = null; return; }
    segmentEmpty = false;
    micRec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
    micRec.onstop = function () {
      stopVAD();
      var mt = (micRec && micRec.mimeType) || mime || 'audio/webm';
      micRec = null;
      transcribeAndSend(new Blob(chunks, { type: mt }));
    };
    try { micRec.start(); } catch (e) { micRec = null; return; }
    startVAD();
  }
  function startVAD() {
    var buf = null;
    try {
      if (!sttCtx) sttCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (sttCtx.state === 'suspended') sttCtx.resume();
      var node = sttCtx.createMediaStreamSource(micStream);
      sttAnalyser = sttCtx.createAnalyser(); sttAnalyser.fftSize = 512;
      node.connect(sttAnalyser); buf = new Uint8Array(sttAnalyser.fftSize);
    } catch (e) { sttAnalyser = null; }
    var t0 = performance.now(), lastVoice = 0, spoke = false;
    (function loop() {
      if (!micRec) return;
      var now = performance.now(), rms = 0;
      if (sttAnalyser && buf) {
        sttAnalyser.getByteTimeDomainData(buf);
        var s = 0; for (var i = 0; i < buf.length; i++) { var v = (buf[i] - 128) / 128; s += v * v; }
        rms = Math.sqrt(s / buf.length);
      }
      if (rms > 0.035) { spoke = true; lastVoice = now; orb.setAmplitude(Math.min(1, rms * 6)); }
      else if (spoke) { orb.setAmplitude(0.15); }
      // end on ~1.2s pause after speech; hard-cap 14s; bail after 9s of pure silence
      if ((spoke && now - lastVoice > 1200) || (now - t0 > 14000) || (!spoke && now - t0 > 9000)) { endSegment(!spoke); return; }
      vadRAF = requestAnimationFrame(loop);
    })();
  }
  function stopVAD() { if (vadRAF) cancelAnimationFrame(vadRAF); vadRAF = null; orb.setAmplitude(0); }
  function endSegment(empty) {
    segmentEmpty = !!empty;
    if (micRec && micRec.state !== 'inactive') { try { micRec.stop(); } catch (e) {} } else { stopVAD(); }
  }
  function sttHeaders(type) { var h = { 'Content-Type': type || 'audio/webm' }; if (state.token) h.Authorization = 'Bearer ' + state.token; return h; }
  var sttFails = 0;
  function transcribeAndSend(blob) {
    if (segmentEmpty || !blob || blob.size < 1400) {   // nothing worth sending — keep the ear open
      segmentEmpty = false; if (state.micActive && state.handsFree) beginSegment(); return;
    }
    fetch(API + '/stt', { method: 'POST', headers: sttHeaders(blob.type), body: blob })
      .then(function (r) { return r.ok ? r.json() : Promise.reject('http ' + r.status); })
      .then(function (d) {
        if (d && d.error) return Promise.reject(d.error);
        sttFails = 0;
        var text = (d && d.text || '').trim();
        if (text) sendMessage(text);                         // Ace's turn; mic resumes after his reply
        else if (state.micActive && state.handsFree) beginSegment();
      })
      .catch(function (err) {
        sttFails++;
        if (sttFails >= 2) {   // transcription is genuinely down — stop the loop, let him type
          addAceMessage('Voice input is having trouble transcribing (' + err + '). I turned the mic off — type to me for now.');
          state.handsFree = false; stopMic();
        } else if (state.micActive && state.handsFree) { beginSegment(); }
      });
  }
  function startMic() {
    ensureStream().then(function () {
      state.micActive = true; $('mic-btn').classList.add('active');
      beginSegment();
    }).catch(function () {
      addAceMessage('I could not reach the microphone — check this site’s mic permission.');
      state.handsFree = false; state.micActive = false; $('mic-btn').classList.remove('active');
    });
  }
  function stopMic() {
    state.micActive = false; $('mic-btn').classList.remove('active');
    if (micRec && micRec.state !== 'inactive') { segmentEmpty = true; try { micRec.stop(); } catch (e) {} }
    stopVAD();
    if (micStream) { try { micStream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {} micStream = null; }
    if (!state.busy && !ttsPlaying) setOrbState('idle');
  }
  function maybeResumeMic() {
    if (state.handsFree && state.micActive && !state.busy && !ttsPlaying && document.visibilityState === 'visible') beginSegment();
  }

  /* ============================================================ LIVE VOICE (full-duplex)
     ElevenLabs Agents: he listens, thinks (via our brain), and talks in real time, and
     you can talk over him. Replaces record-and-transcribe when the agent is configured. */
  var convaiEnabled = false, liveConv = null, liveRAF = null, liveMode = 'idle';
  function checkConvai() {
    fetch(API + '/convai/config', { headers: headers() })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { convaiEnabled = !!(d && d.enabled); })
      .catch(function () {});
  }
  function livePulse() {
    if (!liveConv) return;
    var e = performance.now() / 1000;
    if (liveMode === 'speaking') orb.setAmplitude(0.30 + 0.26 * Math.abs(Math.sin(e * 6.1)) + 0.16 * Math.abs(Math.sin(e * 10.7)));
    else if (liveMode === 'listening') orb.setAmplitude(0.10 + 0.07 * Math.abs(Math.sin(e * 3.1)));
    else orb.setAmplitude(0);
    liveRAF = requestAnimationFrame(livePulse);
  }
  function startLiveVoice() {
    if (liveConv) return;
    var Conv = window.__ConvAI;
    if (!Conv) { addAceMessage('Live voice is still loading — give it a second and tap again.'); return; }
    $('mic-btn').classList.add('active'); setOrbState('listening'); liveMode = 'listening';
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      .then(function () { return fetch(API + '/convai/signed-url', { headers: headers() }); })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.signed_url) throw ((d && d.error) || 'no signed url');
        return Conv.startSession({
          signedUrl: d.signed_url,
          onConnect: function () { liveRAF = requestAnimationFrame(livePulse); },
          onDisconnect: function () { endLiveVoice(); },
          onError: function (err) { addAceMessage('Live voice error: ' + (err && err.message || err)); endLiveVoice(); },
          onModeChange: function (m) { var mm = (m && m.mode) || m; liveMode = (mm === 'speaking') ? 'speaking' : 'listening'; setOrbState(liveMode); },
          onMessage: function (msg) {
            var t = (msg && (msg.message || msg.text)) || '';
            if (!t) return;
            var src = msg && (msg.source || msg.role);
            if (src === 'user' || src === 'user_transcript') addUserMessage(t); else addAceMessage(t);
          },
        });
      })
      .then(function (conv) { liveConv = conv; })
      .catch(function (err) {
        addAceMessage('Couldn’t start live voice (' + err + '). If you just set it up, give ElevenLabs a moment and try again.');
        endLiveVoice();
      });
  }
  function endLiveVoice() {
    if (liveRAF) cancelAnimationFrame(liveRAF); liveRAF = null; liveMode = 'idle';
    if (liveConv) { try { liveConv.endSession(); } catch (e) {} liveConv = null; }
    orb.setAmplitude(0); $('mic-btn').classList.remove('active');
    if (!state.busy) setOrbState('idle');
  }
  $('mic-btn').addEventListener('click', function () {
    if (convaiEnabled) { if (liveConv) endLiveVoice(); else startLiveVoice(); return; }   // full-duplex
    if (state.handsFree || state.micActive) { state.handsFree = false; stopMic(); }        // fallback: record + transcribe
    else { state.handsFree = true; startMic(); }
  });

  $('voice-toggle').addEventListener('click', function () {
    state.voiceOut = !state.voiceOut; localStorage.setItem('ace2_voice', state.voiceOut ? 'on' : 'off');
    $('voice-toggle').classList.toggle('off', !state.voiceOut); $('voice-toggle').setAttribute('aria-pressed', String(state.voiceOut));
    if (!state.voiceOut) { try { window.speechSynthesis.cancel(); } catch (e) {} if (ttsAudio) ttsAudio.pause(); ttsQueue = []; ttsPlaying = false; orb.setAmplitude(0); }
  });
  $('voice-toggle').classList.toggle('off', !state.voiceOut);

  function speak(text) { if (!state.voiceOut || !text) return; ttsQueue.push(text); if (!ttsPlaying) playNextTts(); }
  function playNextTts() {
    if (!ttsQueue.length) {
      ttsPlaying = false;
      if (!state.busy) setOrbState(state.micActive ? 'listening' : 'idle');
      maybeResumeMic();   // hands-free: his turn is over — open yours
      return;
    }
    ttsPlaying = true;
    // Pause the ear while he speaks, so he doesn't transcribe himself.
    try { if (micRec && micRec.state !== 'inactive') { segmentEmpty = true; micRec.stop(); } stopVAD(); } catch (e) {}
    var text = ttsQueue.shift();
    fetch(API + '/tts', { method: 'POST', headers: headers(), body: JSON.stringify({ text: text }) })
      .then(function (r) { if (!r.ok || r.status === 204) throw 'fallback'; return r.blob(); })
      .then(playAudioBlob)
      .catch(function () { browserSpeak(text); });
  }
  function playAudioBlob(blob) {
    // Play the mp3 through the NATIVE <audio> path — no Web Audio rerouting. Routing the
    // element through createMediaElementSource just to read amplitude is the classic cause
    // of stuttery/"broken up" playback; the orb is driven by a synthetic pulse instead.
    setOrbState('speaking');
    var url = URL.createObjectURL(blob);
    ttsAudio = new Audio(url);
    var raf = null, t0 = performance.now();
    (function pulse() {
      if (!ttsAudio) return;
      var e = (performance.now() - t0) / 1000;
      orb.setAmplitude(0.30 + 0.26 * Math.abs(Math.sin(e * 6.1)) + 0.16 * Math.abs(Math.sin(e * 10.7)));
      raf = requestAnimationFrame(pulse);
    })();
    var done = function () {
      if (raf) cancelAnimationFrame(raf);
      orb.setAmplitude(0);
      try { URL.revokeObjectURL(url); } catch (e) {}
      ttsAudio = null; playNextTts();
    };
    ttsAudio.onended = done; ttsAudio.onerror = done;
    ttsAudio.play().catch(done);
  }
  function browserSpeak(text) {
    if (!window.speechSynthesis) { ttsPlaying = false; playNextTts(); return; }  // drain/resume, don't deadlock
    try {
      setOrbState('speaking');
      var u = new SpeechSynthesisUtterance(text); u.rate = 1.06; u.pitch = 1;
      u.onend = function () { playNextTts(); };
      window.speechSynthesis.cancel(); window.speechSynthesis.speak(u);
    } catch (e) { ttsPlaying = false; }
  }

  /* ============================================================ WIRING */
  // Chat panel stays hidden until Brady toggles it — sends never auto-open it. Every reply
  // still glows the toggle (markUnread) so he knows one landed, and Ace speaks it; cards render
  // on the stage regardless of the panel. Toggle it open only when he wants to read the thread.
  $('send-btn').addEventListener('click', function () { sendMessage(); });
  $('chat-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') { sendMessage(); } });
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
