import json
import subprocess
from types import SimpleNamespace

import pytest

from o_engine.cu import judge as judge_mod
from o_engine.cu.judge import ClaudeCodeJudge, Judge, JudgeVerdict, VLMJudge, a11y_assert
from o_engine.schema import SchemaViolation

PNG = b"\x89PNG\r\n\x1a\nfake"


def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


def _chunks(text, size=5):
    out = [_chunk(text[i : i + size]) for i in range(0, len(text), size)]
    out.append(SimpleNamespace(choices=[]))
    return out


class FakeOpenAI:
    calls = []
    reply = ""
    error = None

    def __init__(self, base_url=None, api_key=None, timeout=None, max_retries=None, **kw):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        FakeOpenAI.calls.append(kw)
        if FakeOpenAI.error is not None:
            raise FakeOpenAI.error
        return iter(_chunks(FakeOpenAI.reply))


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeOpenAI.calls = []
    FakeOpenAI.reply = ""
    FakeOpenAI.error = None
    monkeypatch.setattr(judge_mod, "OpenAI", FakeOpenAI)
    yield


@pytest.fixture
def fake_run(monkeypatch):
    state = {"calls": [], "stdout": "", "returncode": 0}

    def run(cmd, **kw):
        state["calls"].append((cmd, kw))
        return subprocess.CompletedProcess(cmd, state["returncode"], stdout=state["stdout"], stderr="")

    monkeypatch.setattr(judge_mod.subprocess, "run", run)
    return state


class StubJudge:
    def __init__(self, name, verdict=None, error=None):
        self.name = name
        self.verdict = verdict
        self.error = error
        self.calls = 0

    def judge(self, png, question):
        self.calls += 1
        if self.error:
            raise self.error
        return self.verdict


def test_vlm_judge_parses_verdict_with_prose():
    FakeOpenAI.reply = 'Looking at it... {"ok": true, "reason": "The dialog says Saved"} done.'
    v = VLMJudge("http://x/v1", "m").judge(PNG, "Is the file saved?")
    assert v == JudgeVerdict(ok=True, reason="The dialog says Saved", source="vlm")
    content = FakeOpenAI.calls[0]["messages"][1]["content"]
    assert content[0]["type"] == "image_url" and "Is the file saved?" in content[1]["text"]


@pytest.mark.parametrize("reply", ["yes", '{"ok": "yes"}', '{"reason": "x"}', '{"ok": true, "extra": 1}', "[]"])
def test_vlm_judge_rejects_garbage(reply):
    FakeOpenAI.reply = reply
    with pytest.raises(SchemaViolation):
        VLMJudge("http://x/v1", "m").judge(PNG, "q")


def test_claude_judge(fake_run):
    fake_run["stdout"] = json.dumps({"result": '{"ok": false, "reason": "still editing"}'})
    v = ClaudeCodeJudge().judge(PNG, "Saved?")
    assert (v.ok, v.reason, v.source) == (False, "still editing", "claude")
    assert ".png" in fake_run["calls"][0][1]["input"]


def test_judge_primary_then_fallback_once_then_failed_verdict():
    ok = JudgeVerdict(ok=True, reason="fine", source="claude")
    j = Judge(StubJudge("vlm", error=RuntimeError("down")), StubJudge("claude", ok))
    assert j.judge(PNG, "q") == ok
    assert j.primary.calls == 1 and j.fallback.calls == 1 and j.last_source == "claude"
    assert j.ask(PNG, "q") == (True, "fine")
    none = Judge(StubJudge("vlm", error=RuntimeError("a")), StubJudge("claude", error=SchemaViolation("b")))
    v = none.judge(PNG, "q")
    assert v.ok is False and "vlm: a" in v.reason and "claude: b" in v.reason
    assert none.primary.calls == 1 and none.fallback.calls == 1
    assert Judge(None, None).ask(PNG, "q")[0] is False


