"""Shared, strict contract for the self-evolution pipeline (plan L). Every module builds against these models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(frozen=True, extra="forbid")

SignalKind = Literal["repo_release", "new_repo", "paper", "changelog", "mcp_server", "package_release"]
ProposalStatus = Literal["proposed", "accepted", "rejected", "building", "built", "gated_pass", "gated_fail",
                         "ready_local", "pr_open", "approved", "shipped", "rolled_back"]
# ready_local: passed every gate, but the branch stays on this computer because O cannot push to the repo.
OK_LICENSES = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense", "MPL-2.0")


class Signal(BaseModel):
    """One observed development (a release, a repo, a paper, a changelog entry)."""

    model_config = _STRICT
    id: str  # stable hash of kind+url+version
    kind: SignalKind
    source: str  # "github", "arxiv", "anthropic", "mcp-registry", "pypi", "npm"
    title: str = Field(min_length=1, max_length=400)
    url: str
    summary: str = Field(default="", max_length=4000)
    published: str = ""  # ISO date
    repo: str = ""  # owner/name when applicable
    version: str = ""
    stars: int = 0
    stars_7d: int = 0  # star velocity when known
    license: str = ""  # SPDX id when known
    topics: list[str] = Field(default_factory=list)
    archived: bool = False
    pushed: str = ""  # ISO date of the repo's last push, for maintenance checks
    docs_url: str = ""  # homepage / docs / registry page (skill signals)
    package: str = ""  # "npm:<name>" / "pypi:<name>" when the signal names an installable package (skill installer)


class Proposal(BaseModel):
    """A concrete, testable upgrade derived from one or more signals."""

    model_config = _STRICT
    id: str
    signal_ids: list[str] = Field(min_length=1)
    subsystem: str  # key in capability_map.json
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=4000)  # why it moves O forward, with evidence
    spec: str = Field(min_length=1, max_length=12000)  # implementation spec handed to the builder
    acceptance_test: str = Field(min_length=1, max_length=4000)  # the new test that must pass
    acceptance_test_path: str = ""  # repo path of that test when known (makes the acceptance gate exact)
    files: list[str] = Field(default_factory=list)  # repo paths the change is expected to touch
    expected_gain: str = ""  # which metric should improve, by roughly how much
    score: float = Field(ge=0, le=1)
    risk: Literal["low", "medium", "high"] = "medium"
    dependency_only: bool = False
    new_dependencies: list[str] = Field(default_factory=list)  # "pypi:name==ver" / "npm:name@ver"
    licenses_ok: bool = True
    status: ProposalStatus = "proposed"


class GateCheck(BaseModel):
    model_config = _STRICT
    name: str
    passed: bool
    detail: str = ""
    metric_before: float | None = None
    metric_after: float | None = None
    skipped: bool = False  # not run (e.g. fail-fast after a static failure); still counts as not passed


class GateReport(BaseModel):
    model_config = _STRICT
    proposal_id: str
    branch: str
    commit: str = ""
    baseline_commit: str = ""
    passed: bool
    checks: list[GateCheck] = Field(default_factory=list)
    diff_files: list[str] = Field(default_factory=list)
    diff_lines: int = 0
    spent_usd: float = 0.0
