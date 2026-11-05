# OSWorld runner for O

`run_o.py` plugs O's computer-use stack (planner, accessibility-tree grounding with vision
fallback, judged steps, bounded self-heal) into OSWorld's harness. `summarize.py` turns the
result directories into a markdown table.

**The score is whatever the run prints.** No number is claimed in this repository; the
only OSWorld figure for O is the one produced by running the commands below on your VM.

## What runs where

- `OAgent.predict(instruction, obs) -> (response, actions)` is OSWorld's agent interface.
  `obs["screenshot"]` is PNG bytes; `obs["accessibility_tree"]` is the VM's AT-SPI tree (XML)
  when `--observation_type` includes `a11y_tree`.
- The agent runs the engine's reactive milestone loop: before anything else the feasibility gate
  (`FeasibilityGate`, read-only probes of the observed tree + one model question; `--no_feasibility`
  turns it off) can stop the task with `FAIL` and the response `infeasible: <reason>`. Then
  `Planner.plan_milestones` runs once per task and `Planner.next_steps` once per observation,
  emitting at most `--steps_per_turn` steps per turn for at most `--max_turns` turns. After a turn
  the current milestone's `done_when` is checked: a `Target` on the VM's tree, a question through
  the `Judge`; done → next milestone, all done → `DONE`, turns exhausted → `FAIL`.
- Every target is resolved on the tree first and on the screenshot (`Grounder`, zoom-in grounding
  unless `--no_zoom`, gate `--ground_confidence`) second; `assert` steps are checked against the
  tree and `screenshot_assert` steps with the `Judge`; a failed step is healed with
  `Planner.replan_step` at most `--max_heals` times before `FAIL`.
- Code-first ops map onto OSWorld's `pyautogui` action space, which runs any Python string inside
  the VM: `script` (`python`: the code itself from its `target_directory`; `bash`: through
  `subprocess.run(..., shell=True, executable="/bin/bash", timeout=...)`; other languages fail the
  step), `state_assert` (`tree` checks are decided on the observed tree; file and shell checks become
  one VM-side Python probe built only from `cu/probes.py`'s allow-list, `test -f` / `grep -c` /
  `sha256sum` / ..., that prints a JSON verdict into OSWorld's step log — the observation channel
  carries no stdout, so the probe cannot steer the agent; `done_when` and `env.evaluate()` stay the
  verdicts), `call` (skipped with a logged note: the VM has no interface store).
- Actions are code strings executed **inside the OSWorld VM** by OSWorld's own `env.step`. O's host
  backends (`o_engine.cu.backends.*`) are never imported here and the host display is never touched.

## Setup

Clone OSWorld and install it per its README (quoted from
<https://github.com/xlang-ai/OSWorld>, read 2026-11-04):

```bash
git clone https://github.com/xlang-ai/OSWorld
cd OSWorld
# Optional conda environment (Python >= 3.10 required)
# conda create -n osworld python=3.10
# conda activate osworld
pip install -r requirements.txt
```

Pick a VM provider as OSWorld's README describes (VMware with `vmrun -T ws list` working,
or Docker on a KVM host: `egrep -c '(vmx|svm)' /proc/cpuinfo` > 0). OSWorld's own smoke test:

```bash
python quickstart.py
python quickstart.py --provider_name vmware --path_to_vm "path/to/your/vm.vmx"
```

Start O's local models (the same ones the engine uses): a vLLM server at `O_VLLM_URL` serving
`O_VLLM_MODEL` for planning and `O_VLLM_VISION_MODEL` (a UI-grounding VLM) for grounding and
judging; `claude` on PATH is the fallback for both.

## Run

OSWorld's upstream invocation (its README; the `run.py` section is marked deprecated there, and
`scripts/python/run_multienv.py` is shown for parallel runs; both hard-wire their `PromptAgent`):

```bash
python run.py \
    --provider_name vmware \
    --path_to_vm Ubuntu/Ubuntu.vmx \
    --headless \
    --observation_type screenshot \
    --model gpt-4o \
    --sleep_after_execution 3 \
    --max_steps 15 \
    --result_dir ./results \
    --client_password password
```

