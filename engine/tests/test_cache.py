import json
import os
import sys

import pytest

from o_engine.cache import CacheEntry, CacheExists, ScriptCache
from o_engine.executor import Sandbox, StepResult
from o_engine.schema import ProvisionRequest, ProvisionStep

PLATFORM = "win32" if sys.platform == "win32" else "linux"


def _request(cmds=("echo ok",), timeouts=None, cwd="."):
    timeouts = timeouts or [10] * len(cmds)
    return ProvisionRequest(
        label="demo",
        steps=[
            ProvisionStep(target_directory=cwd, command_string=c, timeout_seconds=t)
            for c, t in zip(cmds, timeouts)
        ],
    )


@pytest.fixture
def cache(tmp_path):
    return ScriptCache(tmp_path / "cache")


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path / "root")


def test_dir_created(tmp_path):
    ScriptCache(tmp_path / "c" / "d")
    assert (tmp_path / "c" / "d").is_dir()


def test_lookup_miss(cache):
    assert cache.lookup("deadbeef") is None


def test_store_and_lookup(cache):
    req = _request()
    fp = req.fingerprint(PLATFORM)
    entry = cache.store(fp, req, PLATFORM)
    assert isinstance(entry, CacheEntry)
    assert entry.fingerprint == fp
    assert entry.label == "demo"
    assert entry.platform == PLATFORM
    assert entry.runs == 0
    assert entry.created
    script = cache.dir / entry.script_path
    assert script.is_file()
    assert (cache.dir / f"{fp}.json").is_file()
    found = cache.lookup(fp)
    assert found == entry
    data = json.loads((cache.dir / f"{fp}.json").read_text())
    assert data["fingerprint"] == fp


def test_script_rendering_posix(cache):
    req = _request(cmds=("echo one", "echo two"), cwd="sub")
    entry = cache.store("fp1", req, "linux")
    assert entry.script_path.endswith(".sh")
    text = (cache.dir / entry.script_path).read_text()
    assert "set -e" in text
    assert text.count("cd ") >= 2
    assert "echo one" in text and "echo two" in text
    assert text.index("echo one") < text.index("echo two")


def test_script_rendering_windows(cache):
    req = _request(cmds=("echo one", "echo two"), cwd="sub")
    entry = cache.store("fp2", req, "win32")
    assert entry.script_path.endswith(".ps1")
    text = (cache.dir / entry.script_path).read_text()
    assert "$ErrorActionPreference = 'Stop'" in text or "$ErrorActionPreference='Stop'" in text
    assert text.count("Set-Location") >= 2
    assert "echo one" in text and "echo two" in text


def test_noclobber(cache):
    req = _request()
    cache.store("fpx", req, PLATFORM)
    before = (cache.dir / "fpx.json").read_text()
    with pytest.raises(CacheExists):
        cache.store("fpx", _request(cmds=("echo other",)), PLATFORM)
    assert (cache.dir / "fpx.json").read_text() == before
    script = cache.lookup("fpx").script_path
    assert "echo other" not in (cache.dir / script).read_text()


def test_noclobber_when_only_script_exists(cache):
    ext = ".ps1" if PLATFORM == "win32" else ".sh"
    (cache.dir / f"fpy{ext}").write_text("stale")
    with pytest.raises(CacheExists):
        cache.store("fpy", _request(), PLATFORM)
    assert (cache.dir / f"fpy{ext}").read_text() == "stale"
    assert not (cache.dir / "fpy.json").exists()


def test_no_delete_method(cache):
    assert not any(n.startswith(("delete", "remove", "purge", "clear", "evict")) for n in dir(cache))


def test_index(cache):
    assert cache.index() == []
    cache.store("b" * 8, _request(), PLATFORM)
    cache.store("a" * 8, _request(cmds=("echo two",)), PLATFORM)
    (cache.dir / "junk.txt").write_text("x")
    idx = cache.index()
    assert [e.fingerprint for e in idx] == ["a" * 8, "b" * 8]
    assert all(isinstance(e, CacheEntry) for e in idx)


def test_bump_runs(cache):
    cache.store("fpz", _request(), PLATFORM)
    assert cache.bump_runs("fpz") == 1
    assert cache.bump_runs("fpz") == 2
    assert cache.lookup("fpz").runs == 2
    data = json.loads((cache.dir / "fpz.json").read_text())
    assert data["runs"] == 2


def test_bump_runs_missing(cache):
    with pytest.raises(KeyError):
        cache.bump_runs("missing")


def test_replay_echo_ok(cache, sandbox):
    req = _request()
    entry = cache.store(req.fingerprint(PLATFORM), req, PLATFORM)
    r = cache.replay(entry, sandbox)
    assert isinstance(r, StepResult)
    assert r.exit_code == 0
    assert "ok" in r.stdout
    assert r.timed_out is False


def test_replay_runs_steps_in_order_and_cwd(cache, sandbox):
    (sandbox.root / "sub").mkdir()
    pwd = "pwd" if os.name != "nt" else "Get-Location | Select-Object -ExpandProperty Path"
    req = _request(cmds=("echo first", pwd), cwd="sub")
    entry = cache.store(req.fingerprint(PLATFORM), req, PLATFORM)
    r = cache.replay(entry, sandbox)
    assert r.exit_code == 0
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    assert lines[0] == "first"
    assert lines[-1].endswith("sub")


def test_replay_stops_on_failure(cache, sandbox):
    req = _request(cmds=("exit 7", "echo never"))
    entry = cache.store(req.fingerprint(PLATFORM), req, PLATFORM)
    r = cache.replay(entry, sandbox)
    assert r.exit_code != 0
    assert "never" not in r.stdout


def test_replay_timeout_is_max_of_steps(cache, sandbox, monkeypatch):
    seen = {}
    real_run = Sandbox.run

    def spy(self, step, index=0, env_extra=None, isolated_display=False):
        seen["timeout"] = step.timeout_seconds
        return real_run(self, step, index=index, env_extra=env_extra, isolated_display=isolated_display)

    monkeypatch.setattr(Sandbox, "run", spy)
    req = _request(cmds=("echo a", "echo b", "echo c"), timeouts=[5, 42, 7])
    entry = cache.store(req.fingerprint(PLATFORM), req, PLATFORM)
    assert entry.timeout_seconds == 42
    cache.replay(entry, sandbox)
    assert seen["timeout"] == 42


def test_replay_rejects_escaped_script(cache, sandbox, tmp_path):
    entry = cache.store("fpq", _request(), PLATFORM)
    bad = entry.model_copy(update={"script_path": str(tmp_path / "evil.sh")})
    with pytest.raises(Exception):
        cache.replay(bad, sandbox)
