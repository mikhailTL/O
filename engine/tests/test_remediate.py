import json

import pytest

from o_engine.actionlog import ActionLog
from o_engine.executor import Patch
from o_engine.remediate import Remediator, parse_patch
from o_engine.schema import ProvisionRequest, ProvisionStep

SCRIPT = {"language": "python", "code": "print(1)", "target_directory": "proj", "timeout_seconds": 5}


class StubParser:
    def __init__(self, steps):
        self.steps = steps
        self.calls = []

    def parse(self, text, goal=""):
        self.calls.append((text, goal))
        return ProvisionRequest(label=goal or "x", steps=self.steps)


def _entries(log):
    return [json.loads(line) for line in log.jsonl.read_text().splitlines()]


def _state(req):
    return {
        "id": "j", "label": "set up project", "request": req, "attempt": 0, "max_attempts": 3,
        "steps": [{"index": 0, "command": "python a.py", "cwd": "proj", "exit_code": 1,
                   "stdout_tail": "", "stderr_tail": "bash: python: command not found\n", "elapsed_ms": 1}],
        "verify": None, "patches_applied": 0, "cached": False, "fingerprint": req.fingerprint("linux"),
    }


def test_parse_patch_none_is_not_a_patch():
    assert parse_patch(None) is None
    assert parse_patch("echo hi") is None
    assert parse_patch("patch:a.py|||x|||y") == Patch(path="a.py", find="x", replace="y")


def test_plan_with_script_step_returns_request_not_patches(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    parser = StubParser([{"script": SCRIPT}, {"target_directory": "proj", "command_string": "patch:a.py|||x|||y", "timeout_seconds": 5}])
    req = ProvisionRequest(label="x", steps=[{"target_directory": "proj", "command_string": "python a.py", "timeout_seconds": 5}])
    rem = Remediator(parser, object(), log, apply_patch=lambda sb, p: None)
    plan = rem.plan(_state(req))
    assert isinstance(plan, ProvisionRequest)


def test_remediate_rejects_script_step_with_note_and_keeps_command_steps(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    parser = StubParser([{"script": SCRIPT}, {"target_directory": "proj", "command_string": "python3 a.py", "timeout_seconds": 5}])
    req = ProvisionRequest(label="x", steps=[{"target_directory": "proj", "command_string": "python a.py", "timeout_seconds": 5}])
    rem = Remediator(parser, object(), log, apply_patch=lambda sb, p: None)
    state = _state(req)
    update = rem.remediate(state, platform="linux")
    assert update["error"] is None
    assert [s.command_string for s in update["request"].steps] == ["python3 a.py"]
    assert update["diagnostic"]["replacement"] == ["python3 a.py"]
    assert update["diagnostic"]["rejected_script_steps"] == 1
    notes = [e for e in _entries(log) if e["kind"] == "remediate" and "script" in e["summary"]]
    assert len(notes) == 1
    assert notes[0]["detail"]["language"] == "python"


def test_remediate_only_script_steps_is_rejected_not_crashed(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    parser = StubParser([{"script": SCRIPT}])
    req = ProvisionRequest(label="x", steps=[{"target_directory": "proj", "command_string": "python a.py", "timeout_seconds": 5}])
    applied = []
    rem = Remediator(parser, object(), log, apply_patch=lambda sb, p: applied.append(p))
    update = rem.remediate(_state(req), platform="linux")
    assert update["error"]
    assert "script" in update["error"]
    assert update["attempt"] == 1
    assert update["cached"] is False
    assert "request" not in update  # the failing request is left untouched
    assert applied == []
    kinds = [e["kind"] for e in _entries(log)]
    assert kinds.count("remediate") >= 2


def test_remediate_explicit_plan_with_script_step(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    req = ProvisionRequest(label="x", steps=[{"target_directory": "proj", "command_string": "python a.py", "timeout_seconds": 5}])
    plan = ProvisionRequest(label="x", steps=[ProvisionStep(script=SCRIPT), {"target_directory": "proj", "command_string": "patch:a.py|||x|||y", "timeout_seconds": 5}])
    applied = []
    rem = Remediator(StubParser([]), object(), log, apply_patch=lambda sb, p: applied.append(p))
    update = rem.remediate(_state(req), platform="linux", plan=plan)
    assert update["error"] is None
    assert applied == [Patch(path="a.py", find="x", replace="y")]
    assert update["request"] == req
    assert update["diagnostic"]["rejected_script_steps"] == 1
