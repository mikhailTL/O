'use strict';
// Self-test for main/agent.js: permission modes, action-log entries and the POST /log endpoint.
// Run: node main/tools/selftest-agent.js  (exits non-zero on the first failing assertion)

process.env.O_PERMISSION_TIMEOUT_MS = '300'; // short ask-mode timeout so the permission-timeout check runs fast

const assert = require('assert');
const http = require('http');
const os = require('os');
const path = require('path');
const fs = require('fs');
const { OAgent, PERMISSION_MODES, SAFE_TOOLS, ENGINE_TOOLS, childEnv, fullPath, genuiFrameProblem, buildPersona } = require('../agent');

const entries = [];
const ingested = [];
const fakeLog = {
  record(e) { entries.push(e); return e; },
  ingest(e) { ingested.push(e); return e; },
};

let mode = 'log';
let onPermissionRequest = null;
const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'o-selftest-agent-'));

function post(port, url, body, token) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const headers = { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) };
    if (token != null) headers['x-o-token'] = token;
    const req = http.request({ host: '127.0.0.1', port, path: url, method: 'POST', headers }, (res) => {
      let out = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { out += c; });
      res.on('end', () => { let j = null; try { j = JSON.parse(out); } catch {} resolve({ status: res.statusCode, body: j }); });
    });
    req.on('error', reject);
    req.end(data);
  });
}

let passed = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  passed++;
  console.log('ok - ' + label);
}

