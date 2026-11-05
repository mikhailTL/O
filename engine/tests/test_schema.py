import json

import pytest

from o_engine.schema import ProvisionStep, SchemaViolation, ScriptPayload, parse_request, parse_steps

STEP = {"target_directory": "app", "command_string": "npm install", "timeout_seconds": 60}


def test_valid_request_parses_and_fingerprint_is_stable():
    r = parse_request({"label": "x", "steps": [STEP]})
    assert r.steps[0].command_string == "npm install"
    assert r.fingerprint("linux") == parse_request({"label": "other", "steps": [STEP]}).fingerprint("linux")
    assert r.fingerprint("linux") != r.fingerprint("win32")


def test_json_text_accepted():
    r = parse_steps(json.dumps([STEP]), label="l")
    assert r.label == "l"


@pytest.mark.parametrize(
    "raw",
    [
        "```json\n[]\n```",
        [{**STEP, "note": "x"}],
        [{**STEP, "timeout_seconds": "60"}],
        [{**STEP, "command_string": "Sure, here is the command: npm install"}],
        [{**STEP, "command_string": "- npm install\n- npm run build"}],
        [{**STEP, "command_string": "* npm install"}],
        [{**STEP, "command_string": "1. npm install"}],
        [{**STEP, "command_string": "```\nnpm install\n```"}],
        [],
        "not json",
        {"label": "x"},
    ],
)
def test_rejected(raw):
    with pytest.raises(SchemaViolation):
        parse_steps(raw) if isinstance(raw, (list, str)) else parse_request(raw)


@pytest.mark.parametrize(
    "cmd",
    [
        "-v",
        "--version",
        "cd /tmp && npm install",
        "git log --oneline | head",
        "git commit -m 'Add the new feature now.'",
        "pip install -r requirements.txt && python -m pytest -q",
        "make build",
    ],
)
def test_shell_lines_accepted(cmd):
    assert ProvisionStep(**{**STEP, "command_string": cmd}).command_string == cmd


def test_frozen():
    s = ProvisionStep(**STEP)
    with pytest.raises(Exception):
        s.timeout_seconds = 5


SCRIPT = {"language": "python", "code": "print('hi')", "target_directory": "app", "timeout_seconds": 30}


def test_script_payload_strict_and_frozen():
    p = ScriptPayload(**SCRIPT)
    assert p.language == "python"
    with pytest.raises(Exception):
        p.code = "x"
    with pytest.raises(Exception):
        ScriptPayload(**{**SCRIPT, "extra": 1})
    with pytest.raises(Exception):
        ScriptPayload(**{**SCRIPT, "timeout_seconds": "30"})
    with pytest.raises(Exception):
        ScriptPayload(**{**SCRIPT, "language": "ruby"})
    with pytest.raises(Exception):
        ScriptPayload(**{**SCRIPT, "code": ""})
    with pytest.raises(Exception):
        ScriptPayload(**{**SCRIPT, "code": "x" * 65537})


def test_script_only_step_ok_and_copies_dir_and_timeout():
    s = ProvisionStep(script=ScriptPayload(**SCRIPT))
    assert s.command_string is None
    assert s.target_directory == "app"
    assert s.timeout_seconds == 30
    assert s.script.code == "print('hi')"
    r = parse_steps([{"script": SCRIPT}])
    assert r.steps[0].script is not None
    assert r.steps[0].target_directory == "app"


def test_script_step_explicit_fields_must_match_payload():
    ok = ProvisionStep(script=ScriptPayload(**SCRIPT), target_directory="app", timeout_seconds=30)
    assert ok.target_directory == "app"
    with pytest.raises(Exception, match="target_directory"):
        ProvisionStep(script=ScriptPayload(**SCRIPT), target_directory="other")
    with pytest.raises(Exception, match="timeout_seconds"):
        ProvisionStep(script=ScriptPayload(**SCRIPT), timeout_seconds=5)


def test_step_with_both_command_and_script_rejected():
    with pytest.raises(SchemaViolation, match="exactly one of command_string or script"):
        parse_steps([{**STEP, "script": {**SCRIPT, "target_directory": "app", "timeout_seconds": 60}}])


def test_step_with_neither_command_nor_script_rejected():
    with pytest.raises(SchemaViolation, match="exactly one of command_string or script"):
        parse_steps([{"target_directory": "app", "timeout_seconds": 60}])


def test_command_step_still_needs_directory_and_timeout():
    with pytest.raises(SchemaViolation):
        parse_steps([{"command_string": "npm install"}])
    with pytest.raises(SchemaViolation):
        parse_steps([{"command_string": "npm install", "target_directory": "app"}])


def test_script_payload_rejects_markdown_dir():
    with pytest.raises(Exception):
        ScriptPayload(**{**SCRIPT, "target_directory": "```app"})


def test_fingerprint_changes_with_script_code_and_language():
    a = parse_steps([{"script": SCRIPT}])
    b = parse_steps([{"script": {**SCRIPT, "code": "print('bye')"}}])
    c = parse_steps([{"script": {**SCRIPT, "language": "bash"}}])
    assert a.fingerprint("linux") == parse_steps([{"script": SCRIPT}]).fingerprint("linux")
    assert a.fingerprint("linux") != b.fingerprint("linux")
    assert a.fingerprint("linux") != c.fingerprint("linux")


def test_risk_default_and_fingerprint():
    r = parse_request({"label": "x", "steps": [STEP]})
    assert r.risk == "normal"
    high = parse_request({"label": "x", "steps": [STEP], "risk": "high"})
    assert high.risk == "high"
    assert r.fingerprint("linux") != high.fingerprint("linux")
    with pytest.raises(SchemaViolation):
        parse_request({"label": "x", "steps": [STEP], "risk": "medium"})
