# O AI — architecture

O is opia's desktop shell (a small always-there window, chat panel, non-focus-stealing cards, Claude Code CLI as the brain,
O Link into the user's own browser tabs) with a Python provisioning engine underneath and one action log
that records everything either side does. This document describes the components, the merged dataflow,
the two decisions that reconcile the source specs, the action log, its threat model, isolation, and the
hard requirements. File paths are given as the build plan names them. No claim of completeness is made
about the code.

## 1. Components

### Electron shell (`main/`, `renderer/`, `extension/`)

| Component | File | One line |
|---|---|---|
| Pet window | `main/main.js`, `renderer/index.html`, `renderer/sprite.js`, `renderer/behavior.js` | Transparent always-on-top sprite; moods and status lines; never takes focus. |
| Panel | `main/main.js`, `renderer/panel.html`, `renderer/panel.js`, `renderer/panel.css` | Chat, Settings sheet, and the Action log sheet (header button with unread badge). |
| Notify cards | `main/main.js`, `renderer/notify.html`, `renderer/notify.js` | Non-focus-stealing cards; the only place an `ask`-mode approval is shown. |
| Tray | `main/main.js` | Tray icon and menu, including "Open action log". |
| O Link relay + extension | `main/link.js`, `extension/background.js`, `extension/guard.js`, `extension/manifest.json` | WebSocket relay that presents the user's own Chrome tabs as a CDP endpoint to Playwright MCP; the in-page guard refuses payment sites and payment fields. |
| Claude Code CLI agent wrapper | `main/agent.js` | Spawns `claude -p` with the user's own login, queues prompts, applies the permission mode, records every `tool_use` / `tool_result`, serves `POST /approve`, `/screen`, `/log`. |
| Bridge MCP server | `main/tools/bridge-mcp.js` | MCP server named `o` (tool ids `mcp__o__<name>`): `approve`, `see_screen`, `log_note`, `provision`, `cache_lookup`, `diagnose`, `verify_port`, `workspace_context`, (§9, §10) `computer_*`, `mcp_tools`, `mcp_call`, `script_run`, `swarm_run`, `web_run`, and (§11) `ui_emit`; forwards to the engine over HTTP, `ui_emit` to the app's `/ui`. |
| Engine process manager | `main/engine.js` | Finds Python, spawns `python -m o_engine serve` on a free port with the shared token, waits for `/health`, restarts with backoff. |
| Action log (app side) | `main/actionlog.js` | Append-only, hash-chained `o-actions.jsonl` plus text twin; `record`, `ingest`, `tail`, `since`, `markRead`, `verifyChain`. |
| Settings, preload | `main/settings.js`, `main/preload.js` | `permissionMode` (`log` / `ask` / `auto`), engine settings, `window.o` bridge for the renderer. |

### Python engine (`engine/o_engine/`)

| Component | File | One line |
|---|---|---|
| Schema | `schema.py` | Strict, frozen pydantic models: `ProvisionStep`, `VerifyTarget`, `ProvisionRequest` (with `.fingerprint(platform)`); `SchemaViolation` on anything non-conforming. |
| Context | `context.py` | Ambient `WorkspaceProfile` from shell history, a shallow tree, config-file hashes, and screenpipe's recent OCR/window titles; local SQLite `context.db`; asks nothing. |
| Token filter + parser | `tokenfilter.py`, `parser.py` | `TokenFilter` passes only the first balanced JSON value of a token stream; `Parser` tries local vLLM first, falls back once to `claude -p`, else raises `SchemaViolation`. |
| Sandbox executor | `executor.py` | `Sandbox(root)`: paths must resolve inside root; `run(step)` via `subprocess.run` with timeout, `stdin=DEVNULL`, scrubbed env; `apply_patch` for surgical find/replace edits. |
| Isolated display | `display.py` | `IsolatedDisplay(':99')` starts `Xvfb :99 -screen 0 1280x800x24` on Linux; `.available` is false elsewhere. |
| Script cache | `cache.py` | Append-only `ScriptCache`: `lookup`, `store` (never overwrite, never delete), `replay`, `index`. |
| Graph + gate + remediator | `graph.py`, `gate.py`, `remediate.py` | langgraph `StateGraph` with literal edges; `Gate(mode, waiter)` for `log` / `ask` / `auto`; `Remediator` turns stderr into replacement steps or patches. |
| Verify | `verify.py` | Headless Playwright `GET` of a local port and path, expecting a status code; `httpx` stand-in when Playwright is absent, and the result says so. |
| Diagnose | `diagnose.py` | stderr scanned line by line for the first recognisable error, category and missing package lifted out, recent telemetry attached, parser asked for steps; static JSON out. |
| Action log (engine side) | `actionlog.py` | Append-only, hash-chained `engine-actions.jsonl` plus text twin; each entry also POSTed to the app at `O_LOG_URL`. |
| Server | `server.py` | FastAPI on `127.0.0.1`, `x-o-token` required; the HTTP table from the plan; jobs in a thread pool, each in its own sandbox subdirectory. |
| CLI | `cli.py`, `__main__.py` | `o-engine serve | run <file.json> | cache ls | context [cwd] | check`, plus `computer run|procedures`, `tools ls|call`, `script run`, `swarm run`, `web run`. |

## 2. Dataflow

### Merged lifecycle

Spec 1 contributes strict serialization, a gate before execution, headless port verification, and a script
cache. Spec 2 contributes ambient context, the token filter, headless isolation, and bounded
self-correction. One request passes through both, in this order:

1. **Context aggregation** — `context.profile(cwd)` builds the `WorkspaceProfile` from the local DB and the
   last 120 s of screenpipe telemetry. No question is put to the user.
2. **Token pruning and strict schema** — model output (local vLLM, or `claude -p` as fallback) streams
   through `TokenFilter`; only the JSON value reaches `parse_steps`; anything with prose, fences, or extra
   keys is a `SchemaViolation` and the request is rejected before anything runs.
3. **Cache lookup** — the request's fingerprint is looked up in the script cache; a hit skips inference
   and replays the stored script as a fixed utility.
4. **Gate** — `Gate.check` applies the permission mode: `log` records a policy entry and passes, `auto`
   passes, `ask` waits up to 60 s for `/jobs/{id}/approve`.
5. **Headless sandbox execution** — each step runs under `Sandbox.run` inside the sandbox root, with
   `DISPLAY=:99` when the request asks for an isolated display.
6. **Verify** — if the request names a `VerifyTarget`, headless Playwright fetches the port.
7. **Bounded self-correction** — on a non-zero exit or a failed verify, `Remediator` reads stderr line by
   line, asks the parser for replacement steps or a `patch`, applies patches through the sandbox, and the
   graph returns to the gate; the attempt counter lives in state and `max_attempts` bounds it.
