#!/usr/bin/env node
'use strict';
// O bridge: a tiny, dependency-free stdio MCP server (server name `o`, tool ids `mcp__o__<name>`).
//
// Tools:
//   approve            Claude Code permission prompt tool (--permission-prompt-tool mcp__o__approve).
//                      In `log` / `auto` mode it allows immediately (log mode also records a policy entry);
//                      in `ask` mode it forwards to O's localhost HTTP server and returns its decision as
//                      the text JSON Claude Code expects:
//                        {"behavior":"allow","updatedInput":{...}}  or  {"behavior":"deny","message":"..."}
//   see_screen         screenshot of the display under the cursor, fetched from O's HTTP server.
//   log_note           one-line note into O's action log.
//   provision          run the engine graph; blocks until the job is done and returns the final JobState.
//   cache_lookup       is there a cached script for a ProvisionRequest.
//   diagnose           structured diagnostic of a failed command.
//   verify_port        headless Playwright check of a local port.
//   workspace_context  ambient workspace profile (shell history, tree, config files, telemetry).
//   computer_run       plan + run + judge + store a desktop goal through the engine's computer-use graph.
//   computer_replay    replay a stored procedure (no model calls; bounded self-heal on a judged failure).
//   computer_find      resolve one accessibility target on the isolated display.
//   computer_tree      accessibility tree of a window (or the focused one).
//   mcp_tools          tools of every configured MCP server (GET /tools/list).
//   mcp_call           schema-validated call of one MCP tool (POST /tools/call).
//   script_run         run a python/bash/javascript/powershell script in the sandbox (POST /script/run).
//   swarm_run          objective → swarm job; blocks until done (POST /swarm/run + /jobs poll).
//   web_run            headless browser task on the isolated display; blocks until done (POST /web/run + /jobs poll).
//   ui_emit            push A2UI GenUI frames to O's panel (POST /ui on the app, same token as the log).
//
// Env:
//   O_PERM_URL, O_SCREEN_URL, O_PERM_TOKEN      app HTTP endpoints for approve / see_screen
//   O_PERMISSION_MODE                           'log' (default) | 'ask' | 'auto'
//   O_LOG_URL, O_LOG_TOKEN                      app action-log endpoint (POST /log)
//   O_UI_URL, O_SESSION_SURFACE                 app GenUI endpoint (POST /ui, token O_LOG_TOKEN) and this chat's surface id (chat:<id>)
//   O_ENGINE_URL, O_ENGINE_TOKEN                engine base URL (http://127.0.0.1:<port>) and token
//
// Protocol: JSON-RPC 2.0, one message per line on stdin/stdout. Logs go to stderr only.

const http = require('http');

const PERM_URL = process.env.O_PERM_URL;
const SCREEN_URL = process.env.O_SCREEN_URL;
const PERM_TOKEN = process.env.O_PERM_TOKEN || '';
const PERMISSION_MODE = (process.env.O_PERMISSION_MODE || 'log').toLowerCase();
const LOG_URL = process.env.O_LOG_URL;
const LOG_TOKEN = process.env.O_LOG_TOKEN || '';
const UI_URL = process.env.O_UI_URL;
const SESSION_SURFACE = process.env.O_SESSION_SURFACE || 'chat:default';
const ENGINE_URL = (process.env.O_ENGINE_URL || '').replace(/\/+$/, '');
const ENGINE_TOKEN = process.env.O_ENGINE_TOKEN || '';

const HTTP_TIMEOUT_MS = 30000;
const PROVISION_POLL_MS = 1000;
const PROVISION_MAX_MS = 60 * 60 * 1000;
const ENGINE_DOWN = 'The O engine is not running (see Settings → Engine)';

// ---------------------------------------------------------------- schemas

