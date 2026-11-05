#!/usr/bin/env python3
"""OSWorld runner for O's computer-use stack.

``OAgent`` implements OSWorld's agent interface:

    agent.reset(runtime_logger=None, vm_ip=None)
    response, actions = agent.predict(instruction, obs)

``obs`` is OSWorld's observation dict: ``obs["screenshot"]`` (PNG bytes) and, when the
run asks for it, ``obs["accessibility_tree"]`` (the VM's AT-SPI tree as XML). The agent
follows the engine's reactive milestone loop (P1): one ``Planner.plan_milestones`` per task,
then one ``Planner.next_steps`` per observation (at most ``--steps_per_turn`` steps, at most
``--max_turns`` turns), each milestone's ``done_when`` checked on the tree (a ``Target``) or by
the ``Judge`` (a question). Before planning, the feasibility gate (P2) asks one model question
over read-only probes of the observed tree; an infeasible verdict emits OSWorld's ``FAIL``.
Steps are grounded on the tree (vision grounding on the screenshot as fallback) and emitted as
**action strings that run only inside the OSWorld VM** (``pyautogui`` calls, and plain Python
for ``script`` steps and ``state_assert`` probes). Nothing here touches the host display; O's
host backends are never imported.

Mapping of the code-first ops (P4/P7/P8) onto OSWorld's ``pyautogui`` action space, which runs
any Python string inside the VM:

* ``script`` → ``python``: the code itself, run from its ``target_directory``; ``bash``: the
  code through ``subprocess.run(..., shell=True, executable="/bin/bash", timeout=...)``;
  ``javascript``/``powershell``: a step failure (healed like any other).
* ``state_assert`` → ``tree`` checks are decided here on the observed tree; file and shell
  checks become ONE VM-side Python probe built only from ``cu.probes.PROBE_ALLOW_LIST``
  commands (``test -f``, ``grep -c``, ``sha256sum``, ...). The probe prints a JSON verdict into
  OSWorld's step log; the observation channel carries no stdout, so its result cannot steer the
  agent — the milestone's ``done_when`` and ``env.evaluate()`` remain the verdicts.
* ``call`` → skipped with a logged note: the VM has no interface store.

``main()`` mirrors OSWorld's ``run.py`` flags (OSWorld's own ``run.py`` has no agent plug-in
flag, so this file is the drop-in runner: same ``DesktopEnv``, same ``lib_run_single
.run_single_example``, same result-dir layout, only the agent differs). Run it from inside
an OSWorld checkout, or pass ``--osworld``.

The score is whatever the run prints and writes to ``result.txt``; this file claims no number.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ENGINE = _HERE.parent.parent
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from o_engine.cu.graph import summarize_tree  # noqa: E402
from o_engine.cu.probes import PROBE_REFUSED, is_allowed_probe  # noqa: E402

LOG = logging.getLogger("o.osworld")
WAIT_ACTION = "WAIT"
DONE_ACTION = "DONE"
FAIL_ACTION = "FAIL"
TREE_LINES = 400
DEFAULT_STEPS_PER_TURN = 5
DEFAULT_MAX_TURNS = 20
DEFAULT_GROUND_CONFIDENCE = 0.3
PROBE_TIMEOUT_S = 10
A11Y_NS = {
    "component": "https://accessibility.windows.example.org/ns/component",
    "attr": "https://accessibility.windows.example.org/ns/attributes",
    "state": "https://accessibility.windows.example.org/ns/state",
}


# ---------------------------------------------------------------- a11y tree → nodes

def _attr(el: ET.Element, local: str) -> str | None:
    for key, val in el.attrib.items():
        if key == local or key.endswith("}" + local):
            return val
    return None


def _pair(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    try:
        a, b = text.strip("() ").split(",")
        return int(float(a)), int(float(b))
    except Exception:
        return None


def parse_a11y_xml(xml_text: str) -> list[dict]:
    """OSWorld's AT-SPI XML → the ``Node``-shaped dicts ``summarize_tree`` and grounding use."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    counter = [0]

    def convert(el: ET.Element) -> dict:
        counter[0] += 1
        tag = el.tag.split("}", 1)[-1]
        pos = _pair(_attr(el, "screencoord"))
        size = _pair(_attr(el, "size"))
        rect = {"x": pos[0], "y": pos[1], "w": size[0], "h": size[1]} if pos and size else None
        states = [k.split("}", 1)[-1] for k, v in el.attrib.items() if A11Y_NS["state"] in k and v == "true"]
        return {
            "id": f"a{counter[0]}",
            "role": tag.replace("_", " "),
            "name": _attr(el, "name") or "",
            "text": (el.text or "").strip() or None,
            "rect": rect,
            "states": states,
            "children": [convert(c) for c in list(el)],
        }

    return [convert(root)]


