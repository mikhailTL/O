"""SQLite store for the self-evolution pipeline (plan L).

``<data dir>/evolve/evolve.sqlite`` where the data dir is ``O_DATA_DIR`` or ``~/.o-engine``.

Tables:

* ``signals``: one row per observed development, primary key ``Signal.id`` (dedupe: a second insert of
  the same id is ignored and not reported as new);
* ``proposals``: one row per ``Proposal`` (the model JSON is stored verbatim; never rewritten);
* ``decisions``: the append-only ledger ``(seq, proposal_id, status, reason, ts, metrics)``. SQLite
  triggers refuse UPDATE and DELETE, so the ledger can only grow. A proposal's current status is
  its latest decision (or the status it was stored with when it has none);
* ``cursors``: per-source incremental-scan state (last scan time, last seen ids/timestamps);
* ``http_cache``: ETag / Last-Modified validators and the cached body per URL, so a repeated scan
  sends conditional requests and a ``304`` costs nothing.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import Proposal, ProposalStatus, Signal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  published TEXT NOT NULL DEFAULT '',
  repo TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  stars INTEGER NOT NULL DEFAULT 0,
  stars_7d INTEGER NOT NULL DEFAULT 0,
  license TEXT NOT NULL DEFAULT '',
  topics TEXT NOT NULL DEFAULT '[]',
  body TEXT NOT NULL,
  first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signals_kind ON signals(kind);
CREATE INDEX IF NOT EXISTS signals_first_seen ON signals(first_seen);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  subsystem TEXT NOT NULL,
  score REAL NOT NULL,
  status TEXT NOT NULL,
  body TEXT NOT NULL,
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  ts TEXT NOT NULL,
  metrics TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS decisions_pid ON decisions(proposal_id);
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
  BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
  BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TABLE IF NOT EXISTS cursors (
  source TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS http_cache (
  url TEXT PRIMARY KEY,
  etag TEXT NOT NULL DEFAULT '',
  last_modified TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  ts TEXT NOT NULL
);
"""

_STATUSES: frozenset[str] = frozenset(ProposalStatus.__args__)  # type: ignore[attr-defined]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def data_dir() -> Path:
    return Path(os.environ.get("O_DATA_DIR") or (Path.home() / ".o-engine"))


def default_path() -> Path:
    return data_dir() / "evolve" / "evolve.sqlite"


