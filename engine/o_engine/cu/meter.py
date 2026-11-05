"""Per-job accounting of engine-side model calls (computer use).

``run_cu_job`` opens one ``JobMeter`` for the job (``metered``); every model client charges it:
paid Claude Code CLI calls (``charge_paid``) count against ``max_paid`` (setting ``cuMaxModelCalls``,
env ``O_CU_MAX_MODEL_CALLS``) and carry the CLI's reported cost, local vLLM calls are only counted.
A paid call past the cap raises ``ModelCallCapReached`` *before* the CLI is started; the clients'
primary -> fallback loops re-raise it instead of treating it as one more backend failure, so the job
stops and reports instead of looping. The meter also holds the job's judge cache
(screenshot sha256, question) -> verdict and the count of checks deferred to the final goal check.

Outside a job (no meter open) nothing is counted and nothing is capped.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import threading
from typing import Any, Iterator

DEFAULT_MAX_PAID_CALLS = 12

_current: contextvars.ContextVar["JobMeter | None"] = contextvars.ContextVar("o_cu_job_meter", default=None)


class ModelCallCapReached(RuntimeError):
    """The job used its whole engine-side model-call budget; nothing more is called."""

    def __init__(self, meter: "JobMeter", kind: str) -> None:
        self.meter = meter
        self.kind = kind
        super().__init__(
            f"engine-side model-call cap reached: {meter.paid_calls} of cuMaxModelCalls={meter.max_paid} "
            f"Claude calls used (${meter.cost_usd:.3f}); refused a further {kind} call"
        )


class JobMeter:
    def __init__(self, job_id: str = "", max_paid: int | None = DEFAULT_MAX_PAID_CALLS) -> None:
        self.job_id = job_id
        self.max_paid = None if max_paid is None else max(0, int(max_paid))
        self.paid: dict[str, int] = {}
        self.local: dict[str, int] = {}
        self.cost_usd = 0.0
        self.cache_hits = 0
        self.deferred = 0
        self.refused = 0
        self._judge_cache: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ counting

    @property
    def paid_calls(self) -> int:
        return sum(self.paid.values())

    @property
    def local_calls(self) -> int:
        return sum(self.local.values())

    @property
    def remaining(self) -> int | None:
        return None if self.max_paid is None else max(0, self.max_paid - self.paid_calls)

    @property
    def exhausted(self) -> bool:
        return self.remaining == 0

    def charge_paid(self, kind: str) -> None:
        with self._lock:
            if self.max_paid is not None and self.paid_calls >= self.max_paid:
                self.refused += 1
                raise ModelCallCapReached(self, kind)
            self.paid[kind] = self.paid.get(kind, 0) + 1

    def add_cost(self, payload: Any) -> None:
        """Adds ``total_cost_usd`` (or ``cost_usd``) from a ``claude -p --output-format json`` payload."""
        if not isinstance(payload, dict):
            return
        for key in ("total_cost_usd", "cost_usd"):
            val = payload.get(key)
            if isinstance(val, (int, float)) and val >= 0:
                with self._lock:
                    self.cost_usd += float(val)
                return

    def count_local(self, kind: str) -> None:
        with self._lock:
            self.local[kind] = self.local.get(kind, 0) + 1

    # ------------------------------------------------------------ judge cache

    @staticmethod
    def judge_key(png: bytes | None, question: str) -> tuple[str, str]:
        return hashlib.sha256(png or b"").hexdigest(), str(question)

    def cached_verdict(self, png: bytes | None, question: str) -> Any:
        hit = self._judge_cache.get(self.judge_key(png, question))
        if hit is not None:
            self.cache_hits += 1
        return hit

    def cache_verdict(self, png: bytes | None, question: str, verdict: Any) -> None:
        self._judge_cache[self.judge_key(png, question)] = verdict

    # ------------------------------------------------------------ report

    def snapshot(self) -> dict:
        return {
            "paid": self.paid_calls,
            "local": self.local_calls,
            "max_paid": self.max_paid,
            "remaining": self.remaining,
            "by_kind": {**{f"claude:{k}": v for k, v in self.paid.items()}, **{f"local:{k}": v for k, v in self.local.items()}},
            "cache_hits": self.cache_hits,
            "deferred_checks": self.deferred,
            "refused": self.refused,
        }


def current() -> JobMeter | None:
    return _current.get()


@contextlib.contextmanager
def metered(meter: JobMeter) -> Iterator[JobMeter]:
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


def charge_paid(kind: str) -> None:
    m = _current.get()
    if m is not None:
        m.charge_paid(kind)


def add_cost(payload: Any) -> None:
    m = _current.get()
    if m is not None:
        m.add_cost(payload)


def count_local(kind: str) -> None:
    m = _current.get()
    if m is not None:
        m.count_local(kind)