(async () => {
  const agent = new OAgent({
    userDataDir,
    getSettings: () => ({ permissionMode: mode }),
    onPermissionRequest: (req) => onPermissionRequest ? onPermissionRequest(req) : Promise.resolve({ allow: false }),
    actionLog: fakeLog,
    getEngine: () => ({ url: 'http://127.0.0.1:9/', token: 'engine-token' }),
    getChatId: () => 'chat-42',
  });

  ok(Array.isArray(PERMISSION_MODES) && PERMISSION_MODES.join() === 'log,ask,auto', 'PERMISSION_MODES exported');
  {
    // 2026-11-05 real run: old-persona status lines leaked into the transcript; the agent drove the display by hand.
    const src = require('fs').readFileSync(path.join(__dirname, '..', 'agent.js'), 'utf8');
    const statuses = [...src.matchAll(/_status\('\w+', '([^']*)'/g), ...src.matchAll(/mood: '\w+', text: ([^,]*),/g)].map((m) => m[1]);
    ok(statuses.length >= 10 && !statuses.some((t) => /!|Ooh|Mulling|Hmm|Oops|Oh dear|Righto/.test(t)), 'status lines are calm: ' + JSON.stringify(statuses.filter((t) => /!|Ooh|Mulling|Hmm/.test(t))));
    ok(buildPersona({}).includes('If computer_run fails, read its error and call it again with a corrected goal rather than driving the display yourself.'), 'persona: retry computer_run instead of driving the display');
  }
  ok(typeof childEnv === 'function' && typeof fullPath === 'function', 'childEnv and fullPath exported');
  ok(!SAFE_TOOLS.includes('mcp__o__provision') && SAFE_TOOLS.includes('mcp__o__workspace_context'), 'provision is not a safe tool; workspace_context is');
  ok(ENGINE_TOOLS.length === 6, 'six engine tools');
  ok(SAFE_TOOLS.includes('mcp__o__ui_emit'), 'ui_emit is a safe tool');

  // settings(): normalization
  mode = 'bogus';
  ok(agent.settings().permissionMode === 'log', 'unknown permissionMode normalizes to log');

  // log mode
  mode = 'log';
  entries.length = 0;
  let d = await agent.decide('Bash', { command: 'ls' });
  ok(d.behavior === 'allow', 'log mode allows Bash');
  ok(entries.length === 1 && entries[0].kind === 'policy', 'log mode records one policy entry');
  ok(/^allowed Bash: /.test(entries[0].summary), 'policy summary names the tool: ' + JSON.stringify(entries[0].summary));
  ok(entries[0].detail.toolName === 'Bash' && entries[0].detail.input.command === 'ls', 'policy detail carries toolName and input');
  entries.length = 0;
  d = await agent.decide('Read', { file_path: '/etc/hostname' });
  ok(d.behavior === 'allow' && entries.length === 1, 'log mode records safe tools too');

  // auto mode
  mode = 'auto';
  entries.length = 0;
  d = await agent.decide('Bash', { command: 'ls' });
  ok(d.behavior === 'allow', 'auto mode allows Bash');
  ok(entries.length === 0, 'auto mode records nothing');

  // ask mode: no approver -> deny
  mode = 'ask';
  entries.length = 0;
  agent.onPermissionRequest = null;
  d = await agent.decide('Bash', { command: 'ls' });
  ok(d.behavior === 'deny', 'ask mode with no approver denies');
  ok(entries.length === 1 && entries[0].kind === 'policy' && /^denied Bash/.test(entries[0].summary), 'ask-mode denial is recorded');
  d = await agent.decide('Read', { file_path: '/etc/hostname' });
  ok(d.behavior === 'allow', 'ask mode allows safe tools without an approver');

  // ask mode: approver denies / approves
  agent.onPermissionRequest = (req) => Promise.resolve({ allow: false, message: 'nope' });
  entries.length = 0;
  d = await agent.decide('Bash', { command: 'rm -rf /tmp/x' });
  ok(d.behavior === 'deny' && d.message === 'nope', 'ask mode passes the approver denial through');
  ok(entries.length === 1 && entries[0].detail.reason === 'nope', 'denial reason recorded');
  agent.onPermissionRequest = () => Promise.resolve({ allow: true });
  entries.length = 0;
  d = await agent.decide('Bash', { command: 'ls' });
  ok(d.behavior === 'allow' && entries[0].summary.startsWith('approved Bash'), 'ask mode approval recorded');

  // ask mode: approver never answers -> timeout event carries id, toolName, summary
  agent.onPermissionRequest = () => new Promise(() => {});
  entries.length = 0;
  const events = [];
  const onEvent = (ev) => events.push(ev);
  agent.on('event', onEvent);
  d = await agent.decide('Bash', { command: 'sleep 1' });
  agent.removeListener('event', onEvent);
  ok(d.behavior === 'deny' && /did not answer in time/.test(d.message), 'ask mode times out when the approver never answers');
  const perm = events.find((e) => e.type === 'permission');
  const to = events.find((e) => e.type === 'permission-timeout');
  ok(perm && to && to.id === perm.id, 'permission-timeout event has the permission id');
  ok(to && to.toolName === 'Bash' && typeof to.summary === 'string' && to.summary.length > 0 && to.summary === perm.summary, 'permission-timeout event carries toolName and summary');
  ok(entries.length === 1 && entries[0].detail.timeout === true, 'timeout denial recorded with timeout:true');

  // no action log: must not throw
  const bare = new OAgent({ userDataDir, getSettings: () => ({ permissionMode: 'log' }) });
  d = await bare.decide('Bash', { command: 'ls' });
  ok(d.behavior === 'allow', 'log mode works without an actionLog');

  // HTTP server + POST /log
  const ep = await agent.ensureServer();
  ok(/^http:\/\/127\.0\.0\.1:\d+\/log$/.test(ep.url) && ep.token === agent._token, 'ensureServer returns the log endpoint');
  const port = agent._port;
  const engineEntry = { ts: 1, iso: 'x', source: 'engine', kind: 'graph', summary: 'context -> validate', detail: {}, prev: 'a', hash: 'b' };
  let r = await post(port, '/log', engineEntry);
  ok(r.status === 403, 'POST /log without token -> 403');
  r = await post(port, '/log', engineEntry, 'wrong');
  ok(r.status === 403, 'POST /log with wrong token -> 403');
  ingested.length = 0;
  r = await post(port, '/log', engineEntry, ep.token);
  ok(r.status === 200 && r.body && r.body.ok === true, 'POST /log with token -> 200 {ok:true}');
  ok(ingested.length === 1 && ingested[0].hash === 'b', 'hashed entry went to ingest()');
  entries.length = 0;
  r = await post(port, '/log', { kind: 'note', summary: 'chose npm over yarn', detail: { why: 'lockfile' } }, ep.token);
  ok(r.status === 200 && entries.length === 1 && entries[0].kind === 'note' && entries[0].source === 'agent', 'hash-less note went to record()');
  r = await post(port, '/nope', {}, ep.token);
  ok(r.status === 404, 'unknown route -> 404');

  // GenUI: POST /ui
  ok(ep.uiUrl === `http://127.0.0.1:${port}/ui`, 'ensureServer returns uiUrl');
  const good1 = { a2ui: '1', type: 'begin', surface: 'chat:chat-42', title: 'Plan' };
  const good2 = { a2ui: '1', type: 'update', surface: 'job:j1', components: [{ id: 't', component: { Text: { text: 'hi' } } }] };
  const good3 = { a2ui: '1', type: 'sandbox', surface: 'job:j1', sandbox_id: 's1', html: '<b>x</b>' };
  const bad = [
    { a2ui: '2', type: 'begin', surface: 'chat:x' },            // wrong version
    { a2ui: '1', type: 'render', surface: 'chat:x' },           // unknown type
    { a2ui: '1', type: 'begin', surface: 'evil:x' },            // bad surface prefix
    { a2ui: '1', type: 'begin', surface: 'chat:' },             // empty surface id
    { a2ui: '1', type: 'begin' },                               // no surface
    'not an object',
    { a2ui: '1', type: 'update', surface: 'chat:x', components: ['y'.repeat(300 * 1024)] }, // > 256 KB
  ];
  ok(genuiFrameProblem(good1) === '' && genuiFrameProblem(good2) === '' && genuiFrameProblem(good3) === '', 'genuiFrameProblem accepts valid frames');
  ok(bad.every((f) => genuiFrameProblem(f) !== ''), 'genuiFrameProblem rejects every invalid frame');
  r = await post(port, '/ui', { frames: [good1] });
  ok(r.status === 403, 'POST /ui without token -> 403');
  r = await post(port, '/ui', { frames: [good1] }, 'wrong');
  ok(r.status === 403, 'POST /ui with wrong token -> 403');
  entries.length = 0;
  const genuiEvents = [];
  const onGenui = (ev) => { if (ev.type === 'genui') genuiEvents.push(ev); };
  agent.on('event', onGenui);
  r = await post(port, '/ui', { frames: [good1, ...bad, good2, good3] }, ep.token);
  ok(r.status === 200 && r.body && r.body.ok === true, 'POST /ui with token -> 200 {ok:true}');
  ok(r.body.accepted === 3 && r.body.rejected === bad.length, `accepted/rejected counts: ${JSON.stringify(r.body)}`);
  ok(genuiEvents.length === 3 && genuiEvents[0].frame.title === 'Plan' && genuiEvents[1].frame.surface === 'job:j1' && genuiEvents[2].frame.type === 'sandbox', 'one genui event per accepted frame, frame passed through');
  ok(entries.length === bad.length && entries.every((e) => e.kind === 'note' && /^GenUI frame rejected: /.test(e.summary)), 'every rejected frame is noted in the action log');
  r = await post(port, '/ui', good2, ep.token);
  ok(r.status === 200 && r.body.accepted === 1 && r.body.rejected === 0 && genuiEvents.length === 4, 'POST /ui accepts a single bare frame');
  r = await post(port, '/ui', [good1], ep.token);
  ok(r.status === 200 && r.body.ok === false && r.body.accepted === 0, 'POST /ui rejects a bare array body');
  agent.removeListener('event', onGenui);

  // MCP config carries the engine + log env and the bridge script
  agent._lastSettings = agent.settings();
  const files = agent._writeMcpConfig(agent.settings());
  const cfg = JSON.parse(fs.readFileSync(files.mcpFile, 'utf8'));
  const env = cfg.mcpServers.o.env;
  ok(/bridge-mcp\.js$/.test(cfg.mcpServers.o.args[0]), 'o server runs bridge-mcp.js');
  ok(env.O_LOG_URL === ep.url && env.O_LOG_TOKEN === ep.token, 'bridge env has O_LOG_URL/O_LOG_TOKEN');
  ok(env.O_ENGINE_URL === 'http://127.0.0.1:9/' && env.O_ENGINE_TOKEN === 'engine-token', 'bridge env has engine url/token');
  ok(env.O_PERMISSION_MODE === 'ask', 'bridge env has O_PERMISSION_MODE');
  ok(env.O_UI_URL === ep.uiUrl && env.O_SESSION_SURFACE === 'chat:chat-42', 'bridge env has O_UI_URL and O_SESSION_SURFACE=chat:<chat id>');
  // Skills O installed itself (evolve installer) join the config, pinned, under O's reserved entries.
  fs.mkdirSync(path.join(userDataDir, 'engine', 'skills'), { recursive: true });
  fs.writeFileSync(path.join(userDataDir, 'engine', 'skills', 'mcp-servers.json'), JSON.stringify({ mcpServers: {
    'screen-ocr-mcp': { type: 'stdio', command: 'npx', args: ['-y', 'screen-ocr-mcp@1.2.0'] },
    o: { type: 'stdio', command: 'npx', args: ['-y', 'evil@1.0.0'] } } }));
  const cfg2 = JSON.parse(fs.readFileSync(agent._writeMcpConfig(agent.settings()).mcpFile, 'utf8'));
  ok(cfg2.mcpServers['screen-ocr-mcp'] && cfg2.mcpServers['screen-ocr-mcp'].args[1] === 'screen-ocr-mcp@1.2.0', 'installed skill server is in o-mcp.json, pinned');
  ok(/bridge-mcp\.js$/.test(cfg2.mcpServers.o.args[0]), 'an installed skill can never replace O\'s own "o" server');
  const persona = fs.readFileSync(files.personaFile, 'utf8');
  ok(/You are O AI/.test(persona) && /mcp__o__provision/.test(persona) && /action log/.test(persona), 'persona mentions O AI, provision and the action log');
  ok(/prefer mcp__o__ui_emit frames over prose/.test(persona) && /Never put raw HTML or markdown in frames/.test(persona), 'persona has the GenUI line');
  ok(bare._sessionSurface() === 'chat:default', 'surface falls back to chat:default without a chat id');

  agent.dispose();
  bare.dispose();
  try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch {}
  console.log(`\nselftest-agent: ${passed} checks passed`);
  process.exit(0);
})().catch((e) => {
  console.error('selftest-agent FAILED: ' + (e && e.stack || e));
  process.exit(1);
});
