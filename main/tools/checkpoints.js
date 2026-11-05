'use strict';
// Which tool calls are checkpoint actions: publish / send_message / spend / create_account / accept_terms /
// delete_data, or `forbidden` (fake engagement, bought views, bots). Shared by the bridge (bridge-mcp.js gates
// every goal task's calls before they run) and main (main/goals.js decides and executes approved actions).
// Dependency-free; the bridge runs it as plain Node.
//
//   classifyAction('mcp__youtube__videos_insert', {...})            -> { kind: 'publish', reason: '...' }
//   classifyAction('mcp__o__mcp_call', { server, tool, args })      -> classifies server/tool/args
//   classifyAction('mcp__playwright__browser_click', { element })   -> by the button's words
//   classifyAction('Read', {...})                                  -> null (not a checkpoint)

const CHECKPOINT_KINDS = ['publish', 'send_message', 'spend', 'create_account', 'accept_terms', 'delete_data'];
const NEVER_BY_O = ['spend', 'create_account']; // O prepares these; the person always does them

const KIND_LABEL = {
  publish: 'Publish or post publicly',
  send_message: 'Send a message as you',
  spend: 'Spend money or start a paid service',
  create_account: 'Create an account',
  accept_terms: 'Accept terms',
  delete_data: 'Delete your data',
  forbidden: 'Fake engagement or rule-breaking',
};

const FORBIDDEN = /\b(buy|purchase|order)\s+(?:\d[\d,.]*\s*k?\s+)?(?:real\s+|cheap\s+|instant\s+)?(?:[a-z]+\s+)?(views?|subscribers?|subs|likes|followers|comments|watch\s*hours|plays|streams|upvotes|reviews)\b|view\s*bots?|\bbots?\s+(?:views|comments|likes|subscribers|traffic)|sub4sub|follow4follow|like4like|engagement\s*(?:pods?|bots?|farms?|groups?)|fake\s+(?:views|comments|reviews|engagement|accounts?|followers|likes)|smm\s*panel|click\s*farm|(?:mass|auto)[\s-]?(?:follow|subscribe|comment|like)/i;

const READ = new Set(['list', 'get', 'search', 'read', 'query', 'fetch', 'describe', 'find', 'lookup', 'analytics', 'report', 'reports', 'stats', 'insights', 'status', 'preview', 'view', 'show', 'count', 'check', 'download', 'export', 'thumbnails']);
const WRITE = new Set(['publish', 'post', 'upload', 'insert', 'tweet', 'retweet', 'share', 'send', 'reply', 'forward', 'create', 'update', 'set', 'patch', 'put', 'delete', 'remove', 'trash', 'destroy', 'purge', 'wipe', 'erase', 'drop', 'unlink', 'pay', 'buy', 'purchase', 'checkout', 'order', 'subscribe', 'charge', 'donate', 'boost', 'promote', 'accept', 'agree', 'consent', 'register', 'signup', 'broadcast', 'golive', 'premiere', 'schedule', 'comment', 'like', 'follow']);
const MEDIA = new Set(['video', 'videos', 'short', 'shorts', 'reel', 'reels', 'story', 'stories', 'post', 'posts', 'status', 'statuses', 'tweet', 'tweets', 'thread', 'threads', 'comment', 'comments', 'pin', 'pins', 'article', 'articles', 'page', 'episode', 'episodes', 'live', 'livestream', 'broadcast', 'broadcasts', 'playlist', 'playlists', 'issue', 'newsletter', 'campaign', 'media']);
const MSG = new Set(['message', 'messages', 'email', 'emails', 'mail', 'dm', 'dms', 'sms', 'chat', 'text', 'reply', 'forward', 'postmessage', 'conversation']);

/** Lowercase word tokens of a tool name: camelCase, snake_case, dotted and slashed parts. */
function tokens(s) {
  return String(s || '')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}

function argText(args) {
  try { return JSON.stringify(args === undefined ? {} : args); } catch { return String(args); }
}

/** Public-visibility arguments (YouTube privacyStatus, a post's visibility / status). */
function goesPublic(args) {
  const t = argText(args);
  return /"(privacy_?status|privacy|visibility|audience)"\s*:\s*"(public|unlisted|everyone|anyone)"/i.test(t) || /"status"\s*:\s*"(publish|published|public|live)"/i.test(t);
}

