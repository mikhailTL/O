"""Interrupt on return: low-priority headless jobs pause between steps while the user is present and
resume when the user is away again (ambient/pause.py, the IsolatedJobSandbox checkpoint, layer wiring)."""
from __future__ import annotations

import threading
import time
import types
from pathlib import Path

from o_engine.ambient import ABSENT, PRESENT
from o_engine.ambient.isolation import IsolatedSandboxes
from o_engine.ambient.layer import AmbientLayer
from o_engine.ambient.pause import PauseGate, normalise_priority, pop_priority
from o_engine.executor import Sandbox
from o_engine.schema import ProvisionStep


class Log:
    def __init__(self):
        self.rows = []

    def record(self, kind, summary, **detail):
        self.rows.append((kind, summary, detail))


class LocalVM:
    name = "localvm"
    reason = ""
    available = True

    def __init__(self, job_dir):
        self.root = Path(job_dir)
        self.ran = []

    def run(self, step, index=0):
        self.ran.append(index)
        local = step.model_copy(update={"command_string": step.command_string.replace("/work", str(self.root))})
        return Sandbox(str(self.root)).run(local, index)

    def destroy(self):
        pass


def _step(cmd="true"):
    return ProvisionStep(target_directory=".", command_string=cmd, timeout_seconds=10)


def test_priority_parsing():
    body = {"label": "x", "priority": "LOW"}
    assert pop_priority(body) == "low" and "priority" not in body
    assert pop_priority({"label": "x"}) == "normal"
    assert normalise_priority("urgent") == "normal" and normalise_priority("high") == "high"
    assert pop_priority(None) == "normal"


def test_gate_blocks_only_low_jobs_while_present():
    log = Log()
    gate = PauseGate(log)
    gate.register("low1", "low")
    gate.register("norm1", "normal")
    assert gate.checkpoint("low1") == 0.0  # not paused yet
    gate.on_presence({"state": PRESENT})
    assert gate.paused and gate.checkpoint("norm1") == 0.0 and gate.checkpoint("unknown-job") == 0.0
    done = threading.Event()
    waited = {}

    def run():
        waited["s"] = gate.checkpoint("low1", step=2)
        done.set()

    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.3)
    assert not done.is_set() and gate.snapshot()["waiting"] == ["low1"]
    gate.on_presence({"state": ABSENT})
    assert done.wait(3) and waited["s"] >= 0.25
    assert gate.snapshot()["waiting"] == [] and gate.pauses[-1]["step"] == 2
    summaries = [r[1] for r in log.rows]
    assert any("paused at a safe point (before step 2)" in s for s in summaries)
    assert any("resumed after" in s for s in summaries)


def test_gate_release_all_and_forget_never_leave_a_job_blocked():
    gate = PauseGate()
    gate.register("a", "low")
    gate.set_paused(True)
    t = threading.Thread(target=gate.checkpoint, args=("a",))
    t.start()
    time.sleep(0.1)
    gate.forget("a")
    t.join(3)
    assert not t.is_alive()
    gate.register("b", "low")
    t = threading.Thread(target=gate.checkpoint, args=("b",))
    t.start()
    time.sleep(0.1)
    gate.release_all()
    t.join(3)
    assert not t.is_alive()
    assert gate.checkpoint("b", timeout=0.05) == 0.0


def test_isolated_sandbox_checkpoints_between_steps(tmp_path):
    gate = PauseGate()
    vms = {}

    def candidates(job_dir, _s):
        vms[job_dir] = LocalVM(job_dir)
        return [vms[job_dir]]

    isolated = IsolatedSandboxes(tmp_path / "sb", None, None, candidates=candidates, gate=gate)
    gate.register("job1", "low")
    sb = isolated.for_job("job1")
    vm = next(iter(vms.values()))
    assert sb.run(_step("echo one"), 0).exit_code == 0
    gate.on_presence({"state": PRESENT})
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", sb.run(_step("echo two"), 1)))
    th.start()
    time.sleep(0.3)
    assert vm.ran == [0] and "r" not in out  # step 1 did not start: paused at the safe point
    gate.on_presence({"state": ABSENT})
    th.join(5)
    assert out["r"].exit_code == 0 and vm.ran == [0, 1]
    isolated.release("job1")


def test_local_fallback_also_checkpoints(tmp_path):
    gate = PauseGate()
    isolated = IsolatedSandboxes(tmp_path / "sb", None, None, candidates=lambda d, s: [], gate=gate)
    gate.register("j", "low")
    gate.set_paused(True)
    sb = isolated.for_job("j")
    assert sb.backend is None
    th = threading.Thread(target=lambda: sb.run(_step("true"), 0))
    th.start()
    time.sleep(0.2)
    assert th.is_alive()
    gate.set_paused(False)
    th.join(5)
    assert not th.is_alive()


def _settings(tmp_path):
    return types.SimpleNamespace(
        bus_url="", data_dir=str(tmp_path / "data"), ambient=False, sensor_csi=False, csi_udp="", ruview_ws="",
        ruview_mqtt="", sensor_heart=False, sensor_sonar=False, sensor_camera=False, rppg_model="FacePhys.rlap",
        absent_after_s=60.0, vllm_url="http://127.0.0.1:1/v1", vllm_model="m", auto_repair=False,
        sandbox_root=str(tmp_path / "sb"), settings_like={}, vllm_cmd="", screenpipe_cmd="")


