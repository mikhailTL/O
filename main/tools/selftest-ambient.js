#!/usr/bin/env node
'use strict';
// Self-test for ambient sensing in the app (plan J): presence drives the task pool, milestones are posted
// while away, the brief overlay appears on return, and sensors stay off until confirmed.
// The engine is a stub HTTP server with the sibling's routes (GET /presence, GET /sensors, GET/POST
// /milestones, GET /brief/latest, POST /presence/simulate). Electron is not needed: the brief window is
// built by main/brief.js against a recording fake BrowserWindow / screen.
// Run: node main/tools/selftest-ambient.js   (exit 1 on any failure)

const http = require('http');
const os = require('os');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { EventEmitter } = require('events');
const { TaskManager, RESUME_PROMPT, defaultPriority } = require('../tasks');
const { Settings, DEFAULTS } = require('../settings');
const { EngineProcess } = require('../engine');
const amb = require('../ambient');
const { createBriefOverlay, briefBounds } = require('../brief');

let failures = 0;
let passed = 0;
function ok(cond, label) {
  if (cond) { passed++; console.log('ok   ' + label); } else { failures++; console.log('FAIL ' + label); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 6000, step = 20) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(step); }
  return !!(await fn());
}
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'o-ambient-'));

// ------------------------------------------------------------ the stub engine
const TOKEN = crypto.randomBytes(12).toString('hex');
const SENTENCES = [
  'While you were away I finished the nightly build and filed two research notes.',
  'One test run failed on a flaky network check, so I retried it and it passed.',
  'A goal checkpoint is waiting for your approval in the panel.',
];
const eng = {
  presence: { state: 'USER_PRESENT', since: Date.now() / 1000, sources: ['sonar'] },
  sensors: [
    { name: 'wifi_csi', available: false, enabled: false, reason: 'no CSI-capable Wi-Fi card found' },
    { name: 'sonar', available: true, enabled: false, reason: '' },
    { name: 'camera_presence', available: true, enabled: false, reason: '' },
    { name: 'heart_rate', available: true, enabled: false, reason: '' },
  ],
  milestones: [],
  hits: { presence: 0, sensors: 0, brief: 0 },
  missingPresence: false,
  briefSince: null, // null = the current away period
};
const server = http.createServer((req, res) => {
  let raw = '';
  req.on('data', (c) => { raw += c; });
  req.on('end', () => {
    const send = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)); };
    if (req.headers['x-o-token'] !== TOKEN) return send(401, { error: 'token' });
    const body = raw ? JSON.parse(raw) : null;
    const u = req.url.split('?')[0];
    if (req.method === 'GET' && u === '/presence') { eng.hits.presence++; return eng.missingPresence ? send(404, { detail: 'Not Found' }) : send(200, eng.presence); }
    if (req.method === 'POST' && u === '/presence/simulate') { eng.presence = { state: body.state, since: Date.now() / 1000, sources: ['simulated'] }; return send(200, { ok: true, ...eng.presence }); }
    if (req.method === 'GET' && u === '/sensors') { eng.hits.sensors++; return send(200, eng.sensors); }
    if (req.method === 'GET' && u === '/milestones') return send(200, eng.milestones);
    if (req.method === 'POST' && u === '/milestones') { eng.milestones.push({ ...body, ts: Date.now() / 1000 }); return send(200, { ok: true, id: eng.milestones.length }); }
    if (req.method === 'GET' && u === '/brief/latest') {
      eng.hits.brief++;
      const since = eng.briefSince != null ? eng.briefSince : eng.lastAwaySince;
      return send(200, { text: SENTENCES.join(' '), sentences: 3, since, milestones: eng.milestones.length });
    }
    return send(404, { detail: 'Not Found' });
  });
});
function engineCall(method, p, body, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const data = body == null ? null : JSON.stringify(body);
    const req = http.request({ hostname: '127.0.0.1', port: server.address().port, path: p, method, timeout: timeoutMs,
      headers: { 'x-o-token': TOKEN, ...(data ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) } : {}) } }, (res) => {
      let raw = '';
      res.on('data', (c) => { raw += c; });
      res.on('end', () => {
        let j = null; try { j = raw ? JSON.parse(raw) : null; } catch (_) { j = raw; }
        if (res.statusCode >= 200 && res.statusCode < 300) return resolve(j);
        const e = new Error(`${method} ${p} -> ${res.statusCode}`); e.status = res.statusCode; reject(e);
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
    if (data) req.write(data);
    req.end();
  });
}
const simulate = async (state) => {
  const r = await engineCall('POST', '/presence/simulate', { state });
  if (state === 'USER_ABSENT') eng.lastAwaySince = r.since;
  return r;
};

