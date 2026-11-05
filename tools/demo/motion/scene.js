'use strict';
// Browser side of the scene system. One page = one scene file = a few "shots" that crossfade into each other.
// Every motion is a paused Web Animation (or a per-frame function) that render.js seeks frame by frame
// (window.__scene.seek(ms)), so the capture is deterministic regardless of how long a screenshot takes.
// The real renderer (renderer/panel.html) is embedded in an iframe where a shot needs it; mock-o.js in that
// frame talks to window.parent.__stage, the same hub surface the old stage offered.
(function () {
  var EASE = 'cubic-bezier(.23,1,.32,1)';
  var XF = 400;        // crossfade between shots in one file
  var BLUR = 500;      // blur-out at the end of a file
  var FADE_IN = 300;   // from black at the start of a file
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var el = function (tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  var esc = function (s) { return String(s).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; }); };
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  // ---------------------------------------------------------------- hub for mock-o.js (renderer frames)
  var listeners = [];
  function rpc(method, args) {
    if (typeof window.__o_rpc !== 'function') return Promise.resolve(null);
    return Promise.resolve(window.__o_rpc(method, JSON.stringify(args || []))).then(function (r) { return r == null ? null : (typeof r === 'string' ? JSON.parse(r) : r); });
  }
  window.__stage = {
    K: 1,
    moveBy: function () {}, setPos: function () {},
    getBounds: function () { return { x: 0, y: 0, width: 200, height: 200 }; },
    getWorkArea: function () { return { x: 0, y: 0, width: 1920, height: 1080 }; },
    getCursor: function () { return { x: 960, y: 540 }; },
    cursorAt: function () {},
    onAgentEvent: function (cb) { listeners.push(cb); return function () { listeners = listeners.filter(function (f) { return f !== cb; }); }; },
    emit: function (ev) { listeners.forEach(function (cb) { try { cb(ev); } catch (e) { console.error('[scene] listener', e); } }); },
    togglePanel: function () {}, showPanel: function () {}, hidePanel: function () {},
    rpc: rpc,
    settings: { theme: 'glass', permissionMode: 'log', browserMode: 'link', quiet: true, screenAccess: true, browserHeadless: false, browser: '', browserPath: '', extensionToken: '', browserProfile: '', cwd: '~/projects', claudePath: 'claude', model: '',
      vllmUrl: 'http://127.0.0.1:8000/v1', vllmModel: 'meta-llama/Meta-Llama-3-8B-Instruct', screenpipeDb: '', sandboxRoot: '~/o-sandbox', maxRemediationAttempts: 3, enginePython: '', logUnreadBadge: true,
      proxyEnabled: true, proxyPort: 8787, proxyConcurrency: 2, proxyMode: 'code', mcpServers: '{}', highRiskSandbox: 'docker', e2bApiKey: '', dockerImage: 'python:3.12-slim', swarmEnabled: true, cuCandidates: 1, cuMaxHeals: 3, vllmVisionModel: '', genuiEnabled: true, genuiSandbox: true,
      openAtLogin: false, autoUpdateCheck: false, updateRepo: '', updateBranch: 'main', updatePath: '', updateToken: '' },
  };

  // ---------------------------------------------------------------- timeline
  var S = window.__scene = { anims: [], fns: [], cues: [], duration: 0, ms: 0 };
  function anim(node, kf, o) {
    o = o || {};
    // an "out" animation (its first keyframe is the resting state, e.g. opacity 1 -> 0) must not back-fill before it starts
    var out = o.out != null ? o.out : (kf.length > 1 && kf[0].opacity === 1 && kf[kf.length - 1].opacity === 0);
    var a = node.animate(kf, { duration: o.d == null ? 400 : o.d, delay: o.t || 0, easing: o.ease || EASE, fill: o.fill || (out ? 'forwards' : 'both'), composite: o.composite || 'replace' });
    a.pause(); a.currentTime = 0;
    S.anims.push(a);
    return a;
  }
  S.seek = function (ms) {
    S.ms = ms;
    for (var i = 0; i < S.anims.length; i++) S.anims[i].currentTime = ms;
    for (var j = 0; j < S.fns.length; j++) { try { S.fns[j](ms); } catch (e) { console.error('[scene] fn', e); } }
  };

  /** Timeline bound to one shot's start time: every t is relative to the shot. */
  function TL(start) {
    var tl = {
      start: start,
      at: function (node, kf, o) { o = Object.assign({}, o || {}); o.t = start + (o.t || 0); return anim(node, kf, o); },
      fadeIn: function (node, t, d, dy) { return tl.at(node, [{ opacity: 0, transform: 'translateY(' + (dy == null ? 14 : dy) + 'px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: t, d: d || 600 }); },
      fadeOut: function (node, t, d) { return tl.at(node, [{ opacity: 1 }, { opacity: 0 }], { t: t, d: d || 400 }); },
      /** Camera push-in: scale from->to over d (600–900 ms for UI, or the whole shot for a slow Ken Burns). */
      push: function (node, t, d, from, to, ease) { return tl.at(node, [{ transform: 'scale(' + from + ')' }, { transform: 'scale(' + to + ')' }], { t: t, d: d, ease: ease || EASE }); },
      /** Per-frame function with shot-relative ms. */
      fn: function (cb) { S.fns.push(function (ms) { cb(ms - start); }); },
      /** Typewriter: set(text.slice(0, n)) as the frames pass; returns the ms when typing ends. */
      type: function (text, t, cps, set) {
        var last = -1;
        tl.fn(function (ms) { var n = ms < t ? 0 : Math.min(text.length, Math.floor((ms - t) / 1000 * cps)); if (n !== last) { last = n; set(text.slice(0, n), n >= text.length); } });
        return t + text.length / cps * 1000;
      },
      /** Run once when the frame clock passes t. */
      once: function (t, cb) { var done = false; tl.fn(function (ms) { if (!done && ms >= t) { done = true; cb(); } }); },
      /** A Node-side hook (render.js runs film.cues[name] when the frame clock passes t). */
      cue: function (t, name) { S.cues.push({ t: start + t, name: name }); },
    };
    return tl;
  }

  // ---------------------------------------------------------------- the real renderer as a fragment
  /**
   * Embed renderer/panel.html. opts: x, y, scale, window (keep the sheet look), strip ('composer': only the
   * glass composer stays visible). Resolves with {wrap, doc, win, frame} once the panel booted.
   */
  function panel(sec, opts) {
    var wrap = el('div', 'pwrap' + (opts.window ? ' window' : ''));
    wrap.style.left = (opts.x || 0) + 'px'; wrap.style.top = (opts.y || 0) + 'px';
    wrap.style.transform = 'scale(' + (opts.scale || 1) + ')';
    var frame = el('iframe'); frame.src = '/renderer/panel.html';
    wrap.appendChild(frame); sec.appendChild(wrap);
    return new Promise(function (resolve, reject) {
      var t0 = Date.now();
      (function poll() {
        var doc = frame.contentDocument;
        if (doc && doc.readyState === 'complete' && doc.getElementById('prompt')) {
          var link = doc.createElement('link'); link.rel = 'stylesheet'; link.href = '/tools/demo/motion/panel-film.css';
          doc.head.appendChild(link);
          if (opts.strip) doc.documentElement.dataset.filmStrip = opts.strip;
          link.onload = function () { resolve({ wrap: wrap, doc: doc, win: frame.contentWindow, frame: frame }); };
          link.onerror = function () { reject(new Error('panel-film.css did not load')); };
          return;
        }
        if (Date.now() - t0 > 20000) return reject(new Error('panel did not boot'));
        setTimeout(poll, 40);
      })();
    });
  }
  /** Scale a composer-only panel so the glass box is `width` px wide and its top-left sits at x,y. */
  function fitComposer(p, x, y, width) {
    var box = $('.inputwrap', p.doc).getBoundingClientRect();
    var k = width / box.width;
    p.wrap.style.width = box.width + 'px'; p.wrap.style.height = box.height + 'px'; p.wrap.style.overflow = 'visible';
    p.frame.style.left = (-box.left) + 'px'; p.frame.style.top = (-box.top) + 'px';
    p.wrap.style.left = x + 'px'; p.wrap.style.top = y + 'px'; p.wrap.style.transform = 'scale(' + k + ')';
    return k;
  }
  function typeIntoComposer(p, tl, text, t, cps) {
    var ta = p.doc.getElementById('prompt'), wrapEl = $('.inputwrap', p.doc);
    tl.once(t - 200, function () { wrapEl.classList.add('lit'); });
    return tl.type(text, t, cps, function (s) { ta.value = s; ta.dispatchEvent(new p.win.Event('input', { bubbles: true })); });
  }

  // ---------------------------------------------------------------- shots
  // Each shot(sec, o, tl) builds its DOM under sec and returns the shot's length in ms (the next shot starts XF before it ends).
  var shots = {};

  shots.intro = function (sec, o, tl) {
    var halo = el('div', 'halo'); halo.style.left = '420px'; halo.style.top = '160px';
    var mark = el('img', 'mark'); mark.src = '/assets/o-avatar.svg'; mark.style.left = '600px'; mark.style.top = '340px';
    var word = el('div', 'wordmark', 'O'); word.style.left = '800px'; word.style.top = '336px';
    var line = el('div', 'line', esc(o.line)); line.style.left = '602px'; line.style.top = '560px';
    sec.append(halo, mark, word, line);
    tl.at(halo, [{ opacity: 0, transform: 'scale(.6)' }, { opacity: .55, transform: 'scale(1)' }], { t: 200, d: 1400 });
    tl.at(mark, [{ opacity: 0, transform: 'scale(.92)' }, { opacity: 1, transform: 'scale(1)' }], { t: 300, d: 900 });
    tl.at(word, [{ opacity: 0, transform: 'translateX(-18px)' }, { opacity: 1, transform: 'translateX(0)' }], { t: 700, d: 800 });
    tl.fadeIn(line, 1500, 700, 10);
    return o.hold || 5000;
  };

  shots.quiet = function (sec, o, tl) {
    // one quiet line (the honesty frame, a demo's title)
    var line = el('div', o.big ? 'display' : 'line', esc(o.text)); line.style.left = (o.x || 240) + 'px'; line.style.top = (o.y || 490) + 'px';
    if (o.big) line.style.color = 'var(--w96)';
    sec.append(line);
    tl.fadeIn(line, 100, 700, 12);
    if (o.sub) { var sub = el('div', 'line', esc(o.sub)); sub.style.left = (o.x || 240) + 'px'; sub.style.top = ((o.y || 490) + 110) + 'px'; sec.append(sub); tl.fadeIn(sub, 500, 700, 10); }
    return o.hold || 2600;
  };

  shots.composer = function (sec, o, tl) {
    // the glass composer alone, huge; a camera group so the push-in scales around its centre
    var cam = el('div', ''); cam.style.left = '400px'; cam.style.top = '430px'; cam.style.width = '1120px'; cam.style.transformOrigin = '50% 50%';
    sec.appendChild(cam);
    return panel(cam, { strip: 'composer' }).then(function (p) {
      var k = fitComposer(p, 0, 0, 1120);
      cam.style.height = (parseFloat(p.wrap.style.height) * k) + 'px';
      tl.at(cam, [{ opacity: 0, transform: 'scale(.94)' }, { opacity: 1, transform: 'scale(1)' }], { t: 0, d: 900 });
      var end = typeIntoComposer(p, tl, o.prompt, o.typeAt || 1100, o.cps || 15);
      if (o.send !== false) {
        tl.once(end + 500, function () { var b = p.doc.getElementById('btn-send'); b.style.background = 'rgba(255,255,255,.96)'; b.style.color = '#0b0b0d'; b.style.transform = 'translateY(1px)'; });
        tl.once(end + 640, function () { var b = p.doc.getElementById('btn-send'); b.style.transform = ''; });
        tl.once(end + 900, function () { var ta = p.doc.getElementById('prompt'); ta.value = ''; ta.dispatchEvent(new p.win.Event('input', { bubbles: true })); $('.inputwrap', p.doc).classList.remove('lit'); });
      }
      return end + (o.tail == null ? 1500 : o.tail);
    });
  };

  shots.kinetic = function (sec, o, tl) {
    // luminance wave left -> right across the letters, then the line shrinks away
    var line = el('div', 'kinetic'); line.style.left = '240px'; line.style.top = '470px'; line.style.transformOrigin = '0 50%';
    var letters = o.text.split('');
    letters.forEach(function (ch) { line.appendChild(el('span', '', ch === ' ' ? '&nbsp;' : esc(ch))); });
    sec.append(line);
    tl.at(line, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: 0, d: 500 });
    var spans = line.querySelectorAll('span');
    spans.forEach(function (s, i) {
      tl.at(s, [{ color: 'rgba(255,255,255,.40)' }, { color: 'rgba(255,255,255,1)', offset: .5 }, { color: 'rgba(255,255,255,.96)' }], { t: 500 + i * 70, d: 520, ease: 'ease-in-out' });
    });
    var wave = 500 + spans.length * 70 + 520;
    tl.at(line, [{ opacity: 1, transform: 'scale(1)' }, { opacity: 0, transform: 'scale(.86)' }], { t: wave + 300, d: 500, ease: 'cubic-bezier(.4,0,1,1)', out: true });
    return wave + 300 + 500 + XF;
  };

  shots.steps = function (sec, o, tl) {
    // staggered list of real engine entries: [{n, txt, tag, ok, failed}]
    var head = el('div', 'line', esc(o.title || '')); head.style.left = '240px'; head.style.top = '150px';
    var list = el('div', 'steps'); list.style.left = '240px'; list.style.top = '250px';
    sec.append(head, list);
    if (o.title) tl.fadeIn(head, 0, 600, 10);
    o.items.forEach(function (it, i) {
      var row = el('div', 'row' + (it.failed ? ' failed' : ''));
      row.innerHTML = '<span class="n">' + esc(it.n == null ? i + 1 : it.n) + '</span><span class="txt">' + esc(it.txt) + '</span>' + (it.tag ? '<span class="tag">' + esc(it.tag) + '</span>' : '') + (it.ok ? '<span class="ok">' + esc(it.ok) + '</span>' : '');
      list.appendChild(row);
      var t = (o.at || 500) + i * (o.stagger || 110) + (it.wait || 0);
      tl.at(row, [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: t, d: 500 });
    });
    return o.hold || ((o.at || 500) + o.items.length * (o.stagger || 110) + 2600);
  };

  shots.runs = function (sec, o, tl) {
    // the real Runs sheet (GenUI job surface) as a window on the right, slow push-in; one line on the left
    var line = el('div', 'line', esc(o.line || '')); line.style.left = '200px'; line.style.top = '480px'; line.style.width = '560px';
    sec.append(line);
    tl.fadeIn(line, 300, 700, 10);
    var cam = el('div', ''); cam.style.left = '0'; cam.style.top = '0'; cam.style.width = '1920px'; cam.style.height = '1080px'; cam.style.transformOrigin = '1320px 500px';
    sec.append(cam);
    return panel(cam, { x: 980, y: 28, scale: 1.55, window: true }).then(function (p) {
      p.doc.getElementById('btn-runs').click();
      var t0 = Date.now();
      return new Promise(function (resolve) {
        (function poll() {
          var ok = p.doc.querySelector('#runs-list > *');
          if (ok || Date.now() - t0 > 6000) return resolve();
          setTimeout(poll, 60);
        })();
      }).then(function () {
        var runs = p.doc.getElementById('runs'); if (runs && o.scroll) runs.scrollTop = o.scroll;
        p.wrap.style.transformOrigin = '0 0';
        tl.at(p.wrap, [{ opacity: 0, transform: 'scale(1.5) translateY(14px)' }, { opacity: 1, transform: 'scale(1.55) translateY(0)' }], { t: 0, d: 800 });
        tl.at(cam, [{ transform: 'scale(1)' }, { transform: 'scale(1.05)' }], { t: 800, d: (o.hold || 5000) - 800, ease: 'linear' });
        return o.hold || 5000;
      });
    });
  };

  shots.settings = function (sec, o, tl) {
    // the real Settings sheet scrolled to "API proxy" (live status from the running proxy), as a window on the right
    var line = el('div', 'line', esc(o.line || '')); line.style.left = '200px'; line.style.top = '480px'; line.style.width = '560px';
    sec.append(line);
    tl.fadeIn(line, 300, 700, 10);
    return panel(sec, { x: 980, y: 28, scale: 1.55, window: true }).then(function (p) {
      p.doc.getElementById('btn-settings').click();
      return sleep(400).then(function () {
        var h = Array.prototype.find.call(p.doc.querySelectorAll('#settings h3'), function (e) { return /API proxy/.test(e.textContent); });
        var sheet = p.doc.getElementById('settings'); if (h && sheet) sheet.scrollTop = Math.max(0, h.offsetTop - 110);
        p.wrap.style.transformOrigin = '0 0';
        tl.at(p.wrap, [{ opacity: 0, transform: 'scale(1.5) translateY(14px)' }, { opacity: 1, transform: 'scale(1.55) translateY(0)' }], { t: 0, d: 800 });
        return o.hold || 5000;
      });
    });
  };

  shots.log = function (sec, o, tl) {
    // the real action-log sheet, rows streaming in (o.entries), then Verify chain, then (o.tamper) the cue + verify again
    var right = el('div', ''); right.style.left = '1100px'; right.style.top = '0'; right.style.width = '760px'; right.style.height = '1080px';
    var chain = el('div', 'chain'); chain.style.position = 'absolute'; chain.style.left = '0'; chain.style.top = '470px'; chain.style.opacity = '0';
    var note = el('div', 'line mono'); note.style.position = 'absolute'; note.style.left = '0'; note.style.top = '620px'; note.style.fontSize = '22px'; note.style.opacity = '0'; note.style.whiteSpace = 'pre';
    right.append(chain, note); sec.append(right);
    return panel(sec, { x: 140, y: 45, scale: 1.5, window: true }).then(function (p) {
      // the titlebar button moved into Settings ("View activity record"); either opens the same sheet
      (p.doc.getElementById('btn-actionlog') || p.doc.getElementById('btn-view-record')).click();
      return sleep(250).then(function () {
        p.wrap.style.transformOrigin = '0 0';
        tl.at(p.wrap, [{ opacity: 0, transform: 'scale(1.46)' }, { opacity: 1, transform: 'scale(1.5)' }], { t: 0, d: 800 });
        var t = o.at || 700, step = o.stagger || 140;
        o.entries.forEach(function (e, i) { tl.once(t + i * step, function () { window.__stage.emit({ type: 'log', entry: e }); }); });
        var tv = t + o.entries.length * step + 900;
        var chainEl = p.doc.getElementById('actionlog-chain');
        tl.once(tv, function () { p.doc.getElementById('btn-actionlog-verify').click(); });
        var shown = '';
        tl.fn(function () { var s = chainEl.textContent || ''; if (/^[✓✗]/.test(s) && s !== shown) { shown = s; chain.innerHTML = esc(s.replace(/ \(.*/, '')) + '<small>' + esc(/\(/.test(s) ? s.replace(/^[^(]*\(|\)$/g, '') : 'o-actions.jsonl') + '</small>'; } });
        tl.at(chain, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: tv + 350, d: 600 });
        var end = tv + 2800;
        if (o.tamper) {
          var tt = tv + 2600;
          tl.at(chain, [{ opacity: 1 }, { opacity: 0 }], { t: tt - 400, d: 400 });
          note.textContent = o.tamper.note;
          tl.at(note, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: tt, d: 600 });
          tl.cue(tt + 900, o.tamper.cue);
          tl.once(tt + 1500, function () { p.doc.getElementById('btn-actionlog-verify').click(); });
          tl.at(chain, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: tt + 1900, d: 600, fill: 'forwards' });
          tl.at(note, [{ transform: 'translateY(0)' }, { transform: 'translateY(60px)' }], { t: tt + 1900, d: 600, out: true });
          end = tt + 1900 + 3200;
        }
        return end;
      });
    });
  };

  shots.prose = function (sec, o, tl) {
    // streaming assistant prose with a caret (the reply is scripted; the honesty frame says so)
    var box = el('div', 'line'); box.style.left = '240px'; box.style.top = (o.y || 380) + 'px'; box.style.width = (o.width || 1240) + 'px'; box.style.fontSize = (o.size || 44) + 'px'; box.style.color = 'var(--w96)';
    var txt = el('span', ''), caret = el('span', 'caret');
    box.append(txt, caret); sec.append(box);
    tl.fadeIn(box, 0, 500, 8);
    var end = tl.type(o.text, 400, o.cps || 28, function (s, done) { txt.textContent = s; caret.style.opacity = done ? 0 : 1; });
    return end + (o.tail || 2200);
  };

  shots.files = function (sec, o, tl) {
    var head = el('div', 'line', esc(o.title || '')); head.style.left = '240px'; head.style.top = '220px';
    var list = el('div', 'files'); list.style.left = '240px'; list.style.top = '320px';
    sec.append(head, list);
    tl.fadeIn(head, 0, 600, 10);
    o.lines.forEach(function (l, i) {
      var row = el('div', l.dim ? 'd' : (l.out ? 'out' : '')); row.textContent = l.text; row.style.position = 'relative'; list.appendChild(row);
      tl.at(row, [{ opacity: 0, transform: 'translateX(-10px)' }, { opacity: 1, transform: 'translateX(0)' }], { t: 400 + i * 120, d: 500 });
    });
    return o.hold || (400 + o.lines.length * 120 + 2600);
  };

  shots.terminal = function (sec, o, tl) {
    // a terminal card: the command typed, then the real output lines streaming
    var card = el('div', 'glass term'); card.style.left = (o.x || 560) + 'px'; card.style.top = (o.y || 130) + 'px'; if (o.height) card.style.height = o.height + 'px';
    card.innerHTML = '<div class="head"><i></i><i></i><i></i><span>' + esc(o.title || 'zsh') + '</span></div>';
    var cmd = el('span', 'cmd'), caret = el('span', 'caret'), out = el('span', 'out');
    card.append(cmd, caret, out); sec.append(card);
    var left = el('div', 'line', esc(o.line || '')); left.style.left = '160px'; left.style.top = '480px'; left.style.width = '360px'; sec.append(left);
    if (o.line) tl.fadeIn(left, 300, 700, 10);
    card.style.transformOrigin = '50% 50%';
    tl.at(card, [{ opacity: 0, transform: 'scale(.96)' }, { opacity: 1, transform: 'scale(1)' }], { t: 0, d: 800 });
    var t = 600;
    var end = tl.type(o.cmd, t, o.cps || 40, function (s, done) { cmd.textContent = '$ ' + s; if (done) caret.style.display = 'none'; });
    end += 500;
    o.lines.forEach(function (l, i) {
      var sp = el('span', l.hi ? 'hi' : ''); sp.textContent = (l.text != null ? l.text : l) + '\n'; sp.style.display = 'block'; out.appendChild(sp);
      tl.at(sp, [{ opacity: 0 }, { opacity: 1 }], { t: end + i * (o.stagger || 90), d: 260 });
    });
    return end + o.lines.length * (o.stagger || 90) + (o.tail || 2600);
  };

  shots.cu = function (sec, o, tl) {
    // the accessibility tree (left) and the cu run as isolated step cards (right): plan -> failed step -> heal -> judged -> replay
    var tree = el('div', 'glass tree'); tree.style.left = '160px'; tree.style.top = '150px';
    tree.innerHTML = o.tree.map(function (n) { return '<div class="ln" data-id="' + esc(n.id) + '">' + '  '.repeat(n.depth) + '<span class="role">' + esc(n.role) + '</span>' + (n.name ? ' <span class="nm">"' + esc(n.name) + '"</span>' : '') + '</div>'; }).join('');
    sec.append(tree);
    tree.style.transformOrigin = '0 50%';
    tl.at(tree, [{ opacity: 0, transform: 'translateX(-16px)' }, { opacity: 1, transform: 'translateX(0)' }], { t: 0, d: 700 });
    var x = 900, y = 150, t = 700;
    o.cards.forEach(function (c, i) {
      var card = el('div', 'glass card' + (c.failed ? ' failed' : ''));
      card.innerHTML = '<span class="k">' + esc(c.k || '') + '</span><span class="m">' + esc(c.m || '') + '</span>' + (c.x ? '<span class="x">' + esc(c.x) + '</span>' : '');
      card.style.left = (x + (c.dx || 0)) + 'px'; card.style.top = (y + i * 82) + 'px';
      sec.append(card);
      var at = t + (c.at != null ? c.at : i * 110);
      tl.at(card, [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: at, d: 500 });
      if (c.hl) tl.once(at, function () { tree.querySelectorAll('.hl').forEach(function (e) { e.classList.remove('hl'); }); var n = tree.querySelector('[data-id="' + c.hl + '"]'); if (n) n.classList.add('hl'); });
      if (c.failed) tl.once(at + 700, function () { card.classList.add('failed'); });
    });
    return o.hold || 10000;
  };

  shots.claims = function (sec, o, tl) {
    // "O logs." "O verifies." ... serif, alternating sides; the verb crossfades on one baseline while "O" stays
    var dur = o.each || 2000;
    var groups = o.groups; // [{side:'left'|'right', claims:[{verb, frag}]}]
    var t = 0;
    groups.forEach(function (g) {
      var left = g.side === 'left';
      var claim = el('div', 'claim'); claim.style.top = '440px'; claim.style.left = left ? '200px' : '1040px';
      var verb = el('span', 'verb');
      claim.innerHTML = 'O&nbsp;'; claim.appendChild(verb);
      var frag = el('div', 'glass card'); frag.style.top = '490px'; frag.style.left = left ? '1140px' : '200px'; frag.style.whiteSpace = 'nowrap';
      sec.append(claim, frag);
      var gStart = t, gLen = g.claims.length * dur;
      tl.at(claim, [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: gStart, d: 600 });
      tl.at(claim, [{ opacity: 1 }, { opacity: 0 }], { t: gStart + gLen - 400, d: 400 });
      tl.at(frag, [{ opacity: 0 }, { opacity: 0 }], { t: 0, d: 1 });
      g.claims.forEach(function (c, i) {
        var s = el('span', '', esc(c.verb) + '.'); verb.appendChild(s);
        var at = gStart + i * dur;
        if (i > 0) { tl.at(s, [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: at, d: 450 }); }
        if (i < g.claims.length - 1) tl.at(s, [{ opacity: 1 }, { opacity: 0 }], { t: at + dur - 450, d: 450 });
        var f = el('div', ''); f.style.position = 'absolute'; f.style.left = '0'; f.style.top = '0'; f.style.whiteSpace = 'nowrap';
        f.innerHTML = '<span class="k">' + esc(c.k || '') + '</span><span class="m">' + esc(c.frag) + '</span>';
        f.style.display = 'flex'; f.style.gap = '18px'; f.style.alignItems = 'center'; f.style.padding = '22px 30px';
        frag.appendChild(f);
        tl.at(f, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: at + 200, d: 500 });
        if (i < g.claims.length - 1) tl.at(f, [{ opacity: 1 }, { opacity: 0 }], { t: at + dur - 400, d: 400 });
        else tl.at(f, [{ opacity: 1 }, { opacity: 0 }], { t: gStart + gLen - 400, d: 400 });
      });
      // the fragment card itself: size it to its first line, fade with the group
      tl.at(frag, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'translateY(0)' }], { t: gStart + 200, d: 500 });
      tl.at(frag, [{ opacity: 1 }, { opacity: 0 }], { t: gStart + gLen - 400, d: 400 });
      frag.style.width = (g.width || 560) + 'px'; frag.style.height = '80px'; frag.style.padding = '0';
      t += gLen;
    });
    return t + 200;
  };

  shots.outro = function (sec, o, tl) {
    var g = el('div', ''); g.style.left = '0'; g.style.top = '0'; g.style.width = '1920px'; g.style.height = '1080px';
    var halo = el('div', 'halo'); halo.style.position = 'absolute'; halo.style.left = '700px'; halo.style.top = '280px';
    var mark = el('img', 'mark'); mark.src = '/assets/o-avatar.svg'; mark.style.position = 'absolute'; mark.style.left = '880px'; mark.style.top = '460px';
    var word = el('div', 'wordmark', 'O'); word.style.position = 'absolute'; word.style.left = '1080px'; word.style.top = '456px';
    var line = el('div', 'line mono', esc(o.line || '')); line.style.left = '0'; line.style.width = '1920px'; line.style.textAlign = 'center'; line.style.top = '700px'; line.style.fontSize = '26px';
    g.append(halo, mark, word); sec.append(g, line);
    tl.at(mark, [{ opacity: 0, transform: 'scale(.9)' }, { opacity: 1, transform: 'scale(1)' }], { t: 0, d: 800 });
    // one white glow pulse
    tl.at(halo, [{ opacity: 0, transform: 'scale(.5)' }, { opacity: 1, transform: 'scale(1)', offset: .45 }, { opacity: .35, transform: 'scale(1.25)' }], { t: 400, d: 1600, ease: 'ease-out' });
    // the wordmark slides in and the pair re-centres
    tl.at(word, [{ opacity: 0, transform: 'translateX(-40px)' }, { opacity: 1, transform: 'translateX(0)' }], { t: 1100, d: 800 });
    tl.at(g, [{ transform: 'translateX(0)' }, { transform: 'translateX(-120px)' }], { t: 1100, d: 800 });
    if (o.line) tl.fadeIn(line, 2000, 700, 8);
    return o.hold || 5000;
  };

  // ---------------------------------------------------------------- build a file
  /** film = [{shot, ...opts}]; edges = {fadeIn, blur} ms at the file's start and end. Resolves with {duration, cues} once every shot is built and seeked to 0. */
  S.build = async function (film, edges) {
    edges = edges || {};
    var fadeIn = edges.fadeIn == null ? FADE_IN : edges.fadeIn, blurOut = edges.blur == null ? BLUR : edges.blur;
    var all = el('div', ''); all.id = 'all'; all.style.position = 'absolute'; all.style.inset = '0';
    var canvas = $('#canvas'), shotsEl = $('#shots');
    document.body.appendChild(all); all.append(canvas, shotsEl);
    var black = el('div', ''); black.style.position = 'absolute'; black.style.inset = '0'; black.style.background = '#000'; black.style.pointerEvents = 'none';
    document.body.appendChild(black);
    anim(black, [{ opacity: 1 }, { opacity: 0 }], { t: 0, d: fadeIn, ease: 'ease-out' });
    // the glow drifts a little over the whole file (one slow camera move)
    var glow = $('#glow');
    var t = 0, last = null;
    for (var i = 0; i < film.length; i++) {
      var spec = film[i];
      var sec = el('section', 'shot'); sec.dataset.shot = spec.shot; shotsEl.appendChild(sec);
      var tl = TL(t);
      var len = await shots[spec.shot](sec, spec, tl);
      if (typeof len !== 'number' || !(len > 0)) throw new Error('shot ' + spec.shot + ' returned no length');
      anim(sec, [{ opacity: 0 }, { opacity: 1 }], { t: t, d: i === 0 ? 1 : XF, ease: 'ease-in-out' });
      if (i < film.length - 1) anim(sec, [{ opacity: 1 }, { opacity: 0 }], { t: t + len - XF, d: XF, ease: 'ease-in-out' });
      last = { start: t, len: len };
      t += len - XF;
    }
    var total = last.start + last.len;
    anim(glow, [{ transform: 'translate(0,0) scale(1)' }, { transform: 'translate(-60px,30px) scale(1.08)' }], { t: 0, d: total, ease: 'linear' });
    // blur-out to black
    anim(all, [{ filter: 'blur(0px)', opacity: 1 }, { filter: 'blur(26px)', opacity: 0 }], { t: total - blurOut, d: blurOut, ease: 'ease-in', out: true });
    S.duration = total;
    S.seek(0);
    await sleep(50);
    return { duration: total, cues: S.cues };
  };
  S.shots = shots;
  // for shot packs loaded after this file (film-shots.js)
  S.lib = { el: el, esc: esc, $: $, sleep: sleep, anim: anim, panel: panel, fitComposer: fitComposer, typeIntoComposer: typeIntoComposer, EASE: EASE, XF: XF };
})();
