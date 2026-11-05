'use strict';
// O's task pool: every request is a task with its own Claude Code process and session (one OAgent
// each), up to maxParallelTasks at once; the rest wait in a queue. Tasks run whether or not the panel
// is open. Stopping one task never touches the others.
//
//   const tasks = new TaskManager({ createAgent: (taskId) => new OAgent({ ..., taskId }), getSettings });
//   tasks.on('event', (ev) => ...)   // every agent event, tagged ev.taskId
//   tasks.on('changed', () => ...)   // the list changed (status, status line, queue)
//   tasks.on('end', ({ task, ok }) => ...)
//   const { taskId } = tasks.start({ prompt: 'find me a desk' });
//
// Presence (plan J, main/ambient.js): every task has a priority, high | normal | low (goal cycles and
// scheduled tasks are low unless told otherwise). setPresence('absent') resumes paused tasks; going from
// 'absent' to 'present' pauses running low-priority tasks at their next safe point (no tool call in flight:
// the Claude Code process is stopped and later resumed in the same session) and holds queued ones.
// 'unknown' (ambient sensing off) changes nothing.

const crypto = require('crypto');
const { EventEmitter } = require('events');

const DEFAULT_MAX = 3;
const MAX_MAX = 8;
const MAX_KEPT = 60;     // ended tasks kept in memory for the strip and the Runs sheet
const ACTIVE = ['queued', 'running'];
const PRIORITIES = ['high', 'normal', 'low'];
const PRESENCE = ['present', 'absent', 'unknown'];
const PAUSED_LINE = 'Paused · resumes when you step away';
const RESUME_PROMPT = 'Continue the same task from exactly where you stopped. You were paused at a safe point because the person came back to the computer; do not redo steps that already finished.';

const newTaskId = () => 't' + Date.now().toString(36) + crypto.randomBytes(3).toString('hex');
const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };

function titleOf(prompt) { return clip(prompt, 60) || 'New task'; }
/** Goal cycles and scheduled tasks are background work (low); what the person asks for is normal. */
function defaultPriority(source) { return source === 'goal' || source === 'schedule' ? 'low' : 'normal'; }

class TaskManager extends EventEmitter {
  /**
   * @param {object} o
   * @param {(taskId:string) => import('./agent').OAgent} o.createAgent  a fresh agent bound to this task id
   * @param {() => object} [o.getSettings]  reads maxParallelTasks
   * @param {() => number} [o.now]
   */
  constructor({ createAgent, getSettings, now } = {}) {
    super();
    if (typeof createAgent !== 'function') throw new Error('TaskManager needs createAgent');
    this.createAgent = createAgent;
    this.getSettings = getSettings || (() => ({}));
    this.now = now || (() => Date.now());
    this.tasks = new Map(); // id -> task (insertion order = creation order)
    this.queue = [];        // task ids waiting for a slot
    this._spare = null;     // { id, agent } pre-warmed for the next new task
    this._claimed = new Map(); // task id -> pre-warmed agent claimed by a task that has not started yet
    this._disposed = false;
    this.presence = 'unknown';
  }

  maxParallel() {
    const n = parseInt((this.getSettings() || {}).maxParallelTasks, 10);
    return Number.isFinite(n) ? Math.max(1, Math.min(MAX_MAX, n)) : DEFAULT_MAX;
  }

  running() { return [...this.tasks.values()].filter((t) => t.status === 'running'); }
  activeCount() { return [...this.tasks.values()].filter((t) => ACTIVE.includes(t.status) || t.status === 'paused').length; }
  paused() { return [...this.tasks.values()].filter((t) => t.status === 'paused'); }
  get(id) { return this.tasks.get(String(id || '')) || null; }

  /** Snapshot for the renderer (no agent objects). Newest first. */
  list() {
    return [...this.tasks.values()].reverse().map((t) => this.snapshot(t));
  }

  snapshot(t) {
    return {
      id: t.id, title: t.title, prompt: t.prompt, status: t.status, statusLine: t.statusLine, mood: t.mood,
      createdAt: t.createdAt, startedAt: t.startedAt, endedAt: t.endedAt, source: t.source, scheduleId: t.scheduleId || null, goalId: t.goalId || null,
      priority: t.priority || 'normal', pausing: !!t.pauseRequested,
      position: t.status === 'queued' ? this.queue.indexOf(t.id) + 1 : 0, result: clip(t.result, 300), error: clip(t.error, 300),
      runs: t.runs, tools: [...t.tools].slice(0, 20),
    };
  }

