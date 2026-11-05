'use strict';
// D2 "The log is the product": a real job fills the real action log; the panel's log sheet filters and
// expands rows; Verify chain passes; then one byte of o-actions.jsonl is changed on disk and Verify chain
// reports ✗ broken at #n. The chat turn is scripted; everything in the log sheet is the real file.
const fs = require('fs');

async function run(d) {
  const { story, panel, backend } = d;
  const C = story.captions;
  await d.card(true, 'The log is the product');
  await d.sleep(2200);
  await d.card(false);
  await d.sleep(400);

  // ---- 1. a quick real job so the log has substance
  await d.caption(...C.intro);
  await d.openPanel();
  await d.type(panel.locator('#prompt'), story.prompt, 18);
  await panel.locator('#prompt').press('Enter');
  const prompt = await d.nextPrompt();
  d.task(prompt);
  await d.status('thinking', 'Ooh, a task! Let me think…');
  await d.sleep(900);
  await d.tool('mcp__o__workspace_context', 'Using o: workspace context', { cwd: '.' }, 'thinking');
  d.toolResult('mcp__o__workspace_context', true, '{"cwd": "~/o-sandbox"}');
  await d.sleep(700);
  await d.tool('mcp__o__provision', 'Using o: provision', story.request);
  const { job_id } = await backend.engine.call('POST', '/provision', story.request);
  const state = await backend.engine.waitJob(job_id);
  d.toolResult('mcp__o__provision', state.ok, JSON.stringify({ ok: state.ok, cached: state.cached, steps: state.steps.length }));
  await d.sleep(800);
  await d.assistant(story.reply);
  await d.done('Scratch workspace ready.', 4100);
  await d.sleep(1800);

  // ---- 2. open the log
  await d.caption(...C.open);
  await d.click(panel.locator('#btn-actionlog'));
  await d.sleep(2600);

  // ---- 3. filters
  await d.caption(...C.filter);
  const chip = (f) => panel.locator(`#actionlog-filters .filter[data-filter="${f}"]`);
  await d.click(chip('engine'));
  await d.sleep(1800);
  await d.click(chip('policy'));
  await d.sleep(1600);
  await d.click(chip('tools'));
  await d.sleep(1600);
  await d.click(chip('all'));
  await d.sleep(900);

  // ---- 4. expand a row (a real engine result entry)
  await d.caption(...C.expand);
  const rows = panel.locator('#actionlog-list .alrow-head');
  const n = await rows.count();
  let target = null;
  for (let i = n - 1; i >= 0 && !target; i--) {
    const txt = await rows.nth(i).innerText();
    if (/step 1 .*exit|result/i.test(txt)) target = rows.nth(i);
  }
  await d.click(target || rows.nth(Math.max(0, n - 4)));
  await d.sleep(2800);

  // ---- 5. verify
  await d.caption(...C.verify);
  await d.click(panel.locator('#btn-actionlog-verify'));
  await d.sleep(2600);
  const chain = backend.actionLog.verifyChain();
  d.mark('verify ' + JSON.stringify(chain));

  // ---- 6. tamper on disk (one character inside the summary of an entry near the middle)
  await d.caption(...C.tamper);
  const file = backend.actionLog.paths().jsonl;
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  const idx = Math.max(1, Math.floor((lines.length - 1) * 0.6));
  const e = JSON.parse(lines[idx]);
  const before = e.summary;
  e.summary = e.summary.replace(/ok/, 'OK').replace(/^(.)/, (m) => (m === 'g' ? 'G' : m === 'o' ? '0' : m.toUpperCase()));
  if (e.summary === before) e.summary = before + ' ';
  lines[idx] = JSON.stringify(e);
  await d.stage('termClear');
  await d.stage('termLine', `<span class="p">$</span> sed -n '${idx + 1}p' ~/.config/o-ai/o-actions.jsonl | cut -c1-96`, '');
  await d.stage('termLine', lines[idx].slice(0, 96) + '…', 'c');
  await d.stage('termLine', `<span class="p">$</span> <span class="y"># edit line ${idx + 1}: "${before.slice(0, 40)}" → "${e.summary.slice(0, 40)}"</span>`, '');
  await d.stage('win', 'terminal', true);
  await d.sleep(2600);
  fs.writeFileSync(file, lines.join('\n'));
  d.mark('tampered line ' + (idx + 1));
  await d.stage('termLine', '<span class="b">written.</span> hash of that entry no longer matches its contents', '');
  await d.sleep(2200);
  await d.stage('win', 'terminal', false);
  await d.sleep(500);

  // ---- 7. verify again
  await d.caption(...C.broken);
  await d.click(panel.locator('#btn-actionlog-verify'));
  await d.sleep(3200);
  const chain2 = backend.actionLog.verifyChain();
  d.mark('verify ' + JSON.stringify(chain2));
  if (chain2.ok) throw new Error('tamper did not break the chain');
  await d.sleep(1800);
  await d.caption(null);
  await d.sleep(600);
}

module.exports = { run };
