"""Regression tests for the 2026-11-05 real computer-use run (no model is ever called; ``claude`` is a fake script).

1. duplicate app windows: WM on the isolated display, idempotent launch, known dialogs dismissed, focused tree;
2. cost: per-job paid-call cap (cuMaxModelCalls), judge cache, tree-text/deferral policy, JobState counters;
3. /provision ``workspace`` only for a granted folder;
4. the screenshot failure names the failing command and its stderr.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from o_engine import display as display_mod
from o_engine.actionlog import ActionLog
from o_engine.cu import meter
from o_engine.cu.backends import atspi as atspi_mod
from o_engine.cu.backends.base import BackendError, Node, focused_roots
from o_engine.cu.backends.fake import FakeBackend
from o_engine.cu.executor import ScriptRunner
from o_engine.cu.graph import CUDeps, JOBSTATE_FIELDS, build_cu_graph, run_cu_job
from o_engine.cu.judge import ClaudeCodeJudge, Judge, JudgeVerdict, tree_text_verdict
from o_engine.cu.planner import ClaudeCodeClient, ModelClient, Planner
from o_engine.cu.schema import parse_script
from o_engine.display import IsolatedDisplay
from o_engine.schema import SchemaViolation
from o_engine.server import EngineSettings, _granted_folders_env, create_app, resolve_workspace

TOKEN = "t-realrun"


# ------------------------------------------------------------------ helpers

def _mousepad(app_id="a0", frame_states=("active", "visible"), text="", extra=()):
    return Node(id=app_id, role="application", name="mousepad", children=[
        Node(id=f"{app_id}.0", role="frame", name="Untitled 1 - Mousepad", states=list(frame_states), children=[
            Node(id=f"{app_id}.0.0", role="text", text=text, states=["editable", "visible"]),
        ]),
        *extra,
    ])


def _fake_claude(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """An executable stand-in for ``claude -p --output-format json``: appends one line per call to a counter."""
    count = tmp_path / "calls"
    exe = tmp_path / "claude"
    exe.write_text(f"#!{sys.executable}\nimport json, sys\nprompt = sys.stdin.read()\n"
                   f"open({str(count)!r}, 'a').write('1\\n')\n{body}\n")
    exe.chmod(0o755)
    return exe, count


def _calls(count: Path) -> int:
    return len(count.read_text().split()) if count.exists() else 0


class _Down:
    """A local backend that is not running (vLLM down)."""

    name = "local"

    def complete(self, system, user):
        raise ConnectionError("connection refused")

    def judge(self, png, question):
        raise ConnectionError("connection refused")


class _Counting:
    name = "claude"

    def __init__(self, ok=True):
        self.calls = 0
        self.ok = ok

    def judge(self, png, question):
        self.calls += 1
        return JudgeVerdict(ok=self.ok, reason="counted", source="claude")


# ================================================================== 1. duplicate windows

def test_focused_roots_keeps_only_the_app_with_the_active_window_and_puts_that_window_first():
    behind = _mousepad("a0", frame_states=("visible",), text="typed into the hidden copy")
    dialog = Node(id="a1.9", role="dialog", name="Restore", states=["active", "visible"])
    front = _mousepad("a1", frame_states=("visible",), extra=(dialog,))
    roots = focused_roots([behind, front])
    assert [r.id for r in roots] == ["a1"]
    assert [w.id for w in roots[0].children] == ["a1.9", "a1.0"]
    # no window manager -> nothing is active -> unchanged
    assert [r.id for r in focused_roots([behind, _mousepad("a1", frame_states=("visible",))])] == ["a0", "a1"]


def test_atspi_tree_is_the_focused_app_and_tree_all_is_everything(monkeypatch):
    b = atspi_mod.AtspiBackend.__new__(atspi_mod.AtspiBackend)
    b.focused_only = True
    roots = [_mousepad("a0", frame_states=("visible",)), _mousepad("a1")]
    monkeypatch.setattr(b, "_roots", lambda window: list(roots), raising=False)
    assert [r.id for r in b.tree()] == ["a1"]
    assert [r.id for r in b.tree_all()] == ["a0", "a1"]


def _restore_dialog(app_id="a0"):
    return Node(id=f"{app_id}.5", role="alert", name="Session Restore", states=["active", "modal", "visible"], children=[
        Node(id=f"{app_id}.5.0", role="label",
             name="It seems that the previous session did not end normally. Do you want to restore the available data?"),
        Node(id=f"{app_id}.5.1", role="push button", name="Restore"),
        Node(id=f"{app_id}.5.2", role="push button", name="Cancel"),
    ])


def test_known_restore_dialog_is_declined_through_the_tree():
    other = Node(id="a0.6", role="dialog", name="Preferences", children=[Node(id="a0.6.0", role="push button", name="Close")])
    b = FakeBackend([_mousepad(extra=(_restore_dialog(), other))])
    done = b.dismiss_known_dialogs()
    assert done == ["mousepad session restore (mousepad): clicked 'Cancel'"]
    assert b.actions == [("click", "a0.5.2")]  # the unrelated dialog is untouched


def test_runner_dismisses_a_restore_dialog_before_the_step_and_logs_it(tmp_path):
    b = FakeBackend([_mousepad(extra=(_restore_dialog(),))])
    log = ActionLog(str(tmp_path))
    rn = ScriptRunner(b, log, sleep=lambda s: None)
    script = parse_script({"app": "mousepad", "goal": "g", "steps": [{"op": "key", "keys": "ctrl+s"}]})
    assert rn.run(script, "j1").ok
    assert b.actions[:2] == [("click", "a0.5.2"), ("key", "ctrl+s")]
    lines = (tmp_path / "engine-actions.jsonl").read_text()
    assert "dismissed dialog before step 0: mousepad session restore" in lines


def test_launch_of_a_running_app_focuses_it_instead_of_starting_a_second_instance(tmp_path):
    b = FakeBackend([_mousepad()])
    started = []
    rn = ScriptRunner(b, ActionLog(str(tmp_path)), popen=lambda *a, **k: started.append(a) or SimpleNamespace(pid=1))
    run = rn.run(parse_script({"app": "mousepad", "goal": "g", "steps": [{"op": "launch", "cmd": "mousepad"}]}), "j")
    assert run.ok and started == []
    assert ("invoke", "a0.0", "focus") in b.actions
    assert run.steps[0].note.startswith("already running: focused the existing mousepad window")
    # another app, or a cmd that opens a file, still launches
    for cmd in ("gedit", "mousepad notes.txt"):
        run = rn.run(parse_script({"app": "x", "goal": "g", "steps": [{"op": "launch", "cmd": cmd}]}), "j")
        assert run.ok
    assert len(started) == 2


def test_isolated_display_starts_a_window_manager_after_xvfb(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(display_mod.shutil, "which", lambda n, *a, **k: f"/usr/bin/{n}" if n in ("Xvfb", "openbox") else None)
    monkeypatch.setattr(display_mod, "X11_SOCKET_DIR", tmp_path / "x11")
    monkeypatch.setattr(display_mod.time, "sleep", lambda s: None)
    calls = []

    class Proc:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def popen(args, **kw):
        calls.append((list(args), kw.get("env", {}).get("DISPLAY")))
        if args[0] == "Xvfb":
            (tmp_path / ".X97-lock").write_text("1")
        return Proc()

    monkeypatch.setattr(display_mod.subprocess, "Popen", popen)
    d = IsolatedDisplay(":97", lock_dir=tmp_path)
    d.start()
    assert [c[0][0] for c in calls] == ["Xvfb", "/usr/bin/openbox"]
    assert calls[1][1] == ":97"  # the WM runs on the isolated display, never the host's
    assert d.wm_name == "openbox"
    d.stop()


def test_existing_display_with_a_window_manager_gets_none_and_missing_wm_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    (tmp_path / ".X96-lock").write_text("1")
    monkeypatch.setattr(display_mod, "X11_SOCKET_DIR", tmp_path / "none")
    monkeypatch.setattr(display_mod.shutil, "which", lambda n, *a, **k: f"/usr/bin/{n}" if n in ("Xvfb", "openbox", "xprop") else None)
    monkeypatch.setattr(display_mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x1"))
    started = []
    monkeypatch.setattr(display_mod.subprocess, "Popen", lambda *a, **k: started.append(a))
    d = IsolatedDisplay(":96", lock_dir=tmp_path)
    d.start()
    assert started == [] and d.running and d.wm_reason == "display already has a window manager"
    monkeypatch.setattr(display_mod.shutil, "which", lambda n, *a, **k: "/usr/bin/Xvfb" if n == "Xvfb" else None)
    d2 = IsolatedDisplay(":96", lock_dir=tmp_path)
    d2.start()
    assert started == [] and d2.wm_reason.startswith("no window manager installed")


# ================================================================== 4. screenshot error

def _atspi(monkeypatch):
    monkeypatch.setattr(atspi_mod, "_IMPORT_ERROR", None)
    return atspi_mod.AtspiBackend(display=":99", allow_pixels=True)


def test_screenshot_failure_reports_the_failing_command_and_stderr(monkeypatch):
    b = _atspi(monkeypatch)
    monkeypatch.setattr(atspi_mod.shutil, "which", lambda n, *a, **k: "/usr/bin/import" if n == "import" else None)
    monkeypatch.setattr(atspi_mod.subprocess, "run", lambda argv, **k: SimpleNamespace(
        returncode=1, stdout=b"", stderr=b"import: unable to open X server `:99' @ error/import.c/ImportImageCommand/346.\n"))
    with pytest.raises(BackendError) as e:
        b.screenshot()
    msg = str(e.value)
    assert "install ImageMagick" not in msg
    assert "screenshot of :99 failed: `import -window root png:-` exited 1: import: unable to open X server" in msg


def test_screenshot_without_any_tool_still_says_what_to_install(monkeypatch):
    b = _atspi(monkeypatch)
    monkeypatch.setattr(atspi_mod.shutil, "which", lambda n, *a, **k: None)
    with pytest.raises(BackendError, match="install ImageMagick"):
        b.screenshot()


# ================================================================== 2. cost

def test_paid_calls_stop_at_the_cap_before_the_cli_runs_and_cost_is_summed(tmp_path):
    exe, count = _fake_claude(tmp_path, "print(json.dumps({'result': '{}', 'total_cost_usd': 0.05}))")
    client = ModelClient(_Down(), ClaudeCodeClient(str(exe)))
    m = meter.JobMeter("j", max_paid=2)
    with meter.metered(m):
        client.complete("s", "u")
        client.complete("s", "u")
        with pytest.raises(meter.ModelCallCapReached, match="2 of cuMaxModelCalls=2"):
            client.complete("s", "u")
    assert _calls(count) == 2
    assert m.snapshot()["by_kind"] == {"claude:planner": 2} and m.refused == 1
    assert m.cost_usd == pytest.approx(0.10)
    client.complete("s", "u")  # outside a job nothing is capped
    assert _calls(count) == 3


def test_judge_caches_identical_questions_within_a_job():
    paid = _Counting()
    j = Judge(_Down(), paid)
    with meter.metered(meter.JobMeter("j")) as m:
        assert j.judge(b"png", "Q?").ok and j.judge(b"png", "Q?").ok
        j.judge(b"other", "Q?")
    assert paid.calls == 2 and m.cache_hits == 1


def test_non_final_checks_use_tree_text_or_defer_and_only_the_final_check_pays():
    paid = _Counting()
    j = Judge(_Down(), paid)
    tree = [_mousepad(text="Passport\nSunscreen\nPhone charger\n")]
    with meter.metered(meter.JobMeter("j")) as m:
        v = j.judge(b"p", "Does the text area show exactly three lines: 'Passport', 'Sunscreen', 'Phone charger'?",
                    tree=lambda: tree, final=False)
        assert (v.ok, v.source) == (True, "tree-text")
        v = j.judge(b"p", "Does it show 'Hat'?", tree=tree, final=False)
        assert (v.ok, v.source) == (False, "tree-text")
        v = j.judge(b"p", "Is the list nicely formatted?", tree=tree, final=False)
        assert (v.ok, v.source) == (True, "deferred") and m.deferred == 1
        assert paid.calls == 0
        assert j.judge(b"p", "Is the goal done?", tree=tree, final=True).source == "claude"
    assert paid.calls == 1


def test_tree_text_leaves_negations_and_unquoted_questions_undecided():
    tree = [_mousepad(text="Passport\n")]
    assert tree_text_verdict("Is the editor free of 'Passport'? No 'Passport' at all?", tree) is None
    assert tree_text_verdict("Is the document saved?", tree) is None
    assert tree_text_verdict("Is the text area completely empty?", [_mousepad(text="")]).ok is True
    assert tree_text_verdict("Is the text area not empty?", tree) is None


MILESTONES = json.dumps({"milestones": [{"goal": "press OK", "done_when": {"role": "label", "name": "Done"}}]})
TURN = json.dumps({"app": "editor", "goal": "press OK", "steps": [
    {"op": "find", "as": "ok", "target": {"role": "push button", "name": "OK"}}, {"op": "click", "ref": "ok"}]})
ROUTER = (f"out = {MILESTONES!r} if 'ordered milestones' in prompt else {TURN!r}\n"
          "print(json.dumps({'result': out, 'total_cost_usd': 0.01}))")


def _loop_job(tmp_path, cap):
    """A milestone that never completes (vLLM down, every replan paid): the real run's loop shape."""
    exe, count = _fake_claude(tmp_path, ROUTER)
    tree = [Node(id="a0", role="application", name="editor", children=[
        Node(id="a0.0", role="frame", name="Editor", states=["active"], children=[
            Node(id="a0.0.0", role="push button", name="OK")])])]
    backend = FakeBackend(tree)
    paid_judge = _Counting(ok=False)
    deps = CUDeps(backend_factory=lambda slot: backend, grounder=None, judge=Judge(_Down(), paid_judge),
                  planner=Planner(ModelClient(_Down(), ClaudeCodeClient(str(exe)))), procedures=None, notes=None,
                  log=ActionLog(str(tmp_path / "log")), max_heals=3, max_turns=20)
    state = run_cu_job(build_cu_graph(deps), "press OK until done", "editor", "job-cap", max_heals=3, max_model_calls=cap)
    return state, _calls(count), paid_judge


