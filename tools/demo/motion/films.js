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

/**
 * The consumer launch film (docs/videos/o-film.mp4): animated scenes, no take. Every scene illustrates a feature
 * that exists in the code (parallel tasks: main/tasks.js; learned preferences and replays: engine/o_engine/
 * learning.py; goals and checkpoints: main/goals.js + engine/o_engine/goals.py; away/brief: plan section J).
 * Task results, brands and people are examples; the second frame says so.
 */
films.film = async () => ({
  title: 'O — the computer that works while you do',
  cues: {},
  // short joins: under 6 frames of near-black between scenes (the open and the outro keep their own fades)
  edges: { fadeIn: 100, blur: 260 },
  files: [
    { id: '01-open', stills: [3600], edges: { fadeIn: 400, blur: 260 }, film: [
      { shot: 'brand', line: 'The computer that works while you do.', hold: 5600 },
      { shot: 'quiet', text: 'Illustrated with the real interface. Task results are examples.', hold: 2400 },
    ] },
    { id: '02-tasks', stills: [9800], film: [
      { shot: 'parallel', cps: 42, gap: 600, rate: 2.5, tail: 4000, tasks: [
        { prompt: 'Find the 3 best standing desks under $400 and compare them', title: 'Standing desks under $400',
          steps: [[0, 'Starting…'], [1, 'Opening 6 stores'], [4, 'Reading 41 reviews'], [10, 'Checking prices under $400'], [15, 'Comparing stability and noise'], [21, 'Building the table']] },
        { prompt: 'Rename every photo in Downloads by the date it was taken', title: 'Rename photos by date', doneAt: 12,
          steps: [[0, 'Starting…'], [1, 'Scanning Downloads'], [3, 'Reading dates from 312 photos'], [6, 'Renaming 312 photos'], [9, 'Sorting into 4 folders'], [12, 'Done']] },
        { prompt: 'Fill out the dentist intake form with my details', title: 'Dentist intake form',
          steps: [[0, 'Starting…'], [1, 'Opening the intake form'], [3, 'Typing your name'], [5, 'Typing your address'], [8, 'Adding your insurance']] },
      ] },
    ] },
    { id: '03-results', stills: [3400, 7900, 12300], film: [
      { shot: 'deskTable', title: 'Standing desks under $400', sub: '3 picks from 41 reviews across 6 stores', seconds: 38, hold: 4400,
        cols: ['Desk', 'Price', 'Height', 'Load', 'Noise', 'Warranty'],
        rows: [
          { name: 'Fernwood Lift 2', note: 'Dual motor, oak top', cells: ['$379', '25–50 in', '275 lb', '42 dB', '7 yr'], best: 'Best overall' },
          { name: 'Arlo Rise', note: 'Single motor, bamboo top', cells: ['$329', '27–47 in', '220 lb', '46 dB', '5 yr'] },
          { name: 'Kestrel Pro', note: 'Dual motor, laminate top', cells: ['$399', '24–50 in', '300 lb', '39 dB', '10 yr'] },
        ] },
      { shot: 'photos', title: 'Downloads', sub: '312 photos, renamed by the date they were taken', seconds: 12, flyAt: 1100,
        folders: [{ name: 'June 14, 2026', count: 48 }, { name: 'June 22, 2026', count: 117 }, { name: 'July 3, 2026', count: 86 }, { name: 'July 19, 2026', count: 61 }],
        renames: [['IMG_4821.HEIC', '2026-06-14 at 09.12.heic'], ['IMG_4822.HEIC', '2026-06-14 at 09.13.heic'], ['IMG_4907.HEIC', '2026-06-22 at 18.40.heic'], ['IMG_5133.JPG', '2026-07-03 at 11.05.jpg'], ['IMG_5260.HEIC', '2026-07-19 at 20.31.heic'], ['IMG_5261.HEIC', '2026-07-19 at 20.34.heic']] },
      { shot: 'form', title: 'Dentist intake form', sub: 'Filled in from your details', ready: 'Ready to submit', seconds: 21, cps: 46,
        formHead: 'New patient intake · Harborview Dental',
        fields: [['Full name', 'Maya Lindqvist'], ['Date of birth', '04/17/1991'], ['Address', '418 Harbor Lane, Apt 3'], ['Phone', '(555) 014-2290'], ['Insurance', 'Brightline Health · Member 88-2041'], ['Allergies', 'None']] },
    ] },
    { id: '04-apps', stills: [4300], film: [
      { shot: 'apps', label: 'A second display that only O uses', app: 'Lisbon trip — Slides', title1: 'Four days in Lisbon', sub1: 'June 12–15 · 4 people',
        title2: 'Day one · Alfama', bullets: ['Castelo de São Jorge at 10', 'Lunch near the Sé', 'Tram 28 back at sunset'],
        claim1: 'Works in your apps.', claim2: 'Never touches your mouse.', hold: 6000 },
    ] },
    { id: '05-learns', stills: [3800, 9000], film: [
      { shot: 'learn', correction: 'Make it shorter next time', learned: 'you like short summaries', nextLabel: 'Next week',
        long: 'This week in your newsletters: <b>The Morning Ledger</b> covered the new transit plan, rent changes in three neighborhoods and a long profile of the Saturday market. <b>Field Notes</b> walked through fall planting, soil tests and which bulbs to buy now. <b>Kitchen Table</b> had four weeknight recipes and a note on cast iron.',
        short: '<b>Ledger:</b> transit plan, rents up in 3 areas. <b>Field Notes:</b> plant bulbs now. <b>Kitchen Table:</b> 4 quick dinners.', tail: 2000 },
      { shot: 'replay', title: 'Sort this week’s receipts into my expenses sheet', firstLabel: 'First run', replayLabel: 'Replayed', first: 27, replay: 2, firstMs: 2400, claim: 'Learns once. Faster every time.', tail: 2400 },
    ] },
    { id: '06-goals', stills: [3400, 9600, 15200], film: [
      { shot: 'goalAsk', prompt: 'Grow a YouTube channel about desk setups to monetization', button: 'Make this a goal', note: 'Daily work, approvals only at checkpoints', tail: 1000 },
      { shot: 'goalView', eyebrow: 'Goal', title: 'Grow a desk-setup channel to monetization', days: 60, lapseMs: 4000, tail: 1300,
        footer: 'Checks in only when something needs your OK',
        next: [[1, 'next: pick a niche'], [3, 'next: script video one'], [5, 'next: publish video one'], [7, 'next: answer comments'], [9, 'next: your OK on video two']],
        metrics: [
          { name: 'Subscribers', max: 240, values: [0, 0, 3, 5, 41, 88, 120, 171, 214] },
          { name: 'Views', max: 9000, values: [0, 0, 40, 120, 1900, 3600, 5100, 6800, 8420] },
        ],
        log: [[2, 'Picked a niche: desks under $400'], [3, 'Scripted video one'], [5, 'Published video one'], [7, 'Answered 26 comments'], [9, 'Cut video two']] },
      { shot: 'approve', kind: 'Checkpoint · day 9', head: 'Approve video ‘My $400 desk setup’', sum: 'Cut, captioned, thumbnail made. Publishes Thursday at 6 pm.',
        items: [['Length', '8:42'], ['Title', 'My $400 desk setup, 6 months later'], ['Tags', 'desk setup, standing desk, home office']],
        done: '✓  Approved · publishes Thursday', eyebrow: 'Example goal', claim: 'Give it a goal. It works on it every day.', hold: 5400 },
    ] },
    { id: '07-away', stills: [5200, 9200], film: [
      { shot: 'away', here: 'Here with you', away: 'Away · 4 tasks running', hold: 5600 },
      { shot: 'sandboxes', caption: 'Each task runs sealed off from the rest of your computer.', msLabel: 'While you were away',
        pill: ['Away · 4 tasks running', 'Away · 3 tasks running', 'Away · 2 tasks running', 'Away · 1 task running', 'Away · all done'],
        tiles: [
          { title: 'Desk comparison', x: 140, y: 330, w: 600, h: 200, done: 2600, steps: ['Reading reviews', 'Checking prices', 'Building the table'] },
          { title: 'Photo cleanup', x: 770, y: 330, w: 410, h: 200, done: 1300, steps: ['Reading dates', 'Renaming 312 photos'] },
          { title: 'Intake form', x: 140, y: 560, w: 410, h: 200, done: 2000, steps: ['Typing your details', 'Checking fields'] },
          { title: 'Video two edit', x: 580, y: 560, w: 600, h: 200, done: 3500, steps: ['Cutting', 'Adding captions', 'Making the thumbnail'] },
        ],
        milestones: [['10:42', 'Photos renamed and sorted'], ['10:58', 'Intake form ready to submit'], ['11:15', 'Desk comparison finished'], ['11:40', 'Video two cut, needs your OK']], hold: 5400 },
    ] },
    { id: '08-return', stills: [5600], film: [
      { shot: 'ret', pill: ['Away · all done', 'Welcome back'], title: 'While you were out', when: 'Just now', hold: 9600,
        sentences: ['While you were out I finished the desk comparison and renamed 312 photos.', 'Your intake form is ready to submit and video two is cut and waiting for your OK.', 'Nothing needed you, so I didn’t interrupt.'] },
    ] },
    { id: '09-claims', stills: [1200, 7200], film: [
      { shot: 'claims5', each: 2000, groups: [
        { x: 200, y: 430, size: 120, fx: 1280, fy: 404, claims: [
          { verb: 'researches', frag: '<div class="fr"><b>Fernwood Lift 2</b><span>$379</span><span>42 dB</span></div><div class="fr"><b>Arlo Rise</b><span>$329</span><span>46 dB</span></div><div class="fr"><b>Kestrel Pro</b><span>$399</span><span>39 dB</span></div>' },
          { verb: 'builds', frag: '<div class="fr"><b>Lisbon trip</b><span>9 slides</span></div><div class="fr"><b>Intake form</b><span>ready</span></div>' },
          { verb: 'runs your apps', frag: '<span class="f-chip">Slides</span><span class="f-chip">Notes</span><span class="f-chip">Mail</span>' },
        ] },
        { x: 430, y: 600, size: 108, fx: 200, fy: 300, claims: [
          { verb: 'works while you’re away', frag: '<span class="f-pill" style="border:0;background:none;padding:0"><img src="/assets/o-avatar.svg" alt="">Away · 4 tasks running</span>' },
          { verb: 'learns you', frag: '<span class="f-learn" style="box-shadow:none"><span class="k">Learned</span>you like short summaries</span>' },
        ] },
      ] },
    ] },
    { id: '10-close', stills: [2400], edges: { fadeIn: 100, blur: 800 }, film: [
      { shot: 'outro', hold: 4600 },
    ] },
  ],
  facts: () => ({ illustrated: true, note: 'Animated illustration of real O features; task results, brands and people are examples.' }),
});
films.film.standalone = true;

