#!/usr/bin/env node
'use strict';
// Records the O AI demos: the real renderer (pet + panel) on a mock desktop, driven by Playwright, backed by
// the real engine / action log / API proxy from run.js. Each demo is a storyboard (demos/<id>.json) plus a
// driver (demos/<id>.js). Output: docs/videos/<id>.mp4 (h264, 30 fps) when ffmpeg is on PATH, else the
// Playwright .webm, and docs/videos/<id>.json with the duration and file size.
//
//   node tools/demo/record.js            # all demos
//   node tools/demo/record.js d1 d3      # some
//   --out <dir>      where the videos go (default docs/videos)
//   --keep-webm      keep the raw Playwright recording next to the mp4
//   --headed         watch it (slower, not for CI)
//
// O_DEMO_PLAYWRIGHT can point at another Playwright install; O_DEMO_CHROME at a Chrome/Chromium binary.

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync, spawnSync } = require('child_process');
const { chromium } = require(process.env.O_DEMO_PLAYWRIGHT || 'playwright');
const { startBackend, sleep, ROOT } = require('./run');

const HERE = __dirname;
const DEMOS_DIR = path.join(HERE, 'demos');
const W = 1600, H = 1000, SCALE = 2, FPS = 30;
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png', '.json': 'application/json', '.ico': 'image/x-icon' };

function hasFfmpeg() {
  const r = spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' });
  return !r.error && r.status === 0;
}

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

