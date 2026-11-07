'use strict';
// O: the Island (status capsule) and the Command bar, in one transparent always-on-top window.
// The Island reports what O is doing from the agent event stream without being asked. A click or the
// global shortcut opens the Command bar; bubbles bud off it (OLiquid) with approvals, running tasks and
// what O suggests. Enter starts a task and the bar morphs back into the Island.
(function () {
  const bridge = window.o || {};
  const L = window.OLiquid;
  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const stage = $('stage');
  const goo = $('goo');
  const island = $('island');
  const ring = $('ring');
  const lineEl = $('line');
  const metaEl = $('meta');
  const bar = $('bar');
  const input = $('input');
  const side = $('side');
  const gSide = goo.querySelector('.g-side');
  const bubblesEl = $('bubbles');
  const measure = $('measure');

  const isMac = bridge.platform === 'darwin';
  $('mod-key').textContent = isMac ? '⌘↵' : 'Ctrl ↵';
  const shownKey = isMac ? '⌘↵' : 'Ctrl ↵';

  // Geometry shared with island.css.
  const TOP = 12, BAR_H = 56, BUB_H = 48, BUB_GAP = 16, FIRST_GAP = 18;
  const MAX_BUBBLES = 6;
  const slotTop = (i) => TOP + BAR_H + FIRST_GAP + i * (BUB_H + BUB_GAP);
  const ISLAND_MIN = 220, ISLAND_MAX = 420, ISLAND_PAD = 32;

  // ------------------------------------------------------------ state fed by agent events
  let tasks = [];
  let goals = [];
  let checkpoints = [];
  const perms = new Map(); // id -> { id, toolName, summary, taskId }
  const lastTool = new Map(); // taskId -> tool name
  let evolve = null;
  let flash = null; // { k, v, tone, until }
  let setupNeeded = false;
  let pendingStart = null; // { title, at }: a prompt just sent, until the task list shows it
  let proposals = { at: 0, list: [] };
  let suggestions = { at: 0, list: [] };

  const clean = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const clip = (s, n) => { s = clean(s); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const isBrowserTool = (name) => /playwright|browser/i.test(name || '');
  const running = () => tasks.filter((t) => t.status === 'running');
  const queued = () => tasks.filter((t) => t.status === 'queued');
  const taskTitle = (id) => { const t = tasks.find((x) => x.id === id); return t ? t.title : ''; };

  function setFlash(k, v, tone, ms) {
    flash = { k, v: clean(v), tone: tone || 'idle', until: Date.now() + (ms || 4500) };
    renderIsland();
    setTimeout(renderIsland, (ms || 4500) + 30);
  }

  // ------------------------------------------------------------ the island line
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

  function lineHtml(k, v) {
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    return `<span class="k">${esc(k)}</span>${v ? `<span class="sep">·</span><span class="v">${esc(v)}</span>` : ''}`;
  }

  let lastKey = '';
  function renderIsland() {
    const s = status();
    const tone = s.tone === 'working' ? 'is-working' : s.tone === 'attention' ? 'is-attention' : s.tone === 'success' ? 'is-success' : s.tone === 'danger' ? 'is-danger' : 'is-idle';
    if (!ring.classList.contains(tone)) ring.className = 'ring ' + tone;
    const key = s.k + '|' + clip(s.v, 80);
    metaEl.textContent = s.meta || '';
    if (key === lastKey) return;
    lastKey = key;
    const html = lineHtml(s.k, clip(s.v, 80));
    measure.innerHTML = html;
    const textW = Math.min(330, measure.offsetWidth);
    const metaW = s.meta ? 60 : 0;
    const w = Math.round(Math.max(ISLAND_MIN, Math.min(ISLAND_MAX, textW + 14 + 9 + metaW + ISLAND_PAD)));
    setIslandWidth(w);
    lineEl.innerHTML = html;
    lineEl.classList.remove('swap');
    void lineEl.offsetWidth; // restart the crossfade
    lineEl.classList.add('swap');
    island.setAttribute('aria-label', `${s.k}${s.v ? ', ' + s.v : ''}. Open the command bar`);
  }

  function setIslandWidth(w) {
    stage.style.setProperty('--w', String(w));
    stage.style.setProperty('--bsx', String(Math.max(0.2, (w - 6) / 640)));
  }

  setInterval(() => { if (running().length || (flash && flash.until <= Date.now())) renderIsland(); }, 1000);

  // ------------------------------------------------------------ agent events
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
        break;
      case 'permission-resolved':
        perms.delete(ev.id);
        changed();
        break;
      case 'tool':
        if (ev.taskId) lastTool.set(ev.taskId, ev.name || '');
        renderIsland();
        break;
      case 'status':
        // Task status lines reach the island through the task list; app-level lines are shown briefly.
        if (!ev.taskId && ev.text) setFlash(toneLabel(ev.mood), ev.text, toneOf(ev.mood), 4500);
        break;
      case 'done':
        if (ev.taskId) setFlash('Done', taskTitle(ev.taskId) || clip(ev.result, 80) || 'Task finished', 'success', 5000);
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
        if (ev.open) open(); else close();
        break;
      default:
        break;
    }
  });

  function toneOf(mood) { return mood === 'error' ? 'danger' : mood === 'success' ? 'success' : mood === 'idle' ? 'idle' : 'working'; }
  function toneLabel(mood) { return mood === 'error' ? 'Stopped' : mood === 'success' ? 'Done' : mood === 'browsing' ? 'Browsing' : 'Note'; }

  let changeTimer = null;
  function changed() {
    renderIsland();
    if (!isOpen || changeTimer) return;
    changeTimer = setTimeout(() => { changeTimer = null; if (isOpen && opened) syncBubbles(); }, 80);
  }

  // ------------------------------------------------------------ first load
  async function boot() {
    try { const r = await bridge.listTasks(); if (r && Array.isArray(r.tasks)) tasks = r.tasks; } catch (_) {}
    try {
      const st = await bridge.getState();
      for (const p of (st && st.pendingPermissions) || []) perms.set(p.id, { id: p.id, toolName: p.toolName, summary: clean(p.summary || p.toolName), taskId: p.taskId || null });
    } catch (_) {}
    try { const g = await bridge.listGoals(); if (g && g.ok) { goals = g.goals || []; checkpoints = g.checkpoints || []; } } catch (_) {}
    try { const c = await bridge.checkClaude(); setupNeeded = !!(c && c.installed === false); } catch (_) {}
    renderIsland();
  }

  // ------------------------------------------------------------ bubbles: what the bar shows
  const ICONS = {
    run: '<svg viewBox="0 0 16 16"><path d="M13 3.5v4.5a2 2 0 0 1-2 2H3.5"/><path d="M6 7.5 3.5 10 6 12.5"/></svg>',
    goal: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5"/><circle cx="8" cy="8" r="2"/></svg>',
    perm: '<svg viewBox="0 0 16 16"><path d="M8 1.8 13 3.6v4c0 3-2.1 5.4-5 6.6-2.9-1.2-5-3.6-5-6.6v-4Z"/><path d="m5.8 8 1.5 1.5 3-3"/></svg>',
    checkpoint: '<svg viewBox="0 0 16 16"><path d="M3.5 14V2.5"/><path d="M3.5 3h8l-1.6 2.75L11.5 8.5h-8"/></svg>',
    task: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" opacity="0.3"/><path d="M8 2.5a5.5 5.5 0 0 1 5.5 5.5"/></svg>',
    queued: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" stroke-dasharray="2 2.3"/></svg>',
    proposal: '<svg viewBox="0 0 16 16"><path d="M8 2v3M8 11v3M2 8h3M11 8h3M4.2 4.2l1.6 1.6M10.2 10.2l1.6 1.6M11.8 4.2l-1.6 1.6M5.8 10.2l-1.6 1.6"/></svg>',
    schedule: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5"/><path d="M8 5v3.2l2 1.3"/></svg>',
    setup: '<svg viewBox="0 0 16 16"><path d="M8 2 14.5 13.5h-13Z"/><path d="M8 6.5v3M8 11.6v.1"/></svg>',
    hint: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5"/><path d="M8 7.2v3.6M8 5v.1"/></svg>',
  };

  function items() {
    const text = clean(input.value);
    const out = [];
    if (text) {
      out.push({ key: 'run', kind: 'run', t1: text, tag: ['↵', 'Start task'], primary: () => submit() });
      out.push({ key: 'goal', kind: 'goal', t1: 'Make it a long-term goal', t2: 'O plans the steps and runs them on a schedule', acts: [{ label: 'Set goal', run: () => makeGoal(text) }] });
    }
    if (setupNeeded) out.push({ key: 'setup', kind: 'setup', t1: 'Claude Code is not installed', t2: 'O needs it to think', acts: [{ label: 'Open setup', primary: true, run: () => openPanel() }] });
    for (const p of perms.values()) {
      out.push({ key: 'perm:' + p.id, kind: 'perm', t1: p.summary || p.toolName, t2: taskTitle(p.taskId) || p.toolName,
        acts: [{ label: 'Allow', primary: true, run: () => answer(p.id, true) }, { label: 'Deny', run: () => answer(p.id, false) }] });
    }
    for (const c of checkpoints) {
      out.push({ key: 'cp:' + c.id, kind: 'checkpoint', t1: c.headline || 'A goal step is ready', t2: c.goalTitle || c.kindLabel || '',
        open: () => { bridge.checkpointFromNotify(c.id, 'edit'); close(); },
        acts: [{ label: 'Approve', primary: true, run: () => checkpoint(c.id, 'approve') }, { label: 'Skip', run: () => checkpoint(c.id, 'skip') }] });
    }
    for (const t of tasks.filter((x) => x.status === 'running' || x.status === 'queued')) {
      const run = t.status === 'running';
      out.push({ key: 'task:' + t.id, kind: 'task', running: run, icon: run ? 'task' : 'queued', t1: t.title, t2: run ? (t.statusLine || 'Working') : `Queued${t.position ? ' · #' + t.position : ''}`,
        open: () => { bridge.openTaskFromNotify(t.id); close(); },
        acts: [{ label: 'Stop', run: () => stop(t.id) }] });
    }
    const goalTitles = new Set(goals.map((g) => clean(g.title).toLowerCase()));
    for (const p of proposals.list) {
      if (goalTitles.has(clean(p.title).toLowerCase())) continue;
      out.push({ key: 'prop:' + (p.proposal_id || p.title), kind: 'proposal', t1: p.title, t2: 'Suggested goal',
        acts: [{ label: 'Start', run: () => acceptProposal(p) }] });
    }
    for (const sg of suggestions.list) {
      out.push({ key: 'sug:' + (sg.id || sg.text), kind: 'schedule', t1: sg.text, t2: sg.label || 'Suggested schedule',
        acts: [{ label: 'Schedule', run: () => acceptSuggestion(sg) }] });
    }
    if (!out.length) {
      const active = goals.filter((g) => g.status === 'active').length;
      out.push({ key: 'hint', kind: 'hint', passive: true, t1: active ? `Nothing needs you. O is working on ${active} goal${active === 1 ? '' : 's'}.` : 'Nothing needs you right now.',
        tag: ['↵', 'runs a task', shownKey, 'opens the panel'] });
    }
    return out.slice(0, MAX_BUBBLES);
  }

  // key -> { item, el, blob, slot }
  const live = new Map();
  let sel = -1;

  function buildBubble(item) {
    const el = document.createElement('div');
    el.className = `bubble ol-glass k-${item.kind}${item.passive ? ' passive' : ''}${item.running ? ' running' : ''}`;
    el.setAttribute('role', item.passive ? 'note' : 'option');
    const ic = document.createElement('span');
    ic.className = 'ic';
    ic.innerHTML = ICONS[item.icon || item.kind] || ICONS.hint;
    const tx = document.createElement('span');
    tx.className = 'tx';
    const t1 = document.createElement('span'); t1.className = 't1'; t1.textContent = clip(item.t1, 120);
    tx.appendChild(t1);
    if (item.t2) { const t2 = document.createElement('span'); t2.className = 't2'; t2.textContent = clip(item.t2, 120); tx.appendChild(t2); }
    el.append(ic, tx);
    if (item.tag) {
      const tag = document.createElement('span'); tag.className = 'tag';
      for (let i = 0; i < item.tag.length; i += 2) {
        const k = document.createElement('kbd'); k.textContent = item.tag[i];
        const s = document.createElement('span'); s.textContent = item.tag[i + 1] || '';
        tag.append(k, s);
      }
      el.appendChild(tag);
    }
    if (item.acts && item.acts.length) {
      const acts = document.createElement('span'); acts.className = 'acts';
      for (const a of item.acts) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'act' + (a.primary ? ' primary' : '');
        b.textContent = a.label;
        b.addEventListener('click', (e) => { e.stopPropagation(); a.run(); });
        acts.appendChild(b);
      }
      el.appendChild(acts);
    }
    el.addEventListener('click', () => activate(item));
    el.addEventListener('mouseenter', () => setSel(-1));
    const blob = document.createElement('div');
    blob.className = 'ol-blob g-bub';
    return { item, el, blob, slot: -1 };
  }

  function place(rec, slot) {
    rec.slot = slot;
    rec.el.style.top = slotTop(slot) + 'px';
    rec.blob.style.top = (slotTop(slot) + 2) + 'px';
  }

  /** Diff the wanted bubbles against the live ones: new ones bud, gone ones retract, the rest slide. */
  function syncBubbles(first) {
    const want = isOpen ? items() : [];
    const keys = new Set(want.map((x) => x.key));
    const step = 50;
    // gone
    let gone = 0;
    for (const [key, rec] of live) {
      if (keys.has(key)) continue;
      live.delete(key);
      const dy = rec.slot === 0 ? -(slotTop(0) - (TOP + 4)) : -(BUB_H + BUB_GAP);
      L.retract([rec.blob, rec.el], { dy, duration: 220, delay: gone++ * 30, fade: [rec.el, rec.blob] }).then(() => { rec.el.remove(); rec.blob.remove(); });
    }
    // stay + new
    let born = 0;
    want.forEach((item, i) => {
      let rec = live.get(item.key);
      if (rec) {
        const was = rec.slot;
        if (rec.item.t1 !== item.t1 || rec.item.t2 !== item.t2 || rec.item.running !== item.running) {
          const t1 = rec.el.querySelector('.t1');
          const t2 = rec.el.querySelector('.t2');
          if (rec.item.kind === item.kind && t1 && !!t2 === !!item.t2 && rec.item.running === item.running) {
            // Same shape: update the words in place so the bubble does not flicker while typing.
            t1.textContent = clip(item.t1, 120);
            if (t2) t2.textContent = clip(item.t2, 120);
          } else {
            const fresh = buildBubble(item);
            fresh.el.style.top = rec.el.style.top;
            rec.el.replaceWith(fresh.el);
            rec.el = fresh.el;
          }
        }
        rec.item = item;
        if (was !== i) {
          place(rec, i);
          const dy = slotTop(was) - slotTop(i);
          for (const n of [rec.el, rec.blob]) n.animate([{ transform: `translate3d(0, ${dy}px, 0)` }, { transform: 'translate3d(0, 0, 0)' }], { duration: 280, easing: L.EASE_OUT });
        }
        return;
      }
      rec = buildBubble(item);
      live.set(item.key, rec);
      place(rec, i);
      goo.appendChild(rec.blob);
      bubblesEl.appendChild(rec.el);
      // On open, every bubble streams out of the bar; later arrivals bud off the bubble above.
      const dy = first || i === 0 ? -(slotTop(i) - (TOP + 4)) : -(BUB_H + BUB_GAP);
      const delay = born++ * step;
      L.bud([rec.blob, rec.el], { dy, delay, duration: first ? 300 + i * 26 : 300, scale: 0.94, fade: [rec.el] });
    });
    // DOM order follows slots so keyboard order matches.
    want.forEach((item) => { const r = live.get(item.key); if (r) bubblesEl.appendChild(r.el); });
    if (sel >= selectable().length) setSel(selectable().length - 1);
  }

  const selectable = () => [...live.values()].filter((r) => !r.item.passive).sort((a, b) => a.slot - b.slot);
  function setSel(i) {
    sel = i;
    const list = selectable();
    list.forEach((r, j) => { r.el.classList.toggle('sel', j === i); r.el.setAttribute('aria-selected', j === i ? 'true' : 'false'); });
    // With nothing selected, the "Start task" bubble carries the highlight (Enter runs it).
    for (const r of live.values()) if (r.item.kind === 'run') r.el.classList.toggle('dim', i >= 0);
  }

  function activate(item) {
    if (item.primary) return item.primary();
    if (item.open) return item.open();
    if (item.acts && item.acts.length === 1) return item.acts[0].run();
    return undefined;
  }

  // ------------------------------------------------------------ actions
  async function submit() {
    const text = clean(input.value);
    if (!text) return;
    pendingStart = { title: clip(text, 80), at: Date.now() };
    input.value = '';
    close();
    renderIsland();
    try {
      const r = await bridge.sendPrompt(text);
      if (r && r.ok === false) { pendingStart = null; setFlash('Could not start', r.error || 'unknown error', 'danger', 5000); }
    } catch (err) { pendingStart = null; setFlash('Could not start', err && err.message, 'danger', 5000); }
  }

  async function submitToPanel() {
    const text = clean(input.value);
    input.value = '';
    close();
    if (!text) { openPanel(); return; }
    pendingStart = { title: clip(text, 80), at: Date.now() };
    try {
      const r = await bridge.sendPrompt(text);
      if (r && r.ok && r.taskId && typeof bridge.openTaskFromNotify === 'function') bridge.openTaskFromNotify(r.taskId);
      else openPanel();
    } catch (_) { openPanel(); }
  }

  function openPanel() {
    if (typeof bridge.showPanel === 'function') bridge.showPanel(); else bridge.togglePanel();
    close();
  }

  async function makeGoal(text) {
    input.value = '';
    close();
    setFlash('Setting goal', text, 'working', 30000);
    try {
      const r = await bridge.createGoal(text);
      if (r && r.ok) setFlash('Goal set', (r.goal && r.goal.title) || text, 'success', 5000);
      else setFlash('Could not set goal', (r && r.error) || 'unknown error', 'danger', 5000);
    } catch (err) { setFlash('Could not set goal', err && err.message, 'danger', 5000); }
  }

  function answer(id, allow) {
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

  async function acceptProposal(p) {
    proposals.list = proposals.list.filter((x) => x !== p);
    changed();
    setFlash('Setting goal', p.title, 'working', 30000);
    try {
      const r = await bridge.acceptGoal(p);
      setFlash(r && r.ok ? 'Goal set' : 'Could not set goal', r && r.ok ? p.title : (r && r.error) || p.title, r && r.ok ? 'success' : 'danger', 5000);
    } catch (err) { setFlash('Could not set goal', err && err.message, 'danger', 5000); }
  }

  async function acceptSuggestion(sg) {
    suggestions.list = suggestions.list.filter((x) => x !== sg);
    changed();
    try {
      const r = await bridge.acceptSuggestion(sg);
      setFlash(r && r.ok ? 'Scheduled' : 'Could not schedule', r && r.ok ? `${sg.text}${sg.label ? ' · ' + sg.label : ''}` : (r && r.error) || sg.text, r && r.ok ? 'success' : 'danger', 5000);
    } catch (err) { setFlash('Could not schedule', err && err.message, 'danger', 5000); }
  }

  // What O proposes: cached so opening the bar stays instant and cheap.
  async function refreshSuggestions() {
    const now = Date.now();
    const jobs = [];
    if (now - suggestions.at > 2 * 60 * 1000 && typeof bridge.learnProfile === 'function') {
      suggestions.at = now;
      jobs.push(bridge.learnProfile().then((r) => { if (r && r.ok) suggestions.list = (r.suggestions || []).slice(0, 2); }).catch(() => {}));
    }
    if (now - proposals.at > 10 * 60 * 1000 && typeof bridge.proposeGoals === 'function') {
      proposals.at = now;
      jobs.push(bridge.proposeGoals().then((r) => { if (r && r.ok) proposals.list = (r.proposals || []).slice(0, 2); }).catch(() => {}));
    }
    if (typeof bridge.listGoals === 'function') {
      jobs.push(bridge.listGoals().then((g) => { if (g && g.ok) { goals = g.goals || goals; checkpoints = g.checkpoints || checkpoints; } }).catch(() => {}));
    }
    await Promise.all(jobs);
  }

  // ------------------------------------------------------------ open / close
  let isOpen = false;
  let opened = false; // the opening morph has finished laying out bubbles
  let closeTimer = null;

  function open() {
    if (isOpen) { input.focus(); return; }
    isOpen = true;
    opened = false;
    clearTimeout(closeTimer);
    if (typeof bridge.islandOpen === 'function') bridge.islandOpen();
    setIgnore(false);
    for (const rec of live.values()) { rec.el.remove(); rec.blob.remove(); }
    live.clear();
    sel = -1;
    body.classList.add('open', 'ext');
    L.bud([gSide, side], { dx: -84, delay: 90, duration: 300, scale: 0.9, fade: [side] });
    requestAnimationFrame(() => input.focus());
    setTimeout(() => input.focus(), 60);
    setTimeout(() => { if (isOpen) { opened = true; syncBubbles(true); } }, 110);
    refreshSuggestions().then(() => { if (isOpen && opened) syncBubbles(); });
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    opened = false;
    clearTimeout(changeTimer); changeTimer = null;
    if (typeof bridge.islandClose === 'function') bridge.islandClose();
    const recs = [...live.values()].sort((a, b) => b.slot - a.slot);
    live.clear();
    recs.forEach((rec, i) => {
      L.retract([rec.blob, rec.el], { dy: -(slotTop(rec.slot) - (TOP + 4)), duration: 220, delay: i * 25, fade: [rec.el, rec.blob] })
        .then(() => { rec.el.remove(); rec.blob.remove(); });
    });
    L.retract([gSide, side], { dx: -84, duration: 200, scale: 0.9, fade: [side, gSide] });
    input.blur();
    closeTimer = setTimeout(() => body.classList.remove('open'), recs.length ? 110 : 40);
    setTimeout(() => {
      if (isOpen) return;
      body.classList.remove('ext');
      input.value = '';
      bar.classList.remove('has-text');
    }, 420);
    renderIsland();
  }

  island.addEventListener('click', () => open());
  island.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (typeof bridge.petMenu === 'function') bridge.petMenu(); else open();
  });
  side.addEventListener('click', () => submitToPanel());
  input.addEventListener('input', () => {
    bar.classList.toggle('has-text', !!clean(input.value));
    setSel(-1);
    if (opened) syncBubbles();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); if (sel >= 0) setSel(-1); else close(); return; }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submitToPanel(); return; }
    if (e.key === 'Enter') {
      e.preventDefault();
      const list = selectable();
      if (sel >= 0 && list[sel]) activate(list[sel].item); else submit();
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const n = selectable().length;
      if (!n) return;
      setSel(e.key === 'ArrowDown' ? Math.min(n - 1, sel + 1) : Math.max(-1, sel - 1));
    }
  });
  // A click on empty glass-free space closes the bar (macOS and Windows pass it through instead).
  stage.addEventListener('mousedown', (e) => { if (isOpen && (e.target === stage || e.target === bubblesEl)) close(); });
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape' && isOpen && document.activeElement !== input) close(); });

  // ------------------------------------------------------------ click-through
  // macOS and Windows forward mouse moves while ignoring clicks, so transparent pixels pass through.
  // Linux cannot forward, so the window stays solid there (it is small while compact).
  const clickThrough = bridge.platform === 'darwin' || bridge.platform === 'win32';
  let ignoring = false;
  function setIgnore(v) {
    if (!clickThrough || v === ignoring || typeof bridge.setIgnoreMouse !== 'function') return;
    ignoring = v;
    bridge.setIgnoreMouse(v);
  }
  window.addEventListener('mousemove', (e) => {
    const t = e.target;
    const solid = t !== stage && t !== bubblesEl && t !== document.body && t !== document.documentElement && !!t.closest && !!t.closest('#island, #bar, #side, .bubble');
    setIgnore(!solid);
  });
  setIgnore(true);

  setIslandWidth(ISLAND_MIN);
  renderIsland();
  boot();
})();
