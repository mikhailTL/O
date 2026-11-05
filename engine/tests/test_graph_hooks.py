"""Execute-node hooks: script steps go to the Runner, high-risk requests to containment,
and cached replay of script steps works through a real Sandbox + ScriptCache."""
import sys
import types
from dataclasses import dataclass, field

import pytest

import o_engine.containment as containment
from o_engine.actionlog import ActionLog
from o_engine.cache import ScriptCache, render_script
from o_engine.executor import Sandbox, StepResult
from o_engine.graph import GRAPH_EDGES, Deps, build_graph, run_job, step_label
from o_engine.schema import ProvisionRequest

EDGES_SNAPSHOT = (
    ("__start__", "context"), ("context", "validate"), ("validate", "cache_lookup"), ("cache_lookup", "gate"),
    ("gate", "execute"), ("execute", "verify"), ("verify", "cache_store"), ("verify", "remediate"),
    ("verify", "report"), ("remediate", "gate"), ("cache_store", "report"), ("report", "__end__"),
)


class Profile:
    def model_dump(self):
        return {"cwd": "proj"}


class StubContext:
    def profile(self, cwd):
        return Profile()


class StubParser:
    def parse(self, text, goal):
        from o_engine.schema import SchemaViolation

        raise SchemaViolation("stub parser has no plan")

    def describe(self):
        return "stub"


class StubDisplay:
    def env(self):
        return {"DISPLAY": ":99"}


class RecordingSandbox:
    """A Sandbox stand-in that records every call and fails the test if used where it must not be."""

    def __init__(self, root, forbid=False):
        self.root = root
        self.runs = []
        self.forbid = forbid

    def run(self, step, index=0, env_extra=None, isolated_display=False):
        if self.forbid:
            raise AssertionError("sandbox.run must not be called")
        self.runs.append((index, step.command_string, env_extra, isolated_display))
        return StepResult(index=index, command=step.command_string, cwd=step.target_directory, exit_code=0,
                          stdout="ok\n", stderr="", elapsed_ms=1, timed_out=False)


class StubRunner:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def run(self, payload, sandbox, index=0, env_extra=None, isolated_display=False):
        self.calls.append({"payload": payload, "sandbox": sandbox, "index": index, "env_extra": env_extra,
                           "isolated_display": isolated_display})
        code = 1 if index == self.fail_at else 0
        return StepResult(index=index, command=f"{payload.language}:{payload.code[:20]}", cwd=payload.target_directory,
                          exit_code=code, stdout="ran\n", stderr="" if code == 0 else "boom\n", elapsed_ms=2, timed_out=False)


@dataclass
class StubCacheEntry:
    fingerprint: str
    runs: int = 0


class StubCache:
    def __init__(self):
        self.entries = {}
        self.stores = []
        self.replays = []

    def lookup(self, fp):
        return self.entries.get(fp)

    def store(self, fp, request, platform):
        self.entries[fp] = StubCacheEntry(fp)
        self.stores.append(fp)

    def replay(self, entry, sandbox):
        self.replays.append(entry.fingerprint)
        return StepResult(index=0, command="replay", cwd="proj", exit_code=0, stdout="", stderr="", elapsed_ms=1, timed_out=False)

    def bump_runs(self, fp):
        self.entries[fp].runs += 1


def deps_for(tmp_data_dir, sandbox, cache=None, runner=None, **kw):
    return Deps(
        context=StubContext(), parser=StubParser(), cache=cache or StubCache(),
        verify_target=lambda t: {"ok": True, "status": 200, "elapsed_ms": 1, "error": None},
        display=StubDisplay(), log=ActionLog(tmp_data_dir), sandbox=sandbox, max_attempts=1, platform="linux",
        runner=runner, **kw,
    )


def script_step(code, language="python", directory="proj", timeout=5):
    return {"target_directory": directory, "timeout_seconds": timeout,
            "script": {"language": language, "code": code, "target_directory": directory, "timeout_seconds": timeout}}


def test_graph_edges_unchanged():
    assert GRAPH_EDGES == EDGES_SNAPSHOT


