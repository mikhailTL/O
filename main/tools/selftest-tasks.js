#!/usr/bin/env node
'use strict';
// Self-test for the task pool (main/tasks.js), the scheduler (main/scheduler.js) and the bridge's
// schedule / preference tools. Uses fake-claude.js (FAKE_SIMPLE mode): no network, no login.
// Run: node main/tools/selftest-tasks.js   (exit 1 on any failure)

process.env.FAKE_SIMPLE = '1';
process.env.FAKE_DELAY_MS = '20';

const http = require('http');
const os = require('os');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { OAgent } = require('../agent');
const { TaskManager } = require('../tasks');
const { Scheduler, normalizeWhen, nextRunAfter, describeWhen, whenFrom } = require('../scheduler');
const { Settings } = require('../settings');

const FAKE = path.join(__dirname, 'fake-claude.js');
const BRIDGE = path.join(__dirname, 'bridge-mcp.js');
fs.chmodSync(FAKE, 0o755);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'o-tasks-'));
const argsLog = path.join(tmp, 'args.jsonl');
process.env.FAKE_ARGS_LOG = argsLog;

let failures = 0;
let passed = 0;
function ok(cond, label) {
  if (cond) { passed++; console.log('ok   ' + label); } else { failures++; console.log('FAIL ' + label); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 8000, step = 25) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (fn()) return true; await sleep(step); }
  return !!fn();
}
const readArgs = () => { try { return fs.readFileSync(argsLog, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)); } catch (_) { return []; } };

const entries = [];
const actionLog = { record(e) { entries.push(e); return e; }, ingest(e) { entries.push(e); return e; } };
let settings = { claudePath: FAKE, cwd: tmp, permissionMode: 'log', maxParallelTasks: 2 };

function makeManager(extra = {}) {
  const tm = new TaskManager({
    getSettings: () => settings,
    createAgent: (taskId) => new OAgent({ userDataDir: tmp, getSettings: () => settings, actionLog, taskId, ...extra }),
  });
  const events = [];
  const ends = [];
  tm.on('event', (e) => events.push(e));
  tm.on('end', (e) => ends.push(e));
  return { tm, events, ends };
}