8. **Cache store** — on exit 0 the rendered script is written under its fingerprint; then **report**.

### A prompt's path

```
user types in panel
   |
   v
renderer/panel.js --ipc--> main/main.js --> main/agent.js (prompt queue)
                                               |
                                               | spawn: claude -p ... --mcp-config --permission-prompt-tool mcp__o__approve
                                               v
                                        Claude Code CLI
                                               |
                                               | tool call: mcp__o__provision {ProvisionRequest}
                                               v
                                  main/tools/bridge-mcp.js  (MCP server "o")
                                               |
                                               | POST /provision  (x-o-token)  -> {job_id}; polls GET /jobs/{id}
                                               v
                                  engine/o_engine/server.py  (FastAPI, 127.0.0.1:<port>)
                                               |
                                               | run_job(graph, request)
                                               v
             context -> validate -> cache_lookup -> gate -> execute -> verify -> cache_store -> report
                                                    ^                    | (fail)
                                                    +---- remediate <----+
                                               |
                                               | Sandbox.run(step)  in  <sandboxRoot>/<job>/...
                                               v
                                      subprocess (real command, sandbox cwd, DISPLAY=:99 if isolated)
                                               |
                   engine ActionLog.record (engine-actions.jsonl) --POST O_LOG_URL--> agent.js /log
                                                                                        |
                                                                                        v
                                                                      main/actionlog.js ingest -> o-actions.jsonl
                                                                                        |
                                                                   broadcast {type:'log', entry} --> panel Action log sheet
```

The bridge returns the final `JobState` to Claude Code as the tool result; the agent wrapper records that
`tool_result` and the reply reaches the panel through the normal chat stream.

## 3. Reconciliation decisions

Both specs are prescriptive and they disagree in two places. The builder resolved each as follows.

**(a) Gating.** Spec 1 forbids autonomous execution: every command waits for a human click or `Y`, and a
denial or a 60-second timeout destroys the queue. Spec 2 wants zero manual input. The owner chose
**log-only as the default**: every action is recorded in the action log and nothing asks. The two other
behaviours are kept as the `permissionMode` setting: `ask` (a blocking card; 60 s; timeout or denial
raises `QueueDestroyed`, which empties the job queue) and `auto` (no gating, no cards). `gate.py`
implements all three; the default is `log`. Security comes from the record, not from a prompt.

**(b) Graph shape.** Spec 1 requires a linear DAG with hardcoded exit conditions that cannot rewrite its
own routing. Spec 2 requires a cyclic, self-correcting graph that re-tests recursively. These were
resolved as **a fixed topology containing exactly one bounded cycle**: `remediate -> gate`. The edges
are literal in `graph.py`; no node may add, remove or change an edge at runtime; the only cycle is
remediation, and `max_attempts` (a setting, `maxRemediationAttempts`, default 3) bounds it. When the
counter is exceeded the graph goes to `report` with `ok=false`. Spec 1's determinism is preserved
(the routing is static and compiled once) and spec 2's self-correction is preserved (the loop exists).

## 4. The action log

Entry format, as fixed in the plan's Shared contracts:

```json
{
  "ts": 1730000000.123,
  "iso": "2026-11-04T22:30:00",
  "source": "app" | "engine",
  "kind": "task" | "tool" | "result" | "policy" | "graph" | "cache" | "remediate" | "verify" | "settings" | "note",
  "summary": "one human line",
  "detail": { },
  "prev": "<sha256 of previous entry>",
  "hash": "<sha256 of this entry without hash>"
}
```

Files. App side: `<userData>/o-actions.jsonl` and the text twin `o-actions.log`. Engine side:
`<dataDir>/engine-actions.jsonl` and `engine-actions.log`. Each side hashes a canonical (key-sorted)
serialisation of the entry without `hash`; `prev` is the previous entry's hash, and the first entry's
`prev` is sixty-four zeros. Both sides append under a single writer lock, so two parallel jobs' entries
still chain correctly.

Forwarding. The engine POSTs each entry to the app at `O_LOG_URL` with the `x-o-token` header. The
agent wrapper's `POST /log` hands it to `actionlog.ingest`. Ingest **does not recompute the engine's
hash**: the engine's `hash` and `prev` are kept in the entry under `detail.engine_hash` and
`detail.engine_prev`, `source` stays `engine`, and the app assigns its own `prev` and `hash` so the entry
chains into the app file. The engine file remains the engine's own record; if the forward fails, the
engine file still has the entry.

"Verify chain" (footer button in the Action log sheet; `verifyChain()` in `main/actionlog.js`) re-reads
the app file from the first line, checks that each line parses, that each `prev` equals the previous
line's `hash`, and that each `hash` equals the recomputed hash of the entry. It reports `ok`, the entry
count, and on failure the zero-based index of the first broken entry and why (`unparseable`, `prev`,
`hash`).

## 5. Threat model

What the log proves. Given a file that was not replaced wholesale, an intact chain shows that the entries
present are in the order they were written and that none has been edited since it was written: changing
any field changes that entry's hash, which breaks the next entry's `prev`. Engine entries carry their own
engine-side hash as well, so the app copy can be matched against the engine file.

What it does not prove. There is no remote anchoring and no signature; a local attacker with write access
to the user's data directory can truncate the file and re-chain the remainder, or delete the file and
start over, and "Verify chain" will pass. The log is a record for the user, not evidence against the
user's own machine. Secrets are not in the log: the sandbox executor scrubs `CLAUDE*`, `ANTHROPIC*`,
`O_*` and any variable whose name contains `TOKEN`, `SECRET`, `KEY` or `PASSWORD` before a command runs,
and the engine's own `x-o-token` is never written as an entry. Command stdout and stderr tails are
recorded, so a command that prints a secret puts it in the log.

Where O's hard refusals remain. The O Link relay refuses to navigate to payment hosts and the in-page
guard blocks payment fields and buttons; that guard is unchanged and is not subject to the permission
mode. The engine never asks the user anything and never touches the host display.

What runs with the user's privileges. A sandbox step is a real shell command run as the user. The
sandbox confines `target_directory` (and patch paths) to the sandbox root with symlinks resolved; it does
not confine syscalls, network access, or what a command does to the rest of the filesystem by absolute
path. `npm install` in the sandbox is `npm install`. The script cache replays stored scripts as-is; a
cached script that now fails is treated as a failure and remediated, never deleted.

## 6. Isolation

- Nothing drives the host mouse, keyboard, or primary display. `see_screen` reads a screenshot; it does
  not act.
