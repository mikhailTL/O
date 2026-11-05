'use strict';
// O Link server: the bridge between the O Link browser extension and Playwright MCP.
//
//   O Link extension  <--ws /link (HMAC-authenticated)-->  LinkServer  <--ws /cdp/<token>-- Playwright MCP
//
// The extension reports ALL your open tabs and runs DevTools-protocol commands in them via
// chrome.debugger. Playwright MCP connects here with --cdp-endpoint as if this were a browser.
// The translation (tabs <-> CDP targets/sessions) follows Playwright's own extension relay
// (playwright-core src/tools/mcp/browserModel.ts, Apache-2.0), with O's changes:
//   - every open tab is available (no tab picker, no tab group)
//   - Page.bringToFront / Target.activateTarget are ignored, so O never steals focus
//   - new tabs open in the background
//   - navigating to payment sites is refused (O never makes payments)
//   - when a task ends, all debuggers are detached

const fs = require('fs');
const path = require('path');
const http = require('http');
const crypto = require('crypto');
const { EventEmitter } = require('events');
const { WebSocketServer, WebSocket } = require('ws');

const LINK_PORTS = [47615, 47616, 47617, 47618];

// Payment sites O refuses to open (the in-page guard also blocks payment fields/buttons).
const PAY_HOST = /(^|\.)(paypal\.com|paypal\.me|venmo\.com|cash\.app|pay\.google\.com|payments\.google\.com|checkout\.stripe\.com|buy\.stripe\.com|billing\.stripe\.com|klarna\.com|afterpay\.com|clearpay\.co\.uk|affirm\.com|sezzle\.com|zip\.co|pay\.amazon\.com|payments\.amazon\.com|wise\.com|zellepay\.com|payoneer\.com|skrill\.com|revolut\.com|coinbase\.com|paddle\.com|gumroad\.com|ko-fi\.com|buymeacoffee\.com|patreon\.com|gofundme\.com|robux\.com|giftcards\.[a-z.]+)$/i;

function isPaymentUrl(url) {
  try {
    const u = new URL(String(url || ''));
    return PAY_HOST.test(u.hostname) || /(^|\.)roblox\.com$/i.test(u.hostname) && /^\/(upgrades|premium|robux|giftcards)/i.test(u.pathname);
  } catch { return false; }
}

const hmac = (key, msg) => crypto.createHmac('sha256', key).update(msg).digest('hex');
const safeEqual = (a, b) => {
  const x = Buffer.from(String(a)), y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
};

// ------------------------------------------------------------------ extension connection

class ExtensionConnection {
  constructor(ws) {
    this.ws = ws;
    this.callbacks = new Map();
    this.lastId = 0;
    this.onmessage = null;
    this.onclose = null;
    ws.on('message', (data) => this._onMessage(data));
    ws.on('close', () => {
      for (const cb of this.callbacks.values()) cb.reject(new Error('O Link disconnected'));
      this.callbacks.clear();
      if (this.onclose) this.onclose();
    });
    ws.on('error', () => {});
  }
  send(method, params) {
    if (this.ws.readyState !== WebSocket.OPEN) return Promise.reject(new Error('O Link is not connected'));
    const id = ++this.lastId;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.callbacks.delete(id); reject(new Error(`O Link timed out on ${method}`)); }, 60000);
      this.callbacks.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
    });
  }
  close(reason) { try { this.ws.close(1000, reason); } catch (_) {} }
  _onMessage(data) {
    let msg;
    try { msg = JSON.parse(data.toString()); } catch { return; }
    if (msg.id && this.callbacks.has(msg.id)) {
      const cb = this.callbacks.get(msg.id);
      this.callbacks.delete(msg.id);
      if (msg.error) cb.reject(new Error(msg.error)); else cb.resolve(msg.result);
    } else if (msg.method && this.onmessage) {
      this.onmessage(msg.method, msg.params || []);
    }
  }
}

// ------------------------------------------------------------------ one Playwright session

class CdpSession {
  constructor(server, sendToClient) {
    this.server = server;
    this.sendToClient = sendToClient;
    this.tabSessions = new Map(); // tabId -> { tabId, sessionId, targetInfo, childSessions }
    this.autoAttach = false;
    this.nextSessionId = 1;
    this.closed = false;
  }
  ext(method, params) { return this.server.ext.send(method, params); }
  emit(message) { if (!this.closed) this.sendToClient(message); }

