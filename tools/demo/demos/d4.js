'use strict';
// D4 "Computer use without touching your mouse": the REAL cu graph (/computer/run, /computer/replay) runs
// against the engine's FakeBackend fed with tools/demo/cu-tree.json (O_CU_FAKE_TREE). The planner, grounder and
// judge are fake-claude-cu.js, which answers with a plan whose 4th step targets a button that does not exist,
// so the heal loop and the procedure store are exercised for real. The "Notes" window on the stage is drawn
// from the same tree and animated from the engine's own cu-step log entries, paced for the camera.
const fs = require('fs');
const path = require('path');

const TREE = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'cu-tree.json'), 'utf8'));

function fakeState(backend) {
  try { return JSON.parse(fs.readFileSync(path.join(backend.dataDir, 'fake-cu-state.json'), 'utf8')); } catch (_) { return { plans: 0, replans: 0 }; }
}

/** Collect this job's "cu step" entries from the live log stream. */
function collectSteps(backend, jobId) {
  const steps = [];
  const onEv = (ev) => {
    if (ev.type !== 'log') return;
    const e = ev.entry;
    if (!e.summary.startsWith(jobId + ': cu step')) return;
    steps.push(e);
  };
  backend.on('event', onEv);
  return { steps, stop: () => backend.off('event', onEv) };
}

/** Play one executed step into the Notes pane. */
async function playStep(d, entry) {
  const m = /cu step (\d+) (\w+) (.*?)( -> (ok|failed))?$/.exec(entry.summary);
  if (!m) return;
  const [, , op, rest, , verdict] = m;
  const isResult = entry.kind === 'result';
  if (!isResult) {
    if (op === 'wait') await d.stage('notesHi', 'w0');
    else if (op === 'find' && /Editor/.test(rest)) await d.stage('notesHi', 'e0');
    else if (op === 'find' && /name='Save'/.test(rest)) await d.stage('notesHi', 'b3');
    else if (op === 'find') await d.stage('notesMiss', rest.replace(/^as \w+: /, ''));
    else if (op === 'type') { await d.stage('notesHi', 'e0'); await d.stage('notesType', 'Hello from O', 55); }
    else if (op === 'click') { await d.stage('notesHi', 'b3'); await d.stage('notesPress', 'b3'); await d.stage('notesStatus', 'Saved · Untitled note'); }
    else if (op === 'assert') await d.stage('notesHi', 'b3');
    await d.sleep(op === 'type' ? 500 : 750);
  } else {
    await d.sleep(verdict === 'failed' ? 1600 : 350);
  }
}

