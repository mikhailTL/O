"""Ship half of self-evolution (plan L.4-L.6): build, gate, open PRs, ship, and keep evolving continuously.

``Evolver`` is the continuous loop. The app calls ``tick(presence)`` every few minutes (``POST /evolve/tick``);
each tick is cheap and non-blocking:

1. reap finished builds; evaluate shipped releases (``learn.evaluate_shipped``, rolls back on regression);
2. adopt runtime skills O distilled from its own practice (``skills`` registry; third-party code never here);
3. scan sources when the scan interval has passed (ETag caching lives in ``sources``), triage new signals;
4. start builds while build slots and the rolling budget allow: more slots while the person is away, fewer
   while they use the computer. Each build runs in its own worktree on a worker thread: build -> gates ->
   push -> PR (GitHub REST, ``evolveGithubToken`` / ``GITHUB_TOKEN``; without a token a compare URL) -> ledger.

Code-build tier: builds only start while ``deps.sandbox()`` reports a sandbox (``evolveSandbox`` auto|docker|e2b,
see ``sandbox.py``) to run the candidate's tests in. Without one the tier is paused, the feed shows one calm line
(``sandbox.PAUSED_MESSAGE``) and everything else (scanning, triage, practice, skills) keeps running.

Adoption tiers: ``evolveAutoShip`` "low_risk" (default) merges and tags a release only when every gate passed,
risk is low, there are no new dependencies and no protected path changed; "deps_only" only for
dependency-only proposals; "never" leaves every PR for one-tap approval. Everything else waits for the person.

``run_cycle(settings, deps)`` is the same machinery as one synchronous pass (scan -> triage -> top-K -> build in
parallel -> gates -> ship). Sibling stages (store, sources, triage, builder, gates, baseline, skills) are
injected through ``PipelineDeps``; tests use fakes only.
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import sandbox as sbx
from .learn import Journal, Telemetry, evaluate_shipped, prior_multiplier, priors
from .schema import GateReport, Proposal, Signal

AUTO_SHIP_MODES = ("never", "deps_only", "low_risk")
BUILDABLE = ("accepted", "proposed")
MIN_BUILD_USD = 0.25
TRIAGE_BATCH = 40  # signals per triage call
TRIAGE_MAX_USD = 0.5  # cap per triage call (counts toward the rolling budget)
TRIAGE_MIN_USD = 0.05
WEEK_S = 7 * 86400
DAY_S = 86400
DEFAULTS: dict[str, Any] = {
    "evolveEnabled": True,
    "evolveBudgetUsd": 10.0,  # rolling 7-day cap
    "evolvePerProposalUsd": 1.5,  # cap per build
    "evolveAutoShip": "never",  # gates cannot yet measure real-world latency/cost; code waits for Approve
    "evolveRepo": "mikhailTL/O",
    "evolveBaseBranch": "main",
    "evolveGithubToken": "",
    "evolveScanMinutes": 45,
    "evolveSlotsActive": 1,
    "evolveSlotsAway": 3,
    "evolveTopK": 2,
    "evolveAppLog": "",
    "evolveSandbox": "auto",  # auto | docker | e2b (unsafe-host-dev: dev only, needs O_EVOLVE_DEV_UNSAFE=1)
}
_ENV = {
    "evolveEnabled": "O_EVOLVE_ENABLED", "evolveBudgetUsd": "O_EVOLVE_BUDGET_USD",
    "evolvePerProposalUsd": "O_EVOLVE_PER_PROPOSAL_USD", "evolveAutoShip": "O_EVOLVE_AUTO_SHIP",
    "evolveRepo": "O_EVOLVE_REPO", "evolveBaseBranch": "O_EVOLVE_BASE", "evolveGithubToken": "O_EVOLVE_GITHUB_TOKEN",
    "evolveScanMinutes": "O_EVOLVE_SCAN_MINUTES", "evolveSlotsActive": "O_EVOLVE_SLOTS_ACTIVE",
    "evolveSlotsAway": "O_EVOLVE_SLOTS_AWAY", "evolveTopK": "O_EVOLVE_TOP_K", "evolveAppLog": "O_EVOLVE_APP_LOG",
    "evolveSandbox": "O_EVOLVE_SANDBOX",
}


# --------------------------------------------------------------------------------- settings


def _snake(key: str) -> str:
    return "".join("_" + c.lower() if c.isupper() else c for c in key)


def setting(settings: Any, key: str, default: Any = None) -> Any:
    """``key`` (camelCase) from a dict or object, also trying snake_case; ``default`` when unset."""
    if default is None:
        default = DEFAULTS.get(key)
    for k in (key, _snake(key)):
        v = settings.get(k) if isinstance(settings, dict) else getattr(settings, k, None)
        if v not in (None, ""):
            return v
    return default


def evolve_settings(overrides: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Defaults <- ``O_EVOLVE_*`` env (``GITHUB_TOKEN`` for the token) <- ``overrides``; values normalized."""
    env = os.environ if env is None else env
    out = dict(DEFAULTS)
    for key, name in _ENV.items():
        if env.get(name, "") != "":
            out[key] = env[name]
    if not out["evolveGithubToken"] and env.get("GITHUB_TOKEN"):
        out["evolveGithubToken"] = env["GITHUB_TOKEN"]
    for k, v in (overrides or {}).items():
        if k in DEFAULTS and v not in (None, ""):
            out[k] = v
    out["evolveEnabled"] = str(out["evolveEnabled"]).strip().lower() not in ("0", "false", "no", "off")
    for k in ("evolveBudgetUsd", "evolvePerProposalUsd"):
        out[k] = max(0.0, float(out[k]))
    for k, lo, hi in (("evolveScanMinutes", 5, 1440), ("evolveSlotsActive", 0, 8), ("evolveSlotsAway", 0, 8), ("evolveTopK", 1, 8)):
        out[k] = max(lo, min(hi, int(float(out[k]))))
    if out["evolveAutoShip"] not in AUTO_SHIP_MODES:
        out["evolveAutoShip"] = DEFAULTS["evolveAutoShip"]
    out["evolveSandbox"] = sbx.normalize_mode(out["evolveSandbox"])
    return out


# --------------------------------------------------------------------------------- github


