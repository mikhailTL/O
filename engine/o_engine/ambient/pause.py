"""Interrupt on return: low-priority background jobs pause at a safe point when the user comes back.

The app tags each engine job with ``priority`` (``low`` for goal cycles and scheduled tasks, else
``normal``; ``high`` exists too) on ``/provision`` and ``/script/run``. ``PauseGate.register`` keeps it
per job id. The presence machine calls ``on_presence``: ``USER_PRESENT`` sets the pause flag,
``USER_ABSENT`` clears it. Headless jobs run every step through ``IsolatedJobSandbox.run``, which calls
``checkpoint(job_id)`` before the step starts, i.e. between graph steps, never inside one: a low job
blocks there while the flag is set and continues when the user steps away again. Normal and high jobs
never wait. ``release_all`` (engine shutdown) and ``forget`` (job finished) never leave a job blocked.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import ABSENT, PRESENT

PRIORITIES = ("low", "normal", "high")
DEFAULT_PRIORITY = "normal"


def normalise_priority(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in PRIORITIES else DEFAULT_PRIORITY


def pop_priority(body: Any) -> str:
    """Remove ``priority`` from a request body (the strict request schemas forbid extra keys)."""
    if isinstance(body, dict) and "priority" in body:
        return normalise_priority(body.pop("priority"))
    return DEFAULT_PRIORITY


class PauseGate:
    def __init__(self, log: Any = None, clock: Callable[[], float] = time.time) -> None:
        self.log = log
        self.clock = clock
        self._cond = threading.Condition()
        self._priority: dict[str, str] = {}
        self._waiting: dict[str, float] = {}  # job id -> wall time it paused at
        self._paused = False
        self._released = False
        self.pauses: list[dict[str, Any]] = []  # history (job_id, paused_at, resumed_at, step), bounded

    # ---- registry -------------------------------------------------------------------------------
    def register(self, job_id: str, priority: Any = DEFAULT_PRIORITY) -> str:
        p = normalise_priority(priority)
        with self._cond:
            self._priority[str(job_id)] = p
        return p

    def priority(self, job_id: str) -> str:
        with self._cond:
            return self._priority.get(str(job_id), DEFAULT_PRIORITY)

    def forget(self, job_id: str) -> None:
        with self._cond:
            self._priority.pop(str(job_id), None)
            self._cond.notify_all()

    # ---- presence -------------------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    def on_presence(self, msg: dict) -> None:
        state = msg.get("state") if isinstance(msg, dict) else None
        if state == PRESENT:
            self.set_paused(True, reason="user returned")
        elif state == ABSENT:
            self.set_paused(False, reason="user away")

    def set_paused(self, paused: bool, reason: str = "") -> None:
        with self._cond:
            if self._paused == bool(paused):
                return
            self._paused = bool(paused)
            low = sorted(j for j, p in self._priority.items() if p == "low")
            self._cond.notify_all()
        if low:
            self._note(f"low-priority jobs {'pause at their next safe point' if paused else 'resume'} ({reason})",
                       jobs=low, paused=bool(paused))

    def release_all(self) -> None:
        with self._cond:
            self._released = True
            self._paused = False
            self._cond.notify_all()

    # ---- the safe point -------------------------------------------------------------------------
    def should_wait(self, job_id: str) -> bool:
        return (self._paused and not self._released and self._priority.get(str(job_id)) == "low")

    def checkpoint(self, job_id: str, step: int | None = None, timeout: float | None = None) -> float:
        """Block a low-priority job here while the user is present. Returns the seconds waited."""
        job_id = str(job_id)
        with self._cond:
            if not self.should_wait(job_id):
                return 0.0
            started = self.clock()
            self._waiting[job_id] = started
        self._note(f"{job_id}: low-priority job paused at a safe point (before step {step})", job_id=job_id,
                   step=step, paused=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self.should_wait(job_id):
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    break
                self._cond.wait(left if left is not None else 1.0)
            self._waiting.pop(job_id, None)
            resumed = self.clock()
            self.pauses.append({"job_id": job_id, "step": step, "paused_at": started, "resumed_at": resumed})
            del self.pauses[:-100]
        self._note(f"{job_id}: low-priority job resumed after {resumed - started:.1f}s", job_id=job_id, step=step,
                   paused=False, waited_s=round(resumed - started, 3))
        return resumed - started

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            return {"paused": self._paused, "waiting": sorted(self._waiting),
                    "low": sorted(j for j, p in self._priority.items() if p == "low")}

    def _note(self, summary: str, **detail: Any) -> None:
        if self.log is not None:
            try:
                self.log.record("note", summary, **detail)
            except Exception:
                pass
