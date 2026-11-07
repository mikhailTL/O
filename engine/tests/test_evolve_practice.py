"""Practice loop (plan L2) with fakes: guards, spend caps, distillation (append-only), lessons, reverts, curve."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from o_engine.cu import meter as cu_meter
from o_engine.cu.interfaces import InterfaceNotes
from o_engine.cu.procedures import ProcedureStore
from o_engine.cu.schema import parse_script
from o_engine.evolve.curriculum import Curriculum
from o_engine.evolve.practice import (
    DollarCappedMeter,
    IsolatedPracticeEnvironment,
    PracticeEnvironment,
    PracticeRefused,
    Practicer,
    SpendLedger,
    budget_from_settings,
    distill,
    practice_deps,
)
from o_engine.evolve.skills import SkillRegistry


class Clock:
    def __init__(self, t: float = 2_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeEnv(PracticeEnvironment):
    def __init__(self, isolated: bool = True) -> None:
        self.display = ":150"
        self.backend = None
        self.isolated = isolated
        self.resets = 0

    @property
    def is_isolated(self) -> bool:
        return self.isolated

    def reset(self, item):
        self.resets += 1
        super().reset(item)


class FakeDeps:
    def __init__(self, procedures, notes) -> None:
        self.procedures = procedures
        self.notes = notes


FRESH_STEPS = [
    {"op": "launch", "cmd": "mousepad"},
    {"op": "find", "as": "editor", "target": {"role": "text"}},
    {"op": "type", "ref": "editor", "text": "alpha one\n"},
    {"op": "type", "ref": "editor", "text": "beta two\ngamma three\n"},
    {"op": "screenshot_assert", "question": "Does the editor show three lines?"},
    {"op": "key", "keys": "ctrl+s"},
]


class FakeGraph:
    """Stands in for cu.graph: a fresh plan costs model calls and stores a procedure; a replay costs one
    final screenshot question unless the script carries a state_assert. 'Executing' writes the file the
    item's check reads, unless ``break_next`` says this run should not."""

    def __init__(self, procedures: ProcedureStore, cost_per_call: float = 0.02) -> None:
        self.procedures = procedures
        self.cost = cost_per_call
        self.break_next = False
        self.fresh_calls = 4
        self.runs: list[dict] = []

    def __call__(self, item, job_id, meter):
        latest = self.procedures.latest(item.app, item.goal)
        calls = 0
        if latest is None:
            for _ in range(self.fresh_calls):
                meter.charge_paid("planner")
                meter.add_cost({"total_cost_usd": self.cost})
                calls += 1
            steps = list(FRESH_STEPS)
            cached = False
        else:
            raw = json.loads((self.procedures.dir / latest.path).read_text())["script"]
            steps = raw["steps"]
            cached = True
            if not any(s["op"] == "state_assert" for s in steps):
                meter.charge_paid("judge")
                meter.add_cost({"total_cost_usd": self.cost})
        for s in steps:
            if s["op"] == "screenshot_assert":
                meter.charge_paid("judge")
                meter.add_cost({"total_cost_usd": self.cost})
        ok = not self.break_next
        if ok and item.check is not None:
            Path(item.check.path).parent.mkdir(parents=True, exist_ok=True)
            Path(item.check.path).write_text("\n".join(item.check.expect) + "\n")
        self.break_next = False
        pid = latest.id if latest else None
        if ok and latest is None:
            pid = self.procedures.store(item.app, item.goal, {"app": item.app, "goal": item.goal, "steps": steps}).id
        rows = [{"command": f"{s['op']}", "exit_code": 0} for s in steps]
        if not ok:
            rows[-1] = {"command": "find as='editor' target=text view:None", "exit_code": 1}
        self.runs.append({"cached": cached, "steps": len(steps)})
        return {"ok": ok, "error": None if ok else "step 1 failed: element not found", "steps": rows, "cached": cached,
                "procedure_id": pid}


