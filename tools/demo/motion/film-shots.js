'use strict';
// Shot pack for the consumer film (films.film in films.js). Same contract as scene.js: each shot builds its DOM
// under `sec`, schedules paused animations / per-frame functions on `tl`, and returns its length in ms.
// Everything is an illustration of real O features drawn in the panel's own visual language (film.css).
(function () {
  var S = window.__scene, L = S.lib, el = L.el, esc = L.esc, $ = L.$;
  var shots = S.shots;
  var XF = L.XF;

  function put(node, x, y, w, h) {
    node.style.position = 'absolute'; node.style.left = x + 'px'; node.style.top = y + 'px';
    if (w != null) node.style.width = w + 'px';
    if (h != null) node.style.height = h + 'px';
    return node;
  }
  function clamp(p) { return p < 0 ? 0 : p > 1 ? 1 : p; }
  function eo(p) { p = clamp(p); return 1 - Math.pow(1 - p, 3.2); }
  function rng(seed) { return function () { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }; }
  var ORB = '/assets/o-avatar.svg';

  /** Crossfading text lines in one holder. starts: [{t (shot ms), text}]; returns nothing, drives via tl.fn. */
  function swapper(tl, holder, items) {
    var spans = items.map(function (it) { var s = el('span', '', it.html || esc(it.text)); s.style.opacity = '0'; s.style.position = 'absolute'; s.style.left = '0'; s.style.top = '0'; s.style.whiteSpace = 'nowrap'; holder.appendChild(s); return s; });
    tl.fn(function (ms) {
      var k = -1;
      for (var i = 0; i < items.length; i++) if (ms >= items[i].t) k = i;
      for (var j = 0; j < spans.length; j++) {
        var op = 0, dy = 0;
        if (j === k) { var p = items[j].t === 0 && j === 0 ? 1 : eo((ms - items[j].t) / 260); op = p; dy = (1 - p) * 10; }
        else if (j === k - 1) { op = 1 - eo((ms - items[k].t) / 180); dy = -(1 - op) * 8; }
        spans[j].style.opacity = String(op);
        spans[j].style.transform = 'translateY(' + dy.toFixed(2) + 'px)';
      }
    });
    return spans;
  }

  /** Counter: text = fmt(value) where value eases from 0 to `to` between t0 and t1. */
  function counter(tl, node, to, t0, t1, fmt) {
    var last = null;
    tl.fn(function (ms) {
      var v = Math.round(to * clamp((ms - t0) / (t1 - t0)));
      if (v !== last) { last = v; node.textContent = fmt ? fmt(v) : String(v); }
    });
  }

  /** The real composer (renderer/panel.html), stripped to the glass box, at x,y, `width` px wide. */
  function composer(parent, x, y, width) {
    var holder = el('div', ''); put(holder, x, y); parent.appendChild(holder);
    return L.panel(holder, { strip: 'composer' }).then(function (p) {
      // room for two lines from the start, so a long prompt never grows the box mid-shot
      p.doc.documentElement.dataset.filmRows = '2';
      var k = L.fitComposer(p, 0, 0, width);
      p.k = k; p.holder = holder;
      return p;
    });
  }
  function send(p, tl, t) {
    tl.once(t, function () { var b = p.doc.getElementById('btn-send'); b.style.background = 'rgba(255,255,255,.96)'; b.style.color = '#0b0b0d'; b.style.transform = 'translateY(1px)'; });
    tl.once(t + 140, function () { var b = p.doc.getElementById('btn-send'); b.style.transform = ''; });
    tl.once(t + 260, function () {
      var ta = p.doc.getElementById('prompt'); ta.value = ''; ta.dispatchEvent(new p.win.Event('input', { bubbles: true }));
      $('.inputwrap', p.doc).classList.remove('lit');
      var b = p.doc.getElementById('btn-send'); b.style.background = ''; b.style.color = '';
    });
  }

  function ripple(tl, node, from, period) {
    tl.fn(function (ms) {
      if (ms < from) { node.style.opacity = '0'; return; }
      var ph = ((ms - from) % period) / period;
      node.style.transform = 'scale(' + (0.6 + 1.1 * ph).toFixed(3) + ')';
      node.style.opacity = (0.9 * (1 - ph)).toFixed(3);
    });
  }

  /** A task row (panel .task-row) with a crossfading status line and an elapsed counter. */
  function taskRow(tl, parent, spec, spawn, rate) {
    var row = el('div', 'f-row');
    row.innerHTML = '<span class="dot"><i></i></span><span class="tx"><span class="ttl">' + esc(spec.title) + '</span><span class="tline"></span></span><span class="tword"></span>';
    parent.appendChild(row);
    var tline = $('.tline', row), tword = $('.tword', row), ring = $('.dot i', row);
    var toMs = function (sec) { return spawn + sec / rate * 1000; };
    swapper(tl, tline, spec.steps.map(function (s) { return { t: toMs(s[0]), text: s[1] }; }));
    ripple(tl, ring, spawn, 1600);
    var lastSec = -1, done = false;
    tl.fn(function (ms) {
      var sec = Math.max(0, (ms - spawn) / 1000 * rate);
      if (spec.doneAt != null && sec >= spec.doneAt) sec = spec.doneAt;
      var s = Math.floor(sec);
      if (s !== lastSec) { lastSec = s; tword.textContent = s + ' s'; }
      var d = spec.doneAt != null && sec >= spec.doneAt;
      if (d !== done) { done = d; row.classList.toggle('done', d); }
    });
    return row;
  }

  // ------------------------------------------------------------------ 1. brand open
  shots.brand = function (sec, o, tl) {
    var halo = put(el('div', 'halo'), 380, 150);
    var orbWrap = put(el('div', ''), 560, 330, 200, 200);
    var orb = el('img', 'mark'); orb.src = ORB; orb.style.width = '200px'; orb.style.height = '200px'; orb.style.position = 'absolute';
    orbWrap.appendChild(orb);
    var word = put(el('div', 'wordmark', 'O'), 800, 344);
    word.style.fontSize = '172px';
    var line = put(el('div', 'f-claim', esc(o.line)), 566, 592);
    line.style.fontSize = '60px'; line.style.whiteSpace = 'nowrap';
    sec.append(halo, orbWrap, word, line);
    tl.at(orbWrap, [{ opacity: 0, transform: 'scale(.9)' }, { opacity: 1, transform: 'scale(1)' }], { t: 200, d: 900 });
    // one breath: the orb swells and the halo brightens, then both settle
    tl.at(orb, [{ transform: 'scale(1)' }, { transform: 'scale(1.07)', offset: 0.45 }, { transform: 'scale(1)' }], { t: 900, d: 1700, ease: 'ease-in-out' });
    tl.at(halo, [{ opacity: 0, transform: 'scale(.6)' }, { opacity: 0.85, transform: 'scale(1.05)', offset: 0.45 }, { opacity: 0.4, transform: 'scale(1)' }], { t: 700, d: 2100, ease: 'ease-in-out' });
    tl.at(word, [{ opacity: 0, transform: 'translateX(-24px)' }, { opacity: 1, transform: 'translateX(0)' }], { t: 2000, d: 800 });
    tl.fadeIn(line, 2700, 800, 12);
    return o.hold || 6000;
  };

  // ------------------------------------------------------------------ 2. three tasks at once
  shots.parallel = function (sec, o, tl) {
    var X = o.x || 380, Y = o.y || 640, W = o.width || 1100, RH = 118, rate = o.rate || 2.5;
    var cam = put(el('div', ''), 0, 0, 1920, 1080); cam.style.transformOrigin = (X + W / 2) + 'px ' + (Y - 120) + 'px';
    var inner = put(el('div', ''), 0, 0, 1920, 1080); inner.style.transformOrigin = (X + W / 2) + 'px ' + Y + 'px';
    cam.appendChild(inner); sec.appendChild(cam);
    var rowsBox = put(el('div', 'f-rows'), X, Y - 22); inner.appendChild(rowsBox);
    return composer(inner, X, Y, W).then(function (p) {
      tl.at(inner, [{ opacity: 0, transform: 'scale(.95)' }, { opacity: 1, transform: 'scale(1)' }], { t: 0, d: 800 });
      var t = o.typeAt || 800, spawns = [], sends = [];
      o.tasks.forEach(function (task) {
        var end = L.typeIntoComposer(p, tl, task.prompt, t, o.cps || 40);
        send(p, tl, end + 180);
        sends.push(end + 180);
        spawns.push(end + 340);
        t = end + (o.gap || 700);
      });
      var rows = o.tasks.map(function (task, i) { return taskRow(tl, rowsBox, task, spawns[i], rate); });
      tl.fn(function (ms) {
        rows.forEach(function (row, i) {
          var shift = 0;
          for (var j = i + 1; j < spawns.length; j++) shift += RH * eo((ms - spawns[j]) / 450);
          var a = eo((ms - spawns[i]) / 420);
          row.style.top = (-RH) + 'px';
          row.style.opacity = a.toFixed(3);
          row.style.transform = 'translateY(' + (-shift + (1 - a) * 26).toFixed(2) + 'px) scale(' + (0.97 + 0.03 * a).toFixed(4) + ')';
        });
      });
      var len = spawns[spawns.length - 1] + (o.tail || 3200);
      sec._fx = { p: p, spawns: spawns, sends: sends, rows: rows, rowsBox: rowsBox, rate: rate };
      tl.at(cam, [{ transform: 'scale(1)' }, { transform: 'scale(1.07)' }], { t: 800, d: len - 800, ease: 'cubic-bezier(.3,.1,.4,1)' });
      return len;
    });
  };

  // ------------------------------------------------------------------ 3a. desk comparison
  shots.deskTable = function (sec, o, tl) {
    var X = 200, Y = 200, W = 1340, H = 660;
    var push = put(el('div', ''), 0, 0, 1920, 1080); push.style.transformOrigin = '700px 520px';
    var card = put(el('div', 'f-card'), X, Y, W, H); card.style.position = 'absolute';
    push.appendChild(card); sec.appendChild(push);
    var title = put(el('div', 'display', esc(o.title)), 56, 52); title.style.fontSize = '50px'; title.style.position = 'absolute';
    var sub = put(el('div', 'line', esc(o.sub)), 58, 118); sub.style.fontSize = '27px'; sub.style.position = 'absolute'; sub.style.color = 'var(--w40)';
    var time = el('div', 'f-time', 'Done in <b>0 s</b>'); time.style.position = 'absolute'; time.style.right = '56px'; time.style.top = '62px';
    var table = el('div', 'f-table');
    var head = el('div', 'tr head'); head.innerHTML = o.cols.map(function (c) { return '<span>' + esc(c) + '</span>'; }).join('');
    table.appendChild(head);
    var rowsEls = o.rows.map(function (r) {
      var tr = el('div', 'tr' + (r.best ? ' best' : ''));
      tr.innerHTML = '<span class="nm">' + esc(r.name) + '<small>' + esc(r.note) + '</small></span>' + r.cells.map(function (c) { return '<span>' + esc(c) + '</span>'; }).join('') + (r.best ? '<span class="pick">' + esc(r.best) + '</span>' : '');
      table.appendChild(tr); return tr;
    });
    card.append(title, sub, time, table);
    tl.at(card, [{ opacity: 0, transform: 'translateY(24px) scale(.97)' }, { opacity: 1, transform: 'translateY(0) scale(1)' }], { t: 0, d: 800 });
    tl.fadeIn(head, 450, 500, 8);
    rowsEls.forEach(function (tr, i) { tl.fadeIn(tr, 600 + i * 120, 550, 16); });
    var last = 600 + rowsEls.length * 120 + 500;
    counter(tl, $('b', time), o.seconds, 200, last, function (v) { return v + ' s'; });
    var hold = o.hold || 4600;
    sec._fx = { card: card, time: time, rows: rowsEls, last: last };
    tl.at(push, [{ transform: 'scale(1)' }, { transform: 'scale(1.035)' }], { t: 0, d: hold, ease: 'linear' });
    return hold;
  };

  function photoBg(r) {
    var sky = 0.10 + r() * 0.22, ground = 0.04 + r() * 0.1, hz = 45 + Math.floor(r() * 25);
    var sx = Math.floor(r() * 80) + 10, sy = Math.floor(r() * 30) + 10, sun = 0.25 + r() * 0.35;
    return 'radial-gradient(circle at ' + sx + '% ' + sy + '%, rgba(255,255,255,' + sun.toFixed(2) + ') 0, rgba(255,255,255,0) 22%),' +
      'linear-gradient(180deg, rgba(255,255,255,' + sky.toFixed(2) + ') 0%, rgba(255,255,255,' + (sky * 0.6).toFixed(2) + ') ' + hz + '%, rgba(255,255,255,' + ground.toFixed(2) + ') ' + (hz + 1) + '%, rgba(255,255,255,' + (ground * 0.5).toFixed(2) + ') 100%), #141416';
  }

  // ------------------------------------------------------------------ 3b. photos into dated folders
  shots.photos = function (sec, o, tl) {
    var GX = 200, GY = 300, TW = 132, TH = 96, G = 18, COLS = 6, ROWS = 4, FX = 1240, FY = 300, FH = 134;
    var title = put(el('div', 'display', esc(o.title)), GX, 150); title.style.fontSize = '50px';
    var sub = put(el('div', 'line', esc(o.sub)), GX + 2, 216); sub.style.fontSize = '27px'; sub.style.color = 'var(--w40)';
    var time = put(el('div', 'f-time', 'Done in <b>0 s</b>'), FX, 170);
    sec.append(title, sub, time);
    tl.fadeIn(title, 0, 600, 10); tl.fadeIn(sub, 120, 600, 10); tl.fadeIn(time, 250, 600, 10);
    var folders = o.folders.map(function (f, i) {
      var d = put(el('div', 'f-folder'), FX, FY + i * FH);
      d.innerHTML = '<span class="ic"></span><span class="fn"><b>' + esc(f.name) + '</b><span>0 photos</span></span>';
      sec.appendChild(d);
      tl.fadeIn(d, 300 + i * 100, 550, 12);
      return d;
    });
    var r = rng(7), n = COLS * ROWS, flights = [];
    var FLY = o.flyAt || 1300, STEP = 55, FD = 650;
    var order = []; for (var q = 0; q < n; q++) order.push(q);
    for (var a = n - 1; a > 0; a--) { var b = Math.floor(r() * (a + 1)); var tmp = order[a]; order[a] = order[b]; order[b] = tmp; }
    for (var k = 0; k < n; k++) {
      var cx = GX + (k % COLS) * (TW + G), cy = GY + Math.floor(k / COLS) * (TH + G);
      var outer = put(el('div', ''), cx, cy, TW, TH); outer.style.position = 'absolute';
      var th = el('div', 'f-thumb'); th.style.left = '0'; th.style.top = '0'; th.style.background = photoBg(r);
      outer.appendChild(th); sec.appendChild(outer);
      tl.at(th, [{ opacity: 0, transform: 'scale(.9)' }, { opacity: 1, transform: 'scale(1)' }], { t: 250 + k * 22, d: 450 });
      var f = k % folders.length, at = FLY + order[k] * STEP;
      var tx = FX + 28 + 32 - (cx + TW / 2), ty = FY + f * FH + 56 - (cy + TH / 2);
      tl.at(outer, [{ transform: 'translate(0,0) scale(1)', opacity: 1 }, { transform: 'translate(' + (tx * 0.55) + 'px,' + (ty * 0.55 - 40) + 'px) scale(.62)', opacity: 1, offset: 0.55 }, { transform: 'translate(' + tx + 'px,' + ty + 'px) scale(.22)', opacity: 0 }], { t: at, d: FD, ease: 'cubic-bezier(.5,0,.3,1)', fill: 'both' });
      flights.push({ f: f, end: at + FD });
    }
    var lastLand = Math.max.apply(null, flights.map(function (x) { return x.end; }));
    var per = folders.map(function (_, i) { return flights.filter(function (x) { return x.f === i; }).length; });
    var shown = folders.map(function () { return -1; });
    tl.fn(function (ms) {
      folders.forEach(function (d, i) {
        var landed = flights.filter(function (x) { return x.f === i && ms >= x.end; }).length;
        var v = Math.round(o.folders[i].count * landed / per[i]);
        if (v !== shown[i]) { shown[i] = v; $('.fn span', d).textContent = v + ' photos'; }
      });
    });
    counter(tl, $('b', time), o.seconds, 300, lastLand, function (v) { return v + ' s'; });
    // as the grid empties, the renamed files take its place
    o.renames.forEach(function (rn, i) {
      var line = put(el('div', 'f-rename', esc(rn[0]) + '&nbsp;&nbsp;→&nbsp;&nbsp;<b>' + esc(rn[1]) + '</b>'), GX, GY + 20 + i * 66);
      sec.appendChild(line);
      tl.fadeIn(line, lastLand - 700 + i * 110, 500, 10);
    });
    sec._fx = { folders: folders, time: time, lastLand: lastLand };
    return Math.max(o.hold || 0, lastLand + 1300);
  };

  // ------------------------------------------------------------------ 3c. a form filling itself
  shots.form = function (sec, o, tl) {
    var title = put(el('div', 'display', esc(o.title)), 200, 360); title.style.fontSize = '62px';
    var sub = put(el('div', 'line', esc(o.sub)), 202, 446); sub.style.fontSize = '30px';
    var time = put(el('div', 'f-time', 'Done in <b>0 s</b>'), 202, 528);
    var ready = put(el('div', 'f-ready', '<i></i>' + esc(o.ready)), 200, 620);
    sec.append(title, sub, time, ready);
    tl.fadeIn(title, 100, 600, 10); tl.fadeIn(sub, 250, 600, 10); tl.fadeIn(time, 400, 600, 10);
    var card = put(el('div', 'f-card'), 920, 100, 800, 880); card.style.position = 'absolute';
    var head = put(el('div', 'f-eyebrow', esc(o.formHead)), 48, 44); head.style.position = 'absolute';
    card.appendChild(head); sec.appendChild(card);
    tl.at(card, [{ opacity: 0, transform: 'translateX(30px)' }, { opacity: 1, transform: 'translateX(0)' }], { t: 0, d: 800 });
    var t = 700, fields = [];
    o.fields.forEach(function (f, i) {
      var fe = el('div', 'f-field'); fe.style.top = (112 + i * 124) + 'px';
      fe.innerHTML = '<label>' + esc(f[0]) + '</label><div class="box"><span class="v"></span><span class="caret"></span></div>';
      card.appendChild(fe);
      var v = $('.v', fe), caret = $('.caret', fe);
      var start = t, end = tl.type(f[1], start, o.cps || 44, function (s) { v.textContent = s; });
      fields.push({ fe: fe, caret: caret, start: start - 120, end: end + 120 });
      t = end + 170;
    });
    tl.fn(function (ms) {
      fields.forEach(function (x) {
        var on = ms >= x.start && ms < x.end;
        if (x.fe.classList.contains('on') !== on) x.fe.classList.toggle('on', on);
        x.caret.style.opacity = on ? '1' : '0';
      });
    });
    counter(tl, $('b', time), o.seconds, 500, t, function (v) { return v + ' s'; });
    tl.fadeIn(ready, t + 150, 600, 10);
    sec._fx = { fields: fields, ready: ready, card: card, time: time, readyAt: t + 150 };
    return Math.max(o.hold || 0, t + 1900);
  };

  // ------------------------------------------------------------------ 4. working inside an app on O's display
  shots.apps = function (sec, o, tl) {
    var DX = 120, DY = 190, DW = 1100, DH = 720;
    var label = put(el('div', 'f-display-label', esc(o.label)), DX + 4, DY - 46);
    var disp = put(el('div', 'f-display'), DX, DY, DW, DH); disp.style.position = 'absolute';
    var app = el('div', 'f-app'); put(app, 36, 36, DW - 72, DH - 72);
    app.innerHTML = '<div class="bar"><i></i><i></i><i></i><span>' + esc(o.app) + '</span></div><div class="side"></div><div class="stage"></div>';
    disp.appendChild(app); sec.append(label, disp);
    var side = $('.side', app), stage = $('.stage', app);
    tl.fadeIn(label, 0, 600, 8);
    tl.at(disp, [{ opacity: 0, transform: 'scale(.97)' }, { opacity: 1, transform: 'scale(1)' }], { t: 0, d: 800 });
    // slide 1
    var s1 = put(el('div', ''), 0, 0); s1.style.position = 'absolute'; s1.style.inset = '0';
    var t1 = put(el('div', ''), 56, 56); t1.style.position = 'absolute'; t1.style.font = '600 50px/1.1 var(--sans)'; t1.style.color = 'var(--w96)'; t1.style.whiteSpace = 'nowrap';
    var st1 = put(el('div', '', esc(o.sub1)), 58, 128); st1.style.position = 'absolute'; st1.style.font = '400 26px/1 var(--sans)'; st1.style.color = 'var(--w70)';
    s1.append(t1, st1);
    var r = rng(11), imgs = [];
    for (var i = 0; i < 3; i++) {
      var im = put(el('div', ''), 56 + i * 232, 200, 212, 254); im.style.position = 'absolute'; im.style.borderRadius = '10px'; im.style.background = photoBg(r);
      s1.appendChild(im); imgs.push(im);
    }
    stage.appendChild(s1);
    // slide 2
    var s2 = put(el('div', ''), 0, 0); s2.style.position = 'absolute'; s2.style.inset = '0';
    var t2 = put(el('div', '', esc(o.title2)), 56, 56); t2.style.position = 'absolute'; t2.style.font = '600 50px/1.1 var(--sans)'; t2.style.color = 'var(--w96)'; t2.style.whiteSpace = 'nowrap';
    s2.appendChild(t2);
    var bullets = o.bullets.map(function (b, i) { var e = put(el('div', '', '— ' + esc(b)), 58, 170 + i * 66); e.style.position = 'absolute'; e.style.font = '400 28px/1.2 var(--sans)'; e.style.color = '#dcdce2'; e.style.whiteSpace = 'nowrap'; s2.appendChild(e); return e; });
    var im2 = put(el('div', ''), 520, 160, 200, 300); im2.style.position = 'absolute'; im2.style.borderRadius = '10px'; im2.style.background = photoBg(r); s2.appendChild(im2);
    stage.appendChild(s2);
    // thumbs
    var thumbs = [];
    for (var k = 0; k < 5; k++) {
      var th = el('div', 'sthumb'); th.style.top = (28 + k * 96) + 'px';
      th.innerHTML = '<i style="position:absolute;left:10px;top:12px;width:70px;height:7px;border-radius:3px;background:rgba(255,255,255,.55)"></i><i style="position:absolute;left:10px;top:30px;width:110px;height:34px;border-radius:4px;background:rgba(255,255,255,.12)"></i>';
      side.appendChild(th); thumbs.push(th);
    }
    // O's focus ring, hopping between the elements it is working on
    var focus = el('div', 'f-focus'); stage.appendChild(focus);
    var hops = [{ t: 700, x: 44, y: 46, w: 560, h: 80 }, { t: 1850, x: 44, y: 190, w: 718, h: 274 }, { t: 3000, x: 44, y: 46, w: 520, h: 80 }, { t: 3500, x: 40, y: 156, w: 450, h: 206 }];
    tl.fn(function (ms) {
      var k = -1; for (var i = 0; i < hops.length; i++) if (ms >= hops[i].t) k = i;
      if (k < 0) { focus.style.opacity = '0'; return; }
      var a = hops[k], b = k > 0 ? hops[k - 1] : a, p = k > 0 ? eo((ms - a.t) / 380) : 1;
      focus.style.opacity = String(k === 0 ? eo((ms - a.t) / 250) : 1);
      focus.style.left = (b.x + (a.x - b.x) * p) + 'px'; focus.style.top = (b.y + (a.y - b.y) * p) + 'px';
      focus.style.width = (b.w + (a.w - b.w) * p) + 'px'; focus.style.height = (b.h + (a.h - b.h) * p) + 'px';
    });
    tl.type(o.title1, 850, 26, function (s) { t1.textContent = s; });
    tl.fadeIn(st1, 1600, 500, 6);
    imgs.forEach(function (im, i) { tl.at(im, [{ opacity: 0, transform: 'translateY(24px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: 1950 + i * 140, d: 500 }); });
    tl.fadeIn(thumbs[0], 900, 400, 6);
    tl.at(s1, [{ opacity: 1 }, { opacity: 0 }], { t: 2900, d: 300 });
    tl.at(s2, [{ opacity: 0 }, { opacity: 1 }], { t: 2950, d: 300 });
    bullets.forEach(function (b, i) { tl.fadeIn(b, 3500 + i * 160, 450, 8); });
    tl.fadeIn(im2, 3300, 500, 12);
    for (var m = 1; m < 5; m++) tl.fadeIn(thumbs[m], 3000 + m * 260, 400, 6);
    var c1 = put(el('div', 'f-claim', esc(o.claim1)), 1320, 410, 540); var c2 = put(el('div', 'f-claim', esc(o.claim2)), 1320, 560, 540);
    c2.style.color = 'var(--w70)';
    sec.append(c1, c2);
    tl.fadeIn(c1, 1100, 800, 12); tl.fadeIn(c2, 2100, 800, 12);
    sec._fx = { focus: focus, hops: hops, stage: stage, disp: disp };
    return o.hold || 6200;
  };

  // ------------------------------------------------------------------ 5a. one correction, learned
  shots.learn = function (sec, o, tl) {
    var orb = put(el('img', ''), 240, 168, 48, 48); orb.src = ORB;
    var longR = put(el('div', 'f-reply', o.long), 320, 160, 1080);
    var shortHead = put(el('div', 'f-eyebrow', esc(o.nextLabel)), 322, 120);
    var shortR = put(el('div', 'f-reply', o.short), 320, 160, 1080);
    sec.append(orb, longR, shortHead, shortR);
    tl.fadeIn(orb, 0, 500, 6); tl.fadeIn(longR, 100, 700, 10);
    return composer(sec, 320, 720, 1080).then(function (p) {
      tl.fadeIn(p.holder, 300, 600, 10);
      var end = L.typeIntoComposer(p, tl, o.correction, 1200, 26);
      send(p, tl, end + 220);
      var user = el('div', 'f-user', esc(o.correction)); user.style.right = '520px'; user.style.top = '470px';
      sec.appendChild(user);
      tl.at(user, [{ opacity: 0, transform: 'translateY(30px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: end + 380, d: 500 });
      // the chip lands under the correction it came from
      var chip = el('div', 'f-learn', '<span class="k">Learned</span>' + esc(o.learned)); chip.style.right = '520px'; chip.style.top = '584px';
      sec.appendChild(chip);
      var tc = end + 1000;
      tl.at(chip, [{ opacity: 0, transform: 'translateX(-20px) scale(.96)' }, { opacity: 1, transform: 'translateX(0) scale(1)' }], { t: tc, d: 600 });
      var ts = tc + 1300;
      sec._fx = { chip: chip, tc: tc, user: user };
      tl.at(longR, [{ opacity: 1, filter: 'blur(0px)' }, { opacity: 0, filter: 'blur(10px)' }], { t: ts, d: 500, out: true });
      tl.fadeIn(shortHead, ts + 350, 600, 8); tl.fadeIn(shortR, ts + 450, 700, 12);
      return ts + 450 + (o.tail || 2400);
    });
  };

  // ------------------------------------------------------------------ 5b. first run vs replay
  shots.replay = function (sec, o, tl) {
    var X = 240, W = 1440, full = o.first;
    function line(y, ttl, sub, secs, at, dur) {
      var row = el('div', 'f-row'); put(row, X, y, W); row.style.height = '120px';
      row.innerHTML = '<span class="dot"><i></i></span><span class="tx"><span class="ttl">' + esc(ttl) + '</span><span class="tline"><span>' + esc(sub) + '</span></span></span><span class="tword" style="font-size:40px;color:var(--w96)">0 s</span>';
      var bar = el('div', 'f-bar'); put(bar, X + 32, y + 146, W - 64); bar.innerHTML = '<i></i>';
      sec.append(row, bar);
      tl.fadeIn(row, at - 400, 500, 12); tl.fadeIn(bar, at - 300, 500, 0);
      var fill = $('i', bar), ring = $('.dot i', row), word = $('.tword', row), last = -1;
      ripple(tl, ring, at, 1600);
      tl.fn(function (ms) {
        var p = clamp((ms - at) / dur);
        fill.style.transform = 'scaleX(' + (p * secs / full).toFixed(4) + ')';
        var v = Math.round(p * secs);
        if (v !== last) { last = v; word.textContent = v + ' s'; }
        var done = p >= 1;
        if (row.classList.contains('done') !== done) row.classList.toggle('done', done);
      });
      (sec._fx = sec._fx || { lines: [] }).lines.push({ row: row, bar: bar, word: word, at: at, dur: dur });
      return at + dur;
    }
    var e1 = line(250, o.title, o.firstLabel, o.first, 600, o.firstMs || 2600);
    var e2 = line(500, o.title, o.replayLabel, o.replay, e1 + 700, o.replayMs || 160);
    var claim = put(el('div', 'f-claim', esc(o.claim)), X, 780); claim.style.whiteSpace = 'nowrap';
    sec.appendChild(claim);
    tl.fadeIn(claim, e2 + 450, 800, 12);
    return e2 + 450 + (o.tail || 2600);
  };

  // ------------------------------------------------------------------ 6a. "Make this a goal"
  shots.goalAsk = function (sec, o, tl) {
    var X = 380, Y = 580, W = 1100;
    var inner = put(el('div', ''), 0, 0, 1920, 1080); sec.appendChild(inner);
    return composer(inner, X, Y, W).then(function (p) {
      tl.at(inner, [{ opacity: 0, transform: 'scale(.96)' }, { opacity: 1, transform: 'scale(1)' }], { t: 0, d: 800 });
      var end = L.typeIntoComposer(p, tl, o.prompt, 700, o.cps || 34);
      var offer = put(el('div', 'f-offer', '<button type="button">' + esc(o.button) + '</button><span>' + esc(o.note) + '</span>'), X + 10, Y - 92);
      inner.appendChild(offer);
      tl.at(offer, [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: end + 300, d: 500 });
      var btn = $('button', offer), tp = end + 1300;
      sec._fx = { btn: btn, tp: tp };
      tl.at(btn, [{ transform: 'scale(1)' }, { transform: 'scale(.94)', offset: 0.4 }, { transform: 'scale(1)' }], { t: tp, d: 320, ease: 'ease-out' });
      tl.at(btn, [{ boxShadow: '0 0 0 0 rgba(255,255,255,0)' }, { boxShadow: '0 0 0 10px rgba(255,255,255,.18)', offset: 0.4 }, { boxShadow: '0 0 30px 6px rgba(255,255,255,.0)' }], { t: tp, d: 700, ease: 'ease-out' });
      return tp + (o.tail || 1100);
    });
  };

  function spark(svg, values, upto, max) {
    var W = 300, H = 64, n = values.length, pts = [];
    var last = Math.min(n - 1, upto);
    for (var i = 0; i <= Math.floor(last); i++) pts.push([i, values[i]]);
    if (last % 1 > 0 && Math.floor(last) + 1 < n) { var i0 = Math.floor(last), f = last - i0; pts.push([last, values[i0] + (values[i0 + 1] - values[i0]) * f]); }
    var str = pts.map(function (p) { return (p[0] / (n - 1) * W).toFixed(1) + ',' + (H - p[1] / max * H).toFixed(1); }).join(' ');
    var end = pts[pts.length - 1];
    svg.innerHTML = '<polyline points="' + str + '"/><circle r="5" cx="' + (end[0] / (n - 1) * W).toFixed(1) + '" cy="' + (H - end[1] / max * H).toFixed(1) + '"/>';
    return end[1];
  }

  // ------------------------------------------------------------------ 6b. the goal, days passing
  shots.goalView = function (sec, o, tl) {
    var card = put(el('div', 'f-card'), 160, 150, 1060, 760); card.style.position = 'absolute';
    var eb = put(el('div', 'f-eyebrow', esc(o.eyebrow)), 48, 50); eb.style.position = 'absolute';
    var title = put(el('div', 'f-gtitle', esc(o.title)), 48, 92, 940); title.style.position = 'absolute';
    var meta = put(el('div', 'f-gmeta', 'Day <b class="d">1</b> of ' + o.days + ' · <span class="nx"></span>'), 48, 252); meta.style.position = 'absolute';
    card.append(eb, title, meta);
    var nx = $('.nx', meta); nx.style.position = 'relative'; nx.style.display = 'inline-block'; nx.style.minWidth = '520px'; nx.style.height = '36px'; nx.style.verticalAlign = 'top';
    var metrics = o.metrics.map(function (m, i) {
      var me = el('div', 'f-metric'); me.style.top = (336 + i * 150) + 'px';
      me.innerHTML = '<span class="mn">' + esc(m.name) + '</span><span class="mv">0</span><svg viewBox="0 0 300 64"></svg>';
      card.appendChild(me); return me;
    });
    var today = put(el('div', 'f-gmeta'), 48, 680); today.style.position = 'absolute'; today.style.color = 'var(--w40)'; today.textContent = o.footer;
    card.appendChild(today);
    sec.appendChild(card);
    tl.at(card, [{ opacity: 0, transform: 'translateY(20px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: 0, d: 800 });
    var T0 = 900, T1 = T0 + (o.lapseMs || 4200), lastDay = o.metrics[0].values.length;
    var dayAt = function (ms) { return 1 + (lastDay - 1) * clamp((ms - T0) / (T1 - T0)); };
    var tDay = function (d) { return T0 + (d - 1) / (lastDay - 1) * (T1 - T0); };
    swapper(tl, nx, o.next.map(function (n) { return { t: n[0] === 1 ? 0 : tDay(n[0]), text: n[1] }; }));
    var dEl = $('.d', meta), lastD = -1;
    tl.fn(function (ms) {
      var d = dayAt(ms), di = Math.floor(d);
      if (di !== lastD) { lastD = di; dEl.textContent = String(di); }
      metrics.forEach(function (me, i) {
        var m = o.metrics[i];
        var v = spark($('svg', me), m.values, d - 1, m.max);
        $('.mv', me).textContent = Math.round(v).toLocaleString('en-US');
      });
    });
    // what it did, day by day, on the right
    sec._fx = { card: card, metrics: metrics, T0: T0, T1: T1, dayAt: dayAt };
    var rail = put(el('div', 'f-tl-rail'), 1305, 180, null, 0); sec.appendChild(rail);
    rail.style.height = ((o.log.length - 1) * 140 + 10) + 'px';
    tl.fadeIn(rail, 600, 600, 0);
    o.log.forEach(function (e, i) {
      var it = put(el('div', 'f-tl', '<b>Day ' + e[0] + '</b>' + esc(e[1])), 1300, 168 + i * 140);
      sec.appendChild(it);
      tl.fadeIn(it, tDay(e[0]) - 150, 500, 10);
    });
    return T1 + (o.tail || 1500);
  };

  // ------------------------------------------------------------------ 6c. one checkpoint, one tap
  shots.approve = function (sec, o, tl) {
    var card = put(el('div', 'f-cp'), 190, 210, 1010); card.style.position = 'absolute';
    card.innerHTML = '<div class="kind">' + esc(o.kind) + '</div><div class="head">' + esc(o.head) + '</div><p class="sum">' + esc(o.sum) + '</p>' +
      '<div class="items">' + o.items.map(function (kv) { return '<div class="kv"><span>' + esc(kv[0]) + '</span><span>' + esc(kv[1]) + '</span></div>'; }).join('') + '</div>' +
      '<div class="acts"><button type="button">Skip</button><button type="button">Edit</button><button type="button" class="ap">Approve</button><div class="done" style="opacity:0">' + esc(o.done) + '</div></div>';
    sec.appendChild(card);
    var ap = $('.ap', card), r = ap.getBoundingClientRect();
    var cursor = el('div', 'f-cursor');
    cursor.innerHTML = '<svg viewBox="0 0 22 29" width="44" height="58"><path d="M2 2v22l6-5.6 4 9 3.6-1.6-4-8.8h8.4z" fill="#fff" stroke="#0b0b0d" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    var cx = r.left + r.width * 0.55, cy = r.top + r.height * 0.55;
    put(cursor, cx, cy); sec.appendChild(cursor);
    tl.at(card, [{ opacity: 0, transform: 'translateY(26px) scale(.98)' }, { opacity: 1, transform: 'translateY(0) scale(1)' }], { t: 0, d: 800 });
    var tIn = 1500, tTap = 2500;
    tl.at(cursor, [{ opacity: 0, transform: 'translate(360px, 300px)' }, { opacity: 1, transform: 'translate(260px, 220px)', offset: 0.15 }, { opacity: 1, transform: 'translate(0,0)' }], { t: tIn, d: tTap - tIn - 100, ease: 'cubic-bezier(.3,.7,.2,1)' });
    tl.at(cursor, [{ opacity: 1 }, { opacity: 0 }], { t: tTap + 900, d: 400, out: true });
    tl.at(ap, [{ transform: 'scale(1)' }, { transform: 'scale(.93)', offset: 0.35 }, { transform: 'scale(1)' }], { t: tTap, d: 300, ease: 'ease-out' });
    var btns = card.querySelectorAll('.acts button');
    Array.prototype.forEach.call(btns, function (b) { tl.at(b, [{ opacity: 1 }, { opacity: 0 }], { t: tTap + 350, d: 300 }); });
    tl.at($('.done', card), [{ opacity: 0, transform: 'translateX(14px)' }, { opacity: 1, transform: 'translateX(0)' }], { t: tTap + 600, d: 500 });
    sec._fx = { card: card, ap: ap, tTap: tTap };
    var eb = put(el('div', 'f-eyebrow', esc(o.eyebrow)), 1310, 360);
    var c = put(el('div', 'f-claim', esc(o.claim)), 1310, 404, 520); c.style.fontSize = '60px';
    sec.append(eb, c);
    tl.fadeIn(eb, 600, 600, 8); tl.fadeIn(c, 800, 800, 12);
    return o.hold || (tTap + 2600);
  };

  function pill(parent, x, y, tl, states) {
    var p = put(el('div', 'f-pill', '<img src="' + ORB + '" alt=""><span class="sl"></span>'), x, y);
    parent.appendChild(p);
    swapper(tl, $('.sl', p), states);
    return p;
  }

  // ------------------------------------------------------------------ 7a. stepping away (from above)
  shots.away = function (sec, o, tl) {
    var NS = 'http://www.w3.org/2000/svg';
    var wrap = put(el('div', ''), 120, 110, 1100, 860);
    wrap.innerHTML = '<svg width="1100" height="860" viewBox="0 0 1100 860" xmlns="' + NS + '" style="overflow:visible">' +
      '<g fill="none" stroke="rgba(255,255,255,.14)" stroke-width="2">' +
      '<path d="M820 820 H 68 a28 28 0 0 1 -28 -28 V 68 a28 28 0 0 1 28 -28 H 1032 a28 28 0 0 1 28 28 V 792 a28 28 0 0 1 -28 28 H 960"/>' +
      '<path d="M820 820 A140 140 0 0 1 960 680" stroke-dasharray="4 8"/><path d="M960 820 V 680"/>' +
      '<path d="M220 40 H 580" stroke="rgba(255,255,255,.4)" stroke-width="4"/>' +
      '<rect x="120" y="96" width="520" height="160" rx="14" fill="rgba(255,255,255,.035)"/>' +
      '<rect x="700" y="560" width="300" height="160" rx="34" stroke="rgba(255,255,255,.08)"/>' +
      '<circle cx="990" cy="120" r="36" stroke="rgba(255,255,255,.1)"/>' +
      '<circle cx="380" cy="372" r="64"/>' +
      '</g>' +
      '<rect x="300" y="128" width="160" height="104" rx="10" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.4)" stroke-width="2"/>' +
      '<rect x="312" y="128" width="136" height="7" rx="3" fill="rgba(255,255,255,.85)"/>' +
      '<g class="rings"><circle class="rg" cx="380" cy="180" r="100"/><circle class="rg" cx="380" cy="180" r="100"/><circle class="rg" cx="380" cy="180" r="100"/></g>' +
      '<g class="person"><ellipse cx="380" cy="380" rx="80" ry="38" fill="rgba(255,255,255,.2)"/><circle cx="380" cy="368" r="32" fill="rgba(255,255,255,.42)"/></g>' +
      '</svg>';
    sec.appendChild(wrap);
    var rings = wrap.querySelectorAll('.rg');
    Array.prototype.forEach.call(rings, function (c, i) {
      c.setAttribute('fill', 'none'); c.setAttribute('stroke', 'rgba(255,255,255,.55)'); c.setAttribute('stroke-width', '1.5');
      c.style.transformBox = 'fill-box'; c.style.transformOrigin = '50% 50%';
      ripple(tl, c, 300 + i * 600, 1800);
    });
    var person = wrap.querySelector('.person');
    tl.at(wrap, [{ opacity: 0 }, { opacity: 1 }], { t: 0, d: 700 });
    tl.at(person, [
      { transform: 'translate(0px,0px)', opacity: 1 },
      { transform: 'translate(40px,130px)', opacity: 1, offset: 0.25 },
      { transform: 'translate(330px,300px)', opacity: 1, offset: 0.65 },
      { transform: 'translate(510px,470px)', opacity: 0.6, offset: 0.88 },
      { transform: 'translate(520px,560px)', opacity: 0 },
    ], { t: 1100, d: 2500, ease: 'cubic-bezier(.45,0,.4,1)', fill: 'both' });
    var p = pill(sec, 1320, 470, tl, [{ t: 0, text: o.here }, { t: 3800, text: o.away }]);
    tl.at(p, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: 400, d: 600 });
    return o.hold || 6000;
  };

  // ------------------------------------------------------------------ 7b. tasks finishing in sealed spaces
  shots.sandboxes = function (sec, o, tl) {
    var pillStates = [{ t: 0, text: o.pill[0] }];
    var finishes = o.tiles.map(function (t) { return t.done; }).slice().sort(function (a, b) { return a - b; });
    finishes.forEach(function (t, i) { pillStates.push({ t: t, text: o.pill[i + 1] }); });
    pill(sec, 140, 210, tl, pillStates);
    var r = rng(5);
    o.tiles.forEach(function (ti, idx) {
      var tile = put(el('div', 'f-tile'), ti.x, ti.y, ti.w, ti.h);
      tile.innerHTML = '<div class="tt">' + esc(ti.title) + '</div><div class="ts"></div>' +
        '<svg class="seal" viewBox="0 0 24 24"><rect x="5" y="10.5" width="14" height="10" rx="2.2" fill="none" stroke="rgba(255,255,255,.55)" stroke-width="1.6"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5" fill="none" stroke="rgba(255,255,255,.55)" stroke-width="1.6"/></svg>' +
        '<div class="check" style="opacity:0"><svg viewBox="0 0 20 20"><path d="M4.5 10.5l3.5 3.5 7.5-8" fill="none" stroke="#0b0b0d" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg></div><div class="grid"></div><div class="f-bar" style="left:30px;right:30px;bottom:28px;height:4px"><i></i></div>';
      sec.appendChild(tile);
      tl.at(tile, [{ opacity: 0, transform: 'translateY(16px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: 150 + idx * 110, d: 600 });
      var ts = $('.ts', tile);
      var steps = ti.steps.map(function (s, i) { return { t: i === 0 ? 0 : 300 + (ti.done - 300) * i / ti.steps.length, text: s }; });
      steps.push({ t: ti.done, text: 'Finished' });
      swapper(tl, ts, steps);
      var grid = $('.grid', tile), n = Math.floor((ti.w - 60) / 16), bars = [], seeds = [];
      for (var b = 0; b < n; b++) { var bi = el('i', ''); bi.style.left = (b * 16) + 'px'; grid.appendChild(bi); bars.push(bi); seeds.push(r() * 6.28); }
      var check = $('.check', tile), seal = $('.seal', tile), fill = $('.f-bar i', tile);
      (sec._fx = sec._fx || { tiles: [] }).tiles.push({ tile: tile, done: ti.done, spec: ti });
      tl.fn(function (ms) {
        var settle = eo((ms - ti.done) / 500);
        for (var k = 0; k < bars.length; k++) {
          var live = 0.5 + 0.5 * Math.sin(ms / 260 + seeds[k] + k * 0.45);
          var h = 6 + (1 - settle) * live * 36;
          bars[k].style.height = h.toFixed(1) + 'px';
          bars[k].style.opacity = (0.35 + (1 - settle) * 0.45).toFixed(2);
        }
        fill.style.transform = 'scaleX(' + clamp(ms / ti.done).toFixed(4) + ')';
        grid.style.opacity = (1 - settle).toFixed(3);
        check.style.opacity = settle.toFixed(3);
        check.style.transform = 'scale(' + (0.7 + 0.3 * settle).toFixed(3) + ')';
        seal.style.opacity = (1 - settle).toFixed(3);
      });
    });
    var cap = put(el('div', 'line', esc(o.caption)), 140, 806); cap.style.fontSize = '27px'; cap.style.color = 'var(--w40)';
    sec.appendChild(cap); tl.fadeIn(cap, 800, 700, 8);
    o.milestones.forEach(function (m, i) {
      var row = put(el('div', 'f-ms', '<em>' + esc(m[0]) + '</em><i></i><span>' + esc(m[1]) + '</span>'), 1260, 410 + i * 96);
      sec.appendChild(row);
      tl.fadeIn(row, finishes[i] + 150, 500, 10);
    });
    var msLabel = put(el('div', 'f-eyebrow', esc(o.msLabel)), 1260, 340); sec.appendChild(msLabel);
    tl.fadeIn(msLabel, 600, 600, 6);
    return o.hold || (finishes[finishes.length - 1] + 1800);
  };

  // ------------------------------------------------------------------ 8. coming back: the brief
  shots.ret = function (sec, o, tl) {
    var NS = 'http://www.w3.org/2000/svg';
    var r = rng(3), dots = '';
    // a sparse face mesh: rings inside the face oval plus eye, nose and mouth contours
    var fx = 960, fy = 690, rx = 120, ry = 156;
    for (var ring = 1; ring <= 7; ring++) {
      var k = ring / 7, cnt = 6 + ring * 6;
      for (var i = 0; i < cnt; i++) { var a = i / cnt * Math.PI * 2 + ring * 0.3; dots += '<circle cx="' + (fx + Math.cos(a) * rx * k + (r() - 0.5) * 4).toFixed(1) + '" cy="' + (fy + Math.sin(a) * ry * k + (r() - 0.5) * 4).toFixed(1) + '" r="2.2"/>'; }
    }
    [[-38, -22], [38, -22]].forEach(function (e) { for (var j = 0; j < 8; j++) { var a = j / 8 * Math.PI * 2; dots += '<circle cx="' + (fx + e[0] + Math.cos(a) * 16).toFixed(1) + '" cy="' + (fy + e[1] + Math.sin(a) * 7).toFixed(1) + '" r="2"/>'; } });
    for (var m = 0; m < 9; m++) { var mx = -30 + m * 7.5; dots += '<circle cx="' + (fx + mx).toFixed(1) + '" cy="' + (fy + 58 + Math.pow(mx / 30, 2) * -6).toFixed(1) + '" r="2"/>'; }
    // the laptop's screen edge at the bottom of the frame, lit from below as someone sits down; a sparse
    // face-mesh point cloud (20 % at most) is the only sign of the person
    var fig = put(el('div', ''), 0, 0, 1920, 1080);
    fig.innerHTML = '<svg width="1920" height="1080" viewBox="0 0 1920 1080" xmlns="' + NS + '">' +
      '<defs><radialGradient id="lidg" cx="50%" cy="100%" r="60%"><stop offset="0" stop-color="#fff" stop-opacity=".28"/><stop offset=".5" stop-color="#fff" stop-opacity=".07"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>' +
      '<linearGradient id="edgeg" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".9"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient></defs>' +
      '<ellipse class="lid" cx="960" cy="1060" rx="900" ry="460" fill="url(#lidg)"/>' +
      '<path class="edge" d="M300 1050 Q 960 1028 1620 1050" fill="none" stroke="url(#edgeg)" stroke-width="2.5"/>' +
      '<g class="mesh" fill="#fff">' + dots + '</g></svg>';
    sec.appendChild(fig);
    var lid = fig.querySelector('.lid'), edge = fig.querySelector('.edge'), mesh = fig.querySelector('.mesh');
    tl.at(lid, [{ opacity: 0.15 }, { opacity: 1 }], { t: 200, d: 1800, ease: 'cubic-bezier(.3,.2,.2,1)' });
    tl.at(edge, [{ opacity: 0.25 }, { opacity: 1 }], { t: 200, d: 1400 });
    tl.at(mesh, [{ opacity: 0, transform: 'translateY(24px)' }, { opacity: 0.2, transform: 'translateY(0)' }], { t: 700, d: 1600, ease: 'ease-out' });
    tl.at(mesh, [{ opacity: 0.2 }, { opacity: 0.1 }], { t: 3600, d: 1200, out: true });
    var p = pill(sec, 140, 920, tl, [{ t: 0, text: o.pill[0] }, { t: 1500, text: o.pill[1] }]);
    tl.fadeIn(p, 100, 500, 0);
    var brief = put(el('div', 'f-brief'), 340, 70);
    brief.innerHTML = '<div class="hd"><img src="' + ORB + '" alt=""><b>' + esc(o.title) + '</b><span>' + esc(o.when) + '</span></div><p>' + o.sentences.map(function (s) { return '<span>' + esc(s) + ' </span>'; }).join('') + '</p>';
    sec.appendChild(brief);
    var tb = 2000;
    tl.at(brief, [{ opacity: 0, transform: 'translateY(-36px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: tb, d: 700 });
    sec._fx = { brief: brief, tb: tb };
    var spans = brief.querySelectorAll('p span');
    Array.prototype.forEach.call(spans, function (s, i) { tl.at(s, [{ opacity: 0 }, { opacity: 1 }], { t: tb + 400 + i * 650, d: 500 }); });
    return o.hold || 9200;
  };

  // ------------------------------------------------------------------ 9. claims: verb crossfades on one baseline
  shots.claims5 = function (sec, o, tl) {
    var dur = o.each || 2000, t = 0;
    o.groups.forEach(function (g) {
      var claim = put(el('div', 'claim'), g.x, g.y); claim.style.fontSize = g.size + 'px';
      var verb = el('span', 'verb'); claim.innerHTML = 'O&nbsp;'; claim.appendChild(verb);
      sec.appendChild(claim);
      var gStart = t, gLen = g.claims.length * dur;
      tl.at(claim, [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: gStart, d: 600 });
      tl.at(claim, [{ opacity: 1 }, { opacity: 0 }], { t: gStart + gLen - 400, d: 400 });
      g.claims.forEach(function (c, i) {
        var s = el('span', '', esc(c.verb) + '.'); verb.appendChild(s);
        var at = gStart + i * dur;
        ((sec._fx = sec._fx || { verbs: [] }).verbs).push({ claim: claim, span: s, at: at, dur: dur, first: i === 0, last: i === g.claims.length - 1, gStart: gStart, gLen: gLen });
        if (i > 0) tl.at(s, [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: at, d: 450 });
        if (i < g.claims.length - 1) tl.at(s, [{ opacity: 1 }, { opacity: 0 }], { t: at + dur - 450, d: 450 });
        var f = put(el('div', 'f-card f-frag', c.frag), g.fx, g.fy); sec.appendChild(f);
        tl.at(f, [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: at + 150, d: 500 });
        tl.at(f, [{ opacity: 1 }, { opacity: 0 }], { t: at + dur - 380, d: 380 });
      });
      t += gLen;
    });
    return t + 200;
  };
})();
