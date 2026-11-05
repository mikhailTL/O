"""The fixed computer-use graph.

    context -> plan -> ground -> execute -> judge -> store -> report
                                    ^          |  (fail)
                                    |          v
                                    +-- heal <-+     (attempt < max_heals; else report)

Edges are the literal ``CU_EDGES`` below; nodes only write state. Healing is the
only cycle and is bounded by ``max_heals``. Every transition is recorded.

``plan`` replays a stored procedure when one exists for (app, goal): that path
makes zero model calls and marks the job ``cached``. A requested ``procedure_id``
(``/computer/replay``) pins that exact version even when a newer one exists.
Otherwise the planner (or ``BestOfN`` when ``candidates > 1``) produces a fresh
OScript.

``heal`` asks the planner for ONE replacement step (``replan_step``) and splices it
in with ``replace_step``. When the script being healed is a persisted procedure the
healed script is stored immediately as the next version (``parent`` = the version it
heals), so lineage survives even if the job later fails; ``store`` then writes
nothing for a script that is already on disk. A fresh (never persisted) plan is only
stored once it has passed judgement.

Wave-2 wiring (P1/P2/P4/P5/P7/P8/P9), all inside the nodes above; no node or edge was added:

* ``context`` runs the feasibility gate (``check_feasibility``); an infeasible goal sets
  ``infeasible=True, ok=False, error=reason`` and every later node passes through untouched (no
  planner, no runner, no judge call) until ``judge`` routes it to ``report``. It also seeds the
  task working memory (``WorkingMemory.from_goal``).
* ``plan`` uses the milestone API when the planner has ``plan_milestones`` + ``next_steps``:
  milestones are planned once, the first turn's script comes from ``next_steps`` and the job
  runs in *milestone mode*. A planner without those methods keeps the one-shot ``plan()``.
* with ``step_candidates > 1`` and ``planner.candidate_steps``, ``plan`` samples k scripts for the
  first turn and ``ground`` keeps the first whose targets all resolve in the live tree (a dry a11y
  check, no model call), else candidate 0; later turns pick the same way inside ``execute``.
* ``execute`` in milestone mode loops turn -> run -> ``done_when`` -> memory extract -> next
  milestone, bounded by ``max_turns``; every later turn writes one ``plan`` phase entry. A failed
  step returns to the heal cycle exactly as before (``failed_index`` is relative to the turn's
  script, which is what ``heal`` replaces a step of). A replayed procedure never enters this loop.
* ``judge`` order: executor failure -> milestone verdict (already decided by ``done_when``) ->
  declared ``assert`` (a11y, re-checked live) -> declared ``state_assert`` (``StateJudge.verify``
  with the baseline the runner took before that step) -> VLM goal question only when nothing
  above decided.
* ``heal`` treats a ``loop`` step result as a heal reason and passes the runner's loop evidence
  to ``replan_step(..., loop_evidence=...)`` when the planner accepts it.

Wave 3 (same topology):

* ``context`` skips the feasibility gate when ``plan`` will replay (a requested ``procedure_id`` or an
  existing ``procedures.latest``), so a replay really makes zero model calls.
* ``store`` runs ``deps.interface_builder.build`` once after a FRESH store (never on replay), with
  ``BACKEND_API_DOC`` and the slot-0 backend's display; a failing or skipping builder never changes ``ok``.
* memory extraction uses the planner client's ``local_only()`` view when it has one, so a vLLM outage
  never turns into a CLI-fallback call per turn; with no local backend extraction is skipped.

Cost bounds (real run: 84 paid Claude calls across two jobs, mostly per-turn replans + screen checks):

* ``run_cu_job`` opens a ``cu.meter.JobMeter`` capped at ``max_model_calls`` paid calls (setting
  ``cuMaxModelCalls``, default 12). At the cap no further paid call is made: ``judge`` routes to
  ``report`` instead of ``heal``, the milestone loop stops before planning another turn, and a call
  refused mid-node ends the job as a report. ``model_calls`` / ``cost_usd`` / ``turns`` are in every
  JobState.
* milestone ``done_when`` questions and ``screenshot_assert`` steps are non-final judge questions
  (``Judge.judge(..., tree=..., final=False)``): local VLM, else the focused window's a11y text, else
  deferred. When anything was deferred, one final goal check (``final=True``, the only screen check
  allowed to reach the paid fallback) decides the job before it is reported ok.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from .. import genui
from ..actionlog import ActionLog
from . import meter as cu_meter
from .feasibility import check_feasibility
from .memory import WorkingMemory, extract as extract_memory
from .treeutil import TREE_SUMMARY_LINES, _get, summarize_tree  # noqa: F401  (re-exported)

TAIL_CHARS = 4000
MAX_HEALS_CEILING = 20
RECURSION_LIMIT = 16 + 4 * MAX_HEALS_CEILING
NOTE_LINES = 40
DEFAULT_STEPS_PER_TURN = 5
DEFAULT_MAX_TURNS = 20

# What the interface builder (P7) is told about the backend object its generated api.py may use.
BACKEND_API_DOC = (
    "backend.tree(window=None) -> list[Node]: the accessibility tree (Node: id, role, name, text, rect, states, children).\n"
    "backend.find(target, within=None) -> Node | None: first node matching Target(role, name glob, text, index, within).\n"
    "backend.invoke(node, action): run an accessibility action (e.g. 'click', 'press', 'activate') on a node.\n"
    "backend.set_value(node, value): set a node's value (text fields, cells, spin buttons).\n"
    "Only these four attributes of `backend` may be used."
)

CU_PHASES: tuple[str, ...] = ("context", "plan", "ground", "execute", "judge", "heal", "store", "report")

CU_NODES: tuple[str, ...] = ("context", "plan", "ground", "execute", "judge", "heal", "store", "report")

CU_EDGES: tuple[tuple[str, str], ...] = (
    (START, "context"),
    ("context", "plan"),
    ("plan", "ground"),
    ("ground", "execute"),
    ("execute", "judge"),
    ("judge", "store"),
    ("judge", "heal"),
    ("judge", "report"),
    ("heal", "execute"),
    ("store", "report"),
    ("report", END),
)

JOBSTATE_FIELDS: tuple[str, ...] = (
    "id", "label", "phase", "attempt", "max_attempts", "cached", "steps",
    "diagnostic", "verify", "done", "ok", "error",
    "procedure_id", "candidates", "scores", "infeasible",
    "turns", "model_calls", "cost_usd",
)


class CUState(TypedDict, total=False):
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
    procedure_id: Optional[str]
    candidates: int
    scores: list[dict]
    goal: str
    app: str
    script: Any
    procedure: Any
    parent_id: Optional[str]
    notes_text: str
    tree_summary: str
    tree_summaries: list[str]
    ground: Optional[dict]
    run: Optional[dict]
    failed_index: Optional[int]
    heal_reason: Optional[str]
    healed_step: Optional[int]
    healed_reason: Optional[str]
    persisted: bool  # the current ``script`` is already a stored procedure version (replay or healed)
    requested_procedure_id: Optional[str]
    infeasible: bool
    memory: Any  # WorkingMemory
    milestones: Optional[list]  # milestone mode when not None
    milestone_index: int
    turns: int  # next_steps calls made so far (the plan node's counts as turn 1)
    turn_scripts: list  # scripts of the turns that passed, in order (-> the stored procedure)
    steps_before_turn: int  # len(steps) when the current turn began (re-execution drops the rest)
    storable: bool  # False when the milestone turns could not be combined into one valid OScript
    turn_candidates: Optional[list]  # k candidate scripts for turn 1, picked by ``ground``


@dataclass
class CUDeps:
    backend_factory: Callable[[int], Any]
    grounder: Any
    judge: Any
    planner: Any
    procedures: Any
    notes: Any
    log: ActionLog
    max_heals: int = 3
    candidates: int = 1
    displays: list[str] = field(default_factory=lambda: ["host"])
    locator_cache: Any = None
    ui: Any = None  # genui.UIStore; frames are emitted on job:<id> when set
    # ---- wave 2 (all optional; server.py may construct CUDeps without them)
    feasibility: Any = None  # FeasibilityGate; None -> every goal is feasible, no probes
    state_judge_factory: Optional[Callable[[Any, Any], Any]] = None  # (sandbox, backend) -> StateJudge
    interface_store: Any = None  # InterfaceStore (api.py / api.json per app)
    runner: Any = None  # runners.Runner for ``script`` steps
    sandbox_for: Optional[Callable[[str], Any]] = None  # job_id -> Sandbox
    steps_per_turn: int = DEFAULT_STEPS_PER_TURN
    max_turns: int = DEFAULT_MAX_TURNS
    step_candidates: int = 1
    comparative: bool = False  # P9: BestOfN ranks all candidates in one model call
    # ---- wave 3 (I4 / P7): InterfaceBuilder; ``store`` runs one bounded build after a fresh store
    interface_builder: Any = None


# ---------------------------------------------------------------- helpers

def _node_dict(node: Any) -> dict:
    out = {k: _get(node, k) for k in ("id", "role", "name", "text", "rect", "states")}
    out["states"] = list(out["states"] or [])
    kids = _get(node, "children") or []
    out["children"] = [_node_dict(c) for c in kids]
    return out


def _script_steps(script: Any) -> list:
    return list(_get(script, "steps") or [])


def _step_text(step: Any) -> str:
    op = _get(step, "op") or "?"
    bits = [str(op)]
    for key in ("cmd", "ref", "text", "keys", "value", "action", "question", "as", "as_"):
        val = _get(step, key)
        if val not in (None, ""):
            bits.append(f"{key.rstrip('_')}={str(val)[:60]!r}")
    for key in ("target", "for", "for_"):
        tgt = _get(step, key)
        if tgt is not None:
            role = _get(tgt, "role")
            name = _get(tgt, "name")
            bits.append(f"{key.rstrip('_')}={role}:{name}")
    return " ".join(bits)


def _step_targets(step: Any) -> list:
    out = []
    for key in ("target", "for", "for_"):
        tgt = _get(step, key)
        if tgt is not None:
            out.append(tgt)
    return out


def step_record(result: Any, index: int | None = None, script_step: Any = None) -> dict:
    """One JobState ``steps`` row from one ScriptRunner step row (dict or object)."""
    idx = _get(result, "index")
    ok = _get(result, "ok")
    if ok is None:
        ok = _get(result, "passed", True)
    error = _get(result, "error") or _get(result, "reason") or ""
    detail = _get(result, "note") or _get(result, "detail") or _get(result, "stdout") or ""
    resolved_by = _get(result, "resolved_by")
    if resolved_by and not detail:
        detail = f"resolved_by={resolved_by}"
    command = _get(result, "command") or _get(result, "summary") or ""
    if not command:
        command = _step_text(script_step) if script_step is not None else str(_get(result, "op") or "")
    exit_code = _get(result, "exit_code")
    if exit_code is None:
        exit_code = 0 if ok else 1
    row = {
        "index": int(idx) if idx is not None else index,
        "command": str(command),
        "cwd": str(_get(result, "cwd") or _get(result, "app") or ""),
        "exit_code": int(exit_code),
        "stdout_tail": str(detail)[-TAIL_CHARS:],
        "stderr_tail": str(error)[-TAIL_CHARS:],
        "elapsed_ms": int(_get(result, "elapsed_ms", 0) or 0),
        "timed_out": bool(_get(result, "timed_out", False)),
    }
    status = _get(result, "status")
    reason = _get(result, "reason")
    if status == "loop" or reason == "loop":
        row["status"] = "loop"
        row["reason"] = "loop"
        row["evidence"] = [dict(e) for e in (_get(result, "evidence") or ())]
    return row


def _run_is_loop(run: Any, records: list[dict]) -> bool:
    """True when the run stopped on the loop breaker (``StepResult.status == "loop"``)."""
    failed = _get(run, "failed_index")
    if failed is None or not 0 <= int(failed) < len(records):
        return False
    return records[int(failed)].get("status") == "loop"


def run_records(run: Any, script: Any) -> list[dict]:
    steps = _script_steps(script)
    rows: list[dict] = []
    for index, row in enumerate(list(_get(run, "steps") or [])):
        rows.append(step_record(row, index, steps[index] if index < len(steps) else None))
    return rows


def _judge_answer(judge: Any, png: bytes | None, question: str, tree: Any = None, final: bool = True) -> dict:
    """Normalise whatever the judge returns to ``{ok, reason, source}``. ``tree``/``final`` reach a judge
    that accepts them (``cu.judge.Judge``); other judges are asked the plain question."""
    for method in ("judge", "ask", "check", "__call__"):
        fn = getattr(judge, method, None)
        if callable(fn):
            break
    else:
        return {"ok": True, "reason": "no judge", "source": "none"}
    if _accepts_kwarg(fn, "final") and _accepts_kwarg(fn, "tree"):
        raw = fn(png, question, tree=tree, final=final)
    else:
        raw = fn(png, question)
    if isinstance(raw, tuple):
        ok_val = bool(raw[0]) if raw else False
        return {"ok": ok_val, "reason": str(raw[1]) if len(raw) > 1 else "", "source": getattr(judge, "last_source", "judge")}
    if isinstance(raw, bool):
        return {"ok": raw, "reason": "", "source": getattr(judge, "last_source", "judge")}
    ok = _get(raw, "ok")
    if ok is None:
        ok = _get(raw, "passed")
    if ok is None:
        ok = _get(raw, "yes", False)
    return {
        "ok": bool(ok),
        "reason": str(_get(raw, "reason") or ""),
        "source": str(_get(raw, "source") or getattr(judge, "last_source", "judge")),
    }


def _procedure_id(stored: Any) -> Optional[str]:
    if stored is None:
        return None
    if isinstance(stored, str):
        return stored
    for key in ("procedure_id", "id", "path"):
        val = _get(stored, key)
        if val:
            return str(val)
    return None


def _procedure_script(store: Any, proc: Any) -> Any:
    """The OScript behind a stored procedure: ``store.load_script(proc)`` when the store separates
    metadata from the script (ProcedureStore), else the procedure's own ``script``/``steps``."""
    script = _get(proc, "script")
    if script is not None:
        return script
    load = getattr(store, "load_script", None)
    if callable(load):
        return load(proc)
    if _get(proc, "steps") is not None:
        return proc
    return None


