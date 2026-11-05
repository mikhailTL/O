import json

import pytest
from pydantic import ValidationError

from o_engine.actionlog import ActionLog
from o_engine.cu.backends.base import Node
from o_engine.cu.backends.fake import FakeBackend
from o_engine.cu.feasibility import (
    PROBE_OPS,
    FeasibilityGate,
    FeasibilityVerdict,
    Probe,
    ProbeResult,
    check_feasibility,
    default_probes,
)
from o_engine.executor import Sandbox, SandboxEscape, StepResult


class RecordingSandbox:
    """Confines paths like ``Sandbox.resolve``; records every run and never executes anything."""

    def __init__(self, root):
        self.root = root.resolve()
        self.calls = []

    def resolve(self, p):
        return Sandbox.resolve(self, p)

    def run(self, step, index=0, env_extra=None, isolated_display=False):
        self.calls.append(step)
        return StepResult(index=index, command=step.command_string, cwd=str(self.root), exit_code=0,
                          stdout="ok\n", stderr="", elapsed_ms=1, timed_out=False)


class StubModel:
    """``complete_json`` returns the canned reply or raises; records every prompt."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.reply


def _tree():
    return [Node(id="w1", role="frame", name="Text Editor", children=[
        Node(id="b1", role="push button", name="Save"),
    ])]


@pytest.fixture
def backend():
    return FakeBackend(_tree())


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "notes.txt").write_text("hello\n")
    return Sandbox(tmp_path)


@pytest.fixture
def log(tmp_data_dir):
    return ActionLog(tmp_data_dir)


def _entries(log):
    if not log.jsonl.exists():
        return []
    return [json.loads(line) for line in log.jsonl.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------ schema

def test_probe_ops_are_read_only_only():
    assert set(PROBE_OPS) == {"tree", "screenshot", "ls", "read_file", "which"}
    for op in PROBE_OPS:
        Probe(op=op, arg="x")


@pytest.mark.parametrize("op", ["click", "type", "key", "set_value", "invoke", "rm"])
def test_probe_rejects_non_read_only_ops(op):
    with pytest.raises(ValidationError):
        Probe(op=op, arg="x")


def test_probe_is_frozen_and_forbids_extras():
    p = Probe(op="ls", arg="/tmp")
    with pytest.raises(ValidationError):
        p.op = "tree"
    with pytest.raises(ValidationError):
        Probe(op="ls", arg="/tmp", cmd="rm -rf /")


def test_verdict_is_frozen_with_probe_tuple():
    v = FeasibilityVerdict(feasible=True, reason="ok", probes=(ProbeResult(op="tree", arg="", ok=True, output="x"),))
    assert isinstance(v.probes, tuple)
    with pytest.raises(ValidationError):
        v.feasible = False
    with pytest.raises(ValidationError):
        FeasibilityVerdict(feasible=True, reason="ok", probes=(), extra=1)


# ------------------------------------------------------------------ gate

def test_infeasible_goal_records_no_actions_and_logs_note(backend, sandbox, log):
    model = StubModel(reply='{"feasible": false, "reason": "no spreadsheet app is installed or open"}')
    gate = FeasibilityGate(model, backend, sandbox, log=log)
    verdict = gate.check("Enter 42 in cell A1 of the spreadsheet", "no-such-spreadsheet-app-xyz", "job-1")
    assert verdict.feasible is False
    assert "spreadsheet" in verdict.reason
    # probes ran read-only: the backend saw no click/type/key/set_value/invoke
    assert backend.actions == []
    # the model was asked exactly once, with the probe results in the prompt
    assert len(model.calls) == 1
    _system, user = model.calls[0]
    assert "Text Editor" in user  # tree probe output
    assert "no-such-spreadsheet-app-xyz" in user  # which probe
    ops = [p.op for p in verdict.probes]
    assert "tree" in ops and "which" in ops
    which = next(p for p in verdict.probes if p.op == "which")
    assert which.ok is False
    notes = [e for e in _entries(log) if e["kind"] == "note"]
    assert len(notes) == 1
    assert notes[0]["detail"]["job_id"] == "job-1"
    assert notes[0]["detail"]["feasible"] is False


def test_feasible_goal_is_feasible_and_logs_nothing(backend, sandbox, log):
    model = StubModel(reply='{"feasible": true, "reason": "editor is open"}')
    gate = FeasibilityGate(model, backend, sandbox, log=log)
    verdict = gate.check("Save the document", "text-editor", "job-2")
    assert verdict.feasible is True
    assert verdict.reason == "editor is open"
    assert backend.actions == []
    assert [e for e in _entries(log) if e["kind"] == "note"] == []


def test_model_exception_never_blocks(backend, sandbox, log):
    model = StubModel(error=RuntimeError("vllm down"))
    gate = FeasibilityGate(model, backend, sandbox, log=log)
    verdict = gate.check("Save the document", "text-editor", "job-3")
    assert verdict.feasible is True
    assert verdict.reason == "gate unavailable"
    assert backend.actions == []


@pytest.mark.parametrize("reply", ["not json", "[1,2]", '{"feasible": "yes"}', '{"reason": "x"}', '{"feasible": false, "extra": 1}'])
def test_malformed_model_reply_is_gate_unavailable(backend, sandbox, reply):
    gate = FeasibilityGate(StubModel(reply=reply), backend, sandbox)
    verdict = gate.check("Save", "text-editor", "job-4")
    assert verdict.feasible is True
    assert verdict.reason == "gate unavailable"


def test_custom_probes_run_through_sandbox_and_backend(backend, sandbox, tmp_path):
    model = StubModel(reply='{"feasible": true, "reason": "ok"}')
    probes = (
        Probe(op="ls", arg=str(tmp_path)),
        Probe(op="read_file", arg=str(tmp_path / "notes.txt")),
        Probe(op="screenshot"),
        Probe(op="which", arg="sh"),
    )
    gate = FeasibilityGate(model, backend, sandbox, probes=probes)
    verdict = gate.check("Open notes", "text-editor", "job-5")
    by_op = {p.op: p for p in verdict.probes}
    assert by_op["ls"].ok and "notes.txt" in by_op["ls"].output
    assert by_op["read_file"].ok and "hello" in by_op["read_file"].output
    assert by_op["screenshot"].ok and "bytes" in by_op["screenshot"].output
    assert by_op["which"].ok and by_op["which"].output.strip().endswith("sh")
    assert (tmp_path / "notes.txt").read_text() == "hello\n"
    assert backend.actions == []


def test_probe_failures_do_not_raise(backend, log):
    model = StubModel(reply='{"feasible": true, "reason": "ok"}')
    broken = FakeBackend(_tree())
    broken.tree = lambda window=None: (_ for _ in ()).throw(RuntimeError("tree boom"))  # type: ignore[assignment]
    gate = FeasibilityGate(model, broken, sandbox=None, log=log)
    verdict = gate.check("Save", "text-editor", "job-6")
    assert verdict.feasible is True
    by_op = {p.op: p for p in verdict.probes}
    assert by_op["tree"].ok is False and "tree boom" in by_op["tree"].output
    assert by_op["which"].ok is False and "sandbox" in by_op["which"].output


def test_default_probes_and_check_feasibility_helper(backend, sandbox):
    assert [p.op for p in default_probes("my-app")] == ["tree", "which"]
    assert default_probes("my-app")[1].arg == "my-app"
    none_verdict = check_feasibility(None, "goal", "app", "job-7")
    assert none_verdict.feasible is True and none_verdict.probes == ()
    gate = FeasibilityGate(StubModel(reply='{"feasible": false, "reason": "nope"}'), backend, sandbox)
    assert check_feasibility(gate, "goal", "app", "job-8").feasible is False


# ------------------------------------------------------------------ probe confinement (X1)

@pytest.mark.parametrize("op,arg", [("read_file", "/etc/passwd"), ("ls", "/etc"), ("read_file", "../x"), ("ls", "../../")])
def test_path_probe_outside_sandbox_is_refused_without_running(backend, tmp_path, op, arg):
    sb = RecordingSandbox(tmp_path)
    model = StubModel(reply='{"feasible": true, "reason": "ok"}')
    gate = FeasibilityGate(model, backend, sb, probes=(Probe(op=op, arg=arg),))
    verdict = gate.check("goal", "app", "job-x1")
    (res,) = verdict.probes
    assert res.ok is False and res.output == "probe not allowed"
    assert sb.calls == []


def test_path_probe_inside_sandbox_runs_and_is_allowed(backend, tmp_path):
    sb = RecordingSandbox(tmp_path)
    model = StubModel(reply='{"feasible": true, "reason": "ok"}')
    gate = FeasibilityGate(model, backend, sb, probes=(Probe(op="ls", arg="sub"), Probe(op="read_file", arg=str(tmp_path / "n.txt")), Probe(op="which", arg="gedit")))
    verdict = gate.check("goal", "app", "job-x2")
    assert all(r.ok for r in verdict.probes)
    assert len(sb.calls) == 3
    from o_engine.cu.probes import is_allowed_probe
    for step in sb.calls:
        assert is_allowed_probe(step.command_string)


def test_every_shell_probe_is_checked_against_allow_list(backend, tmp_path, monkeypatch):
    import o_engine.cu.feasibility as feas
    sb = RecordingSandbox(tmp_path)
    monkeypatch.setitem(feas._SHELL, "which", lambda arg: f"cat {arg} | sh")
    gate = FeasibilityGate(StubModel(reply='{"feasible": true, "reason": "ok"}'), backend, sb,
                           probes=(Probe(op="which", arg="x"),))
    (res,) = gate.check("goal", "app", "job-x3").probes
    assert res.ok is False and res.output == "probe not allowed"
    assert sb.calls == []


def test_each_probe_is_logged_as_tool_and_result(backend, sandbox, log):
    gate = FeasibilityGate(StubModel(reply='{"feasible": true, "reason": "ok"}'), backend, sandbox, log=log)
    gate.check("Save the document", "text-editor", "job-x4")
    entries = _entries(log)
    tools = [e for e in entries if e["kind"] == "tool"]
    results = [e for e in entries if e["kind"] == "result"]
    assert [e["detail"]["op"] for e in tools] == ["tree", "which"]
    assert [e["detail"]["op"] for e in results] == ["tree", "which"]
    for e in tools + results:
        assert e["summary"].strip()
        assert e["detail"]["job_id"] == "job-x4"
    assert results[1]["detail"]["ok"] is False


# ------------------------------------------------------------------ X2: one tree flattener

def test_tree_probe_uses_the_shared_summarize_tree(backend, sandbox):
    import o_engine.cu.feasibility as feas
    from o_engine.cu.treeutil import summarize_tree

    assert not hasattr(feas, "_flatten_tree")
    model = StubModel(reply='{"feasible": true, "reason": "ok"}')
    verdict = FeasibilityGate(model, backend, sandbox, probes=(Probe(op="tree"),)).check("g", "app", "job-x")
    tree_probe = verdict.probes[0]
    assert tree_probe.ok and tree_probe.output == summarize_tree(backend.tree())
    # an empty tree still yields a non-empty probe output
    class Empty:
        def tree(self, window=None):
            return []
    v2 = FeasibilityGate(model, Empty(), sandbox, probes=(Probe(op="tree"),)).check("g", "app", "job-y")
    assert v2.probes[0].output == "(empty tree)"
