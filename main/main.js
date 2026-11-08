'use strict';
// O AI: Electron main process.
// Owns the Island window (status capsule + command bar), the panel, the tray icon, settings, and the
// Claude Code agent, which keeps running in the background while windows hide.
const path = require('path');
const {
  app, BrowserWindow, ipcMain, screen, Tray, Menu, nativeImage,
  globalShortcut, Notification, shell, desktopCapturer, systemPreferences, clipboard,
} = require('electron');
const { spawn } = require('child_process');
const { LinkServer } = require('./link');
const shortcuts = require('./shortcuts');
const { ChatStore, deleteClaudeTranscripts } = require('./chats');
const { Settings } = require('./settings');
const { createUpdater } = require('./updater');
let createEvolveHost = null;
try { ({ createEvolveHost } = require('./evolve')); } catch (err) { console.error('[o] could not load evolve:', err.message); }

// The action log and the engine are optional modules: a missing one logs an error, O still runs.
let ActionLog = null;
let EngineProcess = null;
let createProxy = null;
try { ({ createProxy } = require('./proxy')); } catch (err) { console.error('[o] could not load the API proxy:', err.message); }
try { ({ ActionLog } = require('./actionlog')); } catch (err) { console.error('[o] could not load actionlog:', err.message); }
try { ({ EngineProcess } = require('./engine')); } catch (err) { console.error('[o] could not load engine:', err.message); }
let ambientMod = null; // presence, sensors, milestones (main/ambient.js)
let briefMod = null;   // the welcome-back overlay window (main/brief.js)
try { ambientMod = require('./ambient'); } catch (err) { console.error('[o] could not load ambient:', err.message); }
try { briefMod = require('./brief'); } catch (err) { console.error('[o] could not load the brief overlay:', err.message); }

// The Island window: compact while it only shows the status capsule, expanded while the command bar is
// open. Both share the anchor edges (top edge, or bottom edge for the bottom positions; the horizontal centre,
// or the corner's side), so swapping bounds never moves what is drawn. Placement: renderer/islandgeom.js.
const ISLAND_SHRINK_MS = 460; // the renderer's collapse morph, before the window shrinks back
const PANEL_W = 440;
const PANEL_H = 660;
const ASSETS = path.join(__dirname, '..', 'assets');
const RENDERER = path.join(__dirname, '..', 'renderer');
const IslandGeom = require(path.join(RENDERER, 'islandgeom.js')); // shared with the Island renderer
const ISLAND_EXPANDED = { width: IslandGeom.C.EXP_W, height: IslandGeom.C.EXP_H };
const ISLAND_PROX_MS = 100; // proximity poll (10 Hz), only while the compact Island is on screen

if (process.platform === 'linux') {
  // Transparent windows on Linux need these.
  app.commandLine.appendSwitch('enable-transparent-visuals');
  app.commandLine.appendSwitch('disable-gpu');
}
app.setName('O AI');
// Windows ties notifications and taskbar grouping to this id (the shortcuts carry it too).
if (process.platform === 'win32') app.setAppUserModelId(shortcuts.APP_ID);

if (!app.requestSingleInstanceLock()) {
  app.quit();
}

let islandWin = null;
let panelWin = null;
let tray = null;
let settings = null;
let agent = null;
let checkClaudeFn = async () => ({ installed: false, error: 'agent module missing' });
let quitting = false;
let commandBarOpen = false; // the Island is showing the command bar (expanded bounds, focused)
let islandShrinkTimer = null;
let islandScreenHooked = false;
let islandShape = null; // the capsule rect (compact window px) the renderer reports: Linux click shape, proximity fade
let islandL = null; // the current layout (IslandGeom.layout): position, mode, compact bounds
let islandAttention = false; // the capsule asks for the user (an approval): it never fades then
let islandFaded = false; // proximity fade: 25% opacity and click-through
let islandRevealed = false; // the user dwelt on the capsule: fully shown and clickable until the cursor leaves
let islandDwellSince = 0;
let islandProxTimer = null;
let rendererIgnore = true; // what the renderer's own hit test wants (macOS/Windows click-through)
let link = null; // O Link: the bridge to the user's own browser
let chats = null; // chat history
let updater = null;
let evolveHost = null;          // self-evolution: engine ticks, updater installs/rollbacks (main/evolve.js)
let actionLog = null; // append-only, hash-chained record of everything O did
let engine = null;    // the Python engine (provisioning, cache, remediation)
let proxy = null;     // the localhost OpenAI-compatible API proxy (main/proxy.js)
let ambient = null;   // presence -> task pool, milestones, the brief (main/ambient.js)
let briefOverlay = null;

let tasks = null;               // the task pool (main/tasks.js): one Claude Code process per task
let scheduler = null;           // saved schedules (main/scheduler.js)
let goals = null;               // long-term goals: schedules, cycles, checkpoints (main/goals.js)
let unseenDone = 0;             // tasks that ended while the panel was closed (tray / dock badge)
const taskCards = new Map();    // task id -> notify-sidebar card {id, kind: 'done'|'needs', title, text, at}
const pendingPerms = new Map(); // id -> { resolve, toolName, input, summary, expiresAt }
// GenUI (Phase 3): frames from the engine and the agent, kept in memory per surface so the panel
// can redraw a surface after a reload. The renderer validates the components; main only relays.
const genuiStore = new Map(); // surface -> { frames: [], title, ended, ok }
const GENUI_RING = 2000;      // frames kept per surface
const GENUI_MAX_SURFACES = 200; // oldest surfaces dropped beyond this
const GENUI_ACTIONS = ['open-log', 'open-file', 'retry', 'approve', 'deny'];
// Electron 26 added 'will-frame-navigate' (per-frame navigation hook); on an older build an unknown event name
// would silently register nothing, so the panel falls back to 'did-start-navigation' (see createPanelWindow).
const HAS_WILL_FRAME_NAVIGATE = (parseInt(process.versions.electron, 10) || 0) >= 26;
// Settings exported to the engine as O_AMBIENT / O_SENSOR_* / O_ABSENT_AFTER_S / O_BRIEF_TTS (main/engine.js).
const AMBIENT_KEYS = ['ambientEnabled', 'sensorWifi', 'sensorSonar', 'sensorCamera', 'sensorHeartRate', 'absentAfterMin', 'briefSpeak'];
const EVOLVE_KEYS = ['evolveEnabled', 'evolveBudgetUsd', 'evolvePerProposalUsd', 'evolveAskBeforeInstall', 'evolveAutoShip', 'evolveGithubToken', 'evolveRepo', 'evolveScanMinutes', 'evolveSlotsActive', 'evolveSlotsAway', 'practiceBudgetUsd', 'evolveSandbox'];
const PERMISSION_TIMEOUT_MS = 60 * 1000; // ask mode: matches the agent's auto-deny; a timeout destroys the queue
const UNREAD_NOTIFY_DEBOUNCE_MS = 30 * 1000;
let unreadNotifyTimer = null;
let notifyWin = null;   // the permission sidebar
let notifyHideTimer = null;

// ---------------------------------------------------------------- helpers

function broadcast(ev) {
  for (const w of [islandWin, panelWin]) {
    if (w && !w.isDestroyed()) w.webContents.send('agent:event', ev);
  }
}

function panelVisible() {
  return !!(panelWin && !panelWin.isDestroyed() && panelWin.isVisible());
}

/** Ambient away mode: the person is not at the computer, so nothing but the tray changes. */
function isAway() { return !!(ambient && ambient.isAway()); }

function notify(title, body) {
  if (isAway()) return; // away: the brief tells them when they are back
  try {
    if (!Notification.isSupported()) return;
    const n = new Notification({ title, body: String(body || '').slice(0, 200), silent: false });
    n.on('click', () => showPanel());
    n.show();
  } catch (_) { /* notifications are best-effort */ }
}

function workAreaFor(bounds) {
  const display = bounds
    ? screen.getDisplayNearestPoint({ x: Math.round(bounds.x + bounds.width / 2), y: Math.round(bounds.y + bounds.height / 2) })
    : screen.getPrimaryDisplay();
  return display.workArea;
}

function sendToIsland(ev) {
  if (islandWin && !islandWin.isDestroyed()) islandWin.webContents.send('agent:event', ev);
}

const THEMES = ['glass', 'light', 'dark'];
/** The saved theme, always one of THEMES (what the preload exposes as window.o.initialTheme). */
function currentTheme() {
  const t = settings ? settings.get().theme : null;
  return THEMES.includes(t) ? t : 'glass';
}

/** Push a theme change to every window (the notify sidebar persists, so it must be told). */
function sendTheme(theme) {
  const ev = { type: 'theme', theme };
  for (const w of [islandWin, panelWin, notifyWin]) {
    if (w && !w.isDestroyed()) w.webContents.send('agent:event', ev);
  }
}

// ---------------------------------------------------------------- action log + engine

// ---------------------------------------------------------------- GenUI relay

/** Store one validated-envelope frame (ring per surface, surfaces capped) and push it to the windows. */
function onGenuiFrame(frame) {
  if (!frame || typeof frame !== 'object' || typeof frame.surface !== 'string') return;
  const surface = frame.surface;
  let s = genuiStore.get(surface);
  if (!s) {
    s = { frames: [], title: '', ended: false, ok: null };
    genuiStore.set(surface, s);
    while (genuiStore.size > GENUI_MAX_SURFACES) genuiStore.delete(genuiStore.keys().next().value);
  } else {
    // Re-insert so the Map's order is "most recently active last".
    genuiStore.delete(surface); genuiStore.set(surface, s);
  }
  if (frame.type === 'begin') { s.title = typeof frame.title === 'string' ? frame.title : s.title; s.ended = false; s.ok = null; }
  if (frame.type === 'end') { s.ended = true; s.ok = frame.ok !== false; }
  s.frames.push(frame);
  if (s.frames.length > GENUI_RING) s.frames.splice(0, s.frames.length - GENUI_RING);
  if (frame.type === 'sandbox' && actionLog) {
    try { actionLog.record({ kind: 'note', summary: `GenUI sandbox preview for ${surface}`, detail: { surface, sandbox_id: frame.sandbox_id || null } }); } catch (_) {}
  }
  broadcast({ type: 'genui', frame });
}

function genuiFrames(surface) {
  const s = genuiStore.get(String(surface || ''));
  return s ? s.frames.slice() : [];
}

function genuiSurfaces() {
  return [...genuiStore.entries()].map(([surface, s]) => ({ surface, title: s.title, ended: s.ended, ok: s.ok, count: s.frames.length }));
}

/** Button actions from the panel: a closed set, every one recorded. */
async function genuiAction(action, payload) {
  action = String(action || '');
  payload = payload && typeof payload === 'object' ? payload : {};
  if (!GENUI_ACTIONS.includes(action)) return { ok: false, error: 'unknown GenUI action' };
  if (actionLog) {
    try { actionLog.record({ kind: 'policy', summary: `GenUI action ${action}`, detail: { payload } }); } catch (_) {}
  }
  if (action === 'open-log') { openActionLog(); return { ok: true }; }
  if (action === 'open-file') {
    // Always the action-log text file. A Button's payload is model/engine-authored and is never read here as a
    // path: a Button can not name an arbitrary file to open (same rule as the 'log:openFile' IPC).
    if (!actionLog) return { ok: false, error: 'action log unavailable' };
    const err = await shell.openPath(actionLog.paths().text);
    return err ? { ok: false, error: err } : { ok: true };
  }
  if ((action === 'approve' || action === 'deny') && payload.id && pendingPerms.has(String(payload.id))) {
    resolvePermission(String(payload.id), action === 'approve');
    return { ok: true, resolved: true };
  }
  return { ok: true };
}

