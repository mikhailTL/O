import json
from types import SimpleNamespace

import pytest

from o_engine.cu.planner import Planner
from o_engine.cu.procedures import HealLoop, Procedure, ProcedureStore, failure_text
from o_engine.cu.schema import OScript, parse_script

SCRIPT = parse_script(
    {
        "app": "calc",
        "goal": "put 42 in A1",
        "steps": [
            {"op": "find", "as": "cell", "target": {"role": "table cell", "name": "A1"}},
            {"op": "click", "ref": "cell"},
            {"op": "type", "ref": "cell", "text": "42"},
        ],
    }
)


class StubClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user):
        self.calls.append(user)
        return self.replies.pop(0)


def _result(ok, failed_index=-1, error=""):
    steps = [{"ok": True}] * (len(SCRIPT.steps) if ok else failed_index) + ([] if ok else [{"ok": False, "error": error}])
    return SimpleNamespace(ok=ok, steps=steps, failed_index=failed_index)


class StubRunner:
    """Fails whenever the step at ``fail_at`` still has the original target name."""

    def __init__(self, log, fail_name="A1", fail_at=0):
        self.log = log
        self.fail_name = fail_name
        self.fail_at = fail_at

    def run(self, script, job_id, start_index=0):
        self.log.append((job_id, start_index, [s.op for s in script.steps]))
        step = script.steps[self.fail_at]
        name = getattr(getattr(step, "target", None), "name", None)
        if name == self.fail_name:
            return _result(False, self.fail_at, f"element not found: {name}")
        return _result(True)


def test_store_key_latest_store_list(tmp_path):
    store = ProcedureStore(tmp_path / "procedures")
    assert store.latest("calc", "put 42 in A1") is None
    key = ProcedureStore.key("calc", "put 42 in A1")
    assert len(key) == 64 and key != ProcedureStore.key("calc", "other")
    p1 = store.store("calc", "put 42 in A1", SCRIPT)
    assert p1.version == 1 and p1.parent is None and p1.healed_step is None
    assert p1.path == f"calc/{key}/v1.json" and (tmp_path / "procedures" / p1.path).is_file()
    assert store.latest("calc", "put 42 in A1") == p1
    assert store.get(p1.id) == p1 and store.get("nope/x/v9") is None
    assert isinstance(store.load_script(p1), OScript) and store.load_script(p1) == SCRIPT
    p2 = store.store("calc", "put 42 in A1", SCRIPT, parent=p1.id, healed_step=1, reason="x")
    assert p2.version == 2 and store.latest("calc", "put 42 in A1") == p2
    other = store.store("writer", "type hello", SCRIPT)
    assert [p.id for p in store.list()] == sorted([p1.id, p2.id, other.id])
    assert not hasattr(store, "delete") and not hasattr(store, "remove")


def test_store_never_overwrites(tmp_path, monkeypatch):
    store = ProcedureStore(tmp_path)
    p1 = store.store("calc", "g", SCRIPT)
    path = tmp_path / p1.path
    before = path.read_bytes()
    monkeypatch.setattr(store, "_versions", lambda goal_dir: [])  # pretend v1 is unseen
    p = store.store("calc", "g", SCRIPT)
    assert p.version == 2 and path.read_bytes() == before


def test_replay_makes_zero_planner_calls(tmp_path):
    store = ProcedureStore(tmp_path)
    proc = store.store("calc", "put 42 in A1", SCRIPT)
    client = StubClient()
    planner = Planner(client)
    log = []
    loop = HealLoop(store, planner, lambda: StubRunner(log, fail_name="never"), max_heals=3)
    out = loop.run(proc, "job-1")
    assert out["result"].ok and out["procedure"] == proc and out["heals"] == 0
    assert planner.calls == 0 and client.calls == []
    assert log == [("job-1", 0, ["find", "click", "type"])]
    assert store.list() == [proc]


