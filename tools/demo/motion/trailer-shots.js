'use strict';
// Shot pack for the O AI cinematic trailer (films.trailer). One shot kind, `cuts`: a list of beats hard-cut or
// flipped into each other inside one scene file (no crossfades: the trailer moves at reel pace). Every beat is a
// procedural canvas or DOM picture driven from the frame clock, so the render is deterministic.
//
// Vocabulary (from the reference reels): a single eye waking in the dark; many eyes cycling at 6 fps; ink
// sketches on white (figures, arrows, blots) cut against black cinematic frames; a figure dissolving into light;
// an orb cycling single words; flipping panels. The light here is always O's white ring.
(function () {
  var S = window.__scene, L = S.lib, el = L.el, esc = L.esc;
  var shots = S.shots;
  var W = 1920, H = 1080, FL = 190; // FL: flip transition length

  function rng(seed) { seed = (seed >>> 0) || 1; return function () { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }; }
  function clamp(p) { return p < 0 ? 0 : p > 1 ? 1 : p; }
  function eo(p) { p = clamp(p); return 1 - Math.pow(1 - p, 3); }
  function ei(p) { p = clamp(p); return p * p * p; }
  function eio(p) { p = clamp(p); return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2; }
  function lerp(a, b, p) { return a + (b - a) * p; }
  function layer(parent, cls) { var d = el('div', 'tr-layer' + (cls ? ' ' + cls : '')); parent.appendChild(d); return d; }
  function canvas(parent) {
    var c = el('canvas', 'tr-canvas'); c.width = W * 2; c.height = H * 2; parent.appendChild(c);
    var ctx = c.getContext('2d'); ctx.scale(2, 2);
    return { c: c, ctx: ctx };
  }
  function text(parent, cls, s, x, y) { var t = el('div', cls, s); t.style.left = x + 'px'; t.style.top = y + 'px'; parent.appendChild(t); return t; }
  // 1-D value noise, deterministic
  function noise1(x, seed) { var i = Math.floor(x), f = x - i, a = rng(i * 7919 + seed)(), b = rng((i + 1) * 7919 + seed)(); f = f * f * (3 - 2 * f); return a + (b - a) * f; }

  // ------------------------------------------------------------------ eyes
  /**
   * One eye, centred at cx,cy, half-width R. p: { hue (deg), sat 0-1, pupil 0-1, open 0-1, seed, ink (bool: black
   * ink on white), glow 0-1, look [-1..1, -1..1] }.
   */
  function lidPath(ctx, cx, cy, R, lidH) {
    ctx.moveTo(cx - R, cy + lidH * 0.05);
    ctx.bezierCurveTo(cx - R * 0.6, cy - lidH * 1.15, cx - R * 0.1, cy - lidH * 1.25, cx + R * 0.15, cy - lidH * 1.1);
    ctx.bezierCurveTo(cx + R * 0.55, cy - lidH * 0.85, cx + R * 0.85, cy - lidH * 0.35, cx + R, cy - lidH * 0.02);
    ctx.bezierCurveTo(cx + R * 0.75, cy + lidH * 0.6, cx + R * 0.35, cy + lidH * 0.9, cx, cy + lidH * 0.88);
    ctx.bezierCurveTo(cx - R * 0.45, cy + lidH * 0.85, cx - R * 0.8, cy + lidH * 0.5, cx - R, cy + lidH * 0.05);
    ctx.closePath();
  }
  function drawEye(ctx, cx, cy, R, p) {
    var Rr = R * 0.56, ir = Rr * (0.36 + p.pupil * 0.34), seed = p.seed || 1, Rn = rng(seed);
    var lx = cx + (p.look ? p.look[0] : 0) * R * 0.18, ly = cy + (p.look ? p.look[1] : 0) * R * 0.1;
    var open = clamp(p.open == null ? 1 : p.open), lidH = R * 0.46 * open;
    ctx.save();
    // lids as a clip: upper and lower curves
    ctx.beginPath();
    lidPath(ctx, cx, cy, R, lidH);
    if (p.ink) {
      ctx.lineWidth = R * 0.028; ctx.strokeStyle = '#0a0a0a'; ctx.stroke();
      ctx.clip();
      ctx.fillStyle = '#f6f5f1'; ctx.fillRect(cx - R, cy - R, R * 2, R * 2);
      // iris: hatched ink
      ctx.save(); ctx.translate(lx, ly);
      for (var i = 0; i < 72; i++) {
        var a = i / 72 * Math.PI * 2, j = (Rn() - 0.5) * 0.08, r0 = ir * 1.02, r1 = Rr * (0.86 + Rn() * 0.14);
        ctx.beginPath(); ctx.moveTo(Math.cos(a + j) * r0, Math.sin(a + j) * r0); ctx.lineTo(Math.cos(a) * r1, Math.sin(a) * r1);
        ctx.lineWidth = R * (0.006 + Rn() * 0.01); ctx.strokeStyle = 'rgba(10,10,10,' + (0.35 + Rn() * 0.5) + ')'; ctx.stroke();
      }
      ctx.beginPath(); ctx.arc(0, 0, Rr, 0, Math.PI * 2); ctx.lineWidth = R * 0.03; ctx.strokeStyle = '#0a0a0a'; ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, ir, 0, Math.PI * 2); ctx.fillStyle = '#0a0a0a'; ctx.fill();
      ctx.beginPath(); ctx.arc(-ir * 0.4, -ir * 0.4, ir * 0.22, 0, Math.PI * 2); ctx.fillStyle = '#f6f5f1'; ctx.fill();
      ctx.restore();
      ctx.restore();
      return;
    }
    ctx.clip();
    // sclera, shaded toward the corners
    var sg = ctx.createRadialGradient(lx, ly, Rr * 0.9, cx, cy, R * 1.05);
    sg.addColorStop(0, 'rgb(178,180,172)'); sg.addColorStop(0.5, 'rgb(118,122,118)'); sg.addColorStop(1, 'rgb(18,20,20)');
    ctx.fillStyle = sg; ctx.fillRect(cx - R, cy - R, R * 2, R * 2);
    // a few faint veins
    ctx.lineWidth = 1.2;
    for (var v = 0; v < 7; v++) {
      var a0 = Rn() * Math.PI * 2, rr = R * 0.95;
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(a0) * rr, cy + Math.sin(a0) * rr * 0.6);
      ctx.quadraticCurveTo(lx + Math.cos(a0) * Rr * 1.6 + (Rn() - 0.5) * 40, ly + Math.sin(a0) * Rr * 1.2, lx + Math.cos(a0) * Rr * 1.05, ly + Math.sin(a0) * Rr * 1.05);
      ctx.strokeStyle = 'rgba(120,70,70,' + (0.12 + Rn() * 0.12) + ')'; ctx.stroke();
    }
    // iris: fibres
    ctx.save(); ctx.translate(lx, ly);
    var hue = p.hue == null ? 200 : p.hue, sat = p.sat == null ? 0.25 : p.sat;
    var base = ctx.createRadialGradient(0, 0, ir, 0, 0, Rr);
    base.addColorStop(0, 'hsl(' + hue + ',' + Math.round(sat * 100) + '%,' + 42 + '%)');
    base.addColorStop(0.75, 'hsl(' + hue + ',' + Math.round(sat * 70) + '%,' + 26 + '%)');
    base.addColorStop(1, 'hsl(' + hue + ',' + Math.round(sat * 50) + '%,' + 9 + '%)');
    ctx.beginPath(); ctx.arc(0, 0, Rr, 0, Math.PI * 2); ctx.fillStyle = base; ctx.fill();
    for (var k = 0; k < 220; k++) {
      var ang = k / 220 * Math.PI * 2 + (Rn() - 0.5) * 0.03, len = Rr * (0.55 + Rn() * 0.45), lw = 0.6 + Rn() * 1.6;
      var light = Rn();
      ctx.beginPath(); ctx.moveTo(Math.cos(ang) * ir * 0.95, Math.sin(ang) * ir * 0.95); ctx.lineTo(Math.cos(ang + 0.02) * len, Math.sin(ang + 0.02) * len);
      ctx.lineWidth = lw; ctx.strokeStyle = 'hsla(' + (hue + (Rn() - 0.5) * 30) + ',' + Math.round(sat * 100) + '%,' + Math.round(30 + light * 55) + '%,' + (0.25 + light * 0.5) + ')'; ctx.stroke();
    }
    // collarette ring and limbal ring
    ctx.beginPath(); ctx.arc(0, 0, ir * 1.5, 0, Math.PI * 2); ctx.lineWidth = Rr * 0.05; ctx.strokeStyle = 'hsla(' + hue + ',' + Math.round(sat * 100) + '%,70%,.25)'; ctx.stroke();
    ctx.beginPath(); ctx.arc(0, 0, Rr, 0, Math.PI * 2); ctx.lineWidth = Rr * 0.09; ctx.strokeStyle = 'rgba(5,6,8,.85)'; ctx.stroke();
    // pupil with soft edge
    var pg = ctx.createRadialGradient(0, 0, ir * 0.7, 0, 0, ir * 1.12);
    pg.addColorStop(0, '#000'); pg.addColorStop(0.8, '#000'); pg.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.beginPath(); ctx.arc(0, 0, ir * 1.12, 0, Math.PI * 2); ctx.fillStyle = pg; ctx.fill();
    // specular highlights
    var hg2 = ctx.createRadialGradient(-ir * 0.5, -ir * 0.55, 0, -ir * 0.5, -ir * 0.55, ir * 0.3); hg2.addColorStop(0, 'rgba(255,255,255,.85)'); hg2.addColorStop(0.5, 'rgba(255,255,255,.35)'); hg2.addColorStop(1, 'rgba(255,255,255,0)'); ctx.beginPath(); ctx.arc(-ir * 0.5, -ir * 0.55, ir * 0.3, 0, Math.PI * 2); ctx.fillStyle = hg2; ctx.fill();
    ctx.beginPath(); ctx.arc(ir * 0.6, ir * 0.5, ir * 0.1, 0, Math.PI * 2); ctx.fillStyle = 'rgba(255,255,255,.45)'; ctx.fill();
    ctx.restore();
    // lid shadow from the top
    var sh = ctx.createLinearGradient(0, cy - lidH, 0, cy - lidH * 0.3);
    sh.addColorStop(0, 'rgba(0,0,0,.92)'); sh.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = sh; ctx.fillRect(cx - R, cy - lidH * 1.3, R * 2, lidH * 1.3);
    var sh2 = ctx.createLinearGradient(0, cy + lidH * 0.4, 0, cy + lidH); sh2.addColorStop(0, 'rgba(0,0,0,0)'); sh2.addColorStop(1, 'rgba(0,0,0,.6)'); ctx.fillStyle = sh2; ctx.fillRect(cx - R, cy, R * 2, lidH);
    ctx.restore();
    // skin around the eye: dark, with the lid edge catching light
    ctx.save();
    ctx.beginPath(); ctx.rect(cx - R * 2, cy - R * 2, R * 4, R * 4);
    lidPath(ctx, cx, cy, R, lidH);
    ctx.clip('evenodd');
    var skin = ctx.createRadialGradient(cx, cy, R * 0.5, cx, cy, R * 2.2);
    skin.addColorStop(0, 'rgb(46,44,42)'); skin.addColorStop(0.5, 'rgb(22,22,22)'); skin.addColorStop(1, 'rgb(4,4,5)');
    ctx.fillStyle = skin; ctx.fillRect(cx - R * 2, cy - R * 2, R * 4, R * 4);
    ctx.restore();
    // lid edge highlight
    ctx.beginPath(); ctx.moveTo(cx - R, cy + lidH * 0.05); ctx.bezierCurveTo(cx - R * 0.6, cy - lidH * 1.15, cx - R * 0.1, cy - lidH * 1.25, cx + R * 0.15, cy - lidH * 1.1); ctx.bezierCurveTo(cx + R * 0.55, cy - lidH * 0.85, cx + R * 0.85, cy - lidH * 0.35, cx + R, cy - lidH * 0.02);
    ctx.lineWidth = 4; ctx.strokeStyle = 'rgba(255,255,255,' + (0.14 * open) + ')'; ctx.stroke();
    // lashes along the upper lid
    if (!p.ink) { var Rl = rng(seed + 3); for (var li = 0; li < 46; li++) { var u = 0.08 + li / 46 * 0.86; var ex = cx - R + u * 2 * R, ey = cy - lidH * (1.1 - Math.pow(Math.abs(u - 0.55) * 2.2, 2) * 0.9); var ln = R * (0.05 + Rl() * 0.07) * open; ctx.beginPath(); ctx.moveTo(ex, ey); ctx.quadraticCurveTo(ex + (u - 0.5) * 30, ey - ln * 0.7, ex + (u - 0.5) * 60, ey - ln); ctx.lineWidth = 1.5 + Rl() * 2; ctx.strokeStyle = 'rgba(0,0,0,' + (0.6 + Rl() * 0.4) + ')'; ctx.stroke(); } }
    // grade: cool teal cast over everything, then grain
    ctx.fillStyle = 'rgba(30,70,74,.14)'; ctx.fillRect(0, 0, W, H);
    grain(ctx, seed, 0.06);
    // glow from the light the eye is looking at
    if (p.glow) {
      var gl = ctx.createRadialGradient(lx, ly, 0, lx, ly, R * 1.6);
      gl.addColorStop(0, 'rgba(255,255,255,' + (0.55 * p.glow) + ')'); gl.addColorStop(0.35, 'rgba(255,255,255,' + (0.16 * p.glow) + ')'); gl.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = gl; ctx.fillRect(0, 0, W, H);
    }
  }
  var grainCv = null;
  function grain(ctx, seed, a) {
    if (!grainCv) { grainCv = document.createElement('canvas'); grainCv.width = 480; grainCv.height = 270; var g = grainCv.getContext('2d'), im = g.createImageData(480, 270), R = rng(99); for (var i = 0; i < im.data.length; i += 4) { var v = 128 + (R() - 0.5) * 255; im.data[i] = im.data[i + 1] = im.data[i + 2] = v; im.data[i + 3] = 255; } g.putImageData(im, 0, 0); }
    ctx.save(); ctx.globalAlpha = a; ctx.globalCompositeOperation = 'overlay'; var o = (seed * 37) % 100; ctx.drawImage(grainCv, -o, -o, W + 200, H + 200); ctx.restore();
  }
  var EYES = [
    { hue: 205, sat: 0.22, pupil: 0.35, seed: 11, look: [0.1, 0] }, { hue: 38, sat: 0.45, pupil: 0.5, seed: 23, look: [-0.3, 0.1] },
    { hue: 160, sat: 0.3, pupil: 0.3, seed: 37, look: [0.2, -0.1] }, { ink: true, pupil: 0.45, seed: 41 },
    { hue: 215, sat: 0.55, pupil: 0.6, seed: 53, look: [0, 0.2] }, { hue: 25, sat: 0.3, pupil: 0.25, seed: 67, look: [-0.2, 0] },
    { ink: true, pupil: 0.3, seed: 71 }, { hue: 190, sat: 0.18, pupil: 0.7, seed: 83, look: [0.3, 0.1] },
    { hue: 90, sat: 0.2, pupil: 0.4, seed: 97, look: [-0.1, -0.2] }, { hue: 0, sat: 0.0, pupil: 0.5, seed: 101, look: [0, 0] },
    { hue: 230, sat: 0.4, pupil: 0.35, seed: 113, look: [0.15, 0.05] }, { ink: true, pupil: 0.6, seed: 127 },
  ];

  // ------------------------------------------------------------------ ink
  function inkFigure(ctx, x, y, h, Rn, pose) {
    // a filled ink silhouette, boiling slightly; pose: 'stand' | 'wait' | 'reach' (one arm up toward the light)
    var j = function (k) { return (Rn() - 0.5) * k; };
    ctx.save(); ctx.translate(j(4), j(3)); ctx.fillStyle = '#0a0a0a';
    silhouette(ctx, x, y, h, '#0a0a0a', 0);
    if (pose === 'reach') { ctx.lineCap = 'round'; ctx.lineWidth = h * 0.05; ctx.strokeStyle = '#0a0a0a'; ctx.beginPath(); ctx.moveTo(x + h * 0.12, y - h * 0.74); ctx.lineTo(x + h * 0.3 + j(6), y - h * 1.02 + j(6)); ctx.stroke(); }
    if (pose === 'wait') { ctx.lineCap = 'round'; ctx.lineWidth = h * 0.05; ctx.strokeStyle = '#0a0a0a'; ctx.beginPath(); ctx.moveTo(x - h * 0.14, y - h * 0.7); ctx.lineTo(x - h * 0.02, y - h * 0.52); ctx.lineTo(x + h * 0.14, y - h * 0.7); ctx.stroke(); }
    ctx.restore(); return;
    ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#0a0a0a'; ctx.fillStyle = '#0a0a0a';
    var hr = h * 0.11, neck = y - h + hr * 2.2;
    ctx.lineWidth = h * 0.045 + j(2);
    ctx.beginPath(); ctx.ellipse(x + j(3), y - h + hr + j(3), hr + j(2), hr * 1.05 + j(2), j(0.3), 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.moveTo(x + j(3), neck); ctx.lineTo(x + j(4), y - h * 0.48 + j(3)); ctx.stroke();
    var hip = y - h * 0.48;
    ctx.beginPath(); ctx.moveTo(x, hip); ctx.lineTo(x - h * 0.14 + j(5), y + j(3)); ctx.moveTo(x, hip); ctx.lineTo(x + h * 0.14 + j(5), y + j(3)); ctx.stroke();
    var sh = neck + h * 0.06;
    if (pose === 'reach') {
      ctx.beginPath(); ctx.moveTo(x, sh); ctx.lineTo(x + h * 0.3 + j(5), sh - h * 0.3 + j(5)); ctx.moveTo(x, sh); ctx.lineTo(x - h * 0.12 + j(5), sh + h * 0.3 + j(4)); ctx.stroke();
    } else if (pose === 'wait') {
      ctx.beginPath(); ctx.moveTo(x, sh); ctx.lineTo(x - h * 0.16 + j(4), sh + h * 0.28 + j(4)); ctx.lineTo(x + h * 0.02, sh + h * 0.2); ctx.moveTo(x, sh); ctx.lineTo(x + h * 0.16 + j(4), sh + h * 0.28 + j(4)); ctx.lineTo(x - h * 0.02, sh + h * 0.2); ctx.stroke();
    } else {
      ctx.beginPath(); ctx.moveTo(x, sh); ctx.lineTo(x - h * 0.1 + j(4), sh + h * 0.32 + j(4)); ctx.moveTo(x, sh); ctx.lineTo(x + h * 0.1 + j(4), sh + h * 0.32 + j(4)); ctx.stroke();
    }
  }
  function inkArrow(ctx, x, y, len, ang, Rn) {
    var j = function (k) { return (Rn() - 0.5) * k; };
    ctx.save(); ctx.translate(x, y); ctx.rotate(ang);
    ctx.lineWidth = 3 + j(1); ctx.strokeStyle = '#0a0a0a'; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(0, len / 2 + j(3)); ctx.lineTo(0, -len / 2 + j(3)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(-len * 0.18 + j(3), -len / 2 + len * 0.22); ctx.lineTo(0, -len / 2); ctx.lineTo(len * 0.18 + j(3), -len / 2 + len * 0.22); ctx.stroke();
    ctx.restore();
  }
  function inkBlot(ctx, x, y, r, Rn, alpha) {
    ctx.save(); ctx.globalAlpha = alpha == null ? 1 : alpha; ctx.fillStyle = '#0a0a0a';
    ctx.beginPath();
    for (var i = 0; i <= 24; i++) { var a = i / 24 * Math.PI * 2, rr = r * (0.75 + noise1(i * 0.9, Math.floor(x + y)) * 0.5); var px = x + Math.cos(a) * rr, py = y + Math.sin(a) * rr * 0.9; if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py); }
    ctx.closePath(); ctx.fill();
    for (var d = 0; d < 9; d++) { var da = Rn() * Math.PI * 2, dr = r * (1.1 + Rn() * 0.8); ctx.beginPath(); ctx.arc(x + Math.cos(da) * dr, y + Math.sin(da) * dr, 1.5 + Rn() * 5, 0, Math.PI * 2); ctx.fill(); }
    ctx.restore();
  }
  function paper(ctx) { ctx.fillStyle = '#f4f3ef'; ctx.fillRect(0, 0, W, H); var g = ctx.createRadialGradient(960, 540, 300, 960, 540, 1300); g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, 'rgba(0,0,0,.18)'); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H); }

  // ------------------------------------------------------------------ the O ring (the light)
  function ring(ctx, cx, cy, r, a, glow) {
    if (glow) { var g = ctx.createRadialGradient(cx, cy, r * 0.6, cx, cy, r * (3.2 + glow * 2)); g.addColorStop(0, 'rgba(255,255,255,' + (0.35 * glow * a) + ')'); g.addColorStop(0.3, 'rgba(255,255,255,' + (0.08 * glow * a) + ')'); g.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H); }
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.lineWidth = Math.max(2, r * 0.11); ctx.strokeStyle = 'rgba(255,255,255,' + a + ')'; ctx.stroke();
  }

  // ------------------------------------------------------------------ silhouettes + particle dissolve
  function bodyPoints(cx, baseY, h, n, seed) {
    // sample points inside a standing human silhouette (head + torso + legs), deterministic
    var R = rng(seed), pts = [];
    while (pts.length < n) {
      var u = R(), v = R(), x = (u - 0.5) * h * 0.46, y = -v * h; // y up from the feet
      var inside = false, hy = -h + h * 0.085;
      if (Math.hypot(x, y - hy) < h * 0.085) inside = true;                                         // head
      else if (y < -h * 0.42 && y > -h * 0.83 && Math.abs(x) < h * (0.12 + 0.06 * Math.sin((-y - h * 0.42) / (h * 0.41) * Math.PI))) inside = true; // torso
      else if (y >= -h * 0.42 && Math.abs(x) > h * 0.025 && Math.abs(x) < h * 0.1) inside = true;    // legs
      else if (y < -h * 0.45 && y > -h * 0.8 && Math.abs(x) > h * 0.11 && Math.abs(x) < h * 0.17) inside = true; // arms
      if (inside) pts.push({ x: cx + x, y: baseY + y, s: R(), d: R() });
    }
    return pts;
  }

  // ------------------------------------------------------------------ beats
  var beats = {};
  // 1. a single eye opens in the dark and the light rises in it
  beats.wake = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx;
    var cap = text(lay, 'tr-cap', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 880) + 'px'; cap.style.opacity = '0';
    return function (ms) {
      var p = ms / dur;
      ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H);
      var open = eo(clamp((ms - 300) / 1500)) * (1 - 0.1 * Math.max(0, Math.sin(ms / 240 - 2) - 0.9) * 10);
      var zoom = 1 + p * 0.18;
      ctx.save(); ctx.translate(960, 560); ctx.scale(zoom, zoom); ctx.translate(-960, -560);
      drawEye(ctx, 960, 560, 900, { hue: 192, sat: 0.18, pupil: lerp(0.75, 0.3, eo(clamp((ms - 900) / 1600))), open: open, seed: 5, glow: eo(clamp((ms - 1200) / 1800)) * 0.8, look: [0.05, 0] });
      ctx.restore();
      cap.style.opacity = String(eo(clamp((ms - (o.textAt || 1800)) / 500)));
      // grade: deep shadow vignette
      var vg = ctx.createRadialGradient(960, 540, 400, 960, 540, 1200); vg.addColorStop(0, 'rgba(0,0,0,0)'); vg.addColorStop(1, 'rgba(0,0,0,.8)'); ctx.fillStyle = vg; ctx.fillRect(0, 0, W, H);
    };
  };
  // 2. many eyes, one every `each` ms, with a slow push-in; ink frames flash white
  beats.eyes = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, each = o.each || 170;
    var cap = text(lay, 'tr-cap', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 880) + 'px'; cap.style.opacity = '0';
    var last = -1;
    return function (ms) {
      var i = Math.floor(ms / each), e = EYES[(i + (o.offset || 0)) % EYES.length];
      if (i !== last) {
        last = i;
        if (e.ink) paper(ctx); else { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); }
        var z = 1 + (i % 3) * 0.12, R = 820 * z, R2 = rng(i * 31 + 7);
        var cx = 960 + (R2() - 0.5) * 120, cy = 560 + (R2() - 0.5) * 60;
        drawEye(ctx, cx, cy, R, Object.assign({}, e, { open: 0.85 + R2() * 0.15, glow: e.ink ? 0 : 0.35 + (o.glow || 0) }));
        if (!e.ink) { var vg = ctx.createRadialGradient(960, 540, 420, 960, 540, 1250); vg.addColorStop(0, 'rgba(0,0,0,0)'); vg.addColorStop(1, 'rgba(0,0,0,.78)'); ctx.fillStyle = vg; ctx.fillRect(0, 0, W, H); }
      }
      if (o.text) cap.style.opacity = String(ms > (o.textAt || 0) ? 1 : 0);
      if (o.text && e.ink) cap.style.color = '#0a0a0a'; else cap.style.color = '';
    };
  };
  // 3. ink page: figures waiting, arrows, blots growing until the page is black
  beats.inkWait = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, last = -1;
    var cap = text(lay, 'tr-cap tr-ink', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 150) + 'px';
    var figs = [[300, 'wait'], [560, 'stand'], [820, 'wait'], [1100, 'stand'], [1360, 'wait'], [1620, 'stand']];
    return function (ms) {
      var f = Math.floor(ms / 100); // boils at 10 fps
      if (f === last) return; last = f;
      var Rn = rng(f * 977 + 3);
      paper(ctx);
      var ink = eo(clamp((ms - (o.blotAt || 1200)) / 1200));
      figs.forEach(function (fg, i) { inkFigure(ctx, fg[0] + (Rn() - 0.5) * 4, 760 + (i % 2) * 40, 260, Rn, fg[1]); });
      for (var a = 0; a < 7; a++) inkArrow(ctx, 200 + a * 260 + (Rn() - 0.5) * 20, 330 + (a % 2) * 60, 90, Math.PI * (0.9 + 0.2 * Rn()), Rn);
      // blots swallow the page
      if (ink > 0) { for (var b = 0; b < 16; b++) { var R2 = rng(b * 19 + 5); var bs = clamp((ink - R2() * 0.5) / 0.5); if (bs > 0) inkBlot(ctx, 100 + R2() * 1720, 120 + R2() * 840, eo(bs) * (90 + R2() * 260), Rn, 1); } }
      cap.style.opacity = String(ms > (o.textAt || 150) ? Math.max(0, 1 - ink * 1.2) : 0);
    };
  };
  // 4. silhouettes before glowing panels (the hall); one walks forward
  beats.hall = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx;
    var cap = text(lay, 'tr-cap', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 880) + 'px'; cap.style.opacity = '0';
    var Rn0 = rng(9), rows = [];
    for (var r = 0; r < 3; r++) for (var c = 0; c < 9 + r * 2; c++) rows.push({ r: r, x: 960 + (c - (4 + r)) * (200 - r * 50) + (Rn0() - 0.5) * 20, h: 300 - r * 80, y: 900 - r * 150 });
    return function (ms) {
      var p = ms / dur;
      ctx.fillStyle = '#040405'; ctx.fillRect(0, 0, W, H);
      // ceiling panels: a long row of lights far above
      for (var i = 0; i < 9; i++) { var lx = 160 + i * 200, pulse = 0.5 + 0.5 * noise1(ms / 400 + i, 77); var g = ctx.createRadialGradient(lx, 90, 0, lx, 90, 260); g.addColorStop(0, 'rgba(255,255,255,' + (0.5 * pulse) + ')'); g.addColorStop(0.08, 'rgba(255,255,255,' + (0.14 * pulse) + ')'); g.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = g; ctx.fillRect(0, 0, W, 500); ctx.fillStyle = 'rgba(255,255,255,' + (0.85 * pulse) + ')'; ctx.fillRect(lx - 60, 86, 120, 6); }
      // floor sheen
      var fl = ctx.createLinearGradient(0, 600, 0, 1080); fl.addColorStop(0, 'rgba(255,255,255,0)'); fl.addColorStop(1, 'rgba(255,255,255,.07)'); ctx.fillStyle = fl; ctx.fillRect(0, 600, W, 480);
      // the crowd, back rows first, all facing away, rim-lit from above
      var zoom = 1 + p * 0.1;
      ctx.save(); ctx.translate(960, 760); ctx.scale(zoom, zoom); ctx.translate(-960, -760);
      rows.slice().sort(function (a, b) { return b.r - a.r; }).forEach(function (f) {
        var k = f.r === 0 ? 1 : 0.55 / f.r;
        silhouette(ctx, f.x, f.y, f.h, 'rgba(' + Math.round(14 * k) + ',' + Math.round(14 * k) + ',' + Math.round(16 * k) + ',1)', 0.55 * k);
      });
      ctx.restore();
      // one figure (front, centre) steps forward and grows: the walk
      var walk = eo(clamp((ms - (o.walkAt || 900)) / (dur - (o.walkAt || 900))));
      silhouette(ctx, 960, 980 + walk * 120, 330 + walk * 220, '#000', 0.5 + walk * 0.3);
      cap.style.opacity = String(eo(clamp((ms - (o.textAt || 600)) / 500)));
    };
  };
  function silhouettePath(ctx, x, baseY, h) {
    // head, neck, rounded shoulders, a waist, two legs slightly apart
    ctx.arc(x, baseY - h + h * 0.08, h * 0.08, 0, Math.PI * 2);
    ctx.moveTo(x - h * 0.05, baseY - h * 0.84);
    ctx.lineTo(x - h * 0.05, baseY - h * 0.8);
    ctx.bezierCurveTo(x - h * 0.2, baseY - h * 0.79, x - h * 0.22, baseY - h * 0.7, x - h * 0.2, baseY - h * 0.6);
    ctx.lineTo(x - h * 0.17, baseY - h * 0.45); ctx.lineTo(x - h * 0.13, baseY - h * 0.45);
    ctx.lineTo(x - h * 0.12, baseY - h * 0.2); ctx.lineTo(x - h * 0.1, baseY); ctx.lineTo(x - h * 0.03, baseY);
    ctx.lineTo(x - h * 0.015, baseY - h * 0.4); ctx.lineTo(x + h * 0.015, baseY - h * 0.4);
    ctx.lineTo(x + h * 0.03, baseY); ctx.lineTo(x + h * 0.1, baseY); ctx.lineTo(x + h * 0.12, baseY - h * 0.2);
    ctx.lineTo(x + h * 0.13, baseY - h * 0.45); ctx.lineTo(x + h * 0.17, baseY - h * 0.45);
    ctx.lineTo(x + h * 0.2, baseY - h * 0.6);
    ctx.bezierCurveTo(x + h * 0.22, baseY - h * 0.7, x + h * 0.2, baseY - h * 0.79, x + h * 0.05, baseY - h * 0.8);
    ctx.lineTo(x + h * 0.05, baseY - h * 0.84); ctx.closePath();
  }
  function silhouette(ctx, x, baseY, h, fill, rim) {
    ctx.save(); ctx.fillStyle = fill;
    ctx.beginPath(); silhouettePath(ctx, x, baseY, h); ctx.fill();
    if (rim) { ctx.lineWidth = 2; ctx.strokeStyle = 'rgba(255,255,255,' + rim + ')'; ctx.beginPath(); ctx.arc(x, baseY - h + h * 0.08, h * 0.08, Math.PI * 1.15, Math.PI * 1.85); ctx.stroke(); ctx.beginPath(); ctx.moveTo(x - h * 0.05, baseY - h * 0.8); ctx.bezierCurveTo(x - h * 0.2, baseY - h * 0.79, x - h * 0.22, baseY - h * 0.7, x - h * 0.2, baseY - h * 0.6); ctx.moveTo(x + h * 0.05, baseY - h * 0.8); ctx.bezierCurveTo(x + h * 0.2, baseY - h * 0.79, x + h * 0.22, baseY - h * 0.7, x + h * 0.2, baseY - h * 0.6); ctx.stroke(); }
    ctx.restore();
  }
  // 5. the figure dissolves into light; the particles gather into the ring
  beats.dissolve = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, pts = bodyPoints(960, 1100, 760, 2600, 21);
    var cap = text(lay, 'tr-claim', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 470) + 'px'; cap.style.opacity = '0';
    var G = o.gather || 1700, E = o.end || 3000;
    return function (ms) {
      ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H);
      var burn = clamp(ms / G); // the body ignites bottom-up, particles leave
      var gather = eo(clamp((ms - G) / (E - G)));
      // body as a glowing silhouette, consumed from the feet up
      var line = 1100 - burn * 860;
      // the beach: a faint horizon and a wet-sand reflection
      ctx.fillStyle = 'rgba(255,255,255,.04)'; ctx.fillRect(0, 760, W, 320);
      ctx.save(); ctx.beginPath(); ctx.rect(0, 0, W, line); ctx.clip();
      var gl = ctx.createRadialGradient(960, 700, 20, 960, 700, 640); gl.addColorStop(0, 'rgba(255,255,255,' + (0.55 * burn + 0.12) + ')'); gl.addColorStop(0.4, 'rgba(255,255,255,' + (0.14 * burn + 0.03) + ')'); gl.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = gl; ctx.fillRect(0, 0, W, H);
      ctx.shadowColor = 'rgba(255,255,255,.95)'; ctx.shadowBlur = 40 + burn * 80;
      silhouette(ctx, 960, 1100, 760, 'rgba(255,255,255,' + (0.75 + 0.25 * burn) + ')', 0);
      ctx.shadowBlur = 0;
      ctx.restore();
      // burn line: a bright band where the body turns to light
      if (burn > 0 && burn < 1) { var bl = ctx.createLinearGradient(0, line - 60, 0, line + 30); bl.addColorStop(0, 'rgba(255,255,255,0)'); bl.addColorStop(0.7, 'rgba(255,255,255,.9)'); bl.addColorStop(1, 'rgba(255,255,255,0)'); ctx.save(); ctx.beginPath(); silhouettePath(ctx, 960, 1100, 760); ctx.clip(); ctx.fillStyle = bl; ctx.fillRect(0, line - 60, W, 90); ctx.restore(); }
      // reflection
      ctx.save(); ctx.globalAlpha = 0.18 * (1 - gather); ctx.translate(0, 2200); ctx.scale(1, -1); ctx.filter = 'blur(6px)'; ctx.beginPath(); ctx.rect(0, 0, W, line); ctx.clip(); silhouette(ctx, 960, 1100, 760, '#fff', 0); ctx.restore();
      // particles
      pts.forEach(function (q) {
        var birth = (1100 - q.y) / 820 * G * 0.9 + q.d * G * 0.1; // leaves when the burn line passes
        if (ms < birth) return;
        var life = (ms - birth) / 1000, rise = life * (90 + q.s * 160), drift = Math.sin(life * 3 + q.s * 10) * 40 * life;
        var x = q.x + drift, y = q.y - rise, al = 0.9;
        if (gather > 0) { var ang = q.s * Math.PI * 2, tx = 960 + Math.cos(ang) * 120, ty = (o.ringY || 520) + Math.sin(ang) * 120; x = lerp(x, tx, gather); y = lerp(y, ty, gather); al = 1 - gather * 0.7; }
        var sz = 1.5 + q.s * 2.5;
        ctx.fillStyle = 'rgba(255,255,255,' + al + ')'; ctx.fillRect(x, y, sz, sz);
      });
      if (gather > 0) ring(ctx, 960, o.ringY || 520, 120, gather, gather);
      cap.style.opacity = String(eo(clamp((ms - (o.textAt || 2600)) / 500)));
    };
  };
  // 6. the command bar: capsule buds into the bar, the task is typed, the bar answers
  beats.bar = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx;
    var hero = el('div', 'tr-hero'); lay.appendChild(hero); var lay0 = lay; lay = hero;
    var cap = el('div', 'tr-capsule'); var dot = el('div', 'tr-dot'); var status = el('div', 'tr-status', 'Ready'); cap.append(dot, status); lay.appendChild(cap);
    var bar = el('div', 'tr-bar'); var inp = el('div', 'tr-input'); var ph = el('span', 'tr-ph', 'Give O a task'); var typed = el('span', 'tr-typed'); var caret = el('span', 'tr-caret'); inp.append(ph, typed, caret); bar.appendChild(inp); lay.appendChild(bar);
    var hint = el('div', 'tr-hint', 'Enter · Esc'); bar.appendChild(hint);
    var kb = el('div', 'tr-kbd', '<span>⌘</span><span>Space</span>'); lay.appendChild(kb);
    var OPEN = o.openAt || 700, TYPE = OPEN + 500, cps = o.cps || 46, prompt = o.prompt || '';
    var typeEnd = TYPE + prompt.length / cps * 1000;
    return function (ms) {
      ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H);
      var hg = ctx.createRadialGradient(960, 440, 0, 960, 440, 900); hg.addColorStop(0, 'rgba(255,255,255,.08)'); hg.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = hg; ctx.fillRect(0, 0, W, H);
      var op = eo(clamp((ms - OPEN) / 420));
      kb.style.opacity = String(ms < OPEN ? eo(clamp(ms / 300)) : 1 - op);
      // the capsule sits top-centre, then the bar buds down out of it
      cap.style.opacity = '1';
      bar.style.opacity = String(op); bar.style.transform = 'translateX(-50%) translateY(' + lerp(-30, 0, op) + 'px) scaleY(' + lerp(0.2, 1, op) + ')';
      var n = ms < TYPE ? 0 : Math.min(prompt.length, Math.floor((ms - TYPE) / 1000 * cps));
      typed.textContent = prompt.slice(0, n); ph.style.opacity = n ? '0' : '1'; caret.style.opacity = (ms < typeEnd || Math.floor(ms / 500) % 2 === 0) && op > 0.5 ? '1' : '0';
      var sent = ms > typeEnd + 450;
      status.textContent = sent ? (o.status || 'Working') : 'Ready'; dot.style.boxShadow = sent ? '0 0 14px 3px rgba(255,255,255,.7)' : '0 0 6px 1px rgba(255,255,255,.35)';
      if (sent) { bar.style.opacity = String(1 - eo(clamp((ms - typeEnd - 450) / 300))); }
    };
  };
  // 7. flipping panels: what O did, each panel a glass card that flips in on X
  beats.panels = function (lay, o, dur) {
    var items = o.items || [], each = o.each || 520;
    var cv = canvas(lay), ctx = cv.ctx;
    var stage = el('div', 'tr-stage'); lay.appendChild(stage);
    var cards = items.map(function (it, i) { var c = el('div', 'tr-card'); c.innerHTML = '<div class="tr-card-k">' + esc(it[0]) + '</div><div class="tr-card-v">' + esc(it[1]) + '</div>'; c.style.left = (i % 2 ? 1000 : 240) + 'px'; c.style.top = (180 + Math.floor(i / 2) * 230) + 'px'; stage.appendChild(c); return c; });
    var cap = text(lay, 'tr-cap', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 920) + 'px'; cap.style.opacity = '0';
    return function (ms) {
      ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H);
      var g = ctx.createRadialGradient(960, 120, 0, 960, 120, 900); g.addColorStop(0, 'rgba(255,255,255,.09)'); g.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
      cards.forEach(function (c, i) {
        var t0 = i * each, p = clamp((ms - t0) / 260), q = eo(p);
        c.style.opacity = String(p > 0 ? 1 : 0);
        c.style.transform = 'perspective(1400px) rotateX(' + lerp(-92, 0, q) + 'deg) translateY(' + lerp(26, 0, q) + 'px)';
        var done = ms > t0 + each * 1.4; c.classList.toggle('done', done);
      });
      cap.style.opacity = String(eo(clamp((ms - (o.textAt || items.length * each + 200)) / 400)));
    };
  };
  // 8. the orb of words: the ring, a word inside, cycling
  beats.orb = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, words = o.words || [], each = o.each || 460;
    var w = text(lay, 'tr-word', '', 0, 0); w.style.left = '0'; w.style.right = '0'; w.style.top = (o.wordY || 470) + 'px';
    var cap = text(lay, 'tr-cap', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 900) + 'px'; cap.style.opacity = '0';
    return function (ms) {
      ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H);
      var i = Math.min(words.length - 1, Math.floor(ms / each)), p = (ms - i * each) / each;
      var breathe = 1 + 0.03 * Math.sin(ms / 420), r = (o.r || 300) * breathe;
      var last = i === words.length - 1;
      ring(ctx, 960, 540, r, 1, 0.9 + (last ? eo(clamp(p * 2)) * 1.2 : 0.2 * Math.sin(ms / 200)));
      // faint stars drifting in
      var Rn = rng(5); for (var s = 0; s < 90; s++) { var sx = Rn() * W, sy = Rn() * H, sp = 0.2 + Rn() * 0.6; var d = ((ms / 1000) * sp * 40) % 400; var dx = sx + (960 - sx) * (d / 2400), dy = sy + (540 - sy) * (d / 2400); ctx.fillStyle = 'rgba(255,255,255,' + (0.1 + Rn() * 0.3) + ')'; ctx.fillRect(dx, dy, 1.5, 1.5); }
      w.textContent = words[i] || ''; w.style.opacity = String(Math.min(eo(clamp(p / 0.25)), last ? 1 : 1 - ei(clamp((p - 0.8) / 0.2)))); w.style.transform = 'translateY(' + lerp(10, 0, eo(clamp(p / 0.3))) + 'px)';
      cap.style.opacity = String(eo(clamp((ms - (o.textAt || 2600)) / 500)));
    };
  };
  // 9. eyes again, faster, each one's pupil replaced by the ring a little more, until only the ring is left
  beats.eyesToRing = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, each = o.each || 130, last = -1;
    var cap = text(lay, 'tr-claim', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 470) + 'px'; cap.style.opacity = '0';
    var N = o.n || 10, END = N * each;
    return function (ms) {
      if (ms < END) {
        var i = Math.floor(ms / each);
        if (i !== last) {
          last = i; var e = EYES[(i * 5 + 3) % EYES.length]; var k = i / N;
          if (e.ink) paper(ctx); else { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); }
          var R = 820 * (1 + k * 0.5);
          drawEye(ctx, 960, 540, R, Object.assign({}, e, { open: 0.9, glow: e.ink ? 0 : 0.3 + k * 0.6, look: [0, 0] }));
          ring(ctx, 960, 540, R * 0.56 * (0.36 + e.pupil * 0.34) * 1.02, 0.3 + k * 0.7, k);
          if (!e.ink) { var vg = ctx.createRadialGradient(960, 540, 420, 960, 540, 1250); vg.addColorStop(0, 'rgba(0,0,0,0)'); vg.addColorStop(1, 'rgba(0,0,0,.78)'); ctx.fillStyle = vg; ctx.fillRect(0, 0, W, H); }
        }
        return;
      }
      last = -1;
      var p = eo(clamp((ms - END) / 900));
      ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H);
      ring(ctx, 960, 540, lerp(260, 150, p), 1, 1.2);
      cap.style.opacity = String(eo(clamp((ms - END - (o.textAt || 700)) / 500)));
    };
  };
  // 10. ink hands reaching toward the light (white page), the ring in the centre
  beats.reach = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, last = -1;
    var cap = text(lay, 'tr-cap tr-ink', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 900) + 'px'; cap.style.opacity = '0';
    return function (ms) {
      var f = Math.floor(ms / 100); if (f === last) return; last = f;
      var Rn = rng(f * 613 + 11), p = eo(clamp(ms / dur));
      paper(ctx);
      // figures around the page, all reaching inward
      var figs = [[220, 900, 'reach', 1], [520, 980, 'reach', 1], [1400, 980, 'reach', -1], [1700, 900, 'reach', -1], [960, 1040, 'reach', 1]];
      figs.forEach(function (fg) { ctx.save(); ctx.translate(fg[0], 0); ctx.scale(fg[3], 1); inkFigure(ctx, 0, fg[1], 300, Rn, fg[2]); ctx.restore(); });
      for (var a = 0; a < 10; a++) { var ang = a / 10 * Math.PI * 2; var rr = 420 - p * 60; inkArrow(ctx, 960 + Math.cos(ang) * rr, 480 + Math.sin(ang) * rr * 0.8, 70, ang + Math.PI / 2, Rn); }
      // the ring in ink, with a halo of hatching
      ctx.beginPath(); ctx.arc(960, 480, 150, 0, Math.PI * 2); ctx.lineWidth = 16; ctx.strokeStyle = '#0a0a0a'; ctx.stroke();
      for (var h = 0; h < 60; h++) { var ha = h / 60 * Math.PI * 2; ctx.beginPath(); ctx.moveTo(960 + Math.cos(ha) * 175, 480 + Math.sin(ha) * 175); ctx.lineTo(960 + Math.cos(ha) * (200 + Rn() * 40), 480 + Math.sin(ha) * (200 + Rn() * 40)); ctx.lineWidth = 2; ctx.strokeStyle = 'rgba(10,10,10,.7)'; ctx.stroke(); }
      cap.style.opacity = String(eo(clamp((ms - (o.textAt || 300)) / 400)));
    };
  };
  // 11. one claim line on black (serif), optional small eyebrow; used for the fast claim run
  beats.claim = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx;
    var c = text(lay, 'tr-claim', esc(o.text || ''), 0, 0); c.style.left = '0'; c.style.right = '0'; c.style.top = (o.textY || 470) + 'px';
    if (o.sub) { var s = text(lay, 'tr-cap', esc(o.sub), 0, 0); s.style.left = '0'; s.style.right = '0'; s.style.top = ((o.textY || 470) + 130) + 'px'; }
    return function (ms) {
      ctx.fillStyle = o.ink ? '#f4f3ef' : '#030304'; ctx.fillRect(0, 0, W, H);
      if (o.ink) { lay.classList.add('tr-inkpage'); } else { var g = ctx.createRadialGradient(960, 540, 0, 960, 540, 1000); g.addColorStop(0, 'rgba(255,255,255,.07)'); g.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H); }
      var p = eo(clamp(ms / 320));
      c.style.opacity = String(p); c.style.transform = 'translateY(' + lerp(16, 0, p) + 'px)';
      if (s) { var q = eo(clamp((ms - 200) / 320)); s.style.opacity = String(q); }
    };
  };
  // 12. the lockup: ring, wordmark, line
  beats.lockup = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx;
    var wm = text(lay, 'tr-wm', 'O', 0, 0); wm.style.left = '0'; wm.style.right = '0'; wm.style.top = '400px';
    var ai = text(lay, 'tr-ai', esc(o.name || 'O AI'), 0, 0); ai.style.left = '0'; ai.style.right = '0'; ai.style.top = '600px';
    var line = text(lay, 'tr-cap', esc(o.text || ''), 0, 0); line.style.left = '0'; line.style.right = '0'; line.style.top = '690px';
    return function (ms) {
      ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H);
      var p = eo(clamp(ms / 900));
      ring(ctx, 960, 470, lerp(150, 118, p), 1, 1.1 + 0.1 * Math.sin(ms / 500));
      wm.style.opacity = '0';
      ai.style.opacity = String(eo(clamp((ms - 500) / 600))); ai.style.transform = 'translateY(' + lerp(12, 0, eo(clamp((ms - 500) / 600))) + 'px)';
      line.style.opacity = String(eo(clamp((ms - 1000) / 600)));
    };
  };

  // ------------------------------------------------------------------ strobe plates (the reels' burst grammar)
  /** Plates: each draws one full frame. `inv` tells the caller to invert the canvas for the frame. */
  var plates = {
    eye: function (ctx, i, Rn) { var e = EYES[i % EYES.length]; if (e.ink) paper(ctx); else { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); } drawEye(ctx, 960 + (Rn() - 0.5) * 100, 540 + (Rn() - 0.5) * 50, 820 * (1 + (i % 3) * 0.15), Object.assign({}, e, { open: 0.85 + Rn() * 0.15, glow: e.ink ? 0 : 0.4 })); },
    arc: function (ctx, i, Rn, p) { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); var a = Math.PI * 2 * (p == null ? 0.3 + i * 0.05 : p); ctx.beginPath(); ctx.arc(960, 540, 260, -Math.PI / 2 - a / 2 - Math.PI * 0.55, -Math.PI / 2 + a / 2 - Math.PI * 0.55); ctx.lineWidth = 26; ctx.lineCap = 'round'; ctx.strokeStyle = '#fff'; ctx.shadowColor = '#fff'; ctx.shadowBlur = 40; ctx.stroke(); ctx.shadowBlur = 0; ctx.beginPath(); ctx.arc(960, 540, 5, 0, Math.PI * 2); ctx.fillStyle = '#fff'; ctx.fill(); },
    figure: function (ctx, i, Rn) { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); silhouette(ctx, 960 + (Rn() - 0.5) * 200, 1000, 700, '#fff', 0); },
    figures: function (ctx, i, Rn) { paper(ctx); for (var k = 0; k < 5; k++) inkFigure(ctx, 300 + k * 330, 900 + (k % 2) * 60, 320, Rn, k % 2 ? 'wait' : 'stand'); },
    schematic: function (ctx, i, Rn) { paper(ctx); ctx.strokeStyle = '#0a0a0a'; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(960, 540, 260, 0, Math.PI * 2); ctx.stroke(); ctx.beginPath(); ctx.arc(960, 540, 190, 0, Math.PI * 2); ctx.stroke(); for (var k = 0; k < 24; k++) { var a = k / 24 * Math.PI * 2; ctx.beginPath(); ctx.moveTo(960 + Math.cos(a) * 190, 540 + Math.sin(a) * 190); ctx.lineTo(960 + Math.cos(a) * 260, 540 + Math.sin(a) * 260); ctx.stroke(); } ctx.beginPath(); ctx.moveTo(300, 540); ctx.lineTo(1620, 540); ctx.moveTo(960, 120); ctx.lineTo(960, 960); ctx.stroke(); ctx.font = '400 18px ' + getComputedStyle(document.body).getPropertyValue('--mono'); ctx.fillStyle = '#0a0a0a'; ['r = 260', 'iris · pupil', 'fig. ' + (i + 1), 'O · self-model'].forEach(function (s, k) { ctx.fillText(s, 300 + k * 420, 100); }); for (var g = 0; g < 40; g++) { ctx.fillStyle = 'rgba(10,10,10,.25)'; ctx.fillRect(100 + g * 44, 1000, 1, 40); } },
    white: function (ctx) { ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H); },
    black: function (ctx) { ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H); },
    blue: function (ctx) { ctx.fillStyle = '#5B9DFF'; ctx.fillRect(0, 0, W, H); silhouette(ctx, 960, 1000, 700, '#030304', 0); },
    grid: function (ctx, i, Rn) { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); ctx.strokeStyle = 'rgba(255,255,255,.25)'; ctx.lineWidth = 1; for (var x = 0; x < W; x += 80) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); } for (var y = 0; y < H; y += 80) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); } for (var k = 0; k < 6; k++) { var px = 160 + Rn() * 1600, py = 100 + Rn() * 880; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.strokeRect(px - 40, py - 40, 80, 80); ctx.beginPath(); ctx.moveTo(px - 60, py); ctx.lineTo(px + 60, py); ctx.moveTo(px, py - 60); ctx.lineTo(px, py + 60); ctx.stroke(); } },
    ring: function (ctx) { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); ring(ctx, 960, 540, 240, 1, 1.4); },
  };
  /**
   * A burst: o.seq is a list of [plate, frames, invert?]; each plate holds `frames` frames (30 fps). The burst
   * loops the sequence until the beat ends, so bursts can be any length.
   */
  beats.strobe = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, seq = o.seq || [['eye', 3], ['eye', 3, 1], ['white', 1], ['eye', 2]], last = -1;
    var cap = o.text ? text(lay, 'tr-cap', esc(o.text), 0, 0) : null; if (cap) { cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 880) + 'px'; cap.style.mixBlendMode = 'difference'; }
    var total = seq.reduce(function (a, s) { return a + s[1]; }, 0);
    return function (ms) {
      var f = Math.floor(ms / 1000 * 30), fi = f % total, acc = 0, k = 0;
      while (fi >= acc + seq[k][1]) { acc += seq[k][1]; k++; }
      var id = Math.floor(f / total) * seq.length + k;
      if (id !== last) {
        last = id; var s = seq[k], Rn = rng(id * 811 + 17);
        plates[s[0]](ctx, Math.floor(f / total) + k, Rn, o.progress ? clamp(ms / dur) : null);
        cv.c.style.filter = s[2] ? 'invert(1)' : '';
      }
    };
  };
  // the eclipse: an arc grows into the full ring while the frame strobes inverted every 2 frames, then blooms white
  beats.eclipse = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, GROW = o.grow || 1400, last = -1;
    var cap = text(lay, 'tr-cap', esc(o.text || ''), 0, 0); cap.style.left = '0'; cap.style.right = '0'; cap.style.top = (o.textY || 880) + 'px'; cap.style.opacity = '0'; cap.style.mixBlendMode = 'difference';
    return function (ms) {
      var f = Math.floor(ms / 1000 * 30); if (f === last) return; last = f;
      var p = eo(clamp(ms / GROW)), bloom = eo(clamp((ms - GROW) / (dur - GROW)));
      ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H);
      var r = 260 + bloom * 120, a = Math.PI * 2 * p;
      if (bloom > 0) { var g = ctx.createRadialGradient(960, 540, r * 0.2, 960, 540, r * (1.2 + bloom * 3)); g.addColorStop(0, 'rgba(255,255,255,' + bloom + ')'); g.addColorStop(0.25, 'rgba(255,255,255,' + (0.5 * bloom) + ')'); g.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H); }
      ctx.beginPath(); ctx.arc(960, 540, r, -Math.PI * 1.05 - a / 2, -Math.PI * 1.05 + a / 2); ctx.lineWidth = 26 + bloom * 40; ctx.lineCap = 'round'; ctx.strokeStyle = '#fff'; ctx.shadowColor = '#fff'; ctx.shadowBlur = 50 + bloom * 120; ctx.stroke(); ctx.shadowBlur = 0;
      // corona rays while it forms
      var Rn = rng(f * 7 + 1);
      for (var k = 0; k < 14; k++) { var ang = -Math.PI * 1.05 - a / 2 + a * Rn(); var L0 = r + 10, L1 = r + 30 + Rn() * 70; ctx.beginPath(); ctx.moveTo(960 + Math.cos(ang) * L0, 540 + Math.sin(ang) * L0); ctx.lineTo(960 + Math.cos(ang) * L1, 540 + Math.sin(ang) * L1); ctx.lineWidth = 1 + Rn() * 2; ctx.strokeStyle = 'rgba(255,255,255,' + (0.3 + Rn() * 0.5) + ')'; ctx.stroke(); }
      cv.c.style.filter = (ms < GROW && Math.floor(f / 2) % 2 === 1) ? 'invert(1)' : '';
      cap.style.opacity = String(eo(clamp((ms - (o.textAt || 300)) / 400)));
    };
  };

  // ------------------------------------------------------------------ the cut engine
  /**
   * o.beats: [{ kind, d, cut: 'hard' | 'flipX' | 'flipY' | 'flash' | 'wipe', ...opts }]. Each beat is built on its
   * own layer; the per-frame driver shows the active beat (and, during a flip, the one leaving) and runs its tick.
   */
  shots.cuts = function (sec, o, tl) {
    sec.classList.add('tr');
    var t = 0, built = [];
    (o.beats || []).forEach(function (b) {
      var lay = layer(sec); lay.style.display = 'none';
      var tick = beats[b.kind](lay, b, b.d);
      built.push({ b: b, lay: lay, tick: tick, t0: t, t1: t + b.d });
      t += b.d;
    });
    var flash = el('div', 'tr-flash'); sec.appendChild(flash);
    // nine directional blur strengths for the smear cut (0 = none … 8 = 96 px)
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('width', '0'); svg.setAttribute('height', '0'); svg.style.position = 'absolute';
    var defs = ''; for (var si = 0; si <= 8; si++) defs += '<filter id="tr-smear-' + si + '" x="-20%" y="0" width="140%" height="100%"><feGaussianBlur stdDeviation="' + (si * 12) + ' 0"/></filter>';
    svg.innerHTML = defs; sec.appendChild(svg);
    tl.fn(function (ms) {
      flash.style.opacity = '0';
      built.forEach(function (x, i) {
        var cut = x.b.cut || 'hard', inT = ms - x.t0, outT = ms - x.t1;
        var vis = ms >= x.t0 && ms < x.t1 + (cut === 'hard' ? 0 : FL);
        if (ms < x.t0 && i > 0 && cut !== 'hard' && ms >= x.t0 - FL) vis = false; // incoming starts exactly on its cut
        x.lay.style.display = vis ? 'block' : 'none';
        if (!vis) return;
        var local = Math.min(x.b.d - 1, Math.max(0, inT));
        x.tick(local);
        var tf = '';
        if (cut === 'flipX' || cut === 'flipY') {
          var ax = cut === 'flipX' ? 'rotateX' : 'rotateY';
          if (inT < FL) { var q = eo(inT / FL); tf = 'perspective(1600px) ' + ax + '(' + lerp(cut === 'flipX' ? 80 : -80, 0, q) + 'deg)'; x.lay.style.opacity = String(Math.min(1, q * 1.5)); }
          else x.lay.style.opacity = '1';
          var nxt = built[i + 1];
          if (nxt && (nxt.b.cut === 'flipX' || nxt.b.cut === 'flipY') && outT > -0.001 && outT < FL) { var qo = ei(outT / FL); var axo = nxt.b.cut === 'flipX' ? 'rotateX' : 'rotateY'; tf = 'perspective(1600px) ' + axo + '(' + lerp(0, nxt.b.cut === 'flipX' ? -80 : 80, qo) + 'deg)'; x.lay.style.opacity = String(1 - qo); x.lay.style.zIndex = '1'; } else x.lay.style.zIndex = '2';
        } else if (cut === 'flash') {
          x.lay.style.opacity = '1';
          if (inT < 140) flash.style.opacity = String(1 - inT / 140);
        } else if (cut === 'smear') {
          // whip: the incoming frame streaks in from the right, the outgoing one streaks out left (directional blur)
          if (inT < FL) { var qs = eo(inT / FL); tf = 'translateX(' + lerp(520, 0, qs) + 'px)'; x.lay.style.filter = 'url(#tr-smear-' + Math.round((1 - qs) * 8) + ')'; x.lay.style.opacity = '1'; }
          else { x.lay.style.filter = ''; x.lay.style.opacity = '1'; }
          var nx = built[i + 1];
          if (nx && nx.b.cut === 'smear' && outT > -0.001 && outT < FL) { var qo2 = ei(outT / FL); tf = 'translateX(' + lerp(0, -520, qo2) + 'px)'; x.lay.style.filter = 'url(#tr-smear-' + Math.round(qo2 * 8) + ')'; x.lay.style.zIndex = '1'; } else x.lay.style.zIndex = '2';
        } else if (cut === 'wipe') {
          var qw = eo(clamp(inT / FL)); x.lay.style.clipPath = 'inset(0 ' + ((1 - qw) * 100) + '% 0 0)'; x.lay.style.opacity = '1';
        } else { x.lay.style.opacity = '1'; x.lay.style.zIndex = '2'; }
        x.lay.style.transform = tf;
      });
    });
    return t;
  };
})();
