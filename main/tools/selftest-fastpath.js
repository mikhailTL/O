#!/usr/bin/env node
'use strict';
// Self-test for the zero-LLM fast path (main/fastpath.js), its task-pool hook (main/tasks.js), model routing,
// the no-ToolSearch child env and the browser-data guard (main/agent.js). No network, no Claude Code.
// Run: node main/tools/selftest-fastpath.js   (exit 1 on any failure)

const os = require('os');
const path = require('path');
const fs = require('fs');
const { createFastPath, toUrl } = require('../fastpath');
const { TaskManager } = require('../tasks');
const { routeModel, agentEnv, childEnv, readsBrowserData, OAgent, buildPersona, DEFAULT_FAST_MODEL, MAX_TURNS_FAST } = require('../agent');
const { isPaymentUrl } = (() => { try { return require('../link'); } catch (_) { return { isPaymentUrl: require('./native-tabs').loadPaymentGuard() }; } })();

let failures = 0;
let passed = 0;
function ok(cond, label) { if (cond) { passed++; console.log('ok   ' + label); } else { failures++; console.log('FAIL ' + label); } }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const APPS = [
  { name: 'Spotify', path: '/Applications/Spotify.app' },
  { name: 'Google Chrome', path: '/Applications/Google Chrome.app' },
  { name: 'Notes', path: '/System/Applications/Notes.app' },
  { name: 'Visual Studio Code', path: '/Applications/Visual Studio Code.app' },
];
const opened = [];
const opener = {
  openUrl: async (url) => { opened.push({ url }); return { ok: true }; },
  openApp: async (app) => { opened.push({ app: app.name }); return { ok: true }; },
};
let fpSettings = {};
const fp = createFastPath({ platform: 'darwin', listApps: () => APPS, opener, isPaymentUrl, getSettings: () => fpSettings });

