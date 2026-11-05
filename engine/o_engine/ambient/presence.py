"""USER_PRESENT / USER_ABSENT state machine (UNKNOWN at start).

Rules (plan J.2):
- PRESENT immediately on micro-Doppler motion (CSI Doppler energy or sonar motion above threshold),
  a RuView room-presence flag, or a face approaching the screen (a face seen while UNKNOWN also counts).
- ABSENT when, for longer than ``absent_after_s``, CSI variance stayed below threshold AND no face was
  seen AND sonar reported no motion (every enabled sensor quiet). Debounced: ABSENT needs the
  quiet window plus ``min_dwell_s`` since the last transition.
- No sensor enabled: OS idle time decides (macOS ``ioreg -c IOHIDSystem`` HIDIdleTime, Linux
  ``xprintidle`` or libXss, Windows ``GetLastInputInfo``), so the feature works on any machine.

Every transition is published on ``presence/state`` at high priority and recorded as an action-log
``note``. Nothing here blocks: sensor events arrive on the bus thread; ``tick()`` runs on its own thread.
"""
from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from . import ABSENT, PRESENT, STATES, UNKNOWN
from .bus import PRIORITY_HIGH

CSI_VARIANCE_THRESHOLD = 0.002
DOPPLER_THRESHOLD = 0.05
SONAR_MOTION_THRESHOLD = 0.05
OS_ACTIVE_IDLE_S = 5.0
MIN_DWELL_S = 3.0
TICK_S = 1.0
_HID_IDLE = re.compile(rb'"HIDIdleTime"\s*=\s*(\d+)')


# ------------------------------------------------------------------------------- OS idle time


def parse_ioreg_idle(output: bytes | str) -> float | None:
    """Seconds since last input from ``ioreg -c IOHIDSystem`` output (HIDIdleTime is in ns)."""
    if isinstance(output, str):
        output = output.encode()
    m = _HID_IDLE.search(output)
    return int(m.group(1)) / 1e9 if m else None


def parse_xprintidle(output: bytes | str) -> float | None:
    try:
        return int(str(output.decode() if isinstance(output, bytes) else output).strip()) / 1000.0
    except ValueError:
        return None


def _run(argv: list[str]) -> bytes | None:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=2.0, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _x11_idle() -> float | None:
    try:
        import ctypes.util

        xlib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
        xss = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xss") or "libXss.so.1")
    except OSError:
        return None

    class XScreenSaverInfo(ctypes.Structure):
        _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                    ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong)]

    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(XScreenSaverInfo)
    display = xlib.XOpenDisplay(None)
    if not display:
        return None
    try:
        info = xss.XScreenSaverAllocInfo()
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        root = xlib.XDefaultRootWindow(ctypes.c_void_p(display))
        if not xss.XScreenSaverQueryInfo(ctypes.c_void_p(display), ctypes.c_ulong(root), info):
            return None
        return info.contents.idle / 1000.0
    finally:
        xlib.XCloseDisplay(ctypes.c_void_p(display))


