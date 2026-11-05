'use strict';
// The welcome-back brief overlay (plan J): a frameless, transparent, always-on-top window at the top centre
// of the screen the person is looking at. It never takes focus and lets every click through
// (setIgnoreMouseEvents(true, {forward: true}) so the page still sees the pointer pass over it).
// The renderer (renderer/brief.js) fades it out after ~12 s or on the second hover, then calls done().
//
//   const overlay = createBriefOverlay({ BrowserWindow, screen, preload, file });
//   overlay.show({ text, sentences, milestones });

const BRIEF_W = 600;
const BRIEF_H = 190;     // first guess; the renderer reports its real height
const TOP_GAP = 18;
const SAFETY_HIDE_MS = 20000; // the renderer fades at ~12 s; main hides regardless after this

function briefWindowOptions(preload, bounds) {
  return {
    width: bounds.width, height: bounds.height, x: bounds.x, y: bounds.y,
    frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false,
    resizable: false, movable: false, minimizable: false, maximizable: false, fullscreenable: false,
    focusable: false, skipTaskbar: true, alwaysOnTop: true, show: false, title: 'O brief',
    webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false },
  };
}

/** Top centre of the display under the cursor (work area, so it clears a top menu bar). */
function briefBounds(screen, height = BRIEF_H) {
  let wa;
  try { wa = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea; } catch (_) { wa = screen.getPrimaryDisplay().workArea; }
  const width = Math.min(BRIEF_W, wa.width - 24);
  return { x: Math.round(wa.x + (wa.width - width) / 2), y: wa.y + TOP_GAP, width, height: Math.max(80, Math.min(Math.round(height), 420)) };
}

function createBriefOverlay({ BrowserWindow, screen, preload, file, safetyMs = SAFETY_HIDE_MS }) {
  let win = null;
  let loaded = false;
  let pending = null;
  let safety = null;
  let current = null;

  const alive = () => !!(win && !win.isDestroyed());

  function ensure() {
    if (alive()) return win;
    loaded = false;
    win = new BrowserWindow(briefWindowOptions(preload, briefBounds(screen)));
    try { win.setAlwaysOnTop(true, 'screen-saver'); } catch (_) {}
    try { win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true }); } catch (_) {}
    win.setIgnoreMouseEvents(true, { forward: true });
    try { win.setFocusable(false); } catch (_) {}
    win.webContents.on('did-finish-load', () => {
      loaded = true;
      if (pending) { const b = pending; pending = null; deliver(b); }
    });
    win.on('closed', () => { win = null; loaded = false; });
    win.loadFile(file);
    return win;
  }

  function deliver(brief) {
    if (!alive()) return;
    win.setBounds(briefBounds(screen, win.getBounds().height));
    win.webContents.send('brief:show', brief);
    if (!win.isVisible()) win.showInactive(); // never focus: the person is busy with something else
    clearTimeout(safety);
    safety = setTimeout(hide, safetyMs);
    if (safety.unref) safety.unref();
  }

  function show(brief) {
    current = { text: String(brief && brief.text || ''), sentences: Array.isArray(brief && brief.sentences) ? brief.sentences.slice(0, 3).map(String) : [], milestones: Number(brief && brief.milestones) || 0, speak: !!(brief && brief.speak) };
    ensure();
    if (loaded) deliver(current); else pending = current;
    return current;
  }

  function hide() {
    clearTimeout(safety); safety = null;
    if (alive() && win.isVisible()) win.hide();
  }

  function resize(h) {
    if (!alive()) return;
    win.setBounds(briefBounds(screen, Number(h) || BRIEF_H));
  }

  function destroy() {
    clearTimeout(safety);
    if (alive()) win.destroy();
    win = null;
  }

  return { show, hide, resize, destroy, get window() { return win; }, get current() { return current; } };
}

module.exports = { createBriefOverlay, briefWindowOptions, briefBounds, BRIEF_W };