function engineStatus() {
  if (!engine) return { ready: false, error: EngineProcess ? 'engine not started' : 'engine module missing' };
  try { const s = engine.status(); delete s.token; return s; } catch (err) { return { ready: false, error: err.message }; }
}

function startProxy() {
  if (!createProxy || !agent) return;
  if (!proxy) {
    proxy = createProxy({
      getSettings: () => settings.get(),
      actionLog,
      getEngine: () => (engine && engine.status().ready ? { url: engine.status().url, token: engine.status().token } : null),
      agent,
      userDataDir: app.getPath('userData'),
    });
  }
  proxy.start().catch((err) => console.error('[o] API proxy not started:', err.message));
}

function restartProxy() {
  if (!proxy) return startProxy();
  proxy.stop().then(() => proxy.start()).catch((err) => console.error('[o] API proxy restart:', err.message));
}

function withTimeout(promise, ms, fallback) {
  let t;
  const timer = new Promise((resolve) => { t = setTimeout(() => resolve(fallback), ms); t.unref(); });
  return Promise.race([promise, timer]).finally(() => clearTimeout(t));
}

/** `python3 --version` (then `python`, then `py -3`): first interpreter found and its version. */
function probePython(explicit) {
  const candidates = explicit ? [[explicit, ['--version']]] : [['python3', ['--version']], ['python', ['--version']], ['py', ['-3', '--version']]];
  const tryOne = ([cmd, args]) => new Promise((resolve) => {
    let out = '';
    let child;
    try {
      child = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    } catch (_) { return resolve(null); }
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { out += d; });
    child.on('error', () => resolve(null));
    child.on('close', () => {
      const m = /Python\s+(\d+)\.(\d+)(?:\.(\d+))?/.exec(out);
      resolve(m ? { cmd: [cmd, ...args.filter((a) => a !== '--version')].join(' '), major: +m[1], minor: +m[2], version: m[0] } : null);
    });
    setTimeout(() => { try { child.kill(); } catch (_) {} resolve(null); }, 4000).unref();
  });
  return candidates.reduce((p, c) => p.then((r) => r || tryOne(c)), Promise.resolve(null));
}

/** Hard requirements as a flat list for the Settings page (green/red). Never throws; 10 s bound. */
async function checkRequirements() {
  const rows = [];
  const nodeMajor = parseInt(process.versions.node, 10);
  rows.push({
    name: 'node', ok: nodeMajor >= 18, detail: 'Node ' + process.versions.node,
    fix: nodeMajor >= 18 ? '' : 'Install Node.js 18 or newer from nodejs.org.',
  });
  const [claude, python, health] = await Promise.all([
    withTimeout(Promise.resolve().then(() => checkClaudeFn(settings.get())).catch((err) => ({ installed: false, error: err.message })), 9000, { installed: false, error: 'timed out' }),
    withTimeout(probePython(settings.get().enginePython).catch(() => null), 9000, null),
    engine
      ? withTimeout(Promise.resolve().then(() => engine.call('GET', '/health')).catch((err) => ({ error: err.message })), 9000, { error: 'engine did not answer in time' })
      : Promise.resolve({ error: EngineProcess ? 'engine not running' : 'engine module missing' }),
  ]);
  rows.push({
    name: 'claude', ok: !!(claude && claude.installed),
    detail: claude && claude.installed ? (claude.version || claude.path || 'installed') : ((claude && claude.error) || 'not found'),
    fix: claude && claude.installed ? '' : 'Install Claude Code (npm i -g @anthropic-ai/claude-code) and run `claude` once to log in.',
  });
  const pyOk = !!(python && (python.major > 3 || (python.major === 3 && python.minor >= 10)));
  rows.push({
    name: 'python', ok: pyOk, detail: python ? `${python.version} (${python.cmd})` : 'no python3/python/py found',
    fix: pyOk ? '' : 'Install Python 3.10+ and set Settings → Engine → Python path if it is not on PATH.',
  });
  const h = health && !health.error ? health : null;
  const engineNote = h ? '' : ' (' + ((health && health.error) || 'engine offline') + ')';
  const fixes = {
    vllm: 'Start vLLM: `python -m vllm.entrypoints.openai.api_server --model ' + settings.get().vllmModel + ' --port 8000` (see install/).',
    screenpipe: 'Install and run screenpipe so its SQLite database exists at the default path, or set Settings → Engine → screenpipe DB.',
    xvfb: process.platform === 'linux' ? 'Install Xvfb (apt install xvfb) and run `Xvfb :99 &`.' : 'Not available on this OS: the isolated display is headless-only here.',
    playwright: 'Run `python -m playwright install chromium` with the engine\'s Python.',
  };
  for (const k of ['vllm', 'screenpipe', 'xvfb', 'playwright']) {
    const ok = !!(h && h[k]);
    rows.push({ name: k, ok, detail: h ? (ok ? 'ready' : 'missing') : 'unknown' + engineNote, fix: ok ? '' : fixes[k] });
  }
  return rows;
}

// ---------------------------------------------------------------- screen capture

const SCREEN_MAX = 1600; // longest side of the screenshot sent to Claude, in pixels

/** Screenshot of the display under the cursor (or the primary one), with O's windows hidden. */
async function captureScreen({ display } = {}) {
  let note = '';
  if (process.platform === 'darwin') {
    const status = systemPreferences.getMediaAccessStatus('screen');
    if (status !== 'granted') {
      note = 'macOS may be hiding other apps: allow O (or your terminal) in System Settings → Privacy & Security → Screen Recording.';
    }
  }
  const cursor = screen.getCursorScreenPoint();
  const displays = screen.getAllDisplays();
  const target = display === 'primary' ? screen.getPrimaryDisplay() : screen.getDisplayNearestPoint(cursor);
  const { width: dw, height: dh } = target.size;
  const scale = Math.min(1, SCREEN_MAX / Math.max(dw, dh));
  const thumb = { width: Math.round(dw * scale), height: Math.round(dh * scale) };

  // Keep O's own windows out of the picture (works on Windows and macOS).
  const own = [islandWin, panelWin].filter((w) => w && !w.isDestroyed() && w.isVisible());
  own.forEach((w) => { try { w.setContentProtection(true); } catch (_) {} });
  let sources;
  try {
    if (own.length) await new Promise((r) => setTimeout(r, 120));
    sources = await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: thumb });
  } finally {
    own.forEach((w) => { try { w.setContentProtection(false); } catch (_) {} });
  }
  const src = (sources || []).find((x) => String(x.display_id) === String(target.id)) || (sources || [])[0];
  if (!src || src.thumbnail.isEmpty()) throw new Error('the system returned no screen image' + (note ? ' (' + note + ')' : ''));
  sendToIsland({ type: 'status', mood: 'browsing', text: 'Reading your screen.' });
  const size = src.thumbnail.getSize();
  return {
    mimeType: 'image/jpeg',
    data: src.thumbnail.toJPEG(80).toString('base64'),
    width: size.width,
    height: size.height,
    displays: displays.length,
    displayIndex: Math.max(0, displays.findIndex((d) => d.id === target.id)),
    cursor: {
      x: Math.round((cursor.x - target.bounds.x) * size.width / target.bounds.width),
      y: Math.round((cursor.y - target.bounds.y) * size.height / target.bounds.height),
    },
    note,
  };
}

// ---------------------------------------------------------------- windows

/** The Island layout for the primary display and the islandPosition setting (renderer/islandgeom.js). */
function computeIslandLayout() {
  const s = settings ? settings.get() : {};
  // O_ISLAND_NOTCH=1|0 forces the notch layout on or off (Electron exposes no notch inset to detect it with).
  const env = process.env.O_ISLAND_NOTCH;
  const forceNotch = env === '1' ? true : env === '0' ? false : undefined;
  return IslandGeom.layout({ platform: process.platform, display: screen.getPrimaryDisplay(), position: s.islandPosition, notchWidth: s.islandNotchWidth, forceNotch });
}

function currentIslandLayout() {
  if (!islandL) islandL = computeIslandLayout();
  return islandL;
}

/** Compact or expanded Island bounds: in the macOS menu bar, above the taskbar, or where the setting says. */
function islandBounds(expanded) {
  const L = currentIslandLayout();
  return expanded ? IslandGeom.expandedBounds(L) : { ...L.compact };
}

/** macOS menu bar / notch: above the menu bar (NSStatusWindowLevel); everywhere else above other windows. */
function applyIslandLevel() {
  if (!islandWin || islandWin.isDestroyed()) return;
  const L = currentIslandLayout();
  const inMenuBar = process.platform === 'darwin' && (L.mode === 'menubar' || L.mode === 'notch');
  try { islandWin.setAlwaysOnTop(true, inMenuBar ? 'status' : 'screen-saver'); } catch (_) {}
}

/** Re-place the Island (a setting changed, a display changed) and tell the renderer its new geometry. */
function relayoutIsland() {
  if (!islandWin || islandWin.isDestroyed()) { islandL = null; return; }
  const was = islandL;
  islandL = computeIslandLayout();
  sendToIsland({ type: 'island-layout', layout: islandL });
  islandWin.setBounds(islandBounds(commandBarOpen));
  applyIslandShape();
  applyIslandLevel();
  if (islandL.hidden && !commandBarOpen) { if (islandWin.isVisible()) islandWin.hide(); }
  else if (was && was.hidden && !islandL.hidden && !islandWin.isVisible()) islandWin.showInactive();
  syncIslandProximity();
  rebuildTrayMenu();
}

/** Persist a new Island position from the menus (the panel goes through settings:set). */
function setIslandPosition(pos) {
  if (!settings || !IslandGeom.POSITIONS.includes(pos)) return;
  const before = settings.get().islandPosition;
  settings.set({ islandPosition: pos });
  if (before !== pos && actionLog) {
    try { actionLog.record({ kind: 'settings', summary: 'Settings changed: islandPosition', detail: { keys: ['islandPosition'] } }); } catch (_) {}
  }
  relayoutIsland();
}

// ---- never in the way: the proximity fade
// While the compact Island is on screen, main polls the cursor at 10 Hz. Within 40 px of the capsule it fades
// to 25% and lets clicks through to whatever is under it (a tab, a menu), unless the cursor dwells on the
// capsule itself for 350 ms: then it is fully shown and clickable until the cursor moves away. An Island that
// is asking for the user (an approval) never fades. The poll stops whenever the compact Island is not shown.
function islandCompactShown() {
  return !!(islandWin && !islandWin.isDestroyed() && islandWin.isVisible() && !commandBarOpen);
}

function syncIslandProximity() {
  const want = islandCompactShown();
  if (want && !islandProxTimer) {
    islandProxTimer = setInterval(islandProximityTick, ISLAND_PROX_MS);
    if (islandProxTimer.unref) islandProxTimer.unref();
  } else if (!want && islandProxTimer) {
    clearInterval(islandProxTimer);
    islandProxTimer = null;
  }
  if (!want) { islandDwellSince = 0; islandRevealed = false; setIslandFaded(false); }
}

