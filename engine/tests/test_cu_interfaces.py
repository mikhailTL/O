import os
import sys
from pathlib import Path

import pytest

from o_engine.cu.interfaces import MAX_FILE_LINES, MAX_SECTION_LINES, InterfaceNotes, split_sections


class StubClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.reply


def test_read_missing_is_empty_and_append_raw(tmp_path):
    notes = InterfaceNotes(tmp_path / "interfaces")
    assert notes.read("calc") == ""
    written = notes.append("calc", "- clicked Save\n- tree: frame 'Calc'", goal="save file")
    text = notes.read("calc")
    assert text.startswith("## ") and " save file\n" in text.splitlines()[0] + "\n"
    assert "- clicked Save\n- tree: frame 'Calc'\n" in text
    assert written in text
    assert (tmp_path / "interfaces" / "calc.md").is_file()
    notes.append("calc", "second run", goal="g2")
    secs = notes.sections("calc")
    assert len(secs) == 2 and secs[1].endswith("second run") and "clicked Save" in secs[0]


def test_append_with_model_client_summarizes_and_demotes_headings(tmp_path):
    notes = InterfaceNotes(tmp_path)
    client = StubClient("## What worked\n- api click on push button\n## Pitfalls\n- dialog steals focus")
    notes.append("calc", "raw long summary", client, goal="g")
    system, user = client.calls[0]
    assert "raw long summary" in user and "Pitfalls" in system
    text = notes.read("calc")
    assert text.count("\n## ") == 0 and text.startswith("## ")
    assert "### What worked" in text and "- dialog steals focus" in text
    assert len(notes.sections("calc")) == 1


def test_model_failure_falls_back_to_raw(tmp_path):
    class Broken:
        def complete(self, s, u):
            raise RuntimeError("down")

    notes = InterfaceNotes(tmp_path)
    notes.append("calc", "raw text", Broken(), goal="g")
    assert "raw text" in notes.read("calc")


def test_section_capped_at_40_lines(tmp_path):
    notes = InterfaceNotes(tmp_path)
    notes.append("app", "\n".join(f"line {i}" for i in range(100)), goal="g")
    lines = notes.read("app").splitlines()
    assert len(lines) == MAX_SECTION_LINES and lines[-1] == "line 38"


def test_file_capped_at_400_lines_keeping_newest_and_never_rewriting_old(tmp_path):
    notes = InterfaceNotes(tmp_path)
    body = "\n".join(f"b{i}" for i in range(39))  # 40 lines with heading
    for n in range(12):
        notes.append("app", body, goal=f"run{n}")
    lines = notes.read("app").splitlines()
    assert len(lines) <= MAX_FILE_LINES and len(lines) == 400
    secs = notes.sections("app")
    assert len(secs) == 10
    assert secs[0].splitlines()[0].endswith("run2") and secs[-1].splitlines()[0].endswith("run11")
    before = secs[:]
    notes.append("app", body, goal="run12")
    after = notes.sections("app")
    assert after[:-1] == before[1:]
    assert after[-1].splitlines()[0].endswith("run12") and len(notes.read("app").splitlines()) == 400


def test_oversized_single_section_is_still_kept(tmp_path):
    notes = InterfaceNotes(tmp_path)
    notes.append("app", "x", goal="only")
    assert len(notes.sections("app")) == 1


def test_for_prompt_returns_newest_within_budget(tmp_path):
    notes = InterfaceNotes(tmp_path)
    for n in range(5):
        notes.append("app", "payload " * 20, goal=f"run{n}")
    full = notes.for_prompt("app")
    assert full == notes.read("app").rstrip("\n")
    one = len(notes.sections("app")[-1])
    small = notes.for_prompt("app", max_chars=one * 2 + 1)
    assert small.count("\n## ") == 1 and small.splitlines()[0].endswith("run3") and small.endswith("payload")
    tiny = notes.for_prompt("app", max_chars=10)
    assert len(tiny) == 10
    assert notes.for_prompt("missing") == ""


def test_split_sections_keeps_leading_text():
    secs = split_sections("intro\n## a\n1\n## b\n2")
    assert secs == [["intro"], ["## a", "1"], ["## b", "2"]]


# --------------------------------------------------------------- executable interfaces (P7)

import json

