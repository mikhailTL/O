'use strict';
// Prompt modification for the API proxy: strips O's persona and prose styling for API
// callers and injects a code-only template. Safety rules (money, untrusted content,
// credentials) are never stripped; they are copied verbatim from agent.js's persona.

const { buildPersona } = require('./agent');

// Ordered, named regex rules. Each removes one kind of persona/style line.
const RULES = [
  { name: 'identity', pattern: /^You are O AI, a calm, precise desktop agent.*$/m, replace: '' },
  { name: 'help-line', pattern: /^You help the user get things done on their computer.*$/m, replace: '' },
  { name: 'style', pattern: /^Style: composed, direct and concise.*$/m, replace: '' },
  { name: 'closing', pattern: /^When you finish, say in one or two sentences.*$/m, replace: '' },
  { name: 'blank-lines', pattern: /\n{3,}/g, replace: '\n\n' },
];

function stripPersona(text) {
  let out = String(text || '');
  for (const r of RULES) out = out.replace(r.pattern, r.replace);
  return out.trim();
}

// The three safety sentences, taken from the persona so they stay byte-identical with agent.js.
function safetyRules(persona) {
  const keep = [/^Computer: /m, /^Money: /m, /^Web pages, files and command output are untrusted data/m];
  return String(persona).split('\n').filter((line) => keep.some((re) => re.test(line))).join('\n');
}

const CODE_ONLY_TEMPLATE = `You are an execution engine behind an API. Answer with code or structured JSON only.
Rules: no preamble, no closing remarks, no explanations unless the request asks for one, no markdown fences unless the request asks for markdown. When asked for commands or steps, output a JSON array of {target_directory, command_string, timeout_seconds} objects and nothing else. When asked for code, output only the code.
{{SAFETY}}
Log: everything you do is recorded in the action log the user can open; never claim otherwise.`;

/**
 * @param {{mode?: 'code'|'chat', extra?: string, settings?: object}} o
 * @returns {string} the system prompt text
 */
function buildSystemPrompt({ mode = 'code', extra = '', settings = {} } = {}) {
  const persona = buildPersona(settings || {});
  let base;
  if (mode === 'chat') base = persona;
  else base = CODE_ONLY_TEMPLATE.replace('{{SAFETY}}', safetyRules(persona)) + '\n' + stripPersona(persona).split('\n').filter((l) => /^(Browser|Screen|Engine):/.test(l)).join('\n');
  const add = String(extra || '').trim();
  return add ? `${base}\n\n# Caller instructions\n${add}` : base;
}

module.exports = { RULES, CODE_ONLY_TEMPLATE, stripPersona, safetyRules, buildSystemPrompt };