  _changed() { this.emit('changed'); }

  /**
   * Start a task (or queue it). With `taskId` of an ended task, the prompt is a follow-up in that task's
   * session; with a task that is still active, a new task is started instead.
   * @returns {{ok:boolean, taskId?:string, queued?:boolean, position?:number, followUp?:boolean, error?:string}}
   */
  start({ prompt, taskId, title, source = 'user', scheduleId = null, goalId = null, priority } = {}) {
    prompt = String(prompt || '').trim();
    if (!prompt) return { ok: false, error: 'empty prompt' };
    if (this._disposed) return { ok: false, error: 'O is shutting down' };
    let t = taskId ? this.get(taskId) : null;
    let followUp = false;
    if (t && !ACTIVE.includes(t.status)) {
      followUp = true;
      t.prompt = prompt;
      t.status = 'queued';
      t.statusLine = 'Waiting for a free slot';
      t.endedAt = null;
      t.result = ''; t.error = '';
      t.runPrompt = null; t.pauseRequested = false;
      if (PRIORITIES.includes(priority)) t.priority = priority;
    } else {
      let id = null;
      // A goal cycle gets its own agent (its bridge gates checkpoint actions), never the generic pre-warmed one.
      if (this._spare && !taskId && !goalId) { // the pre-warmed agent already carries this id: claim it for this task
        id = this._spare.id;
        this._claimed.set(id, this._spare.agent);
        this._spare = null;
        setImmediate(() => { this.prewarm().catch(() => {}); }); // warm the next one
      }
      id = id || newTaskId();
      t = {
        id, title: clip(title, 60) || titleOf(prompt), prompt, status: 'queued', statusLine: 'Waiting for a free slot', mood: 'listening',
        createdAt: this.now(), startedAt: null, endedAt: null, source, scheduleId, goalId: goalId ? String(goalId) : null, sessionId: null, agent: null,
        result: '', error: '', runs: 0, tools: new Set(),
        priority: PRIORITIES.includes(priority) ? priority : defaultPriority(source), inFlight: 0, pauseRequested: false, pausing: false, runPrompt: null,
      };
      this.tasks.set(id, t);
      this._trim();
    }
    this.queue.push(t.id);
    this._pump();
    const queued = t.status === 'queued';
    if (queued) this._emitTask(t, { type: 'status', mood: 'listening', text: `Queued (#${this.queue.indexOf(t.id) + 1})` });
    this._changed();
    return { ok: true, taskId: t.id, queued, position: queued ? this.queue.indexOf(t.id) + 1 : 0, followUp };
  }

  _emitTask(t, ev) { this.emit('event', { ...ev, taskId: t.id }); }

  _trim() {
    if (this.tasks.size <= MAX_KEPT) return;
    for (const [id, t] of this.tasks) {
      if (this.tasks.size <= MAX_KEPT) break;
      if (!ACTIVE.includes(t.status) && t.status !== 'paused') this.tasks.delete(id);
    }
  }

  _pump() {
    while (this.queue.length && this.running().length < this.maxParallel()) {
      const t = this.get(this.queue.shift());
      if (!t || t.status !== 'queued') continue;
      this._run(t);
    }
  }

  _agentFor(t) {
    if (this._claimed.has(t.id)) {
      const a = this._claimed.get(t.id);
      this._claimed.delete(t.id);
      return a;
    }
    const a = this.createAgent(t.id, { goalId: t.goalId || null });
    a.sessionId = t.sessionId || null;
    return a;
  }