const SCRIPT_SCHEMA = {
  type: 'object',
  properties: {
    language: { type: 'string', enum: ['python', 'bash', 'javascript', 'powershell'], description: 'Interpreter the code runs under.' },
    code: { type: 'string', minLength: 1, maxLength: 65536, description: 'Source code to run (no markdown fences).' },
    target_directory: { type: 'string', minLength: 1, maxLength: 1024, description: 'Directory the script runs in (plain path).' },
    timeout_seconds: { type: 'integer', minimum: 1, maximum: 3600 },
  },
  required: ['language', 'code', 'target_directory', 'timeout_seconds'],
  additionalProperties: false,
};

const STEP_SCHEMA = {
  type: 'object',
  properties: {
    target_directory: { type: 'string', minLength: 1, maxLength: 1024, description: 'Directory the command runs in (plain path, no shell syntax).' },
    command_string: { type: 'string', minLength: 1, maxLength: 4000, description: 'Exact shell command. No prose, no list markers. Exactly one of command_string / script.' },
    script: { ...SCRIPT_SCHEMA, description: 'A multi-language script payload instead of command_string. Exactly one of the two.' },
    timeout_seconds: { type: 'integer', minimum: 1, maximum: 3600 },
  },
  required: ['target_directory', 'timeout_seconds'],
  additionalProperties: false,
};

const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    port: { type: 'integer', minimum: 1, maximum: 65535, description: 'Local TCP port to probe.' },
    path: { type: 'string', maxLength: 512, default: '/', description: 'URL path to request (default "/").' },
    expect_status: { type: 'integer', minimum: 100, maximum: 599, default: 200 },
    timeout_seconds: { type: 'integer', minimum: 1, maximum: 600, default: 30 },
  },
  required: ['port'],
  additionalProperties: false,
};

const PROVISION_SCHEMA = {
  type: 'object',
  properties: {
    label: { type: 'string', minLength: 1, maxLength: 200, description: 'Short human label for the job.' },
    steps: { type: 'array', minItems: 1, maxItems: 64, items: STEP_SCHEMA, description: 'Ordered commands to run.' },
    verify: VERIFY_SCHEMA,
    isolated_display: { type: 'boolean', default: false, description: 'Run under the isolated display (Xvfb :99 on Linux).' },
    risk: { type: 'string', enum: ['normal', 'high'], default: 'normal', description: '"high" runs every step in a disposable sandbox (E2B/Docker per settings) and destroys it afterwards.' },
  },
  required: ['label', 'steps'],
  additionalProperties: false,
};

const MCP_CALL_SCHEMA = {
  type: 'object',
  properties: {
    server: { type: 'string', minLength: 1, maxLength: 200, description: 'Configured MCP server name (see mcp_tools).' },
    tool: { type: 'string', minLength: 1, maxLength: 200, description: 'Tool name on that server.' },
    args: { type: 'object', description: 'Arguments; validated against the tool\'s input schema by the engine before dispatch.' },
  },
  required: ['server', 'tool', 'args'],
  additionalProperties: false,
};

const SWARM_RUN_SCHEMA = {
  type: 'object',
  properties: {
    objective: { type: 'string', minLength: 1, maxLength: 4000, description: 'What the environment should end up able to do, in one or two sentences.' },
    cwd: { type: 'string', maxLength: 1024, description: 'Working directory the crew profiles and provisions (default: the agent working directory).' },
  },
  required: ['objective'],
  additionalProperties: false,
};

const WEB_RUN_SCHEMA = {
  type: 'object',
  properties: {
    task: { type: 'string', minLength: 1, maxLength: 4000, description: 'What to do or find in the browser, in plain words.' },
    url: { type: 'string', minLength: 1, maxLength: 4096, description: 'Page to open first (optional).' },
  },
  required: ['task'],
  additionalProperties: false,
};

