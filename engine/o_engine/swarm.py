"""Role-partitioned planning: telemetry audit -> plan compilation -> environment inspection.

The three roles are plain callables over a shared ``state`` dict, so they do the same
work whichever orchestrator drives them. ``SimpleCrew`` runs them sequentially on worker
threads (state handed through a queue) and needs nothing beyond the standard library;
``CrewAICrew`` wraps the same callables as crewAI tools when ``crewai`` is importable.
``get_crew`` picks one.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .schema import ProvisionRequest, SchemaViolation

ROLE_NAMES = ("telemetry_auditor", "subprocess_compiler", "environment_inspector")
PROFILE_SUMMARY_LIMIT = 4000
ROLE_TIMEOUT_SECONDS = 600.0

ROLE_SPECS: dict[str, dict[str, str]] = {
    "telemetry_auditor": {
        "role": "Telemetry Auditor",
        "goal": "Collect the workspace profile (shell history, tree, config files, recent screen telemetry) for the objective.",
        "backstory": "Reads what is already lying around on this machine and reports it without asking the user anything.",
    },
    "subprocess_compiler": {
        "role": "Subprocess Compiler",
        "goal": "Compile the objective and the workspace profile into a strict ProvisionRequest of shell steps.",
        "backstory": "Only emits JSON that passes the engine schema; prose, markdown and partial plans are rejected.",
    },
    "environment_inspector": {
        "role": "Environment Inspector",
        "goal": "Inspect the environment the plan targets: probe verify ports headlessly and judge the isolated display.",
        "backstory": "Never opens a window on the user's display; reports what can be observed and nothing more.",
    },
}


class RoleRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    elapsed_ms: int = Field(ge=0)
    ok: bool
    note: str = ""


class SwarmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: dict[str, Any] = Field(default_factory=dict)
    request: ProvisionRequest | None = None
    inspection: dict[str, Any] = Field(default_factory=dict)
    roles: list[RoleRun] = Field(default_factory=list)


@dataclass
class SwarmDeps:
    context: Any
    parser: Any
    verify_target: Callable[[Any], dict]
    judge: Any = None
    screenshot: Callable[[], bytes] | None = None


# --- roles ----------------------------------------------------------------------------


def profile_summary(profile: Mapping[str, Any], limit: int = PROFILE_SUMMARY_LIMIT) -> str:
    """A compact text rendering of a WorkspaceProfile dump for the parser prompt."""
    if not profile:
        return ""
    lines = [f"cwd: {profile.get('cwd', '')}", f"platform: {profile.get('platform', '')}"]
    history = profile.get("shell_history") or []
    if history:
        lines.append("recent shell history:")
        lines.extend(f"  {cmd}" for cmd in history[-20:])
    configs = profile.get("config_files") or []
    if configs:
        lines.append("config files: " + ", ".join(str(c.get("path", "")) for c in configs))
    tree = profile.get("tree") or {}
    names = [c.get("name", "") for c in (tree.get("children") or [])[:40]]
    if names:
        lines.append("top-level entries: " + ", ".join(names))
    telemetry = profile.get("telemetry") or []
    if telemetry:
        lines.append("recent screen telemetry:")
        for row in telemetry[-10:]:
            text = str(row.get("text", "")).strip().replace("\n", " ")[:160]
            lines.append(f"  [{row.get('app', '')}] {row.get('window', '')}: {text}")
    out = "\n".join(lines)
    return out[:limit]


def telemetry_auditor(state: dict, deps: SwarmDeps) -> str:
    cwd = state.get("cwd") or "."
    profile = deps.context.profile(cwd)
    dumped = profile.model_dump() if hasattr(profile, "model_dump") else dict(profile)
    state["profile"] = dumped
    n_hist = len(dumped.get("shell_history") or [])
    n_tel = len(dumped.get("telemetry") or [])
    return f"profile collected: {n_hist} history lines, {len(dumped.get('config_files') or [])} config files, {n_tel} telemetry rows"


def subprocess_compiler(state: dict, deps: SwarmDeps) -> str:
    objective = str(state.get("objective", "")).strip()
    summary = profile_summary(state.get("profile") or {})
    text = objective if not summary else f"{objective}\n\nWorkspace profile:\n{summary}"
    try:
        request = deps.parser.parse(text, goal=objective)
    except SchemaViolation as e:
        state["request"] = None
        state["compile_error"] = str(e)
        raise
    state["request"] = request
    return f"request compiled: {len(request.steps)} steps (via {getattr(deps.parser, 'describe', lambda: 'parser')()})"


def environment_inspector(state: dict, deps: SwarmDeps) -> str:
    request = state.get("request")
    inspection: dict[str, Any] = {}
    notes: list[str] = []
    verify = getattr(request, "verify", None) if request is not None else None
    if verify is not None:
        try:
            inspection["verify"] = deps.verify_target(verify)
            notes.append(f"verify port {verify.port}: " + ("ok" if inspection["verify"].get("ok") else "not answering"))
        except Exception as e:
            inspection["verify"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            notes.append(f"verify probe failed: {type(e).__name__}")
    if deps.judge is not None and deps.screenshot is not None:
        objective = str(state.get("objective", "")).strip()
        try:
            png = deps.screenshot()
            ok, reason = deps.judge.ask(png, f"Does the screen show that this is done: {objective}?")
            inspection["display"] = {"ok": bool(ok), "reason": str(reason)}
            notes.append(f"display judge: {'ok' if ok else 'not yet'}")
        except Exception as e:
            inspection["display"] = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
            notes.append(f"display judge failed: {type(e).__name__}")
    if not inspection:
        notes.append("nothing to inspect")
    state["inspection"] = inspection
    return "; ".join(notes)


ROLES: dict[str, Callable[[dict, SwarmDeps], str]] = {
    "telemetry_auditor": telemetry_auditor,
    "subprocess_compiler": subprocess_compiler,
    "environment_inspector": environment_inspector,
}


def _run_role(name: str, state: dict, deps: SwarmDeps) -> RoleRun:
    started = time.monotonic()
    try:
        note = ROLES[name](state, deps) or ""
        ok = True
    except SchemaViolation as e:
        note, ok = f"schema violation: {e}", False
    except Exception as e:
        note, ok = f"{type(e).__name__}: {e}", False
    return RoleRun(role=name, elapsed_ms=int((time.monotonic() - started) * 1000), ok=ok, note=note[:2000])


def _result_from_state(state: dict, roles: list[RoleRun]) -> SwarmResult:
    return SwarmResult(
        profile=dict(state.get("profile") or {}),
        request=state.get("request"),
        inspection=dict(state.get("inspection") or {}),
        roles=roles,
    )


def _log_role(log: Any, run: RoleRun, crew: str) -> None:
    if log is None:
        return
    try:
        log.record("note", f"swarm[{crew}] {run.role}: {'ok' if run.ok else 'failed'} in {run.elapsed_ms} ms — {run.note[:160]}",
                   crew=crew, role=run.role, ok=run.ok, elapsed_ms=run.elapsed_ms, note=run.note)
    except Exception:
        pass


# --- crews ----------------------------------------------------------------------------


class SimpleCrew:
    """Three roles, each on its own worker thread, sequential: state is passed through a queue."""

    name = "simple"
    available = True

    def __init__(self, deps: SwarmDeps, log: Any = None, role_timeout: float = ROLE_TIMEOUT_SECONDS) -> None:
        self.deps = deps
        self.log = log
        self.role_timeout = role_timeout

    def run(self, objective: str, cwd: str) -> SwarmResult:
        state: dict[str, Any] = {"objective": objective, "cwd": cwd}
        handoff: "queue.Queue[tuple[dict, RoleRun]]" = queue.Queue()
        roles: list[RoleRun] = []
        for name in ROLE_NAMES:
            worker = threading.Thread(
                target=lambda n=name, s=state: handoff.put((s, _run_role(n, s, self.deps))),
                name=f"swarm-{name}", daemon=True,
            )
            worker.start()
            try:
                state, run = handoff.get(timeout=self.role_timeout)
            except queue.Empty:
                run = RoleRun(role=name, elapsed_ms=int(self.role_timeout * 1000), ok=False,
                              note=f"timed out after {self.role_timeout:g} s")
            roles.append(run)
            _log_role(self.log, run, self.name)
        return _result_from_state(state, roles)


class CrewAICrew:
    """crewAI orchestration over the same role callables (exposed as tools), LLM = local vLLM."""

    name = "crewai"

    def __init__(self, deps: SwarmDeps, log: Any = None, vllm_url: str = "http://127.0.0.1:8000/v1",
                 model: str = "local") -> None:
        self.deps = deps
        self.log = log
        self.vllm_url = vllm_url.rstrip("/")
        self.model = model
        self._mods: dict[str, Any] | None = None
        self.reason = ""
        try:
            from crewai import LLM, Agent, Crew, Process, Task  # type: ignore
            try:
                from crewai.tools import tool as crew_tool  # type: ignore
            except Exception:  # older crewai layouts
                from crewai_tools import tool as crew_tool  # type: ignore
        except Exception as e:
            self.reason = f"crewai not importable ({type(e).__name__}: {e})"
            return
        self._mods = {"LLM": LLM, "Agent": Agent, "Crew": Crew, "Process": Process, "Task": Task, "tool": crew_tool}

    @property
    def available(self) -> bool:
        return self._mods is not None

    def _build(self, state: dict, roles: list[RoleRun]) -> Any:
        m = self._mods
        assert m is not None
        model = self.model if "/" in self.model else f"openai/{self.model}"
        llm = m["LLM"](model=model, base_url=self.vllm_url, api_key="local")
        agents, tasks = [], []
        for name in ROLE_NAMES:
            spec = ROLE_SPECS[name]

            def make_tool(role_name: str):
                def _invoke(arg: str = "") -> str:
                    """Run this role over the shared engine state and return its note."""
                    run = _run_role(role_name, state, self.deps)
                    roles.append(run)
                    _log_role(self.log, run, self.name)
                    return run.note

                _invoke.__name__ = role_name
                return m["tool"](role_name)(_invoke)

            agent = m["Agent"](role=spec["role"], goal=spec["goal"], backstory=spec["backstory"],
                               tools=[make_tool(name)], llm=llm, verbose=False, allow_delegation=False)
            task = m["Task"](
                description=f"Call the {name} tool exactly once for the objective: {state['objective']!r}. Report its note.",
                expected_output="One short sentence summarising the tool's note.",
                agent=agent,
            )
            agents.append(agent)
            tasks.append(task)
        return m["Crew"](agents=agents, tasks=tasks, process=m["Process"].sequential, verbose=False)

    def run(self, objective: str, cwd: str) -> SwarmResult:
        if not self.available:
            raise RuntimeError(self.reason)
        state: dict[str, Any] = {"objective": objective, "cwd": cwd}
        roles: list[RoleRun] = []
        crew = self._build(state, roles)
        narration = ""
        try:
            output = crew.kickoff()
            narration = str(getattr(output, "raw", output) or "")
        except Exception as e:
            narration = f"crewai orchestration failed: {type(e).__name__}: {e}"
        ran = {r.role for r in roles}
        for name in ROLE_NAMES:  # a role the LLM never invoked still has to run
            if name not in ran:
                run = _run_role(name, state, self.deps)
                roles.append(run)
                _log_role(self.log, run, self.name)
        if narration:
            roles = [r.model_copy(update={"note": (r.note + " | crew: " + narration[:400]).strip()}) if r.role == ROLE_NAMES[-1] else r
                     for r in roles]
        return _result_from_state(state, roles)


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def get_crew(deps: SwarmDeps, log: Any = None, settings_like: Any = None) -> SimpleCrew | CrewAICrew:
    """crewAI when importable and ``swarmEnabled``; the thread-based SimpleCrew otherwise."""
    from .containment import _setting  # same dict-or-object reader

    enabled = _truthy(_setting(settings_like, "swarmEnabled", "swarm_enabled", default=os.environ.get("O_SWARM")), True)
    vllm_url = _setting(settings_like, "vllmUrl", "vllm_url", default="http://127.0.0.1:8000/v1")
    model = _setting(settings_like, "vllmModel", "vllm_model", default="local")
    if enabled:
        crew = CrewAICrew(deps, log, vllm_url=vllm_url, model=model)
        if crew.available:
            return crew
        if log is not None:
            try:
                log.record("note", f"swarm: {crew.reason}; using SimpleCrew")
            except Exception:
                pass
    return SimpleCrew(deps, log)


def swarm_summary(result: SwarmResult) -> str:
    """One-line JSON summary for logs and job state."""
    return json.dumps(
        {
            "steps": len(result.request.steps) if result.request else 0,
            "roles": [{"role": r.role, "ok": r.ok, "elapsed_ms": r.elapsed_ms} for r in result.roles],
            "inspected": sorted(result.inspection.keys()),
        },
        separators=(",", ":"),
    )
