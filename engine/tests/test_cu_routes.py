"""Computer-use routes: auth, /computer/run → /jobs round trip (with live cu phases), replay, tree, find, health,
display pixel gating, display shutdown, and the CLI's candidates guard."""
import json
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

import o_engine.cu.backends as backends_pkg
import o_engine.display as display_mod
from o_engine.server import CU_CANDIDATES_NEED_LINUX, CU_PHASES, EngineDeps, EngineSettings, create_app

TOKEN = "test-token-cu"


class StubProcedure:
    def __init__(self, pid, app, goal, version=1):
        self.id = pid
        self.app = app
        self.goal = goal
        self.version = version
        self.path = f"{app}/{pid.split('/')[-2]}/v{version}.json"
        self.parent = None
        self.healed_step = None
        self.reason = None
        self.created = "2026-11-04T00:00:00+00:00"

    def model_dump(self):
        return dict(self.__dict__)


class StubProcedures:
    def __init__(self):
        self.rows = [StubProcedure("libreoffice-calc/abc123/v1", "libreoffice-calc", "Put 42 in A1")]

    def list(self):
        return list(self.rows)

    def get(self, pid):
        return next((r for r in self.rows if r.id == pid), None)


class StubNode:
    def __init__(self, nid, role, name, children=()):
        self.id, self.role, self.name, self.text = nid, role, name, None
        self.rect = {"x": 0, "y": 0, "w": 10, "h": 10}
        self.states = ["visible"]
        self.children = list(children)


class StubBackend:
    name = "fake"
    available = True
    reason = ""

    def __init__(self):
        self.tree_calls = []
        self.find_calls = []

    def tree(self, window=None):
        self.tree_calls.append(window)
        return [StubNode("n0", "frame", "Calc", [StubNode("n1", "table cell", "A1")])]

    def find(self, target):
        self.find_calls.append(target)
        return StubNode("n1", "table cell", "A1") if target.name == "A1" else None


class StubDeps:
    def __init__(self, run_cu_job):
        self.cu_graph = object()
        self.cu_procedures = StubProcedures()
        self.backend = StubBackend()
        self._run_cu_job = run_cu_job
        self.cu_calls = []
        self.cu_kwargs = []
        self.stopped = 0

    def cu_backend(self, slot=0):
        return self.backend

    def cu_display_status(self):
        return [{"display": ":99", "running": False}]

    def stop_cu_displays(self):
        self.stopped += 1

    def run_cu_job(self, graph, goal, app, job_id, candidates=1, **kw):
        self.cu_calls.append((goal, app, job_id, candidates))
        self.cu_kwargs.append(dict(kw))
        return self._run_cu_job(graph, goal, app, job_id, candidates, **kw)


def _ok_run_cu_job(graph, goal, app, job_id, candidates=1, progress=None, **kw):
    base = {
        "id": job_id, "label": goal[:200], "phase": "context", "attempt": 0, "max_attempts": 3, "cached": False,
        "steps": [], "diagnostic": None, "verify": None, "done": False, "ok": False, "error": None,
        "procedure_id": None, "candidates": candidates, "scores": [],
    }
    for phase in CU_PHASES:
        if phase == "heal":
            continue
        if progress is not None:
            progress({**base, "phase": phase})
    return {
        **base, "phase": "report", "done": True, "ok": True, "procedure_id": "libreoffice-calc/abc123/v1",
        "steps": [{"index": 0, "command": "launch cmd='libreoffice --calc'", "cwd": "", "exit_code": 0,
                   "stdout_tail": "", "stderr_tail": "", "elapsed_ms": 4, "timed_out": False}],
        "verify": {"ok": True, "error": None, "reason": "", "source": "vlm", "goal": True},
    }


@pytest.fixture
def settings(tmp_path):
    return EngineSettings(
        token=TOKEN, data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sandbox"),
        screenpipe_db=str(tmp_path / "missing.db"), vllm_url="http://127.0.0.1:1/v1",
    )


@pytest.fixture
def client(settings):
    deps = StubDeps(_ok_run_cu_job)
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