/** Free text (a browser button's label, a web task, a desktop goal): what it would do. */
function classifyPhrase(text, { task = false } = {}) {
  const t = String(text || '');
  if (!t.trim()) return null;
  if (FORBIDDEN.test(t)) return { kind: 'forbidden', reason: 'fake engagement or a platform-rule workaround' };
  // In a whole task description ("research what to buy"), spending counts only as the instruction itself.
  if (task && !/^\W*(?:please\s+)?(?:pay|buy|purchase|check\s?out|order|subscribe|upgrade|promote|boost)\b|\b(?:and|then)\s+(?:pay|buy|purchase|check\s?out|place\s+the\s+order)\b/i.test(t)) {
    const rest = classifyPhrase(t.replace(/\b(pay|buy|purchase|check\s?out|order|subscribe|upgrade|promote|boost)\b/gi, ''), {});
    return rest && rest.kind !== 'spend' ? rest : null;
  }
  if (/\b(pay|buy|purchase|check\s?out|place\s+(?:an?\s+|the\s+)?order|subscribe\s+to\s+(?:a\s+)?(?:paid|premium|pro)|start\s+(?:a\s+)?(?:paid|free)\s+trial|upgrade\s+(?:to\s+)?(?:premium|pro|paid)|add\s+(?:a\s+)?(?:card|payment)|promote\s+(?:this\s+)?(?:post|video)|boost\s+(?:this\s+)?(?:post|video))\b/i.test(t)) return { kind: 'spend', reason: 'spends money' };
  if (/\b(sign\s?up|create\s+(?:an?\s+|new\s+|your\s+)?(?:account|channel|profile)|register(?:\s+an?\s+account)?|join\s+now)\b/i.test(t)) return { kind: 'create_account', reason: 'creates an account' };
  if (/\bi\s+agree\b|\b(agree|accept)\b[^.]{0,40}\b(terms|conditions|policy|agreement|tos|eula)\b/i.test(t)) return { kind: 'accept_terms', reason: 'accepts terms' };
  if (/\b(delete|remove|trash|erase|wipe)\b/i.test(t)) return { kind: 'delete_data', reason: 'deletes data' };
  if (/\b(send|reply|forward|dm)\b/i.test(t)) return { kind: 'send_message', reason: 'sends a message' };
  if (/\b(publish|post|upload|share|tweet|go\s+live|make\s+(?:it\s+)?public|schedule\s+(?:the\s+)?(?:post|video|upload|premiere)|premiere)\b/i.test(t)) return { kind: 'publish', reason: 'publishes or posts' };
  return null;
}

/** A tool name (+ args) from an MCP connector: what it would do. */
function classifyTool(name, args) {
  const text = argText(args);
  if (FORBIDDEN.test(name + ' ' + tokens(name).join(' ') + ' ' + text)) return { kind: 'forbidden', reason: 'fake engagement or a platform-rule workaround' };
  const T = tokens(name);
  const has = (...w) => w.some((x) => T.includes(x));
  const writes = T.some((x) => WRITE.has(x));
  if (!writes && T.some((x) => READ.has(x))) return null;
  if (has('draft', 'drafts') && !has('send', 'publish', 'post')) return null; // drafts stay private
  if (has('pay', 'payment', 'payments', 'purchase', 'buy', 'checkout', 'order', 'orders', 'charge', 'billing', 'donate', 'tip', 'boost', 'promote', 'topup', 'transfer', 'invoice') || (has('ads', 'ad', 'campaign', 'campaigns') && has('create', 'insert', 'update', 'start'))) return { kind: 'spend', reason: 'spends money' };
  if (has('subscribe') && has('plan', 'premium', 'membership', 'subscription')) return { kind: 'spend', reason: 'starts a paid service' };
  if (has('signup', 'register') || ((has('create', 'new', 'insert')) && has('account', 'accounts', 'user', 'users', 'channel', 'channels', 'profile', 'profiles'))) return { kind: 'create_account', reason: 'creates an account' };
  if (has('accept', 'agree', 'consent') || has('terms', 'tos', 'eula')) return { kind: 'accept_terms', reason: 'accepts terms' };
  if (has('delete', 'remove', 'trash', 'destroy', 'purge', 'wipe', 'erase', 'drop', 'unlink')) return { kind: 'delete_data', reason: 'deletes data' };
  if (T.some((x) => MSG.has(x)) && has('send', 'post', 'reply', 'forward', 'create', 'postmessage') || has('send', 'reply', 'forward')) return { kind: 'send_message', reason: 'sends a message as you' };
  if (has('publish', 'post', 'upload', 'tweet', 'retweet', 'share', 'broadcast', 'golive', 'premiere', 'comment', 'like', 'follow') || (has('insert', 'create', 'schedule') && T.some((x) => MEDIA.has(x)))) return { kind: 'publish', reason: 'publishes or posts publicly' };
  if (has('update', 'set', 'patch', 'put') && goesPublic(args)) return { kind: 'publish', reason: 'makes something public' };
  if (goesPublic(args) && writes) return { kind: 'publish', reason: 'makes something public' };
  return null;
}