def _setup(tmp_path: Path, presence="USER_ABSENT", budget=1.0, env=None, clock=None):
    clock = clock or Clock()
    procs = ProcedureStore(tmp_path / "procedures")
    notes = InterfaceNotes(tmp_path / "interfaces")
    cur = Curriculum(tmp_path / "practice", interval_scale=0.0, now=clock, which=lambda n: "/usr/bin/" + n,
                     capability_map=None, offline_episodes=None)
    cur.refresh()
    graph = FakeGraph(procs)
    skills = SkillRegistry(tmp_path / "skills", now=clock)
    pr = Practicer(cur, tmp_path / "practice", env=env or FakeEnv(), deps=FakeDeps(procs, notes), run_job=graph,
                   budget_usd=budget, presence_fn=lambda: presence, now=clock, skills=skills)
    item = next(it for it in cur.items().values() if it.family == "editor.write_save")
    return pr, cur, graph, procs, notes, item, skills


# ------------------------------------------------------------------ guards

def test_refuses_when_user_present_or_unknown(tmp_path):
    pr, *_ , item, _ = _setup(tmp_path, presence="USER_PRESENT")
    assert pr.can_practice() == (False, "presence is USER_PRESENT")
    with pytest.raises(PracticeRefused):
        pr.attempt(item)
    assert pr.run()["stopped"] == "presence is USER_PRESENT"
    pr.presence_fn = lambda: "UNKNOWN"
    pr.idle_fn = lambda: 30.0
    assert pr.can_practice()[0] is False
    pr.idle_fn = lambda: 3600.0
    assert pr.can_practice() == (True, "idle 3600s >= 600s")


def test_refuses_without_isolated_display(tmp_path):
    pr, *_ , item, _ = _setup(tmp_path, env=FakeEnv(isolated=False))
    ok, why = pr.can_practice()
    assert not ok and "host display" in why


def test_isolated_environment_refuses_host_display(monkeypatch, tmp_path):
    monkeypatch.setenv("DISPLAY", ":141")
    env = IsolatedPracticeEnvironment(":141", profile_dir=tmp_path / "p")
    assert env.is_isolated is False
    with pytest.raises(PracticeRefused):
        env.start()


def test_budget_rolls_and_stops_the_session(tmp_path):
    clock = Clock()
    pr, cur, graph, *_ = _setup(tmp_path, budget=0.05, clock=clock)
    out = pr.run(max_items=10)
    assert out["stopped"].startswith("practice budget spent")
    assert pr.ledger.spent() >= 0.05
    assert out["spent_24h_usd"] <= 0.05 + 0.02 + 1e-9  # the per-item meter stops at the remaining budget (one call over at most)
    clock.t += 86400 + 1  # 24 h later the window has rolled
    assert pr.remaining_usd() == pytest.approx(0.05)


def test_dollar_capped_meter_refuses_next_paid_call():
    m = DollarCappedMeter("j", max_paid=None, max_usd=0.05)
    m.charge_paid("planner")
    m.add_cost({"total_cost_usd": 0.06})
    assert m.exhausted
    with pytest.raises(cu_meter.ModelCallCapReached):
        m.charge_paid("planner")
    m2 = DollarCappedMeter("j", max_paid=2, max_usd=10)
    m2.charge_paid("a")
    m2.charge_paid("b")
    with pytest.raises(cu_meter.ModelCallCapReached):
        m2.charge_paid("c")


def test_per_item_call_cap_is_enforced(tmp_path):
    pr, cur, graph, procs, notes, item, _ = _setup(tmp_path)
    graph.fresh_calls = 50
    a = pr.attempt(item)
    assert not a.ok and a.model_calls == item.budget.max_model_calls
    assert "ModelCallCapReached" in (a.error or "")


def test_budget_from_settings(monkeypatch):
    class S:
        practice_budget_usd = 1.25

    assert budget_from_settings(S()) == 1.25
    monkeypatch.setenv("O_PRACTICE_BUDGET_USD", "0.75")
    assert budget_from_settings(None) == 0.75


