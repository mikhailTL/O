'use strict';
// Ambient sensing in the app (plan J): the person's presence drives the task pool, and a short brief greets
// them when they come back. The engine owns the sensors, the presence state machine, the milestone table
// and the brief (GET /presence, GET /sensors, POST /milestones, GET /brief/latest); this file only reads
// derived state from it. No raw sensor data ever reaches the app: presence is a word, sensors are names.
//
//   const amb = new AmbientController({ engineCall, tasks, getSettings, log, showBrief, speak });
//   amb.on('presence', (status) => ...)  // USER_PRESENT | USER_ABSENT | UNKNOWN changed
//   amb.on('sensors', (status) => ...)   // the set of live sensors changed (tray dot, header glyph)
//   amb.start();                          // polls /presence every second while the master switch is on
//   (task ends are picked up from tasks.on('end'): milestones while away)
//   amb.onCheckpoint(card);

const { EventEmitter } = require('events');
const { spawn: spawnDefault } = require('child_process');

const STATES = ['USER_PRESENT', 'USER_ABSENT', 'UNKNOWN'];
const POLL_MS = 1000;
const SENSORS_EVERY = 5;           // /sensors every 5th presence poll
const MISSING_ROUTE_MS = 10000;    // an engine without /presence is asked again every 10 s
const BRIEF_TRIES = 8;
const BRIEF_RETRY_MS = 750;
const BRIEF_SLACK_S = 5;           // a brief whose `since` is older than the away start by more than this is stale

// One entry per sensor: the settings key, the plain one-line explanation, what it reads (the confirmation
// sheet), and how an engine /sensors name maps onto it.
const SENSORS = Object.freeze([
  {
    key: 'sensorWifi', id: 'wifi', label: 'Wi-Fi presence', match: /wi-?fi|csi/i,
    line: 'Notices movement in the room from small changes in your Wi-Fi signal. Room-level only; it never tells people apart.',
    reads: 'Channel state (signal phase and strength) from a CSI-capable Wi-Fi card or an ESP32 node on your network.',
  },
  {
    key: 'sensorSonar', id: 'sonar', label: 'Sonar', match: /sonar|chirp|fmcw|acoustic/i,
    line: 'Plays a near-inaudible 18–22 kHz chirp and listens for its echo to tell whether someone is at the desk.',
    reads: 'Your speakers and microphones, filtered to the chirp band. Speech and other sound are dropped on the spot.',
  },
  {
    key: 'sensorCamera', id: 'camera', label: 'Camera presence', match: /camera|face|webcam|mesh/i,
    line: 'Checks whether a face is in front of the screen. Nobody is recognised.',
    reads: 'Your webcam, one frame at a time, turned into a yes or no and thrown away.',
  },
  {
    key: 'sensorHeartRate', id: 'heart_rate', label: 'Heart rate', match: /heart|rppg|pulse|bpm/i,
    line: 'Estimates your pulse from tiny colour changes in your face on the webcam. Shown live only.',
    reads: 'Your webcam, the skin area of your face only. The number is never saved, logged, or given to any model.',
  },
]);
const SENSOR_KEYS = SENSORS.map((s) => s.key);
const PRIVACY_LINE = 'Everything runs on this computer. No audio, video or Wi-Fi data is stored or leaves it; only "here" or "away" is passed on.';

const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const firstLine = (s) => String(s == null ? '' : s).split(/\r?\n/).map((l) => l.trim()).find(Boolean) || '';
const toSec = (t) => { const n = Number(t); if (!Number.isFinite(n) || n <= 0) return 0; return n > 1e12 ? n / 1000 : n; };

/** The catalog entry for an engine sensor name, or null. */
function sensorFor(name) {
  const n = String(name || '');
  return SENSORS.find((s) => s.id === n || s.key === n) || SENSORS.find((s) => s.match.test(n)) || null;
}

/** Normalize a GET /presence answer. */
function normPresence(p) {
  const state = p && STATES.includes(p.state) ? p.state : 'UNKNOWN';
  const sources = Array.isArray(p && p.sources) ? p.sources.map((x) => clip(typeof x === 'string' ? x : (x && x.name) || '', 40)).filter(Boolean).slice(0, 8) : [];
  return { state, since: toSec(p && p.since), sources };
}

