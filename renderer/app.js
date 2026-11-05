'use strict';
// O AI pet window glue: connects the sprite, the behavior engine, the
// agent event stream, dragging, and the prompt button.
(function () {
  const bridge = window.o;
  const canvas = document.getElementById('pet-canvas');
  const promptBtn = document.getElementById('prompt-btn');
  const bubbleEl = document.getElementById('bubble');
  const bubbleTextEl = document.getElementById('bubble-text') || bubbleEl; // clamped inner span

  // ------------------------------------------------------------ engine
  let pet;
  try {
    const sprite = window.OSprite.create(canvas, { scale: 2 });
    pet = window.OBehavior.start({ canvas, sprite, bridge });
  } catch (err) {
    console.error('[o] behavior engine failed to start:', err);
    pet = fallbackPet();
  }

  // ------------------------------------------------------------ agent events
  let streamBuf = '';
  let sayTimer = null;
  let mood = 'idle';
  let idleTimer = null;

  function setMood(m) {
    mood = m;
    clearTimeout(idleTimer);
    try { pet.setMood(m); } catch (_) {}
  }

  function say(text, ms) {
    if (!text) return;
    try { pet.say(text, ms); } catch (_) {
      bubbleTextEl.textContent = text;
      bubbleEl.classList.add('show');
      document.body.classList.toggle('bubble-tall', bubbleEl.offsetHeight > 52);
      setTimeout(() => { bubbleEl.classList.remove('show'); document.body.classList.remove('bubble-tall'); }, ms || 3000);
    }
  }

  function clip(text, n) {
    text = String(text || '').replace(/\s+/g, ' ').trim();
    return text.length > n ? '…' + text.slice(text.length - n + 1) : text;
  }

  function clipStart(text, n) {
    text = String(text || '').replace(/\s+/g, ' ').trim();
    return text.length > n ? text.slice(0, n - 1) + '…' : text;
  }

  function backToIdleSoon(ms) {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (mood === 'success' || mood === 'error') setMood('idle'); }, ms);
  }

  const isBrowserTool = (name) => /playwright|browser/i.test(name || '');

  bridge.onAgentEvent((ev) => {
    switch (ev.type) {
      case 'status': {
        // A cached script replaying is good news, whatever mood main sent.
        const dejaVu = /^Déjà vu/i.test(String(ev.text || ''));
        if (dejaVu) setMood('success');
        else if (ev.mood) setMood(ev.mood);
        if (ev.text) say(clipStart(ev.text, dejaVu ? 110 : 90), dejaVu ? 3500 : 2600);
        if (dejaVu) backToIdleSoon(4000);
        streamBuf = '';
        break;
      }
      case 'assistant':
        // Full text blocks (deltas are appended if the agent ever sends them).
        streamBuf = ev.delta ? streamBuf + (ev.text || '') : (ev.text || '');
        clearTimeout(sayTimer);
        sayTimer = setTimeout(() => say(clip(streamBuf, 110), 3500), 350);
        break;
      case 'tool':
        streamBuf = '';
        setMood(isBrowserTool(ev.name) ? 'browsing' : 'working');
        say(clipStart(ev.summary || ev.name, 80), 2400);
        break;
      case 'permission':
        // React first: react('prompt') speaks its own line, which must not overwrite the summary.
        setMood('listening');
        tryReact('prompt');
        say('May I? ' + clipStart(ev.summary || ev.toolName, 70), 6000);
        break;
      case 'done':
        streamBuf = '';
        setMood('success');
        say(clipStart(ev.result || 'Done!', 110), 5000);
        backToIdleSoon(5000);
        break;
      case 'error':
        streamBuf = '';
        setMood('error');
        say('Oops: ' + clipStart(ev.message, 90), 5000);
        backToIdleSoon(5000);
        break;
      case 'panel':
        // Stay put (no wandering) while the prompt panel is open next to O.
        panelOpen = !!ev.open;
        applyWander();
        if (ev.open) tryReact('prompt');
        break;
      case 'wander':
        snoozed = !!ev.paused;
        applyWander();
        break;
      case 'physics':
        onPhysics(ev);
        break;
      case 'update':
        if (ev.state === 'available') say('A new version is ready. Tray → Update O now', 6000);
        else if (ev.state === 'restarting') { setMood('success'); say('Updating. Back in a moment.', 3000); }
        break;
      case 'queue':
        if (ev.length > 0) say(`${ev.length} more task${ev.length > 1 ? 's' : ''} queued`, 2000);
        break;
      case 'genui': {
        // Declarative UI frames are the panel's business; the pet only reacts to a job ending.
        const f = ev.frame;
        if (f && f.type === 'end' && /^job:/.test(String(f.surface || ''))) { setMood(f.ok ? 'success' : 'error'); backToIdleSoon(4000); }
        break;
      }
      default:
        break;
    }
  });

  let panelOpen = false;
  let flying = false; // being flung around by the physics in the main process
  let snoozed = false;
  function applyWander() {
    if (typeof pet.setWanderPaused === 'function') pet.setWanderPaused(panelOpen || snoozed || flying);
  }

  function tryReact(kind) {
    try { pet.react(kind); } catch (_) {}
  }

  // ------------------------------------------------------------ prompt button
  promptBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    tryReact('prompt');
    bridge.togglePanel();
  });

  // ------------------------------------------------------------ drag vs click
  const DRAG_THRESHOLD = 4;
  let down = null; // { sx, sy, lastX, lastY, dragging }

  // Recent pointer samples while dragging, used to work out the fling velocity on release.
  let samples = [];
  const FLING_WINDOW_MS = 90;

  canvas.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (flying && typeof bridge.grab === 'function') bridge.grab(); // catch O mid-air
    down = { sx: e.screenX, sy: e.screenY, lastX: e.screenX, lastY: e.screenY, dragging: false };
    samples = [{ x: e.screenX, y: e.screenY, t: performance.now() }];
  });

  window.addEventListener('mousemove', (e) => {
    if (!down) { updateClickThrough(e); return; }
    const dx = e.screenX - down.lastX;
    const dy = e.screenY - down.lastY;
    if (!down.dragging && Math.hypot(e.screenX - down.sx, e.screenY - down.sy) > DRAG_THRESHOLD) {
      down.dragging = true;
      document.body.classList.add('dragging');
      tryReact('drag');
    }
    samples.push({ x: e.screenX, y: e.screenY, t: performance.now() });
    if (samples.length > 12) samples.shift();
    if (down.dragging && (dx || dy)) {
      bridge.moveBy(dx, dy);
      tryReact('drag'); // keeps O dangling while held
      down.lastX = e.screenX;
      down.lastY = e.screenY;
    }
  });

  window.addEventListener('mouseup', () => {
    if (!down) return;
    const wasDrag = down.dragging;
    down = null;
    document.body.classList.remove('dragging');
    if (wasDrag) {
      const v = releaseVelocity();
      if (typeof bridge.fling === 'function') bridge.fling(v.vx, v.vy); // physics takes over (falls even when just let go)
      else tryReact('drop');
    } else {
      tryReact('click');
    }
    samples = [];
  });

  function releaseVelocity() {
    const now = performance.now();
    const recent = samples.filter((p) => now - p.t <= FLING_WINDOW_MS);
    if (recent.length < 2) return { vx: 0, vy: 0 };
    const a = recent[0], b = recent[recent.length - 1];
    const dt = Math.max(16, b.t - a.t) / 1000;
    return { vx: (b.x - a.x) / dt, vy: (b.y - a.y) / dt };
  }

  // ------------------------------------------------------------ fling physics reactions
  let flapTimer = null;
  let lastBounceSay = 0;
  function onPhysics(ev) {
    switch (ev.event) {
      case 'launch':
        flying = true;
        applyWander();
        clearInterval(flapTimer);
        flapTimer = setInterval(() => tryReact('drag'), 120); // legs dangle and arms flail in the air
        if (ev.speed > 1800) say(pick(['Whoa.', 'Airborne.', 'Hold on.']), 1400);
        break;
      case 'bounce': {
        try { pet.play(ev.strength > 900 ? 'land' : 'squish'); } catch (_) {}
        const t = performance.now();
        if (ev.strength > 1400 && t - lastBounceSay > 900) {
          lastBounceSay = t;
          say(pick(['Oof.', 'Careful.', 'Easy.']), 900);
        }
        break;
      }
      case 'rest':
        flying = false;
        clearInterval(flapTimer);
        tryReact('drop');
        if (ev.reason !== 'grabbed') {
          if (ev.bounces >= 4) { try { pet.play('spinShiver'); } catch (_) {} say(pick(['Dizzy.', 'Give me a second.', 'Still spinning.']), 2200); }
          else if (ev.bounces >= 1 && Math.random() < 0.5) say(pick(['Landed.', 'Settled.', 'Fine. I\'m fine.']), 1800);
        }
        applyWander();
        break;
      default:
        break;
    }
  }
  function pick(a) { return a[Math.floor(Math.random() * a.length)]; }

  canvas.addEventListener('dblclick', () => bridge.togglePanel());
  // Right-click O: a small menu (open, stop, snooze, hide, turn off).
  canvas.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (typeof bridge.petMenu === 'function') bridge.petMenu(); else bridge.togglePanel();
  });

  // Petting: slow hovering strokes over O count as pets.
  let strokeDist = 0;
  let lastHover = null;
  canvas.addEventListener('mousemove', (e) => {
    if (down) return;
    if (lastHover) strokeDist += Math.hypot(e.clientX - lastHover.x, e.clientY - lastHover.y);
    lastHover = { x: e.clientX, y: e.clientY };
    if (strokeDist > 400) { strokeDist = 0; tryReact('pet'); }
  });
  canvas.addEventListener('mouseleave', () => { lastHover = null; strokeDist = 0; });

  // ------------------------------------------------------------ click-through
  // On macOS and Windows, let clicks pass through transparent pixels.
  // Linux doesn't support forwarded mouse events, so the small window stays solid there.
  const clickThroughSupported = bridge.platform === 'darwin' || bridge.platform === 'win32';
  let ignoring = false;
  let ctx2d = null;

  function updateClickThrough(e) {
    if (!clickThroughSupported) return;
    const overUi = e.target === promptBtn || e.target === bubbleEl || bubbleEl.contains(e.target);
    let solid = overUi;
    if (!solid && e.target === canvas && typeof pet.hitTest === 'function') {
      const r = canvas.getBoundingClientRect();
      // Pad a few px so tiny gaps between pixels don't flicker.
      solid = [[0, 0], [4, 0], [-4, 0], [0, 4], [0, -4]].some(([ox, oy]) => pet.hitTest(e.clientX - r.left + ox, e.clientY - r.top + oy));
    } else if (!solid && e.target === canvas) {
      try {
        ctx2d = ctx2d || canvas.getContext('2d');
        const r = canvas.getBoundingClientRect();
        const px = Math.floor((e.clientX - r.left) * canvas.width / r.width); // backing store may be dpr-scaled
        const py = Math.floor((e.clientY - r.top) * canvas.height / r.height);
        // Check a small neighbourhood so tiny gaps don't flicker.
        const data = ctx2d.getImageData(Math.max(0, px - 3), Math.max(0, py - 3), 7, 7).data;
        for (let i = 3; i < data.length; i += 4) if (data[i] > 20) { solid = true; break; }
      } catch (_) { solid = true; }
    }
    const shouldIgnore = !solid;
    if (shouldIgnore !== ignoring) {
      ignoring = shouldIgnore;
      bridge.setIgnoreMouse(ignoring);
    }
  }

  // ------------------------------------------------------------ greeting
  setTimeout(async () => {
    say('O here. Click the spark to give me a task.', 4500);
    try {
      const status = await bridge.checkClaude();
      if (status && !status.installed) {
        setTimeout(() => say('I need Claude Code to think. Open my panel for setup.', 5000), 5000);
      }
    } catch (_) {}
  }, 900);

  // ------------------------------------------------------------ fallback
  // Minimal stand-in in case sprite.js / behavior.js fail to load.
  function fallbackPet() {
    const ctx = canvas.getContext('2d');
    const img = new Image();
    img.src = '../assets/o-base.png';
    let t = 0;
    let currentMood = 'idle';
    let hideTimer = null;
    function frame() {
      t++;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.imageSmoothingEnabled = false;
      const jx = Math.random() < 0.3 ? (Math.random() * 2 - 1) | 0 : 0;
      const jy = currentMood === 'error' ? Math.sin(t) * 3 : Math.sin(t / 10) * 2;
      if (img.complete) ctx.drawImage(img, 68 + jx, 112 + jy, 64, 64); // o-base.png is the 64px orb
      requestAnimationFrame(frame);
    }
    frame();
    return {
      setMood(m) { currentMood = m; },
      react() {},
      say(text, ms) {
        bubbleTextEl.textContent = text;
        bubbleEl.classList.add('show');
        document.body.classList.toggle('bubble-tall', bubbleEl.offsetHeight > 52);
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => { bubbleEl.classList.remove('show'); document.body.classList.remove('bubble-tall'); }, ms || 3000);
      },
    };
  }
})();
