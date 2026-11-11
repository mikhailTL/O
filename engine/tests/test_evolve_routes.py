import time

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from .evolve_fakes import Fakes, make, prop, sig
from o_engine.evolve.routes import evolve_router
from o_engine.server import EngineSettings, create_app

H = {"x-o-token": "t"}


class Log:
    def __init__(self):
        self.rows = []

    def record(self, kind, summary, **detail):
        self.rows.append((kind, summary))


@pytest.fixture
def env(tmp_path):
    def guard(request: Request) -> None:
        if request.headers.get("x-o-token") != "t":
            raise HTTPException(status_code=401)

    class Reg:
        approved: list = []

        def candidates(self):
            return [{"id": "mcp-x", "name": "x MCP", "status": "candidate"}]

        def approve(self, sid):
            self.approved.append(sid)
            return {"id": sid}

    f = Fakes(signals=[sig(1), sig(2)], proposals=[prop(1, 0.9, risk="medium"), prop(2, 0.5, risk="low")])
    reg = Reg()
    ev = make(tmp_path, f, skills=reg)
    log = Log()
    app = FastAPI()
    app.include_router(evolve_router(None, [Depends(guard)], log, evolver_factory=lambda: ev))
    return TestClient(app), ev, f, reg, log


def test_token_guard(env):
    c, *_ = env
    for path in ("/evolve/status", "/evolve/signals", "/evolve/proposals", "/evolve/feed", "/evolve/ledger", "/evolve/skills"):
        assert c.get(path).status_code == 401
    assert c.post("/evolve/scan").status_code == 401 and c.post("/evolve/proposals/p1/approve").status_code == 401


def test_scan_tick_and_views(env):
    c, ev, f, _, log = env
    r = c.post("/evolve/scan", headers=H).json()
    assert r["new_signals"] == 2 and r["new_proposals"] == 2
    assert len(c.get("/evolve/signals", headers=H).json()["signals"]) == 2
    r = c.post("/evolve/tick", json={"presence": "away", "wait": True}, headers=H).json()
    assert sorted(r["started"]) == ["p1", "p2"]
    ev.wait()
    props = {p["id"]: p for p in c.get("/evolve/proposals", headers=H).json()["proposals"]}
    assert props["p1"]["status"] == "pr_open" and props["p1"]["pr_url"].endswith("/pull/" + str(ev.journal.last("p1", ("pr_opened",))["pr_number"]))
    assert props["p1"]["checks"][1]["metric_after"] == 0.74 and props["p1"]["signals"][0]["url"].startswith("https://")
    assert props["p2"]["status"] == "pr_open"  # Ask before installing is on: even low risk waits for Approve
    st = c.get("/evolve/status", headers=H).json()
    assert st["ask_before_install"] is True and st["auto_ship"] == "never"
    assert st["counts"]["pr_open"] == 2 and st["waiting_for_you"] == 3 and st["awaiting_install"] == []
    feed = c.get("/evolve/feed?hours=6", headers=H).json()
    assert feed["feed"] and feed["skills"][0]["id"] == "mcp-x"
    led = c.get("/evolve/ledger", headers=H).json()
    assert led["journal"][0]["event"] == "proposed"
    assert c.post("/evolve/tick", json={"presence": "nap"}, headers=H).status_code == 422
    assert any("scanned" in s for _, s in log.rows)


def test_decisions_and_rollback_flow(env):
    c, ev, f, reg, log = env
    c.post("/evolve/scan", headers=H)
    c.post("/evolve/tick", json={"presence": "away", "wait": True}, headers=H)
    ev.wait()
    assert c.post("/evolve/proposals/p1/approve", headers=H).json()["status"] == "approved"
    assert c.post("/evolve/proposals/p1/approve", headers=H).status_code == 409
    assert c.post("/evolve/proposals/nope/reject", headers=H).status_code == 409
    r = c.post("/evolve/proposals/p2/shipped", json={"release": "evolve-p2", "previous": "sha1"}, headers=H).json()
    assert r["status"] == "shipped"
    r = c.post("/evolve/proposals/p2/rollback", json={"reason": "slower"}, headers=H).json()
    assert r["status"] == "rolled_back" and r["ref"] == "sha1"
    assert c.get("/evolve/status", headers=H).json()["rollback_requests"] == [{"id": "p2", "ref": "sha1", "reason": "slower"}]
    c.post("/evolve/proposals/p2/rollback", json={"applied": True, "ref": "sha1"}, headers=H)
    assert c.get("/evolve/status", headers=H).json()["rollback_requests"] == []
    assert c.post("/evolve/skills/mcp-x/approve", headers=H).json()["ok"] and reg.approved == ["mcp-x"]
    assert any(k == "policy" and "approved p1" in s for k, s in log.rows)
    assert c.post("/evolve/evaluate", headers=H).json() == {"verdicts": []}