def test_execute_dispatches_script_steps_to_runner_and_commands_to_sandbox(tmp_data_dir, tmp_path):
    sandbox = RecordingSandbox(tmp_path / "sb")
    runner = StubRunner()
    deps = deps_for(tmp_data_dir, sandbox, runner=runner)
    graph = build_graph(deps)
    req = ProvisionRequest(label="mixed", isolated_display=True, steps=[
        {"target_directory": "proj", "command_string": "echo one", "timeout_seconds": 5},
        script_step("print('two')"),
        {"target_directory": "proj", "command_string": "echo three", "timeout_seconds": 5},
    ])
    out = run_job(graph, req, "j1", "log", None, 1)
    assert out["ok"] is True and out["done"] is True, out
    assert [r[1] for r in sandbox.runs] == ["echo one", "echo three"]
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["payload"].language == "python" and call["payload"].code == "print('two')"
    assert call["sandbox"] is sandbox and call["index"] == 1
    assert call["env_extra"] == {"DISPLAY": ":99"} and call["isolated_display"] is True
    assert [s["index"] for s in out["steps"]] == [0, 1, 2]
    assert out["steps"][1]["command"].startswith("python:")
    assert out["steps"][1]["cwd"] == "proj"


def test_failing_script_step_stops_execution(tmp_data_dir, tmp_path):
    sandbox = RecordingSandbox(tmp_path / "sb")
    runner = StubRunner(fail_at=0)
    graph = build_graph(deps_for(tmp_data_dir, sandbox, runner=runner))
    req = ProvisionRequest(label="fail", steps=[
        script_step("raise SystemExit(1)"),
        {"target_directory": "proj", "command_string": "echo never", "timeout_seconds": 5},
    ])
    out = run_job(graph, req, "j2", "log", None, 1)
    assert out["ok"] is False
    assert sandbox.runs == []
    assert [s["exit_code"] for s in out["steps"]] == [1]


def test_runner_is_built_lazily_from_runners_module_when_none_given(tmp_data_dir, tmp_path, monkeypatch):
    runner = StubRunner()
    fake = types.ModuleType("o_engine.runners")
    asked = []

    def get_runner(prefer="native"):
        asked.append(prefer)
        return runner

    fake.get_runner = get_runner
    monkeypatch.setitem(sys.modules, "o_engine.runners", fake)
    sandbox = RecordingSandbox(tmp_path / "sb")
    deps = deps_for(tmp_data_dir, sandbox)
    assert deps.runner is None
    graph = build_graph(deps)
    out = run_job(graph, ProvisionRequest(label="lazy", steps=[script_step("print(1)")]), "j3", "log", None, 1)
    assert out["ok"] is True
    assert asked == ["native"] and deps.runner is runner and len(runner.calls) == 1


def test_high_risk_routes_to_containment_and_never_calls_sandbox_run(tmp_data_dir, tmp_path, monkeypatch):
    sandbox = RecordingSandbox(tmp_path / "sb", forbid=True)
    runner = StubRunner()
    seen = {}

    def fake_run_high_risk(request, job_dir, preference, settings_like, log, local_sandbox, job_id="", runner=None):
        seen.update(request=request, job_dir=job_dir, preference=preference, settings_like=settings_like,
                    log=log, local_sandbox=local_sandbox, job_id=job_id, runner=runner)
        return [
            StepResult(index=0, command="docker: echo hi", cwd="/work", exit_code=0, stdout="hi\n", stderr="", elapsed_ms=3, timed_out=False),
            StepResult(index=1, command="", cwd="", exit_code=0, stdout="", stderr="", elapsed_ms=3, timed_out=False),
        ]

    monkeypatch.setattr(containment, "run_high_risk", fake_run_high_risk)
    settings = {"dockerImage": "python:3.12-slim"}
    deps = deps_for(tmp_data_dir, sandbox, runner=runner, high_risk_sandbox="e2b", settings_like=settings)
    graph = build_graph(deps)
    req = ProvisionRequest(label="risky", risk="high", steps=[
        {"target_directory": "proj", "command_string": "echo hi", "timeout_seconds": 5},
        script_step("print('x')"),
    ])
    out = run_job(graph, req, "j4", "log", None, 1)
    assert out["ok"] is True, out
    assert seen["request"].risk == "high" and seen["request"].label == "risky"
    assert seen["job_dir"] == sandbox.root and seen["local_sandbox"] is sandbox
    assert seen["preference"] == "e2b" and seen["settings_like"] is settings and seen["job_id"] == "j4"
    assert seen["runner"] is runner  # the local fallback needs it for script steps
    assert sandbox.runs == [] and runner.calls == []
    assert [s["index"] for s in out["steps"]] == [0, 1]
    assert out["steps"][0]["command"] == "docker: echo hi"
    assert out["steps"][1]["command"].startswith("[python script") and out["steps"][1]["cwd"] == "proj"


