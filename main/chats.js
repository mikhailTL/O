'use strict';
// O's chat history: every chat keeps its messages (for showing again) and the Claude Code
// session it belongs to (so O remembers the conversation when you go back to it).
// Stored as JSON in the app-data folder. You create, rename and delete chats yourself.

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const MAX_MESSAGES = 600;      // per chat, oldest dropped first
const MAX_TEXT = 20000;        // per message
const MAX_CHATS = 500;

const clip = (s, n) => { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const newId = () => crypto.randomBytes(8).toString('hex');

function titleFrom(text) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  return t ? clip(t, 48) : 'New chat';
}

class ChatStore {
  constructor(userDataDir) {
    this.file = path.join(userDataDir, 'o-chats.json');
    this.data = { currentId: null, chats: [] };
    try {
      const raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      if (raw && Array.isArray(raw.chats)) this.data = { currentId: raw.currentId || null, chats: raw.chats.filter((c) => c && c.id) };
    } catch (_) { /* first run */ }
    if (!this.current()) this.create();
    this._timer = null;
  }

  // ---------- persistence ----------
  save(now = false) {
    clearTimeout(this._timer);
    const write = () => {
      try {
        fs.mkdirSync(path.dirname(this.file), { recursive: true });
        const tmp = this.file + '.tmp';
        fs.writeFileSync(tmp, JSON.stringify(this.data));
        fs.renameSync(tmp, this.file);
      } catch (e) { console.error('[o] could not save chats:', e.message); }
    };
    if (now) write(); else this._timer = setTimeout(write, 400);
  }
  flush() { this.save(true); }

  // ---------- queries ----------
  current() { return this.data.chats.find((c) => c.id === this.data.currentId) || null; }
  get(id) { return this.data.chats.find((c) => c.id === id) || null; }
  list() {
    return [...this.data.chats]
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .map((c) => ({ id: c.id, title: c.title, updatedAt: c.updatedAt, createdAt: c.createdAt, count: c.messages.filter((m) => m.role === 'user').length, current: c.id === this.data.currentId }));
  }

  // ---------- changes ----------
  /** Start a new, empty chat (reuses the current one if it's still empty). */
  create() {
    const cur = this.current();
    if (cur && cur.messages.length === 0 && !cur.sessionId) return cur;
    const now = Date.now();
    const chat = { id: newId(), title: 'New chat', titled: false, sessionId: null, createdAt: now, updatedAt: now, messages: [] };
    this.data.chats.push(chat);
    if (this.data.chats.length > MAX_CHATS) {
      this.data.chats.sort((a, b) => b.updatedAt - a.updatedAt);
      this.data.chats.length = MAX_CHATS;
    }
    this.data.currentId = chat.id;
    this.save();
    return chat;
  }

  open(id) {
    const chat = this.get(id);
    if (!chat) return null;
    this.data.currentId = id;
    this.save();
    return chat;
  }

  rename(id, title) {
    const chat = this.get(id);
    if (!chat) return null;
    chat.title = clip(String(title || '').replace(/\s+/g, ' ').trim() || 'Untitled chat', 80);
    chat.titled = true;
    this.save();
    return chat;
  }

  /** Delete chats; returns the Claude Code session ids they used (so their transcripts can go too). */
  remove(ids) {
    const set = new Set([].concat(ids));
    const sessions = this.data.chats.filter((c) => set.has(c.id)).flatMap((c) => c.sessions || (c.sessionId ? [c.sessionId] : []));
    this.data.chats = this.data.chats.filter((c) => !set.has(c.id));
    if (!this.current()) {
      const next = [...this.data.chats].sort((a, b) => b.updatedAt - a.updatedAt)[0];
      if (next) this.data.currentId = next.id; else { this.data.currentId = null; this.create(); }
    }
    this.save(true);
    return [...new Set(sessions)];
  }
  removeAll() { return this.remove(this.data.chats.map((c) => c.id)); }

  setSession(sessionId) {
    const chat = this.current();
    if (!chat || !sessionId) return;
    chat.sessionId = sessionId;
    chat.sessions = [...new Set([...(chat.sessions || []), sessionId])];
    this.save();
  }

  // ---------- tasks: every task is a chat with the task's id (main/tasks.js) ----------
  /** The chat for a task, created (without switching the current chat) if it does not exist yet. */
  ensureTask(id, title) {
    let chat = this.get(id);
    if (chat) return chat;
    const now = Date.now();
    chat = { id: String(id), title: title ? clip(String(title).replace(/\s+/g, ' ').trim(), 80) : 'New task', titled: !!title, sessionId: null, createdAt: now, updatedAt: now, messages: [] };
    this.data.chats.push(chat);
    if (this.data.chats.length > MAX_CHATS) {
      this.data.chats.sort((a, b) => b.updatedAt - a.updatedAt);
      this.data.chats.length = MAX_CHATS;
    }
    this.save();
    return chat;
  }

