"""The fixed provisioning graph.

    context -> validate -> cache_lookup -> gate -> execute -> verify -> cache_store -> report
                                            ^                  |  (fail)
                                            |                  v
                                            +--- remediate <---+     (attempt < max_attempts; else report)

Edges are the literal ``GRAPH_EDGES`` below; nodes only write state. Remediation
is the only cycle and is bounded by ``max_attempts``. Every transition is recorded.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from . import genui
from .actionlog import ActionLog
from .gate import DEFAULT_TIMEOUT_S, ApproveWaiter, Gate, GateMode, QueueDestroyed  # noqa: F401
from .remediate import Patch, Remediator
from .schema import ProvisionRequest, parse_request

TAIL_CHARS = 4000
MAX_ATTEMPTS_CEILING = 20
RECURSION_LIMIT = 16 + 4 * MAX_ATTEMPTS_CEILING

GRAPH_NODES: tuple[str, ...] = (
    "context", "validate", "cache_lookup", "gate", "execute", "verify", "cache_store", "remediate", "report",
)

GRAPH_EDGES: tuple[tuple[str, str], ...] = (
    (START, "context"),
    ("context", "validate"),
    ("validate", "cache_lookup"),
    ("cache_lookup", "gate"),
    ("gate", "execute"),
    ("execute", "verify"),
    ("verify", "cache_store"),
    ("verify", "remediate"),
    ("verify", "report"),
    ("remediate", "gate"),
    ("cache_store", "report"),
    ("report", END),
)

JOBSTATE_FIELDS: tuple[str, ...] = (
    "id", "label", "phase", "attempt", "max_attempts", "cached", "steps",
    "diagnostic", "verify", "done", "ok", "error",
)


class EngineState(TypedDict, total=False):
    id: str
    label: str
    phase: str
    attempt: int
    max_attempts: int
    cached: bool
    steps: list[dict]
    diagnostic: Optional[dict]
    verify: Optional[dict]
    done: bool
    ok: bool
    error: Optional[str]
    request: ProvisionRequest
    profile: Optional[dict]
    fingerprint: str
    patches_applied: int
    mode: str
    cache_entry: Any


@dataclass
class Deps:
    context: Any
    parser: Any
    cache: Any
    verify_target: Callable[[Any], dict]
    display: Any
    log: ActionLog
    sandbox: Any = None
    max_attempts: int = 3
    platform: str = "linux"
    apply_patch: Optional[Callable[[Any, Patch], Any]] = None
    gate_timeout_s: int = DEFAULT_TIMEOUT_S
    sandbox_factory: Optional[Callable[[str], Any]] = None
    runner: Any = None
    high_risk_sandbox: str = "docker"
    settings_like: Any = None
    ui: Any = None  # genui.UIStore; frames are emitted on job:<id> when set

    def sandbox_for(self, job_id: str) -> Any:
        """One sandbox per job when a factory is given (parallel jobs never share a tree)."""
        return self.sandbox_factory(job_id) if self.sandbox_factory is not None else self.sandbox

    def runner_for_scripts(self) -> Any:
        """The multi-language script runner; the native one is built lazily when none was given."""
        if self.runner is None:
            from .runners import get_runner

            self.runner = get_runner("native")
        return self.runner


def step_label(step: Any) -> str:
    """What a step record's ``command`` field says: the command, or a short script description."""
    command = getattr(step, "command_string", None)
    if command:
        return str(command)
    script = getattr(step, "script", None)
    if script is not None:
        code = getattr(script, "code", "") or ""
        first = code.strip().splitlines()[0] if code.strip() else ""
        return f"[{getattr(script, 'language', 'script')} script, {len(code)} chars] {first[:60]}"
    return ""