// ------------------------------------------------------------ a scripted agent (what the task pool drives)
// Each run is a loop of tool calls: emit 'tool', wait, emit the toolDone status, repeat; `steps` sets how many.
// cancel() behaves like OAgent.cancel(): status + done(stopped) synchronously, then run() resolves.
const runs = []; // { taskId, prompt, sessionIn, agent }
class ScriptedAgent extends EventEmitter {
  constructor(taskId, plan) {
    super();
    this.taskId = taskId; this.plan = plan; this.sessionId = null; this.cancelled = 0; this.toolMs = plan.toolMs || 40;
  }
  _emit(ev) { this.emit('event', { ...ev, taskId: this.taskId }); }
  run(prompt) {
    runs.push({ taskId: this.taskId, prompt, sessionIn: this.sessionId, agent: this });
    this.sessionId = this.sessionId || 'sess-' + this.taskId;
    return new Promise((resolve) => {
      this._finish = resolve;
      let step = 0;
      const next = () => {
        if (this._done) return;
        if (step >= this.plan.steps) {
          this._done = true;
          if (this.plan.fail) { this._emit({ type: 'error', message: 'npm ERR! network timeout while fetching left-pad' }); this._emit({ type: 'done', result: '' }); resolve({ ok: false }); return; }
          if (this.plan.bypass) this._emit({ type: 'error', message: 'flaky test retried' });
          const result = this.plan.result || `Finished ${this.taskId}.\nLong transcript line 2 that must not be posted.`;
          this._emit({ type: 'done', result }); resolve({ ok: true, result }); return;
        }
        step++;
        this.inTool = true;
        this._emit({ type: 'tool', name: 'Bash', summary: `step ${step}` });
        this._t = setTimeout(() => {
          this.inTool = false;
          this._emit({ type: 'status', mood: 'thinking', text: 'Thinking', toolDone: true });
          this._t = setTimeout(next, this.plan.gapMs || 30);
        }, this.toolMs);
      };
      next();
    });
  }
  cancel() {
    if (this._done) return;
    this._done = true; this.cancelled++;
    clearTimeout(this._t);
    this._emit({ type: 'status', mood: 'idle', text: 'Stopped' });
    this._emit({ type: 'done', result: '(stopped)', stopped: true });
    if (this._finish) this._finish({ ok: false, cancelled: true });
  }
  dispose() {}
}

// ------------------------------------------------------------ a recording fake Electron window / screen
const windows = [];
class FakeWindow extends EventEmitter {
  constructor(opts) {
    super();
    this.opts = opts; this.bounds = { x: opts.x, y: opts.y, width: opts.width, height: opts.height };
    this.calls = []; this.visible = false; this.focused = false; this.destroyed = false;
    const self = this;
    this.webContents = Object.assign(new EventEmitter(), { sent: [], send(ch, payload) { this.sent.push({ ch, payload }); self.calls.push(['send', ch]); } });
    windows.push(this);
  }
  setAlwaysOnTop(...a) { this.calls.push(['setAlwaysOnTop', ...a]); }
  setVisibleOnAllWorkspaces(...a) { this.calls.push(['setVisibleOnAllWorkspaces', ...a]); }
  setIgnoreMouseEvents(...a) { this.calls.push(['setIgnoreMouseEvents', ...a]); this.ignore = a; }
  setFocusable(v) { this.calls.push(['setFocusable', v]); this.focusable = v; }
  loadFile(f) { this.calls.push(['loadFile', f]); this.file = f; setTimeout(() => this.webContents.emit('did-finish-load'), 10); }
  setBounds(b) { this.bounds = b; this.calls.push(['setBounds', b]); }
  getBounds() { return this.bounds; }
  isVisible() { return this.visible; }
  isDestroyed() { return this.destroyed; }
  showInactive() { this.visible = true; this.calls.push(['showInactive']); }
  show() { this.visible = true; this.focused = true; this.calls.push(['show']); }
  focus() { this.focused = true; this.calls.push(['focus']); }
  hide() { this.visible = false; this.calls.push(['hide']); }
  destroy() { this.destroyed = true; this.emit('closed'); }
}
const fakeScreen = {
  getCursorScreenPoint: () => ({ x: 2500, y: 300 }),
  getDisplayNearestPoint: () => ({ workArea: { x: 1920, y: 25, width: 2560, height: 1415 } }),
  getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }),
};

