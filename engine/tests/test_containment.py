import shutil
import subprocess
import sys

import pytest

from o_engine.actionlog import ActionLog
from o_engine.containment import (
    TIMEOUT_EXIT_CODE,
    DockerSandbox,
    E2BSandbox,
    DisposableSandbox,
    run_high_risk,
    select_sandbox,
)
from o_engine.executor import Sandbox, StepResult
from o_engine.schema import ProvisionRequest, ProvisionStep


def step(cmd="echo hi", d=".", t=30):
    return ProvisionStep(target_directory=d, command_string=cmd, timeout_seconds=t)


@pytest.fixture
def log(tmp_data_dir):
    return ActionLog(tmp_data_dir)


def entries(log, kind=None):
    import json

    if not log.jsonl.is_file():
        return []
    rows = [json.loads(ln) for ln in log.jsonl.read_text().splitlines() if ln.strip()]
    return [r for r in rows if kind is None or r["kind"] == kind]


# --- DockerSandbox ---------------------------------------------------------------------


def test_docker_unavailable_when_binary_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    sb = DockerSandbox("python:3.12-slim", tmp_path)
    assert sb.available is False
    assert "not on PATH" in sb.reason


def test_docker_unavailable_when_daemon_down(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")

    def fake_run(argv, **kw):
        assert argv[1] == "info"
        assert kw["timeout"] == 5.0
        return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to the Docker daemon")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sb = DockerSandbox("python:3.12-slim", tmp_path)
    assert sb.available is False
    assert "daemon" in sb.reason


def test_docker_unavailable_when_info_hangs(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    sb = DockerSandbox("python:3.12-slim", tmp_path)
    assert sb.available is False
    assert "5 s" in sb.reason


def test_docker_run_builds_isolated_argv(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "out\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sb = DockerSandbox("python:3.12-slim", tmp_path, env={"PATH": "/usr/bin", "O_ENGINE_TOKEN": "x", "MY_SECRET": "y"})
    res = sb.run(step("pip install foo", d="app", t=42), index=3)
    argv, kw = calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--network") + 1] == "bridge"  # default network
    assert f"{tmp_path.resolve()}:/work" in argv and argv[argv.index("-v") + 1] == f"{tmp_path.resolve()}:/work"
    assert argv[argv.index("-w") + 1] == "/work/app"
    assert argv[argv.index("--name") + 1].startswith("o-hr-")
    assert "-e" in argv and "PATH=/usr/bin" in argv
    assert not any(a.startswith(("O_ENGINE_TOKEN", "MY_SECRET")) for a in argv)
    assert argv[-4:] == ["python:3.12-slim", "sh", "-c", "pip install foo"]
    assert kw["timeout"] == 42
    assert isinstance(res, StepResult)
    assert res.index == 3 and res.exit_code == 0 and res.stdout == "out\n" and res.cwd == "/work/app"
    assert res.command == "pip install foo" and res.timed_out is False


def _argv_capture(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_docker_network_default_is_bridge(monkeypatch, tmp_path):
    monkeypatch.delenv("O_DOCKER_NETWORK", raising=False)
    sb = DockerSandbox("img", tmp_path)
    assert sb.network == "bridge"
    calls = _argv_capture(monkeypatch)
    sb.run(step("ls"))
    assert calls[0][calls[0].index("--network") + 1] == "bridge"


def test_docker_network_none_still_selectable(monkeypatch, tmp_path):
    sb = DockerSandbox("img", tmp_path, network="none")
    calls = _argv_capture(monkeypatch)
    sb.run(step("ls"))
    assert calls[0][calls[0].index("--network") + 1] == "none"


def test_docker_network_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("O_DOCKER_NETWORK", "host")
    monkeypatch.setattr(DockerSandbox, "available", property(lambda self: True))
    chosen = select_sandbox("docker", tmp_path, None, None)
    assert isinstance(chosen, DockerSandbox) and chosen.network == "host"
    calls = _argv_capture(monkeypatch)
    chosen.run(step("ls"))
    assert calls[0][calls[0].index("--network") + 1] == "host"


def test_docker_network_from_settings_camel_and_snake(monkeypatch, tmp_path):
    monkeypatch.setenv("O_DOCKER_NETWORK", "host")  # settings win over the env
    monkeypatch.setattr(DockerSandbox, "available", property(lambda self: True))
    assert select_sandbox("docker", tmp_path, {"dockerNetwork": "none"}, None).network == "none"
    assert select_sandbox("docker", tmp_path, {"docker_network": "mynet"}, None).network == "mynet"


def test_docker_network_blank_setting_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("O_DOCKER_NETWORK", raising=False)
    monkeypatch.setattr(DockerSandbox, "available", property(lambda self: True))
    assert select_sandbox("docker", tmp_path, {"dockerNetwork": ""}, None).network == "bridge"
    assert DockerSandbox("img", tmp_path, network="").network == "bridge"


def test_run_high_risk_logs_docker_network(monkeypatch, tmp_path, log):
    monkeypatch.setenv("O_DOCKER_NETWORK", "none")
    monkeypatch.setattr(DockerSandbox, "available", property(lambda self: True))
    _argv_capture(monkeypatch)
    req = ProvisionRequest(label="x", steps=[step("a")])
    run_high_risk(req, tmp_path, "docker", {}, log, job_id="j9")
    notes = entries(log, "note")
    running = [n for n in notes if "running in docker sandbox" in n["summary"]]
    assert len(running) == 1
    assert running[0]["detail"]["network"] == "none"
    assert "network=none" in running[0]["summary"]
    results = entries(log, "result")
    assert results and results[0]["detail"]["network"] == "none"


def test_docker_run_timeout_gives_124_and_kills_container(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"partial", stderr=b"")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sb = DockerSandbox("img", tmp_path)
    res = sb.run(step("sleep 100", t=1))
    assert res.exit_code == TIMEOUT_EXIT_CODE and res.timed_out is True and res.stdout == "partial"
    name = calls[0][calls[0].index("--name") + 1]
    assert ["docker", "kill", name] in calls
    assert ["docker", "rm", "-f", name] in calls


def test_docker_rejects_escaping_target(tmp_path):
    sb = DockerSandbox("img", tmp_path)
    with pytest.raises(ValueError):
        sb.argv(step("ls", d="../../etc"), "n")


def test_docker_extract_and_destroy_keep_job_dir(monkeypatch, tmp_path):
    (tmp_path / "out.txt").write_bytes(b"data")
    sb = DockerSandbox("img", tmp_path)
    assert sb.extract("out.txt") == b"data"
    with pytest.raises(ValueError):
        sb.extract("../x")
    killed = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: killed.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    sb._containers.append("o-hr-left")
    sb.destroy()
    assert (tmp_path / "out.txt").read_bytes() == b"data"
    assert ["docker", "kill", "o-hr-left"] in killed
    assert sb._containers == []


def test_docker_is_context_manager(monkeypatch, tmp_path):
    sb = DockerSandbox("img", tmp_path)
    assert isinstance(sb, DisposableSandbox)
    with sb as inner:
        assert inner is sb


# --- E2BSandbox -------------------------------------------------------------------------


def test_e2b_without_key():
    sb = E2BSandbox(api_key="")
    assert sb.available is False and sb.reason == "E2B_API_KEY not set"


def test_e2b_without_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "e2b", None)
    sb = E2BSandbox(api_key="k")
    assert sb.available is False and "e2b SDK not importable" in sb.reason


def test_e2b_run_maps_sdk_result(monkeypatch):
    class Out:
        exit_code, stdout, stderr = 0, "ok\n", ""

    class FakeCommands:
        calls = []

        def run(self, cmd, cwd=None, timeout=None):
            self.calls.append((cmd, cwd, timeout))
            return Out()

    class FakeFiles:
        def read(self, path, format="text"):
            return b"bytes:" + path.encode()

    class FakeE2B:
        killed = False

        def __init__(self, template=None, api_key=None):
            self.commands = FakeCommands()
            self.files = FakeFiles()

        def kill(self):
            FakeE2B.killed = True

    import types

    monkeypatch.setitem(sys.modules, "e2b", types.SimpleNamespace(Sandbox=FakeE2B))
    sb = E2BSandbox(api_key="k", template="base")
    assert sb.available
    res = sb.run(step("npm test", d="app", t=7), index=1)
    assert res.exit_code == 0 and res.stdout == "ok\n" and res.cwd == "/work/app"
    assert ("npm test", "/work/app", 7) in FakeCommands.calls
    assert sb.extract("app/x") == b"bytes:/work/app/x"
    sb.destroy()
    assert FakeE2B.killed is True


# --- selection ----------------------------------------------------------------------------


def test_select_sandbox_falls_back_to_local_with_note(monkeypatch, tmp_path, log):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    chosen = select_sandbox("e2b", tmp_path, {"e2bApiKey": ""}, log)
    assert chosen is None
    notes = entries(log, "note")
    assert len(notes) == 1
    assert "falling back to local" in notes[0]["summary"]
    assert notes[0]["detail"]["selected"] == "local"
    assert any("E2B_API_KEY not set" in t for t in notes[0]["detail"]["tried"])
    assert any(t.startswith("docker:") for t in notes[0]["detail"]["tried"])


def test_select_sandbox_falls_back_to_docker_with_note(monkeypatch, tmp_path, log):
    monkeypatch.setattr(DockerSandbox, "available", property(lambda self: True))
    chosen = select_sandbox("e2b", tmp_path, {"e2bApiKey": "", "dockerImage": "custom:1"}, log)
    assert isinstance(chosen, DockerSandbox) and chosen.image == "custom:1"
    notes = entries(log, "note")
    assert len(notes) == 1 and "falling back to docker" in notes[0]["summary"]


def test_select_sandbox_preferred_available_logs_nothing(monkeypatch, tmp_path, log):
    monkeypatch.setattr(DockerSandbox, "available", property(lambda self: True))
    chosen = select_sandbox("docker", tmp_path, None, log)
    assert isinstance(chosen, DockerSandbox)
    assert entries(log, "note") == []


def test_select_sandbox_local_preference(tmp_path, log):
    assert select_sandbox("local", tmp_path, None, log) is None
    assert "local" in entries(log, "note")[0]["summary"]


# --- run_high_risk ----------------------------------------------------------------------


class FakeDisposable(DisposableSandbox):
    name = "fake"

    def __init__(self, exits=(0, 0)):
        self.exits = list(exits)
        self.ran = []
        self.destroyed = 0

    available = True
    reason = ""

    def run(self, s, index=0):
        self.ran.append(index)
        return StepResult(index=index, command=s.command_string, cwd="/work", exit_code=self.exits[index],
                          stdout="", stderr="", elapsed_ms=1, timed_out=False)

    def extract(self, relpath):
        return b""

    def destroy(self):
        self.destroyed += 1


def test_run_high_risk_destroys_and_logs(monkeypatch, tmp_path, log):
    fake = FakeDisposable()
    monkeypatch.setattr("o_engine.containment.select_sandbox", lambda *a, **k: fake)
    req = ProvisionRequest(label="x", steps=[step("a"), step("b")])
    results = run_high_risk(req, tmp_path, "docker", {}, log, local_sandbox=None, job_id="j1")
    assert [r.exit_code for r in results] == [0, 0] and fake.ran == [0, 1]
    assert fake.destroyed == 1
    cache = entries(log, "cache")
    assert len(cache) == 1 and cache[0]["detail"]["ok"] is True and "eligible for cache" in cache[0]["summary"]
    notes = [n["summary"] for n in entries(log, "note")]
    assert any("disposable sandbox destroyed" in n for n in notes)
    assert len(entries(log, "result")) == 2


def test_run_high_risk_stops_on_failure_and_still_destroys(monkeypatch, tmp_path, log):
    fake = FakeDisposable(exits=(2, 0))
    monkeypatch.setattr("o_engine.containment.select_sandbox", lambda *a, **k: fake)
    req = ProvisionRequest(label="x", steps=[step("a"), step("b")])
    results = run_high_risk(req, tmp_path, "docker", {}, log)
    assert len(results) == 1 and results[0].exit_code == 2
    assert fake.destroyed == 1
    assert entries(log, "cache")[0]["detail"]["ok"] is False


def test_run_high_risk_local_fallback_uses_path_sandbox(monkeypatch, tmp_path, log):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    local = Sandbox(tmp_path / "sb")
    req = ProvisionRequest(label="x", steps=[step("echo local")])
    results = run_high_risk(req, tmp_path, "docker", {"e2bApiKey": ""}, log, local_sandbox=local)
    assert results[0].exit_code == 0 and "local" in results[0].stdout
    notes = [n["summary"] for n in entries(log, "note")]
    assert any("falling back to local" in n for n in notes)
    assert any("running on the local path sandbox" in n for n in notes)
    assert entries(log, "cache") == []


def script_step(code, language="python", d=".", t=30):
    return ProvisionStep(target_directory=d, timeout_seconds=t,
                         script={"language": language, "code": code, "target_directory": d, "timeout_seconds": t})


def test_run_high_risk_local_fallback_routes_script_steps_through_native_runner(monkeypatch, tmp_path, log):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    local = Sandbox(tmp_path / "sb")
    req = ProvisionRequest(label="x", risk="high", steps=[step("echo cmd"), script_step("print('from script')")])
    results = run_high_risk(req, tmp_path, "docker", {"e2bApiKey": ""}, log, local_sandbox=local, job_id="j7")
    assert [r.exit_code for r in results] == [0, 0], results
    assert "cmd" in results[0].stdout and "from script" in results[1].stdout
    assert results[1].index == 1
    notes = [n["summary"] for n in entries(log, "note")]
    assert any("falling back to local" in n for n in notes)
    assert any("running on the local path sandbox" in n for n in notes)
    assert len(entries(log, "result")) == 2
    assert not list((tmp_path / "sb").glob(".o-run-*"))  # the runner's temp file is gone


def test_run_high_risk_local_fallback_uses_given_runner_for_script_steps(monkeypatch, tmp_path, log):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    local = Sandbox(tmp_path / "sb")
    calls = []

    class StubRunner:
        def run(self, payload, sandbox, index=0, env_extra=None, isolated_display=False):
            calls.append((payload.code, sandbox, index, env_extra, isolated_display))
            return StepResult(index=index, command="stub", cwd=".", exit_code=0, stdout="", stderr="", elapsed_ms=1, timed_out=False)

    req = ProvisionRequest(label="x", risk="high", isolated_display=True, steps=[script_step("print(1)")])
    results = run_high_risk(req, tmp_path, "docker", {"e2bApiKey": ""}, log, local_sandbox=local, runner=StubRunner())
    assert len(results) == 1 and results[0].command == "stub"
    assert calls == [("print(1)", local, 0, None, True)]


def test_run_high_risk_without_any_sandbox_raises(monkeypatch, tmp_path, log):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    req = ProvisionRequest(label="x", steps=[step("echo")])
    with pytest.raises(RuntimeError):
        run_high_risk(req, tmp_path, "local", {}, log, local_sandbox=None)
