"""Regression tests for the third 2026-11-05 real run (Lisbon list saved as ~/lisbon.txt). The job reported ok, but the
recording ended on a second Save As whose Name field read "lisbon.txt~/lisbon.txt~/lisbon.txt~/lisbon.txt" and a GTK
error dialog. Root cause, from engine-actions.jsonl:

* turn 3 saved /root/lisbon.txt, then ``state_assert file_exists '~/lisbon.txt'`` was resolved inside the job sandbox
  (``<sandbox>/~/lisbon.txt``) and failed;
* every heal replaced only that check but re-ran the turn from step 0 (ctrl+shift+s, ``type ~/lisbon.txt`` into the now
  prefilled entry, Return): ``type`` inserts at the cursor, so the name was appended three times and GTK raised an error;
* the third heal's check was the window title, still "lisbon.txt - Mousepad" from the first save, so the judge passed
  and the dialogs were left open.

No model and no display here: ``SimMousepad`` models the editor, its Save As / Open choosers and the GTK error alert.
The real-display regression is ``test_cu_real_atspi.py::test_lisbon_real_run3_saves_once_and_leaves_one_window_no_dialogs``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from o_engine.actionlog import ActionLog
from o_engine.cu.backends.base import BackendError, CUBackend, Node
from o_engine.cu.executor import ScriptRunner
from o_engine.cu.graph import CUDeps, build_cu_graph, run_cu_job
from o_engine.cu.judge import StateJudge
from o_engine.cu.planner import (
    MILESTONES_SYSTEM_PROMPT,
    STATIC_PREFIX,
    save_targets,
    script_to_dict,
    step_to_dict,
    with_save_checks,
)
from o_engine.cu.schema import Milestone, StateCheck, parse_script
from o_engine.executor import Sandbox

LINES = ("Clothes: light layers, rain jacket", "Gear: phone charger, EU plug adapter", "Documents: passport/ID")
GOAL = ('In the text editor, type a 3-line packing list for Lisbon, then save the file as ~/lisbon.txt '
        '(home directory, name lisbon.txt).')


# ================================================================== a simulated Mousepad

class SimMousepad(CUBackend):
    """Mousepad + GTK file chooser, enough of it to replay real run 3. ``home`` is the desktop user's HOME."""

    name = "sim"

    def __init__(self, home: Path) -> None:
        super().__init__(allow_pixels=True, display=":97")
        self.home = home
        self.doc_name: str | None = None
        self.text = ""
        self.chooser: dict | None = None  # {"title": "Save As"|"Open File", "entry": str}
        self.alert: str | None = None
        self.extra_dialog: tuple[str, str] | None = None  # (role, name) of a dialog the user had open
        self.actions: list[tuple] = []
        self.entry_values: list[str] = []  # the Name field's value every time it changed

    # ---------------------------------------------------------------- tree
    def tree(self, window=None):
        title = f"{self.doc_name or 'Untitled 1'} - Mousepad"
        wins = [Node(id="a0.0", role="frame", name=title, states=["visible", "showing"] + ([] if self.chooser else ["active"]),
                     children=[Node(id="a0.0.0", role="text", text=self.text, states=["editable", "multi line", "visible"])])]
        if self.chooser:
            wins.append(Node(id="a0.1", role="dialog", name=self.chooser["title"],
                             states=["modal", "visible", "showing"] + ([] if self.alert else ["active"]), children=[
                Node(id="a0.1.0", role="file chooser", name="File Chooser Widget", states=["visible"], children=[
                    Node(id="a0.1.0.0", role="text", name="Name:", text=self.chooser["entry"],
                         states=["editable", "single line", "focused", "visible"]),
                ]),
                Node(id="a0.1.1", role="push button", name="Cancel", states=["visible"]),
                Node(id="a0.1.2", role="push button", name="Save" if self.chooser["title"] == "Save As" else "Open",
                     states=["visible"]),
            ]))
        if self.alert:
            wins.append(Node(id="a0.2", role="alert", name="Error", states=["modal", "visible", "showing", "active"],
                             children=[Node(id="a0.2.0", role="label", name=self.alert, states=["visible"]),
                                       Node(id="a0.2.1", role="push button", name="OK", states=["visible"])]))
        if self.extra_dialog:
            wins.append(Node(id="a0.3", role=self.extra_dialog[0], name=self.extra_dialog[1], states=["visible", "showing"],
                             children=[Node(id="a0.3.0", role="push button", name="Close", states=["visible"])]))
        return [Node(id="a0", role="application", name="mousepad", children=wins)]

    def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\nsim"

    # ---------------------------------------------------------------- actions
    def _set_entry(self, value: str) -> None:
        self.chooser["entry"] = value
        self.entry_values.append(value)

    def click(self, node):
        self.actions.append(("click", node.id, node.name))
        if node.id == "a0.2.1":
            self.alert = None
        elif node.id == "a0.1.1":
            self.chooser = None
        elif node.id == "a0.3.0":
            self.extra_dialog = None

    def invoke(self, node, action):
        self.click(node)

    def set_value(self, node, value):
        self.actions.append(("set_value", node.id, value))
        if node.id == "a0.1.0.0" and self.chooser:
            self._set_entry(value)
        elif node.id == "a0.0.0":
            self.text = value
        else:
            raise BackendError(f"sim: cannot set {node.id}")

    def type_text(self, node, text):
        self.actions.append(("type_text", node.id if node else None, text))
        if node is not None and node.id == "a0.1.0.0" and self.chooser:
            self._set_entry(self.chooser["entry"] + text)  # GTK: inserted at the cursor (the end)
        elif node is not None and node.id == "a0.0.0":
            self.text += text
        elif node is None and self.chooser:
            self._set_entry(self.chooser["entry"] + text)

    def key(self, keys):
        self.actions.append(("key", keys))
        if self.alert:
            if keys in ("Escape", "Return"):
                self.alert = None
            return
        if keys in ("ctrl+shift+s",) or (keys == "ctrl+s" and self.doc_name is None):
            if self.chooser is None:
                self.chooser = {"title": "Save As", "entry": ""}
                self._set_entry(self.doc_name or "")
        elif keys == "ctrl+o":
            self.chooser = {"title": "Open File", "entry": ""}
        elif keys == "Escape" and self.chooser:
            self.chooser = None
        elif keys == "Return" and self.chooser and self.chooser["title"] == "Save As":
            name = self.chooser["entry"]
            path = Path(os.path.join(self.home, name[2:])) if name.startswith("~/") else (
                Path(name) if os.path.isabs(name) else self.home / name)
            if "~" in name[1:] or not path.parent.is_dir():
                self.alert = f"Error when getting information for file {path}: No such file or directory"
                return
            path.write_text(self.text)
            self.doc_name = path.name
            self.chooser = None


