'use strict';
/*
 * OLiquid: the liquid-glass goo and bud helper shared by O's surfaces (spec: docs/plans/2026-11-07-ui-glass.md).
 *
 * How the effect is built
 *   Two layers. A "goo" layer holds plain opaque blobs and is filtered by an SVG goo filter
 *   (blur 9 -> alpha threshold 22a-9 -> composite atop), so blobs that come close grow a liquid neck and
 *   blobs that move apart pinch off. The glass tint comes from the layer's opacity (0.58), applied after
 *   the filter, so overlapping blobs never double up. Text, controls and the crisp glass rim (hairline,
 *   top highlight, sheen) sit on an unfiltered layer above. A bubble "buds" by translating away from its
 *   parent blob; its rim and content fade in once it has mostly separated.
 *
 * Markup (liquid.css styles these classes)
 *   <div class="ol-goo">                     the filtered layer (position it yourself; pointer-events: none)
 *     <div class="ol-blob"></div>            any number of blobs; size/position/radius them yourself
 *   </div>
 *   <div class="ol-glass">...</div>          rim + sheen for a surface on the unfiltered layer
 *
 * API (window.OLiquid)
 *   OLiquid.install(doc?)          inject the SVG filter once (id "ol-goo"); called automatically on load.
 *   OLiquid.reduced()              true when prefers-reduced-motion is set (then everything is a crossfade).
 *   OLiquid.bud(targets, opts)     bud out: targets (element or array, e.g. [blob, bubble]) travel from an
 *                                  offset to their resting place. opts: { dx=0, dy=0 (start offset, px),
 *                                  delay=0, duration=320, scale=0.94 (start scale), fade=[elements whose
 *                                  opacity goes 0 -> 1 after they separate] }. Returns a Promise.
 *   OLiquid.retract(targets, opts) the reverse: travel back into the parent; same opts. Returns a Promise.
 *   OLiquid.spring(el, {x,y,s,o}, opts)  Motion-style spring (stiffness 260, damping 26) on translate/scale/opacity;
 *                                  interruptible: a new target mid-flight keeps the velocity. Returns a Promise.
 *   OLiquid.stagger(list, fn, step=50)  calls fn(item, i, delay) with delay = i * step (40-60 ms reads well).
 *   OLiquid.EASE_OUT / EASE_IO     the shared curves; OLiquid.MORPH_MS = 240.
 *
 * Only transform and opacity are animated. No overshoot.
 */
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  const EASE_OUT = 'cubic-bezier(0.23,1,0.32,1)';
  const EASE_IO = 'cubic-bezier(0.77,0,0.175,1)';
  const MORPH_MS = 240;
  const FILTER_ID = 'ol-goo';

  function install(doc) {
    doc = doc || document;
    if (doc.getElementById(FILTER_ID)) return FILTER_ID;
    const svg = doc.createElementNS(NS, 'svg');
    svg.setAttribute('width', '0');
    svg.setAttribute('height', '0');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.setAttribute('class', 'ol-defs');
    const defs = doc.createElementNS(NS, 'defs');
    const f = doc.createElementNS(NS, 'filter');
    f.setAttribute('id', FILTER_ID);
    f.setAttribute('x', '-20%');
    f.setAttribute('y', '-20%');
    f.setAttribute('width', '140%');
    f.setAttribute('height', '140%');
    f.setAttribute('color-interpolation-filters', 'sRGB');
    const blur = doc.createElementNS(NS, 'feGaussianBlur');
    blur.setAttribute('in', 'SourceGraphic');
    blur.setAttribute('stdDeviation', '9');
    blur.setAttribute('result', 'blur');
    const cm = doc.createElementNS(NS, 'feColorMatrix');
    cm.setAttribute('in', 'blur');
    cm.setAttribute('mode', 'matrix');
    cm.setAttribute('values', '1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 22 -9');
    cm.setAttribute('result', 'goo');
    const comp = doc.createElementNS(NS, 'feComposite');
    comp.setAttribute('in', 'SourceGraphic');
    comp.setAttribute('in2', 'goo');
    comp.setAttribute('operator', 'atop');
    f.append(blur, cm, comp);
    defs.appendChild(f);
    svg.appendChild(defs);
    (doc.body || doc.documentElement).appendChild(svg);
    return FILTER_ID;
  }

  function reduced() {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) { return false; }
  }

  const list = (t) => (Array.isArray(t) ? t : [t]).filter(Boolean);

  function run(targets, opts, out) {
    const o = Object.assign({ dx: 0, dy: 0, delay: 0, duration: MORPH_MS, scale: 0.94, fade: [] }, opts || {});
    const away = `translate3d(${o.dx}px, ${o.dy}px, 0) scale(${o.scale})`;
    const home = 'translate3d(0, 0, 0) scale(1)';
    const anims = [];
    const fades = list(o.fade);
    if (reduced()) {
      // Crossfade only: nothing travels.
      for (const el of list(targets).concat(fades)) {
        anims.push(el.animate([{ opacity: out ? 1 : 0 }, { opacity: out ? 0 : 1 }], { duration: 180, delay: o.delay, easing: 'linear', fill: 'both' }));
      }
      return Promise.all(anims.map((a) => a.finished.catch(() => {})));
    }
    for (const el of list(targets)) {
      anims.push(el.animate(out ? [{ transform: home }, { transform: away }] : [{ transform: away }, { transform: home }],
        { duration: o.duration, delay: o.delay, easing: out ? EASE_IO : EASE_OUT, fill: 'both' }));
    }
    for (const el of fades) {
      // In: the rim and content appear once the bubble has mostly pinched off. Out: they go first.
      anims.push(el.animate(out ? [{ opacity: 1 }, { opacity: 0, offset: 0.35 }, { opacity: 0 }] : [{ opacity: 0 }, { opacity: 0, offset: 0.45 }, { opacity: 1 }],
        { duration: o.duration, delay: o.delay, easing: 'linear', fill: 'both' }));
    }
    return Promise.all(anims.map((a) => a.finished.catch(() => {})));
  }

  function bud(targets, opts) { return run(targets, opts, false); }
  function retract(targets, opts) { return run(targets, opts, true); }

  // ---- spring: Motion-style (stiffness 260, damping 26, mass 1), transform/opacity only, interruptible.
  // spring(el, { x, y, s, o }, opts) moves el's translate/scale/opacity to the target from wherever it is now,
  // keeping its velocity, so a new target mid-flight just bends the path. One rAF loop drives every spring.
  // Reduced motion: the target is set at once (opacity still eases through CSS if the element transitions it).
  const S = new Map(); // el -> { x, y, s, o, vx, vy, vs, vo, tx, ty, ts, to, k, d, m, done }
  let loop = 0;
  function spring(el, target, opts) {
    if (!el) return Promise.resolve();
    const o = Object.assign({ stiffness: 260, damping: 26, mass: 1, from: null }, opts || {});
    let st = S.get(el);
    if (!st) {
      const f = o.from || {};
      st = { x: f.x || 0, y: f.y || 0, s: f.s == null ? 1 : f.s, o: f.o == null ? 1 : f.o, vx: 0, vy: 0, vs: 0, vo: 0 };
      S.set(el, st);
    } else if (o.from) Object.assign(st, { x: o.from.x == null ? st.x : o.from.x, y: o.from.y == null ? st.y : o.from.y, s: o.from.s == null ? st.s : o.from.s, o: o.from.o == null ? st.o : o.from.o });
    st.tx = target.x == null ? st.x : target.x;
    st.ty = target.y == null ? st.y : target.y;
    st.ts = target.s == null ? st.s : target.s;
    st.to = target.o == null ? st.o : target.o;
    st.k = o.stiffness; st.d = o.damping; st.m = o.mass;
    st.settled = false;
    if (st.resolve) st.resolve();
    const p = new Promise((res) => { st.resolve = res; });
    if (reduced()) { st.x = st.tx; st.y = st.ty; st.s = st.ts; st.o = st.to; paint(el, st); settle(el, st); return p; }
    if (!loop) { last = 0; loop = requestAnimationFrame(tick); }
    return p;
  }
  function paint(el, st) {
    el.style.transform = `translate3d(${st.x.toFixed(2)}px, ${st.y.toFixed(2)}px, 0) scale(${st.s.toFixed(4)})`;
    el.style.opacity = String(Math.max(0, Math.min(1, st.o)));
  }
  function settle(el, st) { st.settled = true; const r = st.resolve; st.resolve = null; if (r) r(); if (st.onSettle) st.onSettle(); }
  let last = 0;
  function tick(now) {
    const dt = Math.min(0.064, last ? (now - last) / 1000 : 1 / 60);
    last = now;
    let active = 0;
    for (const [el, st] of S) {
      if (st.settled) continue;
      if (!el.isConnected) { S.delete(el); continue; }
      let moving = false;
      for (const [k, v, t] of [['x', 'vx', 'tx'], ['y', 'vy', 'ty'], ['s', 'vs', 'ts'], ['o', 'vo', 'to']]) {
        const dx = st[k] - st[t];
        const a = (-st.k * dx - st.d * st[v]) / st.m;
        st[v] += a * dt;
        st[k] += st[v] * dt;
        const eps = k === 's' || k === 'o' ? 0.002 : 0.15;
        if (Math.abs(st[v]) > eps * 4 || Math.abs(st[k] - st[t]) > eps) moving = true;
        else { st[k] = st[t]; st[v] = 0; }
      }
      paint(el, st);
      if (!moving) settle(el, st); else active++;
    }
    loop = active ? requestAnimationFrame(tick) : 0;
  }
  function springState(el) { const st = S.get(el); return st ? { x: st.x, y: st.y, s: st.s, o: st.o } : null; }
  function springForget(el) { S.delete(el); }

  function stagger(items, fn, step) {
    step = step == null ? 50 : step;
    return Array.prototype.map.call(items || [], (item, i) => fn(item, i, i * step));
  }

  window.OLiquid = Object.freeze({ install, reduced, bud, retract, spring, springState, springForget, stagger, EASE_OUT, EASE_IO, MORPH_MS, FILTER_ID });
  if (document.body) install(); else document.addEventListener('DOMContentLoaded', () => install(), { once: true });
})();