/** The capsule in screen coordinates. */
function islandCapsuleScreenRect() {
  const L = currentIslandLayout();
  const b = islandWin.getBounds();
  const r = islandShape || IslandGeom.capsuleRect(L, L.minW);
  return { x: b.x + r.x, y: b.y + r.y, width: r.width, height: r.height };
}

function islandProximityTick() {
  if (!islandCompactShown()) { syncIslandProximity(); return; }
  let p;
  try { p = screen.getCursorScreenPoint(); } catch (_) { return; }
  const d = IslandGeom.distanceToRect(p, islandCapsuleScreenRect());
  if (d > IslandGeom.C.FADE_NEAR) { islandDwellSince = 0; islandRevealed = false; setIslandFaded(false); return; }
  if (islandAttention) { setIslandFaded(false); return; }
  if (d === 0) {
    if (!islandDwellSince) islandDwellSince = Date.now();
    if (Date.now() - islandDwellSince >= IslandGeom.C.DWELL_MS) islandRevealed = true;
  } else if (!islandRevealed) islandDwellSince = 0;
  setIslandFaded(!islandRevealed);
}

function setIslandFaded(v) {
  if (v === islandFaded) return;
  islandFaded = v;
  sendToIsland({ type: 'island-fade', faded: v });
  applyIslandIgnore();
}

/** Click-through: faded always passes clicks on; otherwise the renderer's hit test decides (macOS/Windows). */
function applyIslandIgnore() {
  if (!islandWin || islandWin.isDestroyed()) return;
  const faded = islandFaded && !commandBarOpen;
  if (process.platform === 'linux') {
    // No forwarded mouse moves on Linux: faded = an empty input region, otherwise the capsule shape.
    try { islandWin.setIgnoreMouseEvents(faded); } catch (_) {}
    if (!faded) applyIslandShape();
    return;
  }
  try { islandWin.setIgnoreMouseEvents(faded || rendererIgnore, { forward: true }); } catch (_) {}
}

// macOS focus return, Spotlight style: the Island is created as an NSPanel-like window (type: 'panel',
// supported since Electron 21; package.json pins ^33). That adds NSWindowStyleMaskNonactivatingPanel, so the
// command bar becomes the key window and takes typing WITHOUT activating O. The app the user was in stays the
// active app, and when the bar closes and resigns key, keyboard focus is simply back in that app. This is
// chosen over the fallback (record whether O was frontmost when the bar opened, then app.hide() on close only
// if it was not) because app.hide() also hides the panel and would flicker the Island itself, and it cannot
// tell "O was frontmost" apart from "the user clicked the Island while in another app". For the same reason
// openCommandBar no longer calls app.focus({ steal: true }) on macOS: activating O is what the panel avoids.
const ISLAND_IS_PANEL = process.platform === 'darwin';

function createIslandWindow() {
  islandL = computeIslandLayout();
  islandWin = new BrowserWindow({
    ...islandBounds(false),
    ...(ISLAND_IS_PANEL ? { type: 'panel' } : {}),
    // macOS: a frameless window with this flag is placed exactly where asked, including over the menu bar
    // (otherwise AppKit's constrainFrameRect pushes it below the bar).
    ...(process.platform === 'darwin' ? { enableLargerThanScreen: true } : {}),
    transparent: true,
    frame: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    hasShadow: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    focusable: true,
    fullscreenable: false,
    backgroundColor: '#00000000',
    title: 'O',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false, // the status line keeps up while O works in the background
    },
  });
  // Never steal focus from what the user is doing: only opening the command bar focuses the Island.
  islandWin.once('ready-to-show', () => {
    if (!currentIslandLayout().hidden || commandBarOpen) {
      islandWin.showInactive();
      // macOS may constrain a window as it is first ordered in: put it back in the menu bar strip.
      if (process.platform === 'darwin') islandWin.setBounds(islandBounds(commandBarOpen));
    }
    syncIslandProximity();
  });
  applyIslandLevel();
  // Not on fullscreen Spaces: a fullscreen app (video, game, presentation) has the screen to itself.
  try { islandWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: false }); } catch (_) {}
  islandWin.loadFile(path.join(RENDERER, 'index.html'));
  applyIslandShape();
  // Spotlight behaviour: clicking anywhere else collapses the command bar.
  islandWin.on('blur', () => { if (commandBarOpen) closeCommandBar({ notify: true }); });
  islandWin.on('closed', () => { islandWin = null; commandBarOpen = false; syncIslandProximity(); });
  islandWin.on('hide', () => syncIslandProximity());
  islandWin.on('show', () => syncIslandProximity());
  // Displays change (resolution, the menu bar or the Dock, a monitor plugged or unplugged): lay out again.
  if (!islandScreenHooked) {
    islandScreenHooked = true;
    screen.on('display-metrics-changed', relayoutIsland);
    screen.on('display-removed', relayoutIsland);
    screen.on('display-added', relayoutIsland);
  }
}

/** Expand the Island into the command bar and give it keyboard focus (shortcut, Island click). */
function openCommandBar({ notify = false } = {}) {
  if (!islandWin || islandWin.isDestroyed()) createIslandWindow();
  clearTimeout(islandShrinkTimer);
  commandBarOpen = true;
  syncIslandProximity(); // no fade while the bar is open
  applyIslandIgnore();
  islandWin.setBounds(islandBounds(true));
  applyIslandShape(); // Linux: the whole bar takes clicks while open
  if (!islandWin.isVisible()) islandWin.showInactive();
  // macOS: no app.focus() here, see ISLAND_IS_PANEL. Focusing a non-activating panel makes it key only.
  islandWin.focus();
  if (notify) sendToIsland({ type: 'island', open: true });
}

/** Collapse back to the capsule; the window shrinks once the renderer's morph has played. */
function closeCommandBar({ notify = false } = {}) {
  if (!commandBarOpen) return;
  commandBarOpen = false;
  if (notify) sendToIsland({ type: 'island', open: false });
  if (islandWin && !islandWin.isDestroyed() && islandWin.isFocused()) islandWin.blur();
  clearTimeout(islandShrinkTimer);
  islandShrinkTimer = setTimeout(() => {
    if (!commandBarOpen && islandWin && !islandWin.isDestroyed()) {
      islandWin.setBounds(islandBounds(false));
      applyIslandShape();
      if (currentIslandLayout().hidden) islandWin.hide(); // 'hidden': only the bar was shown
      syncIslandProximity();
    }
  }, ISLAND_SHRINK_MS);
}

// Linux has no click-through with forwarded mouse moves, so the transparent strip of the compact window
// around the capsule (448x60 window, 220-420x36 capsule; 448x56 at the bottom) would swallow clicks. There the window is shaped to
// the capsule rect the renderer reports (island:shape); while the bar is open the shape is the whole window.
// "Cleared" is a full-window rect rather than [], since an empty list can mean "no region" on X11.
// macOS and Windows keep setIgnoreMouseEvents with forwarding instead.
function applyIslandShape() {
  if (process.platform !== 'linux' || !islandWin || islandWin.isDestroyed() || typeof islandWin.setShape !== 'function') return;
  const full = { x: 0, y: 0, width: ISLAND_EXPANDED.width, height: ISLAND_EXPANDED.height };
  try { islandWin.setShape([commandBarOpen || !islandShape ? full : islandShape]); } catch (_) {}
}

/** Validate a capsule rect from the renderer against the compact window. */
function cleanIslandShape(r) {
  if (!r || typeof r !== 'object') return null;
  const n = (v) => (Number.isFinite(Number(v)) ? Math.round(Number(v)) : NaN);
  const x = n(r.x), y = n(r.y), width = n(r.width), height = n(r.height);
  if ([x, y, width, height].some(Number.isNaN) || width <= 0 || height <= 0) return null;
  const { width: W, height: H } = currentIslandLayout().compact;
  const cx = Math.max(0, Math.min(W - 1, x));
  const cy = Math.max(0, Math.min(H - 1, y));
  return { x: cx, y: cy, width: Math.min(width, W - cx), height: Math.min(height, H - cy) };
}

/**
 * The vertical band other windows (panel, notify cards) keep clear of the Island, for a window spanning
 * [x0, x1] on this work area: nothing when the Island is in the menu bar or does not overlap horizontally.
 */
function islandReserve(wa, x0, x1) {
  const out = { top: wa.y, bottom: wa.y + wa.height, reserved: false };
  if (!islandWin || islandWin.isDestroyed() || !islandWin.isVisible()) return out;
  const L = currentIslandLayout();
  if (L.mode !== 'float') return out; // in the menu bar / on the notch: the work area is all free
  const c = L.compact;
  if (x1 <= c.x || x0 >= c.x + c.width) return out;
  out.reserved = true;
  if (L.dir === 'up') out.bottom = Math.min(out.bottom, c.y + c.height - L.gap - L.ih - 8);
  else out.top = Math.max(out.top, c.y + c.height);
  return out;
}

function toggleCommandBar() {
  if (commandBarOpen) closeCommandBar({ notify: true }); else openCommandBar({ notify: true });
}

// The panel is a rounded glass sheet: a transparent, frameless window whose renderer paints the
// translucent background (renderer/panel.css). macOS and Windows blur the desktop behind it;
// Linux has no such material, so there it is simply translucent. Drag/resize/Escape are unchanged.
function glassWindowOptions() {
  const o = { transparent: true, backgroundColor: '#00000000', roundedCorners: true };
  if (process.platform === 'darwin') { o.vibrancy = 'under-window'; o.visualEffectState = 'active'; }
  else if (process.platform === 'win32') o.backgroundMaterial = 'acrylic';
  return o;
}