  async _run(t) {
    t.status = 'running';
    t.startedAt = this.now();
    t.runs += 1;
    t.statusLine = 'Starting…';
    t.mood = 'thinking';
    t.inFlight = 0;
    t.pausing = false;
    const runPrompt = t.runPrompt || t.prompt;
    t.runPrompt = null;
    const agent = this._agentFor(t);
    agent.sessionId = t.sessionId || agent.sessionId || null;
    t.agent = agent;
    const onEvent = (ev) => this._onAgentEvent(t, ev);
    agent.on('event', onEvent);
    this._changed();
    let r = { ok: false };
    try { r = await agent.run(runPrompt); } catch (e) { r = { ok: false }; t.error = String(e && e.message || e); }
    agent.removeListener('event', onEvent);
    t.sessionId = agent.sessionId || t.sessionId;
    t.warm = !!agent.lastRunWarm;
    t.agent = null;
    try { agent.dispose(); } catch (_) {}
    if (t.pausing) { // stopped at a safe point for the person's return: kept, with its session, to resume later
      t.pausing = false;
      t.pauseRequested = false;
      t.status = 'paused';
      t.statusLine = PAUSED_LINE;
      t.mood = 'idle';
      this._emitTask(t, { type: 'status', mood: 'idle', text: PAUSED_LINE, paused: true });
      this.emit('paused', { task: this.snapshot(t), sessionId: t.sessionId });
      this._changed();
      if (!this._disposed) this._pump();
      return;
    }
    t.pauseRequested = false;
    if (t.status === 'running') t.status = r.ok ? 'done' : (t.stopRequested ? 'stopped' : 'failed');
    t.stopRequested = false;
    t.endedAt = this.now();
    t.mood = t.status === 'done' ? 'success' : t.status === 'stopped' ? 'idle' : 'error';
    if (t.status === 'done') { t.result = r.result || t.result; t.statusLine = 'Done'; }
    else if (t.status === 'stopped') t.statusLine = 'Stopped';
    else t.statusLine = clip(t.error || 'Hit a snag', 80);
    // `result` is the whole final message (the snapshot clips it): a goal cycle's JSON record is read from it.
    this.emit('end', { task: this.snapshot(t), ok: t.status === 'done', sessionId: t.sessionId, durationMs: t.endedAt - (t.startedAt || t.endedAt), result: t.result || '' });
    this._changed();
    if (!this._disposed) { this._pump(); this.prewarm(); }
  }

  _onAgentEvent(t, ev) {
    if (!ev || typeof ev !== 'object') return;
    if (t.pausing && (ev.type === 'done' || ev.type === 'status')) return; // the pause's own stop: not a "stopped" task
    if (ev.type === 'status') {
      t.statusLine = clip(ev.text, 80) || t.statusLine; t.mood = ev.mood || t.mood;
      if (ev.toolDone) t.inFlight = Math.max(0, (t.inFlight || 0) - 1);
    }
    else if (ev.type === 'tool') { t.statusLine = clip(ev.summary || ev.name, 80); if (ev.name) t.tools.add(ev.name); t.inFlight = (t.inFlight || 0) + 1; }
    else if (ev.type === 'error') t.error = String(ev.message || 'error');
    else if (ev.type === 'done') { if (ev.stopped || ev.result === '(stopped)') t.status = 'stopped'; else if (typeof ev.result === 'string') t.result = ev.result; }
    this.emit('event', { ...ev, taskId: t.id });
    if (ev.type === 'status' && ev.toolDone && t.pauseRequested && !t.inFlight) this._pauseNow(t);
    if (ev.type === 'status' || ev.type === 'tool') this._changed();
  }

  /** True when no tool call of this task is executing: stopping now loses no half-done side effect. */
  atSafePoint(t) { return !!t && t.status === 'running' && !(t.inFlight > 0); }

  _pauseNow(t) {
    if (!t || t.status !== 'running' || !t.agent || t.pausing) return false;
    t.pausing = true;
    t.statusLine = 'Pausing…';
    try { t.agent.cancel(); } catch (_) {}
    return true;
  }

  /**
   * Pause every low-priority task: running ones at their next safe point (now when no tool is in flight),
   * queued ones are held. @returns {{paused:string[], pending:string[], held:string[]}}
   */
  pauseLow() {
    const out = { paused: [], pending: [], held: [] };
    for (const t of this.tasks.values()) {
      if ((t.priority || 'normal') !== 'low') continue;
      if (t.status === 'queued') {
        this.queue = this.queue.filter((q) => q !== t.id);
        t.status = 'paused'; t.statusLine = PAUSED_LINE; t.mood = 'idle';
        out.held.push(t.id);
        this._emitTask(t, { type: 'status', mood: 'idle', text: PAUSED_LINE, paused: true });
      } else if (t.status === 'running' && !t.pausing) {
        t.pauseRequested = true;
        if (this.atSafePoint(t)) { this._pauseNow(t); out.paused.push(t.id); } else out.pending.push(t.id);
      }
    }
    this._changed();
    return out;
  }

