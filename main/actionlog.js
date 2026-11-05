'use strict';
// O's action log: an append-only, hash-chained record of everything O does.
// Each entry is one JSON line in `<userData>/o-actions.jsonl`; a human-readable twin goes to
// `<userData>/o-actions.log`. Entries from the engine are re-chained into this file via ingest().
// One writer: appends are synchronous under an in-process lock so two jobs' entries never interleave.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { EventEmitter } = require('events');

const KINDS = Object.freeze(['task', 'tool', 'result', 'policy', 'graph', 'cache', 'remediate', 'verify', 'settings', 'note']);
const GENESIS = '0'.repeat(64);
const MAX_SUMMARY = 500;
const MAX_DETAIL = 64 * 1024;
const CHUNK = 64 * 1024;

const clip = (s, n) => { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; };

/** JSON.stringify with every object's keys sorted (recursively), so the hash is stable. */
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  }
  return value === undefined ? 'null' : JSON.stringify(value);
}

function hashOf(entry) {
  const { hash, ...rest } = entry;
  return crypto.createHash('sha256').update(canonical(rest)).digest('hex');
}

function isoOf(ts) {
  return new Date(ts * 1000).toISOString().replace(/\.\d{3}Z$/, '');
}

/** Read the last complete line of a file, scanning backwards in chunks (works for any line length). */
function lastLine(file) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
    const size = fs.fstatSync(fd).size;
    if (size === 0) return '';
    const parts = [];
    let pos = size;
    // Skip trailing newlines first, then collect until the newline that precedes the last line.
    let skippingTail = true;
    while (pos > 0) {
      const len = Math.min(CHUNK, pos);
      pos -= len;
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, pos);
      let from = len;
      if (skippingTail) {
        while (from > 0 && (buf[from - 1] === 0x0a || buf[from - 1] === 0x0d)) from--;
        if (from === 0) continue;
        skippingTail = false;
      }
      const nl = buf.lastIndexOf(0x0a, from - 1);
      if (nl >= 0) {
        parts.unshift(buf.subarray(nl + 1, from));
        return Buffer.concat(parts).toString('utf8');
      }
      parts.unshift(buf.subarray(0, from));
    }
    return Buffer.concat(parts).toString('utf8');
  } catch (_) {
    return '';
  } finally {
    if (fd !== undefined) { try { fs.closeSync(fd); } catch {} }
  }
}

function sanitizeDetail(detail) {
  if (detail == null) return {};
  if (typeof detail !== 'object') return { value: clip(detail, MAX_DETAIL) };
  try {
    const s = JSON.stringify(detail);
    if (s.length <= MAX_DETAIL) return JSON.parse(s);
    return { truncated: true, text: s.slice(0, MAX_DETAIL) };
  } catch (_) {
    return { text: clip(String(detail), MAX_DETAIL) };
  }
}

class ActionLog extends EventEmitter {
  constructor(userDataDir) {
    super();
    this.dir = userDataDir;
    this.jsonl = path.join(userDataDir, 'o-actions.jsonl');
    this.text = path.join(userDataDir, 'o-actions.log');
    this.stateFile = path.join(userDataDir, 'o-actions.state.json');
    this._locked = false;
    this._queue = [];
    this.lastReadTs = 0;
    this.lastReadHash = '';
    this._unread = 0;
    try { fs.mkdirSync(userDataDir, { recursive: true }); } catch {}
    for (const f of [this.jsonl, this.text]) {
      try { if (!fs.existsSync(f)) fs.writeFileSync(f, ''); } catch (e) { console.error('[o] action log:', e.message); }
    }
    this.prev = GENESIS;
    const last = lastLine(this.jsonl);
    if (last) {
      try {
        const e = JSON.parse(last);
        if (typeof e.hash === 'string' && /^[0-9a-f]{64}$/.test(e.hash)) this.prev = e.hash;
      } catch (_) { /* damaged tail: chain restarts from genesis-prev; verifyChain will report it */ }
    }
    try {
      const st = JSON.parse(fs.readFileSync(this.stateFile, 'utf8'));
      if (typeof st.lastReadTs === 'number') this.lastReadTs = st.lastReadTs;
      if (typeof st.lastReadHash === 'string') this.lastReadHash = st.lastReadHash;
    } catch (_) { /* first run */ }
    this._unread = this._countUnread();
  }

  // ---------- writing ----------

  /** Append an app-side entry. Returns the full entry (with ts/iso/source/prev/hash). */
  record(input) {
    const e = this._build(input, 'app');
    return this._append(e);
  }

  /**
   * Append an entry produced by the engine. Its own hash is kept in detail.engine_hash and
   * its prev/hash are recomputed so it chains into the app file.
   */
  ingest(engineEntry) {
    const src = engineEntry && typeof engineEntry === 'object' ? engineEntry : {};
    const detail = sanitizeDetail(src.detail);
    if (typeof src.hash === 'string') detail.engine_hash = src.hash;
    if (typeof src.prev === 'string') detail.engine_prev = src.prev;
    const e = this._build({ kind: src.kind, summary: src.summary, detail, ts: src.ts, iso: src.iso }, 'engine');
    return this._append(e);
  }