def test_settings_cu_fields_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("O_ENGINE_TOKEN", "abc")
    monkeypatch.setenv("O_CU_CANDIDATES", "3")
    monkeypatch.setenv("O_CU_MAX_HEALS", "5")
    monkeypatch.setenv("O_VLLM_VISION_MODEL", "ui-tars")
    s = EngineSettings.from_env(data_dir=str(tmp_path))
    assert s.cu_candidates == 3 and s.cu_max_heals == 5 and s.vllm_vision_model == "ui-tars"
    monkeypatch.delenv("O_CU_CANDIDATES")
    monkeypatch.delenv("O_CU_MAX_HEALS")
    monkeypatch.delenv("O_VLLM_VISION_MODEL")
    d = EngineSettings.from_env(data_dir=str(tmp_path))
    assert d.cu_candidates == 1 and d.cu_max_heals == 3 and d.vllm_vision_model == ""


def test_computer_routes_require_token(client):
    assert client.post("/computer/run", json={"goal": "x"}).status_code == 401
    assert client.get("/computer/procedures").status_code == 401
    assert client.post("/computer/replay", json={"procedure_id": "p"}).status_code == 401
    assert client.get("/computer/tree").status_code == 401
    assert client.post("/computer/find", json={"target": {"role": "frame"}}).status_code == 401
    assert client.get("/computer/tree", headers={"x-o-token": "wrong"}).status_code == 401


def test_health_has_cu_backend_and_displays(client):
    body = client.get("/health", headers=_h()).json()
    assert set(body["cu_backend"]) == {"name", "available", "reason"}
    assert isinstance(body["cu_backend"]["name"], str) and isinstance(body["cu_backend"]["available"], bool)
    assert isinstance(body["cu_displays"], list) and len(body["cu_displays"]) == 1
    row = body["cu_displays"][0]
    assert set(row) == {"display", "running"}
    assert row["display"] in (":99", "host") and row["running"] is False, "nothing has started a display"


def test_health_reports_running_from_deps_once_they_exist(settings):
    deps = StubDeps(_ok_run_cu_job)
    deps.cu_display_status = lambda: [{"display": ":99", "running": True}]
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        before = client.get("/health", headers=_h()).json()["cu_displays"]
        assert before == [{"display": ":99", "running": False}] or before == [{"display": "host", "running": False}], \
            "deps not built yet: nothing can be running"
        client.get("/computer/tree", headers=_h())  # builds deps
        after = client.get("/health", headers=_h()).json()["cu_displays"]
    assert after == [{"display": ":99", "running": True}]


def test_lifespan_shutdown_stops_cu_displays(settings):
    deps = StubDeps(_ok_run_cu_job)
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        client.get("/computer/tree", headers=_h())
        assert deps.stopped == 0
    assert deps.stopped == 1, "lifespan exit stops the isolated displays next to pool.shutdown"


# ---------------------------------------------------------------- F3-c: pixel gating follows the display

class FakeDisplay:
    """Stands in for ``IsolatedDisplay``: ``available``/``running`` are scripted, start() flips running only when available."""

    instances = []

    def __init__(self, display=":99", **kw):
        self.display = display
        self.started = 0
        self.stopped = 0
        FakeDisplay.instances.append(self)

    available = False
    _running_after_start = False

    @property
    def running(self):
        return self.started > 0 and self._running_after_start

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class RecordingBackend:
    name = "recording"
    available = True

    def __init__(self, display=None, allow_pixels=False):
        self.display = display
        self.allow_pixels = allow_pixels


