'use strict';
// O's engine: the Python `o_engine` FastAPI server, run as a child of the app on a free local port.
// Finds python, spawns `python -m o_engine serve`, waits for /health, restarts with backoff when it
// dies, and offers call(method, path, body) over plain node http with the shared token.

const { spawn, spawnSync } = require('child_process');
const { EventEmitter } = require('events');
const crypto = require('crypto');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');

const IS_WIN = process.platform === 'win32';
const HEALTH_TIMEOUT_MS = 20000;
const HEALTH_POLL_MS = 400;
const CALL_TIMEOUT_MS = 60000;
const BACKOFF_S = [1, 2, 4, 8, 16];
const MAX_RESTARTS = 5;
const STDERR_RING = 200;

// Child env from agent.js: strips CLAUDECODE/CLAUDE_CODE_*/ELECTRON_RUN_AS_NODE and sets PATH=fullPath().
// Fallback keeps engine.js loadable if agent.js is missing.
let childEnv;
let installedSkillServers = () => ({});
try { ({ childEnv } = require('./agent')); } catch (_) { /* fallback below */ }
try { ({ installedSkillServers } = require('./agent')); } catch (_) { /* no installed skills */ }
if (typeof childEnv !== 'function') childEnv = (extra) => ({ ...process.env, ...extra });
let evolveEnv = () => ({});
try { ({ evolveEnv } = require('./scheduler')); } catch (_) { /* engine still starts without evolve settings */ }

