import json

import pytest
from pydantic import ValidationError

from o_engine.cu.memory import (
    MAX_ITEMS,
    WorkingMemory,
    budget_tree_summary,
    extract,
    render,
)

GOAL = "export report.ods to CSV but do not overwrite report.ods"


class StubModel:
    """Returns a fixed delta JSON for every call; counts calls."""

    def __init__(self, delta: dict | None = None, raise_exc: Exception | None = None):
        self.delta = delta
        self.raise_exc = raise_exc
        self.calls = 0
        self.last_user = ""

    def complete_json(self, system: str, user: str) -> str:
        self.calls += 1
        self.last_user = user
        if self.raise_exc is not None:
            raise self.raise_exc
        return json.dumps(self.delta)


def test_model_is_frozen_and_forbids_extras():
    m = WorkingMemory()
    with pytest.raises(ValidationError):
        m.facts = ("x",)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        WorkingMemory(extra="no")  # type: ignore[call-arg]


def test_validator_rejects_more_than_cap():
    with pytest.raises(ValidationError):
        WorkingMemory(facts=tuple(f"f{i}" for i in range(MAX_ITEMS + 1)))
    WorkingMemory(facts=tuple(f"f{i}" for i in range(MAX_ITEMS)))


def test_add_drops_oldest_beyond_cap_and_dedupes():
    m = WorkingMemory()
    for i in range(MAX_ITEMS + 5):
        m = m.add("todos", f"t{i}")
    assert len(m.todos) == MAX_ITEMS
    assert m.todos[0] == "t5"
    assert m.todos[-1] == f"t{MAX_ITEMS + 4}"
    m2 = m.add("todos", f"t{MAX_ITEMS + 4}")
    assert m2.todos == m.todos
    assert m.add("todos", "   ").todos == m.todos


def test_add_never_evicts_goal_seeded_entries():
    m = WorkingMemory.from_goal(GOAL)
    seed = m.risks[0]
    assert seed.startswith("Constraint: ")
    for i in range(MAX_ITEMS * 2):
        m = m.add("risks", f"r{i}")
    assert len(m.risks) == MAX_ITEMS
    assert m.risks[0] == seed
    assert m.risks[1] == f"r{MAX_ITEMS + 1}"


def test_from_goal_seeds_constraint_as_risk():
    m = WorkingMemory.from_goal(GOAL)
    assert any("report.ods" in r for r in m.risks)
    assert any("do not overwrite" in r for r in m.risks)
    assert any("report.ods" in f for f in m.facts)
    assert WorkingMemory.from_goal("open the calculator").risks == ()


def test_extract_merges_deltas_and_holds_caps_after_20_calls():
    seeded = WorkingMemory.from_goal(GOAL)
    seeded_risk = seeded.risks[0]
    m = seeded
    for i in range(20):
        model = StubModel(
            {
                "facts": [f"fact {i}-a", f"fact {i}-b", f"fact {i}-c"],
                "state": [f"state {i}"],
                "todos": [f"todo {i}-a", f"todo {i}-b", f"todo {i}-c"],
                "risks": [f"risk {i}-a", f"risk {i}-b"],
            }
        )
        m = extract(m, {"index": i, "op": "click", "ok": True}, "frame 'Calc'", model)
        assert model.calls == 1
    for field in ("facts", "state", "todos", "risks"):
        assert len(getattr(m, field)) <= MAX_ITEMS
    assert len(m.facts) == MAX_ITEMS
    assert seeded_risk in m.risks
    assert "state 19" in m.state


def test_extract_prompt_includes_step_and_tree():
    model = StubModel({"facts": [], "state": [], "todos": [], "risks": []})
    m = extract(WorkingMemory(), {"op": "type", "text": "42"}, "table cell 'A1'", model)
    assert m == WorkingMemory()
    assert '"text": "42"' in model.last_user
    assert "table cell 'A1'" in model.last_user


@pytest.mark.parametrize("bad", [RuntimeError("boom"), ValueError("x")])
def test_extract_model_exception_leaves_memory_unchanged(bad):
    m = WorkingMemory.from_goal(GOAL).add("facts", "f1")
    out = extract(m, {"op": "click"}, "", StubModel(raise_exc=bad))
    assert out == m


def test_extract_garbage_json_leaves_memory_unchanged():
    class Garbage:
        def complete_json(self, system, user):
            return "[1, 2"

    class WrongShape:
        def complete_json(self, system, user):
            return json.dumps({"facts": "not a list", "nope": []})

    m = WorkingMemory.from_goal(GOAL)
    assert extract(m, {}, "", Garbage()) == m
    assert extract(m, {}, "", WrongShape()) == m


def test_render_is_byte_identical_and_fixed_format():
    a = WorkingMemory.from_goal(GOAL).add("todos", "save as csv")
    b = WorkingMemory.from_goal(GOAL).add("todos", "save as csv")
    assert render(a).encode() == render(b).encode()
    text = render(a)
    assert text.startswith("## Working memory\n")
    assert text.index("### Facts") < text.index("### State") < text.index("### Todos") < text.index("### Risks")
    assert "- save as csv" in text
    assert "(none)" in render(WorkingMemory())
    assert render(a) == a.render()


def test_to_log_detail_is_jsonable_with_counts():
    m = WorkingMemory.from_goal(GOAL).add("state", "Calc open")
    d = m.to_log_detail()
    json.dumps(d)
    assert d["state"] == ["Calc open"]
    assert d["counts"]["risks"] == len(m.risks)
    assert set(d) == {"facts", "state", "todos", "risks", "counts"}


def test_budget_tree_summary_keeps_whole_lines_within_budget():
    lines = [f"line {i:02d} " + "x" * 30 for i in range(50)]
    summary = "\n".join(lines)
    out = budget_tree_summary(summary, 100)
    kept = [ln for ln in out.split("\n") if not ln.startswith("...")]
    assert all(ln in lines for ln in kept)
    assert kept == lines[: len(kept)]
    assert len(kept) < 50
    assert len("\n".join(kept)) // 4 <= 100
    assert out.endswith(f"... (truncated at {len(kept)} lines)")


def test_budget_tree_summary_passthrough_and_edge_cases():
    summary = "a\nb\nc"
    assert budget_tree_summary(summary, 1000) == summary
    assert budget_tree_summary("", 10) == ""
    assert budget_tree_summary(summary, 0) == ""
    assert budget_tree_summary("x" * 400, 10) == "... (truncated at 0 lines)"
