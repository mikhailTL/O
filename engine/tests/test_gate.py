import json

import pytest

from o_engine.actionlog import ActionLog
from o_engine.gate import Gate, QueueDestroyed
from o_engine.schema import ProvisionRequest

REQ = ProvisionRequest(
    label="hello",
    steps=[{"target_directory": "proj", "command_string": "echo hi", "timeout_seconds": 5}],
)


def _entries(log: ActionLog) -> list[dict]:
    return [json.loads(line) for line in log.jsonl.read_text().splitlines()]


def test_log_mode_records_policy_and_passes(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    calls = []
    gate = Gate("log", waiter=lambda job_id, timeout_s: calls.append(job_id) or True)
    gate.check("job-1", REQ, log)
    assert calls == []
    policy = [e for e in _entries(log) if e["kind"] == "policy"]
    assert len(policy) == 1
    assert policy[0]["detail"]["mode"] == "log"
    assert policy[0]["detail"]["job_id"] == "job-1"
    assert policy[0]["detail"]["allowed"] is True


def test_auto_mode_passes_without_waiting(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    gate = Gate("auto", waiter=lambda job_id, timeout_s: False)
    gate.check("job-2", REQ, log)
    assert not log.jsonl.exists()


def test_ask_mode_allow(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    seen = {}

    def waiter(job_id, timeout_s):
        seen["job_id"] = job_id
        seen["timeout_s"] = timeout_s
        return True

    gate = Gate("ask", waiter=waiter, timeout_s=7)
    gate.check("job-3", REQ, log)
    assert seen == {"job_id": "job-3", "timeout_s": 7}
    policy = [e for e in _entries(log) if e["kind"] == "policy"]
    assert policy[-1]["detail"]["allowed"] is True


def test_ask_mode_deny_destroys_queue(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    gate = Gate("ask", waiter=lambda job_id, timeout_s: False)
    with pytest.raises(QueueDestroyed) as exc:
        gate.check("job-4", REQ, log)
    assert "denied" in str(exc.value)
    policy = [e for e in _entries(log) if e["kind"] == "policy"]
    assert policy[-1]["detail"]["allowed"] is False
    assert policy[-1]["detail"]["reason"] == "denied"


def test_ask_mode_timeout_destroys_queue(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    gate = Gate("ask", waiter=lambda job_id, timeout_s: None, timeout_s=1)
    with pytest.raises(QueueDestroyed) as exc:
        gate.check("job-5", REQ, log)
    assert "timeout" in str(exc.value)
    policy = [e for e in _entries(log) if e["kind"] == "policy"]
    assert policy[-1]["detail"]["allowed"] is False
    assert policy[-1]["detail"]["reason"] == "timeout"


def test_script_step_logged_with_language_placeholder(tmp_data_dir):
    log = ActionLog(tmp_data_dir)
    req = ProvisionRequest(
        label="script",
        steps=[
            {"target_directory": "proj", "command_string": "echo hi", "timeout_seconds": 5},
            {"script": {"language": "python", "code": "print(1)", "target_directory": "proj", "timeout_seconds": 5}},
        ],
    )
    Gate("log", waiter=None).check("job-6", req, log)
    policy = [e for e in _entries(log) if e["kind"] == "policy"]
    assert policy[0]["detail"]["commands"] == ["echo hi", "[script:python]"]


def test_default_timeout_is_60s():
    assert Gate("ask", waiter=lambda j, t: True).timeout_s == 60


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        Gate("yolo", waiter=lambda j, t: True)
