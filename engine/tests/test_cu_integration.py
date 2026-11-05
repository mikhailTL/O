"""End-to-end computer-use graph with the real pieces and no monkeypatching.

Real ``FakeBackend`` (in-memory accessibility tree), real ``Planner`` over a counting fake
model client, real ``ScriptRunner``, real ``ProcedureStore`` and ``InterfaceNotes`` in a tmp
dir, no judge. The only fake is the model: it answers with canned JSON text.

The F3 tests below pin the one-shot path with ``LegacyPlanner`` (the milestone methods hidden),
so they stay deterministic whatever the real planner's milestone prompts look like. The wave-2
tests after them use the real ``StateJudge``, ``Sandbox``, ``NativeRunner`` and
``InterfaceStore`` with a scripted milestone planner.
"""
import json
import os

from o_engine.actionlog import ActionLog
from o_engine.cu.backends.base import Node
from o_engine.cu.backends.fake import FakeBackend
from o_engine.cu.graph import CUDeps, build_cu_graph, run_cu_job
from o_engine.cu.interfaces import InterfaceNotes, InterfaceStore
from o_engine.cu.judge import StateJudge
from o_engine.cu.planner import Planner
from o_engine.cu.procedures import ProcedureStore
from o_engine.cu.schema import Target, parse_script
from o_engine.executor import Sandbox
from o_engine.runners import NativeRunner


class LegacyPlanner(Planner):
    """The real planner with the milestone API hidden: the graph takes the one-shot ``plan()`` path."""

    plan_milestones = None
    next_steps = None
    candidate_steps = None

APP = "libreoffice-calc"
GOAL = "Put 42 in cell A1"

TREE = Node(
    id="n0", role="frame", name="Calc", states=["active"],
    children=[Node(id="n1", role="table cell", name="A1", states=["visible"])],
)


def script_json(cell: str) -> str:
    return json.dumps({
        "app": APP,
        "goal": GOAL,
        "steps": [
            {"op": "wait", "for": {"role": "frame", "name": "Calc"}, "timeout_s": 1},
            {"op": "find", "as": "cell", "target": {"role": "table cell", "name": cell}},
            {"op": "type", "ref": "cell", "text": "42"},
        ],
    })


def find_step_json(cell: str) -> str:
    return json.dumps({"op": "find", "as": "cell", "target": {"role": "table cell", "name": cell}})


class CountingModel:
    """``complete(system, user)`` answers from a queue and counts every call."""

    name = "fake-model"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0
        self.prompts = []

    def complete(self, system, user):
        self.calls += 1
        self.prompts.append((system, user))
        if not self.answers:
            raise AssertionError("model asked more often than scripted")
        return self.answers.pop(0)


def make(tmp_path, answers, max_heals=3):
    log = ActionLog(tmp_path / "data")
    backend = FakeBackend(TREE)
    notes = InterfaceNotes(tmp_path / "interfaces")
    store = ProcedureStore(tmp_path / "procedures", parse_script=parse_script)
    model = CountingModel(answers)
    deps = CUDeps(
        backend_factory=lambda slot: backend,
        grounder=None,
        judge=None,
        planner=LegacyPlanner(model, notes, parse_script=parse_script),
        procedures=store,
        notes=notes,
        log=log,
        max_heals=max_heals,
        candidates=1,
    )
    return build_cu_graph(deps), model, store, backend, log, notes


def phases(log, job_id):
    rows = [json.loads(line) for line in log.jsonl.read_text().splitlines()]
    return [r["detail"]["phase"] for r in rows if r["kind"] == "graph"
            and r["detail"].get("job_id") == job_id and r["summary"] == f"{job_id}: {r['detail']['phase']}"]


def version_files(store, app=APP, goal=GOAL):
    return sorted(store._goal_dir(app, goal).glob("v*.json"), key=lambda p: int(p.stem[1:]))


# ---------------------------------------------------------------- F3-a

