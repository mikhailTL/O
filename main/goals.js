'use strict';
// O's long-term goals in the app: each active goal has one schedule (main/scheduler.js, at the goal's cadence);
// when it fires, the day's prompt comes from the engine (GET /goals/<id> next_cycle_prompt) and runs as an
// ordinary background task (main/tasks.js). The task's final message carries a JSON cycle record, posted to
// POST /goals/<id>/cycle. The engine (engine/o_engine/goals.py) owns goals and their journals.
//
// Checkpoints are enforced here, not left to the model:
//   - a goal task's bridge asks `gate()` before every tool call that classifies as a checkpoint action
//     (main/tools/checkpoints.js); without a matching approval it is refused, with instructions to file a
//     checkpoint card instead (bridge tool goal_checkpoint -> addCheckpoint);
//   - Approve on a card runs exactly the prepared action (an MCP connector call through the engine), or, when
//     the action needs the browser, grants one single-use approval for that kind and resumes the task;
//   - spend and create_account are never done by O (Approve hands them to the person), forbidden actions
//     (fake engagement, bought views, bots) are refused always, and only a goal the person set to "act" with
//     the kind listed in `allowed` skips the card.
//
//   const g = new GoalRunner({ engineCall, scheduler, startTask, getTasks, file, log, onChange });

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { EventEmitter } = require('events');
const { classifyAction, CHECKPOINT_KINDS, NEVER_BY_O, KIND_LABEL } = require('./tools/checkpoints');

const MAX_PENDING_PER_GOAL = 6;
const MAX_KEPT_CHECKPOINTS = 200;
const GRANT_MS = 2 * 60 * 60 * 1000;
const ACTIVE = ['queued', 'running', 'paused']; // a paused cycle (the person came back) is still this goal's cycle
const NOUN = { publish: 'post', send_message: 'message', spend: 'purchase', create_account: 'account', accept_terms: 'terms', delete_data: 'deletion' };

const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const newId = (p) => p + crypto.randomBytes(5).toString('hex');

/** The cycle record in an agent's final message: the last ```json block, else the last {...} with cycle fields. */
function parseCycleRecord(text) {
  const s = String(text || '');
  const looks = (o) => o && typeof o === 'object' && !Array.isArray(o) && ['plan', 'did', 'metrics', 'reflection', 'next'].some((k) => k in o);
  const fences = [...s.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi)].map((m) => m[1]);
  for (const body of fences.reverse()) {
    try { const o = JSON.parse(body.trim()); if (looks(o)) return o; } catch (_) { /* next */ }
  }
  for (let end = s.lastIndexOf('}'); end > 0; end = s.lastIndexOf('}', end - 1)) {
    let depth = 0;
    for (let i = end; i >= 0; i--) {
      if (s[i] === '}') depth++;
      else if (s[i] === '{' && --depth === 0) {
        try { const o = JSON.parse(s.slice(i, end + 1)); if (looks(o)) return o; } catch (_) { /* keep looking */ }
        break;
      }
    }
  }
  return null;
}

/** The scheduler `when` for a goal's cadence. */
function whenFor(goal) {
  const time = /^\d{2}:\d{2}$/.test(goal && goal.time) ? goal.time : '09:00';
  if (goal && goal.cadence === 'weekdays') return { type: 'weekly', days: ['mon', 'tue', 'wed', 'thu', 'fri'], time };
  if (goal && goal.cadence === 'every_n_hours') return { type: 'every', hours: Math.max(1, Math.min(168, Number(goal.every_hours) || 24)) };
  return { type: 'daily', time };
}

/** "Approve video ‘My $400 desk setup’" from what the agent filed. */
function cardTitle(cp) {
  const items = cp.items || {};
  if (!items.title && cp.title && /^approve\b/i.test(cp.title)) return clip(cp.title, 90);
  const noun = clip(items.type || NOUN[cp.kind] || 'action', 20).toLowerCase();
  const name = clip(items.title || cp.title || '', 60);
  return name ? `Approve ${noun} ‘${name}’` : `Approve: ${clip(cp.summary || KIND_LABEL[cp.kind] || cp.kind, 70)}`;
}

