'use strict';
// O's scheduler: saved prompts that start a task at a time or on repeat.
// Persisted as JSON in userData (o-schedules.json). Times are the user's local time.
//
//   when: {type:'once', at:'2026-11-06T08:00'} | {type:'daily', time:'08:00'}
//       | {type:'weekly', days:['mon','fri'] | [1,5], time:'08:00'} | {type:'every', hours:3}
//
//   const s = new Scheduler({ file, now: () => Date.now(), onFire: (sched) => startTask(sched.prompt) });
//   s.add({ prompt, title, when }); s.start(); s.tick();

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { EventEmitter } = require('events');

const DAY_NAMES = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
const TYPES = ['once', 'daily', 'weekly', 'every'];
const MAX_SCHEDULES = 100;
const MAX_PROMPT = 4000;
const TICK_MS = 20 * 1000;

const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };

function parseTime(t) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(t || '').trim());
  if (!m) throw new Error('time must be "HH:MM" (24-hour), e.g. "08:00"');
  const h = +m[1], mi = +m[2];
  if (h > 23 || mi > 59) throw new Error('time must be a real time of day, 00:00 to 23:59');
  return `${String(h).padStart(2, '0')}:${m[2]}`;
}

function parseDay(d) {
  if (typeof d === 'number' && Number.isInteger(d) && d >= 0 && d <= 7) return d % 7;
  const k = String(d || '').trim().toLowerCase().slice(0, 3);
  const i = DAY_NAMES.indexOf(k);
  if (i < 0) throw new Error(`unknown day ${JSON.stringify(d)} (use mon..sun or 0..6, 0 = Sunday)`);
  return i;
}

/** Validate and normalize a `when`; throws an Error whose message says what to fix. */
function normalizeWhen(when, nowMs = Date.now()) {
  if (!when || typeof when !== 'object' || Array.isArray(when)) throw new Error('when must be an object like {"type":"daily","time":"08:00"}');
  const type = String(when.type || when.kind || '').toLowerCase();
  if (!TYPES.includes(type)) throw new Error(`when.type must be one of ${TYPES.join(', ')}`);
  if (type === 'once') {
    const ms = typeof when.at === 'number' ? when.at : Date.parse(String(when.at || ''));
    if (!Number.isFinite(ms)) throw new Error('once needs "at": a local date and time like "2026-11-06T08:00"');
    if (ms < nowMs - 60 * 1000) throw new Error('that time is already in the past');
    return { type, at: new Date(ms).toISOString() };
  }
  if (type === 'daily') return { type, time: parseTime(when.time) };
  if (type === 'weekly') {
    const raw = Array.isArray(when.days) ? when.days : when.days != null ? [when.days] : [];
    if (!raw.length) throw new Error('weekly needs "days", e.g. ["mon","wed"]');
    const days = [...new Set(raw.map(parseDay))].sort((a, b) => a - b);
    return { type, days, time: parseTime(when.time) };
  }
  const hours = Number(when.hours);
  if (!Number.isFinite(hours) || hours < 1 || hours > 168) throw new Error('every needs "hours" between 1 and 168');
  return { type, hours: Math.round(hours * 100) / 100 };
}

/** The next run strictly after `fromMs` (local time), or null when there is none (a past one-off). */
function nextRunAfter(when, fromMs, anchorMs) {
  if (when.type === 'once') { const t = Date.parse(when.at); return t > fromMs ? t : null; }
  if (when.type === 'every') {
    const step = when.hours * 3600 * 1000;
    const a = Number.isFinite(anchorMs) ? anchorMs : fromMs;
    if (a > fromMs) return a;
    return a + (Math.floor((fromMs - a) / step) + 1) * step;
  }
  const [h, m] = when.time.split(':').map(Number);
  const d = new Date(fromMs);
  for (let i = 0; i <= 8; i++) {
    const c = new Date(d.getFullYear(), d.getMonth(), d.getDate() + i, h, m, 0, 0);
    if (c.getTime() <= fromMs) continue;
    if (when.type === 'daily' || when.days.includes(c.getDay())) return c.getTime();
  }
  return null;
}

