"""/health answers from a cached snapshot: the first response says ``warming: true`` within milliseconds, nothing
expensive runs on the request thread, the background thread refreshes the probed half, and the cheap half is
always live. Also the global exception handler: every unhandled route failure is JSON with ``detail`` + a
three-line trace and an action-log entry."""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from o_engine import server as srv
from o_engine.server import HEALTH_WARMING, EngineSettings, HealthMonitor, create_app

TOKEN = "health-token"


def _h():
    return {"x-o-token": TOKEN}


@pytest.fixture
def settings(tmp_path):
    return EngineSettings(token=TOKEN, data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sb"),
                          screenpipe_db=str(tmp_path / "missing.db"), vllm_url="http://127.0.0.1:1/v1")


ELECTRON_FIELDS = {"ok", "vllm", "screenpipe", "cu_backend", "accessibility_trusted", "playwright", "docker",
                   "xvfb", "cu_displays", "mcp_servers", "e2b", "crewai", "browser_use", "open_interpreter", "version"}


def test_first_response_is_warming_and_fast(settings, monkeypatch):
    """The expensive probes run on the monitor thread only; the request path never waits on them."""
    probe_threads: list[str] = []
    gate = threading.Event()

    def slow_probes(s):
        probe_threads.append(threading.current_thread().name)
        gate.wait(2.0)  # pretend vLLM / docker info / Playwright take a while
        return {**HEALTH_WARMING, "vllm": True, "docker": True}

    monkeypatch.setattr(srv, "health_probes", slow_probes)
    app = create_app(settings, deps_factory=lambda s: object())
    with TestClient(app) as c:
        t0 = time.monotonic()
        r = c.get("/health", headers=_h())
        elapsed = time.monotonic() - t0
        body = r.json()
        assert r.status_code == 200 and body["ok"] is True and body["warming"] is True
        assert elapsed < 0.5, f"/health took {elapsed:.2f}s while a probe was blocked"
        assert ELECTRON_FIELDS <= set(body)
        assert body["vllm"] is False and body["docker"] is False and body["probed_at"] is None
        assert body["cu_backend"] == {"name": "pending", "available": False, "reason": "probing"}
        gate.set()
        assert app.state.health.wait(5.0)
        body = c.get("/health", headers=_h()).json()
        assert body["warming"] is False and body["vllm"] is True and body["docker"] is True
        assert isinstance(body["probed_at"], float)
    assert probe_threads and all(n == "o-health" for n in probe_threads), probe_threads


def test_request_path_never_runs_a_probe(settings, monkeypatch):
    """Even after warm-up, a /health request itself never calls a probe function."""
    calls: list[str] = []

    def probes(s):
        calls.append(threading.current_thread().name)
        return dict(HEALTH_WARMING)

    for name in ("probe_cu_backend", "_probe_playwright", "_probe_vllm", "_probe_docker", "probe_accessibility_trusted"):
        monkeypatch.setattr(srv, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"probe on request path")))
    monkeypatch.setattr(srv, "health_probes", probes)
    app = create_app(settings, deps_factory=lambda s: object())
    with TestClient(app) as c:
        assert app.state.health.wait(5.0)
        before = len(calls)
        for _ in range(5):
            assert c.get("/health", headers=_h()).status_code == 200
        assert len(calls) == before, "requests did not trigger probes"
        assert all(n == "o-health" for n in calls)


def test_cheap_fields_are_live_between_refreshes(settings, monkeypatch):
    monkeypatch.setattr(srv, "health_probes", lambda s: dict(HEALTH_WARMING))
    app = create_app(settings, deps_factory=lambda s: object())
    with TestClient(app) as c:
        assert app.state.health.wait(5.0)
        assert c.get("/health", headers=_h()).json()["screenpipe"] is False
        settings.data_dir_path.mkdir(parents=True, exist_ok=True)
        (settings.data_dir_path / "sp.db").write_text("x")
        settings.screenpipe_db = str(settings.data_dir_path / "sp.db")
        assert c.get("/health", headers=_h()).json()["screenpipe"] is True
        settings.mcp_servers_path.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}}}))
        assert c.get("/health", headers=_h()).json()["mcp_servers"] == 1


def test_monitor_refreshes_on_its_interval_and_survives_a_failing_probe(settings):
    calls = [0]

    def probes(s):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("docker exploded")
        return {**HEALTH_WARMING, "vllm": calls[0] % 2 == 1}

    m = HealthMonitor(settings, interval_s=0.05, probes=probes)
    m.start()
    assert m.wait(2.0)
    deadline = time.monotonic() + 3.0
    while m.refreshes < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    m.stop()
    assert m.refreshes >= 3, "the thread keeps refreshing on its interval"
    snap = m.snapshot()
    assert snap["warming"] is False and "ok" in snap and snap["ok"] is True
    # the failing refresh was recorded, not fatal
    m2 = HealthMonitor(settings, interval_s=60, probes=lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    m2.refresh()
    snap = m2.snapshot()
    assert snap["warming"] is False and snap["probe_error"].startswith("RuntimeError: boom")
    assert snap["vllm"] is False


def test_default_interval_is_fifteen_seconds():
    assert srv.HEALTH_REFRESH_S == 15.0


def test_health_report_for_the_cli_is_still_the_full_synchronous_report(settings, monkeypatch):
    monkeypatch.setattr(srv, "health_probes", lambda s: {**HEALTH_WARMING, "vllm": True})
    report = srv.health_report(settings)
    assert report["ok"] is True and report["vllm"] is True and "warming" not in report
    assert ELECTRON_FIELDS <= set(report)


# ------------------------------------------------------------------ global exception handler


def test_unhandled_route_exception_is_json_with_trace_and_logged(settings, tmp_path):
    class Deps:
        @property
        def cu_procedures(self):
            raise KeyError("procedures store is broken")

    app = create_app(settings, deps_factory=lambda s: Deps())
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/computer/procedures", headers=_h())
        assert r.status_code == 500
        body = r.json()
        assert set(body) == {"detail", "trace"}
        assert body["detail"] == "KeyError: 'procedures store is broken'"
        assert isinstance(body["trace"], list) and 1 <= len(body["trace"]) <= 3
        assert body["trace"][-1].startswith("KeyError")
        # SchemaViolation keeps its own 422 handler
        r = c.post("/computer/run", json={"goal": ""}, headers=_h())
        assert r.status_code == 422 and "detail" in r.json() and "trace" not in r.json()
    lines = [json.loads(ln) for ln in (tmp_path / "data" / "engine-actions.jsonl").read_text().splitlines()]
    errors = [e for e in lines if e["kind"] == "error"]
    assert errors and errors[-1]["detail"]["route"] == "/computer/procedures"
    assert errors[-1]["detail"]["error"].startswith("KeyError") and len(errors[-1]["detail"]["trace"]) <= 3