const TARGET_SCHEMA = {
  type: 'object',
  properties: {
    role: { type: 'string', maxLength: 64, description: 'Accessibility role (e.g. "push button", "table cell", "frame").' },
    name: { type: 'string', maxLength: 512, description: 'Accessible name; glob allowed (e.g. "*Calc*").' },
    text: { type: 'string', maxLength: 512, description: 'Visible text to match.' },
    index: { type: 'integer', minimum: 0, maximum: 10000, description: 'Which match when several (default 0).' },
    within: { type: 'string', maxLength: 64, description: 'Ref of an earlier `find` to search inside.' },
  },
  additionalProperties: false,
};

const COMPUTER_RUN_SCHEMA = {
  type: 'object',
  properties: {
    goal: { type: 'string', minLength: 1, maxLength: 2000, description: 'What to accomplish in the app, in one sentence.' },
    app: { type: 'string', maxLength: 200, description: 'Application id (e.g. "libreoffice-calc"). Used for procedure lookup and interface notes.' },
    candidates: { type: 'integer', minimum: 1, maximum: 8, description: 'Best-of-N candidates on separate isolated displays (Linux only; default 1).' },
  },
  required: ['goal'],
  additionalProperties: false,
};

const UI_EMIT_SCHEMA = {
  type: 'object',
  properties: {
    frames: {
      type: 'array', minItems: 1, maxItems: 64,
      items: { type: 'object', description: 'One A2UI frame: {"a2ui":"1","type":"begin|update|data|end","surface"?:"chat:<id>",...}.' },
      description: 'Ordered A2UI frames. begin {title} → update {components:[{id, component:{Type:{...}}}]} → data {path, value} → end {ok}. ' +
        'Components (closed set): Column, Row, Card, Text, Step, Tree, Progress, KeyValue, Button, Preview. No HTML or markdown.',
    },
    surface: { type: 'string', minLength: 1, maxLength: 200, description: 'Surface id for frames that have none (default: this chat\'s surface).' },
  },
  required: ['frames'],
  additionalProperties: false,
};