async function recordDemo(id, { outDir, keepWebm, headed }) {
  const story = JSON.parse(fs.readFileSync(path.join(DEMOS_DIR, `${id}.json`), 'utf8'));
  const driver = require(path.join(DEMOS_DIR, `${id}.js`));
  const work = fs.mkdtempSync(path.join(os.tmpdir(), `o-demo-${id}-`));
  const dataDir = path.join(work, 'data');
  const videoDir = path.join(work, 'video');
  fs.mkdirSync(videoDir, { recursive: true });
  console.log(`[${id}] ${story.title} — backend starting (data ${dataDir})`);
  const backend = await startBackend({ dataDir, cuTree: story.cuTree ? path.join(HERE, story.cuTree) : undefined });
  const genui = genuiStore();

  const browser = await chromium.launch({ headless: !headed, ...(process.env.O_DEMO_CHROME ? { executablePath: process.env.O_DEMO_CHROME } : {}) });
  const ctx = await browser.newContext({
    // Playwright records CSS pixels, so a 2x recording is a 2x viewport with the stage scaled up (stage.css html.x2).
    viewport: { width: W * SCALE, height: H * SCALE },
    deviceScaleFactor: 1,
    recordVideo: { dir: videoDir, size: { width: W * SCALE, height: H * SCALE } },
    colorScheme: 'dark',
  });
  await ctx.route('http://o.demo/**', async (route) => {
    const u = new URL(route.request().url());
    const rel = decodeURIComponent(u.pathname).replace(/^\/+/, '');
    const file = path.resolve(ROOT, rel);
    if (!file.startsWith(ROOT)) return route.fulfill({ status: 403, body: 'no' });
    let body;
    try { body = fs.readFileSync(file); } catch (_) { return route.fulfill({ status: 404, body: 'not found' }); }
    route.fulfill({ status: 200, contentType: TYPES[path.extname(file)] || 'application/octet-stream', body });
  });
  await ctx.addInitScript({ path: path.join(HERE, 'mock-o.js') });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => console.error(`[${id}] pageerror`, e.message));
  page.on('console', (m) => { if (m.type() === 'error') console.error(`[${id}] console`, m.text()); });

  // ---- what main.js would answer over IPC
  const state = { busy: false, pendingPrompts: [], onPrompt: null, chats: [] };
  const emit = (ev) => page.evaluate((e) => window.__stage.emit(e), ev).catch(() => {});
  backend.on('event', (ev) => { if (ev.type === 'genui') genui.push(ev.frame); emit(ev); });
  const main = {
    logTail: (n) => backend.actionLog.tail(Number(n) || 200),
    logMarkRead: () => backend.actionLog.markRead(),
    logPaths: () => backend.actionLog.paths(),
    logOpenFile: () => ({ ok: true }),
    logVerifyChain: () => backend.actionLog.verifyChain(),
    logNote: (summary, detail) => { backend.actionLog.record({ kind: 'note', summary: String(summary || ''), detail: detail || {} }); return { ok: true }; },
    engineStatus: () => ({ running: true, ready: true, port: backend.engine.port, url: backend.engine.url, python: 'python3', restarts: 0, lastHealth: backend.health, lastError: null, stderr: [] }),
    proxyStatus: () => backend.proxy.status(),
    getState: () => ({ busy: state.busy, queue: 0, agentLoaded: true, pendingPermissions: [], logUnread: backend.actionLog.unread }),
    sendPrompt: (text) => { if (state.onPrompt) state.onPrompt(String(text || '')); else state.pendingPrompts.push(String(text || '')); return { ok: true }; },
    cancel: () => ({ dropped: 0 }),
    listChats: () => state.chats,
    currentChat: () => null,
    newChat: () => ({ ok: true }),
    openChat: () => ({ ok: true, chat: { messages: [] } }),
    renameChat: () => ({ ok: true }),
    deleteChats: () => ({ ok: true, chat: { messages: [] } }),
    respondPermission: () => ({ ok: true }),
    genuiFrames: (surface) => genui.frames(surface),
    genuiSurfaces: () => genui.surfaces(),
    genuiAction: (action, payload) => { backend.actionLog.record({ kind: 'policy', summary: `GenUI action ${action}`, detail: { payload } }); if (action === 'open-log') emit({ type: 'open-log' }); return { ok: true }; },
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
  await page.exposeFunction('__o_rpc', async (method, argsJson) => {
    const args = JSON.parse(argsJson || '[]');
    const fn = main[method];
    if (!fn) return null;
    try { return JSON.stringify(await fn(...args)); } catch (e) { console.error(`[${id}] rpc ${method}:`, e.message); return null; }
  });

  await page.goto('http://o.demo/tools/demo/stage.html?scale=' + SCALE);
  await page.waitForFunction(() => { const f = document.getElementById('panel'); return f && f.contentWindow && f.contentWindow.document.readyState === 'complete' && f.contentWindow.document.getElementById('prompt'); });
  await sleep(600);

  // ---- helpers for the driver
  let mx = 800 * SCALE, my = 500 * SCALE;
  // move/clickAt take viewport pixels (what locator.boundingBox() returns); moveStage takes stage (1600×1000) coordinates
  const move = async (x, y, ms = 600) => {
    // wall-clock paced (a mouse move on a 3200×2000 page can take tens of ms), eased, always ends exactly at x,y
    const sx = mx, sy = my, start = Date.now();
    for (;;) {
      const k = Math.min(1, (Date.now() - start) / ms), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      await page.mouse.move(sx + (x - sx) * e, sy + (y - sy) * e);
      if (k >= 1) break;
      await sleep(16);
    }
    mx = x; my = y;
  };
  const stage = (fn, ...args) => page.evaluate(([fn, args]) => window.__stage[fn](...args), [fn, args]);
  const t0 = Date.now();
  const marks = [];
  // O_DEMO_SHOTS=<dir>: a PNG at every caption / mark (debugging a storyboard without watching the video)
  let shotN = 0;
  const shot = async (label) => {
    if (!process.env.O_DEMO_SHOTS) return;
    fs.mkdirSync(process.env.O_DEMO_SHOTS, { recursive: true });
    try { await page.screenshot({ path: path.join(process.env.O_DEMO_SHOTS, `${id}-${String(++shotN).padStart(2, '0')}-${label}.png`) }); } catch (_) {}
  };
  const demo = {
    id, story, page, backend, main, state, genui, emit, stage, sleep, move,
    moveStage: (x, y, ms) => move(x * SCALE, y * SCALE, ms),
    panel: page.frameLocator('#panel'),
    pet: page.frameLocator('#pet'),
    caption: async (t, sub) => { marks.push({ t: (Date.now() - t0) / 1000, caption: t || null }); await stage('caption', t || null, sub || null); await shot('caption'); },
    card: (show, sub, title) => stage('card', show, sub == null ? null : sub, title || null),
    status: (mood, text) => emit({ type: 'status', mood, text }),
    assistant: (text) => emit({ type: 'assistant', text }),
    /** A Claude Code tool call the way agent.js records and announces it (tool entry + status + tool event). */
    tool: (name, summary, input, mood = 'working') => {
      backend.actionLog.record({ kind: 'tool', summary, detail: { name, input: input || {}, tool_use_id: 'demo-' + Math.random().toString(36).slice(2, 8) } });
      emit({ type: 'status', mood, text: summary });
      return emit({ type: 'tool', name, summary });
    },
    toolResult: (name, ok = true, content = '') => backend.actionLog.record({ kind: 'result', summary: `${name} ${ok ? 'ok' : 'failed'}`, detail: { is_error: !ok, content: String(content).slice(0, 400) } }),
    task: (prompt) => { backend.actionLog.record({ kind: 'task', summary: 'Task: ' + prompt.slice(0, 120), detail: { prompt } }); state.busy = true; },
    done: (result, durationMs) => { backend.actionLog.record({ kind: 'task', summary: 'Done: ' + String(result || '').slice(0, 120), detail: { durationMs } }); state.busy = false; emit({ type: 'status', mood: 'success', text: 'Done.' }); return emit({ type: 'done', result, sessionId: 'demo', costUsd: 0, durationMs }); },
    async clickAt(x, y, ms = 500) { await move(x, y, ms); await page.mouse.down(); await sleep(70); await page.mouse.up(); },
    async click(locator, ms = 500) { const b = await locator.boundingBox(); if (!b) throw new Error('element not visible'); await demo.clickAt(b.x + b.width / 2, b.y + b.height / 2, ms); },
    async type(locator, text, delay = 26) { await demo.click(locator); await locator.pressSequentially(text, { delay }); },
    async openPanel() {
      // the ✦ button shows when the pet is hovered
      const pb = await stage('getBounds');
      await demo.moveStage(pb.x + 100, pb.y + 110, 600);
      await sleep(350);
      const btn = await demo.pet.locator('#prompt-btn').boundingBox();
      if (btn) await demo.clickAt(btn.x + btn.width / 2, btn.y + btn.height / 2, 400);
      else await stage('showPanel');
      await sleep(600);
    },
    /** Wait for the next prompt the user sends from the panel's composer. */
    nextPrompt: () => new Promise((resolve) => { if (state.pendingPrompts.length) return resolve(state.pendingPrompts.shift()); state.onPrompt = (t) => { state.onPrompt = null; resolve(t); }; }),
    mark: (label) => { marks.push({ t: (Date.now() - t0) / 1000, label }); return shot('mark'); },
    shot,
  };

  let err = null;
  try {
    await driver.run(demo);
  } catch (e) { err = e; console.error(`[${id}] driver failed:`, e); await shot('failed'); }
  await sleep(400);
  const videoPath = await page.video().path();
  await ctx.close();
  await browser.close();
  await backend.stop();
  if (err) throw err;

  fs.mkdirSync(outDir, { recursive: true });
  let out;
  if (hasFfmpeg()) {
    out = path.join(outDir, `${id}.mp4`);
    execFileSync('ffmpeg', ['-v', 'error', '-y', '-i', videoPath, '-vf', `scale=${W * SCALE}:${H * SCALE}:flags=lanczos,fps=${FPS},format=yuv420p`, '-c:v', 'libx264', '-preset', 'medium', '-crf', '21', '-movflags', '+faststart', '-an', out]);
    if (keepWebm) fs.copyFileSync(videoPath, path.join(outDir, `${id}.webm`));
  } else {
    out = path.join(outDir, `${id}.webm`);
    fs.copyFileSync(videoPath, out);
    console.log(`[${id}] ffmpeg not found: delivering the Playwright webm as is`);
  }
  const info = { id, title: story.title, file: path.relative(ROOT, out), bytes: fs.statSync(out).size, ...probe(out), marks, recordedAt: new Date().toISOString() };
  fs.writeFileSync(path.join(outDir, `${id}.json`), JSON.stringify(info, null, 2));
  fs.rmSync(work, { recursive: true, force: true });
  console.log(`[${id}] ${info.file} ${info.durationS.toFixed(1)}s ${(info.bytes / 1e6).toFixed(1)} MB ${info.width}x${info.height}`);
  return info;
}

async function main() {
  const argv = process.argv.slice(2);
  const opt = { outDir: path.join(ROOT, 'docs', 'videos'), keepWebm: false, headed: false };
  const ids = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--out') opt.outDir = path.resolve(argv[++i]);
    else if (argv[i] === '--keep-webm') opt.keepWebm = true;
    else if (argv[i] === '--headed') opt.headed = true;
    else ids.push(argv[i]);
  }
  const all = fs.readdirSync(DEMOS_DIR).filter((f) => f.endsWith('.json')).map((f) => f.replace(/\.json$/, '')).sort();
  const todo = ids.length ? ids : all;
  const results = [];
  for (const id of todo) {
    if (!all.includes(id)) throw new Error(`unknown demo ${id} (have: ${all.join(', ')})`);
    results.push(await recordDemo(id, opt));
  }
  console.log('\n' + results.map((r) => `${r.file}\t${r.durationS.toFixed(1)} s\t${(r.bytes / 1e6).toFixed(1)} MB`).join('\n'));
}

if (require.main === module) main().catch((e) => { console.error(e); process.exit(1); });
module.exports = { recordDemo, probe, hasFfmpeg, W, H, SCALE, FPS };
