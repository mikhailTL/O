'use strict';
// O self-updater (no dependencies).
//
// Source: a GitHub repo + branch + sub-folder (settings: updateRepo, updateBranch, updatePath,
// optional updateToken for private repos). It compares the latest commit on that branch with
// the commit recorded in <appDir>/.o-install.json, downloads the tarball for that commit,
// copies the sub-folder's files over the app (never touching node_modules or your settings,
// which live in the app-data folder), removes files the new version no longer has, runs
// `npm install` if dependencies changed, then relaunches.

const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');
const http = require('http');
const https = require('https');
const { spawn } = require('child_process');

const IS_WIN = process.platform === 'win32';
const MANIFEST = '.o-install.json';
const SKIP = [/^node_modules\//, /^\.o-install\.json$/, /^dist\//, /^out\//];

// ------------------------------------------------------------------ HTTP

function request(url, { headers = {}, maxRedirects = 5, timeoutMs = 60000 } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === 'http:' ? http : https;
    const req = mod.get(u, { headers: { 'user-agent': 'O-Updater', ...headers }, timeout: timeoutMs }, (res) => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location && maxRedirects > 0) {
        res.resume();
        const next = new URL(res.headers.location, u);
        // Never send the GitHub token to another host (the tarball redirect is pre-signed).
        const h = { ...headers };
        if (next.host !== u.host) delete h.authorization;
        return resolve(request(next.toString(), { headers: h, maxRedirects: maxRedirects - 1, timeoutMs }));
      }
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
      res.on('error', reject);
    });
    req.on('timeout', () => req.destroy(new Error('Timed out talking to GitHub')));
    req.on('error', reject);
  });
}

function ghHeaders(cfg) {
  const h = { accept: 'application/vnd.github+json' };
  if (cfg.token) h.authorization = `Bearer ${cfg.token}`;
  return h;
}

function explainHttp(status, cfg) {
  if (status === 404) {
    return cfg.token
      ? `GitHub couldn't find ${cfg.repo} (branch ${cfg.branch}). Check the update settings and that the token can read this repo.`
      : `GitHub couldn't find ${cfg.repo} (branch ${cfg.branch}). If the repo is private, add a read-only GitHub token in Settings → Updates.`;
  }
  if (status === 401) return 'GitHub rejected the update token. Paste a fresh one in Settings → Updates.';
  if (status === 403) return 'GitHub is rate-limiting update checks right now. Try again in a while (or add a token).';
  return `GitHub answered ${status}.`;
}

// ------------------------------------------------------------------ tar

/** Minimal tar reader (ustar + pax + GNU long names), enough for GitHub tarballs. */
function parseTar(buf) {
  const out = [];
  let off = 0;
  let paxPath = null;
  let longName = null;
  const str = (a, b) => buf.toString('utf8', a, b).replace(/\0.*$/s, '');
  while (off + 512 <= buf.length) {
    const header = buf.subarray(off, off + 512);
    if (header.every((x) => x === 0)) break;
    const name = str(off, off + 100);
    const size = parseInt(str(off + 124, off + 136).trim() || '0', 8) || 0;
    const type = String.fromCharCode(buf[off + 156] || 48);
    const magic = str(off + 257, off + 263);
    const prefix = magic.startsWith('ustar') ? str(off + 345, off + 500) : '';
    const dataStart = off + 512;
    const data = buf.subarray(dataStart, dataStart + size);
    off = dataStart + Math.ceil(size / 512) * 512;

    if (type === 'x') { // pax header for the next entry
      const text = data.toString('utf8');
      let i = 0;
      while (i < text.length) {
        const sp = text.indexOf(' ', i);
        if (sp < 0) break;
        const len = parseInt(text.slice(i, sp), 10);
        if (!len) break;
        const rec = text.slice(sp + 1, i + len - 1);
        const eq = rec.indexOf('=');
        if (rec.slice(0, eq) === 'path') paxPath = rec.slice(eq + 1);
        i += len;
      }
      continue;
    }
    if (type === 'g') continue; // global pax header (GitHub puts the commit id here)
    if (type === 'L') { longName = data.toString('utf8').replace(/\0.*$/s, ''); continue; }

    const full = paxPath || longName || (prefix ? `${prefix}/${name}` : name);
    paxPath = null; longName = null;
    if (type === '0' || type === '\0' || type === '7') out.push({ name: full, type: 'file', data: Buffer.from(data) });
    else if (type === '5') out.push({ name: full, type: 'dir' });
  }
  return out;
}

