'use strict';
// One-click setup. status() checks what a full install (install/install.ps1, install/install.sh) leaves behind;
// run() does the missing parts itself and reports each step as a 'progress' event ({step, state, detail}).
// Only winget (Windows system packages), pip (the engine's venv) and npm (Claude Code) ever run: nothing is
// downloaded from an arbitrary URL. Every step is idempotent, so run() can be pressed again at any time.
// macOS and Linux get the same engine venv and Claude install; system packages there are install.sh's job.

const { spawn, spawnSync } = require('child_process');
const { EventEmitter } = require('events');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { resolvePython } = require('./engine');

const IS_WIN = process.platform === 'win32';
const IS_MAC = process.platform === 'darwin';

let childEnv = (extra) => ({ ...process.env, ...extra });
let checkClaudeFn = async () => ({ installed: false, error: 'agent module missing' });
let resolveClaude = (p) => p || 'claude';
try {
  const agent = require('./agent');
  if (typeof agent.childEnv === 'function') childEnv = agent.childEnv;
  if (typeof agent.checkClaude === 'function') checkClaudeFn = agent.checkClaude;
  if (typeof agent.resolveClaude === 'function') resolveClaude = agent.resolveClaude;
} catch (_) { /* fallbacks above */ }

/** The checklist, in the order status() reports and run() performs it. */
const STEPS = Object.freeze([
  { id: 'node', label: 'Node.js' },
  { id: 'claude', label: 'Claude Code' },
  { id: 'login', label: 'Signed in to Claude' },
  { id: 'python', label: 'Python 3.10 or newer' },
  { id: 'venv', label: 'Engine installed' },
  { id: 'engine', label: 'Engine running' },
  { id: 'computer', label: 'Computer use' },
  { id: 'permission', label: 'Permission mode' },
]);
const STEP_IDS = STEPS.map((s) => s.id);

const WINGET_FLAGS = ['--accept-source-agreements', '--accept-package-agreements', '--silent'];
const WINGET = { python: 'Python.Python.3.12', node: 'OpenJS.NodeJS.LTS', git: 'Git.Git' };
const PIP_TIMEOUT_MS = 20 * 60 * 1000;
const WINGET_TIMEOUT_MS = 15 * 60 * 1000;
const LOGIN_WAIT_MS = 8 * 60 * 1000;
const LOGIN_POLL_MS = 4000;
const ENGINE_WAIT_MS = 60 * 1000;
const AX_WAIT_MS = 3 * 60 * 1000;

const MODE_LABEL = { log: 'Log everything', auto: 'Full autonomy', ask: 'Ask first' };