def test_bad_plan_is_healed_with_one_replacement_step_and_stored(tmp_path):
    graph, model, store, backend, log, notes = make(tmp_path, [script_json("Z9"), find_step_json("A1")])
    state = run_cu_job(graph, GOAL, APP, "it-1", max_heals=3)

    assert state["ok"] is True and state["done"] is True and state["phase"] == "report", state
    assert state["attempt"] == 1 and state["cached"] is False
    assert model.calls == 2, "one plan + one replan"
    ph = phases(log, "it-1")
    assert ph == ["context", "plan", "ground", "execute", "judge", "heal", "execute", "judge", "store", "report"]
    for want in ("judge", "heal", "execute", "judge", "store", "report"):
        assert want in ph

    # the failing step was index 1 and its replacement targets the existing cell
    assert [s["exit_code"] for s in state["steps"]] == [0, 0, 0]
    assert "table cell:A1" in state["steps"][1]["command"]
    assert backend.actions[-1] == ("type_text", "n1", "42")
    assert backend.find(parse_script(script_json("A1")).steps[1].target).text == "42"

    # replan prompt carried the failure and the live tree
    system, user = model.prompts[1]
    assert "Failed step index: 1" in user and "element not found" in user and "table cell 'A1'" in user

    # stored once, as v1, with heal lineage recorded on it
    procs = store.list()
    assert len(procs) == 1
    v1 = procs[0]
    assert v1.version == 1 and v1.parent is None and v1.healed_step == 1 and "element not found" in v1.reason
    assert state["procedure_id"] == v1.id
    stored = store.load_script(v1)
    assert stored.steps[1].target.name == "A1"
    assert "run it-1 — ok healed" in notes.read(APP)


def test_replay_of_stored_procedure_makes_zero_model_calls(tmp_path):
    graph, model, store, *_ = make(tmp_path, [script_json("A1")])
    first = run_cu_job(graph, GOAL, APP, "it-a")
    assert first["ok"] is True and model.calls == 1
    second = run_cu_job(graph, GOAL, APP, "it-b")
    assert second["ok"] is True and second["cached"] is True and model.calls == 1
    assert second["procedure_id"] == first["procedure_id"] and len(store.list()) == 1


# ---------------------------------------------------------------- F3-b

def test_failed_heals_of_a_replay_leave_a_full_version_chain_on_disk(tmp_path):
    graph, model, store, backend, log, _ = make(tmp_path, [find_step_json("Q7"), find_step_json("Q8")], max_heals=2)
    v1 = store.store(APP, GOAL, parse_script(script_json("Z9")))
    v1_path = store.dir / v1.path
    v1_bytes = v1_path.read_bytes()

    state = run_cu_job(graph, GOAL, APP, "it-f", max_heals=2)
    assert state["ok"] is False and state["attempt"] == 2 and state["phase"] == "report"
    assert model.calls == 2
    files = version_files(store)
    assert [p.name for p in files] == ["v1.json", "v2.json", "v3.json"]
    assert v1_path.read_bytes() == v1_bytes, "v1 is never rewritten"
    chain = store.versions(APP, GOAL)
    assert [p.version for p in chain] == [1, 2, 3]
    for k in range(1, len(chain)):
        assert chain[k].parent == chain[k - 1].id
        assert chain[k].healed_step == 1 and chain[k].reason and "element not found" in chain[k].reason
    assert store.load_script(chain[1]).steps[1].target.name == "Q7"
    assert store.load_script(chain[2]).steps[1].target.name == "Q8"
    assert state["procedure_id"] == chain[2].id
    ph = phases(log, "it-f")
    assert ph.count("heal") == 2 and "store" not in ph and ph[-1] == "report"


