'use strict';
// O AI prompt panel: tasks (strip, Tasks sheet, one transcript per task), prompt box, permission cards, settings.
(function () {
  const o = window.o;
  const $ = (id) => document.getElementById(id);

  // Theme first, synchronously, so a light-theme user never sees the glass sheet for a frame.
  // The preload exposes `initialTheme` (a string) when it can; otherwise Settings are fetched below.
  const THEMES = ['glass', 'light', 'dark'];
  function applyTheme(t) {
    document.documentElement.dataset.theme = THEMES.includes(t) ? t : 'glass';
  }
  const hasInitialTheme = !!o && typeof o.initialTheme === 'string';
  let savedTheme = hasInitialTheme ? o.initialTheme : 'glass'; // what Save last persisted; previews revert to it
  if (hasInitialTheme) applyTheme(savedTheme);
  // A theme saved elsewhere (another window, a settings write) reaches the panel through `onTheme`.
  if (o && typeof o.onTheme === 'function') {
    try { o.onTheme((t) => { if (typeof t === 'string' && THEMES.includes(t)) { savedTheme = t; applyTheme(t); } }); } catch (_) {}
  }

  const log = $('log');
  const promptEl = $('prompt');
  const sendBtn = $('btn-send');
  const stopBtn = $('btn-stop');
  const stateLine = $('state-line');
  const settingsEl = $('settings');
  const setupEl = $('setup');
  const actionlogEl = $('actionlog');
  const runsEl = $('runs');
  const goalsEl = $('goals');
  const has = (name) => !!o && typeof o[name] === 'function';
  let amb = { enabled: false, away: false, state: 'UNKNOWN', live: [], sensors: [], catalog: [] }; // ambient status from main

  let currentAssistant = null; // { el, text } for the bubble being streamed
  let currentSteps = null;     // container for this turn's tool steps

  const MOOD_LABEL = {
    idle: 'idle', listening: 'listening…', thinking: 'thinking…', working: 'working…',
    browsing: 'browsing…', success: 'done ✓', error: 'hit a snag', sleeping: 'napping 💤',
  };

  // ------------------------------------------------------------ rendering

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // Tiny, safe markdown: code blocks, inline code, bold, italics, links, line breaks.
  function md(text) {
    const parts = String(text || '').split(/```/);
    return parts.map((part, i) => {
      if (i % 2 === 1) {
        const body = part.replace(/^[\w-]*\n/, '');
        return `<pre><code>${escapeHtml(body)}</code></pre>`;
      }
      let h = escapeHtml(part);
      h = h.replace(/`([^`\n]+)`/g, '<code>$1</code>');
      h = h.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
      h = h.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<i>$2</i>');
      h = h.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
      h = h.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noreferrer">$2</a>');
      h = h.replace(/^#{1,6} (.+)$/gm, '<b>$1</b>');
      h = h.replace(/^\s*[-*] (.+)$/gm, '• $1');
      h = h.replace(/\n/g, '<br>');
      return h;
    }).join('');
  }

  function atBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  }

  function scrollDown(force) {
    if (force || atBottom()) requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  }

  function addMsg(cls, html) {
    const stick = atBottom();
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    el.innerHTML = html;
    log.appendChild(el);
    scrollDown(stick);
    return el;
  }

  function endAssistantBubble() {
    if (currentAssistant) currentAssistant.el.classList.remove('streaming');
    currentAssistant = null;
  }

  const COPY_ICON = '<svg viewBox="0 0 20 20"><rect x="7" y="7" width="9.5" height="9.5" rx="2"/><path d="M13 7V5a1.5 1.5 0 0 0-1.5-1.5h-6A1.5 1.5 0 0 0 4 5v6A1.5 1.5 0 0 0 5.5 12.5H7"/></svg>';
  function copyText(text, btn) {
    const done = () => { btn.parentElement.classList.add('copied'); btn.lastChild.textContent = 'Copied'; setTimeout(() => { btn.parentElement.classList.remove('copied'); btn.lastChild.textContent = 'Copy'; }, 1500); };
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(done, () => {
      const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (_) {} ta.remove();
    });
  }

  function appendAssistant(text, isDelta) {
    const stick = atBottom();
    startChat();
    if (!currentAssistant) {
      const el = addMsg('assistant streaming', `<div class="body"></div><div class="actions"><button class="copy" title="Copy">${COPY_ICON}<span>Copy</span></button></div>`);
      currentAssistant = { el, text: '' };
      const holder = currentAssistant;
      el.querySelector('.copy').addEventListener('click', (e) => copyText(holder.text, e.currentTarget));
      currentSteps = null; // steps after this message start a new group
    }
    currentAssistant.text = isDelta ? currentAssistant.text + text : text;
    currentAssistant.el.querySelector('.body').innerHTML = md(currentAssistant.text);
    if (!isDelta) endAssistantBubble();
    scrollDown(stick);
  }

  function startChat() { document.body.classList.add('chatting'); }

  function addStep(name, summary) {
    startChat();
    endAssistantBubble();
    if (!currentSteps) {
      currentSteps = addMsg('steps', '');
    }
    const stick = atBottom();
    const row = document.createElement('div');
    row.className = 'step' + (/playwright|browser/i.test(name || '') ? ' browser' : '');
    row.innerHTML = `<span class="dot"></span><span class="txt">${escapeHtml(summary || name || 'Working')}</span>`;
    currentSteps.appendChild(row);
    // Only the latest step "spins".
    currentSteps.querySelectorAll('.step.active').forEach((s) => s.classList.remove('active'));
    row.classList.add('active');
    scrollDown(stick);
  }

  function finishSteps() {
    if (currentSteps) currentSteps.querySelectorAll('.step.active').forEach((s) => s.classList.remove('active'));
    currentSteps = null;
  }

  function prettyInput(input) {
    if (!input || typeof input !== 'object') return String(input || '');
    if (input.command) return input.command;
    if (input.file_path && input.content != null) return `${input.file_path}\n\n${String(input.content).slice(0, 600)}`;
    if (input.file_path) return input.file_path;
    try { return JSON.stringify(input, null, 2).slice(0, 1200); } catch (_) { return ''; }
  }

  function addPermissionCard(p) {
    if (p.taskId) { pendingFor.add(p.taskId); renderStrip(); renderTaskbar(); }
    if (p.taskId && p.taskId !== viewId) return; // shown when that task is opened (the strip says it needs an OK)
    if (document.querySelector(`.perm[data-id="${CSS.escape(p.id)}"]`)) return;
    startChat();
    endAssistantBubble();
    const el = addMsg('perm', `
      <div class="perm-head">O would like to use <b>${escapeHtml(p.toolName || 'a tool')}</b></div>
      <div class="perm-sum">${escapeHtml(p.summary || '')}</div>
      <pre class="perm-input">${escapeHtml(prettyInput(p.input))}</pre>
      <div class="perm-actions">
        <button class="deny">Deny</button>
        <button class="allow">Allow</button>
      </div>`, true);
    el.dataset.id = p.id;
    if (p.taskId) el.dataset.task = p.taskId;
    el.querySelector('.allow').addEventListener('click', () => respond(p.id, true));
    el.querySelector('.deny').addEventListener('click', () => respond(p.id, false));
    scrollDown(true);
    el.querySelector('.allow').focus();
  }

  function respond(id, allow) {
    o.respondPermission(id, allow);
    markPermission(id, allow);
  }

  function markPermission(id, allow) {
    const el = document.querySelector(`.perm[data-id="${CSS.escape(id)}"]`);
    if (!el || el.classList.contains('resolved')) return;
    el.classList.add('resolved', allow ? 'allowed' : 'denied');
    el.querySelector('.perm-actions').innerHTML = `<span class="verdict">${allow ? '✓ Allowed' : '✕ Denied'}</span>`;
  }

  // The composer is always enabled: while the viewed task runs, sending starts another task.
  function syncButtons() {
    const hasText = !!promptEl.value.trim();
    const t = viewed();
    const running = isActive(t);
    stopBtn.hidden = !(running && !hasText);
    sendBtn.hidden = running && !hasText;
    sendBtn.disabled = !hasText;
    sendBtn.title = running ? 'Start another task (Enter)' : t ? 'Follow up on this task (Enter)' : 'Start task (Enter)';
    promptEl.placeholder = running ? 'Give O another task…' : t ? 'Follow up, or press + for a new task…' : 'Give O a task…';
  }

  // ------------------------------------------------------------ agent events

  // Events that belong to one task (ev.taskId) draw into the chat only when that task is the one shown.
  const TASK_EVENTS = ['status', 'assistant', 'tool', 'permission', 'permission-resolved', 'done', 'error', 'permission-timeout'];
  o.onAgentEvent((ev) => {
    if (ev && ev.taskId && TASK_EVENTS.includes(ev.type)) {
      if (ev.type === 'permission') { addPermissionCard(ev); return; }
      if (ev.type === 'permission-resolved') { pendingFor.delete(ev.taskId); markPermission(ev.id, ev.allow); renderStrip(); renderTaskbar(); return; }
      if (ev.type === 'done' || ev.type === 'error') { pendingFor.delete(ev.taskId); }
      const t = tasksById.get(ev.taskId);
      if (t && ev.type === 'status' && ev.text) { t.statusLine = String(ev.text).slice(0, 80); t.mood = ev.mood; renderStrip(); renderTaskbar(); }
      if (t && ev.type === 'tool' && ev.summary) { t.statusLine = String(ev.summary).slice(0, 80); renderStrip(); renderTaskbar(); }
      if (ev.taskId !== viewId) {
        if (awaitingTask) { const q = early.get(ev.taskId) || []; q.push(ev); if (q.length < 200) early.set(ev.taskId, q); }
        if (ev.type === 'done' || ev.type === 'error') setTimeout(refreshTasks, 200);
        return;
      }
    }
    handleViewedEvent(ev);
  });

  function handleViewedEvent(ev) {
    switch (ev.type) {
      case 'status':
        if (amb.away) break; // away: the header keeps saying so
        // The text already says what O is doing; the mood only colours it (data-mood in panel.css).
        stateLine.textContent = String(ev.text || MOOD_LABEL[ev.mood] || ev.mood || '').slice(0, 80);
        stateLine.dataset.mood = ev.mood || '';
        break;
      case 'assistant':
        appendAssistant(ev.text || '', ev.delta === true);
        break;
      case 'tool':
        addStep(ev.name, ev.summary);
        break;
      case 'permission':
        addPermissionCard(ev);
        break;
      case 'permission-resolved':
        markPermission(ev.id, ev.allow);
        break;
      case 'done': {
        endAssistantBubble();
        finishSteps();
        if (ev.stopped || ev.result === '(stopped)') { addMsg('meta', '■ stopped'); stateLine.textContent = 'stopped'; setTimeout(refreshTasks, 200); break; }
        const bits = [];
        if (ev.durationMs) bits.push(`${(ev.durationMs / 1000).toFixed(1)}s`);
        if (typeof ev.costUsd === 'number' && ev.costUsd > 0) bits.push(`~$${ev.costUsd.toFixed(3)}`);
        addMsg('meta', `✓ done${bits.length ? ' · ' + bits.join(' · ') : ''}`);
        stateLine.textContent = MOOD_LABEL.success;
        if (ev.taskId) seen.add(ev.taskId);
        setTimeout(refreshTasks, 200);
        break;
      }
      case 'error':
        endAssistantBubble();
        finishSteps();
        addMsg('error', `⚠ ${escapeHtml(ev.message || 'Something went wrong')}`);
        stateLine.textContent = MOOD_LABEL.error;
        setTimeout(refreshTasks, 200);
        break;
      case 'tasks':
        setTasks(ev.tasks, ev.schedules);
        break;
      case 'view-task':
        if (ev.taskId) viewTask(ev.taskId);
        break;
      case 'update':
        showUpdate(ev);
        break;
      case 'chats':
        refreshChatList();
        break;
      case 'link':
        refreshExtension();
        break;
      case 'link-blocked':
        addMsg('meta', `Stopped before a payment step (${escapeHtml(ev.reason || 'payment')}). O does not make payments.`);
        break;
      case 'open-log':
        openActionLog();
        break;
      case 'genui':
        onGenuiFrame(ev.frame);
        break;
      case 'goals':
        setGoals(ev.goals, ev.checkpoints);
        break;
      case 'view-goal':
        if (ev.goalId) openGoal(ev.goalId, { checkpointId: ev.checkpointId, edit: !!ev.edit });
        break;
      case 'ambient':
        setAmbient(ev);
        break;
      case 'open-ambient':
        openAmbient();
        break;
      default:
        break;
    }
  }

  async function refreshState() {
    try {
      const st = await o.getState();
      if (Array.isArray(st.tasks)) setTasks(st.tasks, st.schedules);
      pendingFor.clear();
      (st.pendingPermissions || []).forEach(addPermissionCard);
      renderStrip(); renderTaskbar();
    } catch (_) {}
  }

  // ------------------------------------------------------------ composer

  // A follow-up goes to the viewed task when it has ended; otherwise (nothing viewed, or the viewed task is
  // still running) the prompt starts a new task and the chat switches to it. Running tasks keep going.
  let awaitingTask = 0;
  async function send(text) {
    text = (text != null ? text : promptEl.value).trim();
    if (!text) return;
    promptEl.value = '';
    autosize();
    const t = viewed();
    const followUp = !!(viewId && (!isActive(t) || t.status === 'paused')); // typing into a paused task resumes it
    if (!followUp) { viewId = null; viewTitle = ''; clearLog(); }
    endAssistantBubble();
    finishSteps();
    startChat();
    addMsg('user', md(text));
    scrollDown(true);
    awaitingTask++;
    let res;
    try { res = await o.sendPrompt(text, followUp ? { taskId: viewId } : {}); } catch (err) { res = { ok: false, error: err && err.message }; }
    awaitingTask--;
    if (res && res.ok === false) {
      addMsg('error', `⚠ ${escapeHtml(res.error || 'Could not send')}`);
      refreshState();
      return;
    }
    if (res && res.taskId) {
      viewId = res.taskId;
      viewTitle = text.slice(0, 60);
      seen.add(res.taskId);
      if (!tasksById.has(res.taskId)) tasksById.set(res.taskId, { id: res.taskId, title: text.replace(/\s+/g, ' ').slice(0, 60), status: res.queued ? 'queued' : 'running', statusLine: res.queued ? 'Waiting for a free slot' : 'Starting…', position: res.position || 0 });
      for (const ev of early.get(res.taskId) || []) handleViewedEvent(ev);
      early.delete(res.taskId);
      if (res.queued) addMsg('meta', `Waiting for a free slot (#${res.position}). Your other tasks keep running.`);
      setTasks();
    }
  }

  function autosize() {
    promptEl.style.height = 'auto';
    promptEl.style.height = Math.min(promptEl.scrollHeight, 180) + 'px';
    syncButtons();
  }

  promptEl.addEventListener('input', () => { autosize(); syncGoalOffer(); });
  promptEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    if (e.key === 'Escape') { if (!taskMenu.hidden) closeTaskMenu(); else if (!settingsEl.hidden) closeSettings(); else if (!actionlogEl.hidden) actionlogEl.hidden = true; else if (!runsEl.hidden) runsEl.hidden = true; else if (!goalsEl.hidden) closeGoals(); else o.hidePanel(); }
  });
  sendBtn.addEventListener('click', () => send());
  /** The composer's stop button stops the task being shown; other tasks keep running. */
  async function stopNow() {
    if (!viewId || !isActive(viewed())) return;
    endAssistantBubble();
    finishSteps();
    stateLine.textContent = 'stopping…';
    await stopTaskUI(viewId);
  }
  stopBtn.addEventListener('click', stopNow);
  // Ctrl+. or Ctrl+Shift+X in the panel also stops.
  document.addEventListener('keydown', (e) => {
    if (isActive(viewed()) && (e.ctrlKey || e.metaKey) && (e.key === '.' || (e.shiftKey && e.key.toLowerCase() === 'x'))) { e.preventDefault(); stopNow(); }
  });

  $('btn-close').addEventListener('click', () => o.hidePanel());
  $('btn-quit').addEventListener('click', () => {
    const n = [...tasksById.values()].filter(isActive).length;
    if (n && !confirm(`O is working on ${n} task${n === 1 ? '' : 's'}. Stop ${n === 1 ? 'it' : 'them'} and turn O off?`)) return;
    o.turnOff();
  });
  // ------------------------------------------------------------ chat history

  const drawer = $('drawer'), scrim = $('scrim'), chatList = $('chat-list');
  const PENCIL = '<svg viewBox="0 0 20 20"><path d="M13.6 3.9a1.6 1.6 0 0 1 2.3 2.3L7.5 14.6l-3 .8.8-3z"/></svg>';
  const TRASH = '<svg viewBox="0 0 20 20"><path d="M4 6h12M8 6V4.5h4V6M6 6l.7 9.5a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9L14 6"/></svg>';

  function clearLog() {
    endAssistantBubble();
    finishSteps();
    log.querySelectorAll('.msg').forEach((m) => m.remove());
    for (const s of surfaces.values()) if (s.kind === 'chat' && s.host) { s.surface.unmount(); s.host = null; } // kept: shown again when its task is
    document.body.classList.remove('chatting');
    setGreeting();
  }

  /** Show a saved chat's messages. */
  function renderChat(chat) {
    clearLog();
    if (!chat) return;
    for (const m of chat.messages || []) {
      if (m.role === 'user') { startChat(); addMsg('user', md(m.text)); }
      else if (m.role === 'assistant') { appendAssistant(m.text, false); }
      else if (m.role === 'steps') { for (const st of m.steps || []) addStep(st.name, st.summary); finishSteps(); }
      else if (m.role === 'error') { addMsg('error', `⚠ ${escapeHtml(m.text)}`); }
      else if (m.role === 'meta') { addMsg('meta', escapeHtml(m.text)); }
    }
    endAssistantBubble();
    finishSteps();
    scrollDown(true);
  }

  function showErr(res) {
    if (res && res.ok === false) { addMsg('error', `⚠ ${escapeHtml(res.error || 'Could not do that')}`); return true; }
    return false;
  }

  $('btn-new').addEventListener('click', newTaskView);
  $('btn-drawer-new').addEventListener('click', newTaskView);

  function openDrawer() { drawer.hidden = false; scrim.hidden = false; closeSettings(); actionlogEl.hidden = true; runsEl.hidden = true; goalsEl.hidden = true; refreshChatList(); }
  function closeDrawer() { drawer.hidden = true; scrim.hidden = true; }
  $('btn-all-tasks').addEventListener('click', () => { closeRuns(); openDrawer(); });
  $('btn-drawer-close').addEventListener('click', closeDrawer);
  scrim.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !drawer.hidden) { e.stopPropagation(); closeDrawer(); } }, true);

  function groupOf(ts) {
    const d = new Date(ts), now = new Date();
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((day(now) - day(d)) / 86400000);
    if (diff <= 0) return 'Today';
    if (diff === 1) return 'Yesterday';
    if (diff < 7) return 'Previous 7 days';
    if (diff < 30) return 'Previous 30 days';
    return 'Older';
  }

  async function refreshChatList() {
    if (drawer.hidden) return;
    let list = [];
    try { list = await o.listChats(); } catch (_) {}
    chatList.innerHTML = '';
    const real = list.filter((c) => c.count > 0 || c.current);
    $('chat-count').textContent = `${real.length} task${real.length === 1 ? '' : 's'}`;
    if (!real.length) { chatList.innerHTML = '<div class="chat-empty">No past tasks yet.</div>'; return; }
    let group = '';
    for (const c of real) {
      const g = groupOf(c.updatedAt);
      if (g !== group) { group = g; const h = document.createElement('div'); h.className = 'chat-group'; h.textContent = g; chatList.appendChild(h); }
      chatList.appendChild(chatItem(c));
    }
  }

  function chatItem(c) {
    const el = document.createElement('div');
    el.className = 'chat-item' + (c.id === viewId ? ' current' : '');
    el.title = c.title;
    // The title is a real button (Enter and Space open the chat); the icon buttons carry their own names.
    el.innerHTML = `<button type="button" class="ttl"${c.id === viewId ? ' aria-current="true"' : ''}>${escapeHtml(c.title)}</button><span class="acts"><button type="button" class="ren" title="Rename" aria-label="Rename chat">${PENCIL}</button><button type="button" class="del" title="Delete" aria-label="Delete chat">${TRASH}</button></span>`;
    el.addEventListener('click', async (e) => {
      if (e.target.closest('.acts') || el.classList.contains('confirm') || el.querySelector('input')) return;
      await viewTask(c.id);
    });
    el.querySelector('.ren').addEventListener('click', () => startRename(el, c));
    el.querySelector('.del').addEventListener('click', () => confirmDelete(el, c));
    return el;
  }

  function startRename(el, c) {
    const ttl = el.querySelector('.ttl');
    const input = document.createElement('input');
    input.value = c.title; input.maxLength = 80;
    ttl.replaceWith(input); el.querySelector('.acts').style.display = 'none';
    input.focus(); input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      if (save && input.value.trim() && input.value.trim() !== c.title) await o.renameChat(c.id, input.value.trim());
      refreshChatList();
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(true); if (e.key === 'Escape') { e.stopPropagation(); finish(false); } });
    input.addEventListener('blur', () => finish(true));
  }

  function confirmDelete(el, c) {
    el.classList.add('confirm');
    el.innerHTML = `<div class="q">Delete “${escapeHtml(c.title)}”? O forgets this chat for good.</div><button type="button" class="yes">Delete</button><button type="button" class="no">Cancel</button>`;
    el.querySelector('.no').addEventListener('click', (e) => { e.stopPropagation(); refreshChatList(); });
    el.querySelector('.yes').addEventListener('click', async (e) => {
      e.stopPropagation();
      const res = await o.deleteChats([c.id]);
      if (showErr(res)) return;
      if (c.id === viewId) newTaskView();
      refreshChatList();
    });
    el.querySelector('.yes').focus();
  }

  const delAll = $('btn-delete-all');
  let armTimer = null;
  delAll.addEventListener('click', async () => {
    if (!delAll.classList.contains('armed')) {
      delAll.classList.add('armed'); delAll.textContent = 'Click again to delete all';
      clearTimeout(armTimer);
      armTimer = setTimeout(() => { delAll.classList.remove('armed'); delAll.textContent = 'Delete all chats'; }, 3500);
      return;
    }
    clearTimeout(armTimer); delAll.classList.remove('armed'); delAll.textContent = 'Delete all chats';
    const res = await o.deleteChats('all');
    if (showErr(res)) return;
    newTaskView();
    refreshChatList();
  });

  // Opening the panel starts on a fresh task; running and recent tasks are in the strip above the composer.
  // ------------------------------------------------------------ tasks
  // Every prompt is a task with its own transcript. `viewId` is the task shown in the chat (null = a new,
  // empty task). Events for other tasks only update the task strip; switching tasks loads that transcript.

  const tasksById = new Map(); // id -> snapshot from main ({id, title, status, statusLine, ...})
  let schedules = [];
  let viewId = null;
  const seen = new Set();      // ended tasks the user has looked at (they leave the strip)
  const early = new Map();     // task id -> events that arrived before we knew to show that task
  const ACTIVE = ['queued', 'running', 'paused'];
  const isActive = (t) => !!t && ACTIVE.includes(t.status);
  const viewed = () => (viewId ? tasksById.get(viewId) || null : null);
  const STATUS_WORD = { queued: 'Waiting', running: 'Working', paused: 'Paused', done: 'Done', failed: 'Needs you', stopped: 'Stopped' };
  const STOP_ICON = '<svg viewBox="0 0 20 20"><rect x="5" y="5" width="10" height="10" rx="2"/></svg>';

  function relTime(ms) {
    if (!ms) return '';
    const d = new Date(ms), now = new Date();
    const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((day(d) - day(now)) / 86400000);
    if (diff === 0) return `today ${time}`;
    if (diff === 1) return `tomorrow ${time}`;
    if (diff === -1) return `yesterday ${time}`;
    return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) + ' ' + time;
  }

  const pendingFor = new Set(); // task ids with an approval waiting

  /** One task row (strip and Tasks sheet): open button + stop button for an active task. */
  function taskRow(t, { compact } = {}) {
    const row = document.createElement('div');
    row.className = 'task-row';
    row.setAttribute('role', 'listitem');
    row.dataset.status = t.status;
    row.dataset.id = t.id;
    if (t.id === viewId) row.classList.add('current');
    const asks = pendingFor.has(t.id);
    const line = asks ? 'Needs your OK' : t.status === 'queued' ? `Waiting for a free slot${t.position ? ` (#${t.position})` : ''}` : (t.status === 'done' && t.result ? t.result : t.statusLine) || STATUS_WORD[t.status] || '';
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'task-open';
    open.innerHTML = `<span class="tdot" aria-hidden="true"></span><span class="ttext"><span class="ttl">${escapeHtml(t.title)}</span><span class="tline">${escapeHtml(String(line).split('\n')[0])}</span></span>${compact ? '' : `<span class="tword">${escapeHtml(asks ? 'Needs you' : STATUS_WORD[t.status] || '')}</span>`}`;
    open.setAttribute('aria-label', `${t.title}. ${asks ? 'Needs your OK' : STATUS_WORD[t.status] || ''}. ${line}`);
    if (t.id === viewId) open.setAttribute('aria-current', 'true');
    if (asks) row.classList.add('asks');
    open.addEventListener('click', () => { closeRuns(); viewTask(t.id); });
    row.appendChild(open);
    if (isActive(t)) {
      const stop = document.createElement('button');
      stop.type = 'button';
      stop.className = 'task-stop';
      stop.title = 'Stop';
      stop.setAttribute('aria-label', `Stop ${t.title}`);
      stop.innerHTML = STOP_ICON;
      stop.addEventListener('click', (e) => { e.stopPropagation(); stopTaskUI(t.id); });
      row.appendChild(stop);
    }
    return row;
  }

  function scheduleRow(s, { withCancel = true } = {}) {
    const row = document.createElement('div');
    row.className = 'task-row sched';
    row.setAttribute('role', 'listitem');
    row.dataset.status = 'scheduled';
    row.innerHTML = `<div class="task-open static"><span class="tdot" aria-hidden="true"></span><span class="ttext"><span class="ttl">${escapeHtml(s.title)}</span><span class="tline">${escapeHtml(s.label || '')}${s.nextRun ? ` · next ${escapeHtml(relTime(s.nextRun))}` : ''}</span></span></div>`;
    if (withCancel) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'task-cancel';
      b.textContent = 'Cancel';
      b.setAttribute('aria-label', `Cancel schedule ${s.title}`);
      b.addEventListener('click', async () => { if (has('cancelSchedule')) { await o.cancelSchedule(s.id); refreshTasks(); } });
      row.appendChild(b);
    }
    return row;
  }

  const strip = $('task-strip');
  const STRIP_MAX = 3;
  /** Above the composer: every running / waiting task, finished ones not looked at yet, and the schedule count. */
  function renderStrip() {
    const all = [...tasksById.values()];
    const active = all.filter(isActive);
    const others = active.filter((t) => t.id !== viewId); // the task on screen has its own bar above the chat
    const fresh = all.filter((t) => !isActive(t) && !seen.has(t.id) && t.id !== viewId && t.endedAt && Date.now() - t.endedAt < 30 * 60 * 1000 && t.status !== 'stopped');
    const rows = [...others.sort((a, b) => (a.status === b.status ? 0 : a.status === 'running' ? -1 : 1)), ...fresh];
    strip.innerHTML = '';
    rows.slice(0, STRIP_MAX).forEach((t) => strip.appendChild(taskRow(t, { compact: true })));
    const more = rows.length - STRIP_MAX;
    const extras = [];
    if (more > 0) extras.push(`${more} more`);
    if (schedules.length) extras.push(`${schedules.length} scheduled · next ${relTime(schedules[0].nextRun)}`);
    if (extras.length) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'strip-more';
      b.textContent = extras.join(' · ');
      b.addEventListener('click', openRuns);
      strip.appendChild(b);
    }
    strip.hidden = !strip.children.length;
    document.body.classList.toggle('has-strip', !strip.hidden);
    const running = active.filter((t) => t.status === 'running').length;
    const badge = $('tasks-badge');
    badge.hidden = !active.length;
    badge.textContent = String(active.length);
    $('btn-runs').setAttribute('aria-label', active.length ? `Tasks, ${running} running` : 'Tasks');
    document.body.classList.toggle('busy', running > 0);
  }

  function renderTaskSheet() {
    if (runsEl.hidden) return;
    const all = [...tasksById.values()];
    const now = $('tasks-now'), sch = $('tasks-scheduled'), rec = $('tasks-recent');
    const fill = (el, items, empty, fn) => {
      el.innerHTML = '';
      if (!items.length) { const d = document.createElement('div'); d.className = 'task-empty'; d.textContent = empty; el.appendChild(d); return; }
      items.forEach((x) => el.appendChild(fn(x)));
    };
    fill(now, all.filter(isActive), 'Nothing running. Give me a task, or several.', (t) => taskRow(t));
    fill(sch, schedules, 'No schedules. Try “Every weekday at 8, summarize what’s new in my inbox”.', (s) => scheduleRow(s));
    fill(rec, all.filter((t) => !isActive(t)).slice(0, 12), 'Finished tasks show up here.', (t) => taskRow(t));
    const running = all.filter((t) => t.status === 'running').length, queued = all.filter((t) => t.status === 'queued').length;
    $('tasks-sub').textContent = running || queued || schedules.length
      ? [running && `${running} running`, queued && `${queued} waiting`, schedules.length && `${schedules.length} scheduled`].filter(Boolean).join(' · ')
      : 'Everything O is doing, waiting to do, or will do later';
  }

  /** The bar above the chat: the viewed task's title, live status and its overflow menu. */
  function renderTaskbar() {
    const t = viewed();
    const bar = $('taskbar');
    bar.hidden = !viewId;
    if (!viewId) { closeTaskMenu(); return; }
    bar.dataset.status = t ? (pendingFor.has(t.id) ? 'asks' : t.status) : 'done';
    $('taskbar-title').textContent = t ? t.title : (viewTitle || 'Task');
    $('taskbar-status').textContent = t ? (pendingFor.has(t.id) ? 'Needs your OK' : t.status === 'queued' ? 'Waiting for a free slot' : t.status === 'running' ? (t.statusLine || 'Working') : STATUS_WORD[t.status]) : 'Done';
    $('task-menu').querySelector('[data-act="stop"]').hidden = !isActive(t);
    $('task-menu').querySelector('[data-act="redo"]').hidden = isActive(t);
  }
  let viewTitle = '';

  function setTasks(list, scheds) {
    if (Array.isArray(list)) {
      tasksById.clear();
      for (const t of list) tasksById.set(t.id, t);
    }
    if (Array.isArray(scheds)) schedules = scheds;
    renderStrip();
    renderTaskSheet();
    renderTaskbar();
    syncButtons();
    renderStateLine();
  }

  function renderStateLine() {
    const t = viewed();
    if (amb.away) { stateLine.textContent = awayLine(); stateLine.dataset.mood = ''; stateLine.dataset.away = '1'; return; }
    delete stateLine.dataset.away;
    stateLine.textContent = t ? (t.status === 'running' ? 'working on it' : (STATUS_WORD[t.status] || t.status).toLowerCase()) : summaryLine();
    stateLine.dataset.mood = t && t.status === 'running' ? (t.mood || 'working') : '';
  }

  function runningCount() { return [...tasksById.values()].filter((t) => t.status === 'running').length; }
  function summaryLine() {
    const running = runningCount();
    return running ? `${running} task${running === 1 ? '' : 's'} running` : 'ready';
  }
  /** The header while the person is away (ambient sensing): "Away · 3 tasks running". */
  function awayLine() {
    const running = runningCount();
    return running ? `Away · ${running} task${running === 1 ? '' : 's'} running` : 'Away';
  }

  async function refreshTasks() {
    if (!has('listTasks')) return;
    try { const r = await o.listTasks(); setTasks(r.tasks, r.schedules); } catch (_) {}
  }

  /** Show one task's transcript (from main's per-task chat store) and its pending approvals. */
  async function viewTask(id) {
    if (!id) return;
    closeTaskMenu();
    let res = null;
    if (has('viewTask')) { try { res = await o.viewTask(id); } catch (_) { res = null; } }
    if (!res || res.ok === false) { if (res) showErr(res); return; }
    viewId = id;
    seen.add(id);
    early.delete(id);
    viewTitle = (res.chat && res.chat.title) || (res.task && res.task.title) || '';
    if (res.task) tasksById.set(id, res.task);
    renderChat(res.chat);
    if (res.task && isActive(res.task)) { startChat(); }
    showTaskSurface(id);
    refreshState();
    setTasks();
    closeDrawer();
    promptEl.focus();
  }

  function newTaskView() {
    viewId = null;
    viewTitle = '';
    clearLog();
    setTasks();
    closeDrawer();
    promptEl.focus();
  }

  async function stopTaskUI(id) {
    const t = tasksById.get(id);
    if (t) { t.status = t.status === 'queued' ? 'stopped' : t.status; t.statusLine = 'Stopping…'; setTasks(); }
    try { if (has('stopTask')) await o.stopTask(id); else await o.cancel(id); } catch (_) {}
    setTimeout(refreshTasks, 300);
  }

  // ------------------------------------------------------------ task overflow menu
  const taskMenu = $('task-menu'), taskMenuBtn = $('btn-task-menu');
  function closeTaskMenu() { taskMenu.hidden = true; taskMenuBtn.setAttribute('aria-expanded', 'false'); }
  function openTaskMenu() {
    taskMenu.hidden = false;
    taskMenuBtn.setAttribute('aria-expanded', 'true');
    const first = [...taskMenu.querySelectorAll('button')].find((b) => !b.hidden);
    if (first) first.focus();
  }
  taskMenuBtn.addEventListener('click', () => (taskMenu.hidden ? openTaskMenu() : closeTaskMenu()));
  taskMenu.addEventListener('keydown', (e) => {
    const items = [...taskMenu.querySelectorAll('button')].filter((b) => !b.hidden);
    const i = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    if (e.key === 'Escape') { e.stopPropagation(); closeTaskMenu(); taskMenuBtn.focus(); }
  });
  document.addEventListener('click', (e) => { if (!taskMenu.hidden && !e.target.closest('#task-menu, #btn-task-menu')) closeTaskMenu(); });
  taskMenu.addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b || !viewId) return;
    closeTaskMenu();
    const id = viewId;
    if (b.dataset.act === 'record') openActionLog({ taskId: id, title: $('taskbar-title').textContent });
    else if (b.dataset.act === 'stop') stopTaskUI(id);
    else if (b.dataset.act === 'redo' && has('redoTask')) {
      const r = await o.redoTask(id);
      if (!showErr(r)) { addMsg('meta', 'Redoing this task from scratch'); refreshTasks(); }
    }
  });

  // ------------------------------------------------------------ settings

  const FIELDS = ['theme', 'openAtLogin', 'maxParallelTasks', 'permissionMode', 'browserMode', 'extensionToken', 'browserProfile', 'screenAccess', 'quiet', 'autoUpdateCheck', 'updateRepo', 'updateBranch', 'updatePath', 'updateToken', 'browserHeadless', 'browser', 'browserPath', 'cwd', 'claudePath', 'model',
    'vllmUrl', 'vllmModel', 'screenpipeDb', 'sandboxRoot', 'maxRemediationAttempts', 'enginePython',
    'proxyEnabled', 'proxyPort', 'proxyConcurrency', 'proxyMode',
    'mcpServers', 'highRiskSandbox', 'e2bApiKey', 'dockerImage', 'swarmEnabled', 'cuCandidates', 'cuMaxHeals', 'vllmVisionModel', 'genuiEnabled', 'genuiSandbox',
    'cuStepsPerTurn', 'cuMaxTurns', 'cuGroundConfidence', 'cuStepCandidates', 'cuFeasibilityGate', 'cuZoomGrounding', 'cuComparativeRanking', 'cuInterfaceBuilder',
    'ambientEnabled', 'absentAfterMin', 'briefSpeak', 'sensorWifi', 'sensorSonar', 'sensorCamera', 'sensorHeartRate'];

  if ($('set-theme')) $('set-theme').addEventListener('change', () => applyTheme($('set-theme').value)); // live preview; Save persists it

  async function loadSettings() {
    const s = await o.getSettings();
    savedTheme = THEMES.includes(s.theme) ? s.theme : 'glass';
    applyTheme(savedTheme);
    for (const k of FIELDS) {
      const el = $('set-' + k);
      if (!el) continue;
      if (el.type === 'checkbox') el.checked = !!s[k];
      else if (el.tagName === 'SELECT' && s[k] == null) el.selectedIndex = 0;
      else el.value = s[k] == null ? '' : s[k];
      if (k === 'browserMode' && !el.dataset.watch) { el.dataset.watch = '1'; el.addEventListener('change', async () => { await o.setSettings({ browserMode: el.value }); refreshExtension({ card: true }); }); }
    }
  }

  // An unsaved Appearance pick is only a preview: every way of leaving Settings puts the saved theme back.
  function revertThemePreview() { applyTheme(savedTheme); }
  function openSettings() {
    settingsEl.hidden = false; actionlogEl.hidden = true; runsEl.hidden = true; goalsEl.hidden = true;
    loadSettings().then(refreshAmbient); refreshExtension(); refreshProxy(); renderSettingsSchedules(); refreshLearned();
  }

  // ------------------------------------------------------------ ambient sensing (Settings → Ambient, header)
  // Sensors are off by default and turn on one at a time, each only through the confirmation sheet
  // (main refuses any other way). Turning one off, or the master switch, applies at once.
  const SENSOR_KEYS = ['sensorWifi', 'sensorSonar', 'sensorCamera', 'sensorHeartRate'];
  const glyph = $('sensing-glyph');

  function setAmbient(st) {
    if (!st || typeof st !== 'object') return;
    amb = { ...amb, ...st, live: Array.isArray(st.live) ? st.live : [], sensors: Array.isArray(st.sensors) ? st.sensors : amb.sensors, catalog: Array.isArray(st.catalog) ? st.catalog : amb.catalog };
    const live = amb.live;
    glyph.hidden = !live.length;
    const tip = live.length ? `Sensing: ${live.join(', ')}. Open Ambient settings` : '';
    glyph.title = tip;
    glyph.setAttribute('aria-label', tip || 'Ambient settings');
    renderStateLine();
    if (!settingsEl.hidden) renderSensorRows();
  }

  function renderSensorRows() {
    const list = $('ambient-sensors');
    list.classList.toggle('off', !$('set-ambientEnabled').checked);
    for (const row of list.querySelectorAll('.sensor-row')) {
      const key = row.dataset.key;
      const cat = (amb.catalog || []).find((c) => c.key === key);
      const av = row.querySelector('.s-avail');
      row.querySelector('input').disabled = !$('set-ambientEnabled').checked;
      if (!cat || cat.available == null) { av.dataset.state = 'unknown'; av.textContent = amb.enabled ? 'Status unknown: the engine has not reported this sensor.' : ''; continue; }
      av.dataset.state = cat.available ? 'ok' : 'no';
      av.textContent = cat.available ? (cat.on && amb.live.includes(cat.label) ? 'Live' : 'Available on this computer') : `Not available: ${cat.reason || 'no supported hardware found'}`;
    }
  }

  async function refreshAmbient() {
    if (has('ambientStatus')) { try { setAmbient(await o.ambientStatus()); } catch (_) {} }
    renderSensorRows();
  }

  function openAmbient() {
    openSettings();
    requestAnimationFrame(() => { const h = $('ambient-h'); if (h) h.scrollIntoView({ block: 'start' }); });
  }

  const confirmEl = $('sensor-confirm');
  let confirmKey = null;
  let confirmReturn = null;
  function openConfirm(key) {
    const row = document.querySelector(`.sensor-row[data-key="${key}"]`);
    if (!row) return;
    const cat = (amb.catalog || []).find((c) => c.key === key);
    confirmKey = key;
    confirmReturn = document.activeElement;
    $('sc-name').textContent = (cat && cat.label) || row.querySelector('.s-name').textContent;
    $('sc-line').textContent = (cat && cat.line) || row.querySelector('.s-line').textContent;
    $('sc-reads').textContent = (cat && cat.reads) || row.dataset.reads || '';
    confirmEl.hidden = false;
    $('sc-ok').focus();
  }
  function closeConfirm() {
    confirmEl.hidden = true;
    confirmKey = null;
    if (confirmReturn && confirmReturn.focus) confirmReturn.focus();
  }
  $('sc-cancel').addEventListener('click', closeConfirm);
  confirmEl.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); closeConfirm(); } });
  confirmEl.addEventListener('click', (e) => { if (e.target === confirmEl) closeConfirm(); });
  $('sc-ok').addEventListener('click', async () => {
    const key = confirmKey;
    if (!key) return;
    let res = null;
    try { res = await o.ambientEnableSensor(key); } catch (err) { res = { ok: false, error: err && err.message }; }
    if (res && res.ok) $('set-' + key).checked = true;
    closeConfirm();
    refreshAmbient();
  });
  for (const key of SENSOR_KEYS) {
    const el = $('set-' + key);
    el.addEventListener('change', async () => {
      if (el.checked) { el.checked = false; openConfirm(key); return; } // on: only through the sheet
      await o.setSettings({ [key]: false });                            // off: at once
      refreshAmbient();
    });
  }
  $('set-ambientEnabled').addEventListener('change', async () => {
    await o.setSettings({ ambientEnabled: $('set-ambientEnabled').checked });
    renderSensorRows();
    refreshAmbient();
  });
  glyph.addEventListener('click', openAmbient);
  if (has('ambientStatus')) refreshAmbient();

  // Settings → Scheduled tasks: the same rows as the Tasks sheet, each cancellable.
  async function renderSettingsSchedules() {
    const box = $('settings-schedules');
    if (has('listSchedules')) { try { schedules = await o.listSchedules(); } catch (_) {} }
    box.innerHTML = '';
    if (!schedules.length) { const d = document.createElement('div'); d.className = 'task-empty'; d.textContent = 'Nothing scheduled.'; box.appendChild(d); return; }
    schedules.forEach((x) => box.appendChild(scheduleRow(x)));
  }

  // Settings → O has learned: what the engine's /learn profile holds, each line removable, plus
  // one-tap "Schedule this?" offers for habits it noticed.
  async function refreshLearned() {
    const list = $('learned-list'), sug = $('learned-suggestions');
    list.innerHTML = ''; sug.innerHTML = '';
    if (!has('learnProfile')) { list.innerHTML = '<div class="task-empty">Not available in this build.</div>'; return; }
    let r;
    try { r = await o.learnProfile(); } catch (err) { r = { ok: false, error: err && err.message }; }
    if (!r || !r.ok) { const d = document.createElement('div'); d.className = 'task-empty'; d.textContent = (r && r.error) || 'Could not read what O has learned.'; list.appendChild(d); return; }
    for (const sg of r.suggestions || []) {
      const row = document.createElement('div');
      row.className = 'learn-row suggest';
      row.innerHTML = `<span class="ltext"><span class="lkind">Noticed</span>${escapeHtml(sg.text)}<span class="lsub">${escapeHtml(sg.label)}</span></span>`;
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'learn-accept'; b.textContent = 'Schedule this';
      b.addEventListener('click', async () => {
        b.disabled = true;
        const res = await o.acceptSuggestion(sg);
        if (res && res.ok) { row.remove(); renderSettingsSchedules(); refreshTasks(); } else { b.disabled = false; b.textContent = 'Could not schedule'; }
      });
      row.appendChild(b);
      sug.appendChild(row);
    }
    const items = r.items || [];
    if (!items.length) { const d = document.createElement('div'); d.className = 'task-empty'; d.textContent = 'Nothing yet. O learns as you give it tasks and correct its results.'; list.appendChild(d); return; }
    for (const it of items) {
      const row = document.createElement('div');
      row.className = 'learn-row';
      row.innerHTML = `<span class="ltext"><span class="lkind">${escapeHtml(it.kind)}</span>${escapeHtml(it.text)}</span>`;
      if (it.id) {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'learn-forget'; b.title = 'Forget this';
        b.setAttribute('aria-label', `Forget: ${it.text}`);
        b.innerHTML = TRASH;
        b.addEventListener('click', async () => { const res = await o.learnForget(it.id); if (res && res.ok) row.remove(); });
        row.appendChild(b);
      }
      list.appendChild(row);
    }
  }
  $('btn-view-record').addEventListener('click', () => openActionLog());
  function closeSettings() {
    if (settingsEl.hidden) return;
    settingsEl.hidden = true;
    revertThemePreview();
  }
  $('btn-settings-close').addEventListener('click', closeSettings);
  $('mode-chip').addEventListener('click', openSettings);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !settingsEl.hidden && document.activeElement !== promptEl) closeSettings(); });

  // Greeting by time of day, and the composer chip showing where O browses.
  // The empty state is one quiet line; it does not change with the time of day.
  function setGreeting() { $('hero-greeting').textContent = 'What should we get done?'; }
  const MODE_LABEL = { link: 'All my tabs', mine: 'Picked tabs', separate: 'Own browser' };
  async function refreshModeChip() {
    let st, ln;
    try { st = await o.getSettings(); } catch (_) { return; }
    const chip = $('mode-chip');
    chip.classList.remove('ok', 'warn');
    let label = MODE_LABEL[st.browserMode] || 'Browser';
    if (st.browserMode === 'link') {
      try { ln = await o.linkStatus(); } catch (_) { ln = null; }
      if (ln && ln.connected) { chip.classList.add('ok'); label += ` · ${ln.tabs}`; } else { chip.classList.add('warn'); label = 'O Link: not connected'; }
    } else if (st.browserMode === 'separate') chip.classList.add('ok');
    $('mode-label').textContent = label;
  }

  $('btn-settings').addEventListener('click', () => (settingsEl.hidden ? openSettings() : closeSettings()));

  // Engine requirements: one line per item, green or red, with the fix under red ones.
  async function checkRequirements() {
    const status = $('engine-status'), box = $('requirements');
    if (!has('checkRequirements')) { status.textContent = 'Requirements check is not available in this build.'; return; }
    status.textContent = 'Checking…';
    let res;
    try { res = await o.checkRequirements(); } catch (err) { status.textContent = `Could not check: ${err && err.message ? err.message : err}`; return; }
    const items = Array.isArray(res) ? res : (res && (res.items || res.checks || res.requirements)) || [];
    box.innerHTML = '';
    for (const it of items) {
      const ok = !!(it.ok != null ? it.ok : it.installed);
      const row = document.createElement('div');
      row.className = 'req ' + (ok ? 'ok' : 'bad');
      row.innerHTML = `<span class="dot"></span><span class="name">${escapeHtml(it.name || it.id || '')}</span>${it.detail ? `<span class="det">— ${escapeHtml(it.detail)}</span>` : ''}${!ok && it.fix ? `<span class="fix">${escapeHtml(it.fix)}</span>` : ''}`;
      box.appendChild(row);
    }
    box.hidden = !items.length;
    const bad = items.filter((it) => !(it.ok != null ? it.ok : it.installed)).length;
    status.textContent = !items.length ? 'Nothing to check.' : bad ? `${bad} of ${items.length} requirement${items.length === 1 ? '' : 's'} missing` : `✓ All ${items.length} requirements met`;
  }
  $('btn-check-requirements').addEventListener('click', checkRequirements);

  // API proxy: status line, masked token with copy/regenerate.
  let proxyToken = '';
  async function refreshProxy() {
    const status = $('proxy-status'), tok = $('proxy-token');
    if (!has('proxyStatus')) { status.textContent = 'Not available in this build.'; tok.value = ''; proxyToken = ''; return; }
    let st;
    try { st = await o.proxyStatus(); } catch (err) { status.textContent = `Could not read status: ${err && err.message ? err.message : err}`; return; }
    st = st || {};
    proxyToken = st.token || '';
    tok.value = proxyToken;
    const url = st.url || (st.port ? `http://127.0.0.1:${st.port}/v1` : '');
    status.textContent = !st.enabled ? 'Off (enable below)' : `${url || 'http://127.0.0.1'} · ${st.running ? 'running' : 'stopped'}`;
    status.classList.toggle('ok', !!(st.enabled && st.running));
  }
  $('btn-proxy-copy').addEventListener('click', (e) => { if (proxyToken) copyText(proxyToken, e.currentTarget); });
  let regenTimer = null;
  const regenBtn = $('btn-proxy-regen');
  regenBtn.addEventListener('click', async () => {
    if (!has('proxyRegenerateToken')) return;
    if (!regenBtn.classList.contains('armed')) {
      regenBtn.classList.add('armed'); regenBtn.textContent = 'Click again (old token stops working)';
      clearTimeout(regenTimer);
      regenTimer = setTimeout(() => { regenBtn.classList.remove('armed'); regenBtn.textContent = 'Regenerate'; }, 3500);
      return;
    }
    clearTimeout(regenTimer); regenBtn.classList.remove('armed'); regenBtn.textContent = 'Regenerate';
    try { await o.proxyRegenerateToken(); } catch (_) {}
    refreshProxy();
  });

  // ------------------------------------------------------------ action log sheet

  const alList = $('actionlog-list'), alSub = $('actionlog-sub'), alChain = $('actionlog-chain'), alTaskChip = $('actionlog-task');
  const AL_CAP = 500;
  let alEntries = [];
  let alFilter = 'all';
  let alUnread = 0;
  let alSubscribed = false;
  let alTask = null; // set by a task's "What did O do?": only that task's entries
  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

  // The activity record is a background safety feature: no badge on the front UI, only a count kept here.
  function setLogBadge(n) { alUnread = Math.max(0, Number(n) || 0); }

  function isErrorEntry(e) {
    if (!e) return false;
    if (e.kind === 'result' && e.detail && e.detail.is_error) return true;
    return /^Error\b/.test(String(e.summary || ''));
  }

  function matchesFilter(e) {
    if (alTask && !(e.detail && e.detail.taskId === alTask)) return false;
    switch (alFilter) {
      case 'tools': return e.kind === 'tool' || e.kind === 'result';
      case 'engine': return e.source === 'engine' || ['graph', 'cache', 'remediate', 'verify'].includes(e.kind);
      case 'policy': return e.kind === 'policy' || e.kind === 'settings';
      case 'errors': return isErrorEntry(e);
      default: return true;
    }
  }

  function entryTime(e) {
    const ms = typeof e.ts === 'number' ? e.ts * 1000 : Date.parse(e.iso || '') || Date.now();
    try { return timeFmt.format(new Date(ms)); } catch (_) { return ''; }
  }

  let alRowSeq = 0;
  function alRow(e) {
    const id = 'al-d-' + (alRowSeq++);
    const row = document.createElement('div');
    row.className = 'alrow' + (isErrorEntry(e) ? ' error' : '');
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'alrow-head';
    head.setAttribute('aria-expanded', 'false');
    head.setAttribute('aria-controls', id);
    head.innerHTML = `<span class="time">${escapeHtml(entryTime(e))}</span><span class="kind ${escapeHtml(e.source === 'engine' ? 'engine' : '')}">${escapeHtml(e.kind || 'note')}</span><span class="sum">${escapeHtml(e.summary || '')}</span>`;
    const pre = document.createElement('pre');
    pre.id = id;
    pre.hidden = true;
    pre.tabIndex = 0;
    head.addEventListener('click', () => {
      const open = head.getAttribute('aria-expanded') === 'true';
      head.setAttribute('aria-expanded', open ? 'false' : 'true');
      if (open) { pre.hidden = true; return; }
      if (!pre.textContent) {
        let text = '';
        try { text = JSON.stringify(e.detail == null ? {} : e.detail, null, 2); } catch (_) { text = String(e.detail); }
        pre.textContent = text;
      }
      pre.hidden = false;
    });
    row.appendChild(head);
    row.appendChild(pre);
    return row;
  }

  function alAtBottom() { return actionlogEl.scrollHeight - actionlogEl.scrollTop - actionlogEl.clientHeight < 60; }

  function renderActionLog() {
    const stick = alAtBottom();
    alList.innerHTML = '';
    const shown = alEntries.filter(matchesFilter);
    if (!shown.length) {
      const d = document.createElement('div');
      d.className = 'al-empty';
      d.textContent = alTask ? 'Nothing recorded for this task yet.' : alEntries.length ? 'Nothing matches this filter.' : 'Nothing recorded yet.';
      alList.appendChild(d);
    } else {
      const frag = document.createDocumentFragment();
      for (const e of shown) frag.appendChild(alRow(e));
      alList.appendChild(frag);
    }
    if (stick) requestAnimationFrame(() => { actionlogEl.scrollTop = actionlogEl.scrollHeight; });
  }

  function setLogSubtitle(n) {
    alSub.textContent = n ? `${n} action${n === 1 ? '' : 's'} since you last looked` : 'Nothing new since you last looked';
  }

  function appendLogEntry(entry) {
    if (!entry || typeof entry !== 'object') return;
    alEntries.push(entry);
    if (alEntries.length > AL_CAP) alEntries.splice(0, alEntries.length - AL_CAP);
    if (actionlogEl.hidden) return;
    if (!matchesFilter(entry)) return;
    const stick = alAtBottom();
    const empty = alList.querySelector('.al-empty');
    if (empty) empty.remove();
    alList.appendChild(alRow(entry));
    if (stick) requestAnimationFrame(() => { actionlogEl.scrollTop = actionlogEl.scrollHeight; });
  }

  function subscribeLog() {
    if (alSubscribed || !has('onLogEntry')) return;
    alSubscribed = true;
    try { o.onLogEntry(appendLogEntry); } catch (_) { alSubscribed = false; }
  }

  async function openActionLog({ taskId = null, title = '' } = {}) {
    closeSettings();
    runsEl.hidden = true;
    goalsEl.hidden = true;
    closeDrawer();
    actionlogEl.hidden = false;
    alChain.textContent = ''; alChain.className = '';
    alTask = taskId;
    alTaskChip.hidden = !taskId;
    if (taskId) alSub.textContent = `What O did for “${title || 'this task'}”`;
    else setLogSubtitle(alUnread);
    let tail = [];
    if (has('logTail')) { try { tail = await o.logTail(taskId ? 2000 : 300); } catch (_) { tail = []; } }
    alEntries = (Array.isArray(tail) ? tail : (tail && tail.entries) || []);
    alEntries = (taskId ? alEntries.filter((e) => e.detail && e.detail.taskId === taskId) : alEntries).slice(-AL_CAP);
    renderActionLog();
    requestAnimationFrame(() => { actionlogEl.scrollTop = actionlogEl.scrollHeight; });
    if (has('logMarkRead')) { try { await o.logMarkRead(); } catch (_) {} }
    setLogBadge(0);
  }
  function closeActionLog() { actionlogEl.hidden = true; }

  alTaskChip.addEventListener('click', () => openActionLog());
  $('btn-actionlog-close').addEventListener('click', closeActionLog);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !actionlogEl.hidden && settingsEl.hidden && document.activeElement !== promptEl) closeActionLog(); });
  $('actionlog-filters').addEventListener('click', (e) => {
    const b = e.target.closest('.filter');
    if (!b) return;
    alFilter = b.dataset.filter || 'all';
    $('actionlog-filters').querySelectorAll('.filter').forEach((c) => c.setAttribute('aria-pressed', c === b ? 'true' : 'false'));
    renderActionLog();
    requestAnimationFrame(() => { actionlogEl.scrollTop = actionlogEl.scrollHeight; });
  });
  $('btn-actionlog-open').addEventListener('click', () => { if (has('logOpenFile')) o.logOpenFile(); });
  $('btn-actionlog-verify').addEventListener('click', async () => {
    if (!has('logVerifyChain')) { alChain.textContent = 'Not available'; alChain.className = ''; return; }
    alChain.textContent = 'Verifying…'; alChain.className = '';
    let r;
    try { r = await o.logVerifyChain(); } catch (err) { alChain.textContent = `✗ ${err && err.message ? err.message : 'Could not verify'}`; alChain.className = 'bad'; return; }
    if (r && r.ok) { alChain.textContent = `✓ Chain intact (${r.count || 0} entries)`; alChain.className = 'ok'; }
    else { alChain.textContent = `✗ Broken at #${r && typeof r.brokenAt === 'number' ? r.brokenAt : '?'}`; alChain.className = 'bad'; }
  });
  subscribeLog();

  // MCP servers must be a JSON object; a bad value shows inline and is not saved (Save stays enabled).
  function validateMcpServers() {
    const ta = $('set-mcpServers'), err = $('mcpServers-error');
    if (!ta) return true;
    const text = ta.value.trim();
    let problem = '';
    if (text) {
      try {
        const v = JSON.parse(text);
        if (!v || typeof v !== 'object' || Array.isArray(v)) problem = 'MCP servers must be a JSON object: {"name": {"command": "…"}}';
      } catch (e) { problem = `MCP servers is not valid JSON: ${e && e.message ? e.message : e}`; }
    }
    ta.classList.toggle('invalid', !!problem);
    err.textContent = problem;
    err.hidden = !problem;
    return !problem;
  }
  if ($('set-mcpServers')) $('set-mcpServers').addEventListener('input', () => { if ($('set-mcpServers').classList.contains('invalid')) validateMcpServers(); });

  async function saveSettings() {
    const partial = {};
    const mcpOk = validateMcpServers();
    for (const k of FIELDS) {
      const el = $('set-' + k);
      if (!el) continue;
      if (k === 'mcpServers') { if (mcpOk) partial[k] = el.value.trim() || '{}'; continue; }
      if (el.type === 'checkbox') partial[k] = el.checked;
      else if (el.type === 'number') {
        const fractional = String(el.step || '').includes('.');
        const n = fractional ? parseFloat(el.value) : parseInt(el.value, 10);
        const lo = el.min !== '' ? Number(el.min) : 1, hi = el.max !== '' ? Number(el.max) : 10;
        if (Number.isFinite(n)) partial[k] = Math.min(hi, Math.max(lo, n));
      }
      else partial[k] = el.value;
    }
    await o.setSettings(partial);
    if (partial.theme) { savedTheme = THEMES.includes(partial.theme) ? partial.theme : 'glass'; applyTheme(savedTheme); }
    $('settings-saved').textContent = mcpOk ? 'Saved ✓ (applies to the next task)' : 'Saved ✓ except MCP servers (fix the JSON)';
    setTimeout(() => { $('settings-saved').textContent = ''; }, 2500);
    checkClaude();
    refreshExtension({ card: true });
    refreshProxy();
    if (GenUI) syncGenuiSettings();
  }
  $('btn-settings-save').addEventListener('click', saveSettings);

  // ------------------------------------------------------------ "my browser" extension setup

  const BROWSER_NAME = { msedge: 'Edge', chrome: 'Chrome' };
  let extCard = null;
  let linkCard = null;
  function showLinkSteps(res) {
    const where = res && res.browser ? res.browser : 'Edge';
    addMsg('meta setup-card', `I opened ${where}'s Extensions page and the O Link folder (its path is copied). In ${where}:
      <ol class="steps">
        <li>Turn on <b>Developer mode</b> (a switch on the Extensions page).</li>
        <li>Click <b>Load unpacked</b>.</li>
        <li>Paste the path (<b>Ctrl+V</b>) into the folder box, or pick the <b>o-link</b> folder, then click <b>Select Folder</b>.</li>
      </ol>I'll say "O Link connected!" when it works.${res && res.dir ? `<div class="path">${escapeHtml(res.dir)}</div>` : ''}`);
  }
  async function refreshLink({ card = false } = {}) {
    let st;
    try { st = await o.linkStatus(); } catch (_) { return; }
    $('ext-status').textContent = st.connected ? `✓ O Link connected (${st.tabs} tab${st.tabs === 1 ? '' : 's'})` : '✗ O Link not connected yet';
    $('btn-ext-install').hidden = true;
    $('btn-link-connect').hidden = !!st.connected;
    $('btn-link-connect').textContent = 'Connect my Edge';
    if (st.connected && linkCard) { linkCard.remove(); linkCard = null; addMsg('meta', '✓ O Link connected! I can work in all your tabs now.'); }
    if (card && !st.connected && !linkCard) {
      linkCard = addMsg('meta setup-card', `To work in <b>all your Edge tabs</b>, connect O Link, the O browser extension (one-time, about 30 seconds).
        <div class="setup-actions">
          <button class="link-connect">Connect my Edge</button>
          <button class="ext-separate">Use my own invisible browser instead</button>
        </div>`);
      linkCard.querySelector('.link-connect').addEventListener('click', async () => showLinkSteps(await o.connectLink()));
      linkCard.querySelector('.ext-separate').addEventListener('click', async () => {
        await o.setSettings({ browserMode: 'separate' });
        linkCard.remove(); linkCard = null;
        addMsg('meta', 'OK, I\'ll browse in my own invisible browser. Change it any time in Settings.');
      });
    }
  }
  $('btn-link-connect').addEventListener('click', async () => showLinkSteps(await o.connectLink()));

  async function refreshExtension({ card = false } = {}) {
    refreshModeChip();
    let ext, st;
    try { st = await o.getSettings(); } catch (_) { return; }
    $('btn-link-connect').hidden = true;
    if (st.browserMode === 'link') return refreshLink({ card });
    if (linkCard) { linkCard.remove(); linkCard = null; }
    if (st.browserMode === 'separate') {
      $('ext-status').textContent = 'O uses its own invisible browser.';
      $('btn-ext-install').hidden = true;
      return;
    }
    try { ext = await o.checkExtension(); } catch (_) { return; }
    const where = ext.installed ? `${BROWSER_NAME[ext.browser] || ext.browser}${ext.profile && ext.profile !== 'Default' ? ' (' + ext.profile + ')' : ''}` : '';
    $('ext-status').textContent = ext.installed ? `✓ Playwright Extension found in ${where}` : '✗ Playwright Extension not installed yet';
    $('btn-ext-install').hidden = !!ext.installed;
    if (ext.installed && extCard) { extCard.remove(); extCard = null; addMsg('meta', `✓ Extension found in ${where}. I'll work in your own tabs now.`); }
    if (card && !ext.installed && st.browserMode === 'mine' && !extCard) {
      extCard = addMsg('meta setup-card', `To work in <b>your own Edge tabs</b>, O needs the free Playwright Extension (one-time, about a minute).
        <div class="setup-actions">
          <button class="ext-install">Install in Edge</button>
          <button class="ext-recheck">I installed it</button>
          <button class="ext-separate">Use my own invisible browser instead</button>
        </div>`);
      extCard.querySelector('.ext-install').addEventListener('click', () => o.installExtension());
      extCard.querySelector('.ext-recheck').addEventListener('click', () => refreshExtension());
      extCard.querySelector('.ext-separate').addEventListener('click', async () => {
        await o.setSettings({ browserMode: 'separate' });
        extCard.remove(); extCard = null;
        addMsg('meta', 'OK, I\'ll browse in my own invisible browser. Change it any time in Settings.');
      });
    }
  }
  $('btn-ext-install').addEventListener('click', () => o.installExtension());

  $('btn-shortcuts').addEventListener('click', async () => {
    const r = await o.createShortcuts();
    $('shortcut-status').textContent = r.ok ? '✓ Shortcut on your Desktop and in the Start menu.' : (r.error || 'Could not create the shortcut.');
  });

  // ------------------------------------------------------------ updates

  let updateBanner = null;
  function showUpdate(u) {
    if (!u || !u.state) return;
    $('update-status').textContent = u.message || u.state;
    $('btn-update-apply').hidden = u.state !== 'available';
    $('btn-update-check').disabled = ['checking', 'downloading', 'installing', 'restarting'].includes(u.state);
    $('btn-update-apply').disabled = $('btn-update-check').disabled;
    if (u.state === 'available' && !updateBanner) {
      updateBanner = addMsg('meta update', `A new version of O is out. <button class="update-now">Update now</button>`);
      updateBanner.querySelector('.update-now').addEventListener('click', () => o.applyUpdate());
    } else if (updateBanner && u.state !== 'available') {
      const btn = updateBanner.querySelector('.update-now');
      if (btn) btn.remove();
      updateBanner.append(' ' + (u.message || ''));
      if (['error', 'manual', 'restarting', 'current'].includes(u.state)) updateBanner = null;
    }
    if (u.state === 'error' || u.state === 'manual') addMsg('error', `⚠ ${escapeHtml(u.message || 'Update failed')}`);
  }
  $('btn-update-check').addEventListener('click', async () => {
    if (!settingsEl.hidden) await saveSettings(); // use the source/token currently typed in
    showUpdate(await o.checkUpdate());
  });
  $('btn-update-apply').addEventListener('click', () => o.applyUpdate());
  o.getUpdateState().then((u) => { if (u && u.state && u.state !== 'idle') showUpdate(u); }).catch(() => {});

  // ------------------------------------------------------------ Claude Code check

  async function checkClaude() {
    let st;
    try { st = await o.checkClaude(); } catch (err) { st = { installed: false, error: err.message }; }
    if (st && st.installed && st.loggedIn === false) {
      setupEl.hidden = false;
      $('setup-detail').textContent = `Claude Code ${st.version || ''} is installed but not logged in. Run \`claude auth login\` in a terminal to log in.`;
    } else if (st && st.installed) {
      setupEl.hidden = true;
      $('logo').title = `Claude Code ${st.version || ''}`.trim();
    } else {
      setupEl.hidden = false;
      $('setup-detail').textContent = st && st.error ? `(${st.error})` : 'Claude Code CLI was not found.';
    }
  }
  $('btn-recheck').addEventListener('click', checkClaude);

  // ------------------------------------------------------------ generative UI (surfaces in chat + Runs sheet)

  const GenUI = window.OGenUI;
  const runsList = $('runs-list'), runsSub = $('runs-sub');
  const surfaces = new Map(); // surface id -> { surface, kind: 'chat'|'job', host, item, seq }
  let surfaceSeq = 0;

  async function syncGenuiSettings() {
    try { const st = await o.getSettings(); GenUI.sandboxEnabled = !(st && st.genuiSandbox === false); } catch (_) {}
  }
  if (GenUI) syncGenuiSettings();

  function genuiNote(summary, detail) { if (has('logNote')) { try { o.logNote(summary, detail); } catch (_) {} } }

  function surfaceKind(id) { return String(id).startsWith('job:') ? 'job' : 'chat'; }

  function getSurface(id, create) {
    let s = surfaces.get(id);
    if (s || !create || !GenUI) return s;
    s = { surface: new GenUI.Surface(id), kind: surfaceKind(id), host: null, item: null, seq: ++surfaceSeq };
    surfaces.set(id, s);
    if (s.kind === 'chat') {
      // A task's cards (surface chat:<taskId>) show only in that task's chat.
      const own = id === 'chat:' + (viewId || 'default');
      if (own) mountChatSurface(s);
    } else if (!runsEl.hidden) {
      runsList.querySelector('.runs-empty')?.remove();
      runsList.prepend(runItem(s, true));
    }
    return s;
  }

  function mountChatSurface(s) {
    startChat();
    endAssistantBubble();
    currentSteps = null;
    s.host = addMsg('genui', '');
    s.surface.mount(s.host);
  }

  /** After switching to a task: put its cards back (from memory, else from main's frame store). */
  async function showTaskSurface(id) {
    if (!GenUI) return;
    const sid = 'chat:' + id;
    let s = surfaces.get(sid);
    if (!s && has('genuiFrames')) {
      let frames = [];
      try { frames = await o.genuiFrames(sid); } catch (_) { frames = []; }
      if (Array.isArray(frames) && frames.length && viewId === id && !surfaces.get(sid)) {
        s = { surface: new GenUI.Surface(sid), kind: 'chat', host: null, item: null, seq: ++surfaceSeq };
        for (const f of frames) s.surface.apply(f);
        surfaces.set(sid, s);
      }
    }
    if (s && !s.host && viewId === id) mountChatSurface(s);
  }

  function onGenuiFrame(frame) {
    if (!GenUI) return;
    const v = GenUI.validateFrame(frame);
    if (!v.ok) { genuiNote(`GenUI frame rejected: ${v.error}`, { surface: frame && frame.surface, type: frame && frame.type }); return; }
    const s = getSurface(frame.surface, true);
    if (!s) return;
    const stick = s.kind === 'chat' && !!s.host && atBottom();
    s.surface.apply(frame);
    if (s.item) updateRunHead(s);
    if (s.kind === 'job' && !runsEl.hidden) updateRunsSub();
    if (s.kind === 'chat' && s.host) scrollDown(stick);
  }

  function updateRunHead(s) {
    const head = s.item.querySelector('.runs-head');
    head.querySelector('.gu-pill').replaceWith(pillFor(s.surface.status));
    head.querySelector('.ttl').textContent = s.surface.title || s.surface.id;
    s.item.dataset.status = s.surface.status;
  }

  function pillFor(status) {
    const p = document.createElement('span');
    p.className = 'gu-pill'; p.dataset.status = status; p.textContent = status;
    return p;
  }

  let runSeq = 0;
  function runItem(s, expanded) {
    const item = document.createElement('div');
    item.className = 'run';
    item.dataset.surface = s.surface.id;
    const bodyId = 'run-b-' + (runSeq++);
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'runs-head';
    head.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    head.setAttribute('aria-controls', bodyId);
    head.innerHTML = `<svg class="chev" viewBox="0 0 20 20" aria-hidden="true"><path d="M7 5l6 5-6 5"/></svg><span class="gu-pill"></span><span class="ttl"></span><span class="sid">${escapeHtml(s.surface.id.slice(4, 16))}</span>`;
    const body = document.createElement('div');
    body.className = 'run-body';
    body.id = bodyId;
    body.hidden = !expanded;
    head.addEventListener('click', () => {
      const open = head.getAttribute('aria-expanded') === 'true';
      head.setAttribute('aria-expanded', open ? 'false' : 'true');
      body.hidden = open;
    });
    item.appendChild(head);
    item.appendChild(body);
    s.item = item;
    s.surface.unmount();
    s.surface.mount(body);
    updateRunHead(s);
    return item;
  }

  function renderRuns() {
    runsList.innerHTML = '';
    const jobs = [...surfaces.values()].filter((s) => s.kind === 'job').sort((a, b) => b.seq - a.seq);
    if (!jobs.length) {
      const d = document.createElement('div');
      d.className = 'runs-empty';
      d.textContent = 'No engine runs yet.';
      runsList.appendChild(d);
    }
    const frag = document.createDocumentFragment();
    jobs.forEach((s, i) => frag.appendChild(runItem(s, i === 0 || s.surface.status === 'running')));
    runsList.appendChild(frag);
    updateRunsSub();
  }

  function updateRunsSub() {
    const jobs = [...surfaces.values()].filter((s) => s.kind === 'job');
    const running = jobs.filter((s) => s.surface.status === 'running').length;
    runsSub.textContent = !jobs.length ? 'Plans and steps of engine jobs, newest first' : running ? `${running} running · ${jobs.length} total` : `${jobs.length} run${jobs.length === 1 ? '' : 's'}, none running`;
  }

  /** Backfill from main: `genuiSurfaces()` -> [{surface, title, ended, ok, count}], then `genuiFrames(surface)` -> frames[]. */
  async function backfillRuns() {
    if (!has('genuiFrames') || !GenUI) return;
    const ids = new Set([...surfaces.keys()].filter((id) => surfaceKind(id) === 'job'));
    if (has('genuiSurfaces')) {
      try {
        const list = await o.genuiSurfaces();
        for (const it of Array.isArray(list) ? list : []) {
          const id = typeof it === 'string' ? it : it && it.surface;
          if (id && surfaceKind(id) === 'job') ids.add(id);
        }
      } catch (_) {}
    }
    for (const id of ids) {
      let frames;
      try { frames = await o.genuiFrames(id); } catch (_) { continue; }
      if (frames && Array.isArray(frames.frames)) frames = frames.frames;
      if (!Array.isArray(frames) || !frames.length) continue;
      const s = surfaces.get(id);
      if (s && s.surface.frames.length >= frames.length) continue; // nothing new
      if (s) s.surface.unmount();
      const fresh = { surface: new GenUI.Surface(id), kind: 'job', host: null, item: null, seq: s ? s.seq : ++surfaceSeq };
      for (const f of frames) fresh.surface.apply(f);
      surfaces.set(id, fresh);
    }
  }

  async function openRuns() {
    closeSettings();
    actionlogEl.hidden = true;
    goalsEl.hidden = true;
    closeDrawer();
    runsEl.hidden = false;
    renderTaskSheet();
    renderRuns();
    refreshTasks();
    await backfillRuns();
    if (!runsEl.hidden) renderRuns();
  }
  function closeRuns() { runsEl.hidden = true; }
  $('btn-runs').addEventListener('click', () => (runsEl.hidden ? openRuns() : closeRuns()));
  $('btn-runs-close').addEventListener('click', closeRuns);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !runsEl.hidden && settingsEl.hidden && actionlogEl.hidden && document.activeElement !== promptEl) closeRuns(); });

  // Button actions are a closed set (the validator drops anything else). Every one goes through main's
  // `genuiAction`, which records it in the action log and performs it (open-log opens this sheet via the
  // 'open-log' event, open-file opens the action-log text file, approve/deny resolve a pending approval).
  // The local handling below is only a fallback for a preload without `genuiAction`.
  function onGenuiAction(e) {
    const d = e.detail || {};
    if (!GenUI || !GenUI.ACTIONS.includes(d.action)) return;
    if (has('genuiAction')) {
      Promise.resolve(o.genuiAction(d.action, Object.assign({ surface: d.surface }, d.payload || {}))).catch(() => {});
      return;
    }
    switch (d.action) {
      case 'open-log': openActionLog(); break;
      case 'open-file': if (has('logOpenFile')) o.logOpenFile(); break;
      default: {
        const host = e.target.closest('.gu-surface');
        if (host && !host.querySelector('.gu-note')) {
          const n = document.createElement('div');
          n.className = 'gu-note';
          n.textContent = 'This build can\'t send that back yet.';
          host.appendChild(n);
        }
        break;
      }
    }
  }
  log.addEventListener('o-genui-action', onGenuiAction);
  runsList.addEventListener('o-genui-action', onGenuiAction);

  // ------------------------------------------------------------ goals (long-term agenda)
  // A goal is long-running work O does every cycle (main/goals.js, engine /goals). The row above the composer
  // shows each one compactly; the Goals sheet lists them, and a goal's own view shows its objective, metrics,
  // cycle timeline and any checkpoint card waiting for the person (Approve / Edit / Skip).
  let goalsList = [];
  let checkpointCards = [];
  let openGoalId = null;
  let goalDetail = null;
  const goalsRow = $('goals-row');
  const goalOffer = $('goal-offer');
  const GOALS_ROW_MAX = 2;
  const PAUSE_ICON = '<svg viewBox="0 0 20 20"><path d="M7.5 5.5v9M12.5 5.5v9"/></svg>';
  const PLAY_ICON = '<svg viewBox="0 0 20 20"><path d="M7 5.2v9.6l7.5-4.8z"/></svg>';
  const CHECK_KINDS = [['publish', 'Publish or post publicly'], ['send_message', 'Send messages as you'], ['accept_terms', 'Accept terms'], ['delete_data', 'Delete your data']];
  const ALWAYS_YOU = [['spend', 'Spend money or start paid services'], ['create_account', 'Create accounts']];

  function fmtNum(v) {
    if (v == null || !Number.isFinite(Number(v))) return '—';
    const n = Number(v), a = Math.abs(n);
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
    if (a >= 1e4) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1).replace(/\.0$/, '') + 'k';
    return Number.isInteger(n) ? n.toLocaleString() : n.toFixed(1);
  }
  const metricWord = (name) => String(name || '').replace(/_/g, ' ');

  /** A tiny monochrome line of the last readings (currentColor), with the latest point marked. */
  function sparkline(values, w = 56, h = 16) {
    const v = (values || []).map(Number).filter(Number.isFinite);
    const size = w > 60 ? ' lg' : '';
    if (v.length < 2) return `<svg class="spark empty${size}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true" focusable="false"><path d="M1 ${h - 2}H${w - 1}"/></svg>`;
    const min = Math.min(...v), max = Math.max(...v), span = max - min || 1;
    const pts = v.map((x, i) => [1 + (i * (w - 4)) / (v.length - 1), h - 2 - ((x - min) / span) * (h - 4)]);
    const last = pts[pts.length - 1];
    return `<svg class="spark${size}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true" focusable="false"><polyline points="${pts.map((p) => p.map((n) => n.toFixed(1)).join(',')).join(' ')}"/><circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="1.7"/></svg>`;
  }

  /** "9:00 AM" today, "Thu 9:00 AM" later: the next cycle in as few characters as reads clearly. */
  function shortNext(ms) {
    const d = new Date(ms), now = new Date();
    const time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    return d.toDateString() === now.toDateString() ? time : `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${time}`;
  }
  function goalLine(g, { short } = {}) {
    const bits = [`Day ${Math.min(g.day || 1, g.horizon_days || 1)} of ${g.horizon_days}`];
    if (g.status === 'paused') bits.push('Paused');
    else if (g.runningTaskId) bits.push('Working now');
    else if (g.nextRun) bits.push(`next ${short ? shortNext(g.nextRun) : relTime(g.nextRun)}`);
    return bits.join(' · ');
  }

  function primaryHtml(g) {
    const p = g.primary;
    if (!p) return '';
    const val = p.latest == null ? 'no reading yet' : `${fmtNum(p.latest)} ${escapeHtml(metricWord(p.name))}`;
    return `<span class="gmetric">${sparkline(p.series)}<span class="gval">${val}</span></span>`;
  }

  /** One goal in the row above the composer or in the Goals sheet. */
  function goalRowEl(g, { compact } = {}) {
    const row = document.createElement('div');
    row.className = 'goal-row' + (compact ? ' compact' : '');
    row.setAttribute('role', 'listitem');
    row.dataset.id = g.id;
    row.dataset.status = g.status;
    const waiting = g.pendingCheckpoints || 0;
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'goal-open';
    // Compact rows mark a waiting approval with a bright dot (the count is in the label and the header badge).
    open.innerHTML = `<span class="gtext"><span class="gttl">${waiting && compact ? '<span class="gwait" aria-hidden="true"></span>' : ''}${escapeHtml(g.title)}</span><span class="gsub"><span class="gline">${escapeHtml(goalLine(g, { short: true }))}${waiting && !compact ? ` · <b>${waiting} to approve</b>` : ''}</span>${primaryHtml(g)}</span></span>`;
    const p = g.primary;
    open.setAttribute('aria-label', `${g.title}. ${goalLine(g)}.${p ? ` ${metricWord(p.name)} ${p.latest == null ? 'not measured yet' : fmtNum(p.latest)}.` : ''}${waiting ? ` ${waiting} waiting for your approval.` : ''}`);
    open.addEventListener('click', () => openGoal(g.id));
    row.appendChild(open);
    const pause = document.createElement('button');
    pause.type = 'button';
    pause.className = 'goal-pause';
    const paused = g.status === 'paused';
    pause.title = paused ? 'Resume' : 'Pause';
    pause.setAttribute('aria-label', `${paused ? 'Resume' : 'Pause'} ${g.title}`);
    pause.innerHTML = paused ? PLAY_ICON : PAUSE_ICON;
    pause.addEventListener('click', (e) => { e.stopPropagation(); setGoalStatus(g.id, paused ? 'resume' : 'pause'); });
    row.appendChild(pause);
    return row;
  }

  function renderGoalsRow() {
    const live = goalsList.filter((g) => g.status !== 'archived');
    goalsRow.innerHTML = '';
    live.slice(0, GOALS_ROW_MAX).forEach((g) => goalsRow.appendChild(goalRowEl(g, { compact: true })));
    if (live.length > GOALS_ROW_MAX) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'strip-more';
      b.textContent = `${live.length - GOALS_ROW_MAX} more goal${live.length - GOALS_ROW_MAX === 1 ? '' : 's'}`;
      b.addEventListener('click', () => openGoals());
      goalsRow.appendChild(b);
    }
    goalsRow.hidden = !live.length;
    document.body.classList.toggle('has-goals', !goalsRow.hidden);
    const badge = $('goals-badge');
    badge.hidden = !checkpointCards.length;
    badge.textContent = String(checkpointCards.length);
    $('btn-goals').setAttribute('aria-label', checkpointCards.length ? `Goals, ${checkpointCards.length} waiting for your approval` : 'Goals');
  }

  function setGoals(list, cps) {
    if (Array.isArray(list)) goalsList = list;
    if (Array.isArray(cps)) checkpointCards = cps;
    renderGoalsRow();
    if (!goalsEl.hidden) {
      if (openGoalId) loadGoal(openGoalId);
      else renderGoalsIndex();
    }
  }

  async function refreshGoals() {
    if (!has('listGoals')) return;
    try { const r = await o.listGoals(); if (r && r.ok !== false) setGoals(r.goals, r.checkpoints); } catch (_) {}
  }

  async function setGoalStatus(id, op) {
    if (!has('setGoalStatus')) return;
    const r = await o.setGoalStatus(id, op);
    if (r && r.ok === false) { addMsg('error', `⚠ ${escapeHtml(r.error || 'Could not change the goal')}`); return; }
    refreshGoals();
  }

  // ---------- checkpoint card (goal view and the Goals sheet)
  function checkpointEl(c, { focusEdit } = {}) {
    const el = document.createElement('div');
    el.className = 'cp-card';
    el.dataset.id = c.id;
    const items = c.items || {};
    const rows = [['Title', items.title], ['Description', items.description], ['Thumbnail', items.thumbnail], ['File', items.file], ['Schedule', items.schedule]]
      .filter(([, v]) => v).map(([k, v]) => `<div class="cp-kv"><span>${k}</span><span>${escapeHtml(v)}</span></div>`).join('');
    el.innerHTML = `<div class="cp-kind">${escapeHtml(c.kindLabel || c.kind)}${c.goalTitle && !openGoalId ? ` · ${escapeHtml(c.goalTitle)}` : ''}</div>
      <div class="cp-head">${escapeHtml(c.headline)}</div>
      ${c.summary ? `<p class="cp-sum">${escapeHtml(c.summary)}</p>` : ''}
      ${rows ? `<div class="cp-items">${rows}</div>` : ''}
      ${c.personDoesIt ? '<p class="cp-note">O never does this part itself. Approve when you have done it, or skip it.</p>' : ''}
      <div class="cp-edit" hidden><textarea rows="2" aria-label="What should change?" placeholder="What should change?"></textarea><button type="button" class="cp-send">Send changes</button></div>
      <div class="cp-acts"><button type="button" class="cp-skip">Skip</button><button type="button" class="cp-editbtn">Edit</button><button type="button" class="cp-approve">${c.personDoesIt ? 'Done it' : 'Approve'}</button></div>`;
    const edit = el.querySelector('.cp-edit');
    const ta = edit.querySelector('textarea');
    const decide = async (decision, text) => {
      if (!has('resolveCheckpoint')) return;
      el.classList.add('busy');
      const r = await o.resolveCheckpoint(c.id, decision, text);
      el.classList.remove('busy');
      if (r && r.ok === false) { el.querySelector('.cp-acts').insertAdjacentHTML('beforebegin', `<p class="cp-note">${escapeHtml(r.error || 'That did not work')}</p>`); return; }
      refreshGoals();
    };
    el.querySelector('.cp-approve').addEventListener('click', () => decide('approve'));
    el.querySelector('.cp-skip').addEventListener('click', () => decide('skip'));
    el.querySelector('.cp-editbtn').addEventListener('click', () => { edit.hidden = !edit.hidden; if (!edit.hidden) ta.focus(); });
    el.querySelector('.cp-send').addEventListener('click', () => { if (ta.value.trim()) decide('edit', ta.value.trim()); else ta.focus(); });
    if (focusEdit) { edit.hidden = false; setTimeout(() => ta.focus(), 30); }
    return el;
  }

  // ---------- the Goals sheet
  function renderGoalsIndex() {
    $('goals-index').hidden = false;
    $('goal-view').hidden = true;
    $('goals-h').textContent = 'Goals';
    const waiting = $('goals-waiting');
    waiting.innerHTML = '';
    if (checkpointCards.length) {
      const h = document.createElement('h3'); h.className = 'sheet-h'; h.textContent = 'Waiting for you'; waiting.appendChild(h);
      checkpointCards.forEach((c) => waiting.appendChild(checkpointEl(c)));
    }
    const list = $('goals-list');
    list.innerHTML = '';
    const live = goalsList.filter((g) => g.status !== 'archived');
    if (!live.length) {
      const d = document.createElement('p');
      d.className = 'goals-empty';
      d.textContent = 'Give O an objective that takes weeks, like growing a channel or a newsletter. It plans each day, measures, and adapts.';
      list.appendChild(d);
    } else live.forEach((g) => list.appendChild(goalRowEl(g)));
    const active = live.filter((g) => g.status === 'active').length;
    $('goals-sub').textContent = live.length ? `${active} active${checkpointCards.length ? ` · ${checkpointCards.length} waiting for you` : ''}` : 'Long-running work O does every day, checking in only at checkpoints';
  }

  function openGoals() {
    closeSettings(); actionlogEl.hidden = true; runsEl.hidden = true; closeDrawer();
    goalsEl.hidden = false;
    openGoalId = null;
    renderGoalsIndex();
    refreshGoals();
  }
  function closeGoals() { goalsEl.hidden = true; openGoalId = null; }
  $('btn-goals').addEventListener('click', () => (goalsEl.hidden ? openGoals() : closeGoals()));
  $('btn-goals-close').addEventListener('click', closeGoals);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !goalsEl.hidden && document.activeElement !== promptEl && !(e.target && e.target.closest && e.target.closest('textarea'))) { if (openGoalId) openGoals(); else closeGoals(); } });

  $('btn-propose').addEventListener('click', async () => {
    const btn = $('btn-propose');
    const box = $('goals-proposals');
    if (!has('proposeGoals')) return;
    btn.disabled = true;
    btn.textContent = 'Thinking about what you do…';
    let r;
    try { r = await o.proposeGoals(); } catch (err) { r = { ok: false, error: err && err.message }; }
    btn.disabled = false;
    btn.textContent = 'Let O propose goals';
    box.innerHTML = '';
    if (!r || r.ok === false) { box.innerHTML = `<p class="goals-empty">${escapeHtml((r && r.error) || 'O could not propose goals right now.')}</p>`; return; }
    if (!r.proposals.length) { box.innerHTML = '<p class="goals-empty">O needs a few more days of working with you before it can propose goals worth owning.</p>'; return; }
    const h = document.createElement('h3'); h.className = 'sheet-h'; h.textContent = 'O could own'; box.appendChild(h);
    for (const p of r.proposals) {
      const row = document.createElement('div');
      row.className = 'proposal';
      row.innerHTML = `<div class="ptext"><span class="gttl">${escapeHtml(p.title)}</span><span class="gline">${escapeHtml(p.objective)}</span></div>`;
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'learn-accept';
      b.textContent = 'Accept';
      b.setAttribute('aria-label', `Accept goal: ${p.title}`);
      b.addEventListener('click', async () => {
        b.disabled = true;
        const a = await o.acceptGoal(p);
        if (a && a.ok === false) { b.disabled = false; row.insertAdjacentHTML('beforeend', `<p class="cp-note">${escapeHtml(a.error || 'Could not accept')}</p>`); return; }
        row.remove();
        await refreshGoals();
        if (a && a.goal) openGoal(a.goal.id);
      });
      row.appendChild(b);
      box.appendChild(row);
    }
  });

  // ---------- one goal
  async function openGoal(id, opts = {}) {
    if (goalsEl.hidden) { closeSettings(); actionlogEl.hidden = true; runsEl.hidden = true; closeDrawer(); goalsEl.hidden = false; }
    openGoalId = id;
    await loadGoal(id, opts);
  }

  async function loadGoal(id, opts = {}) {
    if (!has('goalDetail')) return;
    let d;
    try { d = await o.goalDetail(id); } catch (err) { d = { ok: false, error: err && err.message }; }
    if (openGoalId !== id) return;
    if (!d || d.ok === false) { renderGoalsIndex(); return; }
    goalDetail = d;
    renderGoal(d, opts);
  }

  function cycleEl(c, prev, goal) {
    const el = document.createElement('li');
    el.className = 'cycle';
    const day = Math.max(1, Math.floor(((c.ts * 1000) - (goal.created * 1000)) / 86400000) + 1);
    const when = new Date(c.ts * 1000).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    const list = (arr) => (arr && arr.length ? `<ul>${arr.map((x) => `<li>${escapeHtml(x)}</li>`).join('')}</ul>` : '<p class="none">Nothing recorded</p>');
    const metrics = Object.entries(c.metrics || {}).map(([k, v]) => {
      const before = prev && prev.metrics && prev.metrics[k] ? Number(prev.metrics[k].value) : null;
      const delta = before != null && Number.isFinite(before) ? Number(v.value) - before : null;
      const unverified = v.source === 'unverified' || v.source === 'self_report';
      return `<span class="cm">${escapeHtml(metricWord(k))} <b>${fmtNum(v.value)}</b>${delta ? ` <span class="delta">${delta > 0 ? '+' : '−'}${fmtNum(Math.abs(delta))}</span>` : ''}${unverified ? ' <span class="pill">unverified</span>' : ''}</span>`;
    }).join('');
    const flags = (c.flags || []).filter((f) => f !== 'unverified_metrics').map((f) => `<span class="pill">${f === 'unsupported_claim' ? 'claim not backed by metrics' : f === 'incomplete' ? 'no work recorded' : escapeHtml(f)}</span>`).join('');
    el.innerHTML = `<div class="cy-head"><span class="cy-day">Day ${day}</span><span class="cy-when">${escapeHtml(when)}</span>${flags}</div>
      <div class="cy-step"><span class="cy-label">Plan</span>${list(c.plan)}</div>
      <div class="cy-step"><span class="cy-label">Did</span>${list(c.did)}</div>
      <div class="cy-step"><span class="cy-label">Result</span>${metrics ? `<div class="cy-metrics">${metrics}</div>` : '<p class="none">Not measured</p>'}</div>
      <div class="cy-step"><span class="cy-label">Reflection</span><div><p>${escapeHtml(c.reflection || '—')}</p>${c.next ? `<p class="cy-next">Next: ${escapeHtml(c.next)}</p>` : ''}</div></div>`;
    return el;
  }

  function renderGoal(d, { checkpointId, edit } = {}) {
    const g = d.goal;
    $('goals-index').hidden = true;
    const view = $('goal-view');
    view.hidden = false;
    $('goals-h').textContent = 'Goal';
    $('goals-sub').textContent = goalLine({ ...g, day: d.day });
    view.innerHTML = '';
    const back = document.createElement('button');
    back.type = 'button'; back.className = 'link-btn back'; back.textContent = 'All goals';
    back.addEventListener('click', openGoals);
    view.appendChild(back);

    const head = document.createElement('div');
    head.className = 'gv-head';
    const sameText = String(g.objective || '').trim().replace(/[.\s]+$/, '').toLowerCase() === String(g.title || '').trim().replace(/[.\s]+$/, '').toLowerCase();
    head.innerHTML = `<h3 class="gv-title">${escapeHtml(g.title)}</h3>${sameText ? '' : `<p class="gv-objective">${escapeHtml(g.objective)}</p>`}
      <p class="gv-meta">${escapeHtml(g.scheduleLabel || (g.status === 'paused' ? 'Paused' : ''))}${g.origin === 'agent' ? ' · proposed by O' : ''}</p>`;
    view.appendChild(head);

    const acts = document.createElement('div');
    acts.className = 'gv-acts';
    const mk = (label, fn, cls) => { const b = document.createElement('button'); b.type = 'button'; b.textContent = label; if (cls) b.className = cls; b.addEventListener('click', fn); acts.appendChild(b); return b; };
    if (g.status === 'active') mk(g.runningTaskId ? 'Working now…' : 'Run a cycle now', async () => { if (has('runGoalNow')) { const r = await o.runGoalNow(g.id); if (r && r.ok === false) $('goals-sub').textContent = r.error || 'Could not start'; loadGoal(g.id); } }).disabled = !!g.runningTaskId;
    mk(g.status === 'paused' ? 'Resume' : 'Pause', () => setGoalStatus(g.id, g.status === 'paused' ? 'resume' : 'pause'));
    const arch = mk('Archive', async () => {
      if (arch.dataset.armed !== '1') { arch.dataset.armed = '1'; arch.textContent = 'Archive for good?'; setTimeout(() => { arch.dataset.armed = ''; arch.textContent = 'Archive'; }, 3000); return; }
      await setGoalStatus(g.id, 'archive'); openGoals();
    }, 'quiet-btn');
    view.appendChild(acts);

    const pending = (d.checkpoints || []).filter((c) => c.status === 'pending');
    if (pending.length) {
      const h = document.createElement('h3'); h.className = 'sheet-h'; h.textContent = 'Waiting for you'; view.appendChild(h);
      pending.forEach((c) => view.appendChild(checkpointEl(c, { focusEdit: edit && c.id === checkpointId })));
    }

    const mh = document.createElement('h3'); mh.className = 'sheet-h'; mh.textContent = 'Metrics'; view.appendChild(mh);
    const mbox = document.createElement('div');
    mbox.className = 'gv-metrics';
    for (const m of g.success_metrics || []) {
      const pts = (d.series[m.name] || []).map((p) => p.value);
      const latest = pts.length ? pts[pts.length - 1] : null;
      const row = document.createElement('div');
      row.className = 'gv-metric';
      row.innerHTML = `<div class="mt"><span class="mname">${escapeHtml(metricWord(m.name))}</span><span class="msrc">${escapeHtml(m.source)}</span></div>
        <div class="mv"><b>${latest == null ? '—' : fmtNum(latest)}</b><span>${m.target != null ? ` of ${fmtNum(m.target)}` : ''}${m.unit && m.unit !== m.name ? ' ' + escapeHtml(m.unit) : ''}</span></div>${sparkline(pts, 120, 28)}`;
      mbox.appendChild(row);
    }
    view.appendChild(mbox);

    const th = document.createElement('h3'); th.className = 'sheet-h'; th.textContent = 'Timeline'; view.appendChild(th);
    const tl = document.createElement('ol');
    tl.className = 'timeline';
    const cycles = d.cycles || [];
    if (!cycles.length) { const p = document.createElement('p'); p.className = 'goals-empty'; p.textContent = g.runningTaskId ? 'The first cycle is running. Its plan shows up here when it finishes.' : 'No cycles yet.'; view.appendChild(p); }
    for (let i = cycles.length - 1; i >= 0; i--) tl.appendChild(cycleEl(cycles[i], cycles[i - 1], g));
    view.appendChild(tl);

    const ah = document.createElement('h3'); ah.className = 'sheet-h'; ah.textContent = 'Approvals'; view.appendChild(ah);
    const ap = document.createElement('div');
    ap.className = 'gv-approvals';
    const allowed = g.autonomy === 'act' ? (g.allowed || []) : [];
    ap.innerHTML = `<p class="hint">O always asks before these. Untick one only if you want O to do it on its own for this goal.</p>` +
      CHECK_KINDS.map(([k, label]) => `<label class="row check"><input type="checkbox" data-kind="${k}" ${allowed.includes(k) ? '' : 'checked'}><span>Ask before: ${escapeHtml(label.toLowerCase())}</span></label>`).join('') +
      ALWAYS_YOU.map(([, label]) => `<div class="row check locked"><span class="lock" aria-hidden="true">—</span><span>${escapeHtml(label)}: always you, never O</span></div>`).join('');
    ap.addEventListener('change', async () => {
      const off = [...ap.querySelectorAll('input[data-kind]')].filter((x) => !x.checked).map((x) => x.dataset.kind);
      if (has('setGoalAutonomy')) await o.setGoalAutonomy(g.id, off.length ? 'act' : 'act_with_checkpoints', off);
      loadGoal(g.id);
    });
    view.appendChild(ap);
    if (checkpointId) { const el = view.querySelector(`.cp-card[data-id="${CSS.escape(checkpointId)}"]`); if (el) el.scrollIntoView({ block: 'center' }); }
  }

  // ---------- "Make this a goal" from the composer
  // A long-term, measurable objective ("Grow a YouTube channel about desk setups to monetization",
  // "Get me to 10k newsletter subscribers") gets one quiet inline offer; everything else stays a task.
  function looksLikeGoal(text) {
    const t = String(text || '').toLowerCase().trim();
    if (t.split(/\s+/).length < 4) return false;
    const longTerm = /\b(grow|reach|get (me |us |it )?to|build( up)?|hit|double|triple|increase|launch|monetiz\w*|scale|go viral|until)\b/.test(t);
    const measurable = /\d|\b(subscribers?|followers?|views|audience|channel|newsletter|revenue|sales|users|customers|listeners|downloads|monetiz\w*|traffic|sign-?ups?|members|readers|mrr)\b/.test(t);
    return longTerm && measurable;
  }
  function syncGoalOffer() {
    const show = has('createGoal') && looksLikeGoal(promptEl.value);
    goalOffer.hidden = !show;
  }
  $('btn-make-goal').addEventListener('click', async () => {
    const text = promptEl.value.trim();
    if (!text) return;
    const btn = $('btn-make-goal');
    btn.disabled = true;
    btn.textContent = 'Setting the goal…';
    let r;
    try { r = await o.createGoal(text); } catch (err) { r = { ok: false, error: err && err.message }; }
    btn.disabled = false;
    btn.textContent = 'Make this a goal';
    if (!r || r.ok === false) { addMsg('error', `⚠ ${escapeHtml((r && r.error) || 'Could not set the goal')}`); startChat(); return; }
    promptEl.value = '';
    autosize();
    syncGoalOffer();
    await refreshGoals();
    if (r.goal) openGoal(r.goal.id);
  });

  // ------------------------------------------------------------ init

  window.addEventListener('focus', () => promptEl.focus());
  if (!hasInitialTheme) o.getSettings().then((s) => { savedTheme = s && THEMES.includes(s.theme) ? s.theme : 'glass'; applyTheme(savedTheme); }).catch(() => {});
  setGreeting();
  syncButtons();
  checkClaude();
  refreshExtension({ card: true });
  window.addEventListener('focus', () => { if (extCard) refreshExtension(); });
  refreshState();
  refreshTasks();
  refreshGoals();
  autosize();
})();
