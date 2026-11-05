"""Ambient workspace context.

Collects what is already lying around (shell history, a shallow directory tree, config file
hashes, recent screen telemetry) into a local SQLite db and exposes it as one
``WorkspaceProfile``. Nothing here asks the user anything.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform as _platform
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

HISTORY_TAIL = 200
TREE_DEPTH = 3
IGNORED_DIRS = frozenset({"node_modules", ".git", "venv", ".venv"})
CONFIG_FILES = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "docker-compose.yml",
    ".env.example",
    "Makefile",
)
CONFIG_HEAD_BYTES = 2048
MAX_TREE_ENTRIES = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shell_history (ts REAL, shell TEXT, cwd TEXT, cmd TEXT);
CREATE TABLE IF NOT EXISTS tree_snapshots (ts REAL, root TEXT, json TEXT);
CREATE TABLE IF NOT EXISTS config_files (ts REAL, path TEXT, sha256 TEXT, head TEXT);
"""


class WorkspaceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str
    collected_at: float
    shell_history: list[str] = Field(default_factory=list)
    tree: dict[str, Any] = Field(default_factory=dict)
    config_files: list[dict[str, Any]] = Field(default_factory=list)
    telemetry: list[dict[str, Any]] = Field(default_factory=list)
    platform: str


def _home() -> Path:
    for var in ("HOME", "USERPROFILE"):
        v = os.environ.get(var)
        if v:
            return Path(v)
    return Path.home()


def _tail_lines(path: Path, limit: int) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    lines = [ln.rstrip("\r") for ln in lines if ln.strip()]
    return lines[-limit:]


def _zsh_strip(line: str) -> str:
    if line.startswith(": ") and ";" in line:
        return line.split(";", 1)[1]
    return line


def _history_sources() -> list[tuple[str, Path]]:
    home = _home()
    sources = [
        ("bash", home / ".bash_history"),
        ("zsh", home / ".zsh_history"),
    ]
    ps_candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        ps_candidates.append(Path(appdata) / "Microsoft" / "Windows" / "PowerShell" / "PSReadLine")
    ps_candidates.append(home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "PowerShell" / "PSReadLine")
    ps_candidates.append(home / ".local" / "share" / "powershell" / "PSReadLine")
    for d in ps_candidates:
        p = d / "ConsoleHost_history.txt"
        if p.is_file():
            sources.append(("powershell", p))
            break
    return sources


def read_shell_history(limit: int = HISTORY_TAIL) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for shell, path in _history_sources():
        if not path.is_file():
            continue
        lines = _tail_lines(path, limit)
        if shell == "zsh":
            lines = [_zsh_strip(ln) for ln in lines]
        out.extend((shell, ln) for ln in lines if ln)
    return out


def walk_tree(root: str, depth: int = TREE_DEPTH) -> dict[str, Any]:
    budget = [MAX_TREE_ENTRIES]

    def _node(p: Path, remaining: int) -> dict[str, Any]:
        node: dict[str, Any] = {"name": p.name or str(p), "type": "dir"}
        if remaining <= 0:
            node["children"] = []
            node["truncated"] = True
            return node
        children: list[dict[str, Any]] = []
        try:
            entries = sorted(os.scandir(p), key=lambda e: e.name)
        except OSError:
            node["children"] = children
            node["error"] = "unreadable"
            return node
        for e in entries:
            if budget[0] <= 0:
                node["truncated"] = True
                break
            budget[0] -= 1
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if e.name in IGNORED_DIRS:
                    continue
                children.append(_node(Path(e.path), remaining - 1))
            else:
                child: dict[str, Any] = {"name": e.name, "type": "file"}
                try:
                    child["size"] = e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
                children.append(child)
        node["children"] = children
        return node

    return _node(Path(root), depth)