const TOOLS = [
  {
    name: 'approve',
    description: 'Permission prompt tool for Claude Code. Decides whether a tool call may run according to the O permission mode.',
    inputSchema: {
      type: 'object',
      properties: {
        tool_name: { type: 'string', description: 'Name of the tool asking for permission' },
        input: { type: 'object', description: 'Input the tool will run with' },
        tool_use_id: { type: 'string' },
      },
      required: ['tool_name', 'input'],
    additionalProperties: false,
    },
  },
  {
    name: 'see_screen',
    description: 'Take a screenshot of the user\'s screen (the display under their mouse cursor) and look at it. ' +
      'Use this whenever the user refers to what is on their screen ("this", "this page", "this question", "what I\'m looking at"), ' +
      'or when seeing their screen would help. It is a still image; call it again for a fresh view. O\'s own windows are hidden from it.',
    inputSchema: {
      type: 'object',
      properties: {
        display: { type: 'string', enum: ['cursor', 'primary'], description: 'Which display: the one under the cursor (default) or the primary one.' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'log_note',
    description: 'Write a one-line explanation of what you are doing or why into the O action log the user reads.',
    inputSchema: {
      type: 'object',
      properties: { text: { type: 'string', minLength: 1, maxLength: 2000 } },
      required: ['text'],
      additionalProperties: false,
    },
  },
  {
    name: 'provision',
    description: 'Run environment-setup commands through the O engine (validate → cache → execute → verify → remediate). ' +
      'Blocks until the job finishes and returns the final JobState. Use this instead of running setup commands yourself.',
    inputSchema: PROVISION_SCHEMA,
  },
  {
    name: 'cache_lookup',
    description: 'Check whether the O engine already has a cached, verified script for this ProvisionRequest.',
    inputSchema: {
      type: 'object',
      properties: { request: PROVISION_SCHEMA },
      required: ['request'],
      additionalProperties: false,
    },
  },
  {
    name: 'diagnose',
    description: 'Get a structured diagnostic (category, likely cause, suggested fix) for a failed command from the O engine.',
    inputSchema: {
      type: 'object',
      properties: {
        stderr: { type: 'string' },
        stdout: { type: 'string' },
        exit_code: { type: 'integer' },
        command: { type: 'string' },
        cwd: { type: 'string' },
      },
      required: ['stderr', 'stdout', 'exit_code', 'command', 'cwd'],
      additionalProperties: false,
    },
  },
  {
    name: 'verify_port',
    description: 'Check that a local HTTP service answers on a port using headless Playwright. Returns {ok, status, elapsed_ms, error?}.',
    inputSchema: VERIFY_SCHEMA,
  },
  {
    name: 'workspace_context',
    description: 'Ambient profile of the workspace: shell history tail, shallow directory tree, config files, recent screen telemetry. ' +
      'Call this before asking the user a question the environment can answer.',
    inputSchema: {
      type: 'object',
      properties: { cwd: { type: 'string', description: 'Directory to profile (default: the agent working directory).' } },
      additionalProperties: false,
    },
  },
  {
    name: 'computer_run',
    description: 'Drive a desktop application through the O engine (context → plan → ground → execute → judge → heal → store). ' +
      'Accessibility-API actions on an isolated display, every step judged and logged; a solved goal is stored as a replayable procedure. ' +
      'Blocks until the job finishes and returns the final JobState (phase, steps, procedure_id, scores).',
    inputSchema: COMPUTER_RUN_SCHEMA,
  },
  {
    name: 'computer_replay',
    description: 'Replay a stored computer-use procedure by id with no model calls; a judged failure triggers a bounded self-heal that writes a new version. ' +
      'Blocks until done and returns the final JobState.',
    inputSchema: {
      type: 'object',
      properties: { procedure_id: { type: 'string', minLength: 1, maxLength: 512 } },
      required: ['procedure_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'computer_find',
    description: 'Resolve one UI element on the isolated display by accessibility target (role/name/text/index). Returns {found, node}.',
    inputSchema: {
      type: 'object',
      properties: { target: TARGET_SCHEMA },
      required: ['target'],
      additionalProperties: false,
    },
  },
  {
    name: 'computer_tree',
    description: 'Accessibility tree of a window on the isolated display (the focused window when `window` is omitted). Returns {window, nodes}.',
    inputSchema: {
      type: 'object',
      properties: { window: { type: 'string', maxLength: 512, description: 'Window title glob to restrict the tree to.' } },
      additionalProperties: false,
    },
  },
  {
    name: 'mcp_tools',
    description: 'List the tools of every MCP server configured in O (Settings → MCP servers). Returns [{server, name, input_schema}].',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  },
  {
    name: 'mcp_call',
    description: 'Call one tool on a configured MCP server through the O engine. Arguments are schema-validated before dispatch; ' +
      'every call is logged. Returns {ok, content, is_error, elapsed_ms}.',
    inputSchema: MCP_CALL_SCHEMA,
  },
  {
    name: 'script_run',
    description: 'Run a python / bash / javascript / powershell script in one directory with a hard timeout through the O engine\'s ' +
      'sandboxed runner (no shell quoting needed). Returns the StepResult {exit_code, stdout, stderr, elapsed_ms, timed_out}.',
    inputSchema: SCRIPT_SCHEMA,
  },
  {
    name: 'swarm_run',
    description: 'Hand an objective to the O swarm (telemetry auditor → subprocess compiler → environment inspector). ' +
      'Blocks until the job finishes and returns the final JobState with the SwarmResult {profile, request, inspection}.',
    inputSchema: SWARM_RUN_SCHEMA,
  },
  {
    name: 'web_run',
    description: 'Do a task in a headless browser on the isolated display through the O engine (browser-use when installed, scripted ' +
      'Playwright DOM otherwise). Blocks until done and returns the final JobState with the WebResult {ok, final_text, steps, screenshots_sha, via}.',
    inputSchema: WEB_RUN_SCHEMA,
  },
  {
    name: 'ui_emit',
    description: 'Render a plan, progress or result in O\'s panel as declarative A2UI frames (preferred over prose for anything structured). ' +
      'Frames without a surface go to this chat\'s surface. Returns "accepted N, rejected M"; rejected frames are noted in the action log.',
    inputSchema: UI_EMIT_SCHEMA,
  },
];

// ---------------------------------------------------------------- helpers

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

function log(...a) {
  process.stderr.write('[o-bridge] ' + a.join(' ') + '\n');
}

function textResult(text, isError) {
  const r = { content: [{ type: 'text', text: String(text) }] };
  if (isError) r.isError = true;
  return r;
}

function request(method, urlStr, payload, token, timeoutMs) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(urlStr); } catch { return reject(new Error('O bridge is not configured.')); }
    const body = payload === undefined ? null : JSON.stringify(payload);
    const headers = { 'x-o-token': token || '' };
    if (body !== null) {
      headers['content-type'] = 'application/json';
      headers['content-length'] = Buffer.byteLength(body);
    }
    const req = http.request({
      hostname: u.hostname, port: u.port, path: u.pathname + u.search, method, headers,
      timeout: timeoutMs || HTTP_TIMEOUT_MS,
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        let parsed;
        try { parsed = raw ? JSON.parse(raw) : {}; } catch { parsed = undefined; }
        if (res.statusCode < 200 || res.statusCode >= 300) {
          const detail = parsed && (parsed.detail || parsed.error || parsed.message);
          return reject(new Error(`HTTP ${res.statusCode}: ${typeof detail === 'string' ? detail : raw.slice(0, 500)}`));
        }
        if (parsed === undefined) return reject(new Error('O returned an unreadable answer.'));
        resolve(parsed);
      });
    });
    req.on('timeout', () => req.destroy(new Error('timed out')));
    req.on('error', (e) => reject(new Error('O is not reachable: ' + e.message)));
    req.end(body === null ? undefined : body);
  });
}