def _windows_idle() -> float | None:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not user32.GetLastInputInfo(ctypes.byref(lii)):
            return None
        return ((kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


def os_idle_seconds(platform: str | None = None) -> float | None:
    """Seconds since the last keyboard/mouse input on this machine, or None when it cannot be read."""
    platform = platform or sys.platform
    if platform == "darwin":
        out = _run(["ioreg", "-c", "IOHIDSystem"])
        return parse_ioreg_idle(out) if out else None
    if platform.startswith("win"):
        return _windows_idle()
    if shutil.which("xprintidle"):
        out = _run(["xprintidle"])
        if out:
            return parse_xprintidle(out)
    return _x11_idle()


# ------------------------------------------------------------------------------- the machine


class PresenceMachine:
    def __init__(self, bus: Any = None, log: Any = None, absent_after_s: float = 300.0,
                 sensors_enabled: Callable[[], set[str]] | None = None,
                 idle_source: Callable[[], float | None] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 min_dwell_s: float = MIN_DWELL_S) -> None:
        self.bus = bus
        self.log = log
        self.absent_after_s = float(absent_after_s)
        self.sensors_enabled = sensors_enabled or (lambda: set())
        self.idle_source = idle_source or os_idle_seconds
        self.clock = clock
        self.wall = wall
        self.min_dwell_s = float(min_dwell_s)
        self._lock = threading.RLock()
        self.state = UNKNOWN
        self.reason = "starting"
        self.source = "none"
        self.since = wall()
        self._since_mono = clock()
        self.absent_since: float | None = None
        self.last_absent_start: float | None = None
        self._last_activity = clock()
        self._last_idle: float | None = None
        self._subs: list[int] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.listeners: list[Callable[[dict], None]] = []

    # ---- wiring -------------------------------------------------------------------------------
    def attach(self) -> None:
        if self.bus is None or self._subs:
            return
        self._subs = [
            self.bus.subscribe("sensor/csi", lambda _t, p: self.on_csi(p), PRIORITY_HIGH),
            self.bus.subscribe("sensor/sonar", lambda _t, p: self.on_sonar(p), PRIORITY_HIGH),
            self.bus.subscribe("sensor/face", lambda _t, p: self.on_face(p), PRIORITY_HIGH),
        ]

    def start(self) -> None:
        self.attach()
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_activity = self.clock()
        self._thread = threading.Thread(target=self._loop, name="o-presence", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        self._thread = None
        if self.bus is not None:
            for sid in self._subs:
                self.bus.unsubscribe(sid)
        self._subs = []

    def _loop(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self._note(f"presence tick failed: {type(exc).__name__}: {exc}")

    # ---- sensor inputs ------------------------------------------------------------------------
    def _activity(self, reason: str, immediate: bool) -> None:
        with self._lock:
            self._last_activity = self.clock()
            if immediate or self.state == UNKNOWN:
                self._transition(PRESENT, reason, "sensors")

    def on_csi(self, p: dict) -> None:
        doppler = float(p.get("doppler_energy") or 0.0)
        variance = float(p.get("variance") or 0.0)
        if p.get("presence") is True and p.get("source") == "ruview":
            self._activity("csi: RuView room presence", immediate=doppler >= DOPPLER_THRESHOLD)
        elif doppler >= DOPPLER_THRESHOLD:
            self._activity("csi: micro-Doppler motion", immediate=True)
        elif variance >= CSI_VARIANCE_THRESHOLD:
            self._activity("csi: variance above threshold", immediate=False)

    def on_sonar(self, p: dict) -> None:
        if float(p.get("motion") or 0.0) >= SONAR_MOTION_THRESHOLD:
            self._activity("sonar: micro-Doppler motion", immediate=True)

    def on_face(self, p: dict) -> None:
        if p.get("approaching"):
            self._activity("camera: face approaching", immediate=True)
        elif p.get("face"):
            self._activity("camera: face in view", immediate=False)

    # ---- evaluation ---------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        with self._lock:
            if self.source == "simulated" and self.reason.startswith("simulated"):
                return  # a simulated state holds until a real event or the next simulate call
            enabled = self.sensors_enabled()
            if not enabled:
                idle = self.idle_source()
                if idle is None:
                    if self.state == UNKNOWN:
                        self.reason = "no sensor enabled and OS idle time unavailable"
                    return
                prev = self._last_idle
                self._last_idle = idle
                if idle >= self.absent_after_s:
                    self._transition(ABSENT, f"os idle {idle:.0f}s >= {self.absent_after_s:.0f}s", "os_idle")
                elif idle < OS_ACTIVE_IDLE_S or (prev is not None and idle < prev):
                    self._transition(PRESENT, f"os input {idle:.1f}s ago", "os_idle")
                return
            quiet = now - self._last_activity
            if quiet > self.absent_after_s and self.state != ABSENT:
                if now - self._since_mono >= self.min_dwell_s:
                    self._transition(ABSENT, f"sensors quiet for {quiet:.0f}s ({', '.join(sorted(enabled))})", "sensors")

    def _transition(self, state: str, reason: str, source: str) -> bool:
        if state not in STATES:
            raise ValueError(f"unknown presence state {state!r}")
        with self._lock:
            if state == self.state:
                self.reason = reason
                if source != "simulated":
                    self.source = source
                return False
            prev = self.state
            wall = self.wall()
            self.state = state
            self.reason = reason
            self.source = source
            self.since = wall
            self._since_mono = self.clock()
            if state == ABSENT:
                self.absent_since = wall
                self.last_absent_start = wall
            else:
                self.absent_since = None
            msg = {"state": state, "prev": prev, "reason": reason, "source": source, "ts": wall,
                   "absent_start": self.last_absent_start}
        self._note(f"presence: {prev} -> {state} ({reason})", state=state, prev=prev, source=source)
        if self.bus is not None:
            self.bus.publish("presence/state", msg, priority=PRIORITY_HIGH)
        for cb in list(self.listeners):
            try:
                cb(dict(msg))
            except Exception:
                pass
        return True

    def simulate(self, state: str, reason: str = "") -> bool:
        if state not in (PRESENT, ABSENT):
            raise ValueError("state must be USER_PRESENT or USER_ABSENT")
        with self._lock:
            if state == PRESENT:
                self._last_activity = self.clock()
            return self._transition(state, f"simulated{': ' + reason if reason else ''}", "simulated") or True

    def _note(self, summary: str, **detail: Any) -> None:
        if self.log is not None:
            try:
                self.log.record("note", summary, **detail)
            except Exception:
                pass

    @property
    def absent(self) -> bool:
        return self.state == ABSENT

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "since": self.since,
                "reason": self.reason,
                "source": self.source,
                "absent_since": self.absent_since,
                "last_absent_start": self.last_absent_start,
                "absent_after_s": self.absent_after_s,
                "headless": self.state == ABSENT,
                "running": self._thread is not None and self._thread.is_alive(),
            }
