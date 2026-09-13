/* StarCloud — the edge-to-edge blue / ice-white field behind NIGEL.
   Static layers (sky, stars, branching light, central column, node clusters) are
   rendered once to an offscreen canvas per viewport size. Each animation frame draws
   that bitmap through a camera transform and adds the light layers that move:
   column shimmer, node pulses and a small set of twinkling stars. */
window.StarCloud = (function () {
  const canvas = document.getElementById('starcloud');
  const ctx = canvas.getContext('2d');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let W = 0, H = 0, dpr = 1, portrait = false;
  let base = null;            // offscreen bitmap in device pixels
  let nodes = [];             // [{id, x, y, attention, label:HTMLElement}]
  let twinkle = [];           // stars that pulse
  let pulses = [];            // moving light along the column
  let frameCbs = [];
  let focusId = null;
  let dim = 0, dimTarget = 0; // 0 = full brightness, 1 = veiled behind panels

  // camera in normalized world units; z is zoom
  const cam = { x: 0.5, y: 0.5, z: 1, tx: 0.5, ty: 0.5, tz: 1 };

  function mulberry32(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      let t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  function nodePos(n) { return portrait && n.posPortrait ? n.posPortrait : n.pos; }

  /* ---------- static layers ---------- */
  function buildBase() {
    const off = document.createElement('canvas');
    off.width = Math.round(W * dpr); off.height = Math.round(H * dpr);
    const c = off.getContext('2d');
    c.scale(dpr, dpr);
    const rnd = mulberry32(20260913);
    const cx = W * 0.5, minDim = Math.min(W, H), maxDim = Math.max(W, H);

    // Sky
    const sky = c.createRadialGradient(cx, H * 0.45, 0, cx, H * 0.45, maxDim * 0.75);
    sky.addColorStop(0, '#12407f');
    sky.addColorStop(0.35, '#0a2a5c');
    sky.addColorStop(0.7, '#061a3d');
    sky.addColorStop(1, '#030b1f');
    c.fillStyle = sky; c.fillRect(0, 0, W, H);

    // Nebula haze — broad blue and ice-white washes
    c.globalCompositeOperation = 'lighter';
    const haze = [
      [0.5, 0.5, 0.55, 'rgba(90,150,235,0.22)'],
      [0.3, 0.35, 0.28, 'rgba(120,180,255,0.16)'],
      [0.72, 0.6, 0.3, 'rgba(120,180,255,0.14)'],
      [0.5, 0.15, 0.25, 'rgba(200,230,255,0.10)'],
      [0.5, 0.88, 0.25, 'rgba(200,230,255,0.08)']
    ];
    for (const [hx, hy, r, col] of haze) {
      const g = c.createRadialGradient(hx * W, hy * H, 0, hx * W, hy * H, r * maxDim);
      g.addColorStop(0, col); g.addColorStop(1, 'rgba(0,0,0,0)');
      c.fillStyle = g; c.fillRect(0, 0, W, H);
    }

    // Stars — dense field, brighter toward the centre
    const starCount = Math.round((W * H) / 900);
    twinkle = [];
    for (let i = 0; i < starCount; i++) {
      const x = rnd() * W, y = rnd() * H;
      const centreBias = 1 - Math.min(1, Math.hypot(x - cx, y - H * 0.5) / (maxDim * 0.6));
      const a = 0.25 + rnd() * 0.75 * (0.4 + centreBias);
      const s = rnd() < 0.9 ? 0.6 + rnd() * 0.9 : 1.5 + rnd() * 1.4;
      c.fillStyle = `rgba(${rnd() < 0.7 ? '225,240,255' : '170,205,255'},${a.toFixed(2)})`;
      c.beginPath(); c.arc(x, y, s, 0, Math.PI * 2); c.fill();
      if (s > 1.5 && twinkle.length < 70) twinkle.push({ x: x / W, y: y / H, s, phase: rnd() * Math.PI * 2, speed: 0.6 + rnd() * 1.2 });
    }
    // A few bright stars with bloom
    for (let i = 0; i < 40; i++) {
      const x = rnd() * W, y = rnd() * H, r = 6 + rnd() * 14;
      const g = c.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, 'rgba(235,245,255,0.9)'); g.addColorStop(0.25, 'rgba(200,225,255,0.35)'); g.addColorStop(1, 'rgba(150,200,255,0)');
      c.fillStyle = g; c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill();
    }

    // Branching light — dendrites that grow from the column and from each system node
    const segments = [];
    function grow(x, y, angle, len, depth, bright) {
      if (depth <= 0 || len < 3) return;
      const steps = 3 + Math.floor(rnd() * 3);
      let px = x, py = y, a = angle;
      for (let s = 0; s < steps; s++) {
        a += (rnd() - 0.5) * 0.55;
        const l = len / steps;
        const nx = px + Math.cos(a) * l, ny = py + Math.sin(a) * l;
        segments.push([px, py, nx, ny, depth, bright]);
        px = nx; py = ny;
        if (rnd() < 0.45) grow(px, py, a + (rnd() < 0.5 ? -1 : 1) * (0.35 + rnd() * 0.8), len * (0.45 + rnd() * 0.3), depth - 1, bright * 0.8);
      }
      grow(px, py, a + (rnd() - 0.5) * 0.6, len * 0.62, depth - 1, bright * 0.85);
    }
    // from the column
    const columnSeeds = portrait ? 26 : 34;
    for (let i = 0; i < columnSeeds; i++) {
      const y = H * (0.04 + (i / columnSeeds) * 0.92) + (rnd() - 0.5) * 18;
      const dir = i % 2 === 0 ? 1 : -1;
      const ang = dir > 0 ? (rnd() - 0.5) * 1.3 : Math.PI + (rnd() - 0.5) * 1.3;
      grow(cx + dir * 4, y, ang, minDim * (0.18 + rnd() * 0.32), 5, 0.95);
    }
    // from the nodes
    for (const n of nodes) {
      const [nx, ny] = nodePos(n);
      const count = 7 + Math.floor(rnd() * 4);
      for (let i = 0; i < count; i++) {
        const ang = (i / count) * Math.PI * 2 + rnd() * 0.5;
        grow(nx * W, ny * H, ang, minDim * (0.08 + rnd() * 0.16), 4, 0.9);
      }
    }
    // free-floating filaments across the field
    for (let i = 0; i < (portrait ? 10 : 18); i++) {
      grow(rnd() * W, rnd() * H, rnd() * Math.PI * 2, minDim * (0.1 + rnd() * 0.22), 4, 0.6);
    }
    // three passes: wide blue bloom, mid ice, thin white core
    c.lineCap = 'round'; c.lineJoin = 'round';
    const passes = [
      [5.5, 0.05, '110,170,255'],
      [2.2, 0.16, '170,210,255'],
      [0.9, 0.55, '235,246,255']
    ];
    for (const [wBase, aBase, col] of passes) {
      for (const [x1, y1, x2, y2, depth, bright] of segments) {
        const k = depth / 5;
        c.strokeStyle = `rgba(${col},${(aBase * bright * (0.35 + k)).toFixed(3)})`;
        c.lineWidth = wBase * (0.35 + k);
        c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
      }
    }

    // Central vertical light column
    const colW = Math.max(60, minDim * 0.16);
    const colG = c.createLinearGradient(cx - colW, 0, cx + colW, 0);
    colG.addColorStop(0, 'rgba(120,180,255,0)');
    colG.addColorStop(0.42, 'rgba(160,205,255,0.20)');
    colG.addColorStop(0.5, 'rgba(230,243,255,0.62)');
    colG.addColorStop(0.58, 'rgba(160,205,255,0.20)');
    colG.addColorStop(1, 'rgba(120,180,255,0)');
    c.fillStyle = colG; c.fillRect(cx - colW, 0, colW * 2, H);
    const coreW = Math.max(6, minDim * 0.014);
    const coreG = c.createLinearGradient(cx - coreW, 0, cx + coreW, 0);
    coreG.addColorStop(0, 'rgba(240,248,255,0)'); coreG.addColorStop(0.5, 'rgba(255,255,255,0.88)'); coreG.addColorStop(1, 'rgba(240,248,255,0)');
    c.fillStyle = coreG; c.fillRect(cx - coreW, 0, coreW * 2, H);
    // vertical fade so the column reads as emerging from the field
    c.globalCompositeOperation = 'destination-over';
    c.globalCompositeOperation = 'source-over';
    const fade = c.createLinearGradient(0, 0, 0, H);
    fade.addColorStop(0, 'rgba(3,11,31,0.55)'); fade.addColorStop(0.18, 'rgba(3,11,31,0)');
    fade.addColorStop(0.82, 'rgba(3,11,31,0)'); fade.addColorStop(1, 'rgba(3,11,31,0.55)');
    c.fillStyle = fade; c.fillRect(0, 0, W, H);

    // Node clusters — a bright core with a wide bloom
    c.globalCompositeOperation = 'lighter';
    for (const n of nodes) {
      const [nx, ny] = nodePos(n);
      const x = nx * W, y = ny * H, r = Math.max(34, minDim * 0.075);
      const g = c.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, 'rgba(240,248,255,0.85)');
      g.addColorStop(0.18, 'rgba(200,228,255,0.35)');
      g.addColorStop(1, 'rgba(140,190,255,0)');
      c.fillStyle = g; c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill();
    }
    c.globalCompositeOperation = 'source-over';
    base = off;
  }

  /* ---------- live layer ---------- */
  function clampCam() {
    const half = 0.5 / cam.z;
    cam.tx = Math.min(1 - half, Math.max(half, cam.tx));
    cam.ty = Math.min(1 - half, Math.max(half, cam.ty));
  }
  function project(nx, ny) {
    return [W / 2 + (nx - cam.x) * W * cam.z, H / 2 + (ny - cam.y) * H * cam.z];
  }

  let last = performance.now();
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    const t = now / 1000;
    if (base) {
      // ease camera
      const k = reduceMotion ? 1 : 1 - Math.pow(0.001, dt);
      cam.x += (cam.tx - cam.x) * k; cam.y += (cam.ty - cam.y) * k; cam.z += (cam.tz - cam.z) * k;
      dim += (dimTarget - dim) * k;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      ctx.save();
      ctx.translate(W / 2, H / 2); ctx.scale(cam.z, cam.z); ctx.translate(-cam.x * W, -cam.y * H);
      ctx.drawImage(base, 0, 0, W, H);

      ctx.globalCompositeOperation = 'lighter';
      // column shimmer: soft pulses drifting upward
      if (!reduceMotion) {
        const cx = W * 0.5;
        for (const p of pulses) {
          p.y -= p.v * dt; if (p.y < -0.1) { p.y = 1.1; p.x = (Math.random() - 0.5) * 0.02; }
          const g = ctx.createRadialGradient(cx + p.x * W, p.y * H, 0, cx + p.x * W, p.y * H, p.r);
          g.addColorStop(0, `rgba(235,245,255,${p.a})`); g.addColorStop(1, 'rgba(200,230,255,0)');
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx + p.x * W, p.y * H, p.r, 0, Math.PI * 2); ctx.fill();
        }
        for (const s of twinkle) {
          const a = 0.25 + 0.45 * (0.5 + 0.5 * Math.sin(t * s.speed + s.phase));
          ctx.fillStyle = `rgba(230,242,255,${a.toFixed(2)})`;
          ctx.beginPath(); ctx.arc(s.x * W, s.y * H, s.s * 1.2, 0, Math.PI * 2); ctx.fill();
        }
      }
      // node pulses — amber when attention is needed, ice-white otherwise
      for (const n of nodes) {
        const [nx, ny] = nodePos(n);
        const x = nx * W, y = ny * H;
        const pulse = reduceMotion ? 0.5 : 0.5 + 0.5 * Math.sin(t * 1.4 + (n.phase || 0));
        const r = Math.max(18, Math.min(W, H) * 0.035) * (1 + pulse * 0.25);
        const col = n.attention ? '255,190,90' : '200,230,255';
        const g = ctx.createRadialGradient(x, y, 0, x, y, r);
        g.addColorStop(0, `rgba(${col},${(0.55 + pulse * 0.3).toFixed(2)})`);
        g.addColorStop(0.4, `rgba(${col},0.18)`);
        g.addColorStop(1, `rgba(${col},0)`);
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
        if (n.attention) {
          // amber ring
          ctx.strokeStyle = `rgba(255,190,90,${(0.35 + pulse * 0.35).toFixed(2)})`;
          ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(x, y, r * 1.35, 0, Math.PI * 2); ctx.stroke();
        }
        if (n.id === focusId) {
          ctx.strokeStyle = 'rgba(235,245,255,0.6)'; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.arc(x, y, r * 1.9, 0, Math.PI * 2); ctx.stroke();
        }
      }
      ctx.restore();
      // veil while a panel is open — keeps the panels legible without losing the field
      if (dim > 0.002) {
        ctx.globalCompositeOperation = 'source-over';
        ctx.fillStyle = `rgba(3,11,31,${(dim * 0.5).toFixed(3)})`;
        ctx.fillRect(0, 0, W, H);
      }
      ctx.globalCompositeOperation = 'source-over';

      // move the DOM labels with the field
      for (const n of nodes) {
        if (!n.label) continue;
        const [nx, ny] = nodePos(n);
        const [sx, sy] = project(nx, ny);
        n.label.style.transform = `translate(${sx.toFixed(1)}px, ${sy.toFixed(1)}px)${n.label.classList.contains('flip') ? ' translateX(-100%)' : n.label.classList.contains('stack') ? ' translateX(-50%)' : ''}`;
      }
      for (const cb of frameCbs) cb();
    }
    requestAnimationFrame(frame);
  }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = window.innerWidth; H = window.innerHeight;
    portrait = W / H < 0.85;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    if (!pulses.length) {
      for (let i = 0; i < 6; i++) pulses.push({ y: Math.random(), x: (Math.random() - 0.5) * 0.02, v: 0.03 + Math.random() * 0.04, r: 40 + Math.random() * 70, a: 0.10 + Math.random() * 0.12 });
    }
    buildBase();
    clampCam();
  }

  let resizeTimer = null;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 120); });

  return {
    setNodes(list) {
      nodes = list.map((n, i) => Object.assign({ phase: i * 0.9 }, n));
      if (W) buildBase();
    },
    setAttention(id, on) { const n = nodes.find(n => n.id === id); if (n) n.attention = !!on; },
    bindLabel(id, el) { const n = nodes.find(n => n.id === id); if (n) n.label = el; },
    focus(id, zoom) {
      focusId = id;
      const n = nodes.find(n => n.id === id);
      if (n) { const [x, y] = nodePos(n); cam.tx = x; cam.ty = y; cam.tz = zoom || 1.5; }
      else { cam.tx = 0.5; cam.ty = 0.5; cam.tz = zoom || 1; }
      clampCam();
    },
    reset() { focusId = null; cam.tx = 0.5; cam.ty = 0.5; cam.tz = 1; },
    veil(on) { dimTarget = on ? 1 : 0; },
    onFrame(cb) { frameCbs.push(cb); },
    isPortrait() { return portrait; },
    start() { resize(); requestAnimationFrame(frame); }
  };
})();
