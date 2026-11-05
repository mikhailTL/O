'use strict';
// Node side of the scene system: a Playwright page per scene file, the repo served from disk at http://o.demo/
// (so renderer/* and assets/* are the real files), the demo backend's calls exposed as window.__o_rpc, and a
// frame-by-frame capture: seek the page's timeline to f/30 s, screenshot (JPEG q92 at 3840×2160), pipe into
// one ffmpeg per scene with identical encoder settings. Files are written to a temp name and renamed on exit 0.
const fs = require('fs');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const { chromium } = require(process.env.O_DEMO_PLAYWRIGHT || 'playwright');
const { ROOT, sleep } = require('../run');

const W = 1920, H = 1080, DSF = 2, FPS = 30;
const ENCODE = ['-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-r', String(FPS), '-movflags', '+faststart', '-an'];
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json', '.ico': 'image/x-icon' };

function probe(file) {
  try {
    const out = execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration:stream=width,height', '-of', 'json', file], { encoding: 'utf8' });
    const j = JSON.parse(out);
    const v = (j.streams || []).find((s) => s.width) || {};
    return { durationS: Number(j.format && j.format.duration) || 0, width: v.width || 0, height: v.height || 0 };
  } catch (_) { return { durationS: 0, width: 0, height: 0 }; }
}

/** Same ring/shape as main.js's genuiStore, so the panel's Runs sheet backfills the way it does in the app. */
function genuiStore() {
  const store = new Map();
  return {
    push(frame) {
      let s = store.get(frame.surface);
      if (!s) { s = { frames: [], title: '', ended: false, ok: null }; store.set(frame.surface, s); } else { store.delete(frame.surface); store.set(frame.surface, s); }
      if (frame.type === 'begin') { s.title = typeof frame.title === 'string' ? frame.title : s.title; s.ended = false; s.ok = null; }
      if (frame.type === 'end') { s.ended = true; s.ok = frame.ok !== false; }
      s.frames.push(frame);
      if (s.frames.length > 2000) s.frames.splice(0, s.frames.length - 2000);
    },
    frames: (surface) => { const s = store.get(String(surface || '')); return s ? s.frames.slice() : []; },
    surfaces: () => [...store.entries()].map(([surface, s]) => ({ surface, title: s.title, ended: s.ended, ok: s.ok, count: s.frames.length })),
  };
}

/**
 * What main.js would answer over IPC, backed by the demo backend. `state.logVisible` is what the panel's log
 * sheet gets from logTail (the scenes stream the rest in as events), `state.busy` what getState reports.
 */
function makeMain(backend, state, genui) {
  return {
    logTail: () => state.logVisible || backend.actionLog.tail(300),
    logMarkRead: () => backend.actionLog.markRead(),
    logPaths: () => backend.actionLog.paths(),
    logOpenFile: () => ({ ok: true }),
    logVerifyChain: () => backend.actionLog.verifyChain(),
    logNote: (summary, detail) => { backend.actionLog.record({ kind: 'note', summary: String(summary || ''), detail: detail || {} }); return { ok: true }; },
    engineStatus: () => ({ running: true, ready: true, port: backend.engine.port, url: backend.engine.url, python: 'python3', restarts: 0, lastHealth: backend.health, lastError: null, stderr: [] }),
    proxyStatus: () => backend.proxy.status(),
    getState: () => ({ busy: !!state.busy, queue: 0, agentLoaded: true, pendingPermissions: [], logUnread: 0 }),
    sendPrompt: () => ({ ok: true }),
    cancel: () => ({ dropped: 0 }),
    listChats: () => [], currentChat: () => null, newChat: () => ({ ok: true }), openChat: () => ({ ok: true, chat: { messages: [] } }), renameChat: () => ({ ok: true }), deleteChats: () => ({ ok: true, chat: { messages: [] } }),
    respondPermission: () => ({ ok: true }),
    genuiFrames: (surface) => genui.frames(surface),
    genuiSurfaces: () => (state.genuiVisible ? genui.surfaces().filter((s) => state.genuiVisible.includes(s.surface)) : genui.surfaces()),
    genuiAction: () => ({ ok: true }),
    checkRequirements: () => {
      const h = backend.health || {};
      return [
        { name: 'node', ok: true, detail: 'Node ' + process.versions.node },
        { name: 'claude', ok: true, detail: 'Claude Code (your own login)' },
        { name: 'python', ok: true, detail: 'Python 3 (engine on port ' + backend.engine.port + ')' },
        { name: 'vllm', ok: !!h.vllm, detail: h.vllm ? 'up' : 'down (falls back to Claude Code)', fix: 'Start vLLM (see install/).' },
        { name: 'playwright', ok: !!h.playwright, detail: h.playwright ? 'chromium installed' : 'missing' },
      ];
    },
  };
}