def test_one_successful_heal_of_a_replay_writes_exactly_v2(tmp_path):
    graph, model, store, backend, log, _ = make(tmp_path, [find_step_json("A1")])
    v1 = store.store(APP, GOAL, parse_script(script_json("Z9")))

    state = run_cu_job(graph, GOAL, APP, "it-h", max_heals=3)
    assert state["ok"] is True and state["attempt"] == 1 and model.calls == 1
    files = version_files(store)
    assert [p.name for p in files] == ["v1.json", "v2.json"], "store node must not add a v3 for the healed script"
    v2 = store.versions(APP, GOAL)[1]
    assert v2.parent == v1.id and v2.healed_step == 1 and "element not found" in v2.reason
    assert state["procedure_id"] == v2.id
    assert phases(log, "it-h")[-2:] == ["store", "report"]

    # a later plain run replays v2 with no model calls
    again = run_cu_job(graph, GOAL, APP, "it-h2")
    assert again["ok"] is True and again["cached"] is True and again["procedure_id"] == v2.id
    assert model.calls == 1 and len(version_files(store)) == 2


def test_requested_procedure_id_replays_v1_when_v2_exists(tmp_path):
    graph, model, store, backend, log, _ = make(tmp_path, [])
    v1 = store.store(APP, GOAL, parse_script(script_json("A1")))
    v2 = store.store(APP, GOAL, parse_script(script_json("A1")), parent=v1.id, healed_step=1, reason="x")
    assert store.latest(APP, GOAL).id == v2.id
    state = run_cu_job(graph, GOAL, APP, "it-r", procedure_id=v1.id)
    assert state["ok"] is True and state["cached"] is True and state["procedure_id"] == v1.id
    assert model.calls == 0 and len(version_files(store)) == 2


# ---------------------------------------------------------------- wave 2 (I-I): real pieces, scripted milestone planner


def entries(log, kind=None, job_id=None):
    rows = [json.loads(line) for line in log.jsonl.read_text().splitlines()]
    return [r for r in rows if (kind is None or r["kind"] == kind) and (job_id is None or r["detail"].get("job_id") == job_id)]


def _tree_with(cells):
    kids = [Node(id=f"n{i}", role="table cell", name=c, states=["visible"]) for i, c in enumerate(cells, start=1)]
    return Node(id="n0", role="frame", name="Calc", states=["active"], children=kids)


class MilestonePlanner:
    """Scripted P1 planner: three milestones; each turn types into the cell the milestone names.

    ``done_when`` is the *next* cell appearing in the tree; the backend below grows the tree by one
    cell after every ``type`` so a milestone passes only once its step really ran."""

    def __init__(self, milestones, turn_scripts, replacements=None):
        self.milestones = list(milestones)
        self.turn_scripts = list(turn_scripts)
        self.replacements = list(replacements or [])
        self.calls = {"plan_milestones": 0, "next_steps": 0, "replan_step": 0, "plan": 0}
        self.loop_evidence_seen = []

    def plan_milestones(self, goal, app, notes, memory=None):
        self.calls["plan_milestones"] += 1
        return tuple(self.milestones)

    def next_steps(self, milestone, tree_summary, memory, notes, interfaces=(), max_steps=5):
        self.calls["next_steps"] += 1
        raw = self.turn_scripts.pop(0)
        return parse_script(raw if isinstance(raw, str) else json.dumps(raw))

    def plan(self, goal, app, tree_summary=""):
        self.calls["plan"] += 1
        raise AssertionError("one-shot plan() must not be used when the milestone API exists")

    def replan_step(self, script, index, reason, tree_summary, loop_evidence=None):
        self.calls["replan_step"] += 1
        self.loop_evidence_seen.append(loop_evidence)
        return self.replacements.pop(0)

    def replace_step(self, script, index, step):
        sdict = script.model_dump(by_alias=True, exclude_none=True)
        steps = list(sdict["steps"])
        steps[index] = step
        sdict["steps"] = steps
        return parse_script(json.dumps(sdict))

    def parse_script(self, raw):
        return parse_script(raw)


class GrowingBackend(FakeBackend):
    """After every ``type_text`` a new cell appears (A1 -> A2 -> A3 -> A4): the tree changes per action."""

    def type_text(self, node, text):
        super().type_text(node, text)
        n = len(list(self._roots[0].children)) + 1
        self.add_node("n0", Node(id=f"n{n}", role="table cell", name=f"A{n}", states=["visible"]))


