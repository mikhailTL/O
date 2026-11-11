'use strict';
// O: the adaptive rules for the Island cluster. Pure functions, no Electron, no DOM: the main process
// (main/main.js) feeds them what it knows every ~1.2 s (the frontmost window, the cursor, the task state) and
// applies what comes back; tools/e2e/islandgeom.test.js checks them.
//
// Modes of the one morphing glass element:
//   dot      the 22 px disc with the ring            (fullscreen apps, games, video, presentations; idle rest)
//   pill     ring + one status line, ~36 px          (a task is running and the cursor is elsewhere)
//   cluster  the O circle + one short input segment, task chips around it (awake: the cursor came near, typing)
//   card     cluster + one result / approval card anchored to a chip (an approval waits; a chip was opened)
//   full     the whole dashboard as a fullscreen glass layer (the user asked for it)
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.OAdapt = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const MODES = Object.freeze(['dot', 'pill', 'cluster', 'card', 'full']);
  const LOCKS = Object.freeze(['auto', 'dot', 'pill', 'cluster']);
  const EDGES = Object.freeze(['top', 'bottom', 'left', 'right', 'auto']);
  const C = Object.freeze({
    NEAR_PX: 40,          // the cursor this close to the capsule wakes the cluster
    DIM_OPACITY: 0.6,     // the dot over a fullscreen app
    TITLEBAR_H: 36,       // what a title bar occupies when the OS gives no better figure
    HUG_GAP: 6,           // the cluster hangs this far below the active window's title bar
    // the cluster
    CIRCLE: 36,           // the O circle
    SEG_W: 300,           // the input segment at rest
    SEG_W_TYPING: 420,    // ... while typing
    SEG_GAP: 10,          // circle -> segment
    CHIP_H: 30,
    CHIP_GAP: 6,
    CHIP_W: 190,          // a chip at most (icon + 28 chars + dot)
    CHIP_ARC: 8,          // each chip steps this far left of the one above: a gentle arc around the circle
    MAX_CHIPS: 5,         // older ones collapse into a "+N" chip
    CARD_W: 420,
    CARD_GAP: 8,
    // "actively moving": the cluster steps aside when the cursor dwells on it without clicking
    NUDGE_DWELL_MS: 1500,
    NUDGE_MIN_PX: 120,
    NUDGE_MAX_PX: 200,
    NUDGE_EVERY_MS: 4000,
    // the spring every move uses (Motion-style)
    SPRING: Object.freeze({ stiffness: 260, damping: 26, mass: 1 }),
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
   * Which mode the Island should be in. ctx: { lock ('auto'|'dot'|'pill'|'cluster'), focused (the input has
   * keyboard focus), fullOpen, cardOpen (the user opened a chip's card), approvalPending, taskRunning,
   * hasStatus, cursorDist (px from the cluster), hover, front: { app, title, bounds, fullscreen? }, display }.
   * Returns { mode, opacity, clickThrough, reason }.
   */
  function decideMode(ctx) {
    const c = ctx || {};
    const front = c.front || null;
    const out = (mode, reason, extra) => Object.assign({ mode, opacity: 1, clickThrough: false, reason }, extra || {});
    // What the user did on purpose always wins.
    if (c.fullOpen) return out('full', 'dashboard open');
    if (c.focused) return out('cluster', 'typing');
    if (c.cardOpen) return out('card', 'card open');
    const lock = LOCKS.includes(c.lock) ? c.lock : 'auto';
    const immersive = !!front && (front.fullscreen === true || isFullscreen(front.bounds, c.display) || isImmersiveApp(front.app, front.title));
    const near = c.hover || (typeof c.cursorDist === 'number' && c.cursorDist < C.NEAR_PX);
    if (lock !== 'auto') {
      // A locked mode holds, but an approval still gets through as a card (it waits for the person).
      if (c.approvalPending) return out('card', 'approval pending (locked ' + lock + ')');
      return out(lock, 'locked by the user', lock === 'dot' && immersive ? { opacity: C.DIM_OPACITY, clickThrough: !c.hover } : {});
    }
    if (c.approvalPending) return out('card', 'approval pending');
    if (immersive) {
      // Over a fullscreen app, a game or a presentation: a 60% dot that lets clicks through unless hovered.
      if (near) return out('cluster', 'fullscreen, cursor near', { opacity: 1, clickThrough: false });
      return out('dot', 'fullscreen or immersive app', { opacity: C.DIM_OPACITY, clickThrough: true });
    }
    if (near) return out('cluster', 'cursor near');
    // Work in flight keeps the cluster up: the chips are where the user watches a task and reads its result.
    if (c.taskRunning || c.hasChips) return out('cluster', c.taskRunning ? 'task running' : 'results to show');
    if (c.hasStatus) return out('pill', 'status to show');
    return out('dot', 'idle');
  }

  /**
   * Where the capsule's centre should be (screen px). ctx: { home: {cx, cy} the user's chosen spot (the
   * layout's capsule centre), capsule: {width, height}, workArea, front: {app, bounds, titleBarH?}, display }.
   * The cluster hugs the active window's top edge, just below its title bar (so the title bar is never
   * covered), centred on that window; without one it keeps the user's spot. Returns { cx, cy, reason }.
   */
  function placeIsland(ctx) {
    const c = ctx || {};
    const wa = c.workArea;
    const cap = c.capsule || { width: 220, height: 36 };
    const home = c.home || { cx: wa ? wa.x + wa.width / 2 : 0, cy: wa ? wa.y + 4 + cap.height / 2 : 0 };
    const front = c.front || null;
    let cx = home.cx, cy = home.cy, reason = 'home';
    if (front && front.bounds && front.bounds.width > 0 && !isFullscreen(front.bounds, c.display)) {
      const b = front.bounds;
      const tbH = Number(front.titleBarH) > 0 ? Number(front.titleBarH) : C.TITLEBAR_H;
      cx = b.x + b.width / 2;
      cy = b.y + tbH + C.HUG_GAP + cap.height / 2;
      reason = isBrowser(front.app) ? 'browser top edge' : 'active window top edge';
    }
    if (wa) {
      cx = clamp(cx, wa.x + cap.width / 2 + 4, wa.x + wa.width - cap.width / 2 - 4);
      cy = clamp(cy, wa.y + cap.height / 2 + 2, wa.y + wa.height - cap.height / 2 - 2);
    }
    return { cx: r(cx), cy: r(cy), reason };
  }

  /**
   * "Actively moving": when the cursor has dwelt on the cluster for NUDGE_DWELL_MS without a click, it steps
   * 120-200 px aside (away from the cursor, along the edge it hugs), at most once per NUDGE_EVERY_MS.
   * ctx: { now, dwellSince (0 = not on it), lastNudgeAt, cursor: {x,y}, rect: {x,y,width,height} (the cluster,
   * screen px), workArea }. Returns { dx, dy } or null.
   */
  function nudge(ctx) {
    const c = ctx || {};
    if (!c.dwellSince || !c.rect || !c.workArea) return null;
    if (c.now - c.dwellSince < C.NUDGE_DWELL_MS) return null;
    if (c.lastNudgeAt && c.now - c.lastNudgeAt < C.NUDGE_EVERY_MS) return null;
    const wa = c.workArea;
    const cx = c.rect.x + c.rect.width / 2;
    const cur = c.cursor || { x: cx, y: c.rect.y };
    // Distance scales with how deep in the cluster the cursor sits; direction is away from the cursor.
    const depth = clamp(Math.abs(cur.x - cx) / Math.max(1, c.rect.width / 2), 0, 1);
    const px = r(C.NUDGE_MAX_PX - (C.NUDGE_MAX_PX - C.NUDGE_MIN_PX) * depth);
    let dir = cur.x >= cx ? -1 : 1;
    const roomL = c.rect.x - wa.x, roomR = wa.x + wa.width - (c.rect.x + c.rect.width);
    if (dir < 0 && roomL < px) dir = roomR >= px ? 1 : 0;
    else if (dir > 0 && roomR < px) dir = roomL >= px ? -1 : 0;
    if (dir === 0) {
      // No room sideways: step down (or up, at the bottom of the screen).
      const roomD = wa.y + wa.height - (c.rect.y + c.rect.height);
      const dy = roomD >= C.NUDGE_MIN_PX ? C.NUDGE_MIN_PX : -C.NUDGE_MIN_PX;
      return { dx: 0, dy };
    }
    return { dx: dir * px, dy: 0 };
  }

  /**
   * The cluster's layout inside the Island window (window px). ctx: { cx, top, ih (the capsule's centre x and
   * top, as islandgeom lays the capsule out), winW, typing, chips: n (how many chips there are), openCard:
   * { index, height } (a card under chip index), drop (menu bar: how far the cluster hangs below the strip) }.
   * Returns { circle: {cx, cy, d}, seg: {x, y, w, h}, chips: [{x, y, w, h, more}], card: {x, y, w}|null,
   * cdx (how far left of cx the circle moved so the cluster is centred on cx) }.
   */
  function clusterLayout(ctx) {
    const c = ctx || {};
    const ih = c.ih || C.CIRCLE;
    const top = (c.top || 0) + (c.drop || 0);
    const segW = c.typing ? C.SEG_W_TYPING : C.SEG_W;
    const total = C.CIRCLE + C.SEG_GAP + C.SEG_W; // the cluster at rest is centred on cx; typing grows rightward
    let cdx = -r(total / 2 - C.CIRCLE / 2);
    const winW = c.winW || 820;
    // keep the typing segment inside the window
    const right = c.cx + cdx + C.CIRCLE / 2 + C.SEG_GAP + segW + 8;
    if (right > winW) cdx -= right - winW;
    const circleCx = c.cx + cdx;
    const circle = { cx: circleCx, cy: top + ih / 2, d: C.CIRCLE };
    const seg = { x: r(circleCx + C.CIRCLE / 2 + C.SEG_GAP), y: top + r((ih - C.CIRCLE) / 2), w: segW, h: C.CIRCLE };
    const n = Math.max(0, Number(c.chips) || 0);
    const shown = n > C.MAX_CHIPS ? C.MAX_CHIPS - 1 : n;
    const chips = [];
    // Bottom-anchored layouts (dir 'up') stack the chips above the segment, so they stay inside the window.
    const up = c.dir === 'up';
    let y = seg.y + C.CIRCLE + 12;
    let card = null;
    for (let i = 0; i < shown + (n > C.MAX_CHIPS ? 1 : 0); i++) {
      const more = n > C.MAX_CHIPS && i === shown;
      chips.push({ x: seg.x - i * C.CHIP_ARC, y, w: more ? 56 : C.CHIP_W, h: C.CHIP_H, more, count: more ? n - shown : 0 });
      y += C.CHIP_H + C.CHIP_GAP;
      if (c.openCard && c.openCard.index === i && !more) {
        card = { x: Math.max(4, Math.min(winW - C.CARD_W - 4, seg.x - i * C.CHIP_ARC)), y: y + 2, w: C.CARD_W };
        y += (Number(c.openCard.height) || 120) + C.CARD_GAP + 2;
      }
    }
    if (c.openCard && !card && c.openCard.index === -1) card = { x: Math.max(4, Math.min(winW - C.CARD_W - 4, seg.x)), y: seg.y + C.CIRCLE + 12, w: C.CARD_W };
    if (up) {
      // mirror everything below the segment to above it
      const base = seg.y - 12;
      const flip = (o, h) => { o.y = base - (o.y - (seg.y + C.CIRCLE + 12)) - h; };
      for (const ch of chips) flip(ch, ch.h);
      if (card) flip(card, Number(c.openCard && c.openCard.height) || 120);
    }
    return { circle, seg, chips, card, cdx, bottom: y, up };
  }

  /** Normalise what a tool or a menu asks for. */
  function cleanMode(m) { return MODES.includes(m) ? m : null; }
  function cleanEdge(e) { return EDGES.includes(e) ? e : null; }

  /**
   * The Island window height a mode needs (the window is top- or bottom-anchored, so only height changes).
   * L is the islandgeom layout. The cluster, card and full modes keep the whole footprint (chips and the card
   * float in it; the window itself is transparent, so nothing shows).
   */
  function modeHeight(L, mode) {
    const strip = (L.mode === 'menubar' || L.mode === 'notch') ? L.menuBarH : 0;
    const full = L.compact.height;
    if (mode === 'cluster' || mode === 'card' || mode === 'full') return full;
    // dot / pill: the capsule strip plus a little room for its glow
    if (L.dir === 'up') return Math.min(full, L.gap + L.ih + 16);
    return Math.min(full, Math.max(strip, L.top + L.ih + 16));
  }

  return Object.freeze({ C, MODES, LOCKS, EDGES, isFullscreen, isImmersiveApp, isBrowser, decideMode, placeIsland, nudge, clusterLayout, cleanMode, cleanEdge, modeHeight });
});