def _lookup_procedure(store: Any, app: str, goal: str, requested_id: Optional[str], log: ActionLog, job_id: str) -> Any:
    """The procedure ``plan`` replays: ``store.get(requested_id)`` when a version was asked for (a replay of
    v1 stays v1 even when v2 exists), else ``store.latest(app, goal)``. An unknown requested id falls back
    to latest and is logged, never fatal."""
    if store is None:
        return None
    if requested_id:
        get = getattr(store, "get", None)
        found = get(requested_id) if callable(get) else None
        if found is not None:
            return found
        log.record("note", f"{job_id}: requested procedure {requested_id} not found; using latest",
                   job_id=job_id, procedure_id=requested_id, app=app)
    return store.latest(app, goal)


def _unpack_best(result: Any) -> tuple[Any, list[dict]]:
    """``BestOfN.run`` → (winning script, per-candidate score rows)."""
    if isinstance(result, tuple) and len(result) == 2:
        script, scores = result
    else:
        script = _get(result, "script")
        if script is None:
            script = _get(result, "winner")
        scores = _get(result, "scores") or _get(result, "candidates") or []
        if script is None and _get(result, "steps") is not None:
            script = result
    errors = [_get(c, "error") for c in (_get(result, "candidates") or [])] if not isinstance(result, tuple) else []
    rows: list[dict] = []
    for i, s in enumerate(list(scores or [])):
        if isinstance(s, (int, float)):
            rows.append({"candidate": i, "score": float(s)})
        elif isinstance(s, dict):
            rows.append({"candidate": s.get("candidate", i), **{k: v for k, v in s.items() if k != "candidate"}})
        else:
            rows.append({"candidate": _get(s, "candidate", i), "score": float(_get(s, "score", 0.0) or 0.0)})
        if i < len(errors) and errors[i]:
            rows[-1]["error"] = str(errors[i])
    return script, rows


