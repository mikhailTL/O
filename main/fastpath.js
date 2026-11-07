'use strict';
// O's zero-LLM fast path: plainly simple requests are done deterministically, with no Claude Code process
// and no model call. main/tasks.js asks it before a task is queued; anything it is not sure about returns
// null and the full agent handles the request as before.
//
//   const fp = createFastPath({ getSettings });
//   const plan = fp.match('open myscs.org');   // { kind: 'url', url: 'https://myscs.org/', ... } | null
//   const r = await fp.run(plan);              // { ok, result } | { ok: false, error }
//
// Handled: "open / go to / visit <domain or URL>" (default browser, http(s) only, payment sites refused through
// main/link.js's isPaymentUrl), "open / launch <App>" (validated against the installed apps; macOS `open -a`,
// Windows Start-menu shortcut), "search / google <query>" (the default search URL). A site is resolved from
// the domain the person typed (myscs.org -> https://myscs.org/), never from browser history or profiles.
// Nothing here goes through a shell: URLs and app names are passed as single argv entries.

const fs = require('fs');
const os = require('os');
const path = require('path');
const childProcess = require('child_process');

const MAX_PROMPT = 200;
const SEARCH_URL = 'https://www.google.com/search?q=';
const APPS_TTL_MS = 60 * 1000;
const DISPATCH_GRACE_MS = 150; // an opener that has not failed by then has handed off to the OS: report done