from o_engine.cu.interfaces import (
    FORBIDDEN_FRAGMENTS,
    InterfaceBuilder,
    InterfaceStore,
    InterfaceViolation,
    check_source,
)
from o_engine.cu.procedures import ProcedureStore
from o_engine.cu.schema import parse_script
from o_engine.executor import Sandbox, StepResult

SCRIPT = parse_script(
    {
        "app": "calc",
        "goal": "put 42 in A1",
        "steps": [
            {"op": "find", "as": "cell", "target": {"role": "table cell", "name": "A1"}},
            {"op": "set_value", "ref": "cell", "value": "42"},
        ],
    }
)

GOOD_REPLY = json.dumps(
    {
        "functions": [
            {
                "name": "set_cell",
                "description": "Write a value into a named cell.",
                "signature": "(backend, cell: str, value: str) -> None",
                "body": "node = backend.find({'role': 'table cell', 'name': cell})\nif node is None:\n    raise RuntimeError('no cell ' + cell)\nbackend.set_value(node, value)",
            }
        ],
        "smoke": "set_cell(backend, 'A1', '42')\nprint('ok')",
    }
)


class StubModel:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return self.reply


class StubRunner:
    def __init__(self, exit_code=0, timed_out=False):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.calls = []

    def run(self, payload, sandbox, index=0, env_extra=None, isolated_display=False):
        self.calls.append({"payload": payload, "sandbox": sandbox, "env_extra": dict(env_extra or {}), "isolated": isolated_display})
        return StepResult(index=index, command="stub", cwd=str(sandbox.root), exit_code=self.exit_code,
                          stdout="", stderr="boom" if self.exit_code else "", elapsed_ms=1, timed_out=self.timed_out)


def _setup(tmp_path, reply=GOOD_REPLY, runner=None):
    procedures = ProcedureStore(tmp_path / "procedures")
    p1 = procedures.store("calc", "put 42 in A1", SCRIPT)
    p2 = procedures.store("calc", "put 7 in B2", SCRIPT)
    store = InterfaceStore(tmp_path / "interfaces")
    runner = runner or StubRunner()
    builder = InterfaceBuilder(StubModel(reply), store, runner, Sandbox(tmp_path / "sandbox"), procedures,
                               backend_bootstrap="backend = object()")
    return procedures, (p1, p2), store, runner, builder


def test_build_creates_v1_then_v2_and_v1_is_byte_identical(tmp_path):
    procedures, (p1, p2), store, runner, builder = _setup(tmp_path)
    assert store.latest("calc") is None and store.versions("calc") == [] and store.functions("calc") == []
    assert builder.build("calc", p1, "find/invoke/set_value/tree", display=":77") == 1
    v1 = tmp_path / "interfaces" / "calc" / "v1"
    assert (v1 / "api.py").is_file() and (v1 / "api.json").is_file()
    meta = json.loads((v1 / "api.json").read_text())
    assert meta["version"] == 1 and meta["parent"] is None and meta["fitness"] is None
    assert meta["functions"] == [{"name": "set_cell", "description": "Write a value into a named cell.",
                                  "signature": "(backend, cell: str, value: str) -> None", "passed_on": [p1.id]}]
    api_src = (v1 / "api.py").read_text()
    assert "def set_cell(backend, cell: str, value: str) -> None:" in api_src and "backend.set_value(node, value)" in api_src
    assert "body" not in meta["functions"][0]
    # the smoke run went through the injected runner with the caller's display, never the host's
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["env_extra"] == {"DISPLAY": ":77"} and call["isolated"] is False
    assert call["payload"].language == "python" and "backend = object()" in call["payload"].code
    assert "def set_cell" in call["payload"].code and "set_cell(backend, 'A1', '42')" in call["payload"].code
    # one bounded model call
    system, user = builder.model.calls[0]
    assert "find" in system and "set_value" in system and "put 42 in A1" in user and "find/invoke/set_value/tree" in user
    v1_bytes = (v1 / "api.py").read_bytes(), (v1 / "api.json").read_bytes()
    assert builder.build("calc", p2, "doc", display=":77") == 2
    assert store.versions("calc") == [1, 2] and store.latest("calc") == 2
    assert ((v1 / "api.py").read_bytes(), (v1 / "api.json").read_bytes()) == v1_bytes
    meta2 = store.read("calc")
    assert meta2["version"] == 2 and meta2["parent"] == 1 and meta2["functions"][0]["passed_on"] == [p2.id]
    assert store.read("calc", 1)["version"] == 1


