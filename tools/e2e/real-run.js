#!/usr/bin/env node
'use strict';
// REAL end-to-end run of O without Electron: the real engine (`python3 -m o_engine serve`), the real
// TaskManager / OAgent / Scheduler / GoalRunner / AmbientController from main/, and the REAL Claude Code CLI
// (through tools/e2e/claude-meter.js, which only records each run's total_cost_usd). main.js's glue
// (observe on task end, correction follow-ups, goal cycle records) is reproduced here as main.js does it.
//
//   node tools/e2e/real-run.js 1 2 3 4 5 6       steps to run (state carries over in $O_E2E_OUT/state.json)
//   O_E2E_OUT=<dir>       logs, transcripts, workspaces, engine data (default: the session scratchpad real/)
//   O_E2E_BUDGET_USD=6    stop before a step when the metered total is above this
//   O_E2E_MODEL=sonnet    model for every Claude Code run (O setting `model`; the meter adds it to engine runs)
//
// Spends real model usage: every task is a real `claude -p` run.

const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { spawn, execFileSync, spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..', '..');
const OUT = process.env.O_E2E_OUT || '/tmp/claude-0/-home-claude/fdad5a75-341e-5cfa-b136-c276d91b255c/scratchpad/real';
const BUDGET = Number(process.env.O_E2E_BUDGET_USD || 6);
const MODEL = process.env.O_E2E_MODEL || 'sonnet';
const METER = path.join(__dirname, 'claude-meter.js');
const FIX = path.join(__dirname, 'fixtures.py');
const COST_LOG = path.join(OUT, 'cost.jsonl');
const STATE_FILE = path.join(OUT, 'state.json');
const PYGI = process.env.O_E2E_PYGI || '/opt/pygi'; // PyGObject built for the engine's python (AT-SPI backend)
fs.mkdirSync(path.join(OUT, 'transcripts'), { recursive: true });

// Env a desktop user's Claude Code would not have: this harness runs inside another Claude Code session.
const STRIP = /^(CLAUDECODE|CLAUDE_CODE_ENTRYPOINT|CLAUDE_CODE_SESSION_ID|CLAUDE_CODE_CHILD_SESSION|CLAUDE_PID|CLAUDE_CODE_DISABLE_BACKGROUND_TASKS|CLAUDE_AUTO_BACKGROUND_TASKS|CLAUDE_CODE_BG_TASKS_REPORT_RUNNING|CLAUDE_CODE_TEE_SDK_STDOUT|CLAUDE_CODE_INCLUDE_PARTIAL_MESSAGES|CLAUDE_CODE_MESSAGING_.*|CLAUDE_CODE_DIAGNOSTICS_FILE|CLAUDE_CODE_REMOTE_SESSION_ID|CLAUDE_EFFORT|O_LOG_URL|O_UI_URL|O_ENGINE_.*)$/;
for (const k of Object.keys(process.env)) if (STRIP.test(k)) delete process.env[k];
process.env.O_E2E_COST_LOG = COST_LOG;
process.env.O_E2E_MODEL = MODEL;

const { TaskManager } = require(path.join(ROOT, 'main', 'tasks'));
const { OAgent } = require(path.join(ROOT, 'main', 'agent'));
const { Scheduler } = require(path.join(ROOT, 'main', 'scheduler'));
const { GoalRunner } = require(path.join(ROOT, 'main', 'goals'));
const { AmbientController } = require(path.join(ROOT, 'main', 'ambient'));
const { ActionLog } = require(path.join(ROOT, 'main', 'actionlog'));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now();
const results = [];
function ok(cond, label, extra) {
  results.push({ ok: !!cond, label, extra: extra == null ? undefined : extra });
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${label}${extra != null ? ' :: ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)).slice(0, 600) : ''}`);
  return !!cond;
}
async function until(fn, ms, step = 250) {
  const end = now() + ms;
  while (now() < end) { try { if (await fn()) return true; } catch (_) {} await sleep(step); }
  try { return !!(await fn()); } catch (_) { return false; }
}
function loadState() { try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch (_) { return {}; } }
function saveState(s) { fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2)); }
function costRows() { try { return fs.readFileSync(COST_LOG, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)); } catch (_) { return []; } }
function spent() { return costRows().reduce((a, r) => a + (Number(r.cost) || 0), 0); }
function py(args) {
  const r = spawnSync('python3', [FIX, ...args], { encoding: 'utf8' });
  try { return JSON.parse(r.stdout.trim().split('\n').pop()); } catch (_) { return { ok: false, problems: [r.stderr.slice(-500) || 'no output'] }; }
}
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer(); srv.unref(); srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => { const { port } = srv.address(); srv.close(() => resolve(port)); });
  });
}
function httpGet(port, p = '/', timeoutMs = 3000) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: p, timeout: timeoutMs }, (res) => {
      let b = ''; res.setEncoding('utf8'); res.on('data', (c) => { b += c; }); res.on('end', () => resolve({ status: res.statusCode, body: b }));
    });
    req.on('error', (e) => resolve({ status: 0, error: e.message }));
    req.on('timeout', () => req.destroy(new Error('timeout')));
  });
}