class GitHub:
    """The four REST calls the pipeline needs. ``token`` empty -> nothing is sent; callers use compare URLs."""

    def __init__(self, repo: str, token: str = "", api: str = "https://api.github.com", client: Any = None) -> None:
        self.repo, self.token, self.api = repo, token or "", api.rstrip("/")
        self._client = client

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=30.0)
        return self._client

    def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"accept": "application/vnd.github+json", "authorization": f"Bearer {self.token}",
                   "x-github-api-version": "2022-11-28", "user-agent": "O-evolve"}
        r = self._http().request(method, f"{self.api}/repos/{self.repo}{path}", json=body, headers=headers)
        if r.status_code >= 300:
            raise RuntimeError(f"GitHub {method} {path} answered {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def compare_url(self, base: str, branch: str) -> str:
        return f"https://github.com/{self.repo}/compare/{base}...{branch}"

    def open_pr(self, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        j = self._req("POST", "/pulls", {"title": title, "head": head, "base": base, "body": body, "maintainer_can_modify": True})
        return {"number": j.get("number"), "url": j.get("html_url", ""), "base_sha": (j.get("base") or {}).get("sha", "")}

    def merge_pr(self, number: int, title: str) -> dict[str, Any]:
        j = self._req("PUT", f"/pulls/{number}/merge", {"merge_method": "squash", "commit_title": title})
        return {"sha": j.get("sha", ""), "merged": bool(j.get("merged", True))}

    def close_pr(self, number: int) -> None:
        self._req("PATCH", f"/pulls/{number}", {"state": "closed"})

    def create_release(self, tag: str, sha: str, name: str, body: str) -> dict[str, Any]:
        j = self._req("POST", "/releases", {"tag_name": tag, "target_commitish": sha, "name": name, "body": body})
        return {"tag": j.get("tag_name", tag), "url": j.get("html_url", "")}


def git_push(workdir: str | os.PathLike[str], branch: str, repo: str = "", token: str = "") -> None:
    """Push ``branch`` from the build worktree. With a token the remote URL carries it (never logged)."""
    remote = f"https://x-access-token:{token}@github.com/{repo}.git" if token and repo else "origin"
    r = subprocess.run(["git", "-C", str(workdir), "push", remote, f"{branch}:{branch}"], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").replace(token, "***") if token else (r.stderr or r.stdout or "")
        raise RuntimeError(f"git push failed: {msg.strip()[:300]}")


# --------------------------------------------------------------------------------- store adapter


def _call(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    """Call ``fn`` with the keyword arguments its signature accepts (all of them when it takes ``**kw``)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kw)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kw)
    return fn(*args, **{k: v for k, v in kw.items() if k in params})


def _method(obj: Any, names: Iterable[str]) -> Callable[..., Any] | None:
    for n in names:
        m = getattr(obj, n, None) if obj is not None else None
        if callable(m):
            return m
    return None


class LocalStore:
    """JSON snapshot of signals/proposals (``<evolve_dir>/state.json``): the fallback for whatever the injected
    store does not provide. The record of what happened is the Journal; this file is only current state."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        try:
            self._d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._d = {"signals": {}, "proposals": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._d), encoding="utf-8")
        os.replace(tmp, self.path)

    def add_signals(self, signals: Iterable[Signal]) -> list[Signal]:
        with self._lock:
            new = [s for s in signals if s.id not in self._d["signals"]]
            for s in new:
                self._d["signals"][s.id] = {**s.model_dump(), "_seen": time.time()}
            if len(self._d["signals"]) > 5000:
                keep = sorted(self._d["signals"].items(), key=lambda kv: kv[1].get("_seen", 0))[-5000:]
                self._d["signals"] = dict(keep)
            if new:
                self._save()
            return new

    def backlog_add(self, ids: list[str]) -> list[str]:
        """Signal ids not yet triaged, oldest first (bounded)."""
        with self._lock:
            cur = self._d.setdefault("backlog", [])
            cur.extend(i for i in ids if i not in cur)
            del cur[:-1000]
            if ids:
                self._save()
            return list(cur)

    def backlog_drop(self, ids: list[str]) -> None:
        with self._lock:
            drop = set(ids)
            self._d["backlog"] = [i for i in self._d.get("backlog", []) if i not in drop]
            self._save()

    def signals(self, limit: int = 200) -> list[Signal]:
        rows = sorted(self._d["signals"].values(), key=lambda r: r.get("_seen", 0), reverse=True)[:limit]
        return [Signal.model_validate({k: v for k, v in r.items() if k != "_seen"}) for r in rows]

    def get_signal(self, sid: str) -> Signal | None:
        r = self._d["signals"].get(sid)
        return Signal.model_validate({k: v for k, v in r.items() if k != "_seen"}) if r else None

    def upsert_proposal(self, p: Proposal) -> None:
        with self._lock:
            self._d["proposals"][p.id] = p.model_dump()
            self._save()

    def get_proposal(self, pid: str) -> Proposal | None:
        r = self._d["proposals"].get(pid)
        return Proposal.model_validate(r) if r else None

    def proposals(self) -> list[Proposal]:
        return [Proposal.model_validate(r) for r in self._d["proposals"].values()]


class StoreAdapter:
    """Uses the injected evolve store where it has the method, ``LocalStore`` otherwise. Decisions always go to
    the store's append-only ledger when it has one, and to the Journal."""

    def __init__(self, store: Any, local: LocalStore) -> None:
        self.store, self.local = store, local

    def add_signals(self, signals: list[Signal]) -> list[Signal]:
        """Signals new to the store or to the local snapshot (a scanner may already have stored them)."""
        local_new = self.local.add_signals(signals)
        m = _method(self.store, ("add_signals", "upsert_signals", "insert_signals", "save_signals", "record_signals"))
        store_new = m(signals) if m is not None else []
        ids = {x.id for x in local_new}
        extra = [x for x in store_new if isinstance(x, Signal) and x.id not in ids] if isinstance(store_new, list) else []
        return local_new + extra

    def signals(self, limit: int = 200) -> list[Signal]:
        m = _method(self.store, ("signals", "list_signals", "recent_signals", "get_signals"))
        if m is not None:
            try:
                return list(_call(m, limit=limit))[:limit]
            except Exception:  # noqa: BLE001 - fall back to the local snapshot
                pass
        return self.local.signals(limit)

    def get_signal(self, sid: str) -> Signal | None:
        m = _method(self.store, ("get_signal", "signal"))
        if m is not None:
            try:
                s = m(sid)
                if s is not None:
                    return s
            except Exception:  # noqa: BLE001
                pass
        return self.local.get_signal(sid)

    def put(self, p: Proposal) -> None:
        self.local.upsert_proposal(p)
        m = _method(self.store, ("upsert_proposal", "save_proposal", "put_proposal", "add_proposal", "insert_proposal"))
        if m is not None:
            m(p)

    def get(self, pid: str) -> Proposal | None:
        m = _method(self.store, ("get_proposal", "proposal"))
        if m is not None:
            try:
                p = m(pid)
                if p is not None:
                    return p
            except Exception:  # noqa: BLE001
                pass
        return self.local.get_proposal(pid)

    def proposals(self) -> list[Proposal]:
        m = _method(self.store, ("proposals", "list_proposals", "get_proposals"))
        if m is not None:
            try:
                return list(m())
            except Exception:  # noqa: BLE001
                pass
        return self.local.proposals()

    def set_status(self, pid: str, status: str, reason: str = "", metrics: dict[str, Any] | None = None) -> Proposal | None:
        """Current status changes are ledger rows in the store (``record_decision``); the snapshot follows."""
        p = self.get(pid)
        if p is None:
            return None
        q = p.model_copy(update={"status": status})
        self.local.upsert_proposal(q)
        m = _method(self.store, ("record_decision", "append_decision", "add_decision", "set_status", "set_proposal_status"))
        if m is not None:
            try:
                _call(m, pid, status, reason=str(reason)[:1000], metrics=metrics or {})
            except Exception:  # noqa: BLE001 - the Journal still has it
                pass
        return q

    def ledger(self, limit: int = 500) -> list[dict[str, Any]] | None:
        m = _method(self.store, ("decisions", "ledger", "list_decisions"))
        if m is None:
            return None
        try:
            return [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in list(_call(m, limit=limit))][-limit:]
        except Exception:  # noqa: BLE001
            return None


# --------------------------------------------------------------------------------- deps


@dataclass
class PipelineDeps:
    store: Any = None
    scan: Callable[[], list[Signal]] | None = None  # sources: incremental, ETag-cached
    triage: Callable[..., list[Proposal]] | None = None  # triage(signals, priors=...) -> proposals
    build: Callable[..., tuple[str, str, float, str]] | None = None  # build(p, repo_root, workdir, budget_usd=...)
    run_gates: Callable[..., GateReport] | None = None  # run_gates(p, workdir, baseline)
    baseline: Callable[[], Any] | None = None
    protected: Callable[[list[str]], list[str]] | None = None  # changed files -> protected violations
    github: GitHub | None = None
    push: Callable[[str, str], None] | None = None  # push(workdir, branch)
    skills: Any = None  # runtime skills registry (evolve/skills.py)
    sandbox: Callable[[], Any] | None = None  # -> sandbox.SandboxChoice; None or unavailable pauses code builds
    telemetry: Telemetry | None = None
    rollback: Callable[[str, str, str], Any] | None = None  # extra hook; the app's updater is driven via status
    repo_root: str = ""
    evolve_dir: str = ""
    now: Callable[[], float] = time.time
    missing: list[str] = field(default_factory=list)  # sibling stages that could not be loaded


# --------------------------------------------------------------------------------- policy


def protected_violations(report: GateReport, deps: PipelineDeps) -> list[str]:
    out = [c.name for c in report.checks if "protect" in c.name.lower() and not c.passed]
    if deps.protected is not None:
        try:
            out += [str(x) for x in deps.protected(list(report.diff_files)) or []]
        except Exception as exc:  # noqa: BLE001 - cannot check -> treat as a violation
            out.append(f"protected check failed: {type(exc).__name__}")
    return out


def auto_ship_allowed(p: Proposal, report: GateReport, mode: str, violations: list[str]) -> tuple[bool, str]:
    """Whether a passing upgrade may merge without the person. The reason is shown in the feed either way."""
    if not report.passed or not all(c.passed for c in report.checks):
        return False, "not every gate passed"
    if violations:
        return False, "touches protected paths"
    if not p.licenses_ok:
        return False, "license needs review"
    if mode == "never":
        return False, "auto-ship is off"
    if mode == "deps_only":
        return (True, "dependency-only, all gates passed") if p.dependency_only and p.risk != "high" else (False, "waits for approval (not dependency-only)")
    if p.risk == "low" and not p.new_dependencies:
        return True, "low risk, no new dependencies, all gates passed"
    return False, "waits for approval: " + ("adds dependencies" if p.new_dependencies else f"{p.risk} risk")


def _fmt(v: float | None) -> str:
    return "—" if v is None else (f"{v:.4g}")


def pr_body(p: Proposal, report: GateReport, signals: list[Signal], note: str = "") -> str:
    rows = "\n".join(
        f"| {c.name} | {'pass' if c.passed else 'FAIL'} | {_fmt(c.metric_before)} | {_fmt(c.metric_after)} | {c.detail.replace('|', '/')[:160]} |"
        for c in report.checks
    ) or "| (no checks reported) | | | | |"
    cited = "\n".join(f"- [{s.title}]({s.url}) ({s.source}{', ' + s.version if s.version else ''})" for s in signals) or "- (signals unavailable)"
    deps = ", ".join(p.new_dependencies) or "none"
    return (
        f"## {p.title}\n\n"
        f"**Subsystem:** `{p.subsystem}` · **Risk:** {p.risk} · **Score:** {p.score:.2f} · **New dependencies:** {deps}\n\n"
        f"### Why\n{p.rationale}\n\n### Signals cited\n{cited}\n\n"
        f"### Gate report: {'all passed' if report.passed else 'FAILED'}\n\n"
        f"| Check | Result | Before | After | Detail |\n|---|---|---|---|---|\n{rows}\n\n"
        f"**Diff:** {len(report.diff_files)} files, {report.diff_lines} lines · **Spend:** ${report.spent_usd:.2f}"
        f"{' · **Commit:** `' + report.commit[:12] + '`' if report.commit else ''}\n\n"
        f"### Expected gain\n{p.expected_gain or '—'}\n\n### Acceptance test\n{p.acceptance_test}\n\n"
        + (f"> {note}\n\n" if note else "")
        + "_Opened by O's evolve pipeline. Protected paths (approvals, payment guard, action log, consent rules, "
        "evolve gates) are never modified by evolve._\n"
    )


# --------------------------------------------------------------------------------- evolver


class Evolver:
    """The continuous self-evolution loop. One instance per engine; thread-safe."""

    def __init__(self, settings: Callable[[], dict[str, Any]] | dict[str, Any], deps: PipelineDeps, max_workers: int = 8) -> None:
        self._settings = settings if callable(settings) else (lambda s=settings: s)
        self.deps = deps
        root = Path(deps.evolve_dir or Path.home() / ".o-engine" / "evolve")
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.journal = Journal(root / "journal.jsonl", now=deps.now)
        self.store = StoreAdapter(deps.store, LocalStore(root / "state.json"))
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="o-evolve")
        self._inflight: dict[str, Future | None] = {}  # proposal id -> build future
        self._reserved: dict[str, float] = {}  # proposal id -> budget held until its spend is recorded
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()

    # ------------------------------------------------------------ helpers

    def settings(self) -> dict[str, Any]:
        s = self._settings() or {}
        return evolve_settings({k: setting(s, k) for k in DEFAULTS}, env={})

    def now(self) -> float:
        return self.deps.now()

    def _record(self, event: str, pid: str = "", **detail: Any) -> dict[str, Any]:
        return self.journal.append(event, pid, **detail)

    def _status(self, pid: str, status: str, reason: str = "", metrics: dict[str, Any] | None = None) -> None:
        self.store.set_status(pid, status, reason, metrics)

    def budget(self) -> dict[str, Any]:
        s = self.settings()
        now = self.now()
        cap = float(s["evolveBudgetUsd"])
        with self._lock:
            spent_7d = self.journal.spent(now - WEEK_S)
            spent_24h = self.journal.spent(now - DAY_S)
            reserved = round(sum(self._reserved.values()), 6)
        # Spread across the week: at most two days' share in any 24 h (and room for two builds), never above the cap.
        pace = min(cap, max(2 * float(s["evolvePerProposalUsd"]), cap / 7 * 2))
        available = max(0.0, min(cap - spent_7d, pace - spent_24h) - reserved)
        return {"cap_usd": cap, "spent_7d": spent_7d, "spent_24h": spent_24h, "reserved_usd": reserved,
                "daily_pace_usd": round(pace, 4), "available_usd": round(available, 4),
                "per_proposal_usd": float(s["evolvePerProposalUsd"])}

    def priors(self) -> dict[str, Any]:
        return priors(self.journal)

    def code_tier(self) -> dict[str, Any]:
        """Whether upgrades that change code may be built now: only with a sandbox to test them in."""
        if self.deps.sandbox is None:
            return {"enabled": False, "backend": "none", "reason": "no sandbox wired", "message": sbx.PAUSED_MESSAGE}
        try:
            choice = self.deps.sandbox()
        except Exception as exc:  # noqa: BLE001 - a probe that crashes is no sandbox
            return {"enabled": False, "backend": "none", "reason": f"{type(exc).__name__}: {exc}"[:300],
                    "message": sbx.PAUSED_MESSAGE}
        view = choice.view() if hasattr(choice, "view") else dict(choice)
        if not view.get("enabled"):
            view["message"] = view.get("message") or sbx.PAUSED_MESSAGE
        return view

    def _sources_of(self, p: Proposal) -> list[str]:
        out: list[str] = []
        for sid in p.signal_ids:
            s = self.store.get_signal(sid)
            if s is not None and s.source not in out:
                out.append(s.source)
        return out

    def _signals_of(self, p: Proposal) -> list[Signal]:
        return [s for s in (self.store.get_signal(sid) for sid in p.signal_ids) if s is not None]

    def rank(self, p: Proposal, pri: dict[str, Any] | None = None) -> float:
        pri = self.priors() if pri is None else pri
        return round(p.score * prior_multiplier(pri, p.subsystem, p.risk, self._sources_of(p)), 6)

    # ------------------------------------------------------------ scan + triage

    def last_scan(self) -> float:
        rows = self.journal.read(events=("scan",))
        return float(rows[-1]["ts"]) if rows else 0.0

    def scan_and_triage(self) -> dict[str, Any]:
        if self.deps.scan is None:
            return {"ok": False, "error": "sources unavailable", "new_signals": 0, "new_proposals": 0}
        try:
            found = list(self.deps.scan() or [])
        except Exception as exc:  # noqa: BLE001
            self._record("scan", error=f"{type(exc).__name__}: {exc}", new=0)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "new_signals": 0, "new_proposals": 0}
        new = self.store.add_signals(found)
        self._ingest_skill_signals(new)
        known = {p.id for p in self.store.proposals()}
        proposals: list[Proposal] = []
        # Signals wait in a backlog until there is budget to triage them (triage is a paid model call).
        backlog = self.store.local.backlog_add([s.id for s in new])
        pending = [s for s in (self.store.get_signal(i) for i in backlog[:TRIAGE_BATCH]) if s is not None]
        cap = min(TRIAGE_MAX_USD, self.budget()["available_usd"])
        if pending and self.deps.triage is not None and cap >= TRIAGE_MIN_USD:
            try:
                proposals = list(_call(self.deps.triage, pending, priors=self.priors(), budget_usd=cap) or [])
                self.store.local.backlog_drop([s.id for s in pending])
            except Exception as exc:  # noqa: BLE001
                self._record("triage_failed", error=f"{type(exc).__name__}: {exc}")
            spent = float(getattr(self.deps.triage, "last_spent", 0.0) or 0.0)
            if spent:
                self._record("triaged", signals=len(pending), spent_usd=round(spent, 6))
        new = pending or new
        added = 0
        for p in proposals:
            if p.id in known:
                continue
            self.store.put(p)
            added += 1
            srcs = sorted({s.source for s in new if s.id in p.signal_ids}) or self._sources_of(p)
            self._record("proposed", p.id, title=p.title, subsystem=p.subsystem, risk=p.risk, score=p.score,
                         sources=srcs, signal_ids=list(p.signal_ids))
        self._record("scan", found=len(found), new=len(new), proposals=added)
        return {"ok": True, "found": len(found), "new_signals": len(new), "new_proposals": added}

    def _ingest_skill_signals(self, new: list[Signal]) -> int:
        """Signals tagged ``skill`` become approval-gated candidates in the skills registry (never installed here)."""
        ingest = getattr(self.deps.skills, "ingest_signals", None)
        if not callable(ingest):
            return 0
        sigs: list[Any] = list(new)
        m = _method(self.deps.store, ("signals",))
        if m is not None:
            try:
                sigs = list(m(topic="skill") or []) + sigs
            except Exception:  # noqa: BLE001 - a store without topic filtering: the new signals are enough
                pass
        try:
            return len(ingest(sigs) or [])
        except Exception as exc:  # noqa: BLE001
            self._record("skills_error", error=f"{type(exc).__name__}: {exc}"[:300])
            return 0

    # ------------------------------------------------------------ build -> gates -> ship

    def candidates(self) -> list[Proposal]:
        """Proposals waiting to be built: the person's accepted ones first, then by score x priors. A build moves
        the status off ``proposed``/``accepted``, so nothing is built twice unless the person re-accepts it."""
        with self._lock:
            busy = set(self._inflight)
        pri = self.priors()
        out = [p for p in self.store.proposals() if p.status in BUILDABLE and p.id not in busy and p.licenses_ok]
        return sorted(out, key=lambda p: (p.status != "accepted", -self.rank(p, pri)))

    def launch(self, p: Proposal) -> bool:
        """Reserve budget and start the build in the background. False when the budget cannot cover it."""
        s = self.settings()
        with self._lock:
            if p.id in self._inflight:
                return False
            cap = min(float(s["evolvePerProposalUsd"]), self.budget()["available_usd"])
            if cap < MIN_BUILD_USD or self.deps.build is None or self.deps.run_gates is None:
                return False
            if not self.code_tier()["enabled"]:
                return False
            self._record("build_started", p.id, reserved_usd=round(cap, 4), title=p.title)
            self._status(p.id, "building")
            self._reserved[p.id] = cap
            self._inflight[p.id] = None
            fut = self._pool.submit(self._work, p, cap)
            if p.id in self._inflight:
                self._inflight[p.id] = fut
            fut.add_done_callback(lambda _f, pid=p.id: self._done(pid))
        return True

    def _done(self, pid: str) -> None:
        with self._lock:
            self._inflight.pop(pid, None)
            self._reserved.pop(pid, None)

    def _work(self, p: Proposal, cap: float) -> dict[str, Any]:
        d = self.deps
        workdir = self.root / "work" / p.id
        workdir.parent.mkdir(parents=True, exist_ok=True)
        try:
            branch, commit, spent, _transcript = _call(d.build, p, d.repo_root, str(workdir), budget_usd=cap, max_usd=cap,
                                                       signals=self._signals_of(p))
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._record("build_failed", p.id, error=f"{type(exc).__name__}: {exc}"[:500], spent_usd=0.0)
                self._reserved.pop(p.id, None)
            self._status(p.id, "gated_fail")
            return {"id": p.id, "status": "gated_fail"}
        spent = round(float(spent or 0.0), 6)
        with self._lock:  # spend recorded and reservation released together, so budget() never counts both
            self._record("built", p.id, branch=branch, commit=commit, spent_usd=spent)
            self._reserved.pop(p.id, None)
        self._status(p.id, "built")
        try:
            base = d.baseline() if callable(d.baseline) else None
            report = _call(d.run_gates, p, str(workdir), base, spent_usd=spent)
        except Exception as exc:  # noqa: BLE001
            report = GateReport(proposal_id=p.id, branch=branch, commit=commit, passed=False,
                                checks=[], spent_usd=spent)
            self._record("gates_error", p.id, error=f"{type(exc).__name__}: {exc}"[:500])
        report = report.model_copy(update={"branch": report.branch or branch, "commit": report.commit or commit,
                                           "spent_usd": report.spent_usd or spent})
        return self.ship(p, report, str(workdir))

    def ship(self, p: Proposal, report: GateReport, workdir: str) -> dict[str, Any]:
        s = self.settings()
        violations = protected_violations(report, self.deps)
        passed = report.passed and not violations
        srcs = self._sources_of(p)
        self._record("gated", p.id, passed=passed, report=report.model_dump(), violations=violations,
                     subsystem=p.subsystem, risk=p.risk, sources=srcs)
        metrics = {c.name: {"passed": c.passed, "before": c.metric_before, "after": c.metric_after} for c in report.checks}
        if not passed:
            failed = [c.name for c in report.checks if not c.passed] + violations
            self._status(p.id, "gated_fail", "failed: " + ", ".join(failed[:8]), metrics)
            return {"id": p.id, "status": "gated_fail", "violations": violations}
        gh = self.deps.github
        base = s["evolveBaseBranch"]
        ok, why = auto_ship_allowed(p, report, s["evolveAutoShip"], violations)
        try:
            if self.deps.push is not None:
                self.deps.push(workdir, report.branch)
        except Exception as exc:  # noqa: BLE001
            self._record("push_failed", p.id, error=str(exc)[:400])
            self._status(p.id, "gated_pass")
            return {"id": p.id, "status": "gated_pass", "error": "push failed"}
        if gh is None or not gh.token:
            url = (gh or GitHub(s["evolveRepo"])).compare_url(base, report.branch)
            self._record("compare", p.id, compare_url=url, auto_ship=False, auto_ship_reason="no GitHub token: open the compare link to make the PR")
            self._status(p.id, "gated_pass")
            return {"id": p.id, "status": "gated_pass", "compare_url": url}
        try:
            pr = gh.open_pr(report.branch, base, f"evolve: {p.title}", pr_body(p, report, self._signals_of(p), note=why))
        except Exception as exc:  # noqa: BLE001
            self._record("pr_failed", p.id, error=str(exc)[:400])
            self._status(p.id, "gated_pass")
            return {"id": p.id, "status": "gated_pass", "error": "PR failed"}
        self._record("pr_opened", p.id, pr_number=pr["number"], pr_url=pr["url"], base_sha=pr.get("base_sha", ""),
                     auto_ship=ok, auto_ship_reason=why)
        self._status(p.id, "pr_open", why, {**metrics, "pr_url": pr["url"]})
        if ok:
            return self.merge(p.id, by="auto")
        return {"id": p.id, "status": "pr_open", "pr_url": pr["url"]}

    # ------------------------------------------------------------ approval / ship / rollback

    def _previous_release(self, pid: str) -> str:
        for r in reversed(self.journal.read(events=("approved",))):
            if r["proposal_id"] != pid and r.get("release"):
                return str(r["release"])
        pr = self.journal.last(pid, ("pr_opened",))
        return str((pr or {}).get("base_sha") or "")

    def merge(self, pid: str, by: str = "user") -> dict[str, Any]:
        """Merge the proposal's PR and tag a release (the updater installs it; learn can roll it back)."""
        p = self.store.get(pid)
        pr = self.journal.last(pid, ("pr_opened",))
        gh = self.deps.github
        if p is None or pr is None or gh is None or not gh.token:
            return {"ok": False, "error": "no open PR to merge"}
        try:
            m = gh.merge_pr(int(pr["pr_number"]), f"evolve: {p.title} (#{pr['pr_number']})")
        except Exception as exc:  # noqa: BLE001
            self._record("merge_failed", pid, error=str(exc)[:400], by=by)
            return {"ok": False, "error": str(exc)[:300]}
        previous = self._previous_release(pid)
        tag = f"evolve-{pid[:16]}"
        release = ""
        try:
            release = gh.create_release(tag, m["sha"], f"O upgrade: {p.title}", f"{p.rationale}\n\n{pr['pr_url']}")["tag"]
        except Exception as exc:  # noqa: BLE001 - merged anyway; the commit sha is still a rollback point
            self._record("release_failed", pid, error=str(exc)[:300])
        self._record("approved", pid, by=by, merge_sha=m["sha"], release=release or m["sha"], previous=previous,
                     pr_number=pr["pr_number"], pr_url=pr["pr_url"])
        self._status(pid, "approved", f"merged by {by}", {"release": release or m["sha"], "previous": previous})
        return {"ok": True, "id": pid, "status": "approved", "release": release or m["sha"], "merge_sha": m["sha"]}

    def approve(self, pid: str) -> dict[str, Any]:
        p = self.store.get(pid)
        if p is None:
            return {"ok": False, "error": "unknown proposal"}
        if p.status in ("proposed", "gated_fail"):
            self._record("accepted", pid, by="user")
            self._status(pid, "accepted")
            return {"ok": True, "id": pid, "status": "accepted"}
        if p.status == "pr_open":
            return self.merge(pid, by="user")
        if p.status == "gated_pass":
            cmp_ = self.journal.last(pid, ("compare",))
            self._record("approved", pid, by="user", release="", previous=self._previous_release(pid),
                         note="merge the branch on GitHub; the updater installs it", compare_url=(cmp_ or {}).get("compare_url", ""))
            self._status(pid, "approved")
            return {"ok": True, "id": pid, "status": "approved", "compare_url": (cmp_ or {}).get("compare_url", "")}
        return {"ok": False, "error": f"cannot approve a proposal that is {p.status}"}

    def reject(self, pid: str, reason: str = "") -> dict[str, Any]:
        p = self.store.get(pid)
        if p is None:
            return {"ok": False, "error": "unknown proposal"}
        if p.status in ("approved", "shipped", "rolled_back", "rejected"):
            return {"ok": False, "error": f"cannot reject a proposal that is {p.status}"}
        pr = self.journal.last(pid, ("pr_opened",))
        if pr and self.deps.github is not None and self.deps.github.token:
            try:
                self.deps.github.close_pr(int(pr["pr_number"]))
            except Exception:  # noqa: BLE001
                pass
        self._record("rejected", pid, by="user", reason=str(reason)[:500])
        self._status(pid, "rejected", reason or "rejected by you")
        return {"ok": True, "id": pid, "status": "rejected"}

    def shipped(self, pid: str, release: str = "", previous: str = "") -> dict[str, Any]:
        """The app's updater installed this proposal's release; the post-ship window starts now."""
        p = self.store.get(pid)
        if p is None:
            return {"ok": False, "error": "unknown proposal"}
        if self.journal.last(pid, ("shipped",)) and p.status == "shipped":
            return {"ok": True, "id": pid, "status": "shipped"}
        a = self.journal.last(pid, ("approved",)) or {}
        self._record("shipped", pid, release=release or a.get("release", ""), previous=previous or a.get("previous", ""))
        self._status(pid, "shipped")
        return {"ok": True, "id": pid, "status": "shipped"}

    def rollback(self, pid: str, reason: str = "", by: str = "user", applied: bool = False, ref: str = "") -> dict[str, Any]:
        p = self.store.get(pid)
        if p is None:
            return {"ok": False, "error": "unknown proposal"}
        if applied:
            self._record("rollback_applied", pid, ref=ref)
            return {"ok": True, "id": pid, "status": p.status}
        if p.status == "rolled_back":
            return {"ok": True, "id": pid, "status": "rolled_back"}
        if p.status not in ("shipped", "approved"):
            return {"ok": False, "error": f"cannot roll back a proposal that is {p.status}"}
        sh = self.journal.last(pid, ("shipped",)) or self.journal.last(pid, ("approved",)) or {}
        ref = str(sh.get("previous") or "")
        self._record("rolled_back", pid, reason=reason or "rolled back by you", ref=ref, by=by)
        self._status(pid, "rolled_back", reason or "rolled back by you", {"ref": ref, "by": by})
        return {"ok": True, "id": pid, "status": "rolled_back", "ref": ref}

    def rollback_requests(self) -> list[dict[str, Any]]:
        applied = {r["proposal_id"] for r in self.journal.read(events=("rollback_applied",))}
        return [{"id": r["proposal_id"], "ref": r.get("ref", ""), "reason": r.get("reason", "")}
                for r in self.journal.read(events=("rolled_back",)) if r["proposal_id"] not in applied]

    def awaiting_install(self) -> list[dict[str, Any]]:
        out = []
        for p in self.store.proposals():
            if p.status != "approved":
                continue
            a = self.journal.last(p.id, ("approved",)) or {}
            out.append({"id": p.id, "title": p.title, "pr_number": a.get("pr_number"), "merge_sha": a.get("merge_sha", ""),
                        "release": a.get("release", ""), "previous": a.get("previous", "")})
        return out

    def evaluate(self) -> list[dict[str, Any]]:
        if self.deps.telemetry is None:
            return []

        def _rb(pid: str, ref: str, reason: str) -> None:
            self._status(pid, "rolled_back", reason, {"ref": ref, "by": "learn"})
            if self.deps.rollback is not None:
                self.deps.rollback(pid, ref, reason)

        return evaluate_shipped(self.journal, self.deps.telemetry, _rb, now=self.now())

    # ------------------------------------------------------------ skills (tier 1)

    def skills_tick(self) -> list[dict[str, Any]]:
        """Skills distilled from O's own practice are adopted by the registry once practiced to threshold
        (``SkillRegistry.observe`` promotes them); this records each newly active one in the feed. Candidate
        skills that would install or run third-party code are never adopted here: they wait for ``decide_skill``."""
        reg = self.deps.skills
        adopted: list[Any] = []
        try:
            m = _method(reg, ("adopt_practiced", "adopt_ready", "promote_practiced"))
            if m is not None:
                adopted = list(m() or [])
            elif callable(getattr(reg, "active", None)):
                adopted = list(reg.active() or [])
        except Exception as exc:  # noqa: BLE001
            self._record("skills_error", error=f"{type(exc).__name__}: {exc}"[:300])
            return []
        seen = {(r.get("skill_id"), r.get("skill_version")) for r in self.journal.read(events=("skill_adopted",))}
        out = []
        for a in adopted:
            row = _as_dict(a)
            key = (str(row.get("id", "")), row.get("version"))
            if key in seen:
                continue
            name = str(row.get("name") or row.get("title") or "") or " ".join(x for x in (str(row.get("goal") or ""), f"in {row['app']}" if row.get("app") else "") if x)
            self._record("skill_adopted", skill_id=key[0], skill_version=key[1], name=name,
                         detail=str(row.get("description") or row.get("summary") or row.get("reason") or "")[:300])
            out.append(row)
        return out

    def skill_candidates(self) -> list[dict[str, Any]]:
        """Third-party skills (MCP servers, CLIs, libraries) waiting for the person's approval."""
        reg = self.deps.skills
        m = _method(reg, ("pending", "candidates", "pending_candidates"))
        rows: list[Any] = []
        try:
            if m is not None:
                rows = list(m() or [])
            else:
                lm = _method(reg, ("list", "skills", "all"))
                rows = [r for r in (lm() if lm else []) or [] if _as_dict(r).get("status") in ("candidate", "pending")]
        except Exception:  # noqa: BLE001
            return []
        return [_as_dict(r) for r in rows]

    def decide_skill(self, sid: str, approve: bool) -> dict[str, Any]:
        """Only reached from the person's Approve / Reject in the Evolution feed."""
        reg = self.deps.skills
        m = _method(reg, ("approve",) if approve else ("reject", "decline", "dismiss"))
        if m is None:
            return {"ok": False, "error": "skills registry unavailable"}
        try:
            r = _call(m, sid, by="user", via="app")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        self._record("skill_approved" if approve else "skill_rejected", skill_id=sid, by="user")
        return {"ok": True, "id": sid, "result": _as_dict(r) if r is not None else None}

    # ------------------------------------------------------------ the loop

    def tick(self, presence: str = "active", force_scan: bool = False) -> dict[str, Any]:
        s = self.settings()
        if not s["evolveEnabled"]:
            return {"ok": True, "enabled": False}
        if not self._tick_lock.acquire(blocking=False):
            return {"ok": True, "busy": True}
        try:
            out: dict[str, Any] = {"ok": True, "enabled": True, "presence": presence}
            out["evaluated"] = self.evaluate()
            out["skills_adopted"] = len(self.skills_tick())
            due = self.now() - self.last_scan() >= s["evolveScanMinutes"] * 60
            out["scan"] = self.scan_and_triage() if (force_scan or due) else None
            slots = s["evolveSlotsAway"] if presence == "away" else s["evolveSlotsActive"]
            with self._lock:
                free = max(0, slots - len(self._inflight))
            started = []
            tier = self.code_tier()
            out["code_builds"] = tier
            for p in (self.candidates() if tier["enabled"] else []):
                if len(started) >= free:
                    break
                if not self.launch(p):
                    break
                started.append(p.id)
            out["started"] = started
            out["in_flight"] = self.in_flight()
            return out
        finally:
            self._tick_lock.release()

    def in_flight(self) -> list[str]:
        with self._lock:
            return sorted(self._inflight)

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            futs = [f for f in self._inflight.values() if f is not None]
        for f in futs:
            try:
                f.result(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ views

    def proposal_view(self, p: Proposal) -> dict[str, Any]:
        j = {e: self.journal.last(p.id, (e,)) for e in ("gated", "pr_opened", "compare", "approved", "shipped", "rolled_back", "held", "built", "build_failed")}
        rep = (j["gated"] or {}).get("report") or {}
        spent = sum(float(r.get("spent_usd") or 0) for r in self.journal.read(proposal_id=p.id) if r.get("spent_usd"))
        return {
            **p.model_dump(),
            "signals": [{"id": s.id, "title": s.title, "url": s.url, "source": s.source} for s in self._signals_of(p)],
            "checks": rep.get("checks") or [],
            "gates_passed": (j["gated"] or {}).get("passed"),
            "violations": (j["gated"] or {}).get("violations") or [],
            "diff_files": len(rep.get("diff_files") or []), "diff_lines": rep.get("diff_lines", 0),
            "spent_usd": round(spent, 4),
            "pr_url": (j["pr_opened"] or {}).get("pr_url") or (j["approved"] or {}).get("pr_url") or "",
            "compare_url": (j["compare"] or {}).get("compare_url", ""),
            "auto_ship_reason": (j["pr_opened"] or j["compare"] or {}).get("auto_ship_reason", ""),
            "release": (j["shipped"] or j["approved"] or {}).get("release", ""),
            "rollback_reason": (j["rolled_back"] or {}).get("reason", ""),
            "post_ship": {k: (j[k] or {}).get("after") for k in ("held", "rolled_back") if j[k]},
            "error": (j["build_failed"] or {}).get("error", ""),
            "in_flight": p.id in self.in_flight(),
            "rank": self.rank(p),
            "updated": max([float(r["ts"]) for r in self.journal.read(proposal_id=p.id)] or [0.0]),
        }

    def proposals_view(self) -> list[dict[str, Any]]:
        return sorted((self.proposal_view(p) for p in self.store.proposals()), key=lambda v: -v["updated"])

    def feed(self, hours: float = 24.0) -> list[dict[str, Any]]:
        """What O learned, added, measured and undid recently, newest first, in plain words."""
        rows = self.journal.read(since=self.now() - hours * 3600)
        titles = {p.id: p.title for p in self.store.proposals()}
        out = []
        tier = self.code_tier()
        if not tier["enabled"] and self.settings()["evolveEnabled"]:
            out.append({"ts": self.now(), "event": "code_paused", "proposal_id": "", "text": tier["message"], "url": ""})
        for r in reversed(rows):
            text = _feed_text(r, titles.get(r.get("proposal_id", ""), ""))
            if text:
                out.append({"ts": r["ts"], "event": r["event"], "proposal_id": r.get("proposal_id", ""), "text": text,
                            "url": r.get("pr_url") or r.get("compare_url") or ""})
        return out

    def status(self) -> dict[str, Any]:
        s = self.settings()
        counts: dict[str, int] = {}
        for p in self.store.proposals():
            counts[p.status] = counts.get(p.status, 0) + 1
        return {
            "enabled": s["evolveEnabled"], "auto_ship": s["evolveAutoShip"], "repo": s["evolveRepo"],
            "github": bool(self.deps.github and self.deps.github.token),
            "budget": self.budget(), "in_flight": self.in_flight(), "last_scan": self.last_scan() or None,
            "scan_minutes": s["evolveScanMinutes"], "slots": {"active": s["evolveSlotsActive"], "away": s["evolveSlotsAway"]},
            "counts": counts, "waiting_for_you": counts.get("pr_open", 0) + counts.get("gated_pass", 0) + len(self.skill_candidates()),
            "rollback_requests": self.rollback_requests(), "awaiting_install": self.awaiting_install(),
            "missing": list(self.deps.missing),
            "code_builds": self.code_tier(), "sandbox_setting": s["evolveSandbox"],
        }


def _as_dict(x: Any) -> dict[str, Any]:
    if isinstance(x, dict):
        return dict(x)
    if hasattr(x, "model_dump"):
        return x.model_dump()
    if hasattr(x, "__dict__"):
        return {k: v for k, v in vars(x).items() if not k.startswith("_")}
    return {"id": str(x)}


def _cost_time(a: Any) -> str:
    a = a if isinstance(a, dict) else {}
    cost = float(a.get("cost_usd") or 0)
    return f"{float(a.get('seconds') or 0):.0f}s/${cost:.2f}" if cost else f"{float(a.get('seconds') or 0):.0f}s/$0"


def _feed_text(r: dict[str, Any], title: str) -> str:
    e, t = r.get("event"), title or r.get("title") or ""
    if e == "scan" and r.get("new"):
        return f"Read {r['new']} new development{'s' if r['new'] != 1 else ''}" + (f", {r.get('proposals')} worth trying" if r.get("proposals") else "")
    if e == "proposed":
        return f"Idea: {t}"
    if e == "build_started":
        return f"Building {t}"
    if e == "gated":
        if r.get("passed"):
            return f"{t} passed every check"
        failed = [c.get("name") for c in ((r.get("report") or {}).get("checks") or []) if not c.get("passed")]
        return f"{t} did not pass" + (f" ({', '.join(failed[:3])})" if failed else "") + "; left unchanged"
    if e == "build_failed":
        return f"Could not build {t}; nothing changed"
    if e == "pr_opened":
        return f"{t} is ready for your review" if not r.get("auto_ship") else f"{t} ready to ship"
    if e == "compare":
        return f"{t} is ready; open the branch to review it"
    if e == "approved":
        return f"Shipping {t}" + (" (all checks passed, low risk)" if r.get("by") == "auto" else "")
    if e == "shipped":
        return f"Now using {t}"
    if e == "held":
        a = r.get("after") or {}
        return f"{t} held up" + (f": {a['success_rate']:.0%} task success" if a.get("success_rate") is not None else "")
    if e == "rolled_back":
        return f"Undid {t}: {r.get('reason', '')}".rstrip(": ")
    if e == "rejected":
        return f"Set aside {t}"
    if e == "skill_adopted":
        return f"Learned a skill from practice: {r.get('name') or r.get('skill_id')}"
    if e == "practiced":
        return f"Learned to {r.get('name') or r.get('family')}: {_cost_time(r.get('first'))} → {_cost_time(r.get('last'))}"
    if e == "skill_approved":
        return f"Installed a skill you approved: {r.get('skill_id')}"
    return ""


# --------------------------------------------------------------------------------- one cycle


def run_cycle(settings: Any, deps: PipelineDeps, evolver: Evolver | None = None) -> dict[str, Any]:
    """scan -> triage -> top-K (``evolveTopK``, default 2) -> build each in parallel -> gates -> ship -> ledger."""
    ev = evolver or Evolver(lambda: settings if isinstance(settings, dict) else {k: setting(settings, k) for k in DEFAULTS}, deps)
    s = ev.settings()
    if not s["evolveEnabled"]:
        return {"ok": True, "enabled": False}
    scan = ev.scan_and_triage()
    picked = []
    tier = ev.code_tier()
    for p in (ev.candidates() if tier["enabled"] else []):
        if len(picked) >= s["evolveTopK"]:
            break
        if not ev.launch(p):
            break
        picked.append(p.id)
    ev.wait()
    return {"ok": True, "enabled": True, "scan": scan, "built": picked,
            "results": [ev.proposal_view(p) for p in (ev.store.get(i) for i in picked) if p is not None],
            "budget": ev.budget(), "code_builds": tier}


# --------------------------------------------------------------------------------- practice (tier 1 learning)


class PracticeHost:
    """Runs ``Practicer`` sessions while the person is away (``USER_ABSENT``) and stops them when they return
    (``USER_PRESENT``): the practicer re-checks presence before every item, and the isolated display is torn
    down when the session ends. Learning curves that improved are recorded in the Evolution feed."""

    ABSENT, PRESENT = "USER_ABSENT", "USER_PRESENT"

    def __init__(self, factory: Callable[[], tuple[Any, Any, Any]], journal: Journal | None = None, log: Any = None,
                 max_items: int = 10, max_seconds: float = 1800.0, spawn: Callable[[Callable[[], None]], Any] | None = None) -> None:
        self._factory = factory  # -> (curriculum, practicer, env); built on first use
        self._parts: tuple[Any, Any, Any] | None = None
        self.journal, self.log = journal, log
        self.max_items, self.max_seconds = max_items, max_seconds
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="o-practice").start())
        self._lock = threading.Lock()
        self.running = False
        self.stop_requested = False
        self.last: dict[str, Any] | None = None
        self.error = ""

    def parts(self) -> tuple[Any, Any, Any]:
        with self._lock:
            if self._parts is None:
                self._parts = self._factory()
            return self._parts

    def on_presence(self, msg: dict[str, Any]) -> None:
        state = msg.get("state") if isinstance(msg, dict) else None
        if state == self.ABSENT:
            self.start()
        elif state == self.PRESENT:
            self.stop()

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running, self.stop_requested = True, False
        self._spawn(self._session)
        return True

    def stop(self) -> None:
        self.stop_requested = True

    def _note(self, summary: str, **detail: Any) -> None:
        if self.log is not None:
            try:
                self.log.record("note", summary, **detail)
            except Exception:  # noqa: BLE001
                pass

    def _session(self) -> None:
        env = None
        try:
            cur, pr, env = self.parts()
            env.start()
            try:
                cur.refresh()
            except Exception as exc:  # noqa: BLE001 - an empty refresh still leaves the existing queue
                self._note(f"practice: curriculum refresh failed: {type(exc).__name__}")
            while not self.stop_requested:
                r = pr.run(max_items=self.max_items, max_seconds=self.max_seconds)
                self.last = {k: v for k, v in r.items() if k != "curves"}
                self._record_curves(r.get("curves") or {})
                if not r.get("attempts") or r.get("stopped") not in ("max items", "session time"):
                    break
            self.error = ""
        except Exception as exc:  # noqa: BLE001 - e.g. PracticeRefused: no Xvfb, host display, display in use
            self.error = f"{type(exc).__name__}: {exc}"[:300]
            self._note(f"practice did not run: {self.error}")
        finally:
            if env is not None:
                try:
                    env.stop()
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self.running = False

    def _record_curves(self, curves: dict[str, Any]) -> None:
        if self.journal is None:
            return
        done = {(r.get("family"), r.get("attempts")) for r in self.journal.read(events=("practiced",))}
        for fam, c in curves.items():
            rows = c.get("attempts") or []
            if len(rows) < 2 or not c.get("improved") or (fam, len(rows)) in done:
                continue
            first, last = c["first"], c["last"]
            self.journal.append("practiced", family=fam, name=fam.replace("_", " ").replace(".", ": ", 1), attempts=len(rows),
                                first={k: first.get(k) for k in ("seconds", "cost_usd", "model_calls", "steps")},
                                last={k: last.get(k) for k in ("seconds", "cost_usd", "model_calls", "steps")},
                                success_rate=c.get("success_rate"))

    def view(self) -> dict[str, Any]:
        try:
            cur, pr, env = self.parts()
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        queue = []
        try:
            for it, sch in cur.queue(limit=20):
                queue.append({"id": it.id, "family": it.family, "app": it.app, "goal": it.goal[:200], "origin": it.origin,
                              "due_ts": getattr(sch, "due_ts", None), "mastered": getattr(sch, "mastered", False)})
        except Exception:  # noqa: BLE001
            pass
        return {
            "available": True, "running": self.running, "error": self.error, "last": self.last,
            "curves": cur.learning_curve(), "queue": queue, "gaps": cur.gap_report(),
            "spend": {"spent_24h_usd": pr.ledger.spent(), "budget_usd": pr.budget_usd, "remaining_usd": pr.remaining_usd()},
            "isolated": bool(getattr(env, "is_isolated", False)),
        }