/** Normalize a GET /sensors answer onto the catalog (unknown names are kept, labelled by their own name). */
function normSensors(list) {
  const out = [];
  for (const s of Array.isArray(list) ? list : []) {
    if (!s || typeof s !== 'object') continue;
    const cat = sensorFor(s.name);
    out.push({
      name: clip(s.name, 40), id: cat ? cat.id : clip(s.name, 40), key: cat ? cat.key : null, label: cat ? cat.label : clip(s.name, 40),
      available: s.available === true, enabled: s.enabled === true, reason: clip(s.reason, 160),
    });
  }
  return out;
}

/**
 * Consent guard for settings writes: a sensor can only go from off to on through the confirmation sheet
 * (`consented` names the one key that sheet approved). Every other off -> on change is dropped.
 */
function consentFilter(before, partial, consented) {
  if (!partial || typeof partial !== 'object') return {};
  const out = { ...partial };
  for (const k of SENSOR_KEYS) {
    if (out[k] === true && !(before && before[k] === true) && k !== consented) delete out[k];
  }
  return out;
}

/** Live sensors: the master switch on, and the engine says the sensor is enabled and available. */
function liveSensors(settings, sensors) {
  if (!settings || settings.ambientEnabled !== true) return [];
  return (sensors || []).filter((s) => s.enabled && s.available && (!s.key || settings[s.key] === true));
}

/** A milestone body for POST /milestones: title and one line of detail, never a transcript. */
function milestoneFor({ task, ok, result }) {
  if (!task || task.status === 'stopped') return null;
  const kind = ok ? (task.error ? 'error_bypassed' : 'completed') : 'failed';
  const detail = ok
    ? clip(firstLine(result || task.result) || 'Finished.', 140)
    : clip(firstLine(task.error || task.statusLine) || 'It hit a snag.', 140);
  return { task_id: String(task.id || ''), kind, title: clip(task.title || 'Task', 80), detail: kind === 'error_bypassed' ? clip(`Worked around: ${firstLine(task.error)}. ${detail}`, 140) : detail };
}

