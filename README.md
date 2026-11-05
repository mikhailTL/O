# O AI

A small desktop app with a chat panel that does things on your computer. O works **inside your own Edge or Chrome tabs** through **O Link**, its own small extension: it reads and clicks in the page directly and never touches your mouse, keyboard or screen. Its brain is the **Claude Code CLI** (`claude -p`) with your own Claude login; no API key. Underneath sits a Python **engine** that sets up dev environments, runs commands in a **sandbox**, replays saved scripts from a **script cache** and repairs failures by bounded **remediation**. Everything either side does is written to one append-only **action log**.

**Credit:** the design and the desktop shell are based on [opia](https://github.com/samuelsortland-cmyk/opia) (MIT) by samuelsortland-cmyk.

## The action log

Security and safety in O come from recording, not from asking. Every entry has a `kind`:

| kind | What it records |
|---|---|
| `task` | a task you gave O started or finished |
| `tool` / `result` | every tool call Claude Code makes, and its result status (`is_error`) |
| `policy` | each permission decision (allowed, denied, timed out) and the mode it was made under |
| `graph` | engine graph transitions (`context → validate → …`) |
| `cache` | script cache lookups, hits and stores |
| `remediate` | each remediation attempt and the patch it applied |
| `verify` | headless port/HTTP checks after a provision |
| `settings` | any settings change |
| `note` | one-line explanations the agent writes (`log_note`) |

- **Files:** app side `<userData>/o-actions.jsonl` plus a text twin `o-actions.log`; engine side `<dataDir>/engine-actions.jsonl` plus `.log`. The engine also posts each entry to the app, which re-chains it into the app file.
- **Hash chain:** each entry carries `prev` (sha256 of the previous entry) and `hash` (sha256 of itself). Append-only. **Verify chain** in the log sheet recomputes every link and reports ✓ intact or ✗ broken at #n.
- **Reading it:** click the list icon in the panel header (it carries an unread badge) or use **tray → Open action log**. The pet says how many actions happened since you last looked; the sheet shows the same count as its subtitle, with filter chips (All · Tools · Engine · Policy · Errors) and **Open log file**.
- **Away notification:** "O did N things while you were away", only when the panel is closed and something was logged since you last read it (debounced 30 s). Turn off with `logUnreadBadge`.

### Permission modes (Settings → Permissions)

| Mode | Behaviour |
|---|---|
| `log` (default) | Nothing asks. Every tool call and engine step is allowed and recorded. |
| `ask` | Shell commands, file changes and engine gates show a blocking card (Allow / Deny) that never takes focus. No answer within **60 s** denies it **and destroys the task queue**. |
| `auto` | No gating, no cards. Still logged. |

Payment blocking in O Link applies in every mode: while O works in a tab, card and bank fields, payment pages and pay / buy / checkout buttons are locked and the page shows "Blocked by O". The guard is unchanged from opia.

## Requirements

Checked at startup and shown green/red in **Settings → Engine → Check requirements**, each with a one-line fix.

| Requirement | Why |
|---|---|
| Node.js 18+ | runs the Electron app |
| Claude Code CLI, logged in | the brain (`claude -p`) |
| Python 3.10+ | runs the engine (`python -m o_engine`) |
| vLLM at `http://127.0.0.1:8000/v1` | local parser model that turns text into provision steps; Claude Code is the fallback |
| screenpipe (SQLite at its default path) | recent OCR text and window titles for workspace context; context still builds without it |
| Playwright + Chromium | headless browser for `verify_port` and O's own invisible browser |
| Xvfb (Linux only) | isolated display `:99` for engine steps that need a screen |

## Install

1. Get the code: `git clone https://github.com/slithr-in4/O` (or download and unzip).
2. Run the installer for your OS. Both are idempotent and end with `o-engine check`.

| OS | Command | What it does |
|---|---|---|
| Linux / macOS | `bash install/install.sh` | Node deps, Python venv + `pip install -e engine`, `playwright install chromium`, vLLM (`pip install vllm` on Linux with an NVIDIA GPU; on macOS or a Linux box without one the step is marked failed, the alternative is printed and the script continues; the final exit code is the number of failed steps), screenpipe (official installer), Xvfb via `apt`/`dnf` on Linux, Phase 2 packages (`mcp open-interpreter e2b crewai browser-use`, each reported on its own, none fatal), Docker presence check (reports only; never installs Docker) |
| Windows | `powershell -ExecutionPolicy Bypass -File install\install.ps1` | same, minus Xvfb; the vLLM step is always marked failed with the alternative printed, and the script continues; the final exit code is the number of failed steps |

3. Start O: `npm start` (Linux/macOS) or double-click **`Start O.bat`** (Windows; installs Node deps on first run). O appears bottom-right with a tray icon.
4. First run: open Settings → Engine → **Check requirements** and fix anything red. Connect your browser with **Connect my Edge** (one-time, Developer mode → Load unpacked → the `extension` folder).

## Using O

| Do this | What happens |
|---|---|
| Hover O and click **✦** (or **Ctrl/Cmd+Shift+O**, or double-click O) | Opens the panel |
| **Right-click O** | Menu: Open O, Stop current task, Snooze wandering, Hide O, Turn off O |
| Type a task, press **Enter** | O thinks, browses, works; progress in the panel and speech bubble; every step goes to the action log |
| Close the panel (✕ / Esc) | O keeps working in the background |
| Send another task while busy | Queued, runs next |
| **■ Stop** (or **Ctrl+.**, or tray → Stop current task) | Stops immediately: Claude Code, browser tools and engine jobs are shut down, queue cleared |
| List icon in the header | Opens the action log sheet; the badge is the unread count |
| **Chats** (▯) | Chat history; each chat is its own Claude Code session. Rename ✎, delete 🗑, **Delete all chats** at the bottom |
| ✎ New chat | Fresh chat; the old one stays in Chats |
| Ask about your screen | O screenshots the display under your cursor and looks at it (`screenAccess`) |
| Drag / fling O | Moves, falls, bounces |

When a provision request matches a saved script, the status line reads "Déjà vu — replaying a saved script, no thinking needed".

## How the engine works

The engine is a FastAPI server on `127.0.0.1` (port chosen by the app, header `x-o-token` required), spawned by the app as `python -m o_engine serve`. Claude Code reaches it through MCP tools `mcp__o__provision`, `cache_lookup`, `diagnose`, `verify_port`, `workspace_context`, `log_note`, for computer use `computer_run`, `computer_replay`, `computer_find`, `computer_tree`, for Phase 2 `mcp_tools`, `mcp_call`, `script_run`, `swarm_run`, `web_run`, and for generative UI `ui_emit`. A provision request is a strict schema: `{label, steps: [{target_directory, command_string | script, timeout_seconds}], verify?, isolated_display, risk?}`; prose, fences and extra keys are rejected.

```
context -> validate -> cache_lookup -> gate -> execute -> verify -> cache_store -> report
                                        ^                  |  (fail)
                                        |                  v
                                        +--- remediate <---+     (attempt < max_attempts, default 3; else report)
```

| Node | One line |
|---|---|
| `context` | builds a workspace profile: shell history tail, project tree (3 levels), config file hashes, last 120 s of screenpipe OCR/titles. Asks the user nothing. |
| `validate` | parses the request through the pydantic schema; a `TokenFilter` keeps only the first balanced JSON from the model stream |
| `cache_lookup` | fingerprints the request per platform and looks in the script cache |
| `gate` | applies the permission mode: `log` records and passes, `auto` passes, `ask` waits ≤ 60 s for approval |
| `execute` | runs each step in the sandbox: `subprocess` with timeout, no stdin, scrubbed env (no `CLAUDE*`, no tokens). A `script` step goes to the Runner; a `risk: "high"` request goes to the disposable sandbox |
| `verify` | optional headless Playwright check of `{port, path, expect_status}` |
| `remediate` | reads stderr, asks the parser for replacement steps, applies surgical `{path, find, replace}` patches inside the sandbox, re-queues execute |
| `cache_store` | writes `<fingerprint>.sh|.ps1` + `.json`; never overwrites, never deletes |
| `report` | final `JobState` back to Claude Code and the log |

- **Script cache:** an identical request (same fingerprint) replays the saved script. No model call. A replay that fails is treated as a failure and goes to remediation; the entry is never deleted.
- **Remediation:** the only cycle in the graph, bounded by `maxRemediationAttempts` (default 3). Exceeding it reports `ok=false`.
- **Sandbox:** every `target_directory` must resolve inside `sandboxRoot` (symlinks resolved); anything else is a `SandboxEscape`. Parallel jobs get their own subdirectory.
- **Isolated display:** on Linux the engine starts `Xvfb :99` and runs isolated steps with `DISPLAY=:99`. On macOS and Windows there is no isolated display; browser work is headless-only, and the requirements check says so.
- Edges are fixed in `graph.py`; no node can add or change them at runtime.

## API proxy

A localhost, OpenAI-compatible endpoint over your own Claude Code login. Any client that speaks the OpenAI chat API can use O as its model: each request becomes one `claude -p` run, the stream is normalized back into standard JSON / SSE, and the request is recorded in the action log. URL and token are in **Settings → API proxy**; the token is generated once and shown masked with a copy button.

| Route | What it does |
|---|---|
| `GET /health` | liveness |
| `GET /v1/models` | `o-ai` (O persona, prose allowed) and `o-ai-code` (code / JSON only, prose filtered out) |
| `POST /v1/chat/completions` | OpenAI shape (`model`, `messages`, `stream`, `tools?`, `response_format?`); streams `chat.completion.chunk` SSE frames ending in `data: [DONE]`, or returns one JSON completion; non-stream replies carry `x-o-cost-usd` and `x-o-duration-ms` headers |
| `POST /v1/run` | O-native `{objective, cwd?}`: engine context → script cache lookup → replay on a hit, else one code-mode `claude -p` call for strict steps → engine `/provision`; returns the `JobState` |

- Listens on `127.0.0.1:<proxyPort>` only (default 8787). Every request needs `Authorization: Bearer <proxyToken>`; anything else is 401.
- With `response_format.type = "json_object"` or model `o-ai-code`, the reply passes through a TokenFilter: only the first balanced JSON value survives; fences and prose are dropped.
- Up to `proxyConcurrency` child processes at once (default 4); further requests queue. Each run is its own sessionless `claude` process, no `--resume`.
- Each request writes a `task` entry (`API: <route> <model> <clip of last user message>`) and a `result` entry on completion. The bearer token is never logged.

```sh
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer $O_PROXY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"o-ai-code","messages":[{"role":"user","content":"Write a bash one-liner that counts lines in *.py"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="<token from Settings → API proxy>")
r = client.chat.completions.create(
    model="o-ai-code",
    messages=[{"role": "user", "content": "Return {\"ok\": true} as JSON"}],
    response_format={"type": "json_object"},
)
print(r.choices[0].message.content)
```

Two decisions, stated plainly:

- **No permission-skipping flag.** The source spec wanted `CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS`. O does not use it: `log` mode already un-gates every tool call, but through the bridge, so each call is still a `policy` entry in the action log. Skipping permissions at the CLI would remove those entries.
- **Persona stripped, safety kept.** API callers get a code-only system prompt; O's persona, prose styling and reply-length rules are stripped (`main/promptmod.js`) so a caller's own `system` message merges without duplicated narrative. The payment, credential and untrusted-content rules are never stripped.

Ruled out: non-loopback interfaces, running without a bearer token, `--dangerously-skip-permissions`.

## Computer use

O can drive desktop apps, not only browser tabs. A goal ("save this sheet as CSV") becomes an **OScript**, a strict JSON plan that O runs against the app's accessibility tree, judges step by step, and saves as a **procedure** it can replay later with no model calls.

OScript in five lines:

1. Each step is one `op`: `launch`, `wait`, `find`, `click`, `type`, `key`, `set_value`, `invoke`, `assert`, `screenshot_assert`, `state_assert`, `script`, `call`.
2. Elements are named by `target` (`role`, `name` glob, `text`, `index`, `within`), never by pixel.
3. `find` binds a `ref`; later steps act on the ref.
4. `assert` checks a node state in the tree (`focused`, `checked`, `visible`, ...); `state_assert` checks the real system (`file_exists`, `file_contains`, `file_sha_changed`, `tree`, `shell_true` over a read-only allow-list); `screenshot_assert` asks a vision judge a yes/no question. `script` runs Python/bash in the job sandbox on the isolated display; `call` runs a function of the app's built interface.
5. The schema is frozen pydantic with `extra=forbid`; prose, fences and unknown keys are rejected.

A goal is not scripted blind: the planner writes a few milestones once, then plans at most `cuStepsPerTurn` steps per look at the tree, checks each milestone's `done_when`, and stops after `cuMaxTurns` turns. Before any of that a feasibility gate runs read-only probes and asks one model question; an impossible goal is reported `infeasible` without a single click.

| Design point | What O does |
|---|---|
| Grounding | accessibility backend first (AT-SPI on Linux, UIA on Windows, AX on macOS) → cached locator (`LocatorCache`, `locators.sqlite`) → vision grounding (`VisionGrounder` with a local VLM, `ClaudeCodeGrounder` as fallback) → structured failure |
| Host disruption | API-level actions (invoke, set-value, type into a node) never move your mouse. Pixel clicks are allowed only when `allow_pixels` is true, which is only on the isolated display. |
| Judged steps | a step is done only when its judge passes: tree assertion first, `state_assert` probes second (no model), VLM judge last and only as a tie-break |
| Loop breaker | the runner keeps the last 8 (screen hash, step) pairs; a third repeat stops the step with reason `loop` and the heal loop replans with that evidence |
| Working memory | facts / state / todos / risks per task, seeded from the goal's constraints, updated after every judged step, placed after the static prompt prefix so the vLLM prefix cache holds |
| Procedures | saved per app and goal, append-only with lineage; replay makes zero model calls; on a judged failure the heal loop replaces that one step and writes the next version (max `cuMaxHeals`, default 3); nothing is deleted |
| Best-of-N | on Linux, `cuCandidates` plans run in parallel on separate Xvfb displays (`:99 + i`); with `cuComparativeRanking` one model call ranks all candidates by their behaviour narratives, else the judge scores each (default 1; 3 recommended). `cuStepCandidates` samples k plans for the next step and keeps the first whose targets resolve in the live tree |
| Grounding gate | `cuZoomGrounding` grounds on the full screenshot, then again on a crop around the answer; the two answers' agreement is the confidence, and anything below `cuGroundConfidence` is discarded (never cached) and the fallback grounder runs |
| Interface notes | per-app markdown (`interfaces/<app>.md`): what worked, what the tree looks like, pitfalls; appended by a bounded summarizer after each run, capped, read into every plan prompt |
| Executable interfaces | with `cuInterfaceBuilder`, a stored procedure becomes `interfaces/<app>/v<N>/api.py` + `api.json` (find/invoke/set_value only, AST-checked, smoke-tested on the isolated display); the planner sees the function names and may emit `call`; fitness = replay pass-rate, shown by `GET /computer/interfaces` |
| Evidence | every step is an action log entry with before/after tree digests and screenshot hashes |

MCP tools: `mcp__o__computer_run`, `computer_replay`, `computer_find`, `computer_tree`. Engine routes: `POST /computer/run`, `POST /computer/replay`, `GET /computer/procedures`, `GET /computer/interfaces`, `GET /computer/tree`, `POST /computer/find`. A job's result carries `infeasible: true` when the gate stopped it. `/health` reports `cu_backend`; a backend whose platform package fails to import is `available=False`.

### How it compares to Simular

Design points from Simular's own pages ([Agent S3 / Behavior Best-of-N](https://simular.ai/articles/simulars-computer-use-agent-outperforms-humans), [what an autonomous computer is](https://www.simular.ai/articles/what-is-an-autonomous-computer)), matched against O:

| Design point | Simular (per their pages) | O |
|---|---|---|
| Element targeting | accessibility tree first, vision fallback | same order, plus a cached locator between them |
| Replay | solved tasks compiled to deterministic scripts, no tokens on replay | procedures replay with zero model calls |
| Judging | judge models check the screen before a step is marked done | tree assertion first, VLM judge second |
| Self-healing | self-healing scripts | one bounded heal cycle per step, append-only lineage |
| Best-of-N | run N agents, pick the best | N candidates on N isolated displays, judge-scored |
| Per-app knowledge | Agent S4 evolves interfaces and usage knowledge from trajectories | interface notes, append-only and inspectable markdown |
| Host disruption | — | API-level actions never touch your mouse; pixel actions only inside the isolated display |
| Benchmark | Agent S3 reports an OSWorld score on its page | O claims no number until you run the OSWorld runner in `engine/eval/osworld/` (`run_o.py`, `summarize.py`); the score is whatever the run prints |

Ruled out: claiming a benchmark result not produced by a run; pixel actions on the host display; deleting procedures; any backend that takes over your mouse outside the isolated display.

## Phase 2: tooling core

Five additions to the engine, each optional at runtime and each falling back to something already in the repo. Nothing in them changes the graph's edges, the schema's `extra=forbid`, or the action log.

| Addition | What it does | When its package is missing |
|---|---|---|
| MCP tool routing | Claude Code is not the only thing that can call MCP servers. The engine's `MCPRouter` reads the `mcpServers` setting (JSON text, same shape as Claude Code's `mcpServers`: `{name: {command, args, env} \| {url}}`), lists their tools (`mcp_tools`) and calls them (`mcp_call`) over stdio or SSE. **Every call is validated against the tool's JSON schema before dispatch**; a call that does not fit is rejected, not sent. The same servers are merged into the `o-mcp.json` that Claude Code is started with, so both brains see one list. Each call is a `tool` entry and its result a `result` entry in the action log. | `mcp` absent: the router is unavailable and both tools say so. |
| Multi-language script steps | A provision step is either `{target_directory, command_string, timeout_seconds}` or `{target_directory, timeout_seconds, script: {language, code, target_directory, timeout_seconds}}`; exactly one of `command_string` / `script`. `language` is `python`, `bash`, `javascript` or `powershell`; `code` up to 64k characters. The engine writes the code to a temp file inside the sandbox and runs it with the native interpreter (`NativeRunner`: `python3` / `bash` / `node` / `pwsh`). Open Interpreter is a second backend (`OpenInterpreterRunner`, `interpreter.computer.run(language, code)`, `DISPLAY` from the isolated display) used when it is importable. Standalone: `script_run`, `POST /script/run`. | Open Interpreter absent: `available=False`, the native runner is used. |
| Disposable containment | A request with `risk: "high"` does not run on the host path sandbox. `highRiskSandbox` picks the backend: `e2b` (an E2B micro-VM through the `e2b` SDK, needs `e2bApiKey`), `docker` (default; one `docker run --rm --network <dockerNetwork>` per step, the job directory mounted at `/work`, image `dockerImage`, network `dockerNetwork`, default `bridge`; set it to `none` for an offline container), or `local`. Order of fallback: the preferred backend, then Docker, then the path sandbox with a logged `note` saying why. On exit 0 the working script is stored in the script cache like any other; the instance is then destroyed, and that is logged too. E2B runs in an empty `/work`: the job directory is never uploaded, so high-risk steps that depend on project files behave differently in E2B than in Docker. | Docker daemon or E2B key absent: logged fallback to the next backend. |
| Three-role swarm | `swarm_run` (`POST /swarm/run {objective, cwd}`) partitions planning across three roles passed in sequence: **Telemetry Auditor** (workspace profile from screenpipe and history) → **Subprocess Compiler** (strict `ProvisionRequest` through the parser) → **Environment Inspector** (verify ports headlessly, judge the isolated display). crewAI drives them (`Crew(process=sequential)`, LLM = local vLLM) when `crewai` is importable and `swarmEnabled` is on; otherwise `SimpleCrew` runs the same three roles on threads with a queue and produces the same `SwarmResult{profile, request, inspection}`. `swarm: true` on `/provision` uses it in the context node. | `crewai` absent: `SimpleCrew`, logged. |
| Headless web | `web_run` (`POST /web/run {task, url?, actions?, timeout_seconds?}`) hands a task to browser-use's agent on headless Chromium under the isolated display (`DISPLAY=:99` on Linux) with the local vLLM as its model; returns `{job_id}`, and the job's `result` carries `WebResult{ok, final_text, steps, screenshots_sha, via, error}`. The fallback is a Playwright DOM runner that takes scripted `{goto, fill, click, read, wait}` actions, no model; passing `actions` selects it directly. Neither touches your own tabs; that stays O Link's job. | `browser_use` absent: `available=False`, the Playwright DOM runner. |

