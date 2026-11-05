#!/usr/bin/env node
'use strict';
// Self-test for bridge-mcp.js: spawns the bridge against an in-script HTTP stub (engine + app log),
// speaks JSON-RPC over its stdin/stdout, and asserts the responses. Exit code 1 on any failure.

const http = require('http');
const path = require('path');
const { spawn } = require('child_process');

const BRIDGE = path.join(__dirname, 'bridge-mcp.js');
const TOKEN = 'selftest-token';
const stub = { log: [], provisionBodies: [], polls: 0, computerBodies: [], replayBodies: [], findBodies: [], treeWindows: [], cuPolls: 0, toolCalls: [], scriptBodies: [], swarmBodies: [], webBodies: [], swarmPolls: 0, webPolls: 0, uiBodies: [] };

function readBody(req) {
  return new Promise((resolve) => {
    let d = '';
    req.setEncoding('utf8');
    req.on('data', (c) => { d += c; });
    req.on('end', () => { try { resolve(d ? JSON.parse(d) : {}); } catch { resolve({ _raw: d }); } });
  });
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  if (req.headers['x-o-token'] !== TOKEN) return json(res, 401, { detail: 'bad token' });
  const u = new URL(req.url, 'http://127.0.0.1');
  const body = req.method === 'POST' ? await readBody(req) : null;
  if (req.method === 'POST' && u.pathname === '/log') { stub.log.push(body); return json(res, 200, { ok: true }); }
  if (req.method === 'POST' && u.pathname === '/ui') {
    // Mirrors agent.js: envelope check only, counts accepted/rejected.
    stub.uiBodies.push(body);
    const frames = Array.isArray(body && body.frames) ? body.frames : [body];
    const okf = frames.filter((f) => f && f.a2ui === '1' && ['begin', 'update', 'data', 'end', 'sandbox'].includes(f.type) && /^(chat|job):./.test(String(f.surface)));
    return json(res, 200, { ok: true, accepted: okf.length, rejected: frames.length - okf.length });
  }
  if (req.method === 'POST' && u.pathname === '/provision') { stub.provisionBodies.push(body); return json(res, 200, { job_id: 'job-1' }); }
  if (req.method === 'GET' && u.pathname === '/jobs/job-1') {
    stub.polls += 1;
    const base = { id: 'job-1', label: 'selftest', phase: 'execute', attempt: 1, max_attempts: 3, cached: false, steps: [], done: false, ok: false };
    if (stub.polls < 2) return json(res, 200, base);
    return json(res, 200, {
      ...base, phase: 'report', cached: true, done: true, ok: true,
      steps: [
        { index: 0, command: 'echo one', cwd: '/tmp', exit_code: 0, stdout_tail: 'one', stderr_tail: '', elapsed_ms: 3 },
        { index: 1, command: 'echo two', cwd: '/tmp', exit_code: 0, stdout_tail: 'two', stderr_tail: '', elapsed_ms: 2 },
        { index: 2, command: 'echo three', cwd: '/tmp', exit_code: 0, stdout_tail: 'three', stderr_tail: '', elapsed_ms: 2 },
      ],
    });
  }
  if (req.method === 'POST' && u.pathname === '/cache/lookup') return json(res, 200, { hit: false, fingerprint: 'abc' });
  if (req.method === 'POST' && u.pathname === '/computer/run') { stub.computerBodies.push(body); return json(res, 200, { job_id: 'cu-1' }); }
  if (req.method === 'POST' && u.pathname === '/computer/replay') { stub.replayBodies.push(body); return json(res, 200, { job_id: 'cu-1' }); }
  if (req.method === 'GET' && u.pathname === '/jobs/cu-1') {
    stub.cuPolls += 1;
    const base = { id: 'cu-1', label: 'open calc', phase: 'plan', attempt: 0, max_attempts: 3, cached: false, steps: [], done: false, ok: false, procedure_id: null, candidates: 1, scores: [] };
    if (stub.cuPolls % 2 === 1) return json(res, 200, base);
    return json(res, 200, {
      ...base, phase: 'report', done: true, ok: true, procedure_id: 'libreoffice-calc/abc/v1',
      steps: [{ index: 0, command: 'launch cmd=\'libreoffice --calc\'', cwd: '', exit_code: 0, stdout_tail: '', stderr_tail: '', elapsed_ms: 5 }],
    });
  }
  if (req.method === 'POST' && u.pathname === '/computer/find') { stub.findBodies.push(body); return json(res, 200, { found: true, node: { id: 'n1', role: body.target.role, name: 'A1' } }); }
  if (req.method === 'GET' && u.pathname === '/computer/tree') { stub.treeWindows.push(u.searchParams.get('window')); return json(res, 200, { window: u.searchParams.get('window'), nodes: [{ id: 'n0', role: 'frame', name: 'Calc', children: [] }] }); }
  if (req.method === 'GET' && u.pathname === '/tools/list') return json(res, 200, { tools: [{ server: 'fs', name: 'read_file', input_schema: { type: 'object', properties: { path: { type: 'string' } }, required: ['path'] } }] });
  if (req.method === 'POST' && u.pathname === '/tools/call') { stub.toolCalls.push(body); return json(res, 200, { ok: true, content: [{ type: 'text', text: 'hello' }], is_error: false, elapsed_ms: 4 }); }
  if (req.method === 'POST' && u.pathname === '/script/run') { stub.scriptBodies.push(body); return json(res, 200, { index: 0, command: 'python3 -I .o-step-0.py', cwd: body.target_directory, exit_code: 0, stdout: '42\n', stderr: '', elapsed_ms: 7, timed_out: false }); }
  if (req.method === 'POST' && u.pathname === '/swarm/run') { stub.swarmBodies.push(body); return json(res, 200, { job_id: 'sw-1' }); }
  if (req.method === 'GET' && u.pathname === '/jobs/sw-1') {
    stub.swarmPolls += 1;
    const base = { id: 'sw-1', label: body === null ? 'swarm' : 'swarm', phase: 'context', attempt: 0, max_attempts: 1, cached: false, steps: [], done: false, ok: false };
    if (stub.swarmPolls < 2) return json(res, 200, base);
    return json(res, 200, { ...base, phase: 'report', done: true, ok: true, result: { profile: { cwd: '/x' }, request: { label: 'swarm', steps: [] }, inspection: { ok: true } } });
  }
  if (req.method === 'POST' && u.pathname === '/web/run') { stub.webBodies.push(body); return json(res, 200, { job_id: 'web-1' }); }
  if (req.method === 'GET' && u.pathname === '/jobs/web-1') {
    stub.webPolls += 1;
    const base = { id: 'web-1', label: 'web', phase: 'execute', attempt: 0, max_attempts: 1, cached: false, steps: [], done: false, ok: false };
    if (stub.webPolls < 2) return json(res, 200, base);
    return json(res, 200, { ...base, phase: 'report', done: true, ok: true, result: { ok: true, final_text: 'Example Domain', steps: [{ index: 0, op: 'goto' }], screenshots_sha: [], via: 'playwright-dom' } });
  }
  if (req.method === 'GET' && u.pathname === '/context') {
    return json(res, 200, {
      cwd: u.searchParams.get('cwd'), collected_at: 1, platform: 'linux',
      shell_history: ['ls', 'npm test'], tree: { name: 'x', type: 'dir', children: [{ name: 'src', type: 'dir' }, { name: 'package.json', type: 'file', size: 10 }] },
      config_files: [{ path: '/x/package.json', sha256: 'f', head: '{\n  "name": "x"\n}' }],
      telemetry: [{ ts: 1730000000, app: 'Terminal', window: 'zsh', text: 'npm ERR! missing script' }],
    });
  }
  json(res, 404, { detail: 'no route ' + req.method + ' ' + u.pathname });
});