def _glob_match(pattern: str | None, value: str | None) -> bool:
    import fnmatch

    if pattern is None:
        return True
    return fnmatch.fnmatchcase((value or "").lower(), pattern.lower())


def find_in_tree(nodes: list[dict], target: Any) -> dict | None:
    """Accessibility-first resolution mirroring ``CUBackend.find``: role equality, name glob, text substring, index."""
    role = getattr(target, "role", None)
    name = getattr(target, "name", None)
    text = getattr(target, "text", None)
    index = int(getattr(target, "index", 0) or 0)
    matches: list[dict] = []

    def walk(node: dict) -> None:
        ok = (role is None or node.get("role") == role) and _glob_match(name, node.get("name"))
        if ok and text is not None:
            ok = text.lower() in ((node.get("text") or "") + " " + (node.get("name") or "")).lower()
        if ok and node.get("rect"):
            matches.append(node)
        for c in node.get("children") or []:
            walk(c)

    for n in nodes:
        walk(n)
    return matches[index] if index < len(matches) else None


def _center(rect: dict) -> tuple[int, int]:
    return int(rect["x"] + rect["w"] / 2), int(rect["y"] + rect["h"] / 2)


def _py(s: str) -> str:
    return repr(str(s))


def _hotkey(keys: str) -> str:
    parts = [k.strip().lower() for k in keys.replace("-", "+").split("+") if k.strip()]
    return "pyautogui.hotkey(" + ", ".join(_py(p) for p in parts) + ")"


def _sq(text: str) -> str:
    """Single-quote ``text`` for a POSIX shell (``shlex.split`` reads it back as one argv item)."""
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


# ---------------------------------------------------------------- code-first ops → VM actions

def map_script(step: Any) -> dict:
    """``{"op":"script"}`` → one Python action string for the VM (``python``/``bash`` only)."""
    payload = step.script
    language = str(payload.language)
    directory = str(payload.target_directory)
    timeout = int(payload.timeout_seconds)
    if language == "python":
        code = "\n".join([
            "import os",
            f"os.makedirs({_py(directory)}, exist_ok=True); os.chdir({_py(directory)})",
            "# --- O script step ---",
            payload.code.rstrip("\n"),
            "",
        ])
        return {"ok": True, "why": f"script python ({len(payload.code)} chars, {timeout}s) in {directory}", "actions": [code]}
    if language == "bash":
        code = "\n".join([
            "import os, subprocess, sys",
            f"os.makedirs({_py(directory)}, exist_ok=True)",
            f"_r = subprocess.run({_py(payload.code)}, shell=True, executable='/bin/bash', cwd={_py(directory)}, "
            f"capture_output=True, text=True, timeout={timeout})",
            "sys.stdout.write(_r.stdout); sys.stderr.write(_r.stderr)",
            "sys.exit(_r.returncode)",
            "",
        ])
        return {"ok": True, "why": f"script bash ({len(payload.code)} chars, {timeout}s) in {directory}", "actions": [code]}
    return {"ok": False, "why": f"script language {language!r} cannot run in the OSWorld VM (python or bash only)", "actions": []}


