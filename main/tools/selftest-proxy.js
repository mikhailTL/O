#!/usr/bin/env node
'use strict';
// Selftest for main/proxy.js: runs the proxy against fake-claude.js (FAKE_SIMPLE mode) and an
// in-script engine stub; no network, no real claude. Exits non-zero on any failure.

const http = require('http');
const path = require('path');
const os = require('os');
const fs = require('fs');
const { spawn } = require('child_process');
const { createProxy } = require('../proxy');

const FAKE = path.join(__dirname, 'fake-claude.js');
let failures = 0;
const ok = (cond, msg) => { console.log(`${cond ? 'ok  ' : 'FAIL'} ${msg}`); if (!cond) failures++; };

function request(port, method, p, body, token, raw) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request({ host: '127.0.0.1', port, method, path: p, headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}), ...(data ? { 'content-length': Buffer.byteLength(data) } : {}) } }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => { const txt = Buffer.concat(chunks).toString('utf8'); let j = null; try { j = JSON.parse(txt); } catch (_) {} resolve({ status: res.statusCode, headers: res.headers, text: txt, json: j }); });
    });
    req.on('error', reject);
    req.end(data);
  });
}

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'o-proxy-'));
  const entries = [];
  const actionLog = { record: (e) => entries.push(e) };
  let fakeEnv = { FAKE_SIMPLE: '1' };
  let spawnedAtOnce = 0, liveChildren = 0;
  const systemsSeen = [];
  let spawnCount = 0;
  const spawnImpl = (cmd, args, opts) => {
    spawnCount++;
    const sfIdx = args.findIndex((a) => /system-prompt-file$/.test(a));
    try { systemsSeen.push(fs.readFileSync(args[sfIdx + 1], 'utf8')); } catch (_) { systemsSeen.push(null); }
    const child = spawn(process.execPath, [FAKE, ...args], { ...opts, env: { ...opts.env, ...fakeEnv } });
    liveChildren++; spawnedAtOnce = Math.max(spawnedAtOnce, liveChildren);
    child.on('close', () => { liveChildren--; });
    return child;
  };
  // engine stub for /v1/run
  let lookups = 0;
  const engine = http.createServer((req, res) => {
    const send = (o) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(o)); };
    if (req.headers['x-o-token'] !== 'etok') { res.writeHead(401); return res.end('{}'); }
    if (req.url.startsWith('/context')) return send({ cwd: '/w', shell_history: ['npm i'], tree: {}, config_files: [], telemetry: [] });
    if (req.url === '/cache/lookup') { lookups++; return send({ hit: lookups >= 1, fingerprint: 'abc' }); }
    if (req.url === '/provision') return send({ job_id: 'j1' });
    if (req.url === '/jobs/j1') return send({ id: 'j1', done: true, ok: true, cached: false, steps: [] });
    res.writeHead(404); res.end('{}');
  });
  await new Promise((r) => engine.listen(0, '127.0.0.1', r));
  const engPort = engine.address().port;

  const settings = { proxyEnabled: true, proxyPort: 0, proxyToken: 'secret-token', proxyConcurrency: 4, proxyMode: 'code', claudePath: FAKE, cwd: dir };
  const agent = { userDataDir: dir, writeMcpConfig: async () => ({ mcpFile: null, allowedTools: [], settings: { browserMode: 'link' } }) };
  const proxy = createProxy({ getSettings: () => settings, actionLog, getEngine: () => ({ url: `http://127.0.0.1:${engPort}`, token: 'etok' }), agent, spawnImpl, userDataDir: dir });
  const st = await proxy.start();
  const port = st.port;
  ok(st.running && st.url.startsWith('http://127.0.0.1:'), `proxy listening on loopback (${st.url})`);

  let r = await request(port, 'GET', '/v1/models', null, null);
  ok(r.status === 401, '401 without bearer token');
  r = await request(port, 'GET', '/v1/models', null, 'wrong');
  ok(r.status === 401, '401 with wrong token');
  r = await request(port, 'GET', '/v1/models', null, 'secret-token');
  ok(r.status === 200 && r.json.data.map((m) => m.id).join(',') === 'o-ai,o-ai-code', '/v1/models lists o-ai and o-ai-code');

  fakeEnv = { FAKE_SIMPLE: '1', FAKE_TEXT: 'hello from fake' };
  r = await request(port, 'POST', '/v1/chat/completions', { model: 'o-ai', messages: [{ role: 'system', content: 'be terse' }, { role: 'user', content: 'hi' }] }, 'secret-token');
  ok(r.status === 200 && r.json.object === 'chat.completion', 'non-stream completion returns chat.completion');
  ok(r.json && r.json.choices[0].message.content === 'hello from fake', 'non-stream content is the fake text');
  ok(r.json && r.json.usage.total_tokens === 30 && r.headers['x-o-cost-usd'] === '0.002', 'usage and cost headers present');
  const sys = systemsSeen[systemsSeen.length - 1] || '';
  ok(/Money: you NEVER/.test(sys) && /be terse/.test(sys) && !/You are O AI|glass orb|Style:/.test(sys), 'system prompt: safety kept, caller system merged, persona stripped');

  // stream
  const sse = await new Promise((resolve, reject) => {
    const data = JSON.stringify({ model: 'o-ai', stream: true, messages: [{ role: 'user', content: 'stream please' }] });
    const req = http.request({ host: '127.0.0.1', port, method: 'POST', path: '/v1/chat/completions', headers: { 'content-type': 'application/json', authorization: 'Bearer secret-token', 'content-length': Buffer.byteLength(data) } }, (res) => {
      let txt = ''; res.setEncoding('utf8'); res.on('data', (c) => { txt += c; }); res.on('end', () => resolve({ status: res.statusCode, type: res.headers['content-type'], txt }));
    });
    req.on('error', reject); req.end(data);
  });
  const frames = sse.txt.split('\n\n').filter(Boolean);
  ok(sse.status === 200 && /text\/event-stream/.test(sse.type), 'stream responds with text/event-stream');
  ok(frames.length >= 3 && frames[frames.length - 1] === 'data: [DONE]', `stream has ${frames.length} frames and ends with [DONE]`);
  const chunk = JSON.parse(frames[0].slice(6));
  ok(chunk.object === 'chat.completion.chunk' && chunk.choices[0].delta.role === 'assistant', 'first chunk carries the assistant role');
  const last = JSON.parse(frames[frames.length - 2].slice(6));
  ok(last.choices[0].finish_reason === 'stop', 'final chunk has finish_reason stop');

  // code mode filtering
  fakeEnv = { FAKE_SIMPLE: '1', FAKE_TEXT: 'Sure! Here you go:\n```json\n[{"target_directory":".","command_string":"npm install","timeout_seconds":60}]\n```\nHope that helps!' };
  r = await request(port, 'POST', '/v1/chat/completions', { model: 'o-ai-code', messages: [{ role: 'user', content: 'steps' }] }, 'secret-token');
  ok(r.status === 200 && r.json.choices[0].message.content === '[{"target_directory":".","command_string":"npm install","timeout_seconds":60}]', 'o-ai-code returns only the JSON');

  // tool calls
  fakeEnv = { FAKE_SIMPLE: '1', FAKE_TEXT: 'ran', FAKE_TOOL: '1' };
  r = await request(port, 'POST', '/v1/chat/completions', { model: 'o-ai', messages: [{ role: 'user', content: 'x' }] }, 'secret-token');
  ok(r.json && r.json.choices[0].finish_reason === 'tool_calls' && r.json.choices[0].message.tool_calls[0].function.name === 'Bash', 'tool_use maps to tool_calls');

  // concurrency cap
  fakeEnv = { FAKE_SIMPLE: '1', FAKE_TEXT: 'slow', FAKE_DELAY_MS: '150' };
  spawnedAtOnce = 0;
  await Promise.all([1, 2, 3, 4, 5, 6].map(() => request(port, 'POST', '/v1/chat/completions', { model: 'o-ai', messages: [{ role: 'user', content: 'p' }] }, 'secret-token')));
  ok(spawnedAtOnce <= 4 && spawnedAtOnce >= 2, `concurrency cap honored (max ${spawnedAtOnce} children at once for 6 requests)`);

  // /v1/run
  fakeEnv = { FAKE_SIMPLE: '1', FAKE_TEXT: '[{"target_directory":".","command_string":"npm install","timeout_seconds":60}]' };
  r = await request(port, 'POST', '/v1/run', { objective: 'set up the project' }, 'secret-token');
  ok(r.status === 200 && r.json.job && r.json.job.ok === true, '/v1/run returns the engine JobState');

  // D2: per-request system prompts under queueing (cap 1): each child must see its own caller system text
  settings.proxyConcurrency = 1;
  fakeEnv = { FAKE_SIMPLE: '1', FAKE_TEXT: 'q', FAKE_DELAY_MS: '60' };
  systemsSeen.length = 0;
  await Promise.all(['AAA', 'BBB', 'CCC'].map((sys) => request(port, 'POST', '/v1/chat/completions', { model: 'o-ai', messages: [{ role: 'system', content: sys }, { role: 'user', content: 'x' }] }, 'secret-token')));
  const seen = systemsSeen.map((t) => (t && /AAA/.test(t) ? 'AAA' : t && /BBB/.test(t) ? 'BBB' : t && /CCC/.test(t) ? 'CCC' : '?')).sort().join(',');
  ok(seen === 'AAA,BBB,CCC', `queued requests each read their own system prompt (${seen})`);
  ok(!fs.readdirSync(dir).some((f) => /^o-proxy-system-/.test(f)), 'per-request system files are removed after the run');

  // D1: a client that disconnects while queued never spawns a child
  fakeEnv = { FAKE_SIMPLE: '1', FAKE_TEXT: 'slow', FAKE_DELAY_MS: '200' };
  spawnCount = 0;
  const live = request(port, 'POST', '/v1/chat/completions', { model: 'o-ai', messages: [{ role: 'user', content: 'a' }] }, 'secret-token');
  await new Promise((r) => setTimeout(r, 50));
  const data = JSON.stringify({ model: 'o-ai', messages: [{ role: 'user', content: 'b' }] });
  const abandoned = http.request({ host: '127.0.0.1', port, method: 'POST', path: '/v1/chat/completions', headers: { 'content-type': 'application/json', authorization: 'Bearer secret-token', 'content-length': Buffer.byteLength(data) } });
  abandoned.on('error', () => {});
  abandoned.end(data);
  await new Promise((r) => setTimeout(r, 50));
  abandoned.destroy();
  await live;
  await new Promise((r) => setTimeout(r, 400));
  ok(spawnCount === 1, `disconnect while queued spawns nothing extra (spawns=${spawnCount})`);
  settings.proxyConcurrency = 4;

  // D3: second identical /v1/run objective → cache first, zero model calls
  fakeEnv = { FAKE_SIMPLE: '1', FAKE_TEXT: '[{"target_directory":".","command_string":"npm install","timeout_seconds":60}]' };
  spawnCount = 0;
  r = await request(port, 'POST', '/v1/run', { objective: 'set up the project' }, 'secret-token');
  ok(r.status === 200 && r.json.cached === true && spawnCount === 0, `/v1/run repeat objective is served from the cache with no model call (spawns=${spawnCount})`);

  // action log
  const tasks = entries.filter((e) => e.kind === 'task').length, results = entries.filter((e) => e.kind === 'result').length;
  ok(tasks >= 12 && results >= 12, `action log has task+result entries (${tasks}/${results})`);
  ok(!JSON.stringify(entries).includes('secret-token'), 'bearer token never appears in the action log');
  ok(proxy.status().token === 'secret-token' && !('token' in JSON.parse(JSON.stringify({ ...proxy.status(), token: undefined }))), 'status carries the token for Settings only');

  await proxy.stop();
  engine.close();
  ok(proxy.status().running === false && liveChildren === 0, 'stopped cleanly, no child processes left');
  console.log(failures ? `\n${failures} proxy selftest(s) FAILED` : '\nall proxy selftests passed');
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
