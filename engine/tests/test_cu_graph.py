"""Computer-use graph: topology, replay path, heal bound, best-of-N, never-raise.

Everything downstream of the graph (ScriptRunner, Planner, BestOfN, Judge, ProcedureStore,
InterfaceNotes, backends) is stubbed here; ``o_engine.cu.executor`` and ``o_engine.cu.planner``
are imported lazily by the graph, so they are swapped in ``sys.modules`` for these tests.
"""
import json
import sys
import types
from dataclasses import dataclass, field

import pytest
from langgraph.graph import END, START

from o_engine.actionlog import ActionLog
from o_engine.cu.graph import (
    CU_EDGES, CU_NODES, CU_PHASES, JOBSTATE_FIELDS, CUDeps, build_cu_graph, run_cu_job, summarize_tree,
)


# ---------------------------------------------------------------- stubs

@dataclass(frozen=True)
class Target:
    role: str
    name: str | None = None


@dataclass(frozen=True)
class Step:
    op: str
    target: Target | None = None
    text: str | None = None
    cmd: str | None = None


@dataclass(frozen=True)
class Script:
    app: str
    goal: str
    steps: tuple


@dataclass
class StepResult:
    ok: bool
    resolved_by: str = "tree"
    before_digest: str = "a"
    after_digest: str = "b"
    screenshot_sha: str = "s"
    elapsed_ms: int = 2
    error: str | None = None
    note: str | None = None


@dataclass
class RunResult:
    ok: bool
    failed_index: int | None
    steps: list


class StubBackend:
    name = "fake"
    available = True

    def __init__(self, slot, known=("A1",)):
        self.slot = slot
        self.known = set(known)
        self.finds = []

    def tree(self, window=None):
        return [{"id": "n0", "role": "frame", "name": "Calc", "states": ["active"],
                 "children": [{"id": "n1", "role": "table cell", "name": "A1", "states": [], "children": []}]}]

    def find(self, target):
        self.finds.append(target)
        if target.name not in self.known:
            return None
        return {"id": "n1", "role": target.role, "name": target.name, "states": ["visible"]}

    def screenshot(self):
        return b"\x89PNG-stub"


class StubRunner:
    """Replaces ``o_engine.cu.executor.ScriptRunner``; outcome script per ``fail_texts``."""

    calls: list = []
    fail_texts: set = set()

    def __init__(self, backend, log, grounder=None, judge=None, **kw):
        self.backend = backend

    def run(self, script, job_id):
        StubRunner.calls.append((job_id, self.backend.slot, [s.op for s in script.steps]))
        rows = []
        for i, s in enumerate(script.steps):
            if s.text in StubRunner.fail_texts:
                rows.append(StepResult(False, error=f"element for {s.text!r} not found"))
                return RunResult(False, i, rows)
            rows.append(StepResult(True))
        return RunResult(True, None, rows)


class StubPlanner:
    def __init__(self):
        self.plan_calls = []
        self.replan_calls = []
        self.fix_text = "42-fixed"

    def plan(self, goal, app, tree_summary):
        self.plan_calls.append((goal, app, tree_summary))
        return Script(app, goal, (
            Step("launch", cmd="libreoffice --calc"),
            Step("find", target=Target("table cell", "A1")),
            Step("type", text="42"),
        ))

    def replan_step(self, script, index, reason, tree_summary):
        """Mirrors ``Planner.replan_step``: returns ONE replacement step, never a script."""
        self.replan_calls.append((index, reason))
        return Step("type", text=self.fix_text)

    def replace_step(self, script, index, step):
        """Mirrors ``Planner.replace_step``: a new script with ``steps[index]`` swapped."""
        steps = list(script.steps)
        steps[index] = step
        return Script(script.app, script.goal, tuple(steps))


class StubBestOfN:
    runs: list = []

    seen_comparative: list = []

    def __init__(self, n, planner, run_fn, judge, comparative=False):
        self.n, self.planner, self.run_fn, self.judge = n, planner, run_fn, judge
        StubBestOfN.seen_comparative.append(comparative)

    def run(self, goal, app, tree_summaries):
        scripts = [self.planner.plan(goal, app, ts) for ts in tree_summaries]
        scores = []
        for i, s in enumerate(scripts):
            r = self.run_fn(s, i)
            scores.append({"candidate": i, "score": 1.0 if r.ok else 0.0, "steps": len(r.steps)})
        StubBestOfN.runs.append(scores)
        best = max(range(len(scripts)), key=lambda i: scores[i]["score"])
        return scripts[best], scores


class StubJudge:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []
        self.last_source = "vlm"

    def ask(self, png, question):
        self.calls.append(question)
        return (self.answers.pop(0) if self.answers else True), "stub reason"


@dataclass
class Procedure:
    procedure_id: str
    app: str
    goal: str
    version: int
    script: object
    parent: str | None = None
    healed_step: int | None = None
    reason: str | None = None


class StubProcedures:
    def __init__(self):
        self.rows = []

    def latest(self, app, goal):
        matches = [r for r in self.rows if r.app == app and r.goal == goal]
        return matches[-1] if matches else None

    def get(self, procedure_id):
        return next((r for r in self.rows if r.procedure_id == procedure_id), None)

    def store(self, app, goal, script, parent=None, healed_step=None, reason=None):
        version = len([r for r in self.rows if r.app == app and r.goal == goal]) + 1
        row = Procedure(f"{app}/{abs(hash(goal)) % 1000}/v{version}", app, goal, version,
                        script, parent, healed_step, reason)
        self.rows.append(row)
        return row

    def list(self):
        return list(self.rows)


class StubNotes:
    def __init__(self):
        self.appended = []

    def for_prompt(self, app):
        return f"notes for {app}"

    def append(self, app, text):
        self.appended.append((app, text))


@dataclass
class Harness:
    log: ActionLog
    planner: StubPlanner
    judge: StubJudge
    procedures: StubProcedures
    notes: StubNotes
    backends: dict = field(default_factory=dict)
    deps: CUDeps = None
    graph: object = None


@pytest.fixture(autouse=True)
def _stub_modules(monkeypatch):
    executor = types.ModuleType("o_engine.cu.executor")
    executor.ScriptRunner = StubRunner
    planner = types.ModuleType("o_engine.cu.planner")
    planner.BestOfN = StubBestOfN
    monkeypatch.setitem(sys.modules, "o_engine.cu.executor", executor)
    monkeypatch.setitem(sys.modules, "o_engine.cu.planner", planner)
    StubRunner.calls = []
    StubRunner.fail_texts = set()
    StubBestOfN.runs = []
    yield


