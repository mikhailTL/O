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

test('adapt: fullscreen window → dot at 60% that lets clicks through, except on hover / near the cursor', () => {
  const front = { app: 'Code', title: 'main.js', bounds: { x: 0, y: 0, width: 1920, height: 1080 } };
  const d = A.decideMode({ front, display: disp, cursorDist: 500 });
  assert.equal(d.mode, 'dot');
  assert.equal(d.opacity, A.C.DIM_OPACITY);
  assert.equal(d.clickThrough, true);
  const near = A.decideMode({ front, display: disp, cursorDist: 20 });
  assert.equal(near.mode, 'pill');
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

test('adapt: state → mode: approval → card, task → pill, idle → dot, cursor near → pill, bar/sheet win', () => {
  assert.equal(A.decideMode({ approvalPending: true, taskRunning: true }).mode, 'card');
  assert.equal(A.decideMode({ taskRunning: true }).mode, 'pill');
  assert.equal(A.decideMode({ cursorDist: 999 }).mode, 'dot');
  assert.equal(A.decideMode({ cursorDist: 10 }).mode, 'pill');
  assert.equal(A.decideMode({ hasStatus: true }).mode, 'pill');
  assert.equal(A.decideMode({ barOpen: true, approvalPending: true }).mode, 'bar');
  assert.equal(A.decideMode({ sheetOpen: true, taskRunning: true }).mode, 'sheet');
});

test('adapt: a user lock holds (dot stays dot while a task runs) but an approval still shows the card', () => {
  assert.equal(A.decideMode({ lock: 'dot', taskRunning: true, cursorDist: 0 }).mode, 'dot');
  assert.equal(A.decideMode({ lock: 'pill', cursorDist: 999 }).mode, 'pill');
  assert.equal(A.decideMode({ lock: 'dot', approvalPending: true }).mode, 'card');
  assert.equal(A.decideMode({ lock: 'nonsense', cursorDist: 999 }).mode, 'dot');
});

test('adapt: a browser in front → the capsule hugs its top centre, clamped to the work area', () => {
  const home = { cx: 960, cy: 22 };
  const cap = { width: 220, height: 36 };
  const p = A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: { app: 'Google Chrome', bounds: { x: 200, y: 100, width: 1000, height: 800 } } });
  assert.equal(p.cx, 700);
  assert.equal(p.cy, 100 + 4 + 18);
  assert.equal(p.reason, 'browser top-centre');
  const edge = A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: { app: 'Safari', bounds: { x: -300, y: 100, width: 500, height: 800 } } });
  assert.ok(edge.cx - cap.width / 2 >= disp.workArea.x);
});

test('adapt: another window whose title bar the capsule would cover → slide to the nearest free edge', () => {
  const home = { cx: 960, cy: 22 };
  const cap = { width: 220, height: 36 };
  // a window across the whole top: no room left or right → below its title bar
  const below = A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: { app: 'Code', bounds: { x: 0, y: 0, width: 1920, height: 900 } } });
  assert.equal(below.reason, 'slid below the title bar');
  assert.ok(below.cy > 36);
  // a window on the right half: the capsule slides left of it
  const left = A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: { app: 'Code', bounds: { x: 900, y: 0, width: 1020, height: 900 } } });
  assert.equal(left.reason, 'slid left of the title bar');
  assert.ok(left.cx + cap.width / 2 <= 900);
  // a window that does not reach the capsule: the home spot stays
  const stay = A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: { app: 'Code', bounds: { x: 0, y: 300, width: 800, height: 600 } } });
  assert.equal(stay.reason, 'home');
  assert.equal(stay.cx, 960);
  // nothing in front: home
  assert.equal(A.placeIsland({ home, capsule: cap, workArea: disp.workArea, display: disp, front: null }).cx, 960);
});