/** One browser for a whole film; openScene() gives a fresh page per scene file. */
async function openStudio(backend, state, genui) {
  const main = makeMain(backend, state, genui);
  const browser = await chromium.launch(process.env.O_DEMO_CHROME ? { executablePath: process.env.O_DEMO_CHROME } : {});
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: DSF, colorScheme: 'dark' });
  await ctx.route('http://o.demo/**', async (route) => {
    const u = new URL(route.request().url());
    const rel = decodeURIComponent(u.pathname).replace(/^\/+/, '');
    const file = path.resolve(ROOT, rel);
    if (!file.startsWith(ROOT)) return route.fulfill({ status: 403, body: 'no' });
    let body;
    try { body = fs.readFileSync(file); } catch (_) { return route.fulfill({ status: 404, body: 'not found' }); }
    route.fulfill({ status: 200, contentType: TYPES[path.extname(file)] || 'application/octet-stream', body });
  });
  await ctx.addInitScript({ path: path.join(__dirname, '..', 'mock-o.js') });
  await ctx.exposeFunction('__o_rpc', async (method, argsJson) => {
    const args = JSON.parse(argsJson || '[]');
    const fn = main[method];
    if (!fn) return null;
    try { return JSON.stringify(await fn(...args)); } catch (e) { console.error(`[rpc ${method}]`, e.message); return null; }
  });
  return {
    main, genui,
    async openScene() {
      const page = await ctx.newPage();
      page.on('pageerror', (e) => console.error('[scene] pageerror', e.message));
      page.on('console', (m) => { if (m.type() === 'error') console.error('[scene] console', m.text()); });
      await page.goto('http://o.demo/tools/demo/motion/scene.html');
      await page.waitForFunction(() => !!window.__scene);
      await page.evaluate(() => document.fonts.ready);
      return page;
    },
    close: () => browser.close(),
  };
}

/**
 * Render one scene file. film = [{shot, ...opts}]; cues = {name: async fn} run from Node when the frame clock
 * passes the shot's cue time; stills = [ms] saves PNGs instead of encoding (for checking frames before a render).
 */
async function renderScene(studio, { id, film, out, cues = {}, stills = null, stillsDir = null, log = console.log }) {
  const page = await studio.openScene();
  const built = await page.evaluate((film) => window.__scene.build(film), film);
  const frames = Math.ceil(built.duration / 1000 * FPS);
  const pending = built.cues.slice().sort((a, b) => a.t - b.t);
  const tick = async (ms) => {
    let ran = false;
    while (pending.length && pending[0].t <= ms) { const c = pending.shift(); if (cues[c.name]) { await cues[c.name](); ran = true; } }
    await page.evaluate((ms) => window.__scene.seek(ms), ms);
    if (ran) await sleep(60);
  };
  const t0 = Date.now();
  if (stills) {
    fs.mkdirSync(stillsDir, { recursive: true });
    const files = [];
    for (const ms of stills) {
      // seek through every frame up to the still so once-only hooks and cues fire in order
      const target = Math.min(ms, built.duration - 1);
      for (let f = 0; f * 1000 / FPS <= target; f++) await tick(Math.min(target, f * 1000 / FPS));
      await sleep(80);
      const file = path.join(stillsDir, `${id}-${String(Math.round(target)).padStart(6, '0')}ms.png`);
      await page.screenshot({ path: file, type: 'png' });
      files.push(file);
    }
    await page.close();
    return { id, durationS: built.duration / 1000, stills: files };
  }
  const tmp = out.replace(/\.mp4$/, '.tmp.mp4');
  fs.mkdirSync(path.dirname(out), { recursive: true });
  const ff = spawn('ffmpeg', ['-v', 'error', '-y', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-framerate', String(FPS), '-i', '-', ...ENCODE, tmp], { stdio: ['pipe', 'inherit', 'inherit'] });
  const done = new Promise((resolve, reject) => { ff.on('error', reject); ff.on('close', (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg exited ${code} for ${id}`)))); });
  let ffErr = null; ff.stdin.on('error', (e) => { ffErr = e; });
  for (let f = 0; f < frames; f++) {
    await tick(f * 1000 / FPS);
    const buf = await page.screenshot({ type: 'jpeg', quality: 92 });
    if (ffErr) throw ffErr;
    if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once('drain', r));
    if (f % 150 === 0) log(`  [${id}] frame ${f}/${frames} (${((Date.now() - t0) / 1000).toFixed(0)} s)`);
  }
  ff.stdin.end();
  await done;
  await page.close();
  fs.renameSync(tmp, out);
  const info = probe(out);
  log(`  [${id}] ${path.relative(ROOT, out)} ${info.durationS.toFixed(2)} s ${(fs.statSync(out).size / 1e6).toFixed(1)} MB ${frames} frames in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
  return { id, file: out, frames, durationS: info.durationS, bytes: fs.statSync(out).size, width: info.width, height: info.height };
}

function ff(args) { execFileSync('ffmpeg', ['-v', 'error', '-y', ...args], { stdio: ['ignore', 'inherit', 'inherit'] }); }

/** Join scene files with the concat demuxer (-c copy); --audio muxes a track with -c:v copy. */
function assemble(files, out, { audio } = {}) {
  const list = out.replace(/\.mp4$/, '.concat.txt');
  fs.writeFileSync(list, files.map((f) => `file '${f.replace(/'/g, "'\\''")}'`).join('\n') + '\n');
  const tmp = out.replace(/\.mp4$/, '.tmp.mp4');
  ff(['-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', '-movflags', '+faststart', tmp]);
  fs.unlinkSync(list);
  if (audio) {
    const mixed = out.replace(/\.mp4$/, '.tmp2.mp4');
    ff(['-i', tmp, '-i', audio, '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', mixed]);
    fs.unlinkSync(tmp);
    fs.renameSync(mixed, out);
  } else fs.renameSync(tmp, out);
  return probe(out);
}

module.exports = { genuiStore, openStudio, renderScene, assemble, probe, W, H, DSF, FPS, ENCODE };