def state_check_probes(checks: Any) -> list[tuple[str, bool, str | None]]:
    """``(command, exit_zero_expected, stdout_prefix)`` per file/shell check, every command on the shared
    probe allow-list; a check that cannot be expressed as an allowed probe yields nothing (the caller refuses)."""
    out: list[tuple[str, bool, str | None]] = []
    for check in checks:
        kind, arg, expect = str(check.kind), str(check.arg), check.expect
        if kind == "file_exists":
            cmd, truth, prefix = f"test -f {_sq(arg)}", (expect or "").strip().lower() != "false", None
        elif kind == "file_contains":
            cmd, truth, prefix = f"grep -c {_sq(expect or '')} {_sq(arg)}", True, None
        elif kind == "file_sha_changed":
            cmd, truth, prefix = f"sha256sum {_sq(arg)}", True, (expect or None)
        elif kind == "shell_true":
            cmd, truth, prefix = arg.strip(), True, (expect if expect is not None else None)
        else:
            continue  # ``tree`` checks never leave the agent
        if not is_allowed_probe(cmd):
            return []
        out.append((cmd, truth, prefix))
    return out


def _probe_code(probes: list[tuple[str, bool, str | None]]) -> str:
    rows = ", ".join(f"({_py(c)}, {t!r}, {_py(p) if p is not None else None})" for c, t, p in probes)
    return "\n".join([
        "import json, subprocess",
        f"_probes = [{rows}]",
        "_out = []",
        "for _cmd, _truth, _prefix in _probes:",
        "    try:",
        f"        _r = subprocess.run(_cmd, shell=True, capture_output=True, text=True, timeout={PROBE_TIMEOUT_S})",
        "        _ok = (_r.returncode == 0) == _truth",
        "        if _ok and _prefix is not None:",
        "            _ok = _r.stdout.strip().startswith(_prefix)",
        "        _out.append({'probe': _cmd, 'ok': _ok, 'exit': _r.returncode, 'stdout': _r.stdout[:200]})",
        "    except Exception as _e:",
        "        _out.append({'probe': _cmd, 'ok': False, 'error': f'{type(_e).__name__}: {_e}'})",
        "print(json.dumps({'o_state_assert': _out}))",
        "",
    ])


def _tree_check(check: Any, nodes: list[dict]) -> tuple[bool, str]:
    from o_engine.cu.schema import Target

    raw = str(check.arg).strip()
    target: Any
    if raw.startswith("{"):
        try:
            target = Target.model_validate(json.loads(raw))
        except Exception as e:
            return False, f"tree check target is not valid: {e}"
    else:
        target = Target(name=raw)
    node = find_in_tree(nodes, target)
    if node is None:
        return False, f"tree: {target.describe()} absent"
    state = (check.expect or "").strip()
    if state and state != "visible" and state not in (node.get("states") or []):
        return False, f"tree: {target.describe()} not {state}"
    return True, f"tree: {target.describe()} present" + (f" and {state}" if state else "")


def map_state_assert(step: Any, nodes: list[dict]) -> dict:
    """``tree`` checks decided on the observed tree now; file/shell checks → one VM probe action (or a
    refused step when any command falls outside the allow-list)."""
    checks = list(step.checks)
    for check in checks:
        if str(check.kind) == "tree":
            ok, why = _tree_check(check, nodes)
            if not ok:
                return {"ok": False, "why": f"state_assert failed: {why}", "actions": []}
    remote = [c for c in checks if str(c.kind) != "tree"]
    if not remote:
        return {"ok": True, "why": f"state_assert ok on the tree ({len(checks)} check(s))", "actions": []}
    probes = state_check_probes(remote)
    if not probes:
        return {"ok": False, "why": f"state_assert refused: a shell check is {PROBE_REFUSED}", "actions": []}
    return {"ok": True, "why": f"state_assert: {len(probes)} VM probe(s) emitted (result lands in OSWorld's step log)",
            "actions": [_probe_code(probes)]}