/**
 * A `when` from an engine suggestion ({when?, cron?}): the structured when if it is valid, else a simple
 * 5-field cron ("M H * * DOW", or every N hours as "0 *\/3 * * *"). Throws when neither can be read.
 */
function whenFrom(sg, nowMs = Date.now()) {
  if (sg && sg.when && typeof sg.when === 'object') { try { return normalizeWhen(sg.when, nowMs); } catch (_) { /* try the cron */ } }
  const f = String((sg && sg.cron) || '').trim().split(/\s+/);
  if (f.length !== 5) throw new Error('the suggestion has no schedule O can read');
  const [mi, h, dom, mon, dow] = f;
  if (dom !== '*' || mon !== '*') throw new Error('monthly schedules are not supported yet');
  const every = /^\*\/(\d+)$/.exec(h);
  if (every && /^\d+$/.test(mi) && dow === '*') return normalizeWhen({ type: 'every', hours: +every[1] }, nowMs);
  if (!/^\d+$/.test(mi) || !/^\d+$/.test(h)) throw new Error('the suggestion has no schedule O can read');
  const time = `${String(+h).padStart(2, '0')}:${String(+mi).padStart(2, '0')}`;
  if (dow === '*') return normalizeWhen({ type: 'daily', time }, nowMs);
  const days = [];
  for (const part of dow.split(',')) {
    const r = /^(\d)(?:-(\d))?$/.exec(part);
    if (!r) throw new Error('the suggestion has no schedule O can read');
    for (let d = +r[1]; d <= +(r[2] || r[1]); d++) days.push(d % 7);
  }
  return normalizeWhen({ type: 'weekly', days, time }, nowMs);
}