def test_layer_wires_presence_to_the_gate_and_reports_it(tmp_path):
    (tmp_path / "data").mkdir()
    layer = AmbientLayer(_settings(tmp_path), Log(), isolation_candidates=lambda d, s: [LocalVM(d)])
    body = {"label": "nightly", "steps": [], "priority": "low"}
    assert layer.take_priority("jobX", body) == "low" and "priority" not in body
    layer.presence.simulate(ABSENT)
    assert layer.pause_gate.paused is False
    sb = layer.isolated.for_job("jobX")
    layer.presence.simulate(PRESENT)  # synchronous listener: the flag is set before simulate returns
    assert layer.pause_gate.paused is True
    th = threading.Thread(target=lambda: sb.run(_step("true"), 0))
    th.start()
    time.sleep(0.2)
    assert layer.presence_view()["pause"]["waiting"] == ["jobX"]
    layer.presence.simulate(ABSENT)
    th.join(5)
    assert not th.is_alive() and layer.presence_view()["pause"]["waiting"] == []
    layer.job_finished("jobX")
    assert layer.pause_gate.priority("jobX") == "normal"
    layer.stop()


# ----------------------------------------------------------------------------- routes (server.py hook)


def test_provision_priority_low_pauses_between_steps_on_return(tmp_path):
    """Needs the two-line server.py hook (``ambient.take_priority`` / ``ambient.job_finished``); skips
    on a server that still rejects ``priority`` as an unknown request field."""
    import pytest
    from fastapi.testclient import TestClient

    from o_engine.server import EngineDeps, EngineSettings, create_app

    h = {"x-o-token": "t"}
    settings = EngineSettings(token="t", data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sandbox"),
                              screenpipe_db=str(tmp_path / "missing.db"), vllm_url="http://127.0.0.1:1/v1", learning=False)
    app = create_app(settings, deps_factory=EngineDeps)
    app.state.ambient.isolated.candidates = lambda job_dir, s: [LocalVM(job_dir)]
    steps = [{"target_directory": ".", "command_string": "for i in $(seq 600); do [ -f go ] && break; sleep 0.05; done; echo a >> trace.txt", "timeout_seconds": 30},
             {"target_directory": ".", "command_string": "echo b >> trace.txt", "timeout_seconds": 30}]
    def wait_phase(c, job_id, phase, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            st = c.get(f"/jobs/{job_id}", headers=h).json()
            if st.get("phase") == phase or st.get("done"):
                return st
            time.sleep(0.02)
        raise AssertionError(f"{job_id} never reached {phase}")

    with TestClient(app) as c:
        warm = c.post("/provision", json={"label": "warm-up", "steps": steps[1:]}, headers=h).json()["job_id"]
        wait_phase(c, warm, "report")  # deps are built; the next jobs pick their graph at once
        c.post("/presence/simulate", json={"state": ABSENT}, headers=h)
        r = c.post("/provision", json={"label": "goal cycle", "steps": steps, "priority": "low"}, headers=h)
        if r.status_code == 422 and "priority" in r.text:
            pytest.skip("server.py does not pass `priority` to the ambient layer yet")
        low = r.json()["job_id"]
        other = [{**st, "command_string": st["command_string"].replace("trace.txt", "other.txt")} for st in steps]
        normal = c.post("/provision", json={"label": "user ask", "steps": other}, headers=h).json()["job_id"]
        assert wait_phase(c, low, "execute")["headless"] is True  # inside step 0 (held until the go file)
        wait_phase(c, normal, "execute")
        c.post("/presence/simulate", json={"state": PRESENT}, headers=h)
        # step 0 of both jobs is held on a "go" file, so the return always lands mid-step 0
        for jid in (low, normal):
            (Path(settings.sandbox_root) / jid / "go").write_text("1")
        deadline = time.time() + 10
        while time.time() < deadline and not c.get(f"/jobs/{low}", headers=h).json().get("paused"):
            time.sleep(0.05)
        st = c.get(f"/jobs/{low}", headers=h).json()
        assert st["paused"] is True and st["priority"] == "low" and not st["done"]
        trace = Path(settings.sandbox_root) / low / "trace.txt"
        assert trace.read_text().split() == ["a"]  # step 0 finished, step 1 has not started
        assert low in c.get("/presence", headers=h).json()["pause"]["waiting"]
        while time.time() < deadline and not c.get(f"/jobs/{normal}", headers=h).json()["done"]:
            time.sleep(0.05)
        assert c.get(f"/jobs/{normal}", headers=h).json()["ok"] is True  # normal priority never waits
        c.post("/presence/simulate", json={"state": ABSENT}, headers=h)
        while time.time() < deadline + 10 and not c.get(f"/jobs/{low}", headers=h).json()["done"]:
            time.sleep(0.05)
        final = c.get(f"/jobs/{low}", headers=h).json()
        assert final["ok"] is True and final["headless"] is True and trace.read_text().split() == ["a", "b"]
        forget_by = time.time() + 3  # job_finished runs just after the job reports done
        while time.time() < forget_by and app.state.ambient.pause_gate.priority(low) != "normal":
            time.sleep(0.02)
        assert app.state.ambient.pause_gate.priority(low) == "normal"  # forgotten after the job