class GoalRunner extends EventEmitter {
  /**
   * @param {object} o
   * @param {(method:string, path:string, body?:object, timeoutMs?:number) => Promise<any>} o.engineCall
   * @param {import('./scheduler').Scheduler} o.scheduler
   * @param {(t:{prompt:string,title?:string,source?:string,goalId?:string,taskId?:string}) => {ok:boolean,taskId?:string,error?:string}} o.startTask
   * @param {() => object[]} [o.getTasks]  task snapshots (to skip a cycle while the goal's last one still runs)
   * @param {string} [o.file]  JSON file for checkpoints, grants and task→goal links
   * @param {(kind:string, summary:string, detail?:object) => void} [o.log]
   * @param {() => number} [o.now]
   */
  constructor({ engineCall, scheduler, startTask, getTasks, file, log, now } = {}) {
    super();
    if (typeof engineCall !== 'function') throw new Error('GoalRunner needs engineCall');
    this.engineCall = engineCall;
    this.scheduler = scheduler || null;
    this.startTask = startTask || (() => ({ ok: false, error: 'no task runner' }));
    this.getTasks = getTasks || (() => []);
    this.file = file || null;
    this.log = log || (() => {});
    this.now = now || (() => Date.now());
    this.goals = new Map(); // id -> summary from GET /goals
    this.checkpoints = [];
    this.grants = [];
    this.taskGoals = {};    // task id -> { goalId, kind: 'cycle' | 'followup' }
    this.recorded = [];     // task ids whose cycle record is already posted
    this._load();
  }

  // ---------------------------------------------------------------- persistence

  _load() {
    if (!this.file) return;
    try {
      const raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      if (Array.isArray(raw.checkpoints)) this.checkpoints = raw.checkpoints.filter((c) => c && c.id && c.goalId);
      if (Array.isArray(raw.grants)) this.grants = raw.grants.filter((g) => g && g.goalId && g.kind);
      if (raw.taskGoals && typeof raw.taskGoals === 'object') this.taskGoals = raw.taskGoals;
      if (Array.isArray(raw.recorded)) this.recorded = raw.recorded.slice(-200);
    } catch (_) { /* first run */ }
  }

