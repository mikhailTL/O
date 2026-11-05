import json
import sys

import pytest

from o_engine.actionlog import ActionLog
from o_engine.context import WorkspaceProfile
from o_engine.schema import SchemaViolation, parse_steps
from o_engine.swarm import (
    ROLE_NAMES,
    CrewAICrew,
    SimpleCrew,
    SwarmDeps,
    SwarmResult,
    get_crew,
    profile_summary,
    swarm_summary,
)


class StubContext:
    def __init__(self):
        self.calls = []

    def profile(self, cwd, telemetry_seconds=120):
        self.calls.append(cwd)
        return WorkspaceProfile(
            cwd=cwd, collected_at=1.0, shell_history=["npm install", "npm test"],
            tree={"name": "proj", "type": "dir", "children": [{"name": "package.json", "type": "file"}]},
            config_files=[{"path": f"{cwd}/package.json", "sha256": "ab", "head": "{}"}],
            telemetry=[{"ts": 1.0, "app": "term", "window": "w", "text": "ERR"}], platform="linux",
        )


class StubParser:
    def __init__(self, steps=None, fail=False):
        self.steps = steps or [{"target_directory": ".", "command_string": "npm install", "timeout_seconds": 60}]
        self.fail = fail
        self.texts = []

    def describe(self):
        return "stub"

    def parse(self, text, goal):
        self.texts.append((text, goal))
        if self.fail:
            raise SchemaViolation("all parser backends failed (garbage)")
        return parse_steps(json.dumps(self.steps), goal)


class StubJudge:
    def ask(self, png, question):
        return True, f"asked: {question[:20]}"


@pytest.fixture
def log(tmp_data_dir):
    return ActionLog(tmp_data_dir)


def notes(log):
    if not log.jsonl.is_file():
        return []
    return [json.loads(ln) for ln in log.jsonl.read_text().splitlines() if ln.strip()]


def test_simple_crew_runs_three_roles(log, tmp_path):
    ctx, parser = StubContext(), StubParser()
    crew = SimpleCrew(SwarmDeps(context=ctx, parser=parser, verify_target=lambda t: {"ok": True}), log)
    result = crew.run("fix the build", str(tmp_path))
    assert isinstance(result, SwarmResult)
    assert [r.role for r in result.roles] == list(ROLE_NAMES)
    assert all(r.ok for r in result.roles)
    assert result.profile["cwd"] == str(tmp_path) and result.profile["shell_history"] == ["npm install", "npm test"]
    assert result.request is not None and result.request.steps[0].command_string == "npm install"
    assert result.request.label == "fix the build"
    text, goal = parser.texts[0]
    assert goal == "fix the build" and "npm test" in text and "Workspace profile" in text
    assert result.inspection == {}
    assert result.roles[2].note == "nothing to inspect"
    recorded = notes(log)
    assert len(recorded) == 3 and all(e["kind"] == "note" for e in recorded)
    assert [e["detail"]["role"] for e in recorded] == list(ROLE_NAMES)
    assert all("elapsed_ms" in e["detail"] for e in recorded)
    assert json.loads(swarm_summary(result))["steps"] == 1


def test_simple_crew_parser_garbage_gives_none_request(log, tmp_path):
    crew = SimpleCrew(SwarmDeps(context=StubContext(), parser=StubParser(fail=True), verify_target=lambda t: {"ok": True}), log)
    result = crew.run("do it", str(tmp_path))
    assert result.request is None
    compiler = result.roles[1]
    assert compiler.ok is False and "schema violation" in compiler.note and "garbage" in compiler.note
    assert result.roles[0].ok and result.roles[2].ok
    assert result.roles[2].note == "nothing to inspect"


def test_inspector_probes_verify_and_judges_display(log, tmp_path):
    steps = [{"target_directory": ".", "command_string": "npm start", "timeout_seconds": 60}]

    class VerifyingParser(StubParser):
        def parse(self, text, goal):
            from o_engine.schema import ProvisionRequest

            return ProvisionRequest(label=goal, steps=parse_steps(json.dumps(steps), goal).steps,
                                    verify={"port": 3000, "path": "/", "expect_status": 200, "timeout_seconds": 5})

    probed = []

    def verify_target(t):
        probed.append(t.port)
        return {"ok": False, "status": None, "error": "refused"}

    deps = SwarmDeps(context=StubContext(), parser=VerifyingParser(), verify_target=verify_target,
                     judge=StubJudge(), screenshot=lambda: b"\x89PNG")
    result = SimpleCrew(deps, log).run("start the dev server", str(tmp_path))
    assert probed == [3000]
    assert result.inspection["verify"]["ok"] is False
    assert result.inspection["display"] == {"ok": True, "reason": "asked: Does the screen show"}
    assert "verify port 3000: not answering" in result.roles[2].note and "display judge: ok" in result.roles[2].note


def test_role_exception_is_contained(log, tmp_path):
    class BrokenContext:
        def profile(self, cwd):
            raise OSError("disk")

    result = SimpleCrew(SwarmDeps(context=BrokenContext(), parser=StubParser(), verify_target=lambda t: {}), log).run("x", str(tmp_path))
    assert result.roles[0].ok is False and "OSError" in result.roles[0].note
    assert result.profile == {}
    assert result.request is not None  # compiler still runs with an empty profile


def test_profile_summary_is_bounded():
    s = profile_summary({"cwd": "/p", "platform": "linux", "shell_history": ["x" * 500] * 50})
    assert len(s) <= 4000 and s.startswith("cwd: /p")
    assert profile_summary({}) == ""


def test_crewai_unavailable_here(monkeypatch, log):
    monkeypatch.setitem(sys.modules, "crewai", None)
    crew = CrewAICrew(SwarmDeps(context=StubContext(), parser=StubParser(), verify_target=lambda t: {}), log)
    assert crew.available is False and "crewai not importable" in crew.reason
    with pytest.raises(RuntimeError):
        crew.run("x", ".")


def test_get_crew_returns_simple_when_crewai_missing(monkeypatch, log):
    monkeypatch.setitem(sys.modules, "crewai", None)
    deps = SwarmDeps(context=StubContext(), parser=StubParser(), verify_target=lambda t: {})
    crew = get_crew(deps, log, {"swarmEnabled": True})
    assert isinstance(crew, SimpleCrew)
    assert any("using SimpleCrew" in e["summary"] for e in notes(log))


def test_get_crew_disabled_skips_crewai(monkeypatch, log):
    deps = SwarmDeps(context=StubContext(), parser=StubParser(), verify_target=lambda t: {})
    assert isinstance(get_crew(deps, log, {"swarmEnabled": False}), SimpleCrew)
    assert isinstance(get_crew(deps, log, {"swarm_enabled": "false"}), SimpleCrew)
    assert notes(log) == []
