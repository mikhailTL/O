'use strict';
// O's permission sidebar: one card per pending approval, Allow / Deny right here.
(function () {
  const o = window.o;
  const stack = document.getElementById('stack');
  const cards = new Map(); // id -> element
  const MAX_SHOWN = 3;
  const OPEN_ICON = '<svg viewBox="0 0 20 20"><path d="M11 4h5v5M16 4l-7 7M14 11.5V15a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h3.5"/></svg>';

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function detailOf(p) {
    const i = p.input || {};
    if (i.command) return i.command;
    if (i.file_path && i.content != null) return `${i.file_path}\n\n${String(i.content).slice(0, 400)}`;
    if (i.file_path) return i.file_path;
    if (i.url) return i.url;
    try { const j = JSON.stringify(i, null, 2); return j === '{}' ? '' : j.slice(0, 600); } catch (_) { return ''; }
  }

  function headline(p) {
    const first = String(p.summary || '').split('\n')[0];
    return first || `Use ${p.toolName || 'a tool'}?`;
  }

  function fmt(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  function makeCard(p) {
    const el = document.createElement('div');
    el.className = 'ncard';
    el.dataset.id = p.id;
    const detail = detailOf(p);
    el.innerHTML = `
      <div class="nhead">
        <img src="../assets/o-icon.png" alt="">
        <div class="ntitle"><b>O needs your OK</b><span>wants to use <b>${esc(p.toolName || 'a tool')}</b></span></div>
        <button class="nopen" title="Open O's chat">${OPEN_ICON}</button>
      </div>
      <div class="nwhat">${esc(headline(p))}</div>
      ${detail ? `<pre class="ndetail">${esc(detail)}</pre>` : ''}
      <div class="nrow">
        <span class="ntimer"></span>
        <span class="spacer"></span>
        <button class="deny">Deny</button>
        <button class="allow">Allow</button>
      </div>`;
    el.querySelector('.allow').addEventListener('click', () => answer(p.id, true));
    el.querySelector('.deny').addEventListener('click', () => answer(p.id, false));
    el.querySelector('.nopen').addEventListener('click', () => o.showPanelFromNotify());
    el._expires = p.expiresAt || 0;
    return el;
  }

  function answer(id, allow) {
    o.respondPermission(id, allow);
    removeCard(id);
  }

  function removeCard(id) {
    const el = cards.get(id);
    if (!el) return;
    cards.delete(id);
    el.classList.add('out');
    setTimeout(() => { el.remove(); fit(); }, 230);
  }

  let moreEl = null;
  function render(list) {
    const ids = new Set(list.map((p) => p.id));
    for (const id of [...cards.keys()]) if (!ids.has(id)) removeCard(id);
    list.slice(0, MAX_SHOWN).forEach((p) => {
      if (cards.has(p.id)) return;
      const el = makeCard(p);
      cards.set(p.id, el);
      stack.appendChild(el);
    });
    const extra = list.length - MAX_SHOWN;
    if (extra > 0) {
      if (!moreEl) { moreEl = document.createElement('div'); moreEl.className = 'nmore'; }
      moreEl.textContent = `+${extra} more waiting in O's chat`;
      stack.appendChild(moreEl);
    } else if (moreEl) { moreEl.remove(); moreEl = null; }
    tick();
    fit();
  }

  // Tell the main process how tall the sidebar should be.
  function fit() {
    requestAnimationFrame(() => o.notifyResize(Math.ceil(document.body.scrollHeight)));
  }

  function tick() {
    const now = Date.now();
    for (const el of cards.values()) {
      const t = el.querySelector('.ntimer');
      if (el._expires) t.textContent = `auto-deny in ${fmt(el._expires - now)}`;
    }
  }
  setInterval(tick, 1000);

  // Same theme as the panel (Settings → Appearance): set before first paint from the preload's
  // synchronous value, confirmed from settings, and updated live when Settings → Save changes it.
  const THEMES = ['glass', 'light', 'dark'];
  function applyTheme(t) { document.documentElement.dataset.theme = THEMES.includes(t) ? t : 'glass'; }
  if (THEMES.includes(o.initialTheme)) applyTheme(o.initialTheme);
  else if (typeof o.getSettings === 'function') o.getSettings().then((s) => applyTheme(s && s.theme)).catch(() => {});
  if (typeof o.onTheme === 'function') o.onTheme(applyTheme);
  o.onNotify((list) => render(Array.isArray(list) ? list : []));
})();