# ================================================================== the real run's plan, scripted

class StubJudge:
    """The screenshot judge: says yes, counts questions. Never a model."""

    def __init__(self):
        self.calls = []

    def judge(self, png, question, tree=None, final=True):
        self.calls.append((question, final))
        return {"ok": True, "reason": "stub: yes", "source": "stub"}

    def ask(self, png, question):
        return self.judge(png, question, final=False)


class Run3Planner:
    """Real run 3's milestones and turns. ``save_tail`` is the last step of the save turn; ``heals`` are the real
    replan_step answers (each counted). ``coerce`` applies what ``Planner.plan_milestones`` applies."""

    def __init__(self, save_tail=None, heals=(), coerce=True, save_turn=True, goal=GOAL):
        self.save_tail = save_tail or {"op": "state_assert", "checks": [{"kind": "file_exists", "arg": "~/lisbon.txt"}]}
        self.heals = list(heals)
        self.replans = 0
        self.turns = 0
        self.coerce = coerce
        self.save_turn = save_turn
        self.goal = goal

    def parse_script(self, raw):
        return parse_script(raw)

    def plan_milestones(self, goal, app, notes="", memory=None):
        ms = (Milestone(goal="Type the 3-line Lisbon packing list into the editor",
                        done_when={"role": "text", "text": LINES[2]}),
              Milestone(goal="Open the Save dialog and save the document as lisbon.txt in the home directory",
                        done_when="Is the document saved as lisbon.txt?"))
        return with_save_checks(goal, ms) if self.coerce else ms

    def next_steps(self, milestone, tree_summary, memory=None, notes="", interfaces=(), max_steps=5, app=""):
        self.turns += 1
        if milestone.goal.startswith("Type"):
            steps = [{"op": "find", "as": "editor", "target": {"role": "text"}},
                     {"op": "type", "ref": "editor", "text": "\n".join(LINES)}]
        elif self.save_turn:
            steps = [{"op": "key", "keys": "ctrl+shift+s"},
                     {"op": "find", "as": "name_entry", "target": {"role": "text", "name": "Name*"}},
                     {"op": "type", "ref": "name_entry", "text": "~/lisbon.txt"},
                     {"op": "key", "keys": "Return"},
                     self.save_tail]
        else:
            steps = [{"op": "screenshot_assert", "question": "Is it saved?"}]
        return parse_script({"app": "text-editor", "goal": milestone.goal, "steps": steps})

    def replan_step(self, script, index, reason, tree_summary="", **kw):
        self.replans += 1
        return self.heals[min(self.replans, len(self.heals)) - 1]

    def replace_step(self, script, index, step):
        d = script_to_dict(script)
        d["steps"] = [step_to_dict(s) for s in d["steps"]]
        d["steps"][index] = step_to_dict(step)
        return parse_script(d)