def test_functions_is_names_and_descriptions_only_and_source_on_request(tmp_path):
    _, (p1, _), store, _, builder = _setup(tmp_path)
    builder.build("calc", p1, "doc", display=":77")
    fns = store.functions("calc")
    assert fns == [{"name": "set_cell", "description": "Write a value into a named cell.",
                    "signature": "(backend, cell: str, value: str) -> None"}]
    src = store.source("calc", "set_cell")
    assert src.startswith("def set_cell(backend, cell: str, value: str) -> None:") and "backend.set_value(node, value)" in src
    assert store.source("calc", "nope") is None and store.source("writer", "x") is None
    assert store.functions("writer") == []


def test_smoke_failure_records_nothing(tmp_path):
    for runner in (StubRunner(exit_code=1), StubRunner(exit_code=0, timed_out=True)):
        root = tmp_path / str(id(runner))
        _, (p1, _), store, runner, builder = _setup(root, runner=runner)
        assert builder.build("calc", p1, "doc", display=":77") is None
        assert len(runner.calls) == 1
        assert store.versions("calc") == [] and not (root / "interfaces" / "calc").exists()


@pytest.mark.parametrize("bad", ["import subprocess\nsubprocess.run(['x'])", "import os\nos.system('x')",
                                 "from os import path", "import pyautogui", "backend.click_pixel('pixel', 1)",
                                 "__import__('xdotool')"])
def test_forbidden_body_is_rejected_before_runner(tmp_path, bad):
    reply = json.loads(GOOD_REPLY)
    reply["functions"][0]["body"] = bad
    _, (p1, _), store, runner, builder = _setup(tmp_path, reply=json.dumps(reply))
    assert builder.build("calc", p1, "doc", display=":77") is None
    assert runner.calls == [] and store.versions("calc") == []


def test_forbidden_smoke_is_rejected_before_runner(tmp_path):
    reply = json.loads(GOOD_REPLY)
    reply["smoke"] = "import subprocess\nset_cell(backend, 'A1', '42')"
    _, (p1, _), store, runner, builder = _setup(tmp_path, reply=json.dumps(reply))
    assert builder.build("calc", p1, "doc", display=":77") is None and runner.calls == []


def test_check_source_is_a_closed_list():
    assert check_source("x = backend.find({})") == []
    assert check_source("import subprocess") == ["import subprocess"]
    assert "pixel" in check_source("# pixel fallback") and "xdotool" in check_source("run xdotool")
    assert set(FORBIDDEN_FRAGMENTS) >= {"import os", "import subprocess", "pyautogui", "pixel", "xdotool"}


def test_display_is_required_and_never_the_host(tmp_path, monkeypatch):
    _, (p1, _), store, runner, builder = _setup(tmp_path)
    with pytest.raises(TypeError):
        builder.build("calc", p1, "doc")  # display has no default
    with pytest.raises(InterfaceViolation):
        builder.build("calc", p1, "doc", display="")
    monkeypatch.setenv("DISPLAY", ":0")
    with pytest.raises(InterfaceViolation):
        builder.build("calc", p1, "doc", display=":0")
    assert runner.calls == [] and store.versions("calc") == []


@pytest.mark.parametrize("reply", ["not json", '{"functions": []}', '{"functions": [{"name": "1bad", "description": "d", "signature": "(backend)", "body": "pass"}], "smoke": "x"}',
                                   '{"functions": [{"name": "f", "description": "d", "signature": "(cell)", "body": "pass"}], "smoke": "x"}',
                                   '{"functions": [{"name": "f", "description": "d", "signature": "(backend)", "body": "def (:"}], "smoke": "x"}'])
def test_malformed_model_reply_records_nothing(tmp_path, reply):
    _, (p1, _), store, runner, builder = _setup(tmp_path, reply=reply)
    assert builder.build("calc", p1, "doc", display=":77") is None
    assert runner.calls == [] and store.versions("calc") == []


def test_model_exception_records_nothing(tmp_path):
    class Broken:
        def complete_json(self, s, u):
            raise RuntimeError("down")

    _, (p1, _), store, runner, builder = _setup(tmp_path)
    builder.model = Broken()
    assert builder.build("calc", p1, "doc", display=":77") is None and runner.calls == []