def _stub_docker(monkeypatch):
    """Docker 'available'; every ``docker run`` succeeds and is recorded. Returns the argv list."""
    import subprocess

    monkeypatch.setattr(containment.DockerSandbox, "available", property(lambda self: True))
    runs = []

    def fake_run(argv, **kw):
        if argv[1] == "run":
            runs.append(argv)
        return subprocess.CompletedProcess(argv, 0, "docker-out\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return runs


def test_cached_high_risk_request_replays_inside_disposable_layer_never_on_host(tmp_data_dir, tmp_path, monkeypatch):
    docker_runs = _stub_docker(monkeypatch)
    sandbox = RecordingSandbox(tmp_path / "sb", forbid=True)
    cache = ScriptCache(tmp_path / "cache")
    monkeypatch.setattr(cache, "replay", lambda *a, **k: (_ for _ in ()).throw(AssertionError("host replay must not run")))
    runner = StubRunner()
    graph = build_graph(deps_for(tmp_data_dir, sandbox, cache=cache, runner=runner, high_risk_sandbox="docker"))
    req = ProvisionRequest(label="risky", risk="high", steps=[
        {"target_directory": "proj", "command_string": "echo hi", "timeout_seconds": 5},
    ])
    first = run_job(graph, req, "h1", "log", None, 1)
    assert first["ok"] is True and first["cached"] is False, first
    assert len(docker_runs) == 1
    fp = req.fingerprint("linux")
    entry = cache.lookup(fp)
    assert entry is not None and entry.runs == 0

    second = run_job(graph, ProvisionRequest(label="again", risk="high", steps=req.steps), "h2", "log", None, 1)
    assert second["ok"] is True and second["cached"] is True, second
    assert sandbox.runs == [] and runner.calls == []
    assert len(docker_runs) == 2  # exactly one more docker run for the replay
    argv = docker_runs[1]
    assert argv[argv.index("-w") + 1] == "/work"
    # The cached script is a script payload: DockerSandbox writes it under <job dir>/.o-scripts/
    # and runs it through its own script path (bash /work/.o-scripts/step-<id>.sh).
    assert argv[-2] == "-c" and argv[-1].startswith("bash /work/.o-scripts/step-") and argv[-1].endswith(".sh")
    written = sorted((tmp_path / "sb" / ".o-scripts").glob("step-*.sh"))
    assert len(written) == 1
    assert written[0].read_text() == (cache.dir / entry.script_path).read_text()
    assert not (tmp_path / "sb" / ".o-scripts" / f"{fp}.sh").exists()  # no host-side copy by fingerprint
    assert len(second["steps"]) == 1 and second["steps"][0]["exit_code"] == 0
    assert second["steps"][0]["command"].startswith("[bash script] /work/.o-scripts/step-")
    assert cache.lookup(fp).runs == 1


def test_cached_replay_request_is_a_script_payload_or_none_when_too_long(tmp_path):
    from o_engine.cache import ScriptCache
    from o_engine.graph import cached_replay_request
    from o_engine.schema import MAX_SCRIPT_LENGTH

    cache = ScriptCache(tmp_path / "cache")
    req = ProvisionRequest(label="r", risk="high", steps=[
        {"target_directory": "proj", "command_string": "echo hi", "timeout_seconds": 5},
    ])
    entry = cache.store(req.fingerprint("linux"), req, "linux")
    replay = cached_replay_request(cache, entry, tmp_path / "job", req)
    assert replay is not None and len(replay.steps) == 1
    step = replay.steps[0]
    assert step.command_string is None and step.script is not None
    assert step.script.language == "bash" and step.script.target_directory == "." and step.target_directory == "."
    assert step.script.code == (cache.dir / entry.script_path).read_text()
    assert step.script.timeout_seconds == entry.timeout_seconds == step.timeout_seconds
    assert replay.risk == "high" and replay.label == "r"
    assert not (tmp_path / "job").exists()  # nothing is copied into the job directory

    (cache.dir / entry.script_path).write_text("#!/bin/bash\n" + "x" * MAX_SCRIPT_LENGTH)
    assert cached_replay_request(cache, entry, tmp_path / "job", req) is None

    ps1 = cache.store(req.fingerprint("win32"), req, "win32")
    assert ps1.script_path.endswith(".ps1")
    assert cached_replay_request(cache, ps1, tmp_path / "job", req).steps[0].script.language == "powershell"


def test_cached_high_risk_request_replays_in_e2b_by_uploading_the_script(tmp_data_dir, tmp_path, monkeypatch):
    writes, cmds, kills = {}, [], []

    class Out:
        exit_code, stdout, stderr = 0, "e2b-out\n", ""

    class FakeE2B:
        def __init__(self, template=None, api_key=None):
            self.files = types.SimpleNamespace(write=lambda p, c: writes.__setitem__(p, c), read=lambda p, format=None: b"")
            self.commands = types.SimpleNamespace(run=lambda cmd, cwd=None, timeout=None: (cmds.append((cmd, cwd, timeout)), Out())[1])

        def kill(self):
            kills.append(self)

    monkeypatch.setitem(sys.modules, "e2b", types.SimpleNamespace(Sandbox=FakeE2B))
    sandbox = RecordingSandbox(tmp_path / "sb", forbid=True)
    cache = ScriptCache(tmp_path / "cache")
    monkeypatch.setattr(cache, "replay", lambda *a, **k: (_ for _ in ()).throw(AssertionError("host replay must not run")))
    runner = StubRunner()
    graph = build_graph(deps_for(tmp_data_dir, sandbox, cache=cache, runner=runner, high_risk_sandbox="e2b",
                                 settings_like={"e2bApiKey": "k"}))
    req = ProvisionRequest(label="risky", risk="high", steps=[
        {"target_directory": "proj", "command_string": "echo hi", "timeout_seconds": 5},
    ])
    first = run_job(graph, req, "e1", "log", None, 1)
    assert first["ok"] is True and first["cached"] is False, first
    assert len(kills) == 1
    fp = req.fingerprint("linux")
    entry = cache.lookup(fp)
    assert entry is not None

    writes.clear()
    cmds.clear()
    second = run_job(graph, ProvisionRequest(label="again", risk="high", steps=req.steps), "e2", "log", None, 1)
    assert second["ok"] is True and second["cached"] is True, second
    assert sandbox.runs == [] and runner.calls == []
    assert len(kills) == 2  # exactly one more instance, killed exactly once
    assert len(writes) == 1
    (path, content), = writes.items()
    assert path.startswith("/work/.o-scripts/") and path.endswith(".sh")
    assert content == (cache.dir / entry.script_path).read_text()
    run_cmds = [c for c in cmds if not c[0].startswith("mkdir")]
    assert len(run_cmds) == 1
    assert run_cmds[0] == (f"bash {path}", "/work", entry.timeout_seconds)
    assert not (tmp_path / "sb" / ".o-scripts").exists()  # nothing written on the host for E2B
    assert cache.lookup(fp).runs == 1


def test_cached_high_risk_failed_replay_does_not_bump_runs(tmp_data_dir, tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(containment.DockerSandbox, "available", property(lambda self: True))
    codes = [0, 3]  # first run stores the script; every docker run after that fails
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, codes.pop(0) if argv[1] == "run" and len(codes) > 1 else codes[0], "", "bad"))
    sandbox = RecordingSandbox(tmp_path / "sb", forbid=True)
    cache = ScriptCache(tmp_path / "cache")
    monkeypatch.setattr(cache, "replay", lambda *a, **k: (_ for _ in ()).throw(AssertionError("host replay must not run")))
    graph = build_graph(deps_for(tmp_data_dir, sandbox, cache=cache, runner=StubRunner()))
    req = ProvisionRequest(label="risky", risk="high", steps=[{"target_directory": "proj", "command_string": "echo hi", "timeout_seconds": 5}])
    assert run_job(graph, req, "h3", "log", None, 1)["ok"] is True
    second = run_job(graph, req, "h4", "log", None, 1)
    assert second["ok"] is False and "exited 3" in second["error"]
    assert cache.lookup(req.fingerprint("linux")).runs == 0  # bump only on success
    assert sandbox.runs == []