def test_a_looping_job_stops_at_cu_max_model_calls_and_reports_counters(tmp_path):
    state, cli_calls, _ = _loop_job(tmp_path, cap=4)
    assert set(state) == set(JOBSTATE_FIELDS)
    assert state["ok"] is False and state["done"] is True
    assert "model-call cap reached (4 of cuMaxModelCalls=4" in state["error"]
    assert state["model_calls"]["paid"] == cli_calls == 4  # 1 milestone plan + 3 turns; no heal, no 20 turns
    assert state["turns"] == 3
    assert state["cost_usd"] == pytest.approx(0.04)
    assert state["attempt"] == 0  # at the cap the judge routes to report, never to a paid heal


def test_a_cap_hit_inside_a_node_ends_the_job_as_a_report(tmp_path):
    state, cli_calls, _ = _loop_job(tmp_path, cap=1)
    assert cli_calls == 1 and state["ok"] is False
    assert state["error"].startswith("stopped: engine-side model-call cap reached (1 of cuMaxModelCalls=1")
    assert state["model_calls"]["refused"] == 1


def test_cu_max_model_calls_setting(monkeypatch):
    assert EngineSettings().cu_max_model_calls == 12
    monkeypatch.setenv("O_CU_MAX_MODEL_CALLS", "5")
    assert EngineSettings.from_env().cu_max_model_calls == 5