function killTree(child) {
  const pid = child && child.pid;
  if (!pid) return;
  if (IS_WIN) {
    try {
      spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
        .on('error', () => { try { child.kill(); } catch {} });
    } catch { try { child.kill(); } catch {} }
    return;
  }
  const sig = (s) => { try { process.kill(-pid, s); } catch { try { child.kill(s); } catch {} } };
  sig('SIGINT');
  setTimeout(() => sig('SIGKILL'), 1500).unref();
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function pythonWorks(cmd, args) {
  try {
    const r = spawnSync(cmd, [...args, '-c', 'import sys; print(sys.version_info[0]*100+sys.version_info[1])'], {
      encoding: 'utf8', timeout: 8000, windowsHide: true, env: childEnv(),
    });
    if (r.status !== 0) return false;
    const v = parseInt(String(r.stdout).trim(), 10);
    return Number.isFinite(v) && v >= 310;
  } catch (_) { return false; }
}

/** Resolve the python to use: the setting, else python3 / python on PATH (Windows: also `py -3`). */
function resolvePython(setting) {
  const candidates = [];
  if (setting && String(setting).trim()) candidates.push({ cmd: String(setting).trim(), args: [] });
  if (IS_WIN) candidates.push({ cmd: 'py', args: ['-3'] });
  candidates.push({ cmd: 'python3', args: [] }, { cmd: 'python', args: [] });
  for (const c of candidates) if (pythonWorks(c.cmd, c.args)) return c;
  return null;
}

/** O_PROXY_URL / O_PROXY_TOKEN / O_FAST_MODEL for the engine, from the same settings main/proxy.js reads. */
function proxyEnv(s) {
  const on = s.proxyEnabled !== false && !!String(s.proxyToken || '');
  const n = Number(s.proxyPort);
  const port = Number.isFinite(n) && s.proxyPort !== '' && s.proxyPort !== undefined && s.proxyPort !== null ? n : 8787;
  return {
    O_PROXY_URL: on ? `http://127.0.0.1:${port}/v1` : '',
    O_PROXY_TOKEN: on ? String(s.proxyToken) : '',
    O_FAST_MODEL: typeof s.fastModel === 'string' && s.fastModel.trim() ? s.fastModel.trim() : 'haiku',
  };
}

class EngineProcess extends EventEmitter {
  /**
   * @param {object} opts
   * @param {string} opts.userDataDir   app userData folder (engine data goes in <userData>/engine)
   * @param {() => object} opts.getSettings  returns the current settings object
   * @param {string} opts.logUrl        app URL the engine POSTs log entries to (O_LOG_URL)
   * @param {string} opts.logToken      token the engine sends with them (O_LOG_TOKEN)
   * @param {string} [opts.uiUrl]       app URL the engine POSTs GenUI frames to (O_UI_URL, same token as the log)
   * @param {string} [opts.appDir]      app root (default: parent of main/); engine code lives in <appDir>/engine
   */
  constructor({ userDataDir, getSettings, logUrl, logToken, uiUrl, appDir } = {}) {
    super();
    this.userDataDir = userDataDir;
    this.getSettings = typeof getSettings === 'function' ? getSettings : () => ({});
    this.logUrl = logUrl || '';
    this.logToken = logToken || '';
    this.uiUrl = uiUrl || '';
    this.appDir = appDir || path.join(__dirname, '..');
    this.engineDir = path.join(this.appDir, 'engine');
    this.dataDir = path.join(userDataDir || '', 'engine');
    this.token = crypto.randomBytes(24).toString('hex');
    this.child = null;
    this.port = 0;
    this.python = null;
    this.ready = false;
    this.restarts = 0;      // lifetime restart count (status())
    this._streak = 0;       // consecutive failed starts, drives backoff and the dead limit
    this.lastHealth = null;
    this.lastError = null;
    this.stderr = [];
    this._disposed = false;
    this._starting = false;
    this._restartTimer = null;
    this._healthTimer = null;
  }

  get url() { return this.port ? `http://127.0.0.1:${this.port}` : ''; }

  _log(summary, detail) {
    this.emit('log', { kind: 'note', summary, detail: detail || {} });
  }

  /** Write the user's MCP servers (settings.mcpServers JSON) for the engine's router; returns the file path. */
  mcpServersFile(s) {
    const file = path.join(this.dataDir, 'mcp-servers.json');
    try {
      const j = JSON.parse((s && s.mcpServers) || '{}');
      // Pinned servers O installed itself (evolve skills) under the person's own; theirs win a name clash.
      const merged = { ...installedSkillServers(this.userDataDir), ...(j && typeof j === 'object' && !Array.isArray(j) ? j : {}) };
      fs.mkdirSync(this.dataDir, { recursive: true });
      fs.writeFileSync(file, JSON.stringify({ mcpServers: merged }, null, 2), { mode: 0o600 });
    } catch (_) { /* malformed JSON: the engine sees an empty file */ }
    return file;
  }

  _env() {
    const s = this.getSettings() || {};
    return childEnv({
      O_ENGINE_TOKEN: this.token,
      O_LOG_URL: this.logUrl,
      O_LOG_TOKEN: this.logToken,
      O_UI_URL: this.uiUrl || '',
      O_VLLM_URL: s.vllmUrl || 'http://127.0.0.1:8000/v1',
      O_VLLM_MODEL: s.vllmModel || '',
      // O's API proxy (main/proxy.js): the engine's headless browser-use agent runs on it, with the fast model,
      // when no vLLM answers. Same URL and token rules as proxy.js; '' when the proxy is switched off.
      ...proxyEnv(s),
      O_SCREENPIPE_DB: s.screenpipeDb || '',
      O_SANDBOX_ROOT: s.sandboxRoot || '',
      O_PERMISSION_MODE: s.permissionMode || 'log',
      O_MAX_ATTEMPTS: String(s.maxRemediationAttempts || 3),
      O_CLAUDE_PATH: s.claudePath || 'claude',
      O_CU_CANDIDATES: String(s.cuCandidates || 1),
      O_CU_MAX_HEALS: String(s.cuMaxHeals || 3),
      O_VLLM_VISION_MODEL: s.vllmVisionModel || '',
      O_CU_STEPS_PER_TURN: String(s.cuStepsPerTurn || 5),
      O_CU_MAX_TURNS: String(s.cuMaxTurns || 20),
      O_CU_GROUND_CONFIDENCE: String(Number.isFinite(s.cuGroundConfidence) ? s.cuGroundConfidence : 0.3),
      O_CU_STEP_CANDIDATES: String(s.cuStepCandidates || 1),
      O_CU_FEASIBILITY: s.cuFeasibilityGate === false ? '0' : '1',
      O_CU_ZOOM: s.cuZoomGrounding === false ? '0' : '1',
      O_CU_COMPARATIVE: s.cuComparativeRanking === false ? '0' : '1',
      O_CU_INTERFACE_BUILDER: s.cuInterfaceBuilder === false ? '0' : '1',
      O_CU_MAX_MODEL_CALLS: String(Number.isInteger(s.cuMaxModelCalls) ? s.cuMaxModelCalls : 12),
      O_GRANTED_FOLDERS: JSON.stringify(Array.isArray(s.grantedFolders) ? s.grantedFolders : []),
      O_MCP_SERVERS_FILE: this.mcpServersFile(s),
      O_HIGH_RISK_SANDBOX: s.highRiskSandbox || 'docker',
      E2B_API_KEY: s.e2bApiKey || '',
      O_DOCKER_IMAGE: s.dockerImage || 'python:3.12-slim',
      O_DOCKER_NETWORK: s.dockerNetwork || 'bridge',
      O_SWARM: s.swarmEnabled === false ? '0' : '1',
      O_GENUI: s.genuiEnabled === false ? '0' : '1',
      // Ambient sensing: a sensor runs only when the master switch and its own toggle are both on.
      O_AMBIENT: s.ambientEnabled === true ? '1' : '0',
      O_SENSOR_WIFI: s.ambientEnabled === true && s.sensorWifi === true ? '1' : '0',
      O_SENSOR_SONAR: s.ambientEnabled === true && s.sensorSonar === true ? '1' : '0',
      O_SENSOR_CAMERA: s.ambientEnabled === true && s.sensorCamera === true ? '1' : '0',
      O_SENSOR_HEART_RATE: s.ambientEnabled === true && s.sensorHeartRate === true ? '1' : '0',
      O_ABSENT_AFTER_S: String(Math.max(1, Math.min(120, parseInt(s.absentAfterMin, 10) || 5)) * 60),
      O_BRIEF_TTS: s.briefSpeak === true ? '1' : '0',
      ...evolveEnv(s, this.userDataDir), // plan L: self-evolution budget, auto-ship tier, GitHub token
      PYTHONPATH: this.engineDir + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : ''),
      PYTHONUNBUFFERED: '1',
    });
  }

  /** Start the engine (idempotent). Resolves when the child is spawned; 'ready' fires after /health is ok. */
  async start() {
    if (this._disposed || this.child || this._starting) return;
    this._starting = true;
    try {
      this.python = this.python || resolvePython((this.getSettings() || {}).enginePython);
      if (!this.python) {
        this.lastError = 'python 3.10+ not found (set enginePython in Settings)';
        this._log(`engine: ${this.lastError}`);
        this.emit('dead', { error: this.lastError });
        return;
      }
      this.port = await freePort();
      const args = [...this.python.args, '-m', 'o_engine', 'serve', '--port', String(this.port), '--data-dir', this.dataDir];
      let child;
      try {
        child = spawn(this.python.cmd, args, {
          cwd: this.engineDir,
          env: this._env(),
          stdio: ['ignore', 'pipe', 'pipe'],
          windowsHide: true,
          detached: !IS_WIN,
        });
      } catch (err) {
        this.lastError = `spawn failed: ${err.message}`;
        this._log(`engine: ${this.lastError}`);
        this._scheduleRestart();
        return;
      }
      this.child = child;
      this.ready = false;
      this.lastError = null;
      this._log(`engine started (pid ${child.pid}, port ${this.port})`, { pid: child.pid, port: this.port, python: this.python.cmd });
      const onText = (buf) => {
        for (const line of String(buf).split(/\r?\n/)) {
          if (!line.trim()) continue;
          this.stderr.push(line);
          if (this.stderr.length > STDERR_RING) this.stderr.splice(0, this.stderr.length - STDERR_RING);
          this.emit('stderr', line);
        }
      };
      child.stdout.on('data', onText);
      child.stderr.on('data', onText);
      child.on('error', (err) => { this.lastError = err.message; });
      child.on('exit', (code, signal) => {
        if (this.child !== child) return;
        this.child = null;
        const wasReady = this.ready;
        this.ready = false;
        this._stopHealthPoll();
        if (this._disposed) return;
        this.lastError = `engine exited (code ${code}, signal ${signal})`;
        this.emit('exit', { code, signal, wasReady });
        this._scheduleRestart();
      });
      this._pollHealth();
    } finally {
      this._starting = false;
    }
  }

  _scheduleRestart() {
    if (this._disposed || this._restartTimer) return;
    if (this._streak >= MAX_RESTARTS) {
      this._log(`engine dead after ${this._streak} restarts: ${this.lastError || 'unknown error'}`, { stderr: this.stderr.slice(-20) });
      this.emit('dead', { error: this.lastError, restarts: this.restarts });
      return;
    }
    const wait = BACKOFF_S[Math.min(this._streak, BACKOFF_S.length - 1)];
    this._streak++;
    this.restarts++;
    this._log(`engine restart ${this._streak}/${MAX_RESTARTS} in ${wait}s: ${this.lastError || ''}`.trim(), { stderr: this.stderr.slice(-20) });
    this._restartTimer = setTimeout(() => {
      this._restartTimer = null;
      this.start().catch((err) => { this.lastError = err.message; this._scheduleRestart(); });
    }, wait * 1000);
  }

  _stopHealthPoll() {
    if (this._healthTimer) { clearTimeout(this._healthTimer); this._healthTimer = null; }
  }

  _pollHealth() {
    const child = this.child;
    const started = Date.now();
    const tick = async () => {
      this._healthTimer = null;
      if (this._disposed || this.child !== child) return;
      try {
        const h = await this.call('GET', '/health', null, 3000);
        if (h && h.ok) {
          this.lastHealth = { ...h, at: Date.now() };
          this.ready = true;
          this._streak = 0;
          this._log(`engine ready on port ${this.port} (vllm ${h.vllm ? 'ok' : 'down'}, screenpipe ${h.screenpipe ? 'ok' : 'down'})`, { health: h, port: this.port });
          this.emit('ready', this.status());
          return;
        }
      } catch (_) { /* not up yet */ }
      if (Date.now() - started > HEALTH_TIMEOUT_MS) {
        this.lastError = 'engine did not answer /health within 20 s';
        this._log(`engine: ${this.lastError}`, { stderr: this.stderr.slice(-20) });
        killTree(child);
        return;
      }
      this._healthTimer = setTimeout(tick, HEALTH_POLL_MS);
    };
    tick();
  }

  /** Refresh lastHealth without changing readiness. */
  async health() {
    try {
      const h = await this.call('GET', '/health', null, 5000);
      this.lastHealth = { ...h, at: Date.now() };
      return h;
    } catch (err) {
      this.lastError = err.message;
      return null;
    }
  }

  /**
   * HTTP call to the engine. JSON in, JSON out; rejects on network error, timeout or non-2xx
   * (err.status and err.body carry the response). Jobs are polled by callers, so 60 s is plenty.
   */
  call(method, pathname, body, timeoutMs = CALL_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      if (!this.port) return reject(new Error('engine not started'));
      const payload = body == null ? null : Buffer.from(JSON.stringify(body));
      const req = http.request({
        host: '127.0.0.1',
        port: this.port,
        method,
        path: pathname,
        headers: {
          'x-o-token': this.token,
          accept: 'application/json',
          ...(payload ? { 'content-type': 'application/json', 'content-length': payload.length } : {}),
        },
      }, (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          let data = text;
          try { data = text ? JSON.parse(text) : null; } catch (_) { /* leave as text */ }
          if (res.statusCode >= 200 && res.statusCode < 300) return resolve(data);
          const err = new Error(`engine ${method} ${pathname} -> ${res.statusCode}`);
          err.status = res.statusCode;
          err.body = data;
          reject(err);
        });
      });
      req.setTimeout(timeoutMs, () => req.destroy(new Error(`engine ${method} ${pathname} timed out after ${timeoutMs} ms`)));
      req.on('error', reject);
      if (payload) req.write(payload);
      req.end();
    });
  }

  /** Token omitted on purpose where this is logged; use `this.token` directly to pass it on. */
  status() {
    return {
      running: !!this.child,
      ready: this.ready,
      port: this.port,
      url: this.url,
      token: this.token,
      python: this.python ? [this.python.cmd, ...this.python.args].join(' ') : '',
      restarts: this.restarts,
      lastHealth: this.lastHealth,
      lastError: this.lastError,
      stderr: this.stderr.slice(-30),
    };
  }

  /** Kill the child (if any) and start a fresh one; the instance stays usable, backoff streak resets. */
  async restart() {
    if (this._disposed) return;
    if (this._restartTimer) { clearTimeout(this._restartTimer); this._restartTimer = null; }
    this._stopHealthPoll();
    const child = this.child;
    this.child = null;
    this.ready = false;
    this._streak = 0;
    if (child) killTree(child);
    this._log('engine restart requested');
    return this.start();
  }

  /** Stop the engine and give up on restarts. Call on app quit. */
  dispose() {
    this._disposed = true;
    if (this._restartTimer) { clearTimeout(this._restartTimer); this._restartTimer = null; }
    this._stopHealthPoll();
    const child = this.child;
    this.child = null;
    this.ready = false;
    if (child) killTree(child);
  }
}

module.exports = { EngineProcess, resolvePython, freePort, killTree, proxyEnv };