def make(tmp_data_dir, max_heals=3, candidates=1, judge_answers=None, with_judge=True):
    h = Harness(
        log=ActionLog(tmp_data_dir),
        planner=StubPlanner(),
        judge=StubJudge(judge_answers) if with_judge else None,
        procedures=StubProcedures(),
        notes=StubNotes(),
    )

    def backend_factory(slot):
        return h.backends.setdefault(slot, StubBackend(slot))

    h.deps = CUDeps(
        backend_factory=backend_factory, grounder=None, judge=h.judge, planner=h.planner,
        procedures=h.procedures, notes=h.notes, log=h.log, max_heals=max_heals, candidates=candidates,
        displays=[f":{99 + i}" for i in range(candidates)],
    )
    h.graph = build_cu_graph(h.deps)
    return h


def entries(log, kind=None):
    rows = [json.loads(line) for line in log.jsonl.read_text().splitlines()]
    return [r for r in rows if kind is None or r["kind"] == kind]


def phases(log, job_id):
    return [e["detail"]["phase"] for e in entries(log, "graph")
            if e["detail"].get("job_id") == job_id and e["summary"] == f"{job_id}: {e['detail']['phase']}"]


GOAL = "Put 42 in cell A1"
APP = "libreoffice-calc"


# ---------------------------------------------------------------- topology

