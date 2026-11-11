"""Engine-side action log.

Every engine event is (1) appended to a local JSONL file with a SHA-256 hash chain and
(2) forwarded to the O desktop app's log endpoint when one is configured, so the user
sees one log. The file is append-only; nothing here ever rewrites or truncates it.
One writer per data dir: use ``get_log(data_dir)`` so every module shares the instance.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

import httpx

KINDS = frozenset({"task", "tool", "result", "policy", "graph", "cache", "remediate", "verify", "settings", "note", "error"})
GENESIS = "0" * 64
FORWARD_TIMEOUT_S = 0.5
FORWARD_QUEUE_MAX = 10_000  # entries waiting for the app; beyond this the oldest waiting entry is dropped

_registry: dict[Path, "ActionLog"] = {}
_registry_lock = threading.Lock()


def get_log(data_dir: str | os.PathLike, forward_url: str | None = None, token: str | None = None) -> "ActionLog":
    """The single ActionLog for a data dir (created on first call; later calls return it)."""
    key = Path(data_dir).resolve()
    with _registry_lock:
        log = _registry.get(key)
        if log is None:
            log = ActionLog(key, forward_url, token)
            _registry[key] = log
        return log


def _last_line(path: Path) -> bytes:
    """The last complete line of a file, however long it is."""
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    if size == 0:
        return b""
    chunk = 4096
    with path.open("rb") as f:
        data = b""
        pos = size
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
            stripped = data.rstrip(b"\n")
            if b"\n" in stripped:
                return stripped.rsplit(b"\n", 1)[1]
            chunk *= 2
        return data.rstrip(b"\n")


class ActionLog:
    def __init__(self, data_dir: str | os.PathLike, forward_url: str | None = None, token: str | None = None) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / "engine-actions.jsonl"
        self.text = self.dir / "engine-actions.log"
        self.forward_url = forward_url
        self.token = token or ""
        self._lock = threading.Lock()
        self._prev = self._last_hash()
        # Forwarding to the app happens on one daemon thread (genui.UIStore has the same pattern): the job thread
        # that records an entry never waits on the app's socket. The local files are the record.
        self._client = httpx.Client(timeout=FORWARD_TIMEOUT_S) if forward_url else None
        self._outbox: "queue.Queue[dict | None]" = queue.Queue(maxsize=FORWARD_QUEUE_MAX)
        self._sender: threading.Thread | None = None
        self.dropped = 0
        if self._client is not None:
            self._sender = threading.Thread(target=self._send_loop, name="o-log-forward", daemon=True)
            self._sender.start()

    def _last_hash(self) -> str:
        try:
            last = _last_line(self.jsonl)
            return json.loads(last).get("hash", GENESIS) if last else GENESIS
        except Exception:
            return GENESIS

    def record(self, kind: str, summary: str, **detail: Any) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown log kind {kind!r}; expected one of {sorted(KINDS)}")
        entry = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "source": "engine",
            "kind": kind,
            "summary": str(summary),
            "detail": _jsonable(detail),
        }
        with self._lock:
            entry["prev"] = self._prev
            body = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
            self._prev = entry["hash"]
            with self.jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            with self.text.open("a", encoding="utf-8") as f:
                f.write(f"{entry['iso']}  [{kind}]  {entry['summary']}\n")
        self._forward(entry)
        return entry

    def _forward(self, entry: dict) -> None:
        """Queue ``entry`` for the sender thread; never blocks. A full queue drops the oldest waiting entry."""
        if not self._client:
            return
        while True:
            try:
                self._outbox.put_nowait(entry)
                return
            except queue.Full:
                try:
                    self._outbox.get_nowait()
                    self._outbox.task_done()
                    self.dropped += 1
                except queue.Empty:
                    pass

    def _send_loop(self) -> None:
        while True:
            entry = self._outbox.get()
            if entry is None:
                self._outbox.task_done()
                return
            try:
                self._client.post(self.forward_url, json=entry, headers={"x-o-token": self.token})
            except Exception:
                pass  # the local file is the record; the app's view is a convenience
            finally:
                self._outbox.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait (bounded) until every queued entry has been sent; tests and shutdown use it."""
        if self._client is None:
            return
        deadline = time.monotonic() + timeout
        while self._outbox.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)


def _jsonable(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        if isinstance(obj, dict):
            return {str(k): _jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [_jsonable(v) for v in obj]
        return str(obj)
