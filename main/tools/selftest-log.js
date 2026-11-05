'use strict';
// Self-test for main/actionlog.js. Run: node main/tools/selftest-log.js
// Exercises record / ingest / verifyChain / tamper detection / unread + markRead in a temp dir.

const fs = require('fs');
const os = require('os');
const path = require('path');
const { ActionLog, hashOf } = require('../actionlog');

let failures = 0;
function check(cond, what) {
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${what}`);
  if (!cond) failures++;
}

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'o-selftest-log-'));
console.log(`dir ${dir}`);

const log = new ActionLog(dir);
const seen = [];
log.on('entry', (e) => seen.push(e));

const a = log.record({ kind: 'task', summary: 'first', detail: { n: 1 } });
const b = log.record({ kind: 'tool', summary: 'second', detail: { name: 'Bash', z: 1, a: 2 } });
const c = log.record({ kind: 'bogus', summary: 'third with\nnewline' });

check(a.source === 'app' && typeof a.ts === 'number' && /^\d{4}-\d{2}-\d{2}T/.test(a.iso), 'record fills ts/iso/source');
check(a.prev === '0'.repeat(64), 'first entry chains from genesis');
check(b.prev === a.hash && c.prev === b.hash, 'prev links follow');
check(a.hash === hashOf(a), 'hash matches recomputation');
check(c.kind === 'note' && c.detail.kind_given === 'bogus', 'unknown kind coerced to note');
check(!/\n/.test(c.summary), 'summary has no newline');
check(seen.length === 3, "'entry' emitted per record");

const engineEntry = {
  ts: 1730000000.123, iso: '2026-11-04T22:30:00', source: 'engine', kind: 'graph',
  summary: 'context -> validate', detail: { job: 'j1' }, prev: 'ab'.repeat(32), hash: 'cd'.repeat(32),
};
const d = log.ingest(engineEntry);
check(d.source === 'engine' && d.prev === c.hash && d.detail.engine_hash === 'cd'.repeat(32), 'ingest re-chains and keeps engine hash');
check(d.ts === engineEntry.ts && d.iso === engineEntry.iso, 'ingest keeps engine timestamps');

let v = log.verifyChain();
check(v.ok === true && v.count === 4, `verifyChain ok (${JSON.stringify(v)})`);

const tail = log.tail(2);
check(tail.length === 2 && tail[1].hash === d.hash, 'tail(2) returns the last two');
const after = log.since(a.ts);
check(log.since(0).length === 4 && after.every((e) => e.ts > a.ts) && !after.some((e) => e.hash === d.hash), 'since(ts) filters by timestamp (old engine ts excluded)');

const { jsonl, text } = log.paths();
const textLines = fs.readFileSync(text, 'utf8').trim().split('\n');
check(textLines.length === 4 && textLines[0] === `${a.iso}  [task]  first`, 'text twin format');

// Reopen: prev must be read back from the last line (large last entry exercises the chunked scan).
const big = log.record({ kind: 'note', summary: 'big', detail: { blob: 'x'.repeat(300000) } });
const reopened = new ActionLog(dir);
const e = reopened.record({ kind: 'note', summary: 'after reopen' });
check(e.prev === big.hash, 'reopened log continues the chain from a huge last entry');
check(reopened.verifyChain().ok, 'chain still ok after reopen');

// Unread / markRead.
check(reopened.unread === 6, `unread counts everything before markRead (${reopened.unread})`);
reopened.markRead();
check(reopened.unread === 0, 'markRead resets unread');
reopened.record({ kind: 'note', summary: 'one more' });
check(reopened.unread === 1, 'unread increments after markRead');
const again = new ActionLog(dir);
check(again.unread === 1, 'unread persists across reopen via state file');

// Tamper with line index 1 (entry b) and expect brokenAt 1.
const lines = fs.readFileSync(jsonl, 'utf8').split('\n');
lines[1] = lines[1].replace('"second"', '"SECOND"');
fs.writeFileSync(jsonl, lines.join('\n'));
v = again.verifyChain();
check(v.ok === false && v.brokenAt === 1, `tampered line reported (${JSON.stringify(v)})`);

try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
console.log(failures ? `${failures} FAILED` : 'all passed');
process.exit(failures ? 1 : 0);