(async () => {
  await new Promise((r) => server.listen(0, '127.0.0.1', r));

  // ------------------------------------------------------------ settings: everything off by default
  {
    const keys = ['ambientEnabled', 'sensorWifi', 'sensorSonar', 'sensorCamera', 'sensorHeartRate', 'briefSpeak'];
    ok(keys.every((k) => DEFAULTS[k] === false), 'ambient master switch, every sensor and "read aloud" default to off');
    ok(DEFAULTS.absentAfterMin === 5, 'away-after defaults to 5 minutes');
    const s = new Settings(path.join(tmp, 'set'));
    ok(keys.every((k) => s.get()[k] === false), 'a fresh settings file has them all off');
    ok(s.set({ absentAfterMin: '500' }).absentAfterMin === 120 && s.set({ absentAfterMin: 0 }).absentAfterMin === 1 && s.set({ sensorSonar: 'yes' }).sensorSonar === false, 'absentAfterMin is clamped to 1..120 and sensor flags must be booleans');
    const ep = new EngineProcess({ userDataDir: path.join(tmp, 'eng'), getSettings: () => ({ ...DEFAULTS }) });
    const off = ep._env();
    ok(off.O_AMBIENT === '0' && off.O_SENSOR_WIFI === '0' && off.O_SENSOR_SONAR === '0' && off.O_SENSOR_CAMERA === '0' && off.O_SENSOR_HEART_RATE === '0' && off.O_BRIEF_TTS === '0' && off.O_ABSENT_AFTER_S === '300',
      'engine env: O_AMBIENT=0, every O_SENSOR_*=0, O_BRIEF_TTS=0, O_ABSENT_AFTER_S=300 by default');
    const ep2 = new EngineProcess({ userDataDir: path.join(tmp, 'eng'), getSettings: () => ({ ...DEFAULTS, ambientEnabled: true, sensorSonar: true, sensorCamera: true, absentAfterMin: 2, briefSpeak: true }) });
    const on = ep2._env();
    ok(on.O_AMBIENT === '1' && on.O_SENSOR_SONAR === '1' && on.O_SENSOR_CAMERA === '1' && on.O_SENSOR_WIFI === '0' && on.O_SENSOR_HEART_RATE === '0' && on.O_ABSENT_AFTER_S === '120' && on.O_BRIEF_TTS === '1',
      'engine env follows the settings (only the confirmed sensors are 1)');
    const ep3 = new EngineProcess({ userDataDir: path.join(tmp, 'eng'), getSettings: () => ({ ...DEFAULTS, ambientEnabled: false, sensorSonar: true }) });
    ok(ep3._env().O_SENSOR_SONAR === '0', 'a sensor toggle without the master switch stays 0 in the engine env');
    const mainSrc = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
    ok(/const AMBIENT_KEYS = \['ambientEnabled', 'sensorWifi', 'sensorSonar', 'sensorCamera', 'sensorHeartRate', 'absentAfterMin', 'briefSpeak'\]/.test(mainSrc) && /\.\.\.AMBIENT_KEYS\]/.test(mainSrc), 'the ambient keys are ENGINE_KEYS in main.js (a change restarts the engine)');
  }

  // ------------------------------------------------------------ consent: enabling a sensor requires the confirmation
  {
    const before = { ...DEFAULTS };
    ok(!('sensorCamera' in amb.consentFilter(before, { sensorCamera: true, theme: 'dark' }, null)) && amb.consentFilter(before, { sensorCamera: true, theme: 'dark' }, null).theme === 'dark',
      'a plain settings write cannot turn a sensor on (dropped; other keys pass)');
    ok(amb.consentFilter(before, { sensorCamera: true }, 'sensorCamera').sensorCamera === true, 'the confirmed sensor (and only that one) turns on');
    ok(!('sensorSonar' in amb.consentFilter(before, { sensorCamera: true, sensorSonar: true }, 'sensorCamera')), 'one confirmation does not turn on a second sensor');
    ok(amb.consentFilter({ ...before, sensorSonar: true }, { sensorSonar: false }, null).sensorSonar === false, 'turning a sensor off needs no confirmation');
    ok(amb.consentFilter({ ...before, sensorSonar: true }, { sensorSonar: true }, null).sensorSonar === true, 'Save with an already-confirmed sensor keeps it on');
    const mainSrc = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
    ok(/ambientMod\.consentFilter\(before, partial, consented\)/.test(mainSrc) && /'settings:set', \(_e, partial\) => applySettings\(partial, null\)/.test(mainSrc) && /if \(confirmed !== true\) return \{ ok: false/.test(mainSrc),
      'main routes every settings write through the consent filter; only ambient:enableSensor(key, true) consents');
    const panel = fs.readFileSync(path.join(__dirname, '..', '..', 'renderer', 'panel.js'), 'utf8');
    ok(/if \(el\.checked\) \{ el\.checked = false; openConfirm\(key\); return; \}/.test(panel) && /o\.ambientEnableSensor\(key\)/.test(panel), 'the panel unticks the box and opens the confirmation sheet; only its Turn on button enables');
    const html = fs.readFileSync(path.join(__dirname, '..', '..', 'renderer', 'panel.html'), 'utf8');
    ok(['sensorWifi', 'sensorSonar', 'sensorCamera', 'sensorHeartRate'].every((k) => html.includes(`id="set-${k}"`)) && /id="sensor-confirm"[^>]*role="dialog"/.test(html) && /Keeps<\/dt><dd>Nothing/.test(html),
      'Settings → Ambient has one toggle per sensor and a confirmation sheet saying nothing is kept');
  }

  // ------------------------------------------------------------ sensors, live indicator, tray dot
  {
    const list = amb.normSensors(eng.sensors);
    ok(list.map((s) => s.key).join(',') === 'sensorWifi,sensorSonar,sensorCamera,sensorHeartRate', 'engine sensor names map onto the four settings toggles');
    ok(list[0].available === false && /CSI/.test(list[0].reason), 'an unavailable sensor keeps its reason');
    const on = list.map((s) => (s.key === 'sensorSonar' ? { ...s, enabled: true } : s));
    ok(amb.liveSensors({ ambientEnabled: false, sensorSonar: true }, on).length === 0, 'nothing is live with the master switch off');
    ok(amb.liveSensors({ ambientEnabled: true, sensorSonar: true }, on).map((s) => s.label).join() === 'Sonar', 'a sensor is live when enabled in settings and the engine runs it');
    ok(amb.liveSensors({ ambientEnabled: true, sensorSonar: true }, list).length === 0, 'a sensor the engine is not running is not shown as live');
    const w = 22, h = 22, buf = Buffer.alloc(w * h * 4, 0);
    const dotted = amb.dotBitmap(buf, w, h);
    const px = (x, y) => dotted.readUInt32LE((y * w + x) * 4);
    ok(px(w - 5, h - 5) === 0xffffffff && px(2, 2) === 0 && !buf.equals(dotted), 'the tray icon gets a white dot in its bottom-right corner (rest untouched)');
  }

  // ------------------------------------------------------------ the task pool's presence mode
  {
    ok(defaultPriority('goal') === 'low' && defaultPriority('schedule') === 'low' && defaultPriority('user') === 'normal', 'goal cycles and scheduled tasks are low priority; asked-for tasks are normal');
  }

  const settings = { ...DEFAULTS, maxParallelTasks: 4 };
  // A task's plan: the agent is created inside start() (synchronously) for a task that gets a slot, so the
  // plan is handed over through `nextPlan`; a queued or resumed task finds it in `plans` by id.
  const plans = new Map();
  const nextPlan = [];
  const tm = new TaskManager({
    getSettings: () => settings,
    createAgent: (taskId) => new ScriptedAgent(taskId, plans.get(taskId) || nextPlan.shift() || { steps: 2 }),
  });
  const ends = [];
  tm.on('end', (e) => ends.push(e));
  tm.prewarm = async () => false; // no spare agents in this test
  const startWith = (opts, plan) => {
    nextPlan.push(plan);
    const r = tm.start(opts);
    plans.set(r.taskId, plan);
    const i = nextPlan.indexOf(plan); if (i >= 0) nextPlan.splice(i, 1); // still queued: not handed over yet
    return r;
  };

  const logs = [];
  const overlay = createBriefOverlay({ BrowserWindow: FakeWindow, screen: fakeScreen, preload: '/x/preload.js', file: '/x/renderer/brief.html', safetyMs: 60000 });
  const shown = [];
  const spoken = [];
  const ctl = new amb.AmbientController({
    engineCall, tasks: tm, getSettings: () => settings, pollMs: 40,
    log: (kind, summary, detail) => logs.push({ kind, summary, detail }),
    showBrief: (b) => { shown.push(b); overlay.show(b); },
    speak: (t) => spoken.push(t),
  });
  const presences = [];
  ctl.on('presence', (st) => presences.push(st.state));

  // ------------------------------------------------------------ off: no polling at all
  ctl.start();
  await sleep(250);
  ok(eng.hits.presence === 0 && eng.hits.sensors === 0, 'with ambient off the app never asks the engine for presence or sensors');
  ok(ctl.status().state === 'UNKNOWN' && !ctl.isAway() && tm.presence === 'unknown', 'with ambient off the presence is UNKNOWN and the task pool is unchanged');

  // ------------------------------------------------------------ on: present
  settings.ambientEnabled = true;
  ctl.onSettings();
  ok(await until(() => ctl.state === 'USER_PRESENT'), 'master switch on: the controller reads USER_PRESENT from GET /presence');
  ok(await until(() => eng.hits.sensors > 0), 'and reads GET /sensors');
  const pollsBefore = eng.hits.presence;
  await sleep(400);
  ok(eng.hits.presence - pollsBefore >= 5, `presence is polled continuously (${eng.hits.presence - pollsBefore} polls in 400 ms at the test's 40 ms interval; 1 s in the app)`);

  // ------------------------------------------------------------ ABSENT: tasks continue, milestones posted
  const goalLong = startWith({ prompt: 'goal cycle: research desk lamps', source: 'goal', goalId: 'g1' }, { steps: 400, toolMs: 1500, gapMs: 15 });
  await simulate('USER_ABSENT');
  ok(await until(() => ctl.isAway()), 'simulated USER_ABSENT: the controller is away');
  ok(tm.presence === 'absent', 'the task pool is in away mode');
  ok(logs.some((l) => /^Away/.test(l.summary)), 'going away is recorded (derived presence only)');
  const build = startWith({ prompt: 'run the nightly build' }, { steps: 3, result: 'Build passed: 212 tests, 0 failures.\nfull log follows...' });
  const fileMgmt = startWith({ prompt: 'tidy ~/Downloads into folders', source: 'schedule' }, { steps: 2, bypass: true, result: 'Moved 48 files into 6 folders.' });
  const failing = startWith({ prompt: 'install the docs toolchain' }, { steps: 1, fail: true });
  ok([build, fileMgmt, failing].every((r) => r.ok && !r.queued), 'new work, low-priority included, starts while away');
  ok(await until(() => eng.milestones.length >= 3, 6000), `every task end while away is POSTed to /milestones (${eng.milestones.length})`);
  const byTask = (id) => eng.milestones.find((m) => m.task_id === id);
  ok(byTask(build.taskId) && byTask(build.taskId).kind === 'completed' && byTask(build.taskId).detail === 'Build passed: 212 tests, 0 failures.', 'a finished task is a "completed" milestone with its first line only');
  ok(byTask(fileMgmt.taskId) && byTask(fileMgmt.taskId).kind === 'error_bypassed', 'a task that finished despite an error is "error_bypassed"');
  ok(byTask(failing.taskId) && byTask(failing.taskId).kind === 'failed' && /network timeout/.test(byTask(failing.taskId).detail), 'a failed task is a "failed" milestone with the error line');
  ok(eng.milestones.every((m) => m.title && typeof m.detail === 'string' && m.detail.length <= 140 && !/\n/.test(m.detail) && !/transcript|full log/.test(m.detail) && Object.keys(m).sort().join() === 'detail,kind,task_id,title,ts'),
    'milestones carry title + one line of detail, never a transcript');
  await ctl.onCheckpoint({ taskId: goalLong.taskId, headline: 'Approve video ‘My $400 desk setup’', summary: 'Uploads the finished video as public.\nmore' });
  ok(eng.milestones.some((m) => m.kind === 'checkpoint' && m.task_id === goalLong.taskId && m.detail === 'Uploads the finished video as public.'), 'a checkpoint filed while away is a "checkpoint" milestone');
  const gl = tm.get(goalLong.taskId);
  ok(gl.status === 'running' && gl.agent.cancelled === 0, 'the long low-priority task keeps running headlessly while away');

  // ------------------------------------------------------------ PRESENT: low-priority pauses at a safe point, brief shown
  settings.maxParallelTasks = 1;
  const queuedLow = startWith({ prompt: 'goal cycle: draft newsletter', source: 'goal', goalId: 'g2' }, { steps: 2 });
  ok(tm.get(queuedLow.taskId).status === 'queued', 'a second low-priority task waits for a slot');
  // Return while the long task is inside a tool call: it must not be stopped until that tool has finished.
  await until(() => gl.agent.inTool === true, 2000, 2);
  const msBefore = eng.milestones.length;
  await simulate('USER_PRESENT');
  ok(await until(() => ctl.state === 'USER_PRESENT'), 'simulated USER_PRESENT: the controller is back');
  ok(gl.pauseRequested === true && gl.agent.cancelled === 0 && gl.status === 'running', 'the low-priority task inside a tool call is not cut off: the pause waits for the safe point');
  ok(await until(() => gl.status === 'paused', 3000, 5), 'the low-priority task pauses at its next safe point');
  ok(gl.agent === null && runs.filter((r) => r.taskId === gl.id).slice(-1)[0].agent.cancelled === 1 && !runs.filter((r) => r.taskId === gl.id).slice(-1)[0].agent.inTool, 'it was stopped between tool calls (no tool in flight)');
  ok(gl.sessionId === 'sess-' + gl.id, 'its session id is kept for the resume');
  ok(!ends.some((e) => e.task.id === gl.id), 'a pause is not an end (no goal cycle recorded, no "stopped")');
  ok(tm.get(queuedLow.taskId).status === 'paused', 'the queued low-priority task is held (not started)');
  settings.maxParallelTasks = 4;
  ok(eng.milestones.length === msBefore, 'nothing is posted as a milestone once the person is back');
  ok(await until(() => shown.length === 1, 8000), 'the brief is shown on return');
  ok(shown[0] && shown[0].sentences.length === 3 && shown[0].sentences.join('|') === SENTENCES.join('|'), 'the brief has the 3 sentences from GET /brief/latest');
  ok(shown[0] && shown[0].milestones === eng.milestones.length, `it says how many milestones (${shown[0] && shown[0].milestones})`);
  ok(spoken.length === 0, 'it is not read aloud while "Read the brief aloud" is off');
  await until(() => windows.length && windows[0].webContents.sent.length, 2000);
  const w = windows[0];
  const o = w && w.opts;
  ok(o && o.frame === false && o.transparent === true && o.alwaysOnTop === true && o.focusable === false && o.skipTaskbar === true && o.show === false && o.backgroundColor === '#00000000',
    'brief window: frameless, transparent, always on top, not focusable, not in the taskbar');
  ok(o && o.webPreferences.sandbox === true && o.webPreferences.contextIsolation === true && o.webPreferences.nodeIntegration === false, 'brief window is sandboxed like the other windows');
  ok(w && w.ignore && w.ignore[0] === true && w.ignore[1] && w.ignore[1].forward === true, 'brief window is click-through: setIgnoreMouseEvents(true, {forward: true})');
  ok(w && w.calls.some((c) => c[0] === 'showInactive') && !w.calls.some((c) => c[0] === 'show' || c[0] === 'focus') && w.focused === false, 'brief window is shown with showInactive and never focused');
  const b = w && w.getBounds();
  ok(b && b.y === 25 + 18 && Math.abs(b.x + b.width / 2 - (1920 + 2560 / 2)) <= 1, 'brief window sits at the top centre of the display under the cursor');
  const sent = w && w.webContents.sent.find((x) => x.ch === 'brief:show');
  ok(sent && sent.payload.sentences.length === 3 && sent.payload.sentences[2] === SENTENCES[2], 'the 3 sentences reach the brief renderer');
  ok(logs.some((l) => /^Brief shown/.test(l.summary)) && logs.every((l) => !/heart|bpm/i.test(JSON.stringify(l))), 'the brief is recorded; nothing biometric is logged');

  // ------------------------------------------------------------ away again: the paused work resumes in its own session
  await simulate('USER_ABSENT');
  ok(await until(() => gl.status === 'running'), 'away again: the paused low-priority task resumes');
  const resumeRun = runs.filter((r) => r.taskId === gl.id).slice(-1)[0];
  ok(resumeRun.sessionIn === 'sess-' + gl.id && resumeRun.prompt === RESUME_PROMPT, 'it resumes in the same session with the continue prompt');
  ok(await until(() => ['running', 'done'].includes(tm.get(queuedLow.taskId).status)), 'the held task starts too');

  // ------------------------------------------------------------ a stale brief is not shown; speaking is opt-in
  settings.briefSpeak = true;
  eng.briefSince = 1; // an old brief: belongs to an earlier away period
  await simulate('USER_PRESENT');
  ok(await until(() => logs.some((l) => /no brief for this time away/.test(l.summary)), 9000), 'a brief from an earlier away period is not shown');
  ok(shown.length === 1 && spoken.length === 0, 'so nothing new appears or is spoken');
  eng.briefSince = null;
  await simulate('USER_ABSENT');
  await until(() => ctl.isAway());
  await simulate('USER_PRESENT');
  ok(await until(() => shown.length === 2 && spoken.length === 1, 8000), 'with "Read the brief aloud" on, the brief is shown and spoken');
  ok(spoken[0] === SENTENCES.join(' '), 'the spoken text is the brief');

  // ------------------------------------------------------------ an UNKNOWN blip is not a return; master off releases held work
  await simulate('USER_ABSENT');
  await until(() => ctl.isAway());
  await simulate('UNKNOWN');
  await until(() => ctl.state === 'UNKNOWN');
  ok(tm.presence === 'absent' && ctl.known === 'USER_ABSENT', 'an UNKNOWN reading in between keeps away mode');
  const shownBefore = shown.length;
  await simulate('USER_PRESENT');
  ok(await until(() => shown.length === shownBefore + 1, 8000), 'away -> unknown -> present still counts as a return (brief shown)');
  const held = tm.paused().map((t) => t.id);
  settings.ambientEnabled = false;
  ctl.onSettings();
  ok(tm.presence === 'unknown' && tm.paused().length === 0 && (held.length === 0 || held.every((id) => ['queued', 'running', 'done'].includes(tm.get(id).status))), `master switch off: presence UNKNOWN and paused work released (${held.length} held)`);
  const hitsOff = eng.hits.presence;
  await sleep(300);
  ok(eng.hits.presence === hitsOff, 'master switch off: polling stops');

  // ------------------------------------------------------------ an engine without the route: back off, no crash
  settings.ambientEnabled = true;
  eng.missingPresence = true;
  ctl.onSettings();
  await sleep(300);
  const h1 = eng.hits.presence;
  await sleep(500);
  ok(eng.hits.presence - h1 <= 1 && ctl.reachable === false, 'an engine without GET /presence is asked again only every 10 s');
  eng.missingPresence = false;

  // ------------------------------------------------------------ stop wins over a pending pause
  {
    const t2 = new TaskManager({ getSettings: () => ({ maxParallelTasks: 2 }), createAgent: (id) => new ScriptedAgent(id, { steps: 400, toolMs: 80 }) });
    t2.prewarm = async () => false;
    const e2 = [];
    t2.on('end', (e) => e2.push(e));
    t2.setPresence('absent');
    const r = t2.start({ prompt: 'goal x', source: 'goal' });
    const n = t2.start({ prompt: 'user thing' });
    const tt = t2.get(r.taskId);
    await until(() => tt.agent && tt.agent.inTool, 2000, 2);
    t2.setPresence('present');
    ok(tt.pauseRequested && t2.get(n.taskId).pauseRequested !== true, 'only the low-priority task is asked to pause; normal work is untouched');
    t2.stop(r.taskId);
    ok(await until(() => tt.status === 'stopped'), 'a stop while a pause is pending stops the task');
    ok(e2.some((e) => e.task.id === r.taskId && e.task.status === 'stopped'), 'and ends it as stopped');
    const p = t2.start({ prompt: 'goal y', source: 'goal' });
    t2.setPresence('absent');
    t2.setPresence('present');
    await until(() => t2.get(p.taskId).status === 'paused', 3000);
    const f = t2.start({ prompt: 'and now this instead', taskId: p.taskId });
    ok(f.ok && f.followUp && ['queued', 'running'].includes(t2.get(p.taskId).status), 'typing a follow-up into a paused task resumes it with that prompt');
    t2.dispose();
  }

  // ------------------------------------------------------------ the real agent marks safe points
  {
    const src = fs.readFileSync(path.join(__dirname, '..', 'agent.js'), 'utf8');
    ok(/type: 'tool_result'[\s\S]{0,400}toolDone: true/.test(src) || /toolDone: true/.test(src.slice(src.indexOf("block.type !== 'tool_result'"), src.indexOf("block.type !== 'tool_result'") + 600)),
      'OAgent emits toolDone on every tool_result (the safe point the pool waits for)');
    process.env.FAKE_ARGS_LOG = path.join(tmp, 'args.jsonl');
    const { OAgent } = require('../agent');
    const FAKE = path.join(__dirname, 'fake-claude.js');
    fs.chmodSync(FAKE, 0o755);
    const a = new OAgent({ userDataDir: tmp, getSettings: () => ({ claudePath: FAKE, cwd: tmp, permissionMode: 'log' }), taskId: 'treal' });
    const evs = [];
    a.on('event', (e) => evs.push(e));
    await a.run('hello there');
    const ti = evs.findIndex((e) => e.type === 'tool');
    const di = evs.findIndex((e) => e.type === 'status' && e.toolDone === true);
    ok(ti >= 0 && di > ti, 'a real OAgent run (fake Claude Code) emits tool, then status{toolDone} after its result');
    a.dispose();
  }

  // ------------------------------------------------------------ brief renderer and overlay files
  {
    const R = path.join(__dirname, '..', '..', 'renderer');
    const html = fs.readFileSync(path.join(R, 'brief.html'), 'utf8');
    const css = fs.readFileSync(path.join(R, 'brief.css'), 'utf8');
    const js = fs.readFileSync(path.join(R, 'brief.js'), 'utf8');
    ok(/Content-Security-Policy/.test(html) && /brief\.js/.test(html) && /brief\.css/.test(html), 'brief.html loads its own css/js under the same CSP as the other windows');
    ok(/LIFE_MS = 12000/.test(js) && /hovers >= 2/.test(js) && /briefDone/.test(js) && !/addEventListener\('click'/.test(js), 'brief fades after 12 s or on the second hover, and needs no click');
    ok(/prefers-reduced-motion: reduce/.test(css) && /--in: 260ms/.test(css) && /--out: 220ms/.test(css), 'brief enter/exit ≤300 ms, reduced motion respected');
    const grey = (r, g, b) => Math.max(r, g, b) - Math.min(r, g, b) <= 10;
    const hexes = [...css.matchAll(/#([0-9a-f]{6})\b/gi)].map((m) => [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)));
    const rgbas = [...css.matchAll(/rgba?\((\d+),\s*(\d+),\s*(\d+)/g)].map((m) => [+m[1], +m[2], +m[3]]);
    ok(hexes.length + rgbas.length > 4 && [...hexes, ...rgbas].every((c) => grey(...c)), 'brief colours are monochrome (every colour token is grey/white/black)');
    ok(briefBounds(fakeScreen, 9999).height === 420 && briefBounds(fakeScreen, 10).height === 80, 'brief window height is bounded');
  }

  // ------------------------------------------------------------ the real engine's response shapes
  {
    // GET /sensors as engine/o_engine/ambient/layer.py sensors_view() answers it
    const real = amb.normSensors({ ambient: true, indicator: false, sensors: [
      { id: 'csi', name: 'wifi-csi', enabled: true, available: false, reason: 'no CSI source configured' },
      { id: 'sonar', name: 'sonar', enabled: false, available: true, reason: '' },
      { id: 'camera', name: 'camera-face', enabled: true, available: true, reason: '' },
      { id: 'heart', name: 'heart-rppg', enabled: false, available: true, reason: '' },
    ], bus: {}, tts: {}, supervisor: {} });
    ok(real.map((s) => s.key).join(',') === 'sensorWifi,sensorSonar,sensorCamera,sensorHeartRate', 'normSensors reads the engine {sensors:[...]} object and maps wifi-csi/sonar/camera-face/heart-rppg');
    ok(amb.unwrapBrief({ brief: null }) === null && amb.unwrapBrief({ brief: { text: '  ' } }) === null, 'unwrapBrief: {brief:null} and an empty text are no brief');
    const b = amb.unwrapBrief({ brief: { text: 'A. B. C.', sentences: ['A.', 'B.', 'C.'], since: 100, count: 3 } });
    ok(b && b.count === 3 && b.since === 100, 'unwrapBrief reads the engine {brief:{text, sentences, since, count}} shape');
    ok(amb.unwrapBrief({ text: 'x.', since: 1 }).text === 'x.', 'unwrapBrief still accepts a bare brief');
  }

  ctl.stop();
  tm.dispose();
  overlay.destroy();
  server.close();
  console.log(`\n${passed} passed, ${failures} failed`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
