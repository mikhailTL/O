"""The circuit-breaker between planning and execution.

``log`` (default) records a policy entry and passes. ``auto`` passes silently.
``ask`` blocks on the app's approval for at most ``timeout_s``; a denial or a
timeout raises ``QueueDestroyed`` so the caller empties the job queue.
"""
from __future__ import annotations

from typing import Callable, Literal, Optional

from .actionlog import ActionLog
from .schema import ProvisionRequest

GateMode = Literal["log", "ask", "auto"]
GATE_MODES: tuple[str, ...] = ("log", "ask", "auto")
DEFAULT_TIMEOUT_S = 60

ApproveWaiter = Callable[[str, int], Optional[bool]]


class QueueDestroyed(Exception):
    """Approval denied or timed out: the job queue must be emptied."""

    def __init__(self, job_id: str, reason: str) -> None:
        self.job_id = job_id
        self.reason = reason
        super().__init__(f"QueueDestroyed: job {job_id} {reason}")


class Gate:
    def __init__(self, mode: GateMode, waiter: ApproveWaiter | None, timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
        if mode not in GATE_MODES:
            raise ValueError(f"unknown gate mode {mode!r}; expected one of {GATE_MODES}")
        self.mode: GateMode = mode
        self.waiter = waiter
        self.timeout_s = int(timeout_s)

    def check(self, job_id: str, request: ProvisionRequest, log: ActionLog) -> None:
        detail = {
            "job_id": job_id,
            "mode": self.mode,
            "label": request.label,
            "commands": [s.command_string or f"[script:{s.script.language}]" for s in request.steps],
        }
        if self.mode == "auto":
            return
        if self.mode == "log":
            log.record("policy", f"gate log: allowed {request.label!r} ({len(request.steps)} steps)", allowed=True, **detail)
            return
        if self.waiter is None:
            log.record("policy", f"gate ask: no approver for {request.label!r}", allowed=False, reason="no_waiter", **detail)
            raise QueueDestroyed(job_id, "has no approver")
        log.record("policy", f"gate ask: waiting up to {self.timeout_s}s for {request.label!r}", allowed=None, **detail)
        decision = self.waiter(job_id, self.timeout_s)
        if decision is True:
            log.record("policy", f"gate ask: approved {request.label!r}", allowed=True, reason="approved", **detail)
            return
        reason = "timeout" if decision is None else "denied"
        log.record("policy", f"gate ask: {reason} for {request.label!r}; queue destroyed", allowed=False, reason=reason, **detail)
        raise QueueDestroyed(job_id, f"{reason} after {self.timeout_s}s" if reason == "timeout" else "denied by user")
