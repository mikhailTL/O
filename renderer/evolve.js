'use strict';
// Evolution view: what O learned and added lately, what waits for one tap (upgrades that need approval and
// candidate skills that would install third-party code), and every upgrade with its measured gate results.
// With Settings → "Ask before installing new tools and upgrades" off (status.ask_before_install === false) there
// are no approval cards: the feed logs each automatic install / ship as it happens, with a one-tap Undo.
// Data: window.o.evolveView() (main/evolve.js -> engine /evolve/feed + /evolve/proposals); live updates
// arrive as {type:'evolve'} agent events.
(function () {
  const o = window.o;
  const $ = (id) => document.getElementById(id);
  const sheet = $('evolve');
  if (!sheet) return;
  const has = (n) => !!o && typeof o[n] === 'function';
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const safeUrl = (u) => (/^https:\/\//.test(String(u || '')) ? String(u) : '');
  const OTHER_SHEETS = ['settings', 'actionlog', 'runs', 'goals', 'drawer'];

  const STATUS = {
    proposed: 'Idea', accepted: 'Queued', building: 'Building', built: 'Checking', gated_pass: 'Passed checks',
    gated_fail: 'Did not pass', pr_open: 'Ready for you', approved: 'Installing', shipped: 'In use',
    rolled_back: 'Undone', rejected: 'Set aside',
  };
  const GOOD = new Set(['gated', 'approved', 'shipped', 'held', 'skill_adopted', 'skill_approved', 'skill_installed', 'practiced']);
  const UNDONE = new Set(['rolled_back', 'rejected', 'skill_uninstalled', 'skill_install_failed']);
  const asks = (st) => !st || st.ask_before_install !== false;
  const WAITS = new Set(['pr_open', 'gated_pass']);
  let data = { feed: [], skills: [], proposals: [], status: {} };
  let open = new Set();
  let busy = new Set();

  const num = (v) => (v == null || !Number.isFinite(Number(v)) ? '—' : String(+(+v).toFixed(Math.abs(v) < 1 ? 3 : 2)));
  const words = (s) => String(s || '').replace(/[_.]/g, ' ');
  function when(ts) {
    const d = new Date(ts * 1000), now = new Date();
    const t = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    return d.toDateString() === now.toDateString() ? t : `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${t}`;
  }

  /** The one number that says what changed: the first check with before and after, or nothing. */
  function gain(p) {
    const c = (p.checks || []).find((x) => x.metric_before != null && x.metric_after != null && x.metric_before !== x.metric_after);
    return c ? `${num(c.metric_before)} → ${num(c.metric_after)}` : '';
  }

  function checksHtml(p) {
    const rows = (p.checks || []).map((c) => `<div class="ev-check" data-ok="${c.passed ? 'true' : 'false'}"><span class="mk" aria-hidden="true"></span><span>${esc(words(c.name))}${c.passed ? '' : ' <span class="sr">did not pass</span>'}</span><span class="ba">${c.metric_before != null || c.metric_after != null ? `${num(c.metric_before)} → ${num(c.metric_after)}` : ''}</span></div>`).join('');
    return rows ? `<div class="ev-checks" role="list" aria-label="Checks">${rows}</div>` : '';
  }

  function signalsHtml(p) {
    const s = (p.signals || []).map((x) => {
      const u = safeUrl(x.url);
      return `<li>${u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(x.title)}</a>` : esc(x.title)}<span>${esc(x.source)}</span></li>`;
    }).join('');
    return s ? `<ul class="ev-sigs" aria-label="What it builds on">${s}</ul>` : '';
  }

  function metaHtml(p) {
    const bits = [`${esc(words(p.subsystem))}`, `${esc(p.risk)} risk`];
    if ((p.new_dependencies || []).length) bits.push(`adds ${esc(p.new_dependencies.join(', '))}`);
    if (p.diff_files) bits.push(`${p.diff_files} files`);
    if (p.spent_usd) bits.push(`$${Number(p.spent_usd).toFixed(2)} to build`);
    const link = safeUrl(p.pr_url) || safeUrl(p.compare_url);
    if (link) bits.push(`<a href="${esc(link)}" target="_blank" rel="noopener">${p.pr_url ? 'pull request' : 'branch'}</a>`);
    return `<p class="ev-meta">${bits.join(' · ')}</p>`;
  }

  async function act(key, fn, el) {
    if (busy.has(key)) return;
    busy.add(key);
    if (el) el.classList.add('busy');
    let r;
    try { r = await fn(); } catch (err) { r = { ok: false, error: err && err.message }; }
    busy.delete(key);
    if (el) el.classList.remove('busy');
    if (r && r.ok === false && el) {
      const note = document.createElement('p');
      note.className = 'cp-note';
      note.textContent = r.error || 'That did not work';
      (el.querySelector('.ev-acts, .cp-acts') || el).insertAdjacentElement('beforebegin', note);
      return;
    }
    refresh();
  }

  function upgradeCard(p) {
    const el = document.createElement('div');
    el.className = 'cp-card';
    el.dataset.id = p.id;
    const why = p.auto_ship_reason && /^waits/.test(p.auto_ship_reason) ? p.auto_ship_reason.replace(/^waits for approval:?\s*/, 'Needs you: ') : '';
    el.innerHTML = `<div class="cp-kind">Upgrade · ${esc(words(p.subsystem))}${why ? ` · ${esc(why)}` : ''}</div>
      <div class="cp-head">${esc(p.title)}</div>
      <p class="cp-sum">${esc(p.rationale)}</p>
      ${signalsHtml(p)}${checksHtml(p)}${metaHtml(p)}
      <div class="cp-acts"><button type="button" class="cp-skip">Not now</button><button type="button" class="cp-approve">Approve</button></div>`;
    el.querySelector('.cp-approve').addEventListener('click', () => act(p.id, () => o.evolveApprove(p.id), el));
    el.querySelector('.cp-skip').addEventListener('click', () => act(p.id, () => o.evolveReject(p.id, 'not now'), el));
    return el;
  }

  function skillCard(k) {
    const el = document.createElement('div');
    el.className = 'cp-card';
    el.dataset.id = k.id;
    const what = k.kind ? words(k.kind) : 'tool';
    const src = safeUrl(k.url || k.source_url);
    el.innerHTML = `<div class="cp-kind">New skill · installs third-party code (${esc(what)})</div>
      <div class="cp-head">${esc(k.name || k.title || k.id)}</div>
      ${k.description || k.summary ? `<p class="cp-sum">${esc(k.description || k.summary)}</p>` : ''}
      ${k.proposed_adapter ? `<pre class="ev-adapter" aria-label="What O would add">${esc(String(k.proposed_adapter).slice(0, 600))}</pre>` : ''}
      <p class="ev-meta">${[k.license ? esc(k.license) : '', k.release ? `pinned to ${esc(k.release)}` : '', src ? `<a href="${esc(src)}" target="_blank" rel="noopener">source</a>` : '', 'nothing is installed until you approve', ((data.status || {}).code_builds || {}).enabled === false ? 'no sandbox here, so it would be installed untested' : 'tested in the sandbox before it is turned on'].filter(Boolean).join(' · ')}</p>
      <div class="cp-acts"><button type="button" class="cp-skip">Decline</button><button type="button" class="cp-approve">Approve</button></div>`;
    el.querySelector('.cp-approve').addEventListener('click', () => act('k:' + k.id, () => o.evolveSkill(k.id, true), el));
    el.querySelector('.cp-skip').addEventListener('click', () => act('k:' + k.id, () => o.evolveSkill(k.id, false), el));
    return el;
  }

  function upgradeRow(p) {
    const row = document.createElement('div');
    row.className = 'ev-row';
    row.setAttribute('role', 'listitem');
    row.dataset.status = p.status;
    const g = gain(p);
    const expanded = open.has(p.id);
    row.innerHTML = `<button type="button" class="ev-open" aria-expanded="${expanded}"><span class="gtext"><span class="gttl">${esc(p.title)}</span><span class="gline">${esc(words(p.subsystem))}${p.rollback_reason ? ` · ${esc(p.rollback_reason)}` : ''}</span></span>${g ? `<span class="ev-gain">${esc(g)}</span>` : ''}<span class="ev-pill">${esc(STATUS[p.status] || p.status)}</span></button>
      <div class="ev-body"${expanded ? '' : ' hidden'}><p class="ev-why">${esc(p.rationale)}</p>${signalsHtml(p)}${checksHtml(p)}${metaHtml(p)}
      ${p.status === 'shipped' || p.status === 'approved' ? '<div class="ev-acts"><button type="button" class="ev-rollback">Roll back</button></div>' : ''}</div>`;
    const btn = row.querySelector('.ev-open');
    const body = row.querySelector('.ev-body');
    btn.addEventListener('click', () => {
      const now = body.hidden;
      body.hidden = !now;
      btn.setAttribute('aria-expanded', String(now));
      if (now) open.add(p.id); else open.delete(p.id);
    });
    const rb = row.querySelector('.ev-rollback');
    if (rb) rb.addEventListener('click', () => act(p.id, () => o.evolveRollback(p.id), row));
    return row;
  }

  function feedItem(f) {
    const li = document.createElement('li');
    li.dataset.tone = UNDONE.has(f.event) ? 'undo' : GOOD.has(f.event) ? 'good' : '';
    const u = safeUrl(f.url);
    li.innerHTML = `<time>${esc(when(f.ts))}</time>${u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(f.text)}</a>` : `<span>${esc(f.text)}</span>`}`;
    const undo = f.undo && (f.undo.kind === 'skill' || f.undo.kind === 'upgrade') ? f.undo : null;
    if (undo) {
      li.classList.add('has-undo');
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ev-undo';
      b.textContent = 'Undo';
      b.setAttribute('aria-label', undo.kind === 'skill' ? `Undo: uninstall ${f.text}` : `Undo: roll back ${f.text}`);
      b.addEventListener('click', () => act('u:' + undo.id, () => (undo.kind === 'skill' ? o.evolveUninstall(undo.id) : o.evolveRollback(undo.id)), b));
      li.appendChild(b);
    }
    return li;
  }

  function subline(st) {
    if (!st || st.enabled === false) return 'Evolution is off. Turn it on in Settings to let O keep upgrading itself.';
    const b = st.budget || {};
    const bits = [];
    const building = (st.in_flight || []).length;
    if (building) bits.push(`${building} building`);
    if (b.cap_usd != null) bits.push(`$${Number(b.spent_7d || 0).toFixed(2)} of $${Number(b.cap_usd).toFixed(0)} this week`);
    if (st.last_scan) bits.push(`last looked ${when(st.last_scan)}`);
    return bits.join(' · ') || 'O keeps learning from new tools and research, and keeps only what measures better';
  }

  function render() {
    const st = data.status || {};
    $('evolve-sub').textContent = subline(st);
    const waiting = $('evolve-waiting');
    waiting.innerHTML = '';
    const ask = asks(st);
    const ups = ask ? (data.proposals || []).filter((p) => WAITS.has(p.status)) : [];
    const skills = ask ? data.skills || [] : [];
    if (ups.length || skills.length) {
      const h = document.createElement('h3'); h.className = 'sheet-h'; h.textContent = 'Waiting for you'; waiting.appendChild(h);
      ups.forEach((p) => waiting.appendChild(upgradeCard(p)));
      skills.forEach((k) => waiting.appendChild(skillCard(k)));
    }
    const feed = $('evolve-feed');
    feed.innerHTML = '';
    const items = (data.feed || []).slice(0, 30);
    if (items.length) items.forEach((f) => feed.appendChild(feedItem(f)));
    else feed.innerHTML = '<li class="ev-empty">Nothing new yet. O looks for new releases and research every so often and tries what fits.</li>';
    const list = $('evolve-list');
    list.innerHTML = '';
    const rest = (data.proposals || []).filter((p) => !ups.includes(p));
    if (rest.length) rest.forEach((p) => list.appendChild(upgradeRow(p)));
    else list.innerHTML = '<p class="goals-empty">No upgrades yet.</p>';
    const mode = ask ? 'Every new tool and upgrade waits for your approval.'
      : 'O installs tools it finds and ships its own upgrades without asking; each one is listed above with Undo. Upgrades install while you are away.';
    $('evolve-foot').textContent = [mode, st.enabled !== false ? 'Safety checks, sandbox testing and automatic rollback always apply; approvals, payments, the action log and consent rules are never changed by an upgrade.' : ''].filter(Boolean).join(' ');
    badge(ups.length + skills.length);
    renderHome();
  }

  // ---------- Home: the Learning section (latest feed lines, with Undo) and the Needs you rows for anything that
  // waits for an approval here. Full cards stay in the Evolution view; Home links to it.
  function needRow(kind, title, id) {
    const el = document.createElement('div');
    el.className = 'need ev-need';
    el.dataset.key = id;
    el.innerHTML = `<div class="need-kind">${esc(kind)}</div><div class="need-head">${esc(title)}</div><div class="need-acts"><span class="spacer"></span><button type="button" class="allow ev-review">Review</button></div>`;
    el.querySelector('.ev-review').addEventListener('click', openSheet);
    return el;
  }
  function renderHome() {
    const feed = $('home-learn-feed');
    if (!feed) return;
    const st = data.status || {};
    const ask = asks(st);
    const needs = $('home-evolve-needs');
    needs.innerHTML = '';
    if (ask) {
      (data.proposals || []).filter((p) => WAITS.has(p.status)).forEach((p) => needs.appendChild(needRow(`Upgrade · ${words(p.subsystem)}`, p.title, 'u:' + p.id)));
      (data.skills || []).forEach((k) => needs.appendChild(needRow('New skill · installs third-party code', k.name || k.title || k.id, 'k:' + k.id)));
    }
    feed.innerHTML = '';
    const items = (data.feed || []).slice(0, 4);
    if (items.length) items.forEach((f) => feed.appendChild(feedItem(f)));
    else feed.innerHTML = '<li class="ev-empty">Nothing new yet. O looks for new releases and research on its own and tries what fits.</li>';
    const foot = $('home-learn-foot');
    if (foot) foot.textContent = st.enabled === false ? 'Self-improvement is off.' : subline(st);
  }

  function badge(n) {
    const b = $('evolve-badge');
    if (!b) return;
    b.hidden = !n;
    b.textContent = String(n);
    $('btn-evolve').setAttribute('aria-label', n ? `Evolution, ${n} waiting for you` : 'Evolution');
  }

  async function refresh() {
    if (!has('evolveView')) return;
    let r;
    try { r = await o.evolveView(48); } catch (err) { r = { ok: false, error: err && err.message }; }
    if (!r || r.ok === false) {
      if (!sheet.hidden) {
        $('evolve-sub').textContent = (r && r.error) || 'Evolution is unavailable right now.';
        $('evolve-feed').innerHTML = '';
      }
      return;
    }
    data = r;
    if (!sheet.hidden) render(); else badge(asks(r.status) ? (r.proposals || []).filter((p) => WAITS.has(p.status)).length + (r.skills || []).length : 0);
    renderHome();
  }

  function openSheet() {
    for (const id of OTHER_SHEETS) { const el = $(id); if (el) el.hidden = true; }
    const scrim = $('scrim'); if (scrim) scrim.hidden = true;
    sheet.hidden = false;
    render();
    refresh();
  }
  function closeSheet() { sheet.hidden = true; }

  $('btn-evolve').addEventListener('click', openSheet);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !sheet.hidden) { e.stopPropagation(); closeSheet(); } }, true);
  // Another sheet opening closes this one (panel.js owns those and does not know about this view).
  const mo = new MutationObserver(() => { if (!sheet.hidden && OTHER_SHEETS.some((id) => { const el = $(id); return el && !el.hidden; })) closeSheet(); });
  for (const id of OTHER_SHEETS) { const el = $(id); if (el) mo.observe(el, { attributes: true, attributeFilter: ['hidden'] }); }
  if (has('onAgentEvent')) o.onAgentEvent((ev) => { if (ev && ev.type === 'evolve') { data.status = ev.status || data.status; refresh(); } });
  setTimeout(refresh, 1500);
  setTimeout(renderHome, 0);
  window.__oEvolve = { refresh, render, openSheet }; // tests and screenshots
})();