# The three heals of real run 3, in order.
REAL_HEALS = (
    {"op": "state_assert", "checks": [{"kind": "file_exists", "arg": "/root/lisbon.txt"}]},
    {"op": "state_assert", "checks": [{"kind": "tree", "arg": '{"role":"frame","name":"*lisbon.txt - Mousepad","state":"visible"}'}]},
    {"op": "state_assert", "checks": [{"kind": "tree", "arg": '{"role":"frame","name":"*lisbon.txt - Mousepad"}'}]},
)


def _harness(tmp_path, planner, sim=None, desktop_home=True, max_heals=3, max_turns=4, goal=GOAL):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    sb = tmp_path / "sandbox"
    sb.mkdir(exist_ok=True)
    sim = sim or SimMousepad(home)
    log = ActionLog(str(tmp_path / "log"))
    judge = StubJudge()
    deps = CUDeps(backend_factory=lambda slot: sim, grounder=None, judge=judge, planner=planner, procedures=None,
                  notes=None, log=log, max_heals=max_heals, max_turns=max_turns,
                  state_judge_factory=lambda sandbox, be: StateJudge(Sandbox(sb), be,
                                                                     desktop_home=str(home) if desktop_home else None),
                  sandbox_for=lambda job_id: Sandbox(sb))
    state = run_cu_job(build_cu_graph(deps), goal, "text-editor", "r3", max_heals=max_heals, max_model_calls=None)
    rows = []
    for p in (tmp_path / "log").glob("*.jsonl"):
        rows += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    return state, sim, rows, home


def _keys(sim):
    return [a[1] for a in sim.actions if a[0] == "key"]


# ================================================================== the redo, end to end

def test_real_run3_saves_once_no_heal_no_redo_no_dialog_left(tmp_path):
    planner = Run3Planner(heals=REAL_HEALS)
    state, sim, rows, home = _harness(tmp_path, planner)
    assert state["ok"] is True, state["error"]
    assert (home / "lisbon.txt").read_text() == "\n".join(LINES)
    assert planner.replans == 0 and state["attempt"] == 0  # ~/lisbon.txt is read where the app saved it
    assert _keys(sim).count("ctrl+shift+s") == 1 and _keys(sim).count("Return") == 1
    assert "~/lisbon.txt~" not in "".join(sim.entry_values)
    assert sim.chooser is None and sim.alert is None
    assert planner.turns == 2  # no turn after the file check passed
    goal_checks = [r for r in rows if r["summary"].endswith("goal file check passed: ~/lisbon.txt")]
    assert goal_checks, "the final verdict of a save goal is the file"