def _goal_question(goal: str) -> str:
    try:
        from .planner import goal_question
    except ImportError:
        return f"Has this goal been achieved: {goal}?"
    fn = getattr(goal_question, "__call__", None)
    return goal_question(goal) if callable(fn) else f"Has this goal been achieved: {goal}?"


def _screenshot(backend: Any) -> bytes | None:
    fn = getattr(backend, "screenshot", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:
        return None


def _as_target(obj: Any) -> Any:
    """A dict ``done_when``/target becomes a schema ``Target`` when the schema is importable."""
    if not isinstance(obj, dict):
        return obj
    try:
        from .schema import Target
    except ImportError:
        return obj
    data = {k: v for k, v in obj.items() if k not in ("state", "states")}
    try:
        return Target.model_validate(data)
    except Exception:
        return obj


def _a11y_check(backend: Any, target: Any, state: Any = None) -> dict:
    """``{ok, reason, source="a11y"}`` for one target against the live tree; no model call."""
    find = getattr(backend, "find", None)
    if not callable(find):
        return {"ok": False, "reason": "backend has no find()", "source": "a11y"}
    if state is None and isinstance(target, dict):
        state = target.get("state") or target.get("states")
    try:
        node = find(_as_target(target))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"find failed: {type(e).__name__}: {e}", "source": "a11y"}
    try:
        from .judge import a11y_assert
    except ImportError:
        a11y_assert = None
    if a11y_assert is not None:
        v = a11y_assert(node, target, state)
        return {"ok": bool(v.ok), "reason": str(v.reason), "source": "a11y"}
    if node is None:
        return {"ok": False, "reason": "element not found in accessibility tree", "source": "a11y"}
    return {"ok": True, "reason": "element present", "source": "a11y"}


def _has_milestone_api(planner: Any) -> bool:
    return callable(getattr(planner, "plan_milestones", None)) and callable(getattr(planner, "next_steps", None))


