import json

from .evolve_fakes import Fakes, make, prop, sig
from o_engine.evolve.learn import (
    Journal, Telemetry, compare, evaluate_shipped, prior_multiplier, priors, window_stats,
)
from o_engine.evolve.pipeline import run_cycle

T0 = 1_000_000.0
H = 3600.0


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _tasks(learning, start, n, ok_every=1, step=600.0):
    """n task observations from ``start``; every ``ok_every``-th one failed when ok_every > 1."""
    _write(learning / "observations.jsonl", [
        {"id": f"o{start}-{i}", "ts": start + i * step, "source": "task", "text": "t", "task_id": f"j{start}-{i}",
         "outcome": "failed" if ok_every > 1 and i % ok_every == 0 else "ok"} for i in range(n)])


def test_telemetry_reads_every_existing_record(tmp_path):
    learning, data, app_log = tmp_path / "learning", tmp_path / "data", tmp_path / "ud" / "o-actions.jsonl"
    _write(learning / "observations.jsonl", [
        {"id": "a", "ts": T0, "source": "task", "text": "t", "task_id": "j1", "outcome": "ok"},
        {"id": "b", "ts": T0 + 1, "source": "task", "text": "t", "task_id": "j2", "outcome": "infeasible"},
        {"id": "c", "ts": T0 + 2, "source": "screen", "text": "t"},
    ])
    _write(data / "engine-actions.jsonl", [
        {"ts": T0 + 3, "kind": "result", "detail": {"job_id": "j1", "ok": False}},  # same job as the observation: deduped
        {"ts": T0 + 4, "kind": "result", "detail": {"job_id": "j3", "ok": False}},
        {"ts": T0 + 5, "kind": "note", "detail": {"ok": True}},
    ])
    _write(data / "interfaces" / "gimp" / "v2" / "fitness.jsonl", [{"ts": "1970-01-12T13:46:46+00:00", "version": 2, "replayed": 4, "passed": 3}])
    _write(app_log, [
        {"ts": T0 + 6, "source": "app", "kind": "task", "summary": "Done: booked", "detail": {"costUsd": 0.2}},
        {"ts": T0 + 7, "source": "app", "kind": "task", "summary": "Error: boom", "detail": {"costUsd": 0.4}},
        {"ts": T0 + 8, "source": "engine", "kind": "task", "summary": "Done: x", "detail": {}},
    ])
    s = Telemetry(learning, data, app_log).samples(T0 - 10, T0 + 100)
    st = window_stats(s)
    assert st["tasks"] == 4  # j1 ok, j3 failed, app done, app error
    assert st["success_rate"] == 0.5 and st["cost_per_task"] == 0.3 and st["fitness"] == 0.75
    assert window_stats(Telemetry(None, None, None).samples(0, 1e12)) == {"tasks": 0, "success_rate": None, "cost_per_task": None, "fitness": None}


def test_compare_thresholds():
    base = {"success_rate": 0.9, "cost_per_task": 0.10, "fitness": 0.8}
    assert compare(base, {"success_rate": 0.85, "cost_per_task": 0.12, "fitness": 0.7}) == []
    why = compare(base, {"success_rate": 0.7, "cost_per_task": 0.2, "fitness": 0.5})
    assert len(why) == 3 and why[0] == "task success fell from 90% to 70%"


def test_regression_rolls_back_immediately(tmp_path):
    learning = tmp_path / "learning"
    j = Journal(tmp_path / "j.jsonl")
    j.append("shipped", "p1", ts=T0, release="evolve-p1", previous="evolve-p0")
    _tasks(learning, T0 - 6 * H, 10)  # 100% before
    _tasks(learning, T0 + 60, 6, ok_every=2)  # 50% in the first hour after
    calls = []
    out = evaluate_shipped(j, Telemetry(learning), lambda pid, ref, why: calls.append((pid, ref, why)), now=T0 + H)
    assert out[0]["verdict"] == "rolled_back" and calls == [("p1", "evolve-p0", "task success fell from 100% to 50%")]
    assert j.last("p1", ("rolled_back",))["ref"] == "evolve-p0"
    assert evaluate_shipped(j, Telemetry(learning), lambda *a: calls.append(a), now=T0 + 2 * H) == []  # closed
    assert len(calls) == 1


