'use strict';
// Shot pack for the launch film v4 (films.film4). The story, UI fragments and copy are the consumer film's shots
// (film-shots.js); each *4 shot runs that shot and lays the cel FX layer (celfx.js) over the moment where O does
// something superhuman: ignition, task spawn, results landing, operating an app, replay, the goal line, the
// approval tap, headless work while away, the claims and the close. The return beat stays calm on purpose.
(function () {
  var S = window.__scene, L = S.lib, el = L.el, esc = L.esc, $ = L.$;
  var shots = S.shots, F = window.__celfx;
  var TW = F.TWOS, TH = F.THREES, put = F.put, clamp = F.clamp, eo = F.eo, quant = F.quant;

  function center(r) { return r ? [r.x + r.w / 2, r.y + r.h / 2] : null; }
  function union(rs) {
    rs = rs.filter(Boolean); if (!rs.length) return null;
    var x0 = Math.min.apply(null, rs.map(function (r) { return r.x; })), y0 = Math.min.apply(null, rs.map(function (r) { return r.y; }));
    var x1 = Math.max.apply(null, rs.map(function (r) { return r.x + r.w; })), y1 = Math.max.apply(null, rs.map(function (r) { return r.y + r.h; }));
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }
  /** Run a film-shots.js shot, then add FX with what it exported on sec._fx. */
  function over(base, fx) {
    return function (sec, o, tl) {
      sec.classList.add('fx4');
      return Promise.resolve(shots[base](sec, o, tl)).then(function (len) {
        Array.prototype.forEach.call(sec.querySelectorAll('iframe'), function (f) { try { f.contentDocument.documentElement.dataset.filmChip = 'off'; } catch (_) {} });
        fx(sec, o, tl, sec._fx || {}, len); return len;
      });
    };
  }
  /** Key FX times (file ms) for picking stills: window.__fx4marks. */
  function mark(tl, name, t) { (window.__fx4marks = window.__fx4marks || []).push({ name: name, t: Math.round(tl.start + t) }); }
  /** A small seeded flicker so a steady flame never holds the exact same size. */
  function flick(k, seed) { var r = F.rng(seed * 131 + k * 977)(); return (r - 0.5); }

  /** Rising embers around a point: deterministic particles, positions stepped. */
  function embers(o) {
    var R = F.rng(o.seed || 9), list = [];
    for (var i = 0; i < (o.n || 18); i++) list.push({ t: o.t0 + R() * (o.t1 - o.t0), dx: (R() - 0.5) * o.spread, vy: 0.12 + R() * 0.16, vx: (R() - 0.5) * 0.06, life: 500 + R() * 600, s: 5 + R() * 7 });
    return function (ctx, tq, k, Rr) {
      var c = typeof o.at === 'function' ? o.at() : o.at; if (!c) return false;
      var any = false;
      list.forEach(function (p) {
        var e = tq - p.t; if (e < 0 || e > p.life) return;
        var f = e / p.life;
        F.spark(ctx, c[0] + p.dx + p.vx * e + Math.sin(e / 90 + p.dx) * 6, c[1] - p.vy * e, p.s * (1 - f * 0.8) * (o.scale ? o.scale() : 1), k * 0.7 + p.dx);
        any = true;
      });
      return any;
    };
  }

  // ------------------------------------------------------------------ 1. the ignition (signature open)
  shots.ignite = function (sec, o, tl) {
    // black, dot grid, a dark glass orb (41 % of frame height); an ember; the cel flame erupts up out of the
    // ring to ~2x the orb's height on twos, brackets frame the eruption, it collapses back into the ring, the
    // ring stays lit with a small steady flame inside, and the orb travels to the lockup as the wordmark lands.
    sec.classList.add('fx4');
    var SZ = 440, IGN = o.ignite || 1000, COL = o.collapse || 2400, CD = 400, MOVE = o.move || 3200;
    var orbWrapEl = put(el('div', ''), 960 - SZ / 2, 660 - SZ / 2, SZ, SZ); orbWrapEl.style.transformOrigin = '50% 50%';
    sec.appendChild(orbWrapEl);
    var halo = put(el('div', 'halo'), SZ / 2 - 260, SZ / 2 - 260); orbWrapEl.appendChild(halo);
    var O = F.orb(orbWrapEl, SZ);
    var word = put(el('div', 'wm4', 'O'), 800, 344);
    var line = put(el('div', 'f-claim', esc(o.line)), 566, 592); line.style.fontSize = '60px'; line.style.whiteSpace = 'nowrap';
    sec.append(word, line);
    var orbRect = function () { return F.rectOf(O.wrap); };
    var orbC = function () { return center(orbRect()); };
    F.grid(sec, tl, { origin: orbC, t0: 150, fade: 900, alpha: 0.085, pulses: [IGN, IGN + 700, COL + CD, MOVE + 300] });

    tl.at(orbWrapEl, [{ opacity: 0 }, { opacity: 1 }], { t: 250, d: 600, ease: 'ease-out' });
    tl.at(orbWrapEl, [{ transform: 'translate(0px,0px) scale(1)' }, { transform: 'translate(-300px,-230px) scale(.5)' }], { t: MOVE, d: 950, ease: 'cubic-bezier(.75,0,.2,1)' });
    tl.at(O.ring, [{ opacity: 0.16 }, { opacity: 0.16 }], { t: 0, d: IGN });
    tl.at(O.ring, [{ opacity: 1 }, { opacity: 1 }], { t: IGN, d: 10, fill: 'forwards' });
    tl.at(O.spec, [{ opacity: 0.3 }, { opacity: 1 }], { t: IGN, d: 300 });
    tl.at(halo, [{ opacity: 0, transform: 'scale(.4)' }, { opacity: 0, transform: 'scale(.4)' }], { t: 0, d: IGN });
    tl.at(halo, [{ opacity: 0.7, transform: 'scale(1.2)' }, { opacity: 0.6, transform: 'scale(1.1)', offset: 0.6 }, { opacity: 0.5, transform: 'scale(1)' }], { t: IGN, d: COL + CD - IGN, ease: 'ease-out', fill: 'forwards' });

    var k = O.k, inBase = [SZ / 2, SZ / 2 + 0.21 * SZ];
    // the eruption: unclipped, behind the ring stroke, up to ~2x the orb's height
    var EW = 600, EH = 1000, ex = (SZ - EW) / 2, ebase = [EW / 2, EH - 200], ey = SZ / 2 + 22 + 200 - EH;
    var ER = { x: 0, y: 0, w: 0, h: 0 };
    F.flame(O.mid, tl, { x: ex, y: ey, w: EW, h: EH, pad: 90, seed: 3, step: TW,
      bloom: { layers: [{ blur: 8, alpha: 0.9, scale: 0.5 }, { blur: 26, alpha: 0.75, scale: 0.25 }] },
      params: function (tq) {
        var e = tq - IGN; if (e < 0 || tq >= COL + CD) return null;
        var st = Math.floor(e / TW + 0.001), sw, sh, I, bl = 1;
        var seq = [[0.45, 0.22, 1.8, 1.7], [1.1, 0.75, 1.65, 1.5], [1.3, 1.08, 1.55, 1.35], [1.2, 1.02, 1.45, 1.2]];
        if (st < seq.length) { sw = seq[st][0]; sh = seq[st][1]; I = seq[st][2]; bl = seq[st][3]; }
        else if (tq < COL) { var fk = flick(st, 5); sw = 1.15 + fk * 0.05; sh = 1 + fk * 0.08; I = 1.35 + fk * 0.08; }
        else { var p = eo((tq - COL) / CD); sw = 1.15 - 0.5 * p; sh = 1 - 0.55 * p; I = 1.35 - 0.3 * p; }
        return { mode: 0, I: +I.toFixed(3), base: ebase, size: [+(135 * sw).toFixed(1), +(560 * sh).toFixed(1)], rim: [4, 3], turb: 1.35, rise: 2.1, freq: 2.0, bloom: bl };
      } });
    // the steady flame inside the glass once it has collapsed back into the ring
    F.flame(O.mid, tl, { x: 0, y: 0, w: SZ, h: SZ, pad: SZ * 0.45, seed: 3, step: TW,
      bloom: { layers: [{ blur: 8, alpha: 0.9, scale: 0.5 }, { blur: 34, alpha: 0.85, scale: 0.25 }] },
      params: function (tq) {
        if (tq < COL + CD) return null;
        var t = quant(tq, TH) / 1000, fk = flick(Math.round(t * 10), 3);
        return { mode: 0, t: t, I: +(0.98 + fk * 0.1).toFixed(3), base: inBase, size: [0.2 * SZ, 0.6 * SZ * (1 + fk * 0.06)], clip: [SZ / 2, SZ / 2, 116.5 * k], rim: [3, 2.5], turb: 1.35, rise: 1.5, freq: 1.7 };
      } });

    var ink = F.ink(sec, tl);
    ink.add(function (ctx, tq, st, R) {
      if (tq < IGN - 420 || tq >= IGN) return false;
      var r = orbRect(); if (!r) return false;
      var g = (tq - (IGN - 420)) / 420;
      if (st % 2 === 0 || g > 0.6) F.spark(ctx, r.x + r.w / 2, r.y + r.h * 0.71 - 8, 10 + 22 * g, st * 0.5);
      return true;
    });
    ink.add(F.impact({ t0: IGN, at: orbC, r: 340, steps: 8 }));
    ink.add(embers({ t0: IGN + 100, t1: COL + 200, at: function () { var r = orbRect(); return r ? [r.x + r.w / 2, r.y - 200] : null; }, spread: 260, n: 22, seed: 11 }));
    ink.add(embers({ t0: COL + CD + 100, t1: MOVE + 1500, at: function () { var r = orbRect(); return r ? [r.x + r.w / 2, r.y + r.h * 0.22] : null; }, spread: 120, n: 10, seed: 12, scale: function () { var r = orbRect(); return r ? r.w / SZ : 1; } }));
    F.flash(O.wrap, tl, { t0: IGN + TW, a: 0.6, radius: 999 });
    // brackets frame the eruption, then lock onto the orb once the flame is back in the ring
    F.hud(sec, tl, { target: { x: 960 - 250, y: 70, w: 500, h: 660 + SZ / 2 - 70 }, t0: IGN + 2 * TW, t1: COL + 100, pad: 0, corner: 48 });
    F.hud(sec, tl, { target: function () { var r = orbRect(); if (!r) return null; var i = r.w * 0.05; return { x: r.x + i, y: r.y + i, w: r.w - 2 * i, h: r.h - 2 * i }; }, t0: COL + CD + 100, t1: MOVE + 900, pad: 14, corner: 34, label: 'Ready' });

    mark(tl, 'ember', IGN - 200); mark(tl, 'erupt', IGN + 2 * TW + 10); mark(tl, 'erupt-hold', IGN + 900); mark(tl, 'collapse', COL + 200); mark(tl, 'ready', COL + CD + 400); mark(tl, 'lockup', MOVE + 2000);
    tl.at(word, [{ opacity: 0, transform: 'translateX(-24px)' }, { opacity: 1, transform: 'translateX(0)' }], { t: MOVE + 550, d: 800 });
    tl.fadeIn(line, MOVE + 1050, 800, 12);
    return o.hold || 6200;
  };

  // ------------------------------------------------------------------ 2. task spawn: energy streak to each row
  shots.parallel4 = over('parallel', function (sec, o, tl, fx) {
    var btn = fx.p.doc.getElementById('btn-send');
    var from = function () { return center(F.rectOf(btn)); };
    F.grid(sec, tl, { origin: from, alpha: 0.05, t0: 0, fade: 900, pulses: fx.spawns.map(function (t) { return t - 100; }) });
    var ink = F.ink(sec, tl, { under: true });
    fx.rows.forEach(function (row, i) {
      var to = function () { var r = F.rectOf(row); return [r.x + r.w - 6, r.y + r.h / 2]; };
      mark(tl, 'streak' + i, fx.spawns[i] - 50);
      ink.add(F.streak({ t0: fx.spawns[i] - 170, dur: 230, from: from, to: to, w: 19, via: function (a, b) { return [[a[0] + 300, a[1] + 10], [b[0] + 300, b[1] - 10]]; } }));
      F.flash(row, tl, { t0: quant(fx.spawns[i] + 60, TW), a: 0.4, radius: 30 });
    });
    var done = o.tasks.map(function (t, i) { return t.doneAt != null ? fx.spawns[i] + t.doneAt / fx.rate * 1000 : null; });
    var labels = [{ t: 0, text: '2 running · parallel' }, { t: fx.spawns[2] + 200, text: '3 running · parallel' }];
    done.forEach(function (d) { if (d != null) labels.push({ t: d, text: '2 running · 1 done' }); });
    F.hud(sec, tl, { t0: quant(fx.spawns[1] + 260, TW), pad: 14, corner: 30, label: labels,
      target: function () { return union(fx.rows.filter(function (r) { return parseFloat(r.style.opacity || '0') > 0.5; }).map(F.rectOf)); } });
  });

  // ------------------------------------------------------------------ 3. results land: flash frame + impact ring
  shots.deskTable4 = over('deskTable', function (sec, o, tl, fx) {
    var ink = F.ink(sec, tl);
    F.flash(fx.card, tl, { t0: quant(330, TW), a: 0.45, radius: 36 });
    F.flash(sec, tl, { t0: quant(330, TW), a: 0.12 });
    mark(tl, 'desk-impact', fx.last + 70);
    ink.add(F.impact({ t0: quant(fx.last, TW), at: function () { return center(F.rectOf($('b', fx.time))); }, r: 130 }));
    F.hud(sec, tl, { target: fx.rows[0], t0: quant(fx.last + 250, TW), pad: 12, corner: 26, label: 'Picked from 41 reviews' });
  });
  shots.photos4 = over('photos', function (sec, o, tl, fx) {
    var ink = F.ink(sec, tl);
    ink.add(F.impact({ t0: quant(fx.lastLand, TW), at: function () { return center(F.rectOf($('b', fx.time))); }, r: 120 }));
    F.hud(sec, tl, { target: function () { return union(fx.folders.map(F.rectOf)); }, t0: quant((o.flyAt || 1300) + 150, TW), pad: 16, corner: 30,
      label: [{ t: 0, text: 'Renaming · 312 photos' }, { t: fx.lastLand, text: 'Sorted · 4 folders' }] });
  });
  shots.form4 = over('form', function (sec, o, tl, fx) {
    var ink = F.ink(sec, tl);
    var active = function (tq) {
      var f = null, idx = 0;
      fx.fields.forEach(function (x, i) { if (tq >= x.start) { f = x; idx = i; } });
      return f ? { f: f, i: idx } : null;
    };
    F.hud(sec, tl, { t0: quant(fx.fields[0].start, TW), t1: quant(fx.readyAt - 100, TW), pad: 10, corner: 22, cls: 'right',
      target: function (tq) { var a = active(tq); return a ? F.rectOf($('.box', a.f.fe)) : null; },
      label: fx.fields.map(function (x, i) { return { t: x.start, text: 'Field ' + (i + 1) + ' of ' + fx.fields.length }; }) });
    // a spark trail as the focus hops field to field
    ink.add(F.trail({ t0: fx.fields[0].start, t1: fx.readyAt, hist: 4, w: 4, pos: function (tq) {
      var a = active(tq); if (!a) return null;
      var r = F.rectOf($('.box', a.f.fe)), prev = a.i > 0 ? F.rectOf($('.box', fx.fields[a.i - 1].fe)) : r;
      var p = eo((tq - a.f.start) / 200);
      return [r.x - 10, prev.y + (r.y - prev.y) * p - 10];
    } }));
    mark(tl, 'form-field', fx.fields[3].start + 300); mark(tl, 'form-ready', fx.readyAt + 200);
    ink.add(F.impact({ t0: quant(fx.readyAt + 120, TW), at: function () { var r = F.rectOf(fx.ready); return [r.x + 30, r.y + r.h / 2]; }, r: 110 }));
    F.flash(fx.ready, tl, { t0: quant(fx.readyAt + 120, TW), a: 0.6, radius: 999 });
  });

  // ------------------------------------------------------------------ 4. apps: brackets + spark trail on O's focus ring
  shots.apps4 = over('apps', function (sec, o, tl, fx) {
    var hops = fx.hops;
    var rel = function (t) {
      var k = -1; for (var i = 0; i < hops.length; i++) if (t >= hops[i].t) k = i;
      if (k < 0) return null;
      var a = hops[k], b = k > 0 ? hops[k - 1] : a, p = k > 0 ? 1 - Math.pow(1 - clamp((t - a.t) / 380), 3.2) : 1;
      return { x: b.x + (a.x - b.x) * p, y: b.y + (a.y - b.y) * p, w: b.w + (a.w - b.w) * p, h: b.h + (a.h - b.h) * p };
    };
    var abs = function (t) { var r = rel(t); if (!r) return null; var s = F.rectOf(fx.stage); return { x: s.x + r.x, y: s.y + r.y, w: r.w, h: r.h }; };
    mark(tl, 'apps-hop', hops[1].t + 130);
    var ink = F.ink(sec, tl);
    ink.add(F.trail({ t0: hops[0].t, hist: 5, w: 5, idle: true, pos: function (t) { var r = abs(t); return r ? [r.x, r.y] : null; } }));
    ink.add(F.trail({ t0: hops[0].t, hist: 5, w: 5, pos: function (t) { var r = abs(t); return r ? [r.x + r.w, r.y + r.h] : null; } }));
    F.hud(sec, tl, { target: function (t) { return abs(t); }, t0: quant(hops[0].t + 60, TW), pad: 14, corner: 22, cls: 'edge',
      label: [{ t: 0, text: 'Typing the title' }, { t: hops[1].t, text: 'Placing 3 photos' }, { t: hops[2].t, text: 'New slide' }, { t: hops[3].t, text: 'Writing the plan' }] });
  });

  // ------------------------------------------------------------------ 5. learning: the chip is locked; the replay is a bolt
  shots.learn4 = over('learn', function (sec, o, tl, fx) {
    var ink = F.ink(sec, tl);
    ink.add(F.impact({ t0: quant(fx.tc + 120, TW), at: function () { var r = F.rectOf(fx.chip); return [r.x + 8, r.y + r.h / 2]; }, r: 70, steps: 6 }));
    F.hud(sec, tl, { target: fx.chip, t0: quant(fx.tc + 300, TW), pad: 12, corner: 20, label: 'Remembered' });
  });
  shots.replay4 = over('replay', function (sec, o, tl, fx) {
    var a = fx.lines[0], b = fx.lines[1];
    mark(tl, 'bolt', b.at + 130); mark(tl, 'replayed', b.at + 900);
    var ink = F.ink(sec, tl);
    var bar = function () { var r = F.rectOf(b.bar); return r; };
    ink.add(F.bolt({ t0: quant(b.at, TW), dur: b.dur, a: function () { var r = bar(); return [r.x - 6, r.y + r.h / 2]; }, b: function () { var r = bar(); return [r.x + r.w + 6, r.y + r.h / 2]; }, amp: 20, w: 13, n: 22, hold: 5 }));
    ink.add(F.impact({ t0: quant(b.at + b.dur + 40, TW), at: function () { return center(F.rectOf(b.word)); }, r: 100, steps: 7 }));
    F.flash(sec, tl, { t0: quant(b.at, TW), a: 0.14 });
    F.hud(sec, tl, { target: a.row, t0: quant(a.at + a.dur + 120, TW), t1: quant(b.at - 200, TW), pad: 12, corner: 24, label: 'First run · 27 s' });
    F.hud(sec, tl, { target: function () { return union([F.rectOf(b.row), F.rectOf(b.bar)]); }, t0: quant(b.at + b.dur + 160, TW), pad: 14, corner: 26, label: 'Replayed · 2 s' });
  });

  shots.goalAsk4 = over('goalAsk', function () {});

  // ------------------------------------------------------------------ 6. goal: sparklines drawn as flame lines; Approve bursts
  shots.goalView4 = over('goalView', function (sec, o, tl, fx) {
    var lastDay = o.metrics[0].values.length;
    fx.metrics.forEach(function (me, i) {
      var m = o.metrics[i], svg = $('svg', me);
      var PX = 34, PT = 84, PB = 18, SW = 300, SH = 64;
      var sr = svg.getBoundingClientRect(), mr = me.getBoundingClientRect();
      var x0 = sr.left - mr.left - PX, y0 = sr.top - mr.top - PT;
      F.flame(me, tl, { x: x0, y: y0, w: SW + 2 * PX, h: SH + PT + PB, pad: 40, seed: 20 + i, step: TW,
        bloom: { layers: [{ blur: 5, alpha: 0.8, scale: 0.5 }, { blur: 18, alpha: 0.6, scale: 0.25 }] },
        params: function (tq) {
          var steady = tq > fx.T1 + 300, tt = steady ? quant(tq, TH) : tq;
          var d = fx.dayAt(tt) - 1; if (tt < fx.T0 - 200) return null;
          var n = m.values.length, pts = [];
          var upto = Math.min(n - 1, d);
          for (var j = 0; j <= Math.floor(upto); j++) pts.push([j, m.values[j]]);
          if (upto % 1 > 0 && Math.floor(upto) + 1 < n) { var i0 = Math.floor(upto), f = upto - i0; pts.push([upto, m.values[i0] + (m.values[i0 + 1] - m.values[i0]) * f]); }
          if (pts.length < 2) pts.push([pts[0][0] + 0.02, pts[0][1]]);
          var P = pts.map(function (p) { return [+(PX + p[0] / (n - 1) * SW).toFixed(1), +(PT + SH - p[1] / m.max * SH).toFixed(1)]; });
          var grow = clamp((tt - fx.T0 + 200) / 500);
          var fk = flick(Math.round(tt / TW), 20 + i);
          return { mode: 1, t: tt / 1000, I: +(grow * (steady ? 0.85 : 1) + fk * 0.06).toFixed(3), pts: P, reveal: P[P.length - 1][0], size: [12, 30], thick: 3.2, head: steady ? 0.5 : 1.1, rim: [2.5, 2], turb: 1.3, rise: 1.8, freq: 1.4 };
        } });
    });
    mark(tl, 'goal-line', fx.T0 + 2600);
    var svg0 = $('svg', fx.metrics[0]);
    F.hud(sec, tl, { target: svg0, t0: quant(fx.T0 + 500, TW), pad: 16, corner: 20, label: 'Every day, on its own', cls: 'right' });
  });
  shots.approve4 = over('approve', function (sec, o, tl, fx) {
    mark(tl, 'approve-burst', fx.tTap + 130);
    var ink = F.ink(sec, tl);
    var apC = function () { return center(F.rectOf(fx.ap)); };
    F.hud(sec, tl, { target: fx.ap, t0: quant(fx.tTap - 900, TW), t1: quant(fx.tTap + 20, TW), pad: 12, corner: 18, label: 'Needs your OK' });
    ink.add(F.burst({ t0: quant(fx.tTap + 30, TW), at: apC, r: 120, steps: 9, n: 10, seed: 5 }));
  });

  // ------------------------------------------------------------------ 7. away: a pilot flame in every sealed space
  shots.sandboxes4 = over('sandboxes', function (sec, o, tl, fx) {
    mark(tl, 'tiles', 1100); mark(tl, 'tile-out', Math.min.apply(null, fx.tiles.map(function (t) { return t.done; })) + 100);
    // one clean status line per tile: whichever line leads the crossfade is shown at full opacity, the rest hidden
    tl.fn(function () {
      fx.tiles.forEach(function (T) {
        var sp = T.tile.querySelectorAll('.ts span'), best = null, bo = -1;
        Array.prototype.forEach.call(sp, function (x) { var v = parseFloat(x.style.opacity || '0'); if (v > bo + 1e-6) { bo = v; best = x; } });
        Array.prototype.forEach.call(sp, function (x) { x.style.opacity = x === best ? '1' : '0'; x.style.transform = 'none'; });
      });
    });
    var ink = F.ink(sec, tl);
    fx.tiles.forEach(function (T, i) {
      var w = T.spec.w, h = T.spec.h, CW = 120, CH = h, cx = w - 14 - CW, cy = 0;
      F.flame(T.tile, tl, { x: cx, y: cy, w: CW, h: CH, pad: 50, seed: 40 + i, step: TW,
        bloom: { layers: [{ blur: 5, alpha: 0.85, scale: 0.5 }, { blur: 20, alpha: 0.7, scale: 0.25 }] },
        params: function (tq) {
          if (tq < 200 + i * 110) return null;
          var e = tq - T.done, base = [CW / 2, CH - 66];
          if (e < 0) {
            var tt = quant(tq, TH), fk = flick(Math.round(tt / TH), 40 + i);
            var lit = clamp((tq - 200 - i * 110) / 400);
            return { mode: 0, t: tt / 1000, I: +((0.95 + fk * 0.12) * lit).toFixed(3), base: base, size: [27 * (1 + fk * 0.05), 95 * (1 + fk * 0.07)], rim: [2.5, 2], turb: 1.25, rise: 1.8, freq: 2.0 };
          }
          // flicker out over four steps
          var s = Math.floor(e / TW + 0.001), seq = [[0.75, 0.8], [0.5, 0.55], [0.75, 0.35], [0.28, 0.2]];
          if (s >= seq.length) return null;
          return { mode: 0, t: quant(tq, TW) / 1000, I: seq[s][0], base: base, size: [27 * (0.7 + 0.3 * seq[s][1]), 95 * seq[s][1]], rim: [2.5, 2], turb: 1.25, rise: 2.4, freq: 2.0 };
        } });
      ink.add(embers({ t0: T.done + 30, t1: T.done + 200, n: 5, spread: 30, seed: 70 + i, at: function () { var r = F.rectOf(T.tile); return [r.x + cx + CW / 2, r.y + cy + CH - 110]; } }));
    });
  });

  // ------------------------------------------------------------------ 8. return: calm, one soft white bloom
  shots.ret4 = over('ret', function (sec, o, tl, fx) {
    mark(tl, 'brief', fx.tb + 900);
    var b = put(el('div', 'soft-bloom'), 160, -140, 1600, 720);
    sec.insertBefore(b, fx.brief);
    tl.at(b, [{ opacity: 0, transform: 'scale(.82)' }, { opacity: 1, transform: 'scale(1.04)', offset: 0.45 }, { opacity: 0.6, transform: 'scale(1)' }], { t: fx.tb - 250, d: 1800, ease: 'ease-out' });
  });

  // ------------------------------------------------------------------ 9. claims: one flame underline per verb
  shots.claims4 = over('claims5', function (sec, o, tl, fx) {
    fx.verbs.forEach(function (v, i) { mark(tl, 'verb' + i, v.at + 700); });
    var groups = [];
    fx.verbs.forEach(function (v) { var g = groups.filter(function (x) { return x.claim === v.claim; })[0]; if (!g) { g = { claim: v.claim, verbs: [] }; groups.push(g); } g.verbs.push(v); });
    groups.forEach(function (g, gi) {
      var spec = o.groups[gi], size = spec.size, verbBox = $('.verb', g.claim);
      var vx = verbBox.offsetLeft, uy = spec.y + size * 1.2;
      var widths = g.verbs.map(function (v) { return v.span.offsetWidth; });
      var maxW = Math.max.apply(null, widths);
      var X0 = spec.x + vx - 50, Y0 = uy - 96, CW = maxW + 120, CH = 124;
      F.flame(sec, tl, { x: X0, y: Y0, w: CW, h: CH, pad: 50, seed: 60 + gi, step: TW,
        bloom: { layers: [{ blur: 6, alpha: 0.85, scale: 0.5 }, { blur: 24, alpha: 0.7, scale: 0.25 }] },
        params: function (tq) {
          var v = null, vi = -1;
          g.verbs.forEach(function (x, i) { if (tq >= x.at && tq < x.at + x.dur) { v = x; vi = i; } });
          if (!v) return null;
          var e = tq - v.at, w = widths[vi] - size * 0.22;
          var x0 = 50, x1 = 50 + w, yb = 96;
          var draw = eo((e - 180) / 420); if (e < 180) return null;
          var out = v.at + v.dur - (v.last ? 420 : 450), I = 1;
          if (tq >= out) { var s = Math.floor((tq - out) / TW + 0.001); I = [0.8, 0.55, 0.3, 0.12][s] || 0; }
          var steady = e > 700, tt = steady ? quant(tq, TH) : tq;
          var fk = flick(Math.round(tt / TW), 60 + gi);
          return { mode: 1, t: tt / 1000, I: +(I * (1 + fk * 0.08)).toFixed(3), pts: [[x0, yb], [x0 + (x1 - x0) * 0.55, yb - 3], [x1, yb - 9]], reveal: +(x0 + (x1 - x0) * draw).toFixed(1), size: [16, 34], thick: 4, head: draw < 1 ? 1.1 : 0.2, rim: [3, 2.5], turb: 1.3, rise: 1.8, freq: 1.3 };
        } });
    });
  });

  // ------------------------------------------------------------------ 10. close: flare once, collapse into the ring, blur to black
  shots.close4 = function (sec, o, tl) {
    sec.classList.add('fx4');
    var SZ = 260, FLARE = o.flare || 900, COL = FLARE + 3 * TW, RING = COL + 480;
    var g = put(el('div', ''), 0, 0, 1920, 1080); sec.appendChild(g);
    var ow = put(el('div', ''), 960 - SZ / 2, 540 - SZ / 2, SZ, SZ); g.appendChild(ow);
    var halo = put(el('div', 'halo'), SZ / 2 - 260, SZ / 2 - 260); ow.appendChild(halo);
    var O = F.orb(ow, SZ), k = O.k;
    var word = put(el('div', 'wm4', 'O'), 1130, 540 - 86); g.appendChild(word);
    var oc = function () { return center(F.rectOf(O.wrap)); };
    F.grid(sec, tl, { origin: oc, t0: 0, fade: 500, alpha: 0.06, pulses: [FLARE, RING], t1: RING + 900 });
    tl.at(ow, [{ opacity: 0, transform: 'scale(.92)' }, { opacity: 1, transform: 'scale(1)' }], { t: 0, d: 700 });
    tl.at(halo, [{ opacity: 0.35, transform: 'scale(.8)' }, { opacity: 1, transform: 'scale(1.3)', offset: 0.3 }, { opacity: 0.4, transform: 'scale(1)' }], { t: FLARE, d: 1500, ease: 'ease-out' });
    var base = [SZ / 2, SZ / 2 + 0.235 * SZ];
    // the burning flame: flares, then drops out as the ring takes it
    F.flame(O.mid, tl, { x: 0, y: 0, w: SZ, h: SZ, pad: SZ * 0.5, seed: 3, step: TW,
      bloom: { layers: [{ blur: 8, alpha: 0.9, scale: 0.5 }, { blur: 30, alpha: 0.85, scale: 0.25 }] },
      params: function (tq) {
        var sw = 1, sh = 1, I = 0.9, t = quant(tq, TH) / 1000, bl = 1;
        if (tq >= FLARE) {
          var s = Math.floor((tq - FLARE) / TW + 0.001); t = tq / 1000;
          var seq = [[1.35, 1.5, 1.7, 1.6], [1.25, 1.4, 1.5, 1.4], [1.1, 1.2, 1.2, 1.2], [0.8, 0.7, 0.9, 1], [0.6, 0.45, 0.6, 1], [0.45, 0.25, 0.35, 1]];
          if (s >= seq.length) return null;
          sw = seq[s][0]; sh = seq[s][1]; I = seq[s][2]; bl = seq[s][3];
        } else { var fk = flick(Math.round(t * 10), 9); I = 0.9 + fk * 0.1; sh = 1 + fk * 0.06; }
        return { mode: 0, t: t, I: I, base: base, size: [0.2 * SZ * sw, 0.6 * SZ * sh], clip: [SZ / 2, SZ / 2, 116.5 * k], rim: [3, 2.5], turb: 1.35, rise: 1.5, freq: 1.7, bloom: bl };
      } });
    // the ring of fire that tightens onto the O ring
    var RP = SZ * 0.55;
    F.flame(ow, tl, { x: -RP, y: -RP, w: SZ + 2 * RP, h: SZ + 2 * RP, pad: 40, seed: 8, step: TW,
      bloom: { layers: [{ blur: 8, alpha: 0.9, scale: 0.5 }, { blur: 30, alpha: 0.8, scale: 0.25 }] },
      params: function (tq) {
        if (tq < COL || tq > RING + TW) return null;
        var p = clamp((tq - COL) / (RING - COL)), e = eo(p);
        var R0 = 128 * k * 1.25, R1 = 80 * k;
        return { mode: 2, t: tq / 1000, I: +(1.25 - 0.9 * p).toFixed(3), base: [RP + SZ / 2, RP + SZ / 2], ringR: +(R0 + (R1 - R0) * e).toFixed(1), size: [26, +(SZ * 0.32 * (1 - e) + 6).toFixed(1)], thick: 5, rim: [3, 2.5], turb: 1.1, rise: 2.2, freq: 2.6 };
      } });
    mark(tl, 'flare', FLARE + 70); mark(tl, 'collapse', COL + 200); mark(tl, 'ring', RING + 100); mark(tl, 'end', RING + 1250);
    var ink = F.ink(sec, tl);
    ink.add(F.impact({ t0: FLARE, at: oc, r: 260, steps: 8 }));
    ink.add(embers({ t0: FLARE + 60, t1: FLARE + 600, at: function () { var r = F.rectOf(O.wrap); return [r.x + r.w / 2, r.y + r.h * 0.2]; }, spread: 120, n: 12, seed: 13 }));
    F.flash(O.wrap, tl, { t0: FLARE, a: 0.7, radius: 999 });
    // the ring takes the energy: one white flash on the ring itself (no colour left after this)
    tl.at(O.ring, [{ opacity: 1, filter: 'brightness(1)' }, { opacity: 1, filter: 'brightness(2.2) drop-shadow(0 0 6px #fff)', offset: 0.2 }, { opacity: 1, filter: 'brightness(1)' }], { t: RING, d: 700, ease: 'ease-out' });
    // the wordmark slides in and the pair re-centres
    var WT = RING + 350;
    tl.at(word, [{ opacity: 0, transform: 'translateX(-40px)' }, { opacity: 1, transform: 'translateX(0)' }], { t: WT, d: 800 });
    tl.at(g, [{ transform: 'translateX(0)' }, { transform: 'translateX(-150px)' }], { t: WT, d: 800 });
    return o.hold || 4800;
  };
})();
