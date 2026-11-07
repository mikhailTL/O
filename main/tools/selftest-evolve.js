#!/usr/bin/env node
'use strict';
// Self-evolution, app side: settings, engine env, the continuous clock, the updater's rollback / pin /
// evolve tracking against a fake GitHub, the main-side host (rollbacks, installs, ship reports), the bridge's
// read-only tools and the Evolution view's wiring. No network, no Electron.

const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const zlib = require('zlib');

const ROOT = path.join(__dirname, '..', '..');
let failed = 0;
const check = (cond, msg) => { if (cond) console.log('  ok  ', msg); else { failed++; console.error('  FAIL', msg); } };
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'o-evolve-'));

// ---------------------------------------------------------------- tiny tar.gz writer (GitHub tarball shape)
function tarGz(files, top = 'o-O-abc') {
  const blocks = [];
  const header = (name, size, type) => {
    const h = Buffer.alloc(512);
    h.write(name, 0, 100, 'utf8');
    h.write('0000644\0', 100); h.write('0000000\0', 108); h.write('0000000\0', 116);
    h.write(size.toString(8).padStart(11, '0') + '\0', 124);
    h.write('00000000000\0', 136);
    h.write('        ', 148);
    h.write(type, 156);
    h.write('ustar\0', 257); h.write('00', 263);
    let sum = 0; for (const b of h) sum += b;
    h.write(sum.toString(8).padStart(6, '0') + '\0 ', 148);
    return h;
  };
  blocks.push(header(top + '/', 0, '5'));
  for (const [name, text] of Object.entries(files)) {
    const data = Buffer.from(text);
    blocks.push(header(`${top}/${name}`, data.length, '0'), data, Buffer.alloc((512 - (data.length % 512)) % 512));
  }
  blocks.push(Buffer.alloc(1024));
  return zlib.gzipSync(Buffer.concat(blocks));
}

// ---------------------------------------------------------------- fake GitHub
function fakeGitHub(state) {
  return http.createServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    state.hits.push(u.pathname);
    const json = (code, j) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(j)); };
    let m;
    if ((m = /^\/repos\/o\/O\/git\/ref\/heads\/main$/.exec(u.pathname))) return json(200, { object: { sha: state.head } });
    if ((m = /^\/repos\/o\/O\/commits\/(.+)$/.exec(u.pathname))) {
      const ref = decodeURIComponent(m[1]);
      return state.refs[ref] ? json(200, { sha: state.refs[ref] }) : json(404, {});
    }
    if ((m = /^\/repos\/o\/O\/compare\/(.+)\.\.\.(.+)$/.exec(u.pathname))) {
      const [base, head] = [decodeURIComponent(m[1]), decodeURIComponent(m[2])];
      return json(200, { status: (state.contains[head] || []).includes(base) ? 'ahead' : 'diverged' });
    }
    if ((m = /^\/repos\/o\/O\/tarball\/(.+)$/.exec(u.pathname))) {
      const sha = m[1];
      res.writeHead(200); return res.end(tarGz({ 'package.json': JSON.stringify({ name: 'o', version: sha }), 'main/x.js': `// ${sha}\n` }));
    }
    return json(404, {});
  });
}

