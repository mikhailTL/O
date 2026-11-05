#!/usr/bin/env node
'use strict';
// Self-test for long-term goals, end to end against the real Python engine (engine/o_engine/goals.py):
// create -> one schedule at the goal's cadence -> a cycle task (fake-claude.js FAKE_GOAL through the real
// OAgent + bridge) -> its JSON cycle record is posted -> the checkpoint card it filed waits for approval ->
// publishing is blocked without approval, runs exactly once after it, and spending / fake engagement never run.
// Run: node main/tools/selftest-goals.js   (exit 1 on any failure)

process.env.FAKE_GOAL = '1';

const http = require('http');
const os = require('os');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { spawn } = require('child_process');
const { OAgent } = require('../agent');
const { TaskManager } = require('../tasks');
const { Scheduler } = require('../scheduler');
const { GoalRunner, parseCycleRecord, whenFor, cardTitle } = require('../goals');
const { classifyAction } = require('./checkpoints');
const { freePort } = require('../engine');

const ROOT = path.join(__dirname, '..', '..');
const FAKE = path.join(__dirname, 'fake-claude.js');
fs.chmodSync(FAKE, 0o755);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'o-goals-'));

let failures = 0;
let passed = 0;
function ok(cond, label) {
  if (cond) { passed++; console.log('ok   ' + label); } else { failures++; console.log('FAIL ' + label); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 15000, step = 50) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(step); }
  return !!(await fn());
}

// ------------------------------------------------------------ a fake "youtube" MCP connector for the engine
const ytCalls = path.join(tmp, 'yt-calls.jsonl');
const ytServer = path.join(tmp, 'fake-youtube-mcp.js');
fs.writeFileSync(ytServer, `
const fs = require('fs');
let buf = '';
const send = (m) => process.stdout.write(JSON.stringify(m) + '\\n');
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => {
  buf += d; let i;
  while ((i = buf.indexOf('\\n')) >= 0) {
    const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
    if (!line) continue;
    const m = JSON.parse(line);
    if (m.id === undefined) continue;
    if (m.method === 'initialize') send({ jsonrpc: '2.0', id: m.id, result: { protocolVersion: m.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: 'youtube', version: '0' } } });
    else if (m.method === 'tools/list') send({ jsonrpc: '2.0', id: m.id, result: { tools: [{ name: 'videos_insert', description: 'Upload a video', inputSchema: { type: 'object', properties: { title: { type: 'string' }, privacyStatus: { type: 'string' } }, required: ['title'] } }] } });
    else if (m.method === 'tools/call') { fs.appendFileSync(${JSON.stringify(ytCalls)}, JSON.stringify(m.params) + '\\n'); send({ jsonrpc: '2.0', id: m.id, result: { content: [{ type: 'text', text: 'uploaded video id vid123' }] } }); }
    else send({ jsonrpc: '2.0', id: m.id, result: {} });
  }
});
`);
const dataDir = path.join(tmp, 'engine-data');
fs.mkdirSync(dataDir, { recursive: true });
fs.writeFileSync(path.join(dataDir, 'mcp-servers.json'), JSON.stringify({ mcpServers: { youtube: { command: process.execPath, args: [ytServer] } } }));
const readYt = () => { try { return fs.readFileSync(ytCalls, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)); } catch (_) { return []; } };

// ------------------------------------------------------------ the real engine
const TOKEN = crypto.randomBytes(16).toString('hex');
let enginePort = 0;
let engineChild = null;
function engineCall(method, p, body, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const data = body === undefined || body === null ? null : JSON.stringify(body);
    const req = http.request({ hostname: '127.0.0.1', port: enginePort, path: p, method, timeout: timeoutMs,
      headers: { 'x-o-token': TOKEN, ...(data ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) } : {}) } }, (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { raw += c; });
      res.on('end', () => {
        let j; try { j = raw ? JSON.parse(raw) : {}; } catch (_) { j = { raw }; }
        if (res.statusCode >= 400) return reject(new Error(`HTTP ${res.statusCode}: ${(j && j.detail) || raw}`));
        resolve(j);
      });
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    req.end(data || undefined);
  });
}

