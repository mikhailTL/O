'use strict';
// Main-side host for O's self-evolution (plan L). The engine does the work (scan, triage, build, gates, PRs,
// post-ship evaluation); this module keeps it moving and connects it to the updater:
//
//   * an EvolveClock (scheduler.js) ticks the engine continuously, more often while the person is away;
//   * each /evolve/status: the updater is told which approved upgrades to look for (trackEvolve), and every
//     rollback the engine decided (a regression after shipping, or the person's Rollback) is applied by
//     installing the previous release and pinning it;
//   * an update that contains approved upgrades installs itself while the person is away and O is idle;
//   * after a restart, ship reports from the updater go to the engine, which starts watching the release.
//
// The panel's Evolution view and the bridge's evolve tools read through `view()`.

const { EvolveClock } = require('./scheduler');

const CHECK_EVERY_MS = 10 * 60 * 1000;

function createEvolveHost({ getSettings, isAway = () => false, isBusy = () => false, engineCall, getUpdater = () => null,
  broadcast = () => {}, log = () => {}, clock = null } = {}) {
  const c = clock || new EvolveClock({ getSettings, isAway, call: engineCall });
  const rolling = new Set();
  let lastStatus = null;
  let lastCheck = 0;
  const now = () => (c.now ? c.now() : Date.now());

  c.on('status', (st) => { onStatus(st).catch(() => {}); });
  c.on('error', () => {}); // engine not running yet: the next poll retries

  async function onStatus(st) {
    if (!st || typeof st !== 'object') return;
    lastStatus = st;
    broadcast({ type: 'evolve', status: st });
    const up = getUpdater();
    if (!up) return;
    const awaiting = st.awaiting_install || [];
    if (typeof up.trackEvolve === 'function') up.trackEvolve(awaiting);
    for (const r of st.rollback_requests || []) await applyRollback(r);
    // An approved upgrade is merged: look for it now instead of waiting for the 6-hourly update check.
    if (awaiting.length && typeof up.check === 'function') {
      const cur = up.state || {};
      if (cur.state === 'available' && Array.isArray(cur.evolve) && cur.evolve.length) onUpdate(cur);
      else if (now() - lastCheck >= CHECK_EVERY_MS) { lastCheck = now(); up.check({ quiet: true }).catch(() => {}); }
    }
  }

  async function applyRollback(r) {
    const up = getUpdater();
    if (!up || !r || !r.id || rolling.has(r.id)) return { ok: false };
    const pin = typeof up.pinned === 'function' ? up.pinned() : null;
    if (pin && pin.evolveId === r.id) { // already installed before the last restart: just acknowledge it
      await engineCall('POST', `/evolve/proposals/${encodeURIComponent(r.id)}/rollback`, { applied: true, ref: pin.ref || r.ref || '' }).catch(() => {});
      return { ok: true, acked: true };
    }
    if (isBusy()) return { ok: false, deferred: true }; // retried on the next status, after the task
    rolling.add(r.id);
    log('policy', `Undoing upgrade ${r.id}${r.reason ? `: ${r.reason}` : ''}`, { id: r.id, ref: r.ref || '' });
    const res = await up.rollback(r.ref || '', { reason: r.reason || '', evolveId: r.id });
    if (res && res.state === 'restarting') {
      await engineCall('POST', `/evolve/proposals/${encodeURIComponent(r.id)}/rollback`, { applied: true, ref: r.ref || '' }).catch(() => {});
      return { ok: true };
    }
    rolling.delete(r.id);
    return { ok: false, error: res && res.message };
  }

  /** Updater events: an update holding approved upgrades installs itself while the person is away and O is idle. */
  function onUpdate(ev) {
    const up = getUpdater();
    if (!up || !ev || ev.state !== 'available' || !Array.isArray(ev.evolve) || !ev.evolve.length) return false;
    if (getSettings().evolveEnabled === false || !isAway() || isBusy()) return false;
    log('note', `Installing approved upgrade${ev.evolve.length === 1 ? '' : 's'}: ${ev.evolve.map((e) => e.title || e.id).join(', ')}`, { ids: ev.evolve.map((e) => e.id) });
    up.apply();
    return true;
  }

  async function reportShipped() {
    const up = getUpdater();
    if (!up || typeof up.pendingShipReports !== 'function') return 0;
    const done = [];
    for (const r of up.pendingShipReports()) {
      try {
        await engineCall('POST', `/evolve/proposals/${encodeURIComponent(r.id)}/shipped`, { release: r.release || '', previous: r.previous || '' });
        done.push(r.id);
      } catch (err) {
        if (err && (err.status === 409 || /409/.test(String(err.message)))) done.push(r.id); // unknown to this engine: drop it
      }
    }
    if (done.length) up.ackShipReports(done);
    return done.length;
  }

  async function onEngineReady() {
    await reportShipped().catch(() => 0);
    return c.refresh();
  }

  /** Everything the Evolution view shows. */
  async function view(hours = 48) {
    let f, p;
    try {
      [f, p] = await Promise.all([
      engineCall('GET', `/evolve/feed?hours=${Math.max(1, Math.min(720, Number(hours) || 48))}`, null, 10000),
      engineCall('GET', '/evolve/proposals', null, 10000),
      ]);
    } catch (err) { return failed(err); }
    return { ok: true, feed: f.feed || [], skills: f.skills || [], status: f.status || lastStatus || {}, proposals: p.proposals || [] };
  }

  const pid = (id) => encodeURIComponent(String(id || '').slice(0, 128));
  const failed = (err) => ({ ok: false, error: (err && err.body && err.body.detail) || (err && err.message) || 'That did not work' });
  async function post(p, body, timeoutMs) {
    try {
      const r = await engineCall('POST', p, body || {}, timeoutMs);
      c.refresh().catch(() => {});
      return { ...r, ok: true };
    } catch (err) { return failed(err); }
  }
  const decide = (id, op, body) => post(`/evolve/proposals/${pid(id)}/${op}`, body, 30000);

  return {
    clock: c,
    start: () => c.start(),
    stop: () => c.stop(),
    onStatus, onUpdate, onEngineReady, reportShipped, applyRollback, view,
    approve: (id) => decide(id, 'approve'),
    reject: (id, reason) => decide(id, 'reject', { reason: String(reason || '').slice(0, 1000) }),
    rollback: (id, reason) => decide(id, 'rollback', { reason: String(reason || 'rolled back by you').slice(0, 1000) }),
    skill: (id, approve) => post(`/evolve/skills/${pid(id)}/${approve ? 'approve' : 'reject'}`, {}, 600000), // approve fetches + sandbox-tests
    uninstall: (id) => post(`/evolve/skills/${pid(id)}/uninstall`, {}, 30000),
    scanNow: () => c.tick({ force: true }),
    status: () => lastStatus,
  };
}

module.exports = { createEvolveHost };