def test_pre_fix_redo_reproduces_without_desktop_home_and_heals_no_longer_redo_the_save(tmp_path):
    """The real failure chain with the sandbox-only check (no desktop home): every heal used to re-run
    ctrl+shift+s / type / Return. Now each heal resumes at the replaced check: one Save As, one Return."""
    planner = Run3Planner(heals=REAL_HEALS, coerce=False, goal="Type a 3-line packing list for Lisbon.")
    state, sim, rows, home = _harness(tmp_path, planner, desktop_home=False, goal="Type a 3-line packing list for Lisbon.")
    # heal 1 (/root/... outside the sandbox) fails again; heal 2 (a tree check with an inline state) now holds
    assert planner.replans == 2 and state["attempt"] == 2
    assert state["ok"] is True
    assert _keys(sim).count("ctrl+shift+s") == 1 and _keys(sim).count("Return") == 1
    assert sim.entry_values == ["", "~/lisbon.txt"] or "~/lisbon.txt~" not in "".join(sim.entry_values)
    assert sim.chooser is None and sim.alert is None
    resumes = [r for r in rows if "resuming the healed script at step 4" in r["summary"]]
    assert len(resumes) == 2
    assert not [r for r in rows if "re-executing the whole healed script" in r["summary"]]


def test_failed_step_after_a_proven_save_ends_the_milestone_without_heal(tmp_path):
    """A redundant, broken check after the save (an invalid tree selector) is not a reason to redo the save:
    the milestone's file_exists check passes, so the loop ends on it."""
    tail = {"op": "state_assert", "checks": [{"kind": "tree", "arg": '{"role":"frame","nam":"x"}'}]}
    planner = Run3Planner(save_tail=tail, heals=REAL_HEALS)
    state, sim, rows, home = _harness(tmp_path, planner)
    assert state["ok"] is True, state["error"]
    assert planner.replans == 0 and _keys(sim).count("ctrl+shift+s") == 1
    assert [r for r in rows if "although step 4 failed" in r["summary"] and "not healing" in r["summary"]]


