#!/usr/bin/env node
'use strict';
// End-to-end check of the ambient layer without Electron: the REAL engine (`python3 -m o_engine serve`
// with a temp data dir and token, as tools/demo/run.js starts it), the REAL AmbientController and
// TaskManager (main/ambient.js, main/tasks.js) driving REAL OAgents over main/tools/fake-claude.js
// (FAKE_SIMPLE), and the REAL brief overlay module (main/brief.js) over a recording fake BrowserWindow.
//
//   USER_ABSENT -> 3 tasks (one low priority) finish -> milestones in GET /milestones
//   -> one /provision job runs headless (isolation backend named; milestone after its final log line)
//   -> USER_PRESENT -> GET /brief/latest: 3 sentences, since == away start -> the brief window gets them
//   -> a low-priority running task pauses at a safe point (and resumes when away again)
//   -> engine side: a low-priority /provision job pauses between steps (when server.py passes `priority`)
//
// Run: node main/tools/e2e-ambient.js   (exit 1 on any failure)
//   O_E2E_PYTHON=python3        interpreter for the engine
//   O_E2E_ENGINE_DIR=<dir>      engine tree to run (default: <repo>/engine)
//   O_E2E_VERBOSE=1             echo engine stderr

const http = require('http');
const net = require('net');
const os = require('os');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { spawn } = require('child_process');
const { EventEmitter } = require('events');

const ROOT = path.resolve(__dirname, '..', '..');
const { TaskManager } = require('../tasks');
const { OAgent } = require('../agent');
const { AmbientController } = require('../ambient');
const { createBriefOverlay } = require('../brief');

const FAKE = path.join(__dirname, 'fake-claude.js');
const ENGINE_DIR = process.env.O_E2E_ENGINE_DIR || path.join(ROOT, 'engine');
const PYTHON = process.env.O_E2E_PYTHON || 'python3';

let failures = 0;
let passed = 0;
const skipped = [];
function ok(cond, label, extra) {
  if (cond) { passed++; console.log('ok   ' + label); } else { failures++; console.log('FAIL ' + label + (extra ? ` :: ${extra}` : '')); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 15000, step = 50) {
  const end = Date.now() + ms;
  while (Date.now() < end) { try { if (await fn()) return true; } catch (_) {} await sleep(step); }
  try { return !!(await fn()); } catch (_) { return false; }
}
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => { const { port } = srv.address(); srv.close(() => resolve(port)); });
  });
}