def turn(cell, text):
    return {"app": APP, "goal": GOAL, "steps": [
        {"op": "find", "as": "cell", "target": {"role": "table cell", "name": cell}},
        {"op": "type", "ref": "cell", "text": text},
    ]}


class CountingJudge:
    def __init__(self, answer=True):
        self.calls = []
        self.answer = answer
        self.last_source = "vlm"

    def ask(self, png, question):
        self.calls.append(question)
        return self.answer, "vlm says so"


def make_w2(tmp_path, planner, backend, judge=None, max_heals=3, max_turns=20, feasibility=None, interface_store=None,
            runner=None, sandbox_root=None):
    log = ActionLog(tmp_path / "data")
    notes = InterfaceNotes(tmp_path / "interfaces")
    store = ProcedureStore(tmp_path / "procedures", parse_script=parse_script)
    root = sandbox_root or (tmp_path / "sandbox")
    sandboxes = {}

    def sandbox_for(job_id):
        return sandboxes.setdefault(job_id, Sandbox(root))

    deps = CUDeps(
        backend_factory=lambda slot: backend, grounder=None, judge=judge, planner=planner, procedures=store,
        notes=notes, log=log, max_heals=max_heals, candidates=1,
        feasibility=feasibility, state_judge_factory=lambda sb, be: StateJudge(sb, be),
        interface_store=interface_store, runner=runner, sandbox_for=sandbox_for, steps_per_turn=5, max_turns=max_turns,
    )
    return build_cu_graph(deps), store, log


MILESTONES = [
    {"goal": "fill A1", "done_when": {"role": "table cell", "name": "A2"}},
    {"goal": "fill A2", "done_when": {"role": "table cell", "name": "A3"}},
    {"goal": "fill A3", "done_when": {"role": "table cell", "name": "A4"}},
]


def test_three_milestones_complete_only_when_each_done_when_passes(tmp_path):
    """P1 acceptance: a tree that changes per action, one ``plan`` entry per turn, zero VLM calls."""
    judge = CountingJudge()
    planner = MilestonePlanner(MILESTONES, [turn("A1", "1"), turn("A2", "2"), turn("A3", "3")])
    backend = GrowingBackend(_tree_with(["A1"]))
    graph, store, log = make_w2(tmp_path, planner, backend, judge=judge)
    state = run_cu_job(graph, GOAL, APP, "m-1", max_heals=3)
    assert state["ok"] is True and state["phase"] == "report" and state["attempt"] == 0, state
    assert planner.calls == {"plan_milestones": 1, "next_steps": 3, "replan_step": 0, "plan": 0}
    assert phases(log, "m-1") == ["context", "plan", "ground", "execute", "plan", "plan", "judge", "store", "report"]
    assert [a for a in backend.actions if a[0] == "type_text"] == [("type_text", "n1", "1"), ("type_text", "n2", "2"), ("type_text", "n3", "3")]
    assert [s["exit_code"] for s in state["steps"]] == [0] * 6
    assert judge.calls == [], "done_when targets decide through the tree; the VLM is never asked"
    assert state["verify"]["ok"] is True and state["verify"]["source"] == "a11y"
    verdicts = [e for e in entries(log, "verify", "m-1") if "milestone" in e["detail"]]
    assert [(e["detail"]["milestone"], e["detail"]["ok"]) for e in verdicts] == [(0, True), (1, True), (2, True)]
    memory_notes = [e for e in entries(log, "note", "m-1") if "working memory" in e["summary"]]
    assert len(memory_notes) == 3 and memory_notes[0]["detail"]["facts"] == [f"Goal: {GOAL}"]
    # the six steps were stored as one procedure, and replaying it makes zero planner calls
    procs = store.list()
    assert len(procs) == 1 and len(store.load_script(procs[0]).steps) == 6
    stored = store.load_script(procs[0])
    assert [getattr(s, "as_", None) for s in stored.steps if s.op == "find"] == ["cell", "cell_t2", "cell_t3"]
    backend.replace_tree(_tree_with(["A1", "A2", "A3"]))
    backend.actions.clear()
    again = run_cu_job(graph, GOAL, APP, "m-2")
    assert again["ok"] is True and again["cached"] is True
    assert planner.calls["next_steps"] == 3 and planner.calls["plan_milestones"] == 1
    assert len([a for a in backend.actions if a[0] == "type_text"]) == 3
    assert "plan" not in phases(log, "m-2")[3:], "replay enters no milestone loop"


