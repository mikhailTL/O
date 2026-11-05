"""``agent_milestones``: a time-stamped record of background completions, error bypasses, failures
and checkpoints (SQLite, WAL). Written by the engine's job hook and by ``POST /milestones``."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

KINDS = ("completed", "error_bypassed", "failed", "checkpoint")
TITLE_MAX = 200
DETAIL_MAX = 2000
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_milestones (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  task_id TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('completed','error_bypassed','failed','checkpoint')),
  title TEXT NOT NULL,
  detail TEXT,
  presence TEXT
);
CREATE INDEX IF NOT EXISTS agent_milestones_ts ON agent_milestones(ts);
"""


class MilestoneIn(BaseModel):
    """Body of ``POST /milestones``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: str = Field(pattern="^(completed|error_bypassed|failed|checkpoint)$")
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    task_id: str | None = Field(default=None, max_length=128)
    detail: str | None = Field(default=None, max_length=DETAIL_MAX)
    ts: float | None = None


def _scrub(text: str) -> str:
    try:
        from ..learning import scrub

        return scrub(text)
    except Exception:
        return text


def kind_for_job(final: dict[str, Any]) -> str:
    """completed / error_bypassed (ok after a remediation or a heal) / failed."""
    if not final.get("ok"):
        return "failed"
    attempt = final.get("attempt") or 0
    try:
        attempt = int(attempt)
    except (TypeError, ValueError):
        attempt = 0
    return "error_bypassed" if attempt > 1 or final.get("patches_applied") else "completed"


class MilestoneStore:
    def __init__(self, path: str | Path, on_record: Callable[[dict], None] | None = None) -> None:
        self.path = Path(path)
        self.on_record = on_record
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        if not self._ready:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._ready = True
        return conn

    def journal_mode(self) -> str:
        with self._lock:
            conn = self._connect()
            try:
                return str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            finally:
                conn.close()

    def record(self, kind: str, title: str, task_id: str | None = None, detail: str | None = None,
               presence: str | None = None, ts: float | None = None) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        title = _scrub(" ".join(str(title or "").split()))[:TITLE_MAX] or "(untitled)"
        detail = _scrub(str(detail))[:DETAIL_MAX] if detail else None
        ts = time.time() if ts is None else float(ts)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO agent_milestones(ts, task_id, kind, title, detail, presence) VALUES (?,?,?,?,?,?)",
                    (ts, task_id, kind, title, detail, presence),
                )
                conn.commit()
                row = {"id": int(cur.lastrowid), "ts": ts, "task_id": task_id, "kind": kind, "title": title,
                       "detail": detail, "presence": presence}
            finally:
                conn.close()
        if self.on_record is not None:
            try:
                self.on_record(dict(row))
            except Exception:
                pass
        return row

    def since(self, ts: float | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, ts, task_id, kind, title, detail, presence FROM agent_milestones "
                    "WHERE ts >= ? ORDER BY ts ASC, id ASC LIMIT ?",
                    (float(ts or 0.0), max(1, min(int(limit), 5000))),
                ).fetchall()
            finally:
                conn.close()
        return [dict(r) for r in rows]