// ------------------------------------------------------------ the real engine
async function startEngine(extraEnv = {}) {
  const token = crypto.randomBytes(24).toString('hex');
  const port = await freePort();
  const dataDir = path.join(OUT, 'engine');
  const sandboxRoot = path.join(OUT, 'sandbox');
  fs.mkdirSync(dataDir, { recursive: true });
  fs.mkdirSync(sandboxRoot, { recursive: true });
  const env = {
    ...process.env,
    O_ENGINE_TOKEN: token,
    O_SANDBOX_ROOT: sandboxRoot,
    O_PERMISSION_MODE: 'log',
    O_MAX_ATTEMPTS: '3',
    O_HIGH_RISK_SANDBOX: 'local',
    O_SWARM: '0',
    O_AMBIENT: '0',                      // presence comes from /presence/simulate
    O_VLLM_URL: 'http://127.0.0.1:1/v1', // no local model: every model call goes to Claude Code
    O_CLAUDE_PATH: METER,
    O_MCP_SERVERS_FILE: path.join(dataDir, 'mcp-servers.json'),
    PYTHONPATH: [path.join(ROOT, 'engine'), fs.existsSync(PYGI) ? PYGI : null, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    PYTHONUNBUFFERED: '1',
    ...extraEnv,
  };
  if (!fs.existsSync(env.O_MCP_SERVERS_FILE)) fs.writeFileSync(env.O_MCP_SERVERS_FILE, '{"mcpServers":{}}');
  const logFile = fs.openSync(path.join(OUT, 'engine.log'), 'a');
  const proc = spawn('python3', ['-m', 'o_engine', 'serve', '--port', String(port), '--data-dir', dataDir], { cwd: path.join(ROOT, 'engine'), env, stdio: ['ignore', logFile, logFile] });
  const call = (method, p, body, timeoutMs = 30000) => new Promise((resolve, reject) => {
    const data = body == null ? null : JSON.stringify(body);
    const req = http.request({ hostname: '127.0.0.1', port, path: p, method, timeout: timeoutMs,
      headers: { 'x-o-token': token, accept: 'application/json', ...(data ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) } : {}) } }, (res) => {
      let raw = ''; res.setEncoding('utf8'); res.on('data', (c) => { raw += c; });
      res.on('end', () => {
        let j = raw; try { j = raw ? JSON.parse(raw) : null; } catch (_) {}
        if (res.statusCode >= 200 && res.statusCode < 300) return resolve(j);
        const e = new Error(`${method} ${p} -> ${res.statusCode} ${JSON.stringify(j).slice(0, 300)}`); e.status = res.statusCode; reject(e);
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error(`${method} ${p} timed out`)));
    if (data) req.write(data);
    req.end();
  });
  const t0 = now();
  let health = null;
  while (now() - t0 < 90000) {
    try { health = await call('GET', '/health', null, 3000); if (health && health.ok) break; } catch (_) {}
    if (proc.exitCode != null) throw new Error('engine exited; see ' + path.join(OUT, 'engine.log'));
    await sleep(300);
  }
  if (!health || !health.ok) throw new Error('engine did not answer /health');
  const watchdog = setInterval(() => { // the engine's own Claude Code runs (computer use) count against the cap too
    if (spent() > BUDGET && proc.exitCode == null) { console.log(`BUDGET: metered $${spent().toFixed(3)} > $${BUDGET}; killing the engine`); try { proc.kill('SIGKILL'); } catch (_) {} }
  }, 2000);
  watchdog.unref();
  const stop = async () => {
    clearInterval(watchdog);
    try { proc.kill('SIGINT'); } catch (_) {}
    await Promise.race([new Promise((r) => proc.on('exit', r)), sleep(5000)]);
    if (proc.exitCode == null) { try { proc.kill('SIGKILL'); } catch (_) {} }
  };
  return { call, port, token, url: `http://127.0.0.1:${port}`, dataDir, stop, health };
}

// ------------------------------------------------------------ the app side (what main.js wires up)
const CORRECTION_RE = /^\s*(no\b|nope|not (that|quite|what)|actually|instead|wrong|that's (not|wrong)|redo|try again|again,|do it again|fix (it|that)|change (it|that)|make it|i (said|meant|wanted))/i; // main.js

function startApp(engine, cwd) {
  const appDir = path.join(OUT, 'app');
  fs.mkdirSync(appDir, { recursive: true });
  const actionLog = new ActionLog(appDir);
  const settings = { claudePath: METER, cwd, permissionMode: 'log', maxParallelTasks: 3, model: MODEL, ambientEnabled: true, briefSpeak: false, browserMode: 'link' };
  const appLog = [];
  const logApp = (kind, summary, detail) => { appLog.push({ ts: now(), kind, summary, detail }); try { actionLog.record({ kind, summary, detail: detail || {} }); } catch (_) {} };
  const observe = (body) => { Promise.resolve().then(() => engine.call('POST', '/learn/observe', body, 5000)).catch(() => {}); };
  const transcripts = new Map(); // task id -> [events]
  let goals = null;
  let scheduler = null;
  const common = { userDataDir: appDir, getSettings: () => settings, actionLog, getEngine: () => ({ url: engine.url, token: engine.token }), getLinkEndpoint: () => null };
  const handleSchedule = (body) => (scheduler ? scheduler.handle(body) : { ok: false, error: 'scheduler unavailable' });
  const handleGoal = (taskId, body) => (goals ? goals.handle(taskId, body) : { ok: false, error: 'goals unavailable' });
  const tm = new TaskManager({
    getSettings: () => settings,
    createAgent: (taskId, opts) => new OAgent({ ...common, taskId, getChatId: () => taskId, onSchedule: handleSchedule, onGoal: handleGoal,
      goalId: (opts && opts.goalId) || (goals && goals.goalForTask(taskId)) || null }),
  });
  tm.on('event', (ev) => {
    if (!ev.taskId) return;
    if (!transcripts.has(ev.taskId)) transcripts.set(ev.taskId, []);
    transcripts.get(ev.taskId).push({ ts: now(), ...ev });
  });
  tm.on('end', ({ task, ok: good, durationMs, result }) => { // main.js onTaskEnd
    if (goals) goals.onTaskEnd({ task, ok: good, result }).catch((err) => logApp('note', `Goal cycle not recorded: ${err.message}`, { taskId: task.id }));
    const viaEngine = (task.tools || []).some((n) => n === 'mcp__o__provision' || n === 'mcp__o__computer_run' || n === 'mcp__o__computer_replay');
    if (task.status !== 'stopped' && !viaEngine) {
      const used = [...(task.tools || [])].map((n) => n.replace(/^mcp__/, '')).slice(0, 8).join(', ');
      const text = `${task.title}${task.prompt && task.prompt !== task.title ? ' — ' + task.prompt : ''}${used ? ` (tools: ${used})` : ''}`;
      observe({ source: 'task', task_id: task.id, text: text.slice(0, 2000), outcome: good ? 'ok' : 'failed', duration_s: Math.round((durationMs || 0) / 100) / 10 });
    }
  });
  const startTask = ({ prompt, taskId, title, source, scheduleId, goalId } = {}) => { // main.js startTask
    const prior = taskId ? tm.get(taskId) : null;
    const r = tm.start({ prompt, taskId, title, source, scheduleId, goalId });
    if (r.ok && r.followUp && CORRECTION_RE.test(prompt)) observe({ source: 'correction', task_id: r.taskId, text: prompt.slice(0, 2000), ...(prior ? { outcome: prior.status === 'done' ? 'ok' : 'failed' } : {}) });
    return r;
  };
  scheduler = new Scheduler({ file: path.join(appDir, 'o-schedules.json'), onFire: (s) => {
    if (s.goalId) { if (goals) goals.fireCycle(s.goalId).catch(() => {}); return; }
    startTask({ prompt: s.prompt, title: s.title, source: 'schedule', scheduleId: s.id });
  } });
  goals = new GoalRunner({ engineCall: (m, p, b, t) => engine.call(m, p, b, t), scheduler, startTask, getTasks: () => tm.list(), file: path.join(appDir, 'o-goals.json'), log: logApp });
  const briefs = [];
  const ambient = new AmbientController({ engineCall: (m, p, b, t) => engine.call(m, p, b, t), tasks: tm, getSettings: () => settings, log: logApp, showBrief: (b) => briefs.push(b), speak: () => {}, pollMs: 300 });
  ambient.start();
  const dumpTranscript = (taskId, label) => {
    const t = tm.get(taskId);
    const rows = transcripts.get(taskId) || [];
    fs.writeFileSync(path.join(OUT, 'transcripts', `${label}.jsonl`), rows.map((r) => JSON.stringify(r)).join('\n') + '\n');
    return { rows, task: t ? tm.snapshot(t) : null };
  };
  const waitEnd = (taskId, ms) => until(() => {
    if (spent() > BUDGET) { console.log(`BUDGET: metered $${spent().toFixed(3)} > $${BUDGET}; stopping ${taskId}`); tm.stop(taskId); }
    const t = tm.get(taskId); return t && !['queued', 'running', 'paused'].includes(t.status);
  }, ms, 500);
  const dispose = () => { try { ambient.stop(); } catch (_) {} try { tm.dispose(); } catch (_) {} };
  return { tm, settings, actionLog, appLog, transcripts, goals, scheduler, ambient, briefs, startTask, dumpTranscript, waitEnd, dispose };
}

// ------------------------------------------------------------ helpers over a finished task
function costFor(taskId) { return costRows().filter((r) => r.tag && taskId && r.tag === taskId.replace(/[^A-Za-z0-9_-]/g, '').slice(0, 40)).reduce((a, r) => a + r.cost, 0); }
function finalText(rows) { const d = [...rows].reverse().find((r) => r.type === 'done' || r.type === 'error'); return d ? (d.result || d.message || '') : ''; }
function toolsUsed(rows) { return rows.filter((r) => r.type === 'tool').map((r) => r.name); }
const QUESTION_RE = /(\?\s*$)|(\b(would you like|do you want|should i|shall i|which (one|folder|format|port)|can you (confirm|tell me)|please (confirm|let me know|clarify)|let me know (if|which|whether))\b)/im;
function asksQuestion(rows) {
  const texts = rows.filter((r) => r.type === 'assistant').map((r) => r.text).concat([finalText(rows)]);
  return texts.filter((t) => t && QUESTION_RE.test(String(t))).map((t) => String(t).slice(0, 300));
}
function budgetGuard(stepName) {
  const s = spent();
  if (s > BUDGET) { console.log(`STOP before ${stepName}: metered total $${s.toFixed(3)} > $${BUDGET}`); return false; }
  console.log(`-- ${stepName} (metered so far $${s.toFixed(3)})`);
  return true;
}
function portListeners(port) {
  // the processes with a LISTEN socket on this port (no ss/lsof here): /proc/net/tcp inode -> /proc/<pid>/fd
  const hex = port.toString(16).toUpperCase().padStart(4, '0');
  const inodes = new Set();
  for (const f of ['/proc/net/tcp', '/proc/net/tcp6']) {
    try { for (const l of fs.readFileSync(f, 'utf8').split('\n').slice(1)) { const c = l.trim().split(/\s+/); if (c[1] && c[1].endsWith(':' + hex) && c[3] === '0A') inodes.add(c[9]); } } catch (_) {}
  }
  const pids = [];
  for (const pid of fs.readdirSync('/proc').filter((d) => /^\d+$/.test(d))) {
    try { for (const fd of fs.readdirSync(`/proc/${pid}/fd`)) { const l = fs.readlinkSync(`/proc/${pid}/fd/${fd}`); const m = l.match(/^socket:\[(\d+)\]$/); if (m && inodes.has(m[1])) { pids.push({ pid: Number(pid), cwd: fs.readlinkSync(`/proc/${pid}/cwd`), cmd: fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').replace(/\0/g, ' ').trim() }); break; } } } catch (_) {}
  }
  return pids;
}

// ------------------------------------------------------------ steps
const PROMPTS = {
  a: 'Rename every photo in ./photos by the date it was taken (YYYY-MM-DD_n.jpg) and put them in month folders',
  b: 'Read the receipts in ./receipts (txt/csv) and build expenses.xlsx with a total per category',
  c: 'Set up ./webapp: a Vite or plain Node static server on port 5173 and confirm it serves',
};

async function step1(ctx, state) {
  const ws = path.join(OUT, 'ws1');
  fs.rmSync(ws, { recursive: true, force: true });
  py(['make', ws, path.join(OUT, 'manifest-ws1.json')]);
  fs.rmSync(path.join(ws, 'notes'), { recursive: true, force: true });
  ok(portListeners(5173).length === 0, 'port 5173 is free before the run', portListeners(5173));
  const app = startApp(ctx.engine, ws);
  ctx.apps.push(app);
  const t0 = now();
  const ids = {};
  for (const k of ['a', 'b', 'c']) {
    const r = app.startTask({ prompt: PROMPTS[k] });
    ok(r.ok && !r.queued, `task ${k} started without queueing (maxParallelTasks 3)`, r);
    ids[k] = r.taskId;
  }
  const allDone = await until(() => ['a', 'b', 'c'].every((k) => { const t = app.tm.get(ids[k]); return t && !['queued', 'running', 'paused'].includes(t.status); }), 15 * 60 * 1000, 1000);
  const total = now() - t0;
  ok(allDone, 'all three tasks ended within 15 min');
  const out = { ws, ids, totalMs: total, tasks: {} };
  for (const k of ['a', 'b', 'c']) {
    const { rows, task } = app.dumpTranscript(ids[k], `step1-${k}`);
    out.tasks[k] = { status: task.status, startedAt: task.startedAt, endedAt: task.endedAt, wallMs: task.endedAt - task.startedAt, costUsd: costFor(ids[k]), tools: toolsUsed(rows), final: finalText(rows).slice(0, 800), questions: asksQuestion(rows), sessionId: (app.tm.get(ids[k]) || {}).sessionId || null };
    ok(task.status === 'done', `task ${k} status done`, task.status + ' ' + (task.error || ''));
  }
  const starts = ['a', 'b', 'c'].map((k) => out.tasks[k].startedAt);
  const ends = ['a', 'b', 'c'].map((k) => out.tasks[k].endedAt);
  out.overlapMs = Math.min(...ends) - Math.max(...starts);
  ok(out.overlapMs > 0, `the three tasks overlapped in time (all three running together for ${(out.overlapMs / 1000).toFixed(1)} s)`);
  // outcomes on disk / port
  const ph = py(['check-photos', ws, path.join(OUT, 'manifest-ws1.json')]);
  out.tasks.a.check = ph;
  ok(ph.ok, 'outcome a: 30 photos renamed YYYY-MM-DD_n.jpg by EXIF date, in month folders', ph);
  const xl = py(['check-xlsx', path.join(ws, 'expenses.xlsx'), path.join(OUT, 'manifest-ws1.json')]);
  out.tasks.b.check = xl;
  ok(xl.ok, 'outcome b: expenses.xlsx has the right total per category', xl);
  const page = await httpGet(5173, '/');
  const listeners = portListeners(5173);
  const webFiles = fs.existsSync(path.join(ws, 'webapp')) ? fs.readdirSync(path.join(ws, 'webapp')) : [];
  out.tasks.c.check = { http: page.status, bodyHead: (page.body || '').slice(0, 200), listeners, webFiles };
  ok(page.status === 200, 'outcome c: http://127.0.0.1:5173/ answers 200 after the task ended', out.tasks.c.check);
  ok(webFiles.length > 0, 'outcome c: ./webapp exists with files', webFiles);
  ok(listeners.some((l) => l.cwd.startsWith(path.join(ws, 'webapp'))), 'outcome c: the server on 5173 runs from ./webapp', listeners);
  ok(out.tasks.c.tools.includes('mcp__o__verify_port'), 'task c used the engine verify tool (mcp__o__verify_port)', out.tasks.c.tools);
  ok(out.tasks.c.tools.includes('mcp__o__provision'), 'task c used the engine provision tool (mcp__o__provision)', out.tasks.c.tools);
  for (const k of ['a', 'b', 'c']) console.log(`   ${k}: ${(out.tasks[k].wallMs / 1000).toFixed(1)} s, $${out.tasks[k].costUsd.toFixed(3)}, tools ${[...new Set(out.tasks[k].tools)].join(',')}`);
  console.log(`   total wall ${(total / 1000).toFixed(1)} s`);
  state.step1 = out;
  return out;
}

function step2(ctx, state) {
  const s1 = state.step1;
  if (!s1) return ok(false, 'step 2 needs step 1');
  let any = false;
  for (const k of ['a', 'b', 'c']) {
    const q = s1.tasks[k].questions;
    ok(q.length === 0, `act-first: task ${k} asked no clarifying question`, q);
    if (q.length) any = true;
  }
  state.step2 = { askedQuestion: any };
  return !any;
}

async function step3(ctx, state) {
  const ws = path.join(OUT, 'ws-replay');
  fs.rmSync(ws, { recursive: true, force: true });
  py(['photos', ws, path.join(OUT, 'manifest-replay.json')]);
  const app = startApp(ctx.engine, ws);
  ctx.apps.push(app);
  const profile = await ctx.engine.call('GET', '/learn/profile').catch(() => null);
  const procs = await ctx.engine.call('GET', '/computer/procedures').catch(() => []);
  const cache = await ctx.engine.call('GET', '/cache').catch(() => null);
  const r = app.startTask({ prompt: PROMPTS.a });
  await app.waitEnd(r.taskId, 10 * 60 * 1000);
  const { rows, task } = app.dumpTranscript(r.taskId, 'step3-a-replay');
  const ph = py(['check-photos', ws, path.join(OUT, 'manifest-replay.json')]);
  const first = state.step1 && state.step1.tasks.a;
  const out = { wallMs: task.endedAt - task.startedAt, firstWallMs: first ? first.wallMs : null, costUsd: costFor(r.taskId), firstCostUsd: first ? first.costUsd : null,
    tools: toolsUsed(rows), cachedReplay: rows.some((x) => /Déjà vu/.test(x.text || '')), check: ph, final: finalText(rows).slice(0, 600),
    before: { profileItems: profile ? ['preferences', 'habits', 'app_skills'].reduce((a, k) => a + ((profile[k] || []).length), 0) : null, procedures: Array.isArray(procs) ? procs.length : null, cache: cache && (cache.entries || cache.count || cache.length) } };
  ok(task.status === 'done' && ph.ok, 'replay of task a produced the right result on fresh fixtures', ph);
  console.log(`   replay ${(out.wallMs / 1000).toFixed(1)} s vs first ${(out.firstWallMs / 1000).toFixed(1)} s; cost $${out.costUsd.toFixed(3)} vs $${(out.firstCostUsd || 0).toFixed(3)}; provision cache replay: ${out.cachedReplay}`);
  state.step3 = out;
}

async function step4(ctx, state) {
  const s1 = state.step1;
  if (!s1 || !s1.tasks.b.sessionId) return ok(false, 'step 4 needs task b from step 1 (with its session id)');
  const app = startApp(ctx.engine, s1.ws);
  ctx.apps.push(app);
  // main.js startTask for a task from an earlier session of O: re-create it with its saved Claude Code session
  const bId = s1.ids.b;
  app.tm.tasks.set(bId, { id: bId, title: PROMPTS.b.slice(0, 60), prompt: '', status: 'done', statusLine: 'Done', mood: 'idle', createdAt: s1.tasks.b.startedAt, startedAt: null, endedAt: s1.tasks.b.endedAt, source: 'user', scheduleId: null, goalId: null, sessionId: s1.tasks.b.sessionId, agent: null, result: '', error: '', runs: 0, tools: new Set() });
  const before = await ctx.engine.call('GET', '/learn/profile');
  const f = app.startTask({ prompt: 'next time put the total row at the top', taskId: bId });
  ok(f.ok && f.followUp, 'follow-up correction started in task b\'s own session', f);
  await app.waitEnd(bId, 10 * 60 * 1000);
  const fu = app.dumpTranscript(bId, 'step4-b-followup');
  const tools = toolsUsed(fu.rows);
  ok(tools.includes('mcp__o__remember_preference'), 'the follow-up called mcp__o__remember_preference', tools);
  let profile = null;
  const has = await until(async () => {
    profile = await ctx.engine.call('GET', '/learn/profile');
    return (profile.preferences || []).some((p) => /total/i.test(p.text || p) && /top/i.test(p.text || p));
  }, 20000, 1000);
  ok(has, 'GET /learn/profile lists the "total row at the top" preference', (profile && profile.preferences) || profile);
  // a NEW receipts task in a fresh workspace
  const ws = path.join(OUT, 'ws-learn');
  fs.rmSync(ws, { recursive: true, force: true });
  py(['receipts', ws, path.join(OUT, 'manifest-learn.json')]);
  app.settings.cwd = ws;
  const n = app.startTask({ prompt: PROMPTS.b });
  await app.waitEnd(n.taskId, 10 * 60 * 1000);
  const nt = app.dumpTranscript(n.taskId, 'step4-b-new');
  const xl = py(['check-xlsx', path.join(ws, 'expenses.xlsx'), path.join(OUT, 'manifest-learn.json')]);
  ok(toolsUsed(nt.rows).includes('mcp__o__recall_preferences'), 'the new receipts task recalled preferences', toolsUsed(nt.rows));
  ok(xl.ok, 'the new receipts task built correct per-category totals', xl);
  ok(xl.total_position === 'top', 'the learned preference was applied: the total row is above the category rows', { total_position: xl.total_position, grand_row: xl.grand_row, category_rows: xl.category_rows });
  state.step4 = { followupCost: costFor(bId) - (s1.tasks.b.costUsd || 0), newCost: costFor(n.taskId), profileBefore: (before.preferences || []).length, preferences: (profile && profile.preferences) || [], check: xl,
    followupWallMs: fu.task.endedAt - (fu.task.startedAt || fu.task.createdAt), newWallMs: nt.task.endedAt - nt.task.startedAt, step1Position: s1.tasks.b.check && s1.tasks.b.check.total_position };
}

async function step5(ctx, state) {
  // O's isolated display :99 with a session bus (AT-SPI) and a GTK text editor on it; recorded with ffmpeg.
  const display = ':99';
  const lock = '/tmp/.X99-lock';
  const target = path.join(process.env.HOME || '/root', 'lisbon.txt');
  fs.rmSync(target, { force: true });
  const procs = [];
  const bg = (cmd, args, env, name) => {
    const fd = fs.openSync(path.join(OUT, `${name}.log`), 'a');
    const p = spawn(cmd, args, { env: { ...process.env, ...env }, stdio: ['ignore', fd, fd], detached: true });
    procs.push(p); return p;
  };
  if (!fs.existsSync(lock)) bg('Xvfb', [display, '-screen', '0', '1280x800x24', '-nolisten', 'tcp'], {}, 'xvfb');
  ok(await until(() => fs.existsSync(lock), 10000), 'Xvfb :99 is up');
  const bus = execFileSync('dbus-daemon', ['--session', '--fork', '--print-address=1', '--print-pid=1'], { encoding: 'utf8', env: { ...process.env, DISPLAY: display } }).trim().split('\n');
  const busEnv = { DISPLAY: display, DBUS_SESSION_BUS_ADDRESS: bus[0], NO_AT_BRIDGE: '', GTK_A11Y: '', ACCESSIBILITY_ENABLED: '1' };
  delete process.env.NO_AT_BRIDGE;
  bg('/usr/libexec/at-spi-bus-launcher', ['--launch-immediately'], busEnv, 'atspi');
  await sleep(1500);
  // a fresh editor profile each run: no "restore the previous session?" dialog after an earlier run was killed
  const edHome = path.join(OUT, 'mousepad-profile');
  fs.rmSync(edHome, { recursive: true, force: true });
  fs.mkdirSync(edHome, { recursive: true });
  const editor = bg('mousepad', ['--disable-server'], { ...busEnv, GSETTINGS_BACKEND: 'memory', XDG_CONFIG_HOME: path.join(edHome, 'config'), XDG_DATA_HOME: path.join(edHome, 'data'), XDG_CACHE_HOME: path.join(edHome, 'cache') }, 'mousepad');
  await sleep(3000);
  ok(editor.exitCode == null, 'mousepad is running on :99', editor.exitCode);
  const rec = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-video_size', '1280x800', '-framerate', '10', '-f', 'x11grab', '-i', display, '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', path.join(OUT, 'cu.mp4')], { stdio: ['pipe', 'ignore', fs.openSync(path.join(OUT, 'ffmpeg.log'), 'a')] });
  // the engine for this step shares the editor's session bus (AT-SPI) and sees :99 as its isolated display
  await ctx.engine.stop();
  ctx.engine = await startEngine({ DBUS_SESSION_BUS_ADDRESS: busEnv.DBUS_SESSION_BUS_ADDRESS, O_CU_MAX_TURNS: process.env.O_E2E_CU_MAX_TURNS || '8', O_CU_MAX_HEALS: process.env.O_E2E_CU_MAX_HEALS || '3' });
  const tree = await ctx.engine.call('GET', '/computer/tree').catch((e) => ({ error: e.message }));
  const nodes = (tree && tree.nodes) || [];
  ok(nodes.length > 0, `GET /computer/tree reads the editor's accessibility tree (${nodes.length} nodes)`, tree.error || nodes.slice(0, 3));
  const ws = path.join(OUT, 'ws-cu');
  fs.mkdirSync(ws, { recursive: true });
  const app = startApp(ctx.engine, ws);
  ctx.apps.push(app);
  const t0 = now();
  const r = app.startTask({ prompt: 'In the editor on O\'s display, write a 3-line packing list for Lisbon and save it as ~/lisbon.txt' });
  await app.waitEnd(r.taskId, 15 * 60 * 1000);
  const { rows, task } = app.dumpTranscript(r.taskId, 'step5-cu');
  try { rec.stdin.write('q'); rec.stdin.end(); } catch (_) {}
  await Promise.race([new Promise((res) => rec.on('exit', res)), sleep(8000)]);
  const tools = toolsUsed(rows);
  const content = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : null;
  const jobs = fs.existsSync(path.join(ctx.engine.dataDir, 'engine-actions.jsonl')) ? fs.readFileSync(path.join(ctx.engine.dataDir, 'engine-actions.jsonl'), 'utf8').split('\n').filter((l) => /computer job/.test(l)).slice(-3) : [];
  ok(tools.includes('mcp__o__computer_run'), 'the task used the engine\'s computer_run bridge tool', tools);
  ok(content != null, '~/lisbon.txt exists', target);
  ok(content != null && content.trim().split(/\r?\n/).filter((l) => l.trim()).length === 3, '~/lisbon.txt has 3 non-empty lines', content);
  const mp4 = path.join(OUT, 'cu.mp4');
  ok(fs.existsSync(mp4) && fs.statSync(mp4).size > 10000, 'cu.mp4 recorded', fs.existsSync(mp4) ? fs.statSync(mp4).size : 0);
  state.step5 = { wallMs: task.endedAt - task.startedAt, totalMs: now() - t0, status: task.status, costUsd: costFor(r.taskId), engineCostUsd: costRows().filter((x) => x.caller === 'engine' && x.ts >= t0).reduce((a, x) => a + x.cost, 0), tools, content, final: finalText(rows).slice(0, 800), engineJobs: jobs };
  await ctx.engine.stop(); // before the display goes: a computer job still running must not see :99 vanish
  ctx.engine = await startEngine();
  for (const p of procs) { try { process.kill(-p.pid, 'SIGTERM'); } catch (_) {} }
  try { process.kill(Number(bus[1]), 'SIGTERM'); } catch (_) {}
}

async function step6(ctx, state) {
  const ws = path.join(OUT, 'ws-goal');
  fs.rmSync(ws, { recursive: true, force: true });
  py(['notes', ws]);
  const app = startApp(ctx.engine, ws);
  ctx.apps.push(app);
  const g = await ctx.engine.call('POST', '/goals', { text: 'Keep ./notes/summary.md updated with a summary of every file in ./notes' });
  ok(g && g.ok && g.goal && g.goal.id, 'POST /goals created the goal', g && g.goal && { id: g.goal.id, title: g.goal.title, cadence: g.goal.cadence, horizon_days: g.goal.horizon_days });
  const gid = g.goal.id;
  await app.goals.refresh();
  ok(!!app.scheduler.forGoal(gid), 'the app gave the goal a schedule (GoalRunner.refresh)', app.scheduler.forGoal(gid));
  await until(() => app.ambient.reachable, 5000);
  const sim = await ctx.engine.call('POST', '/presence/simulate', { state: 'USER_ABSENT', reason: 'e2e real run' });
  const awayStart = sim.presence.absent_since;
  ok(await until(() => app.ambient.known === 'USER_ABSENT' && app.tm.presence === 'absent', 5000), 'the app sees USER_ABSENT');
  const t0 = now();
  const fire = await app.goals.fireCycle(gid, { reason: 'e2e' });
  ok(fire && fire.ok, 'one goal cycle fired as a background task', fire);
  await app.waitEnd(fire.taskId, 10 * 60 * 1000);
  const { rows, task } = app.dumpTranscript(fire.taskId, 'step6-goal-cycle');
  ok(task.status === 'done', 'the goal cycle task finished', task.status + ' ' + (task.error || ''));
  const summary = path.join(ws, 'notes', 'summary.md');
  const sumText = fs.existsSync(summary) ? fs.readFileSync(summary, 'utf8') : '';
  ok(/standup/i.test(sumText) && /ideas/i.test(sumText) && /reading/i.test(sumText), 'notes/summary.md summarizes all three notes', sumText.slice(0, 400));
  await until(() => app.ambient.milestonesPosted >= 1, 10000);
  const det = await until(async () => ((await ctx.engine.call('GET', `/goals/${gid}`)).cycles || []).length > 0, 15000, 1000);
  const detail = await ctx.engine.call('GET', `/goals/${gid}`);
  ok(det, 'the cycle record reached the engine (GET /goals/<id> cycles)', (detail.cycles || []).slice(-1));
  await ctx.engine.call('POST', '/presence/simulate', { state: 'USER_PRESENT', reason: 'e2e real run' });
  ok(await until(() => app.ambient.known === 'USER_PRESENT', 5000), 'the app sees USER_PRESENT');
  const ms = (await ctx.engine.call('GET', `/milestones?since=${awayStart}`)).milestones || [];
  ok(ms.some((m) => m.task_id === fire.taskId), 'GET /milestones has the goal cycle\'s milestone', ms);
  let brief = null;
  await until(async () => { const b = await ctx.engine.call('GET', '/brief/latest'); brief = b && b.brief; return brief && Number(brief.since) >= awayStart - 2; }, 20000, 1000);
  const sentences = brief && Array.isArray(brief.sentences) ? brief.sentences : [];
  ok(sentences.length === 3, 'GET /brief/latest has 3 sentences for this time away', brief);
  ok(await until(() => app.briefs.length > 0, 15000), 'the app fetched the brief on return (AmbientController.welcomeBack)', app.briefs);
  state.step6 = { goalId: gid, wallMs: task.endedAt - task.startedAt, costUsd: costFor(fire.taskId), summary: sumText.slice(0, 1200), milestones: ms, brief, appBrief: app.briefs[0] || null, cycles: (detail.cycles || []).slice(-1), final: finalText(rows).slice(0, 800), totalMs: now() - t0 };
}

(async () => {
  const steps = process.argv.slice(2).map(Number).filter(Boolean);
  if (!steps.length) { console.log('usage: real-run.js <step...>  (1..6)'); process.exit(2); }
  const state = loadState();
  const ctx = { engine: await startEngine(), apps: [] };
  console.log(`engine up on :${ctx.engine.port}; output in ${OUT}`);
  const fns = { 1: step1, 2: step2, 3: step3, 4: step4, 5: step5, 6: step6 };
  try {
    for (const n of steps) {
      if (!budgetGuard(`step ${n}`)) break;
      const t0 = now();
      try { await fns[n](ctx, state); } catch (e) { ok(false, `step ${n} threw`, e.stack); }
      console.log(`-- step ${n} took ${((now() - t0) / 1000).toFixed(1)} s; metered total $${spent().toFixed(3)}`);
      state.results = { ...(state.results || {}), [n]: results.filter((r) => r.step === undefined).map((r) => ({ ...r })) };
      results.forEach((r) => { r.step = n; });
      saveState(state);
    }
  } finally {
    for (const a of ctx.apps) a.dispose();
    await ctx.engine.stop();
    state.spentUsd = spent();
    saveState(state);
    console.log(`metered total $${spent().toFixed(3)} over ${costRows().length} Claude Code runs`);
    process.exit(results.some((r) => !r.ok) ? 1 : 0);
  }
})();