def test_milestone_not_done_is_retried_until_max_turns_then_healed(tmp_path):
    """A turn whose steps run but do not satisfy ``done_when`` costs a turn; ``max_turns`` bounds it."""
    judge = CountingJudge()
    # the first turn types into A1 (tree grows to A2: milestone 0 done); milestone 1 wants A3 but the
    # next two turns only press keys (no growth) -> max_turns=3 exhausts -> judge fails -> heal replaces
    # the last step with the type that grows the tree.
    key_turn = {"app": APP, "goal": GOAL, "steps": [{"op": "key", "keys": "Down"}]}
    planner = MilestonePlanner(
        [MILESTONES[0], MILESTONES[1]], [turn("A1", "1"), key_turn, key_turn],
        replacements=[{"op": "key", "keys": "Up"}],
    )
    backend = GrowingBackend(_tree_with(["A1"]))
    graph, store, log = make_w2(tmp_path, planner, backend, judge=judge, max_heals=1, max_turns=3)
    state = run_cu_job(graph, GOAL, APP, "m-t", max_heals=1)
    assert state["ok"] is False and state["attempt"] == 1
    assert "milestone 1" in state["error"] and "not done after 3 turn(s)" in state["error"]
    assert planner.calls["next_steps"] == 3 and planner.calls["replan_step"] == 1
    assert judge.calls == []
    assert store.list() == []


def test_infeasible_goal_reports_without_any_click_or_type(tmp_path):
    class Gate:
        def __init__(self):
            self.calls = []

        def check(self, goal, app, job_id):
            self.calls.append((goal, app, job_id))
            from o_engine.cu.feasibility import FeasibilityVerdict
            return FeasibilityVerdict(feasible=False, reason="no such app on this machine")

    planner = MilestonePlanner(MILESTONES, [turn("A1", "1")])
    backend = GrowingBackend(_tree_with(["A1"]))
    gate = Gate()
    graph, store, log = make_w2(tmp_path, planner, backend, feasibility=gate)
    state = run_cu_job(graph, "Open the Foo app and click Bar", "foo", "inf-1")
    assert state["ok"] is False and state["done"] is True and state["phase"] == "report"
    assert state["infeasible"] is True and state["error"] == "no such app on this machine"
    assert gate.calls == [("Open the Foo app and click Bar", "foo", "inf-1")]
    assert backend.actions == [] and state["steps"] == []
    assert planner.calls == {"plan_milestones": 0, "next_steps": 0, "replan_step": 0, "plan": 0}
    assert entries(log, "tool", "inf-1") == [] and entries(log, "result", "inf-1") == []
    assert phases(log, "inf-1")[-1] == "report" and "heal" not in phases(log, "inf-1")
    assert store.list() == []


def _save_turn(path, checks):
    return {"app": APP, "goal": GOAL, "steps": [
        {"op": "find", "as": "cell", "target": {"role": "table cell", "name": "A1"}},
        {"op": "type", "ref": "cell", "text": "42"},
        {"op": "state_assert", "checks": checks},
    ]}


