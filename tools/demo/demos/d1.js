'use strict';
// D1 "Set up a project": the user types a request, O runs a REAL provisioning job in the engine (sandboxed
// steps, GenUI job surface in Runs, action-log entries), then the same request again replays from the
// REAL script cache ("Déjà vu"). The chat turn (which tools Claude Code would call) is scripted.
const fs = require('fs');
const path = require('path');

/** One scripted Claude Code turn around a real engine job. Returns the final JobState. */
async function provisionTurn(d, prompt, { cached }) {
  const { story, backend } = d;
  d.task(prompt);
  await d.status('thinking', 'Ooh, a task! Let me think…');
  await d.sleep(1300);
  await d.tool('mcp__o__workspace_context', 'Using o: workspace context', { cwd: '.' }, 'thinking');
  let ctx = null;
  try { ctx = await backend.engine.call('GET', '/context?cwd=' + encodeURIComponent(backend.engine.sandboxRoot)); } catch (_) {}
  d.toolResult('mcp__o__workspace_context', true, JSON.stringify(ctx || {}).slice(0, 300));
  await d.sleep(900);
  if (!cached) await d.assistant('I\'ll set that up as one provisioning job: make the folder and README, write `hello.sh`, run it.');
  await d.sleep(cached ? 300 : 1400);
  await d.tool('mcp__o__provision', 'Using o: provision', story.request);
  const { job_id } = await backend.engine.call('POST', '/provision', story.request);
  d.mark('provision ' + job_id);
  return { job_id, wait: () => backend.engine.waitJob(job_id, { timeoutMs: 120000 }) };
}

async function run(d) {
  const { story, page, panel, backend } = d;
  const C = story.captions;
  await d.card(true, 'Set up a project');
  await d.sleep(2200);
  await d.card(false);
  await d.sleep(500);

  // ---- 1. the pet
  await d.caption(...C.meet);
  await d.moveStage(1150, 560, 800);
  await d.moveStage(1320, 700, 700);
  await d.moveStage(900, 420, 900);
  await d.sleep(1400);

  // ---- 2. ask
  await d.caption(...C.ask);
  await d.openPanel();
  await d.type(panel.locator('#prompt'), story.prompt, 24);
  await d.sleep(500);
  await panel.locator('#prompt').press('Enter');
  const prompt = await d.nextPrompt();

  // ---- 3. the job (real)
  await d.caption(...C.engine);
  const job = await provisionTurn(d, prompt, { cached: false });
  await d.sleep(500);
  // open Runs while the job is live
  await d.click(panel.locator('#btn-runs'));
  await d.caption(...C.runs);
  const state = await job.wait();
  await d.sleep(2600);
  d.toolResult('mcp__o__provision', state.ok, JSON.stringify({ ok: state.ok, cached: state.cached, phase: state.phase, steps: state.steps.length }));
  await d.click(panel.locator('#btn-runs-close'));
  await d.sleep(400);
  await d.assistant(story.reply);
  await d.done('demo-app is set up and hello.sh ran.', 6200);
  await d.sleep(2600);

  // ---- 4. the log (real)
  await d.caption(...C.log);
  await d.click(panel.locator('#btn-actionlog'));
  await d.sleep(2800);
  const rows = panel.locator('#actionlog-list .alrow-head');
  const n = await rows.count();
  if (n) await d.click(rows.nth(Math.max(0, n - 5)));
  await d.sleep(2200);
  await d.click(panel.locator('#btn-actionlog-close'));
  await d.sleep(500);

  // ---- 5. the same request again -> cache replay (real)
  await d.caption(...C.again);
  await d.type(panel.locator('#prompt'), story.prompt, 14);
  await panel.locator('#prompt').press('Enter');
  const prompt2 = await d.nextPrompt();
  const job2 = await provisionTurn(d, prompt2, { cached: true });
  const state2 = await job2.wait();
  d.toolResult('mcp__o__provision', state2.ok, JSON.stringify({ ok: state2.ok, cached: state2.cached, phase: state2.phase, steps: state2.steps.length }));
  if (state2.cached) await d.status('success', 'Déjà vu — replaying a saved script, no thinking needed');
  await d.sleep(2200);
  await d.assistant(state2.cached ? story.replyCached : story.reply);
  await d.done('Replayed the saved script.', 900);
  await d.sleep(1800);

  // ---- 6. what is on disk (real listing of the job sandbox)
  await d.caption(...C.files);
  const jobDir = path.join(backend.engine.sandboxRoot, job.job_id);
  const entries = [];
  try {
    for (const name of fs.readdirSync(jobDir)) {
      const full = path.join(jobDir, name);
      const dir = fs.statSync(full).isDirectory();
      entries.push({ name, dir });
      if (dir) for (const inner of fs.readdirSync(full)) entries.push({ name: name + '/' + inner, dir: false });
    }
  } catch (_) {}
  await d.stage('files', '~/o-sandbox/' + job.job_id.slice(0, 12) + '…', entries);
  await d.stage('win', 'files', true);
  await d.sleep(3400);
  await d.caption(null);
  await d.sleep(600);
}

module.exports = { run };
