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
  /* STILL MODE (2026-08-23, Brady: "not overheat like it does") — one tap freezes the starfield
     to a single painted frame (zero ongoing GPU) and idles the orb at ~2fps; the orb still
     surges to full life the moment Ace speaks. Persisted; the ❄ dock button toggles it. */
  // The ❄ button was removed (it read as a glitch). Clear any saved 'on' so a device that
  // toggled it before the removal can't be left with a frozen background and no way back.
  var stillMode = false;
  try { localStorage.removeItem('ace2_still'); } catch (e) {}
  var _stillHooks = [];   // each visual registers how to react when the mode flips
  function setStill(on) {
    stillMode = !!on;
    try { localStorage.setItem('ace2_still', stillMode ? 'on' : 'off'); } catch (e) {}
    for (var i = 0; i < _stillHooks.length; i++) { try { _stillHooks[i](stillMode); } catch (e) {} }
  }

  (function () {
    var canvas = $('matrix'), ctx = canvas.getContext('2d');
    var stars = [], meteors = [], running = true;
    // COOL MODE (2026-08-11): this full-screen starfield redrew ~60x/sec nonstop — the main
    // source of device heat + battery drain. Cap it (~30fps active, ~8fps when idle) and honor
    // the OS 'reduce motion' setting. Purely visual — Ace's brain/tools/voice are untouched.
    var _rm = false; try { _rm = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}
    var _lastTouch = (typeof performance !== 'undefined' ? performance.now() : 0), _lastPaint = 0;
    ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart'].forEach(function (ev) {
      window.addEventListener(ev, function () { _lastTouch = performance.now(); }, { passive: true });
    });
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
    // STILL MODE: paint the sky ONCE (mid-twinkle, no meteors) and stop the loop entirely.
    function paintStill() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i];
        ctx.fillStyle = 'rgba(' + s.hue + ',' + (.42 * s.z).toFixed(3) + ')';
        ctx.beginPath(); ctx.arc(s.x, s.y, s.s * s.z, 0, 6.283); ctx.fill();
      }
    }
    function frame(now) {
      if (!running) return;
      if (stillMode) { running = false; paintStill(); return; }    // freeze: one frame, loop OFF
      requestAnimationFrame(frame);
      var idle = (now - _lastTouch) > 20000;                       // untouched 20s → chill
      var gap = _rm ? (idle ? 400 : 66) : (idle ? 120 : 33);       // ms between paints
      if (now - _lastPaint < gap) return;                          // FPS cap: skip the draw, keep the loop
      _lastPaint = now;
      var t = now / 1000;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i];
        s.x += .012 * s.z; if (s.x > canvas.width + 2) s.x = -2;
        var a = (.25 + .45 * (.5 + .5 * Math.sin(t * s.ts + s.tw))) * s.z;
        ctx.fillStyle = 'rgba(' + s.hue + ',' + a.toFixed(3) + ')';
        ctx.beginPath(); ctx.arc(s.x, s.y, s.s * s.z, 0, 6.283); ctx.fill();
      }
      if (Math.random() < .0032 && meteors.length < 2) {
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
    }
    resize(); window.addEventListener('resize', function () { resize(); if (stillMode) paintStill(); });
    document.addEventListener('visibilitychange', function () {
      if (stillMode) return;   // frozen sky has nothing to pause or resume
      var was = running; running = document.visibilityState === 'visible';
      if (running && !was) requestAnimationFrame(frame);
    });
    _stillHooks.push(function (on) {
      if (on) { running = false; paintStill(); }
      else if (!running) { running = true; requestAnimationFrame(frame); }
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
    var _orbPaint = 0, _orm = false; try { _orm = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}
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

    // THE HUE JOURNEY — the galaxy's identity color slowly travels aurora → cyan →
    // violet → aurora (~48s loop), so the orb is never the same twice.
    var PAL = [[69, 255, 166], [79, 227, 255], [143, 123, 255]];
    function hue(t, offset) {
      var p = ((t / 16 + (offset || 0)) % 3 + 3) % 3;
      var i = Math.floor(p), f = p - i, a = PAL[i], b = PAL[(i + 1) % 3];
      return Math.round(a[0] + (b[0] - a[0]) * f) + ',' +
             Math.round(a[1] + (b[1] - a[1]) * f) + ',' +
             Math.round(a[2] + (b[2] - a[2]) * f);
    }
    // Comets — three bright wanderers with fading trails, orbiting outside the arms
    var comets = [];
    for (var cm = 0; cm < 3; cm++) comets.push({
      r: 78 + cm * 13, a0: Math.random() * 6.283,
      w: (.14 + Math.random() * .1) * (cm % 2 ? -1 : 1), trail: 9,
    });
    // Sonar pulses — expanding rings from the core, more often when he's speaking
    var pulses = [], lastPulse = 0;

    function frame(now) {
      requestAnimationFrame(frame);
      if (document.hidden) return;                       // COOL MODE: pause the orb in background
      // voice = smooth 60fps; idle = ~25fps; STILL MODE idle = ~2fps whisper (near-zero heat)
      var gap = amp > 0.001 ? 0 : (stillMode ? 500 : (_orm ? 80 : 40));
      if (now - _orbPaint < gap) return;
      _orbPaint = now;
      var t = now / 1000; last = now;
      speed += (speedT - speed) * .04; glow += (glowT - glow) * .05;
      // THE BREATH — slow inhale/exhale of scale and light; voice deepens it
      var breath = 1 + .045 * Math.sin(t * .55) + amp * .1;
      var light = .75 + .25 * Math.sin(t * .55 + .8) + glow * .5 + amp * .8;

      // Autonomous life: the whole cloud slowly drifts, rotates, and tilts on its own.
      var dx = 5.5 * Math.sin(t * 0.11), dy = 4.2 * Math.cos(t * 0.14);   // wander
      var ox = cx + dx, oy = cy + dy;
      var flat = 0.54 + 0.17 * Math.sin(t * 0.17);                      // disc tilt over time
      var gRot = t * 0.078 * speed;                                     // whole-galaxy rotation

      ctx.clearRect(0, 0, W, W);
      ctx.save();
      ctx.translate(ox, oy); ctx.scale(breath, breath); ctx.translate(-ox, -oy);
      ctx.globalCompositeOperation = 'lighter';   // pure additive glow on transparent — it floats in space

      // edgeless glow bed — hue-journeying: the whole halo drifts aurora→cyan→violet
      var H = hue(t, 0);
      var halo = ctx.createRadialGradient(ox, oy, 6, ox, oy, R * 1.02);
      var ha = (.14 + glow * .11 + amp * .14) * light;
      halo.addColorStop(0, 'rgba(' + H + ',' + ha.toFixed(3) + ')');
      halo.addColorStop(.5, 'rgba(' + H + ',' + (ha * .4).toFixed(3) + ')');
      halo.addColorStop(1, 'rgba(' + H + ',0)');
      ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(ox, oy, R * 1.02, 0, 6.283); ctx.fill();

      // sonar pulses — a ring breathes out from the heart every few seconds
      var pulseGap = 6.5 - glow * 3.5 - amp * 1.5;   // speaking = faster pulse
      if (t - lastPulse > pulseGap) { pulses.push({ born: t }); lastPulse = t; }
      for (var pu = pulses.length - 1; pu >= 0; pu--) {
        var age = (t - pulses[pu].born) / 3.2;
        if (age >= 1) { pulses.splice(pu, 1); continue; }
        var pr = 20 + age * R * .95;
        var pa = (1 - age) * .16 * light;
        ctx.strokeStyle = 'rgba(' + H + ',' + pa.toFixed(3) + ')';
        ctx.lineWidth = 1.1 - age * .7;
        ctx.beginPath(); ctx.ellipse(ox, oy, pr, pr * flat, 0, 0, 6.283); ctx.stroke();
      }

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

      // comets — bright wanderers with fading trails, cutting across the outer field
      for (var cc = 0; cc < comets.length; cc++) {
        var co = comets[cc];
        var ca = co.a0 + t * co.w * (0.7 + speed * 0.3);
        var chue = hue(t, cc + 1.2);
        for (var tr = 0; tr < co.trail; tr++) {
          var ta2 = ca - tr * 0.045 * (co.w > 0 ? 1 : -1);
          var txx = ox + Math.cos(ta2) * co.r, tyy = oy + Math.sin(ta2) * co.r * flat;
          var tal = (1 - tr / co.trail) * .5 * light;
          ctx.fillStyle = 'rgba(' + chue + ',' + tal.toFixed(3) + ')';
          ctx.beginPath(); ctx.arc(txx, tyy, 1.7 * (1 - tr / co.trail) + .3, 0, 6.283); ctx.fill();
        }
      }

      // luminous core — the galaxy's heart, white-hot center bleeding into the journey hue
      var coreR = 30 + amp * 12;
      var core = ctx.createRadialGradient(ox, oy, 0, ox, oy, coreR);
      core.addColorStop(0, 'rgba(240,255,248,' + (.92 * light).toFixed(3) + ')');
      core.addColorStop(.32, 'rgba(' + H + ',' + (.55 * light).toFixed(3) + ')');
      core.addColorStop(1, 'rgba(' + H + ',0)');
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
  /* The conversation opens as its OWN FULL SCREEN — a separate "tab", not a floating window
     (Brady). Opening builds a #chat-view overlay that covers the whole dashboard: its own header
     with a ✕, the live #messages, and the text box (#input-bar) MOVED into it so you type inside
     the chat. Closing parks #messages back in its hidden home (#chat-panel) and returns #input-bar
     to the bottom of #app, then removes the overlay — back to the orb dashboard. Same on both. */
  function chatCardMount() {
    if (document.getElementById('chat-view')) { scrollBottom(true); return; }
    var view = document.createElement('div'); view.id = 'chat-view';
    var head = document.createElement('div'); head.className = 'chat-view-head';
    var t = document.createElement('span'); t.className = 'chat-view-title'; t.textContent = 'CONVERSATION';
    var x = document.createElement('button'); x.className = 'chat-view-x'; x.setAttribute('aria-label', 'Close chat');
    x.textContent = '✕'; x.addEventListener('click', function () { setChat(false); });
    head.appendChild(t); head.appendChild(x);
    view.appendChild(head);
    view.appendChild(messagesEl);        // the live conversation
    view.appendChild($('input-bar'));    // move the text box into the chat tab
    $('app').appendChild(view);
    scrollBottom();
  }
  function chatCardUnmount() {
    var view = document.getElementById('chat-view');
    if (!view) return;
    $('chat-panel').appendChild(messagesEl);   // park conversation back in its hidden home
    $('app').appendChild($('input-bar'));      // input-bar back to the bottom of #app
    view.remove();
  }
  function applyChatState() {
    var btn = $('chat-toggle');
    // Same treatment on every device: voice/orb is the dashboard, the conversation is its own
    // windowed card on the stage — no full-screen takeover, no bleed between them.
    document.body.classList.toggle('chatting', chatOpen);
    if (chatOpen) {
      chatCardMount(); btn.classList.add('active'); btn.classList.remove('has-new');
      btn.setAttribute('aria-pressed', 'true'); scrollBottom(true);
    } else {
      chatCardUnmount(); btn.classList.remove('active'); btn.setAttribute('aria-pressed', 'false');
    }
  }
  function setChat(open) { chatOpen = open; localStorage.setItem('ace2_chat', open ? 'on' : 'off'); applyChatState(); }
  function markUnread() { if (!chatOpen) $('chat-toggle').classList.add('has-new'); }   // glow the toggle when a reply lands while closed
  $('chat-toggle').addEventListener('click', function () { setChat(!chatOpen); });
  $('chat-close').addEventListener('click', function () { setChat(false); });

  /* ============================================================ VOICE / CHAT MODE
     Brady's toggle (2026-07-19): VOICE = today's behavior (mic starts a live call,
     replies are spoken). CHAT = meeting-safe — completely silent, mic hidden, panel
     open, works like the Telegram bot. Persisted; survives reloads. */
  var aceMode = localStorage.getItem('ace2_mode') === 'chat' ? 'chat' : 'voice';
  function applyMode() {
    var chat = aceMode === 'chat';
    document.body.classList.toggle('mode-chat', chat);
    $('mode-voice').classList.toggle('on', !chat); $('mode-voice').setAttribute('aria-pressed', String(!chat));
    $('mode-chat').classList.toggle('on', chat); $('mode-chat').setAttribute('aria-pressed', String(chat));
  }
  function setMode(m) {
    if (m === aceMode) return;
    aceMode = m; localStorage.setItem('ace2_mode', m); applyMode();
    if (m === 'chat') {
      // Meeting-safe means NOTHING is listening or talking: kill the live call,
      // the record-and-transcribe fallback mic, and any TTS in flight.
      try { endLiveVoice(); } catch (e) {}
      state.handsFree = false;
      try { stopMic(); } catch (e) {}
      killTts();
      setChat(true);   // chat mode lives in the panel — open it
    }
  }
  $('mode-voice').addEventListener('click', function () { setMode('voice'); });
  $('mode-chat').addEventListener('click', function () { setMode('chat'); });

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
    applyMode();
    if (aceMode === 'chat' && !chatOpen) setChat(true);
    applyChatState();
    connectWS();
    loadToday();   // feeds the up-next strip; the stage stays clean (dock summons cards)
    if (window.__rehydratePins) window.__rehydratePins();   // pinned windows come back open
    checkConvai();
    loadHistory();   // renders the recent thread, then drops the greeting under it
    maybePushPrompt();   // one-time "let Ace reach your phone" offer (no-op if unsupported/unset)
    syncDiscreet();      // reflect the saved Discreet Mode state on the 🔒 toggle
    maybeAutoListen();   // opened via the "Ace" Siri Shortcut? start listening hands-free
  }

  /* DISCREET MODE (2026-08-11): a 🔒 toggle so Ace won't say dollar figures out loud when
     Brady's around people — he offers to read them or show them on screen instead. Server-side
     setting (so voice + brief pushes both respect it); the button just mirrors/flips it. */
  var discreetOn = false;
  function paintDiscreet() {
    var b = $('discreet-btn'); if (!b) return;
    b.classList.toggle('on', discreetOn);
    b.setAttribute('aria-pressed', String(discreetOn));
    b.textContent = discreetOn ? '🔒' : '🔓';
    b.title = discreetOn
      ? 'Discreet mode ON — Ace keeps money quiet (tap to turn off)'
      : 'Discreet mode OFF — tap when you’re around people so Ace won’t say money out loud';
  }
  function syncDiscreet() {
    fetch(API + '/settings', { headers: headers() })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) { discreetOn = !!d.discreet; paintDiscreet(); } })
      .catch(function () {});
  }
  (function () {
    var b = $('discreet-btn'); if (!b) return;
    b.addEventListener('click', function () {
      var want = !discreetOn;
      discreetOn = want; paintDiscreet();   // optimistic
      fetch(API + '/settings/discreet', { method: 'POST', headers: headers(), body: JSON.stringify({ on: want }) })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d) { discreetOn = !!d.discreet; } paintDiscreet(); addAceMessage(discreetOn ? '🔒 Discreet mode on — I’ll keep the money talk off the speaker and offer to show you instead.' : '🔓 Discreet mode off — back to normal.'); })
        .catch(function () { discreetOn = !want; paintDiscreet(); });
    });
  })();

  /* AUTO-LISTEN (2026-08-11): the "Ace" Siri Shortcut opens the app with ?listen=1 (or #listen)
     so Ace starts listening the moment he loads — a wake word via Siri. iOS may still require one
     tap to grant the mic (Apple blocks auto-mic without a gesture); if so, the mic button pulses. */
  function maybeAutoListen() {
    var want = /[?&#]listen(=1)?\b/.test(location.search + location.hash);
    if (!want) return;
    try { history.replaceState(null, '', location.pathname); } catch (e) {}   // don't re-trigger on refresh
    setTimeout(function () {
      try {
        if (aceMode !== 'voice') setMode('voice');
        state.handsFree = true; startMic();
      } catch (e) {}
      // If the browser blocked the mic without a tap, draw the eye to the mic button.
      setTimeout(function () {
        if (!state.micActive) { var m = $('mic-btn'); if (m) { m.classList.add('active'); setTimeout(function(){ if(!state.micActive) m.classList.remove('active'); }, 4000); } }
      }, 900);
    }, 700);
  }
  /* Last ~18 turns of Ace 2.0's own history, preloaded so the panel opens with
     context instead of empty (Brady: "see history without always asking him").
     Guarded so a re-login can't duplicate it, and INSERTED AT THE TOP so a message
     Brady fires off before the fetch resolves still reads in order. */
  var historyLoaded = false, greeted = false, lastThreadTs = null;
  // Apple-Messages feel: don't stack a fresh greeting on a thread you were JUST in. Only greet on
  // a genuinely fresh start — empty thread, or the last message is more than a few hours old.
  function maybeGreet() {
    if (greeted) return; greeted = true;
    if (lastThreadTs) { var age = Date.now() - new Date(lastThreadTs).getTime();
      if (!isNaN(age) && age < 4 * 3600 * 1000) return; }
    greeting();
  }
  function loadHistory() {
    if (historyLoaded) { maybeGreet(); return; }
    historyLoaded = true;
    fetch(API + '/thread?limit=18', { headers: headers() })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var msgs = (d && d.messages) || [];
        if (!msgs.length) return;
        var last = msgs[msgs.length - 1]; if (last && last.ts) lastThreadTs = last.ts;
        var frag = document.createDocumentFragment();
        msgs.forEach(function (m) {
          var el = document.createElement('div');
          el.className = 'msg ' + (m.role === 'user' ? 'user' : 'ace') + ' hist';
          if (m.role !== 'user') { var s = document.createElement('div'); s.className = 'sender'; s.textContent = 'ACE'; el.appendChild(s); }
          el.appendChild(document.createTextNode(m.text));
          var lab = tsLabel(m.ts); if (lab) el.appendChild(stampEl(lab));
          frag.appendChild(el);
        });
        var sep = document.createElement('div'); sep.className = 'hist-sep'; sep.textContent = '— now —'; frag.appendChild(sep);
        messagesEl.insertBefore(frag, messagesEl.firstChild);
      })
      .catch(function () {})
      .then(function () { maybeGreet(); scrollBottom(true); });
  }
  function greeting() {
    var d = new Date(), days = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    var day = days[d.getDay()];
    var lines = [
      'Online, ' + day + '. What are we moving first?',
      day + '. Board\'s in front of me — where do you want to start?',
      'Back online. ' + day + ' — say the word.',
      'Ready when you are, ' + day + '. What\'s first?',
      day + '. Everything\'s live. What do you need?',
      'I\'m up. ' + day + ' — what\'s the priority?'
    ];
    addAceMessage(lines[Math.floor(Math.random() * lines.length)]);
  }

  /* ============================================================ WEBSOCKET */
  function wsURL() { var p = location.protocol === 'https:' ? 'wss:' : 'ws:'; var q = state.token ? ('?token=' + encodeURIComponent(state.token)) : ''; return p + '//' + location.host + '/ws/chat' + q; }
  function connectWS() {
    var ws; try { ws = new WebSocket(wsURL()); } catch (e) { setLink(false); scheduleReconnect(); return; }
    state.ws = ws;
    ws.onopen = function () { state.wsReady = true; state.reconnectDelay = 1000; setLink(true); };
    ws.onclose = function (ev) { state.wsReady = false; state.busy = false; setLink(false); if (ev && ev.code === 4401) { toLogin(); return; } scheduleReconnect(); };
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
      case 'brief':   // proactive brief pushed live — spoken like JARVIS unless in silent chat mode
        addAceMessage(msg.text);
        (function (kind) {   // 👍/👎 feedback row — votes steer tomorrow's brief
          var row = document.createElement('div'); row.className = 'brief-fb';
          ['up', 'down'].forEach(function (v) {
            var b = document.createElement('button'); b.textContent = v === 'up' ? '👍' : '👎';
            b.onclick = function () {
              fetch(API + '/brief/feedback', { method: 'POST', headers: headers(),
                body: JSON.stringify({ kind: kind, vote: v }) }).catch(function () {});
              row.textContent = v === 'up' ? 'Noted — keeping this style.' : 'Noted — tightening it up.';
            };
            row.appendChild(b);
          });
          messagesEl.appendChild(row); scrollBottom();
        })(msg.kind || 'morning');
        if (!document.body.classList.contains('mode-chat')) speak(msg.text);
        break;
      case 'nudge':   // ambient heads-up Ace raised on his own — lands QUIET: no voice, no
        addAceMessage(msg.text).classList.add('nudge');   // panel takeover, just the unread glow
        break;
      case 'open': openLink(msg.url, msg.label); break;
      case 'run_on_hud': runOnHud(msg.message); break;
      case 'confirmation': renderConfirm(msg.text); break;
      case 'final': if (streamMsg) finalizeStream(streamMsg, msg.text); break;
      case 'error': removeTyping(); discardEmptyStream(); addAceMessage(msg.text); state.busy = false;
        if (!ttsPlaying) { setOrbState(state.micActive ? 'listening' : 'idle'); maybeResumeMic(); } break;
      case 'done': discardEmptyStream(); streamMsg = null; activeTool = null; state.busy = false;
        if (!ttsPlaying) { setOrbState(state.micActive ? 'listening' : 'idle'); maybeResumeMic(); } break;
    }
  }

  /* Voice handed a Google Workspace authoring task to the screen (build_on_screen →
     backend broadcast). Run it as a normal typed turn so it goes through the full-MCP
     typed brain and the on-screen confirm flow — while Ace tells Brady, out loud, to
     watch his screen. Open the chat panel so he sees the work and any confirm question.
     If a turn is already in flight, wait for it rather than dropping the handoff. */
  var _hudQueue = [];
  function runOnHud(text) {
    text = (text || '').trim();
    if (!text) return;
    if (document.hidden) return;   // only the focused HUD runs a handoff — avoids duplicate turns across open devices/tabs
    setChat(true);
    if (state.busy) { _hudQueue.push(text); setTimeout(drainHudQueue, 1000); return; }
    sendMessage(text);
  }
  function drainHudQueue() {
    if (!_hudQueue.length) return;
    if (state.busy) { setTimeout(drainHudQueue, 1000); return; }
    runOnHud(_hudQueue.shift());
  }

  function sendMessage(text) {
    text = (text || $('chat-input').value).trim();
    if (!text || state.busy) return;
    state.busy = true; $('chat-input').value = '';
    $('chat-input').style.height = 'auto';   // collapse the grown textarea back to one line
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
  // Sticky scroll (2026-08-23, Brady: "it keeps scrolling to the bottom" while reading):
  // only follow the stream if he is already at (or near) the bottom. His own scroll-up stops
  // the yanking; scrolling back down (or force=true — his own send, opening the panel)
  // re-engages the follow.
  var stickBottom = true;
  messagesEl.addEventListener('scroll', function () {
    stickBottom = (messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight) < 60;
  });
  function scrollBottom(force) {
    if (force) stickBottom = true;
    if (stickBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  function nowLabel() { var d = new Date(), h = d.getHours(), ap = h >= 12 ? 'PM' : 'AM', h12 = h % 12 || 12; return h12 + ':' + (d.getMinutes() < 10 ? '0' : '') + d.getMinutes() + ' ' + ap; }
  // Apple-Messages-style stamp for a given ISO time: time only if today, else a short day/date prefix.
  function tsLabel(iso) {
    if (!iso) return nowLabel();
    var d = new Date(iso); if (isNaN(d.getTime())) return '';
    var h = d.getHours(), ap = h >= 12 ? 'PM' : 'AM', h12 = h % 12 || 12;
    var t = h12 + ':' + (d.getMinutes() < 10 ? '0' : '') + d.getMinutes() + ' ' + ap;
    var now = new Date();
    if (d.toDateString() === now.toDateString()) return t;
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    return ((now - d) < 7 * 864e5 ? days[d.getDay()] : (d.getMonth() + 1) + '/' + d.getDate()) + ' ' + t;
  }
  function stampEl(label) { var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = label; return ts; }
  function addUserMessage(t) { var m = document.createElement('div'); m.className = 'msg user'; m.appendChild(document.createTextNode(t)); m.appendChild(stampEl(nowLabel())); messagesEl.appendChild(m); scrollBottom(true); }
  function addAceMessage(t) { var m = document.createElement('div'); m.className = 'msg ace'; m.innerHTML = '<div class="sender">ACE</div>'; m.appendChild(document.createTextNode(t)); var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel(); m.appendChild(ts); messagesEl.appendChild(m); scrollBottom(); markUnread(); return m; }
  function beginAceStream() { var m = document.createElement('div'); m.className = 'msg ace'; m.innerHTML = '<div class="sender">ACE</div>'; var b = document.createElement('span'); m.appendChild(b); var c = document.createElement('span'); c.className = 'cursor'; c.textContent = ' '; m.appendChild(c); messagesEl.appendChild(m); scrollBottom(); return { el: m, body: b, cursor: c, text: '' }; }
  function appendToStream(s, t) { s.text += t; s.body.textContent = s.text; scrollBottom(); }
  function discardEmptyStream() { if (streamMsg && !streamMsg.text && streamMsg.el.parentNode) { streamMsg.el.parentNode.removeChild(streamMsg.el); streamMsg = null; } }
  function finalizeStream(s, txt) { s.body.textContent = txt || s.text; if (s.cursor.parentNode) s.cursor.parentNode.removeChild(s.cursor); var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel(); s.el.appendChild(ts); speak(txt || s.text); scrollBottom(); markUnread(); }
  function renderTool(msg) {
    if (msg.status === 'running') { activeTool = document.createElement('div'); activeTool.className = 'tool-pill'; activeTool.innerHTML = '<span class="spin">◈</span> '; activeTool.appendChild(document.createTextNode(msg.label + '…')); messagesEl.appendChild(activeTool); scrollBottom(); }
    else if (activeTool) { activeTool.className = 'tool-pill done'; activeTool.innerHTML = '◈ '; activeTool.appendChild(document.createTextNode(msg.label)); activeTool = null; scrollBottom(); }
  }
  function renderConfirm(text) {
    text = String(text == null ? '' : text);
    var p = document.createElement('div'); p.className = 'tool-pill done';
    var nl = text.indexOf('\n'), first = (nl >= 0 ? text.slice(0, nl) : text).trim();
    if (nl < 0 && first.length <= 110) {   // short receipt → the familiar pill, unchanged
      p.appendChild(document.createTextNode(text)); messagesEl.appendChild(p); scrollBottom(); return;
    }
    // Long receipt (2026-08-23, Brady: "takes up most of the screen") → one-line chip;
    // tap to expand the full text, which scrolls inside itself.
    p.classList.add('receipt');
    var head = document.createElement('div'); head.className = 'receipt-head';
    head.textContent = (first.length > 96 ? first.slice(0, 93) + '…' : first) + '  ▸';
    var body = document.createElement('div'); body.className = 'receipt-body';
    body.textContent = text;
    p.appendChild(head); p.appendChild(body);
    p.addEventListener('click', function () {
      var open = p.classList.toggle('open');
      head.textContent = head.textContent.slice(0, -1) + (open ? '▾' : '▸');
    });
    messagesEl.appendChild(p); scrollBottom();
  }
  var typingEl = null;
  function showTyping() { removeTyping(); typingEl = document.createElement('div'); typingEl.className = 'msg ace'; typingEl.innerHTML = '<div class="sender">ACE</div><span class="typing"><span></span><span></span><span></span></span>'; messagesEl.appendChild(typingEl); scrollBottom(); }
  function removeTyping() { if (typingEl && typingEl.parentNode) typingEl.parentNode.removeChild(typingEl); typingEl = null; }

  /* ============================================================ CARDS — Ace's projections */
  // Default screen side per card; Ace can override with a `where` on display_card.
  var CARD_SLOTS = { timeline: 'left', daybank: 'right', calendar: 'left', tasks: 'left',
                     weather: 'right', memory: 'right', inbox: 'right' };
  function slotEl(where) { return (where === 'left') ? $('cards-left') : $('cards'); }
  // WINDOW SYSTEM — pins (cards that stay open across sessions) + per-card size.
  var TITLE_PANEL = { 'TODAY': 'timeline', 'PRIORITY INBOX': 'inbox', 'WEATHER': 'weather',
                      'MEMORY': 'memory', 'CALENDAR': 'calendar', 'TASKS': 'tasks', 'DUE TODAY': 'daybank' };
  function pinsGet() { try { return JSON.parse(localStorage.getItem('ace2_pins') || '{}'); } catch (e) { return {}; } }
  function pinsSet(p) { localStorage.setItem('ace2_pins', JSON.stringify(p)); }
  // WINDOW MANAGER (desktop): cards are free windows — drag them anywhere by the title
  // bar; positions are remembered per panel. Phones keep the stacked flow.
  function freeWM() { return window.matchMedia('(min-width: 900px) and (min-height: 560px)').matches; }
  function winposGet() { try { return JSON.parse(localStorage.getItem('ace2_winpos') || '{}'); } catch (e) { return {}; } }
  function winposSet(p) { localStorage.setItem('ace2_winpos', JSON.stringify(p)); }
  var WIN_DEFAULTS = { timeline: [.015, .05], calendar: [.015, .34], daybank: [.015, .56],
                       inbox: [.72, .05], memory: [.72, .34], weather: [.72, .62], tasks: [.36, .62] };
  var zTop = 10;

  function cardShell(title, where) {
    var host = slotEl(where);
    var card = document.createElement('div'); card.className = 'card';
    var head = document.createElement('div'); head.className = 'card-head';
    head.appendChild(document.createTextNode(title));
    var panel = TITLE_PANEL[title] || null;
    var pins = pinsGet();
    if (panel && pins[panel] && pins[panel].size === 'wide') card.classList.add('card-wide');
    // ⤢ size toggle — normal ↔ wide (remembered per card)
    var sz = document.createElement('button'); sz.className = 'card-x'; sz.textContent = '⤢';
    sz.title = 'Resize';
    sz.addEventListener('click', function (ev) {
      ev.stopPropagation();
      var wide = card.classList.toggle('card-wide');
      if (panel) { var p = pinsGet(); p[panel] = p[panel] || {}; p[panel].size = wide ? 'wide' : null;
        if (!p[panel].pinned && !p[panel].size) delete p[panel]; pinsSet(p); }
    });
    head.appendChild(sz);
    // 📌 pin — this window STAYS OPEN: it survives dismissal and comes back on every boot
    if (panel) {
      var pin = document.createElement('button'); pin.className = 'card-x card-pin'; pin.textContent = '📌';
      pin.title = 'Keep open';
      if (pins[panel] && pins[panel].pinned) pin.classList.add('on');
      pin.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var p = pinsGet(); p[panel] = p[panel] || {};
        p[panel].pinned = !p[panel].pinned;
        pin.classList.toggle('on', p[panel].pinned);
        if (!p[panel].pinned && !p[panel].size) delete p[panel];
        pinsSet(p);
      });
      head.appendChild(pin);
    }
    var x = document.createElement('button'); x.className = 'card-x'; x.textContent = '✕';
    x.addEventListener('click', function (ev) { ev.stopPropagation(); card.remove(); });
    head.appendChild(x);
    // VIEWFINDER — tap the title bar to expand; DRAG it to move the window (desktop).
    head.title = 'Tap to expand · drag to move';
    var dragged = false;
    head.addEventListener('click', function () {
      if (dragged) { dragged = false; return; }   // a drag is not a tap
      if (card.classList.contains('card-max')) {
        card.classList.remove('card-max');
        if (card.dataset.wx) { card.style.left = card.dataset.wx; card.style.top = card.dataset.wy; }
      } else {
        card.dataset.wx = card.style.left || ''; card.dataset.wy = card.style.top || '';
        card.style.left = ''; card.style.top = '';
        card.classList.add('card-max');
      }
    });
    // WINDOW MANAGER drag — pointer-based, 6px threshold, position saved per panel.
    head.addEventListener('pointerdown', function (ev) {
      if (!card.classList.contains('card-free') || card.classList.contains('card-max')) return;
      if (ev.target !== head && ev.target.nodeType === 1 && ev.target.tagName === 'BUTTON') return;
      var sx = ev.clientX, sy = ev.clientY;
      var r = card.getBoundingClientRect(), host = $('stage').getBoundingClientRect();
      var ox = r.left - host.left, oy = r.top - host.top, moved = false;
      function mv(e2) {
        var dx = e2.clientX - sx, dy = e2.clientY - sy;
        if (!moved && Math.abs(dx) + Math.abs(dy) < 6) return;
        moved = true; dragged = true;
        card.style.left = Math.max(0, Math.min(host.width - 60, ox + dx)) + 'px';
        card.style.top = Math.max(0, Math.min(host.height - 40, oy + dy)) + 'px';
      }
      function up() {
        document.removeEventListener('pointermove', mv);
        document.removeEventListener('pointerup', up);
        if (moved && panel) { var wp = winposGet(); wp[panel] = [card.style.left, card.style.top]; winposSet(wp); }
      }
      document.addEventListener('pointermove', mv);
      document.addEventListener('pointerup', up);
    });
    card.addEventListener('pointerdown', function () { card.style.zIndex = ++zTop; });
    var body = document.createElement('div'); body.className = 'card-body';
    card.appendChild(head); card.appendChild(body);
    // one card per panel across BOTH slots (so re-placing moves it); cap each slot at 4
    var dup = document.querySelector('.card[data-panel="' + title + '"]');
    if (dup) {
      // a maximized (viewfinder) card stays maximized through data refreshes
      if (dup.classList.contains('card-max')) card.classList.add('card-max');
      dup.remove();
    }
    card.setAttribute('data-panel', title);
    if (freeWM()) {
      // Desktop: a FREE WINDOW on the stage — saved position, or this panel's default spot.
      card.classList.add('card-free');
      var stage = $('stage'), sr = stage.getBoundingClientRect();
      var saved = panel && winposGet()[panel];
      if (saved && saved[0]) { card.style.left = saved[0]; card.style.top = saved[1]; }
      else {
        var dp = (panel && WIN_DEFAULTS[panel]) || [.4, .1];
        card.style.left = Math.round(sr.width * dp[0]) + 'px';
        card.style.top = Math.round(sr.height * dp[1]) + 'px';
      }
      card.style.zIndex = ++zTop;
      stage.appendChild(card);
    } else {
      host.insertBefore(card, host.firstChild);
      while (host.children.length > 4) host.removeChild(host.lastChild);   // cap each slot at 4
    }
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
      // LEGACY surface (2026-07-31): Google Tasks was retired to Ace's own board. This card
      // stays reachable but is clearly labeled and its checkbox-lookalikes are gone — they
      // were unclickable and made pre-migration copies read as forever-unchecked tasks.
      var body2 = cardShell('GOOGLE TASKS (LEGACY)', slot);
      var lists = data.lists || null;
      function taskRow(t) {
        var row = document.createElement('div'); row.className = 'c-task';
        var box = document.createElement('span'); box.textContent = '·'; box.style.opacity = '.5'; box.style.padding = '0 6px';
        var bd = document.createElement('span'); bd.style.flex = '1'; bd.appendChild(document.createTextNode(t.title || ''));
        if (t.due) { var due = document.createElement('span'); due.className = 'c-due'; due.textContent = '  ⏱ ' + t.due; bd.appendChild(due); }
        row.appendChild(box); row.appendChild(bd); return row;
      }
      if (lists && lists.length) {
        // Click between ALL your lists (incl. empty ones). Default to the fullest.
        var tabs = document.createElement('div'); tabs.className = 'tl-tabs';
        var items = document.createElement('div'); items.className = 'tl-items';
        var sel = 0, best = -1;
        lists.forEach(function (l, i) { if ((l.count || 0) > best) { best = l.count || 0; sel = i; } });
        function renderList(idx) {
          Array.prototype.forEach.call(tabs.children, function (c, i) { c.classList.toggle('on', i === idx); });
          while (items.firstChild) items.removeChild(items.firstChild);
          var ts = lists[idx].tasks || [];
          if (!ts.length) { var e = document.createElement('div'); e.className = 'empty-note'; e.textContent = 'Nothing open here.'; items.appendChild(e); return; }
          ts.slice(0, 50).forEach(function (t) { items.appendChild(taskRow(t)); });
        }
        lists.forEach(function (l, i) {
          var tab = document.createElement('button'); tab.className = 'tl-tab';
          tab.appendChild(document.createTextNode(l.list || 'List'));
          var n = document.createElement('span'); n.className = 'tl-n'; n.textContent = String(l.count || 0); tab.appendChild(n);
          tab.addEventListener('click', function () { renderList(i); });
          tabs.appendChild(tab);
        });
        body2.appendChild(tabs); body2.appendChild(items);
        renderList(sel);
      } else {
        var tasks = data.tasks || [];
        if (!tasks.length) return empty(body2, 'Inbox zero.');
        tasks.slice(0, 12).forEach(function (t) {
          var row = taskRow(t);
          if (t.list) { var meta = document.createElement('span'); meta.className = 'c-meta'; meta.textContent = t.list; row.lastChild.appendChild(meta); }
          body2.appendChild(row);
        });
      }
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
      // TODAY (2026-08-26, Brady): was 'DATA BANK' — a flat dump of whatever was newest, which
      // is why a test row and a DNS task sat side by side. Now it answers the only question he
      // opens it for: what's late, what's due, and what is waiting on my OK. 'Data Bank' is
      // reserved for the curated records layer (people/deals/goals/bills), not the task list.
      var all6 = (data.items || []).filter(function (x) { return x.status === 'open'; });
      var appr = all6.filter(function (x) { return x.kind === 'approval'; });
      var rest = all6.filter(function (x) { return x.kind !== 'approval'; });
      var late = rest.filter(function (x) { return x.due_days != null && x.due_days < 0; })
                     .sort(function (a, b) { return a.due_days - b.due_days; });
      var now6 = rest.filter(function (x) { return x.due_days === 0; });
      var soon = rest.filter(function (x) { return x.due_days === 1; });
      var body6 = cardShell('DUE TODAY', slot);
      if (!late.length && !now6.length && !soon.length && !appr.length) {
        return empty(body6, 'Nothing due and nothing waiting on you. Clear.');
      }
      function tag(cls, t) { var s = document.createElement('span'); s.className = cls; s.textContent = t; return s; }
      function sect(label, rows, cls) {
        if (!rows.length) return;
        var h = document.createElement('div'); h.className = 'db-sect ' + cls;
        h.textContent = label + ' · ' + rows.length; body6.appendChild(h);
        rows.forEach(function (it) {
          var row = document.createElement('div'); row.className = 'db-item ' + cls;
          var box = document.createElement('button'); box.className = 'db-box';
          box.title = (cls === 'appr') ? 'Approve — Ace executes this' : 'Mark done';
          box.addEventListener('click', function () { toggleBankItem(it.id, 'done'); });
          var mid = document.createElement('div'); mid.className = 'db-mid';
          var txt = document.createElement('div'); txt.className = 'db-text'; txt.textContent = it.text || '';
          var meta = document.createElement('div'); meta.className = 'db-meta';
          var c = cmdCatOf(it);
          if (c) { var ct = tag('db-cat', c); ct.style.color = CMD_CATS[c] || '#8aa';
                   ct.style.borderColor = (CMD_CATS[c] || '#8aa') + '55'; meta.appendChild(ct); }
          if (cls === 'appr') meta.appendChild(tag('db-appr', 'TAP TO APPROVE'));
          else if (it.due_days != null) {
            var d = it.due_days;
            meta.appendChild(tag('db-due', d < 0 ? (Math.abs(d) + 'd overdue')
                                                : d === 0 ? 'due today' : 'due tomorrow'));
          }
          mid.appendChild(txt); mid.appendChild(meta);
          row.appendChild(box); row.appendChild(mid); body6.appendChild(row);
        });
      }
      sect('WAITING ON YOUR OK', appr, 'appr');
      sect('OVERDUE', late, 'late');
      sect('DUE TODAY', now6, 'now');
      sect('TOMORROW', soon, 'soon');
    } else if (panel === 'inbox') {
      // TWO LANES (2026-08-25): business + personal, clearly separated, each row tappable
      // straight into the right Gmail account. Falls back to the legacy single list if the
      // server hasn't been updated yet.
      // TABBED INBOX (2026-08-25, Brady: "needs to be bigger, toggle between the 2, and
      // shouldn't just be things I haven't read"). One mailbox at a time, full width, recent
      // mail read OR unread, unread visibly marked. Tab choice is remembered.
      var biz = data.business || data.emails || [];
      var per = data.personal || [];
      var unreadBiz = biz.filter(function (m) { return m.unread; }).length;
      var unreadPer = per.filter(function (m) { return m.unread; }).length;
      var body7 = cardShell('INBOX', slot);
      body7.parentNode.classList.add('card-wide');          // this card earns the width
      var which = 'biz';
      try { which = localStorage.getItem('ace2_inbox_tab') === 'per' ? 'per' : 'biz'; } catch (e) {}
      if (which === 'per' && !per.length && biz.length) which = 'biz';

      var tabs = document.createElement('div'); tabs.className = 'in-tabs';
      var listWrap = document.createElement('div'); listWrap.className = 'in-list';
      function mkTab(key, label, count, unread) {
        var b = document.createElement('button');
        b.className = 'in-tab' + (key === which ? ' on' : '') + ' ' + key;
        b.textContent = label + ' ' + count;
        if (unread) { var d = document.createElement('span'); d.className = 'in-badge';
                      d.textContent = unread; b.appendChild(d); }
        b.addEventListener('click', function () {
          which = key;
          try { localStorage.setItem('ace2_inbox_tab', key); } catch (e) {}
          Array.prototype.forEach.call(tabs.children, function (c) { c.classList.remove('on'); });
          b.classList.add('on'); paint();
        });
        return b;
      }
      tabs.appendChild(mkTab('biz', 'WORK', biz.length, unreadBiz));
      tabs.appendChild(mkTab('per', 'PERSONAL', per.length, unreadPer));
      body7.appendChild(tabs); body7.appendChild(listWrap);

      function paint() {
        listWrap.innerHTML = '';
        var rows = which === 'per' ? per : biz;
        if (!rows.length) {
          var none = document.createElement('div'); none.className = 'in-none';
          none.textContent = which === 'per'
            ? 'Nothing in the personal inbox for the last two weeks.'
            : 'Nothing in the work inbox for the last two weeks.';
          listWrap.appendChild(none); return;
        }
        rows.forEach(function (m) {
          var row = document.createElement('div');
          row.className = 'in-row ' + which + (m.unread ? ' unread' : ' read');
          var top = document.createElement('div'); top.className = 'in-top';
          var from = document.createElement('div'); from.className = 'in-from'; from.textContent = m.from || '';
          var when = document.createElement('div'); when.className = 'in-when'; when.textContent = (m.date || '').slice(0, 17);
          top.appendChild(from); top.appendChild(when);
          var subj = document.createElement('div'); subj.className = 'in-subj'; subj.textContent = m.subject || '';
          var snip = document.createElement('div'); snip.className = 'in-snip'; snip.textContent = m.snippet || '';
          row.appendChild(top); row.appendChild(subj); row.appendChild(snip);
          if (m.open_url) {
            row.classList.add('in-open'); row.title = 'Open in Gmail';
            row.addEventListener('click', function () { window.open(m.open_url, '_blank', 'noopener'); });
          }
          listWrap.appendChild(row);
        });
      }
      paint();
    }
  }
  // CLEAN BY DEFAULT (Brady's call): no auto-cards on startup — the stage is the orb,
  // the dock summons surfaces on demand, and loadToday() keeps the up-next strip live.
  function toggleBankItem(id, status) {
    fetch(API + '/daybank/update', { method: 'POST', headers: headers(), body: JSON.stringify({ id: id, status: status }) })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.items) materializeCard('daybank', { items: d.items });
        // Keep an open Command board in step — same store, one truth (2026-07-31).
        if (typeof cmdSync === 'function') cmdSync();
      })
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
  var convaiEnabled = false, liveConv = null, liveStarting = false, liveRAF = null, liveMode = 'idle';
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
  /* iPad/phone call durability (2026-08-24, Brady: "he keeps timing out mid-conversation on my
     iPad; after a second I can turn him back on"). Two causes, both client-side — the ElevenLabs
     agent has silence_end_call disabled, so it wasn't hanging up on him:
       1. The tablet dims/sleeps or Safari backgrounds the tab → WebRTC dies → onDisconnect.
          A SCREEN WAKE LOCK while a call is live keeps the device awake (iOS 16.4+).
       2. ANY drop — a WiFi-band hop, a momentary blip — called endLiveVoice() and stayed dead.
          Now an UNINTENDED drop auto-resumes (3 tries, short backoff) instead of ending the call. */
  var liveWantOn = false, liveRetries = 0, liveWake = null;
  function wakeAcquire() {
    try {
      if (!navigator.wakeLock || liveWake) return;
      navigator.wakeLock.request('screen').then(function (wl) {
        liveWake = wl;
        wl.addEventListener('release', function () { liveWake = null; });
      }).catch(function () {});
    } catch (e) {}
  }
  function wakeRelease() {
    try { if (liveWake) { liveWake.release(); liveWake = null; } } catch (e) {}
  }
  // The lock is dropped whenever the page hides — re-take it (and re-check the call) on return.
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || !liveWantOn) return;
    wakeAcquire();
    if (!liveConv && !liveStarting) startLiveVoice();   // came back to a dead call → resume it
  });
  function liveDropped() {
    // Session ended without Brady asking. Resume if he's still "on a call".
    if (liveRAF) { cancelAnimationFrame(liveRAF); liveRAF = null; }
    liveConv = null; liveStarting = false; liveMode = 'idle'; orb.setAmplitude(0);
    if (!liveWantOn) { wakeRelease(); $('mic-btn').classList.remove('active');
                       if (!state.busy) setOrbState('idle'); return; }
    if (liveRetries >= 3) {
      liveWantOn = false; wakeRelease(); $('mic-btn').classList.remove('active');
      if (!state.busy) setOrbState('idle');
      addAceMessage('Call dropped and I couldn’t get back on — tap the mic when you’re ready.');
      return;
    }
    liveRetries++;
    setOrbState('listening');
    setTimeout(function () { if (liveWantOn && !liveConv && !liveStarting) startLiveVoice(); }, 700 * liveRetries);
  }

  function startLiveVoice() {
    if (liveConv || liveStarting) return;   // already on a call or spinning one up — ignore extra taps
    var Conv = window.__ConvAI;
    if (!Conv) { addAceMessage('Live voice is still loading — give it a second and tap again.'); return; }
    liveWantOn = true; wakeAcquire();
    liveStarting = true;
    $('mic-btn').classList.add('active'); setOrbState('listening'); liveMode = 'listening';
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      .then(function () { return fetch(API + '/convai/signed-url', { headers: headers() }); })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!liveStarting) return null;   // user hit the mic again to cancel while we were connecting
        if (!d || !d.signed_url) throw ((d && d.error) || 'no signed url');
        return Conv.startSession({
          signedUrl: d.signed_url,
          onConnect: function () { liveRetries = 0; liveRAF = requestAnimationFrame(livePulse); },
          onDisconnect: function () { liveDropped(); },   // auto-resume unless Brady hung up
          onError: function (err) {
            // Don't spam the thread on a transient blip — liveDropped() retries silently and
            // only speaks up if it genuinely can't get back on.
            if (!liveWantOn) addAceMessage('Live voice error: ' + (err && err.message || err));
            liveDropped();
          },
          onModeChange: function (m) { var mm = (m && m.mode) || m; liveMode = (mm === 'speaking') ? 'speaking' : 'listening'; setOrbState(liveMode); },
          onMessage: function (msg) {
            var t = (msg && (msg.message || msg.text)) || '';
            if (!t) return;
            var src = msg && (msg.source || msg.role);
            if (src === 'user' || src === 'user_transcript') addUserMessage(t); else addAceMessage(t);
          },
        });
      })
      .then(function (conv) {
        if (!conv) return;                                   // cancelled before the session opened
        if (!liveStarting) { try { conv.endSession(); } catch (e) {} return; }  // cancelled mid-connect → tear it down
        liveConv = conv; liveStarting = false;
      })
      .catch(function (err) {
        addAceMessage('Couldn’t start live voice (' + err + '). If you just set it up, give ElevenLabs a moment and try again.');
        endLiveVoice();
      });
  }
  function endLiveVoice() {
    // Deliberate hang-up (mic tap, switching to chat mode): stop wanting the call, so a
    // disconnect event can't bounce it back on.
    liveWantOn = false; liveRetries = 0; wakeRelease();
    liveStarting = false;
    if (liveRAF) cancelAnimationFrame(liveRAF); liveRAF = null; liveMode = 'idle';
    if (liveConv) { try { liveConv.endSession(); } catch (e) {} liveConv = null; }
    orb.setAmplitude(0); $('mic-btn').classList.remove('active');
    if (!state.busy) setOrbState('idle');
  }
  $('mic-btn').addEventListener('click', function () {
    if (aceMode === 'chat') return;   // chat mode: mic is hidden/inert — flip to VOICE to call
    if (convaiEnabled) { if (liveConv || liveStarting) endLiveVoice(); else startLiveVoice(); return; }   // full-duplex: one tap starts, next tap ALWAYS stops
    if (state.handsFree || state.micActive) { state.handsFree = false; stopMic(); }        // fallback: record + transcribe
    else { state.handsFree = true; startMic(); }
  });

  $('voice-toggle').addEventListener('click', function () {
    state.voiceOut = !state.voiceOut; localStorage.setItem('ace2_voice', state.voiceOut ? 'on' : 'off');
    $('voice-toggle').classList.toggle('off', !state.voiceOut); $('voice-toggle').setAttribute('aria-pressed', String(state.voiceOut));
    if (!state.voiceOut) killTts();
  });
  $('voice-toggle').classList.toggle('off', !state.voiceOut);

  // Hard-stop ALL reply audio. Nulling ttsAudio first is what stops playAudioBlob's
  // pulse rAF loop (it checks ttsAudio each frame) — pause() alone never fires
  // onended, which used to leave the orb surging in [ SPEAKING ] forever.
  function killTts() {
    try { window.speechSynthesis.cancel(); } catch (e) {}
    var a = ttsAudio; ttsAudio = null;
    if (a) { try { a.onended = a.onerror = null; a.pause(); a.src = ''; } catch (e) {} }
    ttsQueue = []; ttsPlaying = false; orb.setAmplitude(0);
    if (!state.busy) setOrbState(state.micActive ? 'listening' : 'idle');
  }

  function speak(text) { if (aceMode === 'chat' || liveConv || !state.voiceOut || !text) return; ttsQueue.push(text); if (!ttsPlaying) playNextTts(); }
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

  /* ============================================================ COMMAND PANEL
     Ace's OWN task/pipeline surface — replaces Google Tasks. A summonable full-screen
     overlay that collapses to a clean orb; reads/writes the data bank via /daybank. */
  // TACTICAL PALETTE (2026-08-01, Brady: pastels looked "girly") — command-console colors:
  // money green, steel cyan, gunmetal, burnt orange, deep blue, indigo, signal red, amber.
  // LIFE PIVOT (2026-08-10): recovery columns lead in VIVID tactical colors; the old business
  // columns recede to MUTED slate — the board is visual-first on what matters now.
  var CMD_CATS = { 'Money':'#2fd36b', 'Bills':'#ff8c1a', 'Opportunities':'#38bdf8', 'Goals':'#ffb020', 'Personal':'#ff4757', 'Deals':'#8091a8', 'Agents':'#748097', 'Admin':'#8b95a3', 'Networking':'#6f7d95', 'Business':'#79869c', 'Tech':'#828da0' };
  var CMD_ORDER = ['Money','Bills','Opportunities','Goals','Personal','Deals','Agents','Admin','Networking','Business','Tech'];
  var cmd = { open:false, min:false, lens:'pipeline', cat:'All', items:[], editing:null };
  function cmdEsc(s){ return (s||'').replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function cmdCatOf(it){ var t=it.tags||[]; for (var i=0;i<t.length;i++){ if (CMD_CATS[t[i]]) return t[i]; } return 'Admin'; }
  function cmdFetch(){
    return fetch(API+'/daybank?all=true',{headers:headers()})
      .then(function(r){ if(r.status===401){toLogin();throw 0;} return r.json(); })
      .then(function(d){ cmd.items=(d.items||[]); });
  }
  function cmdOpen(){
    cmd.open=true; cmd.min=false;
    var v=document.getElementById('command-view');
    if(!v){ v=document.createElement('div'); v.id='command-view'; $('app').appendChild(v); }
    v.style.display='flex'; v.innerHTML='<div class="cmd-empty">loading…</div>';
    cmdFetch().then(cmdRender).catch(function(){});
    // LIVE BOARD (2026-07-31): an open Command view used to be a stale snapshot — Ace could
    // complete items by voice/chat and Brady would watch them stay open. Refresh on a slow
    // pulse (skipping while he's mid-edit) so the board is always telling the truth.
    if (cmd.timer) clearInterval(cmd.timer);
    cmd.timer = setInterval(function(){
      if (cmd.open && !cmd.min && !cmd.editing) cmdSync();
    }, 25000);
  }
  function cmdClose(){ cmd.open=false; if(cmd.timer){ clearInterval(cmd.timer); cmd.timer=null; } var v=document.getElementById('command-view'); if(v) v.style.display='none'; }
  function cmdSync(){   // refetch + rerender an open board; safe no-op otherwise
    if (!cmd.open) return;
    cmdFetch().then(function(){ if (cmd.open && !cmd.editing) cmdRender(); }).catch(function(){});
  }
  function cmdRow(it){
    var c=cmdCatOf(it), col=CMD_CATS[c], done=it.status==='done';
    if (cmd.editing === it.id) {
      var dText = (cmd.draft && cmd.draft.text != null) ? cmd.draft.text : it.text;
      var dCat  = (cmd.draft && cmd.draft.cat) || c;
      var dDue  = (cmd.draft && cmd.draft.due != null) ? cmd.draft.due : (it.due || '');
      var opts = CMD_ORDER.map(function (o) {
        return '<option value="'+o+'"'+(o===dCat?' selected':'')+'>'+o+'</option>';
      }).join('');
      return '<div class="cmd-row cmd-editing" data-id="'+it.id+'" style="border-left-color:'+col+'">'
        +'<div class="cmd-eform">'
        +'<textarea class="cmd-etext" rows="2">'+cmdEsc(dText)+'</textarea>'
        +'<div class="cmd-erow"><select class="cmd-ecat">'+opts+'</select>'
        +'<input class="cmd-edue" placeholder="due (e.g. Fri 3pm)" value="'+cmdEsc(dDue)+'"></div>'
        +'<div class="cmd-erow"><button class="cmd-esave">SAVE</button><button class="cmd-ecancel">CANCEL</button>'
        +'<button class="cmd-ecancel cmd-edrop" style="margin-left:auto;color:#ff8080;border-color:#ff808055" title="Archive this item (kept in history, never deleted)">REMOVE</button></div>'
        +'</div></div>';
    }
    // A BILL PAID THIS MONTH is not a finished task — it stays on the shelf marked PAID so the
    // register is always complete, and rolls over on the 1st (2026-08-26).
    var paid = !!it.paid_this_period;
    return '<div class="cmd-row '+(paid?'paid':(done?'done':''))+'" data-id="'+it.id+'" style="border-left-color:'+col+'">'
      +'<div class="cmd-box"></div>'
      +'<div class="cmd-b"><div class="cmd-t">'+cmdEsc(it.text)+'</div><div class="cmd-m">'
      +'<span class="cmd-tag" style="color:'+col+';border-color:'+col+'55;background:'+col+'14">'
      +'<span class="cmd-d" style="background:'+col+'"></span>'+c+'</span>'
      +(paid?'<span class="cmd-paid">PAID THIS MONTH</span>':'')
      +(it.due?'<span class="cmd-due">'+cmdEsc(it.due)+'</span>':'')+'</div></div>'
      +'<button class="cmd-pencil" title="Edit">✎</button></div>';
  }
  function cmdRender(resetScroll){
    var v=document.getElementById('command-view'); if(!v) return;
    // KEEP YOUR PLACE (2026-08-01, Brady: "it jumps back to the top"): every re-render
    // rebuilds the list, which zeroed the scroll on each checkbox tick. Capture and restore
    // scrollTop; only a deliberate lens/category switch resets to the top.
    var _ol=v.querySelector('.cmd-list'); var _keep=(!resetScroll && _ol) ? _ol.scrollTop : 0;
    // Preserve unsaved editor input across ANY re-render (checkbox ticks, lens/chip taps):
    // snapshot the open editor's values so the rebuilt form rehydrates them.
    if (cmd.editing) {
      var er = v.querySelector('.cmd-row.cmd-editing');
      if (er) cmd.draft = {
        text: er.querySelector('.cmd-etext').value,
        cat:  er.querySelector('.cmd-ecat').value,
        due:  er.querySelector('.cmd-edue').value
      };
    }
    if(cmd.min){
      var nx=cmd.items.filter(function(x){return x.status!=='done';})[0];
      v.innerHTML='<div class="cmd-clean"><div class="cmd-orb-big" id="cmd-orb-big"></div>'
        +'<div class="cmd-cap">tap the orb to open <b>Command</b></div>'
        +(nx?'<div class="cmd-up">Next: <span>'+cmdEsc(nx.text)+'</span></div>':'')+'</div>';
      var o=v.querySelector('#cmd-orb-big'); if(o) o.onclick=function(){ cmd.min=false; cmdRender(); };
      return;
    }
    var chips=['All'].concat(CMD_ORDER).map(function(c){
      var dot=c==='All'?'':'<span class="cmd-d" style="background:'+CMD_CATS[c]+'"></span>';
      return '<button class="cmd-chip '+(c===cmd.cat?'on':'')+'" data-cat="'+c+'">'+dot+c+'</button>';
    }).join('');
    var LN={due:'Due',pipeline:'Pipeline',all:'All',done:'Done'};
    var lenses=['due','pipeline','all','done'].map(function(l){ return '<button class="'+(l===cmd.lens?'on':'')+'" data-lens="'+l+'">'+LN[l]+'</button>'; }).join('');
    var items=cmd.items.filter(function(x){ return cmd.cat==='All'||cmdCatOf(x)===cmd.cat; });
    // COMPLETION TRUTH (2026-07-31): show the counts, show done struck-through in All, sort
    // groups oldest-first — so 'marked off' looks different from 'never existed', and fresh
    // captures stop shoving to the top like a pile.
    var nOpen=cmd.items.filter(function(x){return x.status==='open';}).length;
    var nDone=cmd.items.filter(function(x){return x.status==='done';}).length;
    function byAge(a,b){ return (a.ts||'')<(b.ts||'')?-1:1; }
    var body='';
    if(cmd.lens==='due'){
      // DUE lens (2026-08-13): what's due, no sifting. Open items with a computed due date,
      // grouped Overdue / Today / Tomorrow / This week (next 7d). due_days comes from the server.
      var due=items.filter(function(x){ return x.status==='open' && x.due_days!=null && x.due_days<=7; })
                   .sort(function(a,b){ return a.due_days-b.due_days; });
      var buckets=[['⚠ OVERDUE',function(d){return d<0;}],['TODAY',function(d){return d===0;}],['TOMORROW',function(d){return d===1;}],['THIS WEEK',function(d){return d>=2&&d<=7;}]];
      buckets.forEach(function(bk){ var g=due.filter(function(x){return bk[1](x.due_days);}); if(g.length){ body+='<div class="cmd-grp">'+bk[0]+' · '+g.length+'</div>'+g.map(cmdRow).join(''); } });
      if(!body) body='<div class="cmd-empty">nothing due in the next 7 days ✓</div>';
    }
    else if(cmd.lens==='done'){ body=items.filter(function(x){return x.status!=='open';}).map(cmdRow).join(''); }
    else if(cmd.lens==='all'){ body=items.slice().sort(byAge).filter(function(x){return x.status!=='dropped';}).map(cmdRow).join(''); }
    else { CMD_ORDER.forEach(function(c){ var g=items.filter(function(x){return cmdCatOf(x)===c && x.status==='open';}).sort(byAge); if(g.length){ body+='<div class="cmd-grp"><span class="cmd-sq" style="background:'+CMD_CATS[c]+'"></span>'+c+' · '+g.length+'</div>'+g.map(cmdRow).join(''); } }); }
    if(!body) body='<div class="cmd-empty">— clear —</div>';
    var addCat=cmd.cat==='All'?'Money':cmd.cat;
    v.innerHTML='<div class="cmd-hd"><div class="cmd-orb"></div><div class="cmd-ttl">COMMAND</div>'
      +'<div class="cmd-n" style="margin-left:8px;font-size:11px;letter-spacing:.08em;opacity:.65">'+nOpen+' open · '+nDone+' done</div>'
      +'<button class="cmd-ic" id="cmd-min" title="Clean view">⌄</button><button class="cmd-ic" id="cmd-x" title="Close">✕</button></div>'
      +'<div class="cmd-lens">'+lenses+'</div><div class="cmd-chips">'+chips+'</div>'
      +'<div class="cmd-list">'+body+'</div>'
      +'<form class="cmd-add" id="cmd-add"><span class="cmd-plus">+</span><input id="cmd-input" placeholder="Add to '+addCat+'…" autocomplete="off"></form>';
    var _nl=v.querySelector('.cmd-list'); if(_nl&&_keep) _nl.scrollTop=_keep;
    v.querySelector('#cmd-x').onclick=cmdClose;
    v.querySelector('#cmd-min').onclick=function(){ cmd.min=true; cmdRender(); };
    Array.prototype.forEach.call(v.querySelectorAll('.cmd-lens button'), function(b){ b.onclick=function(){ cmd.lens=b.getAttribute('data-lens'); cmdRender(true); }; });
    Array.prototype.forEach.call(v.querySelectorAll('.cmd-chip'), function(b){ b.onclick=function(){ cmd.cat=b.getAttribute('data-cat'); cmdRender(true); }; });
    Array.prototype.forEach.call(v.querySelectorAll('.cmd-box'), function(b){ b.onclick=function(){
      var id=b.parentNode.getAttribute('data-id');
      var it=cmd.items.filter(function(x){return x.id===id;})[0]; if(!it) return;
      // VERIFIED tick (2026-07-31): optimistic flip, but if the server doesn't confirm, the
      // row reverts — a completion can no longer be silently lost to a 401/network blip.
      var prev=it.status, prevTs=it.done_ts;
      var ns=it.status==='done'?'open':'done'; it.status=ns; if(ns==='done'){ it.done_ts=new Date().toISOString(); } cmdRender();
      fetch(API+'/daybank/update',{method:'POST',headers:headers(),body:JSON.stringify({id:id,status:ns})})
        .then(function(r){ if(r.status===401){ toLogin(); throw 0; } return r.json(); })
        .then(function(d){ if(!d||!d.ok) throw 0; })
        .catch(function(){ it.status=prev; it.done_ts=prevTs; cmdRender(); });
    }; });
    // FULL EDITING (Brady): ✎ opens the inline editor — rewrite text, move category, set due.
    Array.prototype.forEach.call(v.querySelectorAll('.cmd-pencil'), function(b){ b.onclick=function(){
      cmd.draft = null;   // fresh item → fresh editor
      cmd.editing = b.parentNode.getAttribute('data-id'); cmdRender();
    }; });
    Array.prototype.forEach.call(v.querySelectorAll('.cmd-ecancel'), function(b){ b.onclick=function(){
      if (b.classList.contains('cmd-edrop')) {
        // REMOVE = archive (status 'dropped') — Brady can finally prune Ace's twins himself.
        var row = b.closest('.cmd-row'); var id = row && row.getAttribute('data-id'); if(!id) return;
        b.textContent='REMOVING…'; b.disabled=true;
        fetch(API+'/daybank/update',{method:'POST',headers:headers(),body:JSON.stringify({id:id,status:'dropped'})})
          .then(function(r){ return r.json(); })
          .then(function(d){ if(!d||!d.ok) throw 0; cmd.editing=null; cmd.draft=null; return cmdFetch().then(cmdRender); })
          .catch(function(){ b.textContent='RETRY REMOVE'; b.disabled=false; });
        return;
      }
      cmd.editing = null; cmd.draft = null; cmdRender();
    }; });
    Array.prototype.forEach.call(v.querySelectorAll('.cmd-esave'), function(b){ b.onclick=function(){
      var row = b.closest('.cmd-row'); if (!row) return;
      var id = row.getAttribute('data-id');
      var ta = row.querySelector('.cmd-etext');
      var body = {
        id: id,
        text: (ta.value || '').trim(),
        category: row.querySelector('.cmd-ecat').value,
        due: (row.querySelector('.cmd-edue').value || '').trim()
      };
      if (!body.text) { ta.style.borderColor = '#ff6b6b'; ta.focus(); return; }   // blank = no-op server-side
      b.textContent = 'SAVING…'; b.disabled = true;
      fetch(API+'/daybank/update',{method:'POST',headers:headers(),body:JSON.stringify(body)})
        .then(function(r){ return r.json(); })
        .then(function(d){
          if (!d || !d.ok) throw 0;
          cmd.editing = null; cmd.draft = null;
          return cmdFetch().then(cmdRender);
        })
        .catch(function(){
          // keep the editor open with the typed values — never eat an edit silently
          b.textContent = 'RETRY SAVE'; b.disabled = false;
        });
    }; });
    var f=v.querySelector('#cmd-add');
    if(f) f.onsubmit=function(e){ e.preventDefault(); var inp=v.querySelector('#cmd-input'); var t=(inp.value||'').trim(); if(!t) return; inp.value='';
      fetch(API+'/daybank/add',{method:'POST',headers:headers(),body:JSON.stringify({text:t,category:addCat})})
        .then(function(){ return cmdFetch(); }).then(cmdRender).catch(function(){}); };
  }

  /* ============================================================ KNOWLEDGE GRAPH
     Brady's book of business as a live map: every person, deal and category Ace
     knows about, and the lines between them. A full-screen canvas overlay running a
     hand-rolled force layout — repulsion between every pair, springs along the edges,
     a gentle pull to centre — relaxed 1-2 times per frame. No library: the shell is
     zero-build and CSP-tight, so the physics lives here.
     Tap a node to focus it (it and its neighbours light, everything else dims) and
     read its connections; drag to pan, drag a node to move it, wheel/pinch to zoom. */
  var GR_COL = { deal: '#45FFA6', person: '#53E7FF', category: '#8F7BFF' };
  var gr = { open: false, nodes: [], edges: [], adj: {}, sel: null, hov: null,
             tx: 0, ty: 0, k: 1, k0: 1, alpha: 1, raf: 0, cv: null, ctx: null,
             W: 0, H: 0, dpr: 1, drag: null, pan: null, down: null, moved: 0,
             ptr: {}, pinch: 0, sprites: {}, autofit: true };
  function grNarrow() { return window.innerWidth < 760; }

  // One pre-rendered glow sprite per type, drawn scaled per node. Canvas shadowBlur
  // on 100+ nodes every frame is what kills these things; a cached sprite is free.
  function grSprite(col) {
    if (gr.sprites[col]) return gr.sprites[col];
    var s = document.createElement('canvas'); s.width = s.height = 128;
    var x = s.getContext('2d'), g = x.createRadialGradient(64, 64, 0, 64, 64, 64);
    g.addColorStop(0, '#ffffff'); g.addColorStop(.16, col);
    g.addColorStop(.40, col + '70'); g.addColorStop(1, col + '00');
    x.fillStyle = g; x.beginPath(); x.arc(64, 64, 64, 0, 6.283); x.fill();
    gr.sprites[col] = s; return s;
  }

  function grFit() {
    var cv = gr.cv; if (!cv) return;
    var r = cv.getBoundingClientRect();
    gr.W = Math.max(1, r.width); gr.H = Math.max(1, r.height);
    gr.dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = Math.round(gr.W * gr.dpr); cv.height = Math.round(gr.H * gr.dpr);
    gr.ctx = cv.getContext('2d');
  }

  // Seed on a golden-angle spiral so the first frame is already spread out (a random
  // scatter takes hundreds of iterations to untangle; this one takes a few dozen).
  function grSeed() {
    var n = gr.nodes.length || 1, R = Math.min(gr.W, gr.H) * .36, by = {};
    gr.adj = {};
    gr.nodes.forEach(function (nd, i) {
      var a = i * 2.399963, rr = R * Math.sqrt((i + .5) / n);
      nd.x = gr.W / 2 + Math.cos(a) * rr; nd.y = gr.H / 2 + Math.sin(a) * rr;
      nd.vx = 0; nd.vy = 0; gr.adj[nd.id] = []; by[nd.id] = nd;
    });
    gr.edges = gr.edges.filter(function (e) {
      var a = by[e.source], b = by[e.target];
      if (!a || !b) return false;                     // orphan edge — never reachable
      e.a = a; e.b = b;
      gr.adj[a.id].push({ n: b, kind: e.kind }); gr.adj[b.id].push({ n: a, kind: e.kind });
      return true;
    });
    gr.tx = 0; gr.ty = 0; gr.k = 1; gr.alpha = 1; gr.sel = null; gr.hov = null; gr.autofit = true;
  }

  // AUTO-FRAME — while the layout is still settling and Brady hasn't grabbed it yet, ease
  // the viewport toward the graph's bounding box. The map arrives perfectly composed instead
  // of half off-screen; the first touch hands control back for good.
  function grFrame() {
    var n = gr.nodes.length; if (!n) return;
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, i;
    for (i = 0; i < n; i++) {
      var nd = gr.nodes[i], r = (nd.size || 8) * 2.2;
      if (nd.x - r < x0) x0 = nd.x - r;
      if (nd.x + r > x1) x1 = nd.x + r;
      if (nd.y - r < y0) y0 = nd.y - r;
      if (nd.y + r > y1) y1 = nd.y + r;
    }
    var pad = grNarrow() ? 28 : 70;
    var k = Math.min((gr.W - pad * 2) / Math.max(1, x1 - x0), (gr.H - pad * 2) / Math.max(1, y1 - y0));
    k = Math.max(.3, Math.min(1.4, k));
    var tx = gr.W / 2 - (x0 + x1) / 2 * k, ty = gr.H / 2 - (y0 + y1) / 2 * k;
    gr.k += (k - gr.k) * .09; gr.tx += (tx - gr.tx) * .09; gr.ty += (ty - gr.ty) * .09;
  }

  function grStep() {
    var N = gr.nodes, n = N.length, E = gr.edges, i, j, a, b, dx, dy, d2, d, f;
    var cx = gr.W / 2, cy = gr.H / 2, iters = n > 90 ? 1 : 2, it;
    for (it = 0; it < iters; it++) {
      for (i = 0; i < n; i++) {                       // repulsion — every pair pushes apart
        a = N[i];
        for (j = i + 1; j < n; j++) {
          b = N[j];
          dx = b.x - a.x; dy = b.y - a.y; d2 = dx * dx + dy * dy;
          if (d2 > 300000) continue;                  // far field: skip the sqrt entirely
          if (d2 < 1) { dx = Math.random() - .5; dy = Math.random() - .5; d2 = 1; }
          d = Math.sqrt(d2); f = 4600 / d2; dx = dx / d * f; dy = dy / d * f;
          a.vx -= dx; a.vy -= dy; b.vx += dx; b.vy += dy;
        }
      }
      for (i = 0; i < E.length; i++) {                // springs — edges pull to rest length
        a = E[i].a; b = E[i].b;
        dx = b.x - a.x; dy = b.y - a.y; d = Math.sqrt(dx * dx + dy * dy) || 1;
        f = (d - 88) * .042; dx = dx / d * f; dy = dy / d * f;
        a.vx += dx; a.vy += dy; b.vx -= dx; b.vy -= dy;
      }
      for (i = 0; i < n; i++) {
        a = N[i];
        if (a === gr.drag) { a.vx = 0; a.vy = 0; continue; }   // your finger wins
        a.vx += (cx - a.x) * .0045; a.vy += (cy - a.y) * .0045;
        var mx = a.vx * gr.alpha, my = a.vy * gr.alpha, mm = Math.sqrt(mx * mx + my * my);
        if (mm > 20) { mx = mx / mm * 20; my = my / mm * 20; }  // hard step cap = no explosions
        a.x += mx; a.y += my; a.vx *= .78; a.vy *= .78;
      }
    }
    gr.alpha *= .993;
    if (gr.alpha < .02) gr.alpha = .02;   // never fully freezes — the map keeps breathing
  }

  function grDraw() {
    var c = gr.ctx; if (!c) return;
    c.setTransform(gr.dpr, 0, 0, gr.dpr, 0, 0);
    c.clearRect(0, 0, gr.W, gr.H);
    c.save(); c.translate(gr.tx, gr.ty); c.scale(gr.k, gr.k);
    var focus = gr.sel || gr.hov, lit = null, i;
    if (focus) {
      lit = {}; lit[focus.id] = 1;
      (gr.adj[focus.id] || []).forEach(function (l) { lit[l.n.id] = 1; });
    }
    for (i = 0; i < gr.edges.length; i++) {
      var e = gr.edges[i], on = focus && (e.a === focus || e.b === focus);
      if (on) { c.strokeStyle = GR_COL[focus.type] + 'cc'; c.lineWidth = 1.7 / gr.k; }
      else if (focus) { c.strokeStyle = 'rgba(120,190,175,.055)'; c.lineWidth = .8 / gr.k; }
      else { c.strokeStyle = 'rgba(120,215,190,.15)'; c.lineWidth = .9 / gr.k; }
      c.beginPath(); c.moveTo(e.a.x, e.a.y); c.lineTo(e.b.x, e.b.y); c.stroke();
    }
    var fs = (grNarrow() ? 9.5 : 11.5) / gr.k;   // ÷k so labels hold one readable screen size
    var labelAll = gr.nodes.length <= 70;        // a small book fits every name; a big one doesn't
    c.textAlign = 'center'; c.textBaseline = 'top';
    for (i = 0; i < gr.nodes.length; i++) {
      var nd = gr.nodes[i], col = GR_COL[nd.type] || GR_COL.person;
      var dim = !!(lit && !lit[nd.id]), r = nd.size || 8, R = r * 2.7;
      c.globalAlpha = dim ? .2 : 1;
      c.drawImage(grSprite(col), nd.x - R, nd.y - R, R * 2, R * 2);
      c.beginPath(); c.arc(nd.x, nd.y, r * .4, 0, 6.283);
      c.fillStyle = '#f2fffa'; c.fill();
      if (nd === focus) {
        c.strokeStyle = '#eafff5'; c.lineWidth = 1.5 / gr.k;
        c.beginPath(); c.arc(nd.x, nd.y, r * 1.7, 0, 6.283); c.stroke();
      }
      // Label budget: always for the hubs and the focused set, everything else once
      // you've zoomed in far enough to have room for it.
      if (!(labelAll || r >= 10 || gr.k > .85 || (lit && lit[nd.id]))) continue;
      c.globalAlpha = dim ? .16 : (nd === focus ? 1 : .84);
      c.font = (nd === focus ? '700 ' : '') + fs.toFixed(1) + 'px ' +
        'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      c.fillStyle = nd === focus ? '#ffffff' : '#dff6ec';
      c.fillText(nd.label, nd.x, nd.y + r + 4 / gr.k);
    }
    c.globalAlpha = 1; c.restore();
  }

  function grLoop() {
    if (!gr.open) { gr.raf = 0; return; }
    grStep();
    if (gr.autofit && !gr.sel && gr.alpha > .06) grFrame();
    grDraw();
    gr.raf = requestAnimationFrame(grLoop);
  }

  function grAt(px, py) {
    var x = (px - gr.tx) / gr.k, y = (py - gr.ty) / gr.k, best = null, bd = 1e9;
    for (var i = 0; i < gr.nodes.length; i++) {
      var nd = gr.nodes[i], dx = nd.x - x, dy = nd.y - y, d = dx * dx + dy * dy;
      var rr = Math.max((nd.size || 8) * 1.7, 15 / gr.k);
      if (d < rr * rr && d < bd) { bd = d; best = nd; }
    }
    return best;
  }
  function grZoom(k, px, py) {
    k = Math.max(.25, Math.min(3.4, k));
    var wx = (px - gr.tx) / gr.k, wy = (py - gr.ty) / gr.k;
    gr.k = k; gr.tx = px - wx * k; gr.ty = py - wy * k;
  }
  function grCenter(nd) {
    gr.tx = gr.W / 2 - nd.x * gr.k; gr.ty = gr.H * .42 - nd.y * gr.k;
  }

  function grSelect(nd, center) {
    gr.sel = nd;
    if (nd && center) grCenter(nd);
    var d = document.getElementById('gr-detail'); if (!d) return;
    if (!nd) { d.className = 'gr-detail'; d.innerHTML = ''; return; }
    var links = (gr.adj[nd.id] || []).slice().sort(function (a, b) { return (b.n.size || 0) - (a.n.size || 0); });
    var col = GR_COL[nd.type] || GR_COL.person;
    d.className = 'gr-detail on';
    d.innerHTML = '<div class="gr-dh"><span class="gr-dot" style="background:' + col +
      ';box-shadow:0 0 12px ' + col + '"></span><div class="gr-dt">' + cmdEsc(nd.label) +
      '<span>' + nd.type + ' · ' + links.length + ' connection' + (links.length === 1 ? '' : 's') +
      '</span></div><button class="gr-dx" id="gr-dx" title="Close">✕</button></div>' +
      (links.length
        ? '<div class="gr-dl">' + links.map(function (l) {
            return '<button class="gr-li" data-id="' + l.n.id + '"><span class="gr-dot sm" style="background:' +
              (GR_COL[l.n.type] || GR_COL.person) + '"></span><span class="gr-ln">' + cmdEsc(l.n.label) +
              '</span><span class="gr-lk">' + cmdEsc((l.kind || '').replace(/_/g, ' ')) + '</span></button>';
          }).join('') + '</div>'
        : '<div class="gr-none">nothing linked to this yet</div>');
    d.querySelector('#gr-dx').onclick = function () { grSelect(null); };
    Array.prototype.forEach.call(d.querySelectorAll('.gr-li'), function (b) {
      b.onclick = function () {
        var id = b.getAttribute('data-id');
        var t = gr.nodes.filter(function (x) { return x.id === id; })[0];
        if (t) { gr.alpha = Math.max(gr.alpha, .35); grSelect(t, true); }
      };
    });
  }

  function grBind(cv) {
    function local(ev) { var r = cv.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; }
    function span() {
      var ids = Object.keys(gr.ptr); if (ids.length < 2) return 0;
      var a = gr.ptr[ids[0]], b = gr.ptr[ids[1]];
      return Math.sqrt((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y)) || 1;
    }
    cv.addEventListener('pointerdown', function (ev) {
      try { cv.setPointerCapture(ev.pointerId); } catch (e) {}
      gr.ptr[ev.pointerId] = { x: ev.clientX, y: ev.clientY };
      if (Object.keys(gr.ptr).length === 2) {          // second finger → pinch, cancel any drag
        gr.pinch = span(); gr.k0 = gr.k; gr.drag = null; gr.pan = null; gr.down = null;
        gr.moved = 999; gr.autofit = false;            // ...and don't let the release read as a tap
        return;
      }
      var p = local(ev), hit = grAt(p[0], p[1]);
      gr.autofit = false;                              // his hands are on it now — stop re-framing
      gr.moved = 0; gr.down = hit; gr.sx = ev.clientX; gr.sy = ev.clientY;
      if (hit) { gr.drag = hit; gr.alpha = Math.max(gr.alpha, .45); }
      else gr.pan = { x: ev.clientX, y: ev.clientY, tx: gr.tx, ty: gr.ty };
    });
    cv.addEventListener('pointermove', function (ev) {
      if (gr.ptr[ev.pointerId]) { gr.ptr[ev.pointerId].x = ev.clientX; gr.ptr[ev.pointerId].y = ev.clientY; }
      if (gr.pinch && Object.keys(gr.ptr).length >= 2) {
        var ids = Object.keys(gr.ptr), a = gr.ptr[ids[0]], b = gr.ptr[ids[1]];
        var r = cv.getBoundingClientRect();
        grZoom(gr.k0 * (span() / gr.pinch), (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
        return;
      }
      if (gr.down || gr.pan) gr.moved = Math.abs(ev.clientX - gr.sx) + Math.abs(ev.clientY - gr.sy);
      var p = local(ev);
      if (gr.drag) { gr.drag.x = (p[0] - gr.tx) / gr.k; gr.drag.y = (p[1] - gr.ty) / gr.k; return; }
      if (gr.pan) { gr.tx = gr.pan.tx + (ev.clientX - gr.pan.x); gr.ty = gr.pan.ty + (ev.clientY - gr.pan.y); return; }
      if (ev.pointerType === 'mouse') {
        var h = grAt(p[0], p[1]);
        if (h !== gr.hov) { gr.hov = h; cv.style.cursor = h ? 'pointer' : 'grab'; }
      }
    });
    function up(ev) {
      delete gr.ptr[ev.pointerId];
      if (Object.keys(gr.ptr).length < 2) gr.pinch = 0;
      if (gr.moved < 8) grSelect(gr.down || null);     // a tap, not a drag → focus (or clear)
      gr.drag = null; gr.pan = null; gr.down = null;
    }
    cv.addEventListener('pointerup', up);
    cv.addEventListener('pointercancel', up);
    cv.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      gr.autofit = false;
      var p = local(ev);
      grZoom(gr.k * Math.pow(.999, ev.deltaY * (ev.ctrlKey ? 5 : 1.7)), p[0], p[1]);
    }, { passive: false });
  }

  function graphClose() {
    gr.open = false;
    if (gr.raf) { cancelAnimationFrame(gr.raf); gr.raf = 0; }
    var v = document.getElementById('graph-view'); if (v) v.style.display = 'none';
  }
  function grLoad(refresh) {
    var meta = document.getElementById('gr-meta'), load = document.getElementById('gr-load');
    var rf = document.getElementById('gr-refresh');
    // A cold build is a full Opus read of every fact + the whole board — tens of seconds.
    // Say so, and lock ⟳ so an impatient second tap can't start a second extraction.
    if (rf) { rf.disabled = true; rf.style.opacity = '.45'; }
    if (load) {
      load.style.display = 'block';
      load.textContent = refresh ? '◉ re-reading the whole book — this takes a moment…' : '◉ mapping…';
    }
    if (meta) meta.textContent = 'reading facts + board…';
    function unlock() { if (rf) { rf.disabled = false; rf.style.opacity = ''; } }
    fetch(API + '/graph' + (refresh ? '?refresh=1' : ''), { headers: headers() })
      .then(function (r) { if (r.status === 401) { toLogin(); throw 0; } return r.json(); })
      .then(function (d) {
        unlock();
        gr.nodes = (d.nodes || []).map(function (n) { return { id: n.id, label: n.label, type: n.type, size: n.size || 8 }; });
        gr.edges = (d.edges || []).slice();
        if (load) load.style.display = gr.nodes.length ? 'none' : 'block';
        if (!gr.nodes.length) {
          if (load) load.textContent = d.error ? 'couldn’t read the map — try ⟳' : 'nothing to map yet';
          if (meta) meta.textContent = '';
          return;
        }
        grFit(); grSeed(); grSelect(null);
        if (meta) meta.textContent = gr.nodes.length + ' nodes · ' + gr.edges.length + ' links' +
          (d.cached ? ' · cached' : ' · fresh');
        if (!gr.raf) gr.raf = requestAnimationFrame(grLoop);
      })
      .catch(function () {
        unlock();
        if (load) { load.style.display = 'block'; load.textContent = 'the map didn’t load — try ⟳'; }
      });
  }
  function graphOpen() {
    gr.open = true;
    var v = document.getElementById('graph-view');
    if (!v) { v = document.createElement('div'); v.id = 'graph-view'; $('app').appendChild(v); }
    v.style.display = 'block';
    v.innerHTML = '<canvas id="graph-canvas"></canvas>' +
      '<div class="gr-hd"><span class="gr-orb"></span><div class="gr-ttl">KNOWLEDGE GRAPH</div>' +
      '<div class="gr-meta" id="gr-meta"></div>' +
      '<button class="gr-ic" id="gr-refresh" title="Rebuild from the latest facts">⟳</button>' +
      '<button class="gr-ic" id="gr-x" title="Close">✕</button></div>' +
      '<div class="gr-legend"><span><i style="background:' + GR_COL.person + '"></i>People</span>' +
      '<span><i style="background:' + GR_COL.deal + '"></i>Deals</span>' +
      '<span><i style="background:' + GR_COL.category + '"></i>Categories</span></div>' +
      '<div class="gr-detail" id="gr-detail"></div>' +
      '<div class="gr-load" id="gr-load">◉ mapping…</div>';
    gr.cv = v.querySelector('#graph-canvas');
    grFit(); grBind(gr.cv);
    v.querySelector('#gr-x').onclick = graphClose;
    v.querySelector('#gr-refresh').onclick = function () { grLoad(true); };
    grLoad(false);
  }
  window.addEventListener('resize', function () {
    if (!gr.open || !gr.cv) return;
    grFit();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && gr.open) graphClose();
  });

  /* ============================================================ WIRING */
  // Chat panel stays hidden until Brady toggles it — sends never auto-open it. Every reply
  // still glows the toggle (markUnread) so he knows one landed, and Ace speaks it; cards render
  // on the stage regardless of the panel. Toggle it open only when he wants to read the thread.
  $('send-btn').addEventListener('click', function () { sendMessage(); });
  // The input grows with your text (Brady: long messages ran off to the right in the
  // one-line box). Enter sends; Shift+Enter makes a new line.
  function growInput() {
    var el = $('chat-input');
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 132) + 'px';
  }
  $('chat-input').addEventListener('input', growInput);
  $('chat-input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  Array.prototype.forEach.call(document.querySelectorAll('.qa[data-msg]'), function (btn) {
    btn.addEventListener('click', function () { sendMessage(btn.getAttribute('data-msg')); });
  });
  // "Plan my week" — the marquee ritual. Force CHAT (his DEEP brain = Opus, and silent so he
  // reads Ace's questions instead of hearing them), OPEN the conversation tab so the back-and-forth
  // is visible, then fire the trigger phrase that lights up rule 14 (ask clarifying questions first,
  // then build the day-by-day plan). Not a data-msg button on purpose — it needs this extra setup.
  $('plan-week-btn').addEventListener('click', function () {
    setMode('chat');            // kills voice/TTS, opens the conversation (no-op if already chat)
    setChat(true);              // ...ensure it's open even if he was already in chat mode
    sendMessage('Plan my week');
  });
  // "Command" — summon Ace's own task/pipeline board (his store, not Google Tasks).
  $('command-btn').addEventListener('click', cmdOpen);
  // "Pulse" — the on-demand 'how's my business looking?' deep read.
  function runPulse() {
    var btn = $('pulse-btn'); if (btn) { btn.innerHTML = '<span class="qg">▦</span>Reading…'; btn.disabled = true; }
    fetch(API + '/business/report', { method: 'POST', headers: headers() })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.text) { setChat(true); addAceMessage(d.text); }
      })
      .catch(function () {})
      .then(function () { if (btn) { btn.innerHTML = '<span class="qg">▦</span>Pulse'; btn.disabled = false; } });
  }
  $('pulse-btn').addEventListener('click', runPulse);

  /* ============================================================ CAPTURE ANYTHING */
  // Snap a whiteboard, drop a statement, hum a voice memo — Ace reads/hears it, files the
  // facts to memory and the actions to the board, then tells you what he got. The 📎 button,
  // the iOS camera roll and desktop drag-and-drop all land on this one pipeline.
  var captureBusy = false;

  // Phone photos are 3-12MB; the vision ceiling is 5MB and Brady is usually on cellular.
  // Downscale in-browser to a 1568px long edge (the model's native tile) and re-encode JPEG.
  // Any decode failure (HEIC on Chrome, say) falls through with the untouched file so the
  // server can answer with a readable message instead of the UI silently eating it.
  function shrinkImage(file) {
    return new Promise(function (resolve) {
      // Only shrink images that are actually near the server's 5MB per-image cap. A normal
      // screenshot (well under that) goes through UNTOUCHED — the canvas step is exactly what
      // returned a BLANK image on iOS for big screenshots (Brady: "he said it was blank").
      if (!/^image\//.test(file.type || '') || file.size < 4500000) return resolve(file);
      var done = false, url = URL.createObjectURL(file), img = new Image();
      function finish(f) { if (done) return; done = true; try { URL.revokeObjectURL(url); } catch (e) {} resolve(f); }
      // Never let a slow/stuck decode hang the whole capture — fall back to the original file.
      setTimeout(function () { finish(file); }, 8000);
      img.onload = function () {
        try {
          var s = Math.min(1, 1568 / Math.max(img.width, img.height));
          var c = document.createElement('canvas');
          c.width = Math.round(img.width * s) || 1; c.height = Math.round(img.height * s) || 1;
          var ctx = c.getContext('2d');
          ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, c.width, c.height);   // no transparent→black
          ctx.drawImage(img, 0, 0, c.width, c.height);
          c.toBlob(function (b) {
            // If the canvas blanked out (iOS) the blob is tiny — fall back to the original file.
            finish(b && b.size > 3000 ? new File([b], 'capture.jpg', { type: 'image/jpeg' }) : file);
          }, 'image/jpeg', 0.86);
        } catch (e) { finish(file); }
      };
      img.onerror = function () { finish(file); };
      img.src = url;
    });
  }

  // Rewrite a live bubble in place ("Reading…" → the result) so a capture reads as one beat.
  function replaceBubble(el, text) {
    if (!el || !el.parentNode) { addAceMessage(text); return; }
    el.innerHTML = '<div class="sender">ACE</div>';
    el.appendChild(document.createTextNode(text));
    var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel();
    el.appendChild(ts); scrollBottom(); markUnread();
  }

  // Read ONE file → /capture. Returns a promise so a batch can sequence them.
  function captureOne(file) {
    if (!file) return Promise.resolve();
    setChat(true);
    if (file.size > 18 * 1024 * 1024) {
      addAceMessage('⚠️ "' + (file.name || 'that') + '" is over 18MB — too big to read. Send a photo or a shorter clip.');
      return Promise.resolve();
    }
    var bubble = addAceMessage('📎 Reading ' + (file.name || 'that') + '…');
    return shrinkImage(file).then(function (f) {
      var fd = new FormData();
      fd.append('file', f, f.name || 'capture');
      // NEVER set Content-Type by hand here — the browser owns the multipart boundary.
      var h = {}; if (state.token) h.Authorization = 'Bearer ' + state.token;
      return fetch(API + '/capture', { method: 'POST', headers: h, body: fd });
    }).then(function (r) {
      if (r.status === 401) { toLogin(); throw 0; }
      return r.json();
    }).then(function (d) {
      if (d && d.ok) {
        replaceBubble(bubble, d.summary);
        speak(d.summary);
        if (d.todos_count && cmd && cmd.open) cmdFetch().then(cmdRender).catch(function () {});
      } else {
        replaceBubble(bubble, '⚠️ ' + ((d && d.error) || "Couldn't read that one."));
      }
    }).catch(function () {
      replaceBubble(bubble, '⚠️ Capture failed — the link dropped. Try that again.');
    });
  }

  // Send SEVERAL at once (Brady: personal + super base + this month + rolling 12). Read them
  // ONE AFTER ANOTHER — each screenshot is its own read + filing, and sequencing keeps the
  // link and the model from being hammered by 4 parallel uploads.
  function captureFiles(list) {
    if (!list || !list.length || captureBusy) return;
    var files = Array.prototype.slice.call(list);
    captureBusy = true;
    if (files.length > 1) addAceMessage('📎 Reading ' + files.length + ' files, one at a time…');
    (function next(i) {
      if (i >= files.length) { captureBusy = false; return; }
      captureOne(files[i]).then(function () { next(i + 1); });
    })(0);
  }

  $('clip-btn').addEventListener('click', function () { $('capture-file').click(); });
  $('capture-file').addEventListener('change', function (e) {
    // Copy the FileList out before touching .value (iOS can clear it out from under us).
    var files = e.target.files ? Array.prototype.slice.call(e.target.files) : [];
    e.target.value = '';        // so the SAME file picked twice still fires change
    if (!files.length) return;
    captureBusy = false;        // a fresh pick ALWAYS wins — never let a stuck flag swallow it silently
    setChat(true);              // guarantee he SEES the read, even if he tapped 📎 from the orb dashboard
    captureFiles(files);
  });

  // DRAG-AND-DROP anywhere on the window (desktop). dragenter/dragleave fire per element, so
  // the depth counter keeps the highlight steady when the cursor crosses a child node.
  var dragDepth = 0;
  function hasFiles(e) {
    return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0);
  }
  function dropGlow(on) { document.body.classList.toggle('dropping', !!on); }
  window.addEventListener('dragenter', function (e) {
    if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; dropGlow(true);
  });
  window.addEventListener('dragover', function (e) {
    if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy';
  });
  window.addEventListener('dragleave', function () {
    dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) dropGlow(false);
  });
  window.addEventListener('drop', function (e) {
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault(); dragDepth = 0; dropGlow(false);
    captureFiles(e.dataTransfer.files);
  });
  // "Graph" — the knowledge graph. Its own handler, not the generic DOCK map: this one
  // isn't a card, it's a full-screen canvas with its own physics loop.
  $('graph-btn').addEventListener('click', function () { gr.open ? graphClose() : graphOpen(); });

  /* ⌘K COMMAND PALETTE — one keystroke to run anything or jump anywhere. */
  var PALETTE = [
    { label: '◧ Command — task board', run: cmdOpen },
    { label: '◉ Knowledge Graph — the map of your book of business', run: graphOpen },
    { label: '📊 Business Pulse — how\'s my business?', run: runPulse },
    { label: '◈ Plan my week', run: function () { setMode('chat'); setChat(true); sendMessage('Plan my week'); } },
    { label: '☀ Brief me now', run: function () { setChat(true); sendMessage('Give me my morning brief.'); } },
    { label: '🧠 What do you want to become? (self-notes)', run: function () { setChat(true); sendMessage('What is on your self-improvement list? Read me your ACE SELF-NOTES.'); } },
    { label: 'Schedule window', panel: 'timeline' },
    { label: 'Inbox window', panel: 'inbox' },
    { label: 'Weather window', panel: 'weather' },
    { label: 'Memory window', panel: 'memory' }
  ];
  var palEl = null, palSel = 0;
  function paletteClose() { if (palEl) { palEl.remove(); palEl = null; } }
  function paletteOpen() {
    paletteClose(); palSel = 0;
    palEl = document.createElement('div'); palEl.id = 'palette';
    palEl.innerHTML = '<div class="pal-box"><input id="pal-in" placeholder="Type a command…" autocomplete="off"><div id="pal-list"></div></div>';
    document.body.appendChild(palEl);
    palEl.addEventListener('click', function (e) { if (e.target === palEl) paletteClose(); });
    var inp = palEl.querySelector('#pal-in');
    function render() {
      var q = (inp.value || '').toLowerCase();
      var hits = PALETTE.filter(function (p) { return p.label.toLowerCase().indexOf(q) !== -1; }).slice(0, 9);
      var list = palEl.querySelector('#pal-list');
      if (palSel >= hits.length) palSel = Math.max(0, hits.length - 1);
      list.innerHTML = hits.map(function (p, i) {
        return '<div class="pal-item' + (i === palSel ? ' on' : '') + '" data-i="' + i + '">' + p.label + '</div>';
      }).join('') || '<div class="pal-item">no match</div>';
      Array.prototype.forEach.call(list.children, function (el) {
        el.onclick = function () { var h = hits[+el.getAttribute('data-i')]; if (h) { paletteClose(); exec(h); } };
      });
      return hits;
    }
    function exec(h) {
      if (h.run) { h.run(); return; }
      if (h.panel) { var b = document.querySelector('.qa[data-panel="' + h.panel + '"]'); if (b) b.click(); }
    }
    inp.addEventListener('input', function () { palSel = 0; render(); });
    inp.addEventListener('keydown', function (e) {
      var hits = render();
      if (e.key === 'Escape') { paletteClose(); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); palSel = Math.min(palSel + 1, hits.length - 1); render(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); palSel = Math.max(palSel - 1, 0); render(); }
      else if (e.key === 'Enter') { e.preventDefault(); var h = hits[palSel]; if (h) { paletteClose(); exec(h); } }
    });
    render(); inp.focus();
  }
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); palEl ? paletteClose() : paletteOpen(); }
  });
  // THE DOCK — tap a surface to summon its window, tap again to dismiss (clean by default).
  // Same materializeCard machinery Ace uses, driven client-side so it's instant.
  var DOCK = {
    timeline: { title: 'TODAY',          url: '/calendar?days=1', shape: function (d) { return { events: d.events || [] }; } },
    inbox:    { title: 'INBOX', url: '/inbox?n=15&scope=all',     shape: function (d) { return { emails: d.emails || [], business: d.business || d.emails || [], personal: d.personal || [] }; } },
    weather:  { title: 'WEATHER',        url: '/weather',         shape: function (d) { return d; } },
    memory:   { title: 'MEMORY',         url: '/memory',          shape: function (d) { return { memories: d.memories || [] }; } }
  };
  Array.prototype.forEach.call(document.querySelectorAll('.qa[data-panel]'), function (btn) {
    btn.addEventListener('click', function () {
      var p = btn.getAttribute('data-panel'), cfg = DOCK[p]; if (!cfg) return;
      var open = document.querySelector('.card[data-panel="' + cfg.title + '"]');
      if (open) { open.remove(); return; }   // toggle off → back to the clean stage
      fetch(API + cfg.url, { headers: headers() })
        .then(function (r) { if (r.status === 401) { toLogin(); throw 0; } return r.ok ? r.json() : null; })
        .then(function (d) { if (d) materializeCard(p, cfg.shape(d)); })
        .catch(function () {});
    });
  });
  // PINNED WINDOWS — every pinned card re-opens with fresh data on boot ("hard-code
  // some to stay open", but it's your choice per card via the 📌).
  var REHYDRATE = {
    timeline: DOCK.timeline, inbox: DOCK.inbox, weather: DOCK.weather, memory: DOCK.memory,
    daybank:  { url: '/daybank',         shape: function (d) { return { items: d.items || [] }; } },
    calendar: { url: '/calendar?days=7', shape: function (d) { return { events: d.events || [] }; } },
    tasks:    { url: '/tasks',           shape: function (d) { return { tasks: d.tasks || [], lists: d.lists || null }; } }
  };
  function rehydratePins() {
    var p = pinsGet();
    Object.keys(p).forEach(function (panel) {
      if (!p[panel] || !p[panel].pinned) return;
      var cfg = REHYDRATE[panel]; if (!cfg) return;
      fetch(API + cfg.url, { headers: headers() })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d) materializeCard(panel, cfg.shape(d)); })
        .catch(function () {});
    });
  }
  window.__rehydratePins = rehydratePins;

  /* ============================================================ PHONE ALERTS
     Ace reaches the phone when the app is CLOSED — the gap the proactive briefs
     had (they only landed if a HUD was open). Strictly opt-in and strictly silent
     when anything is missing: no PushManager (older iOS), no VAPID key on the
     server, or a previous "not now" → the affordance simply never appears and not
     a single line here can throw into the boot path.
     iOS: web push exists ONLY for a PWA installed to the home screen (16.4+), and
     permission must be asked from a REAL TAP — hence a dock button, not a prompt
     on load (an auto-request would be denied and unrecoverable). */
  var PUSH_SEEN = 'ace2_push';   // 'on' | 'off' — clear it in localStorage to be asked again
  function pushSupported() {
    return ('serviceWorker' in navigator) && ('PushManager' in window)
      && (typeof Notification !== 'undefined') && (typeof window.atob === 'function');
  }
  function urlB64ToBytes(s) {   // VAPID keys travel base64url; subscribe() wants raw bytes
    var pad = ''; while ((s.length + pad.length) % 4) pad += '=';
    var raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function subscribePush(key) {   // resolves true when the server has the device
    return navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.getSubscription().then(function (existing) {
        // Reuse the existing subscription — re-subscribing with a different key throws.
        return existing || reg.pushManager.subscribe({
          userVisibleOnly: true, applicationServerKey: urlB64ToBytes(key) });
      });
    }).then(function (sub) {
      return fetch(API + '/push/subscribe', { method: 'POST', headers: headers(), body: JSON.stringify(sub) })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { return !!(d && d.ok); });
    });
  }
  function maybePushPrompt() {
    try {
      if (!pushSupported() || localStorage.getItem(PUSH_SEEN) === 'off') return;
      if (Notification.permission === 'denied') return;   // browser-level no — never nag
      fetch(API + '/push/key', { headers: headers() })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          var key = d && d.publicKey;
          if (!key) return;                               // VAPID unset on the server → dormant
          if (Notification.permission === 'granted') {    // already allowed: just re-register
            localStorage.setItem(PUSH_SEEN, 'on');        // (endpoints rotate) and stay quiet
            subscribePush(key).catch(function () {});
            return;
          }
          if (localStorage.getItem(PUSH_SEEN) === 'on') return;
          setTimeout(function () { showPushPrompt(key); }, 1500);   // let the stage settle first
        })
        .catch(function () {});
    } catch (e) {}
  }
  function showPushPrompt(key) {
    var dock = $('quick');
    if (!dock || $('push-enable')) return;
    var yes = document.createElement('button');
    yes.className = 'qa'; yes.id = 'push-enable'; yes.textContent = '🔔 Phone alerts';
    yes.title = 'Let Ace reach this phone when the app is closed — briefs and urgent nudges';
    var no = document.createElement('button');
    no.className = 'qa'; no.id = 'push-later'; no.textContent = 'Not now';
    no.title = 'Hide this';
    function clear() {
      [yes, no].forEach(function (b) { if (b.parentNode) b.parentNode.removeChild(b); });
    }
    function settle(on) { try { localStorage.setItem(PUSH_SEEN, on ? 'on' : 'off'); } catch (e) {} clear(); }
    no.onclick = function () { settle(false); };
    yes.onclick = function () {
      yes.disabled = true; yes.textContent = '🔔 …';
      var ask;
      try { ask = Notification.requestPermission(); } catch (e) { settle(false); return; }
      if (!ask || typeof ask.then !== 'function') { settle(false); return; }   // ancient callback-only API
      ask.then(function (p) {
        if (p !== 'granted') { settle(false); return; }
        subscribePush(key).then(function (ok) {
          settle(ok);
          if (ok) addAceMessage('Phone alerts are on. I\'ll reach you there even when this is closed.');
        }).catch(function () { settle(false); });
      }).catch(function () { settle(false); });
    };
    dock.appendChild(yes); dock.appendChild(no);
  }

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () { navigator.serviceWorker.register('/sw.js').catch(function () {}); });
  }

  // Debug/demo hook (also lets us drive the stage from the console).
  window.aceDebug = { card: materializeCard, open: openLink, event: handleWSEvent, orb: orb };

  boot();
})();
