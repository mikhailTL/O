'use strict';
// D3 "Use O from any app": a terminal pane runs REAL curl against the REAL API proxy (main/proxy.js) with
// streaming on, then a REAL python snippet with the openai client; both go through fake-claude.js (FAKE_SIMPLE)
// so the reply text is canned, but the proxy, bearer auth, SSE normalization and action-log entries are real.
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

/** Run a command, streaming each stdout line into the terminal pane with a small delay per line. */
function streamToTerm(d, cmd, args, { env, lineMs = 70, cls = '' } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { env: { ...process.env, ...(env || {}) }, stdio: ['ignore', 'pipe', 'pipe'] });
    let buf = '', queue = Promise.resolve(), out = '';
    const push = (line) => { out += line + '\n'; queue = queue.then(() => d.stage('termLine', esc(line), cls)).then(() => d.sleep(lineMs)); };
    child.stdout.on('data', (c) => { buf += c; let i; while ((i = buf.indexOf('\n')) >= 0) { push(buf.slice(0, i)); buf = buf.slice(i + 1); } });
    child.stderr.on('data', (c) => { for (const l of String(c).split('\n')) if (l.trim()) push(l); });
    child.on('error', reject);
    child.on('close', (code) => { if (buf.trim()) push(buf); queue.then(() => resolve({ code, out })); });
  });
}

async function run(d) {
  const { story, panel, backend } = d;
  const C = story.captions;
  const { url, token } = backend.proxy;
  await d.card(true, 'Use O from any app');
  await d.sleep(2200);
  await d.card(false);
  await d.sleep(400);

  // ---- 1. where the URL and token live: Settings → API proxy (real status from the running proxy)
  await d.caption(...C.intro);
  await d.openPanel();
  await d.click(panel.locator('#btn-settings'));
  await d.sleep(900);
  const proxyHead = panel.locator('#settings h3', { hasText: 'API proxy' });
  await proxyHead.evaluate((el) => el.scrollIntoView({ block: 'start' }));
  await d.sleep(2600);
  await d.click(panel.locator('#btn-settings-close'));
  await d.sleep(300);
  await d.stage('hidePanel');
  await d.sleep(500);

  // ---- 2. curl, streaming (real proxy; canned reply text)
  await d.caption(...C.curl);
  await d.stage('termClear');
  await d.stage('win', 'terminal', true);
  await d.sleep(400);
  await d.stage('termLine', '<span class="p">$</span> export O_PROXY_TOKEN=<span class="c">…(from Settings → API proxy)</span>', '');
  await d.sleep(600);
  await d.stage('termType', `curl -N ${url}/chat/completions -H "Authorization: Bearer $O_PROXY_TOKEN" -H "Content-Type: application/json" -d '{"model":"o-ai","stream":true,"messages":[{"role":"user","content":"A one-liner that counts lines in all *.py files"}]}'`, 14);
  await d.sleep(300);
  backend.proxy.setFake({ FAKE_TEXT: story.curlReply, FAKE_DELAY_MS: '140' });
  await d.caption(...C.stream);
  const r1 = await streamToTerm(d, 'curl', ['-sN', `${url}/chat/completions`, '-H', `Authorization: Bearer ${token}`, '-H', 'Content-Type: application/json',
    '-d', JSON.stringify({ model: 'o-ai', stream: true, messages: [{ role: 'user', content: 'A one-liner that counts lines in all *.py files' }] })], { lineMs: 40, cls: 'c' });
  d.mark('curl exit ' + r1.code);
  if (!/\[DONE\]/.test(r1.out)) throw new Error('curl did not get a [DONE] frame: ' + r1.out.slice(0, 300));
  await d.sleep(2600);

  // ---- 3. python openai client (real run)
  await d.caption(...C.python);
  const py = [
    'from openai import OpenAI',
    '',
    `client = OpenAI(base_url="${url}", api_key=O_PROXY_TOKEN)`,
    'r = client.chat.completions.create(',
    '    model="o-ai-code",',
    '    messages=[{"role": "user", "content": "Return {\\"ok\\": true} as JSON"}],',
    '    response_format={"type": "json_object"},',
    ')',
    'print(r.choices[0].message.content)',
  ];
  const hl = (l) => esc(l).replace(/\b(from|import)\b/g, '<span class="k">$1</span>').replace(/"[^"]*"/g, (m) => `<span class="s">${m}</span>`).replace(/\b(OpenAI|create|print)\b/g, '<span class="f">$1</span>');
  await d.stage('editor', py.map(hl).join('\n'));
  await d.stage('win', 'editor', true);
  await d.sleep(2800);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'o-d3-'));
  const script = path.join(tmp, 'ask_o.py');
  fs.writeFileSync(script, 'import os\nO_PROXY_TOKEN = os.environ["O_PROXY_TOKEN"]\n' + py.join('\n') + '\n');
  await d.stage('termType', 'python3 ask_o.py', 40);
  backend.proxy.setFake({ FAKE_TEXT: 'Sure! Here you go:\n```json\n' + story.pyReply + '\n```\nHope that helps!', FAKE_DELAY_MS: '60' });
  const r2 = await streamToTerm(d, 'python3', ['-I', script], { env: { O_PROXY_TOKEN: token }, lineMs: 60 });
  d.mark('python exit ' + r2.code);
  fs.rmSync(tmp, { recursive: true, force: true });
  if (!/"ok": true/.test(r2.out)) throw new Error('python client did not print the JSON: ' + r2.out.slice(0, 300));
  await d.sleep(2600);

  // ---- 4. the log has the API entries (real)
  await d.caption(...C.logged);
  await d.stage('win', 'editor', false);
  await d.stage('win', 'terminal', false);
  await d.stage('showPanel');
  await d.sleep(700);
  await d.click(panel.locator('#btn-actionlog'));
  await d.sleep(1200);
  const rows = panel.locator('#actionlog-list .alrow-head');
  const n = await rows.count();
  for (let i = n - 1; i >= 0; i--) {
    if (/API: \/v1\/chat\/completions o-ai-code/.test(await rows.nth(i).innerText())) { await d.click(rows.nth(i)); break; }
  }
  await d.sleep(3400);
  await d.caption(null);
  await d.sleep(600);
}

module.exports = { run };
