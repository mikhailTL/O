"""Append-only procedure store and the bounded self-heal loop.

A procedure is a solved OScript saved as ``procedures/<app>/<key>/v<N>.json`` where
``key = sha256(app, goal)``. Versions are written with ``open(..., 'x')`` and carry
lineage (``parent``, ``healed_step``, ``reason``). Nothing is ever deleted or
rewritten. Replay runs a stored script with zero model calls; when a step fails the
``HealLoop`` asks the planner for one replacement step, stores v<N+1>, and reruns
from the failed step, at most ``max_heals`` times.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from ..schema import SchemaViolation
from .planner import Planner, script_to_dict

_VERSION_FILE = re.compile(r"^v(\d+)\.json$")
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class ProcedureExists(Exception):
    """A version file already exists; procedure versions are never overwritten."""


class Procedure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    app: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    version: int = Field(ge=1)
    path: str = Field(min_length=1)
    parent: str | None = None
    healed_step: int | None = Field(default=None, ge=0)
    reason: str | None = None
    created: str


def slug(app: str) -> str:
    s = _SLUG.sub("-", app.strip()).strip("-.")
    return s or "app"


class ProcedureStore:
    def __init__(self, dir: str | os.PathLike[str], parse_script: Callable[[Any], Any] | None = None) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir = self.dir.resolve()
        self._parse_script = parse_script

    @staticmethod
    def key(app: str, goal: str) -> str:
        return hashlib.sha256(f"{app}\n{goal}".encode("utf-8")).hexdigest()

    def _goal_dir(self, app: str, goal: str) -> Path:
        return self.dir / slug(app) / self.key(app, goal)

    @staticmethod
    def make_id(app: str, key: str, version: int) -> str:
        return f"{slug(app)}/{key}/v{version}"

    def _versions(self, goal_dir: Path) -> list[int]:
        if not goal_dir.is_dir():
            return []
        out = []
        for p in goal_dir.iterdir():
            m = _VERSION_FILE.match(p.name)
            if m and p.is_file():
                out.append(int(m.group(1)))
        return sorted(out)

    def _read(self, path: Path) -> Procedure:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Procedure.model_validate(payload["procedure"])

    def latest(self, app: str, goal: str) -> Procedure | None:
        goal_dir = self._goal_dir(app, goal)
        versions = self._versions(goal_dir)
        if not versions:
            return None
        return self._read(goal_dir / f"v{versions[-1]}.json")

    def get(self, procedure_id: str) -> Procedure | None:
        path = (self.dir / procedure_id).with_suffix(".json")
        try:
            path = path.resolve()
        except OSError:
            return None
        if self.dir not in path.parents or not path.is_file():
            return None
        return self._read(path)

    def versions(self, app: str, goal: str) -> list[Procedure]:
        goal_dir = self._goal_dir(app, goal)
        return [self._read(goal_dir / f"v{v}.json") for v in self._versions(goal_dir)]

    def store(
        self,
        app: str,
        goal: str,
        script: Any,
        parent: str | None = None,
        healed_step: int | None = None,
        reason: str | None = None,
    ) -> Procedure:
        goal_dir = self._goal_dir(app, goal)
        goal_dir.mkdir(parents=True, exist_ok=True)
        key = self.key(app, goal)
        version = (self._versions(goal_dir) or [0])[-1] + 1
        while True:
            path = goal_dir / f"v{version}.json"
            proc = Procedure(
                id=self.make_id(app, key, version),
                app=app,
                goal=goal,
                version=version,
                path=str(path.relative_to(self.dir)),
                parent=parent,
                healed_step=healed_step,
                reason=reason,
                created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            payload = {"procedure": proc.model_dump(), "script": script_to_dict(script)}
            try:
                with open(path, "x", encoding="utf-8") as f:
                    f.write(json.dumps(payload, indent=2, sort_keys=True))
            except FileExistsError:
                version += 1  # a concurrent writer took this slot; never overwrite it
                continue
            return proc

    def load_script(self, procedure: Procedure) -> Any:
        path = (self.dir / procedure.path).resolve()
        if self.dir not in path.parents:
            raise ValueError(f"procedure path {procedure.path!r} is outside the store")
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload["script"]
        if self._parse_script is not None:
            return self._parse_script(raw)
        try:
            from .schema import parse_script
        except ImportError:
            raise SchemaViolation("o_engine.cu.schema.parse_script is not available") from None
        return parse_script(raw)

    def list(self) -> list[Procedure]:
        out: list[Procedure] = []
        for app_dir in sorted(p for p in self.dir.iterdir() if p.is_dir()):
            for goal_dir in sorted(p for p in app_dir.iterdir() if p.is_dir()):
                for v in self._versions(goal_dir):
                    out.append(self._read(goal_dir / f"v{v}.json"))
        return out

    def _app_procedures(self, app: str) -> list[list[Procedure]]:
        """Per goal of ``app``, its versions in ascending order."""
        app_dir = self.dir / slug(app)
        if not app_dir.is_dir():
            return []
        out: list[list[Procedure]] = []
        for goal_dir in sorted(p for p in app_dir.iterdir() if p.is_dir()):
            versions = [self._read(goal_dir / f"v{v}.json") for v in self._versions(goal_dir)]
            if versions:
                out.append(versions)
        return out

    def ids(self, app: str) -> list[str]:
        """Every stored procedure id for ``app`` (all goals, all versions), sorted."""
        return sorted(p.id for versions in self._app_procedures(app) for p in versions)

    def newest(self, app: str) -> list[Procedure]:
        """The newest version of each goal for ``app``.

        This is a lineage fact, not a pass claim: the newest version may be an unverified heal
        (``HealLoop`` stores v<N+1> before rerunning it, and the rerun may fail). Callers that need a
        verified result replay the ids themselves (``InterfaceBuilder.fitness``)."""
        return [versions[-1] for versions in self._app_procedures(app)]


def failure_text(result: Any) -> str:
    """Best-effort error text from a RunResult (its own error, else the failed step's)."""
    for attr in ("error", "reason", "message"):
        v = getattr(result, attr, None)
        if isinstance(v, str) and v:
            return v
    fi = getattr(result, "failed_index", None)
    steps = getattr(result, "steps", None) or []
    if isinstance(fi, int) and 0 <= fi < len(steps):
        s = steps[fi]
        for attr in ("error", "reason", "message", "detail"):
            v = s.get(attr) if isinstance(s, dict) else getattr(s, attr, None)
            if isinstance(v, str) and v:
                return v
    return f"step {fi} failed" if isinstance(fi, int) else "run failed"


def _run_from(runner: Any, script: Any, job_id: str, start_index: int) -> Any:
    """Call ``runner.run`` from ``start_index`` when the runner supports it, else from the top."""
    run = runner.run
    if start_index > 0:
        try:
            params = inspect.signature(run).parameters
        except (TypeError, ValueError):
            params = {}
        for kw in ("start_index", "start", "from_index"):
            if kw in params:
                return run(script, job_id, **{kw: start_index})
    return run(script, job_id)


class HealLoop:
    """Replay a stored procedure; on a judged step failure heal it (bounded) with lineage."""

    def __init__(self, store: ProcedureStore, planner: Planner, runner_factory: Callable[[], Any], max_heals: int = 3) -> None:
        if max_heals < 0:
            raise ValueError("max_heals must be >= 0")
        self.store = store
        self.planner = planner
        self.runner_factory = runner_factory
        self.max_heals = max_heals

    def run(self, procedure: Procedure, job_id: str, tree_summary_fn: Callable[[Any], str] | None = None) -> dict:
        script = self.store.load_script(procedure)
        runner = self.runner_factory()
        result = _run_from(runner, script, job_id, 0)
        heals = 0
        lineage: list[Procedure] = [procedure]
        while not getattr(result, "ok", False) and heals < self.max_heals:
            failed = getattr(result, "failed_index", None)
            if not isinstance(failed, int) or failed < 0:
                break
            error = failure_text(result)
            summary = tree_summary_fn(runner) if tree_summary_fn else ""
            try:
                step = self.planner.replan_step(script, failed, error, summary)
                script = self.planner.replace_step(script, failed, step)
            except SchemaViolation:
                break
            procedure = self.store.store(
                procedure.app, procedure.goal, script,
                parent=procedure.id, healed_step=failed, reason=error[:500],
            )
            lineage.append(procedure)
            heals += 1
            runner = self.runner_factory()
            result = _run_from(runner, script, job_id, failed)
        return {"result": result, "procedure": procedure, "script": script, "heals": heals, "lineage": lineage}
