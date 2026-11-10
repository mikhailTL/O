'use strict';
// O: the adaptive rules for the Island and the sheet. Pure functions, no Electron, no DOM: the main process
// (main/main.js) feeds them what it knows every ~1.2 s (the frontmost window, the cursor, the task state) and
// applies what comes back; tools/e2e/islandgeom.test.js checks them.
//
// Modes of the one morphing glass element:
//   dot    the 14 px ring only                     (fullscreen apps, games, video, presentations; idle rest)
//   pill   ring + one status line, ~36 px          (a task is running, a flash, the cursor comes near)
//   bar    the command bar                         (the user opened it: shortcut or click)
//   card   bar + one result / approval card        (an approval is pending)
//   sheet  the full panel grows out of the island  (the user asked for the panel, or O did)
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.OAdapt = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const MODES = Object.freeze(['dot', 'pill', 'bar', 'card', 'sheet']);
  const LOCKS = Object.freeze(['auto', 'dot', 'pill']);
  const DOCKS = Object.freeze(['none', 'left', 'right']);
  const EDGES = Object.freeze(['top', 'bottom', 'left', 'right', 'auto']);
  const C = Object.freeze({
    NEAR_PX: 40,          // the cursor this close to the capsule expands dot -> pill
    DIM_OPACITY: 0.6,     // the dot over a fullscreen app
    SHEET_MIN_W: 380,
    SHEET_MAX_W: 560,
    SHEET_MAX_FRAC: 0.62, // of the work-area height
    SHEET_GAP: 10,        // between the capsule and the sheet
    SHEET_MARGIN: 12,     // from the work-area edges
    SNAP_PX: 24,          // a dragged sheet this close to an edge snaps to it
    NARROW_W: 420,        // below this the sheet's tabs collapse to icons
    DOCK_W: 380,
    TITLEBAR_H: 36,       // what a title bar occupies when the OS gives no better figure
  });

  // Apps whose frontmost window means "leave the screen alone" even when not literally fullscreen.
  const IMMERSIVE_RE = /\b(keynote|powerpoint|google slides|slides|zoom|teams|webex|vlc|iina|quicktime|netflix|plex|infuse|steam|epic games|minecraft|roblox|fortnite|valorant|league of legends|obs|final cut|premiere|davinci|resolve|kodi|mpv|movies & tv|films & tv|xbox|battle\.net|dota|counter-strike|cs2|apex)\b/i;
  const BROWSER_RE = /\b(safari|chrome|chromium|edge|msedge|firefox|brave|arc|vivaldi|opera|orion|zen)\b/i;

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const r = Math.round;

  /** True when a window covers its display (the usual fullscreen test: bounds == display bounds, 2 px slack). */
  function isFullscreen(bounds, display) {
    if (!bounds || !display) return false;
    const d = display.bounds || display;
    const near = (a, b) => Math.abs((a || 0) - (b || 0)) <= 2;
    return near(bounds.x, d.x) && near(bounds.y, d.y) && near(bounds.width, d.width) && near(bounds.height, d.height);
  }

  function isImmersiveApp(name, title) {
    return IMMERSIVE_RE.test(String(name || '')) || /\b(slide show|presenting|full ?screen)\b/i.test(String(title || ''));
  }

  function isBrowser(name) { return BROWSER_RE.test(String(name || '')); }

  /**
   * Which mode the Island should be in. ctx: { lock ('auto'|'dot'|'pill'), barOpen, sheetOpen, approvalPending,
   * taskRunning, hasStatus (a line worth showing: a flash, queued, goals), cursorDist (px from the capsule),
   * hover (the cursor is on it), front: { app, title, bounds, fullscreen? }, display }.
   * Returns { mode, opacity, clickThrough, reason }.
   */
  function decideMode(ctx) {
    const c = ctx || {};
    const front = c.front || null;
    const out = (mode, reason, extra) => Object.assign({ mode, opacity: 1, clickThrough: false, reason }, extra || {});
    // What the user did on purpose always wins.
    if (c.barOpen) return out('bar', 'command bar open');
    if (c.sheetOpen) return out('sheet', 'sheet open');
    const lock = LOCKS.includes(c.lock) ? c.lock : 'auto';
    const immersive = !!front && (front.fullscreen === true || isFullscreen(front.bounds, c.display) || isImmersiveApp(front.app, front.title));
    if (lock !== 'auto') {
      // A locked mode holds, but an approval still gets through as a card (it waits for the person).
      if (c.approvalPending) return out('card', 'approval pending (locked ' + lock + ')');
      return out(lock, 'locked by the user', lock === 'dot' && immersive ? { opacity: C.DIM_OPACITY, clickThrough: !c.hover } : {});
    }
    if (c.approvalPending) return out('card', 'approval pending');
    if (immersive) {
      // Over a fullscreen app, a game or a presentation: a 60% dot that lets clicks through unless hovered.
      if (c.hover || (typeof c.cursorDist === 'number' && c.cursorDist < C.NEAR_PX)) return out('pill', 'fullscreen, cursor near', { opacity: 1, clickThrough: false });
      return out('dot', 'fullscreen or immersive app', { opacity: C.DIM_OPACITY, clickThrough: true });
    }
    if (c.taskRunning) return out('pill', 'task running');
    if (c.hover || (typeof c.cursorDist === 'number' && c.cursorDist < C.NEAR_PX)) return out('pill', 'cursor near');
    if (c.hasStatus) return out('pill', 'status to show');
    return out('dot', 'idle');
  }

  /**
   * Where the capsule's centre should be (screen px). ctx: { home: {cx, cy} the user's chosen spot (the
   * layout's capsule centre), capsule: {width, height}, workArea, front: {app, bounds, titleBarH?},
   * dock ('none'|'left'|'right'), lock }.
   * Returns { cx, cy, reason }.
   */
  function placeIsland(ctx) {
    const c = ctx || {};
    const wa = c.workArea;
    const cap = c.capsule || { width: 220, height: 36 };
    const home = c.home || { cx: wa ? wa.x + wa.width / 2 : 0, cy: wa ? wa.y + 4 + cap.height / 2 : 0 };
    const front = c.front || null;
    let cx = home.cx, cy = home.cy, reason = 'home';
    if (front && front.bounds && isBrowser(front.app) && !isFullscreen(front.bounds, c.display)) {
      // A browser in front: hug the top centre of that window (just inside its title bar, so the tabs stay
      // reachable left and right of the capsule).
      const b = front.bounds;
      cx = b.x + b.width / 2;
      cy = b.y + 4 + cap.height / 2;
      reason = 'browser top-centre';
    } else if (front && front.bounds && !isFullscreen(front.bounds, c.display)) {
      // Any other window: never sit on its title bar. If the home spot would cover it, slide to the nearest free
      // edge of that title bar (left or right of the window, whichever is closer and fits), else just below it.
      const b = front.bounds;
      const tbH = Number(front.titleBarH) > 0 ? Number(front.titleBarH) : C.TITLEBAR_H;
      const capRect = { x: home.cx - cap.width / 2, y: home.cy - cap.height / 2, w: cap.width, h: cap.height };
      const titleRect = { x: b.x, y: b.y, w: b.width, h: tbH };
      const overlaps = capRect.x < titleRect.x + titleRect.w && capRect.x + capRect.w > titleRect.x && capRect.y < titleRect.y + titleRect.h && capRect.y + capRect.h > titleRect.y;
      if (overlaps && wa) {
        const leftCx = b.x - 8 - cap.width / 2;
        const rightCx = b.x + b.width + 8 + cap.width / 2;
        const fitsLeft = leftCx - cap.width / 2 >= wa.x;
        const fitsRight = rightCx + cap.width / 2 <= wa.x + wa.width;
        const dl = Math.abs(leftCx - home.cx), dr = Math.abs(rightCx - home.cx);
        if ((fitsLeft && (!fitsRight || dl <= dr))) { cx = leftCx; reason = 'slid left of the title bar'; }
        else if (fitsRight) { cx = rightCx; reason = 'slid right of the title bar'; }
        else { cy = b.y + tbH + 4 + cap.height / 2; reason = 'slid below the title bar'; }
      }
    }
    if (wa) {
      cx = clamp(cx, wa.x + cap.width / 2 + 4, wa.x + wa.width - cap.width / 2 - 4);
      cy = clamp(cy, wa.y + cap.height / 2 + 2, wa.y + wa.height - cap.height / 2 - 2);
    }
    return { cx: r(cx), cy: r(cy), reason };
  }

  /**
   * The sheet's bounds. ctx: { anchor: {cx, cy, height} the capsule's centre and height, workArea, want: {width,
   * height} what the content asks for, dock ('none'|'left'|'right'), saved: {x, y, width, height}|null (what the
   * user dragged it to on this display) }.
   */
  function sheetGeometry(ctx) {
    const c = ctx || {};
    const wa = c.workArea || { x: 0, y: 0, width: 1440, height: 900 };
    const want = c.want || {};
    const dock = DOCKS.includes(c.dock) ? c.dock : 'none';
    const maxH = Math.floor(wa.height * C.SHEET_MAX_FRAC);
    const minH = Math.min(380, maxH);
    const M = C.SHEET_MARGIN;
    if (dock !== 'none') {
      // A slim column on the left or right edge, the full work-area height less the margins.
      const width = clamp(r(Number(want.width) || C.DOCK_W), C.SHEET_MIN_W, C.DOCK_W);
      const height = wa.height - 2 * M;
      const x = dock === 'left' ? wa.x + M : wa.x + wa.width - M - width;
      return { x, y: wa.y + M, width, height, dock, narrow: width < C.NARROW_W, anchored: false };
    }
    const width = clamp(r(Number(want.width) || 440), C.SHEET_MIN_W, Math.min(C.SHEET_MAX_W, wa.width - 2 * M));
    const height = clamp(r(Number(want.height) || maxH), minH, maxH);
    if (c.saved && Number.isFinite(c.saved.x) && Number.isFinite(c.saved.y)) {
      const x = clamp(r(c.saved.x), wa.x, wa.x + wa.width - width);
      const y = clamp(r(c.saved.y), wa.y, wa.y + wa.height - height);
      return { x, y, width, height, dock, narrow: width < C.NARROW_W, anchored: false };
    }
    const a = c.anchor || { cx: wa.x + wa.width / 2, cy: wa.y + 22, height: 36 };
    let x = r(a.cx - width / 2);
    x = clamp(x, wa.x + M, wa.x + wa.width - M - width);
    // Below the capsule when it is in the top half, above it otherwise (bottom positions).
    let y = r(a.cy + (a.height || 36) / 2 + C.SHEET_GAP);
    let dir = 'down';
    if (a.cy > wa.y + wa.height / 2) { y = r(a.cy - (a.height || 36) / 2 - C.SHEET_GAP - height); dir = 'up'; }
    y = clamp(y, wa.y + M, wa.y + wa.height - M - height);
    return { x, y, width, height, dock, narrow: width < C.NARROW_W, anchored: true, dir, originX: clamp(a.cx - x, 0, width) };
  }

  /** Snap a dragged rect to a work-area edge it came within SNAP_PX of. Returns the (possibly moved) rect. */
  function snapToEdges(rect, wa, snap) {
    const s = Number.isFinite(snap) ? snap : C.SNAP_PX;
    const M = C.SHEET_MARGIN;
    let { x, y } = rect;
    const right = wa.x + wa.width - rect.width, bottom = wa.y + wa.height - rect.height;
    if (Math.abs(x - (wa.x + M)) <= s || x < wa.x + M) x = wa.x + M;
    else if (Math.abs(x - (right - M)) <= s || x > right - M) x = right - M;
    if (Math.abs(y - (wa.y + M)) <= s || y < wa.y + M) y = wa.y + M;
    else if (Math.abs(y - (bottom - M)) <= s || y > bottom - M) y = bottom - M;
    return { ...rect, x: r(x), y: r(y), snapped: x !== rect.x || y !== rect.y };
  }

  /** Which dock a drop at rect implies: fully against the left or right edge → that column, else none. */
  function dockFor(rect, wa) {
    const M = C.SHEET_MARGIN;
    if (rect.x <= wa.x + M && rect.height >= wa.height - 2 * M - 2) return 'left';
    if (rect.x + rect.width >= wa.x + wa.width - M && rect.height >= wa.height - 2 * M - 2) return 'right';
    return 'none';
  }

  /** Normalise what a tool or a menu asks for. */
  function cleanMode(m) { return MODES.includes(m) ? m : null; }
  function cleanEdge(e) { return EDGES.includes(e) ? e : null; }

  /**
   * The Island window height a mode needs (the window is top- or bottom-anchored, so only height changes).
   * L is the islandgeom layout; dims: { barH, bubH, firstGap }.
   */
  function modeHeight(L, mode, dims) {
    const d = Object.assign({ barH: 56, bubH: 48, firstGap: 18 }, dims || {});
    const strip = (L.mode === 'menubar' || L.mode === 'notch') ? L.menuBarH : 0;
    const full = L.compact.height;
    if (mode === 'bar' || mode === 'sheet') return full;
    if (mode === 'card') {
      const drop = strip ? Math.max(0, L.menuBarH - L.top) : 0;
      return Math.min(full, L.top + drop + d.barH + d.firstGap + d.bubH + 28 + (L.dir === 'up' ? L.gap : 0));
    }
    // dot / pill: the capsule strip plus a little room for its glow
    if (L.dir === 'up') return Math.min(full, L.gap + L.ih + 16);
    return Math.min(full, Math.max(strip, L.top + L.ih + 16));
  }

  return Object.freeze({ C, MODES, LOCKS, DOCKS, EDGES, isFullscreen, isImmersiveApp, isBrowser, decideMode, placeIsland, sheetGeometry, snapToEdges, dockFor, cleanMode, cleanEdge, modeHeight });
});
