# O engine

Python provisioning engine behind the O desktop app: strict step schema, fixed graph, sandboxed execution, script cache, bounded remediation, hash-chained action log. Package `o_engine`, version 1.0.0, Python >= 3.10.

## Install

```
python -m venv .venv && . .venv/bin/activate
pip install -e .
playwright install chromium
```

`pip install -e ".[dev]"` adds pytest.

Hard dependencies (all installed by `pip install -e .`):

| Package | Floor | Used for |
|---|---|---|
| `pydantic` | 2.6 | step schema, grounder/judge result models |
| `langgraph` | 0.2 | the fixed provisioning graph |
| `fastapi` / `uvicorn` | 0.110 / 0.27 | HTTP API |
| `httpx` | 0.27 | vLLM/screenpipe probes, verify fallback |
| `openai` | 1.30 | OpenAI-compatible client for vLLM |
| `playwright` | 1.44 | web runner and verify (`playwright install chromium` after install) |
| `pillow` | 10 | screenshot cropping for zoom-in grounding (`o_engine.cu.ground.ZoomGrounder`, `MultiCropVote`); `crop_fn`/`size_fn` stay pluggable |

## CLI

`o-engine` (or `python -m o_engine`). `--data-dir` is accepted before or after the subcommand.

| Command | Flags | Exit | Example |
|---|---|---|---|
| `serve` | `--port` (required), `--host` (default `127.0.0.1`) | 2 if `O_ENGINE_TOKEN` unset | `O_ENGINE_TOKEN=s3cret o-engine serve --port 8765` |
| `run <file.json>` | — | 0 if `ok`, 1 otherwise; prints final JobState | `O_PERMISSION_MODE=auto o-engine run req.json` |
| `cache ls` | `--json` | 0 | `o-engine cache ls --json` |
| `context [cwd]` | — | 0; prints WorkspaceProfile | `o-engine context ~/proj` |
| `check` | — | 1 if a required probe fails | `o-engine check` |
| `computer run <goal>` | `--app <id>`, `--candidates <N>` (best-of-N; default `O_CU_CANDIDATES`) | 2 if `--candidates` > 1 off Linux (same refusal as `POST /computer/run`); 0 if the job's `ok`, 1 otherwise; prints the job state | `o-engine computer run "open a new sheet" --app libreoffice-calc` |
| `computer procedures` | `--json` | 0; lists stored procedures (`<data_dir>/procedures`) | `o-engine computer procedures --json` |
| `tools ls` | `--json` | 0; lists the tools of every server in `mcp-servers.json` | `o-engine tools ls` |
| `tools call <server> <tool> ['<json args>']` | — | 2 if the args are not a JSON object or the call cannot be built; 0 if the result `ok`, 1 otherwise; prints the ToolResult | `o-engine tools call fs read_file '{"path": "a.txt"}'` |
| `script run <language> <file>` | `--dir <target_directory>` (default `.`), `--timeout <s>` (default 120); language is `python`, `bash`, `javascript` or `powershell` | 2 if the file is unreadable, the payload invalid or the sandbox escaped; 0 if exit code 0 and not timed out, 1 otherwise; prints the step record with `sandbox` | `o-engine script run python hello.py` |
| `swarm run "<objective>"` | `--cwd <dir>` (default: current directory) | 0 if the crew produced a `request`, 1 otherwise (or on any exception); prints `{ok, error, result}` | `o-engine swarm run "set up this repo" --cwd ~/proj` |
| `web run "<task>"` | `--url <url>` | 2 if `browser_use` is absent and no `--url` given; 0 if the WebResult `ok`, 1 otherwise. With browser-use: the agent runs the task from `--url`; without it, `--url` is required and the Playwright DOM runner does `goto` + `read` | `o-engine web run "read the headline" --url https://example.com` |

`run` executes synchronously in-process with the configured permission mode; in `ask` mode its approver always denies. `check` probes vllm, screenpipe, playwright (required) and Xvfb (required on Linux only).

## Environment variables

