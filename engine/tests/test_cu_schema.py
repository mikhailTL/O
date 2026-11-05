import json

import pytest

from o_engine.cu.schema import OScript, Target, parse_script
from o_engine.schema import SchemaViolation


def _script(**over):
    base = {
        "app": "libreoffice-calc",
        "goal": "Put 42 in cell A1 and save",
        "steps": [
            {"op": "launch", "cmd": "libreoffice --calc"},
            {"op": "wait", "for": {"role": "frame", "name": "*Calc*"}, "timeout_s": 20},
            {"op": "find", "as": "cell", "target": {"role": "table cell", "name": "A1"}},
            {"op": "click", "ref": "cell"},
            {"op": "type", "ref": "cell", "text": "42"},
            {"op": "key", "keys": "ctrl+s"},
            {"op": "set_value", "ref": "cell", "value": "43"},
            {"op": "invoke", "ref": "cell", "action": "press"},
            {"op": "assert", "target": {"role": "table cell", "name": "A1", "state": "focused"}},
            {"op": "screenshot_assert", "question": "Is the file saved?"},
        ],
    }
    base.update(over)
    return base


def test_valid_script_parses_from_dict_and_json():
    s = parse_script(_script())
    assert isinstance(s, OScript)
    assert s.app == "libreoffice-calc"
    assert len(s.steps) == 10
    assert [st.op for st in s.steps] == [
        "launch", "wait", "find", "click", "type", "key", "set_value", "invoke", "assert", "screenshot_assert",
    ]
    assert s.steps[1].for_.name == "*Calc*"
    assert s.steps[1].timeout_s == 20
    assert s.steps[2].as_ == "cell"
    assert s.steps[8].state == "focused"
    assert s.steps[8].target.name == "A1"
    s2 = parse_script(json.dumps(_script()))
    assert s2 == s
    s3 = parse_script(json.dumps(_script()).encode())
    assert s3 == s


def test_script_is_frozen_and_roundtrips():
    s = parse_script(_script())
    with pytest.raises(Exception):
        s.app = "other"  # type: ignore[misc]
    dumped = s.model_dump(by_alias=True)
    assert dumped["steps"][1]["for"] == {"role": "frame", "name": "*Calc*", "text": None, "index": 0, "within": None}
    assert parse_script(dumped) == s


def test_target_defaults_and_requires_a_selector():
    t = Target(role="button")
    assert t.index == 0 and t.name is None and t.within is None
    with pytest.raises(Exception):
        Target()
    with pytest.raises(Exception):
        Target(role="button", index=-1)


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "fly"},
        {"op": "click"},
        {"op": "click", "ref": "Cell"},
        {"op": "click", "ref": "cell", "extra": 1},
        {"op": "wait", "for": {"role": "frame"}, "timeout_s": 0},
        {"op": "wait", "for": {"role": "frame"}, "timeout_s": 301},
        {"op": "wait", "for": {"role": "frame"}, "timeout_s": "20"},
        {"op": "find", "as": "9x", "target": {"role": "frame"}},
        {"op": "find", "as": "x", "target": {}},
        {"op": "type", "ref": "cell", "text": "x" * 4001},
        {"op": "launch", "cmd": ""},
        {"op": "assert", "target": {"role": "frame", "name": "x"}},
        {"op": "assert", "target": {"role": "frame", "name": "x", "state": "sparkly"}},
        {"op": "screenshot_assert"},
    ],
)
def test_invalid_steps_raise_schema_violation(bad):
    steps = [{"op": "find", "as": "cell", "target": {"role": "table cell", "name": "A1"}}, bad]
    with pytest.raises(SchemaViolation):
        parse_script(_script(steps=steps))


def test_unknown_ref_and_forward_ref_rejected():
    with pytest.raises(SchemaViolation, match="ref"):
        parse_script(_script(steps=[{"op": "click", "ref": "nope"}]))
    with pytest.raises(SchemaViolation, match="ref"):
        parse_script(
            _script(
                steps=[
                    {"op": "click", "ref": "cell"},
                    {"op": "find", "as": "cell", "target": {"role": "table cell"}},
                ]
            )
        )
    with pytest.raises(SchemaViolation, match="within"):
        parse_script(_script(steps=[{"op": "find", "as": "x", "target": {"role": "button", "within": "win"}}]))


def test_duplicate_as_rejected():
    with pytest.raises(SchemaViolation, match="as"):
        parse_script(
            _script(
                steps=[
                    {"op": "find", "as": "cell", "target": {"role": "table cell"}},
                    {"op": "find", "as": "cell", "target": {"role": "table cell"}},
                ]
            )
        )


def test_steps_bounds_and_extra_keys():
    with pytest.raises(SchemaViolation):
        parse_script(_script(steps=[]))
    with pytest.raises(SchemaViolation):
        parse_script(_script(steps=[{"op": "key", "keys": "a"}] * 201))
    with pytest.raises(SchemaViolation):
        parse_script(dict(_script(), bonus=1))
    with pytest.raises(SchemaViolation):
        parse_script(_script(app=""))


def test_goal_fence_and_prose_checks():
    with pytest.raises(SchemaViolation):
        parse_script(_script(goal="```save the file```"))
    with pytest.raises(SchemaViolation):
        parse_script(_script(goal="Sure, here's the goal: save the file"))
    with pytest.raises(SchemaViolation):
        parse_script(_script(goal="- save\n- quit"))
    with pytest.raises(SchemaViolation):
        parse_script("```json\n" + json.dumps(_script()) + "\n```")
    with pytest.raises(SchemaViolation):
        parse_script("not json")
    with pytest.raises(SchemaViolation):
        parse_script([1, 2])
    # a plain imperative goal with a period is fine
    parse_script(_script(goal="Save the spreadsheet as report.xlsx."))


