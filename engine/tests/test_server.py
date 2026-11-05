import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from o_engine.schema import ProvisionRequest, SchemaViolation, parse_steps
from o_engine.server import EngineSettings, create_app

TOKEN = "test-token-123"


class StubParser:
    def parse(self, text, goal=""):
        if "```" in text:
            raise SchemaViolation("steps are wrapped in a markdown fence")
        return parse_steps(
            '[{"target_directory": ".", "command_string": "pip install flask", "timeout_seconds": 60}]',
            label=goal or "parsed",
        )


class StubProfile:
    def model_dump(self):
        return {"cwd": "/tmp/x", "history": [], "tree": {}, "configs": [], "telemetry": []}


class StubContext:
    def profile(self, cwd, telemetry_seconds=120):
        return StubProfile()

    def recent_telemetry(self, seconds=120):
        return [{"ts": time.time(), "text": "Traceback", "window": "Terminal"}]


class StubCache:
    def __init__(self):
        self.entries = {}

    def lookup(self, fp):
        return self.entries.get(fp)

    def index(self):
        return [
            {"fingerprint": fp, "label": "x", "created": 0.0, "platform": "linux", "runs": 1}
            for fp in self.entries
        ]


class StubDeps:
    def __init__(self, run_job):
        self.parser = StubParser()
        self.context = StubContext()
        self.cache = StubCache()
        self.graph = object()
        self.run_job = run_job
        self.verify = lambda target: {"ok": True, "status": 200, "elapsed_ms": 1, "error": None, "via": "stub"}
        self.sandbox_dirs = []


def _ok_run_job(graph, request, job_id, mode, approve_waiter):
    return {
        "id": job_id,
        "label": request.label,
        "phase": "report",
        "attempt": 1,
        "max_attempts": 3,
        "cached": False,
        "steps": [
            {
                "index": 0,
                "command": request.steps[0].command_string,
                "cwd": request.steps[0].target_directory,
                "exit_code": 0,
                "stdout_tail": "",
                "stderr_tail": "",
                "elapsed_ms": 3,
            }
        ],
        "done": True,
        "ok": True,
    }


REQUEST = {
    "label": "install deps",
    "steps": [{"target_directory": ".", "command_string": "npm install", "timeout_seconds": 120}],
}


@pytest.fixture
def settings(tmp_path):
    return EngineSettings(
        token=TOKEN,
        data_dir=str(tmp_path / "data"),
        sandbox_root=str(tmp_path / "sandbox"),
        screenpipe_db=str(tmp_path / "missing.db"),
        vllm_url="http://127.0.0.1:1/v1",
    )


@pytest.fixture
def client(settings):
    deps = StubDeps(_ok_run_job)
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as c:
        c.deps = deps
        yield c


def _h():
    return {"x-o-token": TOKEN}


def _wait_done(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/jobs/{job_id}", headers=_h()).json()
        if body["done"]:
            return body
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("O_ENGINE_TOKEN", "abc")
    monkeypatch.setenv("O_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("O_PERMISSION_MODE", "ask")
    s = EngineSettings.from_env(data_dir=str(tmp_path))
    assert s.token == "abc"
    assert s.max_attempts == 5
    assert s.permission_mode == "ask"
    assert s.vllm_url == "http://127.0.0.1:8000/v1"
    assert s.claude_path == "claude"


def test_requires_token(client):
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"x-o-token": "wrong"}).status_code == 401
    assert client.get("/cache").status_code == 401
    assert client.post("/provision", json=REQUEST).status_code == 401


def test_health_shape(client):
    r = client.get("/health", headers=_h())
    assert r.status_code == 200
    body = r.json()
    required = {"ok", "version", "vllm", "screenpipe", "xvfb", "playwright", "cu_backend", "cu_displays",
                "mcp_servers", "docker", "e2b", "crewai", "browser_use", "open_interpreter"}
    assert required <= set(body)
    assert set(body["cu_backend"]) == {"name", "available", "reason"}
    assert isinstance(body["cu_backend"]["available"], bool)
    assert isinstance(body["cu_displays"], list) and body["cu_displays"]
    assert body["ok"] is True
    assert body["screenpipe"] is False
    assert body["vllm"] is False
    for key in ("xvfb", "playwright"):
        assert isinstance(body[key], bool)


