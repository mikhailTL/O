'use strict';
// O's API proxy: a localhost, OpenAI-compatible HTTP server that turns API requests into
// `claude -p` subprocess runs (the user's own login), normalizes stream-json back into
// chat.completion JSON / SSE, strips conversational padding in code mode, and records
// every request in the action log. Loopback only; every route needs the bearer token.
//
//   const proxy = createProxy({ getSettings, actionLog, getEngine, agent });
//   await proxy.start();   proxy.status();   await proxy.stop();

const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const { TokenFilter } = require('./tokenfilter');
const { buildSystemPrompt } = require('./promptmod');
const { childEnv, resolveClaude, spawnClaude, killTree } = require('./agent');

const MODELS = ['o-ai', 'o-ai-code'];
const MAX_TURNS = 40;
const MAX_BODY = 4e6;
const QUEUE_LIMIT = 64;
let systemPromptFlag = null; // '--system-prompt-file' | '--append-system-prompt-file', detected once

function clip(s, n) { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

function detectSystemPromptFlag(claudePath, cb) {
  if (systemPromptFlag) return cb(systemPromptFlag);
  execFile(claudePath, ['--help'], { env: childEnv(), timeout: 8000 }, (err, stdout, stderr) => {
    const help = String(stdout || '') + String(stderr || '');
    systemPromptFlag = /--system-prompt-file/.test(help) ? '--system-prompt-file' : '--append-system-prompt-file';
    cb(systemPromptFlag);
  });
}

/** Flatten OpenAI messages into one prompt for `claude -p` plus the caller's system text. */
function flatten(messages) {
  const system = [];
  const turns = [];
  for (const m of messages || []) {
    if (!m || typeof m !== 'object') continue;
    const content = Array.isArray(m.content)
      ? m.content.map((p) => (p && p.type === 'text' ? p.text : '')).join('\n')
      : String(m.content == null ? '' : m.content);
    if (m.role === 'system') system.push(content);
    else if (m.role === 'user') turns.push(`User: ${content}`);
    else if (m.role === 'assistant') turns.push(`Assistant: ${content}`);
    else if (m.role === 'tool') turns.push(`Tool result (${m.tool_call_id || ''}): ${content}`);
  }
  const last = turns.length ? turns[turns.length - 1] : '';
  const prompt = turns.length <= 1 ? last.replace(/^User: /, '') : turns.join('\n\n') + '\n\nAssistant:';
  return { system: system.join('\n\n'), prompt, lastUser: last.replace(/^User: /, '') };
}

function createProxy({ getSettings, actionLog, getEngine, agent, spawnImpl, userDataDir } = {}) {
  let server = null;
  let running = 0;
  const queue = [];
  const children = new Set();
  const dataDir = userDataDir || (agent && agent.userDataDir) || path.join(require('os').homedir(), '.o');

  const settings = () => {
    const s = (getSettings && getSettings()) || {};
    return {
      enabled: s.proxyEnabled !== false,
      port: Number.isFinite(Number(s.proxyPort)) && s.proxyPort !== '' && s.proxyPort !== undefined && s.proxyPort !== null ? Number(s.proxyPort) : 8787,
      token: String(s.proxyToken || ''),
      concurrency: Math.max(1, Math.min(16, Number(s.proxyConcurrency) || 4)),
      mode: s.proxyMode === 'chat' ? 'chat' : 'code',
      claudePath: resolveClaude(s.claudePath || 'claude'),
      model: s.model || '',
      cwd: s.cwd && fs.existsSync(s.cwd) ? s.cwd : require('os').homedir(),
    };
  };

  const log = (entry) => { try { if (actionLog) actionLog.record(entry); } catch (_) {} };
  // objective -> last successful request (state recovery for /v1/run); small JSON file in userData
  const objectivesFile = path.join(dataDir, 'o-proxy-objectives.json');
  const objectives = {
    _map: null,
    _load() { if (this._map) return; try { this._map = JSON.parse(fs.readFileSync(objectivesFile, 'utf8')); } catch { this._map = {}; } },
    get(k) { this._load(); return this._map[k] || null; },
    set(k, v) { this._load(); this._map[k] = v; try { fs.mkdirSync(dataDir, { recursive: true }); fs.writeFileSync(objectivesFile, JSON.stringify(this._map), { mode: 0o600 }); } catch (_) {} },
  };

  const authorized = (req, token) => {
    const h = String(req.headers.authorization || '');
    const given = h.startsWith('Bearer ') ? h.slice(7).trim() : '';
    const a = Buffer.from(given), b = Buffer.from(token);
    return token.length > 0 && a.length === b.length && crypto.timingSafeEqual(a, b);
  };

  const sendJson = (res, code, obj, headers) => {
    res.writeHead(code, { 'content-type': 'application/json', ...(headers || {}) });
    res.end(JSON.stringify(obj));
  };
  const sendError = (res, code, message, type) => sendJson(res, code, { error: { message, type: type || 'invalid_request_error' } });

  function readBody(req) {
    return new Promise((resolve, reject) => {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', (c) => { body += c; if (body.length > MAX_BODY) { req.destroy(); reject(new Error('body too large')); } });
      req.on('end', () => resolve(body));
      req.on('error', reject);
    });
  }

  // ---- running one claude -p ----
  function schedule(job) {
    return new Promise((resolve, reject) => {
      if (queue.length >= QUEUE_LIMIT) return reject(Object.assign(new Error('proxy queue is full'), { status: 429 }));
      queue.push({ job, resolve, reject });
      pump();
    });
  }
  function pump() {
    const s = settings();
    while (running < s.concurrency && queue.length) {
      const { job, resolve, reject } = queue.shift();
      running += 1;
      Promise.resolve().then(job).then(resolve, reject).finally(() => { running -= 1; pump(); });
    }
  }

  async function prepareFiles(system, mode, id) {
    const files = agent && agent.writeMcpConfig ? await agent.writeMcpConfig() : { mcpFile: null, allowedTools: [] };
    const sys = buildSystemPrompt({ mode, extra: system, settings: files.settings || {} });
    // One file per request: queued requests must not read a prompt written by a later caller.
    const sp = path.join(dataDir, `o-proxy-system-${id || crypto.randomBytes(6).toString('hex')}.md`);
    fs.mkdirSync(dataDir, { recursive: true });
    fs.writeFileSync(sp, sys, { mode: 0o600 });
    return { ...files, systemFile: sp };
  }
  const unlinkQuiet = (f) => { try { if (f) fs.unlinkSync(f); } catch (_) {} };

  /**
   * Run one prompt through claude -p. onText(delta) / onTool(block) stream; resolves with the result frame.
   */
  function runClaude({ prompt, files, s, onText, onTool, signal }) {
    return new Promise((resolve, reject) => {
      if (signal && signal.aborted) { unlinkQuiet(files.systemFile); return reject(Object.assign(new Error('client disconnected'), { aborted: true })); }
      detectSystemPromptFlag(s.claudePath, (flag) => {
        if (signal && signal.aborted) { unlinkQuiet(files.systemFile); return reject(Object.assign(new Error('client disconnected'), { aborted: true })); }
        const args = ['-p', '--output-format', 'stream-json', '--verbose', '--max-turns', String(MAX_TURNS), flag, files.systemFile];
        if (files.mcpFile) args.push('--permission-prompt-tool', 'mcp__o__approve', '--strict-mcp-config', '--mcp-config', files.mcpFile);
        if (files.allowedTools && files.allowedTools.length) args.push('--allowedTools', files.allowedTools.join(','));
        if (s.model) args.push('--model', s.model);
        let child;
        try {
          child = spawnImpl
            ? spawnImpl(s.claudePath, args, { cwd: s.cwd, env: childEnv(), stdio: ['pipe', 'pipe', 'pipe'] })
            : spawnClaude(s.claudePath, args, { cwd: s.cwd, env: childEnv(), stdio: ['pipe', 'pipe', 'pipe'] });
        } catch (e) { return reject(e); }
        children.add(child);
        let buf = '', stderr = '', result = null, done = false;
        const finish = (err) => { if (done) return; done = true; children.delete(child); unlinkQuiet(files.systemFile); if (err) reject(err); else resolve(result || { is_error: true, result: stderr.slice(-600) || 'claude exited without a result' }); };
        if (signal) signal.onabort = () => { killTree(child); finish(Object.assign(new Error('client disconnected'), { aborted: true })); };
        child.on('error', (e) => finish(e));
        child.stdin.on('error', () => {});
        child.stdin.end(prompt);
        child.stderr.setEncoding('utf8');
        child.stderr.on('data', (d) => { stderr = (stderr + d).slice(-8000); });
        child.stdout.setEncoding('utf8');
        const handle = (msg) => {
          if (!msg || typeof msg !== 'object') return;
          if (msg.type === 'assistant' && msg.message && Array.isArray(msg.message.content) && !msg.parent_tool_use_id) {
            for (const block of msg.message.content) {
              if (block.type === 'text' && block.text) onText(block.text);
              else if (block.type === 'tool_use') onTool(block);
            }
          } else if (msg.type === 'result') result = msg;
        };
        child.stdout.on('data', (chunk) => {
          buf += chunk; let i;
          while ((i = buf.indexOf('\n')) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (line) { try { handle(JSON.parse(line)); } catch (_) {} } }
        });
        child.on('close', () => { if (buf.trim()) { try { handle(JSON.parse(buf.trim())); } catch (_) {} } finish(); });
      });
    });
  }

  // ---- routes ----
  async function chatCompletions(req, res, body, s) {
    let p;
    try { p = JSON.parse(body || '{}'); } catch { return sendError(res, 400, 'body is not JSON'); }
    if (!Array.isArray(p.messages) || !p.messages.length) return sendError(res, 400, 'messages[] is required');
    const model = MODELS.includes(p.model) ? p.model : 'o-ai';
    const jsonOnly = model === 'o-ai-code' || !!(p.response_format && p.response_format.type === 'json_object');
    const promptMode = jsonOnly || s.mode === 'code' ? 'code' : 'chat';
    const { system, prompt, lastUser } = flatten(p.messages);
    const id = 'chatcmpl-' + crypto.randomBytes(8).toString('hex');
    const created = Math.floor(Date.now() / 1000);
    const stream = !!p.stream;
    log({ kind: 'task', summary: `API: /v1/chat/completions ${model} ${clip(lastUser, 100)}`, detail: { id, model, stream, messages: p.messages.length } });

    const abort = { onabort: null, aborted: false };
    // The response's 'close' fires when the socket goes away before we finished: the client is gone.
    res.on('close', () => { if (!res.writableEnded) { abort.aborted = true; if (abort.onabort) abort.onabort(); } });

    const filter = jsonOnly ? new TokenFilter() : null;
    let text = '';
    const toolCalls = [];
    const emit = (delta) => {
      if (!stream) return;
      res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`);
    };
    const onText = (t) => {
      const piece = filter ? filter.feed(t) : t;
      if (!piece) return;
      if (!text) emit({ role: 'assistant', content: piece }); else emit({ content: piece });
      text += piece;
    };
    const onTool = (block) => {
      const tc = { index: toolCalls.length, id: block.id, type: 'function', function: { name: block.name, arguments: JSON.stringify(block.input || {}) } };
      toolCalls.push(tc);
      emit({ tool_calls: [tc] });
    };

    try {
      const files = await prepareFiles(system, promptMode, id);
      if (stream) res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
      const result = await schedule(() => runClaude({ prompt, files, s, onText, onTool, signal: abort }));
      const finish = toolCalls.length ? 'tool_calls' : 'stop';
      const usage = (result && result.usage) ? { prompt_tokens: result.usage.input_tokens || 0, completion_tokens: result.usage.output_tokens || 0, total_tokens: (result.usage.input_tokens || 0) + (result.usage.output_tokens || 0) } : { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
      const isErr = !!(result && result.is_error);
      if (isErr && !text) text = String(result.result || 'claude returned an error');
      log({ kind: 'result', summary: `API: ${model} ${isErr ? 'failed' : 'ok'} (${text.length} chars${toolCalls.length ? ', ' + toolCalls.length + ' tool calls' : ''})`, detail: { id, costUsd: result && result.total_cost_usd, durationMs: result && result.duration_ms, is_error: isErr } });
      if (stream) {
        res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: {}, finish_reason: finish }], usage })}\n\n`);
        res.write('data: [DONE]\n\n');
        return res.end();
      }
      const message = { role: 'assistant', content: text || null };
      if (toolCalls.length) message.tool_calls = toolCalls;
      return sendJson(res, isErr ? 502 : 200, { id, object: 'chat.completion', created, model, choices: [{ index: 0, message, finish_reason: finish }], usage },
        { 'x-o-cost-usd': String((result && result.total_cost_usd) || 0), 'x-o-duration-ms': String((result && result.duration_ms) || 0) });
    } catch (e) {
      log({ kind: 'result', summary: `API: ${model} error: ${clip(e.message, 120)}`, detail: { id, aborted: !!e.aborted } });
      if (res.writableEnded || e.aborted) return;
      if (stream && res.headersSent) { res.write(`data: ${JSON.stringify({ error: { message: e.message } })}\n\n`); return res.end(); }
      return sendError(res, e.status || 500, e.message, 'server_error');
    }
  }

  // O-native: objective -> engine context -> cache or one code-mode plan -> engine provision
  async function run(req, res, body, s) {
    let p;
    try { p = JSON.parse(body || '{}'); } catch { return sendError(res, 400, 'body is not JSON'); }
    if (!p.objective) return sendError(res, 400, 'objective is required');
    const eng = getEngine && getEngine();
    if (!eng || !eng.url) return sendError(res, 503, 'The O engine is not running (see Settings → Engine)', 'server_error');
    const call = (method, p2, b) => engineCall(eng, method, p2, b);
    log({ kind: 'task', summary: `API: /v1/run ${clip(p.objective, 100)}`, detail: { cwd: p.cwd || null } });
    const cwd = p.cwd || s.cwd;
    const objKey = crypto.createHash('sha256').update(cwd + '\n' + p.objective).digest('hex');
    let closed = false;
    res.on('close', () => { if (!res.writableEnded) closed = true; });
    try {
      // State recovery first: a known objective whose script is cached runs with no model call at all.
      let request = objectives.get(objKey);
      let cached = false;
      if (request) {
        const lookup = await call('POST', '/cache/lookup', { request });
        cached = !!(lookup && lookup.hit);
        if (cached) log({ kind: 'cache', summary: `API: /v1/run cache hit for ${clip(p.objective, 80)} — no model call`, detail: { fingerprint: lookup.fingerprint } });
      }
      if (!cached) {
        const ctx = await call('GET', '/context?cwd=' + encodeURIComponent(cwd));
        const summary = `Workspace: ${clip(JSON.stringify(ctx).slice(0, 4000), 1500)}`;
        const files = await prepareFiles('', 'code', 'run-' + objKey.slice(0, 12));
        const prompt = `${summary}\n\nObjective: ${p.objective}\nReturn ONLY a JSON array of steps {target_directory, command_string, timeout_seconds}.`;
        const filter = new TokenFilter();
        const abort = { aborted: false, onabort: null };
        res.on('close', () => { if (!res.writableEnded) { abort.aborted = true; if (abort.onabort) abort.onabort(); } });
        await schedule(() => runClaude({ prompt, files, s, onText: (t) => filter.feed(t), onTool: () => {}, signal: abort }));
        let steps = null;
        try { steps = JSON.parse(filter.text || 'null'); } catch { steps = null; }
        if (!Array.isArray(steps) || !steps.length) return sendError(res, 502, 'the model did not return a step list', 'server_error');
        request = { label: clip(p.objective, 180), steps };
      }
      const job = await call('POST', '/provision', request);
      let state;
      for (let i = 0; i < 3600 && !closed; i++) {
        state = await call('GET', `/jobs/${job.job_id}`);
        if (state && state.done) break;
        await new Promise((r) => setTimeout(r, 1000));
      }
      if (state && state.ok) objectives.set(objKey, request);
      log({ kind: 'result', summary: `API: /v1/run ${state && state.ok ? 'ok' : closed ? 'client left' : 'failed'} (${cached ? 'cached' : 'fresh'})`, detail: { job_id: job.job_id } });
      if (closed) return;
      return sendJson(res, state && state.ok ? 200 : 502, { job: state, cached });
    } catch (e) {
      log({ kind: 'result', summary: `API: /v1/run error: ${clip(e.message, 120)}` });
      return sendError(res, e.status || 502, e.message, 'server_error');
    }
  }

  function engineCall(eng, method, p, body) {
    return new Promise((resolve, reject) => {
      const u = new URL(p, eng.url);
      const data = body ? JSON.stringify(body) : null;
      const req = http.request({ hostname: u.hostname, port: u.port, path: u.pathname + u.search, method, headers: { 'content-type': 'application/json', 'x-o-token': eng.token, ...(data ? { 'content-length': Buffer.byteLength(data) } : {}) }, timeout: 60000 }, (r) => {
        const chunks = [];
        r.on('data', (c) => chunks.push(c));
        r.on('end', () => {
          const txt = Buffer.concat(chunks).toString('utf8');
          let j; try { j = JSON.parse(txt); } catch { j = { detail: txt }; }
          if (r.statusCode >= 400) return reject(Object.assign(new Error(`engine ${r.statusCode}: ${clip(j.detail || txt, 200)}`), { status: r.statusCode }));
          resolve(j);
        });
      });
      req.on('timeout', () => { req.destroy(new Error('engine timeout')); });
      req.on('error', reject);
      req.end(data);
    });
  }

  async function onRequest(req, res) {
    const s = settings();
    const u = new URL(req.url, 'http://127.0.0.1');
    if (!authorized(req, s.token)) return sendError(res, 401, 'missing or invalid bearer token', 'authentication_error');
    if (req.method === 'GET' && u.pathname === '/health') return sendJson(res, 200, { ok: true, running, queued: queue.length, concurrency: s.concurrency, mode: s.mode });
    if (req.method === 'GET' && u.pathname === '/v1/models') return sendJson(res, 200, { object: 'list', data: MODELS.map((id) => ({ id, object: 'model', owned_by: 'o-ai' })) });
    if (req.method === 'POST' && u.pathname === '/v1/chat/completions') { let body; try { body = await readBody(req); } catch (e) { return sendError(res, 413, e.message); } return chatCompletions(req, res, body, s); }
    if (req.method === 'POST' && u.pathname === '/v1/run') { let body; try { body = await readBody(req); } catch (e) { return sendError(res, 413, e.message); } return run(req, res, body, s); }
    return sendError(res, 404, `no route ${req.method} ${u.pathname}`);
  }

  return {
    start() {
      return new Promise((resolve, reject) => {
        if (server) return resolve(status());
        const s = settings();
        if (!s.enabled) return resolve(status());
        if (!s.token) return reject(new Error('proxyToken is empty; refusing to listen'));
        const srv = http.createServer((req, res) => { onRequest(req, res).catch((e) => { try { sendError(res, 500, e.message, 'server_error'); } catch (_) {} }); });
        srv.on('error', (e) => { if (!server) reject(e); });
        srv.listen(s.port, '127.0.0.1', () => { server = srv; log({ kind: 'note', summary: `API proxy listening on http://127.0.0.1:${srv.address().port}/v1` }); resolve(status()); });
      });
    },
    stop() {
      return new Promise((resolve) => {
        for (const c of children) killTree(c);
        children.clear();
        queue.splice(0).forEach(({ reject }) => reject(new Error('proxy stopped')));
        if (!server) return resolve();
        const srv = server; server = null;
        srv.close(() => { log({ kind: 'note', summary: 'API proxy stopped' }); resolve(); });
      });
    },
    status,
  };

  function status() {
    const s = settings();
    const addr = server && server.address();
    return { enabled: s.enabled, running: !!server, port: addr ? addr.port : s.port, url: `http://127.0.0.1:${addr ? addr.port : s.port}/v1`, token: s.token, concurrency: s.concurrency, mode: s.mode, active: running, queued: queue.length, children: children.size };
  }
}

module.exports = { createProxy, flatten, MODELS };
