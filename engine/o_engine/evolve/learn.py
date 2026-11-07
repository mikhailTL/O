"""Learn (plan L.7): continuous post-ship evaluation, automatic rollback, and triage priors.

``Journal`` is the pipeline's own append-only record (``<data_dir>/evolve/journal.jsonl``, one JSON object
per line, never rewritten): every scan, build, spend, gate result, PR, approval, ship, hold and rollback.
Budget, the Evolution feed and the priors are all derived from it.

Telemetry is read, never written, from what O already records:

* ``<learning_dir>/observations.jsonl``  task outcomes (``source: "task"``, ``outcome: ok|failed``)
* ``<data_dir>/engine-actions.jsonl``    engine ``result`` entries (``detail.ok``), deduplicated by job id
* ``<userData>/o-actions.jsonl``         app task entries (``Done:`` / ``Error:``, ``detail.costUsd``)
* ``<data_dir>/interfaces/*/v*/fitness.jsonl``  per-app interface replay pass rate

``evaluate_shipped`` compares a rolling window before a release with the hours since it, and rolls back the
moment success drops or cost rises past a threshold (once enough tasks ran). ``priors`` turns history into a
score multiplier for triage: which subsystems, sources and risk levels pass gates and hold up after shipping.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

WINDOW_HOURS = 6.0  # rolling window before/after a release
HOLD_HOURS = 48.0  # a release that survived this long without regression "held up"
MIN_SAMPLES = 5  # tasks needed in each window before a verdict
MAX_SUCCESS_DROP = 0.10  # absolute success-rate drop that triggers a rollback
MAX_COST_RISE = 0.25  # relative cost-per-task rise that triggers a rollback
MAX_FITNESS_DROP = 0.15  # absolute interface-fitness drop that triggers a rollback
PRIOR_MIN, PRIOR_MAX = 0.25, 2.0


# --------------------------------------------------------------------------------- journal


class Journal:
    """Append-only JSONL of pipeline events. Thread-safe; unreadable lines are skipped."""

    def __init__(self, path: str | os.PathLike[str], now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now
        self._lock = threading.Lock()

    def append(self, event: str, proposal_id: str = "", **detail: Any) -> dict[str, Any]:
        row = {"ts": float(detail.pop("ts", None) or self.now()), "event": str(event), "proposal_id": str(proposal_id or "")}
        row.update(_jsonable(detail))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        return row

    def read(self, since: float | None = None, events: Iterable[str] | None = None, proposal_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        want = set(events) if events else None
        out: list[dict[str, Any]] = []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if since is not None and float(row.get("ts") or 0) < since:
                continue
            if want is not None and row.get("event") not in want:
                continue
            if proposal_id is not None and row.get("proposal_id") != proposal_id:
                continue
            out.append(row)
        return out

    def last(self, proposal_id: str, events: Iterable[str]) -> dict[str, Any] | None:
        rows = self.read(events=events, proposal_id=proposal_id)
        return rows[-1] if rows else None

    def spent(self, since: float) -> float:
        return round(sum(float(r.get("spent_usd") or 0) for r in self.read(since=since) if r.get("spent_usd")), 6)


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(d, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o)))
    except (TypeError, ValueError):
        return {k: str(v) for k, v in d.items()}


# --------------------------------------------------------------------------------- telemetry


@dataclass(frozen=True)
class Sample:
    ts: float
    ok: bool | None = None
    cost: float | None = None
    fitness: tuple[int, int] | None = None  # (passed, replayed)


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    return None


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


class Telemetry:
    """Reads O's existing records into ``Sample``s. Every path is optional; a missing file is no data."""

    def __init__(self, learning_dir: str | os.PathLike[str] | None = None, data_dir: str | os.PathLike[str] | None = None,
                 app_log: str | os.PathLike[str] | None = None) -> None:
        self.learning_dir = Path(learning_dir) if learning_dir else None
        self.data_dir = Path(data_dir) if data_dir else None
        self.app_log = Path(app_log) if app_log else None

    def samples(self, since: float, until: float) -> list[Sample]:
        out: list[Sample] = []
        jobs: dict[str, Sample] = {}

        def keep(ts: float | None) -> bool:
            return ts is not None and since <= ts < until

        if self.learning_dir is not None:
            for r in _jsonl(self.learning_dir / "observations.jsonl"):
                ts = _ts(r.get("ts"))
                if r.get("source") != "task" or r.get("outcome") not in ("ok", "failed") or not keep(ts):
                    continue
                s = Sample(ts=ts, ok=r["outcome"] == "ok")  # type: ignore[arg-type]
                if r.get("task_id"):
                    jobs[str(r["task_id"])] = s
                else:
                    out.append(s)
        if self.data_dir is not None:
            for r in _jsonl(self.data_dir / "engine-actions.jsonl"):
                ts = _ts(r.get("ts"))
                d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
                if r.get("kind") != "result" or not isinstance(d.get("ok"), bool) or d.get("infeasible") or not keep(ts):
                    continue
                key = str(d.get("job_id") or "")
                if key and key in jobs:
                    continue
                s = Sample(ts=ts, ok=d["ok"])  # type: ignore[arg-type]
                if key:
                    jobs[key] = s
                else:
                    out.append(s)
            for fit in (self.data_dir / "interfaces").glob("*/v*/fitness.jsonl") if (self.data_dir / "interfaces").is_dir() else ():
                for r in _jsonl(fit):
                    ts = _ts(r.get("ts"))
                    if keep(ts) and isinstance(r.get("replayed"), int) and r["replayed"] > 0:
                        out.append(Sample(ts=ts, fitness=(int(r.get("passed") or 0), int(r["replayed"]))))  # type: ignore[arg-type]
        if self.app_log is not None:
            for r in _jsonl(self.app_log):
                ts = _ts(r.get("ts"))
                summary = str(r.get("summary") or "")
                if r.get("source") != "app" or r.get("kind") != "task" or not keep(ts):
                    continue
                if not (summary.startswith("Done:") or summary.startswith("Error:")):
                    continue
                d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
                cost = d.get("costUsd")
                out.append(Sample(ts=ts, ok=summary.startswith("Done:") and "(stopped)" not in summary,  # type: ignore[arg-type]
                                  cost=float(cost) if isinstance(cost, (int, float)) else None))
        out.extend(jobs.values())
        return sorted(out, key=lambda s: s.ts)


