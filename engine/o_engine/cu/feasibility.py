"""Feasibility gate: read-only probes, one model question, never a blocker.

Before planning, ``FeasibilityGate.check`` runs a few read-only probes (the
accessibility tree, ``which <app>``, optionally ``ls``/``read_file``/``screenshot``)
through the backend and the ``Sandbox``, hands their output to the model once via
``complete_json`` and returns ``FeasibilityVerdict``. Probes can never click, type,
press keys or set values: ``Probe.op`` is a closed enum. Shell probes are confined
twice: every command line must pass the shared ``is_allowed_probe`` (``cu/probes.py``)
before ``Sandbox.run``, and ``ls``/``read_file`` paths must resolve inside the sandbox
(``Sandbox.resolve``); a refused probe is ``ProbeResult(ok=False, output="probe not
allowed")`` and nothing runs. Any model failure (outage, garbage reply) yields
``feasible=True`` with reason ``"gate unavailable"`` so an outage never blocks a job.
When a log is given, each probe is recorded (kind ``tool`` for the run, ``result`` for
its output) and an infeasible verdict is logged with kind ``note``.
"""
from __future__ import annotations

import hashlib
import json
import shlex
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..schema import ProvisionStep
from .probes import PROBE_ALLOW_LIST, PROBE_REFUSED, is_allowed_probe  # noqa: F401  (shared allow-list)
from .treeutil import summarize_tree

PROBE_OPS: tuple[str, ...] = ("tree", "screenshot", "ls", "read_file", "which")
PROBE_TIMEOUT_SECONDS = 10
PROBE_OUTPUT_CHARS = 4000
TREE_LINES = 200
GATE_UNAVAILABLE = "gate unavailable"

GATE_SYSTEM_PROMPT = (
    "You decide whether a computer-use goal is feasible on this machine, from read-only probe output only.\n"
    "Infeasible means the goal cannot be achieved here: the app or file it needs does not exist, "
    "the goal contradicts itself, or it asks for something the visible UI cannot do.\n"
    "When in doubt, answer feasible. Reply with ONLY a JSON object:\n"
    '{"feasible": true|false, "reason": "<one short sentence>"}\n'
    "No prose, no markdown, no code fences, no other keys."
)