/**
 * Launch film v4 (docs/videos/o-film4.mp4): the consumer film's story, copy and UI fragments (films.film is the
 * single source for them) with the cel FX energy layer (celfx.js, film4-shots.js) on the moments where O does
 * something no person could: ignition, parallel spawn, results landing, operating an app, replay, the goal line,
 * headless work while away, the claims and the close. The return beat stays calm. Pacing tightened slightly.
 */
films.film4 = async () => {
  const base = await films.film();
  const spec = (id, i, extra) => Object.assign({}, base.files.find((f) => f.id === id).film[i], extra || {});
  return {
    title: 'O — the computer that works while you do (v4)',
    cues: {},
    edges: base.edges,
    files: [
      { id: '01-open', stills: [1400, 1530, 2300, 3300, 5200], edges: { fadeIn: 200, blur: 260 }, film: [
        { shot: 'ignite', line: spec('01-open', 0).line, hold: 6200 },
        spec('01-open', 1, { hold: 2300 }),
      ] },
      { id: '02-tasks', stills: [2470, 8800], film: [spec('02-tasks', 0, { shot: 'parallel4', tail: 3300 })] },
      { id: '03-results', stills: [1530, 11600], film: [
        spec('03-results', 0, { shot: 'deskTable4', hold: 4000 }),
        spec('03-results', 1, { shot: 'photos4' }),
        spec('03-results', 2, { shot: 'form4', cps: 52 }),
      ] },
      { id: '04-apps', stills: [2000], film: [spec('04-apps', 0, { shot: 'apps4', hold: 5800 })] },
      { id: '05-learns', stills: [9000], film: [
        spec('05-learns', 0, { shot: 'learn4', tail: 1700 }),
        spec('05-learns', 1, { shot: 'replay4', replayMs: 200, tail: 2200 }),
      ] },
      { id: '06-goals', stills: [7000, 13000], film: [
        spec('06-goals', 0, { shot: 'goalAsk4', tail: 900 }),
        spec('06-goals', 1, { shot: 'goalView4', tail: 1300 }),
        spec('06-goals', 2, { shot: 'approve4', hold: 5000 }),
      ] },
      { id: '07-away', stills: [8000], film: [
        spec('07-away', 0, { hold: 5000 }),
        spec('07-away', 1, { shot: 'sandboxes4', hold: 5400 }),
      ] },
      { id: '08-return', stills: [4000], film: [spec('08-return', 0, { shot: 'ret4', hold: 9000 })] },
      { id: '09-claims', stills: [1200, 7200], film: [spec('09-claims', 0, { shot: 'claims4' })] },
      { id: '10-close', stills: [960, 1400, 2600], edges: { fadeIn: 100, blur: 800 }, film: [{ shot: 'close4', hold: 4800 }] },
    ],
    facts: () => Object.assign(base.facts(), { fx: 'Cel FX layer (celfx.js): procedural, seeded, drawn on twos/threes.' }),
  };
};
films.film4.standalone = true;