  addUserTo(id, text) {
    const chat = this.ensureTask(id);
    this._push({ role: 'user', text }, chat);
    if (!chat.titled && chat.messages.filter((m) => m.role === 'user').length === 1) chat.title = titleFrom(text);
  }

  /** Record an agent event into the task's own chat (ev.taskId), else into the current chat. */
  recordTo(id, ev) { this.record(ev, id ? this.get(id) : null); }

  setSessionFor(id, sessionId) {
    const chat = this.get(id);
    if (!chat || !sessionId) return;
    chat.sessionId = sessionId;
    chat.sessions = [...new Set([...(chat.sessions || []), sessionId])];
    this.save();
  }

  // ---------- recording what happens in the current chat ----------
  _push(msg, target) {
    const chat = target || this.current() || this.create();
    msg.at = Date.now();
    if (msg.text != null) msg.text = clip(msg.text, MAX_TEXT);
    chat.messages.push(msg);
    if (chat.messages.length > MAX_MESSAGES) chat.messages.splice(0, chat.messages.length - MAX_MESSAGES);
    chat.updatedAt = msg.at;
    this.save();
    return chat;
  }

  addUser(text) {
    const chat = this._push({ role: 'user', text });
    if (!chat.titled && chat.messages.filter((m) => m.role === 'user').length === 1) chat.title = titleFrom(text);
  }

  /** Record an agent event (the same events the panel shows live). */
  record(ev, target) {
    if (!ev || !ev.type) return;
    const chat = target || this.current();
    const push = (m) => this._push(m, chat);
    const last = chat && chat.messages[chat.messages.length - 1];
    switch (ev.type) {
      case 'assistant':
        if (ev.delta && last && last.role === 'assistant' && last.open) { last.text = clip(last.text + (ev.text || ''), MAX_TEXT); this.save(); }
        else if (ev.text) push({ role: 'assistant', text: ev.text, open: !!ev.delta });
        break;
      case 'tool':
        if (last && last.role === 'steps') { last.steps.push({ name: ev.name, summary: clip(ev.summary || ev.name, 200) }); this.save(); }
        else push({ role: 'steps', steps: [{ name: ev.name, summary: clip(ev.summary || ev.name, 200) }] });
        break;
      case 'permission-resolved':
        if (ev.summary || ev.toolName) push({ role: 'meta', text: `${ev.allow ? '✓ Allowed' : '✕ Denied'}: ${ev.toolName || ''} ${String(ev.summary || '').split('\n')[0]}`.trim() });
        break;
      case 'done': {
        if (last && last.role === 'assistant') delete last.open;
        if (ev.stopped || ev.result === '(stopped)') { push({ role: 'meta', text: '■ stopped' }); break; }
        const bits = [];
        if (ev.durationMs) bits.push(`${(ev.durationMs / 1000).toFixed(1)}s`);
        push({ role: 'meta', text: `✓ done${bits.length ? ' · ' + bits.join(' · ') : ''}` });
        break;
      }
      case 'error':
        push({ role: 'error', text: ev.message || 'Something went wrong' });
        break;
      default:
        break;
    }
  }
}

/**
 * Delete Claude Code's own saved transcripts for these sessions (~/.claude/projects/<folder>/<id>.jsonl),
 * so a deleted chat is really gone. Only exact <session-id>.jsonl files are removed.
 */
function deleteClaudeTranscripts(sessionIds) {
  const ids = (sessionIds || []).filter((s) => /^[0-9a-f-]{8,64}$/i.test(String(s)));
  if (!ids.length) return 0;
  const root = path.join(process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude'), 'projects');
  let removed = 0;
  let dirs = [];
  try { dirs = fs.readdirSync(root, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => path.join(root, d.name)); } catch (_) { return 0; }
  for (const dir of dirs) {
    for (const id of ids) {
      const f = path.join(dir, id + '.jsonl');
      try { if (fs.statSync(f).isFile()) { fs.rmSync(f); removed++; } } catch (_) {}
      try { fs.rmSync(path.join(dir, id), { recursive: true, force: true }); } catch (_) {} // per-session tool output folder, if any
    }
  }
  return removed;
}

module.exports = { ChatStore, deleteClaudeTranscripts, titleFrom };