def test_edges_match_the_diagram_literally():
    assert CU_NODES == ("context", "plan", "ground", "execute", "judge", "heal", "store", "report")
    assert CU_EDGES == (
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
    assert CU_PHASES == ("context", "plan", "ground", "execute", "judge", "heal", "store", "report")
    cycles = [(s, d) for s, d in CU_EDGES if d == "execute" and s != "ground"]
    assert cycles == [("heal", "execute")], "heal is the only cycle"


def test_compiled_graph_has_exactly_the_literal_edges(tmp_data_dir):
    h = make(tmp_data_dir)
    g = h.graph.get_graph()
    got = {(e.source, e.target) for e in g.edges}
    want = {("__start__" if s is START else s, "__end__" if d is END else d) for s, d in CU_EDGES}
    assert got == want


# ---------------------------------------------------------------- paths

def test_fresh_run_plans_once_stores_procedure_and_appends_notes(tmp_data_dir):
    h = make(tmp_data_dir)
    state = run_cu_job(h.graph, GOAL, APP, "cu-1", candidates=1)
    assert set(state) == set(JOBSTATE_FIELDS)
    assert set(JOBSTATE_FIELDS) >= {"id", "label", "phase", "attempt", "max_attempts", "cached", "steps",
                                   "diagnostic", "verify", "done", "ok", "error"}
    assert phases(h.log, "cu-1") == ["context", "plan", "ground", "execute", "judge", "store", "report"]
    assert state["ok"] is True and state["done"] is True and state["phase"] == "report"
    assert state["cached"] is False and state["attempt"] == 0 and state["max_attempts"] == 3
    assert state["candidates"] == 1 and state["scores"] == []
    assert len(h.planner.plan_calls) == 1
    assert h.planner.plan_calls[0][1] == APP and "table cell 'A1'" in h.planner.plan_calls[0][2]
    assert [s["command"] for s in state["steps"]] == [
        "launch cmd='libreoffice --calc'", "find target=table cell:A1", "type text='42'",
    ]
    assert all(s["exit_code"] == 0 for s in state["steps"])
    assert set(state["steps"][0]) == {"index", "command", "cwd", "exit_code", "stdout_tail", "stderr_tail", "elapsed_ms", "timed_out"}
    assert state["verify"]["ok"] is True and state["verify"]["source"] == "vlm"
    assert h.judge.calls == [f"Has this goal been achieved: {GOAL}?"]
    assert state["procedure_id"] == h.procedures.rows[0].procedure_id
    assert h.procedures.rows[0].parent is None and h.procedures.rows[0].healed_step is None
    assert h.notes.appended[0][0] == APP and "run cu-1 — ok" in h.notes.appended[0][1]
    assert StubRunner.calls == [("cu-1", 0, ["launch", "find", "type"])]
    ground = [e for e in entries(h.log, "graph") if e["detail"].get("phase") == "ground" and "targets" in e["detail"]]
    assert ground and ground[0]["detail"]["via_tree"] == 1 and ground[0]["detail"]["targets"] == 1


def test_replay_path_makes_zero_planner_calls(tmp_data_dir):
    h = make(tmp_data_dir)
    first = run_cu_job(h.graph, GOAL, APP, "cu-1")
    assert first["cached"] is False
    plan_calls = len(h.planner.plan_calls)
    stored = len(h.procedures.rows)

    second = run_cu_job(h.graph, GOAL, APP, "cu-2")
    assert second["cached"] is True and second["ok"] is True
    assert second["procedure_id"] == first["procedure_id"]
    assert len(h.planner.plan_calls) == plan_calls
    assert h.planner.replan_calls == []
    assert len(h.procedures.rows) == stored, "unchanged replay stores nothing new"
    assert phases(h.log, "cu-2") == ["context", "plan", "ground", "execute", "judge", "store", "report"]
    hits = [e for e in entries(h.log, "cache") if e["detail"].get("job_id") == "cu-2" and e["detail"].get("hit")]
    assert hits and hits[0]["detail"]["procedure_id"] == first["procedure_id"]


def test_heal_is_bounded_by_max_heals_then_reports_failure(tmp_data_dir):
    h = make(tmp_data_dir, max_heals=2)
    StubRunner.fail_texts = {"42", "42-fixed"}
    state = run_cu_job(h.graph, GOAL, APP, "cu-f")
    assert state["ok"] is False and state["done"] is True and state["phase"] == "report"
    assert state["attempt"] == 2 and state["max_attempts"] == 2
    assert "gave up after 2 heal(s)" in state["error"]
    ph = phases(h.log, "cu-f")
    assert ph.count("heal") == 2 and ph.count("execute") == 3 and ph.count("judge") == 3
    assert "store" not in ph and ph[-1] == "report"
    assert [i for i, _ in h.planner.replan_calls] == [2, 2]
    assert "not found" in h.planner.replan_calls[0][1]
    assert h.procedures.rows == [], "a failed goal is never stored"
    assert state["steps"][-1]["exit_code"] == 1 and "not found" in state["steps"][-1]["stderr_tail"]
    assert state["verify"]["source"] == "executor"
    assert h.judge.calls == [], "VLM judge is not asked when the executor already failed"


def test_heal_recovers_and_stores_lineage(tmp_data_dir):
    h = make(tmp_data_dir)
    StubRunner.fail_texts = {"42"}
    first = run_cu_job(h.graph, GOAL, APP, "cu-h")
    assert first["ok"] is True and first["attempt"] == 1 and first["cached"] is False
    assert phases(h.log, "cu-h") == ["context", "plan", "ground", "execute", "judge", "heal", "execute", "judge", "store", "report"]
    assert [s["command"] for s in first["steps"]][-1] == "type text='42-fixed'"
    row = h.procedures.rows[-1]
    assert row.healed_step == 2 and row.reason and row.parent is None
    assert first["procedure_id"] == row.procedure_id
    rem = [e for e in entries(h.log, "remediate") if e["detail"].get("job_id") == "cu-h"]
    assert len(rem) == 1 and rem[0]["detail"]["index"] == 2

    # replay of v1 that fails again is healed into v2 with lineage; v1 is never touched
    StubRunner.fail_texts = {"42-fixed"}
    h.planner.fix_text = "42-final"
    h.planner.replan_calls.clear()
    plan_calls = len(h.planner.plan_calls)
    second = run_cu_job(h.graph, GOAL, APP, "cu-h2")
    assert second["ok"] is True and second["attempt"] == 1
    assert second["cached"] is False, "a healed replay is no longer a pure replay"
    assert len(h.planner.plan_calls) == plan_calls, "replay never calls plan(); heal calls replan_step() once"
    assert h.planner.replan_calls == [(2, "element for '42-fixed' not found")]
    assert phases(h.log, "cu-h2") == ["context", "plan", "ground", "execute", "judge", "heal", "execute", "judge", "store", "report"]
    assert len(h.procedures.rows) == 2
    assert h.procedures.rows[0] is row, "v1 untouched"
    v2 = h.procedures.rows[1]
    assert v2.version == 2 and v2.parent == row.procedure_id and v2.healed_step == 2 and "42-fixed" in v2.reason
    assert second["procedure_id"] == v2.procedure_id
    assert [s.text for s in v2.script.steps if s.op == "type"] == ["42-final"]


def test_heal_calls_replan_step_then_replace_step_with_that_step(tmp_data_dir):
    """F3-a: ``replan_step`` yields a Step; the graph must hand it to ``replace_step`` (never treat it as a script)."""
    h = make(tmp_data_dir)
    StubRunner.fail_texts = {"42"}
    replaced = []
    original_replace = h.planner.replace_step

    def spy(script, index, step):
        replaced.append((type(script).__name__, index, step))
        return original_replace(script, index, step)

    h.planner.replace_step = spy
    state = run_cu_job(h.graph, GOAL, APP, "cu-rs")
    assert state["ok"] is True and state["attempt"] == 1
    assert replaced == [("Script", 2, Step("type", text="42-fixed"))]
    assert StubRunner.calls[-1] == ("cu-rs", 0, ["launch", "find", "type"]), "healed script still has 3 steps"


def test_heal_of_a_replay_persists_each_version_at_heal_time(tmp_data_dir):
    """F3-b: every heal of a persisted procedure writes v<N+1> immediately (lineage even when the job fails)."""
    h = make(tmp_data_dir, max_heals=2)
    first = run_cu_job(h.graph, GOAL, APP, "cu-v1")
    v1 = h.procedures.rows[0]
    assert first["procedure_id"] == v1.procedure_id and len(h.procedures.rows) == 1

    StubRunner.fail_texts = {"42", "42-fixed"}
    state = run_cu_job(h.graph, GOAL, APP, "cu-v23")
    assert state["ok"] is False and state["attempt"] == 2
    assert [r.version for r in h.procedures.rows] == [1, 2, 3]
    v2, v3 = h.procedures.rows[1], h.procedures.rows[2]
    assert v2.parent == v1.procedure_id and v3.parent == v2.procedure_id
    assert v2.healed_step == 2 and v3.healed_step == 2
    assert "not found" in v2.reason and "42-fixed" in v3.reason
    assert state["procedure_id"] == v3.procedure_id, "JobState names the last healed version"
    ph = phases(h.log, "cu-v23")
    assert "store" not in ph and ph.count("heal") == 2
    assert h.procedures.rows[0] is v1, "v1 untouched"


def test_store_skips_a_script_that_heal_already_persisted(tmp_data_dir):
    h = make(tmp_data_dir)
    run_cu_job(h.graph, GOAL, APP, "cu-a")
    v1 = h.procedures.rows[0]
    StubRunner.fail_texts = {"42"}
    state = run_cu_job(h.graph, GOAL, APP, "cu-b")
    assert state["ok"] is True and state["attempt"] == 1
    assert [r.version for r in h.procedures.rows] == [1, 2], "heal wrote v2; store did not write a v3"
    assert h.procedures.rows[1].parent == v1.procedure_id
    assert state["procedure_id"] == h.procedures.rows[1].procedure_id
    assert phases(h.log, "cu-b")[-2:] == ["store", "report"], "store still runs (notes) but writes nothing"
    assert len(h.notes.appended) == 2


def test_requested_procedure_id_replays_that_version_even_when_newer_exists(tmp_data_dir):
    """F3-d: ``/computer/replay`` threads procedure_id; plan prefers ``procedures.get(pid)`` over ``latest``."""
    h = make(tmp_data_dir)
    run_cu_job(h.graph, GOAL, APP, "cu-1")
    v1 = h.procedures.rows[0]
    StubRunner.fail_texts = {"42"}
    run_cu_job(h.graph, GOAL, APP, "cu-2")
    assert [r.version for r in h.procedures.rows] == [1, 2]
    assert h.procedures.latest(APP, GOAL).version == 2

    StubRunner.fail_texts = set()
    plan_calls = len(h.planner.plan_calls)
    state = run_cu_job(h.graph, GOAL, APP, "cu-r", procedure_id=v1.procedure_id)
    assert state["cached"] is True and state["ok"] is True
    assert state["procedure_id"] == v1.procedure_id
    assert [s["command"] for s in state["steps"]][-1] == "type text='42'", "v1's script ran, not v2's"
    assert len(h.planner.plan_calls) == plan_calls and len(h.procedures.rows) == 2
    hits = [e for e in entries(h.log, "cache") if e["detail"].get("job_id") == "cu-r" and e["detail"].get("hit")]
    assert hits and hits[0]["detail"]["procedure_id"] == v1.procedure_id

    # an unknown requested id falls back to latest rather than failing the job
    state = run_cu_job(h.graph, GOAL, APP, "cu-u", procedure_id="nope/v9")
    assert state["cached"] is True and state["procedure_id"] == h.procedures.rows[1].procedure_id


def test_judge_failure_triggers_heal_on_last_step(tmp_data_dir):
    h = make(tmp_data_dir, max_heals=1, judge_answers=[False, True])
    state = run_cu_job(h.graph, GOAL, APP, "cu-j")
    assert state["ok"] is True and state["attempt"] == 1
    assert h.planner.replan_calls[0][0] == 2 and h.planner.replan_calls[0][1] == "stub reason"
    assert len(h.judge.calls) == 2
    assert phases(h.log, "cu-j").count("heal") == 1


def test_best_of_n_runs_each_candidate_on_its_own_slot(tmp_data_dir):
    h = make(tmp_data_dir, candidates=3)
    state = run_cu_job(h.graph, GOAL, APP, "cu-n", candidates=3)
    assert state["ok"] is True and state["candidates"] == 3
    assert [s["candidate"] for s in state["scores"]] == [0, 1, 2]
    assert sorted(h.backends) == [0, 1, 2]
    slots = [slot for job, slot, _ in StubRunner.calls if job.startswith("cu-n-c")]
    assert slots == [0, 1, 2]
    assert StubRunner.calls[-1] == ("cu-n", 0, ["launch", "find", "type"]), "winner re-executed on slot 0"
    assert len(h.planner.plan_calls) == 3


def test_no_judge_means_executor_result_is_final(tmp_data_dir):
    h = make(tmp_data_dir, with_judge=False)
    state = run_cu_job(h.graph, GOAL, APP, "cu-nj")
    assert state["ok"] is True and state["verify"] == {"ok": True, "error": None, "reason": "", "source": "none", "goal": True}


def test_exception_inside_a_node_becomes_failed_jobstate(tmp_data_dir):
    h = make(tmp_data_dir)

    def boom(goal, app, tree_summary):
        raise RuntimeError("planner exploded")

    h.planner.plan = boom
    state = run_cu_job(h.graph, GOAL, APP, "cu-x")
    assert state["done"] is True and state["ok"] is False
    assert state["phase"] == "plan"
    assert state["error"] == "RuntimeError: planner exploded"
    assert set(state) == set(JOBSTATE_FIELDS)
    aborted = [e for e in entries(h.log, "task") if e["detail"].get("job_id") == "cu-x"]
    assert aborted and "aborted in plan" in aborted[-1]["summary"]


def test_progress_callback_sees_every_phase(tmp_data_dir):
    h = make(tmp_data_dir)
    seen = []
    run_cu_job(h.graph, GOAL, APP, "cu-p", progress=lambda s: seen.append(s["phase"]))
    assert seen == ["context", "plan", "ground", "execute", "judge", "store", "report"]
    assert all(set(s) == set(JOBSTATE_FIELDS) for s in [])


def test_summarize_tree_caps_lines():
    nodes = [{"role": "frame", "name": "w", "children": [{"role": "button", "name": f"b{i}"} for i in range(300)]}]
    text = summarize_tree(nodes, limit=50)
    assert text.count("\n") == 50 and text.endswith("(truncated at 50 lines)")
    assert text.startswith("frame 'w'")


def test_best_of_n_with_real_bestofn_uses_slot_screenshots_for_goal_judge(tmp_data_dir, monkeypatch):
    import importlib

    monkeypatch.delitem(sys.modules, "o_engine.cu.planner")
    real = importlib.import_module("o_engine.cu.planner")
    monkeypatch.setitem(sys.modules, "o_engine.cu.planner", real)
    h = make(tmp_data_dir, candidates=2)
    StubRunner.fail_texts = set()
    state = run_cu_job(h.graph, GOAL, APP, "cu-real", candidates=2)
    assert state["ok"] is True
    assert [s["candidate"] for s in state["scores"]] == [0, 1]
    assert all(s["score"] > 1.9 for s in state["scores"]), state["scores"]
    # one goal question per candidate (callable judge, screenshot from that slot) + one final judge
    assert len(h.judge.calls) == 3 and all(GOAL in q for q in h.judge.calls)


# ---------------------------------------------------------------- wave 2 (I-I): feasibility, milestones, judge order, loop, candidates


@dataclass
class LoopStepResult(StepResult):
    status: str = "loop"
    reason: str = "loop"
    evidence: tuple = ()


class LoopRunner(StubRunner):
    """Fails step ``loop_at`` with the loop breaker's shape; exposes ``loop_evidence()`` like the real runner."""

    loop_at: int = 2
    instances: list = []

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.kw = kw
        self._evidence = []
        LoopRunner.instances.append(self)

    def loop_evidence(self):
        return list(self._evidence)

    def run(self, script, job_id):
        StubRunner.calls.append((job_id, self.backend.slot, [s.op for s in script.steps]))
        rows = [StepResult(True) for _ in range(LoopRunner.loop_at)]
        self._evidence = [{"index": i, "op": "type", "step": "type", "screen_sha": "same"} for i in range(3)]
        rows.append(LoopStepResult(False, error="loop: step 2 would run for the 3rd time on an unchanged screen",
                                   evidence=tuple(self._evidence)))
        return RunResult(False, LoopRunner.loop_at, rows)


class MilestonePlanner(StubPlanner):
    """Duck-typed P1/P9 API over the stub Script type. ``turn_scripts`` is popped once per turn."""

    def __init__(self, milestones, turn_scripts, candidates=None):
        super().__init__()
        self.milestones = list(milestones)
        self.turn_scripts = list(turn_scripts)
        self.candidates = list(candidates or [])
        self.milestone_calls = []
        self.next_calls = []
        self.candidate_calls = []
        self.loop_evidence_seen = []

    def plan_milestones(self, goal, app, notes, memory=None):
        self.milestone_calls.append((goal, app, notes, memory))
        return tuple(self.milestones)

    def next_steps(self, milestone, tree_summary, memory, notes, interfaces=(), max_steps=5):
        self.next_calls.append((milestone["goal"], tree_summary, memory, notes, tuple(interfaces), max_steps))
        return self.turn_scripts.pop(0)

    def candidate_steps(self, milestone, tree_summary, memory, notes, interfaces=(), max_steps=5, k=1):
        self.candidate_calls.append((milestone["goal"], k))
        return list(self.candidates)

    def replan_step(self, script, index, reason, tree_summary, loop_evidence=None):
        self.loop_evidence_seen.append(loop_evidence)
        return super().replan_step(script, index, reason, tree_summary)


class ChangingBackend(StubBackend):
    """Every ``type`` the runner reports makes the next cell known (A1 -> A2 -> A3 -> A4)."""

    def __init__(self, slot):
        super().__init__(slot, known=("A1",))
        self.display = None

    def grow(self):
        self.known.add(f"A{len(self.known) + 1}")


class GrowingRunner(StubRunner):
    def run(self, script, job_id):
        result = super().run(script, job_id)
        for s in script.steps:
            if s.op == "type" and result.ok:
                self.backend.grow()
        return result


def milestone(goal, cell):
    return {"goal": goal, "done_when": Target("table cell", cell)}


def turn_script(cell, text):
    return Script(APP, GOAL, (Step("find", target=Target("table cell", cell)), Step("type", text=text)))


def make_w2(tmp_data_dir, planner, backend_cls=StubBackend, runner_cls=None, **deps_kw):
    h = Harness(log=ActionLog(tmp_data_dir), planner=planner, judge=deps_kw.pop("judge", StubJudge()),
                procedures=StubProcedures(), notes=StubNotes())
    if runner_cls is not None:
        sys.modules["o_engine.cu.executor"].ScriptRunner = runner_cls

    def backend_factory(slot):
        return h.backends.setdefault(slot, backend_cls(slot))

    h.deps = CUDeps(backend_factory=backend_factory, grounder=None, judge=h.judge, planner=h.planner,
                    procedures=h.procedures, notes=h.notes, log=h.log, **deps_kw)
    h.graph = build_cu_graph(h.deps)
    return h


def test_cudeps_wave2_fields_default_so_server_construction_is_unchanged(tmp_data_dir):
    h = make(tmp_data_dir)
    d = h.deps
    assert (d.feasibility, d.state_judge_factory, d.interface_store, d.runner, d.sandbox_for) == (None, None, None, None, None)
    assert (d.steps_per_turn, d.max_turns, d.step_candidates) == (5, 20, 1)
    assert "infeasible" in JOBSTATE_FIELDS
    state = run_cu_job(h.graph, GOAL, APP, "cu-d")
    assert state["infeasible"] is False and state["ok"] is True


class StubGate:
    def __init__(self, feasible, reason=""):
        self.feasible, self.reason, self.calls = feasible, reason, []

    def check(self, goal, app, job_id):
        self.calls.append((goal, app, job_id))
        return {"feasible": self.feasible, "reason": self.reason}


def test_infeasible_goal_goes_to_report_without_planning_or_running(tmp_data_dir):
    gate = StubGate(False, "no such app")
    h = make_w2(tmp_data_dir, StubPlanner(), feasibility=gate, max_heals=3)
    state = run_cu_job(h.graph, "Open Foo", "foo", "cu-inf")
    assert state["ok"] is False and state["done"] is True and state["phase"] == "report"
    assert state["infeasible"] is True and state["error"] == "no such app" and state["attempt"] == 0
    assert gate.calls == [("Open Foo", "foo", "cu-inf")]
    assert h.planner.plan_calls == [] and h.planner.replan_calls == [] and StubRunner.calls == []
    assert h.judge.calls == [] and state["steps"] == [] and h.procedures.rows == [] and h.notes.appended == []
    assert phases(h.log, "cu-inf") == ["context", "plan", "ground", "execute", "judge", "report"]
    task = [e for e in entries(h.log, "task") if e["detail"].get("job_id") == "cu-inf"]
    assert task[-1]["detail"]["infeasible"] is True and task[-1]["detail"]["error"] == "no such app"
    assert set(state) == set(JOBSTATE_FIELDS)

    # a feasible verdict changes nothing
    h2 = make_w2(tmp_data_dir, StubPlanner(), feasibility=StubGate(True, "fine"))
    state = run_cu_job(h2.graph, GOAL, APP, "cu-ok")
    assert state["ok"] is True and state["infeasible"] is False and len(h2.planner.plan_calls) == 1


def test_three_milestones_one_plan_entry_per_turn_and_replay_makes_no_planner_calls(tmp_data_dir):
    planner = MilestonePlanner(
        [milestone("fill A1", "A2"), milestone("fill A2", "A3"), milestone("fill A3", "A4")],
        [turn_script("A1", "1"), turn_script("A2", "2"), turn_script("A3", "3")],
    )
    h = make_w2(tmp_data_dir, planner, backend_cls=ChangingBackend, runner_cls=GrowingRunner, steps_per_turn=4)
    state = run_cu_job(h.graph, GOAL, APP, "cu-m")
    assert state["ok"] is True and state["attempt"] == 0 and state["cached"] is False, state
    assert phases(h.log, "cu-m") == ["context", "plan", "ground", "execute", "plan", "plan", "judge", "store", "report"]
    assert len(planner.milestone_calls) == 1 and planner.milestone_calls[0][2] == f"notes for {APP}"
    assert planner.milestone_calls[0][3].facts == (f"Goal: {GOAL}",), "working memory seeded from the goal"
    assert [c[0] for c in planner.next_calls] == ["fill A1", "fill A2", "fill A3"]
    assert all(c[5] == 4 and "table cell" in c[1] for c in planner.next_calls), "steps_per_turn and the live tree reach next_steps"
    assert planner.plan_calls == [], "one-shot plan() is not used"
    assert [cmd for cmd in [s["command"] for s in state["steps"]]] == [
        "find target=table cell:A1", "type text='1'", "find target=table cell:A2", "type text='2'",
        "find target=table cell:A3", "type text='3'",
    ]
    assert h.judge.calls == [], "done_when targets decide via the tree; no VLM question"
    assert state["verify"]["source"] == "a11y" and state["verify"]["ok"] is True
    verdicts = [e["detail"] for e in entries(h.log, "verify") if e["detail"].get("job_id") == "cu-m" and "milestone" in e["detail"]]
    assert [(v["milestone"], v["turn"], v["ok"]) for v in verdicts] == [(0, 1, True), (1, 2, True), (2, 3, True)]
    notes = [e for e in entries(h.log, "note") if e["detail"].get("job_id") == "cu-m" and "working memory" in e["summary"]]
    assert [n["detail"]["turn"] for n in notes] == [1, 2, 3] and notes[0]["detail"]["counts"]["facts"] == 1
    # the stub Script type cannot be re-parsed; the constructor form combines the turns and store writes it
    assert len(h.procedures.rows) == 1 and len(h.procedures.rows[0].script.steps) == 6

    # replay: zero planner calls, no milestone loop, one plan phase only
    h.backends[0].known = {"A1", "A2", "A3"}
    again = run_cu_job(h.graph, GOAL, APP, "cu-m2")
    assert again["ok"] is True and again["cached"] is True
    assert len(planner.milestone_calls) == 1 and len(planner.next_calls) == 3
    assert phases(h.log, "cu-m2") == ["context", "plan", "ground", "execute", "judge", "store", "report"]


def test_milestone_loop_is_bounded_by_max_turns_and_done_when_question_uses_the_judge(tmp_data_dir):
    key_turn = Script(APP, GOAL, (Step("find", target=Target("table cell", "A1")),))
    planner = MilestonePlanner(
        [{"goal": "ask", "done_when": "Is A1 filled?"}, milestone("never", "ZZ")],
        [key_turn, key_turn, key_turn, key_turn],
    )
    h = make_w2(tmp_data_dir, planner, judge=StubJudge([True]), max_turns=3, max_heals=1)
    state = run_cu_job(h.graph, GOAL, APP, "cu-mt")
    assert state["ok"] is False and state["attempt"] == 1
    assert h.judge.calls == ["Is A1 filled?"], "a question done_when asks the VLM once; the Target one never does"
    assert "milestone 1 (never) not done after 3 turn(s)" in state["error"]
    assert "element not found" in state["error"]
    assert len(planner.next_calls) == 3, "the heal re-runs the healed last turn; max_turns is already spent"
    assert phases(h.log, "cu-mt").count("heal") == 1
    assert [i for i, _ in planner.replan_calls] == [0], "heal replaces a step of the current turn's script"
    assert h.procedures.rows == []


def test_loop_is_a_heal_reason_with_evidence_and_last_heal_reports_the_loop(tmp_data_dir):
    LoopRunner.instances = []
    planner = MilestonePlanner([], [])  # no milestones -> one-shot plan(); replan_step accepts loop_evidence
    h = make_w2(tmp_data_dir, planner, runner_cls=LoopRunner, max_heals=2)
    state = run_cu_job(h.graph, GOAL, APP, "cu-loop")
    assert state["ok"] is False and state["attempt"] == 2
    assert state["error"].startswith("step 2 looped: loop: step 2 would run for the 3rd time")
    assert "gave up after 2 heal(s)" in state["error"]
    assert state["steps"][2]["status"] == "loop" and state["steps"][2]["reason"] == "loop"
    assert [e["index"] for e in state["steps"][2]["evidence"]] == [0, 1, 2]
    assert state["verify"]["loop"] is True and state["verify"]["source"] == "executor"
    assert len(planner.loop_evidence_seen) == 2 and all(len(ev) == 3 for ev in planner.loop_evidence_seen)
    rem = [e for e in entries(h.log, "remediate") if e["detail"].get("job_id") == "cu-loop"]
    assert len(rem) == 2 and all(r["detail"]["loop"] and len(r["detail"]["loop_evidence"]) == 3 for r in rem)
    assert h.judge.calls == [] and h.procedures.rows == []

    # a planner whose replan_step has no loop_evidence parameter is called the old way
    legacy = StubPlanner()
    h2 = make_w2(tmp_data_dir, legacy, runner_cls=LoopRunner, max_heals=1)
    state = run_cu_job(h2.graph, GOAL, APP, "cu-loop2")
    assert state["ok"] is False and legacy.replan_calls and legacy.replan_calls[0][1].startswith("loop:")


class CountingStateJudge:
    """Stub ``StateJudge``: snapshot/verify counted; verdict from ``ok``; never a model call."""

    instances: list = []

    def __init__(self, sandbox, backend, ok=True):
        self.sandbox, self.backend, self.ok = sandbox, backend, ok
        self.verify_calls = []
        CountingStateJudge.instances.append(self)

    def snapshot(self, paths):
        return {p: "deadbeef" for p in paths}

    def verify(self, checks, job_id, baseline=None):
        self.verify_calls.append((job_id, list(checks), baseline))
        return {"ok": self.ok, "reason": "stub state", "source": "state"}


@dataclass(frozen=True)
class Check:
    kind: str
    arg: str


@dataclass(frozen=True)
class StateStep:
    op: str
    checks: tuple
    text: str | None = None


def test_judge_order_a11y_then_state_then_vlm(tmp_data_dir):
    CountingStateJudge.instances = []
    sandboxes = []

    def sandbox_for(job_id):
        sandboxes.append(job_id)
        return f"sandbox:{job_id}"

    # declared a11y assert decides: zero VLM calls
    h = make_w2(tmp_data_dir, StubPlanner(), state_judge_factory=lambda sb, be: CountingStateJudge(sb, be), sandbox_for=sandbox_for)
    h.planner.plan = lambda goal, app, ts: Script(app, goal, (
        Step("find", target=Target("table cell", "A1")), Step("type", text="42"), Step("assert", target=Target("table cell", "A1"))))
    state = run_cu_job(h.graph, GOAL, APP, "cu-a11y")
    assert state["ok"] is True and state["verify"]["source"] == "a11y" and h.judge.calls == []

    # declared state_assert decides (a11y not declared): StateJudge.verify is called with the baseline, zero VLM calls
    h2 = make_w2(tmp_data_dir, StubPlanner(), state_judge_factory=lambda sb, be: CountingStateJudge(sb, be), sandbox_for=sandbox_for)
    h2.planner.plan = lambda goal, app, ts: Script(app, goal, (
        Step("find", target=Target("table cell", "A1")), Step("type", text="42"),
        StateStep("state_assert", (Check("file_exists", "out.csv"),))))
    state = run_cu_job(h2.graph, GOAL, APP, "cu-state")
    assert state["ok"] is True and state["verify"]["source"] == "state" and h2.judge.calls == []
    verifies = [c for sj in CountingStateJudge.instances for c in sj.verify_calls if c[0] == "cu-state"]
    assert verifies and verifies[-1][1] == [Check("file_exists", "out.csv")]
    assert "cu-state" in sandboxes and CountingStateJudge.instances[-1].sandbox == "sandbox:cu-state"

    # a failing a11y assert is a heal reason, still without the VLM (the replacement keeps the assert)
    h3 = make_w2(tmp_data_dir, StubPlanner(), max_heals=1)
    h3.planner.plan = lambda goal, app, ts: Script(app, goal, (
        Step("find", target=Target("table cell", "A1")), Step("assert", target=Target("table cell", "nope"))))
    replans = []
    h3.planner.replan_step = lambda script, index, reason, ts: (replans.append((index, reason)) or Step("assert", target=Target("table cell", "nope")))
    state = run_cu_job(h3.graph, GOAL, APP, "cu-a11y-fail")
    assert state["ok"] is False and h3.judge.calls == [] and state["verify"]["source"] == "a11y"
    assert "element not found" in state["error"] and replans[0][0] == 1 and "element not found" in replans[0][1]

    # nothing declared: the VLM question is the tie-break (unchanged behaviour)
    h4 = make_w2(tmp_data_dir, StubPlanner())
    state = run_cu_job(h4.graph, GOAL, APP, "cu-vlm")
    assert state["ok"] is True and state["verify"]["source"] == "vlm" and len(h4.judge.calls) == 1


def test_step_candidates_pick_the_first_that_resolves_in_the_tree(tmp_data_dir):
    bad1 = Script(APP, GOAL, (Step("find", target=Target("table cell", "Q9")), Step("type", text="x")))
    bad2 = Script(APP, GOAL, (Step("find", target=Target("table cell", "Q8")), Step("type", text="y")))
    good = Script(APP, GOAL, (Step("find", target=Target("table cell", "A1")), Step("type", text="z")))
    planner = MilestonePlanner([milestone("fill", "A1")], [], candidates=[bad1, bad2, good])
    h = make_w2(tmp_data_dir, planner, step_candidates=3)
    state = run_cu_job(h.graph, GOAL, APP, "cu-cand")
    assert state["ok"] is True
    assert planner.candidate_calls == [("fill", 3)] and planner.next_calls == []
    assert [s["command"] for s in state["steps"]] == ["find target=table cell:A1", "type text='z'"]
    picked = [e for e in entries(h.log, "graph") if e["detail"].get("job_id") == "cu-cand" and "candidate" in e["detail"]]
    assert picked and picked[0]["detail"]["candidate"] == 2 and picked[0]["detail"]["candidates"] == 3

    # none resolves -> the first candidate, and step_candidates=1 never asks for candidates
    planner2 = MilestonePlanner([milestone("fill", "A1")], [], candidates=[bad1, bad2])
    h2 = make_w2(tmp_data_dir, planner2, step_candidates=2, max_heals=0)
    StubRunner.fail_texts = {"x"}
    state = run_cu_job(h2.graph, GOAL, APP, "cu-cand2")
    assert state["ok"] is False and state["steps"][0]["command"] == "find target=table cell:Q9"
    planner3 = MilestonePlanner([milestone("fill", "A1")], [good], candidates=[bad1])
    h3 = make_w2(tmp_data_dir, planner3, step_candidates=1)
    StubRunner.fail_texts = set()
    assert run_cu_job(h3.graph, GOAL, APP, "cu-cand3")["ok"] is True and planner3.candidate_calls == []


# ---------------------------------------------------------------- wave 3 (I3/I4/memory latency/X2)


def test_replay_skips_the_feasibility_gate_so_it_makes_zero_model_calls(tmp_data_dir):
    """I3: ``context`` runs the gate only for a fresh goal; a stored procedure (latest or a requested id)
    is replayed without the gate's model call."""
    gate = StubGate(True, "fine")
    h = make_w2(tmp_data_dir, StubPlanner(), feasibility=gate)
    first = run_cu_job(h.graph, GOAL, APP, "cu-g1")
    assert first["ok"] is True and first["cached"] is False
    assert gate.calls == [(GOAL, APP, "cu-g1")], "a fresh goal is gated once"

    second = run_cu_job(h.graph, GOAL, APP, "cu-g2")
    assert second["cached"] is True and second["ok"] is True
    assert gate.calls == [(GOAL, APP, "cu-g1")], "replay makes no gate call"
    skipped = [e for e in entries(h.log, "note") if e["detail"].get("job_id") == "cu-g2" and "feasibility skipped" in e["summary"]]
    assert len(skipped) == 1 and "stored procedure" in skipped[0]["summary"]

    third = run_cu_job(h.graph, GOAL, APP, "cu-g3", procedure_id=first["procedure_id"])
    assert third["cached"] is True and gate.calls == [(GOAL, APP, "cu-g1")], "a requested id skips the gate too"

    # a different (fresh) goal is still gated
    run_cu_job(h.graph, "Put 7 in cell A1", APP, "cu-g4")
    assert gate.calls[-1] == ("Put 7 in cell A1", APP, "cu-g4") and len(gate.calls) == 2


class DisplayBackend(StubBackend):
    def __init__(self, slot):
        super().__init__(slot)
        self.display = f":{99 + slot}"


class StubInterfaceBuilder:
    def __init__(self, version=3, raise_exc=None):
        self.version, self.raise_exc, self.calls = version, raise_exc, []

    def build(self, app, procedure, backend_api_doc, display, script=None):
        self.calls.append((app, procedure, backend_api_doc, display))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.version


def test_store_runs_the_interface_builder_once_after_a_fresh_store_and_never_on_replay(tmp_data_dir):
    """I4 / P7: after a fresh store the builder gets (app, stored procedure, backend API doc, slot-0 display)."""
    from o_engine.cu.graph import BACKEND_API_DOC

    builder = StubInterfaceBuilder(version=2)
    h = make_w2(tmp_data_dir, StubPlanner(), backend_cls=DisplayBackend, interface_builder=builder)
    assert CUDeps.__dataclass_fields__["interface_builder"].default is None
    state = run_cu_job(h.graph, GOAL, APP, "cu-b1")
    assert state["ok"] is True and state["cached"] is False
    assert len(builder.calls) == 1
    app, procedure, doc, display = builder.calls[0]
    assert app == APP and procedure is h.procedures.rows[0] and display == ":99"
    assert doc == BACKEND_API_DOC and all(w in doc for w in ("find", "invoke", "set_value", "tree"))
    built = [e for e in entries(h.log, "cache") if e["detail"].get("job_id") == "cu-b1" and "interface" in e["summary"]]
    assert len(built) == 1 and built[0]["detail"]["version"] == 2 and built[0]["detail"]["app"] == APP

    again = run_cu_job(h.graph, GOAL, APP, "cu-b2")
    assert again["cached"] is True and len(builder.calls) == 1, "replay never builds"


def test_interface_builder_failure_or_skip_never_changes_ok(tmp_data_dir):
    raising = StubInterfaceBuilder(raise_exc=RuntimeError("smoke failed"))
    h = make_w2(tmp_data_dir, StubPlanner(), backend_cls=DisplayBackend, interface_builder=raising)
    state = run_cu_job(h.graph, GOAL, APP, "cu-b3")
    assert state["ok"] is True and state["done"] is True and len(raising.calls) == 1
    assert len(h.procedures.rows) == 1 and state["procedure_id"] == h.procedures.rows[0].procedure_id
    notes = [e for e in entries(h.log, "note") if e["detail"].get("job_id") == "cu-b3" and "interface build" in e["summary"]]
    assert len(notes) == 1 and "RuntimeError" in notes[0]["summary"]

    skipping = StubInterfaceBuilder(version=None)
    h2 = make_w2(tmp_data_dir, StubPlanner(), backend_cls=DisplayBackend, interface_builder=skipping)
    state = run_cu_job(h2.graph, GOAL, APP, "cu-b4")
    assert state["ok"] is True and len(skipping.calls) == 1
    notes = [e for e in entries(h2.log, "note") if e["detail"].get("job_id") == "cu-b4" and "interface build" in e["summary"]]
    assert len(notes) == 1

    # no builder configured (server.py default): nothing logged, nothing called
    h3 = make_w2(tmp_data_dir, StubPlanner(), backend_cls=DisplayBackend)
    assert h3.deps.interface_builder is None
    state = run_cu_job(h3.graph, GOAL, APP, "cu-b5")
    assert state["ok"] is True
    assert not [e for e in entries(h3.log) if e["detail"].get("job_id") == "cu-b5" and "interface build" in e["summary"]]


class LocalAwareClient:
    """Shape of ``planner.ModelClient``: ``local_only()`` is the local-model-only view, ``complete_json`` falls
    through to the fallback."""

    def __init__(self, local_ok=True, has_local=True):
        self.local_calls, self.fallback_calls = [], []
        self.local_ok, self.has_local = local_ok, has_local

    def local_only(self):
        if not self.has_local:
            return None
        outer = self

        class _Local:
            def complete_json(self_, system, user):
                outer.local_calls.append(user)
                if not outer.local_ok:
                    raise RuntimeError("vllm down")
                return json.dumps({"facts": ["cell A1 holds 1"], "state": [], "todos": [], "risks": []})

        return _Local()

    def complete_json(self, system, user):
        self.fallback_calls.append(user)
        return json.dumps({"facts": ["from fallback"], "state": [], "todos": [], "risks": []})


def _milestone_harness(tmp_data_dir, client):
    planner = MilestonePlanner([milestone("fill A1", "A2")], [turn_script("A1", "1")])
    planner.client = client
    return make_w2(tmp_data_dir, planner, backend_cls=ChangingBackend, runner_cls=GrowingRunner)


def test_memory_extract_uses_the_local_model_only_and_never_the_fallback(tmp_data_dir):
    client = LocalAwareClient(local_ok=True)
    h = _milestone_harness(tmp_data_dir, client)
    state = run_cu_job(h.graph, GOAL, APP, "cu-mem1")
    assert state["ok"] is True
    assert len(client.local_calls) == 1 and client.fallback_calls == []
    notes = [e for e in entries(h.log, "note") if e["detail"].get("job_id") == "cu-mem1" and "working memory" in e["summary"]]
    assert notes[-1]["detail"]["facts"] == [f"Goal: {GOAL}", "cell A1 holds 1"]

    # local path raises and the fallback would succeed: memory unchanged, no fallback call
    client = LocalAwareClient(local_ok=False)
    h = _milestone_harness(tmp_data_dir, client)
    state = run_cu_job(h.graph, GOAL, APP, "cu-mem2")
    assert state["ok"] is True
    assert len(client.local_calls) == 1 and client.fallback_calls == []
    notes = [e for e in entries(h.log, "note") if e["detail"].get("job_id") == "cu-mem2" and "working memory" in e["summary"]]
    assert notes[-1]["detail"]["facts"] == [f"Goal: {GOAL}"]


def test_memory_extract_is_skipped_with_one_note_when_only_the_fallback_exists(tmp_data_dir):
    client = LocalAwareClient(has_local=False)
    h = _milestone_harness(tmp_data_dir, client)
    state = run_cu_job(h.graph, GOAL, APP, "cu-mem3")
    assert state["ok"] is True and client.fallback_calls == [] and client.local_calls == []
    skipped = [e for e in entries(h.log, "note") if e["detail"].get("job_id") == "cu-mem3" and "memory extract skipped" in e["summary"]]
    assert len(skipped) == 1
    notes = [e for e in entries(h.log, "note") if e["detail"].get("job_id") == "cu-mem3" and "working memory" in e["summary"]]
    assert notes[-1]["detail"]["facts"] == [f"Goal: {GOAL}"]


def test_summarize_tree_and_get_are_the_shared_definitions():
    """X2: one ``_get`` and one tree flattener for graph / judge / planner / feasibility."""
    import importlib

    from o_engine.cu import treeutil

    graph_mod = importlib.import_module("o_engine.cu.graph")
    assert graph_mod.summarize_tree is treeutil.summarize_tree and graph_mod._get is treeutil._get
    assert treeutil._get(None, "x", 1) == 1 and treeutil._get({"x": 2}, "x") == 2
    assert "table cell 'A1'" in treeutil.summarize_tree(StubBackend(0).tree())


def test_cudeps_comparative_reaches_best_of_n(tmp_data_dir):
    from o_engine.cu import graph as graph_mod
    assert graph_mod.CUDeps.__dataclass_fields__["comparative"].default is False
    StubBestOfN.seen_comparative = []
    h = make(tmp_data_dir, candidates=2)
    h.deps.comparative = True
    run_cu_job(h.graph, GOAL, APP, "cu-cmp", candidates=2)
    assert StubBestOfN.seen_comparative == [True]