def test_store_write_is_append_only(tmp_path):
    store = InterfaceStore(tmp_path)
    fns = [{"name": "f", "description": "d", "signature": "(backend)", "body": "return backend.tree()", "passed_on": ["calc/k/v1"]}]
    assert store.write("calc", fns) == 1
    first = (tmp_path / "calc" / "v1" / "api.py").read_bytes()
    (tmp_path / "calc" / "v2").mkdir()
    (tmp_path / "calc" / "v2" / "api.py").write_text("tampered")
    with pytest.raises(FileExistsError):
        store.write("calc", fns)
    assert (tmp_path / "calc" / "v1" / "api.py").read_bytes() == first
    assert (tmp_path / "calc" / "v2" / "api.py").read_text() == "tampered"
    assert not hasattr(store, "delete") and not hasattr(store, "remove")


def test_fitness_is_append_only_and_replays_newest_per_goal(tmp_path):
    procedures, (p1, p2), store, runner, builder = _setup(tmp_path)
    builder.build("calc", p1, "doc", display=":77")
    builder.build("calc", p2, "doc", display=":77")
    v1_bytes = (tmp_path / "interfaces" / "calc" / "v1" / "api.json").read_bytes()
    v2_json = (tmp_path / "interfaces" / "calc" / "v2" / "api.json").read_bytes()
    v2_py = (tmp_path / "interfaces" / "calc" / "v2" / "api.py").read_bytes()
    replayed = []

    def replay(procedure_id):
        replayed.append(procedure_id)
        return procedure_id == p1.id

    out = builder.fitness("calc", replay)
    assert sorted(replayed) == sorted([p1.id, p2.id])
    assert out == {"version": 2, "replayed": 2, "passed": 1}
    fit = store.read("calc")["fitness"]
    assert fit["replayed"] == 2 and fit["passed"] == 1 and fit["version"] == 2 and isinstance(fit["ts"], str)
    assert store.read("calc", 1)["fitness"] is None
    assert builder.fitness("writer", replay) == {"version": None, "replayed": 0, "passed": 0}

    def exploding(procedure_id):
        raise RuntimeError("replay crashed")

    assert builder.fitness("calc", exploding) == {"version": 2, "replayed": 2, "passed": 0}
    # two write_fitness calls: api.json bytes are exactly what write() produced; fitness.jsonl has 2 lines
    vdir = tmp_path / "interfaces" / "calc"
    assert (vdir / "v1" / "api.json").read_bytes() == v1_bytes
    assert (vdir / "v2" / "api.json").read_bytes() == v2_json
    assert (vdir / "v2" / "api.py").read_bytes() == v2_py
    lines = (vdir / "v2" / "fitness.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(l)["passed"] for l in lines] == [1, 0]
    assert store.read("calc")["fitness"] == json.loads(lines[-1])
    assert not (vdir / "v1" / "fitness.jsonl").exists()
    assert json.loads((vdir / "v2" / "api.json").read_text())["fitness"] is None
    # honest fitness: an unverified heal (v2 of goal a) is replayed; the superseded v1 of that goal is not
    p1_healed = procedures.store("calc", "put 42 in A1", SCRIPT, parent=p1.id, healed_step=0, reason="r")
    replayed.clear()
    out = builder.fitness("calc", replay)
    assert sorted(replayed) == sorted([p1_healed.id, p2.id]) and out["replayed"] == 2
    assert len((vdir / "v2" / "fitness.jsonl").read_text().splitlines()) == 3


def test_fitness_replays_exactly_one_id_for_a_goal_with_two_versions(tmp_path):
    procedures = ProcedureStore(tmp_path / "procedures")
    v1 = procedures.store("calc", "g", SCRIPT)
    v2 = procedures.store("calc", "g", SCRIPT, parent=v1.id, healed_step=0, reason="failed")
    store = InterfaceStore(tmp_path / "interfaces")
    builder = InterfaceBuilder(StubModel(GOOD_REPLY), store, StubRunner(), Sandbox(tmp_path / "sandbox"), procedures,
                               backend_bootstrap="backend = object()")
    assert builder.build("calc", v1, "doc", display=":77") == 1
    replayed = []
    out = builder.fitness("calc", lambda pid: replayed.append(pid) or False)
    assert replayed == [v2.id] and out == {"version": 1, "replayed": 1, "passed": 0}


def test_write_fitness_without_a_version_is_a_noop(tmp_path):
    store = InterfaceStore(tmp_path)
    assert store.write_fitness("calc", 1, 1) is None
    assert not (tmp_path / "calc").exists()


# --------------------------------------------------------------- AST allow-list (E2)

from o_engine.cu.interfaces import check_ast


@pytest.mark.parametrize("ok", [
    "node = backend.find({'role': 'x'})\nbackend.set_value(node, 'v')",
    "for n in backend.tree():\n    backend.invoke(n, 'press')",
    "return len(backend.tree())",
    "x = str(1)\nprint(x)",
])
def test_check_ast_accepts_allowed_backend_use(ok):
    assert check_ast(ok) == []


@pytest.mark.parametrize("bad", [
    "import os",
    "from os import path",
    "import json",
    "__import__('os')",
    "exec('x')",
    "eval('x')",
    "compile('x', 'f', 'exec')",
    "open('x')",
    "getattr(backend, 'click')",
    "setattr(backend, 'a', 1)",
    "delattr(backend, 'a')",
    "globals()",
    "locals()",
    "vars()",
    "breakpoint()",
    "input()",
    "backend.click(1, 2)",
    "backend.key('Return')",
    "backend.__class__",
    "backend.find({}).__class__.__subclasses__()",
    "x = ().__class__",
    "y = backend.tree\ny.__dict__",
])
def test_check_ast_rejects_escapes(bad):
    assert check_ast(bad) != []


def test_check_ast_reports_syntax_errors():
    assert check_ast("def (:") != []


@pytest.mark.parametrize("bad", ["backend.click(1, 2)", "backend.key('Return')", "__import__('os')", "open('x')",
                                 "backend.__class__", "import json"])
def test_ast_rejected_body_returns_none_before_runner(tmp_path, bad):
    reply = json.loads(GOOD_REPLY)
    reply["functions"][0]["body"] = bad
    _, (p1, _), store, runner, builder = _setup(tmp_path, reply=json.dumps(reply))
    assert builder.build("calc", p1, "doc", display=":77") is None
    assert runner.calls == [] and store.versions("calc") == []


def test_ast_rejected_smoke_returns_none_before_runner(tmp_path):
    reply = json.loads(GOOD_REPLY)
    reply["smoke"] = "set_cell(backend, 'A1', '42')\nbackend.click(1, 2)"
    _, (p1, _), store, runner, builder = _setup(tmp_path, reply=json.dumps(reply))
    assert builder.build("calc", p1, "doc", display=":77") is None and runner.calls == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="NativeRunner smoke path is exercised on Linux")