def step_record(result: Any, index: int | None = None) -> dict:
    get = (lambda k, d=None: result.get(k, d)) if isinstance(result, dict) else (lambda k, d=None: getattr(result, k, d))
    stdout = get("stdout", "") or ""
    stderr = get("stderr", "") or ""
    return {
        "index": get("index", index) if get("index", None) is not None else index,
        "command": get("command", ""),
        "cwd": get("cwd", ""),
        "exit_code": int(get("exit_code", 1)),
        "stdout_tail": stdout[-TAIL_CHARS:],
        "stderr_tail": stderr[-TAIL_CHARS:],
        "elapsed_ms": int(get("elapsed_ms", 0) or 0),
        "timed_out": bool(get("timed_out", False)),
    }


def cached_replay_request(cache: Any, entry: Any, job_dir: Any, request: ProvisionRequest) -> ProvisionRequest | None:
    """A one-step request whose step is the cached script as a ``ScriptPayload``.

    The script text is read from the cache and carried in the step (``bash`` for ``.sh``,
    ``powershell`` for ``.ps1``, run from the job root), so every disposable backend takes
    its own script path: Docker writes it under the mounted job dir, E2B uploads it with
    ``files.write``, the local fallback hands it to the runner. Nothing is copied into the
    job directory. None when the cache entry does not map to a readable script file or the
    script exceeds ``MAX_SCRIPT_LENGTH`` (the caller re-runs the original steps instead).
    """
    from pathlib import Path

    from .schema import MAX_SCRIPT_LENGTH, MAX_TIMEOUT_SECONDS, ProvisionStep, ScriptPayload

    script_path = getattr(entry, "script_path", None)
    cache_dir = getattr(cache, "dir", None)
    if not (script_path and cache_dir):
        return None
    source = (Path(cache_dir) / str(script_path)).resolve()
    if not source.is_file():
        return None
    try:
        code = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not code.strip() or len(code) > MAX_SCRIPT_LENGTH or "\x00" in code:
        return None
    language = "powershell" if str(script_path).endswith(".ps1") else "bash"
    timeout = int(getattr(entry, "timeout_seconds", 0) or 0) or max(s.timeout_seconds for s in request.steps)
    timeout = max(1, min(timeout, MAX_TIMEOUT_SECONDS))
    payload = ScriptPayload(language=language, code=code, target_directory=".", timeout_seconds=timeout)
    step = ProvisionStep(target_directory=".", timeout_seconds=timeout, script=payload)
    return request.model_copy(update={"steps": (step,)})


def job_state(state: dict) -> dict:
    out = {k: state.get(k) for k in JOBSTATE_FIELDS}
    out["steps"] = list(out["steps"] or [])
    out["cached"] = bool(out["cached"])
    out["done"] = bool(out["done"])
    out["ok"] = bool(out["ok"])
    out["attempt"] = int(out["attempt"] or 0)
    return out


def _tracker(config: RunnableConfig) -> dict:
    return config.get("configurable", {}).setdefault("tracker", {})


