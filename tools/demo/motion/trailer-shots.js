'use strict';
// Shot pack for the O AI cinematic trailer (films.trailer). One shot kind, `cuts`: a list of beats hard-cut,
// flashed, flipped or smeared into each other inside one scene file. Pictures are photographic plates
// (tools/demo/motion/plates/*.png, generated for the trailer and kept out of git) moved by camera: push-ins,
// drifts, punch zooms, polarity inversion strobes; the only drawn things are O's own surfaces (capsule, bar,
// panels, the ring) and type. Everything is a function of the frame clock, so the render is deterministic.
(function () {
  var S = window.__scene, L = S.lib, el = L.el, esc = L.esc;
  var shots = S.shots;
  var W = 1920, H = 1080, FL = 190; // FL: flip/smear transition length

  function rng(seed) { seed = (seed >>> 0) || 1; return function () { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }; }
  function clamp(p) { return p < 0 ? 0 : p > 1 ? 1 : p; }
  function eo(p) { p = clamp(p); return 1 - Math.pow(1 - p, 3); }
  function ei(p) { p = clamp(p); return p * p * p; }
  function lerp(a, b, p) { return a + (b - a) * p; }
  function layer(parent, cls) { var d = el('div', 'tr-layer' + (cls ? ' ' + cls : '')); parent.appendChild(d); return d; }
  function letterbox(sec) { var a = el('div', 'tr-box tr-box-top'), b = el('div', 'tr-box tr-box-bot'); sec.append(a, b); }
  function canvas(parent) {
    var c = el('canvas', 'tr-canvas'); c.width = W * 2; c.height = H * 2; parent.appendChild(c);
    var ctx = c.getContext('2d'); ctx.scale(2, 2);
    return { c: c, ctx: ctx };
  }
  function text(parent, cls, s, x, y) { var t = el('div', cls, s); t.style.left = x + 'px'; t.style.top = y + 'px'; parent.appendChild(t); return t; }
  function caption(lay, o, cls) {
    if (!o.text) return null;
    var c = text(lay, cls || 'tr-cap', esc(o.text), 0, 0); c.style.left = '0'; c.style.right = '0'; c.style.top = (o.textY || (cls === 'tr-claim' ? 470 : 880)) + 'px'; c.style.opacity = '0'; return c;
  }
  function showCap(c, ms, at, d) { if (c) c.style.opacity = String(eo(clamp((ms - at) / (d || 500)))); }

  // ------------------------------------------------------------------ plates
  var plates = {}, loading = [];
  function plate(name) {
    if (plates[name]) return plates[name];
    var im = new Image(); im.src = '/tools/demo/motion/plates/' + name + '.png';
    plates[name] = im;
    loading.push(new Promise(function (res) { im.onload = res; im.onerror = function () { console.error('[trailer] plate missing', name); res(); }; }));
    return im;
  }
  /**
   * Draw a plate covering the frame with a camera: cam = { z (zoom, 1 = cover), x, y (pan in frame fractions,
   * -0.5..0.5 of the overflow), rot (deg) }. Grades after: dim (0-1 multiply toward black), tint [r,g,b,a].
   */
  function drawPlate(ctx, im, cam, g) {
    if (!im || !im.naturalWidth) { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); return; }
    cam = cam || {}; g = g || {};
    var z = (cam.z || 1) * Math.max(W / im.naturalWidth, H / im.naturalHeight);
    var dw = im.naturalWidth * z, dh = im.naturalHeight * z;
    var cx = W / 2 + (cam.x || 0) * (dw - W), cy = H / 2 + (cam.y || 0) * (dh - H);
    // grade: the plates are graded into one look so none reads as a stock render. 'cine' pulls saturation down,
    // lifts contrast, crushes blacks; 'mono' is black and white; 'raw' keeps the plate.
    var grade = g.grade || 'cine';
    ctx.save();
    ctx.filter = grade === 'mono' ? 'grayscale(1) contrast(1.35) brightness(.92)' : grade === 'cine' ? 'saturate(' + (g.sat == null ? 0.42 : g.sat) + ') contrast(1.22) brightness(.9)' : 'none';
    // micro hand-held drift so a hold never sits dead still
    var hd = g.hand == null ? 1 : g.hand, t = (g.seed || 0) / 15;
    ctx.translate(cx + Math.sin(t * 1.7) * 3 * hd + Math.sin(t * 3.1) * 1.5 * hd, cy + Math.cos(t * 1.3) * 2.5 * hd);
    if (cam.rot) ctx.rotate(cam.rot * Math.PI / 180);
    if (g.split) { // chromatic split on strobe frames
      ctx.globalCompositeOperation = 'source-over'; ctx.drawImage(im, -dw / 2, -dh / 2, dw, dh);
      ctx.globalCompositeOperation = 'screen'; ctx.globalAlpha = 0.5; ctx.drawImage(im, -dw / 2 + g.split, -dh / 2, dw, dh); ctx.globalAlpha = 1;
    } else ctx.drawImage(im, -dw / 2, -dh / 2, dw, dh);
    ctx.restore();
    if (grade === 'cine') { // teal shadows, warm highlights, crushed blacks
      ctx.save(); ctx.globalCompositeOperation = 'multiply'; ctx.fillStyle = 'rgba(190,215,225,1)'; ctx.fillRect(0, 0, W, H);
      ctx.globalCompositeOperation = 'soft-light'; ctx.fillStyle = 'rgba(20,40,50,.5)'; ctx.fillRect(0, 0, W, H); ctx.restore();
    }
    if (g.dim) { ctx.fillStyle = 'rgba(0,0,0,' + g.dim + ')'; ctx.fillRect(0, 0, W, H); }
    if (g.tint) { ctx.save(); ctx.globalCompositeOperation = 'overlay'; ctx.fillStyle = 'rgba(' + g.tint.join(',') + ')'; ctx.fillRect(0, 0, W, H); ctx.restore(); }
    if (g.vignette !== 0) { var vg = ctx.createRadialGradient(W / 2, H / 2, H * 0.35, W / 2, H / 2, H * 1.05); vg.addColorStop(0, 'rgba(0,0,0,0)'); vg.addColorStop(1, 'rgba(0,0,0,' + (g.vignette == null ? 0.7 : g.vignette) + ')'); ctx.fillStyle = vg; ctx.fillRect(0, 0, W, H); }
    grain(ctx, g.seed || 1, g.grain == null ? 0.07 : g.grain);
  }
  var BOX = 132; // 2.39:1 letterbox inside the 16:9 frame
  var grainCv = null;
  function grain(ctx, seed, a) {
    if (!a) return;
    if (!grainCv) { grainCv = document.createElement('canvas'); grainCv.width = 480; grainCv.height = 270; var g = grainCv.getContext('2d'), im = g.createImageData(480, 270), R = rng(99); for (var i = 0; i < im.data.length; i += 4) { var v = 128 + (R() - 0.5) * 255; im.data[i] = im.data[i + 1] = im.data[i + 2] = v; im.data[i + 3] = 255; } g.putImageData(im, 0, 0); }
    ctx.save(); ctx.globalAlpha = a; ctx.globalCompositeOperation = 'overlay'; var o = (seed * 37) % 100; ctx.drawImage(grainCv, -o, -o, W + 200, H + 200); ctx.restore();
  }
  function ring(ctx, cx, cy, r, a, glow, lw) {
    if (glow) { var g = ctx.createRadialGradient(cx, cy, r * 0.6, cx, cy, r * (3.2 + glow * 2)); g.addColorStop(0, 'rgba(255,255,255,' + (0.35 * glow * a) + ')'); g.addColorStop(0.3, 'rgba(255,255,255,' + (0.08 * glow * a) + ')'); g.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H); }
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.lineWidth = lw || Math.max(2, r * 0.11); ctx.strokeStyle = 'rgba(255,255,255,' + a + ')'; ctx.stroke();
  }
  /** Camera interpolation: from -> to over the beat with ease-out (a slow cinematic push by default). */
  function camAt(o, ms, dur) {
    var f = o.from || { z: 1 }, t = o.to || { z: 1.12 }, p = o.linear ? clamp(ms / dur) : eo(clamp(ms / dur) * 0.98);
    return { z: lerp(f.z == null ? 1 : f.z, t.z == null ? 1 : t.z, p), x: lerp(f.x || 0, t.x || 0, p), y: lerp(f.y || 0, t.y || 0, p), rot: lerp(f.rot || 0, t.rot || 0, p) };
  }

  var beats = {};
  // A photographic hold: one plate, a camera move, optional fade from black, optional caption.
  beats.photo = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, im = plate(o.plate), cap = caption(lay, o, o.claim ? 'tr-claim' : 'tr-cap');
    return function (ms) {
      drawPlate(ctx, im, camAt(o, ms, dur), { dim: o.dim || 0, tint: o.tint, seed: Math.floor(ms / 66), vignette: o.vignette, grain: o.grain });
      if (o.fadeIn) { var a = 1 - eo(clamp(ms / o.fadeIn)); if (a > 0) { ctx.fillStyle = 'rgba(0,0,0,' + a + ')'; ctx.fillRect(0, 0, W, H); } }
      if (o.fadeOut) { var b = ei(clamp((ms - (dur - o.fadeOut)) / o.fadeOut)); if (b > 0) { ctx.fillStyle = 'rgba(0,0,0,' + b + ')'; ctx.fillRect(0, 0, W, H); } }
      if (o.ring) { var rp = eo(clamp((ms - (o.ring.at || 0)) / (o.ring.d || 800))); if (rp > 0) ring(ctx, o.ring.x || 960, o.ring.y || 540, o.ring.r || 240, rp * (o.ring.a == null ? 1 : o.ring.a), rp * (o.ring.glow == null ? 1 : o.ring.glow), o.ring.lw); }
      cv.c.style.filter = o.invertAt != null && ms >= o.invertAt ? 'invert(1)' : '';
      showCap(cap, ms, o.textAt == null ? 600 : o.textAt);
    };
  };
  // A burst: seq = [[plate|'white'|'black'|'#hex', frames, invert?, zoom?], ...], 30 fps, looping until the beat ends.
  beats.strobe = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, seq = o.seq, last = -1, cap = caption(lay, o);
    if (cap) cap.style.mixBlendMode = 'difference';
    seq.forEach(function (s) { if (s[0][0] !== '#' && s[0] !== 'white' && s[0] !== 'black') plate(s[0]); });
    var total = seq.reduce(function (a, s) { return a + s[1]; }, 0);
    return function (ms) {
      var f = Math.floor(ms / 1000 * 30), fi = f % total, acc = 0, k = 0;
      while (fi >= acc + seq[k][1]) { acc += seq[k][1]; k++; }
      var id = Math.floor(f / total) * seq.length + k;
      if (id !== last) {
        last = id; var s = seq[k], Rn = rng(id * 811 + 17);
        if (s[0] === 'white') { ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H); }
        else if (s[0] === 'black') { ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H); }
        else if (s[0][0] === '#') { ctx.fillStyle = s[0]; ctx.fillRect(0, 0, W, H); }
        else drawPlate(ctx, plates[s[0]], { z: s[3] || (1 + (id % 3) * 0.12), x: (Rn() - 0.5) * 0.3, y: (Rn() - 0.5) * 0.3 }, { seed: id, vignette: 0.5, split: 6 + (id % 3) * 4, grade: s[4] || 'cine', hand: 0 });
        cv.c.style.filter = s[2] ? 'invert(1)' : '';
      }
      if (cap) cap.style.opacity = '1';
    };
  };
  // The eclipse becomes the O: push into the real eclipse, the ring brightens on the corona, then (o.words) one word
  // at a time inside the dark disc; before the words the frame can strobe inverted while a punch-zoom lands (o.strobeMs).
  beats.eclipse = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, im = plate('eclipse'), cap = caption(lay, o), words = o.words || [], each = o.each || 420;
    var w = text(lay, 'tr-word', '', 0, 0); w.style.left = '0'; w.style.right = '0'; w.style.top = (o.wordY || 492) + 'px';
    var ST = o.strobeMs || 0, WS = o.wordsAt == null ? ST + 400 : o.wordsAt, last = -1;
    return function (ms) {
      var f = Math.floor(ms / 1000 * 30);
      var p = eo(clamp(ms / dur));
      var punch = ms < ST ? 1 + 0.5 * Math.pow(1 - ms / ST, 2) * ((Math.floor(f / 2) % 2) ? 1 : 0.6) : 1;
      drawPlate(ctx, im, { z: lerp(o.z0 || 1.0, o.z1 || 1.18, p) * punch }, { seed: f, vignette: 0.6, dim: o.dim || 0 });
      // the ring sits on the disc edge (the plate's disc is centred, radius ~ 0.21 of frame height at z = 1)
      var R = H * (o.discR || 0.205) * lerp(o.z0 || 1.0, o.z1 || 1.18, p) * punch;
      var ra = eo(clamp((ms - (o.ringAt == null ? ST : o.ringAt)) / 700));
      if (ra > 0) ring(ctx, 960, 540, R, ra, ra * 0.8, 10 + ra * 6);
      cv.c.style.filter = ms < ST && (Math.floor(f / 2) % 2 === 1) ? 'invert(1)' : '';
      if (words.length && ms >= WS) {
        var i = Math.min(words.length - 1, Math.floor((ms - WS) / each)), q = (ms - WS - i * each) / each, lastW = i === words.length - 1;
        w.textContent = words[i]; w.style.opacity = String(Math.min(eo(clamp(q / 0.25)), lastW ? 1 : 1 - ei(clamp((q - 0.8) / 0.2)))); w.style.transform = 'translateY(' + lerp(8, 0, eo(clamp(q / 0.3))) + 'px)';
      } else w.style.opacity = '0';
      showCap(cap, ms, o.textAt == null ? 300 : o.textAt);
    };
  };
  // Into the pupil: push hard into an eye plate's pupil until it fills the frame black, the ring lights at its edge.
  beats.pupil = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, im = plate(o.plate), cap = caption(lay, o, 'tr-claim');
    var u = o.u == null ? 0.5 : o.u, v = o.v == null ? 0.5 : o.v; // the pupil, as fractions of the plate
    return function (ms) {
      var p = ei(clamp(ms / (o.zoomMs || dur)));
      var z = lerp(1, o.zMax || 7, p);
      // pan so the pupil stays centred as the zoom grows (cover-fit: the plate is W*z wide at least)
      var k = z * Math.max(W / im.naturalWidth, H / im.naturalHeight), dw = im.naturalWidth * k, dh = im.naturalHeight * k;
      var x = dw > W + 1 ? -(u - 0.5) * dw / (dw - W) * p : 0, y = dh > H + 1 ? -(v - 0.5) * dh / (dh - H) * p : 0;
      drawPlate(ctx, im, { z: z, x: x, y: y }, { seed: Math.floor(ms / 66), vignette: 0.6 + 0.4 * p, dim: 0.5 * p, hand: 0.3 });
      var ra = eo(clamp((ms - (o.ringAt || 1200)) / 700));
      if (ra > 0) ring(ctx, 960, 540, lerp(600, o.ringR || 150, eo(clamp((ms - (o.ringAt || 1200)) / 900))), ra, ra, 8);
      showCap(cap, ms, o.textAt || 1800);
    };
  };

  // ------------------------------------------------------------------ O's own surfaces
  beats.bar = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, bg = o.plate ? plate(o.plate) : null;
    var hero = el('div', 'tr-hero'); lay.appendChild(hero); lay = hero;
    var cap = el('div', 'tr-capsule'); var dot = el('div', 'tr-dot'); var status = el('div', 'tr-status', 'Ready'); cap.append(dot, status); lay.appendChild(cap);
    var bar = el('div', 'tr-bar'); var inp = el('div', 'tr-input'); var ph = el('span', 'tr-ph', 'Give O a task'); var typed = el('span', 'tr-typed'); var caret = el('span', 'tr-caret'); inp.append(ph, typed, caret); bar.appendChild(inp); lay.appendChild(bar);
    var hint = el('div', 'tr-hint', 'Enter · Esc'); bar.appendChild(hint);
    var kb = el('div', 'tr-kbd', '<span>⌘</span><span>Space</span>'); lay.appendChild(kb);
    var OPEN = o.openAt || 700, TYPE = OPEN + 500, cps = o.cps || 46, prompt = o.prompt || '';
    var typeEnd = TYPE + prompt.length / cps * 1000;
    return function (ms) {
      if (bg) drawPlate(ctx, bg, { z: 1.05 + 0.04 * ms / dur }, { dim: 0.82, seed: Math.floor(ms / 66), vignette: 0.8, grain: 0.03 });
      else { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); }
      var hg = ctx.createRadialGradient(960, 440, 0, 960, 440, 900); hg.addColorStop(0, 'rgba(255,255,255,.07)'); hg.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = hg; ctx.fillRect(0, 0, W, H);
      var op = eo(clamp((ms - OPEN) / 420));
      kb.style.opacity = String(ms < OPEN ? eo(clamp(ms / 300)) : 1 - op);
      bar.style.opacity = String(op); bar.style.transform = 'translateX(-50%) translateY(' + lerp(-30, 0, op) + 'px) scaleY(' + lerp(0.2, 1, op) + ')';
      var n = ms < TYPE ? 0 : Math.min(prompt.length, Math.floor((ms - TYPE) / 1000 * cps));
      typed.textContent = prompt.slice(0, n); ph.style.opacity = n ? '0' : '1'; caret.style.opacity = (ms < typeEnd || Math.floor(ms / 500) % 2 === 0) && op > 0.5 ? '1' : '0';
      var sent = ms > typeEnd + 450;
      status.textContent = sent ? (o.status || 'Working') : 'Ready'; dot.style.boxShadow = sent ? '0 0 14px 3px rgba(255,255,255,.7)' : '0 0 6px 1px rgba(255,255,255,.35)';
      if (sent) bar.style.opacity = String(1 - eo(clamp((ms - typeEnd - 450) / 300)));
    };
  };
  beats.panels = function (lay, o, dur) {
    var items = o.items || [], each = o.each || 520, cv = canvas(lay), ctx = cv.ctx, bg = o.plate ? plate(o.plate) : null;
    var stage = el('div', 'tr-stage'); lay.appendChild(stage);
    var cards = items.map(function (it, i) { var c = el('div', 'tr-card'); c.innerHTML = '<div class="tr-card-k">' + esc(it[0]) + '</div><div class="tr-card-v">' + esc(it[1]) + '</div>'; c.style.left = (i % 2 ? 1000 : 240) + 'px'; c.style.top = (180 + Math.floor(i / 2) * 230) + 'px'; stage.appendChild(c); return c; });
    var cap = caption(lay, o); if (cap) cap.style.top = (o.textY || 920) + 'px';
    return function (ms) {
      if (bg) drawPlate(ctx, bg, { z: 1.08 + 0.04 * ms / dur }, { dim: 0.84, seed: Math.floor(ms / 66), vignette: 0.8, grain: 0.03 });
      else { ctx.fillStyle = '#030304'; ctx.fillRect(0, 0, W, H); }
      cards.forEach(function (c, i) {
        var t0 = i * each, p = clamp((ms - t0) / 260), q = eo(p);
        c.style.opacity = String(p > 0 ? 1 : 0);
        c.style.transform = 'perspective(1400px) rotateX(' + lerp(-92, 0, q) + 'deg) translateY(' + lerp(26, 0, q) + 'px)';
        c.classList.toggle('done', ms > t0 + each * 1.4);
      });
      showCap(cap, ms, o.textAt || items.length * each + 200, 400);
    };
  };
  beats.claim = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, bg = o.plate ? plate(o.plate) : null;
    var c = text(lay, 'tr-claim', esc(o.text || ''), 0, 0); c.style.left = '0'; c.style.right = '0'; c.style.top = (o.textY || 470) + 'px';
    var s = null; if (o.sub) { s = text(lay, 'tr-cap', esc(o.sub), 0, 0); s.style.left = '0'; s.style.right = '0'; s.style.top = ((o.textY || 470) + 130) + 'px'; }
    if (o.ink) lay.classList.add('tr-inkpage');
    return function (ms) {
      if (bg) drawPlate(ctx, bg, camAt(o, ms, dur), { dim: o.dim == null ? 0.55 : o.dim, seed: Math.floor(ms / 66), vignette: 0.8 });
      else { ctx.fillStyle = o.ink ? '#f4f3ef' : '#030304'; ctx.fillRect(0, 0, W, H); }
      cv.c.style.filter = o.invert ? 'invert(1)' : '';
      var p = eo(clamp(ms / 320));
      c.style.opacity = String(p); c.style.transform = 'translateY(' + lerp(16, 0, p) + 'px)';
      if (s) s.style.opacity = String(eo(clamp((ms - 200) / 320)));
    };
  };
  beats.lockup = function (lay, o, dur) {
    var cv = canvas(lay), ctx = cv.ctx, bg = plate('eclipse');
    var ai = text(lay, 'tr-ai', esc(o.name || 'O AI'), 0, 0); ai.style.left = '0'; ai.style.right = '0'; ai.style.top = '600px';
    var line = text(lay, 'tr-cap', esc(o.text || ''), 0, 0); line.style.left = '0'; line.style.right = '0'; line.style.top = '690px';
    return function (ms) {
      var p = eo(clamp(ms / 900));
      drawPlate(ctx, bg, { z: lerp(1.3, 1.22, eo(clamp(ms / dur))), y: -0.12 }, { dim: 0.55, seed: Math.floor(ms / 66), vignette: 0.85 });
      ring(ctx, 960, 470, lerp(150, 118, p), 1, 1.1 + 0.1 * Math.sin(ms / 500), 7);
      ai.style.opacity = String(eo(clamp((ms - 500) / 600))); ai.style.transform = 'translateY(' + lerp(12, 0, eo(clamp((ms - 500) / 600))) + 'px)';
      line.style.opacity = String(eo(clamp((ms - 1000) / 600)));
    };
  };

  // ------------------------------------------------------------------ the cut engine
  shots.cuts = function (sec, o, tl) {
    sec.classList.add('tr');
    var t = 0, built = [];
    (o.beats || []).forEach(function (b) {
      var lay = layer(sec); lay.style.display = 'none';
      var tick = beats[b.kind](lay, b, b.d);
      built.push({ b: b, lay: lay, tick: tick, t0: t, t1: t + b.d });
      t += b.d;
    });
    letterbox(sec);
    var flash = el('div', 'tr-flash'); sec.appendChild(flash);
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('width', '0'); svg.setAttribute('height', '0'); svg.style.position = 'absolute';
    var defs = ''; for (var si = 0; si <= 8; si++) defs += '<filter id="tr-smear-' + si + '" x="-20%" y="0" width="140%" height="100%"><feGaussianBlur stdDeviation="' + (si * 12) + ' 0"/></filter>';
    svg.innerHTML = defs; sec.appendChild(svg);
    tl.fn(function (ms) {
      flash.style.opacity = '0';
      built.forEach(function (x, i) {
        var cut = x.b.cut || 'hard', inT = ms - x.t0, outT = ms - x.t1;
        var vis = ms >= x.t0 && ms < x.t1 + (cut === 'hard' || cut === 'flash' ? 0 : FL);
        x.lay.style.display = vis ? 'block' : 'none';
        if (!vis) return;
        x.tick(Math.min(x.b.d - 1, Math.max(0, inT)));
        var tf = '', nxt = built[i + 1], ncut = nxt && (nxt.b.cut || 'hard');
        x.lay.style.filter = ''; x.lay.style.opacity = '1'; x.lay.style.zIndex = '2';
        if (cut === 'flipX' || cut === 'flipY') {
          var ax = cut === 'flipX' ? 'rotateX' : 'rotateY';
          if (inT < FL) { var q = eo(inT / FL); tf = 'perspective(1600px) ' + ax + '(' + lerp(cut === 'flipX' ? 80 : -80, 0, q) + 'deg)'; x.lay.style.opacity = String(Math.min(1, q * 1.5)); }
        } else if (cut === 'flash') {
          if (inT < 140) flash.style.opacity = String(1 - inT / 140);
        } else if (cut === 'smear') {
          if (inT < FL) { var qs = eo(inT / FL); tf = 'translateX(' + lerp(520, 0, qs) + 'px)'; x.lay.style.filter = 'url(#tr-smear-' + Math.round((1 - qs) * 8) + ')'; }
        }
        // leaving under the next beat's flip or smear
        if (nxt && outT > -0.001 && outT < FL) {
          var qo = ei(outT / FL); x.lay.style.zIndex = '1';
          if (ncut === 'flipX' || ncut === 'flipY') { tf = 'perspective(1600px) ' + (ncut === 'flipX' ? 'rotateX' : 'rotateY') + '(' + lerp(0, ncut === 'flipX' ? -80 : 80, qo) + 'deg)'; x.lay.style.opacity = String(1 - qo); }
          else if (ncut === 'smear') { tf = 'translateX(' + lerp(0, -520, qo) + 'px)'; x.lay.style.filter = 'url(#tr-smear-' + Math.round(qo * 8) + ')'; }
        }
        x.lay.style.transform = tf;
      });
    });
    return Promise.all(loading).then(function () { return t; });
  };
})();
