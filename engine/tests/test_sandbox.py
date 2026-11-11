import hashlib
import os
import sys

import pytest

from o_engine.executor import Patch, PatchError, Sandbox, SandboxEscape, StepResult, apply_patch, tail
from o_engine.schema import ProvisionStep


def _step(cmd, cwd=".", timeout=10):
    return ProvisionStep(target_directory=cwd, command_string=cmd, timeout_seconds=timeout)


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path / "root")


def test_root_created(tmp_path):
    root = tmp_path / "a" / "b"
    sb = Sandbox(root)
    assert root.is_dir()
    assert sb.root == root.resolve()


def test_resolve_relative_inside(sandbox):
    (sandbox.root / "proj").mkdir()
    assert sandbox.resolve("proj") == sandbox.root / "proj"
    assert sandbox.resolve(".") == sandbox.root


def test_resolve_absolute_inside(sandbox):
    inner = sandbox.root / "x"
    inner.mkdir()
    assert sandbox.resolve(str(inner)) == inner


def test_resolve_dotdot_escape_rejected(sandbox):
    with pytest.raises(SandboxEscape):
        sandbox.resolve("../outside")
    with pytest.raises(SandboxEscape):
        sandbox.resolve("proj/../../outside")


def test_resolve_absolute_outside_rejected(sandbox, tmp_path):
    with pytest.raises(SandboxEscape):
        sandbox.resolve(str(tmp_path))


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_resolve_symlink_escape_rejected(sandbox, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (sandbox.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxEscape):
        sandbox.resolve("link")
    with pytest.raises(SandboxEscape):
        sandbox.resolve("link/sub")


def test_run_echo(sandbox):
    r = sandbox.run(_step("echo hello"), index=3)
    assert isinstance(r, StepResult)
    assert r.exit_code == 0
    assert r.stdout.strip() == "hello"
    assert r.index == 3
    assert r.cwd == str(sandbox.root)
    assert r.command == "echo hello"
    assert r.timed_out is False
    assert r.elapsed_ms >= 0


def test_run_nonzero_exit(sandbox):
    r = sandbox.run(_step("exit 3"))
    assert r.exit_code == 3


def test_run_in_subdirectory(sandbox):
    (sandbox.root / "sub").mkdir()
    r = sandbox.run(_step("pwd" if os.name != "nt" else "cd", cwd="sub"))
    assert r.exit_code == 0
    assert r.stdout.strip().endswith("sub")


def test_run_rejects_escape(sandbox):
    with pytest.raises(SandboxEscape):
        sandbox.run(_step("echo x", cwd=".."))


def test_run_timeout(sandbox):
    cmd = "sleep 5" if os.name != "nt" else "ping -n 6 127.0.0.1 >NUL"
    r = sandbox.run(_step(cmd, timeout=1))
    assert r.timed_out is True
    assert r.exit_code != 0
    assert r.elapsed_ms < 4000


def test_env_scrubbed(sandbox, monkeypatch):
    monkeypatch.setenv("O_ENGINE_TOKEN", "sekrit")
    monkeypatch.setenv("CLAUDE_CODE_FOO", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("MY_PASSWORD", "x")
    monkeypatch.setenv("GITHUB_SECRET_THING", "x")
    monkeypatch.setenv("KEEP_ME", "yes")
    cmd = "env" if os.name != "nt" else "set"
    r = sandbox.run(_step(cmd))
    assert r.exit_code == 0
    assert "sekrit" not in r.stdout
    assert "O_ENGINE_TOKEN" not in r.stdout
    assert "CLAUDE_CODE_FOO" not in r.stdout
    assert "ANTHROPIC_API_KEY" not in r.stdout
    assert "MY_PASSWORD" not in r.stdout
    assert "GITHUB_SECRET_THING" not in r.stdout
    assert "KEEP_ME=yes" in r.stdout


def test_env_extra_passed(sandbox):
    cmd = "echo $O_X" if os.name != "nt" else "echo %O_X%"
    r = sandbox.run(_step(cmd), env_extra={"O_X": "val"})
    assert r.stdout.strip() == "val"


@pytest.mark.skipif(sys.platform != "linux", reason="isolated display is Linux only")
def test_isolated_display_sets_display(sandbox, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    r = sandbox.run(_step("echo $DISPLAY"), isolated_display=True)
    assert r.stdout.strip() == ":99"
    r2 = sandbox.run(_step("echo ${DISPLAY:-unset}"), isolated_display=False)
    assert r2.stdout.strip() == "unset"
    assert os.environ["DISPLAY"] == ":0"


@pytest.mark.skipif(os.name == "nt", reason="posix shell syntax")
def test_host_display_never_leaks(sandbox, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    r = sandbox.run(_step("echo ${DISPLAY:-unset} ${WAYLAND_DISPLAY:-unset}"))
    assert r.stdout.split() == ["unset", "unset"]
    r2 = sandbox.run(_step("echo ${DISPLAY:-unset}"), isolated_display=True)
    expected = ":99" if sys.platform.startswith("linux") else "unset"
    assert r2.stdout.strip() == expected


def test_run_missing_target_directory_is_failed_step(sandbox):
    r = sandbox.run(ProvisionStep(target_directory="missing/dir", command_string="ls", timeout_seconds=1))
    assert r.exit_code == 127
    assert r.timed_out is False
    assert "missing/dir" in r.stderr
    assert "does not exist" in r.stderr
    assert r.elapsed_ms >= 0
    assert not (sandbox.root / "missing").exists()


def test_run_script_step_rejected_with_clear_error(sandbox, monkeypatch):
    import subprocess

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("must not run")))
    script = {"language": "python", "code": "print(1)", "target_directory": ".", "timeout_seconds": 5}
    step = ProvisionStep(script=script)
    assert step.command_string is None
    with pytest.raises(ValueError, match="script steps must go through a Runner"):
        sandbox.run(step)
    assert calls == []


def test_tail():
    s = "\n".join(str(i) for i in range(100))
    out = tail(s, 40)
    assert out.splitlines() == [str(i) for i in range(60, 100)]
    assert tail("a\nb", 40) == "a\nb"
    assert tail("", 40) == ""


def test_patch_model_strict():
    with pytest.raises(Exception):
        Patch(path="a", find="b", replace="c", extra=1)
    p = Patch(path="a", find="b", replace="c")
    with pytest.raises(Exception):
        p.path = "z"
    with pytest.raises(Exception):
        Patch(path="a", find="b" * 20001, replace="c")
    with pytest.raises(Exception):
        Patch(path="a", find="b", replace="c" * 20001)


def test_patch_single_model():
    from o_engine import remediate

    assert remediate.Patch is Patch


def test_apply_patch_ok(sandbox):
    f = sandbox.root / "f.txt"
    f.write_text("hello world\n")
    digest = apply_patch(sandbox, Patch(path="f.txt", find="world", replace="there"))
    assert f.read_text() == "hello there\n"
    assert digest == hashlib.sha256(b"hello there\n").hexdigest()


def test_apply_patch_find_absent(sandbox):
    (sandbox.root / "f.txt").write_text("hello\n")
    with pytest.raises(PatchError):
        apply_patch(sandbox, Patch(path="f.txt", find="zzz", replace="y"))


def test_apply_patch_find_multiple(sandbox):
    f = sandbox.root / "f.txt"
    f.write_text("a a\n")
    with pytest.raises(PatchError):
        apply_patch(sandbox, Patch(path="f.txt", find="a", replace="b"))
    assert f.read_text() == "a a\n"


def test_apply_patch_missing_file(sandbox):
    with pytest.raises(PatchError):
        apply_patch(sandbox, Patch(path="nope.txt", find="a", replace="b"))


def test_apply_patch_outside_sandbox(sandbox, tmp_path):
    outside = tmp_path / "o.txt"
    outside.write_text("a")
    with pytest.raises(SandboxEscape):
        apply_patch(sandbox, Patch(path=str(outside), find="a", replace="b"))
    with pytest.raises(SandboxEscape):
        apply_patch(sandbox, Patch(path="../o.txt", find="a", replace="b"))
    assert outside.read_text() == "a"


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_timeout_kills_the_whole_process_group(sandbox, tmp_path):
    """A shell step that forks a long-lived child: on timeout the child dies with the shell (its own session),
    instead of outliving the step and holding the pipes open."""
    import subprocess
    import time

    marker = tmp_path / "alive"
    # the shell forks `sleep` into the background and waits on it: the timeout must reach the grandchild
    step = _step(f"(sleep 30; touch {marker}) & echo started; wait", timeout=1)
    t0 = time.monotonic()
    res = sandbox.run(step)
    assert res.timed_out is True and res.exit_code == 124 and "started" in res.stdout
    assert time.monotonic() - t0 < 10, "communicate() returned once the group was killed, not after 30 s"
    time.sleep(0.3)
    # no `sleep 30` in a session of ours is left running under this sandbox's marker path
    ps = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    assert str(marker) not in ps, ps
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_run_command_starts_a_new_session():
    from o_engine.executor import run_command

    seen: dict = {}

    class P:
        pid = 4242
        returncode = 0

        def __init__(self, cmd, **kw):
            seen.update(kw)

        def communicate(self, timeout=None):
            return "out", ""

    code, out, err, timed_out = run_command("true", cwd=".", env={}, timeout=5, popen=P)
    assert (code, out, err, timed_out) == (0, "out", "", False)
    assert seen["start_new_session"] is True and seen["shell"] is True and seen["stdin"] is not None