function createPanelWindow() {
  panelWin = new BrowserWindow({
    width: PANEL_W,
    height: PANEL_H,
    minWidth: 320,
    minHeight: 380,
    show: false,
    frame: false,
    ...glassWindowOptions(),
    resizable: true,
    alwaysOnTop: true,
    skipTaskbar: false,
    title: 'O AI',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  panelWin.loadFile(path.join(RENDERER, 'panel.html'));
  panelWin.on('close', (e) => {
    if (!quitting) { e.preventDefault(); hidePanel(); }
  });
  panelWin.on('closed', () => { panelWin = null; });
  // Open external links in the user's own browser, never inside the panel.
  panelWin.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  // Sub-frames in the panel are the GenUI preview sandboxes (renderer/preview.html, <iframe sandbox="allow-scripts">).
  // Their document arrives by postMessage and runs under preview.html's own CSP; a frame must never be navigated
  // anywhere else (a sandboxed script can still set `location`, open a `javascript:`/`data:` URL or follow a form
  // POST), since that would swap the hash-pinned bootstrap for an arbitrary page inside O's window. Only a
  // navigation to preview.html itself (the initial load, a reload) is allowed. The main frame is not touched here:
  // it only ever loads panel.html. 'will-frame-navigate' is the per-frame hook (Electron >= 26); older builds fall
  // back to 'did-start-navigation', which fires for sub-frames too and can still cancel the navigation.
  const guardSubFrame = (e) => {
    if (e && e.isMainFrame === false && !/\/preview\.html$/.test(String(e.url || ''))) e.preventDefault();
  };
  panelWin.webContents.on(HAS_WILL_FRAME_NAVIGATE ? 'will-frame-navigate' : 'did-start-navigation', guardSubFrame);
}

/** The panel sits at the right of the Island's display, vertically centred, clear of the Island. */
function positionPanel() {
  if (!panelWin || panelWin.isDestroyed()) return;
  const wa = workAreaFor(islandWin && !islandWin.isDestroyed() ? islandWin.getBounds() : null);
  const [w, h0] = panelWin.getSize();
  const x = wa.x + wa.width - w - 16;
  // Clear of the capsule when it shares the panel's column (top/bottom positions, the right corners); in the
  // menu bar the whole work area is free.
  const res = islandReserve(wa, x, x + w);
  const top = res.top > wa.y ? res.top : wa.y + 12;
  const bottom = res.bottom - 12;
  // On a short screen the height is clamped; place the panel by the height it ends up with (never below the
  // 380 px minimum), so it is centred and its top stays clear of the Island.
  const h = Math.max(380, Math.min(h0, bottom - top));
  if (h !== h0) panelWin.setSize(w, h);
  let y = Math.max(top, wa.y + Math.round((wa.height - h) / 2));
  if (y + h > bottom) y = Math.max(top, bottom - h);
  panelWin.setPosition(Math.round(x), Math.round(y));
}

// focus=false shows the panel without taking keyboard focus from whatever the user is doing
// (a game, a document...). Only explicit user actions (prompt button, shortcut, tray) focus it.
// ---------------------------------------------------------------- permission sidebar
// When O needs an OK and its chat is closed, cards slide in at the right edge of the
// screen (never taking focus), styled like the prompt box. Allow/Deny answer right there.

const NOTIFY_W = 400;

function createNotifyWindow() {
  notifyWin = new BrowserWindow({
    width: NOTIFY_W,
    height: 200,
    transparent: true,
    frame: false,
    resizable: false,
    movable: false,
    hasShadow: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    focusable: true,
    fullscreenable: false,
    backgroundColor: '#00000000',
    roundedCorners: true,
    title: 'O needs your OK',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  notifyWin.setAlwaysOnTop(true, 'screen-saver');
  try { notifyWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true }); } catch (_) {}
  notifyWin.loadFile(path.join(RENDERER, 'notify.html'));
  notifyWin.webContents.on('did-finish-load', () => syncNotify());
  notifyWin.on('closed', () => { notifyWin = null; });
}

function placeNotify(height) {
  if (!notifyWin || notifyWin.isDestroyed()) return;
  const wa = workAreaFor(islandWin && !islandWin.isDestroyed() ? islandWin.getBounds() : null);
  const x = wa.x + wa.width - NOTIFY_W - 6;
  const res = islandReserve(wa, x, x + NOTIFY_W); // below a top-right Island, above a bottom-right one
  const y = res.top > wa.y ? res.top + 4 : wa.y + 12;
  const h = Math.max(60, Math.min(Math.round(height || 200), res.bottom - y - 28));
  notifyWin.setBounds({ x, y, width: NOTIFY_W, height: h });
}

function pendingList() {
  return [...pendingPerms.values()].map(({ id, toolName, input, summary, expiresAt, taskId, taskTitle }) => ({ id, toolName, input, summary, expiresAt, taskId: taskId || null, taskTitle: taskTitle || '' }));
}

/** Show, update or hide the sidebar to match the pending approvals and the task cards. */
function syncNotify() {
  const list = pendingList();
  const away = isAway(); // away: no cards slide in (approvals still do, so ask mode keeps working)
  const cards = away ? [] : [...taskCards.values()];
  const checkpoints = goals && !away ? goals.pendingCards() : [];
  const want = (list.length > 0 || cards.length > 0 || checkpoints.length > 0) && !panelVisible();
  if (!want) {
    if (notifyWin && !notifyWin.isDestroyed()) {
      notifyWin.webContents.send('notify:perms', []);
      notifyWin.webContents.send('notify:tasks', []);
      notifyWin.webContents.send('notify:checkpoints', []);
      clearTimeout(notifyHideTimer);
      notifyHideTimer = setTimeout(() => { if (notifyWin && !notifyWin.isDestroyed() && !((pendingPerms.size || (!isAway() && (taskCards.size || (goals && goals.pendingCards().length)))) && !panelVisible())) notifyWin.hide(); }, 260);
    }
    return;
  }
  clearTimeout(notifyHideTimer);
  if (!notifyWin || notifyWin.isDestroyed()) { createNotifyWindow(); return; } // did-finish-load syncs again
  notifyWin.webContents.send('notify:perms', list);
  notifyWin.webContents.send('notify:tasks', cards);
  notifyWin.webContents.send('notify:checkpoints', checkpoints);
  if (!notifyWin.isVisible()) { placeNotify(notifyWin.getBounds().height); notifyWin.showInactive(); }
}

function showPanel({ focus = true } = {}) {
  if (!panelWin) createPanelWindow();
  positionPanel();
  if (focus) {
    panelWin.show();
    panelWin.focus();
  } else if (!panelWin.isVisible()) {
    panelWin.showInactive();
  }
  broadcast({ type: 'panel', open: true });
  // The panel lists every task now: the sidebar's task cards and the badge are done with.
  taskCards.clear();
  unseenDone = 0;
  updateBadge();
  syncNotify(); // the chat shows the approval cards now
}

function hidePanel() {
  if (panelWin && !panelWin.isDestroyed()) panelWin.hide();
  broadcast({ type: 'panel', open: false });
  syncNotify();
}

function togglePanel() {
  if (panelVisible()) hidePanel(); else showPanel();
}

function openActionLog() {
  showPanel();
  broadcast({ type: 'open-log' });
}

/** Right-click → Position: one radio item per islandPosition value. */
function islandPositionMenu() {
  const mac = process.platform === 'darwin';
  const cur = currentIslandLayout().position;
  const items = [
    ['auto', mac ? 'Automatic (in the menu bar)' : 'Automatic (above the taskbar)'],
    ...(mac ? [['menubar', 'Menu bar']] : []),
    ['top', 'Top centre'], ['bottom', 'Bottom centre'],
    ['top-left', 'Top left'], ['top-right', 'Top right'], ['bottom-left', 'Bottom left'], ['bottom-right', 'Bottom right'],
    ['hidden', 'Hidden (the shortcut still opens the bar)'],
  ];
  return items.map(([value, label]) => ({ label, type: 'radio', checked: cur === value, click: () => setIslandPosition(value) }));
}

function toggleIsland() {
  if (!islandWin) { createIslandWindow(); rebuildTrayMenu(); return; }
  if (islandWin.isVisible()) { closeCommandBar({ notify: true }); islandWin.hide(); }
  else if (currentIslandLayout().hidden) { setIslandPosition('auto'); if (!islandWin.isVisible()) islandWin.showInactive(); }
  else islandWin.showInactive();
  syncIslandProximity();
  rebuildTrayMenu();
}

// ---------------------------------------------------------------- tray

function trayIcon(sensing = false) {
  let img = nativeImage.createFromPath(path.join(ASSETS, 'tray.png'));
  if (img.isEmpty()) img = nativeImage.createFromPath(path.join(ASSETS, 'o-base.png'));
  if (img.isEmpty()) return nativeImage.createEmpty();
  const size = process.platform === 'darwin' ? 18 : 22;
  img = img.resize({ width: size, height: Math.round(size * img.getSize().height / img.getSize().width) || size, quality: 'best' });
  if (!sensing || !ambientMod) return img;
  // A sensor is live: a small white dot in the corner, visible whenever anything is sensing.
  try {
    const { width, height } = img.getSize();
    return nativeImage.createFromBitmap(ambientMod.dotBitmap(img.toBitmap(), width, height), { width, height });
  } catch (_) { return img; }
}

let traySensing = false;
function updateTrayIcon() {
  const sensing = !!(ambient && ambient.live.length);
  if (!tray || sensing === traySensing) return;
  traySensing = sensing;
  try { tray.setImage(trayIcon(sensing)); } catch (_) {}
}

/** Show the panel on Settings → Ambient (the header glyph, the tray's sensing item). */
function openAmbientSettings() {
  showPanel();
  broadcast({ type: 'open-ambient' });
}

function broadcastAmbient() {
  if (!ambient) return;
  broadcast({ type: 'ambient', ...ambient.status() });
  updateTrayIcon();
  updateBadge();
}

function createTray() {
  try {
    tray = new Tray(trayIcon());
    tray.setToolTip('O AI');
    tray.on('click', () => togglePanel());
    rebuildTrayMenu();
  } catch (err) {
    console.error('[o] tray unavailable:', err.message);
  }
}

function rebuildTrayMenu() {
  if (!tray) return;
  const islandShown = !!(islandWin && islandWin.isVisible());
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'O AI', enabled: false },
    { type: 'separator' },
    { label: islandShown ? 'Hide the Island' : 'Show the Island', click: toggleIsland },
    { label: 'Open command bar  (Ctrl/Cmd+Shift+O)', click: () => openCommandBar({ notify: true }) },
    { label: 'Open panel', click: () => showPanel() },
    { label: 'Open action log', click: openActionLog },
    ...(ambient && ambient.live.length ? [{ label: `Sensing: ${ambient.live.map((x) => x.label).join(', ')}…`, click: openAmbientSettings }] : []),
    { label: tasks && tasks.running().length > 1 ? `Stop all ${tasks.running().length} tasks` : 'Stop all tasks', click: () => stopTask(), enabled: !!(tasks && tasks.activeCount()) },
    { type: 'separator' },
    {
      label: 'Start O when I log in', type: 'checkbox', checked: shortcuts.getOpenAtLogin(app),
      click: (item) => { settings.set({ openAtLogin: item.checked }); shortcuts.setOpenAtLogin(app, item.checked); },
    },
    ...(process.platform === 'win32' ? [{ label: 'Create desktop shortcut', click: () => {
      const r = shortcuts.createShortcuts(app, shell);
      notify(r.ok ? 'Shortcut created' : 'Could not create the shortcut', r.ok ? 'O AI is on your Desktop and in the Start menu.' : (r.error || ''));
    } }] : []),
    { type: 'separator' },
    updater && updater.state.state === 'available'
      ? { label: 'Update O now', click: () => updater.apply() }
      : { label: 'Check for updates', click: () => updater && updater.check(), enabled: !!updater },
    { type: 'separator' },
    { label: 'Turn off O', click: () => turnOff() },
  ]));
}

// ---------------------------------------------------------------- agent + task pool