Ruled out: CAPTCHA circumvention; proxy rotation; running high-risk code on the host when a disposable backend is configured and available; any MCP tool call that is not schema-validated before dispatch.

## Generative UI

O's panel can show plans, progress and results as components instead of prose. The rule that makes this safe: **the model never renders raw HTML or markdown in the panel.** The agent (Claude Code) and the engine emit small declarative **frames**; the renderer validates each one and maps it onto O's own design tokens and native DOM. A frame that does not fit is dropped and logged, never drawn.

| Design point | What O does |
|---|---|
| Frames | A2UI-style JSON, one object per frame: `begin` (surface, title) → `update` (components) / `data` (one value patch) → `end` (ok). Every frame names its **surface**: `job:<id>` for an engine job, `chat:<session>` for a chat. |
| Closed component set | `Column`, `Row`, `Card`, `Text`, `Step`, `Tree`, `Progress`, `KeyValue`, `Button`, `Preview`. Nothing else. An unknown component or property rejects the whole frame (strict pydantic on the engine, a hand-written validator in the renderer) and writes a `note` entry. |
| Closed Button actions | `open-log`, `open-file`, `retry`, `approve`, `deny`, handled by the panel; `open-file` always opens the action-log text file, never a path from the frame. A Button cannot run a command, open a URL or carry script. |
| Preview (sandboxed) | Custom HTML/CSS/JS the engine produced (for example a status card after `verify`) arrives as a `sandbox` frame and renders only inside an iframe with `sandbox="allow-scripts"`, **no** `allow-same-origin`, and a fixed height. The iframe loads `renderer/preview.html`, which carries its own CSP (`default-src 'none'`; `style-src 'unsafe-inline'`; `script-src` limited to the sha256 of its inline bootstrap plus `'strict-dynamic'`; `img-src data:`). The panel posts the document into the frame as `{type:'o-ui-load', html, css, js}`; the bootstrap accepts that message once, only from its parent window, sets the HTML and CSS and runs `js` as a `blob:` script. The only way out of the iframe is a postMessage `{type:'o-ui-event', name, payload}`, accepted only from that frame's own window; every such event is logged. |
| Where surfaces show | Inline in the chat, one card per surface id, and in the **Runs** sheet, which lists active and past job surfaces. |
| Engine emitters | Every job (provision, computer use, swarm, web) owns `job:<id>`: `begin` at context, a `Tree` of phases and a `Step` per step as the graph moves, `data` patches for step status, `end` at report. Frames are kept per job in a ring buffer; each one is pushed to the app (`POST /ui`, the `O_UI_URL` the app hands the engine), which relays it to the panel. `GET /jobs/{id}/ui` streams the same surface as SSE for backfill (`?since=<seq>`). |
| Agent emitter | `mcp__o__ui_emit {frames: [...]}` pushes frames into the current chat's surface. The persona tells Claude Code to prefer frames over prose when it reports plans, progress or results, and never to put HTML or markdown in them. |
| Settings | `genuiEnabled` (default on) turns the whole feature off; `genuiSandbox` (default on) turns Preview iframes off, in which case a `Preview` renders as an empty placeholder. |