async function run(d) {
  const { story, panel, backend } = d;
  const C = story.captions;
  await d.card(true, 'Computer use without touching your mouse');
  await d.sleep(2200);
  await d.card(false);
  await d.sleep(300);

  // ---- 1. the app and its tree
  await d.caption(...C.tree);
  await d.stage('notesTree', TREE);
  await d.stage('win', 'notes', true);
  await d.stage('hud', 0, 0, '—');
  await d.stage('setPos', 1376, 752);
  await d.sleep(3200);

  // ---- 2. ask
  await d.openPanel();
  await d.type(panel.locator('#prompt'), story.prompt, 22);
  await panel.locator('#prompt').press('Enter');
  const prompt = await d.nextPrompt();
  d.task(prompt);
  await d.status('thinking', 'Ooh, a task! Let me think…');
  await d.sleep(900);
  await d.tool('mcp__o__computer_tree', 'Using o: computer tree', { window: 'Notes' }, 'thinking');
  const tree = await backend.engine.call('GET', '/computer/tree');
  d.toolResult('mcp__o__computer_tree', true, JSON.stringify(tree).slice(0, 300));
  await d.sleep(700);
  await d.tool('mcp__o__computer_run', 'Using o: computer run', { goal: story.goal, app: story.app });

  // ---- 3. the real run (fast); its steps are replayed into the pane at a watchable pace
  await d.caption(...C.plan);
  const { job_id } = await backend.engine.call('POST', '/computer/run', { goal: story.goal, app: story.app });
  const col = collectSteps(backend, job_id);
  const state = await backend.engine.waitJob(job_id, { timeoutMs: 180000 });
  col.stop();
  d.mark(`cu run ok=${state.ok} attempt=${state.attempt} proc=${state.procedure_id}`);
  if (!state.ok) throw new Error('cu run failed: ' + state.error);
  const fs1 = fakeState(backend);
  // phase 1: the first execution up to the failure
  let failedShown = false, healShown = false;
  let shownHeal = false;
  for (const e of col.steps) {
    const failed = /-> failed$/.test(e.summary);
    const stepNo = Number((/cu step (\d+)/.exec(e.summary) || [])[1]);
    if (!failedShown && e.kind === 'tool' && /Save As/.test(e.summary)) { await d.caption(...C.fail); failedShown = true; }
    if (failedShown && !shownHeal && e.kind === 'tool' && stepNo === 0) {
      // the healed script re-executes from step 0
      await d.stage('hud', fs1.plans, 1, '—');
      await d.caption(...C.heal);
      await d.stage('notesUnmiss');
      await d.stage('notesType', '', 1);
      await d.stage('notesStatus', 'Ready');
      shownHeal = true;
      await d.sleep(900);
    }
    if (!failedShown) await d.stage('hud', fs1.plans, 0, '—');
    await playStep(d, e);
    if (failed) healShown = true;
  }
  await d.stage('notesHi', null);
  await d.stage('hud', fs1.plans, state.attempt, state.procedure_id ? state.procedure_id.replace(/\/[0-9a-f]{64}\//, '/…/') : '—');
  d.toolResult('mcp__o__computer_run', true, JSON.stringify({ ok: state.ok, attempt: state.attempt, procedure_id: state.procedure_id }));
  await d.sleep(600);
  await d.assistant(story.reply);
  await d.done('Typed the note and saved it.', 9800);
  await d.sleep(1800);

  // ---- 4. Runs sheet (real GenUI job surface for the cu job)
  await d.caption(...C.runs);
  await d.click(panel.locator('#btn-runs'));
  await d.sleep(3600);
  await d.click(panel.locator('#btn-runs-close'));
  await d.sleep(400);

  // ---- 5. replay (real): zero planner calls
  await d.caption(...C.replay);
  await d.stage('notesType', '', 1);
  await d.stage('notesStatus', 'Ready');
  await d.type(panel.locator('#prompt'), story.prompt, 12);
  await panel.locator('#prompt').press('Enter');
  const prompt2 = await d.nextPrompt();
  d.task(prompt2);
  await d.status('thinking', 'Ooh, a task! Let me think…');
  await d.sleep(600);
  await d.tool('mcp__o__computer_replay', 'Using o: computer replay', { procedure_id: state.procedure_id });
  const r2 = await backend.engine.call('POST', '/computer/replay', { procedure_id: state.procedure_id });
  const col2 = collectSteps(backend, r2.job_id);
  const state2 = await backend.engine.waitJob(r2.job_id, { timeoutMs: 180000 });
  col2.stop();
  const fs2 = fakeState(backend);
  d.mark(`cu replay ok=${state2.ok} cached=${state2.cached} plans ${fs1.plans}->${fs2.plans} replans ${fs1.replans}->${fs2.replans}`);
  if (!state2.ok || !state2.cached) throw new Error('replay did not run from the saved procedure');
  if (fs2.plans !== fs1.plans || fs2.replans !== fs1.replans) throw new Error('replay made a planner call');
  await d.status('success', 'Déjà vu — replaying a saved procedure, no thinking needed');
  for (const e of col2.steps) { await playStep(d, e); }
  await d.stage('notesHi', null);
  await d.stage('hud', fs2.plans, 0, state2.procedure_id ? state2.procedure_id.replace(/\/[0-9a-f]{64}\//, '/…/') : '—');
  d.toolResult('mcp__o__computer_replay', true, JSON.stringify({ ok: state2.ok, cached: state2.cached, procedure_id: state2.procedure_id }));
  await d.assistant(story.replyReplay);
  await d.done('Replayed the saved procedure.', 1200);
  await d.sleep(3000);
  await d.caption(null);
  await d.sleep(600);
}

module.exports = { run };