// A follow-up that reads like a correction of the last result is a learning signal (POST /learn/observe).
const CORRECTION_RE = /^\s*(no\b|nope|not (that|quite|what)|actually|instead|wrong|that's (not|wrong)|redo|try again|again,|do it again|fix (it|that)|change (it|that)|make it|i (said|meant|wanted))/i;
const TASK_CARD_MS = 12 * 1000; // "Done" cards leave on their own; "Needs you" cards stay until seen

/** Engine POST /learn/observe: best effort, never blocks a task. */
function observe(body) {
  if (!engine) return;
  let st;
  try { st = engine.status(); } catch (_) { return; }
  if (!st || !st.ready) return;
  Promise.resolve().then(() => engine.call('POST', '/learn/observe', body, 5000)).catch(() => {});
}

function logApp(kind, summary, detail) {
  if (!actionLog) return;
  try { actionLog.record({ kind, summary, detail: detail || {} }); } catch (_) {}
}

let tasksTimer = null;
/** The task list changed: one 'tasks' broadcast per 120 ms at most (status lines change often). */
function broadcastTasks() {
  if (tasksTimer) return;
  tasksTimer = setTimeout(() => {
    tasksTimer = null;
    broadcast({ type: 'tasks', tasks: tasks ? tasks.list() : [], schedules: scheduler ? scheduler.list() : [] });
    updateBadge();
  }, 120);
}

/** Tray + dock badge: tasks that finished while the panel was closed, and the running count in the tooltip. */
function updateBadge() {
  const running = tasks ? tasks.running().length : 0;
  try { app.setBadgeCount(unseenDone); } catch (_) {}
  if (!tray) return;
  try {
    const away = isAway() ? ` — Away · ${running} task${running === 1 ? '' : 's'} running` : (running ? ` — ${running} running` : '');
    const sensing = ambient && ambient.live.length ? ` — sensing: ${ambient.live.map((x) => x.label).join(', ')}` : '';
    tray.setToolTip(`O AI${away}${unseenDone ? ` — ${unseenDone} new` : ''}${sensing}`);
    if (process.platform === 'darwin') tray.setTitle(unseenDone ? String(unseenDone) : running ? '•' : '');
  } catch (_) {}
}

/** A permission request from one task's agent: the chat card, the sidebar, a 60 s auto-deny. */
function askUser(req) {
  return new Promise((resolve) => {
    const expiresAt = Date.now() + PERMISSION_TIMEOUT_MS;
    const t = req.taskId && tasks ? tasks.get(req.taskId) : null;
    pendingPerms.set(req.id, { resolve, ...req, taskTitle: t ? t.title : '', expiresAt });
    setTimeout(() => {
      if (!pendingPerms.has(req.id)) return;
      pendingPerms.delete(req.id);
      broadcast({ type: 'permission-resolved', id: req.id, allow: false, taskId: req.taskId });
      syncNotify();
    }, PERMISSION_TIMEOUT_MS + 500).unref();
    // The sidebar slides in at the screen edge (no focus stolen, safe while gaming).
    // With quiet mode off, the chat panel opens too (also without taking focus).
    if (!panelVisible() && !settings.get().quiet && !isAway()) showPanel({ focus: false });
    syncNotify();
  });
}

/** POST /schedule from a task's bridge (schedule_task / list_schedules / cancel_schedule). Every change is logged. */
function handleSchedule(body) {
  if (!scheduler) return { ok: false, error: 'scheduler unavailable' };
  const r = scheduler.handle(body);
  if (r.ok && body.op === 'add') logApp('note', `Schedule added: ${r.schedule.title} — ${r.schedule.label}`, { schedule: r.schedule, taskId: body.taskId || null });
  else if (r.ok && body.op === 'cancel') logApp('note', `Schedule cancelled: ${r.cancelled ? r.cancelled.title : body.id}`, { id: body.id, taskId: body.taskId || null });
  else if (!r.ok && body.op !== 'list') logApp('note', `Schedule refused: ${r.error}`, { op: body.op, taskId: body.taskId || null });
  broadcastTasks();
  return r;
}

function fireSchedule(s) {
  if (s.goalId) { // a goal's cycle: today's prompt comes from the engine (objective, trend, last reflection)
    if (goals) goals.fireCycle(s.goalId).catch((err) => logApp('note', `Goal cycle could not start: ${err.message}`, { goalId: s.goalId }));
    return;
  }
  logApp('task', `Scheduled task started: ${s.title} (${s.label})`, { scheduleId: s.id });
  startTask({ prompt: s.prompt, title: s.title, source: 'schedule', scheduleId: s.id });
}

/** POST /goal from a task's bridge (goal tools + the checkpoint gate). */
function handleGoal(taskId, body) {
  if (!goals) return body && body.op === 'gate' ? { ok: true, allow: false, message: 'Goals are not loaded, so checkpoint actions are held.' } : { ok: false, error: 'goals unavailable' };
  return goals.handle(taskId, body);
}

function engineCall(method, p, body, timeoutMs) {
  let ready = false;
  try { ready = !!(engine && engine.status().ready); } catch (_) { ready = false; }
  if (!ready) return Promise.reject(new Error('The engine is not running, so goals are unavailable right now.'));
  return engine.call(method, p, body, timeoutMs);
}

let goalsTimer = null;
function broadcastGoals() {
  if (goalsTimer) return;
  goalsTimer = setTimeout(() => { goalsTimer = null; if (goals) broadcast({ type: 'goals', ...goals.list() }); syncNotify(); }, 120);
}

function loadAgent() {
  let mod, TaskManager, Scheduler;
  try {
    mod = require('./agent');
    ({ TaskManager } = require('./tasks'));
    ({ Scheduler } = require('./scheduler'));
  } catch (err) {
    console.error('[o] could not load agent:', err.message);
    return;
  }
  if (typeof mod.checkClaude === 'function') checkClaudeFn = mod.checkClaude;
  const common = {
    userDataDir: app.getPath('userData'),
    getSettings: () => settings.get(),
    captureScreen,
    actionLog,
    getEngine: () => (engine && engine.status().ready ? { url: engine.status().url, token: engine.status().token } : null),
    getLinkEndpoint: () => (link ? link.cdpEndpoint() : null),
  };
  // The host agent runs no tasks: its HTTP server is the engine's log/GenUI endpoint and the proxy's config writer.
  agent = new mod.OAgent({ ...common, getChatId: () => null, onPermissionRequest: askUser, onSchedule: handleSchedule, onGoal: handleGoal });
  agent.on('event', (ev) => { if (ev.type === 'genui') onGenuiFrame(ev.frame); });

  tasks = new TaskManager({
    getSettings: () => settings.get(),
    actionLog,
    createAgent: (taskId, opts) => new mod.OAgent({
      ...common, taskId, getChatId: () => taskId, onSchedule: handleSchedule, onGoal: handleGoal,
      goalId: (opts && opts.goalId) || (goals && goals.goalForTask(taskId)) || null,
      onPermissionRequest: (req) => askUser({ ...req, taskId }),
    }),
  });
  tasks.on('changed', broadcastTasks);
  tasks.on('event', onTaskEvent);
  tasks.on('end', onTaskEnd);

  scheduler = new Scheduler({ file: path.join(app.getPath('userData'), 'o-schedules.json'), onFire: fireSchedule });
  scheduler.on('changed', broadcastTasks);
  scheduler.on('error', (err) => console.error('[o] scheduler:', err.message));

  try {
    const { GoalRunner } = require('./goals');
    goals = new GoalRunner({
      engineCall, scheduler, startTask, getTasks: () => (tasks ? tasks.list() : []),
      file: path.join(app.getPath('userData'), 'o-goals.json'), log: logApp,
    });
    goals.on('changed', broadcastGoals);
    goals.on('checkpoint', (card) => {
      if (ambient) ambient.onCheckpoint(card).catch(() => {});
      if (!panelVisible()) notify('O has something for you to approve', 'Open the card to approve, edit or skip it.');
    });
    goals.on('error', (err) => console.error('[o] goals:', err.message));
  } catch (err) { console.error('[o] goals not loaded:', err.message); }
  scheduler.start();
}

function onTaskEvent(ev) {
  if (ev.type === 'genui') { onGenuiFrame(ev.frame); return; } // stored + broadcast there; not chat history
  broadcast(ev);
  if (ev.type === 'permission-timeout') {
    // Ask mode: no answer in 60 s stops that task and drops the tasks still waiting (nothing runs on a stale approval).
    tasks.stop(ev.taskId);
    const dropped = tasks.dropQueued();
    logApp('policy', `Approval timed out after 60 s: ${ev.toolName || ev.summary || 'tool'} — task stopped, ${dropped} queued task(s) dropped`,
      { id: ev.id, toolName: ev.toolName, summary: ev.summary, dropped, taskId: ev.taskId });
    sendToIsland({ type: 'status', mood: 'error', text: 'No answer in time. I stopped that task.' });
  }
  if (chats && ev.taskId && ev.type !== 'status') chats.recordTo(ev.taskId, ev);
  if (ev.type === 'done' || ev.type === 'error') {
    // That task's approvals still open (e.g. timed out inside the agent) are now moot.
    for (const [id, p] of pendingPerms) {
      if (p.taskId !== ev.taskId) continue;
      pendingPerms.delete(id);
      broadcast({ type: 'permission-resolved', id, allow: false, taskId: ev.taskId });
    }
    syncNotify();
  }
}

function onTaskEnd({ task, ok, sessionId, durationMs, result }) {
  if (chats) { chats.setSessionFor(task.id, sessionId); broadcast({ type: 'chats' }); }
  if (goals) goals.onTaskEnd({ task, ok, result }).catch((err) => logApp('note', `Goal cycle not recorded: ${err.message}`, { taskId: task.id }));
  // The engine records finished /provision and /computer/run jobs itself: observe only the other tasks.
  const viaEngine = (task.tools || []).some((n) => n === 'mcp__o__provision' || n === 'mcp__o__computer_run' || n === 'mcp__o__computer_replay');
  if (task.status !== 'stopped' && !viaEngine) {
    const used = (task.tools || []).map((n) => n.replace(/^mcp__/, '')).slice(0, 8).join(', ');
    const text = `${task.title}${task.prompt && task.prompt !== task.title ? ' — ' + task.prompt : ''}${used ? ` (tools: ${used})` : ''}`;
    observe({ source: 'task', task_id: task.id, text: text.slice(0, 2000), outcome: ok ? 'ok' : 'failed', duration_s: Math.round((durationMs || 0) / 100) / 10 });
  }
  if (!panelVisible() && task.status !== 'stopped') {
    unseenDone += 1;
    if (!isAway()) addTaskCard(task, ok); // away: tray badge only; the brief covers it
  }
  updateBadge();
  rebuildTrayMenu();
}

/** "Done: <title>" / "Needs you: <title>" in the notify sidebar while the panel is closed. */
function addTaskCard(task, ok) {
  const card = { id: task.id, kind: ok ? 'done' : 'needs', title: task.title, text: ok ? (task.result || 'Finished.') : (task.error || task.statusLine || 'It hit a snag.'), at: Date.now() };
  taskCards.set(task.id, card);
  if (ok) setTimeout(() => { if (taskCards.get(task.id) === card) { taskCards.delete(task.id); syncNotify(); } }, TASK_CARD_MS).unref();
  syncNotify();
}

/** Turn O off completely: stop every task (killing Claude Code and its tools), then quit. */
function turnOff() {
  if (tasks) tasks.stopAll();
  quitting = true;
  app.quit();
}

/** Answer a pending approval (from the chat, the sidebar or a GenUI approve/deny button). */
function resolvePermission(id, allow) {
  const p = pendingPerms.get(id);
  if (!p) return false;
  pendingPerms.delete(id);
  if (chats) chats.recordTo(p.taskId, { type: 'permission-resolved', id, allow: !!allow, toolName: p.toolName, summary: p.summary });
  p.resolve(allow ? { allow: true } : { allow: false, message: 'The user denied this action.' });
  broadcast({ type: 'permission-resolved', id, allow: !!allow, taskId: p.taskId });
  syncNotify();
  return true;
}

/** Stop one task (its approvals are denied), or every task and the queue when no id is given. */
function stopTask(taskId) {
  if (!tasks) return { ok: false, dropped: 0 };
  for (const [id, p] of pendingPerms) {
    if (taskId && p.taskId !== taskId) continue;
    p.resolve({ allow: false, message: 'Cancelled by user' });
    pendingPerms.delete(id);
  }
  syncNotify();
  const r = taskId ? tasks.stop(taskId) : tasks.stopAll();
  rebuildTrayMenu();
  return { ok: !!r.ok, dropped: r.dropped || 0, error: r.error };
}

/** Start a task (or a follow-up in an ended one); its chat is created with the task's id. */
function startTask({ prompt, taskId, title, source, scheduleId, goalId } = {}) {
  if (!tasks) return { ok: false, error: 'O\'s task runner did not load. Check the terminal for errors.' };
  const prior = taskId ? tasks.get(taskId) : null;
  if (taskId && !prior && chats && chats.get(taskId)) {
    // A task from an earlier session of O: resume its saved Claude Code session.
    const c = chats.get(taskId);
    const gid = goalId || (goals && goals.goalForTask(taskId)) || null;
    tasks.tasks.set(taskId, { id: taskId, title: c.title, prompt: '', status: 'done', statusLine: 'Done', mood: 'idle', createdAt: c.createdAt, startedAt: null, endedAt: c.updatedAt, source: gid ? 'goal' : 'user', scheduleId: null, goalId: gid, sessionId: c.sessionId || null, agent: null, result: '', error: '', runs: 0, tools: new Set() });
  }
  const r = tasks.start({ prompt, taskId, title, source, scheduleId, goalId });
  if (!r.ok) return r;
  if (chats) { chats.ensureTask(r.taskId, title || null); chats.addUserTo(r.taskId, prompt); broadcast({ type: 'chats' }); }
  if (r.followUp && CORRECTION_RE.test(prompt)) observe({ source: 'correction', task_id: r.taskId, text: prompt.slice(0, 2000), ...(prior ? { outcome: prior.status === 'done' ? 'ok' : 'failed' } : {}) });
  rebuildTrayMenu();
  return r;
}

function enqueuePrompt(text, opts) {
  text = String(text || '').trim();
  if (!text) return { ok: false, error: 'empty prompt' };
  if (!tasks) {
    const message = 'O\'s brain (agent module) did not load. Check the terminal for errors.';
    broadcast({ type: 'error', message });
    return { ok: false, error: message };
  }
  const taskId = opts && typeof opts.taskId === 'string' ? opts.taskId : null;
  return startTask({ prompt: text, taskId });
}

// ---------------------------------------------------------------- IPC

function registerIpc() {
  // Island window (status capsule + command bar).
  ipcMain.handle('island:getBounds', () => (islandWin ? islandWin.getBounds() : islandBounds(false)));
  ipcMain.handle('island:getWorkArea', () => workAreaFor(islandWin ? islandWin.getBounds() : null));
  ipcMain.handle('island:getCursor', () => screen.getCursorScreenPoint());
  ipcMain.on('island:layout', (e) => { e.returnValue = currentIslandLayout(); }); // preload, synchronously
  ipcMain.handle('island:getLayout', () => currentIslandLayout());
  ipcMain.on('island:setIgnoreMouse', (_e, ignore) => {
    rendererIgnore = !!ignore;
    applyIslandIgnore();
  });
  ipcMain.on('island:shape', (_e, rect) => {
    const r = cleanIslandShape(rect);
    if (!r) return;
    islandShape = r;
    islandAttention = !!(rect && rect.attention === true);
    if (islandAttention) setIslandFaded(false);
    applyIslandShape();
  });
  ipcMain.on('island:open', () => openCommandBar());
  ipcMain.on('island:close', () => closeCommandBar());

  ipcMain.on('panel:toggle', togglePanel);
  ipcMain.on('panel:show', () => showPanel());
  ipcMain.on('panel:hide', hidePanel);
  ipcMain.on('app:quit', () => turnOff());
  ipcMain.on('island:menu', () => {
    if (!islandWin || islandWin.isDestroyed()) return;
    Menu.buildFromTemplate([
      { label: 'Open panel', click: () => showPanel() },
      { label: 'Open action log', click: () => openActionLog() },
      { label: 'Stop all tasks', click: () => stopTask(), enabled: !!(tasks && tasks.activeCount()) },
      { label: 'Position', submenu: islandPositionMenu() },
      { label: 'Hide the Island (the tray icon brings it back)', click: () => toggleIsland() },
      { type: 'separator' },
      { label: 'Turn off O', click: () => turnOff() },
    ]).popup({ window: islandWin });
  });

  ipcMain.handle('agent:prompt', (_e, text, opts) => enqueuePrompt(text, opts));
  ipcMain.handle('agent:cancel', (_e, taskId) => stopTask(typeof taskId === 'string' && taskId ? taskId : undefined));
  // ---------- tasks + schedules ----------
  const taskList = () => ({ tasks: tasks ? tasks.list() : [], schedules: scheduler ? scheduler.list() : [], max: tasks ? tasks.maxParallel() : 3 });
  ipcMain.handle('tasks:list', () => taskList());
  ipcMain.handle('tasks:stop', (_e, id) => stopTask(String(id || '')));
  ipcMain.handle('tasks:resume', (_e, id) => (tasks ? tasks.resume(String(id || '')) : { ok: false, error: 'tasks unavailable' }));
  // ---------- ambient sensing ----------
  ipcMain.handle('ambient:status', () => (ambient ? ambient.status() : { enabled: false, state: 'UNKNOWN', away: false, sensors: [], live: [], catalog: [], error: 'ambient module missing' }));
  ipcMain.handle('ambient:enableSensor', (_e, key, confirmed) => {
    key = String(key || '');
    if (!ambientMod || !ambientMod.SENSOR_KEYS.includes(key)) return { ok: false, error: 'unknown sensor' };
    if (confirmed !== true) return { ok: false, error: 'a sensor turns on only after its confirmation' };
    return applySettings({ [key]: true }, key);
  });
  ipcMain.on('brief:done', () => { if (briefOverlay) briefOverlay.hide(); });
  ipcMain.on('brief:resize', (_e, h) => { if (briefOverlay) briefOverlay.resize(h); });
  ipcMain.handle('tasks:view', (_e, id) => {
    id = String(id || '');
    const t = tasks && tasks.get(id);
    const c = chats && chats.get(id);
    if (!t && !c) return { ok: false, error: 'That task no longer exists.' };
    if (c) chats.open(id);
    return { ok: true, task: t ? tasks.snapshot(t) : null, chat: chatOut(c) };
  });
  ipcMain.handle('tasks:redo', (_e, id) => {
    id = String(id || '');
    const t = tasks && tasks.get(id);
    if (t && ['queued', 'running'].includes(t.status)) return { ok: false, error: 'That task is still running.' };
    const c = chats && chats.get(id);
    const users = c ? c.messages.filter((m) => m.role === 'user') : [];
    const prompt = (t && t.prompt) || (users.length ? users[users.length - 1].text : '');
    if (!prompt) return { ok: false, error: 'Nothing to redo.' };
    observe({ source: 'correction', task_id: id, text: ('Redo requested: ' + prompt).slice(0, 2000), ...(t ? { outcome: t.status === 'done' ? 'ok' : 'failed' } : {}) });
    logApp('note', 'Redo: ' + prompt.slice(0, 120), { taskId: id });
    return startTask({ prompt: 'Do this again from scratch, and do it better than last time: ' + prompt, taskId: id });
  });
  ipcMain.handle('schedules:list', () => (scheduler ? scheduler.list() : []));
  ipcMain.handle('schedules:cancel', (_e, id) => {
    if (!scheduler) return { ok: false };
    const s = scheduler.get(String(id || ''));
    const ok = scheduler.cancel(String(id || ''));
    if (ok) logApp('note', `Schedule cancelled: ${s ? s.title : id}`, { id, by: 'user' });
    broadcastTasks();
    return { ok };
  });
  // ---------- what O has learned (engine /learn) ----------
  const engineReady = () => { try { return !!(engine && engine.status().ready); } catch (_) { return false; } };
  ipcMain.handle('learn:profile', async () => {
    if (!engineReady()) return { ok: false, error: 'The engine is not running, so nothing learned can be shown right now.' };
    try {
      const p = await engine.call('GET', '/learn/profile', null, 5000);
      const items = [];
      // "Schedule this?" offers: a habit the engine noticed, with a ready prompt and schedule.
      const suggestions = [];
      for (const it of Array.isArray(p && p.suggestions) ? p.suggestions : []) {
        const sg = it && it.suggest_schedule;
        if (!sg || !sg.prompt) continue;
        let label = '';
        try { label = require('./scheduler').describeWhen(require('./scheduler').whenFrom(sg)); } catch (_) { continue; }
        suggestions.push({ id: it.id != null ? String(it.id) : null, text: String(it.text || sg.prompt).slice(0, 300), prompt: String(sg.prompt).slice(0, 4000), when: sg.when || null, cron: sg.cron || null, label });
      }
      for (const [kind, key] of [['preference', 'preferences'], ['habit', 'habits'], ['app skill', 'app_skills']]) {
        for (const it of Array.isArray(p && p[key]) ? p[key] : []) {
          const text = typeof it === 'string' ? it : it && (it.text || it.summary || it.name || it.app);
          if (!text) continue;
          items.push({ id: it && typeof it === 'object' && it.id != null ? String(it.id) : null, kind, text: String(text).slice(0, 300) });
        }
      }
      return { ok: true, items, suggestions };
    } catch (err) { return { ok: false, error: err.message }; }
  });
  ipcMain.handle('learn:acceptSuggestion', (_e, sg) => {
    if (!scheduler || !sg || typeof sg !== 'object') return { ok: false, error: 'scheduler unavailable' };
    let when;
    try { when = require('./scheduler').whenFrom(sg); } catch (err) { return { ok: false, error: err.message }; }
    const r = scheduler.handle({ op: 'add', prompt: sg.prompt, title: sg.text, when });
    if (r.ok) {
      logApp('note', `Schedule added from a suggestion: ${r.schedule.title} — ${r.schedule.label}`, { schedule: r.schedule, suggestion: sg.id || null });
      if (sg.id && engineReady()) engine.call('POST', '/learn/feedback', { confirm: String(sg.id) }, 5000).catch(() => {});
    }
    broadcastTasks();
    return r;
  });
  ipcMain.handle('learn:forget', async (_e, id) => {
    if (!engineReady()) return { ok: false, error: 'engine not running' };
    try {
      await engine.call('POST', '/learn/feedback', { forget: String(id || '') }, 5000);
      logApp('settings', 'Forgot a learned preference', { id: String(id || '') });
      return { ok: true };
    } catch (err) { return { ok: false, error: err.message }; }
  });
  // ---------- long-term goals (engine /goals, main/goals.js) ----------
  const goalsOr = (fn) => async (...a) => {
    if (!goals) return { ok: false, error: 'Goals did not load.' };
    try { return await fn(...a); } catch (err) { return { ok: false, error: err.message }; }
  };
  ipcMain.handle('goals:list', goalsOr(async () => { if (engineReady()) await goals.refresh().catch(() => {}); return { ok: true, ...goals.list() }; }));
  ipcMain.handle('goals:detail', goalsOr((_e, id) => goals.detail(String(id || ''))));
  ipcMain.handle('goals:create', goalsOr((_e, text) => goals.create(String(text || '').slice(0, 2000))));
  ipcMain.handle('goals:propose', goalsOr(() => goals.propose()));
  ipcMain.handle('goals:accept', goalsOr((_e, proposal) => goals.createFromProposal(proposal && typeof proposal === 'object' ? proposal : {})));
  ipcMain.handle('goals:status', goalsOr((_e, id, op) => goals.setStatus(String(id || ''), String(op || ''))));
  ipcMain.handle('goals:autonomy', goalsOr((_e, id, autonomy, allowed) => goals.setAutonomy(String(id || ''), String(autonomy || ''), Array.isArray(allowed) ? allowed.map(String) : [])));
  ipcMain.handle('goals:runNow', goalsOr((_e, id) => goals.fireCycle(String(id || ''), { reason: 'run now' })));
  ipcMain.handle('goals:checkpoint', goalsOr((_e, id, decision, text) => goals.resolveCheckpoint(String(id || ''), String(decision || ''), text == null ? '' : String(text))));
  ipcMain.on('notify:checkpoint', (_e, id, decision) => {
    if (!goals) return;
    if (decision === 'edit') { // editing needs words: open the goal view with that card's edit box
      const cp = goals.checkpoints.find((c) => c.id === String(id || ''));
      showPanel();
      broadcast({ type: 'view-goal', goalId: cp ? cp.goalId : '', checkpointId: String(id || ''), edit: true });
      return;
    }
    goals.resolveCheckpoint(String(id || ''), decision === 'approve' ? 'approve' : 'skip').catch(() => {});
  });

  ipcMain.handle('app:shortcuts', () => shortcuts.createShortcuts(app, shell));
  // ---------- chat history ----------
  const chatOut = (c) => (c ? { id: c.id, title: c.title, messages: c.messages, updatedAt: c.updatedAt } : null);
  const activeTask = (id) => { const t = tasks && tasks.get(id); return !!(t && ['queued', 'running'].includes(t.status)); };
  // A new chat is just an empty view: the first prompt creates the task (and its chat). Running tasks keep going.
  const newChat = () => ({ ok: true, chat: null });
  ipcMain.handle('agent:newSession', newChat);
  ipcMain.handle('chats:new', newChat);
  ipcMain.handle('chats:list', () => (chats ? chats.list() : []));
  ipcMain.handle('chats:current', () => chatOut(chats && chats.current()));
  ipcMain.handle('chats:open', (_e, id) => {
    const c = chats.open(id);
    if (!c) return { ok: false, error: 'That chat no longer exists.' };
    broadcast({ type: 'chats' });
    return { ok: true, chat: chatOut(c) };
  });
  ipcMain.handle('chats:rename', (_e, id, title) => {
    const c = chats.rename(id, title);
    broadcast({ type: 'chats' });
    return { ok: !!c };
  });
  ipcMain.handle('chats:delete', (_e, ids) => {
    const list = ids === 'all' ? chats.list().map((c) => c.id) : [].concat(ids || []);
    if (list.some(activeTask)) return { ok: false, error: 'O is still working on that task. Stop it first (■), then delete it.' };
    const sessions = chats.remove(list);
    const removedFiles = deleteClaudeTranscripts(sessions);
    if (tasks) for (const id of list) tasks.tasks.delete(id);
    broadcastTasks();
    broadcast({ type: 'chats' });
    return { ok: true, deleted: list.length, removedFiles, chat: chatOut(chats.current()) };
  });
  ipcMain.on('agent:permission', (_e, id, allow) => resolvePermission(id, allow));
  // ---------- GenUI ----------
  ipcMain.handle('log:note', (_e, summary, detail) => {
    if (!actionLog) return { ok: false };
    try { actionLog.record({ kind: 'note', summary: String(summary || '').slice(0, 500), detail: detail && typeof detail === 'object' ? detail : {} }); return { ok: true }; } catch (err) { return { ok: false, error: err.message }; }
  });
  ipcMain.handle('genui:frames', (_e, surface) => genuiFrames(surface));
  ipcMain.handle('genui:surfaces', () => genuiSurfaces());
  ipcMain.handle('genui:action', (_e, action, payload) => genuiAction(action, payload));
  ipcMain.on('notify:resize', (_e, h) => placeNotify(h));
  ipcMain.on('notify:openChat', () => showPanel());
  ipcMain.on('notify:openTask', (_e, id) => { showPanel(); broadcast({ type: 'view-task', taskId: String(id || '') }); });
  ipcMain.on('notify:dismissTask', (_e, id) => { taskCards.delete(String(id || '')); syncNotify(); });

  ipcMain.handle('settings:get', () => settings.get());
  // Synchronous: the preload reads it before the renderer paints, so there is no theme flash.
  ipcMain.on('settings:theme', (e) => { e.returnValue = currentTheme(); });
  const ENGINE_KEYS = ['permissionMode', 'vllmUrl', 'vllmModel', 'screenpipeDb', 'sandboxRoot', 'maxRemediationAttempts', 'enginePython',
    'cuCandidates', 'cuMaxHeals', 'vllmVisionModel', 'mcpServers', 'highRiskSandbox', 'e2bApiKey', 'dockerImage', 'dockerNetwork', 'swarmEnabled', 'genuiEnabled',
    'cuStepsPerTurn', 'cuMaxTurns', 'cuGroundConfidence', 'cuStepCandidates', 'cuFeasibilityGate', 'cuZoomGrounding', 'cuComparativeRanking', 'cuInterfaceBuilder', 'cuMaxModelCalls', 'grantedFolders',
    ...EVOLVE_KEYS, ...AMBIENT_KEYS];
  const PROXY_KEYS = ['proxyEnabled', 'proxyPort', 'proxyToken', 'proxyConcurrency', 'proxyMode'];
  ipcMain.handle('settings:set', (_e, partial) => applySettings(partial, null).settings);
  // `consented`: the one sensor key the confirmation sheet approved (ambient:enableSensor); a plain settings
  // write can turn sensors off but never on.
  function applySettings(partial, consented) {
    const before = settings.get();
    if (ambientMod) partial = ambientMod.consentFilter(before, partial, consented);
    const r = settings.set(partial);
    if (consented && r[consented] === true && before[consented] !== true) logApp('policy', `Sensor turned on after confirmation: ${consented}`, { key: consented });
    if (r.evolveAskBeforeInstall !== before.evolveAskBeforeInstall) {
      logApp('policy', r.evolveAskBeforeInstall ? 'Ask before installing new tools and upgrades: on (each install and upgrade waits for Approve)'
        : 'Ask before installing new tools and upgrades: off (O installs tools and ships upgrades that pass every check on its own)', { key: 'evolveAskBeforeInstall' });
    }
    if (partial && typeof partial.openAtLogin === 'boolean') { shortcuts.setOpenAtLogin(app, partial.openAtLogin); rebuildTrayMenu(); }
    const keys = Object.keys(r).filter((k) => JSON.stringify(before[k]) !== JSON.stringify(r[k]));
    if (keys.length && actionLog) {
      // Keys only: tokens never reach the log.
      try { actionLog.record({ kind: 'settings', summary: 'Settings changed: ' + keys.join(', '), detail: { keys } }); } catch (_) {}
    }
    if (engine && keys.some((k) => ENGINE_KEYS.includes(k))) {
      try { engine.restart(); } catch (err) { console.error('[o] engine restart:', err.message); }
    }
    if (proxy && keys.some((k) => PROXY_KEYS.includes(k))) restartProxy();
    if (keys.includes('theme')) sendTheme(currentTheme());
    if (keys.includes('islandPosition') || keys.includes('islandNotchWidth')) relayoutIsland();
    if (ambient && keys.some((k) => AMBIENT_KEYS.includes(k))) { ambient.onSettings(); broadcastAmbient(); }
    return { ok: true, settings: r };
  }
  // ---------- API proxy ----------
  const proxyStatusSafe = () => {
    if (!proxy) return { enabled: false, running: false, error: createProxy ? 'proxy not started' : 'proxy module missing' };
    try { return proxy.status(); } catch (err) { return { enabled: false, running: false, error: err.message }; }
  };
  ipcMain.handle('proxy:status', () => proxyStatusSafe());
  ipcMain.handle('proxy:regenToken', () => {
    settings.set({ proxyToken: require('crypto').randomBytes(24).toString('hex') });
    if (actionLog) { try { actionLog.record({ kind: 'settings', summary: 'API proxy token regenerated', detail: { keys: ['proxyToken'] } }); } catch (_) {} }
    restartProxy();
    return { ok: true };
  });
  // ---------- action log + engine ----------
  const noLog = { ok: false, error: 'action log unavailable' };
  ipcMain.handle('log:tail', (_e, n) => (actionLog ? actionLog.tail(Number(n) || 200) : []));
  ipcMain.handle('log:markRead', () => {
    if (!actionLog) return noLog;
    const n = actionLog.unread;
    actionLog.markRead();
    clearTimeout(unreadNotifyTimer); unreadNotifyTimer = null;
    broadcast({ type: 'log-unread', count: 0 });
    sendToIsland({ type: 'status', mood: 'success', text: `Here's everything I did — ${n} action${n === 1 ? '' : 's'} since you last looked.` });
    return { ok: true, wasUnread: n };
  });
  ipcMain.handle('log:paths', () => (actionLog ? actionLog.paths() : { jsonl: '', text: '' }));
  ipcMain.handle('log:openFile', async () => {
    if (!actionLog) return noLog;
    const err = await shell.openPath(actionLog.paths().text);
    return err ? { ok: false, error: err } : { ok: true };
  });
  ipcMain.handle('log:verify', () => {
    if (!actionLog) return { ok: false, count: 0, error: noLog.error };
    try { return actionLog.verifyChain(); } catch (err) { return { ok: false, count: 0, error: err.message }; }
  });
  ipcMain.handle('engine:status', () => engineStatus());
  ipcMain.handle('requirements:check', () => withTimeout(
    checkRequirements().catch((err) => [{ name: 'check', ok: false, detail: err.message, fix: 'Restart O and try again.' }]),
    10000,
    [{ name: 'check', ok: false, detail: 'timed out after 10 s', fix: 'Restart O and try again.' }],
  ));
  ipcMain.handle('claude:check', async () => {
    try { return await checkClaudeFn(settings.get()); } catch (err) { return { installed: false, error: err.message }; }
  });
  ipcMain.handle('link:status', () => (link ? link.status() : { connected: false, error: 'O Link is not running' }));
  ipcMain.handle('link:connect', () => {
    if (!link) return { ok: false, error: 'O Link is not running' };
    const dir = link.prepareExtension();
    try { clipboard.writeText(dir); } catch (_) {}
    shell.openPath(dir).catch(() => {});
    // Open the browser's Extensions page (Edge first, as O's default browser).
    let browserPath = '';
    try { browserPath = require('./agent').findInstalledBrowser(); } catch (_) {}
    const page = /edge/i.test(browserPath) ? 'edge://extensions/' : 'chrome://extensions/';
    if (browserPath) {
      try { spawn(browserPath, [page], { detached: true, stdio: 'ignore', windowsHide: false }).unref(); } catch (_) {}
    }
    return { ok: true, dir, page, browser: /edge/i.test(browserPath) ? 'Edge' : browserPath ? 'Chrome' : '' };
  });
  ipcMain.handle('browser:extension', () => {
    try { return require('./agent').findExtension(); } catch (err) { return { installed: false, error: err.message }; }
  });
  ipcMain.handle('browser:installExtension', async () => {
    const store = 'https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm';
    // Open the store page in Edge on Windows (the extension must be installed in the browser O uses).
    const url = process.platform === 'win32' ? 'microsoft-edge:' + store : store;
    try { await shell.openExternal(url); } catch (_) { await shell.openExternal(store); }
    return { ok: true };
  });
  // ---------- self-evolution (Evolution view) ----------
  const noEvolve = { ok: false, error: 'Evolution is unavailable until the engine is running.' };
  ipcMain.handle('evolve:view', (_e, hours) => (evolveHost && engineReady() ? evolveHost.view(hours) : noEvolve));
  ipcMain.handle('evolve:approve', (_e, id) => (evolveHost ? evolveHost.approve(String(id || '')) : noEvolve));
  ipcMain.handle('evolve:reject', (_e, id, reason) => (evolveHost ? evolveHost.reject(String(id || ''), reason) : noEvolve));
  ipcMain.handle('evolve:rollback', (_e, id) => (evolveHost ? evolveHost.rollback(String(id || '')) : noEvolve));
  ipcMain.handle('evolve:skill', (_e, id, approve) => (evolveHost ? evolveHost.skill(String(id || ''), approve === true) : noEvolve));
  ipcMain.handle('evolve:uninstall', (_e, id) => (evolveHost ? evolveHost.uninstall(String(id || '')) : noEvolve));
  ipcMain.handle('update:check', () => (updater ? updater.check() : { state: 'error', message: 'Updater unavailable' }));
  ipcMain.handle('update:apply', () => (updater ? updater.apply() : { state: 'error', message: 'Updater unavailable' }));
  ipcMain.handle('update:state', () => (updater ? { ...updater.state, installed: updater.installedSha() } : { state: 'idle' }));
  ipcMain.handle('app:state', () => ({
    busy: !!(tasks && tasks.running().length),
    queue: tasks ? tasks.queue.length : 0,
    agentLoaded: !!agent,
    logUnread: actionLog ? actionLog.unread : 0,
    ...taskList(),
    pendingPermissions: [...pendingPerms.values()].map(({ id, toolName, input, summary, taskId }) => ({ id, toolName, input, summary, taskId: taskId || null })),
  }));
}

// ---------------------------------------------------------------- ambient sensing

function startAmbient() {
  if (!ambientMod) return;
  ambient = new ambientMod.AmbientController({
    engineCall: (method, p, body, ms) => engineCall(method, p, body, ms),
    tasks,
    getSettings: () => settings.get(),
    log: logApp,
    showBrief: (b) => {
      if (!briefMod) return;
      if (!briefOverlay) {
        briefOverlay = briefMod.createBriefOverlay({ BrowserWindow, screen, preload: path.join(__dirname, 'preload.js'), file: path.join(RENDERER, 'brief.html') });
      }
      briefOverlay.show(b);
    },
  });
  ambient.on('presence', () => { broadcastAmbient(); rebuildTrayMenu(); syncNotify(); });
  ambient.on('sensors', () => { broadcastAmbient(); rebuildTrayMenu(); });
  ambient.start();
}

// ---------------------------------------------------------------- lifecycle

app.on('second-instance', () => showPanel());

app.whenReady().then(() => {
  settings = new Settings(app.getPath('userData'));
  if (process.platform === 'darwin' && app.dock) app.dock.hide();
  chats = new ChatStore(app.getPath('userData'));
  if (ActionLog) {
    try {
      actionLog = new ActionLog(app.getPath('userData'));
      actionLog.on('entry', (entry) => {
        broadcast({ type: 'log', entry });
        broadcast({ type: 'log-unread', count: actionLog.unread });
        // The engine's circuit breaker (ask mode: denied or 60 s without an answer) destroys the whole queue.
        if (entry && entry.source === 'engine' && entry.detail && entry.detail.queue_destroyed) {
          const dropped = stopTask().dropped;
          sendToIsland({ type: 'status', mood: 'error', text: `Queue destroyed: approval denied or timed out (${dropped} dropped)` });
        }
      });
    } catch (err) { console.error('[o] action log:', err.message); actionLog = null; }
  }
  registerIpc();
  loadAgent();
  startAmbient();
  // The engine POSTs its log entries to the agent's HTTP server; it needs that URL before it starts.
  if (agent && EngineProcess && typeof agent.ensureServer === 'function') {
    agent.ensureServer().then(({ url, uiUrl, token }) => {
      engine = new EngineProcess({
        userDataDir: app.getPath('userData'),
        appDir: path.join(__dirname, '..'),
        getSettings: () => settings.get(),
        logUrl: url,
        logToken: token,
        uiUrl,
      });
      engine.on('log', (e) => { if (actionLog) { try { actionLog.record(e); } catch (_) {} } });
      engine.on('ready', () => {
        broadcast({ type: 'engine', ...engineStatus() });
        if (tasks) tasks.prewarm().catch(() => {});
        if (goals) goals.refresh().catch((err) => console.error('[o] goals refresh:', err.message));
        if (ambient) ambient.refreshSensors().then(broadcastAmbient).catch(() => {});
        if (evolveHost) evolveHost.onEngineReady().catch(() => {});
      });
      engine.on('dead', () => broadcast({ type: 'engine', ...engineStatus() }));
      engine.start();
      startProxy();
    }).catch((err) => console.error('[o] engine not started:', err.message));
  } else {
    startProxy();
  }
  // Easy ways to open O: shortcuts on first run (refreshed if O moved), optional log-in start.
  if (process.platform === 'win32' && !process.env.O_SNAPSHOT_DIR) {
    const first = !settings.get().shortcutsMade;
    if (first || !shortcuts.shortcutsCurrent(app, shell)) {
      const fs = require('fs');
      const desk = path.join(app.getPath('desktop'), 'O AI.lnk');
      shortcuts.createShortcuts(app, shell, { desktop: first || fs.existsSync(desk) });
      settings.set({ shortcutsMade: true });
    }
  }
  if (settings.get().openAtLogin) shortcuts.setOpenAtLogin(app, true);
  link = new LinkServer({ dataDir: app.getPath('userData'), extensionSrc: path.join(__dirname, '..', 'extension') });
  link.start().catch((err) => console.error('[o] O Link:', err.message));
  link.on('status', (st) => {
    broadcast({ type: 'link', ...st });
    if (st.connected) sendToIsland({ type: 'status', mood: 'success', text: 'O Link connected. Your tabs are reachable.' });
  });
  link.on('blocked', (b) => {
    broadcast({ type: 'link-blocked', ...b });
    sendToIsland({ type: 'status', mood: 'error', text: 'Payment blocked. O does not make payments.' });
  });
  let lastAnnounced = null;
  updater = createUpdater({
    appDir: path.join(__dirname, '..'),
    getSettings: () => settings.get(),
    isPackaged: app.isPackaged,
    isBusy: () => !!(tasks && tasks.activeCount()),
    relaunch: () => { quitting = true; app.relaunch(); app.exit(0); },
    onEvent: (ev) => {
      broadcast(ev);
      const evolveInstalls = !!(evolveHost && evolveHost.onUpdate(ev)); // approved upgrades install while you are away
      if (ev.state === 'available' && lastAnnounced !== ev.sha) {
        lastAnnounced = ev.sha;
        if (!evolveInstalls) notify('O update available', 'Open the tray menu → Update O now, or Settings → Updates.');
      }
      rebuildTrayMenu();
    },
  });
  if (createEvolveHost) {
    evolveHost = createEvolveHost({
      getSettings: () => settings.get(), isAway, isBusy: () => !!(tasks && tasks.activeCount()),
      engineCall, getUpdater: () => updater, broadcast, log: logApp,
    });
    evolveHost.start();
  }
  const autoCheck = () => { if (settings.get().autoUpdateCheck) updater.check({ quiet: true }); };
  setTimeout(autoCheck, 20 * 1000);
  setInterval(autoCheck, 6 * 60 * 60 * 1000);
  // Linux transparency bug: create transparent windows a moment after 'ready'.
  setTimeout(() => {
    createIslandWindow();
    createPanelWindow();
    createTray();
    updateBadge();
  }, process.platform === 'linux' ? 300 : 0);
  // Speed: the first task should not wait for Claude Code to start. Warm one process once the app is up
  // (again when the engine is ready, since its URL is part of the config).
  setTimeout(() => { if (tasks) tasks.prewarm().catch(() => {}); }, 2500).unref();
  try {
    globalShortcut.register('CommandOrControl+Shift+O', toggleCommandBar);
  } catch (_) {}
});

// O keeps running with no windows focused; only quit explicitly.
app.on('window-all-closed', (e) => { if (!quitting) e.preventDefault(); });

app.on('before-quit', () => {
  quitting = true;
  try { if (chats) chats.flush(); } catch (_) {}
  try { if (link) link.stop(); } catch (_) {}
  try { if (ambient) ambient.stop(); } catch (_) {}
  try { if (briefOverlay) briefOverlay.destroy(); } catch (_) {}
  try { if (engine) engine.dispose(); } catch (_) {}
  try { if (proxy) proxy.stop(); } catch (_) {}
  for (const p of pendingPerms.values()) p.resolve({ allow: false, message: 'O is quitting' });
  pendingPerms.clear();
  try { if (scheduler) scheduler.stop(); } catch (_) {}
  try { if (evolveHost) evolveHost.stop(); } catch (_) {}
  try { if (tasks) tasks.dispose(); } catch (_) {}
  try { if (agent && typeof agent.dispose === 'function') agent.dispose(); else if (agent) agent.cancel(); } catch (_) {}
});

app.on('will-quit', () => globalShortcut.unregisterAll());

// Dev aid: O_SNAPSHOT_DIR=/tmp/x npm start  -> saves island + panel PNGs a few seconds after launch, then quits.
if (process.env.O_SNAPSHOT_DIR) {
  app.whenReady().then(() => setTimeout(async () => {
    const fs = require('fs');
    const dir = process.env.O_SNAPSHOT_DIR;
    fs.mkdirSync(dir, { recursive: true });
    try {
      showPanel();
      broadcast({ type: 'status', mood: 'browsing', text: 'Peering at a web page…' });
      broadcast({ type: 'assistant', text: 'Looking at **Hacker News** now — top story is about `pixel art`.' });
      broadcast({ type: 'tool', name: 'mcp__playwright__browser_navigate', summary: 'Opening news.ycombinator.com' });
      broadcast({ type: 'permission', id: 'demo', toolName: 'Bash', input: { command: 'du -sh ~/Downloads/*' }, summary: 'Run this command?' });
      await new Promise((r) => setTimeout(r, 1500));
      if (process.env.O_SNAPSHOT_SCREEN) {
        const shot = await captureScreen({});
        fs.writeFileSync(require('path').join(dir, 'screen.jpg'), Buffer.from(shot.data, 'base64'));
        console.log('[o] screen capture', shot.width + 'x' + shot.height, 'cursor', JSON.stringify(shot.cursor));
      }
      if (islandWin && !islandWin.isDestroyed()) {
        const L = currentIslandLayout();
        console.log('[o] island', JSON.stringify({ position: L.position, resolved: L.resolved, mode: L.mode, dir: L.dir, bounds: islandWin.getBounds(), workArea: screen.getPrimaryDisplay().workArea }));
      }
      for (const [name, w] of [['island', islandWin], ['panel', panelWin]]) {
        if (!w) continue;
        const img = await w.webContents.capturePage();
        fs.writeFileSync(require('path').join(dir, name + '.png'), img.toPNG());
      }
    } catch (err) {
      console.error('[o] snapshot failed:', err);
    }
    quitting = true;
    app.quit();
  }, Number(process.env.O_SNAPSHOT_DELAY || 5000)));
}
