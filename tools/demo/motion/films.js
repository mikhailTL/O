'use strict';
// The films: which real runs to make (the "take") and which shots to cut them into. Every fact on screen comes
// from the take: engine job entries, the cache replay, the action log and its chain verification, the proxy's
// SSE frames, the computer-use run's steps, heal and replay. The chat replies are scripted (and the launch cut
// says so once, in its second frame).
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const HERE = __dirname;
const DEMOS = path.join(HERE, '..', 'demos');
const story = (id) => JSON.parse(fs.readFileSync(path.join(DEMOS, `${id}.json`), 'utf8'));
const CU_TREE = path.join(HERE, '..', 'cu-tree.json');

// ---------------------------------------------------------------- the take: real runs, recorded
function recorder(backend) {
  const entries = [];
  const on = (ev) => { if (ev.type === 'log') entries.push(ev.entry); };
  backend.on('event', on);
  return { entries, stop: () => backend.off('event', on) };
}

/** The scripted Claude Code turn around a real engine call, logged the way agent.js logs it. */
function chat(backend, prompt, toolName, toolSummary, input) {
  backend.actionLog.record({ kind: 'task', summary: 'Task: ' + prompt.slice(0, 120), detail: { prompt } });
  backend.actionLog.record({ kind: 'tool', summary: 'Using o: workspace context', detail: { name: 'mcp__o__workspace_context', input: { cwd: '.' }, tool_use_id: 'film-ctx' } });
  backend.actionLog.record({ kind: 'result', summary: 'mcp__o__workspace_context ok', detail: { is_error: false, content: '{"cwd": "~/o-sandbox"}' } });
  backend.actionLog.record({ kind: 'tool', summary: toolSummary, detail: { name: toolName, input, tool_use_id: 'film-' + Math.random().toString(36).slice(2, 8) } });
}
function chatDone(backend, toolName, result, summary, durationMs) {
  backend.actionLog.record({ kind: 'result', summary: `${toolName} ok`, detail: { is_error: false, content: JSON.stringify(result).slice(0, 400) } });
  backend.actionLog.record({ kind: 'task', summary: 'Done: ' + summary.slice(0, 120), detail: { durationMs } });
}

async function provision(backend, prompt, request) {
  const rec = recorder(backend);
  chat(backend, prompt, 'mcp__o__provision', 'Using o: provision', request);
  const { job_id } = await backend.engine.call('POST', '/provision', request);
  const state = await backend.engine.waitJob(job_id, { timeoutMs: 120000 });
  chatDone(backend, 'mcp__o__provision', { ok: state.ok, cached: state.cached, phase: state.phase, steps: state.steps.length }, state.cached ? 'Replayed the saved script.' : 'Job finished.', state.cached ? 900 : 6200);
  rec.stop();
  if (!state.ok) throw new Error('provision job failed: ' + JSON.stringify(state).slice(0, 300));
  return { job_id, state, entries: rec.entries };
}

async function computerRun(backend, goal, app) {
  const fakeState = () => { try { return JSON.parse(fs.readFileSync(path.join(backend.dataDir, 'fake-cu-state.json'), 'utf8')); } catch (_) { return { plans: 0, replans: 0, calls: 0 }; } };
  const rec = recorder(backend);
  chat(backend, `In ${app === 'notes' ? 'Notes' : app}, ${goal.toLowerCase()}`, 'mcp__o__computer_run', 'Using o: computer run', { goal, app });
  const { job_id } = await backend.engine.call('POST', '/computer/run', { goal, app });
  const state = await backend.engine.waitJob(job_id, { timeoutMs: 180000 });
  chatDone(backend, 'mcp__o__computer_run', { ok: state.ok, attempt: state.attempt, procedure_id: state.procedure_id }, 'Typed the note and saved it.', 9800);
  rec.stop();
  if (!state.ok) throw new Error('cu run failed: ' + state.error);
  const calls1 = fakeState();
  const rec2 = recorder(backend);
  chat(backend, `In ${app === 'notes' ? 'Notes' : app}, ${goal.toLowerCase()}`, 'mcp__o__computer_replay', 'Using o: computer replay', { procedure_id: state.procedure_id });
  const r2 = await backend.engine.call('POST', '/computer/replay', { procedure_id: state.procedure_id });
  const replay = await backend.engine.waitJob(r2.job_id, { timeoutMs: 180000 });
  chatDone(backend, 'mcp__o__computer_replay', { ok: replay.ok, cached: replay.cached, procedure_id: replay.procedure_id }, 'Replayed the saved procedure.', 1200);
  rec2.stop();
  const calls2 = fakeState();
  if (!replay.ok || !replay.cached) throw new Error('replay did not run from the saved procedure');
  if (calls2.plans !== calls1.plans || calls2.replans !== calls1.replans) throw new Error('replay made a planner call');
  return { job_id, state, entries: rec.entries, replay, replayEntries: rec2.entries, plannerCalls: calls1.plans + calls1.replans, replayPlannerCalls: (calls2.plans + calls2.replans) - (calls1.plans + calls1.replans) };
}