OSWorld's `run.py` has no agent plug-in flag, so `run_o.py` is the drop-in runner: same
`DesktopEnv`, same `lib_run_single.run_single_example`, same result-dir layout, same flag
names — only the agent is O's. Run it from inside the OSWorld checkout (or pass `--osworld`):

```bash
cd OSWorld
export O_VLLM_URL=http://127.0.0.1:8000/v1
export O_VLLM_MODEL=meta-llama/Meta-Llama-3-8B-Instruct
export O_VLLM_VISION_MODEL=<your UI-grounding VLM>
python /path/to/O/engine/eval/osworld/run_o.py \
    --osworld . \
    --provider_name vmware \
    --path_to_vm Ubuntu/Ubuntu.vmx \
    --headless \
    --observation_type screenshot_a11y_tree \
    --model o \
    --sleep_after_execution 3 \
    --max_steps 15 \
    --result_dir ./results \
    --client_password password
```

O flags: `--steps_per_turn` (default 5, env `O_CU_STEPS_PER_TURN`), `--max_turns` (default 20,
`O_CU_MAX_TURNS`), `--ground_confidence` (default 0.3, `O_CU_GROUND_CONFIDENCE`), `--max_heals`
(default 3, `O_CU_MAX_HEALS`), `--no_feasibility`, `--no_zoom`. The unit tests for the step → VM
mapping and the milestone loop run with a stubbed planner and no VM: `pytest tests/test_osworld_run_o.py`.

`--domain <name>` restricts to one domain (e.g. `libreoffice_calc`);
`--test_all_meta_path evaluation_examples/test_all.json` is the full set (OSWorld-Verified
ships its own meta file; pass it here). Finished examples (those with a `result.txt`) are
skipped on rerun, exactly as OSWorld does.

## Results

Per example OSWorld writes `results/pyautogui/<observation_type>/o/<domain>/<example_id>/`
with `result.txt` (the `env.evaluate()` score), `traj.jsonl`, step screenshots and a
recording. Summarise:

```bash
python /path/to/O/engine/eval/osworld/summarize.py --result_dir ./results --model o
```

which prints a markdown table (finished / success / success rate per domain, plus the
unfinished count; unfinished is never counted as failure). OSWorld's own report works too:

```bash
python show_result.py --action_space pyautogui --model o --observation_type screenshot_a11y_tree --result_dir ./results
python show_result.py --detailed
```

Again: the number in that table is the result. Nothing here states one.

## Offline grounding/judge regression (`offline.py`, no VM)

`offline.py` replays recorded trajectories through O's `Grounder` and `Judge` without a VM. Per
step it asks the grounder for the recorded target on the recorded screenshot and counts a
**hit** when the returned box contains the recorded click; per episode it asks the judge
"has this task been completed on screen" on the final screenshot and counts **agree** when the
verdict equals the recorded task result. It prints a markdown table with one row per app plus
an `all` row, the episodes directory, the dataset hash, and the backend names.

**The numbers are only what the run prints.** This README, the code and the tests state no
hit rate and no agreement figure; the bundled fixtures are synthetic and exist to prove the
plumbing, not to measure anything.

### Episode layout

```
<episodes>/
  <episode>/
    meta.json
    <screenshots referenced by meta.json>
```

`meta.json`:

```json
{
  "instruction": "Open the File menu and click Save",
  "app": "gedit",
  "result": true,
  "steps": [
    {"screenshot": "step0.png", "click": [x, y], "target": "File menu"}
  ],
  "final_screenshot": "step1.png"
}
```

- `instruction` (str), `app` (str, becomes the table row), `result` (bool, the recorded task
  outcome), `steps` (non-empty list) are required.
- Each step needs `screenshot` (path relative to the episode directory) and `click` `[x, y]`
  in that screenshot's pixels. `target` is the text handed to the grounder; when absent, the
  instruction is used.
- `final_screenshot` is optional and defaults to the last step's screenshot; the judge reads it.
- Episodes are read in directory-name order; `--limit N` keeps the first N.

### Run

