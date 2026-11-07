"""O's capability map (plan L step 2): subsystems, their implementation, metrics, limits and watch topics.

``capability_map.json`` beside this file is the data; ``load_capmap`` validates its shape (every
subsystem has description/current/files/metrics/limits/watch_topics; ``watch_repos`` are ``owner/name``),
and the helpers give triage what it needs: the topic vocabulary per subsystem and a compact prompt view.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

CAPMAP_PATH = Path(__file__).with_name("capability_map.json")
_REQUIRED = ("description", "current", "files", "metrics", "limits", "watch_topics")
_GENERIC = frozenset({"agent", "agents", "model", "code", "server", "open", "tool", "tools", "data", "test",
                      "user", "long", "term", "device", "learning", "skill", "library", "automation", "claude", "local"})
# Routing tags the watchers add; they say where a signal goes, not what it is about.
ROUTING_TAGS = frozenset({"skill", "new", "watch", "dependency", "paper-with-code", "archived"})
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class CapmapError(ValueError):
    pass


def validate_capmap(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("subsystems"), dict) or not data["subsystems"]:
        raise CapmapError("capability map needs a non-empty 'subsystems' object")
    for key, sub in data["subsystems"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise CapmapError(f"bad subsystem key {key!r}")
        if not isinstance(sub, dict):
            raise CapmapError(f"subsystem {key} is not an object")
        for f in _REQUIRED:
            if f not in sub:
                raise CapmapError(f"subsystem {key} lacks {f!r}")
        for f in ("files", "metrics", "limits", "watch_topics"):
            if not isinstance(sub[f], list) or not all(isinstance(x, str) for x in sub[f]):
                raise CapmapError(f"subsystem {key}.{f} must be a list of strings")
    for r in data.get("watch_repos", []):
        if not isinstance(r, str) or not _REPO.match(r):
            raise CapmapError(f"bad watch repo {r!r}")
    for f in ("search_topics",):
        if not isinstance(data.get(f, []), list):
            raise CapmapError(f"{f} must be a list")
    return data


@lru_cache(maxsize=4)
def _load(path: str) -> str:
    return Path(path).read_text()


def load_capmap(path: str | Path | None = None) -> dict[str, Any]:
    p = str(path or CAPMAP_PATH)
    return validate_capmap(json.loads(_load(p)))


def subsystems(capmap: dict[str, Any]) -> list[str]:
    return list(capmap["subsystems"])


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def topic_index(capmap: dict[str, Any]) -> dict[str, set[str]]:
    """subsystem -> normalised vocabulary (watch topics, also split into words of 4+ letters)."""
    out: dict[str, set[str]] = {}
    for key, sub in capmap["subsystems"].items():
        vocab: set[str] = set()
        for t in sub["watch_topics"]:
            n = _norm(t)
            vocab.add(n)
            vocab.update(w for w in n.split("-") if len(w) >= 4)
        out[key] = vocab - _GENERIC
    return out


def match_subsystems(text: str, topics: list[str], capmap: dict[str, Any]) -> dict[str, int]:
    """Relevance hits per subsystem for a signal's topics plus title/summary text."""
    norm_topics = {_norm(t) for t in topics if t not in ROUTING_TAGS}
    words = set(_norm(text).split("-"))
    joined = "-" + _norm(text) + "-"
    hits: dict[str, int] = {}
    for key, vocab in topic_index(capmap).items():
        n = 0
        for v in vocab:
            if v in norm_topics:
                n += 2
            elif ("-" in v and f"-{v}-" in joined) or v in words:
                n += 1
        if n:
            hits[key] = n
    return hits


def prompt_view(capmap: dict[str, Any], keys: list[str] | None = None) -> str:
    """Compact text of the subsystems for the triage prompt."""
    lines = []
    for key, sub in capmap["subsystems"].items():
        if keys and key not in keys:
            continue
        lines.append(f"[{key}] {sub['description']}\n  current: {sub['current']}\n  files: {', '.join(sub['files'])}"
                     f"\n  metrics: {'; '.join(sub['metrics'])}\n  limits: {'; '.join(sub['limits'])}")
    return "\n".join(lines)