def window_stats(samples: Iterable[Sample]) -> dict[str, Any]:
    samples = list(samples)
    oks = [s.ok for s in samples if s.ok is not None]
    costs = [s.cost for s in samples if s.cost is not None]
    fits = [s.fitness for s in samples if s.fitness is not None]
    replayed = sum(f[1] for f in fits)
    return {
        "tasks": len(oks),
        "success_rate": round(sum(oks) / len(oks), 4) if oks else None,
        "cost_per_task": round(sum(costs) / len(costs), 6) if costs else None,
        "fitness": round(sum(f[0] for f in fits) / replayed, 4) if replayed else None,
    }


def compare(before: dict[str, Any], after: dict[str, Any], *, max_success_drop: float = MAX_SUCCESS_DROP,
            max_cost_rise: float = MAX_COST_RISE, max_fitness_drop: float = MAX_FITNESS_DROP) -> list[str]:
    """Reasons ``after`` regressed against ``before`` (empty when it did not)."""
    why: list[str] = []
    b, a = before.get("success_rate"), after.get("success_rate")
    if b is not None and a is not None and b - a > max_success_drop:
        why.append(f"task success fell from {b:.0%} to {a:.0%}")
    b, a = before.get("cost_per_task"), after.get("cost_per_task")
    if b and a is not None and (a - b) / b > max_cost_rise:
        why.append(f"cost per task rose from ${b:.3f} to ${a:.3f}")
    b, a = before.get("fitness"), after.get("fitness")
    if b is not None and a is not None and b - a > max_fitness_drop:
        why.append(f"interface fitness fell from {b:.0%} to {a:.0%}")
    return why


# --------------------------------------------------------------------------------- evaluation


def shipped_releases(journal: Journal) -> list[dict[str, Any]]:
    """Shipped proposals whose verdict is still open (not held, not rolled back)."""
    closed = {r["proposal_id"] for r in journal.read(events=("held", "rolled_back"))}
    latest: dict[str, dict[str, Any]] = {}
    for r in journal.read(events=("shipped",)):
        latest[r["proposal_id"]] = r
    return [r for pid, r in latest.items() if pid not in closed]