  _save() {
    if (!this.file) return;
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      const keep = this.checkpoints.filter((c) => c.status === 'pending').concat(this.checkpoints.filter((c) => c.status !== 'pending').slice(-MAX_KEPT_CHECKPOINTS));
      this.checkpoints = this.checkpoints.filter((c) => keep.includes(c));
      const tg = Object.entries(this.taskGoals).slice(-400);
      this.taskGoals = Object.fromEntries(tg);
      const tmp = this.file + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify({ checkpoints: this.checkpoints, grants: this.grants, taskGoals: this.taskGoals, recorded: this.recorded.slice(-200) }, null, 2));
      fs.renameSync(tmp, this.file);
    } catch (e) { this.emit('error', e); }
  }

  _changed() { this.emit('changed'); }

  // ---------------------------------------------------------------- goals

  /** Reload the goal list from the engine and make every active goal have exactly one schedule. */
  async refresh() {
    const r = await this.engineCall('GET', '/goals', undefined, 8000);
    const list = Array.isArray(r && r.goals) ? r.goals : [];
    this.goals = new Map(list.map((g) => [g.id, g]));
    for (const g of list) this._syncSchedule(g);
    this._changed();
    return list;
  }

  _syncSchedule(g) {
    if (!this.scheduler) return null;
    const s = this.scheduler.forGoal(g.id);
    if (g.status === 'active') {
      if (s) return s;
      try {
        const added = this.scheduler.add({ prompt: `Goal cycle: ${g.title}`, title: `Goal · ${clip(g.title, 50)}`, when: whenFor(g), goalId: g.id });
        this.log('note', `Goal scheduled: ${g.title} — ${added.label}`, { goalId: g.id, scheduleId: added.id });
        return added;
      } catch (e) { this.log('note', `Goal could not be scheduled: ${e.message}`, { goalId: g.id }); return null; }
    }
    if (s) this.scheduler.cancel(s.id);
    return null;
  }

  _pending(goalId) { return this.checkpoints.filter((c) => c.status === 'pending' && (!goalId || c.goalId === goalId)); }

  _decorate(g) {
    const s = this.scheduler ? this.scheduler.forGoal(g.id) : null;
    const running = this._activeTask(g.id);
    return { ...g, nextRun: s ? s.nextRun : null, scheduleLabel: s ? s.label : '', pendingCheckpoints: this._pending(g.id).length, runningTaskId: running ? running.id : null };
  }

  /** For the panel: goals (newest active first) and every pending checkpoint card. */
  list() {
    return { goals: [...this.goals.values()].filter((g) => g.status !== 'archived').map((g) => this._decorate(g)), checkpoints: this._pending().map((c) => this._cardOut(c)) };
  }

  async create(text, { startNow = true } = {}) {
    const r = await this.engineCall('POST', '/goals', { text: String(text || '') }, 30000);
    return this._created(r, startNow);
  }

  async createFromProposal(proposal, { startNow = true } = {}) {
    const r = await this.engineCall('POST', '/goals', { proposal }, 30000);
    return this._created(r, startNow);
  }

  async _created(r, startNow) {
    if (!r || !r.goal) return { ok: false, error: (r && (r.detail || r.error)) || 'the engine did not create the goal' };
    const g = r.goal;
    this.log('note', `Goal set: ${g.title} (${g.horizon_days} days, ${g.cadence}, ${g.autonomy})`, { goalId: g.id, origin: g.origin });
    await this.refresh().catch(() => { this.goals.set(g.id, { ...g, day: 1 }); this._syncSchedule(g); });
    let first = null;
    if (startNow) first = await this.fireCycle(g.id, { reason: 'first cycle' });
    this._changed();
    return { ok: true, goal: this._decorate(this.goals.get(g.id) || g), taskId: first && first.taskId };
  }

  async propose() {
    const r = await this.engineCall('POST', '/goals/propose', {}, 30000);
    return { ok: true, proposals: Array.isArray(r && r.proposals) ? r.proposals : [] };
  }

  async detail(id) {
    const d = await this.engineCall('GET', `/goals/${encodeURIComponent(id)}`, undefined, 8000);
    if (d && d.goal) this.goals.set(id, { ...(this.goals.get(id) || {}), ...d.goal, day: d.day });
    const g = this.goals.get(id) || (d && d.goal) || {};
    return {
      ok: true, goal: this._decorate({ ...g, ...(d && d.goal), day: d && d.day }), day: d && d.day, cycles: (d && d.cycles) || [], series: (d && d.series) || {},
      checkpoints: this.checkpoints.filter((c) => c.goalId === id).slice(-20).reverse().map((c) => this._cardOut(c)),
    };
  }

  async setStatus(id, op) {
    if (!['pause', 'resume', 'archive'].includes(op)) return { ok: false, error: 'op must be pause, resume or archive' };
    const r = await this.engineCall('POST', `/goals/${encodeURIComponent(id)}/${op}`, {}, 8000);
    if (r && r.goal) { this.goals.set(id, { ...(this.goals.get(id) || {}), ...r.goal }); this._syncSchedule(r.goal); }
    if (op === 'archive') for (const c of this._pending(id)) { c.status = 'skipped'; c.resolvedAt = this.now(); }
    this._save();
    this.log('note', `Goal ${op === 'resume' ? 'resumed' : op === 'pause' ? 'paused' : 'archived'}: ${(r && r.goal && r.goal.title) || id}`, { goalId: id });
    this._changed();
    return { ok: true, goal: r && r.goal ? this._decorate(this.goals.get(id)) : null };
  }

  /** The person's explicit autonomy choice (panel only, never a bridge tool). */
  async setAutonomy(id, autonomy, allowed) {
    const r = await this.engineCall('POST', `/goals/${encodeURIComponent(id)}/autonomy`, { autonomy, allowed: Array.isArray(allowed) ? allowed : [] }, 8000);
    if (r && r.goal) this.goals.set(id, { ...(this.goals.get(id) || {}), ...r.goal });
    this.log('policy', `Goal autonomy: ${(r && r.goal && r.goal.title) || id} → ${r && r.goal && r.goal.autonomy}${r && r.goal && r.goal.allowed && r.goal.allowed.length ? ` (allowed: ${r.goal.allowed.join(', ')})` : ''}`, { goalId: id });
    this._changed();
    return { ok: true, goal: r && r.goal };
  }

  _activeTask(goalId) {
    let list = [];
    try { list = this.getTasks() || []; } catch (_) { list = []; }
    return list.find((t) => t && ACTIVE.includes(t.status) && (t.goalId === goalId || (this.taskGoals[t.id] && this.taskGoals[t.id].goalId === goalId))) || null;
  }

  /** Notes for the next prompt: what happened to this goal's checkpoints since its last cycle. */
  _notes(goalId, sinceMs) {
    const out = [];
    for (const c of this.checkpoints.filter((x) => x.goalId === goalId)) {
      const t = cardTitle(c).replace(/^Approve\s+/i, '');
      if (c.status === 'pending') out.push(`Waiting for the person's approval (do not redo it): ${t}`);
      else if ((c.resolvedAt || 0) > sinceMs) {
        const word = { done: 'Approved and done', approved: 'Approved; finish it now', failed: 'Approved but the action failed', skipped: 'Skipped by the person (do not retry it)', edited: 'Sent back with changes', handed_off: 'Handed to the person to do themselves' }[c.status] || c.status;
        out.push(`${word}: ${t}${c.status === 'failed' && c.result ? ` (${clip(c.result, 120)})` : ''}${c.status === 'edited' && c.edit ? ` — their change: ${clip(c.edit, 200)}` : ''}`);
      }
    }
    return out.slice(-8);
  }

  /** Start today's cycle for one goal (scheduler fire, the first cycle, or "Run now"). */
  async fireCycle(goalId, { reason = 'scheduled' } = {}) {
    const g = this.goals.get(goalId);
    if (g && g.status !== 'active') return { ok: false, error: `goal is ${g.status}` };
    const busy = this._activeTask(goalId);
    if (busy) { this.log('note', `Goal cycle skipped: the last one is still running (${g ? g.title : goalId})`, { goalId, taskId: busy.id }); return { ok: false, error: 'the last cycle is still running', taskId: busy.id }; }
    let d;
    try { d = await this.engineCall('GET', `/goals/${encodeURIComponent(goalId)}`, undefined, 8000); } catch (e) { this.log('note', `Goal cycle could not start: ${e.message}`, { goalId }); return { ok: false, error: e.message }; }
    if (!d || !d.goal || !d.next_cycle_prompt) return { ok: false, error: 'the engine did not return a cycle prompt' };
    if (d.goal.status !== 'active') return { ok: false, error: `goal is ${d.goal.status}` };
    const lastMs = (Number(d.goal.last_cycle) || 0) * 1000;
    const notes = this._notes(goalId, lastMs);
    const prompt = d.next_cycle_prompt + (notes.length ? '\n\nCheckpoints since the last cycle:\n' + notes.map((n) => '- ' + n).join('\n') : '');
    const r = this.startTask({ prompt, title: `${clip(d.goal.title, 46)} · day ${d.day}`, source: 'goal', goalId });
    if (r && r.ok && r.taskId) {
      this.taskGoals[r.taskId] = { goalId, kind: 'cycle' };
      this._save();
      this.log('task', `Goal cycle started (${reason}): ${d.goal.title}, day ${d.day} of ${d.goal.horizon_days}`, { goalId, taskId: r.taskId });
    }
    this._changed();
    return r;
  }

  goalForTask(taskId) { const x = this.taskGoals[String(taskId || '')]; return x ? x.goalId : null; }

  /** A goal task ended: post its cycle record (once). Follow-ups only post when they carry a record. */
  async onTaskEnd({ task, ok, result }) {
    if (!task) return null;
    const link = this.taskGoals[task.id] || (task.goalId ? { goalId: task.goalId, kind: 'cycle' } : null);
    if (!link || this.recorded.includes(task.id + ':' + (task.runs || 1))) return null;
    let body = parseCycleRecord(result != null ? result : task.result);
    if (!body) {
      if (link.kind !== 'cycle') return null;
      body = {
        plan: [], did: [],
        reflection: task.status === 'stopped' ? 'The cycle was stopped before it finished.' : ok ? 'The cycle finished without a cycle record.' : `The cycle hit a snag: ${clip(task.error || task.statusLine || 'unknown', 200)}`,
        next: '', metrics: {},
      };
    }
    return this._post(link.goalId, task.id, task.runs || 1, body);
  }

  /** goal_record_cycle from a goal task's bridge. */
  async recordCycle(taskId, body) {
    const goalId = this.goalForTask(taskId) || (body && body.goal_id);
    if (!goalId) return { ok: false, error: 'this task is not working on a goal' };
    const t = (this.getTasks() || []).find((x) => x && x.id === taskId);
    return this._post(goalId, taskId, (t && t.runs) || 1, body || {});
  }

  async _post(goalId, taskId, run, body) {
    const clean = { ...body, task_id: String(taskId || '').slice(0, 120) || undefined };
    delete clean.goal_id;
    try {
      const r = await this.engineCall('POST', `/goals/${encodeURIComponent(goalId)}/cycle`, clean, 8000);
      this.recorded.push(String(taskId) + ':' + run);
      this._save();
      const cyc = r && r.cycle;
      const flags = cyc && cyc.flags && cyc.flags.length ? ` [${cyc.flags.join(', ')}]` : '';
      this.log('result', `Goal cycle recorded: ${(this.goals.get(goalId) || {}).title || goalId}${flags}`, { goalId, taskId, flags: (cyc && cyc.flags) || [] });
      await this.refresh().catch(() => {});
      return { ok: true, cycle: cyc };
    } catch (e) {
      this.log('note', `Goal cycle not recorded: ${e.message}`, { goalId, taskId });
      return { ok: false, error: e.message };
    }
  }

  // ---------------------------------------------------------------- checkpoints

  _cardOut(c) {
    return { ...c, headline: cardTitle(c), kindLabel: KIND_LABEL[c.kind] || c.kind, personDoesIt: NEVER_BY_O.includes(c.kind), goalTitle: (this.goals.get(c.goalId) || {}).title || c.goalTitle || '' };
  }

  pendingCards() { return this._pending().map((c) => this._cardOut(c)); }

  /** goal_checkpoint from a goal task: everything is prepared; the person approves, edits or skips. */
  addCheckpoint(taskId, body) {
    const b = body && typeof body === 'object' ? body : {};
    const goalId = this.goalForTask(taskId) || (typeof b.goal_id === 'string' ? b.goal_id : null);
    if (!goalId) return { ok: false, error: 'checkpoints belong to a goal; this task is not working on one' };
    const kind = String(b.kind || '');
    if (!CHECKPOINT_KINDS.includes(kind)) return { ok: false, error: `kind must be one of ${CHECKPOINT_KINDS.join(', ')}` };
    let action = null;
    if (b.action && typeof b.action === 'object') {
      const a = b.action;
      if (typeof a.server !== 'string' || !a.server || typeof a.tool !== 'string' || !a.tool) return { ok: false, error: 'action needs server and tool (an MCP connector call); leave it out for browser steps' };
      action = { server: a.server.slice(0, 200), tool: a.tool.slice(0, 200), args: a.args && typeof a.args === 'object' ? a.args : {} };
      const c = classifyAction('mcp__o__mcp_call', action);
      if (c && c.kind === 'forbidden') return { ok: false, error: 'O never fakes engagement or works around platform rules; that action is refused' };
    }
    if (this._pending(goalId).length >= MAX_PENDING_PER_GOAL) return { ok: false, error: `${MAX_PENDING_PER_GOAL} checkpoints are already waiting for this goal; let the person catch up first` };
    const items = {};
    if (b.items && typeof b.items === 'object') for (const [k, v] of Object.entries(b.items).slice(0, 12)) if (v != null) items[String(k).slice(0, 40)] = clip(typeof v === 'string' ? v : JSON.stringify(v), 600);
    const cp = {
      id: newId('c_'), goalId, goalTitle: (this.goals.get(goalId) || {}).title || '', taskId: String(taskId || ''), kind,
      title: clip(b.title, 100), summary: clip(b.summary, 400), items, action, status: 'pending', createdAt: this.now(), resolvedAt: null, result: '', edit: '',
    };
    this.checkpoints.push(cp);
    this._save();
    this.log('policy', `Checkpoint waiting for you: ${cardTitle(cp)}`, { goalId, taskId, checkpointId: cp.id, kind, hasAction: !!action });
    this._changed();
    this.emit('checkpoint', this._cardOut(cp));
    return { ok: true, checkpoint: { id: cp.id, status: 'pending', headline: cardTitle(cp) }, note: 'Filed. The person approves, edits or skips it in O; do not perform the action. Continue with other work and record the cycle.' };
  }

  /** Approve / Edit / Skip from the notify card or the goal view. */
  async resolveCheckpoint(id, decision, editText) {
    const cp = this.checkpoints.find((c) => c.id === id);
    if (!cp) return { ok: false, error: 'no such checkpoint' };
    if (cp.status !== 'pending') return { ok: false, error: `already ${cp.status}` };
    const title = cardTitle(cp);
    if (decision === 'skip') {
      cp.status = 'skipped'; cp.resolvedAt = this.now();
      this.log('policy', `Checkpoint skipped: ${title}`, { checkpointId: id, goalId: cp.goalId });
    } else if (decision === 'edit') {
      const text = clip(editText, 1000);
      if (!text) return { ok: false, error: 'say what to change' };
      cp.status = 'edited'; cp.resolvedAt = this.now(); cp.edit = text;
      this.log('policy', `Checkpoint sent back with changes: ${title}`, { checkpointId: id, goalId: cp.goalId });
      this._followUp(cp, `The person reviewed your checkpoint "${title}" and wants changes: ${text}\nRevise what you prepared accordingly, then file a new checkpoint with mcp__o__goal_checkpoint. Do not perform the action yourself.`);
    } else if (decision === 'approve') {
      cp.resolvedAt = this.now();
      if (NEVER_BY_O.includes(cp.kind)) {
        cp.status = 'handed_off';
        this.log('policy', `Checkpoint handed to you (O never does this itself): ${title}`, { checkpointId: id, goalId: cp.goalId, kind: cp.kind });
      } else if (cp.action) {
        cp.status = 'approved';
        this.log('policy', `Checkpoint approved: ${title} — running ${cp.action.server}/${cp.action.tool}`, { checkpointId: id, goalId: cp.goalId, action: { server: cp.action.server, tool: cp.action.tool } });
        try {
          const r = await this.engineCall('POST', '/tools/call', { server: cp.action.server, tool: cp.action.tool, args: cp.action.args }, 120000);
          const bad = !r || r.ok === false || r.is_error || r.isError || r.error;
          cp.status = bad ? 'failed' : 'done';
          cp.result = clip(typeof r === 'string' ? r : JSON.stringify(r), 400);
        } catch (e) { cp.status = 'failed'; cp.result = clip(e.message, 400); }
        this.log(cp.status === 'done' ? 'result' : 'note', `Checkpoint ${cp.status === 'done' ? 'done' : 'failed'}: ${title}${cp.status === 'failed' ? ` (${cp.result})` : ''}`, { checkpointId: id, goalId: cp.goalId });
      } else {
        cp.status = 'approved';
        this.grants.push({ goalId: cp.goalId, kind: cp.kind, checkpointId: cp.id, expiresAt: this.now() + GRANT_MS, used: false });
        this.log('policy', `Checkpoint approved: ${title} (one ${cp.kind} action allowed)`, { checkpointId: id, goalId: cp.goalId, kind: cp.kind });
        this._followUp(cp, `The person approved your checkpoint "${title}". Do exactly that one ${cp.kind.replace('_', ' ')} action now, as prepared, and nothing else that needs approval. Then say in one line that it is done.`);
      }
    } else return { ok: false, error: 'decision must be approve, edit or skip' };
    this._save();
    this._changed();
    return { ok: true, checkpoint: this._cardOut(cp) };
  }

  _followUp(cp, prompt) {
    if (!cp.taskId) return;
    this.taskGoals[cp.taskId] = { ...(this.taskGoals[cp.taskId] || { goalId: cp.goalId }), goalId: cp.goalId, kind: 'followup' };
    const r = this.startTask({ prompt, taskId: cp.taskId, source: 'goal', goalId: cp.goalId });
    if (!r || !r.ok) this.log('note', `Could not resume the goal task: ${(r && r.error) || 'unknown'}`, { checkpointId: cp.id });
  }

  /**
   * The bridge asks before a goal task's tool call runs. Returns {allow, kind?, message?}.
   * @param {{goalId?:string, taskId?:string, toolName:string, input:object}} q
   */
  gate(q) {
    const toolName = String((q && q.toolName) || '');
    const input = q && q.input && typeof q.input === 'object' ? q.input : {};
    const goalId = (q && q.goalId) || this.goalForTask(q && q.taskId);
    const c = classifyAction(toolName, input);
    if (!c) return { allow: true };
    const g = goalId ? this.goals.get(goalId) : null;
    const title = g ? g.title : 'this goal';
    const deny = (message, extra) => {
      this.log('policy', `Checkpoint held: ${toolName} (${c.kind}) for ${title}`, { goalId, taskId: q && q.taskId, toolName, kind: c.kind, ...extra });
      return { allow: false, kind: c.kind, message };
    };
    if (c.kind === 'forbidden') return deny('Refused by O: no fake engagement, bought views or followers, bots or platform-rule workarounds, ever. Grow the goal with real work only.');
    if (!goalId) return { allow: true, kind: c.kind }; // not a goal task: the normal permission mode applies
    if (NEVER_BY_O.includes(c.kind)) return deny(`Checkpoint (${c.kind}): O never ${c.kind === 'spend' ? 'spends money or starts paid services' : 'creates accounts'} itself. Prepare everything the person needs and call mcp__o__goal_checkpoint with kind "${c.kind}" so they can do it.`);
    if (g && g.autonomy === 'act' && Array.isArray(g.allowed) && g.allowed.includes(c.kind)) {
      this.log('policy', `Allowed by the goal's own setting: ${toolName} (${c.kind}) for ${title}`, { goalId, toolName, kind: c.kind });
      return { allow: true, kind: c.kind, bySetting: true };
    }
    const now = this.now();
    const grant = this.grants.find((x) => x.goalId === goalId && x.kind === c.kind && !x.used && x.expiresAt > now);
    if (grant) {
      grant.used = true;
      const cp = this.checkpoints.find((x) => x.id === grant.checkpointId);
      if (cp && cp.status === 'approved') { cp.status = 'done'; cp.result = `${toolName} ran after approval`; }
      this.grants = this.grants.filter((x) => !x.used && x.expiresAt > now);
      this._save();
      this.log('policy', `Approved checkpoint used: ${toolName} (${c.kind}) for ${title}`, { goalId, toolName, kind: c.kind, checkpointId: grant.checkpointId });
      this._changed();
      return { allow: true, kind: c.kind, checkpointId: grant.checkpointId };
    }
    return deny(`Checkpoint (${c.kind}): nothing ${c.kind === 'publish' ? 'goes public' : 'like this happens'} without the person's approval. Prepare everything (for a video: the finished file, title, description, thumbnail, schedule) and call mcp__o__goal_checkpoint with kind "${c.kind}", the items and the exact action. Do not retry this call.`);
  }

  /** POST /goal from a task's bridge: {op, ...}. */
  async handle(taskId, body) {
    const op = String((body && body.op) || '');
    try {
      if (op === 'gate') return { ok: true, ...this.gate({ goalId: body.goalId, taskId, toolName: body.tool_name, input: body.input }) };
      if (op === 'checkpoint') return this.addCheckpoint(taskId, body);
      if (op === 'cycle') return await this.recordCycle(taskId, body.record || {});
      if (op === 'create') {
        const text = String(body.text || '').trim();
        if (!text) return { ok: false, error: 'text must be a non-empty string' };
        return await this.create(text);
      }
      if (op === 'propose') return await this.propose();
      if (op === 'status') {
        if (body.id) return await this.detail(String(body.id));
        return { ok: true, ...this.list() };
      }
      return { ok: false, error: 'op must be gate, checkpoint, cycle, create, propose or status' };
    } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
  }
}

module.exports = { GoalRunner, parseCycleRecord, whenFor, cardTitle };