def test_cached_high_risk_request_reruns_steps_when_script_cannot_be_mapped(tmp_data_dir, tmp_path, monkeypatch):
    sandbox = RecordingSandbox(tmp_path / "sb", forbid=True)
    cache = StubCache()
    seen = []

    def fake_run_high_risk(request, job_dir, preference, settings_like, log, local_sandbox, job_id="", **kw):
        seen.append([step_label(s) for s in request.steps])
        return [StepResult(index=i, command="", cwd="", exit_code=0, stdout="", stderr="", elapsed_ms=1, timed_out=False)
                for i in range(len(request.steps))]

    monkeypatch.setattr(containment, "run_high_risk", fake_run_high_risk)
    graph = build_graph(deps_for(tmp_data_dir, sandbox, cache=cache, runner=StubRunner()))
    req = ProvisionRequest(label="risky", risk="high", steps=[
        {"target_directory": "proj", "command_string": "echo hi", "timeout_seconds": 5},
        script_step("print('x')"),
    ])
    assert run_job(graph, req, "h5", "log", None, 1)["ok"] is True
    second = run_job(graph, req, "h6", "log", None, 1)
    assert second["ok"] is True and second["cached"] is True, second
    assert cache.replays == []  # the host replay path is never taken for a high-risk request
    assert seen == [["echo hi", "[python script, 10 chars] print('x')"]] * 2
    assert cache.entries[req.fingerprint("linux")].runs == 1
    assert sandbox.runs == []


