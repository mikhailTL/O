import os
import sys
import types

import pytest

from o_engine.executor import Sandbox, StepResult
from o_engine.runners import NativeRunner, OpenInterpreterRunner, Runner, get_runner
from o_engine.schema import ScriptPayload


def _payload(language, code, cwd=".", timeout=20):
    return ScriptPayload(language=language, code=code, target_directory=cwd, timeout_seconds=timeout)


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path / "root")


def _no_temp_files(root):
    return [p.name for p in root.rglob(".o-run-*")] == []


def test_native_python(sandbox):
    r = NativeRunner().run(_payload("python", "import sys\nprint('hi from py')\nsys.exit(0)\n"), sandbox, index=2)
    assert isinstance(r, StepResult)
    assert r.exit_code == 0
    assert r.stdout.strip() == "hi from py"
    assert r.index == 2
    assert r.cwd == str(sandbox.root)
    assert r.timed_out is False
    assert "python3 -I" in r.command or "python -I" in r.command
    assert _no_temp_files(sandbox.root)


@pytest.mark.skipif(os.name == "nt", reason="bash")
def test_native_bash_in_subdir(sandbox):
    (sandbox.root / "sub").mkdir()
    r = NativeRunner().run(_payload("bash", "echo \"$PWD\"\nexit 4\n", cwd="sub"), sandbox)
    assert r.exit_code == 4
    assert r.stdout.strip().endswith("sub")
    assert r.cwd == str(sandbox.root / "sub")
    assert _no_temp_files(sandbox.root)


@pytest.mark.skipif(os.name == "nt", reason="posix env listing")
def test_native_env_scrubbed(sandbox, monkeypatch):
    monkeypatch.setenv("O_ENGINE_TOKEN", "sekrit")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("KEEP_ME", "yes")
    code = "import os\nfor k, v in sorted(os.environ.items()):\n    print(f'{k}={v}')\n"
    r = NativeRunner().run(_payload("python", code), sandbox)
    assert r.exit_code == 0
    assert "sekrit" not in r.stdout
    assert "O_ENGINE_TOKEN" not in r.stdout
    assert "ANTHROPIC_API_KEY" not in r.stdout
    assert "KEEP_ME=yes" in r.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="isolated display is Linux only")
def test_native_isolated_display_and_env_extra(sandbox, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    code = "import os\nprint(os.environ.get('DISPLAY', 'unset'), os.environ.get('O_X', 'unset'))\n"
    r = NativeRunner().run(_payload("python", code), sandbox, env_extra={"O_X": "val"}, isolated_display=True)
    assert r.stdout.split() == [":99", "val"]
    r2 = NativeRunner().run(_payload("python", code), sandbox)
    assert r2.stdout.split() == ["unset", "unset"]
    assert os.environ["DISPLAY"] == ":0"


def test_native_timeout(sandbox):
    r = NativeRunner().run(_payload("python", "import time\ntime.sleep(5)\n", timeout=1), sandbox)
    assert r.timed_out is True
    assert r.exit_code != 0
    assert _no_temp_files(sandbox.root)


def test_native_missing_target_directory_is_failed_step(sandbox):
    r = NativeRunner().run(_payload("python", "print(1)", cwd="nope/dir"), sandbox)
    assert r.exit_code == 127
    assert "does not exist" in r.stderr
    assert not (sandbox.root / "nope").exists()


def test_native_escape_rejected(sandbox):
    from o_engine.executor import SandboxEscape

    with pytest.raises(SandboxEscape):
        NativeRunner().run(_payload("python", "print(1)", cwd=".."), sandbox)


def test_native_javascript_when_node_present(sandbox):
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = NativeRunner().run(_payload("javascript", "console.log('js ok'); process.exit(2)"), sandbox)
    assert r.exit_code == 2
    assert r.stdout.strip() == "js ok"
    assert _no_temp_files(sandbox.root)


def test_native_missing_interpreter_is_failed_step(sandbox, monkeypatch):
    import o_engine.runners as runners

    monkeypatch.setitem(runners.NATIVE_COMMANDS, "powershell", ("o-definitely-missing-shell", "-File"))
    r = NativeRunner().run(_payload("powershell", "Write-Output 1"), sandbox)
    assert r.exit_code != 0
    assert _no_temp_files(sandbox.root)


def test_open_interpreter_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "interpreter", None)
    oi = OpenInterpreterRunner()
    assert oi.available is False
    assert oi.reason
    runner = get_runner("open-interpreter")
    assert isinstance(runner, NativeRunner)
    assert isinstance(get_runner("native"), NativeRunner)
    assert isinstance(get_runner("something-else"), NativeRunner)
    assert isinstance(get_runner(), Runner)


def test_open_interpreter_absent_run_is_failed_step(monkeypatch, sandbox):
    monkeypatch.setitem(sys.modules, "interpreter", None)
    r = OpenInterpreterRunner().run(_payload("python", "print(1)"), sandbox)
    assert r.exit_code != 0
    assert "open-interpreter" in r.stderr.lower() or "interpreter" in r.stderr.lower()


def _fake_interpreter(calls, chunks):
    computer = types.SimpleNamespace(run=lambda language, code: (calls.append((language, code, os.environ.get("DISPLAY"))), chunks)[1])
    mod = types.ModuleType("interpreter")
    mod.interpreter = types.SimpleNamespace(computer=computer)
    return mod