  // extension -> session
  onTabCreated(tab) {
    if (this.autoAttach) this._attachTab(tab.id).catch(() => {});
  }
  onTabRemoved(tabId) { this._detachTab(tabId); }
  onDebuggerEvent(source, method, params) {
    const ts = this.tabSessions.get(source.tabId);
    if (!ts) return;
    const child = params && params.sessionId;
    if (method === 'Target.attachedToTarget' && child) ts.childSessions.add(child);
    else if (method === 'Target.detachedFromTarget' && child) ts.childSessions.delete(child);
    this.emit({ sessionId: source.sessionId || ts.sessionId, method, params });
  }
  onDebuggerDetach(source) { this._detachTab(source.tabId); }

  // Playwright -> session
  async handle(method, params, sessionId) {
    switch (method) {
      case 'Browser.getVersion':
        return { protocolVersion: '1.3', product: 'Chrome/O-Link', userAgent: 'O-Link/1.0', jsVersion: '' };
      case 'Browser.setDownloadBehavior':
        return {};
      case 'Page.bringToFront':
      case 'Target.activateTarget':
        return {}; // never pull the browser in front of what the user is doing
      case 'Target.setAutoAttach':
        if (sessionId) break;
        await this.enableAutoAttach();
        return {};
      case 'Target.createTarget':
        if (isPaymentUrl(params && params.url)) throw new Error('O never makes payments, so it won\'t open payment sites.');
        return await this.createTarget(params && params.url);
      case 'Target.closeTarget':
        return await this.closeTarget(params && params.targetId);
      case 'Target.getTargetInfo':
        return sessionId ? (this._find((s) => s.sessionId === sessionId) || {}).targetInfo : undefined;
      case 'Page.navigate':
        if (isPaymentUrl(params && params.url)) throw new Error('O never makes payments, so it won\'t open payment sites.');
        break;
      default:
        break;
    }
    if (!sessionId) return this.sendBrowserCommand(method, params);
    return this.sendCommand(sessionId, method, params);
  }

  async enableAutoAttach() {
    this.autoAttach = true;
    await Promise.all([...this.server.tabs.keys()].map((id) => this._attachTab(id).catch(() => {})));
  }
  async createTarget(url) {
    const tab = await this.ext('chrome.tabs.create', [{ url }]);
    if (!tab || tab.id === undefined) throw new Error('Failed to create tab');
    this.server.tabs.set(tab.id, tab);
    const ts = await this._attachTab(tab.id);
    return { targetId: ts.targetInfo && ts.targetInfo.targetId };
  }
  async closeTarget(targetId) {
    const ts = targetId ? this._find((s) => s.targetInfo && s.targetInfo.targetId === targetId) : undefined;
    if (!ts) return { success: false };
    await this.ext('chrome.tabs.remove', [ts.tabId]);
    return { success: true };
  }
  async sendBrowserCommand(method, params) {
    const ts = this.tabSessions.values().next().value;
    if (!ts) throw new Error(`No attached tab to forward browser-level command: ${method}`);
    return this.ext('chrome.debugger.sendCommand', [{ tabId: ts.tabId }, method, params]);
  }
  async sendCommand(sessionId, method, params) {
    let ts = this._find((s) => s.sessionId === sessionId);
    let cdpSessionId;
    if (!ts) { ts = this._find((s) => s.childSessions.has(sessionId)); cdpSessionId = sessionId; }
    if (!ts) throw new Error(`No tab found for sessionId: ${sessionId}`);
    return this.ext('chrome.debugger.sendCommand', [{ tabId: ts.tabId, sessionId: cdpSessionId }, method, params]);
  }

  async _attachTab(tabId) {
    const existing = this.tabSessions.get(tabId);
    if (existing) return existing;
    await this.ext('chrome.debugger.attach', [{ tabId }, '1.3']);
    const r = await this.ext('chrome.debugger.sendCommand', [{ tabId }, 'Target.getTargetInfo']);
    const targetInfo = r && r.targetInfo;
    const ts = { tabId, sessionId: `o-tab-${this.nextSessionId++}`, targetInfo, childSessions: new Set() };
    this.tabSessions.set(tabId, ts);
    this.emit({ method: 'Target.attachedToTarget', params: { sessionId: ts.sessionId, targetInfo: { ...targetInfo, attached: true }, waitingForDebugger: false } });
    return ts;
  }
  _detachTab(tabId) {
    const ts = this.tabSessions.get(tabId);
    if (!ts) return;
    this.tabSessions.delete(tabId);
    this.emit({ method: 'Target.detachedFromTarget', params: { sessionId: ts.sessionId, targetId: ts.targetInfo && ts.targetInfo.targetId } });
  }
  _find(pred) {
    for (const s of this.tabSessions.values()) if (pred(s)) return s;
    return undefined;
  }
  dispose() {
    this.closed = true;
    this.tabSessions.clear();
  }
}

