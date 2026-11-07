"""``/evolve/*`` routes (plan L): status, signals, proposals, the Evolution feed, the ledger, and the person's
approve / reject / rollback decisions. Every route sits behind the same ``x-o-token`` guard as the rest of the
engine. The app drives the continuous loop with ``POST /evolve/tick`` (presence-aware); ``POST /evolve/cycle``
runs one full synchronous cycle as a job.
"""
from __future__ import annotations

import importlib
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .learn import Telemetry
from .pipeline import Evolver, GitHub, PipelineDeps, _call, evolve_settings, git_push, run_cycle


class TickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presence: str = Field(default="active", pattern="^(active|away|unknown)$")
    force_scan: bool = False
    wait: bool = False


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=1000)
    applied: bool = False  # rollback: the app's updater has pinned the previous release
    ref: str = Field(default="", max_length=200)
    release: str = Field(default="", max_length=200)  # shipped: the installed release / commit
    previous: str = Field(default="", max_length=200)  # shipped: what was installed before


def _parse(model: Any, body: Any) -> Any:
    try:
        return model.model_validate(body or {})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _first(module: Any, names: tuple[str, ...]) -> Any:
    for n in names:
        v = getattr(module, n, None)
        if v is not None:
            return v
    return None


def _try(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def build_deps(engine_settings: Any, evo: dict[str, Any]) -> PipelineDeps:
    """Wire the sibling stages (store, sources, triage, builder, gates, baseline, protected, skills) when they
    import; whatever is missing is listed in ``/evolve/status`` and that stage is skipped."""
    data_dir = Path(getattr(engine_settings, "data_dir", Path.home() / ".o-engine"))
    evolve_dir = data_dir / "evolve"
    evolve_dir.mkdir(parents=True, exist_ok=True)
    repo_root = str(Path(__file__).resolve().parents[3])
    missing: list[str] = []

    def mod(name: str) -> Any:
        try:
            return importlib.import_module(f"{__package__}.{name}")
        except Exception:  # noqa: BLE001
            missing.append(name)
            return None

    m_store, m_sources, m_triage, m_builder, m_gates = mod("store"), mod("sources"), mod("triage"), mod("builder"), mod("gates")
    m_baseline, m_protected, m_skills = mod("baseline"), mod("protected"), mod("skills")

    store = None
    if m_store is not None:
        cls = _first(m_store, ("EvolveStore", "Store"))
        store = _try(lambda: cls(evolve_dir / "evolve.db")) if cls else None
    scan = None
    if m_sources is not None:
        fn = _first(m_sources, ("scan_incremental", "scan_all"))
        if fn is not None:
            scan = lambda: list(_call(fn, store=store) or [])  # noqa: E731 - ETag/cursor caching lives in the store
    triage = None
    if m_triage is not None:
        fn = _first(m_triage, ("triage",))
        if fn is not None:
            triage = lambda signals, priors=None: _call(fn, signals, priors=priors, store=store)  # noqa: E731
    build = None
    if m_builder is not None and getattr(m_builder, "build", None) is not None:
        claude = str(getattr(engine_settings, "claude_path", "") or "claude")
        build = lambda p, root, wd, budget_usd=3.0: m_builder.build(p, root, wd, client_cmd=claude, budget_usd=budget_usd)  # noqa: E731
    run_gates = _first(m_gates, ("run_gates",)) if m_gates else None
    baseline = None
    if m_baseline is not None:
        fn = _first(m_baseline, ("ensure_baseline",))
        baseline = (lambda: fn(repo_root)) if fn else None
    protected = _first(m_protected, ("violations",)) if m_protected else None
    skills = None
    if m_skills is not None:
        cls = _first(m_skills, ("SkillRegistry", "Registry", "Skills"))
        skills = _try(lambda: cls(data_dir / "skills")) if cls else _first(m_skills, ("registry", "REGISTRY"))
    token = str(evo.get("evolveGithubToken") or "")
    repo = str(evo.get("evolveRepo") or "")
    learning_dir = getattr(engine_settings, "learning_path", data_dir / "learning")
    return PipelineDeps(
        store=store, scan=scan, triage=triage, build=build, run_gates=run_gates, baseline=baseline, protected=protected,
        github=GitHub(repo, token), push=lambda workdir, branch: git_push(workdir, branch, repo, token), skills=skills,
        telemetry=Telemetry(learning_dir, data_dir, evo.get("evolveAppLog") or None),
        repo_root=repo_root, evolve_dir=str(evolve_dir), missing=missing,
    )


def evolve_router(engine_settings: Any, guard: list[Any], log: Any = None,
                  evolver_factory: Callable[[], Evolver] | None = None) -> APIRouter:
    router = APIRouter(prefix="/evolve", dependencies=guard)
    state: dict[str, Any] = {"ev": None}
    lock = threading.Lock()
    jobs: dict[str, dict[str, Any]] = {}

    def ev() -> Evolver:
        with lock:
            if state["ev"] is None:
                if evolver_factory is not None:
                    state["ev"] = evolver_factory()
                else:
                    evo = evolve_settings()
                    state["ev"] = Evolver(evolve_settings, build_deps(engine_settings, evo))
            return state["ev"]

    def note(kind: str, summary: str, **detail: Any) -> None:
        if log is not None:
            try:
                log.record(kind, summary, **detail)
            except Exception:  # noqa: BLE001
                pass

    def decided(r: dict[str, Any]) -> dict[str, Any]:
        if not r.get("ok"):
            raise HTTPException(status_code=409, detail=r.get("error") or "not allowed")
        return r

    @router.get("/status")
    def status() -> dict[str, Any]:
        return ev().status()

    @router.get("/signals")
    def signals(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
        return {"signals": [s.model_dump() for s in ev().store.signals(limit)]}

    @router.get("/proposals")
    def proposals() -> dict[str, Any]:
        return {"proposals": ev().proposals_view()}

    @router.get("/feed")
    def feed(hours: float = Query(default=24.0, gt=0, le=24 * 30)) -> dict[str, Any]:
        e = ev()
        return {"feed": e.feed(hours), "skills": e.skill_candidates(), "status": e.status()}

    @router.get("/ledger")
    def ledger(limit: int = Query(default=500, ge=1, le=5000)) -> dict[str, Any]:
        e = ev()
        return {"journal": e.journal.read()[-limit:], "decisions": e.store.ledger(limit)}

    @router.get("/priors")
    def priors_route() -> dict[str, Any]:
        return ev().priors()

    @router.post("/scan")
    def scan() -> dict[str, Any]:
        r = ev().scan_and_triage()
        note("note", f"evolve: scanned, {r.get('new_signals', 0)} new signals, {r.get('new_proposals', 0)} proposals")
        return r

    @router.post("/tick")
    def tick(body: dict[str, Any] | None = None) -> dict[str, Any]:
        req = _parse(TickRequest, body)
        e = ev()
        presence = "away" if req.presence == "away" else "active"
        if req.wait:
            return e.tick(presence, req.force_scan)
        threading.Thread(target=e.tick, args=(presence, req.force_scan), daemon=True, name="o-evolve-tick").start()
        return {"ok": True, "queued": True}

    @router.post("/evaluate")
    def evaluate() -> dict[str, Any]:
        out = ev().evaluate()
        for v in out:
            if v["verdict"] == "rolled_back":
                note("policy", f"evolve: rolled back {v['proposal_id']}: {'; '.join(v['reasons'])}", proposal_id=v["proposal_id"])
        return {"verdicts": out}

    @router.post("/cycle")
    def cycle() -> dict[str, Any]:
        jid = uuid.uuid4().hex
        jobs[jid] = {"id": jid, "done": False, "started": time.time()}

        def _run() -> None:
            try:
                e = ev()
                jobs[jid].update(run_cycle(e.settings(), e.deps, evolver=e), done=True)
            except Exception as exc:  # noqa: BLE001
                jobs[jid].update(done=True, ok=False, error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_run, daemon=True, name="o-evolve-cycle").start()
        note("note", "evolve: cycle started", job_id=jid)
        return {"job_id": jid}

    @router.get("/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="unknown evolve job")
        return jobs[job_id]

    @router.post("/proposals/{pid}/approve")
    def approve(pid: str) -> dict[str, Any]:
        r = decided(ev().approve(pid))
        note("policy", f"evolve: you approved {pid} -> {r['status']}", proposal_id=pid)
        return r

    @router.post("/proposals/{pid}/reject")
    def reject(pid: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        req = _parse(DecisionRequest, body)
        r = decided(ev().reject(pid, req.reason))
        note("policy", f"evolve: you rejected {pid}", proposal_id=pid)
        return r

    @router.post("/proposals/{pid}/rollback")
    def rollback(pid: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        req = _parse(DecisionRequest, body)
        r = decided(ev().rollback(pid, req.reason, by="user", applied=req.applied, ref=req.ref))
        note("policy", f"evolve: rollback {'applied' if req.applied else 'requested'} for {pid}", proposal_id=pid)
        return r

    @router.post("/proposals/{pid}/shipped")
    def shipped(pid: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        req = _parse(DecisionRequest, body)
        r = decided(ev().shipped(pid, req.release, req.previous))
        note("note", f"evolve: {pid} installed", proposal_id=pid, release=req.release)
        return r

    @router.get("/skills")
    def skills() -> dict[str, Any]:
        return {"candidates": ev().skill_candidates()}

    @router.post("/skills/{sid}/approve")
    def skill_approve(sid: str) -> dict[str, Any]:
        r = decided(ev().decide_skill(sid, True))
        note("policy", f"evolve: you approved skill {sid}", skill_id=sid)
        return r

    @router.post("/skills/{sid}/reject")
    def skill_reject(sid: str) -> dict[str, Any]:
        r = decided(ev().decide_skill(sid, False))
        note("policy", f"evolve: you declined skill {sid}", skill_id=sid)
        return r

    router.state = state  # type: ignore[attr-defined]  # tests reach the Evolver through this
    return router