def test_spend_ledger_is_append_only(tmp_path):
    led = SpendLedger(tmp_path / "l.jsonl", now=lambda: 10.0)
    led.add("a", 0.1, 2)
    first = (tmp_path / "l.jsonl").read_text()
    led.add("b", 0.2, 1)
    assert (tmp_path / "l.jsonl").read_text().startswith(first)
    assert led.spent() == pytest.approx(0.3)


# ------------------------------------------------------------------ distillation

def test_distill_drops_paid_checks_merges_typing_adds_state_assert():
    raw = {"app": "mousepad", "goal": "g", "steps": FRESH_STEPS}
    from o_engine.evolve.curriculum import Check

    new, notes = distill(raw, Check(kind="file_lines", path="/tmp/practice/x.txt", expect=("a", "b")))
    ops = [s["op"] for s in new["steps"]]
    assert "screenshot_assert" not in ops
    assert ops.count("type") == 1 and new["steps"][2]["text"] == "alpha one\nbeta two\ngamma three\n"
    assert ops[-1] == "state_assert" and len(new["steps"][-1]["checks"]) == 2
    parse_script(new)  # still a valid OScript
    again, _ = distill(new, Check(kind="file_lines", path="/tmp/practice/x.txt", expect=("a", "b")))
    assert again is None  # nothing left to remove: no endless versions


def test_practice_learning_curve_distills_append_only_and_promotes(tmp_path):
    pr, cur, graph, procs, notes, item, skills = _setup(tmp_path)
    attempts = [pr.attempt(item) for _ in range(4)]
    assert [a.ok for a in attempts] == [True, True, True, True]
    calls = [a.model_calls for a in attempts]
    assert calls[0] == 5  # fresh plan (4) + its screenshot question
    assert calls[1:] == [0, 0, 0]  # replays of the distilled version are decided by state: zero model calls
    assert attempts[0].distilled_id and attempts[0].distilled_id.endswith("/v2")
    assert attempts[1].cached and attempts[1].procedure_id == attempts[0].distilled_id  # replayed the distilled v2
    versions = procs.versions(item.app, item.goal)
    assert [v.version for v in versions] == [1, 2]
    assert versions[1].parent == versions[0].id and versions[1].reason.startswith("practice distill")
    v1 = json.loads((procs.dir / versions[0].path).read_text())
    assert any(s["op"] == "screenshot_assert" for s in v1["script"]["steps"])  # v1 untouched
    curve = cur.learning_curve()[item.family]
    assert curve["first"]["model_calls"] > curve["last"]["model_calls"] and curve["improved"]
    assert graph.runs[1]["steps"] < graph.runs[0]["steps"]
    # promoted after 3 consecutive passes; versions are appended, never rewritten
    sk = skills.internal()[skills.skill_id(item.app, item.goal)]
    assert sk.status == "active" and sk.best_model_calls == 0
    statuses = [s.status for s in skills.internal_history(sk.id)]
    assert statuses[0] == "candidate" and "active" in statuses
    fit = [json.loads(x) for x in (tmp_path / "practice" / "fitness.jsonl").read_text().splitlines()]
    assert [r["ok"] for r in fit] == [True] * 4


def test_failure_writes_lesson_to_notes_and_reverts_regression(tmp_path):
    pr, cur, graph, procs, notes, item, _ = _setup(tmp_path)
    assert pr.attempt(item).ok  # v1 fresh, v2 distilled
    good = procs.latest(item.app, item.goal)
    # a later heal elsewhere appended a broken v3; the replay of v3 fails
    procs.store(item.app, item.goal, {"app": item.app, "goal": item.goal, "steps": [{"op": "key", "keys": "ctrl+q"}]},
                parent=good.id, healed_step=0, reason="bad heal")
    graph.break_next = True
    bad = pr.attempt(item)
    assert not bad.ok
    latest = procs.latest(item.app, item.goal)
    assert latest.version == 4 and latest.reason.startswith("practice revert") and good.id in latest.reason
    assert json.loads((procs.dir / latest.path).read_text())["script"] == json.loads((procs.dir / good.path).read_text())["script"]
    assert [v.version for v in procs.versions(item.app, item.goal)] == [1, 2, 3, 4]  # nothing deleted
    lesson = (tmp_path / "practice" / "lessons.jsonl").read_text()
    assert "practice pitfall" in lesson and "element not found" in lesson
    assert "practice pitfall" in notes.read(item.app)  # the next fresh plan reads it via for_prompt
    sch = cur.schedule(item.id)
    assert sch.box == 0 and sch.streak == 0  # failure: back to box 0, due soonest


