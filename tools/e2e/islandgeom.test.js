#!/usr/bin/env node
'use strict';
// Unit tests for renderer/islandgeom.js (shared by main and the Island renderer). No Electron needed:
//   node tools/e2e/islandgeom.test.js      (or: node --test tools/e2e/)
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const G = require(path.join(__dirname, '..', '..', 'renderer', 'islandgeom.js'));

// A 1920x1080 display with the taskbar on the given edge (Windows-style work area).
function winDisplay(taskbar = 'bottom', tb = 48) {
  const bounds = { x: 0, y: 0, width: 1920, height: 1080 };
  const wa = { ...bounds };
  if (taskbar === 'bottom') wa.height -= tb;
  else if (taskbar === 'top') { wa.y += tb; wa.height -= tb; }
  else if (taskbar === 'left') { wa.x += tb; wa.width -= tb; }
  else if (taskbar === 'right') wa.width -= tb;
  return { bounds, workArea: wa, internal: true };
}
const macDisplay = { bounds: { x: 0, y: 0, width: 1440, height: 900 }, workArea: { x: 0, y: 25, width: 1440, height: 875 }, internal: true };

test('resolvePosition: auto is the menu bar on mac, top on Windows, bottom on Linux', () => {
  assert.equal(G.resolvePosition('auto', 'darwin'), 'menubar');
  assert.equal(G.resolvePosition('auto', 'win32'), 'top');
  assert.equal(G.resolvePosition('auto', 'linux'), 'bottom');
  assert.equal(G.resolvePosition('hidden', 'win32'), 'top');
  assert.equal(G.resolvePosition('menubar', 'win32'), 'top');
  assert.equal(G.resolvePosition('menubar', 'linux'), 'top');
  assert.equal(G.resolvePosition('bottom-right', 'win32'), 'bottom-right');
  assert.equal(G.resolvePosition('nonsense', 'linux'), 'bottom');
});

test('win32 auto: a floating capsule centred just below the top of the work area', () => {
  const L = G.layout({ platform: 'win32', display: winDisplay('bottom'), position: 'auto' });
  assert.equal(L.resolved, 'top');
  assert.equal(L.mode, 'float');
  assert.equal(L.dir, 'down');
  assert.equal(L.anchor, 'center');
  assert.equal(L.ih, G.C.BASE_H);
  assert.equal(L.compact.y, G.C.FLOAT_Y);
  assert.equal(L.compact.width, G.C.EXP_W);
  assert.equal(L.compact.height, G.C.EXP_H);
  assert.equal(L.compact.x + L.compact.width / 2, 960);
});

test('win32: the taskbar is respected on every edge (work area, not display bounds)', () => {
  const top = G.layout({ platform: 'win32', display: winDisplay('top', 48), position: 'auto' });
  assert.equal(top.compact.y, 48 + G.C.FLOAT_Y);
  const left = G.layout({ platform: 'win32', display: winDisplay('left', 60), position: 'auto' });
  assert.equal(left.compact.x + left.compact.width / 2, 60 + (1920 - 60) / 2);
  const right = G.layout({ platform: 'win32', display: winDisplay('right', 60), position: 'auto' });
  assert.equal(right.compact.x + right.compact.width / 2, (1920 - 60) / 2);
  const bottom = G.layout({ platform: 'win32', display: winDisplay('bottom', 48), position: 'bottom' });
  assert.equal(bottom.dir, 'up');
  assert.equal(bottom.compact.y + bottom.compact.height, 1080 - 48);
});

test('win32: the capsule rect sits inside the window and clears the window edge', () => {
  const L = G.layout({ platform: 'win32', display: winDisplay(), position: 'auto' });
  const r = G.capsuleRect(L, L.minW);
  assert.equal(r.y, G.C.TOP);
  assert.equal(r.height, G.C.BASE_H);
  assert.ok(r.x >= 0 && r.x + r.width <= L.compact.width);
  assert.equal(G.barDrop(L), 0);
});

test('mac and linux auto are unchanged', () => {
  const mac = G.layout({ platform: 'darwin', display: macDisplay, position: 'auto', forceNotch: false });
  assert.equal(mac.resolved, 'menubar');
  assert.equal(mac.mode, 'menubar');
  assert.equal(mac.compact.y, 0);
  assert.equal(mac.ih, 21);
  const lin = G.layout({ platform: 'linux', display: winDisplay('bottom', 32), position: 'auto' });
  assert.equal(lin.resolved, 'bottom');
  assert.equal(lin.dir, 'up');
  assert.equal(lin.compact.y + lin.compact.height, 1080 - 32);
});

// ---------------------------------------------------------------- renderer/adapt.js: the adaptive rules
const A = require(path.join(__dirname, '..', '..', 'renderer', 'adapt.js'));
const disp = { id: 1, bounds: { x: 0, y: 0, width: 1920, height: 1080 }, workArea: { x: 0, y: 0, width: 1920, height: 1032 } };