/** Run a command and collect its stdout lines (async: the proxy lives in this process). */
function run(cmd, args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { env: { ...process.env, ...(env || {}) }, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '', err = '';
    child.stdout.on('data', (c) => { out += c; });
    child.stderr.on('data', (c) => { err += c; });
    child.on('error', reject);
    child.on('close', (code) => resolve({ code, out, err }));
  });
}

async function proxyCurl(backend, s) {
  const { url, token } = backend.proxy;
  backend.proxy.setFake({ FAKE_TEXT: s.curlReply, FAKE_DELAY_MS: '40' });
  const body = JSON.stringify({ model: 'o-ai', stream: true, messages: [{ role: 'user', content: 'A one-liner that counts lines in all *.py files' }] });
  const rec = recorder(backend);
  const r = await run('curl', ['-sN', `${url}/chat/completions`, '-H', `Authorization: Bearer ${token}`, '-H', 'Content-Type: application/json', '-d', body]);
  rec.stop();
  if (!/\[DONE\]/.test(r.out)) throw new Error('curl did not get a [DONE] frame: ' + (r.out || r.err).slice(0, 300));
  const shownUrl = `http://127.0.0.1:${backend.proxy.port}/v1`;
  const cmd = `curl -N ${shownUrl}/chat/completions -H "Authorization: Bearer $O_PROXY_TOKEN" -H "Content-Type: application/json" -d '${body}'`;
  return { cmd, lines: r.out.split('\n').filter((l) => l.trim()), entries: rec.entries };
}

async function proxyPython(backend, s) {
  const { url, token } = backend.proxy;
  const shownUrl = `http://127.0.0.1:${backend.proxy.port}/v1`;
  const py = ['from openai import OpenAI', '', `client = OpenAI(base_url="${shownUrl}", api_key=O_PROXY_TOKEN)`, 'r = client.chat.completions.create(', '    model="o-ai-code",', '    messages=[{"role": "user", "content": "Return {\\"ok\\": true} as JSON"}],', '    response_format={"type": "json_object"},', ')', 'print(r.choices[0].message.content)'];
  const tmp = fs.mkdtempSync(path.join(require('os').tmpdir(), 'o-film-'));
  const script = path.join(tmp, 'ask_o.py');
  fs.writeFileSync(script, 'import os\nO_PROXY_TOKEN = os.environ["O_PROXY_TOKEN"]\n' + py.join('\n').replace(shownUrl, url) + '\n');
  backend.proxy.setFake({ FAKE_TEXT: 'Sure! Here you go:\n```json\n' + s.pyReply + '\n```\nHope that helps!', FAKE_DELAY_MS: '40' });
  const rec = recorder(backend);
  const r = await run('python3', ['-I', script], { O_PROXY_TOKEN: token });
  rec.stop();
  fs.rmSync(tmp, { recursive: true, force: true });
  if (!/"ok": true/.test(r.out)) throw new Error('python client did not print the JSON: ' + (r.out + r.err).slice(0, 300));
  return { code: py, lines: r.out.split('\n').filter((l) => l.trim()), entries: rec.entries };
}

/** Change one character of one entry in o-actions.jsonl on disk. Returns {line, before, after}. */
function tamper(backend) {
  const file = backend.actionLog.paths().jsonl;
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  const idx = Math.max(1, Math.floor((lines.length - 1) * 0.6));
  const e = JSON.parse(lines[idx]);
  const before = e.summary;
  e.summary = /ok/.test(before) ? before.replace(/ok/, 'OK') : before.replace(/^(.)/, (m) => (m === m.toUpperCase() ? m.toLowerCase() : m.toUpperCase()));
  if (e.summary === before) e.summary = before + ' ';
  lines[idx] = JSON.stringify(e);
  fs.writeFileSync(file, lines.join('\n'));
  return { line: idx + 1, index: idx, before, after: e.summary };
}