# ================================================================== 3. provision workspace

def test_granted_folders_env_parsing(tmp_path):
    assert _granted_folders_env("") == []
    assert _granted_folders_env(json.dumps([str(tmp_path), "relative/x", 3])) == [str(tmp_path)]
    assert _granted_folders_env("not json [") == []


def test_resolve_workspace_accepts_only_a_granted_folder_itself(tmp_path):
    granted = tmp_path / "proj"
    (granted / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(granted)
    assert resolve_workspace(str(granted), [str(granted)]) == granted.resolve()
    assert resolve_workspace(str(link), [str(granted)]) == granted.resolve()  # same real folder
    for bad in (str(granted / "sub"), str(tmp_path), "proj", str(granted / ".." / "other"), "", None, 5):
        with pytest.raises(SchemaViolation):
            resolve_workspace(bad, [str(granted)])
    with pytest.raises(SchemaViolation):
        resolve_workspace(str(granted), [])  # default: nothing granted


class _Deps:
    def __init__(self):
        self.job_workspaces: dict = {}
        self.ran_in: dict = {}
        self.graph = object()
        self.learning = None

    def run_job(self, graph, request, job_id, mode, approve_waiter):
        self.ran_in[job_id] = self.job_workspaces.get(job_id)
        return {"id": job_id, "label": request.label, "phase": "report", "attempt": 0, "max_attempts": 1,
                "cached": False, "steps": [], "done": True, "ok": True}


def _client(tmp_path, granted):
    settings = EngineSettings(token=TOKEN, data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sandbox"),
                              screenpipe_db=str(tmp_path / "x.db"), vllm_url="http://127.0.0.1:1/v1",
                              granted_folders=granted)
    deps = _Deps()
    return TestClient(create_app(settings, deps_factory=lambda s: deps)), deps


REQ = {"label": "setup", "steps": [{"target_directory": ".", "command_string": "true", "timeout_seconds": 5}]}


def _wait(c, job_id):
    import time

    for _ in range(250):
        body = c.get(f"/jobs/{job_id}", headers={"x-o-token": TOKEN}).json()
        if body.get("done"):
            return body
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_provision_workspace_runs_in_the_granted_folder(tmp_path):
    granted = tmp_path / "proj"
    granted.mkdir()
    c, deps = _client(tmp_path, [str(granted)])
    with c:
        r = c.post("/provision", json={**REQ, "workspace": str(granted)}, headers={"x-o-token": TOKEN})
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        _wait(c, job_id)
        plain = c.post("/provision", json=REQ, headers={"x-o-token": TOKEN}).json()["job_id"]
        _wait(c, plain)
    assert deps.ran_in[job_id] == granted.resolve()
    assert deps.ran_in[plain] is None  # an ordinary job keeps <sandbox_root>/<job_id>
    assert not (tmp_path / "sandbox" / job_id).exists() and (tmp_path / "sandbox" / plain).is_dir()
    assert deps.job_workspaces == {}


def test_provision_workspace_outside_the_grants_is_refused(tmp_path):
    granted = tmp_path / "proj"
    (granted / "sub").mkdir(parents=True)
    c, deps = _client(tmp_path, [str(granted)])
    with c:
        for ws in (str(tmp_path), str(granted / "sub"), "/etc", "proj"):
            r = c.post("/provision", json={**REQ, "workspace": ws}, headers={"x-o-token": TOKEN})
            assert r.status_code == 422, (ws, r.text)
    assert deps.ran_in == {}
    c2, deps2 = _client(tmp_path, [])
    with c2:
        assert c2.post("/provision", json={**REQ, "workspace": str(granted)}, headers={"x-o-token": TOKEN}).status_code == 422


def test_engine_deps_sandbox_dir_uses_the_workspace_only_for_that_job(tmp_path):
    from o_engine.server import EngineDeps

    d = EngineDeps.__new__(EngineDeps)
    d.sandbox_root = tmp_path / "sandbox"
    d.job_workspaces = {"j1": tmp_path / "proj"}
    assert d.sandbox_dir("j1") == tmp_path / "proj"
    assert d.sandbox_dir("j2") == tmp_path / "sandbox" / "j2"