// Characters that never appear in a plain open/search request; their presence means "not simple" (or an
// injection attempt such as "open foo.com; rm -rf"), so the full agent (with its guards) gets it.
const UNSAFE = /[;|`<>{}\\\r\n\0]|\$\(|&&/;
const OPEN_VERB = /^(?:open(?: up)?|go to|goto|visit|navigate to|browse to|take me to|pull up|bring up|load)\s+(.+)$/i;
const APP_VERB = /^(?:open(?: up)?|launch|start|run)\s+(.+)$/i;
const SEARCH_VERB = /^(?:search(?: the web| google| online)?(?: for)?|google|web search(?: for)?)\s+(.+)$/i;
const LEAD = /^(?:(?:hey|hi|ok|okay)\s+)?(?:o[,:]?\s+)?(?:please\s+|pls\s+|can you\s+|could you\s+|would you\s+|will you\s+|just\s+)*/i;
const TAIL = /(?:\s+(?:please|pls|for me|now|thanks|thank you|in (?:my|the) (?:default )?browser|in a new tab))+$/i;
// A search that needs the agent (the person's own data, a specific site, several steps, an answer instead of a page).
const SEARCH_NOT_SIMPLE = /\b(?:my|mine|our|files?|folders?|emails?|e-mails?|inbox|messages?|texts?|calendar|desktop|downloads|documents|computer|drive|notes|and then|then|after that|compare|summari[sz]e|tell me|explain|find out|figure out)\b/i;
const SEARCH_ON_SITE = /^\S+\s+(?:for|on)\s/i; // "search amazon for headphones": a site's own search, agent
// Extensions that look like a TLD but almost always mean a file ("open report.pdf", "open main.js").
const FILE_EXT = new Set(['pdf', 'txt', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'csv', 'json', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'py', 'md', 'rb', 'rs', 'sh', 'zsh', 'go', 'java', 'kt', 'swift', 'c', 'h', 'cpp', 'hpp', 'cs', 'php', 'html', 'htm', 'css', 'scss', 'xml', 'yml', 'yaml', 'toml', 'ini', 'cfg', 'conf', 'log', 'lock', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'svg', 'mp3', 'mp4', 'm4a', 'mov', 'wav', 'avi', 'mkv', 'zip', 'gz', 'tgz', 'rar', 'dmg', 'pkg', 'exe', 'msi', 'app', 'rtf', 'pages', 'key', 'numbers', 'psd', 'sketch', 'fig', 'ipynb', 'sql', 'db', 'sqlite', 'bak', 'tmp', 'old', 'env', 'plist', 'icns', 'ico', 'ttf', 'otf', 'woff', 'epub', 'mobi']);
const HOST = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$/i;
// Well-known sites typed without a TLD. Only used when no installed app has the same name.
const KNOWN_SITES = {
  youtube: 'https://www.youtube.com/', gmail: 'https://mail.google.com/', google: 'https://www.google.com/',
  'google docs': 'https://docs.google.com/', 'google drive': 'https://drive.google.com/', 'google calendar': 'https://calendar.google.com/',
  github: 'https://github.com/', reddit: 'https://www.reddit.com/', wikipedia: 'https://www.wikipedia.org/',
  netflix: 'https://www.netflix.com/', twitter: 'https://x.com/', facebook: 'https://www.facebook.com/',
  instagram: 'https://www.instagram.com/', linkedin: 'https://www.linkedin.com/', chatgpt: 'https://chatgpt.com/',
  claude: 'https://claude.ai/', spotify: 'https://open.spotify.com/', 'google maps': 'https://maps.google.com/',
  outlook: 'https://outlook.live.com/', twitch: 'https://www.twitch.tv/',
};
// Common short names for apps; the result is still checked against the installed apps.
const APP_ALIASES = {
  chrome: ['Google Chrome'], 'google chrome': ['Google Chrome'], edge: ['Microsoft Edge'], vscode: ['Visual Studio Code', 'Code'],
  'vs code': ['Visual Studio Code', 'Code'], code: ['Visual Studio Code', 'Code'], word: ['Microsoft Word', 'Word'], excel: ['Microsoft Excel', 'Excel'],
  powerpoint: ['Microsoft PowerPoint', 'PowerPoint'], teams: ['Microsoft Teams', 'Microsoft Teams (work or school)'], outlook: ['Microsoft Outlook', 'Outlook'],
  settings: ['System Settings', 'System Preferences', 'Settings'], 'system settings': ['System Settings', 'System Preferences'],
  terminal: ['Terminal', 'Windows Terminal'], brave: ['Brave Browser'], firefox: ['Firefox'], whatsapp: ['WhatsApp'], zoom: ['zoom.us', 'Zoom'],
  photoshop: ['Adobe Photoshop 2026', 'Adobe Photoshop 2025', 'Adobe Photoshop'], notes: ['Notes'], calculator: ['Calculator'],
};

function cleanPrompt(prompt) {
  let p = String(prompt == null ? '' : prompt).replace(/\s+/g, ' ').trim();
  if (!p || p.length > MAX_PROMPT) return '';
  p = p.replace(/[.!]+$/, '').trim();
  p = p.replace(LEAD, '').replace(TAIL, '').replace(/[.!?]+$/, '').trim();
  return p;
}

/** Strip "the", "website", "site", "page" around a target. */
function cleanTarget(t) {
  return String(t || '').trim().replace(/^the\s+/i, '').replace(/\s+(?:website|web ?site|site|web ?page|page|homepage|home page)$/i, '').replace(/^(?:website|site)\s+/i, '').trim();
}

/**
 * A URL from what the person typed: "myscs.org", "www.x.com/path?q=1", "https://x.com". '' when it is not one
 * (spaces, a file name, a non-http scheme, no TLD).
 */
function toUrl(target) {
  const t = String(target || '').trim();
  if (!t || /\s/.test(t) || UNSAFE.test(t) || /["']/.test(t)) return '';
  const hasScheme = /^[a-z][a-z0-9+.-]*:(?!\d)/i.test(t); // "localhost:3000" is a host and port, not a scheme
  if (hasScheme && !/^https?:\/\//i.test(t)) return ''; // file:, javascript:, mailto: ... never
  const local = /^(?:https?:\/\/)?localhost(?::\d{2,5})?(?:[/?#]|$)/i.test(t);
  let u;
  try { u = new URL(hasScheme ? t : (local ? 'http://' : 'https://') + t); } catch { return ''; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return '';
  if (u.username || u.password) return '';
  const host = u.hostname.toLowerCase();
  if (host !== 'localhost' && !HOST.test(host)) return '';
  if (!hasScheme && host !== 'localhost') {
    const tld = host.slice(host.lastIndexOf('.') + 1);
    const bareHost = t.toLowerCase().replace(/\/$/, '') === host;
    if (FILE_EXT.has(tld) && bareHost && !/^www\./i.test(t)) return ''; // "report.pdf", "main.js": a file, not a site
  }
  return u.href;
}

function hostLabel(url) {
  try { const u = new URL(url); return (u.hostname.replace(/^www\./, '') + (u.pathname !== '/' ? u.pathname : '') + u.search).replace(/\/$/, ''); } catch { return url; }
}

// ------------------------------------------------------------------ installed apps

function listAppsDarwin() {
  const dirs = ['/Applications', '/Applications/Utilities', '/System/Applications', '/System/Applications/Utilities', path.join(os.homedir(), 'Applications')];
  const out = [];
  for (const d of dirs) {
    let names = [];
    try { names = fs.readdirSync(d); } catch { continue; }
    for (const n of names) if (n.endsWith('.app')) out.push({ name: n.slice(0, -4), path: path.join(d, n) });
  }
  return out;
}

function listAppsWindows() {
  const roots = [
    path.join(process.env.ProgramData || 'C:\\ProgramData', 'Microsoft', 'Windows', 'Start Menu', 'Programs'),
    path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'Microsoft', 'Windows', 'Start Menu', 'Programs'),
  ];
  const out = [];
  const walk = (d, depth) => {
    let ents = [];
    try { ents = fs.readdirSync(d, { withFileTypes: true }); } catch { return; }
    for (const e of ents) {
      const p = path.join(d, e.name);
      if (e.isDirectory() && depth < 2) walk(p, depth + 1);
      else if (e.isFile() && /\.lnk$/i.test(e.name) && !/uninstall/i.test(e.name)) out.push({ name: e.name.replace(/\.lnk$/i, ''), path: p });
    }
  };
  for (const r of roots) walk(r, 0);
  return out;
}

function defaultListApps(platform) {
  if (platform === 'darwin') return listAppsDarwin();
  if (platform === 'win32') return listAppsWindows();
  return []; // Linux: app launching is left to the full agent
}

// ------------------------------------------------------------------ openers

function execOk(file, args) {
  return new Promise((resolve) => {
    childProcess.execFile(file, args, { timeout: 10000, windowsHide: true }, (err, _o, stderr) => resolve(err ? { ok: false, error: String(stderr || err.message).trim().slice(0, 300) } : { ok: true }));
  });
}

function electronShell() {
  if (!process.versions.electron || process.type === 'renderer') return null;
  try { const e = require('electron'); return e && e.shell && typeof e.shell.openExternal === 'function' ? e.shell : null; } catch { return null; }
}

function defaultOpener(platform) {
  const shell = electronShell();
  return {
    async openUrl(url) {
      if (shell) { try { await shell.openExternal(url, { activate: true }); return { ok: true }; } catch (e) { return { ok: false, error: String(e && e.message || e) }; } }
      if (platform === 'darwin') return execOk('open', [url]);
      if (platform === 'win32') return execOk('rundll32', ['url.dll,FileProtocolHandler', url]);
      return execOk('xdg-open', [url]);
    },
    async openApp(app) {
      if (platform === 'darwin') return execOk('open', ['-a', app.path || app.name]);
      if (platform === 'win32') {
        if (shell && typeof shell.openPath === 'function') { const err = await shell.openPath(app.path); return err ? { ok: false, error: err } : { ok: true }; }
        return execOk('explorer.exe', [app.path]);
      }
      return { ok: false, error: 'opening apps is not supported on this system by the fast path' };
    },
  };
}

/** Run `fn()` but report success once it has not failed within `graceMs` (the OS has the request by then). */
function dispatch(fn, graceMs) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (r) => { if (!settled) { settled = true; resolve(r); } };
    const timer = setTimeout(() => done({ ok: true, dispatched: true }), graceMs);
    Promise.resolve().then(fn).then((r) => { clearTimeout(timer); done(r && typeof r === 'object' ? r : { ok: true }); }, (e) => { clearTimeout(timer); done({ ok: false, error: String(e && e.message || e) }); });
  });
}

// ------------------------------------------------------------------ the fast path

/**
 * @param {object} [o]
 * @param {() => object} [o.getSettings]  `fastPath: false` turns the fast path off
 * @param {string} [o.platform]
 * @param {{openUrl(url:string):Promise<{ok:boolean,error?:string}>, openApp(app:{name,path}):Promise<{ok:boolean,error?:string}>}} [o.opener]
 * @param {() => Array<{name:string,path:string}>} [o.listApps]  installed apps
 * @param {((url:string) => boolean)|null} [o.isPaymentUrl]  main/link.js's guard by default (loaded like native-tabs.js)
 */
function createFastPath(o = {}) {
  const platform = o.platform || process.platform;
  const getSettings = o.getSettings || (() => ({}));
  let isPaymentUrl = 'isPaymentUrl' in o ? o.isPaymentUrl : undefined;
  if (isPaymentUrl === undefined) {
    try { isPaymentUrl = require('./tools/native-tabs').loadPaymentGuard(); } catch { isPaymentUrl = null; }
  }
  const opener = o.opener || defaultOpener(platform);
  const listApps = o.listApps || (() => defaultListApps(platform));
  const graceMs = Number.isFinite(o.graceMs) ? o.graceMs : DISPATCH_GRACE_MS;
  let appsCache = null;

  function apps() {
    if (appsCache && Date.now() - appsCache.at < APPS_TTL_MS) return appsCache.list;
    let list = [];
    try { list = (listApps() || []).filter((a) => a && typeof a.name === 'string'); } catch { list = []; }
    appsCache = { at: Date.now(), list };
    return list;
  }

  function findApp(name) {
    const n = cleanTarget(name).replace(/\s+(?:app|application)$/i, '').trim();
    if (!n || n.length > 60 || UNSAFE.test(n) || /["'/:]/.test(n)) return null;
    const list = apps();
    if (!list.length) return null;
    const want = [n, ...(APP_ALIASES[n.toLowerCase()] || [])].map((x) => x.toLowerCase());
    for (const w of want) {
      const hit = list.find((a) => a.name.toLowerCase() === w);
      if (hit) return hit;
    }
    return null;
  }

  function urlPlan(url, how) {
    if (typeof isPaymentUrl !== 'function') return null; // no guard: the full agent (with its own guards) decides
    if (isPaymentUrl(url)) return { kind: 'refuse', url, reason: 'Blocked by O: O never makes payments, so it won\'t open payment sites. You can open that one yourself.', summary: 'Refused a payment site' };
    const label = hostLabel(url);
    return { kind: how === 'search' ? 'search' : 'url', url, label, summary: how === 'search' ? `Searching for ${label}` : `Opening ${label}` };
  }

  /** A plan for a plainly simple request, or null (the full agent handles it). Synchronous and fast. */
  function match(prompt) {
    const s = getSettings() || {};
    if (s.fastPath === false) return null;
    const p = cleanPrompt(prompt);
    if (!p || UNSAFE.test(p)) return null;
    if (/\b(?:and|then|also|after|before|while|but)\b|[,&]/i.test(p.replace(/^[^ ]+ /, '').replace(/\?[^ ]*$/, ''))) {
      // several steps ("open x and log in", "open x, then ..."): a URL's own query string is allowed below
      const m0 = OPEN_VERB.exec(p);
      if (!(m0 && toUrl(cleanTarget(m0[1])))) return null;
    }

    const s1 = SEARCH_VERB.exec(p);
    if (s1) {
      const q = s1[1].trim().replace(/^["']|["']$/g, '');
      const words = q.split(/\s+/).filter(Boolean);
      if (!words.length || words.length > 12 || q.length > 120 || SEARCH_NOT_SIMPLE.test(q) || SEARCH_ON_SITE.test(q)) return null;
      if (toUrl(q)) return null; // "search example.com" is ambiguous (open it? search the site?)
      const plan = urlPlan(SEARCH_URL + encodeURIComponent(q), 'search');
      if (plan && plan.kind === 'search') { plan.query = q; plan.summary = `Searching for “${q}”`; plan.label = q; }
      return plan;
    }

    const m = OPEN_VERB.exec(p) || APP_VERB.exec(p);
    if (!m) return null;
    const verb = p.slice(0, p.length - m[1].length).trim().toLowerCase();
    let target = cleanTarget(m[1]);
    if (!target) return null;
    if (/\s+in\s+(?:safari|chrome|edge|firefox|brave|arc|opera|vivaldi|google chrome|microsoft edge)$/i.test(target)) return null; // a specific browser: agent

    const url = toUrl(target);
    if (url) {
      if (/^(?:launch|start|run)$/.test(verb)) return null;
      return urlPlan(url, 'open');
    }
    if (/[./:]/.test(target)) return null; // looked like a URL or a path but is not a valid one
    const words = target.split(/\s+/);
    if (words.length > 4) return null;
    const app = /^(?:go to|goto|visit|navigate to|browse to|take me to|load)$/.test(verb) ? null : findApp(target);
    if (app) return { kind: 'app', app: { name: app.name, path: app.path }, label: app.name, summary: `Opening ${app.name}` };
    const known = KNOWN_SITES[target.toLowerCase()];
    if (known && !/^(?:launch|start|run)$/.test(verb)) return urlPlan(known, 'open');
    return null; // a bare name ("open myscs") with no TLD and no app: the agent resolves it (by domain, never history)
  }

  /** Carry out a plan. Resolves within ~graceMs for openers that hand off to the OS. Never rejects. */
  async function run(plan) {
    const t0 = Date.now();
    if (!plan || typeof plan !== 'object') return { ok: false, error: 'no plan' };
    if (plan.kind === 'refuse') return { ok: false, refused: true, error: plan.reason, durationMs: 0 };
    let r;
    if (plan.kind === 'url' || plan.kind === 'search') {
      // re-check right before opening (the plan may have been built by a caller)
      if (typeof isPaymentUrl !== 'function') return { ok: false, error: 'O\'s payment-site check is unavailable' };
      if (!/^https?:\/\//i.test(String(plan.url || '')) || isPaymentUrl(plan.url)) return { ok: false, refused: true, error: 'Blocked by O: that address cannot be opened.' };
      r = await dispatch(() => opener.openUrl(plan.url), graceMs);
      if (r.ok) r.result = plan.kind === 'search' ? `Searched for “${plan.query || plan.label}” in your default browser.` : `Opened ${plan.label} in your default browser.`;
    } else if (plan.kind === 'app') {
      const app = plan.app && findApp(plan.app.name);
      if (!app) return { ok: false, error: `${plan.app && plan.app.name} is not an installed app` };
      r = await dispatch(() => opener.openApp(app), graceMs);
      if (r.ok) r.result = `Opened ${app.name}.`;
    } else return { ok: false, error: 'unknown plan' };
    return { ...r, durationMs: Date.now() - t0 };
  }

  return { match, run, apps, findApp, platform };
}

module.exports = { createFastPath, cleanPrompt, toUrl, hostLabel, defaultListApps, SEARCH_URL, KNOWN_SITES, APP_ALIASES, UNSAFE };