(async () => {
  // ---------------------------------------------------------------- settings
  console.log('settings');
  {
    const { Settings, DEFAULTS } = require('../settings');
    check(DEFAULTS.evolveEnabled === true && DEFAULTS.evolveBudgetUsd === 10 && DEFAULTS.evolveAutoShip === 'low_risk', 'defaults: enabled, $10/week, low_risk auto-ship');
    check(DEFAULTS.practiceBudgetUsd === 0.5, 'practice budget defaults to $0.50 per 24 h');
    check(DEFAULTS.evolveSlotsAway > DEFAULTS.evolveSlotsActive, 'more build slots while away than while active');
    const s = new Settings(tmp());
    const r = s.set({ evolveBudgetUsd: '-5', evolveAutoShip: 'always', evolveSlotsAway: 99, evolveRepo: 'not a repo', evolveScanMinutes: 1, evolveGithubToken: '  tok  ' });
    check(r.evolveBudgetUsd === 0 && r.evolveAutoShip === 'low_risk' && r.evolveSlotsAway === 8 && r.evolveRepo === '' && r.evolveScanMinutes === 5 && r.evolveGithubToken === 'tok', 'sanitize clamps and rejects bad values');
    check(s.set({ evolveAutoShip: 'never' }).evolveAutoShip === 'never', 'evolveAutoShip never is kept');
  }

  // ---------------------------------------------------------------- engine env + restart keys
  console.log('engine wiring');
  {
    const { evolveEnv } = require('../scheduler');
    const e = evolveEnv({ evolveEnabled: false, evolveBudgetUsd: 4, evolveAutoShip: 'deps_only', updateRepo: 'a/b', evolveGithubToken: 'x' }, '/ud');
    check(e.O_EVOLVE_ENABLED === '0' && e.O_EVOLVE_BUDGET_USD === '4' && e.O_EVOLVE_AUTO_SHIP === 'deps_only' && e.O_EVOLVE_REPO === 'a/b', 'evolveEnv maps settings');
    check(e.O_PRACTICE_BUDGET_USD === '0.5' && evolveEnv({ practiceBudgetUsd: 2 }).O_PRACTICE_BUDGET_USD === '2', 'practice budget (default $0.50 per 24 h) reaches the engine');
    check(e.O_EVOLVE_APP_LOG === path.join('/ud', 'o-actions.jsonl'), 'engine reads the app action log for post-ship cost/outcomes');
    const engineSrc = fs.readFileSync(path.join(ROOT, 'main', 'engine.js'), 'utf8');
    check(/\.\.\.evolveEnv\(s, this\.userDataDir\)/.test(engineSrc), 'engine.js spreads evolveEnv into the child env');
    const mainSrc = fs.readFileSync(path.join(ROOT, 'main', 'main.js'), 'utf8');
    const m = mainSrc.match(/const ENGINE_KEYS = \[([\s\S]*?)\];/);
    check(m && /\.\.\.EVOLVE_KEYS/.test(m[1]) && /const EVOLVE_KEYS = \[[^\]]*'evolveAutoShip'[^\]]*'evolveGithubToken'[^\]]*'practiceBudgetUsd'/.test(mainSrc), 'evolve keys restart the engine');
    check(/evolveHost\.onUpdate\(ev\)/.test(mainSrc) && /evolveHost\.onEngineReady\(\)/.test(mainSrc), 'main wires updater events and engine-ready to the evolve host');
  }

  // ---------------------------------------------------------------- clock
  console.log('clock');
  {
    const { EvolveClock } = require('../scheduler');
    let now = 1e6, away = false, enabled = true;
    const calls = [];
    const clock = new EvolveClock({
      getSettings: () => ({ evolveEnabled: enabled }), isAway: () => away, now: () => now,
      call: async (m, p, b) => { calls.push([m, p, b]); return p === '/evolve/status' ? { enabled: true, rollback_requests: [] } : { ok: true }; },
      activeMs: 1000, awayMs: 100, firstMs: 50,
    });
    let statuses = 0;
    clock.on('status', () => statuses++);
    check(!clock.due(), 'not due right after start');
    now += 60;
    check(clock.due(), 'first tick shortly after start');
    await clock.tick();
    check(calls[0][0] === 'POST' && calls[0][1] === '/evolve/tick' && calls[0][2].presence === 'active' && calls[1][1] === '/evolve/status' && statuses === 1, 'tick posts presence then reads status');
    now += 200;
    check(!clock.due(), 'active: waits the full active interval');
    away = true;
    check(clock.due(), 'turning away ticks at once');
    await clock.tick();
    check(calls[2][2].presence === 'away', 'presence away is passed to the engine (more build slots)');
    now += 150;
    check(clock.due(), 'away: short interval');
    enabled = false;
    check(!clock.due(), 'disabled: never due');
  }

  // ---------------------------------------------------------------- updater: rollback, pin, evolve tracking
  console.log('updater');
  const state = { head: 'h2', refs: { 'evolve-p0': 'h0', h1: 'h1', h2: 'h2' }, contains: { h2: ['m-p1'] }, hits: [] };
  const gh = fakeGitHub(state);
  await new Promise((r) => gh.listen(0, '127.0.0.1', r));
  const apiBase = `http://127.0.0.1:${gh.address().port}`;
  const appDir = tmp();
  fs.writeFileSync(path.join(appDir, 'package.json'), JSON.stringify({ name: 'o', version: 'h1' }));
  fs.writeFileSync(path.join(appDir, '.o-install.json'), JSON.stringify({ sha: 'h1', files: ['package.json'] }));
  const events = [];
  let relaunched = 0;
  const { createUpdater } = require('../updater');
  const up = createUpdater({ appDir, apiBase, getSettings: () => ({ updateRepo: 'o/O', updateBranch: 'main' }), onEvent: (e) => events.push(e), relaunch: () => relaunched++ });
  {
    up.trackEvolve([{ id: 'p1', title: 'Zoomed grounding', merge_sha: 'm-p1', release: 'evolve-p1', previous: 'h1' }, { id: 'p9', merge_sha: 'm-p9' }]);
    const c = await up.check();
    check(c.state === 'available' && c.evolve.length === 1 && c.evolve[0].id === 'p1', 'check reports the approved upgrade contained in the new head');
    check(/Zoomed grounding/.test(c.message), 'calm message names the upgrade');
    await up.apply();
    let man = JSON.parse(fs.readFileSync(path.join(appDir, '.o-install.json'), 'utf8'));
    check(man.sha === 'h2' && man.previous === 'h1', 'apply records the previous version');
    check(JSON.stringify(up.pendingShipReports()) === JSON.stringify([{ id: 'p1', release: 'evolve-p1', previous: 'h1' }]), 'apply keeps a ship report for the engine');
    check(up.ackShipReports(['p1']) === 0 && up.pendingShipReports().length === 0, 'ack clears ship reports');

    const r = await up.rollback('', { reason: 'success fell', evolveId: 'p1' });
    man = JSON.parse(fs.readFileSync(path.join(appDir, '.o-install.json'), 'utf8'));
    check(r.state === 'restarting' && r.rolledBack && man.sha === 'h1', 'rollback with no ref installs the previous version');
    check(man.pinned && man.pinned.skip === 'h2' && man.pinned.evolveId === 'p1', 'rollback pins: the undone head is skipped');
    check(fs.readFileSync(path.join(appDir, 'main', 'x.js'), 'utf8').includes('h1'), 'files of the earlier version are on disk');
    const c2 = await up.check();
    check(c2.state === 'pinned', 'check does not offer the undone release again');
    state.head = 'h3';
    const c3 = await up.check();
    check(c3.state === 'available', 'a newer head (e.g. a fix) is offered again');
    const r2 = await up.rollback('evolve-p0');
    check(r2.state === 'restarting' && JSON.parse(fs.readFileSync(path.join(appDir, '.o-install.json'), 'utf8')).sha === 'h0', 'rollback to a named release tag');
    const r3 = await up.rollback('nope');
    check(r3.state === 'error', 'unknown ref is an error, nothing changes');
    check(up.unpin() && up.pinned() === null, 'unpin clears the pin');
    const busyUp = createUpdater({ appDir, apiBase, isBusy: () => true, getSettings: () => ({ updateRepo: 'o/O', updateBranch: 'main' }), onEvent: () => {}, relaunch: () => {} });
    check((await busyUp.rollback('h1')).busy === true, 'rollback waits while a task runs');
  }
  gh.close();

  // ---------------------------------------------------------------- main-side host
  console.log('host');
  {
    const { createEvolveHost } = require('../evolve');
    const { EventEmitter } = require('events');
    const posted = [];
    let away = false, busy = false;
    const fakeUp = {
      state: { state: 'idle' }, trackedList: null, rolled: [], applied: 0, reports: [{ id: 'p1', release: 'evolve-p1', previous: 'h1' }], pin: null, checks: 0,
      trackEvolve(l) { this.trackedList = l; }, pinned() { return this.pin; },
      async rollback(ref, o) { this.rolled.push([ref, o.evolveId]); return { state: 'restarting' }; },
      apply() { this.applied++; }, async check() { this.checks++; return this.state; },
      pendingShipReports() { return this.reports.slice(); }, ackShipReports(ids) { this.reports = this.reports.filter((r) => !ids.includes(r.id)); },
    };
    const clock = new EventEmitter();
    clock.refresh = async () => null; clock.start = () => {}; clock.stop = () => {}; clock.now = () => 1e9;
    const host = createEvolveHost({
      getSettings: () => ({ evolveEnabled: true }), isAway: () => away, isBusy: () => busy, clock, getUpdater: () => fakeUp,
      engineCall: async (m, p, b) => { posted.push([m, p, b]); return { ok: true }; }, broadcast: () => {}, log: () => {},
    });
    busy = true;
    await host.onStatus({ awaiting_install: [{ id: 'p2', merge_sha: 'm2' }], rollback_requests: [{ id: 'p1', ref: 'h1', reason: 'slower' }] });
    check(fakeUp.trackedList[0].id === 'p2' && fakeUp.checks === 1, 'status: updater tracks approved upgrades and looks for them now');
    check(fakeUp.rolled.length === 0, 'rollback deferred while a task runs');
    busy = false;
    await host.onStatus({ awaiting_install: [], rollback_requests: [{ id: 'p1', ref: 'h1', reason: 'slower' }] });
    check(fakeUp.rolled[0][0] === 'h1' && fakeUp.rolled[0][1] === 'p1', 'rollback request installs the previous release');
    check(posted.some(([m, p, b]) => m === 'POST' && p === '/evolve/proposals/p1/rollback' && b.applied === true), 'engine is told the rollback was applied');
    await host.onStatus({ rollback_requests: [{ id: 'p1', ref: 'h1' }] });
    check(fakeUp.rolled.length === 1, 'never rolls back the same upgrade twice');
    fakeUp.pin = { evolveId: 'p7', ref: 'h5' };
    await host.onStatus({ rollback_requests: [{ id: 'p7', ref: 'h5' }] });
    check(fakeUp.rolled.length === 1 && posted.some(([, p, b]) => p === '/evolve/proposals/p7/rollback' && b.applied), 'after restart: already pinned, only acknowledged');
    const ev = { state: 'available', evolve: [{ id: 'p2', title: 'x' }] };
    check(host.onUpdate(ev) === false && fakeUp.applied === 0, 'approved upgrade does not install while the person is at the computer');
    away = true;
    check(host.onUpdate(ev) === true && fakeUp.applied === 1, 'installs while away and idle');
    check(host.onUpdate({ state: 'available', evolve: [] }) === false, 'ordinary updates keep the normal flow');
    const n = await host.reportShipped();
    check(n === 1 && posted.some(([, p, b]) => p === '/evolve/proposals/p1/shipped' && b.release === 'evolve-p1') && fakeUp.reports.length === 0, 'ship reports go to the engine and are acknowledged');
    const r = await host.approve('p2');
    check(r.ok && posted.some(([, p]) => p === '/evolve/proposals/p2/approve'), 'approve goes to the engine');
    const bad = createEvolveHost({ getSettings: () => ({}), clock, getUpdater: () => null, engineCall: async () => { const e = new Error('409'); e.status = 409; e.body = { detail: 'cannot approve' }; throw e; } });
    const r2 = await bad.approve('x');
    check(r2.ok === false && r2.error === 'cannot approve', 'engine refusal surfaces as a calm error');
  }

  // ---------------------------------------------------------------- bridge tools (read-only)
  console.log('bridge + view');
  {
    const src = fs.readFileSync(path.join(ROOT, 'main', 'tools', 'bridge-mcp.js'), 'utf8');
    check(/name: 'evolve_status'/.test(src) && /name: 'evolve_proposals'/.test(src), 'bridge exposes evolve_status and evolve_proposals');
    const body = src.slice(src.indexOf('async function evolveStatus'), src.indexOf('async function engineGet'));
    check(!/engine\('POST'/.test(body), 'evolve bridge tools only read');
    const html = fs.readFileSync(path.join(ROOT, 'renderer', 'panel.html'), 'utf8');
    check(/<section id="evolve" hidden/.test(html) && /<script src="evolve.js"><\/script>/.test(html) && /id="btn-evolve"/.test(html), 'panel has the Evolution sheet and its script');
    const js = fs.readFileSync(path.join(ROOT, 'renderer', 'evolve.js'), 'utf8');
    check(/evolveSkill\(k\.id, true\)/.test(js) && !/evolveSkill\([^)]*true\)[^;]*setTimeout/.test(js), 'candidate skills install only from the Approve button');
    const pre = fs.readFileSync(path.join(ROOT, 'main', 'preload.js'), 'utf8');
    check(['evolveView', 'evolveApprove', 'evolveReject', 'evolveRollback', 'evolveSkill'].every((k) => pre.includes(k + ':')), 'preload exposes the Evolution actions');
    const wf = fs.readFileSync(path.join(ROOT, '.github', 'workflows', 'evolve-gates.yml'), 'utf8');
    check(/evolve\//.test(wf) && /pytest/.test(wf) && /selftest\*\.js/.test(wf) && /\$\{\{\s*secrets\./.test(wf) === false, 'CI gates run on evolve/* PRs without secrets');
  }

  if (failed) { console.error(`${failed} evolve selftest(s) failed`); process.exit(1); }
  console.log('all evolve selftests passed');
  process.exit(0);
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
