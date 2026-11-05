"""Failure modes the specs imply, proven against the real modules.

Network backends (vLLM, Claude CLI, screenpipe) are simulated only by pointing
settings at dead URLs / missing paths; nothing under test is monkeypatched.
"""
import hashlib
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from o_engine.actionlog import GENESIS, get_log
from o_engine.server import EngineSettings, create_app

TOKEN = "t"
DEAD_VLLM = "http://127.0.0.1:1/v1"
NO_CLAUDE = "/nonexistent/claude"


def _settings(tmp: Path, **overrides) -> EngineSettings:
    values = dict(
        token=TOKEN,
        data_dir=str(tmp / "data"),
        sandbox_root=str(tmp / "sb"),
        screenpipe_db=str(tmp / "missing-screenpipe.db"),
        vllm_url=DEAD_VLLM,
        claude_path=NO_CLAUDE,
        max_attempts=2,
        permission_mode="log",
    )
    values.update(overrides)
    return EngineSettings.from_env(**values)


def _h() -> dict:
    return {"x-o-token": TOKEN}


def _req(label: str, command: str, timeout: int = 30) -> dict:
    return {"label": label, "steps": [{"target_directory": ".", "command_string": command, "timeout_seconds": timeout}]}


def _wait_done(client: TestClient, job_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/jobs/{job_id}", headers=_h()).json()
        if body["done"]:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} not done within {timeout}s")


def _entries(settings: EngineSettings) -> list[dict]:
    path = settings.data_dir_path / "engine-actions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _first_broken_index(path: Path) -> int | None:
    prev = GENESIS
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            e = json.loads(line)
            h = e.pop("hash")
        except (ValueError, KeyError):
            return i
        if e.get("prev") != prev:
            return i
        body = json.dumps(e, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(body.encode()).hexdigest() != h:
            return i
        prev = h
    return None


def _last_hash(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])["hash"] if lines else GENESIS


def test_vllm_down_and_claude_missing(tmp_path):
    """vLLM down and Claude CLI missing: remediation exhausts attempts without hanging; health shows vLLM red."""
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health", headers=_h()).json()["vllm"] is False
        started = time.monotonic()
        job_id = client.post("/provision", json=_req("bad import", 'python3 -c "import nomodule_zz"'), headers=_h()).json()["job_id"]
        body = _wait_done(client, job_id, timeout=15.0)
        elapsed = time.monotonic() - started
    assert elapsed < 15.0
    assert body["ok"] is False
    assert body["done"] is True
    assert body["attempt"] == body["max_attempts"] == settings.max_attempts
    assert body["phase"] == "report"
    assert body["steps"][-1]["exit_code"] != 0
    parser_error = body["diagnostic"]["parser_error"]
    assert "local:" in parser_error
    assert "fallback:" in parser_error
    assert "claude cli failed to run" in parser_error
    assert NO_CLAUDE in parser_error
    kinds = [e["kind"] for e in _entries(settings)]
    assert kinds.count("remediate") >= settings.max_attempts


def test_screenpipe_absent(tmp_path):
    """screenpipe absent: context still builds from shell history + tree; health shows screenpipe red."""
    settings = _settings(tmp_path)
    assert not Path(settings.screenpipe_db).exists()
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health", headers=_h()).json()["screenpipe"] is False
        r = client.get("/context", params={"cwd": str(workspace)}, headers=_h())
    assert r.status_code == 200
    profile = r.json()
    assert {"shell_history", "tree", "config_files", "telemetry", "cwd", "platform"} <= set(profile)
    assert isinstance(profile["shell_history"], list)
    assert isinstance(profile["tree"], dict) and profile["tree"]
    assert any(c["path"].endswith("package.json") for c in profile["config_files"])
    assert profile["telemetry"] == []


def test_cached_script_now_fails(tmp_path):
    """Cache entry whose script now fails: replay is treated as a failure (remediate), entry never deleted, runs not bumped."""
    settings = _settings(tmp_path)
    marker = tmp_path / "marker"
    marker.write_text("1", encoding="utf-8")
    request = _req("marker check", f"test -f {marker} && echo ok")
    cache_dir = settings.data_dir_path / "cache"
    app = create_app(settings)
    with TestClient(app) as client:
        first = _wait_done(client, client.post("/provision", json=request, headers=_h()).json()["job_id"], timeout=10.0)
        assert first["ok"] is True and first["cached"] is False
        jsons = list(cache_dir.glob("*.json"))
        scripts = list(cache_dir.glob("*.sh"))
        assert len(jsons) == 1 and len(scripts) == 1
        entry_before = json.loads(jsons[0].read_text(encoding="utf-8"))
        assert entry_before["runs"] == 0
        fingerprint = entry_before["fingerprint"]

        marker.unlink()
        second_id = client.post("/provision", json=request, headers=_h()).json()["job_id"]
        second = _wait_done(client, second_id, timeout=15.0)

    assert second["ok"] is False
    assert second["attempt"] == second["max_attempts"] == settings.max_attempts
    assert second["phase"] == "report"
    assert second["steps"][-1]["exit_code"] != 0
    entries = [e for e in _entries(settings) if e["detail"].get("job_id") == second_id]
    hits = [e for e in entries if e["kind"] == "cache" and e["detail"].get("hit") is True]
    assert hits and hits[0]["detail"]["fingerprint"] == fingerprint
    replays = [e for e in entries if e["kind"] == "cache" and "replayed" in e["summary"]]
    assert replays and replays[0]["detail"]["exit_code"] != 0
    phases = [e["detail"]["phase"] for e in entries if e["kind"] == "graph" and "phase" in e["detail"]]
    assert phases.index("remediate") > phases.index("execute")
    assert "cache_store" not in phases
    assert not any("bumped runs" in e["summary"] for e in entries)
    assert jsons[0].is_file() and scripts[0].is_file()
    assert set(cache_dir.glob("*")) == {jsons[0], scripts[0]}
    entry_after = json.loads(jsons[0].read_text(encoding="utf-8"))
    assert entry_after == entry_before
    assert entry_after["runs"] == 0


