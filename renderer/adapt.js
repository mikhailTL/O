'use strict';
// O: the adaptive rules for the Island cluster. Pure functions, no Electron, no DOM: the main process
// (main/main.js) feeds them what it knows (the user's lock, the hover, the task state, what waits for the
// person) on every change and applies what comes back; tools/e2e/islandgeom.test.js checks them.
// There is no frontmost-window / fullscreen rule: polling the OS for the active window made the Island jump,
// so the cluster keeps one spot and one shape unless the user, a task or O itself (ui_morph) changes it.
//
// Modes of the one morphing glass element:
//   dot      the 22 px disc with the ring            (idle rest)
//   pill     ring + one status line, ~36 px          (a status worth a line, nothing running)
//   cluster  the O circle + one short input segment, task chips around it (hover, typing, work in flight)
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
    // the spring every move uses (Motion-style)
    SPRING: Object.freeze({ stiffness: 260, damping: 26, mass: 1 }),
  });

  const r = Math.round;

  /**
   * Which mode the Island should be in. ctx: { lock ('auto'|'dot'|'pill'|'cluster'), focused (the input has
   * keyboard focus), fullOpen, cardOpen (the user opened a chip's card), approvalPending, taskRunning,
   * hasChips, hasStatus, cursorDist (px from the cluster), hover }.
   * Returns { mode, opacity, clickThrough, reason } (opacity is always 1 and clickThrough false: the renderer's
   * own hit test decides what passes through; the fields stay so callers need not special-case them).
   */
  function decideMode(ctx) {
    const c = ctx || {};
    const out = (mode, reason) => ({ mode, opacity: 1, clickThrough: false, reason });
    // What the user did on purpose always wins.
    if (c.fullOpen) return out('full', 'dashboard open');
    if (c.focused) return out('cluster', 'typing');
    if (c.cardOpen) return out('card', 'card open');
    const lock = LOCKS.includes(c.lock) ? c.lock : 'auto';
    const near = c.hover || (typeof c.cursorDist === 'number' && c.cursorDist < C.NEAR_PX);
    if (lock !== 'auto') {
      // A locked mode holds, but an approval still gets through as a card (it waits for the person).
      if (c.approvalPending) return out('card', 'approval pending (locked ' + lock + ')');
      return out(lock, 'locked by the user');
    }
    if (c.approvalPending) return out('card', 'approval pending');
    if (near) return out('cluster', 'cursor near');
    // Work in flight keeps the cluster up: the chips are where the user watches a task and reads its result.
    if (c.taskRunning || c.hasChips) return out('cluster', c.taskRunning ? 'task running' : 'results to show');
    if (c.hasStatus) return out('pill', 'status to show');
    return out('dot', 'idle');
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

  return Object.freeze({ C, MODES, LOCKS, EDGES, decideMode, clusterLayout, cleanMode, cleanEdge, modeHeight });
});