function engine(method, path, payload, timeoutMs) {
  return request(method, ENGINE_URL + path, payload, ENGINE_TOKEN, timeoutMs);
}

function postLog(entry) {
  if (!LOG_URL) return Promise.resolve(null);
  return request('POST', LOG_URL, entry, LOG_TOKEN, 5000);
}

function fireAndForgetLog(entry) {
  postLog(entry).catch((e) => log('log post failed:', e.message));
}

function shortInput(input, n = 200) {
  let s;
  try { s = JSON.stringify(input === undefined ? {} : input); } catch { s = String(input); }
  return s.length > n ? s.slice(0, n - 3) + '...' : s;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function pretty(obj) {
  try { return JSON.stringify(obj, null, 2); } catch { return String(obj); }
}

function summarizeJob(job) {
  const label = (job && job.label) || 'job';
  const steps = Array.isArray(job && job.steps) ? job.steps : [];
  if (job && job.ok) {
    const n = steps.length;
    const proc = job.procedure_id ? ` → procedure ${job.procedure_id}` : '';
    return `✓ ${label}: ${n} step${n === 1 ? '' : 's'}, ${job.cached ? 'cached replay' : 'fresh run'}${proc}`;
  }
  const failed = steps.find((s) => s && typeof s.exit_code === 'number' && s.exit_code !== 0);
  const at = failed ? ` at step ${typeof failed.index === 'number' ? failed.index + 1 : steps.indexOf(failed) + 1}` : '';
  const attempts = job && typeof job.attempt === 'number' ? ` after ${job.attempt} attempt${job.attempt === 1 ? '' : 's'}` : '';
  const err = (job && job.error) || (job && job.verify && job.verify.error) || 'unknown error';
  return `✗ ${label} failed${at}${attempts}: ${err}`;
}

// ---------------------------------------------------------------- tools

async function seeScreen(args) {
  if (!SCREEN_URL) return textResult('Screen access is not set up in this O.', true);
  try {
    const r = await request('POST', SCREEN_URL, args || {}, PERM_TOKEN);
    if (!r || r.error || !r.data) return textResult((r && r.error) || 'Could not capture the screen.', true);
    const info = `Screenshot of the user's screen (${r.width}x${r.height}${r.displays > 1 ? `, display ${r.displayIndex + 1} of ${r.displays}` : ''}).` +
      (r.cursor ? ` Their mouse cursor is at about (${r.cursor.x}, ${r.cursor.y}) in this image.` : '') +
      (r.note ? ' ' + r.note : '');
    return { content: [{ type: 'image', data: r.data, mimeType: r.mimeType || 'image/jpeg' }, { type: 'text', text: info }] };
  } catch (e) {
    return textResult(e.message, true);
  }
}

async function approve(args) {
  const input = args.input || {};
  if (PERMISSION_MODE === 'log' || PERMISSION_MODE === 'auto') {
    if (PERMISSION_MODE === 'log') {
      const toolName = args.tool_name || 'tool';
      fireAndForgetLog({
        kind: 'policy',
        summary: `allowed ${toolName}: ${shortInput(input, 160).replace(/\s*\n\s*/g, ' ')}`,
        detail: { toolName, input: shortInput(input, 2000), tool_use_id: args.tool_use_id || null, mode: 'log' },
      });
    }
    return { behavior: 'allow', updatedInput: input };
  }
  const deny = (message) => ({ behavior: 'deny', message });
  if (!PERM_URL) return deny('O permission bridge is not configured.');
  try {
    const d = await request('POST', PERM_URL, {
      tool_name: args.tool_name,
      input,
      tool_use_id: args.tool_use_id || null,
    }, PERM_TOKEN, 90000);
    if (d && d.behavior === 'allow') return { behavior: 'allow', updatedInput: d.updatedInput || input };
    return deny((d && d.message) || 'The user denied this action.');
  } catch (e) {
    log('approve request failed:', e.message);
    return deny('O is not reachable to approve this.');
  }
}

async function logNote(args) {
  const text = String((args && args.text) || '').trim();
  if (!text) return textResult('log_note needs non-empty text.', true);
  if (!LOG_URL) return textResult('The O action log is not reachable from this bridge.', true);
  try {
    await postLog({ kind: 'note', summary: text });
    return textResult('noted');
  } catch (e) {
    return textResult('Could not write the note: ' + e.message, true);
  }
}

async function uiEmit(args) {
  const frames = args && Array.isArray(args.frames) ? args.frames : null;
  if (!frames || !frames.length) return textResult('ui_emit needs a non-empty frames array.', true);
  if (frames.length > 64) return textResult('ui_emit takes at most 64 frames per call.', true);
  if (!UI_URL) return textResult('O\'s GenUI endpoint is not reachable from this bridge.', true);
  const surface = args.surface ? String(args.surface) : SESSION_SURFACE;
  const out = [];
  for (const f of frames) {
    if (!f || typeof f !== 'object' || Array.isArray(f)) return textResult('every frame must be an object.', true);
    out.push(typeof f.surface === 'string' && f.surface ? f : { ...f, surface });
  }
  try {
    const r = await request('POST', UI_URL, { frames: out }, LOG_TOKEN, 10000);
    const accepted = Number(r && r.accepted) || 0;
    const rejected = Number(r && r.rejected) || 0;
    const text = `accepted ${accepted}, rejected ${rejected}` + (rejected ? ' (see the action log for why)' : '');
    return textResult(text, accepted === 0);
  } catch (e) {
    return textResult('Could not send the UI frames: ' + e.message, true);
  }
}

async function runJob(path, args, what) {
  if (!ENGINE_URL) return textResult(ENGINE_DOWN, true);
  let jobId;
  try {
    const r = await engine('POST', path, args);
    jobId = r && r.job_id;
    if (!jobId) return textResult('The engine did not return a job id: ' + pretty(r), true);
  } catch (e) {
    return textResult(`${what} failed: ` + e.message, true);
  }
  const deadline = Date.now() + PROVISION_MAX_MS;
  let lastErr = null;
  let failures = 0;
  while (Date.now() < deadline) {
    await sleep(PROVISION_POLL_MS);
    let job;
    try {
      job = await engine('GET', `/jobs/${encodeURIComponent(jobId)}`);
      failures = 0;
    } catch (e) {
      lastErr = e;
      failures += 1;
      if (failures >= 10) return textResult(`Lost contact with the engine while waiting for job ${jobId}: ${e.message}`, true);
      continue;
    }
    if (job && job.done) {
      return textResult(summarizeJob(job) + '\n' + pretty(job), !job.ok);
    }
  }
  return textResult(`Job ${jobId} did not finish within 60 minutes` + (lastErr ? ` (last error: ${lastErr.message})` : ''), true);
}

function provision(args) {
  return runJob('/provision', args, 'provision');
}

function computerRun(args) {
  return runJob('/computer/run', args, 'computer_run');
}

function computerReplay(args) {
  return runJob('/computer/replay', { procedure_id: args && args.procedure_id }, 'computer_replay');
}

async function engineGet(path) {
  if (!ENGINE_URL) return textResult(ENGINE_DOWN, true);
  try {
    const r = await engine('GET', path);
    return textResult(pretty(r));
  } catch (e) {
    return textResult(`${path} failed: ${e.message}`, true);
  }
}

async function enginePost(path, payload, timeoutMs) {
  if (!ENGINE_URL) return textResult(ENGINE_DOWN, true);
  try {
    const r = await engine('POST', path, payload, timeoutMs);
    return textResult(pretty(r));
  } catch (e) {
    return textResult(`${path} failed: ${e.message}`, true);
  }
}

function renderContext(p) {
  const lines = [];
  lines.push(`Workspace: ${p.cwd || '?'} (${p.platform || '?'})`);
  const hist = Array.isArray(p.shell_history) ? p.shell_history.slice(-15) : [];
  lines.push('', `Shell history (last ${hist.length}):`);
  for (const h of hist) lines.push('  $ ' + String(h));
  if (!hist.length) lines.push('  (none)');
  const kids = p.tree && Array.isArray(p.tree.children) ? p.tree.children : [];
  lines.push('', 'Top level:');
  for (const c of kids.slice(0, 60)) lines.push('  ' + (c.type === 'dir' ? c.name + '/' : c.name));
  if (kids.length > 60) lines.push(`  ... ${kids.length - 60} more`);
  if (!kids.length) lines.push('  (empty or unreadable)');
  const cfg = Array.isArray(p.config_files) ? p.config_files : [];
  lines.push('', 'Config files:');
  for (const c of cfg) {
    const head = String(c.head || '').split('\n').slice(0, 3).join(' | ');
    lines.push(`  ${c.path}` + (head ? ` — ${head.slice(0, 120)}` : ''));
  }
  if (!cfg.length) lines.push('  (none)');
  const tel = Array.isArray(p.telemetry) ? p.telemetry.slice(-10) : [];
  lines.push('', `Recent telemetry (${tel.length}):`);
  for (const t of tel) {
    const when = typeof t.ts === 'number' ? new Date(t.ts * 1000).toISOString().slice(11, 19) : '';
    lines.push(`  ${when} [${t.app || ''}] ${t.window || ''} ${String(t.text || '').replace(/\s+/g, ' ').slice(0, 120)}`.trimEnd());
  }
  if (!tel.length) lines.push('  (none)');
  return lines.join('\n');
}

async function workspaceContext(args) {
  if (!ENGINE_URL) return textResult(ENGINE_DOWN, true);
  const cwd = args && args.cwd ? String(args.cwd) : (process.env.O_AGENT_CWD || process.cwd());
  try {
    const p = await engine('GET', '/context?cwd=' + encodeURIComponent(cwd));
    return textResult(renderContext(p || {}) + '\n\n' + pretty(p));
  } catch (e) {
    return textResult('workspace_context failed: ' + e.message, true);
  }
}

async function callTool(name, args) {
  switch (name) {
    case 'approve':
      return textResult(JSON.stringify(await approve(args)));
    case 'see_screen':
      return seeScreen(args);
    case 'log_note':
      return logNote(args);
    case 'provision':
      return provision(args);
    case 'cache_lookup':
      return enginePost('/cache/lookup', { request: args.request });
    case 'diagnose':
      return enginePost('/diagnose', args);
    case 'verify_port':
      return enginePost('/verify', args);
    case 'workspace_context':
      return workspaceContext(args);
    case 'computer_run':
      return computerRun(args);
    case 'computer_replay':
      return computerReplay(args);
    case 'computer_find':
      return enginePost('/computer/find', { target: args.target });
    case 'computer_tree':
      return engineGet('/computer/tree' + (args && args.window ? '?window=' + encodeURIComponent(String(args.window)) : ''));
    case 'mcp_tools':
      return engineGet('/tools/list');
    case 'mcp_call':
      return enginePost('/tools/call', { server: args.server, tool: args.tool, args: args.args || {} });
    case 'script_run':
      return enginePost('/script/run', {
        language: args.language, code: args.code, target_directory: args.target_directory, timeout_seconds: args.timeout_seconds,
      }, (Number(args.timeout_seconds) || 60) * 1000 + 10000);
    case 'swarm_run':
      return runJob('/swarm/run', {
        objective: args.objective, cwd: args.cwd ? String(args.cwd) : (process.env.O_AGENT_CWD || process.cwd()),
      }, 'swarm_run');
    case 'web_run':
      return runJob('/web/run', args.url ? { task: args.task, url: args.url } : { task: args.task }, 'web_run');
    case 'ui_emit':
      return uiEmit(args);
    default:
      return null;
  }
}

// ---------------------------------------------------------------- JSON-RPC

async function handle(msg) {
  const { id, method, params } = msg;
  const isRequest = id !== undefined && id !== null;
  switch (method) {
    case 'initialize':
      return isRequest && send({
        jsonrpc: '2.0', id,
        result: {
          protocolVersion: (params && params.protocolVersion) || '2024-11-05',
          capabilities: { tools: {} },
          serverInfo: { name: 'o', version: '6.0.0' },
        },
      });
    case 'ping':
      return isRequest && send({ jsonrpc: '2.0', id, result: {} });
    case 'tools/list':
      return isRequest && send({ jsonrpc: '2.0', id, result: { tools: TOOLS } });
    case 'tools/call': {
      const name = params && params.name;
      const args = (params && params.arguments) || {};
      let result;
      try {
        result = await callTool(name, args);
      } catch (e) {
        log('tool error:', name, e && e.message);
        result = textResult(`${name} failed: ${e && e.message}`, true);
      }
      if (result === null) {
        return isRequest && send({ jsonrpc: '2.0', id, error: { code: -32602, message: `Unknown tool: ${name}` } });
      }
      return isRequest && send({ jsonrpc: '2.0', id, result });
    }
    default:
      if (isRequest) send({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } });
  }
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch {
      send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
      continue;
    }
    handle(msg).catch((e) => {
      log('handler error:', e && e.message);
      if (msg.id !== undefined && msg.id !== null) send({ jsonrpc: '2.0', id: msg.id, error: { code: -32603, message: String(e && e.message) } });
    });
  }
});
process.stdin.on('end', () => process.exit(0));
process.on('uncaughtException', (e) => log('uncaught:', e && e.stack || e));
process.on('unhandledRejection', (e) => log('unhandled rejection:', e && e.message || e));