test('adapt: fullscreen window → dot at 60% that lets clicks through; near the cursor it wakes the cluster', () => {
  const front = { app: 'Code', title: 'main.js', bounds: { x: 0, y: 0, width: 1920, height: 1080 } };
  const d = A.decideMode({ front, display: disp, cursorDist: 500 });
  assert.equal(d.mode, 'dot');
  assert.equal(d.opacity, A.C.DIM_OPACITY);
  assert.equal(d.clickThrough, true);
  const near = A.decideMode({ front, display: disp, cursorDist: 20 });
  assert.equal(near.mode, 'cluster');
  assert.equal(near.clickThrough, false);
  assert.equal(A.decideMode({ front, display: disp, hover: true }).clickThrough, false);
});

test('adapt: known game/video/presentation apps count as immersive even when windowed', () => {
  assert.ok(A.isImmersiveApp('Keynote'));
  assert.ok(A.isImmersiveApp('VLC'));
  assert.ok(A.isImmersiveApp('Steam'));
  assert.ok(A.isImmersiveApp('Chrome', 'Slide show - Google Slides'));
  assert.ok(!A.isImmersiveApp('Finder'));
  const d = A.decideMode({ front: { app: 'VLC', bounds: { x: 100, y: 100, width: 800, height: 600 } }, display: disp, cursorDist: 999 });
  assert.equal(d.mode, 'dot');
  assert.equal(d.clickThrough, true);
});

test('adapt: state → mode: full/typing/card win; approval → card; near → cluster; task → pill; idle → dot', () => {
  assert.equal(A.decideMode({ fullOpen: true, approvalPending: true }).mode, 'full');
  assert.equal(A.decideMode({ focused: true, approvalPending: true }).mode, 'cluster');
  assert.equal(A.decideMode({ cardOpen: true, taskRunning: true }).mode, 'card');
  assert.equal(A.decideMode({ approvalPending: true, taskRunning: true }).mode, 'card');
  assert.equal(A.decideMode({ cursorDist: 10, taskRunning: true }).mode, 'cluster');
  assert.equal(A.decideMode({ taskRunning: true, cursorDist: 999 }).mode, 'pill');
  assert.equal(A.decideMode({ hasStatus: true, cursorDist: 999 }).mode, 'pill');
  assert.equal(A.decideMode({ cursorDist: 999 }).mode, 'dot');
  assert.equal(A.decideMode({ hover: true }).mode, 'cluster');
});

test('adapt: a user lock holds (cluster stays up, dot stays dot) but an approval still shows the card', () => {
  assert.equal(A.decideMode({ lock: 'dot', taskRunning: true, cursorDist: 0 }).mode, 'dot');
  assert.equal(A.decideMode({ lock: 'cluster', cursorDist: 999 }).mode, 'cluster');
  assert.equal(A.decideMode({ lock: 'pill', cursorDist: 999 }).mode, 'pill');
  assert.equal(A.decideMode({ lock: 'dot', approvalPending: true }).mode, 'card');
  assert.equal(A.decideMode({ lock: 'nonsense', cursorDist: 999 }).mode, 'dot');
});

test('adapt: the cluster hugs the active window\'s top edge, just below its title bar, clamped to the work area', () => {
  const home = { cx: 960, cy: 22 };
  const cap = { width: 346, height: 36 };
  const p = A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: { app: 'Google Chrome', bounds: { x: 200, y: 100, width: 1000, height: 800 } } });
  assert.equal(p.cx, 700);
  assert.equal(p.cy, 100 + A.C.TITLEBAR_H + A.C.HUG_GAP + 18);
  assert.equal(p.reason, 'browser top edge');
  const code = A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: { app: 'Code', bounds: { x: 0, y: 0, width: 1920, height: 900 }, titleBarH: 28 } });
  assert.equal(code.reason, 'active window top edge');
  assert.equal(code.cy, 28 + A.C.HUG_GAP + 18); // never on the title bar
  const edge = A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: { app: 'Safari', bounds: { x: -300, y: 100, width: 500, height: 800 } } });
  assert.ok(edge.cx - cap.width / 2 >= disp.workArea.x);
  // nothing in front, or a fullscreen window: the user's own spot
  assert.equal(A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: null }).cx, 960);
  assert.equal(A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: { app: 'VLC', bounds: disp.bounds } }).reason, 'home');
});