class EvolveStore:
    """Thread-safe (one connection, one lock) store. ``path=":memory:"`` for tests."""

    def __init__(self, path: str | Path | None = None) -> None:
        p = default_path() if path is None else path
        if str(p) != ":memory:":
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ signals
    def add_signals(self, signals: Iterable[Signal]) -> list[Signal]:
        """Inserts each signal unless its id is already stored; returns the ones that were new."""
        new: list[Signal] = []
        ts = now_iso()
        with self._lock:
            for s in signals:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO signals (id, kind, source, title, url, published, repo, version, stars,"
                    " stars_7d, license, topics, body, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (s.id, s.kind, s.source, s.title, s.url, s.published, s.repo, s.version, s.stars, s.stars_7d,
                     s.license, json.dumps(s.topics), s.model_dump_json(), ts),
                )
                if cur.rowcount:
                    new.append(s)
            self._db.commit()
        return new

    def has_signal(self, signal_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM signals WHERE id=?", (signal_id,)).fetchone() is not None

    def get_signal(self, signal_id: str) -> Signal | None:
        with self._lock:
            row = self._db.execute("SELECT body FROM signals WHERE id=?", (signal_id,)).fetchone()
        return Signal.model_validate_json(row["body"]) if row else None

    def signals(self, *, kind: str | None = None, source: str | None = None, since: str | None = None,
                topic: str | None = None, limit: int | None = None) -> list[Signal]:
        """Stored signals, newest first-seen first. ``since`` compares against ``first_seen`` (ISO)."""
        q, args = "SELECT body FROM signals WHERE 1=1", []
        if kind:
            q += " AND kind=?"; args.append(kind)
        if source:
            q += " AND source=?"; args.append(source)
        if since:
            q += " AND first_seen>=?"; args.append(since)
        if topic:
            q += " AND EXISTS (SELECT 1 FROM json_each(signals.topics) WHERE value=?)"; args.append(topic)
        q += " ORDER BY first_seen DESC, published DESC"
        if limit:
            q += " LIMIT ?"; args.append(int(limit))
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [Signal.model_validate_json(r["body"]) for r in rows]

    def count_signals(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT source, COUNT(*) n FROM signals GROUP BY source").fetchall()
        return {r["source"]: r["n"] for r in rows}

    # ---------------------------------------------------------------- proposals
    def add_proposal(self, p: Proposal) -> bool:
        """Stores a proposal once (same id again is ignored). Returns True when it was new."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO proposals (id, subsystem, score, status, body, created) VALUES (?,?,?,?,?,?)",
                (p.id, p.subsystem, p.score, p.status, p.model_dump_json(), now_iso()),
            )
            self._db.commit()
            return bool(cur.rowcount)

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        """The stored proposal with ``status`` set to its latest decision."""
        with self._lock:
            row = self._db.execute("SELECT body FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            return None
        p = Proposal.model_validate_json(row["body"])
        st = self.status(proposal_id)
        return p if st == p.status else p.model_copy(update={"status": st})

    def status(self, proposal_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM decisions WHERE proposal_id=? ORDER BY seq DESC LIMIT 1", (proposal_id,)
            ).fetchone()
            if row:
                return row["status"]
            row = self._db.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return row["status"] if row else None

    def proposals(self, *, status: str | None = None, subsystem: str | None = None, min_score: float = 0.0,
                  limit: int | None = None) -> list[Proposal]:
        """Proposals by score (desc), filtered on their *current* status."""
        q, args = "SELECT id FROM proposals WHERE score>=?", [min_score]
        if subsystem:
            q += " AND subsystem=?"; args.append(subsystem)
        q += " ORDER BY score DESC, created DESC"
        with self._lock:
            ids = [r["id"] for r in self._db.execute(q, args).fetchall()]
        out: list[Proposal] = []
        for pid in ids:
            p = self.get_proposal(pid)
            if p is None or (status and p.status != status):
                continue
            out.append(p)
            if limit and len(out) >= limit:
                break
        return out

    # ---------------------------------------------------------------- decisions
    def record_decision(self, proposal_id: str, status: str, reason: str = "",
                        metrics: dict[str, Any] | None = None) -> int:
        """Appends one ledger row; returns its sequence number."""
        if status not in _STATUSES:
            raise ValueError(f"unknown proposal status: {status!r}")
        with self._lock:
            if self._db.execute("SELECT 1 FROM proposals WHERE id=?", (proposal_id,)).fetchone() is None:
                raise KeyError(f"no proposal {proposal_id!r}")
            cur = self._db.execute(
                "INSERT INTO decisions (proposal_id, status, reason, ts, metrics) VALUES (?,?,?,?,?)",
                (proposal_id, status, reason, now_iso(), json.dumps(metrics or {}, sort_keys=True)),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def decisions(self, proposal_id: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM decisions", []
        if proposal_id:
            q += " WHERE proposal_id=?"; args.append(proposal_id)
        q += " ORDER BY seq"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [{**dict(r), "metrics": json.loads(r["metrics"])} for r in rows]

    # ------------------------------------------------------------------ cursors
    def get_cursor(self, source: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT value FROM cursors WHERE source=?", (source,)).fetchone()
        return json.loads(row["value"]) if row else {}

    def set_cursor(self, source: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO cursors (source, value, ts) VALUES (?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (source, json.dumps(value, sort_keys=True), now_iso()),
            )
            self._db.commit()

    def cursors(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT source, value FROM cursors").fetchall()
        return {r["source"]: json.loads(r["value"]) for r in rows}

    # --------------------------------------------------------------- http cache
    def http_get(self, url: str) -> dict[str, str] | None:
        with self._lock:
            row = self._db.execute("SELECT etag, last_modified, body FROM http_cache WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    def http_put(self, url: str, etag: str, last_modified: str, body: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO http_cache (url, etag, last_modified, body, ts) VALUES (?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified,"
                " body=excluded.body, ts=excluded.ts",
                (url, etag or "", last_modified or "", body, now_iso()),
            )
            self._db.commit()
