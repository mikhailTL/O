'use strict';
// Demo backend for the recording stage: the REAL Python engine (`python3 -m o_engine serve`), the REAL
// action log (main/actionlog.js) and the REAL API proxy (main/proxy.js) with `claude` replaced by
// main/tools/fake-claude.js (FAKE_SIMPLE), so no Claude login is needed. A tiny relay server stands in
// for main.js's /log and /ui endpoints: engine entries are re-chained into the app log and GenUI frames
// are handed back to the stage as the same `{type:'log', entry}` / `{type:'genui', frame}` events main.js
// broadcasts to the windows.
//
//   const backend = await startBackend({ dataDir });
//   backend.on('event', (ev) => ...);           // {type:'log'|'genui', ...}
//   await backend.engine.call('POST', '/provision', req);
//   await backend.stop();

const http = require('http');
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const { spawn } = require('child_process');
const { EventEmitter } = require('events');

const ROOT = path.resolve(__dirname, '..', '..');
const { ActionLog } = require(path.join(ROOT, 'main', 'actionlog.js'));
const { createProxy } = require(path.join(ROOT, 'main', 'proxy.js'));
const { genuiFrameProblem } = require(path.join(ROOT, 'main', 'agent.js'));
const FAKE_CLAUDE = path.join(ROOT, 'main', 'tools', 'fake-claude.js');
const FAKE_CLAUDE_CU = path.join(__dirname, 'fake-claude-cu.js');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = require('net').createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => { const { port } = srv.address(); srv.close(() => resolve(port)); });
  });
}

function httpJson({ port, method, pathname, body, headers, timeoutMs = 60000 }) {
  return new Promise((resolve, reject) => {
    const payload = body == null ? null : Buffer.from(JSON.stringify(body));
    const req = http.request({ host: '127.0.0.1', port, method, path: pathname, headers: { accept: 'application/json', ...(headers || {}), ...(payload ? { 'content-type': 'application/json', 'content-length': payload.length } : {}) } }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let data = text; try { data = text ? JSON.parse(text) : null; } catch (_) {}
        if (res.statusCode >= 200 && res.statusCode < 300) return resolve(data);
        const err = new Error(`${method} ${pathname} -> ${res.statusCode} ${typeof data === 'string' ? data.slice(0, 200) : JSON.stringify(data).slice(0, 200)}`);
        err.status = res.statusCode; err.body = data; reject(err);
      });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`${method} ${pathname} timed out`)));
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

/**
 * @param {object} opts
 * @param {string} opts.dataDir        fresh folder: app log, engine data dir, sandbox root, proxy files
 * @param {string} [opts.cuTree]       JSON file with a scripted accessibility tree (O_CU_FAKE_TREE) for D4
 * @param {object} [opts.fakeEnv]      extra env for fake-claude (FAKE_TEXT, FAKE_DELAY_MS, ...)
 */
