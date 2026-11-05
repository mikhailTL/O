"""``eval/osworld/run_o.py``: the P1 milestone path of ``OAgent`` and its step → VM-action mapping, with a
stubbed planner/judge/gate and no VM. Every emitted action is a string OSWorld would run inside the VM;
no host backend is imported and nothing here touches a display."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from o_engine.cu.probes import is_allowed_probe
from o_engine.cu.schema import Milestone, Target, parse_script

_RUN_O = Path(__file__).resolve().parent.parent / "eval" / "osworld" / "run_o.py"


@pytest.fixture(scope="module")
def run_o():
    spec = importlib.util.spec_from_file_location("run_o_under_test", _RUN_O)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


TREE_XML = """<?xml version="1.0"?>
<frame xmlns:cp="https://accessibility.windows.example.org/ns/component" xmlns:st="https://accessibility.windows.example.org/ns/state"
       name="Calc" cp:screencoord="(0, 0)" cp:size="(800, 600)" st:visible="true">
  <menu name="File" cp:screencoord="(10, 10)" cp:size="(40, 20)" st:visible="true"/>
  <push_button name="Save" cp:screencoord="(100, 10)" cp:size="(60, 20)" st:visible="true" st:enabled="true"/>
</frame>
"""
TREE_DONE_XML = TREE_XML.replace('name="Save"', 'name="Saved"')


def _obs(xml=TREE_XML):
    return {"screenshot": b"\x89PNG fake", "accessibility_tree": xml}


class StubPlanner:
    """Milestone API only: ``plan`` is never called on the P1 path."""

    def __init__(self, milestones, turns, replans=()):
        self.milestones = list(milestones)
        self.turns = list(turns)
        self.replans = list(replans)
        self.calls = []

    def plan(self, *a, **k):
        raise AssertionError("plan-once path must not be used")

    def plan_milestones(self, goal, app, notes=None, memory=None):
        self.calls.append(("plan_milestones", goal, app))
        return tuple(self.milestones)

    def next_steps(self, milestone, tree_summary, memory=None, notes=None, interfaces=(), max_steps=5, app=""):
        self.calls.append(("next_steps", milestone.goal, max_steps))
        if not self.turns:
            raise AssertionError("no scripted turn left")
        steps = self.turns.pop(0)
        return parse_script({"app": app or "osworld", "goal": milestone.goal, "steps": steps[:max_steps]})

    def replan_step(self, script, failed_index, error, tree_summary="", memory=None, loop_evidence=None):
        self.calls.append(("replan_step", failed_index, error))
        if not self.replans:
            raise AssertionError("no scripted replan left")
        return parse_script(self.replans.pop(0))


class StubJudge:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def ask(self, png, question):
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else (False, "no answer scripted")


class StubGate:
    def __init__(self, feasible, reason="x"):
        self.feasible = feasible
        self.reason = reason
        self.calls = []

    def check(self, goal, app, job_id):
        self.calls.append((goal, app, job_id))
        return {"feasible": self.feasible, "reason": self.reason}


def _agent(run_o, planner, judge=None, gate=None, **kw):
    return run_o.OAgent(planner, grounder=None, judge=judge, gate=gate, app="osworld", **kw)


# ---------------------------------------------------------------- P1 loop

def test_milestone_loop_plans_once_then_one_next_steps_per_observation(run_o):
    m1 = Milestone(goal="open File", done_when=Target(role="menu", name="File"))
    m2 = Milestone(goal="save", done_when=Target(role="push button", name="Saved"))
    planner = StubPlanner(
        [m1, m2],
        turns=[
            [{"op": "find", "target": {"role": "menu", "name": "File"}, "as": "f"}, {"op": "click", "ref": "f"}],
            [{"op": "find", "target": {"role": "push button", "name": "Save"}, "as": "s"}, {"op": "click", "ref": "s"}],
        ],
    )
    agent = _agent(run_o, planner, steps_per_turn=3, max_turns=4)
    agent.reset()
    r1, a1 = agent.predict("Save the sheet", _obs())
    assert a1 == ["pyautogui.click(30, 20)"], r1
    assert [c[0] for c in planner.calls] == ["plan_milestones", "next_steps"]
    assert planner.calls[1][2] == 3, "next_steps is bounded by steps_per_turn"
    # turn 1 done -> milestone 0 done_when (File menu present on the tree) -> milestone 1 -> next_steps on the new observation
    r2, a2 = agent.predict("Save the sheet", _obs())
    assert a2 == ["pyautogui.click(130, 20)"], r2
    assert [c[0] for c in planner.calls] == ["plan_milestones", "next_steps", "next_steps"]
    assert agent.milestone_index == 1
    # the tree now shows "Saved": milestone 1 done_when passes -> DONE, no further planning
    r3, a3 = agent.predict("Save the sheet", _obs(TREE_DONE_XML))
    assert a3 == [run_o.DONE_ACTION], r3
    assert len([c for c in planner.calls if c[0] == "next_steps"]) == 2
    assert [t["actions"] for t in agent.trajectory] == [a1, a2, a3]


def test_done_when_question_goes_to_the_judge_and_a_no_replans_the_same_milestone(run_o):
    m = Milestone(goal="type hello", done_when="Is the word hello visible in the document?")
    planner = StubPlanner([m], turns=[[{"op": "key", "keys": "ctrl+n"}], [{"op": "type", "text": "hello"}]])
    judge = StubJudge([(False, "still empty"), (True, "hello is there")])
    agent = _agent(run_o, planner, judge=judge, max_turns=5)
    agent.reset()
    _, a1 = agent.predict("g", _obs())
    assert a1 == ["pyautogui.hotkey('ctrl', 'n')"]
    _, a2 = agent.predict("g", _obs())
    assert a2 == ["pyautogui.typewrite('hello', interval=0.02)"]
    assert judge.asked == [m.done_when]
    _, a3 = agent.predict("g", _obs())
    assert a3 == [run_o.DONE_ACTION]
    assert judge.asked == [m.done_when, m.done_when]
    assert agent.turn == 2


def test_max_turns_exhausted_emits_fail(run_o):
    m = Milestone(goal="never", done_when=Target(role="push button", name="Nope"))
    planner = StubPlanner([m], turns=[[{"op": "key", "keys": "F5"}]] * 3)
    agent = _agent(run_o, planner, max_turns=2)
    agent.reset()
    agent.predict("g", _obs())
    agent.predict("g", _obs())
    response, actions = agent.predict("g", _obs())
    assert actions == [run_o.FAIL_ACTION]
    assert "turn" in response and "Nope" in response
    assert len([c for c in planner.calls if c[0] == "next_steps"]) == 2


def test_planner_without_milestone_api_falls_back_to_one_goal_milestone(run_o):
    class OldPlanner:
        def __init__(self):
            self.calls = []

        def next_steps(self, milestone, tree_summary, memory=None, notes=None, interfaces=(), max_steps=5, app=""):
            self.calls.append(milestone)
            return parse_script({"app": "osworld", "goal": milestone.goal, "steps": [{"op": "key", "keys": "ctrl+s"}]})

    planner = OldPlanner()
    judge = StubJudge([(True, "done")])
    agent = _agent(run_o, planner, judge=judge)
    agent.reset()
    agent.predict("Save it", _obs())
    assert planner.calls[0].goal == "Save it" and isinstance(planner.calls[0].done_when, str)
    _, actions = agent.predict("Save it", _obs())
    assert actions == [run_o.DONE_ACTION]


# ---------------------------------------------------------------- feasibility

def test_infeasible_goal_emits_fail_before_any_planning(run_o):
    planner = StubPlanner([Milestone(goal="g", done_when="q?")], turns=[[{"op": "key", "keys": "F1"}]])
    gate = StubGate(False, "no such app on this machine")
    agent = _agent(run_o, planner, gate=gate)
    agent.reset()
    response, actions = agent.predict("Open Photoshop", _obs())
    assert actions == [run_o.FAIL_ACTION]
    assert "infeasible" in response and "no such app" in response
    assert planner.calls == []
    assert gate.calls and gate.calls[0][0] == "Open Photoshop"
    # the gate is asked once per task, never again on later observations
    response2, actions2 = agent.predict("Open Photoshop", _obs())
    assert actions2 == [run_o.FAIL_ACTION] and len(gate.calls) == 1


def test_feasible_goal_proceeds_and_obs_backend_serves_the_vm_tree(run_o):
    planner = StubPlanner([Milestone(goal="g", done_when=Target(role="menu", name="File"))], turns=[[{"op": "key", "keys": "F1"}]])
    backend = run_o.ObsBackend()
    seen = {}

    class TreeGate:
        def check(self, goal, app, job_id):
            seen["tree"] = [n["name"] for n in backend.tree()]
            seen["png"] = backend.screenshot()
            return {"feasible": True, "reason": "ok"}

    agent = _agent(run_o, planner, gate=TreeGate(), obs_backend=backend)
    agent.reset()
    _, actions = agent.predict("g", _obs())
    assert actions == ["pyautogui.hotkey('f1')"]
    assert seen["tree"] == ["Calc"] and seen["png"] == b"\x89PNG fake"


# ---------------------------------------------------------------- script / state_assert / call mapping

def test_script_python_runs_in_the_vm_from_its_target_directory(run_o):
    step = parse_script({"app": "a", "goal": "g", "steps": [
        {"op": "script", "script": {"language": "python", "code": "open('out.csv','w').write('1,2')\n",
                                    "target_directory": "work", "timeout_seconds": 30}}]}).steps[0]
    out = run_o.map_script(step)
    assert out["ok"] and len(out["actions"]) == 1
    code = out["actions"][0]
    assert "os.chdir('work')" in code and "open('out.csv','w').write('1,2')" in code
    assert "pyautogui" not in code
    compile(code, "<vm>", "exec")


def test_script_bash_runs_through_subprocess_with_the_timeout(run_o):
    step = parse_script({"app": "a", "goal": "g", "steps": [
        {"op": "script", "script": {"language": "bash", "code": "ls -1 > list.txt", "target_directory": ".", "timeout_seconds": 7}}]}).steps[0]
    out = run_o.map_script(step)
    assert out["ok"]
    code = out["actions"][0]
    assert "subprocess.run" in code and "'ls -1 > list.txt'" in code and "timeout=7" in code and "shell=True" in code
    assert "executable='/bin/bash'" in code
    compile(code, "<vm>", "exec")


@pytest.mark.parametrize("language", ["javascript", "powershell"])
def test_script_other_languages_are_a_step_failure_not_a_crash(run_o, language):
    step = parse_script({"app": "a", "goal": "g", "steps": [
        {"op": "script", "script": {"language": language, "code": "x", "target_directory": ".", "timeout_seconds": 5}}]}).steps[0]
    out = run_o.map_script(step)
    assert out["ok"] is False and language in out["why"] and out["actions"] == []


def test_state_assert_file_checks_become_allow_listed_vm_probes(run_o):
    step = parse_script({"app": "a", "goal": "g", "steps": [{"op": "state_assert", "checks": [
        {"kind": "file_exists", "arg": "out/report.csv"},
        {"kind": "file_exists", "arg": "tmp.lock", "expect": "false"},
        {"kind": "file_contains", "arg": "out/report.csv", "expect": "total"},
        {"kind": "file_sha_changed", "arg": "notes.txt"},
        {"kind": "shell_true", "arg": "test -d out"},
    ]}]}).steps[0]
    out = run_o.map_state_assert(step, nodes=[])
    assert out["ok"] is True
    assert len(out["actions"]) == 1
    code = out["actions"][0]
    compile(code, "<vm>", "exec")
    probes = run_o.state_check_probes(step.checks)
    assert [p[0] for p in probes] == ["test -f 'out/report.csv'", "test -f 'tmp.lock'", "grep -c 'total' 'out/report.csv'",
                                      "sha256sum 'notes.txt'", "test -d out"]
    assert all(is_allowed_probe(p[0]) for p in probes)
    assert [p[1] for p in probes] == [True, False, True, True, True], "expected exit-0 truth per probe"
    for cmd, _, _prefix in probes:
        assert repr(cmd) in code
    assert "pyautogui" not in code


def test_state_assert_refuses_shell_probes_outside_the_allow_list(run_o):
    step = parse_script({"app": "a", "goal": "g", "steps": [{"op": "state_assert", "checks": [
        {"kind": "shell_true", "arg": "rm -rf out"},
    ]}]}).steps[0]
    out = run_o.map_state_assert(step, nodes=[])
    assert out["ok"] is False and "not allowed" in out["why"] and out["actions"] == []
    assert run_o.state_check_probes(step.checks) == []


def test_state_assert_tree_checks_are_decided_on_the_observed_tree(run_o):
    nodes = run_o.parse_a11y_xml(TREE_XML)
    ok_step = parse_script({"app": "a", "goal": "g", "steps": [{"op": "state_assert", "checks": [
        {"kind": "tree", "arg": '{"role": "push button", "name": "Save"}', "expect": "enabled"},
        {"kind": "tree", "arg": "File"},
    ]}]}).steps[0]
    out = run_o.map_state_assert(ok_step, nodes)
    assert out["ok"] is True and out["actions"] == []
    bad_step = parse_script({"app": "a", "goal": "g", "steps": [{"op": "state_assert", "checks": [
        {"kind": "tree", "arg": '{"role": "push button", "name": "Save"}', "expect": "checked"},
    ]}]}).steps[0]
    out = run_o.map_state_assert(bad_step, nodes)
    assert out["ok"] is False and "checked" in out["why"]
    missing = parse_script({"app": "a", "goal": "g", "steps": [{"op": "state_assert", "checks": [{"kind": "tree", "arg": "Nope"}]}]}).steps[0]
    assert run_o.map_state_assert(missing, nodes)["ok"] is False


def test_call_steps_fail_into_heal_not_silent_pass(run_o, caplog):
    planner = StubPlanner([Milestone(goal="g", done_when=Target(role="menu", name="File"))],
                          turns=[[{"op": "call", "fn": "calc.set_cell", "args": {"ref": "A1", "value": "42"}}, {"op": "key", "keys": "F2"}]],
                          replans=[{"app": "osworld", "goal": "g", "steps": [{"op": "key", "keys": "ctrl+s"}, {"op": "key", "keys": "F2"}]}])
    agent = _agent(run_o, planner)
    agent.reset()
    with caplog.at_level("INFO", logger="o.osworld"):
        response, actions = agent.predict("g", _obs())
    # the call step is a failure (no interface store in the VM): the F2 step after it must not run
    assert "pyautogui.hotkey('f2')" not in actions
    assert any("calc.set_cell" in r.getMessage() and "unavailable" in r.getMessage() for r in caplog.records)
    assert any(c[0] == "replan_step" for c in planner.calls)


def test_script_then_state_assert_in_one_turn_emits_both_vm_actions(run_o):
    planner = StubPlanner(
        [Milestone(goal="g", done_when=Target(role="menu", name="File"))],
        turns=[[
            {"op": "script", "script": {"language": "python", "code": "print(1)", "target_directory": ".", "timeout_seconds": 5}},
            {"op": "state_assert", "checks": [{"kind": "file_exists", "arg": "a.txt"}]},
        ]],
    )
    agent = _agent(run_o, planner)
    agent.reset()
    _, a1 = agent.predict("g", _obs())
    assert len(a1) == 1 and "print(1)" in a1[0]
    _, a2 = agent.predict("g", _obs())
    assert len(a2) == 1 and "test -f 'a.txt'" in a2[0]
    _, a3 = agent.predict("g", _obs())
    assert a3 == [run_o.DONE_ACTION]


def test_failed_step_is_healed_with_replan_step_then_fail(run_o):
    planner = StubPlanner(
        [Milestone(goal="g", done_when=Target(role="menu", name="File"))],
        turns=[[{"op": "find", "target": {"role": "push button", "name": "Missing"}, "as": "m"}, {"op": "click", "ref": "m"}]],
        replans=[{"app": "osworld", "goal": "g", "steps": [{"op": "find", "target": {"role": "push button", "name": "Save"}, "as": "m"}, {"op": "click", "ref": "m"}]}],
    )
    agent = _agent(run_o, planner, max_heals=1)
    agent.reset()
    response, actions = agent.predict("g", _obs())
    assert actions == ["pyautogui.click(130, 20)"]
    assert [c[0] for c in planner.calls] == ["plan_milestones", "next_steps", "replan_step"]
    assert agent.heals == 1


def test_build_parser_has_wave2_flags(run_o):
    args = run_o.build_parser().parse_args(["--steps_per_turn", "3", "--max_turns", "7", "--no_feasibility", "--ground_confidence", "0.5"])
    assert (args.steps_per_turn, args.max_turns, args.no_feasibility, args.ground_confidence) == (3, 7, True, 0.5)
    defaults = run_o.build_parser().parse_args([])
    assert (defaults.steps_per_turn, defaults.max_turns, defaults.no_feasibility, defaults.ground_confidence) == (5, 20, False, 0.3)