**oui**, the compact form, in six lines:

1. Same frames, fewer tokens: the SSE backfill can send `oui` instead of JSON (`GET /jobs/{id}/ui?format=oui|a2ui`); `ui_emit` takes A2UI JSON frames; oui is the SSE/backfill form.
2. A frame line starts with `@`: `@begin <surface> <title>`, `@update <surface>`, `@data <surface> <path> <value>`, `@end <surface> true|false`, `@sandbox <surface> <id> <json>`.
3. After `@update`, one component per line: `<Type>#<id>[<k>=<v>;…]{<child ids>}`.
4. A value is bare when it is a plain token; otherwise it is JSON-encoded. Keys at their default are omitted.
5. Both sides have `compile` and `decode`, and both decode through the same validator as the JSON form.
6. Round trip is exact on normalized frames: `decode(compile(frames)) == frames`.

```
@begin job:7 "Install deps"
@update job:7
Col#plan{s0 s1}
Step#s0[l="npm install";st=run;p=0.4]
Step#s1[l="npm test"]
```

Ruled out: rendering model-produced HTML or markdown outside the sandbox; `allow-same-origin` on the Preview iframe; any component or Button action outside the closed sets; a UI frame or sandbox event that skips the action log.

## Settings (⚙ in the panel)

| Key | Meaning |
|---|---|
| `permissionMode` | `log` (default) / `ask` / `auto` |
| `theme` | panel look: `glass` (default, black translucent), `light`, `dark` |
| `enginePython` | Python for the engine; empty = auto (`python3` / `python` / `py -3`) |
| `vllmUrl` | OpenAI-compatible vLLM endpoint, default `http://127.0.0.1:8000/v1` |
| `vllmModel` | default `meta-llama/Meta-Llama-3-8B-Instruct` |
| `screenpipeDb` | screenpipe SQLite path; empty = default |
| `sandboxRoot` | engine command tree, default `<home>/o-sandbox` |
| `maxRemediationAttempts` | default 3 (1..10) |
| `logUnreadBadge` | badge + away notification, default on |
| `browserMode` | `link` (all tabs via O Link, default) / `mine` (Playwright Extension, picked tabs) / `separate` (O's own invisible browser) |
| `extensionToken` | `mine` mode only: `PLAYWRIGHT_MCP_EXTENSION_TOKEN` |
| `browserProfile` | `mine` mode only: Chrome profile folder name |
| `browserHeadless` | O's own browser runs invisibly (default on) |
| `browser` / `browserPath` | force a browser channel, or an explicit executable |
| `quiet` | never take keyboard focus (default on) |
| `screenAccess` | allow screenshots while on a task (default on) |
| `cwd` | working folder for shell and file tools (default: home) |
| `claudePath` | path to the `claude` binary |
| `model` | optional `claude --model` override |
| `autoUpdateCheck`, `updateRepo`, `updateBranch`, `updatePath`, `updateToken` | self-update source |
| `openAtLogin` | start O at login |
| `proxyEnabled` | API proxy on/off, default on |
| `proxyPort` | default 8787, loopback only |
| `proxyToken` | bearer token; generated on first run, persisted |
| `proxyConcurrency` | parallel `claude` children, default 4 |
| `proxyMode` | default system prompt for API callers: `code` (default) / `chat` |
| `cuCandidates` | computer-use best-of-N, default 1 (3 recommended on Linux) |
| `cuMaxHeals` | heal cycles per procedure run, default 3 |
| `vllmVisionModel` | local VLM for vision grounding and the screenshot judge |
| `cuStepsPerTurn` | steps the planner may emit per observation, default 5 (1..20); env `O_CU_STEPS_PER_TURN` |
| `cuMaxTurns` | plan/observe turns per task before `ok=false`, default 20 (1..200); env `O_CU_MAX_TURNS` |
| `cuGroundConfidence` | grounding answers below this confidence are discarded and the fallback runs, default 0.3 (0..1); env `O_CU_GROUND_CONFIDENCE` |
| `cuStepCandidates` | candidate plans per step, judged on the live tree, default 1 (1..8); env `O_CU_STEP_CANDIDATES` |
| `cuFeasibilityGate` | read-only probes + one model question before planning (default on); env `O_CU_FEASIBILITY` |
| `cuZoomGrounding` | two-step zoom-in grounding with the consistency gate (default on); env `O_CU_ZOOM` |
| `cuComparativeRanking` | best-of-N ranks all candidates in one model call (default on); env `O_CU_COMPARATIVE` |
| `cuInterfaceBuilder` | build `api.py` per app after a fresh procedure is stored (default on); env `O_CU_INTERFACE_BUILDER` |
| `mcpServers` | JSON text, Claude Code's `mcpServers` shape; routed by the engine and merged into Claude Code's `o-mcp.json`; default `{}` |
| `highRiskSandbox` | where `risk: "high"` steps run: `docker` (default) / `e2b` / `local` |
| `e2bApiKey` | E2B API key for the `e2b` backend; empty = E2B unavailable; never logged |
| `dockerImage` | image for the Docker backend, default `python:3.12-slim` |
| `dockerNetwork` | Docker network for the Docker backend (`docker run --network <name>`), default `bridge`; `none` runs the container offline (installs inside it fail); env `O_DOCKER_NETWORK` |
| `swarmEnabled` | use crewAI for the three-role swarm when installed (default on); off = `SimpleCrew` |
| `genuiEnabled` | generative UI frames in the panel and the Runs sheet (default on); off = engine emits nothing (`O_GENUI=0`) and `ui_emit` frames are not rendered |
| `genuiSandbox` | sandboxed Preview iframes (default on); off = a `Preview` shows a placeholder, the sandbox HTML is never loaded |

Settings live in O's JSON settings file in Electron's userData folder.

## Demos and launch video

`tools/demo/` records the demos with Playwright on a 1600×1000 mock desktop that embeds the real `renderer/index.html` (pet) and `renderer/panel.html` (panel) and talks to a real backend: the Python engine (`python3 -m o_engine serve`), the real action log (`main/actionlog.js`) and the real API proxy (`main/proxy.js`) with `claude` replaced by `main/tools/fake-claude.js`, so no Claude login is needed. Jobs, cache replays, GenUI frames, log entries, hash-chain verification and proxy requests in the videos are real; the chat turns (which tools Claude Code would call, what it says) are scripted in `tools/demo/demos/<id>.js` from the storyboard `<id>.json`, and the videos say so in a corner badge.

```sh
npm run demo:record          # docs/videos/d1.mp4 … d4.mp4 (+ .json with duration/size), 3200×2000 h264 30 fps, no audio
npm run demo:record -- d2    # one demo
npm run demo:launch          # docs/videos/o-launch.mp4: title card, d1–d4, differentiator card, end card
node tools/demo/launch.js --voiceover ./vo   # mux ./vo/title.wav, d1.wav … d4.wav, why.wav, end.wav
```

| Demo | Shows |
|---|---|
| `d1` Set up a project | a request → real provisioning job (sandboxed steps) → Runs sheet → action log → the same request replays from the script cache (“Déjà vu”) → the files it made |
| `d2` The log is the product | log sheet, filters, expanded row, **Verify chain ✓**, one byte edited in `o-actions.jsonl` on disk, **Verify chain ✗ broken at #n** |
| `d3` Use O from any app | `curl` with `stream: true` against the real proxy (SSE chunks, `[DONE]`), the `openai` Python client with `o-ai-code`, both requests in the action log |
| `d4` Computer use without touching your mouse | `/computer/run` on a scripted accessibility tree (`O_CU_FAKE_TREE`): plan → a step fails → heal → judged → stored as a procedure; `/computer/replay` with zero planner calls |

Needs Playwright with Chromium (`npx playwright install chromium`) and `ffmpeg` on PATH for mp4; without ffmpeg the Playwright `.webm` is written instead and `demo:launch` is skipped. `O_DEMO_SHOTS=<dir>` saves a PNG at every caption for checking a storyboard without watching the video. The differentiator card in the launch video only states what this README states; it names no other product.

## Troubleshooting

- **"Claude Code is not logged in"**: run `claude` once in a terminal and sign in. On macOS, apps launched from Finder do not see your shell PATH; set `claudePath` (find it with `which claude`).
- **Python row red, "no python3/python/py found"**: install Python 3.10+ or set Settings → Engine → Python path.
- **"engine not running" / "engine did not answer /health within 20 s"**: the engine failed to start. Check that `pip install -e engine` ran with the Python shown in the requirements check; the engine's stderr is in the action log entry. The app restarts it with backoff, up to 5 times.
- **vLLM red**: start it with `python -m vllm.entrypoints.openai.api_server --model <vllmModel> --port 8000`. Parsing falls back to Claude Code meanwhile.
- **screenpipe red**: install and run screenpipe, or set `screenpipeDb`. Context still builds from history and tree.
- **Xvfb red on macOS/Windows**: expected; "Not available on this OS: the isolated display is headless-only here."
- **Playwright red**: `python -m playwright install chromium` with the engine's Python.
- **Verify chain shows ✗ broken at #n**: the log file was edited or truncated at that index. Nothing before it is affected; keep the file and treat later entries as unverified.
- **"Blocked by O" on a page**: the payment guard. O never pays.
- **Linux: black box instead of transparency**: O already starts with `--disable-gpu` and `--enable-transparent-visuals`; a compositing window manager (picom, Mutter, KWin) is still required.

## Project layout

```
O/
  main/main.js             Electron main: pet, panel, tray, task queue, action log wiring, requirements check, GenUI store + relay (POST /ui -> {type:'genui', frame})
  main/preload.js          window.o bridge (logTail, logMarkRead, logVerifyChain, engineStatus, checkRequirements, onGenui, genuiFrames, genuiSurfaces, genuiAction, logNote)
  main/settings.js         JSON settings
  main/agent.js            Claude Code CLI wrapper; log/ask/auto decisions; records tool/result entries; POST /ui envelope check for GenUI frames
  main/actionlog.js        hash-chained action log: record, ingest, tail, verifyChain
  main/engine.js           spawns and supervises the Python engine, HTTP client
  main/chats.js            chat history
  main/link.js             O Link relay (extension <-> Playwright MCP), payment-site block
  main/proxy.js            API proxy: OpenAI-compatible HTTP on 127.0.0.1, claude -p per request, SSE/JSON normalizer
  main/promptmod.js        buildSystemPrompt (code | chat), stripPersona rules
  main/tokenfilter.js      node port of TokenFilter (first balanced JSON)
  main/tools/bridge-mcp.js MCP server `o`: approve, see_screen, log_note, provision, cache_lookup, diagnose, verify_port, workspace_context, computer_run, computer_replay, computer_find, computer_tree, mcp_tools, mcp_call, script_run, swarm_run, web_run, ui_emit
  main/tools/selftest-proxy.js  proxy selftest against main/tools/fake-claude.js
  extension/               O Link browser extension + payment guard
  renderer/                pet window, panel, log sheet, Runs sheet, sprite
  renderer/genui.js        GenUI: frame validator, oui decoder, A2UI -> DOM on panel.css tokens, sandboxed Preview iframe + postMessage bridge
  renderer/preview.html    Preview iframe host: own CSP (hash-pinned bootstrap, 'strict-dynamic'), takes one o-ui-load message, runs the js as a blob: script
  renderer/genui.css       GenUI component styles (status pills shared with the action log)
  engine/o_engine/
    schema.py              ProvisionStep / ScriptPayload / ProvisionRequest (risk) / VerifyTarget (pydantic, strict)
    tokenfilter.py         first balanced JSON from a token stream
    parser.py              LocalModelParser (vLLM) with ClaudeCodeParser fallback
    context.py             workspace profile, screenpipe telemetry
    executor.py            Sandbox, StepResult
    display.py             IsolatedDisplay (Xvfb :99)
    cache.py               ScriptCache (noclobber)
    gate.py                permission gate, QueueDestroyed
    remediate.py           Remediator
    graph.py               langgraph StateGraph, fixed edges
    server.py              FastAPI app
    cli.py                 o-engine serve | run | cache ls | context | check | computer run|procedures | tools ls|call | script run | swarm run | web run
    actionlog.py           engine-side action log
    mcp_router.py          MCPRouter: mcp-servers.json -> list_tools / call, schema-validated before dispatch
    runners.py             Runner for script steps: NativeRunner, OpenInterpreterRunner
    containment.py         DisposableSandbox: E2BSandbox, DockerSandbox, select_sandbox, run_high_risk
    swarm.py               three roles; SimpleCrew (threads) and CrewAICrew (crewAI); get_crew
    web.py                 BrowserUseRunner (headless, isolated display), PlaywrightDomRunner fallback
    genui.py               GenUI frames (pydantic, closed component set), UIStore ring buffer per surface, JobSurface emitters, oui compile/decode
    cu/schema.py           OScript (pydantic, frozen, extra=forbid)
    cu/backends/           CUBackend: atspi.py (Linux), uia.py (Windows), ax.py (macOS)
    cu/executor.py         runs OScript against a backend; per-step action log entries
    cu/ground.py           VisionGrounder, ClaudeCodeGrounder, LocatorCache
    cu/judge.py            a11y_assert on the tree; VLMJudge with ClaudeCodeJudge fallback
    cu/planner.py          plan(goal) -> OScript; BestOfN
    cu/procedures.py       append-only procedure store with lineage
    cu/interfaces.py       per-app interface notes
    cu/graph.py            second fixed graph: context -> plan -> ground -> execute -> judge -> (heal) -> store -> report
  engine/eval/osworld/     OSWorld-Verified runner: run_o.py, summarize.py, README
  engine/tests/            pytest
  install/install.sh       Linux/macOS installer
  install/install.ps1      Windows installer
  docs/ARCHITECTURE.md     components, dataflow, threat model
  engine/README.md         CLI, HTTP API, schema, graph
```