class ObsBackend:
    """The read-only backend the feasibility gate probes: the last observation's tree and screenshot.
    ``tree``/``screenshot`` only — there is no ``find``/``click``/``type`` here, so a probe can never act."""

    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.png: bytes = b""
        self.display = None
        self.allow_pixels = False

    def update(self, nodes: list[dict], png: bytes | None) -> None:
        self.nodes = list(nodes or [])
        self.png = bytes(png or b"")

    def tree(self, window: Any = None) -> list[dict]:
        return list(self.nodes)

    def screenshot(self) -> bytes:
        return self.png


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ---------------------------------------------------------------- the agent

class OAgent:
    """OSWorld agent: O's milestone planner + feasibility gate + tree/vision grounding + judge, emitting
    VM-only action strings."""

    action_space = "pyautogui"

    def __init__(
        self,
        planner: Any,
        grounder: Any = None,
        judge: Any = None,
        app: str = "osworld",
        max_heals: int = 3,
        observation_type: str = "screenshot_a11y_tree",
        gate: Any = None,
        obs_backend: ObsBackend | None = None,
        steps_per_turn: int = DEFAULT_STEPS_PER_TURN,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        if steps_per_turn < 1 or max_turns < 1:
            raise ValueError("steps_per_turn and max_turns must be >= 1")
        self.planner = planner
        self.grounder = grounder
        self.judge = judge
        self.app = app
        self.max_heals = max_heals
        self.observation_type = observation_type
        self.gate = gate
        self.obs_backend = obs_backend or ObsBackend()
        self.steps_per_turn = int(steps_per_turn)
        self.max_turns = int(max_turns)
        self.task_counter = 0
        self.reset()

    # OSWorld calls reset(runtime_logger, vm_ip=...) or reset(vm_ip=...)
    def reset(self, runtime_logger: Any = None, vm_ip: str | None = None, **_: Any) -> None:
        self.logger = runtime_logger or LOG
        self.script: Any = None
        self.index = 0
        self.refs: dict[str, dict] = {}
        self.heals = 0
        self.trajectory: list[dict] = []
        self.milestones: list[Any] = []
        self.milestone_index = 0
        self.turn = 0
        self.memory: Any = None
        self.infeasible: str | None = None
        self.gate_checked = False
        self.task_counter += 1
        self.job_id = f"osworld-{self.task_counter}"
        self.obs_backend.update([], b"")

    # ------------------------------------------------------------ helpers

    def _nodes(self, obs: dict) -> list[dict]:
        tree = obs.get("accessibility_tree") if isinstance(obs, dict) else None
        if isinstance(tree, bytes):
            tree = tree.decode("utf-8", "replace")
        return parse_a11y_xml(tree or "")

    def _resolve(self, target: Any, nodes: list[dict], png: bytes | None) -> dict | None:
        node = find_in_tree(nodes, target)
        if node is not None:
            return {"rect": node["rect"], "via": "tree", "node": node.get("id")}
        if self.grounder is not None and png:
            try:
                box = self.grounder.ground(png, target)
            except Exception as e:  # grounder failures are a miss, not a crash
                self.logger.warning("grounder failed: %s", e)
                box = None
            if box:
                return {"rect": dict(box), "via": getattr(self.grounder, "last_source", "vision")}
        return None

    def _ref(self, ref: str | None, nodes: list[dict], png: bytes | None) -> dict | None:
        if ref is None:
            return None
        cached = self.refs.get(ref)
        if cached is None:
            return None
        target = cached.get("target")
        fresh = self._resolve(target, nodes, png) if target is not None else None
        return fresh or cached.get("where")

    def _heal(self, reason: str, nodes: list[dict]) -> bool:
        if self.heals >= self.max_heals:
            return False
        self.heals += 1
        self.logger.info("heal %d/%d at step %d: %s", self.heals, self.max_heals, self.index, reason)
        self.script = self.planner.replan_step(self.script, self.index, reason, summarize_tree(nodes, TREE_LINES))
        return True

    # ------------------------------------------------------------ milestones (P1) and the gate (P2)

    def _milestones(self, instruction: str) -> list[Any]:
        from o_engine.cu.planner import goal_question
        from o_engine.cu.schema import Milestone

        fallback = [Milestone(goal=instruction, done_when=goal_question(instruction))]
        fn = getattr(self.planner, "plan_milestones", None)
        if not callable(fn):
            return fallback
        try:
            rows = list(fn(instruction, self.app, None, memory=self.memory) or [])
        except TypeError:
            rows = list(fn(instruction, self.app) or [])
        return rows or fallback

    def _check_gate(self, instruction: str) -> str | None:
        """One feasibility question per task; ``None`` when feasible or when no gate is configured."""
        if self.gate is None:
            return None
        try:
            verdict = self.gate.check(instruction, self.app, self.job_id)
        except Exception as e:  # a gate outage never blocks a task
            self.logger.warning("feasibility gate failed open: %s", e)
            return None
        if bool(_get(verdict, "feasible", True)):
            return None
        return str(_get(verdict, "reason") or "goal judged infeasible")

    def _next_turn(self, nodes: list[dict]) -> None:
        self.turn += 1
        milestone = self.milestones[self.milestone_index]
        self.script = self.planner.next_steps(milestone, summarize_tree(nodes, TREE_LINES), self.memory,
                                              max_steps=self.steps_per_turn, app=self.app)
        self.index = 0
        self.logger.info("turn %d/%d: milestone %d/%d %r -> %d step(s)", self.turn, self.max_turns,
                         self.milestone_index + 1, len(self.milestones), str(_get(milestone, "goal", ""))[:80],
                         len(getattr(self.script, "steps", []) or []))

    def _milestone_done(self, nodes: list[dict], png: bytes | None) -> tuple[bool, str]:
        milestone = self.milestones[self.milestone_index]
        done_when = _get(milestone, "done_when")
        if done_when is None:
            return True, "milestone declares no done_when"
        if isinstance(done_when, str):
            if self.judge is None or not png:
                return False, "no judge for the done_when question"
            ok, reason = self.judge.ask(png, done_when)
            return bool(ok), str(reason or done_when)
        node = find_in_tree(nodes, done_when)
        describe = getattr(done_when, "describe", None)
        text = describe() if callable(describe) else str(done_when)
        return (node is not None), (f"{text} present" if node is not None else f"{text} absent")

    # ------------------------------------------------------------ predict

    def predict(self, instruction: str, obs: dict) -> tuple[str, list[str]]:
        png = obs.get("screenshot") if isinstance(obs, dict) else None
        nodes = self._nodes(obs)
        self.obs_backend.update(nodes, png)
        if self.infeasible is not None:
            return self._emit(f"infeasible: {self.infeasible}", [FAIL_ACTION])
        if not self.gate_checked:
            self.gate_checked = True
            self.infeasible = self._check_gate(instruction)
            if self.infeasible is not None:
                self.logger.info("goal judged infeasible: %s", self.infeasible)
                return self._emit(f"infeasible: {self.infeasible}", [FAIL_ACTION])
        if not self.milestones:
            try:
                from o_engine.cu.memory import WorkingMemory

                self.memory = WorkingMemory.from_goal(instruction)
            except Exception:
                self.memory = None
            self.milestones = self._milestones(instruction)
            self.milestone_index = 0
            self.logger.info("planned %d milestone(s)", len(self.milestones))
            self._next_turn(nodes)
        while True:
            steps = list(getattr(self.script, "steps", []) or [])
            if self.index >= len(steps):
                done, reason = self._milestone_done(nodes, png)
                self.logger.info("milestone %d %s after turn %d: %s", self.milestone_index, "done" if done else "not done",
                                 self.turn, reason)
                if done:
                    self.milestone_index += 1
                    if self.milestone_index >= len(self.milestones):
                        return self._emit(f"{len(self.milestones)} milestone(s) done: {reason}", [DONE_ACTION])
                if self.turn >= self.max_turns:
                    goal = str(_get(self.milestones[self.milestone_index], "goal", ""))[:80]
                    return self._emit(f"milestone {self.milestone_index} ({goal}) not done after {self.turn} turn(s): {reason}",
                                      [FAIL_ACTION])
                self._next_turn(nodes)
                continue
            step = steps[self.index]
            outcome = self._step_actions(step, nodes, png)
            if outcome["ok"]:
                if not outcome.get("stay"):
                    self.index += 1
                if not outcome["actions"] and not outcome.get("stay"):
                    if outcome.get("note"):
                        self.trajectory.append({"step": self.index - 1, "response": outcome["why"], "actions": []})
                    continue  # find/assert/tree checks produce no VM action; move straight on
                return self._emit(outcome["why"], outcome["actions"])
            if not self._heal(outcome["why"], nodes):
                return self._emit(f"gave up after {self.heals} heal(s): {outcome['why']}", [FAIL_ACTION])

    def _emit(self, response: str, actions: list[str]) -> tuple[str, list[str]]:
        self.trajectory.append({"step": self.index, "response": response, "actions": actions})
        return response, actions

    def _step_actions(self, step: Any, nodes: list[dict], png: bytes | None) -> dict:
        op = getattr(step, "op", None)
        if op == "launch":
            cmd = step.cmd
            return {"ok": True, "why": f"launch {cmd}",
                    "actions": [f"import subprocess; subprocess.Popen({_py(cmd)}, shell=True)", WAIT_ACTION]}
        if op == "wait":
            where = self._resolve(step.for_, nodes, png)
            if where is None:
                return {"ok": True, "why": f"waiting for {step.for_.describe()}", "actions": [WAIT_ACTION],
                        "stay": True}
            return {"ok": True, "why": f"{step.for_.describe()} present", "actions": []}
        if op == "find":
            where = self._resolve(step.target, nodes, png)
            if where is None:
                return {"ok": False, "why": f"element not found: {step.target.describe()}", "actions": []}
            self.refs[step.as_] = {"target": step.target, "where": where}
            return {"ok": True, "why": f"found {step.as_} via {where['via']}", "actions": []}
        if op in ("click", "invoke"):
            where = self._ref(step.ref, nodes, png)
            if where is None:
                return {"ok": False, "why": f"ref {step.ref!r} is not resolved", "actions": []}
            x, y = _center(where["rect"])
            return {"ok": True, "why": f"{op} {step.ref} at ({x},{y})", "actions": [f"pyautogui.click({x}, {y})"]}
        if op == "type":
            actions = []
            if step.ref:
                where = self._ref(step.ref, nodes, png)
                if where is None:
                    return {"ok": False, "why": f"ref {step.ref!r} is not resolved", "actions": []}
                x, y = _center(where["rect"])
                actions.append(f"pyautogui.click({x}, {y})")
            actions.append(f"pyautogui.typewrite({_py(step.text)}, interval=0.02)")
            return {"ok": True, "why": f"type {step.text[:40]!r}", "actions": actions}
        if op == "set_value":
            where = self._ref(step.ref, nodes, png)
            if where is None:
                return {"ok": False, "why": f"ref {step.ref!r} is not resolved", "actions": []}
            x, y = _center(where["rect"])
            return {"ok": True, "why": f"set_value {step.ref}", "actions": [
                f"pyautogui.click({x}, {y})", "pyautogui.hotkey('ctrl', 'a')",
                f"pyautogui.typewrite({_py(step.value)}, interval=0.02)",
            ]}
        if op == "key":
            return {"ok": True, "why": f"key {step.keys}", "actions": [_hotkey(step.keys)]}
        if op == "assert":
            node = find_in_tree(nodes, step.target)
            if node is None:
                return {"ok": False, "why": f"assert: {step.target.describe()} absent", "actions": []}
            if step.state not in node.get("states", []) and step.state != "visible":
                return {"ok": False, "why": f"assert: {step.target.describe()} not {step.state}", "actions": []}
            return {"ok": True, "why": f"assert {step.state} ok", "actions": []}
        if op == "screenshot_assert":
            if self.judge is None or not png:
                return {"ok": True, "why": "screenshot_assert skipped (no judge)", "actions": []}
            ok, reason = self.judge.ask(png, step.question)
            if not ok:
                return {"ok": False, "why": f"screenshot_assert failed: {reason or step.question}", "actions": []}
            return {"ok": True, "why": f"screenshot_assert ok: {reason}", "actions": []}
        if op == "script":
            return map_script(step)
        if op == "state_assert":
            return map_state_assert(step, nodes)
        if op == "call":
            why = f"call {step.fn} unavailable: the OSWorld VM has no interface store"
            self.logger.info("%s", why)
            return {"ok": False, "why": why, "actions": []}
        return {"ok": False, "why": f"unknown op {op!r}", "actions": []}


# ---------------------------------------------------------------- O wiring

def build_agent(args: argparse.Namespace) -> OAgent:
    from o_engine.cu.ground import ClaudeCodeGrounder, Grounder, VisionGrounder
    from o_engine.cu.interfaces import InterfaceNotes
    from o_engine.cu.judge import ClaudeCodeJudge, Judge, VLMJudge
    from o_engine.cu.planner import ClaudeCodeClient, LocalModelClient, ModelClient, Planner

    from o_engine.cu.feasibility import FeasibilityGate
    from o_engine.cu.ground import ZoomGrounder

    vision = args.vllm_vision_model or args.vllm_model
    notes = InterfaceNotes(str(Path(args.data_dir) / "interfaces"))
    model = ModelClient(LocalModelClient(args.vllm_url, args.vllm_model), ClaudeCodeClient(args.claude_path))
    planner = Planner(model, notes)
    primary: Any = VisionGrounder(args.vllm_url, vision)
    if not args.no_zoom:
        primary = ZoomGrounder(primary)
    grounder = Grounder(primary, ClaudeCodeGrounder(args.claude_path), min_confidence=args.ground_confidence)
    judge = Judge(VLMJudge(args.vllm_url, vision), ClaudeCodeJudge(args.claude_path))
    backend = ObsBackend()
    # sandbox=None: the ``ls``/``read_file``/``which`` probes answer "no sandbox configured" (the VM's files are
    # not reachable from here); ``tree`` and ``screenshot`` read the observation.
    gate = None if args.no_feasibility else FeasibilityGate(model, backend, None)
    return OAgent(planner, grounder, judge, app="osworld", max_heals=args.max_heals, observation_type=args.observation_type,
                  gate=gate, obs_backend=backend, steps_per_turn=args.steps_per_turn, max_turns=args.max_turns)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run OSWorld with O's computer-use agent (actions run only inside the VM).")
    p.add_argument("--osworld", default=".", help="path to an OSWorld checkout (default: cwd)")
    # OSWorld run.py flags (same names/defaults)
    p.add_argument("--path_to_vm", type=str, default=None)
    p.add_argument("--provider_name", type=str, default="vmware")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--observation_type", choices=["screenshot", "a11y_tree", "screenshot_a11y_tree"], default="screenshot_a11y_tree")
    p.add_argument("--screen_width", type=int, default=1920)
    p.add_argument("--screen_height", type=int, default=1080)
    p.add_argument("--sleep_after_execution", type=float, default=3.0)
    p.add_argument("--max_steps", type=int, default=15)
    p.add_argument("--test_config_base_dir", type=str, default="evaluation_examples")
    p.add_argument("--domain", type=str, default="all")
    p.add_argument("--test_all_meta_path", type=str, default="evaluation_examples/test_all.json")
    p.add_argument("--result_dir", type=str, default="./results")
    p.add_argument("--model", type=str, default="o", help="label used in the result-dir layout")
    p.add_argument("--client_password", type=str, default="password")
    # O flags
    p.add_argument("--vllm_url", default=os.environ.get("O_VLLM_URL", "http://127.0.0.1:8000/v1"))
    p.add_argument("--vllm_model", default=os.environ.get("O_VLLM_MODEL", "meta-llama/Meta-Llama-3-8B-Instruct"))
    p.add_argument("--vllm_vision_model", default=os.environ.get("O_VLLM_VISION_MODEL", ""))
    p.add_argument("--claude_path", default=os.environ.get("O_CLAUDE_PATH", "claude"))
    p.add_argument("--data_dir", default=os.environ.get("O_DATA_DIR", str(Path.home() / ".o-engine")))
    p.add_argument("--max_heals", type=int, default=int(os.environ.get("O_CU_MAX_HEALS", "3")))
    p.add_argument("--steps_per_turn", type=int, default=int(os.environ.get("O_CU_STEPS_PER_TURN", str(DEFAULT_STEPS_PER_TURN))))
    p.add_argument("--max_turns", type=int, default=int(os.environ.get("O_CU_MAX_TURNS", str(DEFAULT_MAX_TURNS))))
    p.add_argument("--ground_confidence", type=float, default=float(os.environ.get("O_CU_GROUND_CONFIDENCE", str(DEFAULT_GROUND_CONFIDENCE))))
    p.add_argument("--no_feasibility", action="store_true", help="skip the feasibility gate (O_CU_FEASIBILITY=0)")
    p.add_argument("--no_zoom", action="store_true", help="plain vision grounding, no zoom-in step (O_CU_ZOOM=0)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    osworld = Path(args.osworld).resolve()
    if str(osworld) not in sys.path:
        sys.path.insert(0, str(osworld))
    try:
        import lib_run_single  # OSWorld
        from desktop_env.desktop_env import DesktopEnv  # OSWorld
    except ImportError as e:
        print(f"OSWorld not importable from {osworld}: {e}. Clone it and pass --osworld.", file=sys.stderr)
        return 2
    os.chdir(osworld)

    agent = build_agent(args)
    env = DesktopEnv(
        provider_name=args.provider_name,
        path_to_vm=args.path_to_vm,
        action_space=agent.action_space,
        screen_size=(args.screen_width, args.screen_height),
        headless=args.headless,
        os_type="Ubuntu",
        require_a11y_tree=args.observation_type in ("a11y_tree", "screenshot_a11y_tree"),
    )
    with open(args.test_all_meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    if args.domain != "all":
        meta = {args.domain: meta[args.domain]}

    scores: list[float] = []
    for domain, example_ids in meta.items():
        for example_id in example_ids:
            config_file = os.path.join(args.test_config_base_dir, f"examples/{domain}/{example_id}.json")
            with open(config_file, encoding="utf-8") as f:
                example = json.load(f)
            example_result_dir = os.path.join(args.result_dir, agent.action_space, args.observation_type,
                                              args.model, domain, example_id)
            os.makedirs(example_result_dir, exist_ok=True)
            if os.path.exists(os.path.join(example_result_dir, "result.txt")):
                LOG.info("skip finished %s/%s", domain, example_id)
                continue
            LOG.info("[%s/%s] %s", domain, example_id, example.get("instruction", "")[:120])
            try:
                lib_run_single.run_single_example(agent, env, example, args.max_steps, example["instruction"],
                                                  args, example_result_dir, scores)
            except Exception as e:
                LOG.exception("example %s/%s crashed: %s", domain, example_id, e)
                with open(os.path.join(example_result_dir, "traj.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps({"Error": f"{domain}/{example_id} - {e}"}) + "\n")
    env.close()
    if scores:
        print(f"Average score: {sum(scores) / len(scores):.4f} over {len(scores)} example(s) run now")
    print(f"Run {_HERE / 'summarize.py'} --result_dir {args.result_dir} for the per-domain table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
