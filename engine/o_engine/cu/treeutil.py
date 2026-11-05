"""Shared helpers for the computer-use modules: duck-typed field access and the accessibility-tree
flattener. Kept import-free of the rest of ``cu`` so graph, judge, planner and feasibility can all
import it without a cycle."""
from __future__ import annotations

from typing import Any

TREE_SUMMARY_LINES = 200


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """``obj[key]`` for a dict, ``getattr`` otherwise; ``default`` for None or a missing key."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def summarize_tree(nodes: Any, limit: int = TREE_SUMMARY_LINES) -> str:
    """Flatten an accessibility tree to ``role "name" [states]`` lines (depth-indented, capped)."""
    lines: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if len(lines) >= limit:
            return
        role = _get(node, "role") or "?"
        name = _get(node, "name") or _get(node, "text") or ""
        states = list(_get(node, "states") or [])
        tail = f" [{','.join(str(s) for s in states)}]" if states else ""
        lines.append(f"{'  ' * depth}{role} {str(name)[:80]!r}{tail}")
        for child in _get(node, "children") or []:
            walk(child, depth + 1)

    for n in nodes or []:
        walk(n, 0)
    if len(lines) >= limit:
        lines.append(f"... (truncated at {limit} lines)")
    return "\n".join(lines)