(async () => {
  // ------------------------------------------------------------ matching
  {
    const m = fp.match('open myscs.org');
    ok(m && m.kind === 'url' && m.url === 'https://myscs.org/', 'myscs.org resolves by its domain: https://myscs.org/');
    ok(fp.match('Open myscs.org.') && fp.match('please go to myscs.org') && fp.match('visit the myscs.org website').url === 'https://myscs.org/', 'myscs.org with punctuation / please / "the ... website"');
    const p = fp.match('open github.com/anthropics/claude-code?tab=readme#top');
    ok(p && p.url === 'https://github.com/anthropics/claude-code?tab=readme#top', 'URL with path, query and hash is kept');
    ok(fp.match('go to https://example.com/a/b').url === 'https://example.com/a/b', 'full https URL');
    ok(fp.match('open localhost:3000').url === 'http://localhost:3000/', 'localhost:port opens over http');
    const a = fp.match('open spotify');
    ok(a && a.kind === 'app' && a.app.name === 'Spotify', 'an installed app: open spotify');
    ok(fp.match('launch chrome').app.name === 'Google Chrome' && fp.match('open vs code').app.name === 'Visual Studio Code', 'app aliases are validated against installed apps');
    ok(fp.match('open notes app').app.name === 'Notes', '"<name> app"');
    ok(fp.match('launch photoshop') === null, 'an app that is not installed falls through');
    const pay = fp.match('open paypal.com');
    ok(pay && pay.kind === 'refuse' && /Blocked by O/.test(pay.reason), 'payment URL refused (paypal.com)');
    ok(fp.match('go to https://checkout.stripe.com/c/pay/x').kind === 'refuse', 'payment URL with path refused (checkout.stripe.com)');
    const s = fp.match('search best pizza in sarasota');
    ok(s && s.kind === 'search' && s.url === 'https://www.google.com/search?q=best%20pizza%20in%20sarasota', 'search <query> -> default search URL');
    ok(fp.match('google how tall is everest').kind === 'search', 'google <query>');
    for (const amb of ['open myscs', 'find me a cheap flight', 'open foo.com and log in', 'open youtube.com, then play music', 'search amazon for headphones',
      'search my files for the tax pdf', 'open report.pdf', 'open main.js', 'open youtube.com in safari', 'open the thing from yesterday', 'book a table at 7',
      'open', 'search', '', 'what is on my screen', 'open file:///etc/passwd', 'open javascript:alert(1)', 'open mailto:a@b.com', 'launch myscs.org']) {
      ok(fp.match(amb) === null, `ambiguous or not simple falls through: ${JSON.stringify(amb)}`);
    }
    for (const bad of ['open foo.com; rm -rf', 'open foo.com && rm -rf ~', 'open $(rm -rf ~).com', 'open `id`.com', 'open foo.com | sh', 'open foo.com\nrm -rf /', 'search x; rm -rf /', 'open "foo.com"']) {
      ok(fp.match(bad) === null, `injection-like input rejected: ${JSON.stringify(bad)}`);
    }
    ok(toUrl('foo.com;rm') === '' && toUrl('user:pw@evil.com') === '' && toUrl('https://user:pw@evil.com') === '', 'toUrl: no ; and no credentials in URLs');
    ok(fp.match('open youtube').url === 'https://www.youtube.com/', 'well-known site name without TLD');
    fpSettings = { fastPath: false };
    ok(fp.match('open myscs.org') === null, 'settings.fastPath false turns it off');
    fpSettings = {};
    const noGuard = createFastPath({ platform: 'darwin', listApps: () => APPS, opener, isPaymentUrl: null });
    ok(noGuard.match('open myscs.org') === null, 'no payment guard -> no URL fast path (fail closed)');
  }

  // ------------------------------------------------------------ running
  {
    opened.length = 0;
    const t0 = Date.now();
    const r = await fp.run(fp.match('open myscs.org'));
    ok(r.ok && opened[0] && opened[0].url === 'https://myscs.org/' && /Opened myscs\.org/.test(r.result), 'run: opens the URL in the default browser');
    ok(Date.now() - t0 < 200, `run: done in under 200 ms (${Date.now() - t0} ms)`);
    const ra = await fp.run(fp.match('open spotify'));
    ok(ra.ok && opened[1] && opened[1].app === 'Spotify', 'run: opens the app');
    const rp = await fp.run({ kind: 'url', url: 'https://paypal.com/', label: 'paypal.com' });
    ok(!rp.ok && rp.refused && opened.length === 2, 'run: a forged plan to a payment site is refused again at run time');
    const slow = createFastPath({ platform: 'darwin', listApps: () => APPS, isPaymentUrl, graceMs: 50, opener: { openUrl: () => new Promise(() => {}), openApp: async () => ({ ok: true }) } });
    const t1 = Date.now();
    const rs = await slow.run(slow.match('open myscs.org'));
    ok(rs.ok && Date.now() - t1 < 200, 'run: an opener that hands off slowly is reported done after the grace period');
    const failing = createFastPath({ platform: 'darwin', listApps: () => APPS, isPaymentUrl, opener: { openUrl: async () => ({ ok: false, error: 'no browser' }), openApp: async () => ({ ok: false, error: 'x' }) } });
    ok(!(await failing.run(failing.match('open myscs.org'))).ok, 'run: an opener error is reported (the pool falls back to the agent)');
  }

  // ------------------------------------------------------------ the task pool hook
  {
    const created = [];
    const logged = [];
    const fakeAgent = (id) => {
      const { EventEmitter } = require('events');
      const a = new EventEmitter();
      a.run = async () => { created.push(id); a.emit('event', { type: 'done', result: 'agent did it' }); return { ok: true, result: 'agent did it' }; };
      a.cancel = () => {}; a.dispose = () => {};
      return a;
    };
    opened.length = 0;
    const tm = new TaskManager({ getSettings: () => ({}), createAgent: fakeAgent, fastPath: fp, actionLog: { record: (e) => logged.push(e) } });
    const ends = [];
    const events = [];
    tm.on('end', (e) => ends.push(e));
    tm.on('event', (e) => events.push(e));
    const t0 = Date.now();
    const r = tm.start({ prompt: 'open myscs.org' });
    ok(r.ok && r.fast && !r.queued, 'pool: "open myscs.org" starts on the fast path (not queued)');
    for (let i = 0; i < 40 && !ends.length; i++) await sleep(5);
    const ms = Date.now() - t0;
    const snap = tm.get(r.taskId) && tm.snapshot(tm.get(r.taskId));
    ok(ends.length === 1 && ends[0].ok && snap.status === 'done' && snap.fast, 'pool: the task ends as done');
    ok(ms < 200, `pool: done in under 200 ms (${ms} ms)`);
    ok(created.length === 0, 'pool: no agent / Claude Code process was created');
    ok(events.some((e) => e.type === 'tool' && e.name === 'o_fastpath') && events.some((e) => e.type === 'done' && e.costUsd === 0 && e.fast), 'pool: one o_fastpath step and a done event with cost 0');
    ok(logged.some((e) => e.kind === 'task' && /Fast path: Opening myscs\.org \(no model call/.test(e.summary)), 'pool: the run is in the action log');
    ok(opened.length === 1 && opened[0].url === 'https://myscs.org/', 'pool: the URL was opened once');

    const r2 = tm.start({ prompt: 'find me a cheap desk and compare three' });
    ok(r2.ok && !r2.fast, 'pool: a real task goes to the agent');
    for (let i = 0; i < 40 && created.length === 0; i++) await sleep(5);
    ok(created.length === 1, 'pool: ... and the agent ran it');

    ends.length = 0;
    const r3 = tm.start({ prompt: 'open paypal.com' });
    const end3 = () => ends.find((e) => e.task.id === r3.taskId);
    for (let i = 0; i < 40 && !end3(); i++) await sleep(5);
    ok(r3.fast && end3() && !end3().ok && /Blocked by O/.test(tm.get(r3.taskId).error) && created.length === 1, 'pool: a payment site is refused with no agent');

    const r4 = tm.start({ prompt: 'open myscs.org', source: 'schedule' });
    ok(r4.ok && !r4.fast, 'pool: scheduled / goal tasks never take the fast path');
    const r5 = tm.start({ prompt: 'open myscs.org', taskId: r.taskId });
    ok(r5.ok && !r5.fast && r5.followUp, 'pool: a follow-up in an ended task goes to the agent');
    await sleep(30);

    // an opener failure falls back to the agent
    const before = created.length;
    const tmF = new TaskManager({ getSettings: () => ({}), createAgent: fakeAgent, fastPath: createFastPath({ platform: 'darwin', listApps: () => APPS, isPaymentUrl, opener: { openUrl: async () => ({ ok: false, error: 'no browser' }) } }) });
    const endsF = [];
    tmF.on('end', (e) => endsF.push(e));
    const rf = tmF.start({ prompt: 'open myscs.org' });
    for (let i = 0; i < 60 && !endsF.length; i++) await sleep(5);
    ok(rf.fast && endsF[0] && endsF[0].ok && created.length === before + 1 && tmF.get(rf.taskId).result === 'agent did it', 'pool: an opener failure hands the task to the full agent');
    const tmOff = new TaskManager({ getSettings: () => ({ fastPath: false }), createAgent: fakeAgent, fastPath: fp });
    ok(!tmOff.start({ prompt: 'open myscs.org' }).fast, 'pool: settings.fastPath false -> agent');
    ok(new TaskManager({ createAgent: fakeAgent }).fastPath === null, 'pool: no fast path by default outside Electron (tests and plain Node)');
    tm.dispose(); tmF.dispose(); tmOff.dispose();
  }

  // ------------------------------------------------------------ model routing
  {
    ok(DEFAULT_FAST_MODEL === 'haiku', 'fastModel defaults to the haiku alias');
    const r1 = routeModel('open myscs.org', { model: 'opus' });
    ok(r1.route === 'fast' && r1.model === 'haiku', 'routing: short single-verb task -> fastModel');
    ok(routeModel('what time is it in Tokyo', { model: '' }).route === 'fast', 'routing: a short question -> fastModel');
    ok(routeModel('find me a cheap flight to NYC and compare three airlines', { model: 'opus' }).route === 'main', 'routing: multi-step -> main model');
    ok(routeModel('open myscs.org, then log in', {}).route === 'main', 'routing: comma / then -> main');
    ok(routeModel('grow my channel to monetization', {}).route === 'main', 'routing: no simple verb -> main');
    ok(routeModel('open x.com', { fastModel: '' }).route === 'main' && routeModel('open x.com', { fastModel: 'off' }).route === 'main', 'routing: fastModel "" / off turns it off');
    ok(routeModel('open x.com', { fastModel: 'sonnet', model: 'opus' }).model === 'sonnet', 'routing: a custom fastModel');
    ok(routeModel('open x.com', {}, { resume: true }).route === 'main' && routeModel('open x.com', {}, { goal: true }).route === 'main', 'routing: follow-ups and goal cycles keep the main model');
    ok(routeModel('o'.repeat(150), {}).route === 'main', 'routing: long prompt -> main');
    ok(MAX_TURNS_FAST > 0 && MAX_TURNS_FAST < 60, 'routing: fast runs get a smaller turn budget');
  }

  // ------------------------------------------------------------ env, args, log note, persona, browser-data guard
  {
    ok(agentEnv().ENABLE_TOOL_SEARCH === 'false', 'child env: ENABLE_TOOL_SEARCH=false (MCP tools load up front, no ToolSearch step)');
    ok(childEnv().ENABLE_TOOL_SEARCH === process.env.ENABLE_TOOL_SEARCH, 'childEnv (proxy, checkClaude) is unchanged');
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'o-fastpath-'));
    const log = [];
    const a = new OAgent({ userDataDir: tmp, getSettings: () => ({ model: 'opus', permissionMode: 'log' }), actionLog: { record: (e) => log.push(e) }, findStagehand: () => null });
    let spawned = null;
    a._spawnChild = (s, args) => { spawned = args; const { EventEmitter } = require('events'); const c = new EventEmitter(); c.stdin = { on() {}, end() {} }; c.stdout = new EventEmitter(); c.stderr = new EventEmitter(); c.stdout.setEncoding = () => {}; c.stderr.setEncoding = () => {}; c.pid = 0;
      setImmediate(() => { c.stdout.emit('data', JSON.stringify({ type: 'result', subtype: 'success', result: 'ok', session_id: 's1' }) + '\n'); c.emit('close', 0, null); }); return c; };
    let r = await a.run('open myscs.org');
    ok(r.ok && spawned.includes('--model') && spawned[spawned.indexOf('--model') + 1] === 'haiku' && spawned[spawned.indexOf('--max-turns') + 1] === String(MAX_TURNS_FAST), 'run: a simple task spawns claude with --model haiku and the small turn budget');
    ok(log.some((e) => e.kind === 'note' && /^Model: haiku \(fast: short single-verb task\)/.test(e.summary)), 'run: the routing decision is an action-log note');
    a.sessionId = null;
    r = await a.run('research standing desks under $300 and compare the top three');
    ok(spawned[spawned.indexOf('--model') + 1] === 'opus' && spawned[spawned.indexOf('--max-turns') + 1] === '60' && log.some((e) => /^Model: opus \(main: /.test(e.summary)), 'run: a complex task keeps the main model, noted in the log');
    a.dispose();

    const p = buildPersona({});
    ok(/Never search browser history, list browser profiles/.test(p) && /Group every action one observation allows/.test(p) && /prefer one web_act \/ web_extract call/.test(p), 'persona: act directly, no history/profile search, group actions, prefer Stagehand');
    ok(/don't take a screenshot or peek at the screen to confirm/.test(p) && /past 20, stop and report/.test(p) && /only when the result depends on taste/.test(p), 'persona: no confirm peeks, step budget, recall only when needed');
    ok(readsBrowserData('Bash', { command: 'ls ~/Library/Application\\ Support/Google/Chrome/' }) || readsBrowserData('Bash', { command: 'ls "$HOME/Library/Application Support/Google/Chrome/"' }), 'guard: listing Chrome profile directories is browser data');
    ok(readsBrowserData('Bash', { command: 'sqlite3 ~/Library/Application\\ Support/Google/Chrome/Default/History "select url from urls where url like \'%scs%\'"' }), 'guard: searching Chrome history is browser data');
    ok(!readsBrowserData('Bash', { command: 'open https://myscs.org' }) && !readsBrowserData('Bash', { command: 'git log --oneline' }) && !readsBrowserData('Read', { file_path: '/x' }), 'guard: ordinary commands pass');
    const g = new OAgent({ userDataDir: tmp, getSettings: () => ({ permissionMode: 'auto' }), actionLog: { record: (e) => log.push(e) } });
    const d = await g.decide('Bash', { command: 'sqlite3 ~/.config/google-chrome/Default/History .tables' });
    ok(d.behavior === 'deny' && /does not read browser history/.test(d.message) && log.some((e) => e.kind === 'policy' && /browser history/.test(e.summary)), 'guard: refused even in auto mode, and logged');
    g.dispose();
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
  }

  console.log(failures ? `\nselftest-fastpath: ${failures} FAILED, ${passed} passed` : `\nselftest-fastpath: all ${passed} checks passed`);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error('selftest-fastpath crashed:', e && e.stack || e); process.exit(1); });