const failures = [];
function check(cond, what) {
  if (cond) console.log('  ok   ' + what);
  else { failures.push(what); console.log('  FAIL ' + what); }
}

function rpcClient(child) {
  const pending = new Map();
  let buf = '';
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    buf += chunk;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { failures.push('non-JSON line on stdout: ' + line.slice(0, 80)); continue; }
      const p = pending.get(msg.id);
      if (p) { pending.delete(msg.id); p(msg); }
    }
  });
  let next = 1;
  return (method, params, timeoutMs = 10000) => new Promise((resolve, reject) => {
    const id = next++;
    const t = setTimeout(() => { pending.delete(id); reject(new Error(`timeout waiting for ${method}`)); }, timeoutMs);
    pending.set(id, (m) => { clearTimeout(t); resolve(m); });
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
}

function text(res) {
  return res && res.result && res.result.content && res.result.content[0] && res.result.content[0].text;
}

async function runWith(env, fn) {
  const child = spawn(process.execPath, [BRIDGE], { env: { ...process.env, ...env }, stdio: ['pipe', 'pipe', 'pipe'] });
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (d) => process.stderr.write('  [bridge stderr] ' + d));
  try { await fn(rpcClient(child)); } finally { child.stdin.end(); await new Promise((r) => child.on('exit', r)); }
}