async function startBackend({ dataDir, cuTree, fakeEnv } = {}) {
  dataDir = dataDir || fs.mkdtempSync(path.join(os.tmpdir(), 'o-demo-'));
  fs.mkdirSync(dataDir, { recursive: true });
  const engineData = path.join(dataDir, 'engine');
  const sandboxRoot = path.join(dataDir, 'sandbox');
  fs.mkdirSync(engineData, { recursive: true });
  fs.mkdirSync(sandboxRoot, { recursive: true });

  const events = new EventEmitter();
  const actionLog = new ActionLog(dataDir);
  actionLog.on('entry', (entry) => events.emit('event', { type: 'log', entry }));

  // ---- relay: what main.js (agent.js _onHttp) offers the engine: POST /log and POST /ui
  const relayToken = crypto.randomBytes(16).toString('hex');
  const relay = http.createServer((req, res) => {
    const reply = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)); };
    if (req.method !== 'POST' || !['/log', '/ui'].includes(req.url)) return reply(404, { message: 'not found' });
    if ((req.headers['x-o-token'] || '') !== relayToken) return reply(403, { message: 'bad token' });
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      let p; try { p = JSON.parse(body); } catch { return reply(400, { message: 'bad request' }); }
      if (req.url === '/log') {
        try { if (p && p.hash) actionLog.ingest(p); else actionLog.record({ kind: (p && p.kind) || 'note', summary: String((p && p.summary) || ''), detail: (p && p.detail) || {} }); } catch (e) { return reply(200, { ok: false, error: e.message }); }
        return reply(200, { ok: true });
      }
      const frames = Array.isArray(p && p.frames) ? p.frames : [p];
      let accepted = 0, rejected = 0;
      for (const frame of frames) {
        const problem = genuiFrameProblem(frame);
        if (problem) { rejected++; actionLog.record({ kind: 'note', summary: 'GenUI frame rejected: ' + problem }); continue; }
        accepted++;
        events.emit('event', { type: 'genui', frame });
      }
      reply(200, { ok: true, accepted, rejected });
    });
  });
  await new Promise((r) => relay.listen(0, '127.0.0.1', r));
  const relayPort = relay.address().port;

  // ---- the real engine
  const engineToken = crypto.randomBytes(24).toString('hex');
  const enginePort = await freePort();
  const engineEnv = {
    ...process.env,
    O_ENGINE_TOKEN: engineToken,
    O_LOG_URL: `http://127.0.0.1:${relayPort}/log`,
    O_LOG_TOKEN: relayToken,
    O_UI_URL: `http://127.0.0.1:${relayPort}/ui`,
    O_SANDBOX_ROOT: sandboxRoot,
    O_PERMISSION_MODE: 'log',
    O_MAX_ATTEMPTS: '3',
    O_CU_CANDIDATES: '1',
    O_CU_MAX_HEALS: '3',
    O_HIGH_RISK_SANDBOX: 'local',
    O_SWARM: '0',
    O_GENUI: '1',
    O_VLLM_URL: 'http://127.0.0.1:1/v1', // nothing listens: the local parser/judge fail fast, Claude Code (faked) answers
    O_CLAUDE_PATH: FAKE_CLAUDE_CU,
    FAKE_CU_STATE: path.join(dataDir, 'fake-cu-state.json'),
    O_MCP_SERVERS_FILE: path.join(engineData, 'mcp-servers.json'),
    PYTHONPATH: path.join(ROOT, 'engine') + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : ''),
    PYTHONUNBUFFERED: '1',
  };
  for (const k of Object.keys(engineEnv)) if (/^(CLAUDECODE|CLAUDE_CODE_)/.test(k)) delete engineEnv[k];
  if (cuTree) engineEnv.O_CU_FAKE_TREE = cuTree;
  fs.writeFileSync(engineEnv.O_MCP_SERVERS_FILE, '{"mcpServers":{}}');
  const python = process.env.O_DEMO_PYTHON || 'python3';
  const engineProc = spawn(python, ['-m', 'o_engine', 'serve', '--port', String(enginePort), '--data-dir', engineData], {
    cwd: path.join(ROOT, 'engine'), env: engineEnv, stdio: ['ignore', 'pipe', 'pipe'],
  });
  const stderr = [];
  const onText = (b) => { for (const l of String(b).split(/\r?\n/)) if (l.trim()) { stderr.push(l); if (stderr.length > 200) stderr.shift(); if (process.env.O_DEMO_VERBOSE) console.error('[engine]', l); } };
  engineProc.stdout.on('data', onText);
  engineProc.stderr.on('data', onText);
  const engine = {
    port: enginePort,
    url: `http://127.0.0.1:${enginePort}`,
    token: engineToken,
    dataDir: engineData,
    sandboxRoot,
    call: (method, pathname, body, timeoutMs) => httpJson({ port: enginePort, method, pathname, body, headers: { 'x-o-token': engineToken }, timeoutMs }),
    async waitJob(jobId, { onState, pollMs = 150, timeoutMs = 120000 } = {}) {
      const t0 = Date.now();
      let last = null;
      for (;;) {
        const st = await this.call('GET', `/jobs/${jobId}`);
        if (onState && JSON.stringify(st) !== JSON.stringify(last)) onState(st);
        last = st;
        if (st && st.done) return st;
        if (Date.now() - t0 > timeoutMs) throw new Error(`job ${jobId} did not finish in ${timeoutMs} ms`);
        await sleep(pollMs);
      }
    },
  };
  const t0 = Date.now();
  let health = null;
  while (Date.now() - t0 < 30000) {
    try { health = await engine.call('GET', '/health', null, 3000); if (health && health.ok) break; } catch (_) {}
    if (engineProc.exitCode != null) throw new Error('engine exited: ' + stderr.slice(-10).join('\n'));
    await sleep(250);
  }
  if (!health || !health.ok) throw new Error('engine did not answer /health: ' + stderr.slice(-10).join('\n'));
  actionLog.record({ kind: 'note', summary: `engine ready on port ${enginePort} (vllm ${health.vllm ? 'ok' : 'down'}, screenpipe ${health.screenpipe ? 'ok' : 'down'})`, detail: { health, port: enginePort } });

  // ---- the real API proxy over fake-claude
  const proxyToken = 'o-demo-' + crypto.randomBytes(12).toString('hex');
  const proxyPort = await freePort();
  let fakeEnvNow = { FAKE_SIMPLE: '1', ...(fakeEnv || {}) };
  const settings = { proxyEnabled: true, proxyPort, proxyToken, proxyConcurrency: 2, proxyMode: 'code', claudePath: FAKE_CLAUDE, cwd: sandboxRoot };
  const agent = { userDataDir: dataDir, writeMcpConfig: async () => ({ mcpFile: null, allowedTools: [], settings: { browserMode: 'link' } }) };
  const spawnImpl = (cmd, args, opts) => spawn(process.execPath, [FAKE_CLAUDE, ...args], { ...opts, env: { ...opts.env, ...fakeEnvNow } });
  const proxy = createProxy({ getSettings: () => settings, actionLog, getEngine: () => ({ url: engine.url, token: engine.token }), agent, spawnImpl, userDataDir: dataDir });
  await proxy.start();

  const backend = {
    dataDir, actionLog, engine, relayPort, health,
    proxy: { port: proxyPort, url: `http://127.0.0.1:${proxyPort}/v1`, token: proxyToken, status: () => proxy.status(), setFake: (env) => { fakeEnvNow = { FAKE_SIMPLE: '1', ...(env || {}) }; } },
    on: (...a) => events.on(...a),
    off: (...a) => events.off(...a),
    stderr,
    async stop() {
      try { await proxy.stop(); } catch (_) {}
      try { engineProc.kill('SIGINT'); } catch (_) {}
      await Promise.race([new Promise((r) => engineProc.on('exit', r)), sleep(3000)]);
      if (engineProc.exitCode == null) { try { engineProc.kill('SIGKILL'); } catch (_) {} }
      await new Promise((r) => relay.close(r));
    },
  };
  return backend;
}

module.exports = { startBackend, httpJson, sleep, ROOT };

if (require.main === module) {
  // Smoke run: start everything, run one provisioning job twice (second is a cache replay), print and stop.
  (async () => {
    const b = await startBackend({});
    b.on('event', (ev) => console.log(ev.type === 'log' ? `[log] ${ev.entry.source}/${ev.entry.kind}: ${ev.entry.summary}` : `[genui] ${ev.frame.type} ${ev.frame.surface}`));
    const req = { label: 'demo', steps: [{ target_directory: '.', command_string: 'mkdir -p demo-app && echo "hello" > demo-app/README.md', timeout_seconds: 30 }, { target_directory: 'demo-app', command_string: 'ls -la', timeout_seconds: 30 }] };
    for (let i = 0; i < 2; i++) {
      const { job_id } = await b.engine.call('POST', '/provision', req);
      const st = await b.engine.waitJob(job_id);
      console.log('JOB', i, st.ok, st.cached, st.phase, st.steps.map((s) => s.exit_code));
    }
    console.log('chain', b.actionLog.verifyChain(), 'proxy', b.proxy.url);
    await b.stop();
  })().catch((e) => { console.error(e); process.exit(1); });
}