// ---------------------------------------------------------------- real data -> shot props
const short = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
const stripJob = (e) => String(e.summary).replace(/^[0-9a-f]{32}: /, '');

/** The engine's own entries for a provisioning job as list rows: graph nodes, the gate, each step's exit code. */
function jobRows(entries, job_id) {
  const rows = [];
  for (const e of entries) {
    if (e.source !== 'engine') continue;
    const s = stripJob(e);
    if (e.kind === 'graph' && /^(context|execute|report)$/.test(s)) rows.push({ txt: s, tag: 'graph' });
    else if (e.kind === 'cache' && /^(cache (miss|hit)|stored|replayed)/.test(s)) rows.push({ txt: s, tag: 'cache' });
    else if (e.kind === 'policy') rows.push({ txt: s.replace(/^gate log: /, ''), tag: 'gate' });
    else if (e.kind === 'result' && /^step \d+ exit=/.test(s)) { const m = /^step (\d+) exit=(\d+) (.*)$/.exec(s); rows.push({ txt: short(m[3], 50), tag: 'step ' + m[1], ok: 'exit ' + m[2] }); }
    else if (e.kind === 'verify') rows.push({ txt: s, tag: 'verify' });
  }
  return rows.slice(0, 11);
}

function treeLines() {
  const tree = JSON.parse(fs.readFileSync(CU_TREE, 'utf8'));
  const out = [];
  (function walk(n, depth) { out.push({ id: n.id, depth, role: n.role, name: n.name || '' }); (n.children || []).forEach((c) => walk(c, depth + 1)); })(tree[0], 0);
  return out;
}

/** A cu step summary ("find as save: role='push button' name='Save'") as a short phrase. */
function stepPhrase(t) {
  const target = (m) => `${m[1]} '${m[2]}'`;
  let m;
  if ((m = /^wait for role='([^']*)' name='([^']*)'/.exec(t))) return 'wait for ' + target(m);
  if ((m = /^find as (\w+): role='([^']*)' name='([^']*)'/.exec(t))) return `find ${m[2]} '${m[3]}' as ${m[1]}`;
  if ((m = /^type ref (\w+) \((\d+) chars\)/.exec(t))) return `type ${m[2]} chars into ${m[1]}`;
  if ((m = /^click ref (\w+)/.exec(t))) return `click ${m[1]}`;
  return t.replace(/ ≤5s$/, '');
}