- Browser work in the user's own tabs goes through O Link: the extension attaches `chrome.debugger` to a
  tab and the relay presents it to Playwright MCP as a CDP endpoint over a local WebSocket. Only
  attached tabs are reachable.
- Port verification and anything that needs a fresh browser use headless Playwright Chromium; no window
  is opened.
- Background UI work on Linux runs on `Xvfb :99`; the engine starts it if it is not already running and
  passes `DISPLAY=:99` to isolated steps. The host `DISPLAY` is never read or changed. On macOS and
  Windows there is no isolated display: `IsolatedDisplay.available` is false, `/health` reports `xvfb`
  false, and only headless mode is offered.

## 7. Hard requirements

| Requirement | Why it exists |
|---|---|
| Node 18+ and the Claude Code CLI | The shell is Electron; the brain is `claude -p` with the user's own login. No API key anywhere. |
| Python 3.10+ | The engine is a Python package (`pydantic`, `langgraph`, `fastapi`, `uvicorn`, `httpx`, `openai`, `playwright`). |
| vLLM, OpenAI-compatible at `http://127.0.0.1:8000/v1` | Error text and goals are parsed into steps locally; nothing leaves the machine for parsing. `claude -p` is the once-only fallback when vLLM is down. |
| screenpipe, local SQLite | Crash context: the last 120 s of OCR text and window titles are attached to diagnostics and the workspace profile. Absent, context still builds from history and tree. |
| Playwright + Chromium | Headless port verification after a provisioning run, without rendering a window. |
| Xvfb on Linux (`:99`) | A background display for UI work that must not touch the user's screen. |

`/health` reports each probe as a boolean and Settings shows them green or red with a one-line fix.

## 8. API proxy

An OpenAI-compatible HTTP server in the Electron main process (`main/proxy.js`, `createProxy({getSettings,
actionLog, getEngine, spawn?})` → `{start, stop, status}`). It turns API requests into `claude -p` runs
under the user's own login and normalizes the stream back into standard JSON / SSE.

| Component | File | One line |
|---|---|---|
| Proxy server | `main/proxy.js` | Listens on `127.0.0.1:<proxyPort>` only; `Authorization: Bearer <proxyToken>` on every request; routes `GET /health`, `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/run`. |
| Prompt modifier | `main/promptmod.js` | `buildSystemPrompt({mode:'code'|'chat', extra})`; `stripPersona(text)` applies the ordered `RULES` list. |
| Token filter (node) | `main/tokenfilter.js` | Port of `engine/o_engine/tokenfilter.py`: first balanced JSON value only. |
| Selftest | `main/tools/selftest-proxy.js` | Spawns the proxy with `spawn` injected to run `main/tools/fake-claude.js`. |
| Wiring | `main/main.js`, `main/settings.js`, `main/preload.js` | Start/stop with the app and on settings change; `proxyStatus()`; Settings → "API proxy" (URL, masked token, on/off). |

Dataflow:

```
HTTP request (bearer checked)
   |
   v
main/promptmod.js  buildSystemPrompt(mode)  +  caller `system` merged after stripPersona
   |
   | spawn: claude -p --output-format stream-json --verbose --max-turns N
   |        --permission-prompt-tool mcp__o__approve --mcp-config <same file agent.js writes> --allowedTools ...
   |        system prompt via --system-prompt-file (detected once from `claude --help`; else --append-system-prompt-file)
   |        conversation flattened to one prompt on stdin
   v
claude -p child  (stream-json)
   |
   v
normalizer:  assistant text deltas -> chat.completion.chunk ;  tool_use -> tool_calls ;  result -> finish_reason + usage
   |         (o-ai-code or response_format json_object: text through main/tokenfilter.js first)
   v
SSE frames `data: {...}\n\n` ... `data: [DONE]`   |   one JSON body (+ x-o-cost-usd, x-o-duration-ms headers)
```

`POST /v1/run {objective, cwd?}` is O-native: engine `/context` for state recovery → script cache lookup by
objective fingerprint → on a hit, replay through engine `/provision` with the cached request; else one
code-mode `claude -p` call for strict steps, parsed by the TokenFilter, posted to `/provision`; the
`JobState` is returned.

Concurrency. Up to `proxyConcurrency` (default 4) child processes; a queue beyond that. Each run is its own
sessionless `claude` process (no `--resume`); nothing is shared between requests except the settings and
the action log.

What is logged. Per request a `task` entry (`API: <route> <model> <clip of last user message>`) and a
`result` entry on completion; the child's tool calls still reach the log through the bridge as `tool` /
`result` / `policy` entries. The bearer token is never written to the log.