test('adapt: the sheet grows out of the capsule, 380-560 wide, at most 62% of the work area tall', () => {
  const wa = disp.workArea;
  const g = A.sheetGeometry({ anchor: { cx: 960, cy: 22, height: 36 }, workArea: wa, want: { width: 440, height: 9999 }, dock: 'none' });
  assert.equal(g.width, 440);
  assert.equal(g.height, Math.floor(wa.height * 0.62));
  assert.equal(g.x, 960 - 220);
  assert.equal(g.y, 22 + 18 + A.C.SHEET_GAP);
  assert.equal(g.anchored, true);
  assert.equal(g.dir, 'down');
  assert.equal(g.originX, 220);
  assert.equal(A.sheetGeometry({ anchor: { cx: 960, cy: 22, height: 36 }, workArea: wa, want: { width: 100 } }).width, 380);
  assert.equal(A.sheetGeometry({ anchor: { cx: 960, cy: 22, height: 36 }, workArea: wa, want: { width: 900 } }).width, 560);
  // a bottom capsule: the sheet grows upward
  const up = A.sheetGeometry({ anchor: { cx: 960, cy: 1010, height: 36 }, workArea: wa, want: { width: 440 } });
  assert.equal(up.dir, 'up');
  assert.ok(up.y + up.height <= 1010 - 18);
  // a saved spot wins over the anchor and is kept on screen
  const saved = A.sheetGeometry({ anchor: { cx: 960, cy: 22, height: 36 }, workArea: wa, want: { width: 440 }, saved: { x: 5000, y: -50 } });
  assert.equal(saved.anchored, false);
  assert.ok(saved.x + saved.width <= wa.x + wa.width && saved.y >= wa.y);
});

test('adapt: docked left/right is a slim full-height column; under 420 px the tabs collapse (narrow)', () => {
  const wa = disp.workArea;
  const l = A.sheetGeometry({ workArea: wa, dock: 'left', want: { width: 440 } });
  assert.equal(l.x, wa.x + A.C.SHEET_MARGIN);
  assert.equal(l.width, A.C.DOCK_W);
  assert.equal(l.height, wa.height - 2 * A.C.SHEET_MARGIN);
  assert.equal(l.narrow, true);
  const r = A.sheetGeometry({ workArea: wa, dock: 'right', want: { width: 440 } });
  assert.equal(r.x + r.width, wa.x + wa.width - A.C.SHEET_MARGIN);
  assert.equal(A.sheetGeometry({ workArea: wa, dock: 'none', want: { width: 440 } }).narrow, false);
});

test('adapt: a dragged sheet snaps to edges within 24 px and docks when dropped against a side at full height', () => {
  const wa = disp.workArea;
  const s = A.snapToEdges({ x: 20, y: 300, width: 440, height: 600 }, wa);
  assert.equal(s.x, A.C.SHEET_MARGIN);
  assert.equal(s.y, 300);
  assert.equal(s.snapped, true);
  const far = A.snapToEdges({ x: 400, y: 300, width: 440, height: 600 }, wa);
  assert.equal(far.snapped, false);
  assert.equal(A.dockFor({ x: 12, y: 12, width: 380, height: wa.height - 24 }, wa), 'left');
  assert.equal(A.dockFor({ x: wa.width - 12 - 380, y: 12, width: 380, height: wa.height - 24 }, wa), 'right');
  assert.equal(A.dockFor({ x: 12, y: 300, width: 380, height: 500 }, wa), 'none');
});

test('adapt: the window is only as tall as the mode needs; bar and sheet take the full footprint', () => {
  const L = G.layout({ platform: 'win32', display: winDisplay('bottom'), position: 'auto' });
  const dot = A.modeHeight(L, 'dot');
  const card = A.modeHeight(L, 'card');
  assert.ok(dot < card && card < A.modeHeight(L, 'bar'));
  assert.equal(A.modeHeight(L, 'bar'), L.compact.height);
  assert.equal(A.modeHeight(L, 'sheet'), L.compact.height);
  assert.equal(dot, L.top + L.ih + 16);
  const up = G.layout({ platform: 'linux', display: winDisplay('bottom', 32), position: 'auto' });
  assert.equal(A.modeHeight(up, 'pill'), up.gap + up.ih + 16);
  const mac = G.layout({ platform: 'darwin', display: macDisplay, position: 'auto', forceNotch: false });
  assert.ok(A.modeHeight(mac, 'dot') >= mac.menuBarH);
});

test('adapt: cleanMode / cleanEdge accept only the known words', () => {
  assert.equal(A.cleanMode('card'), 'card');
  assert.equal(A.cleanMode('huge'), null);
  assert.equal(A.cleanEdge('left'), 'left');
  assert.equal(A.cleanEdge('middle'), null);
});
