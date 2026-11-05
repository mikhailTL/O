/* O AI — avatar renderer.
 *
 * window.OSprite.create(canvas, { scale }) -> { draw(pose), resize(), hitTest(cssX, cssY), toCss(lx, ly), LW, LH, CX, FEET_Y, scale, colors }
 *
 * O is a black glass orb with a luminous ring (the letter O) and one highlight that
 * doubles as its eye. Pure drawing: given a pose it paints one frame; all timing and
 * personality live in behavior.js, whose pose vocabulary (written for the old
 * pixel critter) is kept and translated:
 *
 *   x, y            logical offset of the whole avatar (y < 0 = in the air)
 *   jx, jy          micro-jitter in device pixels
 *   eyeDX, eyeDY    gaze, -1..1 (the highlight slides inside the orb)
 *   blink           0..1 (>0.5 = highlight closes to a line)
 *   eyes            'normal' | 'happy' | 'x' | 'wide' | 'closed'
 *   squint          true = highlight narrows
 *   squash          -1..1 (+ = flatter/wider)
 *   tilt            radians (the ring and highlight lean with it)
 *   armL, armR      -1..1 raised arms -> the ring brightens a touch
 *   legPhase        radians; walking=true rolls the ring's seam
 *   dangle          0..1 carried: the orb stretches a little
 *   hatTilt         radians -> the ring tips (perspective squeeze); hatLift lifts the ring glow
 *   monocleGlint    0..1 a glint sweeps around the ring
 *   blush           0..1 warm tint low on the orb
 *   shadow          bool (default true)
 *   mood            idle | listening | thinking | working | browsing | success | error | sleeping
 *   particles       [{ kind, x, y, a, size }] in logical canvas coords
 *
 * Ring by mood: thinking pulses slowly, browsing sweeps a glint, working spins a bright
 * arc, listening brightens, success flashes once, error turns red, sleeping dims.
 */
