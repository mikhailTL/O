"""Isolated X display via Xvfb. The host DISPLAY is never read or changed.

A bare Xvfb has no window manager: windows stack in map order, nothing is ever "active" and focus
follows whatever was mapped last, so keystrokes can land in one window while a screenshot shows
another (real run: a second editor instance covered the first). ``start()`` therefore also runs a
minimal window manager on the display when one is installed (``WINDOW_MANAGERS``, first found
wins), for a display this process started and for an already-running one that has none.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_DISPLAY = ":99"
DEFAULT_SIZE = "1280x800x24"
DEFAULT_LOCK_DIR = "/tmp"
# (executable, extra argv) in order of preference; each is small and needs no session.
WINDOW_MANAGERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openbox", ()),
    ("matchbox-window-manager", ("-use_titlebar", "no")),
    ("xfwm4", ("--compositor=off",)),
)
READY_TIMEOUT_S = 5.0
X11_SOCKET_DIR = Path("/tmp/.X11-unix")
WM_ATOM = "_NET_SUPPORTING_WM_CHECK"


def _which(name: str) -> str | None:
    """``shutil.which`` that only accepts an executable actually called ``name``."""
    path = shutil.which(name)
    if not path or os.path.basename(path) != name:
        return None
    return path


def find_window_manager() -> tuple[str, tuple[str, ...]] | None:
    """(path, argv tail) of the first installed window manager in ``WINDOW_MANAGERS``, else None."""
    for name, extra in WINDOW_MANAGERS:
        path = _which(name)
        if path:
            return path, extra
    return None


class IsolatedDisplay:
    def __init__(
        self,
        display: str = DEFAULT_DISPLAY,
        size: str = DEFAULT_SIZE,
        lock_dir: str | os.PathLike[str] = DEFAULT_LOCK_DIR,
        window_manager: bool = True,
    ):
        if not display.startswith(":") or not display[1:].isdigit():
            raise ValueError(f"display must look like ':99', got {display!r}")
        self.display = display
        self.size = size
        self.lock_path = Path(lock_dir) / f".X{display[1:]}-lock"
        self.window_manager = bool(window_manager)
        self.wm_name: str | None = None  # the window manager this object started, if any
        self.wm_reason = ""  # why none was started (none installed / already managed / ...)
        self._proc: subprocess.Popen | None = None
        self._wm: subprocess.Popen | None = None
        self._external = False

    @property
    def available(self) -> bool:
        return sys.platform.startswith("linux") and shutil.which("Xvfb") is not None

    @property
    def running(self) -> bool:
        if self.is_host:
            return False
        if self._proc is not None and self._proc.poll() is None:
            return True
        if self._external and self.lock_path.exists():
            return True
        return False

    @property
    def is_host(self) -> bool:
        """True when this display name is the host session's own DISPLAY: never isolated, never pixels."""
        host = os.environ.get("DISPLAY", "")
        return bool(host) and host.split(".")[0] == self.display.split(".")[0]

    def _child_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        env["DISPLAY"] = self.display
        return env

    def start(self) -> None:
        if self.is_host or not self.available or self.running:
            return
        if self.lock_path.exists():
            self._external = True
            self.ensure_window_manager()
            return
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        self._proc = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", self.size, "-nolisten", "tcp"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self.ensure_window_manager()

    def _wait_ready(self, timeout: float = READY_TIMEOUT_S) -> bool:
        socket = X11_SOCKET_DIR / f"X{self.display[1:]}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.lock_path.exists() and (socket.exists() or not X11_SOCKET_DIR.exists()):
                return True
            if self._proc is not None and self._proc.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    def has_window_manager(self) -> bool | None:
        """True/False when ``xprop`` can tell whether a WM manages the display, None when it cannot."""
        xprop = _which("xprop")
        if xprop is None:
            return None
        try:
            proc = subprocess.run([xprop, "-root", WM_ATOM], env=self._child_env(), capture_output=True,
                                  text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return "window id" in (proc.stdout or "")

    def ensure_window_manager(self) -> str | None:
        """Start the first installed window manager on this display unless one already runs. Never raises;
        returns the WM's name when this call started it."""
        if not self.window_manager or self.is_host:
            self.wm_reason = "disabled" if not self.window_manager else "host display"
            return None
        if self._wm is not None and self._wm.poll() is None:
            return self.wm_name
        found = find_window_manager()
        if found is None:
            self.wm_reason = "no window manager installed (" + ", ".join(n for n, _ in WINDOW_MANAGERS) + ")"
            return None
        if not self._wait_ready():
            self.wm_reason = f"display {self.display} not ready"
            return None
        if self._external and self.has_window_manager():
            self.wm_reason = "display already has a window manager"
            return None
        path, extra = found
        try:
            self._wm = subprocess.Popen(
                [path, *extra],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._child_env(),
                start_new_session=True,
            )
        except OSError as e:
            self.wm_reason = f"{os.path.basename(path)} failed to start: {e}"
            return None
        self.wm_name = os.path.basename(path)
        self.wm_reason = ""
        time.sleep(0.3)  # let it take the root window before the first app maps
        if self._wm.poll() is not None:
            # another WM already owns this display (openbox exits when it cannot become the manager)
            self.wm_reason = f"{self.wm_name} exited at once (code {self._wm.returncode}); another WM may own the display"
            self._wm, self.wm_name = None, None
            return None
        return self.wm_name

    def stop(self) -> None:
        wm, self._wm = self._wm, None
        self.wm_name = None
        if wm is not None and wm.poll() is None:
            wm.terminate()
            try:
                wm.wait(timeout=5)
            except subprocess.TimeoutExpired:
                wm.kill()
        proc, self._proc = self._proc, None
        self._external = False
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def env(self) -> dict[str, str]:
        if self.running:
            return {"DISPLAY": self.display}
        return {}
