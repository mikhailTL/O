#!/usr/bin/env node
'use strict';
// Transparent meter around the REAL Claude Code CLI for tools/e2e/real-run.js. Every byte of stdin, stdout,
// stderr and the exit code pass through unchanged; each `result` line (stream-json) or json result object is
// appended to $O_E2E_COST_LOG with its total_cost_usd, so the harness can enforce its cost cap.
//   O_E2E_REAL_CLAUDE  the real CLI (default /opt/node22/bin/claude)
//   O_E2E_MODEL        --model to add when the caller passed none (the engine's own calls pass none)
const { spawn } = require('child_process');
const fs = require('fs');
const REAL = process.env.O_E2E_REAL_CLAUDE || '/opt/node22/bin/claude';
const LOG = process.env.O_E2E_COST_LOG || '';
const args = process.argv.slice(2);
if (process.env.O_E2E_MODEL && !args.includes('--model') && args.includes('-p')) args.push('--model', process.env.O_E2E_MODEL);
const caller = args.includes('stream-json') ? 'agent' : (args.includes('-p') ? 'engine' : 'other');
const mcp = args[args.indexOf('--mcp-config') + 1] || '';
const tag = (mcp.match(/o-mcp-([A-Za-z0-9_-]+)\.json/) || [])[1] || '';
const child = spawn(REAL, args, { stdio: ['pipe', 'pipe', 'inherit'] });
process.stdin.pipe(child.stdin);
child.stdin.on('error', () => {});
let buf = '';
const record = (line) => {
  if (!LOG) return;
  let j; try { j = JSON.parse(line); } catch (_) { return; }
  if (!j || j.type !== 'result') return;
  const row = { ts: Date.now(), caller, tag, cwd: process.cwd(), cost: Number(j.total_cost_usd) || 0, durationMs: j.duration_ms, turns: j.num_turns,
    subtype: j.subtype, isError: !!j.is_error, models: Object.keys(j.modelUsage || {}), session: j.session_id };
  try { fs.appendFileSync(LOG, JSON.stringify(row) + '\n'); } catch (_) {}
};
child.stdout.on('data', (c) => {
  process.stdout.write(c);
  buf += c.toString('utf8');
  let i;
  while ((i = buf.indexOf('\n')) >= 0) { record(buf.slice(0, i).trim()); buf = buf.slice(i + 1); }
});
child.on('close', (code, signal) => {
  if (buf.trim()) record(buf.trim());
  if (signal) { try { process.kill(process.pid, signal); } catch (_) {} }
  process.exitCode = code == null ? 1 : code;
});
for (const s of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(s, () => { try { child.kill(s); } catch (_) {} });
