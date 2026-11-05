'use strict';
// Fling physics for the pet window: gravity, bouncy walls/floor/ceiling, floor friction.
// Runs in the main process so the window moves smoothly (setBounds at ~60 Hz) even while
// the renderer is busy animating. The renderer only reports the release velocity.

const GRAVITY = 2600;        // px/s²
const RESTITUTION = 0.62;    // bounciness (velocity kept after a bounce)
const WALL_RESTITUTION = 0.7;
const AIR_DRAG = 0.15;       // fraction of speed lost per second in the air
const FLOOR_FRICTION = 6;    // 1/s, slows sliding along the floor
const MAX_SPEED = 6000;      // px/s, clamp silly flings
const REST_SPEED = 40;       // below this on the floor, O stops
const TICK_MS = 16;

/**
 * @param {object} o
 * @param {() => Electron.BrowserWindow|null} o.getWindow
 * @param {(bounds) => {x,y,width,height}} o.getWorkArea   work area of the display under the pet
 * @param {(ev: object) => void} o.onEvent                 physics events for the renderer
 * @param {{left:number,right:number,top:number}} [o.slack] how far the transparent window may poke past edges
 */
function createPhysics({ getWindow, getWorkArea, onEvent, slack = { left: 50, right: 50, top: 60 } }) {
  let timer = null;
  let st = null; // { x, y, vx, vy, last, bounces, airborne }

  function stop(reason) {
    if (!timer) return;
    clearInterval(timer);
    timer = null;
    const bounces = st ? st.bounces : 0;
    st = null;
    onEvent({ type: 'physics', event: 'rest', bounces, reason: reason || 'rest' });
  }

  function fling(vx, vy) {
    const win = getWindow();
    if (!win || win.isDestroyed()) return;
    const b = win.getBounds();
    const clamp = (v) => Math.max(-MAX_SPEED, Math.min(MAX_SPEED, Number(v) || 0));
    if (timer) clearInterval(timer);
    st = { x: b.x, y: b.y, vx: clamp(vx), vy: clamp(vy), last: Date.now(), bounces: 0, airborne: true };
    onEvent({ type: 'physics', event: 'launch', speed: Math.round(Math.hypot(st.vx, st.vy)) });
    timer = setInterval(step, TICK_MS);
  }

  function step() {
    const win = getWindow();
    if (!win || win.isDestroyed() || !st) return stop('gone');
    const now = Date.now();
    const dt = Math.min(0.05, (now - st.last) / 1000);
    st.last = now;
    const b = win.getBounds();
    const wa = getWorkArea({ x: st.x, y: st.y, width: b.width, height: b.height });
    const minX = wa.x - slack.left;
    const maxX = wa.x + wa.width - b.width + slack.right;
    const minY = wa.y - slack.top;
    const floorY = wa.y + wa.height - b.height;

    const onFloor = st.y >= floorY - 0.5 && Math.abs(st.vy) < 1;
    if (!onFloor) {
      st.vy += GRAVITY * dt;
      const drag = Math.max(0, 1 - AIR_DRAG * dt);
      st.vx *= drag;
    } else {
      st.vx *= Math.max(0, 1 - FLOOR_FRICTION * dt);
    }
    st.x += st.vx * dt;
    st.y += st.vy * dt;

    // Walls
    if (st.x < minX || st.x > maxX) {
      st.x = st.x < minX ? minX : maxX;
      const hit = Math.abs(st.vx);
      st.vx = -st.vx * WALL_RESTITUTION;
      if (hit > 150) { st.bounces++; onEvent({ type: 'physics', event: 'bounce', side: 'wall', strength: hit }); }
    }
    // Ceiling
    if (st.y < minY) {
      st.y = minY;
      const hit = Math.abs(st.vy);
      st.vy = Math.abs(st.vy) * RESTITUTION;
      if (hit > 150) { st.bounces++; onEvent({ type: 'physics', event: 'bounce', side: 'ceiling', strength: hit }); }
    }
    // Floor
    if (st.y >= floorY) {
      st.y = floorY;
      const hit = st.vy;
      if (hit > 220) {
        st.vy = -hit * RESTITUTION;
        st.vx *= 0.9;
        if (hit > 900) st.bounces++; // only hard bounces count toward "dizzy"
        onEvent({ type: 'physics', event: 'bounce', side: 'floor', strength: hit });
      } else {
        if (st.airborne && hit > 0) onEvent({ type: 'physics', event: 'bounce', side: 'floor', strength: hit });
        st.vy = 0;
        st.airborne = false;
      }
    } else {
      st.airborne = true;
    }

    win.setBounds({ x: Math.round(st.x), y: Math.round(st.y), width: b.width, height: b.height });
    if (!st.airborne && Math.abs(st.vx) < REST_SPEED) stop('rest');
  }

  return {
    fling,
    stop,
    get active() { return !!timer; },
  };
}

module.exports = { createPhysics, GRAVITY };
