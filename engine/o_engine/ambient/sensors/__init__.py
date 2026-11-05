"""Sensor workers. Each runs on its own daemon thread, reports ``available() -> (bool, reason)``
instead of faking data, and publishes derived features only."""
from __future__ import annotations

import threading
import time
from typing import Any


class SensorWorker:
    """Base: ``start()`` spawns a thread that calls ``_run_loop()``; ``stop()`` sets ``_stop``."""

    name = "sensor"
    topic = ""
    description = ""

    def __init__(self, bus: Any = None, log: Any = None, enabled: bool = False) -> None:
        self.bus = bus
        self.log = log
        self.enabled = bool(enabled)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.last_emit: float | None = None

    def available(self) -> tuple[bool, str]:
        return False, "not implemented"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return True
        ok, reason = self.available()
        if not ok:
            self.last_error = reason
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._guarded, name=f"o-sensor-{self.name}", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    def _guarded(self) -> None:
        try:
            self._run_loop()
        except Exception as exc:  # noqa: BLE001 - the supervisor restarts a dead worker
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _run_loop(self) -> None:
        raise NotImplementedError

    def emit(self, payload: dict) -> None:
        payload = dict(payload)
        payload.setdefault("ts", time.time())
        self.last_emit = payload["ts"]
        if self.bus is not None:
            self.bus.publish(self.topic, payload)

    def status(self) -> dict[str, Any]:
        ok, reason = self.available()
        return {"name": self.name, "topic": self.topic, "enabled": self.enabled, "available": ok,
                "reason": reason, "running": self.running, "last_error": self.last_error,
                "description": self.description}
