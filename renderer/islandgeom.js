'use strict';
// O: where the Island sits. Shared by the main process (window bounds, proximity fade) and the Island
// renderer (capsule size, anchor), so both always agree on the same numbers.
//
// Positions (setting islandPosition): 'auto' (the menu bar on macOS, bottom centre elsewhere), 'menubar',
// 'top', 'bottom', the four corners, 'hidden'. Modes:
//   menubar  macOS: the compact window sits inside the menu bar strip at the top centre; the capsule is
//            (menu bar height - 4) tall and capped in width so it stays clear of the app menus (left) and
//            the status items (right).
//   notch    macOS with a notch: the capsule is centred on the notch, ring on the left wing, text on the right.
//   float    every other position: the classic 36 px capsule in the work area (top, bottom, corners).
// dir 'down' = the command bar buds down from the capsule; 'up' = it buds upward (bottom positions).
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.OIslandGeom = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const C = Object.freeze({
    COMPACT_W: 448,   // compact window width (at least; wider when the capsule cap allows more)
    COMPACT_H: 60,    // compact window height, top positions
    EXP_W: 820,       // expanded window (command bar + bubbles)
    EXP_H: 480,
    BASE_H: 36,       // the capsule height everywhere but the menu bar
    TOP: 12,          // capsule top inside the window, top positions
    FLOAT_Y: 4,       // window top below the work area top, top positions
    BOTTOM_GAP: 8,    // capsule bottom above the work area bottom, bottom positions
    PAD: 4,           // corner positions: capsule edge inside the window edge
    EDGE: 8,          // corner positions: capsule edge from the work area edge
    MENU_SAFE: 560,   // notch: symmetric space kept free at each end of the bar (the notch is centred)
    // Non-notch menu bar: the app menus (left) and the status items (right) can't be measured from Electron
    // and vary by frontmost app, so a generous fixed zone is reserved at each end and the capsule lives in the
    // gap between them. The left zone is larger because app menus (File Edit View … plus the app name) run wide.
    MENU_SAFE_LEFT: 640,
    MENU_SAFE_RIGHT: 560,
    MENU_MARGIN: 16,  // the capsule keeps at least this clear of each zone
    MAX_W: 420,
    MIN_W: 220,
    NOTCH_W: 185,     // typical notch width in points (MacBook Pro 14/16, MacBook Air 13/15)
    MAX_WING: 160,
    RING_ONLY_BELOW: 120, // below this width (or a wing below RING_ONLY_WING) only the ring shows
    RING_ONLY_WING: 52,
    FADE_NEAR: 40,    // proximity fade: px around the capsule
    FADE_OPACITY: 0.25,
    DWELL_MS: 350,
  });
  const POSITIONS = Object.freeze(['auto', 'menubar', 'top', 'bottom', 'top-left', 'top-right', 'bottom-left', 'bottom-right', 'hidden']);

  const r = Math.round;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function menuBarHeight(display) {
    if (!display || !display.bounds || !display.workArea) return 0;
    return Math.max(0, display.workArea.y - display.bounds.y);
  }

  // Electron 33 exposes no notch or safe-area inset, so: the built-in panel, a tall menu bar (the notch makes
  // it 32-38 pt instead of 24), and the aspect ratio of the notched panels (1512x982, 1728x1117, 1470x956,
  // 1710x1112 and their scaled modes all sit at 1.53-1.55; the 16:10 panels without one are 1.60).
  function hasNotch(display, platform, force) {
    if (force === true || force === false) return force;
    if (platform !== 'darwin' || !display || !display.bounds) return false;
    if (display.internal !== true) return false;
    if (menuBarHeight(display) < 32) return false;
    const ratio = display.bounds.width / Math.max(1, display.bounds.height);
    return ratio >= 1.52 && ratio <= 1.56;
  }

  function resolvePosition(pref, platform) {
    const p = POSITIONS.includes(pref) ? pref : 'auto';
    if (p === 'auto' || p === 'hidden') return platform === 'darwin' ? 'menubar' : 'bottom';
    if (p === 'menubar' && platform !== 'darwin') return 'top';
    return p;
  }

  /**
   * The Island layout for a display. opts: { platform, display, position, notchWidth, forceNotch }.
   * Returns the compact window bounds and everything the renderer needs to draw the capsule in it.
   */
  function layout(opts) {
    const o = opts || {};
    const platform = o.platform || 'linux';
    const display = o.display;
    const bounds = display.bounds;
    const wa = display.workArea;
    const pref = POSITIONS.includes(o.position) ? o.position : 'auto';
    let pos = resolvePosition(pref, platform);
    const L = {
      position: pref, resolved: pos, hidden: pref === 'hidden', platform,
      mode: 'float', anchor: 'center', dir: 'down', fallback: null,
      ih: C.BASE_H, top: C.TOP, gap: 0, menuBarH: menuBarHeight(display),
      maxW: Math.min(C.MAX_W, wa.width - 2 * C.EDGE), minW: C.MIN_W, ringOnly: false,
      notchW: 0, maxWing: 0, compact: null, exp: { width: C.EXP_W, height: C.EXP_H },
    };
    const safeLeft = Math.max(0, r(Number(o.menuSafeLeft) || C.MENU_SAFE_LEFT));
    const safeRight = Math.max(0, r(Number(o.menuSafeRight) || C.MENU_SAFE_RIGHT));

    if (pos === 'menubar') {
      // A hidden menu bar (auto-hide) reports no height: use the nominal strip it slides into.
      const mb = L.menuBarH >= 18 ? L.menuBarH : 24;
      L.menuBarH = mb;
      L.ih = Math.max(16, mb - 4);
      L.top = Math.floor((mb - L.ih) / 2);
      const notch = hasNotch(display, platform, o.forceNotch);
      const ringW = r(L.ih * 2);
      if (notch) {
        // The notch is centred, so the capsule is centred on the screen with symmetric wings.
        L.mode = 'notch';
        L.notchW = clamp(r(Number(o.notchWidth) || C.NOTCH_W), 80, 400);
        const room = bounds.width - 2 * C.MENU_SAFE;
        L.maxWing = Math.max(0, Math.min(C.MAX_WING, Math.floor((room - L.notchW) / 2)));
        L.maxW = L.notchW + 2 * Math.max(L.maxWing, C.RING_ONLY_WING);
        L.minW = L.notchW + 2 * C.RING_ONLY_WING;
        L.ringOnly = L.maxWing < C.RING_ONLY_WING;
        const w = Math.max(C.COMPACT_W, L.maxW + 16);
        L.compact = { x: r(bounds.x + (bounds.width - w) / 2), y: bounds.y, width: w, height: mb };
        return L;
      }
      // Non-notch: the capsule lives in the gap between the left (app menus) and right (status items) zones,
      // centred in that gap. Its full width must fit the gap with a margin on both sides, else the ring alone
      // (tiny, in the empty centre of the gap); if even the ring can't clear, drop to the bottom float.
      const gapW = bounds.width - safeLeft - safeRight;
      const gapCenter = safeLeft + gapW / 2;
      const availW = gapW - 2 * C.MENU_MARGIN; // the widest the capsule may be and still keep both margins
      if (availW >= C.RING_ONLY_BELOW) {
        L.mode = 'menubar';
        L.maxW = Math.min(C.MAX_W, availW);
        L.minW = Math.min(L.maxW, r(C.MIN_W * clamp(L.ih / C.BASE_H, 0.6, 1)));
      } else if (availW >= ringW) {
        L.mode = 'menubar';
        L.ringOnly = true;
        L.maxW = ringW;
        L.minW = ringW;
      } else {
        // No room for even the ring between the menus: fall back to the bottom-centre float.
        L.fallback = 'menubar-no-room';
        pos = 'bottom';
        L.resolved = 'bottom';
        L.ih = C.BASE_H; L.top = C.TOP;
      }
      if (L.mode === 'menubar') {
        const w = Math.max(C.COMPACT_W, L.maxW + 16);
        L.compact = { x: r(gapCenter - w / 2), y: bounds.y, width: w, height: mb };
        return L;
      }
      // fall through to the float layout with pos = 'bottom'
    }

    // Floating positions, inside the work area.
    L.maxW = Math.min(C.MAX_W, wa.width - 2 * C.EDGE);
    L.ringOnly = false;
    L.minW = Math.min(L.minW, L.maxW);
    const w = C.COMPACT_W;
    const bottom = pos === 'bottom' || pos === 'bottom-left' || pos === 'bottom-right';
    L.anchor = /-left$/.test(pos) ? 'left' : /-right$/.test(pos) ? 'right' : 'center';
    let x;
    if (L.anchor === 'left') x = wa.x + C.EDGE - C.PAD;
    else if (L.anchor === 'right') x = wa.x + wa.width - C.EDGE + C.PAD - w;
    else x = r(wa.x + (wa.width - w) / 2);
    if (bottom) {
      L.dir = 'up';
      L.gap = C.BOTTOM_GAP;
      const h = C.TOP + L.ih + C.BOTTOM_GAP;
      L.top = C.TOP;
      L.compact = { x, y: wa.y + wa.height - h, width: w, height: h };
    } else {
      L.compact = { x, y: wa.y + C.FLOAT_Y, width: w, height: C.COMPACT_H };
    }
    return L;
  }

  /** The expanded (command bar) window: same top edge (down) or bottom edge (up), same anchor edge. */
  function expandedBounds(L) {
    const c = L.compact;
    let x;
    if (L.anchor === 'left') x = c.x;
    else if (L.anchor === 'right') x = c.x + c.width - C.EXP_W;
    else x = r(c.x + c.width / 2 - C.EXP_W / 2);
    const y = L.dir === 'up' ? c.y + c.height - C.EXP_H : c.y;
    return { x, y, width: C.EXP_W, height: C.EXP_H };
  }

  /** Capsule centre x inside a window of width winW (the anchor edge is shared by both windows). */
  function capsuleCx(L, w, winW) {
    if (L.anchor === 'left') return C.PAD + w / 2;
    if (L.anchor === 'right') return winW - C.PAD - w / 2;
    return winW / 2;
  }

  /** The capsule rect in compact-window px. */
  function capsuleRect(L, w) {
    const cx = capsuleCx(L, w, L.compact.width);
    const y = L.dir === 'up' ? L.compact.height - L.gap - L.ih : L.top;
    return { x: r(cx - w / 2), y, width: r(w), height: L.ih };
  }

  /** Distance in px from a point to a rect (0 inside). */
  function distanceToRect(p, rect) {
    const dx = Math.max(rect.x - p.x, 0, p.x - (rect.x + rect.width));
    const dy = Math.max(rect.y - p.y, 0, p.y - (rect.y + rect.height));
    return Math.hypot(dx, dy);
  }

  /** Type and glyph sizes for a capsule height (type never below 12 px). */
  function sizes(ih) {
    const s = ih / C.BASE_H;
    return {
      fs: Math.max(12, Math.round(13 * s * 10) / 10),
      ring: clamp(Math.round(ih * 0.45), 10, 14),
      gap: Math.max(6, Math.round(9 * s)),
      pad: Math.max(20, Math.round(32 * s)),
    };
  }

  return Object.freeze({ C, POSITIONS, menuBarHeight, hasNotch, resolvePosition, layout, expandedBounds, capsuleCx, capsuleRect, distanceToRect, sizes });
});