def test_state_check_decides_with_zero_vlm_calls_and_removed_file_flips_to_heal(tmp_path):
    """P4 acceptance: ``state_assert file_exists`` passes only when the sandbox has the file."""
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "out.csv").write_text("a,b\n")
    judge = CountingJudge()
    checks = [{"kind": "file_exists", "arg": "out.csv"}]
    planner = MilestonePlanner([{"goal": "save", "done_when": {"role": "table cell", "name": "A2"}}], [_save_turn("out.csv", checks)])
    backend = GrowingBackend(_tree_with(["A1"]))
    graph, store, log = make_w2(tmp_path, planner, backend, judge=judge, sandbox_root=root)
    state = run_cu_job(graph, GOAL, APP, "st-1")
    assert state["ok"] is True and judge.calls == [], state
    assert state["steps"][2]["exit_code"] == 0 and "state:" in state["steps"][2]["stdout_tail"]

    # same stored procedure, file gone: the verdict flips and the heal loop starts (the planner's
    # replacement keeps the same check, so the heal fails too and the VLM is still never asked)
    (root / "out.csv").unlink()
    planner.replacements = [{"op": "state_assert", "checks": checks}]
    backend.replace_tree(_tree_with(["A1"]))
    state2 = run_cu_job(graph, GOAL, APP, "st-2", max_heals=1)
    assert state2["ok"] is False and state2["attempt"] == 1 and state2["cached"] is False
    assert "state_assert failed" in state2["error"] and "file not found" in state2["error"]
    assert planner.calls["replan_step"] == 1 and judge.calls == []
    assert phases(log, "st-2").count("heal") == 1
    assert [s["exit_code"] for s in state2["steps"]] == [0, 0, 1]
    assert state2["verify"]["source"] == "executor"


def test_never_changing_tree_stops_with_loop_after_three_repeats(tmp_path):
    """P5 acceptance: 10 identical key presses on a static tree -> ``loop`` at the third, cuMaxHeals respected, v1 untouched."""
    ten = {"app": APP, "goal": GOAL, "steps": [{"op": "key", "keys": "Down"}] * 10}
    judge = CountingJudge()
    planner = MilestonePlanner([{"goal": "scroll", "done_when": {"role": "table cell", "name": "A9"}}], [ten],
                               replacements=[{"op": "key", "keys": "Down"}, {"op": "key", "keys": "Down"}])
    backend = FakeBackend(_tree_with(["A1"]))
    graph, store, log = make_w2(tmp_path, planner, backend, judge=judge, max_heals=2)
    v1 = store.store(APP, "other goal", parse_script(script_json("A1")))
    v1_bytes = (store.dir / v1.path).read_bytes()
    state = run_cu_job(graph, GOAL, APP, "lp-1", max_heals=2)
    assert state["ok"] is False and state["attempt"] == 2
    # each heal resumes at the replaced step (steps before it are never re-run): refused at 2, then 4, then 6
    assert "step 6 looped" in state["error"] and "loop" in state["error"] and "gave up after 2 heal(s)" in state["error"]
    assert state["steps"][-1]["status"] == "loop" and len(state["steps"][-1]["evidence"]) == 3
    assert [s["index"] for s in state["steps"]] == [0, 1, 2, 3, 4, 5, 6]
    assert len(backend.actions) == 2 * 3, "two keys execute per attempt; the third is refused each time"
    assert planner.calls["replan_step"] == 2
    assert all(ev and len(ev) == 3 for ev in planner.loop_evidence_seen), planner.loop_evidence_seen
    assert judge.calls == []
    assert (store.dir / v1.path).read_bytes() == v1_bytes and len(store.list()) == 1
    rem = entries(log, "remediate", "lp-1")
    assert len(rem) == 2 and all(r["detail"]["loop"] for r in rem)


