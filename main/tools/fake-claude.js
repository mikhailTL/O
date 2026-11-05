#!/usr/bin/env node
'use strict';
// Test double for `claude -p --output-format stream-json`: used by selftest.js.
// Reads the prompt from stdin, starts the `o` MCP server from --mcp-config, asks it to
// approve a Bash call over JSON-RPC (like Claude Code does), and prints recorded-style stream-json.

const fs = require('fs');
const { spawn } = require('child_process');

const argv = process.argv.slice(2);
if (argv[0] === '--version') { console.log('9.9.9 (Fake Claude)'); process.exit(0); }
if (argv[0] === 'auth') { console.log(JSON.stringify({ loggedIn: process.env.FAKE_LOGGED_OUT ? false : true })); process.exit(0); }

const flag = (n) => { const i = argv.indexOf(n); return i >= 0 ? argv[i + 1] : undefined; };
const out = (o) => process.stdout.write(JSON.stringify(o) + '\n');
const SID = flag('--resume') || (process.env.FAKE_NEW_SESSION === 'random' ? require('crypto').randomUUID() : 'fake-session-1');
// Test aid: log each run's arguments (used to check that chats resume their own session).
if (process.env.FAKE_ARGS_LOG) fs.appendFileSync(process.env.FAKE_ARGS_LOG, JSON.stringify({ resume: flag('--resume') || null, sid: SID }) + '\n');

let prompt = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => { prompt += d; });
process.stdin.on('end', main);

function rpc(child) {
  let buf = ''; const waiting = new Map(); let next = 1;
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (d) => {
    buf += d; let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1);
      const w = waiting.get(m.id); if (w) { waiting.delete(m.id); w(m); }
    }
  });
  return (method, params) => new Promise((res) => {
    const id = next++; waiting.set(id, res);
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
}

async function main() {
  if (process.env.FAKE_LOGGED_OUT) {
    out({ type: 'result', subtype: 'success', is_error: true, result: 'Invalid API key · Please run /login', session_id: SID });
    return;
  }
  if (flag('--resume') === 'stale') { process.stderr.write('No conversation found with session ID: stale\n'); process.exit(1); }

  // Proxy selftests: plain text frames, no MCP round trip. FAKE_TEXT sets the reply; FAKE_DELAY_MS slows it.
  if (process.env.FAKE_SIMPLE) {
    const text = process.env.FAKE_TEXT || `You said: ${prompt.trim()}`;
    const delay = Number(process.env.FAKE_DELAY_MS || 0);
    out({ type: 'system', subtype: 'init', session_id: SID, mcp_servers: [] });
    for (const piece of text.match(/.{1,24}/gs) || []) {
      out({ type: 'assistant', session_id: SID, parent_tool_use_id: null, message: { content: [{ type: 'text', text: piece }] } });
      if (delay) await new Promise((r) => setTimeout(r, delay));
    }
    if (process.env.FAKE_TOOL) out({ type: 'assistant', session_id: SID, parent_tool_use_id: null, message: { content: [{ type: 'tool_use', id: 'tp', name: 'Bash', input: { command: 'ls' } }] } });
    out({ type: 'result', subtype: 'success', is_error: false, result: text, session_id: SID, total_cost_usd: 0.002, duration_ms: 7, usage: { input_tokens: 10, output_tokens: 20 } });
    return;
  }
  const cfg = JSON.parse(fs.readFileSync(flag('--mcp-config'), 'utf8'));
  out({ type: 'system', subtype: 'init', session_id: SID, mcp_servers: [{ name: 'playwright', status: 'connected' }, { name: 'o', status: 'connected' }] });
  out({ type: 'assistant', session_id: SID, parent_tool_use_id: null, message: { content: [{ type: 'text', text: `You said: ${prompt.trim()}` }] } });
  out({ type: 'assistant', session_id: SID, parent_tool_use_id: null, message: { content: [{ type: 'tool_use', id: 't1', name: 'mcp__playwright__browser_navigate', input: { url: 'https://www.example.com/x' } }] } });
  out({ type: 'user', session_id: SID, message: { content: [{ type: 'tool_result', tool_use_id: 't1', content: 'ok' }] } });

  const o = cfg.mcpServers.o;
  const child = spawn(o.command, o.args, { env: { ...process.env, ...o.env }, stdio: ['pipe', 'pipe', 'inherit'] });
  const call = rpc(child);
  await call('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'fake', version: '0' } });
  const results = {};
  for (const [name, input] of [['mcp__playwright__browser_click', { element: 'Search' }], ['Bash', { command: 'ls ~', description: 'List home folder' }]]) {
    const r = await call('tools/call', { name: 'approve', arguments: { tool_name: name, input, tool_use_id: 'x' } });
    results[name] = JSON.parse(r.result.content[0].text);
  }
  child.stdin.end();

  if (process.env.FAKE_HANG) { await new Promise(() => {}); }

  out({ type: 'assistant', session_id: SID, parent_tool_use_id: null, message: { content: [{ type: 'tool_use', id: 't2', name: 'Bash', input: { command: 'ls ~', description: 'List home folder' } }] } });
  out({ type: 'assistant', session_id: SID, parent_tool_use_id: null, message: { content: [{ type: 'text', text: 'PERMS ' + JSON.stringify(results) }] } });
  out({ type: 'result', subtype: 'success', is_error: false, result: 'PERMS ' + JSON.stringify(results), session_id: SID, total_cost_usd: 0.01, duration_ms: 42 });
}