function withTimeout(promise, ms, fallback) {
  let t;
  return Promise.race([
    Promise.resolve(promise).finally(() => clearTimeout(t)),
    new Promise((resolve) => { t = setTimeout(() => resolve(fallback), ms); }),
  ]);
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Quote one argument for cmd.exe (same rule as agent.js: .cmd shims need shell:true on Windows).
function winQuote(a) {
  a = String(a);
  if (a === '' || /[\s"&|<>^()%!]/.test(a)) return '"' + a.replace(/"/g, '""') + '"';
  return a;
}

/** The engine's own virtualenv python (engine/.venv), whether or not it exists yet. */
function venvPython(engineDir) {
  return IS_WIN ? path.join(engineDir, '.venv', 'Scripts', 'python.exe') : path.join(engineDir, '.venv', 'bin', 'python');
}

/** `python -c` version probe: "3.12.4" or '' when the interpreter does not run or is older than 3.10. */
function pythonVersion(cmd, args) {
  try {
    const r = spawnSync(cmd, [...(args || []), '-c', 'import sys; v=sys.version_info; print("%d.%d.%d" % v[:3])'], {
      encoding: 'utf8', timeout: 8000, windowsHide: true, env: childEnv(),
    });
    if (r.status !== 0) return '';
    const v = String(r.stdout).trim();
    const [maj, min] = v.split('.').map((x) => parseInt(x, 10));
    return maj > 3 || (maj === 3 && min >= 10) ? v : '';
  } catch (_) { return ''; }
}

/** Windows: where winget's Python lands when PATH has not been refreshed for this process yet. */
function windowsPythonCandidates() {
  if (!IS_WIN) return [];
  const la = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  const out = [];
  for (const base of [path.join(la, 'Programs', 'Python'), 'C:\\Program Files', 'C:\\Program Files (x86)']) {
    for (const v of ['313', '312', '311', '310']) out.push(path.join(base, `Python${v}`, 'python.exe'));
  }
  return out.filter((p) => fs.existsSync(p)).map((cmd) => ({ cmd, args: [] }));
}

/** First working Python 3.10+: the setting, engine/.venv, PATH names, then winget's install folders. */
function findPython(setting, engineDir) {
  const r = resolvePython(setting, engineDir);
  if (r) return r;
  for (const c of windowsPythonCandidates()) if (pythonVersion(c.cmd, c.args)) return c;
  return null;
}

function commandExists(cmd) {
  try {
    const r = spawnSync(cmd, ['--version'], { encoding: 'utf8', timeout: 15000, windowsHide: true, env: childEnv(), shell: IS_WIN });
    return r.status === 0 ? String(r.stdout || r.stderr).trim().split(/\r?\n/)[0] : '';
  } catch (_) { return ''; }
}

class Setup extends EventEmitter {
  /**
   * @param {object} opts
   * @param {string} opts.appDir        repo root (engine lives in <appDir>/engine, install scripts in <appDir>/install)
   * @param {object} opts.settings      the Settings instance (get/set)
   * @param {() => object|null} opts.getEngine  the EngineProcess, when one exists
   * @param {(s:object) => Promise<object>} [opts.checkClaude]  agent.js checkClaude (installed, version, loggedIn)
   * @param {(summary:string, detail?:object) => void} [opts.log]  action-log note
   */
  constructor({ appDir, settings, getEngine, checkClaude, log } = {}) {
    super();
    this.appDir = appDir || path.join(__dirname, '..');
    this.engineDir = path.join(this.appDir, 'engine');
    this.settings = settings;
    this.getEngine = typeof getEngine === 'function' ? getEngine : () => null;
    this.checkClaude = typeof checkClaude === 'function' ? checkClaude : checkClaudeFn;
    this.log = typeof log === 'function' ? log : () => {};
    this.running = false;
    this.lastRun = null;
  }

  _settings() { try { return (this.settings && this.settings.get()) || {}; } catch (_) { return {}; } }

  _progress(step, state, detail) {
    const ev = { step, state, detail: String(detail || '').slice(0, 400), at: Date.now() };
    this.emit('progress', ev);
    return ev;
  }

  /**
   * Run a command, streaming its last output line as progress detail for `step`.
   * @returns {Promise<{code:number|null, out:string, err:string}>}
   */
  _exec(step, cmd, args, { timeout = PIP_TIMEOUT_MS, shell = false, cwd } = {}) {
    return new Promise((resolve) => {
      let child;
      const opts = { env: childEnv(), windowsHide: true, cwd: cwd || os.homedir(), stdio: ['ignore', 'pipe', 'pipe'] };
      try {
        child = shell && IS_WIN
          ? spawn([cmd, ...args].map(winQuote).join(' '), [], { ...opts, shell: true })
          : spawn(cmd, args, opts);
      } catch (err) { return resolve({ code: null, out: '', err: err.message }); }
      let out = '', err = '', last = '';
      const onText = (which) => (buf) => {
        const s = String(buf);
        if (which === 'out') out += s; else err += s;
        const lines = s.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
        if (lines.length) { last = lines[lines.length - 1]; this._progress(step, 'running', last); }
      };
      child.stdout.on('data', onText('out'));
      child.stderr.on('data', onText('err'));
      const t = setTimeout(() => { try { child.kill(); } catch (_) {} }, timeout);
      child.on('error', (e) => { clearTimeout(t); resolve({ code: null, out, err: err + e.message }); });
      child.on('close', (code) => { clearTimeout(t); resolve({ code, out, err }); });
    });
  }

  _pythonCmd(step, py, args, opts) { return this._exec(step, py.cmd, [...py.args, ...args], opts); }

  // ------------------------------------------------------------------ status

  async _claudeState() {
    const s = this._settings();
    return withTimeout(Promise.resolve().then(() => this.checkClaude(s)).catch((err) => ({ installed: false, error: err.message })), 20000, { installed: false, error: 'claude did not answer in time' });
  }

  _venvState() {
    const vp = venvPython(this.engineDir);
    if (!fs.existsSync(vp)) return { ok: false, python: vp, detail: 'engine/.venv is missing' };
    try {
      const r = spawnSync(vp, ['-c', 'import o_engine, fastapi, uvicorn; print(getattr(o_engine, "__version__", "ok"))'], { encoding: 'utf8', timeout: 20000, windowsHide: true, env: childEnv(), cwd: os.homedir() });
      if (r.status === 0) return { ok: true, python: vp, detail: 'engine/.venv' };
      const line = String(r.stderr || '').trim().split(/\r?\n/).pop() || 'o_engine does not import';
      return { ok: false, python: vp, detail: line.slice(0, 160) };
    } catch (err) { return { ok: false, python: vp, detail: err.message }; }
  }

  async _access() {
    const engine = this.getEngine();
    if (!engine) return null;
    let st = null;
    try { st = engine.status(); } catch (_) { return null; }
    if (!st.ready) return null;
    try { return await engine.call('GET', '/computer/access', null, 6000); } catch (_) { return null; }
  }

  /** Every step as {step, label, ok, detail, fix}; `ok` is true when nothing remains to do for it. */
  async status() {
    const s = this._settings();
    const rows = [];
    const row = (id, ok, detail, fix) => rows.push({ step: id, label: STEPS.find((x) => x.id === id).label, ok: !!ok, detail: String(detail || ''), fix: ok ? '' : String(fix || '') });

    const nodeMajor = parseInt(process.versions.node, 10);
    row('node', nodeMajor >= 18, `Node ${process.versions.node}`, 'Install Node.js 18 or newer (nodejs.org) and start O again.');

    const claude = await this._claudeState();
    row('claude', !!claude.installed, claude.installed ? `Claude Code ${claude.version || ''}`.trim() : (claude.error || 'not found'),
      'Set up O installs it: npm install -g @anthropic-ai/claude-code');
    if (!claude.installed) row('login', false, 'Install Claude Code first', 'Set up O opens a terminal with `claude auth login` once Claude Code is installed.');
    else if (claude.loggedIn === false) row('login', false, 'Not signed in', 'Set up O opens a terminal with `claude auth login`; sign in there.');
    else row('login', true, claude.loggedIn === true ? 'Signed in' : 'Signed in (not verified by this CLI version)');

    const py = findPython(s.enginePython, this.engineDir);
    const pyv = py ? pythonVersion(py.cmd, py.args) : '';
    row('python', !!pyv, pyv ? `Python ${pyv} (${[py.cmd, ...py.args].join(' ')})` : 'No Python 3.10+ found',
      IS_WIN ? 'Set up O installs it: winget install -e --id Python.Python.3.12'
        : IS_MAC ? 'Install it: brew install python@3.12, or run bash install/install.sh'
          : 'Install it: sudo apt install python3 python3-venv (or your distro\'s equivalent), or run bash install/install.sh');

    const venv = this._venvState();
    row('venv', venv.ok, venv.detail, pyv ? 'Set up O creates engine/.venv and runs pip install -e engine.' : 'Needs Python first.');

    const engine = this.getEngine();
    let est = null;
    try { est = engine ? engine.status() : null; } catch (_) { est = null; }
    row('engine', !!(est && est.ready), est ? (est.ready ? `Running on port ${est.port}` : (est.lastError || (est.running ? 'Starting…' : 'Not running'))) : 'Engine module missing',
      venv.ok ? 'Set up O starts it.' : 'Needs the engine installed first.');

    const access = await this._access();
    const cu = (access && access.cu_backend) || null;
    const available = !!(cu && cu.available);
    if (!access) {
      row('computer', false, est && est.ready ? 'The engine did not report' : 'Waiting for the engine', 'Starts once the engine runs.');
    } else if (IS_MAC) {
      const trusted = access.accessibility_trusted === true;
      row('computer', available && trusted,
        !available ? `Accessibility backend: ${cu.reason || 'not available'}` : trusted ? 'Accessibility access granted' : 'Accessibility access is off for O',
        !available ? 'Re-run bash install/install.sh (installs atomacos and pyobjc), then restart O.' : 'System Settings → Privacy & Security → Accessibility: turn on O (or Terminal, if O runs from Terminal).');
    } else if (IS_WIN) {
      row('computer', available, available ? 'Ready (UI Automation, nothing to grant)' : `UI Automation backend: ${cu.reason || 'not available'}`,
        'Set up O installs the uiautomation package into the engine.');
    } else {
      row('computer', available, available ? `Ready (${cu.name || 'AT-SPI'})` : `Accessibility backend: ${cu.reason || 'not available'}`,
        'Run bash install/install.sh (Xvfb, openbox, python3-gi), then restart O.');
    }

    const mode = s.permissionMode || 'log';
    row('permission', true, MODE_LABEL[mode] || mode);

    return { ok: rows.every((r) => r.ok), steps: rows, platform: process.platform, running: this.running, lastRun: this.lastRun };
  }

  // ------------------------------------------------------------------ run

  /**
   * Perform the missing steps (all of them, or just `only`). Resolves with the status afterwards.
   * Steps already green report 'ok' at once; a failed prerequisite skips what depends on it.
   */
  async run(only) {
    if (this.running) return { ok: false, error: 'Setup is already running.', running: true };
    this.running = true;
    only = STEP_IDS.includes(only) ? only : '';
    const want = (id) => !only || only === id;
    const failed = {};
    const fail = (id, detail) => { failed[id] = detail; this._progress(id, 'fail', detail); };
    this.log('Set up O started', { only: only || 'all', platform: process.platform });
    try {
      const before = await this.status();
      const by = Object.fromEntries(before.steps.map((r) => [r.step, r]));
      for (const r of before.steps) if (r.ok && want(r.step)) this._progress(r.step, 'ok', r.detail);

      if (want('node') && !by.node.ok) fail('node', by.node.fix);

      if (want('claude') && !by.claude.ok) {
        const r = await this._installClaude();
        if (r.ok) this._progress('claude', 'ok', r.detail); else fail('claude', r.detail);
      }

      if (want('python') && !by.python.ok) {
        const r = await this._installPython();
        if (r.ok) this._progress('python', 'ok', r.detail); else fail('python', r.detail);
      }

      if (want('venv') && !by.venv.ok) {
        if (failed.python) fail('venv', 'Skipped: Python is missing.');
        else {
          const r = await this._installEngine();
          if (r.ok) this._progress('venv', 'ok', r.detail); else fail('venv', r.detail);
        }
      }

      if (want('engine') && (!by.engine.ok || (!only && !by.venv.ok && !failed.venv))) {
        if (failed.venv || failed.python) fail('engine', 'Skipped: the engine is not installed.');
        else {
          const r = await this._startEngine();
          if (r.ok) this._progress('engine', 'ok', r.detail); else fail('engine', r.detail);
        }
      }

      if (want('computer') && (!by.computer.ok || (!only && !by.engine.ok && !failed.engine))) {
        if (failed.engine || failed.venv || failed.python) fail('computer', 'Skipped: the engine is not running.');
        else {
          const r = await this._computerUse();
          if (r.ok) this._progress('computer', 'ok', r.detail); else fail('computer', r.detail);
        }
      }

      // Last: it opens a terminal and waits for the person, so everything silent is done first.
      if (want('login') && !by.login.ok) {
        if (failed.claude || (!by.claude.ok && !want('claude'))) fail('login', 'Skipped: Claude Code is not installed.');
        else {
          const r = await this._login();
          if (r.ok) this._progress('login', 'ok', r.detail); else fail('login', r.detail);
        }
      }

      if (want('permission')) this._progress('permission', 'ok', MODE_LABEL[this._settings().permissionMode] || 'Log everything');

      const after = await this.status();
      this.lastRun = { at: Date.now(), ok: after.ok, failed: Object.keys(failed) };
      this.log(after.ok ? 'Set up O finished: everything is ready' : `Set up O finished with ${Object.keys(failed).length} step(s) still open`, { failed });
      return { ...after, failed };
    } catch (err) {
      this.log('Set up O failed', { error: err.message });
      return { ok: false, error: err.message, failed };
    } finally {
      this.running = false;
    }
  }

  /** winget install (Windows only). Resolves true when the package is present afterwards (already installed counts). */
  async _winget(step, id) {
    if (!IS_WIN) return false;
    if (!commandExists('winget')) { this._progress(step, 'running', 'winget is not available on this Windows'); return false; }
    this._progress(step, 'running', `winget install ${id}`);
    const r = await this._exec(step, 'winget', ['install', '-e', '--id', id, ...WINGET_FLAGS], { timeout: WINGET_TIMEOUT_MS, shell: true });
    // 0 = installed; winget's "already installed" / "no applicable upgrade" codes are fine too.
    return r.code === 0 || /already installed|No available upgrade|no applicable/i.test(r.out + r.err);
  }

  async _installClaude() {
    const step = 'claude';
    let npm = commandExists('npm');
    if (!npm && IS_WIN) {
      if (await this._winget(step, WINGET.node)) npm = commandExists('npm');
    }
    if (!npm) {
      return { ok: false, detail: IS_WIN ? 'npm not found: winget could not install Node.js. Install Node.js LTS from nodejs.org and press Set up O again.'
        : 'npm not found. Install Node.js (nodejs.org, or brew install node), or run bash install/install.sh.' };
    }
    this._progress(step, 'running', 'npm install -g @anthropic-ai/claude-code');
    const r = await this._exec(step, 'npm', ['install', '-g', '@anthropic-ai/claude-code', '--no-audit', '--no-fund'], { timeout: 10 * 60 * 1000, shell: true });
    const c = await this._claudeState();
    if (c.installed) { this.log('Claude Code installed with npm', { version: c.version || '' }); return { ok: true, detail: `Claude Code ${c.version || ''}`.trim() }; }
    const tail = (r.err || r.out).trim().split(/\r?\n/).pop() || `npm exited ${r.code}`;
    return { ok: false, detail: `npm install failed: ${tail.slice(0, 200)}` };
  }

  async _installPython() {
    const step = 'python';
    if (!IS_WIN) {
      return { ok: false, detail: IS_MAC ? 'Install Python 3.10+ (brew install python@3.12) or run bash install/install.sh, then press Set up O again.'
        : 'Install Python 3.10+ with python3-venv (apt/dnf) or run bash install/install.sh, then press Set up O again.' };
    }
    const got = await this._winget(step, WINGET.python);
    const py = findPython(this._settings().enginePython, this.engineDir);
    const v = py ? pythonVersion(py.cmd, py.args) : '';
    if (v) { this.log('Python installed with winget', { version: v }); return { ok: true, detail: `Python ${v}` }; }
    return { ok: false, detail: got ? 'Python installed, but not found yet. Restart O and press Set up O again.' : 'winget could not install Python. Install Python 3.12 from python.org (tick "Add python.exe to PATH") and press Set up O again.' };
  }

  async _installEngine() {
    const step = 'venv';
    const vp = venvPython(this.engineDir);
    if (!fs.existsSync(path.join(this.engineDir, 'pyproject.toml'))) return { ok: false, detail: `engine/ not found at ${this.engineDir}` };
    if (!fs.existsSync(vp)) {
      const base = findPython(this._settings().enginePython, this.engineDir);
      if (!base) return { ok: false, detail: 'No Python 3.10+ to create the venv with.' };
      this._progress(step, 'running', 'Creating engine/.venv');
      const r = await this._pythonCmd(step, base, ['-m', 'venv', path.join(this.engineDir, '.venv')], { timeout: 3 * 60 * 1000 });
      if (r.code !== 0 || !fs.existsSync(vp)) {
        const tail = (r.err || r.out).trim().split(/\r?\n/).pop() || '';
        return { ok: false, detail: `Could not create engine/.venv${tail ? ': ' + tail.slice(0, 160) : ''}${!IS_WIN ? ' (Debian/Ubuntu: sudo apt install python3-venv)' : ''}` };
      }
    }
    const venv = { cmd: vp, args: [] };
    if (IS_WIN && !commandExists('git')) await this._winget(step, WINGET.git); // some pip builds want git; best effort
    this._progress(step, 'running', 'pip install --upgrade pip');
    await this._pythonCmd(step, venv, ['-m', 'pip', 'install', '--upgrade', 'pip'], { timeout: 5 * 60 * 1000 });
    this._progress(step, 'running', 'pip install -e engine');
    const r = await this._pythonCmd(step, venv, ['-m', 'pip', 'install', '-e', this.engineDir], { cwd: this.engineDir });
    if (r.code !== 0) {
      const tail = (r.err || r.out).trim().split(/\r?\n/).filter((l) => /error/i.test(l)).pop() || (r.err || r.out).trim().split(/\r?\n/).pop() || `pip exited ${r.code}`;
      return { ok: false, detail: `pip install -e engine failed: ${tail.slice(0, 220)}` };
    }
    // (Windows: uiautomation, pyautogui, mss, pywin32 and comtypes come with pyproject's win32 markers;
    // _computerUse re-installs uiautomation on its own if the backend still does not import.)
    // The headless browser agent's Chromium (same step as the installers'); the engine runs without it.
    this._progress(step, 'running', 'playwright install chromium');
    const pw = await this._pythonCmd(step, venv, ['-m', 'playwright', 'install', 'chromium'], { timeout: 10 * 60 * 1000 });
    const st = this._venvState();
    if (!st.ok) return { ok: false, detail: `Installed, but o_engine does not import: ${st.detail}` };
    if (this._settings().enginePython !== vp) {
      try { this.settings.set({ enginePython: vp }); } catch (_) {}
      this.log('Engine Python set to engine/.venv', { enginePython: vp });
    }
    this.log('Engine installed (pip install -e engine)', { python: vp, playwright: pw.code === 0 });
    return { ok: true, detail: pw.code === 0 ? 'engine/.venv' : 'engine/.venv (Chromium for the browser agent did not install; O runs without it)' };
  }

  async _waitEngineReady(step, ms) {
    const engine = this.getEngine();
    const started = Date.now();
    while (Date.now() - started < ms) {
      let st = null;
      try { st = engine.status(); } catch (_) { st = null; }
      if (st && st.ready) return st;
      this._progress(step, 'running', st && st.lastError ? st.lastError : 'Waiting for the engine…');
      await sleep(700);
    }
    return null;
  }

  async _startEngine() {
    const step = 'engine';
    const engine = this.getEngine();
    if (!engine) return { ok: false, detail: 'The engine module did not load; restart O.' };
    let st = null;
    try { st = engine.status(); } catch (_) {}
    if (st && st.ready) return { ok: true, detail: `Running on port ${st.port}` };
    this._progress(step, 'running', 'Starting the engine');
    try { await engine.restart(); } catch (err) { return { ok: false, detail: `Could not start the engine: ${err.message}` }; }
    const ready = await this._waitEngineReady(step, ENGINE_WAIT_MS);
    if (ready) return { ok: true, detail: `Running on port ${ready.port}` };
    let last = null;
    try { last = engine.status(); } catch (_) {}
    const tail = last && last.stderr && last.stderr.length ? last.stderr[last.stderr.length - 1] : '';
    return { ok: false, detail: `${(last && last.lastError) || 'The engine did not answer'}${tail ? ' — ' + tail.slice(0, 160) : ''}` };
  }

  async _computerUse() {
    const step = 'computer';
    let access = await this._access();
    if (!access) return { ok: false, detail: 'The engine is not answering.' };
    let cu = access.cu_backend || {};
    if (IS_WIN) {
      if (cu.available) return { ok: true, detail: 'Ready (UI Automation)' };
      const vp = venvPython(this.engineDir);
      if (!fs.existsSync(vp)) return { ok: false, detail: 'Engine venv missing.' };
      this._progress(step, 'running', 'pip install uiautomation');
      const r = await this._pythonCmd(step, { cmd: vp, args: [] }, ['-m', 'pip', 'install', 'uiautomation'], { timeout: 5 * 60 * 1000 });
      if (r.code !== 0) return { ok: false, detail: 'pip could not install uiautomation.' };
      const engine = this.getEngine();
      this._progress(step, 'running', 'Restarting the engine');
      try { await engine.restart(); } catch (_) {}
      if (!(await this._waitEngineReady(step, ENGINE_WAIT_MS))) return { ok: false, detail: 'The engine did not come back after the install.' };
      access = await this._access(); cu = (access && access.cu_backend) || {};
      return cu.available ? { ok: true, detail: 'Ready (UI Automation)' } : { ok: false, detail: `UI Automation backend: ${cu.reason || 'not available'}` };
    }
    if (IS_MAC) {
      if (!cu.available) return { ok: false, detail: `Accessibility backend: ${cu.reason || 'not available'}. Run bash install/install.sh, then restart O.` };
      if (access.accessibility_trusted === true) return { ok: true, detail: 'Accessibility access granted' };
      // The grant is the person's: macOS's own prompt, the Accessibility pane, then wait for it.
      let electron = null;
      try { electron = require('electron'); } catch (_) {}
      try { if (electron && electron.systemPreferences) electron.systemPreferences.isTrustedAccessibilityClient(true); } catch (_) {}
      try { if (electron && electron.shell) electron.shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility').catch(() => {}); } catch (_) {}
      const started = Date.now();
      while (Date.now() - started < AX_WAIT_MS) {
        this._progress(step, 'running', 'Turn on O under System Settings → Privacy & Security → Accessibility…');
        await sleep(3000);
        let trusted = null;
        try { if (electron && electron.systemPreferences) trusted = electron.systemPreferences.isTrustedAccessibilityClient(false); } catch (_) {}
        access = await this._access();
        if ((access && access.accessibility_trusted === true) || trusted === true) return { ok: true, detail: 'Accessibility access granted' };
      }
      return { ok: false, detail: 'Accessibility access is still off. Turn on O in System Settings → Privacy & Security → Accessibility, then press Set up O again.' };
    }
    return cu.available ? { ok: true, detail: `Ready (${cu.name || 'AT-SPI'})` }
      : { ok: false, detail: `Accessibility backend: ${cu.reason || 'not available'}. Run bash install/install.sh (Xvfb, openbox, python3-gi), then restart O.` };
  }

  /** Open a terminal running `claude auth login`; resolves false when no terminal could be opened. */
  _openLoginTerminal() {
    const claude = resolveClaude(this._settings().claudePath || 'claude');
    const detached = { detached: true, stdio: 'ignore', env: childEnv() };
    try {
      if (IS_WIN) {
        spawn(`start "Sign in to Claude" cmd /k ${winQuote(claude)} auth login`, [], { ...detached, shell: true, windowsHide: false }).unref();
        return true;
      }
      const sh = (a) => `'${String(a).replace(/'/g, `'\\''`)}'`;
      const cmd = `${sh(claude)} auth login`;
      if (IS_MAC) {
        const as = cmd.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
        spawn('osascript', ['-e', `tell application "Terminal" to do script "${as}"`, '-e', 'tell application "Terminal" to activate'], detached).unref();
        return true;
      }
      const terms = [
        ['x-terminal-emulator', ['-e', `sh -c ${sh(cmd + '; exec sh')}`]],
        ['gnome-terminal', ['--', 'sh', '-c', `${cmd}; exec sh`]],
        ['konsole', ['-e', 'sh', '-c', `${cmd}; exec sh`]],
        ['xfce4-terminal', ['-e', `sh -c ${sh(cmd + '; exec sh')}`]],
        ['xterm', ['-e', 'sh', '-c', `${cmd}; exec sh`]],
      ];
      for (const [bin, args] of terms) {
        const found = spawnSync('sh', ['-c', `command -v ${bin}`], { encoding: 'utf8', env: childEnv() });
        if (found.status !== 0) continue;
        spawn(bin, args, detached).unref();
        return true;
      }
    } catch (_) { /* fall through */ }
    return false;
  }

  async _login() {
    const step = 'login';
    const before = await this._claudeState();
    if (before.installed && before.loggedIn !== false) return { ok: true, detail: 'Signed in' };
    if (!this._openLoginTerminal()) return { ok: false, detail: 'Open a terminal and run `claude auth login`, then press Set up O again.' };
    this.log('Opened a terminal for `claude auth login`');
    const started = Date.now();
    while (Date.now() - started < LOGIN_WAIT_MS) {
      this._progress(step, 'running', 'Sign in in the terminal that just opened…');
      await sleep(LOGIN_POLL_MS);
      const c = await this._claudeState();
      if (c.installed && c.loggedIn === true) { this.log('Claude Code signed in'); return { ok: true, detail: 'Signed in' }; }
    }
    return { ok: false, detail: 'Still not signed in. Finish `claude auth login` in the terminal, then press Set up O again.' };
  }
}

function createSetup(opts) { return new Setup(opts); }

module.exports = { Setup, createSetup, STEPS, venvPython, findPython };