(async () => {
  // ------------------------------------------------------------ settings
  {
    const s = new Settings(fs.mkdtempSync(path.join(os.tmpdir(), 'o-tasks-set-')));
    ok(s.get().maxParallelTasks === 3, 'maxParallelTasks defaults to 3');
    ok(s.set({ maxParallelTasks: '99' }).maxParallelTasks === 8 && s.set({ maxParallelTasks: 0 }).maxParallelTasks === 1 && s.set({ maxParallelTasks: 'x' }).maxParallelTasks === 1, 'maxParallelTasks is sanitized to 1..8');
  }

  // ------------------------------------------------------------ two tasks at once, stop one
  {
    const { tm, events, ends } = makeManager();
    const a = tm.start({ prompt: 'hang alpha' });
    const b = tm.start({ prompt: 'hang bravo' });
    ok(a.ok && b.ok && a.taskId !== b.taskId && !a.queued && !b.queued, 'two tasks start without queueing (max 2)');
    const bothRunning = await until(() => events.some((e) => e.taskId === a.taskId && e.type === 'tool') && events.some((e) => e.taskId === b.taskId && e.type === 'tool'));
    ok(bothRunning, 'both tasks stream their own events, tagged with taskId');
    ok(tm.running().length === 2, 'two tasks running concurrently');
    const pids = new Set(readArgs().filter((r) => /hang (alpha|bravo)/.test(r.prompt)).map((r) => r.pid));
    ok(pids.size === 2, `each task has its own Claude Code process (pids ${[...pids].join(',')})`);
    const mcpFiles = fs.readdirSync(tmp).filter((f) => /^o-mcp-t/.test(f));
    ok(mcpFiles.length === 2, `each task writes its own MCP config (${mcpFiles.join(', ')})`);
    ok(tm.get(a.taskId).statusLine && /hang alpha|Searching|alpha/i.test(tm.get(a.taskId).statusLine), 'task keeps a live status line: ' + JSON.stringify(tm.get(a.taskId).statusLine));

    // queue beyond max
    const c = tm.start({ prompt: 'charlie says hi' });
    ok(c.ok && c.queued && c.position === 1 && tm.get(c.taskId).status === 'queued', 'third task waits in the queue at position 1');
    ok(tm.list()[0].id === c.taskId && tm.list().find((t) => t.id === c.taskId).position === 1, 'list() shows the queued task with its position');

    // stop one, the other keeps running, the queued one gets the slot
    tm.stop(a.taskId);
    await until(() => tm.get(a.taskId).status === 'stopped');
    ok(tm.get(a.taskId).status === 'stopped', 'stopped task is marked stopped');
    ok(tm.get(b.taskId).status === 'running', 'stopping one task leaves the other running');
    const cDone = await until(() => tm.get(c.taskId).status === 'done');
    ok(cDone && tm.get(c.taskId).result === 'You said: charlie says hi', 'queued task ran when a slot freed and finished');
    ok(tm.get(b.taskId).status === 'running', 'still running after the queued task finished');
    ok(events.some((e) => e.taskId === a.taskId && e.type === 'done' && (e.stopped || e.result === '(stopped)')), 'stopped task emits its own done(stopped) event');
    ok(!events.some((e) => e.taskId === b.taskId && e.type === 'done'), 'no done event leaked to the running task');

    // stop a queued task
    tm.start({ prompt: 'hang delta' }); // takes the free slot
    const e2 = tm.start({ prompt: 'echo queued' });
    ok(e2.queued, 'fourth task queued again');
    tm.stop(e2.taskId);
    ok(tm.get(e2.taskId).status === 'stopped' && !tm.queue.includes(e2.taskId), 'stopping a queued task removes it from the queue');

    // follow-up in an ended task resumes its session
    const f = tm.start({ prompt: 'again please', taskId: c.taskId });
    ok(f.followUp && f.taskId === c.taskId, 'a prompt for an ended task is a follow-up in the same task');
    tm.stopAll();
    await until(() => tm.activeCount() === 0);
    ok(tm.activeCount() === 0, 'stopAll ends every task ' + JSON.stringify(tm.list().map((t) => [t.prompt, t.status])));
    ok(ends.some((x) => x.task.id === c.taskId && x.ok), 'end event for a finished task (ok)');

    // action log entries carry taskId
    const tagged = entries.filter((x) => x.detail && x.detail.taskId);
    const ids = new Set(tagged.map((x) => x.detail.taskId));
    ok(ids.has(a.taskId) && ids.has(b.taskId) && ids.has(c.taskId), 'every task\'s action-log entries carry detail.taskId');
    ok(entries.filter((x) => x.kind === 'task').every((x) => x.detail && x.detail.taskId), 'every task entry is tagged');
    tm.dispose();
    await sleep(200);
    ok(fs.readdirSync(tmp).filter((f2) => /^o-mcp-t/.test(f2)).length === 0, 'task MCP config files are removed when tasks end');
  }

  // ------------------------------------------------------------ stop right after start (before the process exists)
  {
    const { tm, events } = makeManager();
    const r = tm.start({ prompt: 'hang instantly stopped' });
    tm.stop(r.taskId);
    const ended = await until(() => tm.get(r.taskId).status === 'stopped', 4000);
    await sleep(300);
    ok(ended && tm.running().length === 0 && events.some((e) => e.taskId === r.taskId && e.type === 'done'), 'a task stopped before its process started never runs');
    tm.dispose();
  }

  // ------------------------------------------------------------ ask mode: approvals stay per task, payment-free flow unchanged
  {
    delete process.env.FAKE_SIMPLE;
    settings = { ...settings, permissionMode: 'ask' };
    const asked = [];
    const tm = new TaskManager({
      getSettings: () => settings,
      createAgent: (taskId) => new OAgent({ userDataDir: tmp, getSettings: () => settings, actionLog, taskId,
        onPermissionRequest: (req) => { asked.push({ ...req, taskId }); return Promise.resolve({ allow: req.toolName === 'Bash' && taskId === tm._allowFor }); } }),
    });
    const evs = [];
    tm.on('event', (e) => evs.push(e));
    const x = tm.start({ prompt: 'task x' });
    tm._allowFor = x.taskId;
    const y = tm.start({ prompt: 'task y' });
    await until(() => ['done', 'failed'].includes(tm.get(x.taskId).status) && ['done', 'failed'].includes(tm.get(y.taskId).status), 15000);
    const px = JSON.parse(tm.get(x.taskId).result.slice(6) || '{}'), py = JSON.parse(tm.get(y.taskId).result.slice(6) || '{}');
    ok(asked.filter((a) => a.toolName === 'Bash').length === 2 && new Set(asked.map((a) => a.taskId)).size === 2, 'ask mode: each task asks for its own Bash approval');
    ok(px.Bash && px.Bash.behavior === 'allow' && py.Bash && py.Bash.behavior === 'deny', 'an approval answers only its own task (x allowed, y denied)');
    ok(px.mcp__playwright__browser_click.behavior === 'allow', 'browser tools stay pre-approved in ask mode');
    ok(evs.filter((e) => e.type === 'permission').every((e) => e.taskId), 'permission events carry taskId');
    const policy = entries.filter((e) => e.kind === 'policy' && e.detail && (e.detail.taskId === x.taskId || e.detail.taskId === y.taskId));
    ok(policy.some((e) => /^approved Bash/.test(e.summary)) && policy.some((e) => /^denied Bash/.test(e.summary)), 'policy decisions are logged per task');
    const args = readArgs();
    ok(!fs.readFileSync(path.join(__dirname, '..', 'agent.js'), 'utf8').includes("'--dangerously-skip-permissions'"), 'agent never passes --dangerously-skip-permissions');
    ok(args.length > 0, 'fake CLI runs were recorded');
    tm.dispose();
    settings = { ...settings, permissionMode: 'log' };
    process.env.FAKE_SIMPLE = '1';
  }

  // ------------------------------------------------------------ prewarm
  {
    const { tm } = makeManager();
    const warmed = await tm.prewarm();
    ok(warmed === true, 'prewarm starts an idle Claude Code process');
    await sleep(400); // the prompt arrives well after the process started
    const before = readArgs().length;
    const r = tm.start({ prompt: 'warm start' });
    await until(() => tm.get(r.taskId).status === 'done');
    const t = tm.get(r.taskId);
    ok(t.status === 'done' && t.warm === true, 'the next new task adopted the pre-warmed process');
    const rec = readArgs().slice(before).find((x) => x.prompt === 'warm start');
    ok(rec && rec.promptAt - rec.spawnedAt >= 300, `warm process was spawned before its prompt (${rec ? rec.promptAt - rec.spawnedAt : '?'} ms earlier)`);
    await sleep(300);
    ok(!!tm._spare, 'a new spare is warmed after the warm one is used');
    // a settings change makes the warm process stale: it is replaced, not reused
    await tm.prewarm();
    settings = { ...settings, model: 'other-model' };
    const r2 = tm.start({ prompt: 'cold start' });
    await until(() => tm.get(r2.taskId).status === 'done');
    ok(tm.get(r2.taskId).status === 'done' && tm.get(r2.taskId).warm === false, 'a stale warm process (settings changed) is not used');
    settings = { ...settings, model: '' };
    tm.dispose();
  }

  // ------------------------------------------------------------ scheduler (fake clock)
  {
    let now = new Date(2026, 10, 9, 7, 59, 0).getTime(); // Monday 9 Nov 2026, 07:59 local
    const file = path.join(tmp, 'o-schedules.json');
    const fired = [];
    const { tm } = makeManager();
    const sch = new Scheduler({ file, now: () => now, onFire: (s) => { fired.push(s); tm.start({ prompt: s.prompt, title: s.title, source: 'schedule', scheduleId: s.id }); } });
    const daily = sch.add({ prompt: 'Summarize what is new in my inbox', when: { type: 'daily', time: '08:00' } });
    ok(daily.nextRun === new Date(2026, 10, 9, 8, 0).getTime() && daily.label === 'Every day at 08:00', 'daily 08:00 next run is today 08:00');
    const wk = sch.add({ prompt: 'weekday digest', when: { type: 'weekly', days: ['mon', 'tue', 'wed', 'thu', 'fri'], time: '08:00' } });
    ok(wk.label === 'Every weekday at 08:00', 'weekly label reads "Every weekday at 08:00"');
    const every = sch.add({ prompt: 'check prices', when: { type: 'every', hours: 3 } });
    ok(every.nextRun === now + 3 * 3600 * 1000, 'every 3 hours: first run 3 hours from now');
    const once = sch.add({ prompt: 'remind me', when: { type: 'once', at: new Date(2026, 10, 9, 8, 0, 30).toISOString() } });
    ok(sch.list().length === 4, 'four schedules saved');
    ok(sch.tick().length === 0 && fired.length === 0, 'nothing fires before 08:00');
    now = new Date(2026, 10, 9, 8, 1, 0).getTime();
    const f1 = sch.tick();
    ok(f1.length === 3 && fired.length === 3, `due schedules fire once at 08:01 (${f1.map((x) => x.prompt).join(' | ')})`);
    ok(tm.list().filter((t) => t.source === 'schedule').length === 3, 'each firing starts a task (source: schedule)');
    ok(sch.tick().length === 0, 'a second tick in the same minute fires nothing');
    ok(sch.get(daily.id).nextRun === new Date(2026, 10, 10, 8, 0).getTime(), 'daily moves to tomorrow 08:00');
    ok(!sch.get(once.id), 'a once schedule is gone after it fires');
    now = new Date(2026, 10, 13, 8, 5).getTime(); // Friday
    sch.tick();
    ok(sch.get(wk.id).nextRun === new Date(2026, 10, 16, 8, 0).getTime(), 'weekday schedule skips the weekend (Fri → Mon)');
    const reloaded = new Scheduler({ file, now: () => now });
    ok(reloaded.list().length === 3 && reloaded.get(daily.id).prompt === daily.prompt, 'schedules persist to JSON and reload');
    ok(sch.cancel(every.id) && !sch.get(every.id) && new Scheduler({ file, now: () => now }).list().length === 2, 'cancel removes and persists');
    let threw = 0;
    for (const bad of [null, { type: 'hourly' }, { type: 'daily', time: '25:00' }, { type: 'daily' }, { type: 'weekly', time: '08:00' }, { type: 'weekly', days: ['funday'], time: '08:00' }, { type: 'every', hours: 0 }, { type: 'once', at: 'soon' }, { type: 'once', at: new Date(now - 3600e3).toISOString() }]) {
      try { normalizeWhen(bad, now); } catch (_) { threw++; }
    }
    ok(threw === 9, 'normalizeWhen rejects every invalid when (9/9)');
    ok(nextRunAfter({ type: 'every', hours: 2 }, 1000 + 5 * 3600e3, 1000) === 1000 + 6 * 3600e3, 'every: catches up on the interval grid');
    ok(describeWhen({ type: 'weekly', days: [0, 6], time: '10:00' }) === 'Every weekend day at 10:00', 'weekend label');
    ok(describeWhen(whenFrom({ cron: '0 8 * * 1-5' })) === 'Every weekday at 08:00' && describeWhen(whenFrom({ cron: '0 */3 * * *' })) === 'Every 3 hours' &&
      describeWhen(whenFrom({ cron: '30 7 * * *' })) === 'Every day at 07:30' && describeWhen(whenFrom({ when: { type: 'daily', time: '06:00' }, cron: 'junk' })) === 'Every day at 06:00', 'engine suggestions: structured when, else a simple cron');
    let monthly = false; try { whenFrom({ cron: '0 8 1 * *' }); } catch (_) { monthly = true; }
    ok(monthly, 'unsupported cron (monthly) is refused, not guessed');
    ok(sch.handle({ op: 'add', prompt: '', when: { type: 'daily', time: '08:00' } }).ok === false, 'handle(add) with an empty prompt fails');
    tm.stopAll(); tm.dispose();
  }

  // ------------------------------------------------------------ bridge schedule + preference tools
  {
    let now = Date.now();
    const sch = new Scheduler({ file: path.join(tmp, 'o-sched-bridge.json'), now: () => now });
    const seenTask = [];
    const agent = new OAgent({ userDataDir: tmp, getSettings: () => settings, actionLog, taskId: 'tbridge1', onSchedule: (b) => { seenTask.push(b.taskId); return sch.handle(b); } });
    await agent.ensureServer();
    const base = `http://127.0.0.1:${agent._port}`;
    // engine stub for /learn
    const learn = { gets: 0, observed: [] };
    const eng = http.createServer((req, res) => {
      let d = ''; req.on('data', (c) => { d += c; });
      req.on('end', () => {
        const send = (o) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(o)); };
        if (req.headers['x-o-token'] !== 'etok') { res.writeHead(401); return res.end('{}'); }
        if (req.url.startsWith('/learn/profile')) { learn.gets++; return send({ preferences: [{ id: 'p1', text: 'Prefers aisle seats' }], habits: [], app_skills: [] }); }
        if (req.url === '/learn/observe') { learn.observed.push(JSON.parse(d)); return send({ ok: true }); }
        res.writeHead(404); res.end('{}');
      });
    });
    await new Promise((r) => eng.listen(0, '127.0.0.1', r));
    const child = spawn(process.execPath, [BRIDGE], {
      env: { ...process.env, O_SCHEDULE_URL: base + '/schedule', O_PERM_TOKEN: agent._token, O_TASK_ID: 'tbridge1', O_ENGINE_URL: `http://127.0.0.1:${eng.address().port}`, O_ENGINE_TOKEN: 'etok', O_PERMISSION_MODE: 'log' },
      stdio: ['pipe', 'pipe', 'inherit'],
    });
    let buf = ''; const waiting = new Map(); let next = 1;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (d) => { buf += d; let i; while ((i = buf.indexOf('\n')) >= 0) { const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); const w = waiting.get(m.id); if (w) { waiting.delete(m.id); w(m); } } });
    const rpc = (method, params) => new Promise((res) => { const id = next++; waiting.set(id, res); child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); });
    const call = async (name, args) => { const r = await rpc('tools/call', { name, arguments: args }); return { text: r.result.content[0].text, isError: !!r.result.isError }; };
    await rpc('initialize', { protocolVersion: '2024-11-05' });
    const tools = (await rpc('tools/list', {})).result.tools;
    const st = tools.find((t) => t.name === 'schedule_task');
    ok(st && st.inputSchema.additionalProperties === false && JSON.stringify(st.inputSchema.required) === '["prompt","when"]' && st.inputSchema.properties.when.properties.type.enum.join() === 'once,daily,weekly,every', 'schedule_task inputSchema is strict {prompt, when{type…}, title?}');
    ok(['list_schedules', 'cancel_schedule', 'recall_preferences', 'remember_preference'].every((n) => tools.some((t) => t.name === n)), 'list_schedules, cancel_schedule, recall_preferences, remember_preference listed');
    const bads = [
      {}, { prompt: 'x' }, { prompt: '', when: { type: 'daily', time: '08:00' } }, { prompt: 'x', when: { type: 'daily', time: '8am' } },
      { prompt: 'x', when: { type: 'weekly', time: '08:00' } }, { prompt: 'x', when: { type: 'every', hours: 0.5 } }, { prompt: 'x', when: { type: 'once', at: 'tomorrow' } },
      { prompt: 'x', when: { type: 'daily', time: '08:00', cron: '* * *' } }, { prompt: 'x', when: { type: 'daily', time: '08:00' }, extra: 1 },
    ];
    let rejected = 0;
    for (const b of bads) { const r = await call('schedule_task', b); if (r.isError && /^schedule_task: /.test(r.text)) rejected++; }
    ok(rejected === bads.length && sch.list().length === 0, `bridge rejects every invalid schedule_task before it reaches O (${rejected}/${bads.length})`);
    const add = await call('schedule_task', { prompt: 'Summarize what is new in my inbox', when: { type: 'weekly', days: ['mon'], time: '08:00' }, title: 'Monday inbox' });
    const saved = !add.isError && JSON.parse(add.text).schedule;
    ok(saved && saved.title === 'Monday inbox' && saved.label === 'Every Mon at 08:00' && sch.list().length === 1, 'valid schedule_task is saved: ' + (saved && saved.label));
    ok(seenTask[seenTask.length - 1] === 'tbridge1', 'the schedule request carries the task id');
    const list = await call('list_schedules', {});
    ok(!list.isError && JSON.parse(list.text).schedules.length === 1, 'list_schedules returns the saved schedule');
    const cbad = await call('cancel_schedule', { id: 'nope' });
    ok(cbad.isError && /no schedule/.test(cbad.text), 'cancel_schedule with an unknown id is an error');
    const cok = await call('cancel_schedule', { id: saved.id });
    ok(!cok.isError && sch.list().length === 0, 'cancel_schedule removes it');
    const r1 = await call('recall_preferences', {});
    const r2 = await call('recall_preferences', {});
    ok(!r1.isError && /aisle seats/.test(r1.text) && r1.text === r2.text && learn.gets === 1, `recall_preferences → GET /learn/profile, cached (gets=${learn.gets})`);
    const rem = await call('remember_preference', { text: 'Name photos YYYY-MM-DD' });
    ok(!rem.isError && learn.observed.length === 1 && learn.observed[0].source === 'correction' && learn.observed[0].task_id === 'tbridge1' && learn.observed[0].text === 'Name photos YYYY-MM-DD', 'remember_preference → POST /learn/observe {source:"correction", task_id, text}');
    await call('recall_preferences', {});
    ok(learn.gets === 2, 'remembering clears the preference cache');
    // regression (real run, Claude Code 2.1.318 defers MCP schemas): the model called {preference: ...} first
    const alias = await call('remember_preference', { preference: 'Put the total row at the top' });
    ok(!alias.isError && learn.observed.length === 2 && learn.observed[1].text === 'Put the total row at the top', 'remember_preference also takes the sentence as {preference}');
    const empty = await call('remember_preference', { preference: '  ' });
    ok(empty.isError && /"text"/.test(empty.text) && learn.observed.length === 2, 'remember_preference with no sentence is an error that names the text field');
    // wrong token on /schedule is refused
    const bad = await new Promise((resolve) => {
      const data = JSON.stringify({ op: 'list' });
      const rq = http.request({ host: '127.0.0.1', port: agent._port, path: '/schedule', method: 'POST', headers: { 'x-o-token': 'wrong', 'content-type': 'application/json', 'content-length': data.length } }, (res) => resolve(res.statusCode));
      rq.end(data);
    });
    ok(bad === 403, 'POST /schedule with a wrong token → 403');
    child.stdin.end();
    eng.close();
    agent.dispose();
  }

  // ------------------------------------------------------------ persona
  {
    const { buildPersona } = require('../agent');
    const p = buildPersona({});
    ok(!/can't click or type on their screen/i.test(p) && /computer_run/.test(p) && /never touched/.test(p), 'persona: no "can\'t click", computer use on an isolated display');
    ok(/do not ask clarifying questions when a reasonable default exists/.test(p) && /Assumed:/.test(p), 'persona: act first, state the assumption');
    ok(/recall_preferences/.test(p) && /remember_preference/.test(p) && /schedule_task/.test(p), 'persona: preferences and schedules');
  }

  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
  console.log(failures ? `\nselftest-tasks: ${failures} FAILED, ${passed} passed` : `\nselftest-tasks: all ${passed} checks passed`);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error('selftest-tasks crashed:', e && e.stack || e); process.exit(1); });