async function startEngine() {
  enginePort = await freePort();
  const py = process.env.O_PYTHON || 'python3';
  engineChild = spawn(py, ['-m', 'o_engine', 'serve', '--port', String(enginePort), '--data-dir', dataDir], {
    cwd: path.join(ROOT, 'engine'),
    env: { ...process.env, PYTHONPATH: path.join(ROOT, 'engine'), O_ENGINE_TOKEN: TOKEN, O_VLLM_URL: 'http://127.0.0.1:1/v1', O_LEARNING_MODEL: '0', O_SCREENPIPE_DB: path.join(tmp, 'none.db'), O_SANDBOX_ROOT: path.join(tmp, 'sandbox') },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  let err = '';
  engineChild.stderr.on('data', (d) => { err += d; });
  const up = await until(async () => { try { await engineCall('GET', '/health', null, 1000); return true; } catch (_) { return false; } }, 30000, 200);
  if (!up) { console.log(err.slice(-2000)); throw new Error('engine did not start'); }
}

(async () => {
  // ------------------------------------------------------------ pure helpers
  {
    const rec = parseCycleRecord('Done.\n```json\n{"plan":["a"],"did":["b"],"reflection":"r","next":"n"}\n```');
    ok(rec && rec.plan[0] === 'a', 'parseCycleRecord reads the ```json block');
    ok(parseCycleRecord('text {"reflection":"x","plan":[]} trailing').reflection === 'x', 'parseCycleRecord falls back to the last {...}');
    ok(parseCycleRecord('no record here') === null, 'parseCycleRecord: none -> null');
    ok(JSON.stringify(whenFor({ cadence: 'weekdays', time: '07:30' })) === JSON.stringify({ type: 'weekly', days: ['mon', 'tue', 'wed', 'thu', 'fri'], time: '07:30' })
      && whenFor({ cadence: 'every_n_hours', every_hours: 6 }).hours === 6 && whenFor({ cadence: 'daily' }).time === '09:00', 'whenFor maps cadence to a scheduler when');
    ok(cardTitle({ kind: 'publish', items: { type: 'video', title: 'X' } }) === 'Approve video ‘X’', 'cardTitle: "Approve video ‘X’"');
    const k = (n, i) => (classifyAction(n, i) || {}).kind || null;
    ok(k('mcp__youtube__videos_insert', {}) === 'publish' && k('mcp__youtube__videos_list', {}) === null, 'classifier: upload is publish, list is not');
    ok(k('mcp__gmail__send_message', {}) === 'send_message' && k('mcp__gmail__create_draft', {}) === null, 'classifier: send is send_message, a draft is not');
    ok(k('mcp__o__web_run', { task: 'buy 1000 youtube views' }) === 'forbidden' && k('mcp__smm__buy_views', {}) === 'forbidden' && k('mcp__youtube__analytics_query', { metrics: 'views' }) === null, 'classifier: bought views are forbidden, reading views is not');
    ok(k('mcp__stripe__create_payment_intent', {}) === 'spend' && k('mcp__youtube__channels_insert', {}) === 'create_account', 'classifier: payments spend, new channels create_account');
    ok(k('Bash', { command: 'rm -rf ~/Videos/old' }) === 'delete_data' && k('Bash', { command: 'rm -rf /tmp/render' }) === null, 'classifier: deleting home files is delete_data, temp files are not');
  }

  await startEngine();
  ok(true, `real engine up on port ${enginePort}`);

  // ------------------------------------------------------------ wiring like main.js
  const entries = [];
  const actionLog = { record(e) { entries.push(e); return e; }, ingest(e) { entries.push(e); return e; } };
  const logApp = (kind, summary, detail) => actionLog.record({ kind, summary, detail: detail || {} });
  const settings = { claudePath: FAKE, cwd: tmp, permissionMode: 'log', maxParallelTasks: 3 };
  let goals = null;
  const scheduler = new Scheduler({ file: path.join(tmp, 'o-schedules.json'), onFire: (s) => { if (s.goalId) goals.fireCycle(s.goalId); } });
  const tasks = new TaskManager({
    getSettings: () => settings,
    createAgent: (taskId, opts) => new OAgent({
      userDataDir: tmp, getSettings: () => settings, actionLog, taskId,
      goalId: (opts && opts.goalId) || (goals && goals.goalForTask(taskId)) || null,
      onGoal: (tid, body) => goals.handle(tid, body),
      getEngine: () => ({ url: `http://127.0.0.1:${enginePort}`, token: TOKEN }),
    }),
  });
  const startTask = (t) => tasks.start(t);
  goals = new GoalRunner({ engineCall, scheduler, startTask, getTasks: () => tasks.list(), file: path.join(tmp, 'o-goals.json'), log: logApp });
  const ends = [];
  tasks.on('end', (e) => { ends.push(e); goals.onTaskEnd(e); });

  // ------------------------------------------------------------ create -> schedule -> first cycle
  const created = await goals.create('Grow a YouTube channel about desk setups to monetization');
  ok(created.ok && /^g-[0-9a-f]+$/.test(created.goal.id), 'goal created through the engine: ' + (created.goal && created.goal.id));
  const gid = created.goal.id;
  ok(created.goal.autonomy === 'act_with_checkpoints' && ['publish', 'send_message', 'spend', 'create_account', 'accept_terms', 'delete_data'].every((x) => created.goal.checkpoints.includes(x)),
    'default autonomy is act_with_checkpoints with every required checkpoint kind');
  const sched = scheduler.forGoal(gid);
  ok(sched && sched.goalId === gid && sched.label === 'Every day at 09:00' && sched.nextRun > Date.now(), 'one schedule drives the goal: ' + (sched && sched.label));
  ok(scheduler.list().filter((s) => s.goalId === gid).length === 1, 'exactly one schedule per goal');
  goals._syncSchedule(goals.goals.get(gid));
  ok(scheduler.list().filter((s) => s.goalId === gid).length === 1, 'syncing again does not add a second schedule');
  ok(scheduler.handle({ op: 'cancel', id: sched.id }).ok === false, "a task's cancel_schedule cannot cancel a goal's schedule");
  ok(!!created.taskId, 'the first cycle starts right away as a background task');
  const firstTask = tasks.get(created.taskId);
  ok(firstTask && firstTask.goalId === gid && firstTask.source === 'goal', 'the cycle task carries the goal id');
  ok(/Goal cycle for "Grow a YouTube channel/.test(firstTask.prompt) && firstTask.prompt.includes('day 1 of 60') && firstTask.prompt.includes('```json'), 'cycle prompt comes from the engine (objective, day N of M, record contract)');

  const ended = await until(() => ends.some((e) => e.task.id === created.taskId), 20000);
  ok(ended, 'the fake cycle task finished');
  const recorded = await until(async () => { const d = await engineCall('GET', `/goals/${gid}`); return d.cycles.length === 1 ? d : false; }, 8000);
  ok(!!recorded, 'its JSON cycle record was posted to /goals/<id>/cycle');
  const det = await engineCall('GET', `/goals/${gid}`);
  const cyc = det.cycles[0] || {};
  ok(cyc.metrics && cyc.metrics.views && cyc.metrics.views.value === 37 && cyc.metrics.views.source === 'connector:youtube', 'the cycle keeps the platform metric reading (views 37 from connector:youtube)');
  ok(det.series.views && det.series.views[0].value === 37, 'the metric series has the reading');
  const gates = JSON.parse((/GATES (\{.*?\}) CHECKPOINT/.exec(cyc.reflection || '') || [])[1] || '{}');
  ok(gates.upload === 'deny', 'publish (videos_insert) was blocked by the bridge gate without approval');
  ok(gates.analytics === 'allow', 'reading analytics was not held');
  ok(gates.buyViews === 'deny', 'buying views was refused');
  ok(gates.ads === 'deny', 'starting an ad campaign (spend) was held');
  ok(/CHECKPOINT filed/.test(cyc.reflection || ''), 'the cycle filed its checkpoint through goal_checkpoint');
  ok(readYt().length === 0, 'nothing reached the YouTube connector before approval');
  ok(entries.some((e) => e.kind === 'policy' && /Checkpoint held: mcp__youtube__videos_insert \(publish\)/.test(e.summary)), 'the hold is in the action log');

  // ------------------------------------------------------------ the checkpoint card
  const cards = goals.pendingCards();
  ok(cards.length === 1 && cards[0].headline === 'Approve video ‘My $400 desk setup’' && cards[0].goalId === gid, 'one pending card: ' + (cards[0] && cards[0].headline));
  ok(cards[0].items.thumbnail === 'thumb-a.png' && cards[0].items.file === 'desk-setup-v3.mp4' && cards[0].items.schedule === 'Fri 17:00', 'the card carries the prepared video, thumbnail and schedule');
  ok(goals.list().goals[0].pendingCheckpoints === 1 && goals.list().checkpoints.length === 1, 'the goal and the panel list show it');

  // still blocked while the card waits, in every path
  const g1 = goals.gate({ goalId: gid, toolName: 'mcp__o__mcp_call', input: { server: 'youtube', tool: 'videos_insert', args: { title: 'x' } } });
  ok(g1.allow === false && g1.kind === 'publish' && /goal_checkpoint/.test(g1.message), 'gate: publish stays blocked while the card is pending');
  ok(goals.gate({ goalId: gid, toolName: 'mcp__playwright__browser_click', input: { element: 'Publish button' } }).allow === false, 'gate: a browser Publish click is blocked too');
  ok(goals.gate({ goalId: gid, toolName: 'mcp__youtube__analytics_query', input: {} }).allow === true, 'gate: reads pass');

  // next cycle sees the pending card (so it does not redo it)
  const notes = goals._notes(gid, 0);
  ok(notes.some((n) => /Waiting for the person's approval/.test(n)), 'the next prompt is told the card is still waiting');

  // approve: exactly the prepared action runs, once, through the engine
  const ap = await goals.resolveCheckpoint(cards[0].id, 'approve');
  ok(ap.ok && ap.checkpoint.status === 'done', 'Approve ran the prepared action: ' + (ap.checkpoint && ap.checkpoint.status) + ' ' + (ap.checkpoint && ap.checkpoint.result));
  const yt = readYt();
  ok(yt.length === 1 && yt[0].name === 'videos_insert' && yt[0].arguments.title === 'My $400 desk setup' && yt[0].arguments.privacyStatus === 'public', 'the connector got exactly the approved call');
  ok((await goals.resolveCheckpoint(cards[0].id, 'approve')).ok === false && readYt().length === 1, 'approving twice does nothing');
  ok(goals.gate({ goalId: gid, toolName: 'mcp__youtube__videos_insert', input: {} }).allow === false, 'approving a card with an action grants nothing else');

  // ------------------------------------------------------------ browser path: approval allows exactly one action
  process.env.FAKE_GOAL_NOACTION = '1';
  const cp2 = goals.addCheckpoint(created.taskId, { kind: 'publish', title: "Approve short 'Cable tray in 30 s'", summary: 'Posts the short in YouTube Studio.', items: { type: 'short', title: 'Cable tray in 30 s' } });
  ok(cp2.ok && goals.pendingCards().length === 1, 'a browser-step checkpoint is filed without an action');
  const before = goals.gate({ goalId: gid, toolName: 'mcp__playwright__browser_click', input: { element: 'Publish button' } });
  ok(before.allow === false, 'before approval the Publish click is blocked');
  const endsBefore = ends.length;
  const ap2 = await goals.resolveCheckpoint(cp2.checkpoint.id, 'approve');
  ok(ap2.ok && ap2.checkpoint.status === 'approved', 'Approve grants one publish action and resumes the task');
  const followed = await until(() => ends.length > endsBefore, 20000);
  const fu = ends[ends.length - 1];
  const fuOut = JSON.parse((/FOLLOWUP (\{.*\})/.exec((fu && fu.result) || '') || [])[1] || '{}');
  ok(followed && fu.task.id === created.taskId, 'the follow-up ran in the same goal task');
  ok(fuOut.first === 'allow' && fuOut.again === 'deny', 'after approval exactly one Publish click is allowed, the next is held again: ' + JSON.stringify(fuOut));
  ok(goals.checkpoints.find((c) => c.id === cp2.checkpoint.id).status === 'done', 'the card is done once its action ran');
  const detAfter = await engineCall('GET', `/goals/${gid}`);
  ok(detAfter.cycles.length === 1, 'a follow-up without a record adds no cycle');

  // ------------------------------------------------------------ never by O, edit, skip, act
  const cp3 = goals.addCheckpoint(created.taskId, { kind: 'spend', title: 'Approve purchase ‘Ring light’', summary: 'A $39 ring light for better lighting.' });
  const ap3 = await goals.resolveCheckpoint(cp3.checkpoint.id, 'approve');
  ok(ap3.checkpoint.status === 'handed_off' && ap3.checkpoint.personDoesIt, 'Approve on a spend card hands it to the person (O never spends)');
  ok(goals.gate({ goalId: gid, toolName: 'mcp__stripe__create_payment_intent', input: {} }).allow === false, 'spending stays blocked after that approval');
  ok(goals.addCheckpoint(created.taskId, { kind: 'publish', title: 't', summary: 's', action: { server: 'smm', tool: 'buy_views', args: { count: 1000 } } }).ok === false, 'a checkpoint for bought views is refused outright');
  const cp4 = goals.addCheckpoint(created.taskId, { kind: 'publish', title: 'Approve video ‘Monitor arms’', summary: 's' });
  ok((await goals.resolveCheckpoint(cp4.checkpoint.id, 'edit', '')).ok === false, 'Edit needs words');
  const cp5 = goals.addCheckpoint(created.taskId, { kind: 'send_message', title: 'Approve message ‘Collab pitch’', summary: 'Emails a creator.' });
  ok((await goals.resolveCheckpoint(cp5.checkpoint.id, 'skip')).checkpoint.status === 'skipped', 'Skip closes the card');
  ok(goals._notes(gid, 0).some((n) => /Skipped by the person/.test(n)), 'the next cycle hears about the skip');
  await goals.resolveCheckpoint(cp4.checkpoint.id, 'skip');

  await goals.setAutonomy(gid, 'act', ['publish', 'spend']);
  ok(goals.goals.get(gid).autonomy === 'act' && JSON.stringify(goals.goals.get(gid).allowed) === '["publish"]', '"act" with publish allowed (spend is dropped)');
  ok(goals.gate({ goalId: gid, toolName: 'mcp__youtube__videos_insert', input: {} }).allow === true, 'only then publish runs without a card');
  ok(goals.gate({ goalId: gid, toolName: 'mcp__gmail__send_message', input: {} }).allow === false, 'kinds not listed still need a card under "act"');
  ok(goals.gate({ goalId: gid, toolName: 'mcp__o__web_run', input: { task: 'buy 1000 youtube views' } }).allow === false, 'fake engagement is refused even under "act"');
  await goals.setAutonomy(gid, 'act_with_checkpoints', []);

  // ------------------------------------------------------------ pause / resume / archive keep the schedule in step
  await goals.setStatus(gid, 'pause');
  ok(!scheduler.forGoal(gid), 'pause removes the goal schedule');
  ok((await goals.fireCycle(gid)).ok === false, 'a paused goal does not run');
  await goals.setStatus(gid, 'resume');
  ok(!!scheduler.forGoal(gid), 'resume schedules it again');

  // the scheduler firing starts a cycle with the trend and the checkpoint notes
  const sid = scheduler.forGoal(gid).id;
  scheduler.items.find((x) => x.id === sid).nextRun = Date.now() - 1000;
  const endsTick = ends.length;
  scheduler.tick();
  const fired = await until(() => tasks.list().some((t) => t.goalId === gid && t.id !== created.taskId), 8000);
  ok(fired, 'the schedule firing starts the next cycle task');
  const t2 = tasks.list().find((t) => t.goalId === gid && t.id !== created.taskId);
  ok(t2 && /last 1: 37/.test(t2.prompt) && /Checkpoints since the last cycle/.test(t2.prompt) && /Skipped by the person/.test(t2.prompt), 'cycle 2 prompt has the metric trend and what happened to the checkpoints');
  await until(() => ends.length > endsTick, 20000);
  ok(await until(async () => (await engineCall('GET', `/goals/${gid}`)).cycles.length === 2, 8000), 'cycle 2 recorded');
  await goals.setStatus(gid, 'archive');
  ok(!scheduler.forGoal(gid) && goals.pendingCards().length === 0, 'archive removes the schedule and closes its cards');

  // ------------------------------------------------------------ a bridge with no app endpoint holds, never passes
  {
    const { spawn: sp } = require('child_process');
    const child = sp(process.execPath, [path.join(__dirname, 'bridge-mcp.js')], { env: { ...process.env, O_GOAL_ID: gid, O_PERMISSION_MODE: 'auto', O_GOAL_URL: '' }, stdio: ['pipe', 'pipe', 'ignore'] });
    let buf = ''; const waits = new Map();
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (d) => { buf += d; let i; while ((i = buf.indexOf('\n')) >= 0) { const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); const w = waits.get(m.id); if (w) { waits.delete(m.id); w(m); } } });
    let n = 1;
    const rpc = (method, params) => new Promise((res) => { const id = n++; waits.set(id, res); child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); });
    await rpc('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 't', version: '0' } });
    const r = await rpc('tools/call', { name: 'approve', arguments: { tool_name: 'mcp__youtube__videos_insert', input: {} } });
    const v = JSON.parse(r.result.content[0].text);
    ok(v.behavior === 'deny', 'auto mode + unreachable app: a goal task\'s publish is still held (' + v.message + ')');
    const r2 = await rpc('tools/call', { name: 'approve', arguments: { tool_name: 'Read', input: { file_path: '/x' } } });
    ok(JSON.parse(r2.result.content[0].text).behavior === 'allow', 'ordinary tools are unaffected');
    child.stdin.end();
  }

  tasks.dispose();
  scheduler.stop();
  try { engineChild.kill('SIGTERM'); } catch (_) {}
  console.log(`\n${passed} passed, ${failures} failed`);
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error(e);
  try { engineChild && engineChild.kill('SIGTERM'); } catch (_) {}
  process.exit(1);
});