| Variable | Default | Used for |
|---|---|---|
| `O_ENGINE_TOKEN` | `""` | `x-o-token` the HTTP API requires; `serve` refuses without it |
| `O_LOG_URL` | unset | app endpoint each action-log entry is POSTed to |
| `O_LOG_TOKEN` | `O_ENGINE_TOKEN` | header sent with forwarded entries |
| `O_VLLM_URL` | `http://127.0.0.1:8000/v1` | local parser model |
| `O_VLLM_MODEL` | `meta-llama/Meta-Llama-3-8B-Instruct` | local parser model name |
| `O_SCREENPIPE_DB` | `~/.screenpipe/db.sqlite` | telemetry source |
| `O_SANDBOX_ROOT` | `~/o-sandbox` | one subdirectory per job id |
| `O_PERMISSION_MODE` | `log` | gate mode: `log`, `ask`, `auto` |
| `O_MAX_ATTEMPTS` | `3` | remediation ceiling per job |
| `O_CLAUDE_PATH` | `claude` | Claude Code CLI, parser fallback |
| `O_CU_CANDIDATES` | `1` | best-of-N computer-use candidates per goal (and the number of `cu_displays` the engine prepares); N > 1 is Linux-only |
| `O_CU_MAX_HEALS` | `3` | bounded self-heal attempts per computer-use job |
| `O_VLLM_VISION_MODEL` | `""` | UI-grounding VLM served by vLLM for vision grounding and judging; empty = `O_VLLM_MODEL` |
| `O_WEB_PREFER` | `browser-use` | web runner choice: `browser-use` when importable, anything else (or browser-use absent) = Playwright DOM runner |
| `O_DATA_DIR` | `~/.o-engine` | action log, `cache/`, `context.db`, `telemetry.jsonl`, `mcp-servers.json` |
| `O_MCP_SERVERS_FILE` | `<O_DATA_DIR>/mcp-servers.json` | MCP server config the router reads (`{name: {command, args, env} \| {url}}`, Claude Code's `mcpServers` shape) |
| `O_HIGH_RISK_SANDBOX` | `docker` | backend for `risk: "high"` requests: `e2b`, `docker`, `local` |
| `E2B_API_KEY` | `""` | E2B API key; empty = E2B backend unavailable; never logged |
| `O_DOCKER_IMAGE` | `python:3.12-slim` | image for the Docker backend |
| `O_DOCKER_NETWORK` | `bridge` | Docker network passed as `docker run --network <name>`; `none` runs the container offline. The `dockerNetwork` setting wins over the env |
| `O_SWARM` | `1` | `0`/`false` = never use crewAI; `SimpleCrew` runs the roles |
| `O_GENUI` | `1` | `0` = no `UIStore`; jobs emit no UI frames and `/jobs/{id}/ui` has nothing to stream |
| `O_CU_STEPS_PER_TURN` | `5` | steps the planner may emit per observation on the milestone loop (1..20) |
| `O_CU_MAX_TURNS` | `20` | observe→plan→run turns per computer-use job before it reports failure (1..200) |
| `O_CU_GROUND_CONFIDENCE` | `0.3` | zoom-grounding consistency gate; below it the locator is not cached and the fallback grounder runs (0..1) |
| `O_CU_STEP_CANDIDATES` | `1` | plans sampled for the next step; the first whose targets resolve in the live tree wins (1..8) |
| `O_CU_FEASIBILITY` | `1` | `0` = skip the read-only feasibility gate before planning a fresh goal |
| `O_CU_ZOOM` | `1` | `0` = ground on the full screenshot only (no crop-and-regrind, confidence always 1.0) |
| `O_CU_COMPARATIVE` | `1` | `0` = best-of-N scores candidates one by one instead of one comparative ranking call |
| `O_CU_INTERFACE_BUILDER` | `1` | `0` = never build `interfaces/<app>/v<N>/api.py` after a fresh procedure is stored |
| `O_UI_URL` | unset | app endpoint (`POST /ui`) each UI frame is forwarded to with `x-o-token`, like `O_LOG_URL` for log entries; the store is the record, a failed forward is dropped |

## HTTP API

FastAPI, no docs/openapi routes. Every route requires header `x-o-token` matching `O_ENGINE_TOKEN` (401 otherwise). A `SchemaViolation` anywhere returns 422 `{"detail": "<message>"}` and writes a `policy` log entry. Jobs run on a 4-thread pool, each in `<O_SANDBOX_ROOT>/<job_id>`.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/health` | — | `{ok, version, vllm, screenpipe, xvfb, playwright, cu_backend, cu_displays, mcp_servers, docker, e2b, crewai, browser_use, open_interpreter}` (`vllm`, `screenpipe`, `xvfb`, `playwright`, `docker`, `e2b`, `crewai`, `browser_use`, `open_interpreter` are bools, `mcp_servers` the count of configured servers; vLLM probe is `GET <url>/models`, 2 s) |
| POST | `/provision` | ProvisionRequest | `{job_id}`; job queued, returns immediately |
| GET | `/jobs/{job_id}` | — | JobState; 404 unknown job |
| POST | `/jobs/{job_id}/approve` | `{allow: bool}` | `{ok}`; `ok` is true only in `ask` mode; 404 unknown job |
| POST | `/cache/lookup` | `{request: ProvisionRequest}` | `{hit, fingerprint, script_path?}` |
| GET | `/cache` | — | `[CacheEntry]` |
| POST | `/diagnose` | `{stderr, stdout, exit_code, command, cwd}` | Diagnostic |
| POST | `/parse` | `{text}` | `{steps: [...]}`; empty text is 422; text starting with `[`, ``` ``` ``` or `~~~` is parsed as a JSON step list, otherwise sent to the parser |
| POST | `/verify` | VerifyTarget | `{ok, status, elapsed_ms, error, via}`; 422 on invalid target |
| GET | `/context?cwd=` | — | WorkspaceProfile (cwd defaults to the engine process cwd) |
| GET | `/tools/list` | — (`?refresh=true` re-lists) | `{tools: [{server, name, input_schema}]}` from every configured MCP server |
| POST | `/tools/call` | `ToolCall {server, tool, args}` | `ToolResult {ok, content, is_error, elapsed_ms}`; args that do not fit the tool's schema are refused before dispatch |
| POST | `/script/run` | `ScriptPayload {language, code, target_directory, timeout_seconds}` | StepResult-shaped record (`exit_code, stdout_tail, stderr_tail, elapsed_ms, timed_out`); runs in the job sandbox through the Runner |
| POST | `/swarm/run` | `{objective, cwd}` | `{job_id}`; the job's final state carries the `SwarmResult {profile, request, inspection, roles}` |
| POST | `/web/run` | `{task?, url?, actions?, timeout_seconds?}` (one of `task`, `url`, `actions` required; `actions` is a list of `DomAction {op: goto\|fill\|click\|read\|wait, selector?, text?, url?, seconds?}`; `timeout_seconds` 1..3600, used by the DOM runner, default `DOM_TIMEOUT_S`) | `{job_id}`; the job's `result` carries the `WebResult {ok, final_text, steps, screenshots_sha, via, error}`. `actions` selects the scripted Playwright DOM runner; otherwise `task` goes to browser-use under the isolated display, or, when browser-use is absent and `url` is given, to a goto+read on the DOM runner |
| POST | `/computer/run` | `{goal, app?, candidates?, display?, procedure_id?}` | `{job_id}`; the job's state carries the cu phases, `cached`, `infeasible`, the procedure id and the judge verdict |
| GET | `/computer/procedures` | — | every stored procedure (append-only versions with lineage) |
| POST | `/computer/replay` | `{procedure_id}` | `{job_id}`; replays the stored OScript with zero planner calls |
| GET | `/computer/interfaces` | — | `{apps: [{app, latest, functions: [{name, description, signature}], fitness}]}` — executable per-app interfaces; bodies are never returned |
| GET | `/computer/tree` | — (`?window=`) | the accessibility tree summary of the isolated display |
| POST | `/computer/find` | `{target: Target}` | `{found, node}` from the slot-0 backend (no action taken) |
| GET | `/jobs/{job_id}/ui` | — (`?format=a2ui\|oui`, `?since=<seq>`) | SSE stream of the job's surface `job:<id>`: the stored frames after `since`, then live frames until the surface's `end`; `a2ui` sends one JSON frame per event, `oui` the compiled text; 404 unknown job |
| POST | `/ui/emit` | `{frames: [...]}` (JSON frames, each a dict or JSON text) | `{accepted}`; every frame goes through `parse_frame` and into the `UIStore`; the first invalid frame is 422 and nothing from that body is stored |

## Schema

`ProvisionStep` is frozen, strict, `extra="forbid"`:

| Field | Type | Limits |
|---|---|---|
| `target_directory` | str | 1–1024 chars, single line, no fence |
| `command_string` | str | 1–4000 chars, no NUL |
| `timeout_seconds` | int | 1–3600 |

A step may carry `script` instead of `command_string` (exactly one of the two). `ScriptPayload` is frozen, strict, `extra="forbid"`:

| Field | Type | Limits |
|---|---|---|
| `language` | `python` \| `bash` \| `javascript` \| `powershell` | — |
| `code` | str | 1–65536 chars, no NUL |
| `target_directory` | str | as above; must equal the step's |
| `timeout_seconds` | int | 1–3600; must equal the step's |

`ProvisionRequest`: `label` (1–200), `steps` (1–64), `verify` (VerifyTarget or null), `isolated_display` (default false), `risk` (`normal` default, or `high`). `VerifyTarget`: `port` 1–65535, `path` default `/` (must start with `/`, max 512), `expect_status` default 200 (100–599), `timeout_seconds` default 30 (1–600).

Rejected with `SchemaViolation`: request text wrapped in ``` ``` ``` or `~~~`; non-JSON; non-object (non-array for `/parse`); extra keys; wrong types (strict, no coercion); any `command_string` line that is a markdown list item, opens like prose (`sure`, `here is`, `let's`, `first`, `step 1`, `note:`, `you can`, ...), or contains a three-word sentence ending in `.!?` with no shell hint (`-x`, `$`, `|`, `&&`, `;`, `>`, `<`, `./`, `/`, `=`, `\`).

Fingerprint: SHA-256 over `{platform, steps, verify, isolated_display, risk}` (script payloads included); label excluded.

## Graph

```
START -> context -> validate -> cache_lookup -> gate -> execute -> verify -> cache_store -> report -> END
                                                 ^                    |  (fail, attempt < max_attempts)
                                                 |                    v
                                                 +---- remediate <----+   (else: report)
```

| Node | Does |
|---|---|
| context | WorkspaceProfile for `steps[0].target_directory`; resolves `max_attempts` |
| validate | re-parses the request; computes fingerprint |
| cache_lookup | `cached = entry exists` |
| gate | permission check (see below) |
| execute | replays cached script, or runs steps in order, stopping at first non-zero exit / timeout; a `script` step goes through the Runner, a `risk: "high"` request through `containment.run_high_risk` |
| verify | fails if any step failed; else probes `verify` target; else ok |
| cache_store | new entry on first success, `runs += 1` on replay |
| remediate | one bounded repair pass, then back to gate; `cached` reset to false |
| report | final `ok`/`error` |

Edges are the literal `GRAPH_EDGES` tuple; remediate -> gate is the only cycle. Recursion limit 96 (`16 + 4 * 20`). Any exception aborts the job with `error = "<Type>: <msg>"` and the phase it died in.

JobState keys: `id, label, phase, attempt, max_attempts, cached, steps, diagnostic, verify, done, ok, error`. Each `steps[]` record: `index, command, cwd, exit_code, stdout_tail, stderr_tail` (last 4000 chars), `elapsed_ms, timed_out`.

## Gate (`log` / `ask` / `auto`)

| Mode | Behaviour |
|---|---|
| `log` | writes a `policy` entry listing every command, passes |
| `ask` | blocks up to 60 s for `POST /jobs/{id}/approve`; deny, timeout or no approver raises `QueueDestroyed` |
| `auto` | passes silently |

## Script cache

`<O_DATA_DIR>/cache/<fingerprint>.json` (CacheEntry: `fingerprint, label, created, platform, script_path, timeout_seconds, runs`) plus `<fingerprint>.sh` (`set -e`, chmod 755) or `.ps1` on Windows. Files are opened with `x` (noclobber): an existing fingerprint raises `CacheExists`. Nothing deletes entries; `bump_runs` is the only rewrite. Replay runs `bash <script>` (or `pwsh`/`powershell -File`) as a single step in the job sandbox with timeout = max step timeout.

## Remediation

Trigger: verify failed and `attempt < max_attempts`. The failing step's `stderr_tail` (else `stdout_tail`, else the verify error) is scanned against `ERROR_PATTERNS` (specific first: `ModuleNotFoundError`, `ImportError`, `npm ERR!`, `command not found`, `ENOENT`, `EACCES`, `EADDRINUSE`, `No such file or directory`, `Permission denied`, `address already in use`, `SyntaxError`, `TypeError`, `fatal:`, `error[E..]`, `error`, `failed`, `Traceback`, ...); the last 40 lines, headed by the matched line, go to the parser. Returned steps:

- `patch:<path>|||<find>|||<replace>` — pseudo-step applied through the sandbox (`find` 1–20000 chars, `replace` <= 20000); malformed form is a `SchemaViolation`.
- anything else — replaces the failing step (appended if no step index).

`diagnostic` on JobState: `error_line, pattern, failed_index, failed_command, attempt, patches, replacement` (or `parser_error`). Nothing in remediation runs commands.

## Context

`<O_DATA_DIR>/context.db` (SQLite) tables: `shell_history(ts, shell, cwd, cmd)` (wiped and refilled each collect), `tree_snapshots(ts, root, json)` (appended), `config_files(ts, path, sha256, head)` (rows under cwd replaced). Collected: last 200 lines of `~/.bash_history`, `~/.zsh_history`, PowerShell `ConsoleHost_history.txt`; directory tree depth 3, max 2000 entries, skipping `node_modules .git venv .venv`; sha256 + first 2048 bytes of `package.json pyproject.toml requirements.txt docker-compose.yml .env.example Makefile`; telemetry from the last 120 s (screenpipe `frames`/`ocr_text`, else `<O_DATA_DIR>/telemetry.jsonl`). `WorkspaceProfile`: `cwd, collected_at, shell_history, tree, config_files, telemetry, platform`.

## Isolated display

Linux only: `Xvfb :99 -screen 0 1280x800x24 -nolisten tcp`, lock `/tmp/.X99-lock` (an existing lock is treated as an external server). Host `DISPLAY` is never read or changed. With `isolated_display: true` the sandbox sets `DISPLAY=:99` on Linux; elsewhere no display is set. Verify is headless Playwright Chromium, falling back to a plain `httpx` GET (`via: "httpx"`, note in `error`) when Playwright is unavailable.

## Action log

`<O_DATA_DIR>/engine-actions.jsonl` (+ `engine-actions.log` text twin), append-only, one `ActionLog` per data dir via `get_log(data_dir, forward_url, token)`. Each entry is also POSTed to `O_LOG_URL` with `x-o-token` (0.5 s timeout, failures ignored).

```
{"ts": 1730000000.1, "iso": "2026-11-04T22:30:00", "source": "engine",
 "kind": "task|tool|result|policy|graph|cache|remediate|verify|settings|note",
 "summary": "one line", "detail": {}, "prev": "<sha256 of previous entry>", "hash": "<sha256 of entry without hash>"}
```

Genesis `prev` is 64 zeros. Unknown `kind` raises `ValueError`.

## Sandbox

`Sandbox(root)`: `target_directory` must resolve inside root (symlinks followed) or `SandboxEscape`. Env vars starting `CLAUDE`, `ANTHROPIC`, `O_` or containing `TOKEN`, `SECRET`, `KEY`, `PASSWORD` are scrubbed from step environments.

## Script runners

`runners.py`. `Runner.run(payload, sandbox) -> StepResult`. `NativeRunner` writes `code` to a temp file inside the sandbox and runs `python3` / `bash` / `node` / `pwsh` on it through `Sandbox.run` (same timeout, scrubbed env, `DISPLAY` when isolated). `OpenInterpreterRunner` calls `interpreter.computer.run(language, code)` when `interpreter` is importable; otherwise `available` is False and `get_runner` returns the native runner.

## Disposable containment

`containment.py`. `select_sandbox(preference, job_dir)` tries `[preference, docker]` and returns `None` when neither is available (the path sandbox is used, and a `note` entry says why). `DockerSandbox(image, job_dir, network)`: one `docker run --rm --network <network> -v <job dir>:/work -w /work` per step (`network` from the `dockerNetwork` setting, else `O_DOCKER_NETWORK`, default `bridge`; `none` for an offline container), the step's scrubbed env passed with `-e`, container killed and removed in `destroy()`. `E2BSandbox(api_key, template)`: one `e2b` instance created on the first `run`, killed in `destroy()`. `run_high_risk` runs every step, stops at the first failure, destroys the instance in `finally`, and returns the StepResults so verify and cache_store behave as for any other run.

## Swarm

`swarm.py`. Roles `telemetry_auditor` → `subprocess_compiler` → `environment_inspector`, each a callable over a shared state dict. `SimpleCrew` (always available) runs each role on its own thread and waits on a queue, with a per-role timeout; `CrewAICrew` wraps the same callables as crewAI tools with the local vLLM as LLM. `get_crew` returns crewAI when importable and enabled (`O_SWARM`), else `SimpleCrew`, and logs the choice. Result: `SwarmResult {profile, request, inspection, roles: [{role, elapsed_ms, ok, note}]}`.

## Headless web

`web.py`. `BrowserUseRunner(vllm_url, model)` runs `browser_use.Agent` on headless Chromium with `DISPLAY` from the isolated display; `available` is False when `browser_use` is not importable. `PlaywrightDomRunner` executes scripted `{goto, fill, click, read, wait}` actions with no model. Neither attaches to the user's own browser.

## Generative UI

`genui.py`. The engine never renders anything; it emits validated frames the panel maps onto its own DOM. Frames are strict, frozen pydantic (`extra="forbid"`): `Begin {surface, title}`, `Update {surface, components}`, `Data {surface, path, value}`, `End {surface, ok}`, `Sandbox {surface, sandbox_id, html, css, js}`; `a2ui` is always `"1"`; `surface` is `job:<id>` or `chat:<id>`. `parse_frame(raw)` accepts a dict or JSON text and raises `SchemaViolation` on an unknown frame type, component, property or Button action.

Closed component set: `Column {children}`, `Row {children}`, `Card {title, children}`, `Text {text, tone: muted|normal|error}`, `Step {label, status: pending|running|ok|failed|skipped, detail, progress 0..1}`, `Tree {nodes: [{id, label, status, children}]}`, `Progress {value 0..1, label}`, `KeyValue {items: [[k, v]]}`, `Button {label, action: open-log|open-file|retry|approve|deny, payload}`, `Preview {sandbox_id, height}`. On the wire a component is `{"id": "...", "component": {"<Type>": {...}}}`.

```
{"a2ui":"1","type":"begin","surface":"job:7","title":"Install deps"}
{"a2ui":"1","type":"update","surface":"job:7","components":[
  {"id":"plan","component":{"Column":{"children":["s0","s1"]}}},
  {"id":"s0","component":{"Step":{"label":"npm install","status":"running","progress":0.4}}},
  {"id":"s1","component":{"Step":{"label":"npm test"}}}]}
{"a2ui":"1","type":"data","surface":"job:7","path":"steps.0.status","value":"ok"}
{"a2ui":"1","type":"end","surface":"job:7","ok":true}
```

`UIStore(maxlen=2000, forward_url=O_UI_URL, token)`: one ring buffer per surface with a per-surface sequence number; `append` stores, notifies SSE subscribers and forwards `{frames: [frame]}` to the app from a background thread. `JobSurface(store, job_id, title, phases)` is the emitter the graphs use through `job_surface` / `ui_phase` / `ui_step` / `ui_preview_status` / `ui_end` (all no-ops without a store): `begin` once, a `Tree` of phases and a `current` `Step` on each phase, a `Step` per executed step with `data` patches, an optional `Preview` whose content is `status_card_html` (engine-generated, escaped; the only producer of `Sandbox` frames in the engine), `end` once.

`oui.compile(frames) -> str` and `oui.decode(text) -> list[Frame]` are the compact form: `@begin|@update|@data|@sandbox|@end <surface> …` lines, one `<Type>#<id>[k=v;…]{child ids}` line per component, values bare when they match `[A-Za-z0-9_.:/@-]+` and JSON-encoded otherwise, defaults omitted. `decode(compile(frames)) == frames`, exact on normalized frames. The same example as oui:

```
@begin job:7 "Install deps"
@update job:7
Col#plan{s0 s1}
Step#s0[l="npm install";st=run;p=0.4]
Step#s1[l="npm test"]
@data job:7 steps.0.status "ok"
@end job:7 true
```

## Running tests

```
python -m pytest -q
```