def test_a11y_assert_states_role_name_glob():
    node = SimpleNamespace(role="push button", name="Save As", text="", states=["focused", "visible"])
    target = SimpleNamespace(role="push button", name="Save*", text=None, state="focused")
    assert a11y_assert(node, target).ok
    assert a11y_assert(node, target, state="visible").ok
    missing = a11y_assert(node, SimpleNamespace(role="push button", name="Save*", state="checked"))
    assert not missing.ok and "checked" in missing.reason
    assert not a11y_assert(node, {"role": "menu", "name": "Save*"}).ok
    assert not a11y_assert(node, {"name": "Open*"}).ok
    assert not a11y_assert(None, target).ok
    assert a11y_assert(node, {"role": "push button"}, state=["focused", "visible"]).source == "a11y"


def test_a11y_assert_with_real_target():
    from o_engine.cu.backends.base import Node
    from o_engine.cu.schema import Target

    node = Node(id="n1", role="check box", name="Bold", states=["checked"])
    assert a11y_assert(node, Target(role="check box", name="bold"), state="checked").ok
    assert not a11y_assert(node, Target(role="check box", name="bold"), state="focused").ok


# ------------------------------------------------------------------ StateJudge (P4)

from o_engine.cu.backends.base import Node  # noqa: E402
from o_engine.cu.backends.fake import FakeBackend  # noqa: E402
from o_engine.cu.judge import PROBE_ALLOW_LIST, StateJudge, is_allowed_probe  # noqa: E402
from o_engine.cu.schema import StateCheck  # noqa: E402
from o_engine.executor import Sandbox, StepResult  # noqa: E402


def _backend():
    return FakeBackend(
        Node(
            id="win",
            role="frame",
            name="report.csv - LibreOffice Calc",
            states=["visible", "active"],
            children=[Node(id="cell", role="table cell", name="A1", states=["focused"])],
        )
    )


class RecordingSandbox:
    """Records every shell probe; never runs anything."""

    def __init__(self, root):
        self.root = root
        self.calls = []

    def resolve(self, p):
        return self.root / p

    def run(self, step, index=0, env_extra=None, isolated_display=False):
        self.calls.append(step)
        return StepResult(
            index=index, command=step.command_string, cwd=str(self.root), exit_code=0, stdout="ok\n",
            stderr="", elapsed_ms=1, timed_out=False,
        )


def _checks(*items):
    return tuple(StateCheck(**i) for i in items)


def test_state_judge_file_exists_flips_with_the_file(tmp_path):
    sb = Sandbox(tmp_path)
    judge = StateJudge(sb, _backend())
    checks = _checks({"kind": "file_exists", "arg": "out/report.csv"})
    v = judge.verify(checks, "job1")
    assert v.ok is False and v.source == "state" and "out/report.csv" in v.reason
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.csv").write_text("a,b\n1,2\n")
    v = judge.verify(checks, "job1")
    assert v.ok is True and v.source == "state"
    (tmp_path / "out" / "report.csv").unlink()
    assert judge.verify(checks, "job1").ok is False
    # expect "false" requires absence
    assert judge.verify(_checks({"kind": "file_exists", "arg": "out/report.csv", "expect": "false"}), "job1").ok


def test_state_judge_file_contains_and_escape(tmp_path):
    sb = Sandbox(tmp_path)
    (tmp_path / "r.csv").write_text("name,total\nx,42\n")
    judge = StateJudge(sb, _backend())
    assert judge.verify(_checks({"kind": "file_contains", "arg": "r.csv", "expect": "x,42"}), "j").ok
    v = judge.verify(_checks({"kind": "file_contains", "arg": "r.csv", "expect": "y,99"}), "j")
    assert v.ok is False and "y,99" in v.reason
    v = judge.verify(_checks({"kind": "file_contains", "arg": "../../etc/passwd", "expect": "root"}), "j")
    assert v.ok is False and "outside sandbox" in v.reason
    v = judge.verify(_checks({"kind": "file_contains", "arg": "missing.csv", "expect": "x"}), "j")
    assert v.ok is False


