'use strict';
// Stream filter that lets only the first balanced top-level JSON value through.
// Node port of engine/o_engine/tokenfilter.py with the same semantics: string and
// escape state are tracked so brackets inside strings don't affect depth; everything
// before the opening [ or { and after its matching close is discarded.

const OPENERS = { '[': ']', '{': '}' };
const CLOSERS = new Set([']', '}']);

class TokenFilter {
  constructor() {
    this.started = false;
    this.done = false;
    this._depth = 0;
    this._inString = false;
    this._escape = false;
    this._buf = [];
  }

  get text() { return this._buf.join(''); }

  /** Feed one token; returns the characters that belong to the JSON value. */
  feed(token) {
    if (this.done || !token) return '';
    const out = [];
    for (const ch of String(token)) {
      if (!this.started) {
        if (ch in OPENERS) { this.started = true; this._depth = 1; out.push(ch); }
        continue;
      }
      out.push(ch);
      if (this._inString) {
        if (this._escape) this._escape = false;
        else if (ch === '\\') this._escape = true;
        else if (ch === '"') this._inString = false;
        continue;
      }
      if (ch === '"') this._inString = true;
      else if (ch in OPENERS) this._depth += 1;
      else if (CLOSERS.has(ch)) {
        this._depth -= 1;
        if (this._depth === 0) { this.done = true; break; }
      }
    }
    const chunk = out.join('');
    if (chunk) this._buf.push(chunk);
    return chunk;
  }

  filterStream(tokens) {
    for (const t of tokens) { this.feed(t); if (this.done) break; }
    return this.text;
  }
}

/** One-shot: the first balanced JSON value inside `text`, or '' when there is none. */
function filterText(text) {
  const f = new TokenFilter();
  f.feed(text);
  return f.done ? f.text : '';
}

module.exports = { TokenFilter, filterText };