def test_build_with_real_native_runner_and_fake_backend(tmp_path, monkeypatch):
    """The generated api.py + smoke really run under python3 -I on the named display with a FakeBackend."""
    from o_engine.runners import NativeRunner

    engine_dir = Path(__file__).resolve().parents[1]
    bootstrap = "\n".join([
        "import os, sys",
        f"sys.path.insert(0, {str(engine_dir)!r})",
        "assert os.environ.get('DISPLAY') == ':77', os.environ.get('DISPLAY')",
        "from o_engine.cu.backends.base import Node",
        "from o_engine.cu.backends.fake import FakeBackend",
        "from o_engine.cu.schema import Target",
        "_tree = Node(id='w', role='frame', name='Calc', children=[Node(id='a1', role='table cell', name='A1')])",
        "class _B(FakeBackend):",
        "    def find(self, target, within=None):",
        "        return super().find(Target(**target) if isinstance(target, dict) else target, within)",
        "backend = _B(_tree, display=':77')",
    ])
    monkeypatch.setenv("DISPLAY", ":0")
    procedures = ProcedureStore(tmp_path / "procedures")
    p1 = procedures.store("calc", "put 42 in A1", SCRIPT)
    store = InterfaceStore(tmp_path / "interfaces")
    builder = InterfaceBuilder(StubModel(GOOD_REPLY), store, NativeRunner(), Sandbox(tmp_path / "sandbox"), procedures,
                               backend_bootstrap=bootstrap, timeout_seconds=60)
    assert builder.build("calc", p1, "doc", display=":77") == 1
    assert store.functions("calc")[0]["name"] == "set_cell"
    assert os.environ["DISPLAY"] == ":0"
    # a smoke script that really fails (wrong cell) records nothing
    reply = json.loads(GOOD_REPLY)
    reply["smoke"] = "set_cell(backend, 'Z9', '1')"
    builder.model = StubModel(json.dumps(reply))
    assert builder.build("calc", p1, "doc", display=":77") is None and store.versions("calc") == [1]
