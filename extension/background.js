// O Link: connects your browser to the O desktop pet running on this computer.
//
// O (a local app on 127.0.0.1) asks this extension to attach the browser's debugger to
// your open tabs and run DevTools-protocol commands in them. That's how O reads pages and
// clicks/types *inside* them without moving your mouse or touching your keyboard.
//
// Security:
// - It only connects to 127.0.0.1, and both sides prove they know a secret that O wrote
//   into this folder's config.js (HMAC challenge in both directions). A web page can't use it.
// - guard.js (a content script) locks payment fields and pay/buy buttons while O is attached.
// - Debuggers are attached only while O is working on a task, and detached afterwards.

/* global O_LINK_CONFIG */
try { importScripts('config.js'); } catch (_) { /* not set up by O yet */ }
const CFG = self.O_LINK_CONFIG || { secret: '', ports: [] };
const VERSION = chrome.runtime.getManifest().version;

let ws = null;
let ready = false;
let connecting = false;
const attached = new Set();   // tabIds with O's debugger attached
const reported = new Set();   // tabIds announced to O

// ------------------------------------------------------------------ helpers

const enc = (s) => new TextEncoder().encode(s);
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
function randHex(n) { const a = new Uint8Array(n); crypto.getRandomValues(a); return hex(a); }
async function hmac(key, msg) {
  const k = await crypto.subtle.importKey('raw', enc(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return hex(await crypto.subtle.sign('HMAC', k, enc(msg)));
}
function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}
function workable(tab) {
  const url = (tab && (tab.url || tab.pendingUrl)) || '';
  return /^(https?|file):/i.test(url);
}
function report(tab) {
  if (!ready || !tab || tab.id === undefined || reported.has(tab.id) || !workable(tab)) return;
  reported.add(tab.id);
  send({ method: 'chrome.tabs.onCreated', params: [{ id: tab.id, url: tab.url, title: tab.title, active: tab.active, windowId: tab.windowId }] });
}
function notifyGuard(tabId, on) {
  chrome.tabs.sendMessage(tabId, { o: 'active', on }).catch(() => {});
}
async function detachAll() {
  for (const tabId of [...attached]) {
    try { await chrome.debugger.detach({ tabId }); } catch (_) {}
    attached.delete(tabId);
    notifyGuard(tabId, false);
  }
}

// ------------------------------------------------------------------ connection

async function connect() {
  if (connecting || (ws && ws.readyState <= WebSocket.OPEN) || !CFG.secret) return;
  connecting = true;
  try {
    for (const port of CFG.ports || []) {
      if (await tryPort(port)) return;
    }
  } finally {
    connecting = false;
  }
}

function tryPort(port) {
  return new Promise((resolve) => {
    let settled = false;
    let myNonce = '';
    const done = (ok) => { if (!settled) { settled = true; resolve(ok); } };
    let sock;
    try { sock = new WebSocket(`ws://127.0.0.1:${port}/link`); } catch (_) { return done(false); }
    const timer = setTimeout(() => { try { sock.close(); } catch (_) {} done(false); }, 3000);

    sock.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (!ready) {
        if (msg.type === 'challenge') {
          myNonce = randHex(16);
          sock.send(JSON.stringify({ type: 'hello', version: VERSION, nonce: myNonce, mac: await hmac(CFG.secret, 'ext:' + msg.nonce) }));
        } else if (msg.type === 'welcome') {
          if (msg.mac !== await hmac(CFG.secret, 'o:' + myNonce)) { sock.close(); return done(false); }
          if (msg.wantVersion && msg.wantVersion !== VERSION) { chrome.runtime.reload(); return; } // O updated our files
          clearTimeout(timer);
          ws = sock;
          ready = true;
          reported.clear();
          for (const tab of await chrome.tabs.query({})) report(tab);
          send({ method: 'extension.initialized' });
          done(true);
        }
        return;
      }
      handleCommand(msg);
    };
    sock.onclose = () => {
      clearTimeout(timer);
      if (ws === sock) {
        ws = null;
        ready = false;
        detachAll();
        setTimeout(connect, 3000);
      }
      done(false);
    };
    sock.onerror = () => {};
  });
}

async function handleCommand(msg) {
  const { id, method, params = [] } = msg;
  if (!id) return;
  try {
    const result = await run(method, params);
    send({ id, result: result === undefined ? {} : result });
  } catch (e) {
    send({ id, error: (e && e.message) || String(e) });
  }
}

async function run(method, params) {
  switch (method) {
    case 'chrome.debugger.attach': {
      const [target] = params;
      if (attached.has(target.tabId)) return {};
      await chrome.debugger.attach({ tabId: target.tabId }, '1.3');
      attached.add(target.tabId);
      notifyGuard(target.tabId, true);
      return {};
    }
    case 'chrome.debugger.detach': {
      const [target] = params;
      try { await chrome.debugger.detach({ tabId: target.tabId }); } catch (_) {}
      attached.delete(target.tabId);
      notifyGuard(target.tabId, false);
      return {};
    }
    case 'chrome.debugger.sendCommand': {
      const [target, cdpMethod, cdpParams] = params;
      const t = { tabId: target.tabId };
      if (target.sessionId) t.sessionId = target.sessionId;
      return await chrome.debugger.sendCommand(t, cdpMethod, cdpParams || {});
    }
    case 'chrome.tabs.create': {
      // In the background, so O never switches the tab you're looking at.
      const tab = await chrome.tabs.create({ url: (params[0] && params[0].url) || 'about:blank', active: false });
      reported.add(tab.id);
      return { id: tab.id, url: tab.pendingUrl || tab.url, windowId: tab.windowId };
    }
    case 'chrome.tabs.remove':
      await chrome.tabs.remove(params[0]);
      return {};
    case 'o.detachAll':
      await detachAll();
      return {};
    case 'o.ping':
      return { version: VERSION };
    default:
      throw new Error('Unknown command: ' + method);
  }
}

// ------------------------------------------------------------------ browser events

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (attached.has(source.tabId)) send({ method: 'chrome.debugger.onEvent', params: [source, method, params] });
});
chrome.debugger.onDetach.addListener((source, reason) => {
  attached.delete(source.tabId);
  notifyGuard(source.tabId, false);
  send({ method: 'chrome.debugger.onDetach', params: [source, reason] });
});
chrome.tabs.onCreated.addListener((tab) => report(tab));
chrome.tabs.onUpdated.addListener((_id, info, tab) => { if (info.url || info.status) report(tab); });
chrome.tabs.onRemoved.addListener((tabId) => {
  attached.delete(tabId);
  if (reported.delete(tabId)) send({ method: 'chrome.tabs.onRemoved', params: [tabId] });
});

// The payment guard asks whether O is attached to its tab, and reports what it blocked.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.o) return;
  if (msg.o === 'state') sendResponse({ active: !!(sender.tab && attached.has(sender.tab.id)) });
  if (msg.o === 'blocked') send({ method: 'o.blocked', params: [{ reason: msg.reason, url: sender.url }] });
});

// Stay connected: keep the service worker awake while connected, retry while O is off.
setInterval(() => send({ method: 'o.keepalive' }), 20000);
chrome.alarms.create('o-reconnect', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'o-reconnect') connect(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