/** The cu run as cards: first attempt's steps (the failed one marked), heal, judge, store, replay. */
function cuCards(cu) {
  const cards = [];
  let attempt = 0;
  const hl = { Notes: 'w0', Editor: 'e0', Save: 'b3' };
  for (const e of cu.entries) {
    const s = stripJob(e);
    if (e.kind === 'tool' && /^cu step/.test(s) && attempt === 0) {
      const m = /^cu step (\d+) (.*)$/.exec(s);
      const name = (/name='([^']*)'/.exec(m[2]) || [])[1];
      cards.push({ k: 'step ' + m[1], m: stepPhrase(m[2]), hl: name ? hl[name] : (/type/.test(m[2]) ? 'e0' : null) });
    } else if (e.kind === 'result' && /-> FAILED/.test(s) && attempt === 0) {
      const c = cards[cards.length - 1]; c.failed = true; c.x = '✗ not in the tree';
    } else if (e.kind === 'remediate') {
      attempt = 1;
      cards.push({ k: 'heal', m: s.replace(/^healed step (\d+).*$/, 'step $1') });
    } else if (attempt === 1 && e.kind === 'tool' && /^cu step 3/.test(s)) {
      cards[cards.length - 1].m += ' → ' + stepPhrase(s.replace(/^cu step 3 /, ''));
      cards[cards.length - 1].hl = 'b3';
    } else if (e.kind === 'verify' && /judge passed/.test(s)) cards.push({ k: 'judge', m: 'passed', x: '✓' });
    else if (e.kind === 'cache' && /^stored procedure/.test(s)) cards.push({ k: 'store', m: short(s.replace(/\/[0-9a-f]{64}\//, '/…/'), 48) });
  }
  const rp = cu.replayEntries.find((e) => e.kind === 'cache' && /replaying procedure/.test(e.summary));
  cards.push({ k: 'replay', m: (rp ? short(stripJob(rp).replace(/\/[0-9a-f]{64}\//, '/…/'), 40) : 'replayed') + ` · ${cu.replayPlannerCalls} planner calls`, x: cu.replay.ok ? '✓' : '✗', at: cards.length * 110 + 900 });
  return cards;
}

function filesOnDisk(backend, job_id) {
  const dir = path.join(backend.engine.sandboxRoot, job_id);
  const lines = [{ text: '~/o-sandbox/' + job_id.slice(0, 12) + '…/', dim: true }];
  try {
    for (const name of fs.readdirSync(dir).sort()) {
      const full = path.join(dir, name);
      if (fs.statSync(full).isDirectory()) { lines.push({ text: name + '/' }); for (const inner of fs.readdirSync(full).sort()) lines.push({ text: '  ' + inner }); } else lines.push({ text: name });
    }
  } catch (_) {}
  return lines;
}

function sseLines(lines) {
  // the real frames, verbatim; when there are many, the middle ones are elided so the card stays one screen
  if (lines.length <= 5) return lines.map((l) => ({ text: l, hi: /\[DONE\]/.test(l) }));
  return [...lines.slice(0, 2).map((l) => ({ text: l })), { text: `… ${lines.length - 4} more chunks …` }, ...lines.slice(-2).map((l) => ({ text: l, hi: /\[DONE\]/.test(l) }))];
}

// ---------------------------------------------------------------- films
/** Each film: async (backend, state) -> { title, files: [{id, film:[shots], stills:[ms]}], cues:{name: fn} }. */
const films = {};

films.launch = async (backend, state) => {
  const d1 = story('d1'), d3 = story('d3'), d4 = story('d4');
  const job = await provision(backend, d1.prompt, d1.request);
  const logCount = backend.actionLog.tail(5000).length;
  state.genuiVisible = state.genui.surfaces().map((s) => s.surface); // the Runs sheet shows this job only
  const again = await provision(backend, d1.prompt, d1.request);
  if (!again.state.cached) throw new Error('second run did not replay from the cache');
  const cu = await computerRun(backend, d4.goal, d4.app);
  const curl = await proxyCurl(backend, d3);
  const chainBefore = backend.actionLog.verifyChain();
  if (!chainBefore.ok) throw new Error('chain not intact before the film: ' + JSON.stringify(chainBefore));
  // the log sheet starts with the entries written before the first job; the job's entries stream in on camera
  const all = backend.actionLog.tail(5000);
  const jobEntries = all.slice(logCount - job.entries.length, logCount);
  state.logVisible = all.slice(0, logCount - job.entries.length);
  const hit = again.entries.find((e) => e.kind === 'cache' && /cache hit/.test(e.summary));
  const replayed = again.entries.find((e) => e.kind === 'cache' && /replayed/.test(e.summary));
  const heal = cu.entries.find((e) => e.kind === 'remediate');
  const lastStep = job.entries.filter((e) => e.kind === 'result' && /step \d+ exit=/.test(e.summary)).pop();
  const stdout = (job.state.steps[job.state.steps.length - 1].stdout_tail || '').trim();
  let tampered = null;
  return {
    title: 'O — launch film',
    cues: { tamper: async () => { tampered = tamper(backend); } },
    files: [
      { id: 'a-open', stills: [1900], film: [
        { shot: 'intro', line: 'a desktop agent that writes down everything it does', hold: 5200 },
        { shot: 'quiet', text: 'Real engine, real log. The chat replies in this film are scripted.', hold: 2800 },
      ] },
      { id: 'b-ask', stills: [4200, 10200, 13400], film: [
        { shot: 'composer', prompt: d1.prompt, cps: 16 },
        { shot: 'kinetic', text: 'Working' },
        { shot: 'steps', title: 'One provisioning job in O’s engine, every step recorded', items: jobRows(job.entries, job.job_id), hold: 5600 },
      ] },
      { id: 'c-result', stills: [2800, 16500], film: [
        { shot: 'runs', line: 'Plan, steps and result as live cards, from checked frames', hold: 6000, scroll: 0 },
        { shot: 'prose', text: 'Done. demo-app is set up: a README, hello.sh, and it ran' + (stdout ? ' — ' + stdout + '.' : '.'), cps: 30 },
        { shot: 'composer', prompt: d1.prompt, cps: 44, typeAt: 700, tail: 900 },
        { shot: 'steps', title: 'The same request again', items: [{ txt: hit ? stripJob(hit) : 'cache hit', tag: 'cache' }, { txt: replayed ? stripJob(replayed) : 'replayed', tag: 'cache' }, { txt: 'no model call', tag: 'claude' }], at: 400, stagger: 260, hold: 3600 },
        { shot: 'files', title: 'On disk', lines: filesOnDisk(backend, job.job_id).concat(stdout ? [{ text: '$ ./hello.sh', dim: true }, { text: stdout, out: true }] : []) },
      ] },
      { id: 'd-log', stills: [6200, 11800], film: [
        { shot: 'log', entries: jobEntries, stagger: 110, tamper: { cue: 'tamper', note: 'one character changed in o-actions.jsonl, on disk' } },
      ] },
      { id: 'e-api', stills: [8600], film: [
        { shot: 'terminal', title: 'zsh', line: 'An OpenAI-compatible API on localhost, over your own Claude login', cmd: curl.cmd, lines: sseLines(curl.lines), cps: 44, tail: 2800 },
      ] },
      { id: 'f-computer', stills: [9000], film: [
        { shot: 'cu', tree: treeLines(), cards: cuCards(cu), hold: 11500 },
      ] },
      { id: 'g-claims', stills: [2600, 6800], film: [
        { shot: 'claims', each: 2250, groups: [
          { side: 'left', width: 620, claims: [{ verb: 'logs', k: 'log', frag: lastStep ? short(stripJob(lastStep), 34) : 'step 2 exit=0' }, { verb: 'verifies', k: 'chain', frag: `✓ chain intact · ${chainBefore.count} entries` }] },
          { side: 'right', width: 620, claims: [{ verb: 'heals', k: 'heal', frag: heal ? stripJob(heal) : 'healed step 3' }, { verb: 'replays', k: 'cache', frag: replayed ? short(stripJob(replayed), 34) : 'replayed' }] },
        ] },
      ] },
      { id: 'h-close', stills: [2600], film: [
        { shot: 'outro', line: 'cd O && npm install && npm start', hold: 5800 },
      ] },
    ],
    facts: () => ({ job: job.job_id, cached: again.state.cached, cuProcedure: cu.state.procedure_id, replayPlannerCalls: cu.replayPlannerCalls, chainBefore, tampered, chainAfter: backend.actionLog.verifyChain() }),
  };
};

films.d1 = async (backend, state) => {
  const d1 = story('d1');
  const job = await provision(backend, d1.prompt, d1.request);
  const again = await provision(backend, d1.prompt, d1.request);
  if (!again.state.cached) throw new Error('second run did not replay from the cache');
  const hit = again.entries.find((e) => e.kind === 'cache' && /cache hit/.test(e.summary));
  const replayed = again.entries.find((e) => e.kind === 'cache' && /replayed/.test(e.summary));
  const stdout = (job.state.steps[job.state.steps.length - 1].stdout_tail || '').trim();
  return {
    title: d1.title, cues: {},
    files: [
      { id: 'd1-1', stills: [12000], film: [{ shot: 'quiet', text: d1.title, big: true, hold: 2600 }, { shot: 'composer', prompt: d1.prompt, cps: 18 }, { shot: 'kinetic', text: 'Working' }, { shot: 'steps', title: 'One provisioning job, every step recorded', items: jobRows(job.entries, job.job_id), hold: 5000 }] },
      { id: 'd1-2', film: [
        { shot: 'runs', line: 'Plan, steps and result as live cards', hold: 5000 },
        { shot: 'composer', prompt: d1.prompt, cps: 48, typeAt: 600, tail: 800 },
        { shot: 'steps', title: 'The same request again', items: [{ txt: hit ? stripJob(hit) : 'cache hit', tag: 'cache' }, { txt: replayed ? stripJob(replayed) : 'replayed', tag: 'cache' }, { txt: 'no model call', tag: 'claude' }], at: 400, stagger: 260, hold: 3200 },
        { shot: 'files', title: 'On disk', lines: filesOnDisk(backend, job.job_id).concat(stdout ? [{ text: '$ ./hello.sh', dim: true }, { text: stdout, out: true }] : []), hold: 3800 },
      ] },
    ],
    facts: () => ({ job: job.job_id, cached: again.state.cached }),
  };
};

films.d2 = async (backend, state) => {
  const d2 = story('d2');
  const before = backend.actionLog.tail(5000).length;
  const job = await provision(backend, d2.prompt, d2.request);
  const all = backend.actionLog.tail(5000);
  state.logVisible = all.slice(0, before);
  const entries = all.slice(before);
  let tampered = null;
  return {
    title: d2.title, cues: { tamper: async () => { tampered = tamper(backend); } },
    files: [
      { id: 'd2-1', film: [{ shot: 'quiet', text: d2.title, big: true, hold: 2600 }, { shot: 'composer', prompt: d2.prompt, cps: 22, tail: 900 }, { shot: 'kinetic', text: 'Working' }] },
      { id: 'd2-2', stills: [6000], film: [{ shot: 'log', entries, stagger: 100, tamper: { cue: 'tamper', note: 'one character changed in o-actions.jsonl, on disk' } }] },
      { id: 'd2-3', film: [{ shot: 'claims', each: 2200, groups: [{ side: 'left', width: 620, claims: [{ verb: 'logs', k: 'log', frag: `${entries.length} entries, hash-chained` }, { verb: 'verifies', k: 'chain', frag: 'each entry hashes the one before' }] }] }] },
    ],
    facts: () => ({ job: job.job_id, tampered, chainAfter: backend.actionLog.verifyChain() }),
  };
};

films.d3 = async (backend, state) => {
  const d3 = story('d3');
  const curl = await proxyCurl(backend, d3);
  const py = await proxyPython(backend, d3);
  const api = backend.actionLog.tail(5000).filter((e) => /API: \/v1\/chat\/completions/.test(e.summary));
  state.logVisible = backend.actionLog.tail(5000).filter((e) => !/API: \/v1\/chat\/completions/.test(e.summary));
  return {
    title: d3.title, cues: {},
    files: [
      { id: 'd3-1', stills: [5000], film: [{ shot: 'quiet', text: d3.title, big: true, hold: 2600 }, { shot: 'settings', line: 'Settings → API proxy: a URL and a bearer token, loopback only', hold: 5000 }] },
      { id: 'd3-2', film: [{ shot: 'terminal', title: 'zsh', line: 'curl it like any chat API; each request is one claude -p run', cmd: curl.cmd, lines: sseLines(curl.lines), cps: 48, tail: 2600 }] },
      { id: 'd3-3', stills: [4200], film: [{ shot: 'terminal', title: 'ask_o.py', line: 'or the openai client; o-ai-code returns only the JSON', cmd: 'python3 ask_o.py', lines: py.code.map((l) => ({ text: '  ' + l })).concat([{ text: '' }], py.lines.map((l) => ({ text: l, hi: true }))), cps: 30, stagger: 120, tail: 3200 }] },
      { id: 'd3-4', film: [{ shot: 'log', entries: api, stagger: 300, at: 900 }] },
    ],
    facts: () => ({ curlFrames: curl.lines.length, python: py.lines }),
  };
};

films.d4 = async (backend, state) => {
  const d4 = story('d4');
  const cu = await computerRun(backend, d4.goal, d4.app);
  const cards = cuCards(cu);
  const first = cards.filter((c) => /^step/.test(c.k)), rest = cards.filter((c) => !/^step/.test(c.k)).map((c, i) => ({ ...c, at: i * 420 }));
  return {
    title: d4.title, cues: {},
    files: [
      { id: 'd4-1', film: [{ shot: 'quiet', text: d4.title, big: true, hold: 2600 }, { shot: 'composer', prompt: d4.prompt, cps: 22, tail: 900 }, { shot: 'kinetic', text: 'Working' }] },
      { id: 'd4-2', stills: [5000, 11000], film: [{ shot: 'cu', tree: treeLines(), cards: first, hold: 6500 }, { shot: 'cu', tree: treeLines(), cards: rest, hold: 7000 }] },
      { id: 'd4-3', film: [{ shot: 'runs', line: 'Runs shows the phases: context, plan, ground, execute, judge, heal, store', hold: 5000 }, { shot: 'claims', each: 2200, groups: [{ side: 'right', width: 620, claims: [{ verb: 'heals', k: 'heal', frag: 'one replacement step, judged' }, { verb: 'replays', k: 'cache', frag: `${cu.replayPlannerCalls} planner calls` }] }] }] },
    ],
    facts: () => ({ procedure: cu.state.procedure_id, attempt: cu.state.attempt, replayPlannerCalls: cu.replayPlannerCalls }),
  };
};

module.exports = { films, story, CU_TREE };