/**
 * The O AI cinematic trailer (docs/videos/o-trailer.mp4): reel-paced hard cuts and flips in the dark-cinema
 * vocabulary (a waking eye, many eyes cycling, ink pages, silhouettes, a figure dissolving into light, an orb of
 * words), with O's real surfaces (the island capsule, the command bar) and claims that are true of the code
 * (any app/browser: main/tools; self-improvement: engine/o_engine/evolve; no payments: main/link.js guard).
 * `sfx` is the sound cue list tools/demo/motion/trailer-audio.py renders against the finished picture.
 */
films.trailer = async () => {
  const B = (kind, d, cut, extra) => Object.assign({ kind, d, cut }, extra || {});
  // rhythm (from the reference reels): a hold of 1-3.5 s with one line and a slow push, then a burst of 2-4 frame
  // plates (the same subject flipping polarity, white flash frames, a solid colour), a whip-smear into the next hold.
  // Pictures are photographic plates (tools/demo/motion/plates), graded to one cinematic look and letterboxed.
  const EYES = [['eye-teal', 3], ['eye-teal', 2, 1], ['white', 1], ['eye-amber', 3], ['eye-flare', 2, 1], ['black', 1], ['iris', 3], ['eye-ring', 2], ['eye-tear', 2, 1], ['eye-amber', 2, 0, 1.6]];
  const SPACE = [['saturn', 3], ['saturn', 2, 1], ['white', 1], ['moon', 3], ['jupiter', 2], ['earth', 2, 1], ['black', 1], ['galaxy', 3], ['flare', 2], ['moon', 2, 1]];
  const THINGS = [['lens', 3], ['lens', 2, 1], ['white', 1], ['watch', 3], ['prism', 2, 0, 1.4, 'raw'], ['liquid', 2, 1], ['black', 1], ['tunnel', 3], ['nautilus', 2], ['candle', 2, 1]];
  const RINGS = [['eclipse', 2], ['eye-ring', 2], ['eclipse', 2, 1], ['iris', 2, 1], ['white', 1], ['eclipse', 2], ['tunnel', 3], ['eye-ring', 2, 1], ['nautilus', 2], ['eclipse', 2]];
  const files = [
    { id: 't1-wake', beats: [
      B('photo', 2600, 'hard', { plate: 'watch', from: { z: 1.0 }, to: { z: 1.22, x: 0.1 }, fadeIn: 900, grade: 'mono' }),
      B('photo', 3400, 'smear', { plate: 'eye-teal', from: { z: 1.0 }, to: { z: 1.3 }, text: 'Every AI waits to be told.', textAt: 1200 }),
      B('strobe', 1100, 'flash', { seq: EYES }),
    ] },
    { id: 't2-wait', beats: [
      B('photo', 1800, 'smear', { plate: 'lens', from: { z: 1.0 }, to: { z: 1.35 }, text: 'For a prompt.', textAt: 300 }),
      B('photo', 1400, 'flipY', { plate: 'liquid', from: { z: 1.1, x: -0.2 }, to: { z: 1.25, x: 0.2 }, text: 'Then another.', textAt: 100, linear: true }),
      B('strobe', 900, 'flash', { seq: THINGS }),
    ] },
    { id: 't3-light', beats: [
      B('photo', 2400, 'smear', { plate: 'moon', from: { z: 1.0, y: 0.1 }, to: { z: 1.18, y: -0.1 }, text: 'One of them stopped waiting.', textAt: 500 }),
      B('photo', 1700, 'flipX', { plate: 'earth', from: { z: 1.1, x: 0.15 }, to: { z: 1.3, x: -0.1 }, linear: true }),
      B('photo', 1400, 'flipX', { plate: 'galaxy', from: { z: 1.0 }, to: { z: 1.5 } }),
      B('strobe', 800, 'flash', { seq: SPACE }),
      B('eclipse', 3000, 'flash', { z0: 1.0, z1: 1.14, strobeMs: 0, ringAt: 300, words: [] }),
    ] },
    { id: 't4-work', beats: [
      B('bar', 3500, 'smear', { plate: 'liquid', prompt: 'Plan the Berlin week, book the trains, answer what I owe', cps: 46, openAt: 600, status: 'Working · 4 tasks' }),
      B('panels', 3900, 'flipX', { plate: 'tunnel', each: 380, text: 'In your apps. Any browser. Nothing to install.', textAt: 2500, textY: 880,
        items: [['Calendar', '6 meetings moved'], ['Rail', 'Berlin → Munich · Tue 08:34'], ['Mail', '4 replies drafted'], ['Notes', 'Itinerary written'], ['Slides', 'Pitch deck · 9 slides'], ['Messages', 'Sent “landed, see you at 7”']] }),
      B('strobe', 700, 'flash', { seq: [['prism', 2, 0, 1.3, 'raw'], ['lens', 2, 1], ['white', 1], ['nautilus', 2], ['tunnel', 2, 1], ['watch', 2], ['prism', 2, 1]] }),
    ] },
    { id: 't5-learn', beats: [
      B('claim', 1700, 'smear', { plate: 'nautilus', from: { z: 1.1 }, to: { z: 1.3, rot: 6 }, dim: 0.6, text: 'Then it grades its own work.' }),
      B('eclipse', 6000, 'flash', { z0: 1.5, z1: 1.2, strobeMs: 1200, ringAt: 1200, wordsAt: 1700, each: 420, words: ['faster.', 'quieter.', 'kinder.', 'surer.', 'sharper.', 'better.', 'better.', 'better.', 'yours.'], text: 'It rewrites its own skills. Keeps only what improved.', textAt: 1400 }),
    ] },
    { id: 't6-human', beats: [
      B('photo', 1800, 'smear', { plate: 'tunnel', from: { z: 1.0 }, to: { z: 1.6 }, text: 'Not a tool you drive.', textAt: 200 }),
      B('strobe', 1000, 'flash', { seq: RINGS }),
      B('pupil', 3400, 'flash', { plate: 'eye-ring', u: 0.665, v: 0.45, zMax: 6.5, zoomMs: 1500, ringAt: 1100, ringR: 150, text: 'The most human AI.', textAt: 1900 }),
    ] },
    { id: 't7-claims', beats: [
      B('claim', 800, 'flipY', { plate: 'lens', dim: 0.75, text: 'Works in every app.' }),
      B('claim', 800, 'flipY', { plate: 'tunnel', dim: 0.5, text: 'Every browser.', invert: true }),
      B('claim', 800, 'flipY', { plate: 'nautilus', dim: 0.65, text: 'Learns from itself.' }),
      B('claim', 800, 'flipY', { plate: 'galaxy', dim: 0.5, text: 'Sets its own goals.', invert: true }),
      B('strobe', 600, 'flash', { seq: [['eclipse', 2], ['white', 1], ['eclipse', 2, 1], ['black', 1], ['eclipse', 2], ['eclipse', 2, 1]] }),
      B('claim', 1800, 'flash', { text: 'Never spends your money.', sub: 'Payments, accounts and passwords stay yours.' }),
    ] },
    { id: 't8-close', beats: [
      B('lockup', 5200, 'flash', { name: 'O AI', text: 'The computer that works while you do.' }),
    ] },
  ];
  // sound cues at absolute ms: a whoosh on every flip, a flash hit on every flash, ticks under the eye cycles,
  // a riser into the dissolve, an impact on the ring, keyboard clicks under the typing, a final chord
  const sfx = []; let t = 0;
  for (const f of files) {
    let bt = t;
    for (const b of f.beats) {
      if (b.cut === 'flipX' || b.cut === 'flipY') sfx.push({ t: bt, k: 'flip' });
      if (b.cut === 'flash') sfx.push({ t: bt, k: 'flash' });
      if (b.cut === 'smear') sfx.push({ t: bt, k: 'whoosh' });
      if (b.kind === 'strobe') { let f = 0; const total = b.seq.reduce((a, q) => a + q[1], 0); for (let x = 0; x < b.d; ) { const q = b.seq[f % b.seq.length]; sfx.push({ t: bt + x, k: q[0] === 'white' ? 'flash' : 'tick' }); x += q[1] * 1000 / 30; f++; } }
      if (b.kind === 'eclipse') { const g = b.strobeMs || 0; if (g) { sfx.push({ t: bt, k: 'riser', d: g }); for (let x = 0; x < g; x += 133) sfx.push({ t: bt + x, k: 'tick' }); } sfx.push({ t: bt + (b.ringAt || 0), k: 'impact' }); if (b.words) for (let i = 0; i < b.words.length; i++) sfx.push({ t: bt + (b.wordsAt || 0) + i * (b.each || 420), k: 'tick' }); }
      if (b.kind === 'pupil') { sfx.push({ t: bt, k: 'riser', d: b.zoomMs || 1500 }); sfx.push({ t: bt + (b.ringAt || 1200), k: 'impact' }); }
      if (b.kind === 'photo' && b.plate === 'galaxy') sfx.push({ t: bt, k: 'riser', d: b.d + 800 });
      if (b.kind === 'bar') { const T = bt + (b.openAt || 700) + 500; for (let i = 0; i < (b.prompt || '').length; i++) sfx.push({ t: T + i * 1000 / b.cps, k: 'key' }); sfx.push({ t: bt + b.openAt, k: 'whoosh' }); }
      if (b.kind === 'panels') for (let i = 0; i < b.items.length; i++) sfx.push({ t: bt + i * b.each, k: 'flip' });
      if (b.kind === 'lockup') sfx.push({ t: bt, k: 'chord' });
      bt += b.d;
    }
    t = bt;
  }
  return {
    title: 'O AI — the most human AI',
    cues: {},
    edges: { fadeIn: 1, blur: 1 },
    files: files.map((f, i) => ({ id: f.id, stills: f.stills || [800, Math.round(f.beats.reduce((a, b) => a + b.d, 0) * 0.6)], edges: i === files.length - 1 ? { fadeIn: 1, blur: 900 } : undefined, film: [{ shot: 'cuts', beats: f.beats }] })),
    facts: () => ({ sfx, durationMs: t, note: 'Procedural cinematic trailer; the capsule, bar and panels illustrate real O surfaces; results are examples.' }),
  };
};
films.trailer.standalone = true;

module.exports = { films, story, CU_TREE };