test('adapt: nudge: dwell > 1.5 s on the cluster → a 120-200 px step away from the cursor, at most once per 4 s', () => {
  const wa = disp.workArea;
  const rect = { x: 787, y: 40, width: 346, height: 36 };
  const base = { now: 10000, dwellSince: 8000, lastNudgeAt: 0, rect, workArea: wa };
  assert.equal(A.nudge({ ...base, dwellSince: 9000 }), null, 'not yet dwelt long enough');
  assert.equal(A.nudge({ ...base, dwellSince: 0 }), null, 'not on it');
  const right = A.nudge({ ...base, cursor: { x: 800, y: 50 } }); // cursor on the left half → step right
  assert.ok(right.dx >= A.C.NUDGE_MIN_PX && right.dx <= A.C.NUDGE_MAX_PX && right.dy === 0);
  const left = A.nudge({ ...base, cursor: { x: 1100, y: 50 } });
  assert.ok(left.dx <= -A.C.NUDGE_MIN_PX && left.dx >= -A.C.NUDGE_MAX_PX);
  const centre = A.nudge({ ...base, cursor: { x: 960, y: 50 } });
  assert.equal(Math.abs(centre.dx), A.C.NUDGE_MAX_PX, 'dead centre: the largest step');
  assert.equal(A.nudge({ ...base, lastNudgeAt: 7000, cursor: { x: 800, y: 50 } }), null, 'one move per 4 s');
  assert.ok(A.nudge({ ...base, lastNudgeAt: 5000, cursor: { x: 800, y: 50 } }), 'after 4 s it may move again');
  // against the right edge with the cursor on the left: no room right → it goes left instead
  const pinned = A.nudge({ ...base, rect: { ...rect, x: wa.width - rect.width - 10 }, cursor: { x: wa.width - rect.width, y: 50 } });
  assert.ok(pinned.dx < 0);
  // no room either side: it steps down
  const tight = A.nudge({ ...base, workArea: { x: 0, y: 0, width: 400, height: 800 }, rect: { x: 20, y: 40, width: 346, height: 36 }, cursor: { x: 100, y: 50 } });
  assert.deepEqual(tight, { dx: 0, dy: A.C.NUDGE_MIN_PX });
});

test('adapt: cluster layout: circle + segment centred on cx, chips stacked in an arc, +N beyond 5, card under its chip', () => {
  const lay = A.clusterLayout({ cx: 410, top: 12, ih: 36, winW: 820, typing: false, chips: 3 });
  assert.equal(lay.circle.d, A.C.CIRCLE);
  assert.equal(lay.seg.w, A.C.SEG_W);
  assert.equal(lay.seg.x, lay.circle.cx + A.C.CIRCLE / 2 + A.C.SEG_GAP);
  const clusterW = A.C.CIRCLE + A.C.SEG_GAP + A.C.SEG_W;
  assert.equal(Math.round((lay.circle.cx - A.C.CIRCLE / 2 + lay.seg.x + lay.seg.w) / 2), 410, 'the cluster is centred on cx');
  assert.equal(lay.chips.length, 3);
  assert.equal(lay.chips[0].y, lay.seg.y + A.C.CIRCLE + 12);
  assert.equal(lay.chips[1].y - lay.chips[0].y, A.C.CHIP_H + A.C.CHIP_GAP);
  assert.equal(lay.chips[0].x - lay.chips[1].x, A.C.CHIP_ARC, 'a gentle arc');
  assert.ok(clusterW < 820);
  // typing grows the segment rightwards and keeps it inside the window
  const typing = A.clusterLayout({ cx: 410, top: 12, ih: 36, winW: 820, typing: true, chips: 0 });
  assert.equal(typing.seg.w, A.C.SEG_W_TYPING);
  assert.ok(typing.seg.x + typing.seg.w <= 820);
  // more than 5 chips: 4 + a "+N" chip
  const many = A.clusterLayout({ cx: 410, top: 12, ih: 36, winW: 820, chips: 8 });
  assert.equal(many.chips.length, A.C.MAX_CHIPS);
  assert.equal(many.chips[4].more, true);
  assert.equal(many.chips[4].count, 4);
  // a card under chip 1 pushes chip 2 down by its height
  const card = A.clusterLayout({ cx: 410, top: 12, ih: 36, winW: 820, chips: 3, openCard: { index: 1, height: 140 } });
  assert.ok(card.card && card.card.w === A.C.CARD_W);
  assert.equal(card.card.y, card.chips[1].y + A.C.CHIP_H + A.C.CHIP_GAP + 2);
  assert.equal(card.chips[2].y - card.chips[1].y, A.C.CHIP_H + A.C.CHIP_GAP + 140 + A.C.CARD_GAP + 2);
  assert.ok(card.card.x + card.card.w <= 820);
});

test('adapt: the window is only as tall as the dot/pill need; cluster, card and full take the full footprint', () => {
  const L = G.layout({ platform: 'win32', display: winDisplay('bottom'), position: 'auto' });
  assert.equal(A.modeHeight(L, 'dot'), L.top + L.ih + 16);
  assert.equal(A.modeHeight(L, 'pill'), L.top + L.ih + 16);
  for (const m of ['cluster', 'card', 'full']) assert.equal(A.modeHeight(L, m), L.compact.height);
  const up = G.layout({ platform: 'linux', display: winDisplay('bottom', 32), position: 'auto' });
  assert.equal(A.modeHeight(up, 'pill'), up.gap + up.ih + 16);
  const mac = G.layout({ platform: 'darwin', display: macDisplay, position: 'auto', forceNotch: false });
  assert.ok(A.modeHeight(mac, 'dot') >= mac.menuBarH);
});

test('adapt: cleanMode / cleanEdge accept only the known words', () => {
  assert.equal(A.cleanMode('cluster'), 'cluster');
  assert.equal(A.cleanMode('sheet'), null);
  assert.equal(A.cleanEdge('left'), 'left');
  assert.equal(A.cleanEdge('middle'), null);
});