def build_graph(deps: Deps):
    log = deps.log

    def enter(state: EngineState, phase: str, config: RunnableConfig, **detail: Any) -> None:
        log.record("graph", f"{state['id']}: {phase}", job_id=state["id"], phase=phase, attempt=state.get("attempt", 0), **detail)
        tracker = _tracker(config)
        tracker["phase"] = phase
        tracker["state"] = {**state, "phase": phase}
        tracker["log"] = log
        progress = tracker.get("progress")
        if callable(progress):
            try:
                progress(job_state({**state, "phase": phase}))
            except Exception:
                pass
        genui.job_surface(deps.ui, tracker, state["id"], state.get("label", ""), GRAPH_NODES)
        genui.ui_phase(tracker, phase, state.get("attempt", 0))

    def context(state: EngineState, config: RunnableConfig) -> dict:
        enter(state, "context", config)
        request: ProvisionRequest = state["request"]
        cwd = request.steps[0].target_directory
        profile = deps.context.profile(cwd)
        dumped = profile.model_dump() if hasattr(profile, "model_dump") else dict(profile or {})
        max_attempts = int(state.get("max_attempts") or deps.max_attempts)
        return {"phase": "context", "profile": dumped, "max_attempts": max_attempts}

    def validate(state: EngineState, config: RunnableConfig) -> dict:
        enter(state, "validate", config)
        request = parse_request(state["request"].model_dump())
        return {"phase": "validate", "request": request, "fingerprint": request.fingerprint(deps.platform)}

    def cache_lookup(state: EngineState, config: RunnableConfig) -> dict:
        enter(state, "cache_lookup", config)
        fp = state["fingerprint"]
        entry = deps.cache.lookup(fp)
        hit = entry is not None
        log.record("cache", f"{state['id']}: cache {'hit' if hit else 'miss'} {fp[:12]}", job_id=state["id"], hit=hit, fingerprint=fp)
        return {"phase": "cache_lookup", "cached": hit, "cache_entry": entry}

    def route_after_cache(state: EngineState) -> str:
        return "hit" if state.get("cached") else "miss"

    def gate(state: EngineState, config: RunnableConfig) -> dict:
        enter(state, "gate", config, mode=state["mode"], cached=state.get("cached", False))
        waiter = config.get("configurable", {}).get("approve_waiter")
        Gate(state["mode"], waiter, timeout_s=deps.gate_timeout_s).check(state["id"], state["request"], log)
        return {"phase": "gate"}

    def execute(state: EngineState, config: RunnableConfig) -> dict:
        enter(state, "execute", config, cached=state.get("cached", False))
        request: ProvisionRequest = state["request"]
        records: list[dict] = []
        cached = bool(state.get("cached")) and state.get("cache_entry") is not None
        high_risk = getattr(request, "risk", "normal") == "high"
        if cached and not high_risk:
            result = deps.cache.replay(state["cache_entry"], deps.sandbox_for(state["id"]))
            rec = step_record(result, 0)
            records.append(rec)
            genui.ui_step(_tracker(config), 0, "cached replay", "ok" if rec["exit_code"] == 0 else "failed", f"exit {rec['exit_code']}")
            log.record("cache", f"{state['id']}: replayed {state['fingerprint'][:12]} exit={rec['exit_code']}",
                       job_id=state["id"], fingerprint=state["fingerprint"], exit_code=rec["exit_code"], elapsed_ms=rec["elapsed_ms"])
            return {"phase": "execute", "steps": records}
        env_extra = deps.display.env() if request.isolated_display else None
        sandbox = deps.sandbox_for(state["id"])
        if high_risk:
            from .containment import run_high_risk

            log.record("note", f"{state['id']}: high-risk request; using the disposable layer ({deps.high_risk_sandbox})",
                       job_id=state["id"], risk="high", preference=deps.high_risk_sandbox)
            run_request = request
            if cached:
                # A cached high-risk script never replays on the host: it runs inside the
                # disposable layer, or the original steps do when it cannot be mapped.
                replay = cached_replay_request(deps.cache, state["cache_entry"], sandbox.root, request)
                if replay is not None:
                    run_request = replay
                    log.record("cache", f"{state['id']}: replaying {state['fingerprint'][:12]} inside the disposable layer",
                               job_id=state["id"], fingerprint=state["fingerprint"], risk="high",
                               command=step_label(replay.steps[0]))
                else:
                    log.record("cache", f"{state['id']}: cached script for {state['fingerprint'][:12]} not mappable; "
                               "re-running the original steps in the disposable layer",
                               job_id=state["id"], fingerprint=state["fingerprint"], risk="high")
            results = run_high_risk(run_request, sandbox.root, deps.high_risk_sandbox, deps.settings_like, log,
                                    sandbox, job_id=state["id"], runner=deps.runner_for_scripts())
            for index, result in enumerate(results):
                step = run_request.steps[index] if index < len(run_request.steps) else None
                rec = step_record(result, index)
                rec["command"] = rec["command"] or step_label(step)
                rec["cwd"] = rec["cwd"] or (step.target_directory if step is not None else "")
                records.append(rec)
                genui.ui_step(_tracker(config), index, rec["command"], "ok" if rec["exit_code"] == 0 else "failed", f"exit {rec['exit_code']}")
                log.record("result", f"{state['id']}: step {index} exit={rec['exit_code']} [high-risk] {rec['command'][:80]}",
                           job_id=state["id"], index=index, command=rec["command"], cwd=rec["cwd"], risk="high",
                           exit_code=rec["exit_code"], elapsed_ms=rec["elapsed_ms"], timed_out=rec["timed_out"],
                           stderr_tail=rec["stderr_tail"][-500:])
            return {"phase": "execute", "steps": records}
        for index, step in enumerate(request.steps):
            label = step_label(step)
            genui.ui_step(_tracker(config), index, label, "running")
            if getattr(step, "script", None) is not None:
                result = deps.runner_for_scripts().run(step.script, sandbox, index, env_extra, request.isolated_display)
            else:
                result = sandbox.run(step, index, env_extra=env_extra, isolated_display=request.isolated_display)
            rec = step_record(result, index)
            rec["command"] = rec["command"] or label
            rec["cwd"] = rec["cwd"] or step.target_directory
            records.append(rec)
            genui.ui_step(_tracker(config), index, label, "failed" if (rec["exit_code"] != 0 or rec["timed_out"]) else "ok",
                          f"exit {rec['exit_code']}" + (" (timeout)" if rec["timed_out"] else ""))
            log.record("result", f"{state['id']}: step {index} exit={rec['exit_code']} {label[:80]}",
                       job_id=state["id"], index=index, command=label, cwd=step.target_directory,
                       exit_code=rec["exit_code"], elapsed_ms=rec["elapsed_ms"], timed_out=rec["timed_out"],
                       stderr_tail=rec["stderr_tail"][-500:])
            if rec["exit_code"] != 0 or rec["timed_out"]:
                break
        return {"phase": "execute", "steps": records}

    def verify(state: EngineState, config: RunnableConfig) -> dict:
        enter(state, "verify", config)
        request: ProvisionRequest = state["request"]
        failed = [s for s in state.get("steps", []) if s["exit_code"] != 0 or s.get("timed_out")]
        if failed:
            f = failed[-1]
            result = {"ok": False, "status": None, "elapsed_ms": 0,
                      "error": f"step {f['index']} exited {f['exit_code']}" + (" (timeout)" if f.get("timed_out") else "")}
        elif request.verify is not None:
            result = dict(deps.verify_target(request.verify))
            result.setdefault("ok", False)
        else:
            result = {"ok": True, "status": None, "elapsed_ms": 0, "error": None}
        log.record("verify", f"{state['id']}: verify {'ok' if result['ok'] else 'failed'}", job_id=state["id"], **result)
        if result["ok"]:
            genui.ui_preview_status(_tracker(config), state.get("label", ""), True, result.get("elapsed_ms") or 0, result.get("status"))
        return {"phase": "verify", "verify": result, "ok": bool(result["ok"]),
                "error": None if result["ok"] else result.get("error")}

    def route_after_verify(state: EngineState) -> str:
        if state.get("ok"):
            return "ok"
        if int(state.get("attempt", 0)) < int(state.get("max_attempts", 0)):
            return "remediate"
        return "exhausted"

    def cache_store(state: EngineState, config: RunnableConfig) -> dict:
        enter(state, "cache_store", config)
        fp = state["fingerprint"]
        if state.get("cached"):
            deps.cache.bump_runs(fp)
            log.record("cache", f"{state['id']}: bumped runs {fp[:12]}", job_id=state["id"], fingerprint=fp)
        else:
            deps.cache.store(fp, state["request"], deps.platform)
            log.record("cache", f"{state['id']}: stored {fp[:12]}", job_id=state["id"], fingerprint=fp, platform=deps.platform)
        return {"phase": "cache_store"}

    def remediate(state: EngineState, config: RunnableConfig) -> dict:
        enter(state, "remediate", config)
        remediator = Remediator(deps.parser, deps.sandbox_for(state["id"]), log, apply_patch=deps.apply_patch)
        update = remediator.remediate(dict(state), platform=deps.platform)
        return {"phase": "remediate", "cache_entry": None, **update}

    def report(state: EngineState, config: RunnableConfig) -> dict:
        enter(state, "report", config, ok=bool(state.get("ok")))
        ok = bool(state.get("ok"))
        error = state.get("error")
        if not ok and not error:
            error = f"failed after {state.get('attempt', 0)} remediation attempt(s)"
        elif not ok and int(state.get("attempt", 0)) >= int(state.get("max_attempts", 0)):
            error = f"{error}; gave up after {state.get('attempt', 0)} remediation attempt(s)"
        log.record("task", f"{state['id']}: {'done' if ok else 'failed'} {state['label']!r}",
                   job_id=state["id"], ok=ok, error=error, attempt=state.get("attempt", 0), cached=state.get("cached", False))
        genui.ui_end(_tracker(config), ok)
        return {"phase": "report", "done": True, "ok": ok, "error": error}

    nodes = {
        "context": context, "validate": validate, "cache_lookup": cache_lookup, "gate": gate,
        "execute": execute, "verify": verify, "cache_store": cache_store, "remediate": remediate, "report": report,
    }
    builder = StateGraph(EngineState)
    for name in GRAPH_NODES:
        builder.add_node(name, nodes[name])

    conditional = {"cache_lookup", "verify"}
    for src, dst in GRAPH_EDGES:
        if src not in conditional:
            builder.add_edge(src, dst)
    builder.add_conditional_edges("cache_lookup", route_after_cache, {"hit": "gate", "miss": "gate"})
    builder.add_conditional_edges("verify", route_after_verify, {"ok": "cache_store", "remediate": "remediate", "exhausted": "report"})
    return builder.compile()


