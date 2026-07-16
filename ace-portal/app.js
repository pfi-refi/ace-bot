/* ============================================================
   ACE PORTAL — frontend controller
   Matrix rain · particle orb · streaming chat · voice · panels
   ============================================================ */
(function () {
  'use strict';

  var API = ''; // same-origin
  var state = {
    token: sessionStorage.getItem('ace_token') || '',
    authRequired: true,
    ws: null,
    wsReady: false,
    busy: false,
    micActive: false,
    recognition: null,
  };

  var $ = function (id) { return document.getElementById(id); };

  /* ============================================================
     MATRIX RAIN
     ============================================================ */
  (function matrixRain() {
    var canvas = $('matrix');
    var ctx = canvas.getContext('2d');
    var chars = 'アイウエオカキクケコサシスセソタチツテトナニヌネノ0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    var fontSize = 14, columns, drops;
    function resize() {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
      columns = Math.floor(canvas.width / fontSize);
      drops = [];
      for (var i = 0; i < columns; i++) drops[i] = Math.random() * -canvas.height / fontSize;
    }
    function draw() {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.06)';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#39FF14';
      ctx.font = fontSize + 'px monospace';
      for (var i = 0; i < drops.length; i++) {
        var text = chars.charAt(Math.floor(Math.random() * chars.length));
        ctx.fillText(text, i * fontSize, drops[i] * fontSize);
        if (drops[i] * fontSize > canvas.height && Math.random() > 0.975) drops[i] = 0;
        drops[i]++;
      }
    }
    resize();
    window.addEventListener('resize', resize);
    setInterval(draw, 55);
  })();

  /* ============================================================
     PARTICLE ORB (neural network swarm) — per spec config
     ============================================================ */
  var orb = (function () {
    var canvas = $('orb-canvas');
    var ctx = canvas.getContext('2d');
    var W = canvas.width, H = canvas.height;
    var cx = W / 2, cy = H / 2;

    var NUM_PARTICLES = 150;
    var MAX_DIST = 60;
    // Spring/jitter tuned so the swarm fills the orb as a neural network rather
    // than collapsing to a point (the spec's literal constants over-damp).
    var SPRING_K = 0.0035;
    var ORB_RADIUS = 110;
    var JITTER_IDLE = 2.0;
    var JITTER_ACTIVE = 3.0;

    var damping = 0.90;            // eased toward a target per state
    var dampingTarget = 0.90;
    var particles = [];
    for (var i = 0; i < NUM_PARTICLES; i++) {
      var a = Math.random() * Math.PI * 2;
      var r = Math.random() * ORB_RADIUS;
      particles.push({
        x: Math.cos(a) * r, y: Math.sin(a) * r,
        vx: (Math.random() - 0.5) * 2, vy: (Math.random() - 0.5) * 2,
        size: 1.2 + Math.random() * 1.8,
      });
    }

    var stateName = 'idle';
    var glowEl = $('orb-glow');

    function setState(s) {
      stateName = s;
      if (s === 'speaking') { dampingTarget = 0.93; glowEl.style.opacity = '1'; glowEl.style.transform = 'scale(1.1)'; }
      else if (s === 'listening') { dampingTarget = 0.92; glowEl.style.opacity = '0.85'; glowEl.style.transform = 'scale(1.05)'; }
      else { dampingTarget = 0.90; glowEl.style.opacity = '0.65'; glowEl.style.transform = 'scale(1)'; }
    }

    function frame() {
      damping += (dampingTarget - damping) * 0.05;
      ctx.clearRect(0, 0, W, H);
      var jitter = stateName === 'idle' ? JITTER_IDLE : JITTER_ACTIVE;

      for (var i = 0; i < particles.length; i++) {
        var p = particles[i];
        var fx = -SPRING_K * p.x, fy = -SPRING_K * p.y;
        p.vx += fx; p.vy += fy;
        p.vx *= damping; p.vy *= damping;
        p.vx += (Math.random() - 0.5) * jitter;
        p.vy += (Math.random() - 0.5) * jitter;
        var dist = Math.sqrt(p.x * p.x + p.y * p.y);
        if (dist > ORB_RADIUS) {
          var push = (dist - ORB_RADIUS) * 0.05;
          p.vx -= (p.x / dist) * push; p.vy -= (p.y / dist) * push;
        }
        p.x += p.vx; p.y += p.vy;
      }

      // connection lines
      ctx.lineWidth = 1;
      for (var a2 = 0; a2 < particles.length; a2++) {
        for (var b = a2 + 1; b < particles.length; b++) {
          var dx = particles[a2].x - particles[b].x;
          var dy = particles[a2].y - particles[b].y;
          var d = Math.sqrt(dx * dx + dy * dy);
          if (d < MAX_DIST) {
            ctx.strokeStyle = 'rgba(57,255,20,' + ((1 - d / MAX_DIST) * 0.35).toFixed(3) + ')';
            ctx.beginPath();
            ctx.moveTo(cx + particles[a2].x, cy + particles[a2].y);
            ctx.lineTo(cx + particles[b].x, cy + particles[b].y);
            ctx.stroke();
          }
        }
      }
      // dots
      ctx.fillStyle = 'rgba(57,255,20,0.8)';
      for (var k = 0; k < particles.length; k++) {
        ctx.beginPath();
        ctx.arc(cx + particles[k].x, cy + particles[k].y, particles[k].size, 0, Math.PI * 2);
        ctx.fill();
      }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
    return { setState: setState };
  })();

  function setOrbState(s) {
    orb.setState(s);
    var el = $('orb-state');
    el.classList.remove('listening', 'speaking');
    if (s === 'listening') { el.classList.add('listening'); el.textContent = '[ LISTENING ]'; }
    else if (s === 'speaking') { el.classList.add('speaking'); el.textContent = '[ SPEAKING ]'; }
    else { el.textContent = '[ IDLE ]'; }
  }

  /* ============================================================
     CLOCK (12-hour with AM/PM)
     ============================================================ */
  (function clock() {
    var months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
    function pad(n) { return n < 10 ? '0' + n : '' + n; }
    function tick() {
      var d = new Date();
      var h = d.getHours(), ampm = h >= 12 ? 'PM' : 'AM';
      var h12 = h % 12; if (h12 === 0) h12 = 12;
      $('clock').innerHTML = pad(h12) + ':' + pad(d.getMinutes()) +
        '<span class="sec">:' + pad(d.getSeconds()) + '</span>' +
        '<span class="ampm">' + ampm + '</span>';
      $('topdate').textContent = months[d.getMonth()] + ' ' + pad(d.getDate()) + ' ' + d.getFullYear();
    }
    tick();
    setInterval(tick, 1000);
  })();

  /* ============================================================
     AUTH
     ============================================================ */
  function headers() {
    var h = { 'Content-Type': 'application/json' };
    if (state.token) h['Authorization'] = 'Bearer ' + state.token;
    return h;
  }

  function boot() {
    fetch(API + '/health').then(function (r) { return r.json(); }).then(function (h) {
      state.authRequired = !!h.auth_required;
      $('bb-version').textContent = 'ACE ' + (h.version || 'v18.17') + ' // OPERATIONAL';
      if (!state.authRequired) { startApp(); return; }
      if (state.token) { startApp(); return; }   // trust stored token; API calls re-validate
      showLogin();
    }).catch(function () {
      // Backend unreachable — still show UI so the shell is visible.
      showLogin();
    });
  }

  function showLogin() {
    $('login').classList.remove('hidden');
    $('login-input').focus();
  }

  function doLogin() {
    var pw = $('login-input').value;
    $('login-err').textContent = '';
    fetch(API + '/auth', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw }),
    }).then(function (r) {
      if (!r.ok) throw new Error('bad');
      return r.json();
    }).then(function (data) {
      state.token = data.token || '';
      sessionStorage.setItem('ace_token', state.token);
      $('login').classList.add('hidden');
      startApp();
    }).catch(function () {
      $('login-err').textContent = 'ACCESS DENIED';
      $('login-input').value = '';
    });
  }

  $('login-btn').addEventListener('click', doLogin);
  $('login-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });

  /* ============================================================
     APP START
     ============================================================ */
  function startApp() {
    $('app').classList.remove('hidden');
    connectWS();
    refreshCalendar();
    refreshTasks();
    refreshWeather();
    greeting();
    setInterval(refreshCalendar, 5 * 60 * 1000);
    setInterval(refreshTasks, 5 * 60 * 1000);
    setInterval(refreshWeather, 10 * 60 * 1000);
  }

  function greeting() {
    var d = new Date();
    var days = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    var months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    addAceMessage('Command center online. ' + days[d.getDay()] + ', ' + months[d.getMonth()] + ' ' + d.getDate() +
      '. I have your calendar, tasks, mail, and memory live. What do you want to move first?');
  }

  /* ============================================================
     WEBSOCKET STREAMING CHAT
     ============================================================ */
  function wsURL() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var q = state.token ? ('?token=' + encodeURIComponent(state.token)) : '';
    return proto + '//' + location.host + '/ws/chat' + q;
  }

  function connectWS() {
    try { state.ws = new WebSocket(wsURL()); } catch (e) { setLink(false); return; }
    var ws = state.ws;
    ws.onopen = function () { state.wsReady = true; setLink(true); };
    ws.onclose = function () { state.wsReady = false; setLink(false); setTimeout(connectWS, 4000); };
    ws.onerror = function () { setLink(false); };
    ws.onmessage = function (ev) { handleWSEvent(JSON.parse(ev.data)); };
  }

  var streamMsg = null; // active streaming Ace bubble

  function handleWSEvent(msg) {
    switch (msg.type) {
      case 'start':
        removeTyping();
        setOrbState('speaking');
        streamMsg = beginAceStream();
        break;
      case 'delta':
        if (!streamMsg) streamMsg = beginAceStream();
        appendToStream(streamMsg, msg.text);
        break;
      case 'final':
        if (streamMsg) { finalizeStream(streamMsg, msg.text); }
        break;
      case 'confirmation':
        addConfirmation(msg.text);
        pushIntel(msg.text);
        break;
      case 'error':
        removeTyping();
        discardEmptyStream();
        addAceMessage(msg.text);
        break;
      case 'done':
        discardEmptyStream();
        streamMsg = null;
        state.busy = false;
        setOrbState(state.micActive ? 'listening' : 'idle');
        break;
    }
  }

  function sendMessage(text) {
    text = (text || $('chat-input').value).trim();
    if (!text || state.busy) return;
    state.busy = true;
    $('chat-input').value = '';
    addUserMessage(text);
    showTyping();
    setOrbState('listening');

    if (state.wsReady && state.ws) {
      state.ws.send(JSON.stringify({ message: text }));
    } else {
      // HTTP fallback
      fetch(API + '/chat', { method: 'POST', headers: headers(), body: JSON.stringify({ message: text }) })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          removeTyping();
          setOrbState('speaking');
          if (data.reply) addAceMessage(data.reply);
          (data.confirmations || []).forEach(function (c) { addConfirmation(c); pushIntel(c); });
          state.busy = false;
          setOrbState(state.micActive ? 'listening' : 'idle');
        })
        .catch(function () {
          removeTyping();
          addAceMessage('⚠️ Connection to Ace failed. Retrying link…');
          state.busy = false; setOrbState('idle');
        });
    }
  }

  /* ============================================================
     CHAT RENDERING
     ============================================================ */
  var messagesEl = $('messages');
  function scrollBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }
  function nowLabel() {
    var d = new Date(), h = d.getHours(), ampm = h >= 12 ? 'PM' : 'AM', h12 = h % 12 || 12;
    return (h12 < 10 ? '0' + h12 : h12) + ':' + (d.getMinutes() < 10 ? '0' : '') + d.getMinutes() + ' ' + ampm;
  }

  function addUserMessage(text) {
    var m = document.createElement('div');
    m.className = 'msg user';
    m.appendChild(document.createTextNode(text));
    var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel();
    m.appendChild(ts);
    messagesEl.appendChild(m); scrollBottom();
  }

  function addAceMessage(text) {
    var m = document.createElement('div');
    m.className = 'msg ace';
    m.innerHTML = '<div class="sender">ACE</div>';
    m.appendChild(document.createTextNode(text));
    var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel();
    m.appendChild(ts);
    messagesEl.appendChild(m); scrollBottom();
    return m;
  }

  function beginAceStream() {
    var m = document.createElement('div');
    m.className = 'msg ace';
    m.innerHTML = '<div class="sender">ACE</div>';
    var body = document.createElement('span'); body.className = 'stream-body';
    m.appendChild(body);
    var cursor = document.createElement('span'); cursor.className = 'cursor'; cursor.textContent = ' ';
    m.appendChild(cursor);
    messagesEl.appendChild(m); scrollBottom();
    return { el: m, body: body, cursor: cursor, text: '' };
  }
  function discardEmptyStream() {
    if (streamMsg && !streamMsg.text && streamMsg.el && streamMsg.el.parentNode) {
      streamMsg.el.parentNode.removeChild(streamMsg.el);
      streamMsg = null;
    }
  }
  function appendToStream(s, t) { s.text += t; s.body.textContent = s.text; scrollBottom(); }
  function finalizeStream(s, finalText) {
    s.body.textContent = finalText || s.text;
    if (s.cursor && s.cursor.parentNode) s.cursor.parentNode.removeChild(s.cursor);
    var ts = document.createElement('div'); ts.className = 'ts'; ts.textContent = nowLabel();
    s.el.appendChild(ts);
    speak(finalText || s.text);
    scrollBottom();
  }

  function addConfirmation(text) {
    var m = document.createElement('div');
    m.className = 'msg ace';
    var c = document.createElement('div'); c.className = 'confirm'; c.textContent = text;
    m.appendChild(c);
    messagesEl.appendChild(m); scrollBottom();
  }

  var typingEl = null;
  function showTyping() {
    removeTyping();
    typingEl = document.createElement('div');
    typingEl.className = 'msg ace';
    typingEl.innerHTML = '<div class="sender">ACE</div><span class="typing"><span></span><span></span><span></span></span>';
    messagesEl.appendChild(typingEl); scrollBottom();
  }
  function removeTyping() { if (typingEl && typingEl.parentNode) typingEl.parentNode.removeChild(typingEl); typingEl = null; }

  /* ============================================================
     INTEL FEED — surface Ace's action confirmations as signals
     ============================================================ */
  var intelEl = $('intel-list');
  var intelHasItems = false;
  function pushIntel(text) {
    if (!intelHasItems) { intelEl.innerHTML = ''; intelHasItems = true; }
    var item = document.createElement('div');
    item.className = 'intel-item';
    item.innerHTML = '<span class="it-ts">' + nowLabel() + '</span>';
    item.appendChild(document.createTextNode(text));
    intelEl.insertBefore(item, intelEl.firstChild);
    while (intelEl.children.length > 12) intelEl.removeChild(intelEl.lastChild);
  }

  /* ============================================================
     CALENDAR PANEL
     ============================================================ */
  function refreshCalendar() {
    fetch(API + '/calendar?days=7', { headers: headers() })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (data) { renderCalendar(data.events || []); setSvc('svc-calendar', true); })
      .catch(function () { setSvc('svc-calendar', false); });
  }

  function renderCalendar(events) {
    var todayStr = localDateStr(new Date());
    var today = events.filter(function (e) { return e.date === todayStr; });
    var upcoming = events.filter(function (e) { return e.date > todayStr; }).slice(0, 5);

    var todayEl = $('today-list');
    if (!today.length) {
      todayEl.innerHTML = '<div class="empty-state">[ ALL CLEAR ]</div>';
    } else {
      todayEl.innerHTML = '';
      var now = new Date();
      var nextIdx = -1;
      for (var i = 0; i < today.length; i++) {
        if (!today[i].all_day && new Date(today[i].iso) > now) { nextIdx = i; break; }
      }
      today.forEach(function (e, i) {
        todayEl.appendChild(eventRow(e, i === nextIdx));
      });
    }

    var upEl = $('upcoming-list');
    if (!upcoming.length) {
      upEl.innerHTML = '<div class="empty-state">nothing ahead</div>';
    } else {
      upEl.innerHTML = '';
      upcoming.forEach(function (e) { upEl.appendChild(eventRow(e, false, true)); });
    }
  }

  function eventRow(e, isNext, showDay) {
    var row = document.createElement('div');
    row.className = 'event' + (isNext ? ' next' : '');
    if (showDay) {
      var d = document.createElement('span'); d.className = 'day-label'; d.textContent = e.day_label;
      row.appendChild(d);
    }
    var t = document.createElement('span'); t.className = 'event-time'; t.textContent = e.time;
    var n = document.createElement('span'); n.className = 'event-name'; n.textContent = e.title;
    row.appendChild(t); row.appendChild(n);
    if (isNext) { var b = document.createElement('span'); b.className = 'next-badge'; b.textContent = 'NEXT'; row.appendChild(b); }
    return row;
  }

  function localDateStr(d) {
    return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
  }

  /* ============================================================
     TASKS PANEL
     ============================================================ */
  function refreshTasks() {
    fetch(API + '/tasks', { headers: headers() })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (data) { renderTasks(data.tasks || []); setSvc('svc-tasks', true); })
      .catch(function () { setSvc('svc-tasks', false); });
  }

  function renderTasks(tasks) {
    var el = $('tasks-list');
    if (!tasks.length) { el.innerHTML = '<div class="empty-state">[ INBOX ZERO ]</div>'; return; }
    el.innerHTML = '';
    tasks.slice(0, 40).forEach(function (t) {
      var row = document.createElement('div');
      row.className = 'task';
      var box = document.createElement('span'); box.className = 'box';
      var body = document.createElement('span'); body.className = 't-body';
      body.appendChild(document.createTextNode(t.title));
      var meta = document.createElement('span'); meta.className = 't-list';
      meta.textContent = t.list + (t.due ? '' : '');
      if (t.due) { var due = document.createElement('span'); due.className = 't-due'; due.textContent = '  ⏱ ' + t.due; meta.appendChild(due); }
      body.appendChild(meta);
      row.appendChild(box); row.appendChild(body);
      el.appendChild(row);
    });
  }

  /* ============================================================
     WEATHER (status bar)
     ============================================================ */
  function refreshWeather() {
    fetch(API + '/weather', { headers: headers() })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (w) {
        if (w.ok && w.temp != null) {
          $('bb-weather').textContent = 'CLE ' + w.temp + '°F' + (w.condition ? ('  ' + w.condition) : '');
        } else {
          $('bb-weather').textContent = 'CLE --°F';
        }
      })
      .catch(function () { $('bb-weather').textContent = 'CLE --°F'; });
  }

  /* ============================================================
     MEMORY MODAL
     ============================================================ */
  function openMemory() {
    var body = $('modal-body');
    body.innerHTML = '<div class="loading">reading ace_memory.json…</div>';
    $('modal').classList.remove('hidden');
    fetch(API + '/memory', { headers: headers() })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (data) {
        setSvc('svc-drive', true);
        var mem = data.memories || [];
        if (!mem.length) { body.innerHTML = '<div class="empty-state">memory empty</div>'; return; }
        body.innerHTML = '';
        mem.forEach(function (m) {
          var item = document.createElement('div'); item.className = 'm-item';
          item.textContent = (typeof m === 'string') ? m : JSON.stringify(m);
          body.appendChild(item);
        });
      })
      .catch(function () { setSvc('svc-drive', false); body.innerHTML = '<div class="empty-state">could not read memory</div>'; });
  }
  $('qa-memory').addEventListener('click', openMemory);
  $('modal-close').addEventListener('click', function () { $('modal').classList.add('hidden'); });
  $('modal').addEventListener('click', function (e) { if (e.target === $('modal')) $('modal').classList.add('hidden'); });

  /* ============================================================
     STATUS INDICATORS
     ============================================================ */
  function setSvc(id, on) {
    var el = $(id); if (!el) return;
    el.classList.toggle('off', !on);
  }
  function setLink(on) {
    $('chip-link').classList.toggle('off', !on);
    $('chip-sync').classList.toggle('off', !on);
    setSvc('svc-gmail', on); // Gmail proven live through Ace link
  }

  /* ============================================================
     VOICE — Web Speech API in, SpeechSynthesis out
     ============================================================ */
  var voiceOut = true;
  function setupRecognition() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return null;
    var rec = new SR();
    rec.continuous = false;
    rec.interimResults = false;
    rec.lang = 'en-US';
    rec.onresult = function (e) {
      var transcript = e.results[0][0].transcript;
      $('chat-input').value = transcript;
      stopMic();
      sendMessage(transcript);
    };
    rec.onend = function () { if (state.micActive) stopMic(); };
    rec.onerror = function () { stopMic(); };
    return rec;
  }

  function startMic() {
    if (!state.recognition) state.recognition = setupRecognition();
    if (!state.recognition) { addAceMessage('Voice input is not supported in this browser.'); return; }
    state.micActive = true;
    $('mic-btn').classList.add('active');
    setOrbState('listening');
    try { state.recognition.start(); } catch (e) {}
  }
  function stopMic() {
    state.micActive = false;
    $('mic-btn').classList.remove('active');
    if (!state.busy) setOrbState('idle');
    try { state.recognition && state.recognition.stop(); } catch (e) {}
  }
  $('mic-btn').addEventListener('click', function () { state.micActive ? stopMic() : startMic(); });

  function speak(text) {
    if (!voiceOut || !window.speechSynthesis || !text) return;
    try {
      var u = new SpeechSynthesisUtterance(text);
      u.rate = 1.08; u.pitch = 1.0;
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(u);
    } catch (e) {}
  }

  /* ============================================================
     WIRING
     ============================================================ */
  $('send-btn').addEventListener('click', function () { sendMessage(); });
  $('chat-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') sendMessage(); });
  Array.prototype.forEach.call(document.querySelectorAll('.qa[data-msg]'), function (btn) {
    btn.addEventListener('click', function () { sendMessage(btn.getAttribute('data-msg')); });
  });

  boot();
})();