def test_provision_jobs_round_trip(client, settings):
    r = client.post("/provision", json=REQUEST, headers=_h())
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    body = _wait_done(client, job_id)
    assert body["id"] == job_id
    assert body["ok"] is True
    assert body["phase"] == "report"
    assert body["steps"][0]["command"] == "npm install"
    assert (settings.sandbox_root_path / job_id).is_dir()
    lines = (settings.data_dir_path / "engine-actions.jsonl").read_text().splitlines()
    kinds = [json.loads(l)["kind"] for l in lines]
    assert "task" in kinds and "result" in kinds


def test_provision_rejects_bad_request(client):
    r = client.post("/provision", json={"label": "x", "steps": [{"command_string": "ls"}]}, headers=_h())
    assert r.status_code == 422


def test_jobs_unknown_404(client):
    assert client.get("/jobs/nope", headers=_h()).status_code == 404
    assert client.post("/jobs/nope/approve", json={"allow": True}, headers=_h()).status_code == 404


def test_approve_unblocks_waiter(settings):
    seen = {}

    def waiting_run_job(graph, request, job_id, mode, approve_waiter):
        seen["decision"] = approve_waiter(job_id, 5.0)
        return {**_ok_run_job(graph, request, job_id, mode, approve_waiter), "ok": bool(seen["decision"])}

    settings = settings.model_copy(update={"permission_mode": "ask"})
    deps = StubDeps(waiting_run_job)
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        job_id = client.post("/provision", json=REQUEST, headers=_h()).json()["job_id"]
        time.sleep(0.05)
        assert client.get(f"/jobs/{job_id}", headers=_h()).json()["done"] is False
        r = client.post(f"/jobs/{job_id}/approve", json={"allow": True}, headers=_h())
        assert r.status_code == 200 and r.json() == {"ok": True}
        body = _wait_done(client, job_id)
    assert seen["decision"] is True
    assert body["ok"] is True


def test_job_failure_is_reported(settings):
    def boom(graph, request, job_id, mode, approve_waiter):
        raise RuntimeError("graph exploded")

    deps = StubDeps(boom)
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        job_id = client.post("/provision", json=REQUEST, headers=_h()).json()["job_id"]
        body = _wait_done(client, job_id)
    assert body["done"] is True and body["ok"] is False
    assert "graph exploded" in body["error"]


def test_parse_ok_and_422_on_fence(client):
    r = client.post("/parse", json={"text": "ModuleNotFoundError: No module named 'flask'"}, headers=_h())
    assert r.status_code == 200
    assert r.json()["steps"][0]["command_string"] == "pip install flask"
    r = client.post("/parse", json={"text": "```json\n[]\n```"}, headers=_h())
    assert r.status_code == 422
    assert "fence" in r.json()["detail"]


def test_diagnose_extracts_missing_package(client):
    payload = {
        "stderr": "Traceback (most recent call last):\n  File \"app.py\", line 1\nModuleNotFoundError: No module named 'flask'\n",
        "stdout": "",
        "exit_code": 1,
        "command": "python app.py",
        "cwd": "/tmp/proj",
    }
    r = client.post("/diagnose", json=payload, headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["missing_package"] == "flask"
    assert body["category"] == "missing_module"
    assert body["error_line"].startswith("ModuleNotFoundError")
    assert body["exit_code"] == 1
    assert body["telemetry"][0]["text"] == "Traceback"
    assert body["suggested_steps"][0]["command_string"] == "pip install flask"


def test_cache_lookup_and_list(client):
    req = ProvisionRequest.model_validate(REQUEST)
    import platform as _p

    fp = req.fingerprint(_p.system().lower())
    r = client.post("/cache/lookup", json={"request": REQUEST}, headers=_h())
    assert r.status_code == 200
    assert r.json()["hit"] is False and r.json()["fingerprint"] == fp
    client.deps.cache.entries[fp] = "/cache/" + fp + ".sh"
    r = client.post("/cache/lookup", json={"request": REQUEST}, headers=_h())
    assert r.json() == {"hit": True, "fingerprint": fp, "script_path": "/cache/" + fp + ".sh"}
    r = client.get("/cache", headers=_h())
    assert r.status_code == 200 and r.json()[0]["fingerprint"] == fp


def test_verify_and_context(client):
    r = client.post("/verify", json={"port": 3000}, headers=_h())
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.get("/context", params={"cwd": "/tmp/x"}, headers=_h())
    assert r.status_code == 200 and r.json()["cwd"] == "/tmp/x"
