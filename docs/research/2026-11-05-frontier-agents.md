# Frontier computer-use agents vs O — research memo (2026-11-05)

Scope: what is pushing open agents past "macros/wrappers over the screen" as of early Nov 2026, and
what of it O can build locally (vLLM + Claude Code login, no cloud key). Nothing here is a claim about
O's performance; O has no benchmark number until `engine/eval/osworld/run_o.py` is run.

Method note: the two official leaderboards were read from their data files, not from third-party
trackers: OSWorld-Verified rows come from `http://osworld-v1.xlang.ai/static/data/osworld_verified_results.xlsx`
(fetched 2026-11-04; per-row dates are the sheet's `Date` column), OSWorld 2.0 rows from
`https://osworld-v2.xlang.ai/static/data/leaderboard/official-results.json` (`updatedAt: 2026-10-15`),
ScreenSpot-Pro rows from `https://gui-agent.github.io/grounding-leaderboard/results/screenspot_pro.json`
(page dated Sep 10, 2026). "OSWorld-Verified" below is the benchmark's name, not a statement about O.

## 1. Ranked repos and models

Ranking = usefulness to O (open code or weights, runnable locally, ideas O lacks), not raw score.
"O has / lacks" refers to plan sections F, G, H and README "Computer use".

| # | Name | URL | License | What it does | Numbers (source, date) | O has | O lacks |
|---|---|---|---|---|---|---|---|
| 1 | Pointer harness (`Pointer-so/OSWorld`) | https://pointer.ai/blog/sota , https://github.com/Pointer-so/OSWorld | Apache-2.0 (repo badge) | Three-agent controller: feasibility gate using read-only probes, milestone planner (target states, not clicks), single executor with one unified toolset (GUI + code + DevTools + background jobs); Haiku compacts context; separate end-state verifier. No fine-tuned grounder. | OSWorld-Verified 83.64 (Opus 4.7), 81.45 (Sonnet 4.6), 100 steps — official xlsx, row date 2026-05-21; blog: raw Opus 78.0 → 83.6 with harness, 24/28 infeasible tasks caught, "superscore" 90.9 diagnostic only (https://pointer.ai/blog/sota, 2026-05-26) | judge, procedures, a11y-first | feasibility gate; milestone (state-goal) planning; reactive observe→act loop (O's OSWorld runner plans once per task); context compaction |
| 2 | Agent S / Agent S4 (Simular) | https://github.com/simular-ai/Agent-S , https://arxiv.org/abs/2610.28601 , PR https://github.com/simular-ai/Agent-S/pull/240 | Apache-2.0 | S3: worker + grounding model (UI-TARS-1.5-7B recommended) + reflection + behavior best-of-N. S4 (merged 2026-10-21): a Builder agent evolves a *Python shadow interface* the task agent imports; fitness = held-out validation tasks; population-based selection; OpenCode and Prime harnesses. | OSWorld-Verified: S3 + Opus 4.5 + GPT-5 bBoN N=10 = 72.58 (row 2025-12-11), S3 Opus 4.5 N=1 = 67.46 (official xlsx). S4 abstract: OSWorld V2 binary 21.3% with Qwen3.8-27B, 3.8x the fixed-interface baseline; ScienceBoard 41.4→58.6 (https://arxiv.org/abs/2610.28601, submitted 2026-10-20) | best-of-N on N displays; per-app *markdown* notes | executable per-app interfaces that are evolved and scored on held-out tasks; bBoN's comparative (not per-candidate) selection |
| 3 | VLAA-GUI (UCSC) | https://github.com/UCSC-VLAA/VLAA-GUI , https://arxiv.org/abs/2604.21375 | Apache-2.0 | Manager loop + two mandatory post-action modules: Completeness Verifier (rejects "done" unless success is visible) and Loop Breaker (escalates on repeated failures / recurring screen states); on-demand Search, Coding and Grounding agents; `vlaa_gui/memory/` procedural memory. | OSWorld-Verified 76.26 w/ Opus 4.5, 100 steps (official xlsx, row 2026-04-20); README states 77.5 (https://github.com/UCSC-VLAA/VLAA-GUI, undated) | per-step judge | loop detection on screen-hash recurrence; explicit completion-claim rejection; search agent for unfamiliar workflows |
| 4 | OpenAPA (Laiye) | https://github.com/laiye-ai/open-apa | MIT | Reflection→decision loop, hierarchical plan updated by reflection, CodeAgent + GroundingAgent, "delegate-execute-verify" (UI agent visually confirms code results), global runtime context shared by prompt injection, sliding-window screenshot pruning. | OSWorld-Verified 78.34 w/ gemini-3.1-pro + doubao grounding, 100 steps (official xlsx, row 2026-04-17; README: 282.8/361) | script steps (G1), VLM judge | code-then-GUI-verify as a first-class pattern inside cu; token-budget pruning of screenshot history |
| 5 | Intelligence Indeed agent + Indeed-UI grounder | https://github.com/intelligence-indeed/intelligence-indeed , https://github.com/intelligence-indeed/Indeed-UI | Apache-2.0 (both) | Agent: static skill library (name/description, progressive disclosure) + per-task dynamic skill built by read-only probing and doc/web lookup, then plan/execute. Indeed-UI-32B: GUI-Owl-1.5-based grounder with a two-step zoom-in eval. | OSWorld-Verified 90.19 w/ claude-opus-4-7, 100 steps (official xlsx, row 2026-07-25). ScreenSpot-Pro: Indeed-UI-32B 73.3, zoom-in 82.7; GroundCascade (Indeed-UI-32B + KV-Ground-8B) 85.1 (official grounding JSON, page Sep 10 2026) | interface notes read into prompts | skills with metadata + progressive disclosure; task-time probing to build a dynamic skill; zoom-in grounding; multi-model geometric-consistency cascade |
| 6 | HIPPO / muscle-mem (Lenovo) | https://github.com/wadang/muscle-mem-agent | not stated on page | Agent-S3 fork with task-level working memory (facts, UI state, completed subgoals, extracted values, TODO checkpoints, risks) with selective retrieval; guardrails: finish verification, plan-quality and feasibility checks. | OSWorld-Verified 74.48 w/ Opus 4.5, 100 steps, single rollout (official xlsx, row 2026-02-25) | — | structured working memory across steps; feasibility validation |
| 7 | Zoom-consistency routing / Zoom Consistency paper | https://github.com/omxyz/zoom-consistency-routing , https://arxiv.org/abs/2604.15376 | MIT | Two-step zoom pipeline; "zoom consistency" = distance between step-2 prediction and crop centre, a free, calibration-free confidence signal usable to route between a specialist and a generalist grounder. | ScreenSpot-Pro: KV-Ground-8B+Qwen3.5-27B routing 80.9 vs KV-Ground zoom-in 80.5, Qwen3.5-27B zoom-in 60.9 (repo README, 2026); correlation is weak (AUC 0.60 for KV-Ground-8B) and the routing gain is not significant (p=0.19) per the paper (submitted 2026-04-15) | single-shot VisionGrounder + Claude fallback | any confidence signal to decide when to fall back; zoom-in refinement |
| 8 | Interactive Reward Agent (IRA), VAGEN, SeekJudge, OpenComputer | https://arxiv.org/abs/2607.25904 , https://arxiv.org/abs/2602.00575 , https://arxiv.org/abs/2607.23263 , https://echo0715.github.io/OpenComputer | papers; OpenComputer license not stated | Verifiers that *probe environment state* (files, settings, app state) instead of grading screenshots: IRA proposes completion conditions then checks them with system/app/GUI tools; VAGEN moves from cheap surface evidence to expensive latent probes; SeekJudge is a 4-role trajectory judge (Condense/Ground/Seek/Analyze) with a step-level benchmark; OpenComputer generates app-specific state verifiers with inspection endpoints. | IRA: 86.9% on GUI-RewardBench (321 trajectories), 34.0% OSWorld as RL reward (arXiv, 2026-07-28). SeekJudge-9B: +9.5 F1 over rule-based on AgentRewardBench (arXiv, 2026-07-25). VAGEN/OpenComputer: no absolute numbers in text. | tree assertion → VLM yes/no | state-probing judge; trajectory-level judge; step-level judge data |
| 9 | Holo3-35B-A3B (H Company) | https://huggingface.co/Hcompany/Holo3-35B-A3B , https://hcompany.ai/holo3 | Apache-2.0 (35B); 122B not open | End-to-end CUA model on Qwen3.5-35B-A3B, 3B active; `vllm serve` one-liner; OpenAI-compatible vision endpoint. | OSWorld-Verified 82.56 / 78.15, 100 steps (official xlsx, rows 2026-04-20); model card 77.8; Holo3-122B 78.85 (https://hcompany.ai/holo3, 2026-03-31). Holo2-30B-A3B ScreenSpot-Pro 66.1 (agentic 75.2), OSWorld-G 76.1, but Holo2 is cc-by-nc (https://huggingface.co/Hcompany/Holo2-30B-A3B) | `vllmVisionModel` slot | a default recommended local model for grounding+judging; 3B-active MoE fits a single GPU |
| 10 | GUI-Owl-1.5-8B / KV-Ground-8B | https://huggingface.co/mPLUG/GUI-Owl-1.5-8B-Instruct , https://huggingface.co/vocaela/KV-Ground-8B-BaseGuiOwl1.5-0315 | GUI-Owl MIT; KV-Ground **CC BY-NC-SA 4.0** | 9B Qwen3-VL-based GUI model (MIT). KV-Ground re-trains it with MLLM-judged data cleaning + high-res synthesis + GRPO. | GUI-Owl-1.5 32B OSWorld-Verified 55.44, 50 steps (official xlsx, row 2026-03-27); 8B card: OSWorld-Verified 52.3. KV-Ground-8B ScreenSpot-Pro 73.2 (zoom-in 80.5), OSWorld-G 68.1 vs GUI-Owl base 70.5 / 78.0 / 64.7 (model card) | — | GUI-Owl-1.5-8B is the best MIT-licensed small grounder found; KV-Ground is non-commercial, use only for research runs |
| 11 | OpenCUA (xlang) | https://github.com/xlang-ai/OpenCUA , https://huggingface.co/xlangai/OpenCUA-72B | MIT | Open CUA foundation models (7B/32B/72B) + AgentNet data; pyautogui action output on smart-resized coordinates; vLLM support added 2026-01-17. | OSWorld-Verified 72B-preview 46.10, 100 steps (official xlsx, row 2025-10-01); card: ScreenSpot-Pro 60.8, OSWorld-G 59.2, ScreenSpot-V2 92.9 | — | a fully open e2e policy; mainly a reference for action format + smart-resize coordinate remap |
| 12 | GTA1 (Salesforce) | https://huggingface.co/Salesforce/GTA1-7B , https://arxiv.org/abs/2507.05791 | MIT | RL-trained grounder plus test-time scaling (sample several candidate plans, judge picks). | OSWorld-Verified GTA1 w/ GPT-5 63.41 (row 2025-10-03); ScreenSpot-Pro 7B 55.5 / 32B 63.6, OSWorld-G 60.1 / 65.2 (model card) | best-of-N | per-step candidate sampling + judge (cheaper than whole-run best-of-N) |
| 13 | SkillWeaver (OSU) | https://github.com/OSU-NLP-Group/SkillWeaver | MIT | Web agent explores, synthesizes skills as Python APIs, practices them, patches APIs that throw, blocks APIs that have not passed a run at eval time. | no numbers on repo page | script cache | skills as *code with tests*, repaired on failure |
| 14 | ComputerRL / AutoGLM-OS | https://arxiv.org/abs/2508.14040 , https://github.com/thudm/ComputerRL | code repo (license not read) | API-GUI paradigm (program calls + GUI), Entropulse RL/SFT alternation, thousands of parallel desktops. | OSWorld-Verified autoglm-os-9b-20250925 48.03, 50 steps; the only leaderboard rows with "Additional a11y tree used = Yes" are autoglm-os-9b 47.26/46.26 (official xlsx, rows 2025-09) | script steps | the API-first action mix; RL training is out of O's local scope |
| 15 | GUI-Actor (Microsoft) | https://github.com/microsoft/GUI-Actor | MIT | Coordinate-free grounding via attention head + separate grounding verifier that picks among candidates. | ScreenSpot-Pro 7B(2.5VL) 44.6 → 47.7 with verifier; ScreenSpot-v2 92.1 → 92.5 (repo README; official JSON lists 44.6) | — | a *verifier over candidate boxes* (the idea transfers to any grounder) |
| 16 | Jedi / OSWorld-G | https://osworld-grounding.github.io/ , https://huggingface.co/datasets/xlangai/Jedi | "open-sourced", license not stated | 4M synthetic grounding examples; OSWorld-G 564-sample benchmark incl. refusal category. | Jedi-7B w/ o3 OSWorld-Verified 51.0 (row 2025-07-28); Jedi-7B ScreenSpot-Pro 39.5, OSWorld-G 54.1 (project page) | — | an offline grounding regression set for O's grounder |
| 17 | UI-TARS (ByteDance) | https://github.com/bytedance/UI-TARS | code Apache-2.0; weights license not stated; only UI-TARS-1.5-7B public, UI-TARS-2 weights not released | Native GUI model; `Thought:/Action: click(start_box='(x,y)')` format, factor 1000. | UI-TARS-2-2509 OSWorld-Verified 53.10 (row 2025-10-14, closed); UI-TARS-1.5 ScreenSpot-Pro 61.6, 1.5-7B 49.6 (repo README) | — | nothing to adopt beyond the action grammar; S3's recommended grounder |
| 18 | Beagle / DarwinX (Salesforce) | https://github.com/SalesforceAIResearch/Beagle | Apache-2.0 | Harness-as-data evolution: an evolver agent edits the evolvee harness's source, candidates scored on the same tasks/budget, kept nodes pushed to `evolve/<sha>` branches, genealogy DB. | OSWorld 2.0: "Darwinx w/ Claude Opus 4.8" partial 75.97 / binary 44.44, v2.1, 450 steps (official JSON, updatedAt 2026-10-15) | append-only procedure lineage | whole-harness evolution; far too heavy for local-only, note only |
| 19 | Sai + SimuLang (Simular, closed) | https://www.simular.ai/articles/sai-tops-osworld-2-0 , https://www.simular.ai/articles/what-is-an-autonomous-computer | closed; trajectories on HF (`other`) | Neuro-symbolic: solved task compiled to a SimuLang script, a11y-tree targeting, vision fallback, self-heal by handing a failed step back to the model, independent judge models, adaptive summarization + constant prompt prefix, specialist models for localization/verification. | OSWorld 2.0 (v2.1): partial 79.38 / binary 41.67, 500 steps (official JSON, 2026-10-15); Simular page: 77.3 partial single run, $14.34/task, ~1.5x fewer model calls (page updated 2026-10-02); "15–217x lower cost per run, up to 90% fewer tokens" (Simular page dated 2026-10-27) | the same six design points (plan F) | prompt-prefix caching, adaptive summarization, specialist-model routing |
| 20 | Closed references: Claude, OpenAI, Meta, Fara, Perplexity, Manus, Fellou | official xlsx; https://huggingface.co/microsoft/Fara-7B | — | For calibration only. | OSWorld-Verified: claude-fable-5[1m] 85.96 (row 2026-08-01), claude-opus-5[1m] 83.39, Muse Spark 1.1 80.67 (row 2026-07-09), claude-sonnet-4-6 72.11, OpenAI computer-use-preview 31.4 (row 2025-07-28). Fara-7B (MIT, Qwen2.5-VL-7B): WebVoyager 73.5, no OSWorld (model card, 2025-11-24). Perplexity Computer, Manus, Fellou: no OSWorld-Verified row in the official sheet. | — | — |

Older grounders for reference (official ScreenSpot-Pro JSON): Aguvis-7B 22.9, OS-Atlas-7B 18.9, AriaUI 11.3,
ShowUI-2B 7.7, UGround-v1-72B 34.5, Qwen2.5-VL-72B 53.3, Claude (Computer Use) 17.1, GPT-4o 0.8.
Magma and ScreenAgent do not appear on either official board and were not pursued.

### What the boards say about the direction (Nov 2026)

- Every top-20 OSWorld-Verified row is screenshot-only (`Additional a11y tree used = No`); the frontier gain
  is coming from *harness* design around a general model (feasibility gates, milestone plans, verifiers,
  loop breakers, working memory, code+GUI mixing), not from new grounders. Pointer's blog quantifies
  this: raw Opus 4.7 78.0 → 83.6 with the harness, cost roughly flat (https://pointer.ai/blog/sota, 2026-05-26).
- OSWorld 2.0's authors say failures are no longer basic GUI control: agents lose constraints, miss
  mid-task information, guess instead of asking, skip verification, and fail on hidden state
  (https://arxiv.org/abs/2606.29537, 2026-06-28). That is a judge/memory/verification problem.
- Grounding has moved to *two-step zoom-in + consistency gating*; the top of ScreenSpot-Pro is a
  training-free cascade (85.1) over 73-point single-shot models (official ScreenSpot-Pro JSON, Sep 10 2026).
- "Macro" critique in practice: Simular's own pages describe deterministic replay only with a model
  on call for self-heal and a judge in the loop; the CLI-agents-on-OSWorld post
  (https://bowenbryanwang.github.io/blog/cli-agents-gui-benchmark, 2026-05-11) reports Codex 54.0 /
  Claude Code 49.9 with no GUI policy at all, i.e. a large share of GUI tasks fall to code — so a
  pure click-macro replay layer is the wrong primitive; code+GUI with state verification is.
- Agent S4 replaces prose "usage knowledge" with an *executable, evolved* interface scored on held-out
  tasks. O's `interfaces/<app>.md` is the prose version of this.

## 2. Feature parcels for O

All parcels keep plan F's rules: frozen pydantic with `extra=forbid`, append-only procedures, no pixel
actions on the host display, every step in the action log, fixed graph topology (changes are inside
nodes or via the existing heal edge). Model calls go to the local vLLM first and `claude -p` second,
exactly as `ModelClient`, `Grounder` and `Judge` already do. Local-only = works with vLLM + Claude Code
login and no cloud key.

### P1 — Reactive milestone loop (replaces plan-once)
- From: Pointer harness (milestone planner + single executor), Agent S3 worker loop, every OSWorld top entry.
- Why: `engine/eval/osworld/run_o.py` and `cu/graph.py:plan` produce one OScript up front from a tree
  summary; every leaderboard agent observes after each action. Long tasks cannot be scripted blind.
- Integration: `o_engine/cu/planner.py` — add `plan_milestones(goal, app, notes) -> list[Milestone{goal,
  done_when: Target|question}]` and `next_steps(milestone, tree_summary, memory) -> OScript` (bounded,
  ≤ `cuStepsPerTurn`, default 5). `o_engine/cu/graph.py:execute` loops milestone → next_steps → run →
  judge(done_when) inside the execute node (no new edges); `cu/schema.py` adds `Milestone` model.
  `eval/osworld/run_o.py:OAgent.predict` becomes one `next_steps` call per observation. Settings:
  `cuStepsPerTurn`, `cuMaxTurns`.
- Effort: L. Local-only: yes.
- Acceptance check: with `FakeBackend` whose tree changes after each action, a 3-milestone goal completes
  only when each `done_when` passes; the action log shows one `plan` entry per turn; the replay path
  (`computer_replay`) still makes zero model calls on a stored procedure.

### P2 — Feasibility gate with read-only probes
- From: Pointer (24/28 impossible tasks caught), HIPPO feasibility validation, OSWorld's infeasible tasks.
- Integration: new `o_engine/cu/feasibility.py`: `Probe` ops limited to `tree`, `screenshot`, `ls`,
  `read_file`, `which` (all read-only, executed through `Sandbox`/`CUBackend`, never `click`/`type`);
  `FeasibilityGate.check(goal, app) -> {feasible: bool, reason}` via `ModelClient.complete_json`.
  Called in `cu/graph.py:context`; infeasible → `report` with `ok=False, reason`, logged kind `note`.
  Route `POST /computer/run` returns `infeasible` in the job result; `run_o.py` emits OSWorld's `FAIL`.
- Effort: S. Local-only: yes.
- Acceptance check: a goal naming an app the `FakeBackend` does not expose is reported infeasible with
  no `click`/`type` log entries; a feasible goal proceeds to `plan`.

### P3 — Zoom-in grounding with a consistency gate
- From: Indeed-UI zoom-in (73.3→82.7), KV-Ground zoom-in (73.2→80.5), GroundCascade (85.1), Zoom
  Consistency paper (signal definition), GTA1/GUI-Actor (candidate + verifier). Numbers and URLs: table rows 5, 7, 10, 12, 15.
- Integration: `o_engine/cu/ground.py` — `ZoomGrounder(inner: GrounderBackend, scale=0.5)`: step 1 on
  the full screenshot → crop `scale` around the centre → step 2 on the crop → remap; `consistency =
  dist(step2_center, crop_center)/crop_diag`; result carries `confidence`. `Grounder.ground` uses the
  gate: `confidence < cuGroundConfidence` → cached locator stays unwritten and the fallback grounder
  (Claude Code) runs. Optional `MultiCropVote(k)` (k shifted crops, k-means-free: pick the mode of
  rounded centres). `LocatorCache.put` stores `confidence`. Setting `cuGroundConfidence` (default 0.3).
- Effort: M. Local-only: yes (any OpenAI-vision model on vLLM; MIT GUI-Owl-1.5-8B or Apache Holo3-35B-A3B
  are the licence-clean picks; KV-Ground is CC BY-NC-SA, research only).
- Acceptance check: a stub grounder that returns a deliberately offset box in step 1 and the true box
  in step 2 yields the true box after remap; a stub whose step 2 lands far from the crop centre produces
  `confidence` below the gate and triggers the fallback; an offline run over a ScreenSpot-Pro slice
  (`likaixin/ScreenSpot-Pro`) prints hit rate with and without zoom (number is whatever it prints).

### P4 — State-probing judge
- From: IRA (propose conditions → check with system/app tools, 86.9% on GUI-RewardBench), VAGEN
  (surface→latent, cheap→expensive), OpenComputer app verifiers, Pointer end-state verifier. Numbers and URLs: table rows 1, 8.
- Integration: `o_engine/cu/schema.py` — new step `{"op":"state_assert","checks":[{"kind":"file_exists"|
  "file_contains"|"file_sha_changed"|"tree"|"shell_true","arg":…}]}` (closed enum). `o_engine/cu/judge.py`
  — `StateJudge.verify(checks, sandbox, backend) -> JudgeVerdict`; order in `cu/graph.py:judge`:
  a11y assert → state checks → VLM screenshot question (only when the first two cannot decide).
  `Planner.plan` prompt asks for `state_assert` after every save/export/submit step. Shell probes run
  through `Sandbox.run` with a read-only allow-list (`test -f`, `sha256sum`, `stat`, `grep -c`).
- Effort: M. Local-only: yes.
- Acceptance check: a "save as CSV" script with a `state_assert file_exists` passes only when the
  `FakeBackend`'s sandbox has the file; removing the file flips the verdict and starts the heal loop;
  no VLM call is logged when the state check decides.

### P5 — Loop breaker and completion-claim rejection
- From: VLAA-GUI (Loop Breaker on recurring screen states; Completeness Verifier), HIPPO finish check.
- Integration: `o_engine/cu/executor.py:ScriptRunner` keeps a ring of the last 8 `screen_sha` + step
  digests; ≥3 identical (screen, step) pairs → `_StepFailure("loop")`. `cu/graph.py:route_after_judge`
  treats `loop` as a heal reason that forces `replan_step` with the loop evidence in the prompt and, on
  the last heal, reports `ok=False`. Goal-level `screenshot_assert` is replaced by P4 state checks when
  the plan declares any; the VLM "is the goal done?" question is asked only as a tie-break.
- Effort: S. Local-only: yes.
- Acceptance check: a `FakeBackend` that never changes the tree makes a 10-step script stop with
  `loop` after 3 repeats, with `cuMaxHeals` respected and v1 of the procedure untouched.

### P6 — Task working memory + compaction
- From: HIPPO (facts/state/todos/risks), Sai (adaptive summarization, stable prompt prefix), Pointer
  (background compaction), OSWorld 2.0 failure taxonomy (constraints lost, mid-task info missed).
- Integration: new `o_engine/cu/memory.py`: `WorkingMemory{facts, state, todos, risks}` (pydantic,
  bounded lists, 40 items each); after every judged step `extract(step_record, tree_summary)` via the
  local model appends; `render()` goes into `build_plan_prompt`/`build_replan_prompt` as a fixed-position
  block *after* the static persona so the vLLM prefix cache holds. Stored in the job's `CUState` and in
  the action log (kind `note`, one per update). `summarize_tree` output is capped by a token budget
  (`TokenFilter`) instead of line count.
- Effort: M. Local-only: yes.
- Acceptance check: a goal that states a constraint ("do not overwrite report.ods") survives into the
  replan prompt after 20 steps; memory never exceeds its caps; the prompt prefix bytes before the memory
  block are identical across turns.

### P7 — Executable per-app interfaces, evolved and scored
- From: Agent S4 shadow interface (Builder edits a Python interface; fitness on held-out tasks),
  SkillWeaver (skills as APIs with tests and repair), Intelligence Indeed static skills (metadata +
  progressive disclosure).
- Integration: `o_engine/cu/interfaces.py` grows a second artefact beside the markdown:
  `interfaces/<app>/v<N>/api.py` + `api.json` (name, description, signature, `passed_on: [procedure_id]`).
  Source: after `store`, a bounded "builder" prompt turns a passing procedure into a function over
  `CUBackend` (find/invoke/set_value only, no pixels) and a smoke script; `runners.NativeRunner` runs the
  smoke script on the isolated display; pass → append v<N+1> (append-only, lineage like procedures).
  `Planner` prompt lists `api.json` entries (progressive disclosure: names first, bodies on request);
  OScript gains `{"op":"call","fn":"calc.set_cell","args":{…}}` dispatched by `executor._dispatch`.
  Fitness = replay pass-rate of the newest version over the app's stored procedures (`computer_replay`),
  shown by `GET /computer/interfaces`.
- Effort: L. Local-only: yes.
- Acceptance check: after two stored procedures for `FakeBackend`'s app, `interfaces/<app>/v1/api.py`
  exists, its smoke script passes under the runner, a plan for a third goal uses `call`, and deleting
  nothing is possible (v1 remains when v2 is written).

### P8 — Code-first actions inside the cu graph
- From: ComputerRL API-GUI, OpenAPA delegate-execute-verify, Pointer unified toolset, CLI-agents-on-OSWorld
  (Codex 54.0 / Claude Code 49.9 with no GUI policy; https://bowenbryanwang.github.io/blog/cli-agents-gui-benchmark, 2026-05-11).
- Integration: `o_engine/cu/schema.py` — step `{"op":"script","script": ScriptPayload}` (the G1 model,
  reused, exactly-one-of rule kept); `cu/executor.py:_dispatch` sends it to `runners.Runner` with the
  isolated display's `DISPLAY` and the job sandbox; the planner prompt states "prefer `script` for
  file/format/bulk work, GUI for everything the app only exposes in its UI, and follow a `script` with a
  `state_assert` or tree `assert`". High-risk scripts follow G2 containment unchanged.
- Effort: M. Local-only: yes.
- Acceptance check: a goal "convert all .csv in dir to .ods" plans a `script` step plus a `state_assert`;
  the script runs through `NativeRunner`; a failing script is healed by one replacement step and the
  action log holds `tool`/`result` entries for it.

### P9 — Comparative best-of-N and per-step candidate judging
- From: Agent S3 bBoN (compare behaviour narratives of N runs; 67.46 → 72.58 at N=10), GTA1 test-time
  scaling, SeekJudge's step-level judge. Numbers and URLs: table rows 2, 8, 12.
- Integration: `o_engine/cu/planner.py:BestOfN` — build a "behaviour narrative" per candidate from
  `run_records` (step text, resolved_by, judge verdicts, screen hashes) and ask the local model *once*
  to rank all N (comparative), instead of scoring each; tie → fewest steps. Per-step variant: in
  `graph.py:ground`, sample `cuStepCandidates` plans for the next step only and let the a11y/state judge
  pick (no extra displays needed).
- Effort: S (comparative) / M (per-step). Local-only: yes.
- Acceptance check: with three stubbed candidates where only one passes its `state_assert`, the
  comparative ranker returns that one; the ranker is called once per best-of-N run.

### P10 — Offline grounding/judge regression from public trajectories
- From: `xlangai/ubuntu_osworld_verified_trajs` (MIT, screenshots + actions + results),
  `likaixin/ScreenSpot-Pro`, Jedi's OSWorld-G, `simular-ai/sai-osworld-v2-benchmark-runs` (licence `other`).
- Integration: `engine/eval/osworld/offline.py`: feeds recorded screenshots + instructions to O's
  `Grounder` and `Judge` with no VM, prints grounding hit rate (box contains recorded click) and judge
  agreement with the recorded task result; `summarize.py` gains an `--offline` table. Numbers are only
  what the run prints.
- Effort: S. Local-only: yes (datasets are downloads; the Sai set is non-open and optional).
- Acceptance check: the script runs on a 20-episode slice and writes a table with per-app rows and the
  dataset commit hash; a unit test runs it on two bundled synthetic episodes.

Not proposed: RL training (ARPO, ComputerRL, GUI-R1/UI-R1 families) and harness evolution at Beagle
scale — both need fleets of VMs and are outside "local-only". A Holo3/GUI-Owl default for
`vllmVisionModel` is a docs/install change, not a parcel.

## 3. What O can claim today vs what needs a run

Today O can state, from its own code, the six design points in plan F: accessibility-first
targeting with a cached locator and a local-VLM/Claude fallback, API-level actions that never move the
host mouse, judged steps (tree assertion then VLM), append-only procedures with replay that makes no
model calls and bounded self-heal, best-of-N across isolated displays, and per-app interface notes. It
cannot state any success rate: there is no OSWorld-Verified or OSWorld 2.0 number for O, and none
should be written until `engine/eval/osworld/run_o.py` is run on a VM and `summarize.py` prints the
table; the same holds for grounding accuracy, which needs the P10 offline run before anything is said
about it. What the leaderboards show is that O's current architecture is closest to Simular's closed
Sai in design but structurally behind every top open harness in three ways: it plans once per task
instead of observing after each action, its judge reads screenshots and the tree but never probes
files or app state, and its per-app knowledge is prose rather than executable, scored interfaces.
P1, P4 and P7 close those gaps; P2, P5, P6 are cheap and address the failure modes OSWorld 2.0's
authors name.

## Sources

- OSWorld-Verified results sheet: http://osworld-v1.xlang.ai/static/data/osworld_verified_results.xlsx (via http://osworld-v1.xlang.ai/, fetched 2026-11-04)
- OSWorld 2.0 official results JSON: https://osworld-v2.xlang.ai/static/data/leaderboard/official-results.json (updatedAt 2026-10-15); site https://osworld-v2.xlang.ai/ ; paper https://arxiv.org/abs/2606.29537
- ScreenSpot-Pro official leaderboard JSON: https://gui-agent.github.io/grounding-leaderboard/results/screenspot_pro.json (page https://gui-agent.github.io/grounding-leaderboard/, Sep 10 2026); dataset https://huggingface.co/datasets/likaixin/ScreenSpot-Pro
- Pointer: https://pointer.ai/blog/sota ; https://github.com/Pointer-so/OSWorld
- Agent S / S4: https://github.com/simular-ai/Agent-S ; https://arxiv.org/abs/2610.28601 ; https://github.com/simular-ai/Agent-S/pull/240
- Simular Sai / SimuLang: https://www.simular.ai/articles/sai-tops-osworld-2-0 ; https://www.simular.ai/articles/what-is-an-autonomous-computer ; https://www.sai.work/blog/introducing-sai-api ; https://huggingface.co/datasets/simular-ai/sai-osworld-v2-benchmark-runs
- VLAA-GUI: https://github.com/UCSC-VLAA/VLAA-GUI ; https://arxiv.org/abs/2604.21375
- OpenAPA: https://github.com/laiye-ai/open-apa
- Intelligence Indeed: https://github.com/intelligence-indeed/intelligence-indeed ; https://github.com/intelligence-indeed/Indeed-UI
- HIPPO / muscle-mem: https://github.com/wadang/muscle-mem-agent
- Zoom consistency: https://github.com/omxyz/zoom-consistency-routing ; https://arxiv.org/abs/2604.15376
- KV-Ground-8B: https://huggingface.co/vocaela/KV-Ground-8B-BaseGuiOwl1.5-0315 ; GUI-Owl-1.5-8B: https://huggingface.co/mPLUG/GUI-Owl-1.5-8B-Instruct
- Holo3: https://huggingface.co/Hcompany/Holo3-35B-A3B ; https://hcompany.ai/holo3 ; Holo2: https://huggingface.co/Hcompany/Holo2-30B-A3B
- OpenCUA: https://github.com/xlang-ai/OpenCUA ; https://huggingface.co/xlangai/OpenCUA-72B
- GTA1: https://huggingface.co/Salesforce/GTA1-7B ; https://arxiv.org/abs/2507.05791
- Jedi / OSWorld-G: https://osworld-grounding.github.io/
- GUI-Actor: https://github.com/microsoft/GUI-Actor
- UI-TARS: https://github.com/bytedance/UI-TARS
- Fara-7B: https://huggingface.co/microsoft/Fara-7B
- SkillWeaver: https://github.com/OSU-NLP-Group/SkillWeaver
- ComputerRL: https://arxiv.org/abs/2508.14040 ; ARPO: https://arxiv.org/abs/2505.16282
- Verifiers: IRA https://arxiv.org/abs/2607.25904 ; VAGEN https://arxiv.org/abs/2602.00575 ; SeekJudge https://arxiv.org/abs/2607.23263 ; OpenComputer https://echo0715.github.io/OpenComputer
- Beagle / DarwinX: https://github.com/SalesforceAIResearch/Beagle
- CLI agents on OSWorld: https://bowenbryanwang.github.io/blog/cli-agents-gui-benchmark
- OSWorld-Verified trajectories: https://huggingface.co/datasets/xlangai/ubuntu_osworld_verified_trajs