def evaluate_shipped(
    journal: Journal,
    telemetry: Telemetry,
    rollback: Callable[[str, str, str], Any] | None = None,
    *,
    now: float | None = None,
    window_hours: float = WINDOW_HOURS,
    hold_hours: float = HOLD_HOURS,
    min_samples: int = MIN_SAMPLES,
    max_success_drop: float = MAX_SUCCESS_DROP,
    max_cost_rise: float = MAX_COST_RISE,
    max_fitness_drop: float = MAX_FITNESS_DROP,
) -> list[dict[str, Any]]:
    """One pass over every open release. Verdicts: ``watching`` | ``held`` | ``rolled_back``.

    ``rollback(proposal_id, previous_ref, reason)`` is called once per regression, right after the ledger
    records ``rolled_back``; the app's updater then pins ``previous_ref``.
    """
    now = time.time() if now is None else now
    w = window_hours * 3600
    out: list[dict[str, Any]] = []
    for rel in shipped_releases(journal):
        pid, t0 = rel["proposal_id"], float(rel["ts"])
        before = window_stats(telemetry.samples(t0 - w, t0))
        after = window_stats(telemetry.samples(t0, min(now, t0 + max(w, hold_hours * 3600))))
        verdict: dict[str, Any] = {"proposal_id": pid, "before": before, "after": after, "verdict": "watching", "reasons": []}
        enough = before["tasks"] >= min_samples and after["tasks"] >= min_samples
        fit_enough = before["fitness"] is not None and after["fitness"] is not None
        reasons = compare(before, after, max_success_drop=max_success_drop, max_cost_rise=max_cost_rise,
                          max_fitness_drop=max_fitness_drop) if (enough or fit_enough) else []
        if not enough:
            reasons = [r for r in reasons if r.startswith("interface fitness")]
        if reasons:
            ref = str(rel.get("previous") or "")
            reason = "; ".join(reasons)
            journal.append("rolled_back", pid, reason=reason, ref=ref, before=before, after=after, by="learn")
            verdict.update(verdict="rolled_back", reasons=reasons, ref=ref)
            if rollback is not None:
                try:
                    rollback(pid, ref, reason)
                except Exception as exc:  # noqa: BLE001 - the ledger already has the verdict; the app retries
                    verdict["rollback_error"] = f"{type(exc).__name__}: {exc}"
        elif now - t0 >= hold_hours * 3600:
            journal.append("held", pid, before=before, after=after)
            verdict["verdict"] = "held"
        out.append(verdict)
    return out


# --------------------------------------------------------------------------------- priors


def _bucket() -> dict[str, int]:
    return {"passed": 0, "failed": 0, "held": 0, "rolled_back": 0}


def priors(journal: Journal) -> dict[str, dict[str, dict[str, Any]]]:
    """``{"subsystem"|"source"|"risk": {key: {passed, failed, held, rolled_back, multiplier}}}``.

    multiplier = (gate pass rate / 0.5) x (post-ship hold rate / 0.5), both Laplace-smoothed, so no history
    is exactly 1.0; clipped to [0.25, 2.0].
    """
    meta: dict[str, dict[str, Any]] = {}
    for r in journal.read(events=("proposed", "gated")):
        m = meta.setdefault(r["proposal_id"], {})
        for k in ("subsystem", "risk", "sources"):
            if r.get(k):
                m[k] = r[k]
    out: dict[str, dict[str, dict[str, Any]]] = {"subsystem": {}, "source": {}, "risk": {}}

    def bump(pid: str, field: str) -> None:
        m = meta.get(pid) or {}
        keys = {"subsystem": [m.get("subsystem")], "risk": [m.get("risk")], "source": list(m.get("sources") or [])}
        for dim, vals in keys.items():
            for v in vals:
                if v:
                    out[dim].setdefault(str(v), _bucket())[field] += 1

    for r in journal.read(events=("gated", "held", "rolled_back")):
        field = {"held": "held", "rolled_back": "rolled_back"}.get(r["event"]) or ("passed" if r.get("passed") else "failed")
        bump(r["proposal_id"], field)
    for dim in out.values():
        for b in dim.values():
            gate = (b["passed"] + 1) / (b["passed"] + b["failed"] + 2)
            hold = (b["held"] + 1) / (b["held"] + b["rolled_back"] + 2)
            b["multiplier"] = round(min(PRIOR_MAX, max(PRIOR_MIN, (gate / 0.5) * (hold / 0.5))), 4)
    return out


def prior_multiplier(pri: dict[str, dict[str, dict[str, Any]]], subsystem: str, risk: str, sources: Iterable[str] = ()) -> float:
    """Product of the subsystem, risk and (mean) source multipliers; 1.0 where there is no history."""
    m = float((pri.get("subsystem", {}).get(subsystem) or {}).get("multiplier", 1.0))
    m *= float((pri.get("risk", {}).get(risk) or {}).get("multiplier", 1.0))
    srcs = [float((pri.get("source", {}).get(s) or {}).get("multiplier", 1.0)) for s in sources]
    if srcs:
        m *= sum(srcs) / len(srcs)
    return round(min(PRIOR_MAX, max(PRIOR_MIN, m)), 4)