def test_script_step_runs_through_runner_on_the_isolated_display_only(tmp_path, monkeypatch):
    """P8: ``script`` -> runner with DISPLAY = the backend's isolated display, never the host's."""
    monkeypatch.setenv("DISPLAY", ":0")
    seen = []

    class SpyRunner(NativeRunner):
        def run(self, payload, sandbox, index=0, env_extra=None, isolated_display=False):
            seen.append({"env_extra": dict(env_extra or {}), "isolated_display": isolated_display, "index": index,
                         "language": payload.language, "sandbox": sandbox.root})
            return super().run(payload, sandbox, index, env_extra, isolated_display)

    root = tmp_path / "sandbox"
    root.mkdir()
    code = "import os, pathlib\npathlib.Path('out.txt').write_text(os.environ.get('DISPLAY', 'unset'))\n"
    script_turn = {"app": APP, "goal": GOAL, "steps": [
        {"op": "script", "script": {"language": "python", "code": code, "target_directory": ".", "timeout_seconds": 30}},
        {"op": "state_assert", "checks": [{"kind": "file_contains", "arg": "out.txt", "expect": ":97"}]},
    ]}
    judge = CountingJudge()
    planner = MilestonePlanner([{"goal": "write", "done_when": {"role": "frame", "name": "Calc"}}], [script_turn])
    backend = FakeBackend(_tree_with(["A1"]), display=":97")
    graph, store, log = make_w2(tmp_path, planner, backend, judge=judge, runner=SpyRunner(), sandbox_root=root)
    state = run_cu_job(graph, GOAL, APP, "sc-1")
    assert state["ok"] is True, state
    assert seen == [{"env_extra": {"DISPLAY": ":97"}, "isolated_display": False, "index": 0, "language": "python",
                     "sandbox": root.resolve()}]
    assert (root / "out.txt").read_text() == ":97", "the script saw the isolated display, not the host's :0"
    assert judge.calls == []
    tool = [e for e in entries(log, "tool", "sc-1") if e["detail"].get("op") == "script"]
    result = [e for e in entries(log, "result", "sc-1") if e["detail"].get("op") == "script"]
    assert len(tool) == 1 and len(result) == 1 and result[0]["detail"]["ok"] is True
    assert "python" in tool[0]["summary"] and "chars in ." in tool[0]["summary"]


def test_call_step_dispatches_into_generated_api(tmp_path):
    """P7: ``call`` executes the newest ``interfaces/<app>/vN/api.py`` against the backend."""
    istore = InterfaceStore(tmp_path / "ifaces")
    istore.write(APP, [{
        "name": "set_cell", "description": "type into a cell", "signature": "(backend, cell, value)",
        "body": "node = backend.find({'role': 'table cell', 'name': cell})\nbackend.set_value(node, value)\nreturn 'set ' + cell",
    }])
    call_turn = {"app": APP, "goal": GOAL, "steps": [
        {"op": "call", "fn": "calc.set_cell", "args": {"cell": "A1", "value": "99"}},
        {"op": "assert", "target": {"role": "table cell", "name": "A1"}, "state": "visible"},
    ]}
    judge = CountingJudge()
    planner = MilestonePlanner([{"goal": "fill", "done_when": {"role": "table cell", "name": "A1"}}], [call_turn])
    backend = FakeBackend(_tree_with(["A1"]))
    graph, store, log = make_w2(tmp_path, planner, backend, judge=judge, interface_store=istore)
    state = run_cu_job(graph, GOAL, APP, "call-1")
    assert state["ok"] is True, state
    assert backend.actions == [("set_value", "n1", "99")]
    assert backend.find(Target(role="table cell", name="A1")).text == "99"
    assert "call calc.set_cell -> set A1" in state["steps"][0]["stdout_tail"]
    assert judge.calls == [], "the declared a11y assert decides; no VLM call"

    # an unknown function is a structured failure that goes to heal, never an exception
    planner.turn_scripts = [{"app": APP, "goal": GOAL, "steps": [{"op": "call", "fn": "calc.nope", "args": {}}]}]
    planner.replacements = [{"op": "call", "fn": "calc.nope", "args": {}}]
    planner.milestones = [{"goal": "fill", "done_when": {"role": "table cell", "name": "ZZ"}}]
    state2 = run_cu_job(graph, "Different goal entirely", APP, "call-2", max_heals=1)
    assert state2["ok"] is False and state2["attempt"] == 1 and "unknown function 'calc.nope'" in state2["error"]
    assert state2["steps"][-1]["exit_code"] == 1 and "not in api.json" in state2["steps"][-1]["stderr_tail"]