// ------------------------------------------------------------ the real engine
async function startEngine(tmp) {
  const token = crypto.randomBytes(24).toString('hex');
  const port = await freePort();
  const dataDir = path.join(tmp, 'engine');
  const sandboxRoot = path.join(tmp, 'sandbox');
  fs.mkdirSync(dataDir, { recursive: true });
  fs.mkdirSync(sandboxRoot, { recursive: true });
  const env = {
    ...process.env,
    O_ENGINE_TOKEN: token,
    O_SANDBOX_ROOT: sandboxRoot,
    O_PERMISSION_MODE: 'log',
    O_MAX_ATTEMPTS: '1',
    O_HIGH_RISK_SANDBOX: 'local',
    O_SWARM: '0',
    O_AMBIENT: '0',                    // no sensors or devices: presence comes from /presence/simulate
    O_VLLM_URL: 'http://127.0.0.1:1/v1', // nothing listens: the brief is the deterministic template
    O_CLAUDE_PATH: FAKE,
    O_MCP_SERVERS_FILE: path.join(dataDir, 'mcp-servers.json'),
    PYTHONPATH: ENGINE_DIR + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : ''),
    PYTHONUNBUFFERED: '1',
  };
  for (const k of Object.keys(env)) if (/^(CLAUDECODE|CLAUDE_CODE_|O_LOG_URL|O_UI_URL)/.test(k)) delete env[k];
  fs.writeFileSync(env.O_MCP_SERVERS_FILE, '{"mcpServers":{}}');
  const proc = spawn(PYTHON, ['-m', 'o_engine', 'serve', '--port', String(port), '--data-dir', dataDir], { cwd: ENGINE_DIR, env, stdio: ['ignore', 'pipe', 'pipe'] });
  const stderr = [];
  const onText = (b) => { for (const l of String(b).split(/\r?\n/)) if (l.trim()) { stderr.push(l); if (stderr.length > 200) stderr.shift(); if (process.env.O_E2E_VERBOSE) console.error('[engine]', l); } };
  proc.stdout.on('data', onText);
  proc.stderr.on('data', onText);
  const call = (method, p, body, timeoutMs = 30000) => new Promise((resolve, reject) => {
    const data = body == null ? null : JSON.stringify(body);
    const req = http.request({ hostname: '127.0.0.1', port, path: p, method, timeout: timeoutMs,
      headers: { 'x-o-token': token, accept: 'application/json', ...(data ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) } : {}) } }, (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { raw += c; });
      res.on('end', () => {
        let j = raw; try { j = raw ? JSON.parse(raw) : null; } catch (_) {}
        if (res.statusCode >= 200 && res.statusCode < 300) return resolve(j);
        const e = new Error(`${method} ${p} -> ${res.statusCode} ${typeof j === 'string' ? j.slice(0, 300) : JSON.stringify(j).slice(0, 300)}`);
        e.status = res.statusCode; e.body = j; reject(e);
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error(`${method} ${p} timed out`)));
    if (data) req.write(data);
    req.end();
  });
  const t0 = Date.now();
  let health = null;
  while (Date.now() - t0 < 60000) {
    try { health = await call('GET', '/health', null, 3000); if (health && health.ok) break; } catch (_) {}
    if (proc.exitCode != null) throw new Error('engine exited: ' + stderr.slice(-15).join('\n'));
    await sleep(250);
  }
  if (!health || !health.ok) throw new Error('engine did not answer /health: ' + stderr.slice(-15).join('\n'));
  const logRows = () => {
    try { return fs.readFileSync(path.join(dataDir, 'engine-actions.jsonl'), 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)); } catch (_) { return []; }
  };
  const stop = async () => {
    try { proc.kill('SIGINT'); } catch (_) {}
    await Promise.race([new Promise((r) => proc.on('exit', r)), sleep(4000)]);
    if (proc.exitCode == null) { try { proc.kill('SIGKILL'); } catch (_) {} }
  };
  return { call, port, token, dataDir, sandboxRoot, logRows, stop, stderr };
}