def test_cycle_job(env):
    c, ev, f, *_ = env
    jid = c.post("/evolve/cycle", headers=H).json()["job_id"]
    for _ in range(100):
        j = c.get(f"/evolve/jobs/{jid}", headers=H).json()
        if j["done"]:
            break
        time.sleep(0.05)
    assert j["done"] and j["ok"] and sorted(j["built"]) == ["p1", "p2"]
    assert c.get("/evolve/jobs/zzz", headers=H).status_code == 404


def test_mounted_in_engine_app(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("O_EVOLVE_GITHUB_TOKEN", raising=False)
    d = tmp_path / "data"
    app = create_app(EngineSettings(token="t", data_dir=str(d), sandbox_root=str(tmp_path / "sb")))
    c = TestClient(app)
    assert c.get("/evolve/status").status_code == 401
    st = c.get("/evolve/status", headers=H).json()
    assert st["enabled"] is True and st["github"] is False and st["budget"]["cap_usd"] == 10.0
    assert isinstance(st["missing"], list)
    assert (d / "evolve").is_dir()


def test_practice_and_skills_routes(tmp_path):
    from o_engine.evolve.pipeline import PracticeHost  # noqa: F401
    from o_engine.evolve.skills import SkillRegistry

    def guard(request: Request) -> None:
        if request.headers.get("x-o-token") != "t":
            raise HTTPException(status_code=401)

    class Presence:
        state = "USER_PRESENT"
        listeners: list = []

    class Amb:
        presence = Presence()

    class Cur:
        def refresh(self): pass
        def learning_curve(self): return {"calc.add": {"attempts": [{}, {}], "improved": True, "first": {"seconds": 20, "cost_usd": 0.05}, "last": {"seconds": 4, "cost_usd": 0}}}
        def queue(self, limit=None): return []
        def gap_report(self): return {}

    class Pr:
        budget_usd = 0.5
        class ledger:
            @staticmethod
            def spent(): return 0.0
        def remaining_usd(self): return 0.5
        def run(self, **kw): return {"attempts": [], "stopped": "presence is USER_PRESENT", "curves": Cur().learning_curve()}

    class Env:
        is_isolated = False
        def start(self): pass
        def stop(self): pass

    reg = SkillRegistry(tmp_path / "skills")
    ev = make(tmp_path, Fakes(), skills=reg)
    amb = Amb()
    app = FastAPI()
    app.include_router(evolve_router(None, [Depends(guard)], None, evolver_factory=lambda: ev, ambient=amb,
                                     practice_factory=lambda: (Cur(), Pr(), Env())))
    c = TestClient(app)
    assert len(amb.presence.listeners) == 1
    assert c.get("/evolve/practice").status_code == 401
    v = c.get("/evolve/practice", headers=H).json()
    assert v["available"] and v["spend"]["budget_usd"] == 0.5 and "calc.add" in v["curves"] and v["running"] is False
    amb.presence.listeners[0]({"state": "USER_ABSENT"})
    for _ in range(50):
        if not c.get("/evolve/practice", headers=H).json()["running"]:
            break
        time.sleep(0.02)
    assert any(x["event"] == "practiced" for x in c.get("/evolve/feed", headers=H).json()["feed"])
    sk = c.get("/evolve/skills", headers=H).json()
    assert sk["pending_approval"] == 0 and sk["candidates"] == [] and "internal" in sk
    assert c.post("/evolve/skills/nope/approve", headers=H).status_code == 409


def test_build_deps_wires_token_push_check_git_check_and_triage_result(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from o_engine.evolve import routes as R
    from o_engine.evolve import sources as S
    from o_engine.evolve import triage as T

    calls = {}
    monkeypatch.setattr(S, "scan", lambda store, **kw: calls.setdefault("scan", kw) and SimpleNamespace(new=[], per_source={}))
    monkeypatch.setattr(R, "git_push_access", lambda root, repo, token, gh: calls.setdefault("push", (repo, token)) and (False, "no write access"))
    settings = SimpleNamespace(data_dir=tmp_path, claude_path="/nonexistent/claude")
    deps = R.build_deps(settings, {"evolveGithubToken": "tok", "evolveRepo": "someone/O"})
    assert deps.scan is not None and deps.can_push is not None and deps.git_reason is not None
    deps.scan()
    assert calls["scan"] == {"incremental": True, "github_token": "tok"}  # the evolve token reaches the scanner
    assert deps.can_push() == (False, "no write access") and calls["push"] == ("someone/O", "tok")
    assert deps.git_reason() == ""  # this checkout has git
    assert deps.triage is not None and deps.triage.last_result is None
    deps.triage.client = T.TriageClient("/nonexistent/claude-for-test")
    from o_engine.evolve.schema import Signal

    relevant = Signal(id="g1", kind="new_repo", source="github", title="org/gui-agent: GUI grounding with zoom", url="https://github.com/org/r1",
                      repo="org/r1", stars=500, stars_7d=200, license="MIT", topics=["gui-agent", "computer-use"])
    with pytest.raises(T.TriageError, match="not found"):
        deps.triage([relevant], budget_usd=0.2)
    assert deps.triage.last_result.failed == ["g1"] and deps.triage.last_result.errors
