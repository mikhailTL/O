"""Task working memory for computer-use jobs: facts, state, todos, risks.

Frozen, bounded (``MAX_ITEMS`` per list). ``from_goal`` seeds constraint-like
sentences so a stated "do not ..." survives every replan; ``extract`` asks the
model once per judged step for deltas and merges them (any model failure leaves
the memory untouched); ``render`` is a fixed-format block meant to sit *after*
the static prompt prefix so a vLLM prefix cache keeps hitting across turns.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

MAX_ITEMS = 40
FIELDS = ("facts", "state", "todos", "risks")
TITLES = {"facts": "Facts", "state": "State", "todos": "Todos", "risks": "Risks"}
PINNED_PREFIXES = ("Goal: ", "Constraint: ")  # goal-seeded entries; never evicted by ``add``
_CONSTRAINT_WORDS = ("do not", "don't", "never", "must", "only", "without")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\s+(?=\bbut\b)|\s*\n+\s*")

EXTRACT_SYSTEM_PROMPT = (
    "You maintain a short working memory for a desktop automation task.\n"
    "Given the current memory, the step just executed and the current accessibility tree, respond\n"
    "with ONLY one JSON object: {\"facts\":[],\"state\":[],\"todos\":[],\"risks\":[]}. Each list holds\n"
    "NEW short sentences only (omit anything already in memory): facts = durable things learned,\n"
    "state = where the UI is now, todos = remaining work, risks = constraints or hazards. Never explain."
)


def _clean(items: Any) -> tuple[str, ...]:
    out: list[str] = []
    for it in items or ():
        s = str(it).strip() if isinstance(it, (str, int, float)) else ""
        if s and s not in out:
            out.append(s)
    return tuple(out)


class WorkingMemory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    todos: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    @field_validator(*FIELDS)
    @classmethod
    def _cap(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) > MAX_ITEMS:
            raise ValueError(f"at most {MAX_ITEMS} items")
        return v

    @classmethod
    def from_goal(cls, goal: str) -> "WorkingMemory":
        """Seed ``facts`` with the goal and ``risks`` with its constraint-like sentences."""
        risks = [f"Constraint: {s.strip()}" for s in _SENTENCE_SPLIT.split(goal or "") if _is_constraint(s)]
        return cls(facts=_clean([f"Goal: {goal.strip()}"] if goal and goal.strip() else []), risks=_clean(risks))

    def add(self, field: str, *items: str) -> "WorkingMemory":
        """A new memory with ``items`` appended to ``field``; deduped; beyond the cap the oldest
        non-pinned entry is dropped (goal-seeded ``Goal:``/``Constraint:`` entries survive)."""
        if field not in FIELDS:
            raise ValueError(f"unknown field {field!r}")
        merged = list(getattr(self, field))
        for s in _clean(items):
            if s in merged:
                continue
            merged.append(s)
            if len(merged) > MAX_ITEMS:
                victims = [i for i, x in enumerate(merged) if not x.startswith(PINNED_PREFIXES)] or [0]
                del merged[victims[0]]
        return self.model_copy(update={field: tuple(merged)})

    def merge(self, delta: dict[str, Any]) -> "WorkingMemory":
        mem = self
        for field in FIELDS:
            mem = mem.add(field, *(delta.get(field) or ()))
        return mem

    def render(self) -> str:
        return render(self)

    def to_log_detail(self) -> dict[str, Any]:
        """Detail for an action-log ``note`` entry (plain JSON types only)."""
        d: dict[str, Any] = {f: list(getattr(self, f)) for f in FIELDS}
        d["counts"] = {f: len(getattr(self, f)) for f in FIELDS}
        return d


def _is_constraint(sentence: str) -> bool:
    low = sentence.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in _CONSTRAINT_WORDS)


def render(memory: WorkingMemory) -> str:
    """Deterministic block: ``## Working memory`` + four subsections in fixed order."""
    parts = ["## Working memory"]
    for field in FIELDS:
        items = getattr(memory, field)
        parts.append(f"### {TITLES[field]}")
        parts.extend([f"- {s}" for s in items] or ["(none)"])
    return "\n".join(parts) + "\n"


def _parse_delta(raw: str) -> dict[str, list[str]] | None:
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or not any(k in obj for k in FIELDS):
        return None
    delta: dict[str, list[str]] = {}
    for field in FIELDS:
        v = obj.get(field, [])
        if not isinstance(v, list):
            return None
        delta[field] = [x for x in v if isinstance(x, str)]
    return delta


def extract(memory: WorkingMemory, step_record: dict, tree_summary: str, model: Any) -> WorkingMemory:
    """One ``model.complete_json`` call for deltas, merged into ``memory``; any failure -> ``memory``."""
    user = "\n\n".join(
        [
            render(memory).rstrip(),
            "Step just executed (JSON):\n" + json.dumps(step_record or {}, sort_keys=True, default=str),
            "Current accessibility tree summary:\n" + (tree_summary or "(none)"),
            "Delta JSON object:",
        ]
    )
    try:
        delta = _parse_delta(model.complete_json(EXTRACT_SYSTEM_PROMPT, user))
        return memory.merge(delta) if delta is not None else memory
    except Exception:
        return memory


def budget_tree_summary(summary: str, max_tokens: int) -> str:
    """Truncate ``summary`` to about ``max_tokens`` (len // 4) keeping whole lines, newest-first order kept."""
    if not summary:
        return ""
    budget_chars = max(0, max_tokens) * 4
    if budget_chars == 0:
        return ""
    if len(summary) <= budget_chars:
        return summary
    kept: list[str] = []
    used = 0
    for line in summary.split("\n"):
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget_chars:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept + [f"... (truncated at {len(kept)} lines)"])