const BASH_PUBLISH = /\b(youtube-upload|gh\s+release\s+create|gh\s+gist\s+create|npm\s+publish|twine\s+upload|git\s+push|vercel\s+--prod|netlify\s+deploy)\b|\bcurl\b[^\n]*(?:upload|\/videos|\/statuses|\/tweets|\/posts|\/media)\b/i;
const BASH_SEND = /\b(sendmail|mutt|msmtp)\b|\bmail\s+-s\b|\bcurl\b[^\n]*(?:\/messages\/send|chat\.postMessage|\/sendMessage)\b/i;
const BASH_DELETE = /\b(rm|rmdir|del|rd|shred|Remove-Item)\b[^\n]*?(?:~|\$HOME|%USERPROFILE%|\/home\/|\/Users\/|[A-Za-z]:\\Users\\)/i;

/**
 * What one tool call would do, or null when it is not a checkpoint action.
 * @returns {{kind: string, reason: string} | null}
 */
function classifyAction(toolName, input) {
  const name = String(toolName || '');
  const i = input && typeof input === 'object' ? input : {};
  if (name === 'mcp__o__mcp_call') return classifyTool(`${i.server || ''}__${i.tool || ''}`, i.args);
  if (name === 'mcp__o__web_run') return classifyPhrase(i.task, { task: true });
  if (name === 'mcp__o__computer_run') return classifyPhrase(i.goal, { task: true });
  if (name.startsWith('mcp__o__')) return null; // O's own tools touch only O's state (goal_checkpoint files the card)
  if (name.startsWith('mcp__playwright__')) {
    if (/browser_(click|press_key|type|select_option|file_upload|drag)/.test(name)) {
      const el = String(i.element || i.text || i.key || '');
      if (/\b(textbox|text\s*box|field|input|combobox|search|text\s*area|editor)\b/i.test(el) && !/\bbutton\b/i.test(el)) return null; // typing into a field is not the action
      if (/browser_type/.test(name) && !i.submit) return FORBIDDEN.test(String(i.text || '')) ? { kind: 'forbidden', reason: 'fake engagement' } : null;
      return classifyPhrase(el);
    }
    if (/browser_navigate/.test(name)) return FORBIDDEN.test(String(i.url || '')) ? { kind: 'forbidden', reason: 'fake engagement service' } : null;
    return null;
  }
  if (name === 'Bash' || name === 'PowerShell') {
    const cmd = String(i.command || '');
    if (FORBIDDEN.test(cmd)) return { kind: 'forbidden', reason: 'fake engagement' };
    if (BASH_DELETE.test(cmd)) return { kind: 'delete_data', reason: 'deletes files in your home folder' };
    if (BASH_SEND.test(cmd)) return { kind: 'send_message', reason: 'sends a message' };
    if (BASH_PUBLISH.test(cmd)) return { kind: 'publish', reason: 'publishes' };
    return null;
  }
  if (['Read', 'Glob', 'Grep', 'LS', 'WebSearch', 'WebFetch', 'TodoWrite', 'NotebookRead', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'Task'].includes(name)) return null;
  if (name.startsWith('mcp__')) {
    const parts = name.split('__');
    return classifyTool(parts.slice(1).join('__'), i);
  }
  return null;
}

module.exports = { classifyAction, classifyTool, classifyPhrase, tokens, CHECKPOINT_KINDS, NEVER_BY_O, KIND_LABEL, FORBIDDEN };
