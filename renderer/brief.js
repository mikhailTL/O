'use strict';
// The welcome-back brief (main/brief.js opens this window): three sentences, no clicks. It fades out after
// ~12 s, or when the pointer passes over it a second time (the window forwards mouse moves while it lets
// every click through). Then main hides the window.
(function () {
  const o = window.o || {};
  const card = document.getElementById('brief');
  const lines = document.getElementById('b-lines');
  const count = document.getElementById('b-count');
  const LIFE_MS = 12000;
  const OUT_MS = 220;
  let lifeTimer = null;
  let hovers = 0;
  let leaving = false;

  function sentencesOf(text) {
    return String(text || '').replace(/\s+/g, ' ').trim().split(/(?<=[.!?])\s+(?=[A-Z0-9"'“‘(])/).filter(Boolean).slice(0, 3);
  }

  function leave() {
    if (leaving) return;
    leaving = true;
    clearTimeout(lifeTimer);
    card.classList.remove('in');
    card.classList.add('out');
    setTimeout(() => { card.hidden = true; if (typeof o.briefDone === 'function') o.briefDone(); }, OUT_MS + 20);
  }

  function show(b) {
    b = b || {};
    const sentences = Array.isArray(b.sentences) && b.sentences.length ? b.sentences.slice(0, 3) : sentencesOf(b.text);
    lines.textContent = '';
    for (const s of sentences) { const p = document.createElement('p'); p.textContent = String(s); lines.appendChild(p); }
    const n = Number(b.milestones) || 0;
    count.textContent = n ? `${n} milestone${n === 1 ? '' : 's'}` : '';
    hovers = 0; leaving = false;
    card.hidden = false;
    card.classList.remove('in', 'out');
    void card.offsetWidth; // restart the enter transition and the time bar
    requestAnimationFrame(() => card.classList.add('in'));
    if (typeof o.briefResize === 'function') o.briefResize(Math.ceil(document.body.scrollHeight));
    clearTimeout(lifeTimer);
    lifeTimer = setTimeout(leave, LIFE_MS);
  }

  card.addEventListener('mouseenter', () => { hovers += 1; if (hovers >= 2) leave(); });
  if (typeof o.onBrief === 'function') o.onBrief(show);
})();
