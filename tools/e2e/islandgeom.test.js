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