/** GitHub tarballs wrap everything in "<owner>-<repo>-<sha>/". Keep files under `sub/`. */
function selectFiles(entries, sub) {
  const subDir = String(sub || '').replace(/^\/+|\/+$/g, '');
  const files = [];
  for (const e of entries) {
    if (e.type !== 'file') continue;
    const parts = e.name.split('/').slice(1); // drop the top-level wrapper folder
    let rel = parts.join('/');
    if (subDir) {
      if (!rel.startsWith(subDir + '/')) continue;
      rel = rel.slice(subDir.length + 1);
    }
    if (!rel || rel.endsWith('/')) continue;
    if (rel.split('/').some((p) => p === '..' || p === '') || path.isAbsolute(rel)) continue;
    if (SKIP.some((re) => re.test(rel))) continue;
    files.push({ rel, data: e.data });
  }
  return files;
}

// ------------------------------------------------------------------ install

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

function depsKey(pkg) {
  return JSON.stringify({ d: (pkg && pkg.dependencies) || {}, v: (pkg && pkg.devDependencies) || {} });
}

function installFiles(appDir, files, sha) {
  const manifestFile = path.join(appDir, MANIFEST);
  const old = readJson(manifestFile);
  const oldPkg = readJson(path.join(appDir, 'package.json'));

  if (!files.some((f) => f.rel === 'package.json')) {
    throw new Error('The downloaded update does not look like O (no package.json). Nothing was changed.');
  }

  // Stage everything first, so a failed write can't leave a half-copied app.
  const stage = fs.mkdtempSync(path.join(os.tmpdir(), 'o-update-'));
  try {
    for (const f of files) {
      const dest = path.join(stage, f.rel);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, f.data);
    }
    for (const f of files) {
      const dest = path.join(appDir, f.rel);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.copyFileSync(path.join(stage, f.rel), dest);
    }
  } finally {
    fs.rmSync(stage, { recursive: true, force: true });
  }

  // Remove files that the previous update installed but the new version dropped.
  const keep = new Set(files.map((f) => f.rel));
  let removed = 0;
  if (old && Array.isArray(old.files)) {
    for (const rel of old.files) {
      if (keep.has(rel) || SKIP.some((re) => re.test(rel)) || rel.split('/').includes('..')) continue;
      try { fs.rmSync(path.join(appDir, rel), { force: true }); removed++; } catch (_) {}
    }
  }

  fs.writeFileSync(manifestFile, JSON.stringify({ sha, files: [...keep].sort(), updatedAt: new Date().toISOString() }, null, 2));

  const newPkg = readJson(path.join(appDir, 'package.json'));
  const electronOld = oldPkg && oldPkg.devDependencies && oldPkg.devDependencies.electron;
  const electronNew = newPkg && newPkg.devDependencies && newPkg.devDependencies.electron;
  return {
    count: files.length,
    removed,
    needsInstall: depsKey(oldPkg) !== depsKey(newPkg),
    electronChanged: electronOld !== electronNew,
    version: newPkg && newPkg.version,
  };
}

function npmInstall(appDir) {
  return new Promise((resolve) => {
    let tail = '';
    const child = spawn(IS_WIN ? 'npm.cmd' : 'npm', ['install', '--no-audit', '--no-fund'], {
      cwd: appDir, shell: IS_WIN, windowsHide: true, env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined },
    });
    const keep = (d) => { tail = (tail + d).slice(-2000); };
    child.stdout.on('data', keep);
    child.stderr.on('data', keep);
    child.on('error', (e) => resolve({ ok: false, output: e.message }));
    child.on('close', (code) => resolve({ ok: code === 0, output: tail }));
  });
}

// ------------------------------------------------------------------ updater

/**
 * @param {object} o
 * @param {string} o.appDir                  folder holding package.json (the app root)
 * @param {() => object} o.getSettings       updateRepo, updateBranch, updatePath, updateToken
 * @param {(ev:object) => void} o.onEvent    {type:'update', state, message, ...}
 * @param {() => void} o.relaunch            restart the app after installing
 * @param {() => boolean} [o.isBusy]         don't update in the middle of a task
 * @param {boolean} [o.isPackaged]           installed builds update by reinstalling instead
 * @param {string} [o.apiBase]               for tests (default https://api.github.com)
 */
