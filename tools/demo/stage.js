'use strict';
// The demo stage hub: stands in for Electron's main process from the renderer frames' point of view.
// Loaded in <head> before the frames, so mock-o.js (the window.o stub in each frame) finds window.parent.__stage.
// Backend-backed calls (log tail, chain verify, GenUI frames, prompts) go to Node through window.__o_rpc,
// which record.js exposes with page.exposeFunction; everything else is answered here.
(function () {
  var W = 1600, H = 1000, TASKBAR = 48, P = 200, PW = 440, PH = 660;
  var listeners = [];
  var pos = { x: W - P - 24, y: H - TASKBAR - P };
  var cursor = { x: 800, y: 500 };
  var panelOpen = false;
  var $ = function (id) { return document.getElementById(id); };
  var petEl, panelEl, cursorEl;

  function place() { if (!petEl) return; petEl.style.left = pos.x + 'px'; petEl.style.top = pos.y + 'px'; }
  function placePanel() {
    if (!panelEl) return;
    var x = pos.x - PW - 8; if (x < 0) x = pos.x + P + 8;
    if (x + PW > W) x = W - PW;
    var y = Math.max(8, Math.min(pos.y + P - PH, H - TASKBAR - PH));
    panelEl.style.left = x + 'px'; panelEl.style.top = y + 'px';
  }
  function rpc(method, args) {
    if (typeof window.__o_rpc !== 'function') return Promise.resolve(null);
    return Promise.resolve(window.__o_rpc(method, JSON.stringify(args || []))).then(function (r) { return r == null ? null : (typeof r === 'string' ? JSON.parse(r) : r); });
  }

  var hub = window.__stage = {
    W: W, H: H, K: 1, // K: viewport pixels per stage pixel (2 when recording at 2x)
    // ---- what the frames call (through mock-o.js)
    moveBy: function (dx, dy) { pos.x = Math.max(0, Math.min(W - P, pos.x + dx)); pos.y = Math.max(0, Math.min(H - TASKBAR - P, pos.y + dy)); place(); if (panelOpen) placePanel(); },
    getBounds: function () { return { x: pos.x, y: pos.y, width: P, height: P }; },
    getWorkArea: function () { return { x: 0, y: 0, width: W, height: H - TASKBAR }; },
    getCursor: function () { return { x: Math.round(cursor.x), y: Math.round(cursor.y) }; },
    cursorAt: function (x, y) { cursor.x = x; cursor.y = y; if (cursorEl) { cursorEl.style.left = x + 'px'; cursorEl.style.top = y + 'px'; } },
    onAgentEvent: function (cb) { listeners.push(cb); return function () { listeners = listeners.filter(function (f) { return f !== cb; }); }; },
    emit: function (ev) { listeners.forEach(function (cb) { try { cb(ev); } catch (e) { console.error('[stage] listener', e); } }); },
    togglePanel: function () { panelOpen ? hub.hidePanel() : hub.showPanel(); },
    showPanel: function () { panelOpen = true; placePanel(); panelEl.classList.add('open'); hub.emit({ type: 'panel', open: true }); },
    hidePanel: function () { panelOpen = false; panelEl.classList.remove('open'); hub.emit({ type: 'panel', open: false }); },
    setPos: function (x, y) { pos.x = x; pos.y = y; place(); if (panelOpen) placePanel(); },
    rpc: rpc,
    settings: { theme: 'glass', permissionMode: 'log', browserMode: 'link', quiet: true, screenAccess: true, browserHeadless: false, browser: '', browserPath: '', extensionToken: '', browserProfile: '', cwd: '~/projects', claudePath: 'claude', model: '',
      vllmUrl: 'http://127.0.0.1:8000/v1', vllmModel: 'meta-llama/Meta-Llama-3-8B-Instruct', screenpipeDb: '', sandboxRoot: '~/o-sandbox', maxRemediationAttempts: 3, enginePython: '', logUnreadBadge: true,
      proxyEnabled: true, proxyPort: 8787, proxyConcurrency: 2, proxyMode: 'code', mcpServers: '{}', highRiskSandbox: 'docker', e2bApiKey: '', dockerImage: 'python:3.12-slim', swarmEnabled: true, cuCandidates: 1, cuMaxHeals: 3, vllmVisionModel: '', genuiEnabled: true, genuiSandbox: true,
      openAtLogin: false, autoUpdateCheck: false, updateRepo: '', updateBranch: 'main', updatePath: '', updateToken: '' },

    // ---- demo helpers (called from record.js through page.evaluate)
    caption: function (t, sub) {
      var cap = $('caption');
      if (!t) { cap.classList.remove('show'); return; }
      cap.innerHTML = t + (sub ? '<small>' + sub + '</small>' : '');
      cap.classList.add('show');
    },
    card: function (show, sub, title) {
      var c = $('card');
      if (title) c.querySelector('h1').innerHTML = title;
      if (sub != null) $('card-sub').textContent = sub;
      c.classList.toggle('hide', !show);
    },
    badge: function (t) { $('badge').textContent = t || ''; $('badge').style.display = t ? '' : 'none'; },
    win: function (id, show) { var el = $(id); if (!el) return; if (show) { el.hidden = false; requestAnimationFrame(function () { el.classList.add('show'); }); } else { el.classList.remove('show'); setTimeout(function () { el.hidden = true; }, 350); } },
    // files pane
    files: function (dir, entries) {
      $('files-path').textContent = dir;
      var list = $('files-list'); list.innerHTML = '';
      entries.forEach(function (e, i) {
        var d = document.createElement('div'); d.className = 'f';
        var kind = e.dir ? 'dir' : /\.sh$/.test(e.name) ? 'sh' : /\.json$/.test(e.name) ? 'json' : /\.md$/.test(e.name) ? 'md' : '';
        d.innerHTML = '<i class="' + kind + '"></i>' + e.name;
        list.appendChild(d);
        setTimeout(function () { d.classList.add('show'); }, 120 * i);
      });
    },
    // terminal pane
    termClear: function () { $('term-out').innerHTML = ''; },
    termLine: function (html, cls) { var out = $('term-out'); var span = document.createElement('span'); span.className = cls || ''; span.innerHTML = html + '\n'; out.appendChild(span); out.scrollTop = out.scrollHeight; return span; },
    termType: function (text, ms) {
      var out = $('term-out'); var span = document.createElement('span'); span.className = 'cursor'; out.appendChild(span);
      var i = 0;
      return new Promise(function (resolve) {
        var t = setInterval(function () { span.textContent = text.slice(0, ++i); if (i >= text.length) { clearInterval(t); span.classList.remove('cursor'); span.textContent += '\n'; resolve(); } }, ms || 28);
      });
    },
    termAppend: function (text, cls) { var out = $('term-out'); var span = document.createElement('span'); span.className = cls || ''; span.textContent = text; out.appendChild(span); out.scrollTop = out.scrollHeight; },
    editor: function (html) { $('editor-body').innerHTML = html; },
    // Notes app pane (D4)
    notesTree: function (nodes) {
      var mb = $('notes-menubar'), tb = $('notes-toolbar'); mb.innerHTML = ''; tb.innerHTML = '';
      var lines = [];
      function walk(n, depth) {
        lines.push('<span class="ln" data-node="' + n.id + '">' + '  '.repeat(depth) + '<span class="tag">' + n.role + '</span>' + (n.name ? ' <span class="nm">"' + n.name + '"</span>' : '') + (n.states && n.states.length ? ' [' + n.states.join(' ') + ']' : '') + '</span>');
        (n.children || []).forEach(function (c) { walk(c, depth + 1); });
        if (n.role === 'menu') { var m = document.createElement('span'); m.className = 'n'; m.dataset.node = n.id; m.textContent = n.name; mb.appendChild(m); }
        if (n.role === 'push button') { var b = document.createElement('span'); b.className = 'n'; b.dataset.node = n.id; b.textContent = n.name; tb.appendChild(b); }
      }
      nodes.forEach(function (n) { walk(n, 0); });
      $('notes-tree').innerHTML = lines.join('\n');
    },
    notesHi: function (id, miss) {
      document.querySelectorAll('#notes .hi, #notes .miss').forEach(function (e) { e.classList.remove('hi', 'miss'); });
      if (!id) return;
      document.querySelectorAll('#notes [data-node="' + id + '"]').forEach(function (e) { e.classList.add(miss ? 'miss' : 'hi'); });
    },
    notesMiss: function (label) {
      document.querySelectorAll('#notes .hi, #notes .miss').forEach(function (e) { e.classList.remove('hi', 'miss'); });
      var pre = $('notes-tree'); var ln = document.createElement('span'); ln.className = 'ln miss'; ln.dataset.node = '__miss'; ln.textContent = '  ? ' + label + '   ← not in the tree'; pre.appendChild(document.createTextNode('\n')); pre.appendChild(ln);
    },
    notesUnmiss: function () { var m = document.querySelector('#notes [data-node="__miss"]'); if (m) { if (m.previousSibling) m.previousSibling.remove(); m.remove(); } },
    notesType: function (text, ms) {
      var ed = $('notes-editor'); ed.innerHTML = ''; var i = 0;
      return new Promise(function (resolve) { var t = setInterval(function () { ed.textContent = text.slice(0, ++i); if (i >= text.length) { clearInterval(t); resolve(); } }, ms || 45); });
    },
    notesPress: function (id) { document.querySelectorAll('#notes [data-node="' + id + '"].n').forEach(function (e) { e.classList.add('pressed'); setTimeout(function () { e.classList.remove('pressed'); }, 420); }); },
    notesStatus: function (t) { $('notes-status').textContent = t; },
    hud: function (plans, heals, proc) { $('hud').hidden = false; $('hud-plans').textContent = plans; $('hud-heals').textContent = heals; $('hud-proc').textContent = proc || '—'; },
    hudHide: function () { $('hud').hidden = true; },
    clock: function (t) { $('clock').textContent = t; },

    boot: function () {
      if (/[?&]scale=2\b/.test(location.search)) { document.documentElement.classList.add('x2'); hub.K = 2; }
      petEl = $('pet'); panelEl = $('panel'); cursorEl = $('cursor');
      place(); placePanel(); hub.cursorAt(cursor.x, cursor.y);
      document.addEventListener('mousemove', function (e) { hub.cursorAt(e.clientX / hub.K, e.clientY / hub.K); }, true);
      var d = new Date(); hub.clock(['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()] + ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'));
    },
  };
})();