def initial_state(request: ProvisionRequest, job_id: str, mode: GateMode, max_attempts: int) -> EngineState:
    return {
        "id": job_id,
        "label": request.label,
        "phase": "context",
        "attempt": 0,
        "max_attempts": int(max_attempts),
        "cached": False,
        "steps": [],
        "diagnostic": None,
        "verify": None,
        "done": False,
        "ok": False,
        "error": None,
        "request": request,
        "profile": None,
        "fingerprint": "",
        "patches_applied": 0,
        "mode": mode,
        "cache_entry": None,
    }


def run_job(
    graph,
    request: ProvisionRequest,
    job_id: str,
    mode: GateMode,
    approve_waiter: ApproveWaiter | None,
    max_attempts: int | None = None,
    progress=None,
) -> dict:
    state = initial_state(request, job_id, mode, max_attempts or 0)
    tracker: dict = {"phase": "context", "state": dict(state), "progress": progress}
    config: RunnableConfig = {
        "configurable": {"approve_waiter": approve_waiter, "tracker": tracker},
        "recursion_limit": RECURSION_LIMIT,
    }
    try:
        final = graph.invoke(state, config)
        return job_state(final)
    except Exception as e:
        last = dict(tracker.get("state") or state)
        last["phase"] = tracker.get("phase", last.get("phase"))
        last["done"] = True
        last["ok"] = False
        last["error"] = f"{type(e).__name__}: {e}"
        if isinstance(e, QueueDestroyed):
            last["queue_destroyed"] = True
        genui.ui_end(tracker, False)
        graph_log = tracker.get("log")
        if graph_log is not None:
            graph_log.record("task", f"{job_id}: aborted in {last['phase']} ({type(e).__name__})",
                             job_id=job_id, phase=last["phase"], error=last["error"], ok=False)
        return job_state(last)