def _accepts_kwarg(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _call_supported(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` with only the keyword arguments its signature accepts (duck-typed planner API)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


def _interfaces_for(store: Any, app: str) -> tuple:
    fn = getattr(store, "functions", None)
    if not callable(fn):
        return ()
    try:
        return tuple(fn(app) or ())
    except Exception:  # noqa: BLE001
        return ()


def _step_dict(step: Any) -> Any:
    dump = getattr(step, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True, exclude_none=True)
    return step


def _rename_refs(turn_steps: list, bound: set[str], suffix: str) -> list:
    """Step dicts of one turn with every ``as`` name that is already bound renamed to ``<name><suffix>``
    (and its ``ref``/``within`` uses with it), so turns concatenate into one valid OScript."""
    mapping: dict[str, str] = {}
    out: list = []
    for raw in turn_steps:
        step = dict(raw) if isinstance(raw, dict) else raw
        if not isinstance(step, dict):
            out.append(step)
            continue
        for key in ("ref",):
            if step.get(key) in mapping:
                step[key] = mapping[step[key]]
        for key in ("target", "for"):
            tgt = step.get(key)
            if isinstance(tgt, dict) and tgt.get("within") in mapping:
                step[key] = {**tgt, "within": mapping[tgt["within"]]}
        name = step.get("as")
        if isinstance(name, str):
            if name in bound:
                new = f"{name}{suffix}"[:64]
                mapping[name] = new
                step["as"] = new
            bound.add(step["as"])
        out.append(step)
    return out


def _combine_scripts(planner: Any, app: str, goal: str, scripts: list) -> tuple[Any, bool]:
    """One script holding every turn's steps in order (what ``store`` persists for replay).

    Refs a later turn rebinds are renamed (``cell`` -> ``cell_t2``). Returns ``(script, True)`` when
    the combination validates, else ``(last script, False)`` and nothing is stored."""
    if not scripts:
        return None, False
    if len(scripts) == 1:
        return scripts[0], True
    parse = getattr(planner, "parse_script", None)
    if callable(parse):
        bound: set[str] = set()
        steps: list = []
        for k, sc in enumerate(scripts, start=1):
            steps.extend(_rename_refs([_step_dict(s) for s in _script_steps(sc)], bound, f"_t{k}"))
        try:
            return parse(json.dumps({"app": app, "goal": goal, "steps": steps})), True
        except Exception:  # noqa: BLE001 - fall through to the constructor form
            pass
    steps = [s for sc in scripts for s in _script_steps(sc)]
    try:
        return scripts[-1].__class__(app=app, goal=goal, steps=tuple(steps)), True
    except Exception:  # noqa: BLE001
        return scripts[-1], False


def _cap_reason(m: Any) -> str:
    return (f"stopped: engine-side model-call cap reached ({m.paid_calls} of cuMaxModelCalls={m.max_paid} "
            f"Claude calls, ${m.cost_usd:.3f}); raise cuMaxModelCalls or run a local vLLM to continue")


def job_state(state: dict) -> dict:
    out = {k: state.get(k) for k in JOBSTATE_FIELDS}
    m = cu_meter.current()
    if m is not None:
        out["model_calls"] = m.snapshot()
        out["cost_usd"] = round(m.cost_usd, 6)
    else:
        out["model_calls"] = dict(out["model_calls"] or {})
        out["cost_usd"] = float(out["cost_usd"] or 0.0)
    out["turns"] = int(out["turns"] or 0)
    out["steps"] = list(out["steps"] or [])
    out["scores"] = list(out["scores"] or [])
    out["cached"] = bool(out["cached"])
    out["infeasible"] = bool(out["infeasible"])
    out["done"] = bool(out["done"])
    out["ok"] = bool(out["ok"])
    out["attempt"] = int(out["attempt"] or 0)
    out["candidates"] = int(out["candidates"] or 1)
    return out


def _tracker(config: RunnableConfig) -> dict:
    return config.get("configurable", {}).setdefault("tracker", {})


# ---------------------------------------------------------------- graph

def build_cu_graph(deps: CUDeps):
    log = deps.log

    def enter(state: CUState, phase: str, config: RunnableConfig, **detail: Any) -> None:
        log.record("graph", f"{state['id']}: {phase}", job_id=state["id"], phase=phase, attempt=state.get("attempt", 0), **detail)
        tracker = _tracker(config)
        tracker["phase"] = phase
        tracker["state"] = {**state, "phase": phase}
        tracker["log"] = log
        genui.job_surface(deps.ui, tracker, state["id"], state.get("label", ""), CU_PHASES)
        genui.ui_phase(tracker, phase, state.get("attempt", 0))
        progress = tracker.get("progress")
        if callable(progress):
            progress(job_state({**state, "phase": phase}))

    def sandbox_for(job_id: str) -> Any:
        if not callable(deps.sandbox_for):
            return None
        try:
            return deps.sandbox_for(job_id)
        except Exception as e:  # noqa: BLE001
            log.record("note", f"{job_id}: sandbox unavailable ({type(e).__name__}: {e})", job_id=job_id)
            return None

    def state_judge_for(job_id: str, backend: Any, sandbox: Any = None) -> Any:
        if not callable(deps.state_judge_factory):
            return None
        try:
            return deps.state_judge_factory(sandbox if sandbox is not None else sandbox_for(job_id), backend)
        except Exception as e:  # noqa: BLE001
            log.record("note", f"{job_id}: state judge unavailable ({type(e).__name__}: {e})", job_id=job_id)
            return None

    def runner(slot: int, job_id: str = ""):
        from .executor import ScriptRunner

        backend = deps.backend_factory(slot)
        sandbox = sandbox_for(job_id) if job_id else None
        # The script display is the backend's own (an isolated one, or None): ``deps.displays`` may
        # name "host", which a script must never receive.
        return ScriptRunner(backend, log, grounder=deps.grounder, judge=deps.judge,
                            locator_cache=deps.locator_cache,
                            state_judge=state_judge_for(job_id, backend, sandbox) if job_id else None,
                            runner=deps.runner, sandbox=sandbox,
                            display=getattr(backend, "display", None), interface_store=deps.interface_store)

    def tree_summary(slot: int, window: Any = None) -> str:
        backend = deps.backend_factory(slot)
        try:
            return summarize_tree(backend.tree(window) if window is not None else backend.tree())
        except Exception as e:
            return f"(tree unavailable: {type(e).__name__}: {e})"

    def has_stored_procedure(state: CUState) -> bool:
        """True when ``plan`` will replay: a requested version, or ``procedures.latest(app, goal)`` exists."""
        if state.get("requested_procedure_id"):
            return True
        if deps.procedures is None:
            return False
        try:
            return deps.procedures.latest(state["app"], state["goal"]) is not None
        except Exception:  # noqa: BLE001 - a broken store is plan's problem; gate as a fresh goal
            return False

    def context(state: CUState, config: RunnableConfig) -> dict:
        enter(state, "context", config, app=state["app"], candidates=state.get("candidates", 1))
        notes_text = ""
        if deps.notes is not None:
            try:
                notes_text = str(deps.notes.for_prompt(state["app"]) or "")
            except Exception as e:
                log.record("note", f"{state['id']}: interface notes unavailable ({type(e).__name__})", job_id=state["id"])
        n = max(1, int(state.get("candidates") or 1))
        summaries = [tree_summary(i) for i in range(n)]
        max_heals = int(state.get("max_attempts") or deps.max_heals)
        memory = state.get("memory") or WorkingMemory.from_goal(state["goal"])
        out = {"phase": "context", "notes_text": notes_text, "tree_summary": summaries[0],
               "tree_summaries": summaries, "max_attempts": max_heals, "memory": memory, "infeasible": False}
        if has_stored_procedure(state):
            # I3 / P1: a replay makes zero model calls, so the gate (one model question) is for fresh goals only.
            log.record("note", f"{state['id']}: feasibility skipped: stored procedure", job_id=state["id"],
                       app=state["app"], procedure_id=state.get("requested_procedure_id"))
            return out
        verdict = check_feasibility(deps.feasibility, state["goal"], state["app"], state["id"])
        if not bool(_get(verdict, "feasible", True)):
            reason = str(_get(verdict, "reason") or "goal judged infeasible")
            # The gate already wrote the ``note`` entry; nothing downstream acts on an infeasible job.
            out.update({"infeasible": True, "ok": False, "error": reason, "verify": {
                "ok": False, "error": reason, "reason": reason, "source": "feasibility", "goal": False}})
        return out

    def plan(state: CUState, config: RunnableConfig) -> dict:
        enter(state, "plan", config, candidates=state.get("candidates", 1))
        if state.get("infeasible"):
            return {"phase": "plan", "script": None, "scores": [], "milestones": None}
        app, goal = state["app"], state["goal"]
        procedure = _lookup_procedure(deps.procedures, app, goal, state.get("requested_procedure_id"), log, state["id"])
        if procedure is not None:
            script = _procedure_script(deps.procedures, procedure)
            pid = _procedure_id(procedure)
            log.record("cache", f"{state['id']}: replaying procedure {pid}", job_id=state["id"], hit=True, procedure_id=pid, app=app)
            return {"phase": "plan", "cached": True, "script": script, "procedure": procedure,
                    "procedure_id": pid, "parent_id": pid, "scores": [], "persisted": True}
        log.record("cache", f"{state['id']}: no procedure for {app!r}", job_id=state["id"], hit=False, app=app)
        n = max(1, int(state.get("candidates") or 1))
        if n > 1:
            from .planner import BestOfN

            # RunResult carries no screenshot, so the final screen of each slot is captured right after
            # its run and handed to BestOfN through a callable judge (BestOfN accepts either form).
            shots: dict[int, bytes | None] = {}

            def run_fn(script: Any, slot: int) -> Any:
                result = runner(slot).run(script, f"{state['id']}-c{slot}")
                shots[id(result)] = _screenshot(deps.backend_factory(slot))
                return result

            def goal_judge(script: Any, result: Any, goal_text: str) -> bool:
                png = shots.get(id(result))
                if png is None or deps.judge is None:
                    return bool(_get(result, "ok", False))
                return _judge_answer(deps.judge, png, _goal_question(goal_text))["ok"]

            best = BestOfN(n, deps.planner, run_fn, goal_judge, comparative=deps.comparative).run(goal, app, list(state.get("tree_summaries") or []))
            script, scores = _unpack_best(best)
            log.record("graph", f"{state['id']}: best-of-{n} picked a candidate", job_id=state["id"], phase="plan", scores=scores)
            return {"phase": "plan", "cached": False, "script": script, "scores": scores, "procedure": None,
                    "parent_id": None, "persisted": False}
        if _has_milestone_api(deps.planner):
            memory = state.get("memory") or WorkingMemory.from_goal(goal)
            milestones = list(_call_supported(deps.planner.plan_milestones, goal, app, state.get("notes_text", ""),
                                              memory=memory) or [])
            if milestones:
                log.record("graph", f"{state['id']}: planned {len(milestones)} milestone(s)", job_id=state["id"],
                           phase="plan", milestones=[str(_get(m, "goal") or "")[:120] for m in milestones])
                script = next_turn_script(state, milestones[0], 0, memory, 1, defer_pick=True)
                candidates = script if isinstance(script, list) else None
                if candidates:
                    script = candidates[0]
                return {"phase": "plan", "cached": False, "script": script, "scores": [], "procedure": None,
                        "turn_candidates": candidates,
                        "parent_id": None, "persisted": False, "milestones": milestones, "milestone_index": 0,
                        "turns": 1, "turn_scripts": [], "steps_before_turn": 0, "storable": True, "memory": memory}
            log.record("note", f"{state['id']}: planner returned no milestones; one-shot plan", job_id=state["id"])
        script = deps.planner.plan(goal, app, state.get("tree_summary", ""))
        return {"phase": "plan", "cached": False, "script": script, "scores": [], "procedure": None,
                "parent_id": None, "persisted": False}

    def next_turn_script(state: CUState, milestone: Any, index: int, memory: Any, turn: int,
                         defer_pick: bool = False) -> Any:
        """``planner.next_steps`` for one turn, or the pick among k ``candidate_steps`` when configured.
        With ``defer_pick`` (turn 1) the candidates are returned as a list for the ``ground`` node to pick."""
        app = state["app"]
        interfaces = _interfaces_for(deps.interface_store, app)
        notes_text = state.get("notes_text", "")
        summary = tree_summary(0)
        k = int(deps.step_candidates or 1)
        candidates = getattr(deps.planner, "candidate_steps", None)
        if k > 1 and callable(candidates):
            scripts = list(_call_supported(candidates, milestone, summary, memory, notes_text, interfaces=interfaces,
                                           max_steps=deps.steps_per_turn, k=k, app=app) or [])
            if scripts and defer_pick:
                return scripts
            if scripts:
                chosen, picked = pick_candidate(scripts)
                log.record("graph", f"{state['id']}: turn {turn}: candidate {picked} of {len(scripts)} for milestone {index}",
                           job_id=state["id"], phase="ground", turn=turn, milestone=index, candidate=picked,
                           candidates=len(scripts))
                return chosen
        return _call_supported(deps.planner.next_steps, milestone, summary, memory, notes_text, interfaces=interfaces,
                               max_steps=deps.steps_per_turn, app=app)

    def pick_candidate(scripts: list) -> tuple[Any, int]:
        """P9 per-step: the first candidate whose targets all resolve in the live tree and whose
        ``assert`` steps hold now (a dry a11y check, no model call); else candidate 0."""
        backend = deps.backend_factory(0)
        for i, sc in enumerate(scripts):
            ok = True
            for step in _script_steps(sc):
                state_ = _get(step, "state") if (_get(step, "op") == "assert") else None
                for tgt in _step_targets(step):
                    if not _a11y_check(backend, tgt, state_)["ok"]:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                return sc, i
        return scripts[0], 0

    def ground(state: CUState, config: RunnableConfig) -> dict:
        enter(state, "ground", config, cached=state.get("cached", False))
        if state.get("infeasible"):
            return {"phase": "ground", "ground": None}
        script = state.get("script")
        if script is None:
            raise RuntimeError("plan produced no script")
        out: dict = {"phase": "ground"}
        candidates = state.get("turn_candidates")
        if candidates:
            # P9 per-step: the first turn's k candidates are judged here, on the live tree.
            script, picked = pick_candidate(list(candidates))
            log.record("graph", f"{state['id']}: turn 1: candidate {picked} of {len(candidates)} for milestone 0",
                       job_id=state["id"], phase="ground", turn=1, milestone=0, candidate=picked, candidates=len(candidates))
            out.update({"script": script, "turn_candidates": None})
        backend = deps.backend_factory(0)
        find = getattr(backend, "find", None)
        total = resolved = 0
        for step in _script_steps(script):
            for tgt in _step_targets(step):
                total += 1
                if callable(find):
                    try:
                        if find(tgt) is not None:
                            resolved += 1
                    except Exception:
                        pass
        result = {"targets": total, "via_tree": resolved, "needs_vision": total - resolved,
                  "grounder": bool(deps.grounder is not None)}
        log.record("graph", f"{state['id']}: ground {resolved}/{total} targets via accessibility tree",
                   job_id=state["id"], phase="ground", **result)
        out["ground"] = result
        return out

    def run_once(state: CUState, config: RunnableConfig, script: Any, offset: int = 0) -> tuple[Any, list[dict], dict]:
        """Run ``script`` on slot 0; returns (runner, records, run dict with loop/baseline evidence)."""
        rn = runner(0, state["id"])
        run = rn.run(script, state["id"])
        records = run_records(run, script)
        for rec in records:
            genui.ui_step(_tracker(config), offset + int(rec["index"] or 0), rec["command"],
                          "ok" if rec["exit_code"] == 0 and not rec["timed_out"] else "failed", f"exit {rec['exit_code']}")
        failed_index = _get(run, "failed_index")
        ok = bool(_get(run, "ok", False))
        error = None
        if failed_index is not None and 0 <= int(failed_index) < len(records):
            error = records[int(failed_index)].get("stderr_tail") or ""
        loop = _run_is_loop(run, records)
        evidence_fn = getattr(rn, "loop_evidence", None)
        baseline_fn = getattr(rn, "baseline", None)
        info = {
            "ok": ok, "failed_index": failed_index, "error": error, "loop": loop,
            "loop_evidence": list(evidence_fn() or []) if (loop and callable(evidence_fn)) else [],
            "baselines": {int(rec["index"] or i): baseline_fn(int(rec["index"] or i)) for i, rec in enumerate(records)}
            if callable(baseline_fn) else {},
        }
        log.record("result", f"{state['id']}: executed {len(records)} step(s) {'ok' if ok else ('loop' if loop else 'failed')}",
                   job_id=state["id"], ok=ok, steps=len(records), failed_index=failed_index, loop=loop)
        return rn, records, info

    def judge_done_when(state: CUState, milestone: Any, index: int) -> dict:
        """``{ok, reason, source}`` for a milestone's ``done_when``: an a11y target through the
        backend's tree (no model call), or a question through the VLM judge."""
        done_when = _get(milestone, "done_when")
        if done_when is None:
            return {"ok": True, "reason": "milestone declares no done_when", "source": "none"}
        if isinstance(done_when, str):
            if deps.judge is None:
                return {"ok": True, "reason": "no judge configured", "source": "none"}
            backend = deps.backend_factory(0)
            return _judge_answer(deps.judge, _screenshot(backend), done_when, tree=backend.tree, final=False)
        return _a11y_check(deps.backend_factory(0), done_when)

    def remember(state: CUState, memory: Any, records: list[dict], turn: int) -> Any:
        """``memory.extract`` after a judged step through the planner's client, local model only: a client
        exposing ``local_only()`` (``planner.ModelClient``) is narrowed to that view so a vLLM outage never
        falls through to the slow CLI fallback; with no local backend extraction is skipped. Failures keep memory."""
        model = getattr(deps.planner, "client", None)
        local_only = getattr(model, "local_only", None)
        if callable(local_only):
            model = local_only()
            if model is None:
                log.record("note", f"{state['id']}: memory extract skipped: no local model (fallback only)",
                           job_id=state["id"], turn=turn)
        if model is not None and callable(getattr(model, "complete_json", None)) and records:
            memory = extract_memory(memory, records[-1], tree_summary(0), model)
        detail = memory.to_log_detail() if hasattr(memory, "to_log_detail") else {}
        log.record("note", f"{state['id']}: working memory after turn {turn}", job_id=state["id"], turn=turn, **detail)
        return memory

    def execute(state: CUState, config: RunnableConfig) -> dict:
        enter(state, "execute", config, cached=state.get("cached", False))
        if state.get("infeasible"):
            return {"phase": "execute", "steps": [], "run": None, "failed_index": None}
        script = state["script"]
        milestones = state.get("milestones")
        if milestones is None:
            _, records, info = run_once(state, config, script)
            return {"phase": "execute", "steps": records, "run": info, "failed_index": info["failed_index"]}
        return execute_milestones(state, config, script, list(milestones))

    def execute_milestones(state: CUState, config: RunnableConfig, script: Any, milestones: list) -> dict:
        """P1: turn -> run -> done_when -> memory -> next milestone, bounded by ``max_turns``."""
        job_id = state["id"]
        mi = int(state.get("milestone_index") or 0)
        turn = max(1, int(state.get("turns") or 1))
        memory = state.get("memory") or WorkingMemory.from_goal(state["goal"])
        passed = list(state.get("turn_scripts") or [])
        kept = int(state.get("steps_before_turn") or 0)
        steps = list(state.get("steps") or [])[:kept]
        max_turns = max(1, int(deps.max_turns or 1))
        decided: dict | None = None
        while True:
            _, records, info = run_once(state, config, script, offset=len(steps))
            steps_before = len(steps)
            steps.extend(records)
            if not info["ok"]:
                return {"phase": "execute", "steps": steps, "run": info, "failed_index": info["failed_index"],
                        "script": script, "milestone_index": mi, "turns": turn, "memory": memory,
                        "turn_scripts": passed, "steps_before_turn": steps_before}
            passed.append(script)
            verdict = judge_done_when(state, milestones[mi], mi)
            log.record("verify", f"{job_id}: milestone {mi} {'done' if verdict['ok'] else 'not done'} after turn {turn}",
                       job_id=job_id, milestone=mi, turn=turn, **verdict)
            memory = remember(state, memory, records, turn)
            if verdict["ok"]:
                mi += 1
                if mi >= len(milestones):
                    decided = {"ok": True, "reason": f"{len(milestones)} milestone(s) done", "source": verdict["source"]}
                    break
            m = cu_meter.current()
            if m is not None and m.exhausted and not verdict["ok"]:
                decided = {"ok": False, "source": "budget", "cap": True,
                           "reason": f"milestone {mi} not done after {turn} turn(s); {_cap_reason(m)}"}
                break
            if turn >= max_turns:
                decided = {"ok": False, "source": verdict["source"],
                           "reason": f"milestone {mi} ({str(_get(milestones[mi], 'goal') or '')[:80]}) not done after "
                                     f"{turn} turn(s): {verdict['reason'][:200]}"}
                break
            turn += 1
            enter(state, "plan", config, turn=turn, milestone=mi)
            steps_before = len(steps)
            script = next_turn_script(state, milestones[mi], mi, memory, turn)
        m = cu_meter.current()
        if decided["ok"] and m is not None and m.deferred:
            decided = final_goal_check(state, decided, m)
        combined, storable = _combine_scripts(deps.planner, state["app"], state["goal"], passed)
        if not storable:
            log.record("note", f"{job_id}: milestone turns do not combine into one procedure; nothing will be stored",
                       job_id=job_id, turns=len(passed))
        info = {**info, "decided": decided}
        return {"phase": "execute", "steps": steps, "run": info, "failed_index": None,
                "script": combined if (decided["ok"] and storable) else script, "milestone_index": mi,
                "turns": turn, "memory": memory, "turn_scripts": passed, "steps_before_turn": steps_before,
                "storable": storable}

    def final_goal_check(state: CUState, decided: dict, m: Any) -> dict:
        """Some milestone or screen check was deferred (no local judge): one final goal question decides."""
        if deps.judge is None:
            return decided
        backend = deps.backend_factory(0)
        try:
            answer = _judge_answer(deps.judge, _screenshot(backend), _goal_question(state["goal"]),
                                   tree=backend.tree, final=True)
        except cu_meter.ModelCallCapReached:
            return {"ok": False, "source": "budget", "cap": True,
                    "reason": f"{decided['reason']}, but the final goal check could not run; {_cap_reason(m)}"}
        log.record("verify", f"{state['id']}: final goal check {'passed' if answer['ok'] else 'failed'} "
                   f"({m.deferred} check(s) deferred)", job_id=state["id"], deferred=m.deferred, **answer)
        if answer["ok"]:
            return {"ok": True, "source": answer["source"], "reason": f"{decided['reason']}; final goal check: {answer['reason']}"}
        return {"ok": False, "source": answer["source"], "reason": f"final goal check failed: {answer['reason']}"}

    def declared_verdict(state: CUState, run: dict) -> dict | None:
        """a11y ``assert`` steps re-checked live, then ``state_assert`` steps through ``StateJudge.verify``
        with the baseline the runner took before that step. None when the script declares neither."""
        script = state.get("script")
        steps = _script_steps(script)
        asserts = [s for s in steps if _get(s, "op") == "assert"]
        state_asserts = [(i, s) for i, s in enumerate(steps) if _get(s, "op") == "state_assert"]
        if not asserts and not state_asserts:
            return None
        backend = deps.backend_factory(0)
        verdict: dict | None = None
        if asserts:
            last = asserts[-1]
            verdict = _a11y_check(backend, _get(last, "target"), _get(last, "state"))
            if not verdict["ok"]:
                return verdict
        if state_asserts:
            index, last = state_asserts[-1]
            sj = state_judge_for(state["id"], backend)
            if sj is None:
                return verdict  # nothing can re-verify; the executor's own pass stands (or a11y decided)
            baseline = (run.get("baselines") or {}).get(index) or (run.get("baselines") or {}).get(str(index)) or None
            v = sj.verify(_get(last, "checks"), state["id"], baseline)
            verdict = {"ok": bool(_get(v, "ok")), "reason": str(_get(v, "reason") or ""), "source": "state"}
        return verdict

    def judge(state: CUState, config: RunnableConfig) -> dict:
        enter(state, "judge", config)
        if state.get("infeasible"):
            verify = state.get("verify") or {"ok": False, "error": state.get("error"), "reason": state.get("error"),
                                             "source": "feasibility", "goal": False}
            log.record("verify", f"{state['id']}: judge skipped (infeasible)", job_id=state["id"], **verify)
            return {"phase": "judge", "verify": verify, "ok": False, "error": state.get("error"),
                    "failed_index": None, "heal_reason": None}
        run = state.get("run") or {}
        failed_index = state.get("failed_index")
        current_last = max(0, len(_script_steps(state.get("script"))) - 1)
        if not run.get("ok"):
            steps = state.get("steps") or []
            reason = run.get("error") or ""
            if not reason and failed_index is not None and 0 <= int(failed_index) < len(steps):
                reason = steps[int(failed_index)].get("stderr_tail") or ""
            kind = "looped" if run.get("loop") else "failed"
            result = {"ok": False, "error": f"step {failed_index} {kind}" + (f": {reason[:200]}" if reason else ""),
                      "reason": reason, "source": "executor", "goal": False, "loop": bool(run.get("loop"))}
        elif run.get("decided"):
            d = run["decided"]
            result = {"ok": bool(d["ok"]), "error": None if d["ok"] else f"goal not achieved: {d['reason'][:200]}",
                      "reason": d["reason"], "source": d.get("source", "milestone"), "goal": bool(d["ok"])}
            if not d["ok"] and failed_index is None:
                failed_index = current_last
        elif (declared := declared_verdict(state, run)) is not None:
            result = {"ok": declared["ok"], "error": None if declared["ok"] else f"goal not achieved: {declared['reason'][:200]}",
                      "reason": declared["reason"], "source": declared["source"], "goal": declared["ok"]}
            if not declared["ok"] and failed_index is None:
                failed_index = current_last
        elif deps.judge is not None:
            backend = deps.backend_factory(0)
            answer = _judge_answer(deps.judge, _screenshot(backend), _goal_question(state["goal"]),
                                   tree=backend.tree, final=True)
            result = {"ok": answer["ok"], "error": None if answer["ok"] else f"goal not achieved: {answer['reason'][:200]}",
                      "reason": answer["reason"], "source": answer["source"], "goal": answer["ok"]}
            if not answer["ok"] and failed_index is None:
                failed_index = current_last
        else:
            result = {"ok": True, "error": None, "reason": "", "source": "none", "goal": True}
        m = cu_meter.current()
        if not result["ok"] and m is not None and m.exhausted:
            result["error"] = f"{result['error'] or 'goal not achieved'}; {_cap_reason(m)}"
            result["cap"] = True
        log.record("verify", f"{state['id']}: judge {'passed' if result['ok'] else 'failed'}", job_id=state["id"], **result)
        if result["ok"]:
            genui.ui_preview_status(_tracker(config), state.get("label", ""), True, 0, result.get("source"))
        return {"phase": "judge", "verify": result, "ok": bool(result["ok"]),
                "error": None if result["ok"] else result["error"],
                "failed_index": failed_index, "heal_reason": None if result["ok"] else (result["reason"] or result["error"])}

    def route_after_judge(state: CUState) -> str:
        if state.get("ok"):
            return "ok"
        if state.get("infeasible"):
            return "exhausted"
        m = cu_meter.current()
        if m is not None and m.exhausted:
            return "exhausted"  # a heal is a paid replan; at the cap the job reports instead
        if int(state.get("attempt", 0)) < int(state.get("max_attempts", 0)):
            return "heal"
        return "exhausted"

    def heal(state: CUState, config: RunnableConfig) -> dict:
        enter(state, "heal", config, failed_index=state.get("failed_index"), reason=(state.get("heal_reason") or "")[:200])
        script = state["script"]
        index = int(state.get("failed_index") or 0)
        reason = str(state.get("heal_reason") or "step failed")
        run = state.get("run") or {}
        loop_evidence = list(run.get("loop_evidence") or []) if run.get("loop") else []
        # replan_step yields exactly one replacement Step; replace_step splices it into a revalidated script.
        # A loop break hands the planner the repeat evidence when it accepts ``loop_evidence``.
        if loop_evidence and _accepts_kwarg(deps.planner.replan_step, "loop_evidence"):
            step = deps.planner.replan_step(script, index, reason, tree_summary(0), loop_evidence=loop_evidence)
        else:
            step = deps.planner.replan_step(script, index, reason, tree_summary(0))
        new_script = deps.planner.replace_step(script, index, step)
        attempt = int(state.get("attempt", 0)) + 1
        log.record("remediate", f"{state['id']}: healed step {index} (attempt {attempt})",
                   job_id=state["id"], index=index, reason=reason[:500], attempt=attempt,
                   loop=bool(loop_evidence), loop_evidence=loop_evidence)
        out: dict = {"phase": "heal", "script": new_script, "attempt": attempt, "healed_step": index,
                     "healed_reason": reason, "cached": False, "run": None, "failed_index": None,
                     "persisted": False}
        current_pid = state.get("procedure_id")
        if deps.procedures is not None and current_pid:
            # The script being healed is a stored version: persist v<N+1> now so lineage exists even if
            # this attempt fails too. A fresh plan has no version to descend from; store() writes it once it passes.
            stored = deps.procedures.store(
                state["app"], state["goal"], new_script,
                parent=current_pid, healed_step=index, reason=reason[:500],
            )
            new_pid = _procedure_id(stored)
            log.record("cache", f"{state['id']}: stored healed procedure {new_pid}", job_id=state["id"],
                       procedure_id=new_pid, parent=current_pid, healed_step=index, attempt=attempt)
            out.update({"procedure_id": new_pid, "parent_id": current_pid, "procedure": stored, "persisted": True})
        log.record("note", f"{state['id']}: re-executing the whole healed script from step 0 (ScriptRunner has no start index)",
                   job_id=state["id"], healed_step=index, attempt=attempt)
        return out

    def build_interface(state: CUState, stored: Any, pid: Optional[str]) -> None:
        """I4 / P7: one bounded ``InterfaceBuilder.build`` after a FRESH store (never on replay), on the
        slot-0 backend's own display. Nothing here can change the job's outcome."""
        builder = deps.interface_builder
        if builder is None:
            return
        job_id = state["id"]
        try:
            display = getattr(deps.backend_factory(0), "display", None)
            version = builder.build(state["app"], stored, BACKEND_API_DOC, display=display)
        except Exception as e:  # noqa: BLE001
            log.record("note", f"{job_id}: interface build failed ({type(e).__name__}: {e})",
                       job_id=job_id, app=state["app"], procedure_id=pid)
            return
        if version is None:
            log.record("note", f"{job_id}: interface build skipped (no version written)",
                       job_id=job_id, app=state["app"], procedure_id=pid)
            return
        log.record("cache", f"{job_id}: built interface v{version} for {state['app']!r}",
                   job_id=job_id, app=state["app"], procedure_id=pid, version=version)

    def store(state: CUState, config: RunnableConfig) -> dict:
        enter(state, "store", config, cached=state.get("cached", False), persisted=state.get("persisted", False))
        pid = state.get("procedure_id")
        healed = state.get("healed_step")
        if deps.procedures is not None and state.get("storable") is False:
            log.record("cache", f"{state['id']}: milestone run not stored (turns do not form one procedure)",
                       job_id=state["id"], procedure_id=None)
        elif deps.procedures is not None and not state.get("persisted"):
            stored = deps.procedures.store(
                state["app"],
                state["goal"],
                state["script"],
                parent=state.get("parent_id"),
                healed_step=healed,
                reason=state.get("healed_reason") if healed is not None else None,
            )
            pid = _procedure_id(stored) or pid
            log.record("cache", f"{state['id']}: stored procedure {pid}", job_id=state["id"],
                       procedure_id=pid, parent=state.get("parent_id"), healed_step=healed)
            build_interface(state, stored, pid)
        elif deps.procedures is not None:
            log.record("cache", f"{state['id']}: procedure {pid} already persisted; nothing written",
                       job_id=state["id"], procedure_id=pid, healed_step=healed)
        if deps.notes is not None:
            try:
                deps.notes.append(state["app"], _note_text(state))
            except Exception as e:
                log.record("note", f"{state['id']}: interface notes append failed ({type(e).__name__})", job_id=state["id"])
        return {"phase": "store", "procedure_id": pid}

    def report(state: CUState, config: RunnableConfig) -> dict:
        enter(state, "report", config, ok=bool(state.get("ok")))
        ok = bool(state.get("ok"))
        error = state.get("error")
        if state.get("infeasible"):
            error = error or "goal judged infeasible"
        elif not ok and not error:
            error = f"failed after {state.get('attempt', 0)} heal(s)"
        elif not ok and int(state.get("attempt", 0)) >= int(state.get("max_attempts", 0)) and "model-call cap" not in str(error):
            error = f"{error}; gave up after {state.get('attempt', 0)} heal(s)"
        log.record("task", f"{state['id']}: {'done' if ok else 'failed'} {state['label']!r}",
                   job_id=state["id"], ok=ok, error=error, attempt=state.get("attempt", 0),
                   cached=state.get("cached", False), procedure_id=state.get("procedure_id"),
                   infeasible=bool(state.get("infeasible")), turns=int(state.get("turns") or 0),
                   **_meter_detail())
        genui.ui_end(_tracker(config), ok)
        return {"phase": "report", "done": True, "ok": ok, "error": error}

    nodes = {
        "context": context, "plan": plan, "ground": ground, "execute": execute,
        "judge": judge, "heal": heal, "store": store, "report": report,
    }
    builder = StateGraph(CUState)
    for name in CU_NODES:
        builder.add_node(name, nodes[name])

    conditional = {"judge"}
    for src, dst in CU_EDGES:
        if src not in conditional:
            builder.add_edge(src, dst)
    builder.add_conditional_edges("judge", route_after_judge, {"ok": "store", "heal": "heal", "exhausted": "report"})
    return builder.compile()


def _meter_detail() -> dict:
    m = cu_meter.current()
    if m is None:
        return {}
    return {"model_calls": m.snapshot(), "cost_usd": round(m.cost_usd, 6)}


def _note_text(state: dict) -> str:
    """Deterministic, bounded (≤ NOTE_LINES) summary of one run for the per-app interface notes."""
    lines = [
        f"## run {state.get('id')} — {'ok' if state.get('ok') else 'failed'}"
        f"{' (replay)' if state.get('cached') else ''}{' healed' if state.get('healed_step') is not None else ''}",
        f"goal: {str(state.get('goal') or '')[:200]}",
    ]
    for row in (state.get("steps") or [])[: NOTE_LINES - 4]:
        mark = "ok" if row.get("exit_code") == 0 else "FAIL"
        lines.append(f"- {mark} {row.get('command', '')[:120]}")
    if state.get("healed_reason"):
        lines.append(f"pitfall: {str(state['healed_reason'])[:200]}")
    return "\n".join(lines[:NOTE_LINES])


def initial_state(
    goal: str, app: str, job_id: str, candidates: int, max_heals: int, procedure_id: Optional[str] = None
) -> CUState:
    """``procedure_id`` (from ``/computer/replay``) pins the version ``plan`` replays."""
    return {
        "id": job_id,
        "label": goal[:200],
        "phase": "context",
        "attempt": 0,
        "max_attempts": int(max_heals),
        "cached": False,
        "steps": [],
        "diagnostic": None,
        "verify": None,
        "done": False,
        "ok": False,
        "error": None,
        "procedure_id": None,
        "requested_procedure_id": procedure_id or None,
        "persisted": False,
        "infeasible": False,
        "memory": None,
        "milestones": None,
        "milestone_index": 0,
        "turns": 0,
        "turn_scripts": [],
        "steps_before_turn": 0,
        "storable": True,
        "turn_candidates": None,
        "candidates": max(1, int(candidates or 1)),
        "scores": [],
        "goal": goal,
        "app": app,
        "script": None,
        "procedure": None,
        "parent_id": None,
        "notes_text": "",
        "tree_summary": "",
        "tree_summaries": [],
        "ground": None,
        "run": None,
        "failed_index": None,
        "heal_reason": None,
        "healed_step": None,
        "healed_reason": None,
    }


def run_cu_job(
    graph,
    goal: str,
    app: str,
    job_id: str,
    candidates: int = 1,
    max_heals: int | None = None,
    progress: Callable[[dict], None] | None = None,
    procedure_id: str | None = None,
    max_model_calls: int | None = cu_meter.DEFAULT_MAX_PAID_CALLS,
) -> dict:
    """Run one computer-use job to completion; never raises (failures become ``ok=False`` JobStates).

    ``progress`` (optional) receives the current JobState at every phase transition.
    ``procedure_id`` (optional) pins the stored version that ``plan`` replays.
    ``max_model_calls`` caps the job's paid (Claude Code CLI) model calls (None: no cap); at the cap the job
    stops and reports. The JobState carries ``model_calls``, ``cost_usd`` and ``turns``.
    """
    with cu_meter.metered(cu_meter.JobMeter(job_id, max_model_calls)):
        return _run_cu_job(graph, goal, app, job_id, candidates, max_heals, progress, procedure_id)


def _run_cu_job(graph, goal, app, job_id, candidates, max_heals, progress, procedure_id) -> dict:
    state = initial_state(goal, app, job_id, candidates, max_heals or 0, procedure_id)
    tracker: dict = {"phase": "context", "state": dict(state), "progress": progress}
    config: RunnableConfig = {
        "configurable": {"tracker": tracker},
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
        if isinstance(e, cu_meter.ModelCallCapReached):
            last["error"] = _cap_reason(e.meter) + f" (in {last['phase']})"
        else:
            last["error"] = f"{type(e).__name__}: {e}"
        genui.ui_end(tracker, False)
        graph_log = tracker.get("log")
        if graph_log is not None:
            graph_log.record("task", f"{job_id}: aborted in {last['phase']} ({type(e).__name__})",
                             job_id=job_id, phase=last["phase"], error=last["error"], ok=False, **_meter_detail())
        return job_state(last)