def test_rules_turn_failure_classes_into_lessons(tmp_path):
    pr, *_ , item, _ = _setup(tmp_path)
    steps = [{"command": "wait for role='frame'", "exit_code": 1}]
    rules = Practicer.rules(item, "SandboxEscape: x resolves outside sandbox root", steps)
    assert any("launch mousepad first" in r for r in rules)
    assert any("through the app's own GUI" in r for r in rules)


def test_practice_deps_repoints_display_and_disables_spend_features():
    from o_engine.actionlog import ActionLog
    from o_engine.cu.graph import CUDeps

    base = CUDeps(backend_factory=lambda s: "HOST", grounder=None, judge=None, planner=None, procedures="P", notes="N",
                  log=ActionLog.__new__(ActionLog), candidates=3, step_candidates=2, interface_builder=object(),
                  displays=[":99", ":100"])
    env = FakeEnv()
    env.backend = "PRACTICE"
    env.desktop_home = "/tmp/practice/ws"
    d = practice_deps(base, env)
    assert d.backend_factory(0) == "PRACTICE" and d.displays == [":150"]
    assert d.candidates == 1 and d.step_candidates == 1 and d.interface_builder is None
    assert d.procedures == "P" and d.notes == "N"  # shared memory with real jobs
    sj = d.state_judge_factory(None, None)
    assert sj.desktop_home.endswith("/tmp/practice/ws")


def test_false_success_is_quarantined_and_next_attempt_plans_fresh(tmp_path):
    from o_engine.evolve.practice import QuarantinedProcedures

    pr, cur, graph, procs, notes, item, _ = _setup(tmp_path)
    view = QuarantinedProcedures(procs, pr.quarantine)
    graph.procedures = view  # the practice graph sees the quarantine view, as practice_deps builds it

    real_call = graph.__call__

    def wrong_then_right(it, job_id, meter, _n=[0]):
        _n[0] += 1
        out = real_call(it, job_id, meter)
        if _n[0] == 1:  # the app "looked done" but the file has the old text plus the new
            Path(it.check.path).write_text("old line\n" + "\n".join(it.check.expect) + "\n")
        return out

    pr.run_job = wrong_then_right
    first = pr.attempt(item)
    assert first.graph_ok and not first.check_ok and not first.ok
    assert first.procedure_id in pr.quarantine.ids()
    assert view.latest(item.app, item.goal) is None  # quarantined: the next practice run plans fresh
    lesson = (tmp_path / "practice" / "lessons.jsonl").read_text()
    assert "select all (ctrl+a)" in lesson and "check the saved file" in lesson
    second = pr.attempt(item)
    assert second.ok and not second.cached
    assert [v.version for v in procs.versions(item.app, item.goal)] == [1, 2, 3]  # v1 bad (kept), v2 fresh, v3 distilled
    assert view.latest(item.app, item.goal).version == 3


def test_rule_when_end_state_was_right_but_the_plan_check_failed(tmp_path):
    pr, *_ , item, _ = _setup(tmp_path)
    steps = [{"command": "launch mousepad", "exit_code": 0}, {"command": "state_assert", "exit_code": 1}]
    rules = Practicer.rules(item, "step 3 failed: state_assert failed: probe exited 1", steps, check_ok=True)
    assert any("already right" in r and "file_contains" in r for r in rules)
    assert not any("launch mousepad first" in r for r in rules)  # it did launch first