def test_cost_rise_rolls_back(tmp_path):
    log = tmp_path / "o-actions.jsonl"
    _write(log, [{"ts": T0 - 3 * H + i, "source": "app", "kind": "task", "summary": "Done: x", "detail": {"costUsd": 0.1}} for i in range(5)])
    _write(log, [{"ts": T0 + 10 + i, "source": "app", "kind": "task", "summary": "Done: x", "detail": {"costUsd": 0.2}} for i in range(5)])
    j = Journal(tmp_path / "j.jsonl")
    j.append("shipped", "p1", ts=T0, previous="sha0")
    v = evaluate_shipped(j, Telemetry(app_log=log), now=T0 + H)[0]
    assert v["verdict"] == "rolled_back" and v["reasons"] == ["cost per task rose from $0.100 to $0.200"]


def test_too_few_tasks_keeps_watching_then_holds(tmp_path):
    learning = tmp_path / "learning"
    j = Journal(tmp_path / "j.jsonl")
    j.append("shipped", "p1", ts=T0, previous="sha0")
    _tasks(learning, T0 - 5 * H, 10)
    _tasks(learning, T0 + 60, 2, ok_every=1)
    _write(learning / "observations.jsonl", [{"id": "f", "ts": T0 + 5000, "source": "task", "text": "t", "outcome": "failed"}])
    assert evaluate_shipped(j, Telemetry(learning), now=T0 + 2 * H)[0]["verdict"] == "watching"  # 3 tasks < 5
    _tasks(learning, T0 + 3 * H, 10)
    v = evaluate_shipped(j, Telemetry(learning), now=T0 + 49 * H)[0]
    assert v["verdict"] == "held" and j.last("p1", ("held",))


def test_fitness_drop_rolls_back_without_task_volume(tmp_path):
    data = tmp_path / "data"
    fit = data / "interfaces" / "calc" / "v1" / "fitness.jsonl"
    _write(fit, [{"ts": T0 - H, "version": 1, "replayed": 10, "passed": 9}, {"ts": T0 + 600, "version": 1, "replayed": 10, "passed": 5}])
    j = Journal(tmp_path / "j.jsonl")
    j.append("shipped", "p1", ts=T0, previous="sha0")
    v = evaluate_shipped(j, Telemetry(data_dir=data), now=T0 + H)[0]
    assert v["verdict"] == "rolled_back" and "interface fitness" in v["reasons"][0]


def test_priors_reward_what_passes_and_holds(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    for pid, sub, src, risk in [("a", "cu", "github", "low"), ("b", "cu", "github", "low"), ("c", "memory", "arxiv", "high")]:
        j.append("proposed", pid, subsystem=sub, risk=risk, sources=[src])
    j.append("gated", "a", passed=True)
    j.append("gated", "b", passed=True)
    j.append("held", "a")
    j.append("gated", "c", passed=False)
    pri = priors(j)
    cu, mem = pri["subsystem"]["cu"], pri["subsystem"]["memory"]
    assert cu["passed"] == 2 and cu["held"] == 1 and cu["multiplier"] > 1.0
    assert mem["failed"] == 1 and mem["multiplier"] < 1.0
    assert prior_multiplier(pri, "cu", "low", ["github"]) > 1.0 > prior_multiplier(pri, "memory", "high", ["arxiv"])
    assert prior_multiplier(pri, "unknown", "medium", []) == 1.0
    assert prior_multiplier({}, "x", "low") == 1.0


def test_evolver_rolls_back_shipped_upgrade_and_priors_drop(tmp_path):
    learning = tmp_path / "learning"
    clock = [T0]
    f = Fakes(signals=[sig(1)], proposals=[prop(1)])
    hooks = []
    ev = make(tmp_path, f, clock=clock, telemetry=Telemetry(learning), rollback=lambda *a: hooks.append(a))
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    ev.shipped("p1", "evolve-p1", "sha-prev")
    before = ev.priors()["subsystem"]["cu.grounding"]["multiplier"]
    _tasks(learning, T0 - 5 * H, 8)
    _tasks(learning, T0 + 60, 8, ok_every=2)
    clock[0] = T0 + 2 * H
    r = ev.tick("active")
    assert r["evaluated"][0]["verdict"] == "rolled_back"
    assert ev.store.get("p1").status == "rolled_back" and hooks and hooks[0][1] == "sha-prev"
    assert ev.rollback_requests()[0]["ref"] == "sha-prev"
    assert ev.priors()["subsystem"]["cu.grounding"]["multiplier"] < before
    assert any(x["text"].startswith("Undid Upgrade 1: task success fell") for x in ev.feed())