def test_high_risk_failure_is_reported(tmp_data_dir, tmp_path, monkeypatch):
    sandbox = RecordingSandbox(tmp_path / "sb", forbid=True)

    def fake_run_high_risk(request, job_dir, preference, settings_like, log, local_sandbox, job_id="", **kw):
        return [StepResult(index=0, command="x", cwd="", exit_code=7, stdout="", stderr="bad", elapsed_ms=1, timed_out=False)]

    monkeypatch.setattr(containment, "run_high_risk", fake_run_high_risk)
    graph = build_graph(deps_for(tmp_data_dir, sandbox, runner=StubRunner()))
    req = ProvisionRequest(label="risky", risk="high", steps=[{"target_directory": "proj", "command_string": "echo hi", "timeout_seconds": 5}])
    out = run_job(graph, req, "j5", "log", None, 1)
    assert out["ok"] is False and "exited 7" in out["error"]
    assert sandbox.runs == []


def test_render_script_heredocs_script_steps_sh_and_ps1():
    req = ProvisionRequest(label="r", steps=[
        {"target_directory": "proj", "command_string": "echo one", "timeout_seconds": 5},
        script_step("import sys\nprint('hi O_STEP_EOF')\nsys.exit(0)\n"),
        script_step("console.log(1)", language="javascript"),
    ])
    sh = render_script(req, "linux")
    assert "cat > .o-step-1.py <<'O_STEP_EOF_'" in sh
    assert "python3 -I .o-step-1.py || __o_rc=$?" in sh
    assert "rm -f .o-step-1.py" in sh
    assert "node .o-step-2.js" in sh
    assert "print('hi O_STEP_EOF')" in sh
    ps = render_script(req, "win32")
    assert "Set-Content -LiteralPath '.o-step-1.py'" in ps and "@'\nimport sys" in ps
    assert "& python -I '.o-step-1.py'" in ps and "Remove-Item -LiteralPath '.o-step-1.py'" in ps
    assert "& node '.o-step-2.js'" in ps


def test_cached_replay_of_script_request_runs_python_through_real_sandbox(tmp_data_dir, tmp_path):
    root = tmp_path / "sb"
    (root / "proj").mkdir(parents=True)
    sandbox = Sandbox(root)
    cache = ScriptCache(tmp_path / "cache")
    runner = StubRunner()
    deps = deps_for(tmp_data_dir, sandbox, cache=cache, runner=runner)
    graph = build_graph(deps)
    code = "from pathlib import Path\nPath('made-by-python.txt').write_text('42')\nprint('wrote')\n"
    req = ProvisionRequest(label="cache me", steps=[
        {"target_directory": "proj", "command_string": "echo first", "timeout_seconds": 5},
        script_step(code),
    ])
    first = run_job(graph, req, "c1", "log", None, 1)
    assert first["ok"] is True and first["cached"] is False, first
    assert len(runner.calls) == 1
    assert not (root / "proj" / "made-by-python.txt").exists()  # the stub runner did not run the code
    fp = req.fingerprint("linux")
    assert cache.lookup(fp) is not None

    second = run_job(graph, ProvisionRequest(label="again", steps=req.steps), "c2", "log", None, 1)
    assert second["ok"] is True and second["cached"] is True, second
    assert len(runner.calls) == 1  # replay bypasses the Runner
    assert (root / "proj" / "made-by-python.txt").read_text() == "42"
    assert not (root / "proj" / ".o-step-1.py").exists()
    assert "wrote" in second["steps"][0]["stdout_tail"]
    assert cache.lookup(fp).runs == 1


def test_step_label_for_scripts_and_commands():
    req = ProvisionRequest(label="l", steps=[
        {"target_directory": "p", "command_string": "echo one", "timeout_seconds": 5},
        script_step("  \nprint('x')\n", language="python"),
    ])
    assert step_label(req.steps[0]) == "echo one"
    assert step_label(req.steps[1]) == "[python script, 14 chars] print('x')"