// ------------------------------------------------------------ a recording fake Electron window / screen
class FakeWindow extends EventEmitter {
  constructor(opts) {
    super();
    this.opts = opts; this.bounds = { x: opts.x, y: opts.y, width: opts.width, height: opts.height };
    this.visible = false; this.destroyed = false; this.focused = false;
    this.webContents = Object.assign(new EventEmitter(), { sent: [], send(ch, payload) { this.sent.push({ ch, payload }); } });
    FakeWindow.all.push(this);
  }
  setAlwaysOnTop() {} setVisibleOnAllWorkspaces() {} setFocusable(v) { this.focusable = v; }
  setIgnoreMouseEvents(...a) { this.ignore = a; }
  loadFile(f) { this.file = f; setTimeout(() => this.webContents.emit('did-finish-load'), 10); }
  setBounds(b) { this.bounds = b; } getBounds() { return this.bounds; }
  isVisible() { return this.visible; } isDestroyed() { return this.destroyed; }
  showInactive() { this.visible = true; } show() { this.visible = true; this.focused = true; } hide() { this.visible = false; }
  destroy() { this.destroyed = true; this.emit('closed'); }
}
FakeWindow.all = [];
const fakeScreen = {
  getCursorScreenPoint: () => ({ x: 100, y: 100 }),
  getDisplayNearestPoint: () => ({ workArea: { x: 0, y: 25, width: 1920, height: 1055 } }),
  getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }),
};

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'o-e2e-ambient-'));
  fs.chmodSync(FAKE, 0o755);
  process.env.FAKE_SIMPLE = '1';
  process.env.FAKE_DELAY_MS = '120'; // 120 ms per 24-char piece: a task's length follows its prompt's
  const engine = await startEngine(tmp);
  console.log(`engine up on :${engine.port} (${ENGINE_DIR})`);

  const appLog = [];
  const settings = { claudePath: FAKE, cwd: tmp, permissionMode: 'log', maxParallelTasks: 4, ambientEnabled: true, briefSpeak: false };
  const actionLog = { record(e) { return e; }, ingest(e) { return e; } };
  const tm = new TaskManager({
    getSettings: () => settings,
    createAgent: (taskId) => new OAgent({ userDataDir: tmp, getSettings: () => settings, actionLog, taskId }),
  });
  const ends = [];
  const pausedEv = [];
  tm.on('end', (e) => ends.push(e));
  tm.on('paused', (e) => pausedEv.push(e));
  const overlay = createBriefOverlay({ BrowserWindow: FakeWindow, screen: fakeScreen, preload: path.join(tmp, 'preload.js'), file: path.join(ROOT, 'renderer', 'brief.html') });
  const ctl = new AmbientController({
    engineCall: (m, p, b, t) => engine.call(m, p, b, t), tasks: tm, getSettings: () => settings,
    log: (kind, summary, detail) => appLog.push({ kind, summary, detail }),
    showBrief: (b) => overlay.show(b), speak: () => {}, pollMs: 150,
  });
  const engineJobs = {};
  try {
    ctl.start();
    await until(() => ctl.reachable, 5000);
    ok(ctl.reachable && ctl.state === 'UNKNOWN', 'controller polls the real engine /presence (UNKNOWN with ambient sensing off)');
    await ctl.refreshSensors();
    ok(ctl.sensors.length === 4 && ctl.sensors.every((s) => s.key) && ctl.sensors.every((s) => s.enabled === false),
      'GET /sensors (real {sensors:[...]} shape) maps all four engine sensors onto the catalog, all off', JSON.stringify(ctl.sensors));

    // warm the engine's deps (first /provision builds them) so later jobs pick their graph immediately
    const warm = await engine.call('POST', '/provision', { label: 'warm-up', steps: [{ target_directory: '.', command_string: 'true', timeout_seconds: 30 }] });
    await until(async () => (await engine.call('GET', `/jobs/${warm.job_id}`)).done, 60000, 100);

    // ---------------------------------------------------------- away
    const sim = await engine.call('POST', '/presence/simulate', { state: 'USER_ABSENT', reason: 'e2e' });
    const awayStart = sim.presence.absent_since;
    ok(sim.ok && sim.presence.state === 'USER_ABSENT' && typeof awayStart === 'number', 'POST /presence/simulate USER_ABSENT');
    ok(await until(() => ctl.known === 'USER_ABSENT' && tm.presence === 'absent', 5000), 'the controller sees USER_ABSENT and tells the task pool');
    ok(Math.abs(ctl.awaySince - awayStart) < 1e-6, 'the controller keeps the engine away start', `${ctl.awaySince} vs ${awayStart}`);

    const t1 = tm.start({ prompt: 'Tidy the downloads', title: 'Tidy downloads' });
    const t2 = tm.start({ prompt: 'Summarise the inbox', title: 'Inbox summary' });
    const t3 = tm.start({ prompt: 'Nightly backup check', title: 'Backup check', source: 'schedule' });
    const longPrompt = 'Research a long topic. ' + 'x'.repeat(3000);
    const t4 = tm.start({ prompt: longPrompt, title: 'Long goal cycle', source: 'goal', goalId: 'g-e2e' });
    ok([t1, t2, t3, t4].every((r) => r.ok && !r.queued), 'four tasks start while away (one is a long goal cycle)');
    ok(tm.get(t3.taskId).priority === 'low' && tm.get(t4.taskId).priority === 'low' && tm.get(t1.taskId).priority === 'normal', 'scheduled and goal tasks are low priority; user tasks normal');

    // engine job A (normal) while away: headless, isolation named, milestone after its final log line
    const stepsA = [{ target_directory: '.', command_string: 'echo a > a.txt', timeout_seconds: 30 }, { target_directory: '.', command_string: 'cat a.txt', timeout_seconds: 30 }];
    engineJobs.a = (await engine.call('POST', '/provision', { label: 'Headless provision A', steps: stepsA })).job_id;

    // engine job B (low) while away: pauses between steps on return, when server.py passes `priority`
    const stepsB = [{ target_directory: '.', command_string: 'sleep 2; echo a >> trace.txt', timeout_seconds: 60 }, { target_directory: '.', command_string: 'echo b >> trace.txt', timeout_seconds: 60 }];
    let hook = true;
    try {
      engineJobs.b = (await engine.call('POST', '/provision', { label: 'Low-priority provision B', steps: stepsB, priority: 'low' })).job_id;
    } catch (e) {
      if (e.status === 422 && /priority/.test(JSON.stringify(e.body))) { hook = false; skipped.push('engine pause: this server.py rejects `priority` on /provision (apply the take_priority/job_finished hook)'); } else throw e;
    }

    const threeDone = await until(() => [t1, t2, t3].every((r) => ends.some((e) => e.task.id === r.taskId)), 30000);
    ok(threeDone && [t1, t2, t3].every((r) => ends.find((e) => e.task.id === r.taskId).ok), 'the three short tasks finish (fake Claude Code)');
    ok(await until(() => ctl.milestonesPosted >= 3, 5000), 'the controller posts a milestone for each task that ended while away');
    let ms = (await engine.call('GET', `/milestones?since=${awayStart}`)).milestones;
    const titles = ms.map((m) => m.title);
    ok(['Tidy downloads', 'Inbox summary', 'Backup check'].every((t) => titles.includes(t)) && ms.filter((m) => m.title !== 'Headless provision A').every((m) => m.presence === 'USER_ABSENT' && m.kind === 'completed'),
      'GET /milestones lists the three task milestones (completed, recorded while USER_ABSENT)', JSON.stringify(ms));
    ok(ms.every((m) => !/Long transcript|You said/.test(m.detail || '') || (m.detail || '').length <= 140), 'milestone details are one clipped line');

    const jobA = await (async () => { let st; await until(async () => (st = await engine.call('GET', `/jobs/${engineJobs.a}`)).done, 60000, 100); return st; })();
    ok(jobA.done && jobA.ok === true, 'headless /provision job A finished ok', JSON.stringify(jobA).slice(0, 400));
    ok(jobA.headless === true && typeof jobA.isolation === 'string' && jobA.isolation.length > 0, `job A state: headless: true, isolation: "${jobA.isolation}"`);
    await until(async () => (await engine.call('GET', `/milestones?since=${awayStart}`)).milestones.some((m) => m.task_id === engineJobs.a), 5000);
    ms = (await engine.call('GET', `/milestones?since=${awayStart}`)).milestones;
    const msA = ms.find((m) => m.task_id === engineJobs.a);
    const rows = engine.logRows();
    const finalLine = rows.filter((r) => r.detail && r.detail.job_id === engineJobs.a).pop();
    ok(msA && /headless:/.test(msA.detail) && msA.presence === 'USER_ABSENT', 'job A wrote a milestone row (headless, while away)', JSON.stringify(msA));
    ok(finalLine && finalLine.kind === 'result' && /job .* ok/.test(finalLine.summary) && msA && msA.ts >= finalLine.ts,
      'job A milestone row is written after its final log line', `${finalLine && finalLine.summary} @${finalLine && finalLine.ts} vs milestone @${msA && msA.ts}`);
    const isoNote = rows.find((r) => r.detail && r.detail.job_id === engineJobs.a && /isolated sandbox destroyed|no isolated backend available|headless job isolated in/.test(r.summary));
    ok(!!isoNote, 'the engine logged the isolation choice for job A', isoNote && isoNote.summary);

    // ---------------------------------------------------------- return
    ok(tm.get(t4.taskId).status === 'running', 'the long low-priority task is still running at the return');
    if (hook) {
      ok(await until(async () => { const st = await engine.call('GET', `/jobs/${engineJobs.b}`); return st.phase === 'execute' && st.headless === true; }, 30000, 50),
        'engine job B (priority low) is executing headless before the return');
    }
    const back = await engine.call('POST', '/presence/simulate', { state: 'USER_PRESENT', reason: 'e2e' });
    ok(back.presence.state === 'USER_PRESENT', 'POST /presence/simulate USER_PRESENT');

    let brief = null;
    ok(await until(async () => { brief = (await engine.call('GET', '/brief/latest')).brief; return !!brief; }, 15000), 'GET /brief/latest returns a brief');
    ok(brief && Array.isArray(brief.sentences) && brief.sentences.length === 3, 'the brief is exactly 3 sentences', brief && JSON.stringify(brief.sentences));
    ok(brief && brief.since === awayStart, 'brief.since == the away start', brief && `${brief.since} vs ${awayStart}`);
    const msAtReturn = (await engine.call('GET', `/milestones?since=${awayStart}`)).milestones.filter((m) => m.ts <= brief.until);
    ok(brief && brief.count === msAtReturn.length && brief.count >= 4, `the brief covers the ${brief && brief.count} milestones of this away period`);

    ok(await until(() => FakeWindow.all.some((w) => w.webContents.sent.some((s) => s.ch === 'brief:show')), 15000), 'the brief window receives brief:show');
    const win = FakeWindow.all.find((w) => w.webContents.sent.some((s) => s.ch === 'brief:show'));
    const shown = win && win.webContents.sent.find((s) => s.ch === 'brief:show').payload;
    ok(shown && shown.sentences.length === 3 && shown.sentences.join(' ') === brief.sentences.join(' ') && shown.milestones === brief.count,
      'the window got the same 3 sentences and the milestone count', JSON.stringify(shown));
    ok(win && win.visible && !win.focused && win.opts.focusable === false && win.ignore && win.ignore[0] === true, 'the brief window is shown inactive, unfocusable and click-through');

    ok(await until(() => tm.get(t4.taskId).status === 'paused', 8000), 'the low-priority running task pauses at a safe point on return');
    const pe = pausedEv.find((e) => e.task.id === t4.taskId);
    ok(pe && pe.sessionId, 'its Claude Code session is kept for the resume', pe && pe.sessionId);
    ok(!ends.some((e) => e.task.id === t4.taskId), 'pausing is not an end (no milestone, no failed task)');
    ok(appLog.some((l) => /^Back: \d+ low-priority task\(s\) pausing/.test(l.summary)), 'the controller logs the return', JSON.stringify(appLog.map((l) => l.summary)));

    if (hook) {
      let st = null;
      ok(await until(async () => (st = await engine.call('GET', `/jobs/${engineJobs.b}`)).paused === true, 10000, 50), 'engine job B (low) pauses between steps on return', st && JSON.stringify(st).slice(0, 300));
      const trace = path.join(engine.sandboxRoot, engineJobs.b, 'trace.txt');
      ok(fs.existsSync(trace) && fs.readFileSync(trace, 'utf8').split(/\s+/).filter(Boolean).join(',') === 'a', 'job B finished step 0 and has not started step 1');
      const pres = await engine.call('GET', '/presence');
      ok(pres.pause && pres.pause.paused === true && pres.pause.waiting.includes(engineJobs.b), 'GET /presence reports the paused job');
      await sleep(600);
      st = await engine.call('GET', `/jobs/${engineJobs.b}`);
      ok(!st.done && st.paused === true, 'job B stays paused while the user is present');
    }

    // ---------------------------------------------------------- away again: everything resumes
    await engine.call('POST', '/presence/simulate', { state: 'USER_ABSENT', reason: 'e2e again' });
    ok(await until(() => ['running', 'done'].includes(tm.get(t4.taskId).status) && tm.get(t4.taskId).runs >= 2, 8000), 'away again: the paused task resumes in its own session');
    ok(await until(() => ends.some((e) => e.task.id === t4.taskId && e.ok), 30000), 'the resumed task finishes');
    if (hook) {
      let st = null;
      ok(await until(async () => (st = await engine.call('GET', `/jobs/${engineJobs.b}`)).done, 30000, 100) && st.ok === true && st.headless === true,
        'job B resumes when away again and finishes ok (headless)', st && JSON.stringify(st).slice(0, 300));
      const trace = path.join(engine.sandboxRoot, engineJobs.b, 'trace.txt');
      ok(fs.readFileSync(trace, 'utf8').split(/\s+/).filter(Boolean).join(',') === 'a,b', 'job B ran step 1 exactly once after the resume');
      const summaries = engine.logRows().map((r) => r.summary);
      ok(summaries.some((s) => s.includes(`${engineJobs.b}: low-priority job paused at a safe point`)) && summaries.some((s) => s.includes(`${engineJobs.b}: low-priority job resumed after`)),
        'the engine log records the pause and the resume of job B');
    }
  } catch (err) {
    failures++;
    console.log('FAIL unexpected error: ' + (err && err.stack || err));
    console.log(engine.stderr.slice(-20).join('\n'));
  } finally {
    ctl.stop();
    try { for (const t of tm.list()) if (['running', 'queued', 'paused'].includes(t.status)) tm.stop(t.id); } catch (_) {}
    try { tm.dispose(); } catch (_) {}
    overlay.destroy();
    await engine.stop();
  }
  for (const s of skipped) console.log('SKIP ' + s);
  console.log(`\n${passed} passed, ${failures} failed, ${skipped.length} skipped`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