def test_state_judge_sha_changed_uses_snapshot(tmp_path):
    sb = Sandbox(tmp_path)
    f = tmp_path / "r.csv"
    f.write_text("v1")
    judge = StateJudge(sb, _backend())
    checks = _checks({"kind": "file_sha_changed", "arg": "r.csv"})
    base = judge.snapshot(["r.csv", "absent.txt"])
    assert set(base) == {"r.csv", "absent.txt"} and base["absent.txt"] is None
    assert len(base["r.csv"]) == 64
    v = judge.verify(checks, "j", baseline=base)
    assert v.ok is False and "unchanged" in v.reason
    f.write_text("v2")
    assert judge.verify(checks, "j", baseline=base).ok is True
    # with expect: the new digest must match exactly
    new_sha = judge.snapshot(["r.csv"])["r.csv"]
    assert judge.verify(_checks({"kind": "file_sha_changed", "arg": "r.csv", "expect": new_sha}), "j", baseline=base).ok
    assert not judge.verify(_checks({"kind": "file_sha_changed", "arg": "r.csv", "expect": "0" * 64}), "j", baseline=base).ok
    # a file that did not exist at snapshot time and now does counts as changed
    (tmp_path / "absent.txt").write_text("now")
    assert judge.verify(_checks({"kind": "file_sha_changed", "arg": "absent.txt"}), "j", baseline=base).ok
    # no baseline for the path -> cannot decide -> fail with a reason
    v = judge.verify(checks, "j", baseline=None)
    assert v.ok is False and "baseline" in v.reason
    v = judge.verify(checks, "j", baseline={})
    assert v.ok is False and "baseline" in v.reason


def test_state_judge_tree_check_uses_backend_find(tmp_path):
    judge = StateJudge(Sandbox(tmp_path), _backend())
    assert judge.verify(_checks({"kind": "tree", "arg": '{"role":"frame","name":"*Calc*"}'}), "j").ok
    assert judge.verify(_checks({"kind": "tree", "arg": '{"role":"frame","name":"*Calc*"}', "expect": "active"}), "j").ok
    v = judge.verify(_checks({"kind": "tree", "arg": '{"role":"frame","name":"*Calc*"}', "expect": "checked"}), "j")
    assert v.ok is False and "checked" in v.reason
    v = judge.verify(_checks({"kind": "tree", "arg": '{"role":"dialog","name":"Save*"}'}), "j")
    assert v.ok is False and "not found" in v.reason
    # a bare string is a name glob
    assert judge.verify(_checks({"kind": "tree", "arg": "A1"}), "j").ok
    v = judge.verify(_checks({"kind": "tree", "arg": '{"nope":1}'}), "j")
    assert v.ok is False and "selector" in v.reason
    v = judge.verify(_checks({"kind": "tree", "arg": "{broken"}), "j")
    assert v.ok is False


@pytest.mark.parametrize(
    "cmd,ok",
    [
        ("test -f out/report.csv", True),
        ("test -d out", True),
        ("sha256sum out/report.csv", True),
        ("stat out/report.csv", True),
        ("grep -c total out/report.csv", True),
        ("ls out", True),
        ("ls", True),
        ("test -w out/report.csv", False),
        ("rm -rf out", False),
        ("grep total out/report.csv", False),
        ("cat out/report.csv", False),
        ("test -f a; rm -rf /", False),
        ("test -f a && rm x", False),
        ("ls | sh", False),
        ("ls > x", False),
        ("ls $(rm x)", False),
        ("ls `rm x`", False),
        ("stat out/report.csv\nrm x", False),
        ("/usr/bin/ls", False),
        ("", False),
        ("'ls", False),
    ],
)
def test_probe_allow_list(cmd, ok):
    assert is_allowed_probe(cmd) is ok