/** "Every weekday at 08:00" style label for the panel. */
function describeWhen(when) {
  if (!when) return '';
  if (when.type === 'once') {
    const d = new Date(when.at);
    return 'Once, ' + d.toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  if (when.type === 'daily') return `Every day at ${when.time}`;
  if (when.type === 'every') return `Every ${when.hours} hour${when.hours === 1 ? '' : 's'}`;
  const days = when.days.join(',');
  const label = days === '1,2,3,4,5' ? 'weekday' : days === '0,6' ? 'weekend day' : when.days.map((d) => DAY_NAMES[d][0].toUpperCase() + DAY_NAMES[d].slice(1)).join(', ');
  return `Every ${label} at ${when.time}`;
}

class Scheduler extends EventEmitter {
  /**
   * @param {object} o
   * @param {string} o.file  JSON file to persist to
   * @param {() => number} [o.now]  clock (tests pass a fake one)
   * @param {(sched: object) => any} [o.onFire]  starts the task
   */
  constructor({ file, now, onFire } = {}) {
    super();
    this.file = file || null;
    this.now = now || (() => Date.now());
    this.onFire = onFire || null;
    this.items = [];
    this._timer = null;
    this._load();
  }

  _load() {
    if (!this.file) return;
    try {
      const raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      if (Array.isArray(raw && raw.schedules)) this.items = raw.schedules.filter((s) => s && s.id && s.prompt && s.when);
    } catch (_) { this.items = []; }
  }

  _save() {
    if (!this.file) return;
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      const tmp = this.file + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify({ schedules: this.items }, null, 2));
      fs.renameSync(tmp, this.file);
    } catch (e) { this.emit('error', e); }
  }

  _out(s) { return { ...s, label: describeWhen(s.when) }; }

  list() { return this.items.filter((s) => s.enabled !== false).sort((a, b) => (a.nextRun || 0) - (b.nextRun || 0)).map((s) => this._out(s)); }

  get(id) { const s = this.items.find((x) => x.id === id); return s ? this._out(s) : null; }

  /** Add a schedule. Throws on a bad prompt / when (message says why). */
  add({ prompt, title, when, taskId, goalId } = {}) {
    const p = String(prompt || '').trim();
    if (!p) throw new Error('prompt must be a non-empty string');
    if (p.length > MAX_PROMPT) throw new Error(`prompt is longer than ${MAX_PROMPT} characters`);
    if (this.items.filter((s) => s.enabled !== false).length >= MAX_SCHEDULES) throw new Error(`O keeps at most ${MAX_SCHEDULES} schedules; cancel one first`);
    const now = this.now();
    const w = normalizeWhen(when, now);
    const s = {
      id: 's_' + crypto.randomBytes(5).toString('hex'),
      prompt: p,
      title: clip(title || p, 60),
      when: w,
      createdAt: now,
      createdBy: taskId || null,
      goalId: goalId ? String(goalId) : null, // a goal's cycle schedule: main builds the prompt when it fires
      lastRun: null,
      runs: 0,
      enabled: true,
      nextRun: w.type === 'every' ? now + w.hours * 3600 * 1000 : nextRunAfter(w, now - 1),
    };
    if (s.nextRun == null) throw new Error('that schedule never runs');
    this.items.push(s);
    this._save();
    this.emit('changed');
    return this._out(s);
  }

  /** The schedule that drives one goal's cycles, or null. */
  forGoal(goalId) { const s = this.items.find((x) => x.goalId && x.goalId === goalId && x.enabled !== false); return s ? this._out(s) : null; }

  cancel(id) {
    const i = this.items.findIndex((s) => s.id === id);
    if (i < 0) return false;
    this.items.splice(i, 1);
    this._save();
    this.emit('changed');
    return true;
  }

  /** Fire every due schedule once (a run missed while O was off fires once, then the schedule moves on). */
  tick() {
    const now = this.now();
    const fired = [];
    let changed = false;
    for (const s of this.items.slice()) {
      if (s.enabled === false || !s.nextRun || s.nextRun > now) continue;
      s.lastRun = now;
      s.runs = (s.runs || 0) + 1;
      fired.push(this._out(s));
      if (s.when.type === 'once') { s.enabled = false; s.nextRun = null; } else s.nextRun = nextRunAfter(s.when, now, s.when.type === 'every' ? s.nextRun : undefined);
      changed = true;
    }
    this.items = this.items.filter((s) => s.enabled !== false);
    if (changed) { this._save(); this.emit('changed'); }
    for (const s of fired) {
      this.emit('fire', s);
      if (this.onFire) { try { this.onFire(s); } catch (e) { this.emit('error', e); } }
    }
    return fired;
  }

  start(intervalMs = TICK_MS) {
    this.stop();
    this._timer = setInterval(() => this.tick(), intervalMs);
    if (this._timer.unref) this._timer.unref();
    this.tick();
  }

  stop() { if (this._timer) clearInterval(this._timer); this._timer = null; }

  /** The bridge's schedule tools (POST /schedule on the agent's server): {op, prompt, when, title, id}. */
  handle(body) {
    const op = String((body && body.op) || '');
    try {
      // goalId is main's own field: a schedule added from a task is always a plain one.
      if (op === 'add') return { ok: true, schedule: this.add({ prompt: body.prompt, title: body.title, when: body.when, taskId: body.taskId }) };
      if (op === 'list') return { ok: true, schedules: this.list() };
      if (op === 'cancel') {
        const id = String(body.id || '');
        const s = this.get(id);
        if (s && s.goalId) return { ok: false, error: 'that schedule drives a goal; the person pauses or archives the goal in O instead' };
        return this.cancel(id) ? { ok: true, cancelled: s } : { ok: false, error: `no schedule with id ${JSON.stringify(id)}` };
      }
      return { ok: false, error: 'op must be add, list or cancel' };
    } catch (e) { return { ok: false, error: e.message }; }
  }
}

// ------------------------------------------------------------------ self-evolution clock (plan L)
// O evolves continuously, not in a weekly batch: the clock pokes the engine (POST /evolve/tick) every few
// minutes, more often while the person is away (idle time gets more build slots in the engine), and only when
// evolveEnabled. Scans are incremental and cached engine-side, so a tick is cheap. After each tick it reads
// /evolve/status and emits 'status' so main can drive the updater (installs, rollbacks) and the panel.

const EVOLVE_ACTIVE_MS = 30 * 60 * 1000;
const EVOLVE_AWAY_MS = 8 * 60 * 1000;
const EVOLVE_FIRST_MS = 2 * 60 * 1000;
const EVOLVE_POLL_MS = 60 * 1000;
const AUTO_SHIP = ['never', 'deps_only', 'low_risk'];