def test_log_tampered_is_detected(tmp_path):
    """Log file tampered: recomputing the hash chain reports the first broken index; an untampered copy continues the chain."""
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        _wait_done(client, client.post("/provision", json=_req("echo", "echo hi"), headers=_h()).json()["job_id"], timeout=10.0)
    jsonl = settings.data_dir_path / "engine-actions.jsonl"
    assert _first_broken_index(jsonl) is None
    lines = jsonl.read_bytes().split(b"\n")
    lines = [ln for ln in lines if ln]
    assert len(lines) >= 5
    untampered = tmp_path / "copy"
    untampered.mkdir()
    shutil.copy(jsonl, untampered / "engine-actions.jsonl")
    original_last = _last_hash(jsonl)

    target = len(lines) // 2
    line = lines[target]
    key = b'"summary":"'
    pos = line.index(key) + len(key)
    original = line[pos:pos + 1]
    replacement = b"Z" if original != b"Z" else b"Y"
    lines[target] = line[:pos] + replacement + line[pos + 1:]
    jsonl.write_bytes(b"\n".join(lines) + b"\n")
    assert _first_broken_index(jsonl) == target

    log2 = get_log(untampered)
    assert log2._prev == original_last
    entry = log2.record("note", "continued after restart")
    assert entry["prev"] == original_last
    copy_path = untampered / "engine-actions.jsonl"
    assert _first_broken_index(copy_path) is None
    assert _last_hash(copy_path) == entry["hash"]


def test_two_jobs_at_once(tmp_path):
    """Two jobs at once: separate sandbox subdirs, interleaved log entries still chain end to end."""
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        a = client.post("/provision", json=_req("job a", "sleep 1 && echo a"), headers=_h()).json()["job_id"]
        b = client.post("/provision", json=_req("job b", "sleep 1 && echo b"), headers=_h()).json()["job_id"]
        started = time.monotonic()
        body_a = _wait_done(client, a, timeout=10.0)
        body_b = _wait_done(client, b, timeout=10.0)
        elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert a != b
    assert body_a["ok"] is True and body_b["ok"] is True
    cwd_a = Path(body_a["steps"][0]["cwd"]).resolve()
    cwd_b = Path(body_b["steps"][0]["cwd"]).resolve()
    assert cwd_a != cwd_b
    assert cwd_a == (settings.sandbox_root_path / a).resolve()
    assert cwd_b == (settings.sandbox_root_path / b).resolve()
    assert "a" in body_a["steps"][0]["stdout_tail"] and "b" in body_b["steps"][0]["stdout_tail"]
    jsonl = settings.data_dir_path / "engine-actions.jsonl"
    assert _first_broken_index(jsonl) is None
    owners = [e["detail"].get("job_id") for e in _entries(settings)]
    idx_a = [i for i, j in enumerate(owners) if j == a]
    idx_b = [i for i, j in enumerate(owners) if j == b]
    assert idx_a and idx_b
    assert min(idx_b) < max(idx_a) and min(idx_a) < max(idx_b)


def test_step_timeout_does_not_hang(tmp_path):
    """Engine side of a crash mid-job: a step that overruns its timeout is killed, reported as 124, and the job fails promptly."""
    settings = _settings(tmp_path, max_attempts=1)
    app = create_app(settings)
    with TestClient(app) as client:
        started = time.monotonic()
        job_id = client.post("/provision", json=_req("hang", "sleep 5", timeout=1), headers=_h()).json()["job_id"]
        body = _wait_done(client, job_id, timeout=10.0)
        elapsed = time.monotonic() - started
    assert elapsed < 8.0
    assert body["done"] is True and body["ok"] is False
    assert body["attempt"] == settings.max_attempts
    for step in body["steps"]:
        assert step["timed_out"] is True
        assert step["exit_code"] == 124
        assert step["elapsed_ms"] < 2500
    assert "timeout" in (body["error"] or "")
    results = [e for e in _entries(settings) if e["kind"] == "result" and e["detail"].get("timed_out") is True]
    assert results


def test_schema_invalid_request_leaves_no_job(tmp_path):
    """Engine side of a crash mid-job: a schema-invalid request is rejected with 422 and never becomes a job."""
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        before = len(_entries(settings))
        bad = {"label": "x", "steps": [{"target_directory": ".", "command_string": "Sure, here is how you install it.", "timeout_seconds": 5}]}
        r = client.post("/provision", json=bad, headers=_h())
        assert r.status_code == 422
        assert "conversation" in r.json()["detail"]
        r2 = client.post("/provision", json={"label": "x", "steps": [{"command_string": "ls"}]}, headers=_h())
        assert r2.status_code == 422
        assert app.state.jobs._jobs == {}
        entries = _entries(settings)[before:]
    assert all(e["kind"] == "policy" for e in entries)
    assert len(entries) == 2
    assert not any(e["kind"] in ("task", "graph", "result") for e in entries)
    assert not any(settings.sandbox_root_path.iterdir())