Two decisions (builder's, stated):

**(a) No permission-skipping flag.** The source spec's `CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS` is not used.
`log` mode already un-gates every tool call, but through the bridge, so each call is a logged `policy`
entry; skipping permissions at the CLI would remove those entries. `--dangerously-skip-permissions` is
ruled out for the same reason.

**(b) Persona stripped, safety kept.** The spec's "prompt interception" strips O's pet persona, prose
styling and reply-length rules for API callers and injects a code-only template (answer with code or
structured JSON only; no preamble). The money, untrusted-content and no-credentials rules are kept
verbatim from `agent.js` `PERSONA_TEMPLATE` and are never stripped.

Ruled out: non-loopback interfaces; running without a bearer token.

## 9. Computer use

A second subsystem in the engine (`engine/o_engine/cu/`) that drives desktop apps through their
accessibility APIs, with vision as the fallback, and saves what worked as replayable procedures. Target
stated in the plan: surpass Simular's Agent S3/S4 on axes O can prove in-repo; no benchmark number is
claimed by the build.

### Components

| Component | File | One line |
|---|---|---|
| OScript schema | `cu/schema.py` | Strict, frozen pydantic, `extra=forbid`: `{app, goal, steps:[{op, ...}]}`; ops `launch`, `wait`, `find`, `click`, `type`, `key`, `set_value`, `invoke`, `assert`, `screenshot_assert`, `state_assert`, `script`, `call`; `target` = `role`, `name` (glob), `text`, `index`, `within`; `Milestone{goal, done_when: Target | question}`. |
| Backends | `cu/backends/{base,atspi,uia,ax}.py` | `CUBackend`: `tree`, `find`, `click`, `invoke`, `set_value`, `type_text`, `key`, `screenshot`, `focused_window`. `atspi.py` (Linux, pyatspi, `DISPLAY` from `IsolatedDisplay`), `uia.py` (Windows, `uiautomation`), `ax.py` (macOS, `atomacos`/pyobjc). Import failure → `available=False`; `/health` reports `cu_backend`. |
| Executor | `cu/executor.py` | Runs an OScript against a backend; one action log entry per step with before/after tree digests and screenshot hashes; missing element → structured failure. |
| Grounding | `cu/ground.py` | `Grounder` chain: `LocatorCache` (`locators.sqlite`, keyed by app, target, screen hash; insert-or-ignore) → `VisionGrounder` (local VLM, OpenAI vision format, model `vllmVisionModel`, bbox parsed through `TokenFilter`) → `ClaudeCodeGrounder` (`claude -p` with the image path). |
| Judge | `cu/judge.py` | `a11y_assert` checks `assert` steps on the accessibility tree; `StateJudge.verify` checks `state_assert` against the sandbox and backend with no model (shell probes only from `cu/probes.py`'s allow-list); `screenshot_assert` / goal questions answered by `VLMJudge` (local VLM, `ClaudeCodeJudge` fallback) as a `JudgeVerdict` (yes/no + reason). |
| Feasibility gate | `cu/feasibility.py` | `FeasibilityGate.check(goal, app, job_id)`: read-only probes (`tree`, `screenshot`, `ls`, `read_file`, `which`; never click/type) + one `complete_json` question → `{feasible, reason}`; any model failure is `feasible=True` ("gate unavailable"). |
| Planner, best-of-N | `cu/planner.py` | `plan_milestones(goal, app, notes, memory) -> Milestone[]` once, `next_steps(milestone, tree_summary, memory, ...) -> OScript` (≤ `cuStepsPerTurn`) per turn, `candidate_steps(k)` for per-step candidates, `replan_step` for heals; `BestOfN(n, comparative)` from `cuCandidates`/`cuComparativeRanking`. Every prompt is `STATIC_PREFIX` + memory block + dynamic context. |
| Working memory | `cu/memory.py` | `WorkingMemory{facts, state, todos, risks}` (40 items each, goal-seeded constraints pinned); `extract` once per judged step via the local model; `render` sits after the static prefix. |
| Executable interfaces | `cu/interfaces.py` | `InterfaceStore` (`interfaces/<app>/v<N>/{api.py, api.json, fitness.jsonl}`, append-only), `InterfaceBuilder` (one bounded model call → AST allow-list → smoke run through `NativeRunner` on the isolated display → next version), `fitness` = replay pass-rate. |
| Procedures | `cu/procedures.py` | Append-only store `procedures/<app>/<goal-hash>/v<N>.json` with lineage `{parent, healed_step, reason}`. |
| Interface notes | `cu/interfaces.py` | `interfaces/<app>.md`, appended by a bounded summarizer, read into every plan prompt. |
| Graph | `cu/graph.py` | Second fixed topology, literal edges, compiled once. |
| Routes, tools | `server.py`, `main/tools/bridge-mcp.js` | `POST /computer/run {goal, app?, candidates?, display?} -> {job_id}` (result carries `infeasible`), `GET /computer/procedures`, `POST /computer/replay {procedure_id}`, `GET /computer/interfaces -> {apps:[{app, latest, functions:[{name, description, signature}], fitness}]}`, `GET /computer/tree?window=`, `POST /computer/find {target}`; MCP `computer_run`, `computer_replay`, `computer_find`, `computer_tree`. |
| OSWorld runner | `engine/eval/osworld/run_o.py`, `offline.py`, `summarize.py`, `README` | OSWorld's `predict(instruction, obs) -> (response, actions)` on the milestone loop; VM-only action strings. `offline.py` replays recorded trajectories through the grounder and judge with no VM. |

### The second fixed graph

```
context -> plan -> ground -> execute -> judge -> store -> report
                      ^                    | (judge fails)
                      +------- heal <------+     (heals < cuMaxHeals, default 3; else report ok=false)
```

Same job model and `JobState` as provisioning (phases `context | plan | ground | execute | judge | heal |
store | report`). The only cycle is heal: on a judged failure the planner is asked for a replacement for
that one step, the procedure's next version is written, and execution continues from it. `cuMaxHeals`
bounds the cycle. Edges are literal in `cu/graph.py`; no node changes them at runtime.

The frontier features (plan section I) all live inside these nodes:

- **Feasibility gate** (`context`): skipped when `plan` will replay; otherwise `FeasibilityGate.check`
  runs its read-only probes and one model question. Infeasible → `infeasible=True, ok=False, error=reason`,
  a `note` log entry, and every later node passes through to `report`; no planner, runner or judge call.
  `server.py` builds one gate per job over the slot-0 backend and the job's sandbox
  (`PerJobFeasibilityGate`); the job result and the `result` log entry carry `infeasible`.
- **Milestone loop** (`plan` + `execute`): `plan_milestones` once, then per turn `next_steps` (≤
  `cuStepsPerTurn`) → run → the milestone's `done_when` (a `Target` through the live tree, a question
  through the judge) → memory extract → next milestone, bounded by `cuMaxTurns`. Each later turn writes
  one `plan` phase entry. A replayed procedure never enters the loop. Turns that pass are combined into
  one procedure when they form a valid OScript; otherwise nothing is stored and the log says so.
- **Per-step candidates** (`plan`/`ground`): with `cuStepCandidates > 1`, `candidate_steps(k)` samples k
  one-step scripts and the first whose targets resolve in the live tree is kept (a dry a11y check, no
  model call), else candidate 0.
- **Judge order** (`judge`): executor failure → milestone verdict (already decided by `done_when`) →
  declared `assert` re-checked live on the tree → declared `state_assert` through `StateJudge.verify`
  with the baseline digests the runner took before that step → VLM goal question only when nothing above
  decided. No VLM call is logged when a tree or state check decides.
- **Loop breaker** (`execute` → `heal`): `ScriptRunner` keeps a ring of the last 8 `(screen_sha,
  step_digest)` pairs; a pair about to run for the third time is not executed and fails with
  `reason="loop"` plus the repeat evidence; `heal` passes that evidence to `replan_step(...,
  loop_evidence=)`; on the last heal the job reports `ok=False`.
- **Working memory** (`context`/`execute`): `WorkingMemory.from_goal` seeds constraint sentences; after
  each judged step `extract` (local model only; skipped when there is none) merges deltas, capped at 40
  items per list; `render` goes into every plan/replan prompt after the byte-identical `STATIC_PREFIX`.
- **Code-first actions** (`execute`): a `script` step runs through `runners.Runner` with the backend's
  own `DISPLAY` and the job sandbox; a `call` step runs a function of the app's newest `api.py` through
  the backend (find/invoke/set_value only). The planner is told to prefer `script` for file/format/bulk
  work and to follow it with a `state_assert`.
- **Executable interfaces** (`store`): after a fresh store (never on replay) `InterfaceBuilder.build`
  turns the passing procedure into `api.py` functions plus a smoke script, checks them against an AST
  allow-list, runs the smoke script through `NativeRunner` on the isolated display (the host display is
  refused), and appends `v<N+1>`. `api.json` lists names, descriptions and signatures (progressive
  disclosure for the planner); `fitness.jsonl` holds replay pass-rates; `GET /computer/interfaces`
  reads fitness through `InterfaceStore.read()` and never returns bodies.
- **Comparative best-of-N** (`plan`): with `cuComparativeRanking`, `BestOfN` builds one behaviour
  narrative per candidate (step text, resolved_by, verdicts, screen hashes) and asks the model once to
  rank them; ties go to the fewest steps. Per-candidate scoring is the fallback.

### Backends and the `allow_pixels` rule

Resolution and action are API-level first: `invoke`, `set_value` and `type_text` act on a node through
AT-SPI / UIA / AX and never move the mouse. `click` falls back to coordinates only when `allow_pixels` is
true, and `allow_pixels` is true only on the isolated display. On the host display a step that needs a
pixel action fails rather than acting.

### Grounding chain

accessibility backend `find(target)` → cached locator → vision grounding (screenshot + local VLM → bbox;
Claude Code fallback) → structured failure. A vision hit is cached per (app, target, screen hash), so the
next run of the same procedure on the same screen does not call the VLM.

Zoom-in grounding (`cuZoomGrounding`, default on): `ZoomGrounder` wraps the vision backend — ground on the
full screenshot, crop a half-size window around that answer (pillow), ground again on the crop, remap.
`confidence = 1 - dist(step-2 centre, crop centre) / (crop diagonal / 2)`, never above what the backend
itself reported. `Grounder(min_confidence=cuGroundConfidence)` is the consistency gate: an answer below it
is recorded in `last_errors`, never written to `LocatorCache`, and the fallback grounder runs.
`MultiCropVote(k)` (k shifted crops, mode of the rounded centres) is available for the offline eval.

### Judge

A step is "done" only when its judge passes. Tree assertions (`focused`, `checked`, `visible`, ...) run first
and need no model; the VLM judge answers `screenshot_assert` questions second. For best-of-N the judge
scores each candidate by goal-level `screenshot_assert`, step pass ratio, then fewest steps, and the winner
is stored.

### Procedures store lineage

`procedures/<app>/<goal-hash>/v<N>.json`. Replay runs `v<N>` with no model calls. A heal writes `v<N+1>`
with `parent = v<N>`, `healed_step` and `reason`; `v<N>` stays. Nothing is deleted, so every version a
goal ever ran under is readable.

### Interface notes cap

After each run a bounded summarizer (local model) appends at most 40 lines to `interfaces/<app>.md`; the
file is capped at 400 lines by keeping the newest sections. This is the Agent S4 "interfaces + usage
knowledge" idea kept append-only and inspectable as markdown.

### Best-of-N across displays

On Linux `BestOfN(n)` plans `n` candidates and runs each in its own isolated display (`:99 + i`);
`cuCandidates` defaults to 1 and 3 is recommended on Linux. On macOS and Windows there is no isolated
display, so pixel actions are never allowed there.

### OSWorld runner and offline eval

`engine/eval/osworld/` holds `run_o.py` (OSWorld's agent interface on the same milestone loop: one
`plan_milestones` per task, one `next_steps` per observation, `done_when` on the VM's tree or through the
judge, the feasibility gate over the observed tree, `FAIL` when infeasible or out of turns), `offline.py`
(recorded trajectories → grounding hit rate and judge agreement, no VM; `--zoom both` prints the two
settings side by side) and `summarize.py` (result dirs → table, `--offline` appends the offline table).
`script` steps run as Python inside the VM, `state_assert` file checks become one VM-side probe built
only from the shared allow-list, `tree` checks are decided on the observation, `call` is skipped with a
note. The README there documents the exact commands. Every number is whatever a run prints; nothing in
this repository states one.

### Threat model additions

- API-level actions run with the user's privileges inside the apps they target. `set_value` on a text
  field is a real edit; `invoke` on a button is a real press. The sandbox root does not apply to them;
  the action log entry with before/after tree digests and screenshot hashes is the record.
- Pixel actions are confined to the isolated display by `allow_pixels`; the host mouse, keyboard and
  primary display are never driven (§6 still holds).
- Procedures and interface notes are model output stored on disk and replayed later; a bad plan that
  passed its judge once will replay. Replay without inference is a feature, so the store is append-only
  and inspectable rather than trusted.
- The proxy token (§8) is a local secret: anyone on the machine who reads the settings file can run
  `claude -p` as the user through the proxy, with the user's login and the user's permission mode. It is
  never logged; it is not scrubbed from the log if a command prints it.

## 10. Phase 2 — tooling core

Five additions to the engine: an MCP tool router, multi-language script steps with pluggable runners,
disposable containment for high-risk requests, a three-role planning swarm, and headless web. Each has a
fallback that is already in the repository, so the graph never depends on an optional package. The
graph's edges, the schema's `extra=forbid` and the action log format are unchanged.

### Components

| Component | File | One line |
|---|---|---|
| MCP router | `mcp_router.py` | `MCPRouter(config: {name: {command, args, env} \| {url}})` over the `mcp` Python SDK (stdio and SSE). `list_tools() -> [{server, name, input_schema}]`; `call(ToolCall) -> ToolResult`. `ToolCall{server, tool, args}` and `ToolResult{ok, content, is_error, elapsed_ms}` are strict. Args are validated against the tool's JSON schema before anything is sent. Config file `<data_dir>/mcp-servers.json`, same shape as Claude Code's `mcpServers`. |
| Script schema | `schema.py` | `ScriptPayload{language: python \| bash \| javascript \| powershell, code (≤ 64k), target_directory, timeout_seconds}`; `ProvisionStep` takes exactly one of `command_string` / `script`; `ProvisionRequest.risk: 'normal' \| 'high'` (default `normal`). The fingerprint covers script payloads and `risk`. |
| Runners | `runners.py` | `Runner.run(payload, sandbox) -> StepResult`. `NativeRunner` writes the code to a temp file inside the sandbox and runs `python3` / `bash` / `node` / `pwsh` through `Sandbox.run`. `OpenInterpreterRunner` calls `interpreter.computer.run(language, code)` with `DISPLAY` from the isolated display; `available=False` when not importable. `get_runner` picks one. |
| Containment | `containment.py` | `DisposableSandbox` ABC: `run(step) -> StepResult`, `extract(relpath) -> bytes`, `destroy()`. `DockerSandbox(image, job_dir, network)`: one `docker run --rm --network <network> -v <job dir>:/work -w /work` per step; `network` is the `dockerNetwork` setting, else `O_DOCKER_NETWORK`, default `bridge` (`none` for an offline container). `E2BSandbox(api_key, template)`: one instance from the `e2b` SDK, created on first `run`. `select_sandbox(preference, job_dir)`: first available of `[preference, docker]`, else `None` (the path sandbox), every fall-back logged. `run_high_risk` runs all steps and destroys the instance in `finally`. |
| Swarm | `swarm.py` | Roles `telemetry_auditor`, `subprocess_compiler`, `environment_inspector` as plain callables over a shared `state` dict. `SimpleCrew`: one worker thread per role, state handed through a queue, sequential. `CrewAICrew`: the same callables as crewAI tools, `Crew(process=sequential)`, LLM = local vLLM. `get_crew` returns crewAI when importable and `swarmEnabled`, else `SimpleCrew`. Output `SwarmResult{profile, request: ProvisionRequest \| None, inspection, roles}`. |
| Headless web | `web.py` | `BrowserUseRunner(vllm_url, model).run(task, url?, display) -> WebResult{ok, final_text, steps, screenshots_sha}` with `browser_use.Agent` on headless Chromium under `DISPLAY=:99`; `available=False` when not importable. `PlaywrightDomRunner` takes scripted `{goto, fill, click, read, wait}` actions, no model. |
| Routes, tools, settings | `server.py`, `main/tools/bridge-mcp.js`, `main/settings.js`, `main/agent.js` | `GET /tools/list`, `POST /tools/call`, `POST /script/run`, `POST /swarm/run {objective, cwd}`, `POST /web/run {task?, url?, actions?, timeout_seconds?}` (returns `{job_id}`; the job's `result` carries the `WebResult`); bridge tools `mcp_tools`, `mcp_call`, `script_run`, `swarm_run`, `web_run` (strict schemas). Settings `mcpServers` (`{}`), `highRiskSandbox` (`docker`), `e2bApiKey` (empty), `dockerImage` (`python:3.12-slim`), `dockerNetwork` (`bridge`; `none` selectable), `swarmEnabled` (true). The app writes `mcp-servers.json` from `mcpServers` and merges the same servers into `o-mcp.json` for Claude Code. |

### Execute-node dispatch

The `execute` node keeps its position in the graph and gains two branches inside the node, not new edges:

```
execute(request)
   |
   +-- request.risk == "high" --> containment.run_high_risk(request, job_dir, highRiskSandbox)
   |                                   select_sandbox: e2b (key + SDK) -> docker (daemon) -> None
   |                                   None  -> path sandbox, with a logged note
   |                                   steps run in the instance; exit 0 all round -> cache_store as usual
   |                                   finally: destroy() (logged)
   |
   +-- step.command_string        --> Sandbox.run(step)             (unchanged)
   |
   +-- step.script                --> Runner.run(payload, sandbox)  (OpenInterpreterRunner if importable, else NativeRunner)
```

A failed step stops the run at that index like before; verify, remediate and cache_store see the same
`StepResult` records whichever path produced them.

### MCP router dataflow

```
Settings -> mcpServers (JSON text)
   |  app writes <data_dir>/mcp-servers.json          app merges the same servers into o-mcp.json
   |                                                   (Claude Code --mcp-config sees them as its own)
   v
MCPRouter(config)  --stdio / SSE-->  each configured server
   |
   | list_tools(): [{server, name, input_schema}]              <- mcp_tools / GET /tools/list
   |
   | call(ToolCall{server, tool, args}):
   |     1. look up the tool's input_schema
   |     2. validate args against it; mismatch -> ToolResult{ok=false, is_error=true}, nothing sent
   |     3. send; log kind 'tool'
   |     4. ToolResult{ok, content, is_error, elapsed_ms}; log kind 'result'
   v
mcp_call / POST /tools/call
```

Both brains see one server list: Claude Code calls a server directly through its own MCP client, the
engine calls it through the router. Either way the call is a `tool` entry and its outcome a `result`
entry.

### The swarm's sequential role passing

```
objective, cwd
   |
   v
telemetry_auditor      state.profile     = context.profile(cwd) (shell history, tree, config hashes, screenpipe)
   |
   v
subprocess_compiler    state.request     = parser(objective + profile summary) -> strict ProvisionRequest
   |
   v
environment_inspector  state.inspection  = verify ports headlessly; judge the isolated display when a judge is given
   |
   v
SwarmResult{profile, request, inspection, roles: [RoleRun{role, elapsed_ms, ok, note}]}
```

The roles are the same callables under both orchestrators. `SimpleCrew` starts one thread per role in
turn and waits on a queue for the role to hand the state back (per-role timeout; a timed-out role is a
`RoleRun{ok=false}` and the next role still runs on the state as it is). `CrewAICrew` wraps the callables
as crewAI tools in a `Crew(process=sequential)` with the local vLLM as LLM. Each role is logged. With
`swarm: true` on `/provision` the context node uses the swarm's `request` in place of the parser's;
`POST /swarm/run` returns the `SwarmResult` as a job.

### Threat model additions

- Docker containment mounts nothing from the host except the job directory at `/work`. Code inside
  can read and write that directory and nothing else of the user's files. The container is on the
  `dockerNetwork` Docker network, `bridge` by default (so installs inside it work); `none` cuts its
  network. The container is removed on exit (`--rm`) and killed in `destroy()` if still running.
- **E2B is a cloud service.** When `highRiskSandbox` is `e2b` and an instance is created, the step's
  code, its working files and its output leave the machine. This is the one place in O where that
  happens for execution, and it happens only when the user has set `e2bApiKey` and chosen `e2b`.
  E2B runs in an empty `/work`: the job directory is never uploaded, so high-risk steps that depend
  on project files behave differently in E2B than in Docker.
- `e2bApiKey` is a local secret like the proxy token (§8): stored in the settings file, read by the
  engine, never written to the action log. It is not scrubbed from the log if a command prints it.
- MCP servers run with the user's privileges, as child processes of the engine (stdio) or as whatever
  is listening at the URL (SSE). Schema validation checks the shape of a call, not what the server does
  with it; a server that deletes files will delete files. The settings-side list is the user's own and
  is merged into Claude Code's config, so a server added there is reachable from both brains.
- Script steps are code the model wrote, run with the user's privileges inside the path sandbox like
  any `command_string`; the sandbox root bounds `target_directory`, not what the code does by absolute
  path (§5 still holds). `risk: "high"` is set by the request, so a request that omits it runs on the
  host; the ruled-out case is only running high-risk code on the host when a disposable backend is
  configured and available.
- Headless web runs its own Chromium under the isolated display and never attaches to the user's tabs,
  so it carries none of the user's sessions; by the same token O Link's in-page payment guard is not
  injected there. No CAPTCHA circumvention and no proxy rotation are implemented.

## 11. Generative UI

Phase 3 lets the engine and the agent describe what the panel should show, without either of them
producing anything the panel draws as-is. The plan's stated decision: UI reaches the panel only as
validated declarative frames; raw HTML or markdown from the model is never rendered there. The renderer
owns the mapping from frames to O's own design tokens and native DOM. Custom previews (HTML/CSS/JS the
engine itself generated, such as a status card after `verify`) render only inside a sandboxed iframe. The
same frames have a token-compressed text form, "oui", validated on both sides.

### Components

| Component | File | One line |
|---|---|---|
| Frame models | `engine/o_engine/genui.py` | Strict, frozen pydantic (`extra="forbid"`): frames `Begin`, `Update`, `Data`, `End`, `Sandbox`; closed component set `Column`, `Row`, `Card`, `Text`, `Step`, `Tree`, `Progress`, `KeyValue`, `Button`, `Preview`; `ButtonAction` literal `open-log \| open-file \| retry \| approve \| deny`; `parse_frame(raw)` raises `SchemaViolation` on anything else. Surfaces are `job:<id>` or `chat:<id>`. |
| Store | `genui.py` `UIStore` | Per-surface ring buffer (`STORE_MAXLEN` 2000) with a per-surface sequence number, queue subscribers for SSE, and a fire-and-forget forward of each frame to the app (`POST O_UI_URL {frames: [frame]}`, `x-o-token`, from a background thread). The store is the record; a failed forward is dropped. |
| Job emitters | `genui.py` `JobSurface`, `job_surface`, `ui_phase`, `ui_step`, `ui_preview_status`, `ui_end` | One `JobSurface` per job, kept on the graph's tracker; `begin` on first use, a `Tree` of phases plus a "current" `Step` on each phase change, a `Step` per executed step with `data` patches for status/detail/progress, an optional `Preview` whose HTML is `status_card_html` (engine-generated, escaped), `end` at report. Every helper is a no-op when the store is absent (`O_GENUI=0`). |
| oui compiler | `genui.py` `oui` | `compile(frames) -> str`, `decode(text) -> list[Frame]`; short type and key names, bare tokens where the value matches `[A-Za-z0-9_.:/@-]+`, JSON otherwise; `decode` goes through `parse_frame`. |
| Routes | `server.py` | `GET /jobs/{id}/ui` streams the job's surface as SSE (`?format=a2ui` JSON frames, `?format=oui` compiled text; `?since=` sequence number); `POST /ui/emit {frames: [...]}` accepts A2UI JSON frames, validates each one and appends it to the store, answering `{accepted}`. |
| App `/ui` endpoint | `main/agent.js` | The localhost server the bridge and the engine already use gains `POST /ui`; `genuiFrameProblem` checks only the envelope (`a2ui` is `"1"`, known `type`, surface prefix `chat:` / `job:`, size cap) and the renderer does the full check. |
| Relay, store, IPC | `main/main.js`, `main/preload.js` | The engine pushes every frame to the app's `POST /ui` (`O_UI_URL`, `x-o-token`); main.js stores it (ring 2000 per surface, 200 surfaces) and broadcasts `{type:'genui', frame}`. `GET /jobs/{id}/ui` is the SSE backfill. Preload exposes `onGenui(cb)`, `genuiFrames(surface)` (replay of a surface for a reopened panel), `genuiSurfaces()`, `genuiAction(action, payload)` and `logNote`. |
| Bridge tool | `main/tools/bridge-mcp.js` | `mcp__o__ui_emit {frames: [...]}` (strict schema; JSON frame objects, at most 64 per call) posts to the app's `/ui`, filling in surface `chat:<session>` on frames that name none. Listed as a safe tool in every permission mode: it changes the panel, nothing else. |
| Renderer | `renderer/genui.js`, `renderer/genui.css` | `validateFrame` (hand-written, same closed sets as the engine), `decodeOui` / `encodeOui`, `Surface` (`apply(frame)` → DOM on `panel.css` tokens; Step/Tree status pills reuse the action-log pill styles), the sandboxed Preview iframe (`renderer/preview.html`) and its postMessage bridge, Button actions dispatched to the panel. `window.OGenUI` is the only export. |
| Panel surfaces | `renderer/panel.js`, `renderer/panel.html` | A `Surface` card per surface id inline in the chat; a "Runs" sheet listing active and past `job:` surfaces. The panel CSP stays `script-src 'self'`; the iframe loads `renderer/preview.html`, which carries its own CSP. |
| Settings, env | `main/settings.js`, `main/engine.js` | `genuiEnabled` (true) → `O_GENUI`; `genuiSandbox` (true) decides whether a `Preview` loads its iframe or shows a placeholder. |

### Dataflow

```
graph node (provision / cu / swarm / web)
   |
   | ui_phase / ui_step / ui_preview_status / ui_end   (tracker["ui"])
   v
JobSurface  --Begin/Update/Data/Sandbox/End-->  UIStore (ring buffer per surface, seq++)
   |                                                |
   | POST O_UI_URL {frames:[frame]}  (x-o-token)    | GET /jobs/{id}/ui  (SSE backfill, ?format=a2ui|oui, ?since=)
   v                                                v
main/agent.js  POST /ui  (envelope check) ----> main/main.js store + broadcast {type:'genui', frame}
   ^                                                |
   | POST /ui {frames}                              v
main/tools/bridge-mcp.js  ui_emit        renderer: Surface(surface).apply(frame)
   ^                                                |
   | mcp__o__ui_emit {frames}             +---------+----------+
   |                                      |                    |
Claude Code child                   native DOM            <iframe sandbox="allow-scripts" src="preview.html">
                                    (panel.css tokens)    own CSP; in: one o-ui-load {html,css,js}; out: o-ui-event only
                                                                     |
                                              every render and every event -> action log (kind 'note')
```

Two sources, one path: the engine's frames and the agent's frames both end at `Surface.apply`, and both
are checked twice, by the envelope check in `main/agent.js` and by the full validator in
`renderer/genui.js`. The engine additionally validates its own frames at construction (pydantic), so a
malformed engine emitter fails in the engine, not in the panel.

### The oui grammar

One frame per `@` line; after `@update`, one component per line until the next `@` line. Blank lines
are ignored.

```
@begin    <surface> <title>
@update   <surface>
<Type>#<id>[<k>=<v>;<k>=<v>…]{<child id> <child id>…}
@data     <surface> <path> <json value>
@sandbox  <surface> <sandbox_id> <json {"html","css","js"}>
@end      <surface> true|false
```

- `<Type>` is a short name from the closed set (`Col`, `Row`, `Card`, `Txt`, `Step`, `Tree`, `Prog`,
  `KV`, `Btn`, `Prev`); `<id>`, `<surface>` and `<path>` match `[A-Za-z0-9_.:/@-]+`.
- `[…]` holds properties under short keys; children never go in `[…]`, always in `{…}` as
  space-separated ids. A key at its default value is omitted on compile. `Step` status is written
  short (`pend`, `run`, `ok`, `fail`, `skip`).
- **Quoting rule.** A value is written bare only when it is a string matching the token pattern above.
  Any other value, including a string with a space, `;`, `]` or quote, a number, a boolean, a list or an
  object, is JSON-encoded in place (`l="npm install"`, `p=0.4`, `n=[{"id":"a","label":"A"}]`). The
  decoder reads a value starting with `"`, `[` or `{` as JSON and anything else as a bare token.
- Unknown short type, unknown key for that type, trailing text, an unterminated `[` or `{`, or an
  `@update` with no component lines is a `SchemaViolation` (engine) or a rejected frame (renderer).
- **Round-trip guarantee.** `decode(compile(frames)) == frames` exactly, on the engine's models; the
  renderer's round trip is exact on normalized frames: `decodeOui` yields the same JSON frames as the
  engine's `frame_to_wire`, so a surface
  rendered from oui and the same surface rendered from JSON are identical. The plan requires this as a
  test on the engine side.

Example, the same surface in both forms:

```
@begin job:7 "Install deps"
@update job:7
Col#plan{s0 s1}
Step#s0[l="npm install";st=run;p=0.4]
Step#s1[l="npm test"]
@data job:7 steps.0.status "ok"
@end job:7 true
```

```json
{"a2ui":"1","type":"begin","surface":"job:7","title":"Install deps"}
{"a2ui":"1","type":"update","surface":"job:7","components":[
  {"id":"plan","component":{"Column":{"children":["s0","s1"]}}},
  {"id":"s0","component":{"Step":{"label":"npm install","status":"running","progress":0.4}}},
  {"id":"s1","component":{"Step":{"label":"npm test"}}}]}
{"a2ui":"1","type":"data","surface":"job:7","path":"steps.0.status","value":"ok"}
{"a2ui":"1","type":"end","surface":"job:7","ok":true}
```

### Threat model

- **The panel never executes model output.** Frames are data; the renderer builds DOM from a fixed
  switch over the closed component set and sets frame text with `textContent`; no frame value is ever
  written as HTML. A frame
  carrying an unknown component, an unknown property or a Button action outside the closed enum is
  dropped as a whole and logged as a `note`; nothing partial is drawn.
- **Two validations, in different processes.** `main/agent.js` checks the envelope of every frame
  that reaches `POST /ui` (shape, version, surface prefix, size) so the main process never relays
  arbitrary payloads; `renderer/genui.js` checks every property before drawing. Engine frames are
  additionally pydantic-validated when they are built. A frame that passes one check and fails the
  other is logged where it fails.
- **Sandbox frames carry engine HTML, not model HTML.** The only producer of `Sandbox` frames in the
  repository is `JobSurface.preview`, fed by `status_card_html`, whose text is HTML-escaped. A
  `sandbox` frame arriving from the agent through `ui_emit` is still accepted by the envelope check
  and the validator, which is why the iframe model below assumes hostile content.
- **The Preview iframe.** `sandbox="allow-scripts"` without `allow-same-origin`: the frame runs in an
  opaque origin, cannot read the panel's DOM, storage or `window.o`, and cannot reach the preload
  bridge. It is not a srcdoc: the iframe loads `renderer/preview.html`, whose own `<meta>` CSP is
  `default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-<bootstrap>' 'strict-dynamic'; img-src data:`,
  so the only script allowed to start is the page's inline bootstrap, pinned by its hash, and
  `'strict-dynamic'` lets that bootstrap create one more script. Inline `<script>` blocks and inline
  event handlers (`onclick=` and the like) inside the posted `html` are blocked by that CSP. Editing
  the bootstrap means recomputing its sha256 and updating the `<meta>`. The document arrives by
  postMessage: the panel posts `{type:'o-ui-load', html, css, js}` into the frame on load; the
  bootstrap accepts that message once, only from `window.parent`, injects `css` as a `<style>`, sets
  `html` as the body and runs `js` as a `blob:` script it creates. Nothing else loads: no network, no fonts, no frames, no
  navigation targets, `data:` images only. `referrerpolicy` is `no-referrer`. Height is fixed by the
  `Preview` component, so the frame cannot grow to cover the panel.
- **The postMessage bridge.** The panel's `message` listener accepts an event only when its `data` is
  an object with `type === 'o-ui-event'` and its `source` is the `contentWindow` of an iframe this
  `Surface` created; anything else is ignored without logging the payload. `name` is clipped to a short
  string and `payload` must be a plain object. The accepted event becomes a `note` entry and a DOM
  `CustomEvent` on the surface root; nothing from the iframe reaches the main process except through
  that entry and whatever the panel decides to do with the event. No API is exposed into the iframe.
- **Every sandbox render and every event is logged.** Creating an iframe for a `sandbox_id` and each
  accepted `o-ui-event` write `note` entries with the surface and sandbox id, so the Action log shows
  what was shown and what it sent back. A UI frame that skips the action log is ruled out by the plan.
- **Button actions are panel verbs, not capabilities.** The enum is `open-log`, `open-file`, `retry`,
  `approve`, `deny`, and the panel handles each one through the IPC it already uses for that action
  (the log sheet, the app, the task queue, the `ask`-mode card), recorded the same way; `open-file`
  always opens the action-log text file (`shell.openPath` on the log's own path), never a path from
  the frame; a Button cannot name a command, a URL or a script.
- **`genuiSandbox` off** means a `Preview` is a placeholder and the sandbox HTML is never posted into
  a preview frame; `genuiEnabled` off means the engine's `UIStore` is not created (`O_GENUI=0`), `ui_emit`
  frames are still accepted at `/ui` but not rendered, and the Runs sheet is empty.
- **What this does not protect against.** The iframe can still compute, loop and allocate; a heavy
  preview costs the panel's CPU until the surface is closed. `img-src data:` permits large inline
  images, bounded only by the frame size cap in `main/agent.js`. Frames are stored in the engine's ring
  buffer and relayed over the loopback engine API, which `x-o-token` protects as in §5; anyone who can
  POST to `/ui/emit` with the token can put frames on a surface.

Ruled out (plan): rendering model-produced HTML/markdown outside the sandbox; `allow-same-origin`; any
component or action outside the closed sets; UI frames or sandbox events that skip the action log.
