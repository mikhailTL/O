"""Curriculum (plan L2): sources, safety filter, ground-truth checks, spaced repetition, learning curve."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from o_engine.cu.interfaces import InterfaceStore
from o_engine.cu.procedures import ProcedureStore
from o_engine.evolve.curriculum import (
    FAIL_INTERVAL_S,
    INTERVALS_S,
    MASTERED_BOX,
    Attempt,
    Check,
    Curriculum,
    Environment,
    PracticeItem,
    evaluate_check,
    item_id,
    reset_workspace,
)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _cur(tmp_path: Path, **kw) -> Curriculum:
    kw.setdefault("which", lambda name: f"/usr/bin/{name}")
    return Curriculum(tmp_path / "practice", **kw)


def _attempt(item: PracticeItem, n: int, ok: bool, ts: float, **kw) -> Attempt:
    return Attempt(item_id=item.id, family=item.family, attempt=n, ts=ts, ok=ok, **kw)


def test_builtin_families_are_local_isolated_and_checkable(tmp_path):
    cur = _cur(tmp_path, capability_map=None, offline_episodes=None)
    items = cur.family_items()
    assert len(items) >= 3
    for it in items:
        assert it.environment.display == "isolated"
        assert it.check is not None and it.runnable
        assert it.environment.workspace.startswith(str(tmp_path / "practice" / "ws"))
        assert it.check.path.startswith(it.environment.workspace)  # the check reads only the practice workspace
        assert it.budget.max_usd > 0 and it.budget.max_model_calls > 0 and it.budget.max_seconds > 0


def test_missing_app_makes_family_unrunnable(tmp_path):
    cur = _cur(tmp_path, which=lambda name: None, capability_map=None, offline_episodes=None)
    assert all(not it.runnable and "mousepad" in it.reason for it in cur.family_items())


def test_offline_episodes_recorded_but_not_runnable(tmp_path):
    ep = tmp_path / "eps" / "ep-a"
    ep.mkdir(parents=True)
    (ep / "meta.json").write_text(json.dumps({"instruction": "Click Sum", "app": "libreoffice_calc", "result": False, "steps": []}))
    cur = _cur(tmp_path, offline_episodes=tmp_path / "eps", capability_map=None)
    (it,) = cur.fixture_items()
    assert it.origin == "fixture" and not it.runnable and "screenshot-only" in it.reason


def test_reset_workspace_refuses_non_practice_dirs(tmp_path):
    with pytest.raises(ValueError):
        reset_workspace(Environment(workspace=str(tmp_path / "home" / "user")))
    ws = tmp_path / "practice" / "ws" / "x"
    ws.mkdir(parents=True)
    (ws / "junk.txt").write_text("old")
    reset_workspace(Environment(setup="notes", workspace=str(ws)))
    assert not (ws / "junk.txt").exists()
    assert (ws / "notes" / "ideas.txt").read_text().startswith("App ideas")  # tools/e2e/fixtures.py make_notes


def test_evaluate_check_kinds(tmp_path):
    p = tmp_path / "f.txt"
    assert evaluate_check(None)[0] is False
    assert evaluate_check(Check(kind="file_exists", path=str(p)))[0] is False
    p.write_text("a\nb\n")
    assert evaluate_check(Check(kind="file_exists", path=str(p)))[0]
    assert evaluate_check(Check(kind="file_lines", path=str(p), expect=("a", "b")))[0]
    assert not evaluate_check(Check(kind="file_lines", path=str(p), expect=("a",)))[0]
    assert evaluate_check(Check(kind="file_contains", path=str(p), expect=("b",)))[0]
    assert not evaluate_check(Check(kind="file_not_contains", path=str(p), expect=("b",)))[0]


def _write_experience(data: Path) -> None:
    (data / "learning").mkdir(parents=True)
    obs = [
        {"id": "o1", "ts": 1.0, "source": "task", "text": "Write a list and save it as ~/lisbon.txt", "task_id": "j1",
         "outcome": "failed", "app": "mousepad", "duration_s": 40.0},
        {"id": "o2", "ts": 2.0, "source": "task", "text": "Type notes and save as ~/slow.txt", "task_id": "j2",
         "outcome": "ok", "app": "mousepad", "duration_s": 400.0},
        {"id": "o3", "ts": 3.0, "source": "task", "text": "Fill the form and save as ~/costly.txt", "task_id": "j3",
         "outcome": "ok", "app": "mousepad", "duration_s": 10.0},
        {"id": "o4", "ts": 4.0, "source": "task", "text": "Send the report by email to my boss", "task_id": "j4",
         "outcome": "failed", "app": "thunderbird", "duration_s": 5.0},
        {"id": "o5", "ts": 5.0, "source": "task", "text": "Quick save as ~/fast.txt", "task_id": "j5",
         "outcome": "ok", "app": "mousepad", "duration_s": 3.0},
    ]
    (data / "learning" / "observations.jsonl").write_text("\n".join(json.dumps(o) for o in obs) + "\n")
    rows = [{"kind": "task", "summary": "j3: done", "detail": {"job_id": "j3", "ok": True, "cost_usd": 0.6,
                                                               "model_calls": {"paid": 9}, "cached": False}},
            {"kind": "task", "summary": "j5: done", "detail": {"job_id": "j5", "ok": True, "cost_usd": 0.0,
                                                               "model_calls": {"paid": 0}, "cached": True}}]
    (data / "engine-actions.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_experience_items_from_failures_slow_and_expensive(tmp_path):
    data = tmp_path / "data"
    _write_experience(data)
    cur = _cur(tmp_path, data_dir=data, capability_map=None, offline_episodes=None)
    items = {it.source_ref: it for it in cur.experience_items()}
    assert items["j1"].origin == "failure" and items["j1"].runnable
    assert items["j2"].origin == "slow"
    assert items["j3"].origin == "expensive"
    assert "j5" not in items  # fast, cheap, cached: nothing to practise
    # user paths are moved into the practice workspace: practice never touches the user's files
    for ref in ("j1", "j2", "j3"):
        it = items[ref]
        assert "~/" not in it.goal and it.environment.workspace in it.goal
        assert it.check is not None and it.check.path.startswith(str(tmp_path / "practice" / "ws"))
    # sending email is never practised
    assert not items["j4"].runnable and "message" in items["j4"].reason


def test_lineage_items_for_healed_procedures_and_low_fitness(tmp_path):
    procs = ProcedureStore(tmp_path / "procs", parse_script=lambda raw: raw)
    script = {"app": "mousepad", "goal": "g", "steps": [{"op": "key", "keys": "ctrl+s"}]}
    v1 = procs.store("mousepad", "g", script)
    procs.store("mousepad", "g", script, parent=v1.id, healed_step=0, reason="broke")
    ifaces = InterfaceStore(tmp_path / "ifaces")
    ifaces.write("mousepad", [{"name": "save", "signature": "(backend)", "body": "pass"}])
    ifaces.write_fitness("mousepad", 4, 2)
    cur = _cur(tmp_path, procedures=procs, interfaces=ifaces, capability_map=None, offline_episodes=None)
    origins = sorted(it.origin for it in cur._lineage_items())
    assert origins == ["fitness", "healed"]


def test_gaps_raise_priority_and_refresh_is_append_only(tmp_path):
    cmap = tmp_path / "cap.json"
    cmap.write_text(json.dumps({"subsystems": {"cu_planning": {}, "voice": {}}}))
    cur = _cur(tmp_path, capability_map=cmap, offline_episodes=None)
    assert cur.unmeasured_subsystems() == {"cu_planning", "voice"}
    assert cur.gap_report()["voice"] == []  # no local family covers voice: a real, reported gap
    counts = cur.refresh()
    assert counts.get("gap", 0) >= 1
    before = cur.items_path.read_text()
    assert cur.refresh() == {}  # nothing new -> nothing appended
    assert cur.items_path.read_text() == before


def test_spaced_repetition_failures_sooner_mastery_rare(tmp_path):
    clock = Clock()
    cur = _cur(tmp_path, now=clock, capability_map=None, offline_episodes=None)
    cur.add(cur.family_items()[:2])
    a, b = sorted(cur.items().values(), key=lambda i: i.family)
    sch = cur.record(_attempt(a, 1, False, clock.t))
    assert sch.box == 0 and sch.due_ts == clock.t + FAIL_INTERVAL_S
    sch = cur.record(_attempt(b, 1, True, clock.t))
    assert sch.box == 1 and sch.due_ts == clock.t + INTERVALS_S[0]
    assert FAIL_INTERVAL_S < INTERVALS_S[0]  # a failure comes back before a pass does
    clock.t += FAIL_INTERVAL_S + 1
    due = [it.id for it, _ in cur.queue()]
    assert due == [a.id]  # the failed item is due, the passed one is not yet
    t = clock.t
    for n in range(2, 2 + MASTERED_BOX):
        sch = cur.record(_attempt(a, n, True, t))
    assert sch.mastered and sch.box == MASTERED_BOX
    assert sch.due_ts - t == INTERVALS_S[MASTERED_BOX - 1] >= 86400  # mastered: rare re-checks
    sch = cur.record(_attempt(a, 9, False, t))
    assert sch.box == 0 and not sch.mastered  # one failure resets mastery


def test_interval_scale_zero_makes_everything_due(tmp_path):
    clock = Clock()
    cur = _cur(tmp_path, now=clock, interval_scale=0.0, capability_map=None, offline_episodes=None)
    cur.add(cur.family_items()[:1])
    (it,) = cur.items().values()
    cur.record(_attempt(it, 1, True, clock.t))
    assert [i.id for i, _ in cur.queue()] == [it.id]


def test_queue_orders_failures_first(tmp_path):
    clock = Clock()
    cur = _cur(tmp_path, now=clock, capability_map=None, offline_episodes=None)
    fam = cur.family_items()[0]
    fail = fam.model_copy(update={"id": item_id("experience.mousepad", "x"), "family": "experience.mousepad", "origin": "failure"})
    cur.add([fam, fail])
    assert [i.origin for i, _ in cur.queue()] == ["failure", "family"]


def test_learning_curve_first_to_last(tmp_path):
    clock = Clock()
    cur = _cur(tmp_path, now=clock, capability_map=None, offline_episodes=None)
    cur.add(cur.family_items()[:1])
    (it,) = cur.items().values()
    cur.record(_attempt(it, 1, False, 1.0, model_calls=6, seconds=40.0, steps=9))
    cur.record(_attempt(it, 2, True, 2.0, model_calls=4, seconds=30.0, steps=7))
    cur.record(_attempt(it, 3, True, 3.0, model_calls=0, seconds=8.0, steps=5, cached=True))
    c = cur.learning_curve()[it.family]
    assert [r["model_calls"] for r in c["attempts"]] == [6, 4, 0]
    assert c["first"]["ok"] is False and c["last"]["ok"] is True and c["improved"]
    assert c["success_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert cur.attempts_path.read_text().count("\n") == 3  # one appended line per attempt