def hash_config_files(root: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    base = Path(root)
    for name in CONFIG_FILES:
        p = base / name
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        out.append(
            {
                "path": str(p),
                "sha256": hashlib.sha256(data).hexdigest(),
                "head": data[:CONFIG_HEAD_BYTES].decode("utf-8", errors="replace"),
            }
        )
    return out


def _parse_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    s = s.replace(" ", "T", 1) if "T" not in s and " " in s else s
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


class ScreenpipeTelemetry:
    def __init__(self, db_path: str | os.PathLike | None) -> None:
        self.db_path = Path(db_path) if db_path else None

    @property
    def available(self) -> bool:
        return bool(self.db_path and self.db_path.is_file())

    def recent(self, seconds: int = 120) -> list[dict[str, Any]]:
        if not self.available:
            return []
        cutoff = time.time() - seconds
        uri = f"file:{self.db_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        except sqlite3.Error:
            return []
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(frames)")}
            if not {"id", "timestamp"} <= cols:
                return []
            app_col = "app_name" if "app_name" in cols else None
            win_col = "window_name" if "window_name" in cols else None
            ocr_cols = {r[1] for r in conn.execute("PRAGMA table_info(ocr_text)")}
            has_ocr = {"frame_id", "text"} <= ocr_cols
            sel = [
                "f.timestamp",
                f"f.{app_col}" if app_col else "''",
                f"f.{win_col}" if win_col else "''",
                "o.text" if has_ocr else "''",
            ]
            sql = f"SELECT {', '.join(sel)} FROM frames f"
            if has_ocr:
                sql += " LEFT JOIN ocr_text o ON o.frame_id = f.id"
            sql += " ORDER BY f.id DESC LIMIT 2000"
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for ts_raw, app, win, text in rows:
            ts = _parse_ts(ts_raw)
            if ts is None or ts < cutoff:
                continue
            out.append({"ts": ts, "app": app or "", "window": win or "", "text": text or ""})
        out.sort(key=lambda r: r["ts"])
        return out


class LocalTelemetry:
    def __init__(self, data_dir: str | os.PathLike) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "telemetry.jsonl"
        self._lock = threading.Lock()

    def record(self, text: str, app: str = "", window: str = "") -> dict[str, Any]:
        entry = {"ts": time.time(), "app": app, "window": window, "text": text}
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        return entry

    def recent(self, seconds: int = 120) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        cutoff = time.time() - seconds
        out: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()[-5000:]
        except OSError:
            return []
        for ln in lines:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            ts = _parse_ts(e.get("ts")) if isinstance(e, dict) else None
            if ts is None or ts < cutoff:
                continue
            out.append(
                {
                    "ts": ts,
                    "app": str(e.get("app", "") or ""),
                    "window": str(e.get("window", "") or ""),
                    "text": str(e.get("text", "") or ""),
                }
            )
        out.sort(key=lambda r: r["ts"])
        return out


class ContextStore:
    def __init__(self, data_dir: str | os.PathLike, screenpipe_db: str | os.PathLike | None = None) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "context.db"
        self.screenpipe = ScreenpipeTelemetry(screenpipe_db)
        self.telemetry = LocalTelemetry(self.dir)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        return conn

    def collect(self, cwd: str) -> dict[str, Any]:
        cwd = str(Path(cwd))
        now = time.time()
        history = read_shell_history(HISTORY_TAIL)
        tree = walk_tree(cwd, TREE_DEPTH)
        configs = hash_config_files(cwd)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM shell_history")
            conn.executemany(
                "INSERT INTO shell_history (ts, shell, cwd, cmd) VALUES (?,?,?,?)",
                [(now, shell, cwd, cmd) for shell, cmd in history],
            )
            conn.execute(
                "INSERT INTO tree_snapshots (ts, root, json) VALUES (?,?,?)",
                (now, cwd, json.dumps(tree, separators=(",", ":"))),
            )
            conn.execute("DELETE FROM config_files WHERE path LIKE ?", (cwd + "%",))
            conn.executemany(
                "INSERT INTO config_files (ts, path, sha256, head) VALUES (?,?,?,?)",
                [(now, c["path"], c["sha256"], c["head"]) for c in configs],
            )
        return {"collected_at": now, "shell_history": history, "tree": tree, "config_files": configs}

    def recent_telemetry(self, seconds: int = 120) -> list[dict[str, Any]]:
        if self.screenpipe.available:
            rows = self.screenpipe.recent(seconds)
            if rows:
                return rows
        return self.telemetry.recent(seconds)

    def profile(self, cwd: str, telemetry_seconds: int = 120) -> WorkspaceProfile:
        cwd = str(Path(cwd))
        snap = self.collect(cwd)
        return WorkspaceProfile(
            cwd=cwd,
            collected_at=snap["collected_at"],
            shell_history=[cmd for _, cmd in snap["shell_history"]],
            tree=snap["tree"],
            config_files=snap["config_files"],
            telemetry=self.recent_telemetry(telemetry_seconds),
            platform=_platform.system().lower() or "unknown",
        )