  /** Resume every paused task in its own session (a held one simply starts). @returns {string[]} ids */
  resumePaused() {
    const ids = [];
    for (const t of this.tasks.values()) {
      if (t.status === 'running' && t.pauseRequested && !t.pausing) { t.pauseRequested = false; continue; }
      if (t.status !== 'paused') continue;
      t.status = 'queued';
      t.statusLine = 'Waiting for a free slot';
      if (t.runs > 0 && t.sessionId) t.runPrompt = RESUME_PROMPT; // started before: pick the session up again
      this.queue.push(t.id);
      ids.push(t.id);
    }
    if (ids.length) { this._pump(); this._changed(); }
    return ids;
  }

  resume(id) {
    const t = this.get(id);
    if (!t || t.status !== 'paused') return { ok: false, error: 'that task is not paused' };
    t.status = 'queued'; t.statusLine = 'Waiting for a free slot';
    if (t.runs > 0 && t.sessionId) t.runPrompt = RESUME_PROMPT;
    this.queue.push(t.id); this._pump(); this._changed();
    return { ok: true };
  }

  /**
   * The person's presence from the ambient controller. Away -> paused work resumes; back from away ->
   * low-priority work pauses. @returns {{prev:string, state:string, paused?:object, resumed?:string[]}}
   */
  setPresence(state) {
    state = PRESENCE.includes(state) ? state : 'unknown';
    const prev = this.presence;
    this.presence = state;
    const r = { prev, state };
    if (state === prev) return r;
    if (state === 'absent') r.resumed = this.resumePaused();
    else if (state === 'present' && prev === 'absent') r.paused = this.pauseLow();
    this.emit('presence', r);
    return r;
  }

  /** Stop one task: a queued one leaves the queue, a running one has its Claude Code process killed. */
  stop(id) {
    const t = this.get(id);
    if (!t) return { ok: false, error: 'no such task' };
    if (t.status === 'queued' || t.status === 'paused') {
      this.queue = this.queue.filter((q) => q !== t.id);
      t.status = 'stopped';
      t.statusLine = 'Stopped';
      t.endedAt = this.now();
      const claimed = this._claimed.get(t.id);
      if (claimed) { this._claimed.delete(t.id); try { claimed.dispose(); } catch (_) {} }
      this._emitTask(t, { type: 'done', result: '(stopped)', stopped: true });
      this.emit('end', { task: this.snapshot(t), ok: false, sessionId: t.sessionId, durationMs: 0 });
      this._changed();
      return { ok: true, wasQueued: true };
    }
    if (t.status === 'running' && t.agent) {
      t.stopRequested = true;
      t.pauseRequested = false;
      if (t.pausing) { t.pausing = false; t.status = 'stopped'; } // a stop wins over a pause in progress
      t.agent.cancel();
      return { ok: true };
    }
    return { ok: true, alreadyEnded: true };
  }

  /** Drop every queued task (not started yet). Returns how many. */
  dropQueued() {
    const ids = this.queue.slice();
    for (const id of ids) this.stop(id);
    return ids.length;
  }

  stopAll() {
    for (const t of this.paused()) this.stop(t.id);
    const dropped = this.dropQueued();
    for (const t of this.running()) this.stop(t.id);
    return { ok: true, dropped };
  }

  /** Speed: keep one idle agent with a pre-spawned Claude Code process for the next new task. */
  async prewarm() {
    if (this._disposed) return false;
    if (!this._spare) {
      const id = newTaskId();
      let agent;
      try { agent = this.createAgent(id); } catch (_) { return false; }
      this._spare = { id, agent };
    }
    const spare = this._spare;
    if (typeof spare.agent.prewarm !== 'function') return false;
    const ok = await spare.agent.prewarm();
    return ok && this._spare === spare;
  }

  dispose() {
    this._disposed = true;
    this.stopAll();
    if (this._spare) { try { this._spare.agent.dispose(); } catch (_) {} this._spare = null; }
    for (const a of this._claimed.values()) { try { a.dispose(); } catch (_) {} }
    this._claimed.clear();
  }
}

module.exports = { TaskManager, titleOf, defaultPriority, DEFAULT_MAX, MAX_MAX, PRIORITIES, RESUME_PROMPT, PAUSED_LINE };