```bash
cd engine
export O_VLLM_URL=http://127.0.0.1:8000/v1
export O_VLLM_VISION_MODEL=<your UI-grounding VLM>   # falls back to O_VLLM_MODEL
export O_CLAUDE_PATH=claude                            # fallback backend for both
python -m eval.osworld.offline --episodes /path/to/episodes --limit N --out offline.md --details offline.jsonl
```

The default backends are the ones `o_engine.server` builds from `EngineSettings.from_env()`:
`Grounder(VisionGrounder, ClaudeCodeGrounder)` and `Judge(VLMJudge, ClaudeCodeJudge)`, local
vLLM first and `claude -p` second. `--grounder stub --judge stub` swaps in the test backends
(the stub grounder returns a fixed top-left box for every target, the stub judge answers yes to
everything); they exist for the unit test and measure nothing. A grounder that raises or returns
no box counts as a miss, never as a crash; the `--details` JSON-lines log records the error.

### Zoom-in grounding (`--zoom`)

`--zoom {off,on,both}` (default `off`) switches on O's zoom-in grounding
(`o_engine.cu.ground.ZoomGrounder`): ground on the full screenshot, crop a half-size window
(`scale=0.5`) around that answer, ground again on the crop, remap, and report
`1 - distance/half-diagonal` as the confidence.

- `on` wraps every grounding backend in `ZoomGrounder(inner, scale=0.5)`; with the `real`
  grounder the `Grounder` gate (`min_confidence`) is kept, so a zoom answer that lands far from
  its crop centre is discarded and the fallback runs, exactly as in the engine. Two grounder
  calls per step instead of one; the `--details` log records which setting each line came from.
- `both` runs the whole episode set twice, zoom off then on, and prints one table per setting
  under `## zoom: off` and `## zoom: on`, each with a leading `zoom` column, so the two hit rates
  sit side by side. `parse_table` keys those rows `<zoom>:<app>` (e.g. `on:all`).
- The `stub` grounder works under zoom too (it answers the same fixed box on the crop); the
  counts it produces prove only that the wiring runs.

```bash
python -m eval.osworld.offline --episodes /path/to/episodes --zoom both --out offline-zoom.md
```

As everywhere in this directory: whichever setting wins is only what that run prints. No hit
rate for zoom off or zoom on is stated here, and the fixtures measure nothing.

The `dataset hash` line is `git:<HEAD>` when the episodes directory is inside a git checkout,
otherwise `sha256:` over the sorted `relative path:size` lines of every file under it, so a
table can be matched back to the exact slice it was computed on.

Include the table in the OSWorld summary:

```bash
python eval/osworld/summarize.py --result_dir ./results --model o --offline offline.md
```

`summarize.py --offline` appends the table verbatim under its own heading; with no OSWorld
results it prints the offline table alone.

### Public datasets and conversion

None of these is in O's layout as published; a converter writes one `meta.json` per episode.
The conversion is a small script per dataset (map each recorded action's coordinates to
`click`, each pre-action screenshot to `screenshot`, the recorded evaluation outcome to
`result`, and the task's domain or app to `app`). Keep the original files next to the
converted ones so the dataset hash covers both.

- `xlangai/ubuntu_osworld_verified_trajs` (Hugging Face, MIT): OSWorld-Verified trajectories
  with screenshots, actions and results on Ubuntu. Clicks come from the recorded `pyautogui`
  actions; `result` from the recorded evaluation score; `app` from the task domain.
- `likaixin/ScreenSpot-Pro` (Hugging Face): grounding-only pairs (screenshot, instruction,
  ground-truth box). Use the box centre as `click`; there is no task result, so set `result`
  to `true` and read only the grounding columns of the table for that slice.
- Jedi's OSWorld-G: grounding set in the same spirit as ScreenSpot-Pro; convert the same way.
- `simular-ai/sai-osworld-v2-benchmark-runs` (licence `other`, not open): optional; convert
  only if its terms allow your use, and never commit it to this repository.

The bundled synthetic episodes under `tests/fixtures/offline/` are two tiny hand-made episodes
used by `tests/test_osworld_offline.py`; they are not a dataset.
