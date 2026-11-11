'use strict';
// O: the Island cluster, in one transparent always-on-top window.
// One glass element with five modes (renderer/adapt.js decides, main/main.js drives): the dot, the pill
// (ring + status line), the cluster (the O circle + one short input segment, task chips springing around it),
// the card (one result / approval box anchored to a chip) and full (the dashboard window covers the screen).
// Everything is transform / opacity / clip-path; the window never shows a rectangle.
(function () {
  const bridge = window.o || {};
  const L = window.OLiquid;
  const A = window.OAdapt;
  const G = window.OIslandGeom;
  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const root = document.documentElement;
  const stage = $('stage');
  const island = $('island');
  const ring = $('ring');
  const lineEl = $('line');
  const metaEl = $('meta');
  const seg = $('seg');
  const input = $('input');
  const expand = $('expand');
  const chipsEl = $('chips');
  const cardEl = $('card');
  const measure = $('measure');
  const SHAPE_PAD = 2; // keeps the capsule's antialiased edge inside the Linux window shape

  // Geometry shared with island.css; placement (menu bar, notch, bottom, corners) from islandgeom.js.
  let LY = (bridge.initialIslandLayout && bridge.initialIslandLayout.compact) ? bridge.initialIslandLayout
    : G.layout({ platform: bridge.platform, display: { bounds: { x: 0, y: 0, width: 1440, height: 900 }, workArea: { x: 0, y: 0, width: 1440, height: 900 } }, position: 'top' });
  let SZ = G.sizes(LY.ih);
  const capTop = () => (LY.dir === 'up' ? window.innerHeight - LY.gap - LY.ih : LY.top);

  // ------------------------------------------------------------ state fed by agent events
  let tasks = [];
  let goals = [];
  let checkpoints = [];
  const perms = new Map(); // id -> { id, toolName, summary, taskId, input }
  const lastTool = new Map(); // taskId -> tool name (bounded, see capMap)
  let evolve = null;
  let flash = null; // { k, v, tone, until }
  let setupNeeded = false;
  let pendingStart = null; // { title, at }: a prompt just sent, until the task list shows it
  const results = new Map(); // taskId -> one-line result (from 'done' events; bounded)
  const MAP_CAP = 200;
  /** Keep a Map / Set at MAP_CAP entries: the oldest (insertion order) go first. */
  function capMap(m) { while (m.size > MAP_CAP) m.delete(m.keys().next().value); }
  let mode = 'pill';     // dot | pill | cluster | card | full
  let typing = false;    // the input has keyboard focus (main: commandBarOpen)
  let hovering = false;
  let openKey = null;    // the chip whose card is open
  let cardH = 0;
  let cardTimer = null;  // a finished task's card closes itself (A3): 20 s, unless the pointer is on it
  // The user's locked mode lives here (localStorage, per machine) and is told to main at boot.
  const prefs = (() => { try { return JSON.parse(localStorage.getItem('o.island.prefs') || '{}') || {}; } catch (_) { return {}; } })();
  function savePrefs(next) {
    Object.assign(prefs, next || {});
    try { localStorage.setItem('o.island.prefs', JSON.stringify(prefs)); } catch (_) {}
  }

  const clean = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const clip = (s, n) => { s = clean(s); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const isBrowserTool = (name) => /playwright|browser/i.test(name || '');
  const running = () => tasks.filter((t) => t.status === 'running');
  const queued = () => tasks.filter((t) => t.status === 'queued');
  const taskTitle = (id) => { const t = tasks.find((x) => x.id === id); return t ? t.title : ''; };

  function setFlash(k, v, tone, ms) {
    flash = { k, v: clean(v), tone: tone || 'idle', until: Date.now() + (ms || 4500) };
    renderIsland();
    setTimeout(renderIsland, (ms || 4500) + 30);
  }

  // ------------------------------------------------------------ the island line (pill)
  function elapsed(since) {
    if (!since) return '';
    const s = Math.max(0, Math.round((Date.now() - since) / 1000));
    const m = Math.floor(s / 60);
    return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${m}:${String(s % 60).padStart(2, '0')}`;
  }

  function evolveLine() {
    const st = evolve;
    if (!st || st.enabled === false) return null;
    const f = (st.in_flight || [])[0];
    if (!f) return null;
    const title = typeof f === 'string' ? f : (f.title || f.name || f.subsystem || '');
    const building = typeof f === 'object' && /build|gate|test|sandbox/i.test(String(f.status || ''));
    return { k: building ? 'Building' : 'Learning', v: title || 'a new tool', tone: 'working' };
  }

  function status() {
    if (setupNeeded) return { k: 'Setup needed', v: 'Claude Code is not installed', tone: 'attention' };
    if (perms.size) {
      const p = [...perms.values()][0];
      return { k: 'Needs your OK', v: p.summary || p.toolName || 'an action', tone: 'attention', meta: perms.size > 1 ? `+${perms.size - 1}` : '' };
    }
    if (checkpoints.length) {
      const c = checkpoints[0];
      return { k: 'Ready for review', v: c.headline || c.goalTitle || 'a goal step', tone: 'attention', meta: checkpoints.length > 1 ? `+${checkpoints.length - 1}` : '' };
    }
    if (flash && flash.until > Date.now()) return { k: flash.k, v: flash.v, tone: flash.tone };
    flash = null;
    const run = running().sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
    if (run.length) {
      const t = run[0];
      const verb = isBrowserTool(lastTool.get(t.id)) ? 'Browsing' : 'Working';
      return { k: verb, v: t.title || t.statusLine, tone: 'working', meta: run.length > 1 ? `+${run.length - 1}` : elapsed(t.startedAt) };
    }
    if (pendingStart && Date.now() - pendingStart.at < 8000) return { k: 'Starting', v: pendingStart.title, tone: 'working' };
    const q = queued();
    if (q.length) return { k: 'Queued', v: q[0].title, tone: 'working', meta: q.length > 1 ? `+${q.length - 1}` : '' };
    const ev = evolveLine();
    if (ev) return ev;
    const active = goals.filter((g) => g.status === 'active');
    if (active.length) {
      const next = active.map((g) => g.nextRun).filter(Boolean).sort()[0];
      return { k: `${active.length} goal${active.length === 1 ? '' : 's'} active`, v: '', tone: 'idle', meta: next ? nextIn(next) : '' };
    }
    return { k: 'Ready', v: '', tone: 'idle' };
  }

  function nextIn(when) {
    const t = typeof when === 'number' ? when : Date.parse(when);
    if (!t || t < Date.now()) return '';
    const m = Math.round((t - Date.now()) / 60000);
    return m < 60 ? `next in ${m}m` : m < 24 * 60 ? `next in ${Math.round(m / 60)}h` : '';
  }

  const lineHtml = (k, v) => `<span class="k">${esc(k)}</span>${v ? `<span class="sep">·</span><span class="v">${esc(v)}</span>` : ''}`;

  let lastKey = '';
  let lastText = { textW: 0, meta: false };
  let lastTone = 'idle';
  function renderIsland() {
    const s = status();
    const tone = s.tone === 'working' ? 'is-working' : s.tone === 'attention' ? 'is-attention' : s.tone === 'success' ? 'is-success' : s.tone === 'danger' ? 'is-danger' : 'is-idle';
    if (!ring.classList.contains(tone)) ring.className = 'ring ' + tone;
    const key = s.k + '|' + clip(s.v, 80) + '|' + mode;
    metaEl.textContent = s.meta || '';
    if (key === lastKey) return;
    lastKey = key;
    let html = lineHtml(s.k, clip(s.v, 80));
    measure.innerHTML = html;
    lastText = { textW: Math.min(330, measure.offsetWidth), meta: !!s.meta };
    lastTone = s.tone;
    let w = effectiveWidth();
    if (LY.mode === 'notch' && s.v && mode === 'pill') {
      measure.innerHTML = lineHtml(s.k, '');
      const kW = measure.offsetWidth;
      if (lineRoom(w) - kW < 56) { html = lineHtml(s.k, ''); lastText.textW = kW; w = effectiveWidth(); }
    }
    setIslandWidth(w);
    lineEl.innerHTML = html;
    lineEl.classList.remove('swap');
    void lineEl.offsetWidth; // restart the crossfade
    lineEl.classList.add('swap');
    const full = `${s.k}${s.v ? ' · ' + s.v : ''}`;
    island.title = full; // the whole line, also when only the ring shows
    island.setAttribute('aria-label', `${s.k}${s.v ? ', ' + s.v : ''}. Click to type a task`);
  }

  /** The ring alone: the caps overlap into a circle (notch: the notch plus the narrowest wings). */
  function dotWidth() {
    if (LY.mode === 'notch') return LY.notchW + 2 * G.C.RING_ONLY_WING;
    return LY.ih;
  }
  /** The capsule width the mode wants: the circle in every mode but the pill. */
  function effectiveWidth() { return mode === 'pill' ? islandWidth() : dotWidth(); }

  /** The pill width for the current line and layout. */
  function islandWidth() {
    const { textW, meta } = lastText;
    const metaW = meta ? 52 : 0;
    if (LY.mode === 'notch') {
      const inset = Math.round(LY.ih * 0.42);
      const left = inset + SZ.ring + (meta ? SZ.gap + 44 : 0) + inset;
      const right = 8 + Math.min(textW, 240) + inset;
      const wing = LY.ringOnly ? G.C.RING_ONLY_WING : Math.max(G.C.RING_ONLY_WING, Math.min(LY.maxWing, Math.max(left, right)));
      return LY.notchW + 2 * wing;
    }
    if (LY.ringOnly) return LY.maxW;
    return Math.round(Math.max(LY.minW, Math.min(LY.maxW, textW + SZ.ring + SZ.gap + metaW + SZ.pad)));
  }

  function lineRoom(w) {
    if (LY.mode === 'notch') return Math.max(0, Math.round(w / 2 - LY.notchW / 2 - 8 - LY.ih * 0.42));
    return Math.max(0, Math.round(w - SZ.pad - SZ.ring - SZ.gap - (lastText.meta ? 44 + SZ.gap : 0)));
  }

  let lastW = 0;
  /** The capsule centre x in window px (what --cx resolves to). */
  function capCx(w) {
    const pad = G.C.PAD + w / 2;
    return LY.anchor === 'left' ? pad : LY.anchor === 'right' ? window.innerWidth - pad : window.innerWidth / 2;
  }

  function setIslandWidth(w) {
    lastW = w;
    stage.style.setProperty('--w', String(w));
    stage.style.setProperty('--line-max', `${Math.min(330, lineRoom(w))}px`);
    const pad = G.C.PAD + w / 2;
    root.style.setProperty('--cx', LY.anchor === 'left' ? `${pad}px` : LY.anchor === 'right' ? `calc(100vw - ${pad}px)` : '50vw');
    layoutCluster(); // reports the shape too
  }

  /**
   * The solid rect (footprint px) main shapes the Linux window to and measures against: the capsule alone
   * while compact; the whole cluster (circle + expand + segment + chips + card) while it is up, so the
   * chips and the card take clicks before the input has focus.
   */
  let lastShapeKey = '';
  function reportShape() {
    if (typeof bridge.setIslandShape !== 'function') return;
    const r = G.capsuleRect(LY, lastW);
    const on = clusterOn();
    const lay = lastCluster;
    const cdx = on ? lay.cdx : 0;
    let x0 = r.x + cdx, y0 = r.y, x1 = r.x + cdx + r.width, y1 = r.y + r.height;
    const add = (x, y, w, h) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h); };
    if (on && lay.seg) {
      const drop = G.barDrop(LY);
      add(x0 - 10 - 22, r.y + drop, 22, r.height); // the expand circle, left of the circle
      add(lay.seg.x, lay.seg.y + drop, lay.seg.w, lay.seg.h);
      [...live.keys()].forEach((key, i) => { const c = lay.chips[i]; if (c) add(c.x, c.y + drop, c.w, c.h); });
      if (openKey && lay.card && !cardEl.hidden) add(lay.card.x, lay.card.y + drop, lay.card.w, cardH || 120);
    }
    const W = window.innerWidth, H = window.innerHeight;
    x0 = Math.max(0, Math.floor(x0 - SHAPE_PAD)); y0 = Math.max(0, Math.floor(y0 - SHAPE_PAD));
    x1 = Math.min(W, Math.ceil(x1 + SHAPE_PAD)); y1 = Math.min(H, Math.ceil(y1 + SHAPE_PAD));
    const shape = { x: x0, y: y0, width: Math.max(1, x1 - x0), height: Math.max(1, y1 - y0), attention: lastTone === 'attention' };
    const key = JSON.stringify(shape);
    if (key === lastShapeKey) return;
    lastShapeKey = key;
    bridge.setIslandShape(shape);
  }

  /** Apply a layout from main: CSS variables and body classes, then re-measure the capsule. */
  function applyLayout(next) {
    if (next && next.compact) LY = next;
    SZ = G.sizes(LY.ih);
    root.style.setProperty('--ih', String(LY.ih));
    root.style.setProperty('--top', LY.dir === 'up' ? `calc(100vh - ${LY.gap + LY.ih}px)` : `${LY.top}px`);
    root.style.setProperty('--fs', `${SZ.fs}px`);
    root.style.setProperty('--ring', `${SZ.ring}px`);
    root.style.setProperty('--gap', `${SZ.gap}px`);
    root.style.setProperty('--notch', `${LY.notchW || 0}px`);
    root.style.setProperty('--drop', `${G.barDrop(LY)}px`);
    body.classList.toggle('up', LY.dir === 'up');
    body.classList.toggle('mb', LY.mode === 'menubar' || LY.mode === 'notch');
    body.classList.toggle('notch', LY.mode === 'notch');
    body.classList.toggle('ring-only', !!LY.ringOnly);
    lastKey = '';
    if (lineEl) renderIsland();
  }

  setInterval(() => {
    if (pendingStart && Date.now() - pendingStart.at >= 8000) { pendingStart = null; changed(); return; }
    if (running().length || (flash && flash.until <= Date.now())) renderIsland();
  }, 1000);
  // Finished chips leave after 15 min even when no event arrives (chipItems' cutoff is re-evaluated).
  setInterval(() => { if (clusterOn() && live.size) syncChips(); }, 30 * 1000);

  // ------------------------------------------------------------ the cluster: circle + segment + chips + card
  const clusterOn = () => mode === 'cluster' || mode === 'card';
  let lastCluster = { cdx: 0, chips: [], card: null, seg: null, circle: null };

  function chipItems() {
    const out = [];
    for (const p of perms.values()) out.push({ key: 'perm:' + p.id, kind: 'perm', label: p.summary || p.toolName || 'Needs your OK', title: 'Needs your OK', p });
    for (const c of checkpoints) out.push({ key: 'cp:' + c.id, kind: 'checkpoint', label: c.headline || 'A goal step is ready', title: 'Ready for review', c });
    if (setupNeeded) out.push({ key: 'setup', kind: 'failed', label: 'Set up O', title: 'Claude Code is not installed', tip: 'O needs it to think', setup: true });
    if (pendingStart && Date.now() - pendingStart.at < 8000 && !tasks.some((t) => clean(t.title) === clean(pendingStart.title) && (t.status === 'running' || t.status === 'queued'))) out.push({ key: 'start', kind: 'queued', label: pendingStart.title, title: 'Starting', tip: 'O is picking it up' });
    const live = tasks.filter((t) => t.status === 'running' || t.status === 'queued').sort((a, b) => (b.startedAt || b.createdAt || 0) - (a.startedAt || a.createdAt || 0));
    for (const t of live) out.push({ key: 'task:' + t.id, kind: t.status, label: t.title, title: t.title, tip: t.status === 'running' ? (t.statusLine || 'Working') : `Queued${t.position ? ' · #' + t.position : ''}`, t });
    const cutoff = Date.now() - 15 * 60 * 1000;
    const done = tasks.filter((t) => (t.status === 'done' || t.status === 'failed' || t.status === 'stopped') && (t.endedAt || 0) > cutoff).sort((a, b) => (b.endedAt || 0) - (a.endedAt || 0)).slice(0, 4);
    for (const t of done) out.push({ key: 'task:' + t.id, kind: t.status === 'done' ? 'done' : 'failed', label: t.title, title: t.title, tip: results.get(t.id) || clip(t.result || t.error || t.statusLine || (t.status === 'done' ? 'Done' : 'Stopped'), 120), t });
    return out;
  }

  const ICON = {
    running: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" opacity="0.3"/><path d="M8 2.5a5.5 5.5 0 0 1 5.5 5.5"/></svg>',
    queued: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" stroke-dasharray="2 2.3"/></svg>',
    done: '<svg viewBox="0 0 16 16"><path d="m4 8.3 2.6 2.6L12.2 5.3"/></svg>',
    failed: '<svg viewBox="0 0 16 16"><path d="M8 2 14.5 13.5h-13Z"/><path d="M8 6.5v3M8 11.6v.1"/></svg>',
    perm: '<svg viewBox="0 0 16 16"><path d="M8 1.8 13 3.6v4c0 3-2.1 5.4-5 6.6-2.9-1.2-5-3.6-5-6.6v-4Z"/><path d="m5.8 8 1.5 1.5 3-3"/></svg>',
    checkpoint: '<svg viewBox="0 0 16 16"><path d="M3.5 14V2.5"/><path d="M3.5 3h8l-1.6 2.75L11.5 8.5h-8"/></svg>',
    stopped: '<svg viewBox="0 0 16 16"><rect x="4.5" y="4.5" width="7" height="7" rx="1.5"/></svg>',
  };

  const live = new Map(); // key -> { item, el }

  function buildChip(item) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = `chip ${item.kind}`;
    el.innerHTML = `<span class="c-ic">${ICON[item.kind] || ICON.queued}</span><span class="c-t">${esc(clip(item.label, 28))}</span><span class="c-dot" aria-hidden="true"></span>${item.tip ? `<span class="c-tip">${esc(clip(item.tip, 90))}</span>` : ''}`;
    el.title = `${item.title}${item.tip ? ' — ' + item.tip : ''}`;
    el.addEventListener('mousedown', (e) => e.preventDefault()); // the caret stays in the input
    el.addEventListener('click', (e) => { e.stopPropagation(); toggleCard(item.key); });
    return el;
  }

  function buildMore(n) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'chip more';
    el.innerHTML = `<span class="c-t">+${n}</span>`;
    el.title = `${n} more. Open the dashboard`;
    el.addEventListener('mousedown', (e) => e.preventDefault());
    el.addEventListener('click', (e) => { e.stopPropagation(); openFull(); });
    return el;
  }

  /** Diff the wanted chips against the live ones, then spring everything to its slot. */
  function syncChips() {
    const want = clusterOn() ? chipItems() : [];
    const keys = new Set(want.map((x) => x.key));
    if (openKey && !keys.has(openKey) && openKey !== '+more') closeCard();
    const n = want.length;
    const shown = n > A.C.MAX_CHIPS ? A.C.MAX_CHIPS - 1 : n;
    const wantShown = want.slice(0, shown);
    if (n > A.C.MAX_CHIPS) wantShown.push({ key: '+more', kind: 'more', more: n - shown });
    const shownKeys = new Set(wantShown.map((x) => x.key));
    for (const [key, rec] of live) {
      if (shownKeys.has(key)) continue;
      live.delete(key);
      rec.el.inert = true;
      const c = lastCluster.circle || { cx: window.innerWidth / 2, cy: capTop() + LY.ih / 2 };
      L.spring(rec.el, { x: c.cx - 10, y: c.cy - 15, s: 0.5, o: 0 }, { stiffness: 420, damping: 36 }).then(() => rec.el.remove());
    }
    wantShown.forEach((item) => {
      let rec = live.get(item.key);
      if (rec) {
        if (item.kind === 'more') { rec.el.querySelector('.c-t').textContent = `+${item.more}`; return; }
        if (rec.item.kind !== item.kind || rec.item.label !== item.label || rec.item.tip !== item.tip) {
          const fresh = buildChip(item);
          fresh.style.transform = rec.el.style.transform; fresh.style.opacity = rec.el.style.opacity;
          const st = L.springState(rec.el);
          rec.el.replaceWith(fresh);
          L.springForget(rec.el);
          rec.el = fresh;
          if (st) L.spring(fresh, st, { from: st });
        }
        rec.item = item;
        return;
      }
      const el = item.kind === 'more' ? buildMore(item.more) : buildChip(item);
      rec = { item, el };
      live.set(item.key, rec);
      chipsEl.appendChild(el);
      const c = lastCluster.circle || { cx: window.innerWidth / 2, cy: capTop() + LY.ih / 2 };
      el.style.transform = `translate3d(${c.cx - 10}px, ${c.cy - 15}px, 0) scale(0.5)`;
      el.style.opacity = '0';
    });
    // DOM order follows slots so keyboard order matches.
    wantShown.forEach((item) => { const r = live.get(item.key); if (r) chipsEl.appendChild(r.el); });
    for (const [key, rec] of live) rec.el.classList.toggle('open', key === openKey);
    layoutCluster();
  }

  /** Positions from adapt.clusterLayout → CSS variables (circle, segment) and springs (chips, card). */
  function layoutCluster() {
    const on = clusterOn();
    const order = [...live.keys()];
    const openIndex = openKey ? order.indexOf(openKey) : -1;
    const lay = A.clusterLayout({
      cx: capCx(on ? dotWidth() : lastW), top: capTop(), ih: LY.ih, winW: window.innerWidth, typing: typing || !!clean(input.value),
      chips: live.has('+more') ? (live.size - 1) + (live.get('+more').item.more || 0) : live.size,
      openCard: openKey ? { index: openIndex, height: cardH || 120 } : null, drop: 0, dir: LY.dir,
    });
    lastCluster = lay;
    root.style.setProperty('--cdx', on ? `${lay.cdx}px` : '0px');
    root.style.setProperty('--seg-x', `${lay.seg.x}px`);
    root.style.setProperty('--seg-y', `${lay.seg.y}px`);
    root.style.setProperty('--seg-w', `${lay.seg.w}px`);
    const drop = G.barDrop(LY);
    order.forEach((key, i) => {
      const rec = live.get(key);
      const slot = lay.chips[i];
      if (!rec || !slot) return;
      L.spring(rec.el, { x: slot.x, y: slot.y + drop, s: 1, o: 1 });
    });
    if (openKey && lay.card && !cardEl.hidden) L.spring(cardEl, { x: lay.card.x, y: lay.card.y + drop, s: 1, o: 1 });
    reportShape();
  }

  // ------------------------------------------------------------ the card
  function cardSpec(item) {
    if (!item) return null;
    if (item.kind === 'perm') {
      const p = item.p;
      return { kind: 'Needs your OK', title: p.summary || p.toolName, body: `<b>${esc(p.toolName || '')}</b>${p.taskId && taskTitle(p.taskId) ? ' · ' + esc(taskTitle(p.taskId)) : ''}`,
        acts: [{ label: 'Allow', primary: true, run: () => answer(p.id, true) }, { label: 'Deny', run: () => answer(p.id, false) }] };
    }
    if (item.kind === 'checkpoint') {
      const c = item.c;
      return { kind: c.goalTitle || c.kindLabel || 'Goal checkpoint', title: c.headline || 'A goal step is ready', body: esc(clip(c.summary || '', 240)),
        acts: [{ label: 'Approve', primary: true, run: () => checkpoint(c.id, 'approve') }, { label: 'Edit', run: () => { bridge.checkpointFromNotify(c.id, 'edit'); closeCard(); } }, { label: 'Skip', quiet: true, run: () => checkpoint(c.id, 'skip') }] };
    }
    if (item.setup) return { kind: 'Setup', title: 'Claude Code is not installed', body: 'O needs it to think. The dashboard sets it up in one click.', acts: [{ label: 'Open setup', primary: true, run: () => openFull() }] };
    const t = item.t;
    if (!t) return { kind: 'Starting', title: item.label, body: 'O is picking it up.', acts: [] };
    if (t.status === 'running' || t.status === 'queued') {
      return { kind: t.status === 'running' ? `Working · ${elapsed(t.startedAt)}` : 'Queued', title: t.title, body: esc(t.statusLine || (t.status === 'running' ? 'Working' : 'Waiting for a free slot')),
        acts: [{ label: 'Open', quiet: true, run: () => { bridge.openTaskFromNotify(t.id); closeCard(); } }, { label: 'Stop', danger: true, run: () => stop(t.id) }] };
    }
    const text = results.get(t.id) || t.result || t.error || t.statusLine || '';
    return { kind: t.status === 'done' ? 'Done' : t.status === 'failed' ? 'Stopped with a snag' : 'Stopped', title: t.title, body: esc(clip(text, 360)) || (t.status === 'done' ? 'Finished.' : ''),
      acts: [{ label: 'Open', primary: true, run: () => { bridge.openTaskFromNotify(t.id); closeCard(); } }, { label: 'Dismiss', quiet: true, run: () => closeCard() }] };
  }

  function renderCard(item) {
    const spec = cardSpec(item);
    if (!spec) return false;
    cardEl.innerHTML = `<div class="card-kind">${esc(spec.kind)}</div><div class="card-title" id="card-title">${esc(clip(spec.title, 120))}</div><div class="card-body" id="card-body"${spec.body ? '' : ' hidden'}>${spec.body || ''}</div><div class="card-acts"></div>`;
    cardEl.dataset.kind = item.kind;
    const acts = cardEl.querySelector('.card-acts');
    for (const a of spec.acts) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'act' + (a.primary ? ' primary' : '') + (a.quiet ? ' quiet' : '') + (a.danger ? ' danger' : '');
      b.textContent = a.label;
      b.dataset.act = a.label.toLowerCase();
      b.addEventListener('mousedown', (e) => e.preventDefault());
      b.addEventListener('click', (e) => { e.stopPropagation(); a.run(); });
      acts.appendChild(b);
    }
    return true;
  }

  function toggleCard(key) { if (openKey === key) closeCard(); else openCard(key); }

  /** A finished task's card is information, not a question: it leaves on its own. */
  const isResultCard = (item) => !!(item && item.t && (item.kind === 'done' || item.kind === 'failed'));
  const RESULT_CARD_MS = 20000;
  function armCardTimer(item) {
    clearTimeout(cardTimer); cardTimer = null;
    if (!isResultCard(item)) return;
    const key = openKey;
    const tick = (ms) => { cardTimer = setTimeout(() => {
      if (openKey !== key) return;
      if (cardEl.matches(':hover')) { tick(5000); return; } // the pointer is on it: being read
      closeCard();
    }, ms); };
    tick(RESULT_CARD_MS);
  }

  function openCard(key) {
    const rec = live.get(key);
    if (!rec || rec.item.kind === 'more') return;
    if (!renderCard(rec.item)) return;
    const first = !openKey;
    openKey = key;
    cardEl.hidden = false;
    cardH = cardEl.offsetHeight;
    for (const [k, r] of live) r.el.classList.toggle('open', k === key);
    const st = L.springState(rec.el) || { x: 0, y: 0 };
    layoutCluster();
    const lay = lastCluster;
    if (first && lay.card) L.spring(cardEl, { x: lay.card.x, y: lay.card.y + G.barDrop(LY), s: 1, o: 1 }, { from: { x: st.x, y: st.y + 10, s: 0.9, o: 0 } });
    armCardTimer(rec.item);
    // Keyboard: the dialog takes focus (its primary action, else the card itself), unless the caret is in the
    // input (a click on a chip, the arrow keys, an approval arriving mid-sentence: typing goes on).
    if (document.activeElement !== input) {
      const target = cardEl.querySelector('.act.primary') || cardEl;
      requestAnimationFrame(() => { if (openKey === key) target.focus({ preventScroll: true }); });
    }
    if (typeof bridge.islandCard === 'function') bridge.islandCard(true);
  }

  function closeCard() {
    if (!openKey) return;
    const rec = live.get(openKey);
    const hadFocus = cardEl.contains(document.activeElement);
    openKey = null;
    cardH = 0;
    clearTimeout(cardTimer); cardTimer = null;
    for (const r of live.values()) r.el.classList.remove('open');
    const st = rec ? L.springState(rec.el) : null;
    L.spring(cardEl, { x: st ? st.x : lastCluster.seg.x, y: (st ? st.y : lastCluster.seg.y) + 10, s: 0.9, o: 0 }).then(() => { if (!openKey) cardEl.hidden = true; });
    layoutCluster();
    if (hadFocus) (typing ? input : rec && live.has(rec.item.key) ? rec.el : island).focus({ preventScroll: true });
    if (typeof bridge.islandCard === 'function') bridge.islandCard(false);
  }

  /** Enter runs the primary action (Allow / Approve / Open); Esc on an approval is Deny, elsewhere it closes. */
  cardEl.addEventListener('keydown', (e) => {
    if (!openKey) return;
    if (e.key === 'Enter' && e.target === cardEl) { e.preventDefault(); const b = cardEl.querySelector('.act.primary'); if (b) b.click(); return; }
    if (e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation();
      const deny = cardEl.dataset.kind === 'perm' ? cardEl.querySelector('.act[data-act="deny"]') : null;
      if (deny) deny.click(); else closeCard();
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); cycleCard(e.key === 'ArrowDown' ? 1 : -1); }
  });

  /** Open the next / previous chip's card (arrow keys). */
  function cycleCard(dir) {
    const keys = [...live.keys()].filter((k) => k !== '+more');
    if (!keys.length) return;
    const i = keys.indexOf(openKey);
    openCard(keys[(i + dir + keys.length) % keys.length]);
  }

  /** Refresh the open card's words (a status line moved on) without re-springing it. */
  function refreshCard() {
    if (!openKey) return;
    const rec = live.get(openKey);
    if (!rec) return;
    const focused = cardEl.contains(document.activeElement) ? (document.activeElement.dataset.act || '') : null;
    renderCard(rec.item);
    if (focused !== null) ((focused && cardEl.querySelector(`.act[data-act="${focused}"]`)) || cardEl).focus({ preventScroll: true });
    const h = cardEl.offsetHeight;
    if (h !== cardH) { cardH = h; layoutCluster(); }
  }

  // ------------------------------------------------------------ modes
  function setMode(m) {
    if (!A.MODES.includes(m) || m === mode) return;
    const wasCluster = clusterOn();
    mode = m;
    body.dataset.mode = m;
    for (const k of A.MODES) body.classList.toggle(k, k === m);
    lastKey = '';
    renderIsland();
    setIslandWidth(effectiveWidth());
    if (clusterOn() !== wasCluster) syncChips();
    if (!clusterOn() && typing) blurInput();
    if (!clusterOn() && openKey) closeCard();
    if (m === 'card' && !openKey) {
      // Only something that waits for the person opens by itself; a finished task's card is opened by a click.
      const first = [...live.keys()].find((k) => /^(perm|cp):/.test(k));
      if (first) openCard(first);
    }
    syncExpand();
  }

  /** Main's decision (renderer/adapt.js): mode, opacity and whether clicks pass through. */
  function applyAdapt(ev) {
    const m = A.cleanMode(ev && ev.mode);
    if (!m) return;
    root.style.setProperty('--adapt-opacity', String(typeof ev.opacity === 'number' ? Math.max(0.2, Math.min(1, ev.opacity)) : 1));
    setMode(m);
  }

  // ------------------------------------------------------------ typing (the segment has focus)
  /** The expand circle is in the tab order only while it is shown (hover, or typing in the cluster). */
  function syncExpand() {
    const shown = (hovering && (mode === 'cluster' || mode === 'card' || mode === 'pill')) || (typing && clusterOn());
    expand.tabIndex = shown ? 0 : -1;
  }

  function focusInput(opts) {
    if (!clusterOn()) setMode('cluster');
    if (!typing) {
      typing = true;
      body.classList.add('typing');
      if (typeof bridge.islandOpen === 'function' && !(opts && opts.fromMain)) bridge.islandOpen();
      setIgnore(false);
      layoutCluster();
      syncExpand();
    }
    requestAnimationFrame(() => input.focus());
    setTimeout(() => input.focus(), 60);
  }

  function blurInput(opts) {
    if (!typing) return;
    typing = false;
    body.classList.remove('typing');
    if (document.activeElement === input) input.blur();
    if (typeof bridge.islandClose === 'function' && !(opts && opts.fromMain)) bridge.islandClose();
    layoutCluster();
    syncExpand();
  }

  // ------------------------------------------------------------ actions
  async function submit() {
    const text = clean(input.value);
    if (!text) return;
    pendingStart = { title: clip(text, 80), at: Date.now() };
    input.value = '';
    body.classList.remove('has-text');
    renderIsland();
    syncChips();
    try {
      const r = await bridge.sendPrompt(text);
      if (r && r.ok === false) { pendingStart = null; setFlash('Could not start', r.error || 'unknown error', 'danger', 5000); syncChips(); }
    } catch (err) { pendingStart = null; setFlash('Could not start', err && err.message, 'danger', 5000); syncChips(); }
  }

  async function makeGoal(text) {
    if (!text) return;
    input.value = '';
    setFlash('Setting goal', text, 'working', 30000);
    try {
      const r = await bridge.createGoal(text);
      if (r && r.ok) setFlash('Goal set', (r.goal && r.goal.title) || text, 'success', 5000);
      else setFlash('Could not set goal', (r && r.error) || 'unknown error', 'danger', 5000);
    } catch (err) { setFlash('Could not set goal', err && err.message, 'danger', 5000); }
  }

  function openFull() {
    if (typeof bridge.showPanel === 'function') bridge.showPanel(); else bridge.togglePanel();
    blurInput();
  }

  const answered = new Set();
  function answer(id, allow) {
    if (answered.has(id)) return;
    answered.add(id);
    capMap(answered);
    bridge.respondPermission(id, allow);
    perms.delete(id);
    changed();
  }

  async function checkpoint(id, decision) {
    checkpoints = checkpoints.filter((c) => c.id !== id);
    changed();
    try { await bridge.resolveCheckpoint(id, decision); } catch (_) {}
  }

  async function stop(id) {
    try { await bridge.stopTask(id); } catch (_) {}
  }

  // ------------------------------------------------------------ agent events
  let changeTimer = null;
  function changed() {
    renderIsland();
    if (changeTimer) return;
    changeTimer = setTimeout(() => { changeTimer = null; syncChips(); refreshCard(); }, 60);
  }

  if (typeof bridge.onAgentEvent === 'function') bridge.onAgentEvent((ev) => {
    if (!ev || typeof ev !== 'object') return;
    switch (ev.type) {
      case 'tasks':
        tasks = Array.isArray(ev.tasks) ? ev.tasks : [];
        if (pendingStart && tasks.some((t) => t.status === 'running' || t.status === 'queued')) pendingStart = null;
        changed();
        break;
      case 'goals':
        goals = Array.isArray(ev.goals) ? ev.goals : goals;
        checkpoints = Array.isArray(ev.checkpoints) ? ev.checkpoints : checkpoints;
        changed();
        break;
      case 'permission':
        perms.set(ev.id, { id: ev.id, toolName: ev.toolName, summary: clean(ev.summary || ev.toolName), taskId: ev.taskId || null });
        changed();
        // an approval opens its card as soon as the cluster is up (over a finished task's card, never over another question)
        setTimeout(() => {
          const cur = openKey ? live.get(openKey) : null;
          if (clusterOn() && (!cur || isResultCard(cur.item)) && live.has('perm:' + ev.id)) openCard('perm:' + ev.id);
        }, 120);
        break;
      case 'permission-resolved':
        perms.delete(ev.id);
        changed();
        break;
      case 'tool':
        if (ev.taskId) { lastTool.set(ev.taskId, ev.name || ''); capMap(lastTool); }
        renderIsland();
        break;
      case 'status':
        if (!ev.taskId && ev.text) setFlash(toneLabel(ev.mood), ev.text, toneOf(ev.mood), 4500);
        break;
      case 'done':
        if (ev.taskId) { results.set(ev.taskId, clip(ev.result || '', 300)); capMap(results); setFlash('Done', taskTitle(ev.taskId) || clip(ev.result, 80) || 'Task finished', 'success', 5000); changed(); }
        break;
      case 'error':
        setFlash('Stopped', ev.message || 'The task hit a snag', 'danger', 5000);
        break;
      case 'evolve':
        evolve = ev.status || null;
        renderIsland();
        break;
      case 'update':
        if (ev.state === 'available') setFlash('Update ready', 'Tray menu, Update O now', 'attention', 6000);
        else if (ev.state === 'restarting') setFlash('Updating', 'Back in a moment', 'working', 4000);
        break;
      case 'island':
        if (ev.open) focusInput({ fromMain: true }); else blurInput({ fromMain: true });
        break;
      case 'island-mode':
        applyAdapt(ev);
        break;
      case 'island-layout':
        applyLayout(ev.layout);
        break;
      case 'island-fade':
        body.classList.toggle('faded', !!ev.faded);
        break;
      case 'island-prefs':
        savePrefs({ lock: ev.lock || prefs.lock || 'auto' });
        break;
      default:
        break;
    }
  });

  function toneOf(mood) { return mood === 'error' ? 'danger' : mood === 'success' ? 'success' : mood === 'idle' ? 'idle' : 'working'; }
  function toneLabel(mood) { return mood === 'error' ? 'Stopped' : mood === 'success' ? 'Done' : mood === 'browsing' ? 'Browsing' : 'Note'; }

  // ------------------------------------------------------------ first load
  async function boot() {
    try { const r = await bridge.listTasks(); if (r && Array.isArray(r.tasks)) tasks = r.tasks; } catch (_) {}
    try {
      const st = await bridge.getState();
      for (const p of (st && st.pendingPermissions) || []) perms.set(p.id, { id: p.id, toolName: p.toolName, summary: clean(p.summary || p.toolName), taskId: p.taskId || null });
    } catch (_) {}
    try { const g = await bridge.listGoals(); if (g && g.ok) { goals = g.goals || []; checkpoints = g.checkpoints || []; } } catch (_) {}
    try { const c = await bridge.checkClaude(); setupNeeded = !!(c && c.installed === false); } catch (_) {}
    changed();
  }

  // ------------------------------------------------------------ input + clicks
  island.addEventListener('click', () => { if (mode === 'full') { bridge.togglePanel(); return; } focusInput(); });
  island.addEventListener('contextmenu', (e) => { e.preventDefault(); if (typeof bridge.petMenu === 'function') bridge.petMenu(); });
  seg.addEventListener('mousedown', (e) => { if (e.target !== input) { e.preventDefault(); focusInput(); } else if (!typing) focusInput(); });
  seg.addEventListener('contextmenu', (e) => { e.preventDefault(); if (typeof bridge.petMenu === 'function') bridge.petMenu(); });
  expand.addEventListener('mousedown', (e) => e.preventDefault());
  expand.addEventListener('click', (e) => { e.stopPropagation(); openFull(); });
  input.addEventListener('focus', () => { if (!typing) focusInput(); });
  input.addEventListener('input', () => { body.classList.toggle('has-text', !!clean(input.value)); layoutCluster(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); if (openKey) closeCard(); else if (clean(input.value)) { input.value = ''; body.classList.remove('has-text'); layoutCluster(); } else blurInput(); return; }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && e.shiftKey) { e.preventDefault(); makeGoal(clean(input.value)); return; }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); const text = clean(input.value); if (text) submit(); openFull(); return; }
    if (e.key === 'Enter') { e.preventDefault(); submit(); return; }
    if (e.key === 'ArrowDown' && live.size) { e.preventDefault(); cycleCard(1); }
    if (e.key === 'ArrowUp' && live.size) { e.preventDefault(); cycleCard(-1); }
  });
  // A click on empty glass-free space ends typing (macOS and Windows pass it through instead).
  stage.addEventListener('mousedown', (e) => { if (e.target === stage || e.target === chipsEl) { if (openKey) closeCard(); else blurInput(); } });
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape' && document.activeElement !== input) { if (openKey) closeCard(); else blurInput(); } });
  window.addEventListener('blur', () => {
    if (typing) blurInput();
    const rec = openKey ? live.get(openKey) : null;
    if (rec && isResultCard(rec.item)) closeCard();
  });
  window.addEventListener('resize', () => layoutCluster());

  // ------------------------------------------------------------ click-through + hover
  // macOS and Windows forward mouse moves while ignoring clicks, so transparent pixels pass through.
  // Linux cannot forward, so the window stays solid there (it is small while compact).
  const clickThrough = bridge.platform === 'darwin' || bridge.platform === 'win32';
  let ignoring = false;
  function setIgnore(v) {
    if (!clickThrough || v === ignoring || typeof bridge.setIgnoreMouse !== 'function') return;
    ignoring = v;
    bridge.setIgnoreMouse(v);
  }
  function setHover(v) {
    if (v === hovering) return;
    hovering = v;
    body.classList.toggle('hover', v);
    syncExpand();
    if (typeof bridge.islandHover === 'function') bridge.islandHover(v);
  }
  window.addEventListener('mousemove', (e) => {
    const t = e.target;
    const solid = t !== stage && t !== chipsEl && t !== document.body && t !== document.documentElement && !!t.closest && !!t.closest('#island, #seg, #expand, .chip, #card');
    setIgnore(!solid);
    setHover(solid);
  });
  document.addEventListener('mouseleave', () => setHover(false));
  setIgnore(true);

  applyLayout(LY);
  body.dataset.mode = mode;
  body.classList.add(mode);
  setIslandWidth(effectiveWidth());
  renderIsland();
  syncExpand();
  if (typeof bridge.islandPrefs === 'function') bridge.islandPrefs({ lock: prefs.lock || 'auto' });
  boot();
})();