def test_heal_writes_v2_with_lineage_and_keeps_v1_bytes(tmp_path):
    store = ProcedureStore(tmp_path)
    v1 = store.store("calc", "put 42 in A1", SCRIPT)
    v1_path = tmp_path / v1.path
    v1_bytes = v1_path.read_bytes()
    client = StubClient('{"op":"find","as":"cell","target":{"role":"table cell","name":"Cell A1"}}')
    planner = Planner(client)
    log = []
    out = HealLoop(store, planner, lambda: StubRunner(log), max_heals=3).run(v1, "job-2")
    assert out["result"].ok and out["heals"] == 1 and planner.calls == 1
    v2 = out["procedure"]
    assert isinstance(v2, Procedure) and v2.version == 2 and v2.parent == v1.id and v2.healed_step == 0
    assert "element not found: A1" in v2.reason
    assert v1_path.read_bytes() == v1_bytes
    assert store.latest("calc", "put 42 in A1") == v2
    assert store.load_script(v2).steps[0].target.name == "Cell A1"
    assert json.loads(v1_path.read_text())["script"]["steps"][0]["target"]["name"] == "A1"
    assert log == [("job-2", 0, ["find", "click", "type"]), ("job-2", 0, ["find", "click", "type"])]
    assert "element not found: A1" in client.calls[0]
    assert out["lineage"] == [v1, v2]


def test_heal_reruns_from_failed_step(tmp_path):
    store = ProcedureStore(tmp_path)
    v1 = store.store("calc", "g", SCRIPT)
    log = []

    class Runner(StubRunner):
        def run(self, script, job_id, start_index=0):
            self.log.append(start_index)
            if script.steps[2].text == "42":
                return _result(False, 2, "type failed")
            return _result(True)

    planner = Planner(StubClient('{"op":"type","ref":"cell","text":"43"}'))
    out = HealLoop(store, planner, lambda: Runner(log), max_heals=2).run(v1, "j")
    assert out["result"].ok and log == [0, 2] and out["procedure"].healed_step == 2


def test_max_heals_bounds_planner_calls_and_versions(tmp_path):
    store = ProcedureStore(tmp_path)
    v1 = store.store("calc", "put 42 in A1", SCRIPT)
    replies = ['{"op":"find","as":"cell","target":{"role":"table cell","name":"A1"}}'] * 10
    planner = Planner(StubClient(*replies))
    log = []
    out = HealLoop(store, planner, lambda: StubRunner(log), max_heals=2).run(v1, "job-3")
    assert not out["result"].ok and out["heals"] == 2 and planner.calls == 2
    assert len(log) == 3
    versions = store.versions("calc", "put 42 in A1")
    assert [v.version for v in versions] == [1, 2, 3]
    assert [v.parent for v in versions] == [None, versions[0].id, versions[1].id]
    assert out["procedure"] == versions[-1]
    zero = HealLoop(store, Planner(StubClient(*replies)), lambda: StubRunner(log), max_heals=0).run(v1, "job-4")
    assert zero["heals"] == 0 and zero["procedure"] == v1


def test_heal_stops_on_planner_garbage_without_writing(tmp_path):
    store = ProcedureStore(tmp_path)
    v1 = store.store("calc", "g", SCRIPT)
    out = HealLoop(store, Planner(StubClient("I refuse")), lambda: StubRunner([]), max_heals=3).run(v1, "j")
    assert not out["result"].ok and out["heals"] == 0 and store.list() == [v1]


def test_failure_text():
    assert failure_text(SimpleNamespace(error="boom")) == "boom"
    assert failure_text(SimpleNamespace(ok=False, failed_index=1, steps=[{}, {"error": "x"}])) == "x"
    assert failure_text(SimpleNamespace(ok=False, failed_index=4, steps=[])) == "step 4 failed"


def test_ids_and_newest_per_app(tmp_path):
    store = ProcedureStore(tmp_path)
    assert store.ids("calc") == [] and store.newest("calc") == []
    a1 = store.store("calc", "goal a", SCRIPT)
    a2 = store.store("calc", "goal a", SCRIPT, parent=a1.id, healed_step=0, reason="r")
    b1 = store.store("calc", "goal b", SCRIPT)
    store.store("writer", "other", SCRIPT)
    assert store.ids("calc") == sorted([a1.id, a2.id, b1.id])
    newest = store.newest("calc")
    # newest per goal: a2 is an unverified heal, yet it is what newest() returns -- by design, not a pass claim
    assert sorted(p.id for p in newest) == sorted([a2.id, b1.id])
    assert all(isinstance(p, Procedure) for p in newest)
    assert store.ids("missing-app") == []
    assert not hasattr(store, "passing")
    assert "unverified" in (ProcedureStore.newest.__doc__ or "")
