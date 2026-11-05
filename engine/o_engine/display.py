"""Isolated X display via Xvfb. The host DISPLAY is never read or changed."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_DISPLAY = ":99"
DEFAULT_SIZE = "1280x800x24"
DEFAULT_LOCK_DIR = "/tmp"


class IsolatedDisplay:
    def __init__(
        self,
        display: str = DEFAULT_DISPLAY,
        size: str = DEFAULT_SIZE,
        lock_dir: str | os.PathLike[str] = DEFAULT_LOCK_DIR,
    ):
        if not display.startswith(":") or not display[1:].isdigit():
            raise ValueError(f"display must look like ':99', got {display!r}")
        self.display = display
        self.size = size
        self.lock_path = Path(lock_dir) / f".X{display[1:]}-lock"
        self._proc: subprocess.Popen | None = None
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

    def start(self) -> None:
        if self.is_host or not self.available or self.running:
            return
        if self.lock_path.exists():
            self._external = True
            return
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        self._proc = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", self.size, "-nolisten", "tcp"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

    def stop(self) -> None:
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