function createUpdater({ appDir, getSettings, onEvent, relaunch, isBusy = () => false, isPackaged = false, apiBase }) {
  const api = (apiBase || process.env.O_UPDATE_API || 'https://api.github.com').replace(/\/+$/, '');
  let state = { state: 'idle' };
  let working = false;

  const emit = (s) => { state = { ...s, at: Date.now() }; onEvent({ type: 'update', ...state }); return state; };
  const cfg = () => {
    const s = getSettings() || {};
    return {
      repo: String(s.updateRepo || '').trim(),
      branch: String(s.updateBranch || '').trim(),
      sub: String(s.updatePath || '').trim(),
      token: String(s.updateToken || '').trim(),
    };
  };
  const installed = () => {
    const m = readJson(path.join(appDir, MANIFEST));
    return m && m.sha ? m.sha : null;
  };

  async function latest(c) {
    if (!/^[\w.-]+\/[\w.-]+$/.test(c.repo) || !c.branch) throw new Error('Set the update repo (owner/name) and branch in Settings → Updates.');
    const ref = c.branch.split('/').map(encodeURIComponent).join('/');
    const res = await request(`${api}/repos/${c.repo}/git/ref/heads/${ref}`, { headers: ghHeaders(c) });
    if (res.status !== 200) throw new Error(explainHttp(res.status, c));
    const j = JSON.parse(res.body.toString('utf8'));
    const sha = j && j.object && j.object.sha;
    if (!sha) throw new Error('GitHub did not return a commit for that branch.');
    return sha;
  }

  async function check({ quiet = false } = {}) {
    if (working) return state;
    working = true;
    try {
      if (!quiet) emit({ state: 'checking', message: 'Checking for updates…' });
      const c = cfg();
      const sha = await latest(c);
      const have = installed();
      if (have && have === sha) return emit({ state: 'current', sha, message: 'O is up to date.' });
      return emit({
        state: 'available', sha, installed: have,
        message: have ? 'A new version of O is available.' : 'An update is available (this copy doesn\'t record its version yet, so updating once will set that up).',
      });
    } catch (err) {
      return quiet ? state : emit({ state: 'error', message: err.message || String(err) });
    } finally {
      working = false;
    }
  }

  async function apply() {
    if (working) return state;
    if (isPackaged) return emit({ state: 'error', message: 'This is an installed build: download the new installer to update.' });
    if (isBusy()) return emit({ state: 'error', message: 'O is in the middle of a task. Try updating when it\'s done.' });
    working = true;
    try {
      const c = cfg();
      emit({ state: 'downloading', message: 'Downloading the update…' });
      const sha = await latest(c);
      const res = await request(`${api}/repos/${c.repo}/tarball/${sha}`, { headers: ghHeaders(c), timeoutMs: 180000 });
      if (res.status !== 200) throw new Error(explainHttp(res.status, c));
      const files = selectFiles(parseTar(zlib.gunzipSync(res.body)), c.sub);
      emit({ state: 'installing', message: `Installing ${files.length} files…` });
      const r = installFiles(appDir, files, sha);
      if (r.needsInstall) {
        if (r.electronChanged) {
          return emit({ state: 'manual', sha, message: 'Update copied. It needs a new Electron: quit O, run `npm install` in O\'s folder, then `npm start`.' });
        }
        emit({ state: 'installing', message: 'Updating dependencies (npm install)…' });
        const n = await npmInstall(appDir);
        if (!n.ok) {
          return emit({ state: 'manual', sha, message: 'Files updated, but `npm install` failed. Quit O, run `npm install` in O\'s folder, then `npm start`.', output: n.output });
        }
      }
      emit({ state: 'restarting', sha, version: r.version, message: 'Updated! Restarting O…' });
      setTimeout(relaunch, 1200);
      return state;
    } catch (err) {
      return emit({ state: 'error', message: err.message || String(err) });
    } finally {
      working = false;
    }
  }

  return { check, apply, get state() { return state; }, installedSha: installed };
}

module.exports = { createUpdater, parseTar, selectFiles, installFiles };