async function main() {
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const env = {
    O_ENGINE_URL: base, O_ENGINE_TOKEN: TOKEN,
    O_LOG_URL: base + '/log', O_LOG_TOKEN: TOKEN,
    O_UI_URL: base + '/ui', O_SESSION_SURFACE: 'chat:abc',
    O_PERMISSION_MODE: 'log',
  };

  console.log('bridge with engine + log stub (mode=log)');
  await runWith(env, async (rpc) => {
    const init = await rpc('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'selftest', version: '0' } });
    check(init.result && init.result.serverInfo && init.result.serverInfo.name === 'o', 'initialize → serverInfo.name == "o"');

    const list = await rpc('tools/list', {});
    const names = (list.result.tools || []).map((t) => t.name).sort();
    const want = ['approve', 'cache_lookup', 'cancel_schedule', 'computer_find', 'computer_replay', 'computer_run', 'computer_tree', 'diagnose', 'goal_checkpoint', 'goal_create', 'goal_propose', 'goal_record_cycle', 'goal_status', 'list_schedules', 'log_note', 'mcp_call', 'mcp_tools', 'provision', 'recall_preferences', 'remember_preference', 'schedule_task', 'script_run', 'see_screen', 'swarm_run', 'ui_emit', 'verify_port', 'web_run', 'workspace_context'];
    check(JSON.stringify(names) === JSON.stringify(want), 'tools/list → ' + names.join(','));
    const prov = list.result.tools.find((t) => t.name === 'provision');
    const stepSchema = prov.inputSchema.properties.steps.items;
    check(prov.inputSchema.additionalProperties === false && stepSchema.additionalProperties === false &&
      JSON.stringify(stepSchema.required) === JSON.stringify(['target_directory', 'timeout_seconds']) &&
      stepSchema.properties.command_string && stepSchema.properties.script && stepSchema.properties.script.additionalProperties === false &&
      JSON.stringify(prov.inputSchema.properties.risk.enum) === JSON.stringify(['normal', 'high']), 'provision inputSchema is strict ProvisionRequest (command_string | script, risk)');
    for (const [name, required] of [['mcp_tools', []], ['mcp_call', ['server', 'tool', 'args']], ['script_run', ['language', 'code', 'target_directory', 'timeout_seconds']], ['swarm_run', ['objective']], ['web_run', ['task']]]) {
      const t = list.result.tools.find((x) => x.name === name);
      check(t && t.inputSchema.additionalProperties === false && JSON.stringify(t.inputSchema.required || []) === JSON.stringify(required), `${name} inputSchema is strict (required=${required.join(',')})`);
    }
    check(list.result.tools.find((x) => x.name === 'script_run').inputSchema.properties.language.enum.length === 4, 'script_run language enum has 4 languages');

    const ap = await rpc('tools/call', { name: 'approve', arguments: { tool_name: 'Bash', input: { command: 'ls' }, tool_use_id: 'tu1' } });
    const decision = JSON.parse(text(ap));
    check(decision.behavior === 'allow' && decision.updatedInput.command === 'ls', 'approve (log mode) → allow with updatedInput');

    const note = await rpc('tools/call', { name: 'log_note', arguments: { text: 'installing deps' } });
    check(text(note) === 'noted' && !note.result.isError, 'log_note → "noted"');
    await new Promise((r) => setTimeout(r, 100));
    check(stub.log.some((e) => e.kind === 'note' && e.summary === 'installing deps' && !('source' in e)), 'log stub received note entry (no source field)');
    const pol = stub.log.find((e) => e.kind === 'policy' && /^allowed Bash: /.test(e.summary));
    check(!!pol, 'log stub received policy entry for approve');
    check(pol && pol.detail && pol.detail.toolName === 'Bash' && pol.detail.mode === 'log' && !('source' in pol), 'policy detail has toolName=Bash, mode=log, no source');

    const req = { label: 'selftest', steps: [{ target_directory: '/tmp', command_string: 'echo one', timeout_seconds: 10 }] };
    const pv = await rpc('tools/call', { name: 'provision', arguments: req }, 20000);
    const out = text(pv) || '';
    check(stub.provisionBodies.length === 1 && stub.provisionBodies[0].label === 'selftest', 'POST /provision received the request');
    check(stub.polls === 2, `polled /jobs/job-1 until done (polls=${stub.polls})`);
    check(out.split('\n')[0] === '✓ selftest: 3 steps, cached replay', 'provision summary line: ' + JSON.stringify(out.split('\n')[0]));
    let job = null;
    try { job = JSON.parse(out.slice(out.indexOf('\n') + 1)); } catch {}
    check(job && job.done === true && job.ok === true && job.id === 'job-1', 'provision body is the final JobState JSON');
    check(!pv.result.isError, 'provision result not flagged isError');

    const cl = await rpc('tools/call', { name: 'cache_lookup', arguments: { request: req } });
    check(JSON.parse(text(cl)).hit === false, 'cache_lookup → forwards to /cache/lookup');

    const wc = await rpc('tools/call', { name: 'workspace_context', arguments: { cwd: '/x' } });
    const wtxt = text(wc) || '';
    check(/\$ npm test/.test(wtxt) && /src\//.test(wtxt) && /package\.json/.test(wtxt) && /\[Terminal\]/.test(wtxt), 'workspace_context compact rendering has history, tree, config, telemetry');
    check(/"cwd": "\/x"/.test(wtxt), 'workspace_context includes the JSON profile');

    const cr = list.result.tools.find((t) => t.name === 'computer_run');
    check(cr.inputSchema.additionalProperties === false && cr.inputSchema.required.length === 1 && cr.inputSchema.required[0] === 'goal', 'computer_run inputSchema is strict {goal, app?, candidates?}');
    const cf = list.result.tools.find((t) => t.name === 'computer_find');
    check(cf.inputSchema.properties.target.additionalProperties === false && cf.inputSchema.required[0] === 'target', 'computer_find inputSchema is strict {target}');

    const run = await rpc('tools/call', { name: 'computer_run', arguments: { goal: 'open calc', app: 'libreoffice-calc', candidates: 1 } }, 20000);
    const rtxt = text(run) || '';
    check(stub.computerBodies.length === 1 && stub.computerBodies[0].goal === 'open calc' && stub.computerBodies[0].app === 'libreoffice-calc', 'POST /computer/run received {goal, app, candidates}');
    check(stub.cuPolls === 2, `computer_run polled /jobs/cu-1 until done (polls=${stub.cuPolls})`);
    check(rtxt.split('\n')[0] === '✓ open calc: 1 step, fresh run → procedure libreoffice-calc/abc/v1', 'computer_run summary line: ' + JSON.stringify(rtxt.split('\n')[0]));
    let cujob = null;
    try { cujob = JSON.parse(rtxt.slice(rtxt.indexOf('\n') + 1)); } catch {}
    check(cujob && cujob.done === true && cujob.procedure_id === 'libreoffice-calc/abc/v1' && !run.result.isError, 'computer_run body is the final JobState with procedure_id');

    const rp = await rpc('tools/call', { name: 'computer_replay', arguments: { procedure_id: 'libreoffice-calc/abc/v1' } }, 20000);
    check(stub.replayBodies.length === 1 && stub.replayBodies[0].procedure_id === 'libreoffice-calc/abc/v1', 'POST /computer/replay received {procedure_id}');
    check(stub.cuPolls === 4 && !rp.result.isError && /"done": true/.test(text(rp) || ''), 'computer_replay polled to completion');

    const fd = await rpc('tools/call', { name: 'computer_find', arguments: { target: { role: 'table cell', name: 'A1' } } });
    check(stub.findBodies.length === 1 && stub.findBodies[0].target.name === 'A1' && JSON.parse(text(fd)).found === true, 'computer_find → POST /computer/find {target}');

    const tr = await rpc('tools/call', { name: 'computer_tree', arguments: { window: '*Calc*' } });
    check(stub.treeWindows[0] === '*Calc*' && JSON.parse(text(tr)).nodes[0].role === 'frame', 'computer_tree → GET /computer/tree?window=');
    const tr2 = await rpc('tools/call', { name: 'computer_tree', arguments: {} });
    check(stub.treeWindows[1] === null && JSON.parse(text(tr2)).nodes.length === 1, 'computer_tree without window omits the query');

    const mt = await rpc('tools/call', { name: 'mcp_tools', arguments: {} });
    check(!mt.result.isError && JSON.parse(text(mt)).tools[0].server === 'fs', 'mcp_tools → GET /tools/list');

    const mc = await rpc('tools/call', { name: 'mcp_call', arguments: { server: 'fs', tool: 'read_file', args: { path: '/etc/hostname' } } });
    check(stub.toolCalls.length === 1 && stub.toolCalls[0].server === 'fs' && stub.toolCalls[0].tool === 'read_file' && stub.toolCalls[0].args.path === '/etc/hostname', 'mcp_call → POST /tools/call {server, tool, args}');
    check(!mc.result.isError && JSON.parse(text(mc)).ok === true && JSON.parse(text(mc)).content[0].text === 'hello', 'mcp_call returns the ToolResult');

    const sr = await rpc('tools/call', { name: 'script_run', arguments: { language: 'python', code: 'print(6*7)', target_directory: '/tmp', timeout_seconds: 5 } });
    check(stub.scriptBodies.length === 1 && stub.scriptBodies[0].language === 'python' && stub.scriptBodies[0].code === 'print(6*7)' && stub.scriptBodies[0].timeout_seconds === 5, 'script_run → POST /script/run {language, code, target_directory, timeout_seconds}');
    check(!sr.result.isError && JSON.parse(text(sr)).exit_code === 0 && JSON.parse(text(sr)).stdout === '42\n', 'script_run returns the StepResult');

    const sw = await rpc('tools/call', { name: 'swarm_run', arguments: { objective: 'make the tests pass', cwd: '/x' } }, 20000);
    check(stub.swarmBodies.length === 1 && stub.swarmBodies[0].objective === 'make the tests pass' && stub.swarmBodies[0].cwd === '/x', 'swarm_run → POST /swarm/run {objective, cwd}');
    check(stub.swarmPolls === 2 && !sw.result.isError && /"done": true/.test(text(sw) || '') && /"inspection"/.test(text(sw) || ''), 'swarm_run polled /jobs/sw-1 to completion and returned the SwarmResult');

    const wr = await rpc('tools/call', { name: 'web_run', arguments: { task: 'read the heading', url: 'http://127.0.0.1:1/' } }, 20000);
    check(stub.webBodies.length === 1 && stub.webBodies[0].task === 'read the heading' && stub.webBodies[0].url === 'http://127.0.0.1:1/', 'web_run → POST /web/run {task, url}');
    check(stub.webPolls === 2 && !wr.result.isError && /"final_text": "Example Domain"/.test(text(wr) || ''), 'web_run polled /jobs/web-1 to completion and returned the WebResult');
    const wr2 = await rpc('tools/call', { name: 'web_run', arguments: { task: 'no url' } }, 20000);
    check(stub.webBodies.length === 2 && !('url' in stub.webBodies[1]) && !wr2.result.isError, 'web_run without url omits the key');

    const ue = list.result.tools.find((t) => t.name === 'ui_emit');
    check(ue && ue.inputSchema.additionalProperties === false && JSON.stringify(ue.inputSchema.required) === '["frames"]' &&
      ue.inputSchema.properties.frames.minItems === 1 && ue.inputSchema.properties.frames.maxItems === 64 && ue.inputSchema.properties.frames.items.type === 'object' &&
      ue.inputSchema.properties.surface.type === 'string', 'ui_emit inputSchema is strict {frames: object[1..64], surface?: string}');
    const frames = [
      { a2ui: '1', type: 'begin', title: 'Plan' },
      { a2ui: '1', type: 'update', surface: 'job:j9', components: [{ id: 't', component: { Text: { text: 'hi' } } }] },
      { a2ui: '1', type: 'bogus' },
    ];
    const em = await rpc('tools/call', { name: 'ui_emit', arguments: { frames } });
    check(stub.uiBodies.length === 1 && Array.isArray(stub.uiBodies[0].frames) && stub.uiBodies[0].frames.length === 3, 'ui_emit → POST /ui {frames}');
    check(stub.uiBodies[0].frames[0].surface === 'chat:abc' && stub.uiBodies[0].frames[2].surface === 'chat:abc', 'frames without a surface get O_SESSION_SURFACE');
    check(stub.uiBodies[0].frames[1].surface === 'job:j9', 'a frame with its own surface keeps it');
    check(text(em) === 'accepted 2, rejected 1 (see the action log for why)' && !em.result.isError, 'ui_emit returns "accepted N, rejected M": ' + JSON.stringify(text(em)));
    const em2 = await rpc('tools/call', { name: 'ui_emit', arguments: { frames: [{ a2ui: '1', type: 'end', ok: true }], surface: 'chat:other' } });
    check(stub.uiBodies[1].frames[0].surface === 'chat:other' && text(em2) === 'accepted 1, rejected 0', 'ui_emit surface argument overrides the session surface');
    const em3 = await rpc('tools/call', { name: 'ui_emit', arguments: { frames: [] } });
    check(em3.result.isError === true, 'ui_emit with no frames → isError');
    const em4 = await rpc('tools/call', { name: 'ui_emit', arguments: { frames: [{ a2ui: '1', type: 'begin', surface: 'nope:1' }] } });
    check(em4.result.isError === true && text(em4) === 'accepted 0, rejected 1 (see the action log for why)', 'ui_emit with every frame rejected → isError');

    const unk = await rpc('tools/call', { name: 'nope', arguments: {} });
    check(unk.error && unk.error.code === -32602, 'unknown tool → -32602');
  });

  console.log('bridge without engine (mode=auto)');
  await runWith({ O_PERMISSION_MODE: 'auto', O_LOG_URL: base + '/log', O_LOG_TOKEN: TOKEN }, async (rpc) => {
    await rpc('initialize', { protocolVersion: '2024-11-05' });
    const before = stub.log.length;
    const ap = await rpc('tools/call', { name: 'approve', arguments: { tool_name: 'Edit', input: { a: 1 } } });
    check(JSON.parse(text(ap)).behavior === 'allow', 'approve (auto mode) → allow');
    await new Promise((r) => setTimeout(r, 100));
    check(stub.log.length === before, 'auto mode posts no policy entry');
    for (const name of ['provision', 'cache_lookup', 'diagnose', 'verify_port', 'workspace_context', 'computer_run', 'computer_replay', 'computer_find', 'computer_tree', 'mcp_tools', 'mcp_call', 'script_run', 'swarm_run', 'web_run', 'recall_preferences', 'remember_preference']) {
      const r = await rpc('tools/call', { name, arguments: name === 'remember_preference' ? { text: 'x' } : {} });
      check(r.result && r.result.isError === true && /engine is not running/.test(text(r)), `${name} without O_ENGINE_URL → isError "engine is not running"`);
    }
    const sc = await rpc('tools/call', { name: 'schedule_task', arguments: { prompt: 'x', when: { type: 'daily', time: '08:00' } } });
    check(sc.result && sc.result.isError === true && /not reachable/.test(text(sc)), 'schedule_task without O_SCHEDULE_URL → isError');
    const ue = await rpc('tools/call', { name: 'ui_emit', arguments: { frames: [{ a2ui: '1', type: 'begin' }] } });
    check(ue.result && ue.result.isError === true && /not reachable/.test(text(ue)), 'ui_emit without O_UI_URL → isError');
  });

  console.log('bridge in ask mode with unreachable O_PERM_URL');
  await runWith({ O_PERMISSION_MODE: 'ask', O_PERM_URL: 'http://127.0.0.1:1/approve', O_PERM_TOKEN: 'x' }, async (rpc) => {
    await rpc('initialize', { protocolVersion: '2024-11-05' });
    const ap = await rpc('tools/call', { name: 'approve', arguments: { tool_name: 'Bash', input: {} } });
    const d = JSON.parse(text(ap));
    check(d.behavior === 'deny' && /not reachable/.test(d.message), 'approve (ask mode, O down) → deny');
  });

  server.close();
  if (failures.length) {
    console.log(`\n${failures.length} failure(s):\n  - ` + failures.join('\n  - '));
    process.exit(1);
  }
  console.log('\nall bridge selftests passed');
}

main().catch((e) => { console.error('selftest crashed:', e && e.stack || e); process.exit(1); });
