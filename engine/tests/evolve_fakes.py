"""Fakes for the evolve pipeline tests: no network, no git, no Claude."""
from __future__ import annotations

import threading
from typing import Any

from o_engine.evolve.pipeline import Evolver, PipelineDeps
from o_engine.evolve.schema import GateCheck, GateReport, Proposal, Signal


def sig(i: int, source: str = "github") -> Signal:
    return Signal(id=f"s{i}", kind="repo_release", source=source, title=f"lib {i} v2", url=f"https://github.com/x/lib{i}/releases/v2",
                  repo=f"x/lib{i}", version="2.0", license="MIT")


def prop(i: int, score: float = 0.5, risk: str = "low", new_deps: list[str] | None = None, dep_only: bool = False,
         subsystem: str = "cu.grounding", sids: list[str] | None = None) -> Proposal:
    return Proposal(id=f"p{i}", signal_ids=sids or [f"s{i}"], subsystem=subsystem, title=f"Upgrade {i}",
                    rationale="faster grounding", spec="do it", acceptance_test="test_it", expected_gain="+2% success",
                    score=score, risk=risk, new_dependencies=new_deps or [], dependency_only=dep_only)


class FakeGitHub:
    def __init__(self, token: str = "tok", repo: str = "o/O") -> None:
        self.token, self.repo = token, repo
        self.prs: list[dict[str, Any]] = []
        self.merged: list[int] = []
        self.closed: list[int] = []
        self.releases: list[dict[str, Any]] = []

    def compare_url(self, base: str, branch: str) -> str:
        return f"https://github.com/{self.repo}/compare/{base}...{branch}"

    def open_pr(self, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        n = len(self.prs) + 1
        self.prs.append({"number": n, "head": head, "base": base, "title": title, "body": body})
        return {"number": n, "url": f"https://github.com/{self.repo}/pull/{n}", "base_sha": f"base{n}"}

    def merge_pr(self, number: int, title: str) -> dict[str, Any]:
        self.merged.append(number)
        return {"sha": f"merge{number}", "merged": True}

    def close_pr(self, number: int) -> None:
        self.closed.append(number)

    def create_release(self, tag: str, sha: str, name: str, body: str) -> dict[str, Any]:
        self.releases.append({"tag": tag, "sha": sha, "name": name})
        return {"tag": tag, "url": ""}


class Fakes:
    """scan / triage / build / gates with knobs; records calls."""

    def __init__(self, signals: list[Signal] | None = None, proposals: list[Proposal] | None = None,
                 spent: float = 1.0, fail_gates: set[str] | None = None, protected: set[str] | None = None,
                 barrier: threading.Barrier | None = None) -> None:
        self.signals = signals or []
        self.proposals = proposals or []
        self.spent = spent
        self.fail_gates = fail_gates or set()
        self.protected_ids = protected or set()
        self.barrier = barrier
        self.scans = 0
        self.built: list[tuple[str, float]] = []
        self.triaged: list[list[str]] = []
        self.pushed: list[str] = []
        self.lock = threading.Lock()

    def scan(self) -> list[Signal]:
        self.scans += 1
        return list(self.signals)

    def triage(self, signals: list[Signal], priors: Any = None) -> list[Proposal]:
        self.triaged.append([s.id for s in signals])
        ids = {s.id for s in signals}
        return [p for p in self.proposals if set(p.signal_ids) & ids]

    def build(self, p: Proposal, repo_root: str, workdir: str, budget_usd: float = 0.0) -> tuple[str, str, float, str]:
        if self.barrier is not None:
            self.barrier.wait(timeout=5)  # proves builds run concurrently
        with self.lock:
            self.built.append((p.id, budget_usd))
        if p.id.startswith("boom"):
            raise RuntimeError("client crashed")
        return (f"evolve/{p.id}", f"c-{p.id}", self.spent, "")

    def run_gates(self, p: Proposal, workdir: str, baseline: Any, spent_usd: float = 0.0) -> GateReport:
        ok = p.id not in self.fail_gates
        checks = [GateCheck(name="engine_pytest", passed=ok, detail="1200 passed" if ok else "3 failed", metric_before=1200, metric_after=1201 if ok else 1197),
                  GateCheck(name="offline_eval", passed=True, metric_before=0.71, metric_after=0.74),
                  GateCheck(name="protected_paths", passed=p.id not in self.protected_ids, detail="")]
        files = ["engine/o_engine/cu/ground.py"] + (["main/actionlog.js"] if p.id in self.protected_ids else [])
        return GateReport(proposal_id=p.id, branch=f"evolve/{p.id}", commit=f"c-{p.id}", passed=all(c.passed for c in checks),
                          checks=checks, diff_files=files, diff_lines=42, spent_usd=spent_usd)

    def push(self, workdir: str, branch: str) -> None:
        self.pushed.append(branch)


def make(tmp_path, fakes: Fakes, settings: dict[str, Any] | None = None, github: Any = "default", clock: list[float] | None = None,
         **extra: Any) -> Evolver:
    clock = clock if clock is not None else [1_000_000.0]
    gh = FakeGitHub() if github == "default" else github
    deps = PipelineDeps(scan=fakes.scan, triage=fakes.triage, build=fakes.build, run_gates=fakes.run_gates,
                        baseline=lambda: {"commit": "base"}, github=gh, push=fakes.push, repo_root=str(tmp_path / "repo"),
                        evolve_dir=str(tmp_path / "evolve"), now=lambda: clock[0], **extra)
    s = {"evolveRepo": "o/O", **(settings or {})}
    return Evolver(lambda: s, deps)
