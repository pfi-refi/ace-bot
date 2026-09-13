/* StarCloud — the field behind NIGEL, built to match the reference:
   a near-black ground, a blue nebula concentrated at the centre, galaxy-like
   arcs of particle filaments sweeping around a bright core, a thin vertical
   light column with a starburst where it crosses the field, faint vertical
   streaks and thousands of small blue stars.
   Static layers are rendered once per viewport to an offscreen canvas. Each
   frame draws that bitmap through a camera transform and adds the parts that
   move: column shimmer, node pulses and a small set of twinkling stars. */
window.StarCloud = (function () {
  const canvas = document.getElementById('starcloud');
  const ctx = canvas.getContext('2d');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const TAU = Math.PI * 2;

  let W = 0, H = 0, dpr = 1, portrait = false;
  let base = null, nodes = [], twinkle = [], pulses = [], frameCbs = [];
  let arcs = [], dust = [], comets = [], sparks = [], ringPulses = [], shooting = null, nextShot = 4, nextRing = 1.5;
  let focusId = null, dim = 0, dimTarget = 0;
  const cam = { x: 0.5, y: 0.5, z: 1, tx: 0.5, ty: 0.5, tz: 1 };
  const core = { x: 0.53, y: 0.46 };   // where the column crosses the field
  function coreX() { return portrait ? 0.5 : core.x; }

  function mulberry32(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      let t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  function nodePos(n) { return portrait && n.posPortrait ? n.posPortrait : n.pos; }

  function buildBase() {
    const off = document.createElement('canvas');
    off.width = Math.round(W * dpr); off.height = Math.round(H * dpr);
    const c = off.getContext('2d');
    c.scale(dpr, dpr);
    const rnd = mulberry32(20260912);
    arcs = [];
    const gauss = () => (rnd() + rnd() + rnd() - 1.5) * 0.8;
    const cx = coreX() * W, cy = core.y * H, minDim = Math.min(W, H), maxDim = Math.max(W, H);
    const dot = (x, y, r, col, a) => { c.fillStyle = `rgba(${col},${a.toFixed(3)})`; c.beginPath(); c.arc(x, y, r, 0, TAU); c.fill(); };
    const BLUE = '96,160,255', ICE = '210,232,255', DEEP = '40,100,220';

    // ground + nebula haze, wider than tall like a galaxy seen from above
    c.fillStyle = '#01040b'; c.fillRect(0, 0, W, H);
    c.globalCompositeOperation = 'lighter';
    const haze = (sx, sy, r, stops) => {
      c.save(); c.translate(cx, cy); c.scale(sx, sy);
      const g = c.createRadialGradient(0, 0, 0, 0, 0, r);
      for (const [o, col] of stops) g.addColorStop(o, col);
      c.fillStyle = g; c.fillRect(-r, -r, r * 2, r * 2); c.restore();
    };
    haze(1, 0.58, maxDim * 0.62, [[0, 'rgba(24,70,170,0.55)'], [0.35, 'rgba(14,44,120,0.30)'], [0.7, 'rgba(6,18,60,0.12)'], [1, 'rgba(0,0,0,0)']]);
    haze(1, 0.7, minDim * 0.34, [[0, 'rgba(70,140,255,0.42)'], [0.5, 'rgba(40,100,220,0.16)'], [1, 'rgba(0,0,0,0)']]);
    haze(0.5, 1, minDim * 0.55, [[0, 'rgba(60,120,240,0.22)'], [1, 'rgba(0,0,0,0)']]);
    haze(1.15, 0.5, W * 0.5, [[0, 'rgba(30,80,200,0.18)'], [0.6, 'rgba(20,60,160,0.08)'], [1, 'rgba(0,0,0,0)']]);

    // galaxy arcs: noisy ellipses around the core, drawn as glow + particle dust
    const arcCount = portrait ? 26 : 44;
    for (let k = 0; k < arcCount; k++) {
      const rx = W * (0.06 + rnd() * 0.42), ry = rx * (0.28 + rnd() * 0.5);
      const rot = (rnd() - 0.5) * 0.5, ox = gauss() * W * 0.08, oy = gauss() * H * 0.1;
      const a0 = rnd() * TAU, span = (0.5 + rnd() * 1.5) * Math.PI;
      const f = [2 + rnd() * 3, 5 + rnd() * 6, 11 + rnd() * 9], p = [rnd() * TAU, rnd() * TAU, rnd() * TAU];
      const amp = 0.04 + rnd() * 0.07, bright = 0.5 + rnd() * 0.5;
      const steps = Math.max(40, Math.round(span * rx / 3));
      const pts = [];
      for (let i = 0; i <= steps; i++) {
        const a = a0 + (i / steps) * span;
        const r = 1 + amp * (Math.sin(a * f[0] + p[0]) + 0.6 * Math.sin(a * f[1] + p[1]) + 0.3 * Math.sin(a * f[2] + p[2]));
        const x0 = Math.cos(a) * rx * r, y0 = Math.sin(a) * ry * r;
        pts.push([cx + ox + x0 * Math.cos(rot) - y0 * Math.sin(rot), cy + oy + x0 * Math.sin(rot) + y0 * Math.cos(rot)]);
      }
      if (pts.length > 60) arcs.push(pts);
      c.lineCap = 'round'; c.lineJoin = 'round';
      for (const [w, a, col] of [[12, 0.04, DEEP], [4, 0.07, BLUE], [1.2, 0.11, ICE]]) {
        c.strokeStyle = `rgba(${col},${(a * bright).toFixed(3)})`; c.lineWidth = w;
        c.beginPath(); pts.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y)); c.stroke();
      }
      for (const [x, y] of pts) {
        if (rnd() < 0.8) dot(x + gauss() * 9, y + gauss() * 6, 0.4 + rnd() * 0.9, rnd() < 0.6 ? BLUE : ICE, (0.18 + rnd() * 0.55) * bright);
        if (rnd() < 0.25) dot(x + gauss() * 16, y + gauss() * 10, 0.4 + rnd() * 0.6, BLUE, 0.2 * bright);
        if (rnd() < 0.02) dot(x + gauss() * 4, y + gauss() * 4, 1.6 + rnd() * 1.4, ICE, 0.7 * bright);
      }
    }

    // wisps: soft dendrites from the column and from each system node
    const segs = [];
    function grow(x, y, ang, len, depth, bright) {
      if (depth <= 0 || len < 4) return;
      const steps = 3 + Math.floor(rnd() * 3); let px = x, py = y, a = ang;
      for (let s = 0; s < steps; s++) {
        a += (rnd() - 0.5) * 0.7; const l = len / steps;
        const nx = px + Math.cos(a) * l, ny = py + Math.sin(a) * l;
        segs.push([px, py, nx, ny, depth, bright]); px = nx; py = ny;
        if (rnd() < 0.4) grow(px, py, a + (rnd() < 0.5 ? -1 : 1) * (0.4 + rnd() * 0.8), len * (0.45 + rnd() * 0.3), depth - 1, bright * 0.8);
      }
      grow(px, py, a + (rnd() - 0.5) * 0.6, len * 0.6, depth - 1, bright * 0.85);
    }
    for (let i = 0; i < (portrait ? 14 : 22); i++) {
      const y = cy + gauss() * H * 0.32, dir = i % 2 ? -1 : 1;
      grow(cx + dir * 6, y, dir > 0 ? (rnd() - 0.5) * 1.2 : Math.PI + (rnd() - 0.5) * 1.2, minDim * (0.12 + rnd() * 0.3), 5, 0.8);
    }
    for (const n of nodes) {
      const [nx, ny] = nodePos(n);
      for (let i = 0, cnt = 5 + Math.floor(rnd() * 3); i < cnt; i++) grow(nx * W, ny * H, (i / cnt) * TAU + rnd() * 0.6, minDim * (0.06 + rnd() * 0.14), 4, 0.75);
    }
    for (const [w, a, col] of [[6, 0.03, DEEP], [2, 0.07, BLUE], [0.8, 0.2, ICE]]) {
      for (const [x1, y1, x2, y2, d, b] of segs) {
        const k = d / 5; c.strokeStyle = `rgba(${col},${(a * b * (0.3 + k)).toFixed(3)})`; c.lineWidth = w * (0.4 + k);
        c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
        if (rnd() < 0.3) dot((x1 + x2) / 2 + gauss() * 3, (y1 + y2) / 2 + gauss() * 3, 0.5 + rnd() * 0.7, ICE, 0.5 * b);
      }
    }

    // faint vertical streaks, denser toward the column
    for (let i = 0; i < (portrait ? 30 : 60); i++) {
      const x = rnd() < 0.6 ? cx + gauss() * W * 0.35 : rnd() * W;
      const len = H * (0.05 + rnd() * 0.35), y0 = rnd() * (H - len);
      const g = c.createLinearGradient(0, y0, 0, y0 + len);
      const a = 0.08 + rnd() * 0.2;
      g.addColorStop(0, `rgba(${BLUE},0)`); g.addColorStop(0.5, `rgba(${ICE},${a.toFixed(2)})`); g.addColorStop(1, `rgba(${BLUE},0)`);
      c.fillStyle = g; c.fillRect(x, y0, rnd() < 0.8 ? 0.8 : 1.5, len);
    }

    // stars
    const starCount = Math.round((W * H) / 560);
    twinkle = [];
    for (let i = 0; i < starCount; i++) {
      const x = rnd() * W, y = rnd() * H;
      const d = Math.hypot((x - cx) / W, (y - cy) / (H * 0.7));
      const bias = 1 - Math.min(1, d / 0.7);
      const a = (0.15 + rnd() * 0.6) * (0.5 + bias * 0.8);
      const s = rnd() < 0.92 ? 0.4 + rnd() * 0.8 : 1.2 + rnd() * 1.2;
      dot(x, y, s, rnd() < 0.65 ? BLUE : ICE, Math.min(1, a));
      if (s > 1.2 && twinkle.length < 80) twinkle.push({ x: x / W, y: y / H, s, phase: rnd() * TAU, speed: 0.5 + rnd() * 1.2 });
    }
    for (let i = 0; i < (portrait ? 40 : 70); i++) {
      const x = cx + gauss() * W * 0.45, y = cy + gauss() * H * 0.4, r = 3 + rnd() * 9;
      const g = c.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, 'rgba(225,240,255,0.95)'); g.addColorStop(0.3, 'rgba(120,180,255,0.45)'); g.addColorStop(1, 'rgba(60,120,240,0)');
      c.fillStyle = g; c.beginPath(); c.arc(x, y, r, 0, TAU); c.fill();
    }

    // the column: a thin full-height core, a soft sheath, and a tall glow that widens at the centre
    haze(0.11, 1, H * 0.6, [[0, 'rgba(120,180,255,0.55)'], [0.4, 'rgba(70,130,240,0.22)'], [1, 'rgba(0,0,0,0)']]);
    const sheath = c.createLinearGradient(cx - 14, 0, cx + 14, 0);
    sheath.addColorStop(0, 'rgba(120,180,255,0)'); sheath.addColorStop(0.5, 'rgba(160,205,255,0.35)'); sheath.addColorStop(1, 'rgba(120,180,255,0)');
    c.fillStyle = sheath; c.fillRect(cx - 14, 0, 28, H);
    const coreG = c.createLinearGradient(cx - 2.5, 0, cx + 2.5, 0);
    coreG.addColorStop(0, 'rgba(230,242,255,0)'); coreG.addColorStop(0.5, 'rgba(255,255,255,0.95)'); coreG.addColorStop(1, 'rgba(230,242,255,0)');
    c.fillStyle = coreG; c.fillRect(cx - 2.5, 0, 5, H);
    // starburst at the core
    const burst = c.createRadialGradient(cx, cy, 0, cx, cy, minDim * 0.16);
    burst.addColorStop(0, 'rgba(255,255,255,1)'); burst.addColorStop(0.06, 'rgba(225,240,255,0.9)'); burst.addColorStop(0.2, 'rgba(120,180,255,0.45)'); burst.addColorStop(1, 'rgba(60,120,240,0)');
    c.fillStyle = burst; c.beginPath(); c.arc(cx, cy, minDim * 0.16, 0, TAU); c.fill();
    const flare = (len, thick, a) => {
      const g = c.createLinearGradient(cx - len, 0, cx + len, 0);
      g.addColorStop(0, 'rgba(200,228,255,0)'); g.addColorStop(0.5, `rgba(235,245,255,${a})`); g.addColorStop(1, 'rgba(200,228,255,0)');
      c.fillStyle = g; c.fillRect(cx - len, cy - thick / 2, len * 2, thick);
    };
    flare(W * 0.16, 1, 0.95); flare(W * 0.12, 4, 0.35); flare(W * 0.07, 12, 0.14);

    // node blooms
    for (const n of nodes) {
      const [nx, ny] = nodePos(n); const x = nx * W, y = ny * H, r = Math.max(28, minDim * 0.06);
      const g = c.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, 'rgba(235,245,255,0.9)'); g.addColorStop(0.12, 'rgba(140,195,255,0.5)'); g.addColorStop(0.45, 'rgba(70,130,240,0.16)'); g.addColorStop(1, 'rgba(40,100,220,0)');
      c.fillStyle = g; c.beginPath(); c.arc(x, y, r, 0, TAU); c.fill();
    }
    c.globalCompositeOperation = 'source-over';
    // vignette so the corners fall to black
    const vig = c.createRadialGradient(cx, cy, minDim * 0.3, cx, cy, maxDim * 0.85);
    vig.addColorStop(0, 'rgba(1,4,11,0)'); vig.addColorStop(1, 'rgba(1,4,11,0.75)');
    c.fillStyle = vig; c.fillRect(0, 0, W, H);
    base = off;
  }

  function clampCam() {
    const half = 0.5 / cam.z;
    cam.tx = Math.min(1 - half, Math.max(half, cam.tx));
    cam.ty = Math.min(1 - half, Math.max(half, cam.ty));
  }
  function project(nx, ny) { return [W / 2 + (nx - cam.x) * W * cam.z, H / 2 + (ny - cam.y) * H * cam.z]; }

  /* ---------- moving parts ---------- */
  function initLive() {
    const R = Math.random;
    dust = []; comets = []; sparks = []; ringPulses = []; shooting = null;
    const dustCount = portrait ? 200 : 340;
    for (let i = 0; i < dustCount; i++) {
      const depth = 0.4 + R() * 0.9;   // parallax: nearer dust moves more
      dust.push({ x: R() * W, y: R() * H, vx: (R() - 0.5) * 8 * depth, vy: (R() - 0.5) * 5 * depth - 2 * depth, r: 0.4 + R() * 1.2 * depth, a: 0.15 + R() * 0.45, ph: R() * TAU, depth });
    }
    const cometCount = Math.min(arcs.length * 2, portrait ? 28 : 48);
    for (let i = 0; i < cometCount; i++) {
      comets.push({ arc: i % arcs.length, t: R(), v: (0.025 + R() * 0.05) * (R() < 0.5 ? 1 : -1), len: 8 + Math.floor(R() * 8), b: 0.5 + R() * 0.5 });
    }
    for (let i = 0; i < (portrait ? 30 : 56); i++) sparks.push(newSpark(R() * H));
  }
  function newSpark(y) {
    const R = Math.random;
    return { x: (R() - 0.5) * 22, y: y == null ? H + 10 : y, vy: 35 + R() * 70, r: 0.6 + R() * 1.4, a: 0.3 + R() * 0.6, wob: R() * TAU, ws: 1 + R() * 2 };
  }

  let last = performance.now();
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000); last = now; const t = now / 1000;
    if (base) {
      const k = reduceMotion ? 1 : 1 - Math.pow(0.001, dt);
      cam.x += (cam.tx - cam.x) * k; cam.y += (cam.ty - cam.y) * k; cam.z += (cam.tz - cam.z) * k; dim += (dimTarget - dim) * k;
      const cx = coreX() * W, cy = core.y * H, minDim = Math.min(W, H);
      // the whole field sways and breathes very slowly
      const swayX = reduceMotion ? 0 : Math.sin(t * 0.11) * W * 0.006, swayY = reduceMotion ? 0 : Math.cos(t * 0.08) * H * 0.005;
      const rot = reduceMotion ? 0 : Math.sin(t * 0.045) * 0.012;
      const breathe = reduceMotion ? 1 : 1 + Math.sin(t * 0.19) * 0.012;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
      ctx.save();
      ctx.translate(W / 2, H / 2); ctx.scale(cam.z, cam.z); ctx.translate(-cam.x * W, -cam.y * H);
      ctx.save(); ctx.translate(cx + swayX, cy + swayY); ctx.rotate(rot); ctx.scale(breathe, breathe); ctx.translate(-cx, -cy);
      ctx.drawImage(base, 0, 0, W, H);
      ctx.restore();
      ctx.globalCompositeOperation = 'lighter';

      if (!reduceMotion) {
        // drifting dust with depth
        for (const d of dust) {
          d.x += d.vx * dt; d.y += d.vy * dt;
          if (d.x < -4) d.x = W + 4; else if (d.x > W + 4) d.x = -4;
          if (d.y < -4) d.y = H + 4; else if (d.y > H + 4) d.y = -4;
          const a = d.a * (0.55 + 0.45 * Math.sin(t * 1.3 * d.depth + d.ph));
          ctx.fillStyle = `rgba(190,220,255,${a.toFixed(2)})`; ctx.beginPath(); ctx.arc(d.x + swayX * d.depth, d.y + swayY * d.depth, d.r, 0, TAU); ctx.fill();
        }
        // light travelling along the filaments
        for (const cm of comets) {
          const pts = arcs[cm.arc]; if (!pts) continue;
          cm.t += cm.v * dt; if (cm.t > 1) cm.t -= 1; else if (cm.t < 0) cm.t += 1;
          const n = pts.length - 1, head = Math.floor(cm.t * n), dir = cm.v > 0 ? 1 : -1;
          for (let j = 0; j < cm.len; j++) {
            const idx = head - j * dir; if (idx < 0 || idx > n) break;
            const [x, y] = pts[idx]; const f = 1 - j / cm.len;
            ctx.fillStyle = `rgba(${j ? '150,200,255' : '255,255,255'},${(0.85 * f * f * cm.b).toFixed(3)})`;
            ctx.beginPath(); ctx.arc(x + swayX, y + swayY, 0.6 + 1.6 * f, 0, TAU); ctx.fill();
          }
          const [hx, hy] = pts[head];
          const g = ctx.createRadialGradient(hx + swayX, hy + swayY, 0, hx + swayX, hy + swayY, 9);
          g.addColorStop(0, `rgba(235,245,255,${(0.45 * cm.b).toFixed(2)})`); g.addColorStop(1, 'rgba(120,180,255,0)');
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(hx + swayX, hy + swayY, 9, 0, TAU); ctx.fill();
        }
        // sparks rising in the column
        for (let i = 0; i < sparks.length; i++) {
          const sp = sparks[i]; sp.y -= sp.vy * dt;
          if (sp.y < -10) { sparks[i] = newSpark(); continue; }
          const x = cx + sp.x + Math.sin(t * sp.ws + sp.wob) * 4, fade = Math.min(1, sp.y / (H * 0.15)) * Math.min(1, (H - sp.y) / (H * 0.15));
          ctx.fillStyle = `rgba(225,240,255,${(sp.a * fade).toFixed(2)})`; ctx.beginPath(); ctx.arc(x, sp.y, sp.r, 0, TAU); ctx.fill();
        }
        for (const p of pulses) {
          p.y -= p.v * dt; if (p.y < -0.1) p.y = 1.1;
          const g = ctx.createRadialGradient(cx, p.y * H, 0, cx, p.y * H, p.r);
          g.addColorStop(0, `rgba(225,240,255,${p.a})`); g.addColorStop(1, 'rgba(120,180,255,0)');
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, p.y * H, p.r, 0, TAU); ctx.fill();
        }
        for (const s of twinkle) {
          const a = 0.2 + 0.5 * (0.5 + 0.5 * Math.sin(t * s.speed + s.phase));
          ctx.fillStyle = `rgba(225,240,255,${a.toFixed(2)})`; ctx.beginPath(); ctx.arc(s.x * W + swayX, s.y * H + swayY, s.s * 1.1, 0, TAU); ctx.fill();
        }
        // the core: breathing glow, slowly rotating flare beams, holographic rings, ring pulses
        const b = 0.5 + 0.5 * Math.sin(t * 0.8);
        let g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 70 + b * 40);
        g.addColorStop(0, `rgba(235,245,255,${(0.3 + b * 0.25).toFixed(2)})`); g.addColorStop(1, 'rgba(120,180,255,0)');
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, 110, 0, TAU); ctx.fill();
        ctx.save(); ctx.translate(cx, cy); ctx.rotate(t * 0.12);
        for (let i = 0; i < 2; i++) {
          ctx.rotate(Math.PI / 2);
          const len = minDim * (0.22 + b * 0.05);
          const fg = ctx.createLinearGradient(-len, 0, len, 0);
          fg.addColorStop(0, 'rgba(160,205,255,0)'); fg.addColorStop(0.5, `rgba(220,238,255,${(0.35 + b * 0.2).toFixed(2)})`); fg.addColorStop(1, 'rgba(160,205,255,0)');
          ctx.fillStyle = fg; ctx.fillRect(-len, -0.6, len * 2, 1.2);
        }
        ctx.restore();
        const ring = (r, ticks, speed, alpha, dashed) => {
          ctx.save(); ctx.translate(cx, cy); ctx.rotate(t * speed);
          ctx.strokeStyle = `rgba(160,205,255,${alpha})`; ctx.lineWidth = 0.8;
          if (dashed) ctx.setLineDash([r * 0.35, r * 0.2]);
          ctx.beginPath(); ctx.ellipse(0, 0, r, r * 0.42, 0, 0, TAU); ctx.stroke(); ctx.setLineDash([]);
          for (let i = 0; i < ticks; i++) {
            const a = (i / ticks) * TAU, big = i % 6 === 0;
            const x1 = Math.cos(a) * r, y1 = Math.sin(a) * r * 0.42;
            ctx.strokeStyle = `rgba(200,228,255,${(alpha * (big ? 1.6 : 0.9)).toFixed(3)})`;
            ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x1 * (big ? 1.045 : 1.02), y1 * (big ? 1.045 : 1.02)); ctx.stroke();
          }
          ctx.restore();
        };
        ring(minDim * 0.21, 48, 0.05, 0.3, false);
        ring(minDim * 0.29, 0, -0.03, 0.2, true);
        ring(minDim * 0.38, 72, 0.018, 0.13, false);
        nextRing -= dt;
        if (nextRing <= 0) { ringPulses.push({ r: 8, a: 0.5 }); nextRing = 3.2 + Math.random() * 2; }
        for (let i = ringPulses.length - 1; i >= 0; i--) {
          const rp = ringPulses[i]; rp.r += minDim * 0.16 * dt; rp.a -= 0.16 * dt;
          if (rp.a <= 0) { ringPulses.splice(i, 1); continue; }
          ctx.strokeStyle = `rgba(190,222,255,${rp.a.toFixed(3)})`; ctx.lineWidth = 1.2;
          ctx.beginPath(); ctx.ellipse(cx, cy, rp.r, rp.r * 0.5, 0, 0, TAU); ctx.stroke();
        }
        // a shooting star now and then
        nextShot -= dt;
        if (!shooting && nextShot <= 0) {
          const ang = Math.PI * (0.15 + Math.random() * 0.3) * (Math.random() < 0.5 ? 1 : -1) + (Math.random() < 0.5 ? 0 : Math.PI);
          shooting = { x: Math.random() * W, y: Math.random() * H * 0.6, vx: Math.cos(ang) * 900, vy: Math.sin(ang) * 400, life: 0.9 };
          nextShot = 5 + Math.random() * 7;
        }
        if (shooting) {
          const sh = shooting; sh.life -= dt; sh.x += sh.vx * dt; sh.y += sh.vy * dt;
          const f = Math.max(0, sh.life / 0.9), L = 0.14;
          const g2 = ctx.createLinearGradient(sh.x - sh.vx * L, sh.y - sh.vy * L, sh.x, sh.y);
          g2.addColorStop(0, 'rgba(180,215,255,0)'); g2.addColorStop(1, `rgba(255,255,255,${(0.9 * f).toFixed(2)})`);
          ctx.strokeStyle = g2; ctx.lineWidth = 1.4; ctx.beginPath(); ctx.moveTo(sh.x - sh.vx * L, sh.y - sh.vy * L); ctx.lineTo(sh.x, sh.y); ctx.stroke();
          if (sh.life <= 0) shooting = null;
        }
      }
      // system nodes: pulse, orbit and a mote circling each one
      for (const n of nodes) {
        const [nx, ny] = nodePos(n); const x = nx * W + swayX, y = ny * H + swayY;
        const pulse = reduceMotion ? 0.5 : 0.5 + 0.5 * Math.sin(t * 1.3 + (n.phase || 0));
        const r = Math.max(16, minDim * 0.03) * (1 + pulse * 0.3);
        const col = n.attention ? '255,186,80' : '160,205,255';
        const g = ctx.createRadialGradient(x, y, 0, x, y, r);
        g.addColorStop(0, `rgba(${col},${(0.4 + pulse * 0.3).toFixed(2)})`); g.addColorStop(0.5, `rgba(${col},0.12)`); g.addColorStop(1, `rgba(${col},0)`);
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill();
        if (!reduceMotion) {
          const orx = r * 2.3, ory = r * 0.85, tilt = (n.phase || 0) * 0.5;
          ctx.save(); ctx.translate(x, y); ctx.rotate(tilt);
          ctx.strokeStyle = `rgba(${col},${n.attention ? 0.35 : 0.18})`; ctx.lineWidth = 0.8;
          ctx.beginPath(); ctx.ellipse(0, 0, orx, ory, 0, 0, TAU); ctx.stroke();
          const ma = t * (n.attention ? 1.7 : 0.9) + (n.phase || 0);
          const mx = Math.cos(ma) * orx, my = Math.sin(ma) * ory;
          const mg = ctx.createRadialGradient(mx, my, 0, mx, my, 7);
          mg.addColorStop(0, `rgba(${n.attention ? '255,220,160' : '255,255,255'},0.95)`); mg.addColorStop(1, `rgba(${col},0)`);
          ctx.fillStyle = mg; ctx.beginPath(); ctx.arc(mx, my, 7, 0, TAU); ctx.fill();
          ctx.restore();
        }
        if (n.attention) { ctx.strokeStyle = `rgba(255,186,80,${(0.3 + pulse * 0.35).toFixed(2)})`; ctx.lineWidth = 1; ctx.beginPath(); ctx.arc(x, y, r * 1.4, 0, TAU); ctx.stroke(); }
        if (n.id === focusId) { ctx.strokeStyle = 'rgba(225,240,255,0.55)'; ctx.lineWidth = 1; ctx.beginPath(); ctx.arc(x, y, r * 2, 0, TAU); ctx.stroke(); }
      }
      ctx.restore();
      if (dim > 0.002) { ctx.globalCompositeOperation = 'source-over'; ctx.fillStyle = `rgba(1,4,11,${(dim * 0.45).toFixed(3)})`; ctx.fillRect(0, 0, W, H); }
      // intro fade
      if (intro > 0) { intro = Math.max(0, intro - dt / 1.6); ctx.globalCompositeOperation = 'source-over'; ctx.fillStyle = `rgba(1,4,11,${intro.toFixed(3)})`; ctx.fillRect(0, 0, W, H); }
      ctx.globalCompositeOperation = 'source-over';
      for (const n of nodes) {
        if (!n.label) continue;
        const [nx, ny] = nodePos(n); const [sx, sy] = project(nx, ny);
        n.label.style.transform = `translate(${(sx + swayX * cam.z).toFixed(1)}px, ${(sy + swayY * cam.z - 30 * cam.z).toFixed(1)}px) translate(-50%, -100%)`;
      }
      for (const cb of frameCbs) cb();
    }
    requestAnimationFrame(frame);
  }
  let intro = reduceMotion ? 0 : 1;

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = window.innerWidth; H = window.innerHeight; portrait = W / H < 0.85;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    if (!pulses.length) for (let i = 0; i < 5; i++) pulses.push({ y: Math.random(), v: 0.025 + Math.random() * 0.035, r: 30 + Math.random() * 50, a: 0.08 + Math.random() * 0.1 });
    buildBase(); initLive(); clampCam();
  }
  let resizeTimer = null;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 120); });

  return {
    setNodes(list) { nodes = list.map((n, i) => Object.assign({ phase: i * 0.9 }, n)); if (W) buildBase(); },
    setAttention(id, on) { const n = nodes.find(n => n.id === id); if (n) n.attention = !!on; },
    bindLabel(id, el) { const n = nodes.find(n => n.id === id); if (n) n.label = el; },
    focus(id, zoom) {
      focusId = id; const n = nodes.find(n => n.id === id);
      if (n) { const [x, y] = nodePos(n); cam.tx = x; cam.ty = y; cam.tz = zoom || 1.35; }
      else { cam.tx = coreX(); cam.ty = core.y; cam.tz = zoom || 1; }
      clampCam();
    },
    reset() { focusId = null; cam.tx = 0.5; cam.ty = 0.5; cam.tz = 1; },
    veil(on) { dimTarget = on ? 1 : 0; },
    onFrame(cb) { frameCbs.push(cb); },
    isPortrait() { return portrait; },
    start() { resize(); if (!reduceMotion) { cam.z = 1.22; cam.tz = 1; } requestAnimationFrame(frame); }
  };
})();