/** Split a brief into its sentences (at most `n`). */
function sentencesOf(text, n = 3) {
  const parts = String(text || '').replace(/\s+/g, ' ').trim().split(/(?<=[.!?])\s+(?=[A-Z0-9"'“‘(])/).map((x) => x.trim()).filter(Boolean);
  return parts.slice(0, n);
}

/** Read text aloud with the OS voice: `say` (macOS), System.Speech (Windows), spd-say or espeak (Linux). */
function osSpeak(text, { spawn = spawnDefault, platform = process.platform } = {}) {
  text = clip(text, 1200);
  if (!text) return false;
  const tries = platform === 'darwin' ? [['say', [text]]]
    : platform === 'win32' ? [['powershell', ['-NoProfile', '-NonInteractive', '-Command', 'Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak($env:O_TTS_TEXT)']]]
      : [['spd-say', ['--wait', text]], ['espeak', [text]]];
  const run = (i) => {
    if (i >= tries.length) return;
    let child;
    try { child = spawn(tries[i][0], tries[i][1], { stdio: 'ignore', windowsHide: true, env: { ...process.env, O_TTS_TEXT: text } }); } catch (_) { run(i + 1); return; }
    child.on('error', () => run(i + 1));
  };
  run(0);
  return true;
}

/**
 * Draw the "sensing" dot into the bottom-right corner of a raw 32-bit bitmap (BGRA or RGBA: the dot is white
 * with a black ring, so channel order does not matter). Returns a new buffer.
 */
function dotBitmap(buf, width, height) {
  const out = Buffer.from(buf);
  const r = Math.max(2, Math.round(Math.min(width, height) * 0.2));
  const cx = width - r - 1, cy = height - r - 1;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const d = Math.hypot(x + 0.5 - cx, y + 0.5 - cy);
      if (d > r + 1.2) continue;
      const i = (y * width + x) * 4;
      const v = d <= r ? 255 : 0;
      out[i] = v; out[i + 1] = v; out[i + 2] = v; out[i + 3] = 255;
    }
  }
  return out;
}

class AmbientController extends EventEmitter {
  /**
   * @param {object} o
   * @param {(method:string, path:string, body?:object, timeoutMs?:number) => Promise<any>} o.engineCall  rejects while the engine is down
   * @param {import('./tasks').TaskManager} [o.tasks]
   * @param {() => object} o.getSettings
   * @param {(kind:string, summary:string, detail?:object) => void} [o.log]
   * @param {(brief:{text:string, sentences:string[], milestones:number, since:number}) => void} [o.showBrief]
   * @param {(text:string) => void} [o.speak]
   * @param {() => number} [o.now]
   * @param {number} [o.pollMs]
   */
  constructor({ engineCall, tasks, getSettings, log, showBrief, speak, now, pollMs } = {}) {
    super();
    this.engineCall = engineCall || (() => Promise.reject(new Error('no engine')));
    this.tasks = tasks || null;
    this.getSettings = getSettings || (() => ({}));
    this.log = log || (() => {});
    this.showBrief = showBrief || (() => {});
    this.speak = speak || ((t) => osSpeak(t));
    this.now = now || (() => Date.now());
    this.pollMs = pollMs || POLL_MS;
    this.state = 'UNKNOWN';    // what the engine last said
    this.known = 'UNKNOWN';    // the last PRESENT/ABSENT (an UNKNOWN in between does not count as a return)
    this.since = 0;
    this.sources = [];
    this.awaySince = 0;        // engine `since` of the current/last away period (seconds)
    this.sensors = [];
    this.live = [];
    this.reachable = false;
    this.lastBrief = null;
    this.milestonesPosted = 0;
    this._timer = null;
    this._ticks = 0;
    this._busy = false;
    this._stopped = true;
    this._welcome = null;
    // Every task end goes through here (a milestone only while away).
    if (this.tasks && typeof this.tasks.on === 'function') this.tasks.on('end', (ev) => { this.onTaskEnd(ev).catch(() => {}); });
  }

  enabled() { return (this.getSettings() || {}).ambientEnabled === true; }
  isAway() { return this.enabled() && this.known === 'USER_ABSENT'; }

  status() {
    const running = this.tasks ? this.tasks.running().length : 0;
    const s = this.getSettings() || {};
    return {
      enabled: this.enabled(), state: this.enabled() ? this.state : 'UNKNOWN', away: this.isAway(), since: this.since, sources: this.sources.slice(),
      reachable: this.reachable, running,
      sensors: this.sensors.map((x) => ({ ...x })), live: this.live.map((x) => x.label),
      catalog: SENSORS.map((c) => {
        const eng = this.sensors.find((x) => x.key === c.key);
        return { key: c.key, id: c.id, label: c.label, line: c.line, reads: c.reads, on: s[c.key] === true, available: eng ? eng.available : null, reason: eng ? eng.reason : '' };
      }),
      privacy: PRIVACY_LINE,
    };
  }

  start() {
    if (!this._stopped) return;
    this._stopped = false;
    this._schedule(0);
  }

  stop() {
    this._stopped = true;
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
  }

  _schedule(ms) {
    if (this._stopped) return;
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => { this._timer = null; this.tick().finally(() => this._schedule(this._next || this.pollMs)); }, ms);
    if (this._timer.unref) this._timer.unref();
  }

  /** Settings changed: re-read the sensors now; turning the master switch off releases everything held. */
  onSettings() {
    if (!this.enabled()) this._disable();
    this.refreshSensors().catch(() => {});
    if (!this._stopped) this._schedule(0);
  }

  _disable() {
    const wasAway = this.known === 'USER_ABSENT';
    this.state = 'UNKNOWN'; this.known = 'UNKNOWN'; this.sources = [];
    if (this.tasks) { this.tasks.setPresence('unknown'); this.tasks.resumePaused(); }
    if (wasAway) this.emit('presence', this.status());
    this._setLive([]);
  }

  /** One poll: GET /presence (and /sensors every few polls). Never throws. */
  async tick() {
    this._next = this.pollMs;
    if (!this.enabled()) {
      if (this.known !== 'UNKNOWN' || this.state !== 'UNKNOWN' || this.live.length) this._disable();
      this._next = this.pollMs * 2;
      return;
    }
    if (this._busy) return;
    this._busy = true;
    try {
      if (this._ticks++ % SENSORS_EVERY === 0) await this.refreshSensors().catch(() => {});
      let p;
      try { p = await this.engineCall('GET', '/presence', null, 2000); this.reachable = true; } catch (err) {
        this.reachable = false;
        if (err && err.status === 404) this._next = MISSING_ROUTE_MS;
        return;
      }
      await this.apply(normPresence(p));
    } finally { this._busy = false; }
  }

  async refreshSensors() {
    let list;
    try { list = normSensors(await this.engineCall('GET', '/sensors', null, 3000)); } catch (_) { list = []; }
    const before = JSON.stringify(this.sensors);
    this.sensors = list;
    if (JSON.stringify(list) !== before) this.emit('sensors', this.status());
    this._setLive(liveSensors(this.getSettings(), list));
    return list;
  }

  _setLive(list) {
    const names = (l) => l.map((x) => x.label).join('|');
    const changed = names(list) !== names(this.live);
    this.live = list;
    if (changed) {
      this.log('policy', list.length ? `Sensing live: ${list.map((x) => x.label).join(', ')}` : 'Sensing off', { sensors: list.map((x) => x.id) });
      this.emit('sensors', this.status());
    }
  }

  /** Apply one presence reading (the poll, or a test). */
  async apply({ state, since, sources }) {
    const prevKnown = this.known;
    const changed = state !== this.state;
    this.state = state;
    this.since = since || this.since;
    this.sources = sources || [];
    if (!changed) return;
    if (state === 'UNKNOWN') { this.emit('presence', this.status()); return; }
    this.known = state;
    if (state === 'USER_ABSENT') {
      this.awaySince = since || this.now() / 1000;
      const r = this.tasks ? this.tasks.setPresence('absent') : { resumed: [] };
      this.log('note', `Away: background work continues${r.resumed && r.resumed.length ? ` (${r.resumed.length} paused task(s) resumed)` : ''}`, { sources: this.sources, resumed: r.resumed || [] });
      this.emit('presence', this.status());
      return;
    }
    // USER_PRESENT
    const r = this.tasks ? this.tasks.setPresence('present') : {};
    this.emit('presence', this.status());
    if (prevKnown !== 'USER_ABSENT') return;
    const p = r.paused || { paused: [], pending: [], held: [] };
    this.log('note', `Back: ${p.paused.length + p.pending.length} low-priority task(s) pausing, ${p.held.length} held`, { sources: this.sources, ...p });
    this._welcome = this.welcomeBack(); // not awaited: presence polling keeps going while the brief is fetched
  }

  /** Fetch the brief for this away period (retrying while the engine writes it) and show it. */
  async welcomeBack() {
    const since = this.awaySince;
    let brief = null;
    for (let i = 0; i < BRIEF_TRIES; i++) {
      try {
        const b = await this.engineCall('GET', '/brief/latest', null, 8000);
        if (b && typeof b.text === 'string' && b.text.trim() && toSec(b.since) >= since - BRIEF_SLACK_S) { brief = b; break; }
      } catch (_) { /* not ready yet */ }
      if (this.known !== 'USER_PRESENT' || !this.enabled()) return null;
      await new Promise((r) => setTimeout(r, BRIEF_RETRY_MS));
    }
    if (!brief) { this.log('note', 'Back: no brief for this time away', { since }); return null; }
    const out = { text: clip(brief.text, 900), sentences: sentencesOf(brief.text, 3), milestones: Number(brief.milestones) || 0, since: toSec(brief.since) };
    this.lastBrief = out;
    this.log('note', `Brief shown (${out.milestones} milestone(s) while away)`, { milestones: out.milestones });
    try { this.showBrief(out); } catch (_) {}
    if ((this.getSettings() || {}).briefSpeak === true) { try { this.speak(out.text); } catch (_) {} }
    this.emit('brief', out);
    return out;
  }

  async _post(body) {
    if (!body) return false;
    for (let i = 0; i < 2; i++) {
      try { await this.engineCall('POST', '/milestones', body, 5000); this.milestonesPosted++; return true; } catch (_) { /* once more */ }
    }
    return false;
  }

  /** A task ended: while away, that is a milestone (completed, error_bypassed or failed). */
  onTaskEnd(ev) {
    if (!this.isAway()) return Promise.resolve(false);
    return this._post(milestoneFor(ev || {}));
  }

  /** A goal filed a checkpoint card: while away, that is a milestone too. */
  onCheckpoint(card) {
    if (!this.isAway() || !card) return Promise.resolve(false);
    return this._post({ task_id: String(card.taskId || ''), kind: 'checkpoint', title: clip(card.headline || card.kindLabel || 'Checkpoint', 80), detail: clip(firstLine(card.summary) || 'Waiting for your approval.', 140) });
  }
}

module.exports = {
  AmbientController, SENSORS, SENSOR_KEYS, PRIVACY_LINE, consentFilter, liveSensors, milestoneFor, normPresence, normSensors,
  sensorFor, sentencesOf, osSpeak, dotBitmap,
};