# ---------------------------------------------------------------- P4/P8/P7 ops

_PAYLOAD = {"language": "bash", "code": "ls", "target_directory": ".", "timeout_seconds": 5}


def test_state_assert_step_parses_and_keeps_order():
    s = parse_script(
        _script(
            steps=[
                {
                    "op": "state_assert",
                    "checks": [
                        {"kind": "file_exists", "arg": "out/report.csv"},
                        {"kind": "file_contains", "arg": "out/report.csv", "expect": "a,b"},
                        {"kind": "file_sha_changed", "arg": "out/report.csv"},
                        {"kind": "tree", "arg": '{"role":"frame","name":"*Calc*"}', "expect": "active"},
                        {"kind": "shell_true", "arg": "test -f out/report.csv"},
                    ],
                }
            ]
        )
    )
    step = s.steps[0]
    assert step.op == "state_assert"
    assert [c.kind for c in step.checks] == ["file_exists", "file_contains", "file_sha_changed", "tree", "shell_true"]
    assert step.checks[0].expect is None and step.checks[1].expect == "a,b"
    assert isinstance(step.checks, tuple)
    assert parse_script(json.loads(s.to_json())) == s
    with pytest.raises(Exception):
        step.checks[0].arg = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "state_assert", "checks": []},
        {"op": "state_assert"},
        {"op": "state_assert", "checks": [{"kind": "file_exists"}]},
        {"op": "state_assert", "checks": [{"kind": "file_exists", "arg": ""}]},
        {"op": "state_assert", "checks": [{"kind": "registry", "arg": "x"}]},
        {"op": "state_assert", "checks": [{"kind": "file_exists", "arg": "x", "bonus": 1}]},
        {"op": "state_assert", "checks": [{"kind": "file_contains", "arg": "x"}]},
        {"op": "state_assert", "checks": [{"kind": "file_exists", "arg": "x", "expect": 1}]},
        {"op": "state_assert", "checks": [{"kind": "shell_true", "arg": "test -f a\nrm -rf /"}]},
        {"op": "state_assert", "checks": [{"kind": "file_exists", "arg": "x"}], "extra": 1},
    ],
)
def test_state_assert_rejects_malformed_checks(bad):
    with pytest.raises(SchemaViolation):
        parse_script(_script(steps=[bad]))


def test_script_step_reuses_script_payload():
    from o_engine.schema import ScriptPayload

    s = parse_script(_script(steps=[{"op": "script", "script": _PAYLOAD}]))
    step = s.steps[0]
    assert step.op == "script"
    assert isinstance(step.script, ScriptPayload)
    assert step.script.language == "bash" and step.script.timeout_seconds == 5
    assert parse_script(json.loads(s.to_json())) == s


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "script"},
        {"op": "script", "script": None},
        {"op": "script", "script": {}},
        {"op": "script", "script": dict(_PAYLOAD, language="ruby")},
        {"op": "script", "script": dict(_PAYLOAD, code="")},
        {"op": "script", "script": dict(_PAYLOAD, timeout_seconds=0)},
        {"op": "script", "script": dict(_PAYLOAD, bonus=1)},
        # a shell command is not a script payload: the step holds exactly one ScriptPayload
        {"op": "script", "command_string": "ls"},
        {"op": "script", "script": _PAYLOAD, "command_string": "ls"},
        {"op": "script", "script": _PAYLOAD, "extra": 1},
    ],
)
def test_script_step_rejects_malformed_payload(bad):
    with pytest.raises(SchemaViolation):
        parse_script(_script(steps=[bad]))


def test_call_step_parses():
    s = parse_script(
        _script(
            steps=[
                {"op": "call", "fn": "calc.set_cell", "args": {"cell": "A1", "value": 42, "ratio": 0.5, "bold": True}},
                {"op": "call", "fn": "calc.save"},
            ]
        )
    )
    a, b = s.steps
    assert a.op == "call" and a.fn == "calc.set_cell"
    assert a.args == {"cell": "A1", "value": 42, "ratio": 0.5, "bold": True}
    assert b.args == {}
    assert parse_script(json.loads(s.to_json())) == s


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "call"},
        {"op": "call", "fn": "set_cell"},
        {"op": "call", "fn": "calc.set_cell.now"},
        {"op": "call", "fn": "Calc.set_cell"},
        {"op": "call", "fn": "calc.9x"},
        {"op": "call", "fn": "calc.set_cell", "args": {"cell": ["A1"]}},
        {"op": "call", "fn": "calc.set_cell", "args": {"cell": None}},
        {"op": "call", "fn": "calc.set_cell", "args": {"cell": {"a": 1}}},
        {"op": "call", "fn": "calc.set_cell", "args": "A1"},
        {"op": "call", "fn": "calc.set_cell", "args": {}, "extra": 1},
        {"op": "call", "fn": "calc.set_cell", "ref": "cell"},
    ],
)
def test_call_step_rejects_bad_fn_and_extra_keys(bad):
    with pytest.raises(SchemaViolation):
        parse_script(_script(steps=[bad]))


def test_ops_tuple_lists_new_ops_and_unknown_still_rejected():
    from o_engine.cu.schema import OPS

    assert {"state_assert", "script", "call"} <= set(OPS)
    with pytest.raises(SchemaViolation):
        parse_script(_script(steps=[{"op": "state_check", "checks": [{"kind": "file_exists", "arg": "x"}]}]))