  _build(input, source) {
    const i = input && typeof input === 'object' ? input : {};
    const kind = KINDS.includes(i.kind) ? i.kind : 'note';
    const ts = typeof i.ts === 'number' && Number.isFinite(i.ts) ? i.ts : Date.now() / 1000;
    const iso = typeof i.iso === 'string' && i.iso ? i.iso : isoOf(ts);
    const summary = clip(i.summary, MAX_SUMMARY).replace(/[\r\n]+/g, ' ');
    const detail = sanitizeDetail(i.detail);
    if (kind !== i.kind) detail.kind_given = i.kind == null ? null : String(i.kind);
    return { ts, iso, source, kind, summary, detail };
  }

  _append(entry) {
    // Synchronous writes make the lock almost redundant, but a re-entrant 'entry' listener
    // that records again must not interleave with the write in progress.
    if (this._locked) { this._queue.push(entry); return entry; }
    this._locked = true;
    try {
      this._write(entry);
      while (this._queue.length) this._write(this._queue.shift());
    } finally {
      this._locked = false;
    }
    return entry;
  }

  _write(entry) {
    entry.prev = this.prev;
    entry.hash = hashOf(entry);
    try {
      fs.appendFileSync(this.jsonl, JSON.stringify(entry) + '\n');
      this.prev = entry.hash;
    } catch (err) {
      console.error('[o] could not append action log:', err.message);
      return;
    }
    try { fs.appendFileSync(this.text, `${entry.iso}  [${entry.kind}]  ${entry.summary}\n`); } catch {}
    this._unread++;
    try { this.emit('entry', entry); } catch (err) { console.error('[o] action log listener:', err.message); }
  }

  // ---------- reading ----------

  _readAll() {
    let raw = '';
    try { raw = fs.readFileSync(this.jsonl, 'utf8'); } catch (_) { return []; }
    const out = [];
    for (const line of raw.split('\n')) {
      if (!line.trim()) continue;
      try { out.push(JSON.parse(line)); } catch (_) { out.push({ ts: 0, iso: '', source: 'app', kind: 'note', summary: '(unreadable line)', detail: {}, corrupt: true }); }
    }
    return out;
  }

  tail(n = 200) {
    const all = this._readAll();
    return n > 0 && all.length > n ? all.slice(all.length - n) : all;
  }

  since(ts) {
    const t = typeof ts === 'number' ? ts : 0;
    return this._readAll().filter((e) => typeof e.ts === 'number' && e.ts > t);
  }

  /** Entries appended since the last markRead(). */
  get unread() { return this._unread; }

  // The last-read position is the hash of the newest entry at markRead() time (exact, immune to
  // same-millisecond timestamps); lastReadTs is the fallback when that line is gone or changed.
  _countUnread() {
    const all = this._readAll();
    if (this.lastReadHash) {
      for (let i = all.length - 1; i >= 0; i--) if (all[i].hash === this.lastReadHash) return all.length - 1 - i;
    }
    return all.filter((e) => typeof e.ts === 'number' && e.ts > this.lastReadTs).length;
  }

  markRead() {
    this.lastReadTs = Date.now() / 1000;
    this.lastReadHash = this.prev === GENESIS ? '' : this.prev;
    this._unread = 0;
    try {
      fs.writeFileSync(this.stateFile, JSON.stringify({ lastReadTs: this.lastReadTs, lastReadHash: this.lastReadHash }));
    } catch (e) { console.error('[o] action log state:', e.message); }
    return 0;
  }

  paths() { return { jsonl: this.jsonl, text: this.text }; }

  /** Recompute every hash and prev link. Returns {ok, count, brokenAt?} (brokenAt = 0-based index). */
  verifyChain() {
    let raw = '';
    try { raw = fs.readFileSync(this.jsonl, 'utf8'); } catch (_) { return { ok: true, count: 0 }; }
    const lines = raw.split('\n').filter((l) => l.trim());
    let prev = GENESIS;
    for (let i = 0; i < lines.length; i++) {
      let e;
      try { e = JSON.parse(lines[i]); } catch (_) { return { ok: false, count: lines.length, brokenAt: i, reason: 'unparseable' }; }
      if (!e || typeof e !== 'object' || e.prev !== prev) return { ok: false, count: lines.length, brokenAt: i, reason: 'prev' };
      if (hashOf(e) !== e.hash) return { ok: false, count: lines.length, brokenAt: i, reason: 'hash' };
      prev = e.hash;
    }
    return { ok: true, count: lines.length };
  }
}

module.exports = { ActionLog, KINDS, hashOf, canonical };
