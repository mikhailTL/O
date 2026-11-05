"""Plan J routes and wiring: /presence, /presence/simulate, /sensors, /sensors/live, /milestones,
/brief/latest; headless isolation while ABSENT; milestone hook on finished jobs; settings/env."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from o_engine.ambient import ABSENT, PRESENT, UNKNOWN
from o_engine.executor import Sandbox
from o_engine.server import EngineDeps, EngineSettings, create_app

TOKEN = "ambient-token"


def _h():
    return {"x-o-token": TOKEN}


class FakeVM:
    """A stand-in isolated backend: runs the step locally with /work mapped to the job dir."""

    name = "fakevm"
    reason = ""
    available = True
    runs: list = []

    def __init__(self, job_dir):
        self.root = Path(job_dir)

    def run(self, step, index=0):
        FakeVM.runs.append((str(self.root), step.target_directory, step.command_string))
        local = step.model_copy(update={"command_string": step.command_string.replace("/work", str(self.root))})
        return Sandbox(str(self.root)).run(local, index)

    def destroy(self):
        pass


@pytest.fixture
def settings(tmp_path):
    return EngineSettings(token=TOKEN, data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sandbox"),
                          screenpipe_db=str(tmp_path / "missing.db"), vllm_url="http://127.0.0.1:1/v1",
                          learning=False)


@pytest.fixture
def client(settings):
    app = create_app(settings, deps_factory=EngineDeps)
    app.state.ambient.isolated.candidates = lambda job_dir, s: [FakeVM(job_dir)]
    FakeVM.runs = []
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def _wait(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/jobs/{job_id}", headers=_h()).json()
        if body["done"]:
            return body
        time.sleep(0.02)
    raise AssertionError("job never finished")


def _log_rows(settings):
    return [json.loads(l) for l in (Path(settings.data_dir) / "engine-actions.jsonl").read_text().splitlines()]


REQ = {"label": "write marker", "steps": [{"target_directory": ".", "command_string": "echo hi > marker.txt",
                                           "timeout_seconds": 30}]}


def test_routes_require_token(client):
    for method, path in (("get", "/presence"), ("get", "/sensors"), ("get", "/sensors/live"), ("get", "/milestones"),
                         ("post", "/milestones"), ("get", "/brief/latest"), ("post", "/presence/simulate")):
        assert getattr(client, method)(path).status_code == 401, path


def test_presence_shape_and_defaults(client):
    body = client.get("/presence", headers=_h()).json()
    assert body["state"] == UNKNOWN and body["ambient"] is False and body["headless"] is False
    assert isinstance(body["since"], float) and body["sources"] == [] and body["absent_after_s"] == 300.0


def test_sensors_off_by_default_with_reasons(client):
    body = client.get("/sensors", headers=_h()).json()
    names = [s["name"] for s in body["sensors"]]
    assert names == ["wifi-csi", "sonar", "camera-face", "heart-rppg"]
    assert all(s["enabled"] is False and s["running"] is False for s in body["sensors"])
    assert all(isinstance(s["available"], bool) and isinstance(s["reason"], str) for s in body["sensors"])
    wifi = body["sensors"][0]
    assert "unvalidated" in wifi["vitals_note"] and wifi["available"] is False
    assert body["indicator"] is False and body["bus"]["kind"] == "inproc" and body["tts"]["spoken_by"] == "app"


def test_milestones_post_get_and_validation(client):
    r = client.post("/milestones", json={"kind": "completed", "title": "Chat task: summarise inbox", "task_id": "c1"},
                    headers=_h())
    assert r.status_code == 200 and r.json()["milestone"]["presence"] == UNKNOWN
    assert client.post("/milestones", json={"kind": "nope", "title": "x"}, headers=_h()).status_code == 422
    assert client.post("/milestones", json={"kind": "completed", "title": ""}, headers=_h()).status_code == 422
    assert client.post("/milestones", json={"kind": "completed", "title": "x", "extra": 1}, headers=_h()).status_code == 422
    rows = client.get("/milestones", headers=_h()).json()["milestones"]
    assert [m["title"] for m in rows] == ["Chat task: summarise inbox"]
    later = client.get("/milestones", params={"since": time.time() + 60}, headers=_h()).json()["milestones"]
    assert later == []


def test_present_job_keeps_existing_sandbox_and_records_milestone(client, settings):
    job_id = client.post("/provision", json=REQ, headers=_h()).json()["job_id"]
    body = _wait(client, job_id)
    assert body["ok"] is True and "headless" not in body
    assert FakeVM.runs == []
    assert (Path(settings.sandbox_root) / job_id / "marker.txt").read_text().strip() == "hi"
    deadline = time.time() + 5
    rows = []
    while time.time() < deadline and not rows:
        rows = client.get("/milestones", headers=_h()).json()["milestones"]
        time.sleep(0.02)
    assert rows[0]["task_id"] == job_id and rows[0]["kind"] == "completed" and rows[0]["presence"] == UNKNOWN


def test_absent_runs_headless_then_present_briefs(client, settings):
    r = client.post("/presence/simulate", json={"state": ABSENT, "reason": "demo"}, headers=_h())
    assert r.status_code == 200 and r.json()["presence"]["state"] == ABSENT
    absent_start = r.json()["presence"]["absent_since"]
    assert client.get("/presence", headers=_h()).json()["sources"] == ["simulated"]
    job_id = client.post("/provision", json=REQ, headers=_h()).json()["job_id"]
    body = _wait(client, job_id)
    assert body["ok"] is True and body["headless"] is True and body["isolation"] == "fakevm"
    assert FakeVM.runs and FakeVM.runs[0][1] == "."
    assert (Path(settings.sandbox_root) / job_id / "marker.txt").read_text().strip() == "hi"
    deadline = time.time() + 5
    while time.time() < deadline and not client.get("/milestones", headers=_h()).json()["milestones"]:
        time.sleep(0.02)
    ms = client.get("/milestones", headers=_h()).json()["milestones"]
    assert ms[0]["presence"] == ABSENT and "headless:fakevm" in ms[0]["detail"]

    assert client.post("/presence/simulate", json={"state": PRESENT}, headers=_h()).json()["presence"]["state"] == PRESENT
    brief = None
    deadline = time.time() + 15
    while time.time() < deadline and brief is None:
        brief = client.get("/brief/latest", headers=_h()).json()["brief"]
        time.sleep(0.05)
    assert brief is not None and brief["since"] == absent_start and brief["count"] == 1
    assert len(brief["sentences"]) == 3 and brief["via"] == "template"  # vLLM is unreachable in tests

    job2 = client.post("/provision", json=REQ, headers=_h()).json()["job_id"]
    assert "headless" not in _wait(client, job2)
    summaries = [r["summary"] for r in _log_rows(settings)]
    assert any(s.startswith("presence simulated: USER_ABSENT") for s in summaries)
    assert any("UNKNOWN -> USER_ABSENT" in s for s in summaries)
    assert any("USER_ABSENT -> USER_PRESENT" in s for s in summaries)
    assert any("running headless in an isolated sandbox" in s for s in summaries)
    assert any(s.startswith("brief ready") for s in summaries)


def test_script_run_headless_while_absent(client):
    client.post("/presence/simulate", json={"state": ABSENT}, headers=_h())
    r = client.post("/script/run", json={"language": "bash", "code": "echo from-script"}, headers=_h())
    body = r.json()
    assert r.status_code == 200 and body["exit_code"] == 0 and body["headless"] is True and body["isolation"] == "fakevm"
    assert "from-script" in body["stdout_tail"]


def test_simulate_validation(client):
    assert client.post("/presence/simulate", json={"state": "ASLEEP"}, headers=_h()).status_code == 422
    assert client.post("/presence/simulate", json={}, headers=_h()).status_code == 422


def test_sensors_live_is_empty_and_never_logged(client, settings):
    body = client.get("/sensors/live", headers=_h()).json()
    assert body["enabled"] is False and body["pulse"] is None and body["breathing"] is None
    assert not any("sensors/live" in r["summary"] or "bpm" in json.dumps(r) for r in _log_rows(settings))


def test_settings_from_env_reads_app_names(monkeypatch, tmp_path):
    for k, v in {"O_AMBIENT": "1", "O_SENSOR_WIFI": "1", "O_SENSOR_SONAR": "1", "O_SENSOR_CAMERA": "1",
                 "O_SENSOR_HEART_RATE": "1", "O_ABSENT_AFTER_S": "120", "O_BRIEF_TTS": "1",
                 "O_BUS_URL": "mqtt://127.0.0.1:1883", "O_AUTO_REPAIR": "1"}.items():
        monkeypatch.setenv(k, v)
    s = EngineSettings.from_env(data_dir=str(tmp_path))
    assert (s.ambient, s.sensor_csi, s.sensor_sonar, s.sensor_camera, s.sensor_heart) == (True,) * 5
    assert s.absent_after_s == 120.0 and s.brief_tts and s.bus_url.startswith("mqtt") and s.auto_repair
    for k in ("O_SENSOR_WIFI", "O_SENSOR_HEART_RATE"):
        monkeypatch.delenv(k)
    monkeypatch.setenv("O_SENSOR_CSI", "1")
    monkeypatch.setenv("O_SENSOR_HEART", "1")
    s = EngineSettings.from_env(data_dir=str(tmp_path))
    assert s.sensor_csi and s.sensor_heart


def test_settings_defaults_all_off(tmp_path, monkeypatch):
    for k in ("O_AMBIENT", "O_SENSOR_WIFI", "O_SENSOR_CSI", "O_SENSOR_SONAR", "O_SENSOR_CAMERA", "O_SENSOR_HEART_RATE",
              "O_SENSOR_HEART", "O_BRIEF_TTS", "O_ABSENT_AFTER_S"):
        monkeypatch.delenv(k, raising=False)
    s = EngineSettings.from_env(data_dir=str(tmp_path))
    assert not any((s.ambient, s.sensor_csi, s.sensor_sonar, s.sensor_camera, s.sensor_heart, s.brief_tts))
    assert s.absent_after_s == 300.0


def test_ambient_on_without_sensors_uses_os_idle(tmp_path, monkeypatch):
    from o_engine.ambient import presence as presence_mod

    monkeypatch.setattr(presence_mod, "TICK_S", 0.02)
    s = EngineSettings(token=TOKEN, data_dir=str(tmp_path / "d"), sandbox_root=str(tmp_path / "s"),
                       screenpipe_db=str(tmp_path / "x.db"), vllm_url="http://127.0.0.1:1/v1", ambient=True,
                       absent_after_s=60)
    app = create_app(s, deps_factory=EngineDeps)
    idle = [900.0]
    app.state.ambient.presence.idle_source = lambda: idle[0]
    with TestClient(app) as c:
        deadline = time.time() + 5
        while time.time() < deadline and c.get("/presence", headers=_h()).json()["state"] != ABSENT:
            time.sleep(0.02)
        body = c.get("/presence", headers=_h()).json()
        assert body["state"] == ABSENT and body["source"] == "os_idle" and body["running"] is True
        assert body["sources"] == ["os_idle"]
        idle[0] = 0.5
        deadline = time.time() + 5
        while time.time() < deadline and c.get("/presence", headers=_h()).json()["state"] != PRESENT:
            time.sleep(0.02)
        assert c.get("/presence", headers=_h()).json()["state"] == PRESENT
    assert app.state.ambient.presence.snapshot()["running"] is False