class EvolveClock extends EventEmitter {
  /**
   * @param {object} o
   * @param {() => object} o.getSettings
   * @param {() => boolean} o.isAway          ambient presence (true while the person is away / idle)
   * @param {(method:string, path:string, body?:object, timeoutMs?:number) => Promise<any>} o.call  engine call
   * @param {() => number} [o.now]
   */
  constructor({ getSettings, isAway, call, now, activeMs = EVOLVE_ACTIVE_MS, awayMs = EVOLVE_AWAY_MS, firstMs = EVOLVE_FIRST_MS } = {}) {
    super();
    this.getSettings = getSettings || (() => ({}));
    this.isAway = isAway || (() => false);
    this.call = call;
    this.now = now || (() => Date.now());
    this.activeMs = activeMs;
    this.awayMs = awayMs;
    this.lastTick = this.now() - activeMs + firstMs; // first tick shortly after start, not during startup
    this.wasAway = false;
    this.busy = false;
    this.last = null;
    this._timer = null;
  }

  enabled() { return this.getSettings().evolveEnabled !== false; }

  /** Due when enabled and the interval for the current presence has passed (or presence just turned away). */
  due() {
    if (!this.enabled() || this.busy) return false;
    const away = !!this.isAway();
    const justLeft = away && !this.wasAway;
    this.wasAway = away;
    return justLeft || this.now() - this.lastTick >= (away ? this.awayMs : this.activeMs);
  }

  async tick({ force = false } = {}) {
    if (!this.call || this.busy || (!force && !this.enabled())) return null;
    this.busy = true;
    this.lastTick = this.now();
    try {
      await this.call('POST', '/evolve/tick', { presence: this.isAway() ? 'away' : 'active', force_scan: !!force }, 15000);
      return await this.refresh();
    } catch (err) {
      this.emit('error', err);
      return null;
    } finally {
      this.busy = false;
    }
  }

  async refresh() {
    try {
      const st = await this.call('GET', '/evolve/status', null, 10000);
      this.last = st;
      this.emit('status', st);
      return st;
    } catch (err) { this.emit('error', err); return null; }
  }

  check() { if (this.due()) return this.tick(); return null; }

  start(intervalMs = EVOLVE_POLL_MS) {
    this.stop();
    this._timer = setInterval(() => { this.check(); }, intervalMs);
    if (this._timer.unref) this._timer.unref();
  }

  stop() { if (this._timer) clearInterval(this._timer); this._timer = null; }
}

/** The engine's O_EVOLVE_* env for these settings (main/engine.js spreads it into the child env). */
function evolveEnv(s = {}, userDataDir = '') {
  const num = (v, d, lo, hi) => { const n = Number(v); return String(Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : d); };
  return {
    O_EVOLVE_ENABLED: s.evolveEnabled === false ? '0' : '1',
    O_EVOLVE_BUDGET_USD: num(s.evolveBudgetUsd, 10, 0, 1000),
    O_EVOLVE_PER_PROPOSAL_USD: num(s.evolvePerProposalUsd, 1.5, 0, 100),
    O_EVOLVE_AUTO_SHIP: AUTO_SHIP.includes(s.evolveAutoShip) ? s.evolveAutoShip : 'low_risk',
    O_EVOLVE_REPO: String(s.evolveRepo || s.updateRepo || 'mikhailTL/O'),
    O_EVOLVE_BASE: String(s.updateBranch || 'main'),
    O_EVOLVE_GITHUB_TOKEN: String(s.evolveGithubToken || ''),
    O_EVOLVE_SCAN_MINUTES: num(s.evolveScanMinutes, 45, 5, 1440),
    O_EVOLVE_SLOTS_ACTIVE: num(s.evolveSlotsActive, 1, 0, 8),
    O_EVOLVE_SLOTS_AWAY: num(s.evolveSlotsAway, 3, 0, 8),
    O_EVOLVE_APP_LOG: userDataDir ? path.join(userDataDir, 'o-actions.jsonl') : '',
  };
}

module.exports = { Scheduler, normalizeWhen, whenFrom, nextRunAfter, describeWhen, TYPES, DAY_NAMES, EvolveClock, evolveEnv };
