/* O AI — the "alive" behaviour engine.
 *
 * window.OBehavior.start({ canvas, sprite, bridge }) ->
 *   { setMood(m), getMood(), react(kind), say(text, ms), hitTest(x, y),
 *     setWanderPaused(bool), stop() }
 *
 * moods: idle | listening | thinking | working | browsing | success | error | sleeping
 * react: click | pet | drag | drop | prompt
 *
 * Design notes (from the team's research): blinks every 2-6s with a ~12% double
 * blink, 1px micro-jitter at irregular 80-250ms intervals, a fidget every few
 * seconds picked by weighted random with a no-repeat cooldown, saccadic gaze
 * (eyes jump, the body follows a beat later), squash & stretch on hops,
 * follow-through on the ring's lag and chain channels (the hatX/monocleX names
 * are kept because sprite.js maps them), wandering only along the bottom of the screen,
 * and sleep after being ignored. Capped at ~30fps.
 */
(function () {
  'use strict';

  var FRAME_MS = 33;
  var MOODS = ['idle', 'listening', 'thinking', 'working', 'browsing', 'success', 'error', 'sleeping'];

  function rand(a, b) { return a + Math.random() * (b - a); }
  function irand(a, b) { return Math.floor(rand(a, b + 1)); }
  function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function lerp(a, b, k) { return a + (b - a) * k; }
  function ease(k) { return k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2; }
  function bell(k) { return Math.sin(Math.PI * clamp(k, 0, 1)); }

  // Weighted pick that avoids the last few choices.
  function weightedPick(weights, recent) {
    var keys = Object.keys(weights), total = 0, w = {};
    keys.forEach(function (k) {
      var v = weights[k];
      if (recent.indexOf(k) !== -1) v *= 0.12;
      w[k] = v; total += v;
    });
    var r = Math.random() * total;
    for (var i = 0; i < keys.length; i++) { r -= w[keys[i]]; if (r <= 0) return keys[i]; }
    return keys[keys.length - 1];
  }

  // O's voice: calm, short, present. No costume jokes.
  var LINES = {
    click: ['Yes?', 'Here.', 'Hm?', 'Listening.', 'Go on.', 'Still here.', 'Gently, I\'m glass.'],
    pet: ['Thank you.', 'That\'s nice.', 'Mm.', 'Noted.'],
    prompt: ['Ready.', 'What\'s the plan?', 'Ring\'s lit.', 'Say the word.', 'Go ahead.'],
    wake: ['I\'m up.', 'Hm? Here.', 'Awake.'],
    drop: ['Landed.', 'Steady.', 'There.'],
    startle: ['!', 'Oh.', 'Hm!'],
    success: ['Done.', 'All set.', 'Finished.', 'That worked.'],
    error: ['That didn\'t work.', 'Hm, something broke.', 'Not this time.'],
    idle: ['…', 'Quiet today.', 'Waiting.', 'Here if you need me.', 'Hm.']
  };

  function start(opts) {
    opts = opts || {};
    var canvas = opts.canvas || document.getElementById('pet-canvas');
    var sprite = opts.sprite || window.OSprite.create(canvas, {});
    var bridge = opts.bridge || null;
    var bubbleEl = document.getElementById('bubble');
    var bubbleText = document.getElementById('bubble-text') || bubbleEl; // text lives in the clamped inner span

    var now = performance.now();
    var S = {
      mood: 'idle', moodSince: now, autoSleep: false,
      actions: [],
      recent: [],
      nextFidget: now + rand(1500, 3500),
      nextBlink: now + rand(1500, 4000), blinkUntil: 0, blinkQueue: 0,
      nextJitter: now + rand(80, 250), jx: 0, jy: 0, twitchUntil: 0, tx: 0, ty: 0,
      nextIdleChatter: now + rand(40000, 90000),
      nextStartle: now + rand(60000, 140000),
      gazeX: 0, gazeY: 0, gazeAt: 0, lookOverride: null,
      cursor: null, cursorPrev: null, lastCursorMove: now, lastInteraction: now,
      bounds: null, workArea: null,
      walk: null, nextWander: now + rand(8000, 16000), wanderPaused: false,
      dragUntil: 0, legPhase: 0,
      particles: [],
      nextEmit: 0,
      // smoothed values
      sm: { tilt: 0, armL: 0, armR: 0, hatTilt: 0, hatLift: 0, squash: 0, chain: 0, chainV: 0, bodyX: 0, hatLagX: 0 },
      running: true, raf: 0, timer: 0, last: now
    };

    // ---------------------------------------------------------------- bubble
    var bubbleTimer = 0;
    function say(text, ms) {
      if (!bubbleEl) return;
      text = String(text == null ? '' : text);
      if (!text) { bubbleEl.classList.remove('show'); document.body.classList.remove('bubble-tall'); return; }
      bubbleText.textContent = text;
      bubbleEl.classList.add('show');
      // A wide 3-4 line pill reaches the prompt button's row; the button steps down while it shows.
      document.body.classList.toggle('bubble-tall', bubbleEl.offsetHeight > 52);
      clearTimeout(bubbleTimer);
      var dur = ms || clamp(1800 + text.length * 55, 1800, 9000);
      bubbleTimer = setTimeout(function () { bubbleEl.classList.remove('show'); document.body.classList.remove('bubble-tall'); }, dur);
    }
    var lastLine = {};
    function sayFrom(key, ms) {
      var arr = LINES[key]; if (!arr) return;
      var t, n = 0;
      do { t = pick(arr); } while (t === lastLine[key] && ++n < 5);
      lastLine[key] = t;
      say(t, ms);
    }

    // ------------------------------------------------------------- particles
    function emit(kind, x, y, vx, vy, life, size) {
      if (S.particles.length > 40) S.particles.shift();
      S.particles.push({ kind: kind, x: x, y: y, vx: vx || 0, vy: vy || 0, life: life || 1200, age: 0, size: size || 1 });
    }
    function headPos() { return { x: sprite.CX + S.sm.bodyX, y: sprite.FEET_Y - 18 }; }

    // --------------------------------------------------------------- actions
    // An action: { name, dur, t0, fn(p, k, t) } modifying the pose for its duration.
    function play(name, extra) {
      var def = ACTIONS[name]; if (!def) return;
      // one instance of each action at a time
      S.actions = S.actions.filter(function (a) { return a.name !== name; });
      var a = { name: name, t0: performance.now(), dur: def.dur, fn: def.fn, data: extra || {} };
      if (def.start) def.start(a);
      S.actions.push(a);
      S.recent.unshift(name); S.recent.length = Math.min(S.recent.length, 2);
    }

    var ACTIONS = {
      hatBounce: { dur: 650, fn: function (p, k) { p.hatLift += Math.abs(Math.sin(k * Math.PI * 2)) * 3 * (1 - k); p.hatTilt += Math.sin(k * 12) * 0.08 * (1 - k); } },
      monocleAdjust: {
        dur: 1200, fn: function (p, k) {
          var u = bell(k);
          p.armR = Math.max(p.armR, u); p.squint = k > 0.15 && k < 0.85; p.eyeDX = 1; p.eyeDY = 0;
          p.monocleGlint = k > 0.55 ? (k - 0.55) / 0.4 : 0; p.chainKick = k < 0.1 ? 1 : 0; p.tilt += 0.06 * u;
        }
      },
      armWiggle: { dur: 900, fn: function (p, k) { var w = Math.sin(k * Math.PI * 8); p.armL = w; p.armR = -w; p.tilt += 0.04 * w; } },
      lookAround: {
        dur: 1600, start: function (a) { a.data.seq = [pick([-1, 1]), 0, 0, 0]; a.data.seq[1] = -a.data.seq[0]; a.data.up = Math.random() < 0.4 ? -1 : 0; },
        fn: function (p, k, t, a) { var i = Math.min(3, Math.floor(k * 4)); p.eyeDX = a.data.seq[i]; p.eyeDY = i === 2 ? a.data.up : 0; p.tilt += a.data.seq[i] * 0.04; }
      },
      hop: {
        dur: 620, fn: function (p, k) {
          if (k < 0.18) p.squash += 0.8 * bell(k / 0.18);            // anticipation
          else if (k < 0.82) { var u = (k - 0.18) / 0.64; p.y -= 7 * bell(u); p.squash -= 0.6 * bell(u * 2 > 1 ? 0 : u * 2); p.legsDangle = 0.6; p.hatLift += 1.5 * bell(u); p.chainKick = 1; }
          else p.squash += 0.9 * bell((k - 0.82) / 0.18);             // landing
        }
      },
      doubleHop: { dur: 1000, fn: function (p, k) { var u = (k * 2) % 1; p.y -= 4 * bell(u); p.squash += u > 0.85 ? 0.6 : -0.2 * bell(u); p.chainKick = 1; } },
      spinShiver: { dur: 650, fn: function (p, k) { p.tilt += Math.sin(k * 50) * 0.13 * (1 - k); p.jxExtra = irand(-2, 2); p.hatTilt += Math.sin(k * 40) * 0.12; p.chainKick = 1; } },
      stretch: { dur: 1300, fn: function (p, k) { var u = bell(k); p.squash -= 0.9 * u; p.armL = p.armR = u; p.eyes = 'closed'; p.y -= u; } },
      tapFoot: { dur: 1100, fn: function (p, k) { p.walking = true; p.legPhase = k * Math.PI * 10; p.eyeDY = 1; } },
      peek: { dur: 1400, start: function (a) { a.data.d = pick([-1, 1]); }, fn: function (p, k, t, a) { var u = bell(k); p.tilt += a.data.d * 0.18 * u; p.eyeDX = a.data.d; p.x += a.data.d * u; } },
      wiggleDance: { dur: 1700, fn: function (p, k) { var b = Math.sin(k * Math.PI * 8); p.tilt += 0.12 * b; p.armL = b > 0 ? 1 : -0.3; p.armR = b > 0 ? -0.3 : 1; p.y -= Math.abs(b) * 1.5; p.eyes = 'happy'; p.chainKick = 1; } },
      hatTip: { dur: 1400, fn: function (p, k) { var u = bell(k); p.armR = Math.max(p.armR, u * 1.2); p.hatLift += 4 * u; p.hatTilt -= 0.45 * u; p.hatX += 2 * u; p.eyes = 'happy'; } },
      startle: {
        dur: 900, start: function () { var h = headPos(); emit('bang', h.x + 7, h.y - 6, 0, -0.01, 700); sayFrom('startle', 900); },
        fn: function (p, k) { var u = bell(Math.min(1, k * 1.6)); p.y -= 8 * u; p.eyes = 'wide'; p.hatLift += 7 * u; p.hatTilt += 0.5 * u; p.armL = p.armR = 1; p.jxExtra = irand(-2, 2); p.legsDangle = u; p.chainKick = 1; }
      },
      squish: { dur: 480, fn: function (p, k) { p.squash += 1.0 * (1 - k) * Math.cos(k * Math.PI * 3); p.eyes = 'happy'; p.chainKick = 1; } },
      shake: { dur: 900, fn: function (p, k) { p.tilt += Math.sin(k * 40) * 0.14 * (1 - k * 0.6); p.x += Math.round(Math.sin(k * 35) * 1.5); p.eyes = 'x'; p.chainKick = 1; } },
      land: { dur: 520, fn: function (p, k) { p.squash += 1.0 * (1 - k) * Math.cos(k * Math.PI * 2.5); p.eyes = k < 0.5 ? 'wide' : p.eyes; p.hatLift += 3 * bell(k); p.chainKick = 1; } },
      yawn: { dur: 1600, fn: function (p, k) { var u = bell(k); p.squash -= 0.6 * u; p.eyes = 'closed'; p.armL = p.armR = u * 0.8; } },
      sneeze: { dur: 1100, fn: function (p, k) { if (k < 0.7) { p.squash -= 0.5 * (k / 0.7); p.eyes = 'closed'; p.tilt -= 0.1 * k; } else { p.squash += 0.9 * bell((k - 0.7) / 0.3); p.hatLift += 6 * bell((k - 0.7) / 0.3); p.jxExtra = irand(-2, 2); } } },
      typeBurst: { dur: 900, fn: function (p, k) { var b = Math.sin(k * 60) > 0; p.armL = b ? 0.7 : 0; p.armR = b ? 0 : 0.7; p.eyeDY = 1; p.jxExtra = irand(-1, 1); } }
    };

    var FIDGETS = {
      idle: { hatBounce: 3, monocleAdjust: 3, armWiggle: 3, lookAround: 5, hop: 2, spinShiver: 1.5, stretch: 1, tapFoot: 2, peek: 2, wiggleDance: 1, sneeze: 0.4 },
      listening: { hatBounce: 2, lookAround: 1, armWiggle: 2, hop: 1, tapFoot: 2, monocleAdjust: 1 },
      thinking: { tapFoot: 3, lookAround: 2, monocleAdjust: 2, hatBounce: 1, spinShiver: 0.5 },
      working: { typeBurst: 4, spinShiver: 1, hatBounce: 1.5, tapFoot: 2, monocleAdjust: 1 },
      browsing: { monocleAdjust: 4, peek: 3, lookAround: 1, hatBounce: 1 },
      success: { hop: 2, wiggleDance: 2, hatTip: 1 },
      error: { spinShiver: 1, lookAround: 1 },
      sleeping: {}
    };
    var FIDGET_GAP = { idle: [1200, 4200], listening: [1500, 3500], thinking: [1400, 3200], working: [700, 2200], browsing: [1500, 3500], success: [600, 1400], error: [1500, 3000], sleeping: [99999, 99999] };

    // ------------------------------------------------------------------ mood
    function setMood(m) {
      if (MOODS.indexOf(m) === -1) m = 'idle';
      S.autoSleep = false;
      if (m === S.mood) { S.moodSince = performance.now(); return; }
      var prev = S.mood;
      S.mood = m; S.moodSince = performance.now();
      if (m !== 'idle') stopWalk();
      if (prev === 'sleeping' && m !== 'sleeping') play('startle');
      switch (m) {
        case 'success': play('hop'); setTimeout(function () { if (S.mood === 'success') play('hatTip'); }, 650); burst('spark', 6); break;
        case 'error': play('shake'); var h = headPos(); emit('drop', h.x + 9, h.y + 2, 0.002, 0.004, 1400); break;
        case 'thinking': play('lookAround'); break;
        case 'browsing': play('monocleAdjust'); break;
        case 'working': play('typeBurst'); break;
        case 'listening': play('hatBounce'); break;
        case 'sleeping': play('yawn'); break;
      }
      S.nextFidget = performance.now() + rand(900, 1800);
    }
    function moodNow() { return S.autoSleep ? 'sleeping' : S.mood; }

    function burst(kind, n) {
      var h = headPos();
      for (var i = 0; i < n; i++) emit(kind, h.x + rand(-12, 12), h.y + rand(-4, 8), rand(-0.01, 0.01), rand(-0.02, -0.008), rand(700, 1300));
    }

    function wake(silent) {
      S.lastInteraction = performance.now();
      if (S.autoSleep) { S.autoSleep = false; play('startle'); if (!silent) sayFrom('wake'); return true; }
      return false;
    }

    // ----------------------------------------------------------------- react
    function react(kind) {
      var t = performance.now();
      var woke = wake(kind === 'drag');
      switch (kind) {
        case 'click':
          play('squish');
          if (!woke) sayFrom('click');
          if (Math.random() < 0.35) setTimeout(function () { play('hop'); }, 300);
          stopWalk();
          break;
        case 'pet':
          S.blushUntil = t + 2500; S.happyUntil = t + 1800;
          for (var i = 0; i < 4; i++) (function (i) { setTimeout(function () { var h = headPos(); emit('heart', h.x + rand(-10, 8), h.y - 2, rand(-0.006, 0.006), -0.012, 1300); }, i * 220); })(i);
          if (Math.random() < 0.5) sayFrom('pet');
          break;
        case 'drag':
          if (S.dragUntil < t) { S.dragStart = t; }
          S.dragUntil = t + 350; stopWalk();
          break;
        case 'drop':
          S.dragUntil = 0; play('land');
          if (Math.random() < 0.4) sayFrom('drop');
          refreshBounds();
          break;
        case 'prompt':
          play('hatTip'); sayFrom('prompt'); stopWalk();
          break;
      }
    }

    // ------------------------------------------------------- bridge helpers
    var hasBridge = !!(bridge && typeof bridge.moveBy === 'function');
    function safe(fn) { try { var r = fn(); if (r && r.catch) r.catch(function () {}); return r; } catch (e) { return null; } }
    function refreshBounds() {
      if (!bridge) return;
      if (bridge.getBounds) { var r = safe(function () { return bridge.getBounds(); }); if (r && r.then) r.then(function (b) { if (b) S.bounds = b; }); }
      if (bridge.getWorkArea) { var w = safe(function () { return bridge.getWorkArea(); }); if (w && w.then) w.then(function (a) { if (a) S.workArea = a; }); }
    }
    refreshBounds();
    var boundsTimer = setInterval(function () { if (!S.walk) refreshBounds(); }, 1500);

    var cursorBusy = false;
    var cursorTimer = setInterval(function () {
      if (!bridge || !bridge.getCursor || cursorBusy || document.hidden) return;
      cursorBusy = true;
      var r = safe(function () { return bridge.getCursor(); });
      if (!r || !r.then) { cursorBusy = false; return; }
      r.then(function (c) { cursorBusy = false; if (c) onCursor(c.x, c.y, true); }, function () { cursorBusy = false; });
    }, 110);

    // In-window fallback when there is no bridge (plain browser preview).
    window.addEventListener('mousemove', function (e) {
      if (bridge && bridge.getCursor) return;
      var b = canvas.getBoundingClientRect();
      onCursor(e.clientX, e.clientY, false, b);
    });

    function petScreenCenter() {
      if (S.bounds) {
        var head = sprite.toCss(sprite.CX + S.sm.bodyX, sprite.FEET_Y - 12);
        return { x: S.bounds.x + head.x, y: S.bounds.y + head.y };
      }
      var r = canvas.getBoundingClientRect();
      var hc = sprite.toCss(sprite.CX, sprite.FEET_Y - 12);
      return { x: r.left + hc.x, y: r.top + hc.y };
    }

    function onCursor(x, y) {
      var t = performance.now();
      var prev = S.cursor;
      S.cursor = { x: x, y: y, t: t };
      if (prev) {
        var d = Math.hypot(x - prev.x, y - prev.y);
        if (d > 2) {
          S.lastCursorMove = t;
          if (S.autoSleep && d > 40) wake(false);
        }
        var c = petScreenCenter();
        var dist = Math.hypot(x - c.x, y - c.y);
        var speed = d / Math.max(1, t - prev.t) * 1000;
        // a fast swoosh right past O startles it (rare, with cooldown)
        if (dist < 90 && speed > 2600 && t > (S.startleCooldown || 0) && moodNow() === 'idle') {
          S.startleCooldown = t + 20000; play('startle');
        }
      }
      updateGaze(x, y);
    }

    function updateGaze(x, y) {
      var c = petScreenCenter();
      var dx = x - c.x, dy = y - c.y, dist = Math.hypot(dx, dy);
      var gx = 0, gy = 0;
      if (dist < 700) {
        gx = Math.abs(dx) > 30 ? Math.sign(dx) : 0;
        gy = dy < -45 ? -1 : dy > 70 ? 1 : 0;
      }
      if (gx !== S.gazeX || gy !== S.gazeY) {   // saccade: eyes jump now, body follows later
        S.gazeX = gx; S.gazeY = gy; S.gazeAt = performance.now();
      }
    }

    // ---------------------------------------------------------------- wander
    function startWander() {
      if (!hasBridge || S.wanderPaused || !S.bounds) return;
      var wa = S.workArea || { x: 0, y: 0, width: screen.availWidth || 1280, height: screen.availHeight || 800 };
      var b = S.bounds;
      var minX = wa.x, maxX = wa.x + wa.width - b.width;
      if (maxX - minX < 60) return;
      var dist = rand(70, 420) * pick([-1, 1]);
      var target = clamp(b.x + dist, minX, maxX);
      if (Math.abs(target - b.x) < 40) target = clamp(b.x - dist, minX, maxX);
      if (Math.abs(target - b.x) < 40) return;
      S.walk = { target: target, x: b.x, dir: Math.sign(target - b.x), speed: rand(45, 85), acc: 0, hopChance: 0.004 };
    }
    function stopWalk() { if (S.walk) { S.walk = null; refreshBounds(); } }

    function stepWalk(dt) {
      var w = S.walk; if (!w) return;
      if (S.wanderPaused || moodNow() !== 'idle' || S.dragUntil > performance.now()) { stopWalk(); return; }
      w.acc += w.speed * dt / 1000;
      var step = Math.floor(w.acc);
      if (step >= 1) {
        w.acc -= step;
        var remain = Math.abs(w.target - w.x);
        step = Math.min(step, remain);
        w.x += step * w.dir;
        safe(function () { return bridge.moveBy(step * w.dir, 0); });
        if (S.bounds) S.bounds.x = w.x;
      }
      S.legPhase += dt * 0.018 * (w.speed / 60);
      if (Math.random() < w.hopChance) play('hop');
      if (Math.abs(w.target - w.x) < 1) { stopWalk(); if (Math.random() < 0.5) play(pick(['lookAround', 'hatBounce', 'stretch'])); }
    }

    // ------------------------------------------------------------------ loop
    function frame(t) {
      var dt = Math.min(100, t - S.last);
      S.last = t;
      var mood = moodNow();

      // auto revert / auto sleep
      if ((S.mood === 'success' && t - S.moodSince > 4200) || (S.mood === 'error' && t - S.moodSince > 5500)) setMood('idle');
      if (S.mood === 'idle' && !S.autoSleep && t - Math.max(S.lastCursorMove, S.lastInteraction) > 90000 && !S.walk) { S.autoSleep = true; play('yawn'); }

      // ---- base pose by mood
      var T = t / 1000;
      var p = {
        x: 0, y: 0, jx: 0, jy: 0, eyeDX: 0, eyeDY: 0, blink: 0, eyes: 'normal', squint: false,
        squash: 0, tilt: 0, armL: 0.2 * Math.sin(T * 1.7), armR: 0.2 * Math.sin(T * 1.9 + 1),
        legPhase: S.legPhase, walking: false, dangle: 0, hatTilt: 0, hatLift: 0, hatX: 0,
        monocleGlint: 0, chainSwing: 0, blush: 0, particles: null, mood: mood
      };
      var breathRate = 2.6, breathAmp = 0.09, jitterP = 0.55, jitterAmp = 1;
      var gazeOK = true;
      var dragging = S.dragUntil > t;

      switch (mood) {
        case 'listening':
          p.armL += 0.35; p.armR += 0.35; p.y -= Math.abs(Math.sin(T * 5)) * 1.2; p.tilt += 0.05 * Math.sin(T * 2.2);
          break;
        case 'thinking':
          gazeOK = false; p.eyeDY = -1; p.eyeDX = Math.sin(T * 0.9) > 0 ? 1 : -1;
          p.armR = 0.55 + (Math.sin(T * 13) > 0 ? 0.35 : 0); p.tilt += 0.07 * Math.sin(T * 1.3);
          if (t > S.nextEmit) { var h = headPos(); emit(Math.random() < 0.5 ? 'q' : 'dot', h.x + 10 + irand(0, 3), h.y - 4, 0.002, -0.006, 1200); S.nextEmit = t + rand(900, 1600); }
          break;
        case 'working':
          gazeOK = false; jitterP = 0.85; jitterAmp = 2;
          var b = Math.sin(T * 18) > 0; p.armL = b ? 0.6 : -0.1; p.armR = b ? -0.1 : 0.6; p.eyeDY = 1; p.eyeDX = Math.sin(T * 0.7) > 0.6 ? 1 : 0;
          p.walking = true; p.legPhase = T * 12; p.y -= Math.abs(Math.sin(T * 9)) * 0.8;
          if (t > S.nextEmit) { var h2 = headPos(); emit(Math.random() < 0.3 ? 'drop' : 'star', h2.x + rand(-12, 12), h2.y + rand(-2, 4), rand(-0.01, 0.01), -0.01, 800); S.nextEmit = t + rand(600, 1500); }
          break;
        case 'browsing':
          gazeOK = false; p.eyeDX = 1; p.eyeDY = Math.sin(T * 0.8) > 0.5 ? 1 : 0; p.squint = true;
          p.tilt += 0.1 + 0.05 * Math.sin(T * 1.1); p.armR = 0.45; p.x += Math.round(Math.sin(T * 0.6) * 1);
          var gp = (T % 2.4) / 0.55; p.monocleGlint = gp < 1 ? gp : 0;
          if (t > S.nextEmit) { var h3 = headPos(); emit('spark', h3.x + 12, h3.y + 1, 0.004, -0.004, 500); S.nextEmit = t + rand(1800, 3200); }
          break;
        case 'success':
          p.eyes = 'happy'; p.y -= Math.abs(Math.sin(T * 6)) * 1.5; p.armL = p.armR = 0.6 + 0.4 * Math.sin(T * 10);
          if (t > S.nextEmit) { burst('spark', 2); S.nextEmit = t + 450; }
          break;
        case 'error':
          p.eyes = 'x'; p.hatTilt = 0.38; p.hatX = 2; p.hatLift = -1; p.tilt -= 0.05; p.armL = -0.5; p.armR = -0.5; jitterP = 0.7;
          if (t > S.nextEmit) { var h4 = headPos(); emit('drop', h4.x + 10, h4.y + 1, 0.003, 0.006, 1200); S.nextEmit = t + rand(1400, 2200); }
          break;
        case 'sleeping':
          gazeOK = false; p.eyes = 'closed'; breathRate = 4.2; breathAmp = 0.22; jitterP = 0.04; p.tilt = 0.07; p.hatTilt = 0.22; p.hatX = 1; p.armL = p.armR = -0.4;
          if (t > S.nextEmit) { var h5 = headPos(); emit(Math.random() < 0.5 ? 'z' : 'zs', h5.x + 9, h5.y - 2, 0.006, -0.009, 2200); S.nextEmit = t + rand(1100, 1700); }
          break;
      }

      // breathing (1px-ish bob + squash)
      var br = Math.sin(T * Math.PI * 2 / breathRate);
      p.squash += br * breathAmp; p.y += br > 0.6 ? 0.3 : 0;

      // gaze (saccade) — body follows a beat later
      if (gazeOK && !S.lookOverride) {
        p.eyeDX = S.gazeX; p.eyeDY = S.gazeY;
        if (t - S.gazeAt > 160) p.tilt += S.gazeX * 0.035;
      }

      // happy / blush timers from reactions
      if (S.happyUntil > t) p.eyes = 'happy';
      if (S.blushUntil > t) p.blush = clamp((S.blushUntil - t) / 800, 0, 1);

      // walking
      if (S.walk) { p.walking = true; p.legPhase = S.legPhase; p.tilt += S.walk.dir * 0.07; p.y -= Math.abs(Math.sin(S.legPhase)) * 0.8; p.eyeDX = S.walk.dir; }

      // dragged: dangle legs, flail arms, wide eyes
      if (dragging) {
        p.dangle = 1; p.legPhase = T * 8; p.eyes = 'wide'; p.armL = 0.8 + 0.2 * Math.sin(T * 14); p.armR = 0.8 + 0.2 * Math.sin(T * 13 + 1);
        p.tilt += 0.12 * Math.sin(T * 4); p.chainKick = 1; p.shadow = false;
      }

      // actions overlay
      var still = [];
      for (var i = 0; i < S.actions.length; i++) {
        var a = S.actions[i], k = (t - a.t0) / a.dur;
        if (k >= 1) continue;
        a.fn(p, clamp(k, 0, 1), t, a);
        still.push(a);
      }
      S.actions = still;
      if (p.legsDangle) p.dangle = Math.max(p.dangle, p.legsDangle);

      // blink scheduler (2-6s, ~12% double blink, 110ms)
      if (t > S.nextBlink && p.eyes === 'normal') {
        S.blinkUntil = t + rand(90, 140);
        if (S.blinkQueue > 0) { S.blinkQueue--; S.nextBlink = t + 230; }
        else { S.blinkQueue = Math.random() < 0.13 ? 1 : 0; S.nextBlink = t + rand(2000, 6000); }
      }
      if (t < S.blinkUntil) p.blink = 1;

      // fidgets
      if (t > S.nextFidget && !dragging) {
        var gap = FIDGET_GAP[mood] || [2000, 4000];
        var table = FIDGETS[mood] || {};
        if (Object.keys(table).length && S.actions.length < 2 && !S.walk) play(weightedPick(table, S.recent));
        S.nextFidget = t + rand(gap[0], gap[1]);
      }

      // wandering (idle only, never while the panel is open)
      if (mood === 'idle' && !S.walk && t > S.nextWander && !dragging) {
        if (Math.random() < 0.45) startWander();
        S.nextWander = t + rand(9000, 22000);
      }
      stepWalk(dt);

      // idle chatter / random startle
      if (mood === 'idle' && t > S.nextIdleChatter) { if (Math.random() < 0.5) sayFrom('idle', 2200); S.nextIdleChatter = t + rand(45000, 110000); }
      if (mood === 'idle' && t > S.nextStartle) { if (!S.walk) play('startle'); S.nextStartle = t + rand(70000, 160000); }

      // micro-jitter: irregular 80-250ms, 1px; occasional bigger twitch
      if (t > S.nextJitter) {
        S.nextJitter = t + rand(80, 250);
        if (Math.random() < jitterP) {
          var d = Math.max(1, Math.round(window.devicePixelRatio || 1)) * jitterAmp;
          S.jx = irand(-1, 1) * d; S.jy = irand(-1, 0) * d;
        } else { S.jx = 0; S.jy = 0; }
        if (Math.random() < (mood === 'sleeping' ? 0.005 : 0.05)) { S.twitchUntil = t + rand(40, 90); S.tx = pick([-1, 1]); S.ty = pick([0, -1]); }
      }
      p.jx = S.jx + (p.jxExtra || 0) * Math.max(1, Math.round(window.devicePixelRatio || 1));
      p.jy = S.jy;
      if (t < S.twitchUntil) { p.x += S.tx; p.y += S.ty; }

      // smoothing / follow-through
      var sm = S.sm, f = 1 - Math.pow(0.001, dt / 1000 * 2.2);
      sm.tilt = lerp(sm.tilt, p.tilt, Math.min(1, f * 4)); p.tilt = sm.tilt;
      sm.armL = lerp(sm.armL, p.armL, Math.min(1, f * 6)); p.armL = sm.armL;
      sm.armR = lerp(sm.armR, p.armR, Math.min(1, f * 6)); p.armR = sm.armR;
      // the hat* channel (ring lag in sprite.js) trails the body by a frame or two
      sm.hatTilt = lerp(sm.hatTilt, p.hatTilt + p.tilt * 0.35, Math.min(1, f * 3)); p.hatTilt = sm.hatTilt;
      sm.hatLift = lerp(sm.hatLift, p.hatLift, Math.min(1, f * 5)); p.hatLift = Math.round(sm.hatLift);
      p.hatX = Math.round(p.hatX);
      sm.bodyX = p.x;
      // chain channel: a damped pendulum kicked by motion
      if (p.chainKick) sm.chainV += (Math.random() - 0.5) * 0.5 + p.tilt * 0.4;
      sm.chainV += (-sm.chain * 0.18 - p.tilt * 0.08) - sm.chainV * 0.12;
      sm.chain = clamp(sm.chain + sm.chainV, -1, 1);
      p.chainSwing = sm.chain;

      // particles
      var out = [];
      for (var j = 0; j < S.particles.length; j++) {
        var q = S.particles[j];
        q.age += dt; if (q.age >= q.life) continue;
        q.x += q.vx * dt; q.y += q.vy * dt;
        if (q.kind === 'z' || q.kind === 'zs' || q.kind === 'heart') q.x += Math.sin(q.age / 180) * 0.04;
        if (q.kind === 'drop') q.vy += 0.00002 * dt;
        out.push({ kind: q.kind, x: q.x, y: q.y, a: 1 - Math.pow(q.age / q.life, 2), size: q.size });
      }
      S.particles = S.particles.filter(function (q) { return q.age < q.life; });
      p.particles = out;

      p.x = Math.round(p.x); p.y = Math.round(p.y);
      sprite.draw(p);
      S.lastPose = p;
    }

    function loop(t) {
      if (!S.running) return;
      if (document.hidden) { S.timer = setTimeout(function () { loop(performance.now()); }, 1000); return; }
      S.raf = requestAnimationFrame(loop);
      if (t - S.last < FRAME_MS - 2) return;
      frame(t);
    }
    S.raf = requestAnimationFrame(loop);

    window.addEventListener('resize', function () { sprite.resize(); });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && S.running) { cancelAnimationFrame(S.raf); clearTimeout(S.timer); S.last = performance.now(); S.raf = requestAnimationFrame(loop); }
    });

    var api = {
      setMood: setMood,
      getMood: function () { return moodNow(); },
      react: react,
      say: say,
      hitTest: function (x, y) { return sprite.hitTest(x, y); },
      setWanderPaused: function (v) { S.wanderPaused = !!v; if (v) stopWalk(); },
      play: play, // debug / extra: play a named action
      stop: function () { S.running = false; cancelAnimationFrame(S.raf); clearTimeout(S.timer); clearInterval(cursorTimer); clearInterval(boundsTimer); },
      // test hook: render a deterministic frame at time t (ms)
      _frame: function (t) { frame(t); },
      _state: S
    };
    return api;
  }

  window.OBehavior = { start: start };
})();