@pytest.fixture
def engine_deps(settings, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(display_mod, "IsolatedDisplay", FakeDisplay)
    monkeypatch.setattr(backends_pkg, "get_backend", lambda display=None, allow_pixels=False: RecordingBackend(display, allow_pixels))
    deps = EngineDeps(settings)
    FakeDisplay.instances = []  # drop the provisioning display __init__ built; only cu displays matter below
    return deps


def test_cu_backend_denies_pixels_when_xvfb_is_unavailable(engine_deps, monkeypatch):
    monkeypatch.setattr(FakeDisplay, "available", False)
    monkeypatch.setattr(FakeDisplay, "_running_after_start", False)
    backend = engine_deps.cu_backend(0)
    assert backend.allow_pixels is False
    assert backend.display == ":99"
    assert FakeDisplay.instances[0].started == 1, "start() was attempted before deciding"
    assert engine_deps.cu_display_status() == [{"display": ":99", "running": False}]


def test_cu_backend_allows_pixels_only_on_a_running_display(engine_deps, monkeypatch):
    monkeypatch.setattr(FakeDisplay, "available", True)
    monkeypatch.setattr(FakeDisplay, "_running_after_start", True)
    backend = engine_deps.cu_backend(0)
    assert backend.allow_pixels is True and backend.display == ":99"
    assert engine_deps.cu_backend(0) is backend, "one backend per slot"
    assert engine_deps.cu_display_status() == [{"display": ":99", "running": True}]
    engine_deps.stop_cu_displays()
    assert FakeDisplay.instances[0].stopped == 1


def test_cu_backend_off_linux_never_allows_pixels(settings, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(backends_pkg, "get_backend", lambda display=None, allow_pixels=False: RecordingBackend(display, allow_pixels))
    deps = EngineDeps(settings)
    backend = deps.cu_backend(0)
    assert backend.allow_pixels is False and backend.display is None
    assert deps.cu_display_status() == [{"display": "host", "running": False}]
    deps.stop_cu_displays()  # nothing to stop; must not raise


def test_cu_backend_honours_o_cu_fake_tree(settings, monkeypatch, tmp_path):
    """O_CU_FAKE_TREE=<json path> (demo/test hook) serves that tree from the in-memory FakeBackend: no display is
    started, pixels stay off, and the tree is the file's nodes."""
    from o_engine.cu.backends.fake import FakeBackend
    from o_engine.cu.schema import Target

    tree = tmp_path / "tree.json"
    tree.write_text(json.dumps([{"id": "w0", "role": "frame", "name": "Notes", "states": ["active"],
                                 "children": [{"id": "w1", "role": "push button", "name": "Save", "states": ["visible"]}]}]))
    monkeypatch.setenv("O_CU_FAKE_TREE", str(tree))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(display_mod, "IsolatedDisplay", FakeDisplay)
    monkeypatch.setattr(backends_pkg, "get_backend", lambda **kw: (_ for _ in ()).throw(AssertionError("platform backend built")))
    deps = EngineDeps(settings)
    FakeDisplay.instances = []
    backend = deps.cu_backend(0)
    assert isinstance(backend, FakeBackend) and backend.allow_pixels is False
    assert deps.cu_backend(0) is backend, "one backend per slot"
    assert FakeDisplay.instances == [], "no isolated display for a scripted tree"
    assert [n.name for n in backend.tree()] == ["Notes"]
    assert backend.find(Target(role="push button", name="Save")).id == "w1"
    assert deps.cu_display_status() == [{"display": ":99", "running": False}]
    rows = [json.loads(l) for l in (settings.data_dir_path / "engine-actions.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "note" and "O_CU_FAKE_TREE" in r["summary"] and r["detail"]["nodes"] == 1 for r in rows)


# ---------------------------------------------------------------- F3-d: replay threads procedure_id; cli guard

def test_replay_threads_procedure_id_into_the_job(client):
    r = client.post("/computer/replay", json={"procedure_id": "libreoffice-calc/abc123/v1"}, headers=_h())
    assert r.status_code == 200
    _wait_done(client, r.json()["job_id"])
    assert client.deps.cu_kwargs[-1]["procedure_id"] == "libreoffice-calc/abc123/v1"

    r = client.post("/computer/run", json={"goal": "g"}, headers=_h())
    _wait_done(client, r.json()["job_id"])
    assert client.deps.cu_kwargs[-1]["procedure_id"] is None


def test_computer_run_rejects_candidates_off_linux_with_the_shared_message(settings, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    deps = StubDeps(_ok_run_cu_job)
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        r = client.post("/computer/run", json={"goal": "g", "candidates": 2}, headers=_h())
    assert r.status_code == 422 and r.json()["detail"] == CU_CANDIDATES_NEED_LINUX
    assert deps.cu_calls == []


def test_cli_computer_run_rejects_candidates_off_linux(settings, monkeypatch, capsys):
    from o_engine import cli

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "EngineSettings", type("S", (), {"from_env": staticmethod(lambda **kw: settings)}))

    def no_deps(*a, **kw):
        raise AssertionError("EngineDeps must not be built when the request is refused")

    import o_engine.server as server_mod
    monkeypatch.setattr(server_mod, "EngineDeps", no_deps)
    rc = cli.main(["computer", "run", "open calc", "--candidates", "2"])
    assert rc != 0
    out = capsys.readouterr()
    assert CU_CANDIDATES_NEED_LINUX in out.out + out.err
    body = json.loads(out.out)
    assert body["ok"] is False and body["error"] == CU_CANDIDATES_NEED_LINUX


def test_computer_run_jobs_round_trip(client, settings):
    r = client.post("/computer/run", json={"goal": "Put 42 in A1", "app": "libreoffice-calc"}, headers=_h())
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    body = _wait_done(client, job_id)
    assert body["id"] == job_id and body["ok"] is True and body["done"] is True
    assert body["phase"] == "report"
    assert body["label"] == "Put 42 in A1"
    assert body["procedure_id"] == "libreoffice-calc/abc123/v1"
    assert body["candidates"] == 1 and body["scores"] == []
    assert body["steps"][0]["command"].startswith("launch")
    assert client.deps.cu_calls == [("Put 42 in A1", "libreoffice-calc", job_id, 1)]
    lines = (settings.data_dir_path / "engine-actions.jsonl").read_text().splitlines()
    rows = [json.loads(l) for l in lines]
    assert any(r["kind"] == "task" and r["summary"].startswith("computer run queued") for r in rows)
    assert any(r["kind"] == "result" and r["summary"] == f"computer job {job_id} ok" for r in rows)


def test_running_cu_job_reports_its_own_phase_set(settings):
    gate = threading.Event()
    seen = {}

    def slow_run(graph, goal, app, job_id, candidates=1, progress=None, **kw):
        progress({"id": job_id, "phase": "plan", "done": False, "ok": False, "steps": [], "cached": False})
        gate.wait(5.0)
        return _ok_run_cu_job(graph, goal, app, job_id, candidates, progress=progress)

    deps = StubDeps(slow_run)
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        job_id = client.post("/computer/run", json={"goal": "g"}, headers=_h()).json()["job_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            seen = client.get(f"/jobs/{job_id}", headers=_h()).json()
            if seen["phase"] == "plan":
                break
            time.sleep(0.01)
        assert seen["phase"] == "plan" and seen["done"] is False, "cu phase 'plan' is not normalised away"
        gate.set()
        final = _wait_done(client, job_id)
    assert final["phase"] == "report" and final["ok"] is True


def test_cu_job_with_unknown_phase_is_normalised_to_report(settings):
    def odd(graph, goal, app, job_id, candidates=1, **kw):
        return {**_ok_run_cu_job(graph, goal, app, job_id, candidates), "phase": "validate"}

    deps = StubDeps(odd)
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        job_id = client.post("/computer/run", json={"goal": "g"}, headers=_h()).json()["job_id"]
        assert _wait_done(client, job_id)["phase"] == "report"


def test_computer_run_validates_body(client):
    assert client.post("/computer/run", json={}, headers=_h()).status_code == 422
    assert client.post("/computer/run", json={"goal": "x", "bogus": 1}, headers=_h()).status_code == 422
    assert client.post("/computer/run", json={"goal": "x", "candidates": 0}, headers=_h()).status_code == 422


def test_computer_run_failure_is_reported(settings):
    def boom(graph, goal, app, job_id, candidates=1, **kw):
        raise RuntimeError("cu graph exploded")

    deps = StubDeps(boom)
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        job_id = client.post("/computer/run", json={"goal": "g"}, headers=_h()).json()["job_id"]
        body = _wait_done(client, job_id)
    assert body["ok"] is False and "cu graph exploded" in body["error"] and body["phase"] == "report"


def test_procedures_and_replay(client):
    r = client.get("/computer/procedures", headers=_h())
    assert r.status_code == 200
    assert r.json()[0]["id"] == "libreoffice-calc/abc123/v1" and r.json()[0]["goal"] == "Put 42 in A1"

    r = client.post("/computer/replay", json={"procedure_id": "libreoffice-calc/abc123/v1"}, headers=_h())
    assert r.status_code == 200
    body = _wait_done(client, r.json()["job_id"])
    assert body["ok"] is True
    assert client.deps.cu_calls[-1][:2] == ("Put 42 in A1", "libreoffice-calc")
    assert client.deps.cu_calls[-1][3] == 1

    assert client.post("/computer/replay", json={"procedure_id": "nope/v9"}, headers=_h()).status_code == 404
    assert client.post("/computer/replay", json={}, headers=_h()).status_code == 422


def test_tree_and_find(client):
    r = client.get("/computer/tree", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["window"] is None and body["nodes"][0]["role"] == "frame"
    assert body["nodes"][0]["children"][0]["name"] == "A1"
    assert client.deps.backend.tree_calls == [None]
    r = client.get("/computer/tree", params={"window": "*Calc*"}, headers=_h())
    assert r.json()["window"] == "*Calc*" and client.deps.backend.tree_calls[-1] == "*Calc*"

    r = client.post("/computer/find", json={"target": {"role": "table cell", "name": "A1"}}, headers=_h())
    assert r.status_code == 200 and r.json()["found"] is True and r.json()["node"]["id"] == "n1"
    assert client.deps.backend.find_calls[0].role == "table cell"
    r = client.post("/computer/find", json={"target": {"role": "table cell", "name": "Z9"}}, headers=_h())
    assert r.json() == {"found": False, "node": None}
    assert client.post("/computer/find", json={"target": {"role": "x", "bogus": 1}}, headers=_h()).status_code == 422


# ---------------------------------------------------------------- wave 2 (I-J): settings, CUDeps wiring, infeasible, /computer/interfaces

def test_settings_wave2_cu_fields_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("O_ENGINE_TOKEN", "abc")
    monkeypatch.setenv("O_CU_STEPS_PER_TURN", "7")
    monkeypatch.setenv("O_CU_MAX_TURNS", "11")
    monkeypatch.setenv("O_CU_GROUND_CONFIDENCE", "0.55")
    monkeypatch.setenv("O_CU_STEP_CANDIDATES", "3")
    monkeypatch.setenv("O_CU_FEASIBILITY", "0")
    monkeypatch.setenv("O_CU_ZOOM", "false")
    monkeypatch.setenv("O_CU_COMPARATIVE", "off")
    monkeypatch.setenv("O_CU_INTERFACE_BUILDER", "no")
    s = EngineSettings.from_env(data_dir=str(tmp_path))
    assert (s.cu_steps_per_turn, s.cu_max_turns, s.cu_ground_confidence, s.cu_step_candidates) == (7, 11, 0.55, 3)
    assert (s.cu_feasibility, s.cu_zoom, s.cu_comparative, s.cu_interface_builder) == (False, False, False, False)
    for name in ("O_CU_STEPS_PER_TURN", "O_CU_MAX_TURNS", "O_CU_GROUND_CONFIDENCE", "O_CU_STEP_CANDIDATES",
                 "O_CU_FEASIBILITY", "O_CU_ZOOM", "O_CU_COMPARATIVE", "O_CU_INTERFACE_BUILDER"):
        monkeypatch.delenv(name)
    d = EngineSettings.from_env(data_dir=str(tmp_path))
    assert (d.cu_steps_per_turn, d.cu_max_turns, d.cu_ground_confidence, d.cu_step_candidates) == (5, 20, 0.3, 1)
    assert (d.cu_feasibility, d.cu_zoom, d.cu_comparative, d.cu_interface_builder) == (True, True, True, True)


def test_settings_wave2_bounds():
    s = EngineSettings(token="t", cu_ground_confidence=1.0, cu_steps_per_turn=1, cu_max_turns=1, cu_step_candidates=1)
    assert s.cu_ground_confidence == 1.0
    with pytest.raises(Exception):
        EngineSettings(token="t", cu_ground_confidence=1.5)
    with pytest.raises(Exception):
        EngineSettings(token="t", cu_steps_per_turn=0)
    with pytest.raises(Exception):
        EngineSettings(token="t", cu_max_turns=0)
    with pytest.raises(Exception):
        EngineSettings(token="t", cu_step_candidates=0)


def _wave2_deps(settings, monkeypatch, **overrides):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(display_mod, "IsolatedDisplay", FakeDisplay)
    monkeypatch.setattr(backends_pkg, "get_backend", lambda display=None, allow_pixels=False: RecordingBackend(display, allow_pixels))
    deps = EngineDeps(settings.model_copy(update=overrides))
    FakeDisplay.instances = []
    return deps


def test_cu_deps_wires_wave2_fields(settings, monkeypatch, tmp_path):
    from o_engine.cu.feasibility import FeasibilityGate
    from o_engine.cu.ground import ZoomGrounder
    from o_engine.cu.interfaces import InterfaceBuilder, InterfaceStore
    from o_engine.cu.judge import StateJudge

    deps = _wave2_deps(settings, monkeypatch, cu_steps_per_turn=4, cu_max_turns=9, cu_ground_confidence=0.6, cu_step_candidates=2)
    cu = deps.cu_deps
    assert (cu.steps_per_turn, cu.max_turns, cu.step_candidates) == (4, 9, 2)
    assert cu.grounder.min_confidence == 0.6
    assert isinstance(cu.grounder.primary, ZoomGrounder)
    assert isinstance(cu.interface_store, InterfaceStore)
    assert cu.interface_store.dir == settings.data_dir_path / "interfaces"
    assert isinstance(cu.interface_builder, InterfaceBuilder)
    assert cu.interface_builder.store is cu.interface_store
    assert cu.interface_builder.runner is deps.runner
    assert cu.interface_builder.procedures is deps.cu_procedures
    assert "DISPLAY" in cu.interface_builder.backend_bootstrap and "sys.path" in cu.interface_builder.backend_bootstrap
    assert cu.runner is deps.runner
    assert callable(cu.sandbox_for)
    sb = cu.sandbox_for("job-x")
    assert sb.root == (settings.sandbox_root_path / "job-x").resolve()
    assert callable(cu.state_judge_factory)
    judge = cu.state_judge_factory(sb, deps.cu_backend(0))
    assert isinstance(judge, StateJudge) and judge.sandbox is sb
    # the gate builds one FeasibilityGate per job over the slot-0 backend and that job's sandbox; none is built up front
    assert cu.feasibility is not None and callable(cu.feasibility.check)
    built = []
    monkeypatch.setattr(FeasibilityGate, "check", lambda self, goal, app, job_id: built.append((self.backend, self.sandbox.root, job_id)) or {"feasible": True})
    cu.feasibility.check("g", "a", "job-y")
    assert built and built[0][0] is deps.cu_backend(0) and built[0][1] == (settings.sandbox_root_path / "job-y").resolve()


def test_cu_deps_flags_off_leave_wave2_fields_unset(settings, monkeypatch):
    from o_engine.cu.ground import VisionGrounder

    deps = _wave2_deps(settings, monkeypatch, cu_feasibility=False, cu_zoom=False, cu_interface_builder=False)
    cu = deps.cu_deps
    assert cu.feasibility is None
    assert cu.interface_builder is None
    assert isinstance(cu.grounder.primary, VisionGrounder)
    assert cu.interface_store is not None, "the store is read by /computer/interfaces even when the builder is off"


def test_cu_job_result_surfaces_infeasible(settings):
    def infeasible(graph, goal, app, job_id, candidates=1, progress=None, **kw):
        return {"id": job_id, "label": goal, "phase": "report", "done": True, "ok": False, "infeasible": True,
                "error": "no such app", "steps": [], "verify": {"ok": False, "source": "feasibility"}}

    deps = StubDeps(infeasible)
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        job_id = client.post("/computer/run", json={"goal": "g", "app": "nope"}, headers=_h()).json()["job_id"]
        body = _wait_done(client, job_id)
    assert body["ok"] is False and body["infeasible"] is True and body["error"] == "no such app"
    rows = [json.loads(l) for l in (settings.data_dir_path / "engine-actions.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r["kind"] == "result" and r["detail"].get("job_id") == job_id)
    assert row["detail"]["infeasible"] is True


def test_cu_job_result_defaults_infeasible_false(client):
    job_id = client.post("/computer/run", json={"goal": "g"}, headers=_h()).json()["job_id"]
    queued = client.get(f"/jobs/{job_id}", headers=_h()).json()
    assert queued["infeasible"] is False
    body = _wait_done(client, job_id)
    assert body["infeasible"] is False and body["ok"] is True


class StubInterfaceStore:
    """Mirrors ``InterfaceStore``'s read API; fitness is only ever served through ``read()``."""

    def __init__(self):
        self.read_calls = []
        self.apps = {
            "libreoffice-calc": {
                "latest": 2,
                "functions": [{"name": "set_cell", "description": "Put a value in a cell", "signature": "set_cell(backend, ref, value)",
                               "body": "SECRET", "passed_on": ["p1"]}],
                "fitness": {"version": 2, "replayed": 3, "passed": 2, "at": "2026-11-05T00:00:00+00:00"},
            },
            "gedit": {"latest": 1, "functions": [{"name": "open_file", "description": "", "signature": "open_file(backend, path)"}], "fitness": None},
        }

    def list_apps(self):
        return sorted(self.apps)

    def latest(self, app):
        return self.apps[app]["latest"] if app in self.apps else None

    def read(self, app, version=None):
        self.read_calls.append((app, version))
        if app not in self.apps:
            return None
        row = self.apps[app]
        return {"app": app, "version": row["latest"], "functions": row["functions"], "fitness": row["fitness"]}


def test_computer_interfaces_route(settings):
    deps = StubDeps(_ok_run_cu_job)
    deps.cu_interfaces = StubInterfaceStore()
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        assert client.get("/computer/interfaces").status_code == 401
        r = client.get("/computer/interfaces", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"apps"}
    rows = {row["app"]: row for row in body["apps"]}
    assert set(rows) == {"libreoffice-calc", "gedit"}
    calc = rows["libreoffice-calc"]
    assert calc["latest"] == 2
    assert calc["functions"] == [{"name": "set_cell", "description": "Put a value in a cell", "signature": "set_cell(backend, ref, value)"}]
    assert calc["fitness"] == {"version": 2, "replayed": 3, "passed": 2, "at": "2026-11-05T00:00:00+00:00"}
    assert rows["gedit"]["fitness"] is None and rows["gedit"]["functions"][0]["name"] == "open_file"
    assert all("body" not in fn for row in body["apps"] for fn in row["functions"])
    assert deps.cu_interfaces.read_calls == [("gedit", None), ("libreoffice-calc", None)]


def test_computer_interfaces_route_with_real_store(settings, tmp_path):
    from o_engine.cu.interfaces import InterfaceStore

    store = InterfaceStore(tmp_path / "ifaces")
    store.write("gedit", [{"name": "open_file", "description": "Open a file", "signature": "open_file(backend, path)",
                           "body": "return backend.find(path)", "passed_on": ["gedit/x/v1"]}])
    store.write_fitness("gedit", 2, 1)
    deps = StubDeps(_ok_run_cu_job)
    deps.cu_interfaces = store
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        body = client.get("/computer/interfaces", headers=_h()).json()
    assert len(body["apps"]) == 1
    row = body["apps"][0]
    assert row["app"] == "gedit" and row["latest"] == 1
    assert row["functions"][0]["name"] == "open_file" and "body" not in row["functions"][0]
    assert row["fitness"]["replayed"] == 2 and row["fitness"]["passed"] == 1


def test_computer_interfaces_route_empty_store(settings, tmp_path):
    from o_engine.cu.interfaces import InterfaceStore

    deps = StubDeps(_ok_run_cu_job)
    deps.cu_interfaces = InterfaceStore(tmp_path / "ifaces")
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as client:
        assert client.get("/computer/interfaces", headers=_h()).json() == {"apps": []}