def _display_recorder(seen, chunks=()):
    def run(language, code):
        seen.append((os.environ.get("DISPLAY"), os.environ.get("WAYLAND_DISPLAY")))
        return list(chunks)

    mod = types.ModuleType("interpreter")
    mod.interpreter = types.SimpleNamespace(computer=types.SimpleNamespace(run=run))
    return mod


def test_open_interpreter_strips_display_when_not_isolated(monkeypatch, sandbox):
    seen = []
    monkeypatch.setitem(sys.modules, "interpreter", _display_recorder(seen))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    r = OpenInterpreterRunner().run(_payload("python", "x"), sandbox)
    assert r.exit_code == 0
    assert seen == [(None, None)]
    assert os.environ["DISPLAY"] == ":0"
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-0"


def test_open_interpreter_no_display_set_stays_unset_and_restores(monkeypatch, sandbox):
    seen = []
    monkeypatch.setitem(sys.modules, "interpreter", _display_recorder(seen))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    OpenInterpreterRunner().run(_payload("python", "x"), sandbox)
    assert seen == [(None, None)]
    assert "DISPLAY" not in os.environ
    assert "WAYLAND_DISPLAY" not in os.environ


@pytest.mark.skipif(sys.platform != "linux", reason="isolated display is Linux only")
def test_open_interpreter_isolated_uses_99_and_strips_wayland(monkeypatch, sandbox):
    seen = []
    monkeypatch.setitem(sys.modules, "interpreter", _display_recorder(seen))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    OpenInterpreterRunner().run(_payload("python", "x"), sandbox, isolated_display=True)
    assert seen == [(":99", None)]
    assert os.environ["DISPLAY"] == ":0"
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-0"


def test_open_interpreter_isolated_env_extra_display_wins(monkeypatch, sandbox):
    seen = []
    monkeypatch.setitem(sys.modules, "interpreter", _display_recorder(seen))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    OpenInterpreterRunner().run(_payload("python", "x"), sandbox, env_extra={"DISPLAY": ":7"}, isolated_display=True)
    assert seen == [(":7", None)]
    assert "DISPLAY" not in os.environ


def test_open_interpreter_env_extra_display_ignored_when_not_isolated(monkeypatch, sandbox):
    seen = []
    monkeypatch.setitem(sys.modules, "interpreter", _display_recorder(seen))
    monkeypatch.setenv("DISPLAY", ":0")
    OpenInterpreterRunner().run(_payload("python", "x"), sandbox, env_extra={"DISPLAY": ":7"})
    assert seen == [(None, None)]
    assert os.environ["DISPLAY"] == ":0"


def test_open_interpreter_display_restored_after_exception(monkeypatch, sandbox):
    def explode(language, code):
        raise RuntimeError("boom")

    mod = types.ModuleType("interpreter")
    mod.interpreter = types.SimpleNamespace(computer=types.SimpleNamespace(run=explode))
    monkeypatch.setitem(sys.modules, "interpreter", mod)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "w")
    r = OpenInterpreterRunner().run(_payload("python", "x"), sandbox, isolated_display=True)
    assert r.exit_code == 1
    assert os.environ["DISPLAY"] == ":0" and os.environ["WAYLAND_DISPLAY"] == "w"


def test_open_interpreter_present_runs_and_restores_display(monkeypatch, sandbox):
    calls = []
    chunks = [{"type": "console", "format": "output", "content": "line1\n"}, {"type": "console", "format": "output", "content": "line2\n"}]
    monkeypatch.setitem(sys.modules, "interpreter", _fake_interpreter(calls, chunks))
    monkeypatch.setenv("DISPLAY", ":0")
    oi = OpenInterpreterRunner()
    assert oi.available is True
    r = oi.run(_payload("bash", "echo x"), sandbox, index=1, env_extra={"DISPLAY": ":99"}, isolated_display=True)
    assert r.exit_code == 0
    assert r.stdout == "line1\nline2\n"
    assert r.index == 1
    assert calls == [("shell", "echo x", ":99")]
    assert os.environ["DISPLAY"] == ":0"
    assert isinstance(get_runner("open-interpreter"), OpenInterpreterRunner)


def test_open_interpreter_error_marker_and_exception(monkeypatch, sandbox):
    calls = []
    chunks = [{"type": "console", "format": "output", "content": "Traceback (most recent call last):\n  boom\n"}]
    monkeypatch.setitem(sys.modules, "interpreter", _fake_interpreter(calls, chunks))
    r = OpenInterpreterRunner().run(_payload("python", "raise ValueError"), sandbox)
    assert r.exit_code == 1
    assert "Traceback" in r.stderr

    def explode(language, code):
        raise RuntimeError("kaboom")

    mod = types.ModuleType("interpreter")
    mod.interpreter = types.SimpleNamespace(computer=types.SimpleNamespace(run=explode))
    monkeypatch.setitem(sys.modules, "interpreter", mod)
    r2 = OpenInterpreterRunner().run(_payload("python", "x"), sandbox)
    assert r2.exit_code == 1
    assert "kaboom" in r2.stderr