(function () {
  'use strict';

  var LW = 48; // logical canvas size
  var LH = 44;
  var FEET_Y = LH - 3; // logical y where the orb touches the ground
  var CX = 24; // logical x of the avatar's centre
  var R = 11; // orb radius, logical px
  var RING = 0.72; // ring radius as a fraction of R
  var TAU = Math.PI * 2;

  var C = {
    orbHi: '#3b3b44',
    orbMid: '#17171b',
    orbLo: '#050506',
    ring: '#9fd8ff',
    ringCore: '#f2fbff',
    ringSoft: '#bfe6ff',
    ringBad: '#ff6f66',
    ringBadCore: '#ffd9d6',
    eye: '#ffffff',
    blush: '#ff9a8a',
    shadow: 'rgba(0,0,0,0.38)',
    particle: '#bfe6ff',
    particleWarm: '#ffd27a',
    heart: '#ff7d9a',
    drop: '#8ed0f5'
  };

  var MOOD_RING = {
    idle: { a: 0.78, glow: 0.55, w: 1.5 },
    listening: { a: 1, glow: 1.0, w: 1.7 },
    thinking: { a: 0.75, glow: 0.6, w: 1.5 },
    working: { a: 0.6, glow: 0.45, w: 1.4 },
    browsing: { a: 0.8, glow: 0.6, w: 1.5 },
    success: { a: 0.95, glow: 0.9, w: 1.6 },
    error: { a: 0.9, glow: 0.7, w: 1.6 },
    sleeping: { a: 0.22, glow: 0.12, w: 1.3 }
  };

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function lerp(a, b, k) { return a + (b - a) * k; }
  function mix(hexA, hexB, k) {
    var a = parseInt(hexA.slice(1), 16), b = parseInt(hexB.slice(1), 16);
    var r = Math.round(lerp(a >> 16, b >> 16, k)), g = Math.round(lerp((a >> 8) & 255, (b >> 8) & 255, k)), bl = Math.round(lerp(a & 255, b & 255, k));
    return 'rgb(' + r + ',' + g + ',' + bl + ')';
  }
  function reducedMotion() {
    try { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) { return false; }
  }

  function create(canvas, opts) {
    opts = opts || {};
    var ctx = canvas.getContext('2d');
    var dpr = 1, scale = 1, offX = 0, offY = 0;
    var moodName = 'idle', moodSince = 0;
    var eyeX = 0, eyeY = 0; // smoothed gaze
    var lastT = 0;
    var font = '-apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, "Helvetica Neue", sans-serif';

    function resize() {
      dpr = window.devicePixelRatio || 1;
      var cssW = canvas.clientWidth || canvas.width / dpr || 200;
      var cssH = canvas.clientHeight || canvas.height / dpr || 200;
      var bw = Math.round(cssW * dpr), bh = Math.round(cssH * dpr);
      if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh; }
      var fit = Math.min(cssW / LW, cssH / LH);
      var cssScale = Math.max(opts.scale || 1, fit, 1);
      scale = Math.max(1, cssScale * dpr); // device px per logical px
      offX = Math.round((bw - LW * scale) / 2);
      offY = bh - LH * scale;
      api.scale = scale;
    }

    // ---- the orb (drawn in logical units; the caller has set the transform) ----
    function drawOrb(p, ring, T) {
      var g = ctx.createRadialGradient(-R * 0.3, -R * 0.42, R * 0.1, 0, 0, R * 1.05);
      g.addColorStop(0, C.orbHi); g.addColorStop(0.5, C.orbMid); g.addColorStop(1, C.orbLo);
      ctx.beginPath(); ctx.arc(0, 0, R, 0, TAU);
      ctx.fillStyle = g; ctx.fill();
      // rim light
      var rim = ctx.createRadialGradient(0, 0, R * 0.8, 0, 0, R);
      rim.addColorStop(0, 'rgba(255,255,255,0)'); rim.addColorStop(0.92, 'rgba(255,255,255,0.16)'); rim.addColorStop(1, 'rgba(255,255,255,0.04)');
      ctx.fillStyle = rim; ctx.fill();
      // blush: warm wash low on the sphere
      if ((p.blush || 0) > 0.05) {
        ctx.save(); ctx.clip();
        ctx.globalAlpha = clamp(p.blush, 0, 1) * 0.45;
        var bg = ctx.createRadialGradient(0, R * 0.55, 0, 0, R * 0.55, R * 0.8);
        bg.addColorStop(0, C.blush); bg.addColorStop(1, 'rgba(255,154,138,0)');
        ctx.fillStyle = bg; ctx.fillRect(-R, -R, R * 2, R * 2);
        ctx.restore();
      }
      // top sheen
      ctx.save();
      ctx.beginPath(); ctx.arc(0, 0, R, 0, TAU); ctx.clip();
      var sh = ctx.createLinearGradient(0, -R, 0, -R * 0.35);
      sh.addColorStop(0, 'rgba(255,255,255,0.15)'); sh.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = sh;
      ctx.beginPath(); ctx.ellipse(0, -R * 0.64, R * 0.62, R * 0.24, 0, 0, TAU); ctx.fill();
      ctx.restore();

      // ---- ring ----
      var rr = R * RING;
      var ry = rr * (1 - 0.22 * clamp(Math.abs(p.hatTilt || 0), 0, 1)) - (p.hatLift || 0) * 0.08;
      var col = ring.red > 0 ? mix(C.ring, C.ringBad, ring.red) : C.ring;
      var core = ring.red > 0 ? mix(C.ringCore, C.ringBadCore, ring.red) : C.ringCore;
      ctx.save();
      ctx.rotate(ring.spin);
      // glow
      if (ring.glow > 0.02) {
        ctx.save();
        ctx.shadowColor = col; ctx.shadowBlur = scale * 4.5 * ring.glow;
        ctx.strokeStyle = col; ctx.globalAlpha = clamp(ring.a * 0.5 * ring.glow, 0, 1); ctx.lineWidth = ring.w * 1.9;
        ctx.beginPath(); ctx.ellipse(0, 0, rr, ry, 0, 0, TAU); ctx.stroke();
        ctx.restore();
      }
      // body of the ring
      ctx.strokeStyle = C.ringSoft; ctx.globalAlpha = clamp(ring.a * 0.9, 0, 1); ctx.lineWidth = ring.w;
      if (ring.red > 0) ctx.strokeStyle = mix(C.ringSoft, C.ringBad, ring.red);
      ctx.beginPath(); ctx.ellipse(0, 0, rr, ry, 0, 0, TAU); ctx.stroke();
      ctx.strokeStyle = core; ctx.globalAlpha = clamp(ring.a, 0, 1); ctx.lineWidth = ring.w * 0.45;
      ctx.beginPath(); ctx.ellipse(0, 0, rr, ry, 0, 0, TAU); ctx.stroke();
      // working: a brighter arc chases around the ring
      if (ring.arc) {
        ctx.save();
        ctx.shadowColor = core; ctx.shadowBlur = scale * 3;
        ctx.strokeStyle = core; ctx.globalAlpha = 1; ctx.lineWidth = ring.w * 0.9; ctx.lineCap = 'round';
        ctx.beginPath(); ctx.ellipse(0, 0, rr, ry, 0, ring.arc - 0.9, ring.arc + 0.9); ctx.stroke();
        ctx.restore();
      }
      // glint: a short bright sweep (browsing / monocle fidget)
      if (ring.glint >= 0 && ring.glint <= 1) {
        var ga = -Math.PI * 0.85 + ring.glint * TAU;
        ctx.save();
        ctx.shadowColor = '#ffffff'; ctx.shadowBlur = scale * 3;
        ctx.strokeStyle = '#ffffff'; ctx.globalAlpha = Math.sin(ring.glint * Math.PI); ctx.lineWidth = ring.w * 0.8; ctx.lineCap = 'round';
        ctx.beginPath(); ctx.ellipse(0, 0, rr, ry, 0, ga - 0.35, ga + 0.35); ctx.stroke();
        ctx.restore();
      }
      ctx.restore();

      // ---- the highlight / eye ----
      drawEye(p, ring);
    }

    function drawEye(p, ring) {
      var style = p.eyes || 'normal';
      if ((p.blink || 0) > 0.5 && style !== 'x') style = 'closed';
      var ex = -R * 0.3 + eyeX * R * 0.2, ey = -R * 0.32 + eyeY * R * 0.18;
      var a = ring.a < 0.3 ? 0.45 : 0.96; // dim with the ring when asleep
      ctx.save();
      ctx.translate(ex, ey);
      ctx.fillStyle = C.eye; ctx.strokeStyle = C.eye; ctx.lineCap = 'round';
      var rx = 2.3, ry = 1.9;
      if (p.squint) { rx = 2.7; ry = 0.9; }
      switch (style) {
        case 'closed':
          ctx.globalAlpha = a * 0.8; ctx.lineWidth = 0.9;
          ctx.beginPath(); ctx.moveTo(-2.2, 0.2); ctx.lineTo(2.2, 0.2); ctx.stroke();
          break;
        case 'happy':
          ctx.globalAlpha = a; ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.arc(0, 1.2, 2.5, Math.PI + 0.35, TAU - 0.35); ctx.stroke();
          break;
        case 'x':
          ctx.globalAlpha = a * 0.9; ctx.lineWidth = 1.1;
          ctx.beginPath(); ctx.moveTo(-1.8, -1.8); ctx.lineTo(1.8, 1.8); ctx.moveTo(1.8, -1.8); ctx.lineTo(-1.8, 1.8); ctx.stroke();
          break;
        case 'wide':
          softDot(3.1, 2.9, a);
          break;
        default:
          softDot(rx, ry, a);
      }
      ctx.restore();
    }

    function softDot(rx, ry, a) {
      ctx.save();
      ctx.shadowColor = '#ffffff'; ctx.shadowBlur = scale * 2.2; ctx.globalAlpha = a * 0.55;
      ctx.beginPath(); ctx.ellipse(0, 0, rx * 1.15, ry * 1.15, 0, 0, TAU); ctx.fill();
      ctx.restore();
      ctx.globalAlpha = a;
      ctx.beginPath(); ctx.ellipse(0, 0, rx, ry, 0, 0, TAU); ctx.fill();
    }

    // ---- particles (logical coords, drawn on the base transform) ----
    function drawParticle(q) {
      var s = scale * (q.size || 1);
      var X = offX + q.x * scale, Y = offY + q.y * scale;
      ctx.save();
      ctx.globalAlpha = clamp(q.a == null ? 1 : q.a, 0, 1);
      ctx.translate(X, Y);
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      switch (q.kind) {
        case 'z': case 'zs': case 'q': case 'bang': case 'note': {
          var txt = { z: 'z', zs: 'z', q: '?', bang: '!', note: '♪' }[q.kind];
          var px = s * (q.kind === 'zs' ? 3.4 : q.kind === 'z' ? 4.8 : 5);
          ctx.fillStyle = q.kind === 'bang' ? C.particleWarm : C.particle;
          ctx.font = '600 ' + px + 'px ' + font; ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
          ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = s * 1.5;
          ctx.fillText(txt, 0, 0);
          break;
        }
        case 'heart':
          ctx.fillStyle = C.heart; ctx.shadowColor = C.heart; ctx.shadowBlur = s * 1.5;
          ctx.beginPath();
          ctx.moveTo(0, 2.2 * s);
          ctx.bezierCurveTo(-3 * s, -0.2 * s, -1.6 * s, -2.4 * s, 0, -0.9 * s);
          ctx.bezierCurveTo(1.6 * s, -2.4 * s, 3 * s, -0.2 * s, 0, 2.2 * s);
          ctx.fill();
          break;
        case 'spark': case 'star': {
          var r1 = (q.kind === 'star' ? 1.4 : 2) * s, r2 = r1 * 0.32;
          ctx.fillStyle = q.kind === 'star' ? '#ffffff' : C.ringSoft; ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = s * 1.5;
          ctx.beginPath();
          for (var i = 0; i < 8; i++) { var rr = i % 2 ? r2 : r1, an = i * Math.PI / 4 - Math.PI / 2; ctx.lineTo(Math.cos(an) * rr, Math.sin(an) * rr); }
          ctx.closePath(); ctx.fill();
          break;
        }
        case 'drop':
          ctx.fillStyle = C.drop; ctx.shadowColor = C.drop; ctx.shadowBlur = s;
          ctx.beginPath(); ctx.moveTo(0, -1.8 * s); ctx.quadraticCurveTo(1.6 * s, 0.6 * s, 0, 1.6 * s); ctx.quadraticCurveTo(-1.6 * s, 0.6 * s, 0, -1.8 * s); ctx.fill();
          break;
        case 'puff':
          ctx.fillStyle = 'rgba(255,255,255,0.7)';
          ctx.beginPath(); ctx.arc(0, 0, 1.4 * s, 0, TAU); ctx.fill();
          break;
        default: // dot
          ctx.fillStyle = C.particle; ctx.shadowColor = C.particle; ctx.shadowBlur = s;
          ctx.beginPath(); ctx.arc(0, 0, 0.9 * s, 0, TAU); ctx.fill();
      }
      ctx.restore();
    }

    // ---- ring state from mood + pose ----
    function ringFor(p, T, now) {
      var mood = p.mood || 'idle';
      if (mood !== moodName) { moodName = mood; moodSince = now; }
      var since = (now - moodSince) / 1000;
      var base = MOOD_RING[mood] || MOOD_RING.idle;
      var still = reducedMotion();
      var ring = { a: base.a, glow: base.glow, w: base.w, spin: 0, arc: 0, glint: -1, red: 0 };
      // idle breathing on the ring (quiet)
      if (!still) ring.a += 0.05 * Math.sin(T * TAU / 3.4);
      var arms = Math.max(p.armL || 0, p.armR || 0);
      if (arms > 0) { ring.a += 0.12 * arms; ring.glow += 0.2 * arms; }
      switch (mood) {
        case 'thinking': { var u = still ? 0.5 : 0.5 + 0.5 * Math.sin(T * TAU / 2.2); ring.a = lerp(0.5, 0.95, u); ring.glow = lerp(0.3, 1.0, u); break; }
        case 'working': ring.arc = still ? Math.PI * 1.5 : (T * TAU / 1.1) % TAU; break;
        case 'success': { var f = Math.exp(-since * 2.6); ring.a = Math.min(1, 0.85 + f); ring.glow = 0.8 + 1.4 * f; ring.w += 0.5 * f; break; }
        case 'error': ring.red = Math.min(1, since * 4); break;
        case 'listening': if (!still) ring.glow += 0.15 * Math.sin(T * TAU / 1.6); break;
      }
      if ((p.monocleGlint || 0) > 0 && (p.monocleGlint || 0) < 1) ring.glint = p.monocleGlint;
      if (p.walking && !still) ring.spin = (p.legPhase || 0) * 0.35;
      ring.spin += (p.tilt || 0) * 0.5;
      return ring;
    }

    function draw(p) {
      p = p || {};
      if (!scale || canvas.width === 0) resize();
      var now = performance.now(), T = now / 1000;
      var dt = lastT ? Math.min(100, now - lastT) : 33; lastT = now;
      ctx.imageSmoothingEnabled = true;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      var s = scale;
      var bx = CX + (p.x || 0), by = FEET_Y + (p.y || 0);

      // gaze: quick but not instant, so the highlight slides like a reflection
      var k = 1 - Math.pow(0.02, dt / 1000 * 6);
      eyeX = lerp(eyeX, clamp(p.eyeDX || 0, -1, 1), k);
      eyeY = lerp(eyeY, clamp(p.eyeDY || 0, -1, 1), k);

      var ring = ringFor(p, T, now);

      // ground shadow: soft ellipse that shrinks as O rises
      if (p.shadow !== false) {
        var air = Math.max(0, -(p.y || 0));
        var sw = Math.max(5, R * 0.95 - air * 0.5), sa = clamp(0.38 - air * 0.03, 0.08, 0.38);
        ctx.save();
        ctx.translate(offX + bx * s, offY + (FEET_Y + 1.2) * s);
        var sg = ctx.createRadialGradient(0, 0, 0, 0, 0, sw * s);
        sg.addColorStop(0, 'rgba(0,0,0,' + sa + ')'); sg.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = sg;
        ctx.beginPath(); ctx.ellipse(0, 0, sw * s, sw * 0.32 * s, 0, 0, TAU); ctx.fill();
        ctx.restore();
      }

      var sq = clamp(p.squash || 0, -1, 1);
      var dangle = clamp(p.dangle || 0, 0, 1);
      var sx = (1 + 0.12 * sq) * (1 - 0.05 * dangle), sy = (1 - 0.14 * sq) * (1 + 0.1 * dangle);
      ctx.save();
      ctx.translate(offX + bx * s + (p.jx || 0), offY + by * s + (p.jy || 0));
      ctx.rotate((p.tilt || 0) * 0.6);
      ctx.scale(sx * s, sy * s);
      ctx.translate(0, -R); // orb centre sits R above the ground
      drawOrb(p, ring, T);
      ctx.restore();

      var parts = p.particles;
      if (parts) for (var i = 0; i < parts.length; i++) drawParticle(parts[i]);
    }

    function hitTest(cssX, cssY) {
      var X = Math.round(cssX * dpr), Y = Math.round(cssY * dpr);
      if (X < 0 || Y < 0 || X >= canvas.width || Y >= canvas.height) return false;
      try {
        var d = ctx.getImageData(Math.max(0, X - 2), Math.max(0, Y - 2), 5, 5).data;
        for (var i = 3; i < d.length; i += 4) if (d[i] > 60) return true;
      } catch (e) { return true; }
      return false;
    }

    // Logical -> CSS pixel helpers (for positioning particles / bubbles)
    function toCss(lx, ly) {
      return { x: (offX + lx * scale) / dpr, y: (offY + ly * scale) / dpr };
    }

    var api = {
      draw: draw, resize: resize, hitTest: hitTest, toCss: toCss,
      LW: LW, LH: LH, CX: CX, FEET_Y: FEET_Y, scale: 1, colors: C
    };
    resize();
    return api;
  }

  window.OSprite = { create: create, LW: LW, LH: LH };
})();