def test_goal_file_written_before_the_job_does_not_count(tmp_path):
    """A ~/lisbon.txt left by an earlier run satisfies file_exists vacuously; the job-start record says it is stale."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "lisbon.txt").write_text("old")
    os.utime(home / "lisbon.txt", (1_000_000, 1_000_000))
    planner = Run3Planner(save_turn=False)
    state, sim, rows, _ = _harness(tmp_path, planner, max_heals=0, max_turns=2)
    assert state["ok"] is False
    assert "unchanged since the job started" in state["error"]
    assert (home / "lisbon.txt").read_text() == "old"


def test_final_judge_checks_the_file_not_the_window(tmp_path):
    """One-shot plan whose last step asserts the window title: with no file on disk the job fails."""

    class OneShot:
        def plan(self, goal, app, tree_summary=""):
            return parse_script({"app": "text-editor", "goal": goal, "steps": [
                {"op": "assert", "target": {"role": "frame", "name": "*Mousepad", "state": "visible"}}]})

        def replan_step(self, *a, **k):
            raise AssertionError("no heal expected")

    state, sim, rows, home = _harness(tmp_path, OneShot(), max_heals=0)
    assert state["ok"] is False and "goal file check failed" in state["error"]
    assert not (home / "lisbon.txt").exists()


def test_dialogs_the_job_opened_are_closed_at_job_end_and_the_users_are_kept(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sim = SimMousepad(home)
    sim.extra_dialog = ("dialog", "Preferences")  # the user's own, open before the job

    class LeavesOpen(Run3Planner):
        def plan_milestones(self, goal, app, notes="", memory=None):
            return (Milestone(goal="look at the Open dialog", done_when={"role": "dialog", "name": "Open File"}),)

        def next_steps(self, milestone, tree_summary, memory=None, notes="", interfaces=(), max_steps=5, app=""):
            return parse_script({"app": "text-editor", "goal": "look", "steps": [{"op": "key", "keys": "ctrl+o"}]})

    state, sim, rows, _ = _harness(tmp_path, LeavesOpen(), sim=sim, goal="Look at the Open dialog.")
    assert state["ok"] is True
    assert sim.chooser is None, "O's Open dialog is closed"
    assert sim.extra_dialog == ("dialog", "Preferences"), "the user's dialog is left alone"
    cleanup = [r["detail"]["cleanup"] for r in rows if r.get("detail", {}).get("cleanup")]
    assert cleanup == ["dialog 'Open File' (mousepad): clicked 'Cancel'"]


def test_cleanup_closes_the_error_alert_then_the_save_as_under_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sim = SimMousepad(home)
    sim.key("ctrl+shift+s")
    sim.type_text(None, "lisbon.txt~/lisbon.txt")
    sim.key("Return")
    assert sim.alert and sim.chooser
    done = sim.close_dialogs(keep=(), settle=0)
    assert done == ["alert 'Error' (mousepad): clicked 'OK'", "dialog 'Save As' (mousepad): clicked 'Cancel'"]
    assert sim.open_dialogs() == []


# ================================================================== executor: type vs set_value

def _runner(sim, tmp_path):
    return ScriptRunner(sim, ActionLog(str(tmp_path / "x")), sleep=lambda s: None)


def test_type_into_a_prefilled_file_name_entry_replaces_it(tmp_path):
    sim = SimMousepad(tmp_path)
    sim.doc_name = "lisbon.txt"
    rn = _runner(sim, tmp_path)
    res = rn.run(parse_script({"app": "m", "goal": "g", "steps": [
        {"op": "key", "keys": "ctrl+shift+s"},
        {"op": "find", "as": "n", "target": {"role": "text", "name": "Name*"}},
        {"op": "type", "ref": "n", "text": "~/lisbon.txt"}]}), "j")
    assert res.ok and sim.chooser["entry"] == "~/lisbon.txt"
    assert "replaced its value (set_value)" in res.steps[2].note
    assert ("set_value", "a0.1.0.0", "~/lisbon.txt") in sim.actions


def test_type_into_the_editor_still_inserts(tmp_path):
    sim = SimMousepad(tmp_path)
    sim.text = "first\n"
    res = _runner(sim, tmp_path).run(parse_script({"app": "m", "goal": "g", "steps": [
        {"op": "find", "as": "e", "target": {"role": "text"}},
        {"op": "type", "ref": "e", "text": "~/not/a/field.txt"}]}), "j")
    assert res.ok and sim.text == "first\n~/not/a/field.txt" and res.steps[1].note is None


def test_type_rule_for_single_line_entries_outside_file_dialogs(tmp_path):
    from o_engine.cu.backends.fake import FakeBackend

    def tree(text):
        return Node(id="r", role="application", name="app", children=[
            Node(id="w", role="dialog", name="Preferences", states=["visible"], children=[
                Node(id="e", role="text", name="Folder", text=text, states=["editable", "single line", "visible"])])])

    for prior, typed, replaced in (("/old/dir", "/new/dir", True), ("", "/new/dir", False),
                                   ("hello", " world", False), ("a.txt", "b.txt", True)):
        fb = FakeBackend(tree(prior))
        res = _runner(fb, tmp_path).run(parse_script({"app": "m", "goal": "g", "steps": [
            {"op": "find", "as": "f", "target": {"role": "text", "name": "Folder"}},
            {"op": "type", "ref": "f", "text": typed}]}), "j")
        assert res.ok
        assert fb.find(parse_script({"app": "m", "goal": "g", "steps": [
            {"op": "find", "as": "f", "target": {"role": "text", "name": "Folder"}}]}).steps[0].target).text == (
            typed if replaced else prior + typed), (prior, typed)


def test_resume_skips_the_steps_that_already_ran_and_refinds_refs(tmp_path):
    sim = SimMousepad(tmp_path)
    rn = _runner(sim, tmp_path)
    script = parse_script({"app": "m", "goal": "g", "steps": [
        {"op": "key", "keys": "ctrl+shift+s"},
        {"op": "find", "as": "n", "target": {"role": "text", "name": "Name*"}},
        {"op": "set_value", "ref": "n", "value": "~/a.txt"},
        {"op": "set_value", "ref": "n", "value": "~/b.txt"}]})
    rn.run(script, "j")
    sim.actions.clear()
    res = rn.run(script, "j", start=3)
    assert res.ok and [s.index for s in res.steps] == [3]
    assert sim.actions == [("set_value", "a0.1.0.0", "~/b.txt")]  # no ctrl+shift+s again


# ================================================================== StateJudge paths

def _sj(tmp_path, home=True):
    (tmp_path / "sb").mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    return StateJudge(Sandbox(tmp_path / "sb"), SimMousepad(tmp_path), desktop_home=str(tmp_path / "home") if home else None)


def test_state_judge_reads_tilde_and_absolute_paths_in_the_desktop_home(tmp_path):
    sj = _sj(tmp_path)
    (tmp_path / "home" / "lisbon.txt").write_text("Clothes")
    for arg in ("~/lisbon.txt", str(tmp_path / "home" / "lisbon.txt")):
        assert sj.verify((StateCheck(kind="file_exists", arg=arg),), "j").ok, arg
        assert sj.verify((StateCheck(kind="file_contains", arg=arg, expect="Clothes"),), "j").ok
    assert not sj.verify((StateCheck(kind="file_exists", arg="~/missing.txt"),), "j").ok
    # still contained: outside the home and the sandbox is refused; relative paths stay sandbox-relative
    v = sj.verify((StateCheck(kind="file_exists", arg="/etc/hostname"),), "j")
    assert not v.ok and "outside sandbox" in v.reason
    v = sj.verify((StateCheck(kind="file_contains", arg="~/../../etc/passwd", expect="root"),), "j")
    assert not v.ok and "outside sandbox" in v.reason
    (tmp_path / "home" / ".ssh").mkdir()
    (tmp_path / "home" / ".ssh" / "id_rsa").write_text("KEY")
    v = sj.verify((StateCheck(kind="file_contains", arg="~/.ssh/id_rsa", expect="KEY"),), "j")
    assert not v.ok and "private folder" in v.reason
    assert sj.stat(["~/lisbon.txt"])["~/lisbon.txt"][1] == len("Clothes")


def test_state_judge_without_a_desktop_home_says_so_for_tilde(tmp_path):
    v = _sj(tmp_path, home=False).verify((StateCheck(kind="file_exists", arg="~/lisbon.txt"),), "j")
    assert not v.ok and "no desktop home" in v.reason


def test_tree_check_accepts_an_inline_state(tmp_path):
    sj = _sj(tmp_path)
    ok = sj.verify((StateCheck(kind="tree", arg='{"role":"frame","name":"*Mousepad","state":"visible"}'),), "j")
    assert ok.ok, ok.reason
    no = sj.verify((StateCheck(kind="tree", arg='{"role":"frame","name":"*Mousepad","state":"checked"}'),), "j")
    assert not no.ok and "checked" in no.reason


# ================================================================== planner: done_when from state

def test_save_targets_and_milestone_checks():
    assert save_targets(GOAL) == ["~/lisbon.txt"]
    assert save_targets("export the sheet to /tmp/report.pdf.") == ["/tmp/report.pdf"]
    assert save_targets("save time by typing faster") == [] and save_targets("save it in notes.md") == []
    ms = (Milestone(goal="type the list", done_when="Is the list there?"),
          Milestone(goal="save the document as lisbon.txt", done_when={"role": "frame", "name": "*lisbon.txt*"}),
          Milestone(goal="verify", done_when="Is it saved?"))
    out = with_save_checks(GOAL, ms)
    assert out[0] == ms[0] and out[2] == ms[2]
    assert out[1].checks == (StateCheck(kind="file_exists", arg="~/lisbon.txt"),)
    assert with_save_checks(GOAL, out) == out  # already checked: unchanged
    assert with_save_checks("type a list", ms) == ms


def test_milestone_done_when_accepts_state_checks():
    m = Milestone.model_validate({"goal": "save", "done_when": {"checks": [{"kind": "file_exists", "arg": "~/a.txt"}]}})
    assert m.checks == (StateCheck(kind="file_exists", arg="~/a.txt"),)
    assert Milestone.model_validate({"goal": "g", "done_when": [{"kind": "file_exists", "arg": "~/a.txt"}]}).checks
    assert Milestone(goal="g", done_when="q?").checks is None
    with pytest.raises(Exception):
        Milestone.model_validate({"goal": "g", "done_when": {"checks": []}})


def test_planner_prompts_state_the_rules():
    assert "set_value REPLACES a field's whole value" in STATIC_PREFIX
    assert "type INSERTS at the cursor" in STATIC_PREFIX
    assert "state_assert file_exists on the target path" in STATIC_PREFIX
    assert "never re-open a dialog or redo a save" in STATIC_PREFIX
    assert '{"checks": [{"kind": "file_exists"' in MILESTONES_SYSTEM_PROMPT