// ------------------------------------------------------------------ server

class LinkServer extends EventEmitter {
  /**
   * @param {object} o
   * @param {string} o.dataDir       where to keep the secret and the installable extension copy
   * @param {string} o.extensionSrc  the extension source folder shipped with O
   */
  constructor({ dataDir, extensionSrc }) {
    super();
    this.dataDir = dataDir;
    this.extensionSrc = extensionSrc;
    this.extensionDir = path.join(dataDir, 'o-link');
    this.secret = this._loadSecret();
    this.cdpToken = crypto.randomBytes(16).toString('hex');
    this.port = 0;
    this.ext = null;          // ExtensionConnection
    this.extVersion = '';
    this.tabs = new Map();    // tabId -> tab info, from the extension
    this.session = null;      // current CdpSession
    this.lastBlocked = null;
    this.wantVersion = this._manifestVersion(extensionSrc);
  }

  _loadSecret() {
    const f = path.join(this.dataDir, 'o-link-secret');
    try {
      const s = fs.readFileSync(f, 'utf8').trim();
      if (/^[0-9a-f]{64}$/.test(s)) return s;
    } catch (_) {}
    const s = crypto.randomBytes(32).toString('hex');
    fs.mkdirSync(this.dataDir, { recursive: true });
    fs.writeFileSync(f, s, { mode: 0o600 });
    return s;
  }
  _manifestVersion(dir) {
    try { return JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8')).version || ''; } catch { return ''; }
  }

  /** Copy the extension to a stable folder (for "Load unpacked") with its config. Returns the folder. */
  prepareExtension() {
    fs.mkdirSync(this.extensionDir, { recursive: true });
    for (const name of fs.readdirSync(this.extensionSrc)) {
      const src = path.join(this.extensionSrc, name);
      if (fs.statSync(src).isFile()) fs.copyFileSync(src, path.join(this.extensionDir, name));
    }
    const cfg = { secret: this.secret, ports: LINK_PORTS };
    fs.writeFileSync(path.join(this.extensionDir, 'config.js'),
      `// Written by O: lets O Link find and authenticate O on this computer. Keep private.\nself.O_LINK_CONFIG = ${JSON.stringify(cfg)};\n`);
    return this.extensionDir;
  }

  async start() {
    this.prepareExtension(); // keep the installed copy current (the extension reloads itself on a version change)
    const wss = new WebSocketServer({ noServer: true, maxPayload: 64 * 1024 * 1024 });
    for (const port of LINK_PORTS) {
      const server = http.createServer((req, res) => { res.writeHead(404); res.end(); });
      server.on('upgrade', (req, socket, head) => this._onUpgrade(wss, req, socket, head));
      const ok = await new Promise((resolve) => {
        server.once('error', () => resolve(false));
        server.listen(port, '127.0.0.1', () => resolve(true));
      });
      if (ok) { this.http = server; this.port = port; return port; }
    }
    throw new Error('O Link could not open a local port (47615-47618 are all in use).');
  }

  stop() {
    try { if (this.ext) this.ext.close('O is quitting'); } catch (_) {}
    try { if (this.http) this.http.close(); } catch (_) {}
  }

  status() {
    return {
      connected: !!this.ext,
      version: this.extVersion,
      tabs: this.tabs.size,
      port: this.port,
      extensionDir: this.extensionDir,
      lastBlocked: this.lastBlocked,
    };
  }

  /** The CDP endpoint for Playwright MCP, or null when the extension isn't connected. */
  cdpEndpoint() {
    return this.ext && this.port ? `ws://127.0.0.1:${this.port}/cdp/${this.cdpToken}` : null;
  }

  _onUpgrade(wss, req, socket, head) {
    const url = new URL(req.url, 'http://127.0.0.1');
    const origin = String(req.headers.origin || '');
    if (url.pathname === '/link') {
      // Only browser extensions (never web pages) may connect, and they must know the secret.
      if (!/^(chrome-extension|extension):\/\//.test(origin)) { socket.destroy(); return; }
      wss.handleUpgrade(req, socket, head, (ws) => this._onExtension(ws));
    } else if (url.pathname === `/cdp/${this.cdpToken}` && !origin) {
      wss.handleUpgrade(req, socket, head, (ws) => this._onCdpClient(ws));
    } else {
      socket.destroy();
    }
  }

  _onExtension(ws) {
    const nonce = crypto.randomBytes(16).toString('hex');
    ws.send(JSON.stringify({ type: 'challenge', nonce }));
    const timer = setTimeout(() => ws.close(1008, 'handshake timeout'), 5000);
    ws.once('message', (data) => {
      clearTimeout(timer);
      let hello;
      try { hello = JSON.parse(data.toString()); } catch { return ws.close(1008, 'bad hello'); }
      if (!hello || hello.type !== 'hello' || !safeEqual(hello.mac, hmac(this.secret, 'ext:' + nonce))) {
        return ws.close(1008, 'bad secret');
      }
      ws.send(JSON.stringify({ type: 'welcome', mac: hmac(this.secret, 'o:' + String(hello.nonce || '')), wantVersion: this.wantVersion }));
      if (this.wantVersion && hello.version !== this.wantVersion) return; // it reloads itself with the new files
      if (this.ext) this.ext.close('replaced by a newer connection');
      const conn = new ExtensionConnection(ws);
      this.ext = conn;
      this.extVersion = hello.version || '';
      this.tabs.clear();
      conn.onmessage = (method, params) => this._onExtensionEvent(method, params);
      conn.onclose = () => {
        if (this.ext !== conn) return;
        this.ext = null;
        this.tabs.clear();
        if (this.session) this._closeSession('O Link disconnected');
        this.emit('status', this.status());
      };
      this.emit('status', this.status());
    });
  }

  _onExtensionEvent(method, params) {
    const s = this.session;
    switch (method) {
      case 'chrome.tabs.onCreated': {
        const [tab] = params;
        if (!tab || tab.id === undefined) return;
        this.tabs.set(tab.id, tab);
        if (s) s.onTabCreated(tab);
        break;
      }
      case 'chrome.tabs.onRemoved': {
        const [tabId] = params;
        this.tabs.delete(tabId);
        if (s) s.onTabRemoved(tabId);
        break;
      }
      case 'chrome.debugger.onEvent': {
        const [source, cdpMethod, cdpParams] = params;
        if (s && source) s.onDebuggerEvent(source, cdpMethod, cdpParams);
        break;
      }
      case 'chrome.debugger.onDetach': {
        const [source] = params;
        if (s && source) s.onDebuggerDetach(source);
        break;
      }
      case 'o.blocked': {
        this.lastBlocked = { ...(params[0] || {}), at: Date.now() };
        this.emit('blocked', this.lastBlocked);
        break;
      }
      default:
        break; // extension.initialized, o.keepalive
    }
  }

  _onCdpClient(ws) {
    if (!this.ext) { ws.close(1000, 'O Link is not connected'); return; }
    if (this.session) this._closeSession('replaced by a new task');
    const session = new CdpSession(this, (msg) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); });
    session.ws = ws;
    this.session = session;
    ws.on('message', async (data) => {
      let msg;
      try { msg = JSON.parse(data.toString()); } catch { return; }
      const { id, sessionId, method, params } = msg;
      try {
        const result = await session.handle(method, params, sessionId);
        session.emit({ id, sessionId, result });
      } catch (e) {
        session.emit({ id, sessionId, error: { code: -32000, message: (e && e.message) || String(e) } });
      }
    });
    ws.on('close', () => { if (this.session === session) this._closeSession('task finished'); });
    ws.on('error', () => {});
  }

  _closeSession(reason) {
    const s = this.session;
    this.session = null;
    if (!s) return;
    s.dispose();
    try { if (s.ws && s.ws.readyState === WebSocket.OPEN) s.ws.close(1000, reason); } catch (_) {}
    // Let go of the user's tabs (removes the "debugging" bar) when the task is over.
    if (this.ext) this.ext.send('o.detachAll', []).catch(() => {});
  }
}

module.exports = { LinkServer, isPaymentUrl, LINK_PORTS };
