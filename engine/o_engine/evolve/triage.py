"""Triage (plan L step 3): signals + capability map -> scored, validated Proposals.

1. ``prefilter`` (deterministic, no network, no model): license gate for code adoption (a repo whose
   SPDX id is not in ``OK_LICENSES`` or unknown cannot be adopted as code), archived repos out, minimum
   stars for new repos, papers must link code, relevance by topic overlap with ``capability_map.json``.
   Survivors are ranked by a prior (relevance, star velocity, recency, kind).
2. ``triage``: batches of at most ``BATCH`` signals go to the model with the relevant subsystems; the
   answer must be strict JSON ``{"proposals": [...]}``. Every proposal is validated field by field; a
   proposal that names unknown signals or subsystems, has a malformed dependency spec, a score outside
   [0, 1], or touches a protected area (``PROTECTED_AREAS`` / ``PROTECTED_PATHS`` here, plus the gate's
   own list in ``evolve/protected.py``) is dropped with a reason in ``TriageResult.rejected``.

The model client is injected. ``TriageClient`` runs ``claude -p --output-format json`` with no tools and
no MCP servers, a dedicated system prompt, ``--max-budget-usd`` set to what is left of the budget, and
sums ``total_cost_usd``; tests pass a fake with the same ``complete``/``spent_usd`` surface.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import ValidationError

from ..tokenfilter import TokenFilter
from .capmap import match_subsystems, prompt_view
from .schema import OK_LICENSES, Proposal, Signal

BATCH = 20
MIN_STARS = 50
MAX_SIGNALS = 40
TRIAGE_TIMEOUT_S = 240.0

# Areas evolve may never change (plan L step 5). A proposal whose title, spec or file list hits one of
# these is rejected before it reaches the builder; the gates enforce the same rule on the real diff.
PROTECTED_AREAS: dict[str, tuple[str, ...]] = {
    "permission/approval prompts": (r"permission[- ]?prompt", r"approval prompt", r"onPermissionRequest",
                                    r"mcp__o__approve", r"permission[- ]?mode", r"skip[- ]?permissions",
                                    r"\bapprove tool\b", r"checkpoint classifier"),
    "payment guard": (r"payment guard", r"isPaymentUrl", r"PAY_HOST", r"guard\.js", r"payment (fields?|sites?|block)"),
    "action log": (r"action[- ]?log", r"actionlog", r"hash[- ]chain"),
    "consent/sensor rules": (r"consentFilter", r"\bconsent", r"sensor defaults?", r"enable ?sensor"),
    "evolve gates": (r"evolve/(gates|protected|baseline|builder|schema|triage)\.py", r"evolve gates?",
                     r"OK_LICENSES", r"protected[- ]paths?", r"diff budget"),
}
PROTECTED_PATHS: tuple[str, ...] = (
    "main/agent.js", "main/tools/bridge-mcp.js", "main/tools/checkpoints.js", "main/proxy.js",
    "engine/o_engine/gate.py", "extension/guard.js", "extension/background.js", "extension/manifest.json",
    "main/link.js", "engine/o_engine/actionlog.py", "main/actionlog.js", "main/settings.js", "main/ambient.js",
    "engine/o_engine/ambient/sensors/", "engine/o_engine/ambient/layer.py", "engine/o_engine/ambient/__init__.py",
    "engine/o_engine/evolve/gates.py", "engine/o_engine/evolve/protected.py", "engine/o_engine/evolve/baseline.py",
    "engine/o_engine/evolve/builder.py", "engine/o_engine/evolve/schema.py", "engine/o_engine/evolve/triage.py",
    "engine/tests/test_evolve_gates.py", "engine/tests/test_evolve_builder.py", "engine/tests/test_evolve_triage.py",
    ".github/",
)
_PROTECTED_RE = [(area, re.compile(p, re.I)) for area, pats in PROTECTED_AREAS.items() for p in pats]
_DEP_RE = re.compile(r"^(pypi:[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.+!_-]+"
                     r"|npm:(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*@[0-9A-Za-z.+_-]+)$")
_FILE_RE = re.compile(r"^(engine|main|renderer|extension|install|docs)/[A-Za-z0-9_./-]+$")
CODE_ADOPTION_KINDS = ("new_repo", "repo_release")
SKILL_ONLY_SUBSYSTEMS = frozenset({"mcp"})  # matching only these makes a signal a runtime skill, not an upgrade


class TriageClientLike(Protocol):
    spent_usd: float

    def complete(self, system: str, user: str, max_budget_usd: float | None = None) -> str: ...


class TriageError(RuntimeError):
    pass


class TriageClient:
    """Lean ``claude -p``: no tools, no MCP, no session persistence, budget passed to the CLI."""

    name = "claude-cli"

    def __init__(self, claude_path: str = "claude", model: str | None = None,
                 timeout: float = TRIAGE_TIMEOUT_S) -> None:
        self.claude_path = claude_path
        self.model = model
        self.timeout = timeout
        self.spent_usd = 0.0
        self.calls = 0

    def command(self, system: str, max_budget_usd: float | None) -> list[str]:
        cmd = [self.claude_path, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
               "--mcp-config", '{"mcpServers":{}}', "--no-session-persistence", "--system-prompt", system]
        if self.model:
            cmd += ["--model", self.model]
        if max_budget_usd is not None:
            cmd += ["--max-budget-usd", f"{max(max_budget_usd, 0.01):.2f}"]
        return cmd

    def complete(self, system: str, user: str, max_budget_usd: float | None = None) -> str:
        self.calls += 1
        try:
            proc = subprocess.run(self.command(system, max_budget_usd), input=user, capture_output=True,
                                  text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as e:
            raise TriageError(f"claude cli failed to run: {e}") from None
        try:
            payload = json.loads(proc.stdout or "")
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            cost = payload.get("total_cost_usd", payload.get("cost_usd"))
            if isinstance(cost, (int, float)) and cost >= 0:
                self.spent_usd += float(cost)
        if proc.returncode != 0 or not isinstance(payload, dict):
            raise TriageError(f"claude cli exited {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:300]}")
        result = payload.get("result")
        if payload.get("is_error") or not isinstance(result, str):
            raise TriageError(f"claude cli returned no result ({payload.get('subtype', '')})")
        return result


# --------------------------------------------------------------------------- prefilter
@dataclass
class Candidate:
    signal: Signal
    subsystems: dict[str, int]
    prior: float
    code_ok: bool  # the license allows adopting the code itself


@dataclass
class TriageResult:
    proposals: list[Proposal] = field(default_factory=list)
    kept: list[Candidate] = field(default_factory=list)
    filtered: list[tuple[str, str]] = field(default_factory=list)  # (signal id, reason)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (proposal title, reason)
    spent_usd: float = 0.0
    calls: int = 0
    errors: list[str] = field(default_factory=list)


def license_ok(lic: str) -> bool:
    return lic in OK_LICENSES


def _age_days(published: str) -> float:
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return 30.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - dt).total_seconds() / 86400, 0.0)


_KIND_WEIGHT = {"repo_release": 0.5, "new_repo": 0.6, "paper": 0.7, "changelog": 0.8, "mcp_server": 0.3,
                "package_release": 0.4}


def prefilter(signals: list[Signal], capmap: dict[str, Any], *, min_stars: int = MIN_STARS,
              max_signals: int = MAX_SIGNALS) -> tuple[list[Candidate], list[tuple[str, str]]]:
    kept: list[Candidate] = []
    dropped: list[tuple[str, str]] = []
    seen_repo_kind: set[tuple[str, str]] = set()
    for s in signals:
        lic_ok = license_ok(s.license)
        if s.kind == "new_repo":
            if not lic_ok:
                dropped.append((s.id, f"license {s.license or 'unknown'} not adoptable")); continue
            if s.stars < min_stars:
                dropped.append((s.id, f"{s.stars} stars < {min_stars}")); continue
        if s.kind == "repo_release" and "dependency" not in s.topics and not lic_ok:
            dropped.append((s.id, f"watch repo license {s.license or 'unknown'} not adoptable")); continue
        if s.kind == "paper" and "github.com" not in s.summary and not s.repo:
            dropped.append((s.id, "paper without code")); continue
        if "archived" in s.topics:
            dropped.append((s.id, "archived")); continue
        hits = match_subsystems(f"{s.title} {s.summary[:1500]}", s.topics, capmap)
        if not hits and s.kind == "changelog":
            hits = {"model_routing": 1}  # Anthropic / Claude Code releases always bear on model routing
        elif not hits and (s.kind == "package_release" or "dependency" in s.topics):
            hits = {"_dependency": 1}  # O's own dependency: relevant by definition, subsystem left to the model
        if not hits:
            dropped.append((s.id, "no topic overlap with the capability map")); continue
        qualifying = {k: v for k, v in hits.items() if k not in SKILL_ONLY_SUBSYSTEMS}
        if not qualifying and s.kind in ("mcp_server", "new_repo"):
            # Another MCP server is something O could *use* (topic "skill", for the skills worker),
            # not evidence for changing O's code.
            dropped.append((s.id, "skill-only: relevant to runtime use, not a code upgrade")); continue
        hits = qualifying or hits
        # one candidate per (repo, kind): the newest release of a repo stands for its series
        rk = (s.repo, s.kind)
        if s.repo and s.kind in ("repo_release", "changelog", "package_release") and rk in seen_repo_kind:
            dropped.append((s.id, "older release of the same repo")); continue
        seen_repo_kind.add(rk)
        rel = min(max(hits.values()) / 4, 1.0)
        vel = min(math.log10(1 + s.stars_7d) / 3, 1.0) if s.kind == "new_repo" else 0.0
        rec = max(0.0, 1 - _age_days(s.published) / 14) if s.published else 0.3
        prior = round(0.45 * rel + 0.2 * vel + 0.15 * rec + 0.2 * _KIND_WEIGHT.get(s.kind, 0.3), 4)
        kept.append(Candidate(s, hits, prior, lic_ok))
    kept.sort(key=lambda c: c.prior, reverse=True)
    for c in kept[max_signals:]:
        dropped.append((c.signal.id, "below the top-N prior cut"))
    return kept[:max_signals], dropped


# --------------------------------------------------------------------------- prompts
SYSTEM = """You are the triage stage of O's self-evolution pipeline. O is a desktop agent (Electron app + Python engine).
You read new AI developments (signals) and O's capability map and propose concrete, testable upgrades to O's own code.
Rules:
- Only propose what the evidence supports; cite the signal (repo, version, paper, benchmark numbers it states) in rationale.
- Prefer small, verifiable changes. A proposal must name the repo files to touch and the approach.
- acceptance_test describes one runnable test (pytest under engine/tests/ or a node selftest under main/tools/) that fails before and passes after, using fakes, no network.
- Code may only be adopted from repos whose license is in the allowed list; otherwise re-implement the idea (say so).
- dependency_only=true only when the whole change is bumping/adding a dependency version with no code change beyond pins.
- new_dependencies entries look like "pypi:name==1.2.3" or "npm:name@1.2.3".
- Never touch: permission/approval prompts, the payment guard, the action log, consent/sensor rules, or the self-evolution gates.
- Skip signals that are irrelevant, hype, or not better than what O already has. Zero proposals is a valid answer.
- score in [0,1]: expected value for O (evidence strength x gain x feasibility, minus risk).
Answer with JSON only: {"proposals":[{"signal_ids":[...],"subsystem":"<capability map key>","title":"...","rationale":"...","files":["engine/..."],"spec":"...","acceptance_test":"...","expected_gain":"<metric and rough size>","risk":"low|medium|high","dependency_only":false,"new_dependencies":[],"score":0.0}]}"""


def build_user_prompt(batch: list[Candidate], capmap: dict[str, Any]) -> str:
    keys = sorted({k for c in batch for k in c.subsystems if not k.startswith("_")}) or None
    sigs = []
    for c in batch:
        s = c.signal
        sigs.append({"id": s.id, "kind": s.kind, "source": s.source, "title": s.title, "url": s.url,
                     "repo": s.repo, "version": s.version, "published": s.published[:10], "stars": s.stars,
                     "stars_7d": s.stars_7d, "license": s.license or "unknown",
                     "code_adoptable": c.code_ok, "topics": s.topics[:12], "summary": s.summary[:900],
                     "likely_subsystems": sorted(k for k in c.subsystems if not k.startswith("_"))})
    return (f"Allowed licenses: {', '.join(OK_LICENSES)}\n\nCapability map (relevant subsystems):\n"
            f"{prompt_view(capmap, keys)}\n\nAll subsystem keys: {', '.join(capmap['subsystems'])}\n\n"
            f"Signals:\n{json.dumps(sigs, ensure_ascii=False, indent=0)}\n\nReturn the JSON now.")


# --------------------------------------------------------------------------- validation
def protected_hits(*texts: str, files: list[str] = ()) -> list[str]:
    hits: list[str] = []
    for f in files:
        for p in PROTECTED_PATHS:
            if f == p or (p.endswith("/") and f.startswith(p)) or (p.endswith("_") and f.startswith(p)):
                hits.append(f"protected path {f}")
        try:
            from . import protected as _gate  # the gate's own list, when present
            why = _gate.match(f)
            if why:
                hits.append(f"protected path {f}: {why}")
        except Exception:
            pass
    blob = "\n".join(texts)
    for area, rx in _PROTECTED_RE:
        if rx.search(blob):
            hits.append(f"protected area: {area}")
    return sorted(set(hits))


def proposal_id(signal_ids: list[str], title: str) -> str:
    return hashlib.sha256(("|".join(sorted(signal_ids)) + "|" + title.strip().lower()).encode()).hexdigest()[:16]


def validate_proposal(raw: Any, batch: dict[str, Candidate], capmap: dict[str, Any]) -> Proposal:
    if not isinstance(raw, dict):
        raise ValueError("proposal is not an object")
    allowed = {"signal_ids", "subsystem", "title", "rationale", "files", "spec", "acceptance_test",
               "expected_gain", "risk", "dependency_only", "new_dependencies", "score"}
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f"unknown fields {sorted(extra)}")
    ids = raw.get("signal_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in batch for i in ids):
        raise ValueError("signal_ids must name signals of this batch")
    sub = raw.get("subsystem")
    if sub not in capmap["subsystems"]:
        raise ValueError(f"unknown subsystem {sub!r}")
    score = raw.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError(f"score {score!r} not in [0,1]")
    files = raw.get("files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) and _FILE_RE.match(f) and ".." not in f for f in files):
        raise ValueError("files must be repo-relative paths under engine/, main/, renderer/, extension/, install/, docs/")
    deps = raw.get("new_dependencies", [])
    if not isinstance(deps, list) or not all(isinstance(d, str) and _DEP_RE.match(d) for d in deps):
        raise ValueError(f"malformed new_dependencies {deps!r}")
    dep_only = raw.get("dependency_only", False)
    if not isinstance(dep_only, bool):
        raise ValueError("dependency_only must be a boolean")
    if dep_only and not (deps or any(batch[i].signal.kind in ("package_release", "repo_release") for i in ids)):
        raise ValueError("dependency_only without a dependency")
    for k in ("title", "rationale", "spec", "acceptance_test"):
        if not isinstance(raw.get(k), str) or not raw[k].strip():
            raise ValueError(f"{k} must be a non-empty string")
    if not files and not dep_only:
        raise ValueError("a code proposal must name the files it touches")
    hits = protected_hits(raw["title"], raw["spec"], files=files)
    if hits:
        raise ValueError("touches protected areas: " + "; ".join(hits))
    spec = raw["spec"].strip()
    if files:
        spec = f"Files: {', '.join(files)}\n\n{spec}"
    code_adopt = any(batch[i].signal.kind in CODE_ADOPTION_KINDS for i in ids)
    lic_ok = all(batch[i].code_ok for i in ids if batch[i].signal.kind in CODE_ADOPTION_KINDS) if code_adopt else True
    try:
        return Proposal(id=proposal_id(ids, raw["title"]), signal_ids=ids, subsystem=sub, title=raw["title"].strip(),
                        rationale=raw["rationale"].strip(), spec=spec, acceptance_test=raw["acceptance_test"].strip(),
                        expected_gain=str(raw.get("expected_gain") or ""), score=float(score),
                        risk=raw.get("risk", "medium"), dependency_only=dep_only, new_dependencies=deps,
                        licenses_ok=lic_ok)
    except ValidationError as e:
        raise ValueError(f"schema: {e.errors()[0].get('msg', e)}") from None


def parse_answer(text: str) -> list[Any]:
    body = TokenFilter().feed(text or "")
    if not body:
        raise ValueError("model produced no JSON value")
    data = json.loads(body)
    if isinstance(data, dict) and isinstance(data.get("proposals"), list):
        return data["proposals"]
    raise ValueError('answer must be {"proposals": [...]}')


# --------------------------------------------------------------------------- triage
def triage(signals: list[Signal], capmap: dict[str, Any], client: TriageClientLike, budget_usd: float, *,
           batch_size: int = BATCH, max_signals: int = MAX_SIGNALS, min_stars: int = MIN_STARS,
           store: Any = None, result: TriageResult | None = None) -> list[Proposal]:
    """Prefilter, then ask the model in batches of <= ``batch_size`` while spend < ``budget_usd``."""
    res = result if result is not None else TriageResult()
    batch_size = max(1, min(batch_size, BATCH))
    kept, dropped = prefilter(signals, capmap, min_stars=min_stars, max_signals=max_signals)
    res.kept, res.filtered = kept, dropped
    start = float(getattr(client, "spent_usd", 0.0))
    by_id: dict[str, Proposal] = {}
    for i in range(0, len(kept), batch_size):
        spent = float(getattr(client, "spent_usd", 0.0)) - start
        remaining = budget_usd - spent
        if remaining <= 0.02:
            res.errors.append(f"budget reached before batch {i // batch_size + 1}")
            break
        batch = kept[i:i + batch_size]
        index = {c.signal.id: c for c in batch}
        res.calls += 1
        try:
            raw = client.complete(SYSTEM, build_user_prompt(batch, capmap), max_budget_usd=remaining)
            items = parse_answer(raw)
        except Exception as e:  # one bad batch must not lose the others
            res.errors.append(f"batch {i // batch_size + 1}: {type(e).__name__}: {str(e)[:200]}")
            continue
        for item in items:
            try:
                p = validate_proposal(item, index, capmap)
            except ValueError as e:
                title = item.get("title", "?") if isinstance(item, dict) else "?"
                res.rejected.append((str(title)[:120], str(e)[:300]))
                continue
            by_id.setdefault(p.id, p)
    res.spent_usd = round(float(getattr(client, "spent_usd", 0.0)) - start, 6)
    res.proposals = sorted(by_id.values(), key=lambda p: p.score, reverse=True)
    if store is not None:
        for p in res.proposals:
            if store.add_proposal(p):
                store.record_decision(p.id, "proposed", "triage", {"score": p.score, "spent_usd": res.spent_usd})
    return res.proposals