def test_state_judge_refuses_disallowed_probe_without_running(tmp_path):
    sb = RecordingSandbox(tmp_path)
    judge = StateJudge(sb, _backend())
    v = judge.verify(_checks({"kind": "shell_true", "arg": "rm -rf out"}), "job7")
    assert v.ok is False and v.reason.startswith("probe not allowed")
    assert sb.calls == []
    v = judge.verify(_checks({"kind": "shell_true", "arg": "test -f a; rm -rf /"}), "job7")
    assert v.ok is False and v.reason.startswith("probe not allowed")
    assert sb.calls == []
    # an allowed probe goes through Sandbox.run exactly once as a ProvisionStep with command_string
    v = judge.verify(_checks({"kind": "shell_true", "arg": "test -f out/report.csv"}), "job7")
    assert v.ok is True
    assert len(sb.calls) == 1
    step = sb.calls[0]
    assert step.command_string == "test -f out/report.csv" and step.script is None
    assert step.timeout_seconds == StateJudge.PROBE_TIMEOUT_S
    # expect compares stripped stdout
    assert judge.verify(_checks({"kind": "shell_true", "arg": "grep -c x f", "expect": "ok"}), "job7").ok
    assert not judge.verify(_checks({"kind": "shell_true", "arg": "grep -c x f", "expect": "2"}), "job7").ok
    assert set(PROBE_ALLOW_LIST) == {"test -f", "test -d", "sha256sum", "stat", "grep -c", "ls", "head -c", "command -v"}


def test_probe_allow_list_is_the_single_shared_one():
    from o_engine.cu import feasibility, judge, probes

    assert judge.PROBE_ALLOW_LIST is probes.PROBE_ALLOW_LIST
    assert feasibility.PROBE_ALLOW_LIST is probes.PROBE_ALLOW_LIST
    assert judge.is_allowed_probe is probes.is_allowed_probe
    assert feasibility.is_allowed_probe is probes.is_allowed_probe


@pytest.mark.parametrize(
    "cmd,ok",
    [
        ("head -c 4000 -- x", True),
        ("command -v -- gedit", True),
        ("head x | sh", False),
        ("head x", False),
        ("command gedit", False),
    ],
)
def test_probe_allow_list_feasibility_templates(cmd, ok):
    assert is_allowed_probe(cmd) is ok


def test_state_judge_shell_true_runs_in_real_sandbox(tmp_path):
    sb = Sandbox(tmp_path)
    judge = StateJudge(sb, _backend())
    assert judge.verify(_checks({"kind": "shell_true", "arg": "test -f r.csv"}), "j").ok is False
    (tmp_path / "r.csv").write_text("x\n")
    assert judge.verify(_checks({"kind": "shell_true", "arg": "test -f r.csv"}), "j").ok is True
    assert judge.verify(_checks({"kind": "shell_true", "arg": "grep -c x r.csv", "expect": "1"}), "j").ok is True


def test_state_judge_all_checks_must_hold_and_stops_at_first_failure(tmp_path):
    sb = RecordingSandbox(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    judge = StateJudge(sb, _backend())
    v = judge.verify(
        _checks(
            {"kind": "file_exists", "arg": "a.txt"},
            {"kind": "file_exists", "arg": "b.txt"},
            {"kind": "shell_true", "arg": "ls"},
        ),
        "j",
    )
    assert v.ok is False and "check 2" in v.reason and "b.txt" in v.reason
    assert sb.calls == []  # the shell probe after the failing check never ran
    v = judge.verify(_checks({"kind": "file_exists", "arg": "a.txt"}, {"kind": "shell_true", "arg": "ls"}), "j")
    assert v.ok is True and len(sb.calls) == 1
    assert judge.model_calls == 0


def test_state_judge_makes_no_model_call(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("StateJudge must not call a model")

    monkeypatch.setattr(judge_mod, "OpenAI", boom)
    monkeypatch.setattr(judge_mod.subprocess, "run", boom)
    sb = Sandbox(tmp_path)
    (tmp_path / "r.csv").write_text("x")
    judge = StateJudge(sb, _backend())
    assert judge.verify(_checks({"kind": "file_exists", "arg": "r.csv"}, {"kind": "tree", "arg": "A1"}), "j").ok