class Probe(BaseModel):
    """One read-only observation. ``arg`` is the window (tree), path (ls/read_file) or name (which)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    op: Literal["tree", "screenshot", "ls", "read_file", "which"]
    arg: str = Field(default="", max_length=1024)


class ProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str
    arg: str = ""
    ok: bool
    output: str = ""


class FeasibilityVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    feasible: bool
    reason: str = ""
    probes: tuple[ProbeResult, ...] = ()


class _RawVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    feasible: bool
    reason: str = ""


def default_probes(app: str) -> tuple[Probe, ...]:
    return (Probe(op="tree"), Probe(op="which", arg=app))


def build_gate_prompt(goal: str, app: str, results: tuple[ProbeResult, ...]) -> str:
    blocks = [f"Goal: {goal}", f"App: {app}", "", "Probe results (read-only):"]
    for r in results:
        head = f"--- {r.op} {r.arg}".rstrip() + (" (ok)" if r.ok else " (failed)")
        blocks.append(f"{head}\n{r.output}")
    blocks.append("\nJSON verdict:")
    return "\n".join(blocks)


_SHELL = {
    "ls": lambda arg: f"ls -la -- {shlex.quote(arg or '.')}",
    "read_file": lambda arg: f"head -c {PROBE_OUTPUT_CHARS} -- {shlex.quote(arg)}",
    "which": lambda arg: f"command -v -- {shlex.quote(arg)}",
}
_PATH_OPS = frozenset({"ls", "read_file"})


class FeasibilityGate:
    """``check(goal, app, job_id)`` → ``FeasibilityVerdict``; never raises."""

    def __init__(self, model: Any, backend: Any, sandbox: Any, log: Any = None,
                 probes: tuple[Probe, ...] | None = None) -> None:
        self.model = model
        self.backend = backend
        self.sandbox = sandbox
        self.log = log
        self.probes = tuple(probes) if probes is not None else None

    def _log(self, kind: str, summary: str, **detail: Any) -> None:
        if self.log is None:
            return
        try:
            self.log.record(kind, summary, **detail)
        except Exception:
            pass  # logging never breaks a probe

    def _run_probe(self, probe: Probe, job_id: str = "", app: str = "") -> ProbeResult:
        self._log("tool", f"{job_id}: feasibility probe {probe.op} {probe.arg}".rstrip(),
                  job_id=job_id, op=probe.op, arg=probe.arg)
        result = self._probe(probe, app)
        self._log("result", f"{job_id}: probe {probe.op} {'ok' if result.ok else 'failed'}: "
                  + (result.output.strip()[:200] or "(no output)"),
                  job_id=job_id, op=probe.op, arg=probe.arg, ok=result.ok, output=result.output[:PROBE_OUTPUT_CHARS])
        return result

    def _probe(self, probe: Probe, app: str = "") -> ProbeResult:
        try:
            if probe.op == "tree":
                from .backends.base import backend_tree

                tree = backend_tree(self.backend, probe.arg or None, app=app or None)
                return ProbeResult(op=probe.op, arg=probe.arg, ok=True,
                                   output=summarize_tree(tree, TREE_LINES) or "(empty tree)")
            if probe.op == "screenshot":
                png = self.backend.screenshot() or b""
                digest = hashlib.sha256(png).hexdigest()[:16]
                return ProbeResult(op=probe.op, arg=probe.arg, ok=bool(png), output=f"{len(png)} bytes sha256:{digest}")
            if self.sandbox is None:
                return ProbeResult(op=probe.op, arg=probe.arg, ok=False, output="no sandbox configured")
            if probe.op in _PATH_OPS:
                try:
                    self.sandbox.resolve(probe.arg or ".")  # confined: absolute or ``..`` paths outside refuse
                except Exception:
                    return ProbeResult(op=probe.op, arg=probe.arg, ok=False, output=PROBE_REFUSED)
            cmd = _SHELL[probe.op](probe.arg)
            if not is_allowed_probe(cmd):
                return ProbeResult(op=probe.op, arg=probe.arg, ok=False, output=PROBE_REFUSED)
            step = ProvisionStep(target_directory=".", command_string=cmd, timeout_seconds=PROBE_TIMEOUT_SECONDS)
            res = self.sandbox.run(step)
            out = (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")
            ok = res.exit_code == 0 and not res.timed_out
            return ProbeResult(op=probe.op, arg=probe.arg, ok=ok, output=out.strip()[:PROBE_OUTPUT_CHARS])
        except Exception as e:
            return ProbeResult(op=probe.op, arg=probe.arg, ok=False, output=f"{type(e).__name__}: {e}")

    def _ask(self, goal: str, app: str, results: tuple[ProbeResult, ...]) -> _RawVerdict | None:
        try:
            raw = self.model.complete_json(GATE_SYSTEM_PROMPT, build_gate_prompt(goal, app, results))
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                return None
            return _RawVerdict.model_validate(obj)
        except (Exception, ValidationError):
            return None

    def check(self, goal: str, app: str, job_id: str) -> FeasibilityVerdict:
        probes = self.probes if self.probes is not None else default_probes(app)
        results = tuple(self._run_probe(p, job_id, app) for p in probes)
        answer = self._ask(goal, app, results)
        if answer is None:
            return FeasibilityVerdict(feasible=True, reason=GATE_UNAVAILABLE, probes=results)
        verdict = FeasibilityVerdict(feasible=answer.feasible, reason=answer.reason, probes=results)
        if not verdict.feasible and self.log is not None:
            self.log.record("note", f"{job_id}: goal judged infeasible: {verdict.reason[:200]}",
                            job_id=job_id, app=app, feasible=False, reason=verdict.reason[:500],
                            probes=[p.op for p in probes])
        return verdict


def check_feasibility(gate: FeasibilityGate | None, goal: str, app: str, job_id: str) -> FeasibilityVerdict:
    """What the graph's ``context`` node calls: no gate configured → feasible, no probes, no model call."""
    if gate is None:
        return FeasibilityVerdict(feasible=True, reason="no gate configured")
    return gate.check(goal, app, job_id)
