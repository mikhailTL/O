#!/usr/bin/env node
'use strict';
// Stand-in for `claude -p --output-format json` used by the engine's computer-use planner, grounder and judge
// (o_engine/cu/planner.py ClaudeCodeClient, judge.py ClaudeCodeJudge, ground.py ClaudeCodeGrounder) in the
// demo. It reads the prompt from stdin and answers with canned JSON so the demo needs no model:
//   plan prompt      -> an OScript whose 4th step looks for a button that does not exist ("Save As…"),
//                       so the first run fails there and the heal loop shows up on screen;
//   replan prompt    -> the one replacement step (find the real "Save" button);
//   judge prompt     -> {"ok": true};   grounder prompt -> {} (element absent; the tree is the truth).
// Every call is counted in FAKE_CU_STATE (a JSON file) so the demo can show "N model calls" honestly.

const fs = require('fs');

const argv = process.argv.slice(2);
if (argv[0] === '--version') { console.log('9.9.9 (Fake Claude, computer-use demo)'); process.exit(0); }

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => { input += d; });
process.stdin.on('end', () => {
  const stateFile = process.env.FAKE_CU_STATE || '';
  let state = { calls: 0, plans: 0, replans: 0, judges: 0, grounds: 0 };
  if (stateFile) { try { state = { ...state, ...JSON.parse(fs.readFileSync(stateFile, 'utf8')) }; } catch (_) {} }
  state.calls++;
  let result;
  if (/JSON verdict:/.test(input)) {
    state.judges++;
    result = JSON.stringify({ ok: true, reason: 'The editor shows the text and the document is saved.' });
  } else if (/JSON bounding box:/.test(input)) {
    state.grounds++;
    result = '{}';
  } else if (/Replacement step JSON object:/.test(input)) {
    state.replans++;
    result = JSON.stringify({ op: 'find', as: 'save', target: { role: 'push button', name: 'Save' } });
  } else {
    state.plans++;
    const goal = (/Goal: (.*)/.exec(input) || [])[1] || 'Type a note and save it';
    const app = (/App: (.*)/.exec(input) || [])[1] || 'notes';
    result = JSON.stringify({
      app, goal,
      steps: [
        { op: 'wait', for: { role: 'frame', name: 'Notes' }, timeout_s: 5 },
        { op: 'find', as: 'editor', target: { role: 'text', name: 'Editor' } },
        { op: 'type', ref: 'editor', text: 'Hello from O' },
        { op: 'find', as: 'save', target: { role: 'push button', name: 'Save As…' } },
        { op: 'click', ref: 'save' },
        { op: 'assert', target: { role: 'push button', name: 'Save', state: 'visible' } },
      ],
    });
  }
  if (stateFile) { try { fs.writeFileSync(stateFile, JSON.stringify(state)); } catch (_) {} }
  process.stdout.write(JSON.stringify({ type: 'result', subtype: 'success', is_error: false, result, session_id: 'fake-cu' }) + '\n');
});
