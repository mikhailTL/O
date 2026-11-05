"""Process and dependency healing for the engine's long-running children.

Restart semantics after systemd (``Restart=on-failure|always``, ``RestartSec`` with exponential
backoff, ``StartLimitBurst``/``StartLimitIntervalSec``) and s6 (a supervise loop per service that
restarts whatever exits, readiness = still running after ``stable_s``). Children are subprocesses
(vLLM when local, screenpipe: given as argv) or in-process workers with ``start()/stop()/running``
(the sensors). Every start, exit, restart, give-up and repair is written to the action log.

Dependency healing: after every child exit the engine's Python packages are re-checked with
``importlib.util.find_spec``. A missing one is repaired only when ``O_AUTO_REPAIR=1``:
- a ``flake.nix`` (``NixOS/nix``) in the project root -> ``nix develop --command <python> -m pip install``,
  a ``devbox.json`` (``jetify-com/devbox``) -> ``devbox run -- <python> -m pip install``, restoring the
  pinned toolchain first;
- otherwise ``<python> -m pip install <package>``.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

ENGINE_PACKAGES: dict[str, str] = {
    "pydantic": "pydantic", "langgraph": "langgraph", "fastapi": "fastapi", "uvicorn": "uvicorn",
    "httpx": "httpx", "openai": "openai", "playwright": "playwright", "PIL": "pillow", "numpy": "numpy",
}
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0
STABLE_S = 30.0
BURST = 5
BURST_INTERVAL_S = 120.0
POLL_S = 1.0
REPAIR_TIMEOUT_S = 600


@dataclass
class Child:
    name: str
    argv: Sequence[str] | None = None
    worker: Any = None
    restart: str = "on-failure"  # "on-failure" | "always" | "no"
    env: dict[str, str] | None = None
    # runtime
    proc: Any = None
    state: str = "stopped"  # stopped | running | backoff | failed
    restarts: int = 0
    started_at: float = 0.0
    next_start: float = 0.0
    backoff: float = BACKOFF_BASE_S
    starts: list[float] = field(default_factory=list)
    last_exit: int | None = None

    def alive(self) -> bool:
        if self.worker is not None:
            return bool(getattr(self.worker, "running", False))
        return self.proc is not None and self.proc.poll() is None


def missing_packages(packages: dict[str, str] | None = None,
                     find_spec: Callable[[str], Any] = importlib.util.find_spec) -> list[str]:
    out = []
    for module in (packages or ENGINE_PACKAGES):
        try:
            if find_spec(module) is None:
                out.append(module)
        except Exception:
            out.append(module)
    return out


def repair_command(pip_name: str, project_root: Path, python: str = sys.executable,
                   which: Callable[[str], str | None] = shutil.which) -> list[str]:
    pip = [python, "-m", "pip", "install", pip_name]
    if (project_root / "flake.nix").is_file() and which("nix"):
        return [which("nix") or "nix", "develop", str(project_root), "--command", *pip]
    if (project_root / "devbox.json").is_file() and which("devbox"):
        return [which("devbox") or "devbox", "run", "--config", str(project_root), "--", *pip]
    return pip


class Supervisor:
    def __init__(self, log: Any = None, auto_repair: bool = False, project_root: str | Path | None = None,
                 packages: dict[str, str] | None = None, clock: Callable[[], float] = time.monotonic,
                 popen: Callable[..., Any] = subprocess.Popen, runner: Callable[..., Any] = subprocess.run,
                 find_spec: Callable[[str], Any] = importlib.util.find_spec,
                 which: Callable[[str], str | None] = shutil.which) -> None:
        self.log = log
        self.auto_repair = bool(auto_repair)
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
        self.packages = dict(packages or ENGINE_PACKAGES)
        self.clock = clock
        self.popen = popen
        self.runner = runner
        self.find_spec = find_spec
        self.which = which
        self.children: dict[str, Child] = {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.repairs: list[dict[str, Any]] = []

    def _note(self, kind: str, summary: str, **detail: Any) -> None:
        if self.log is not None:
            try:
                self.log.record(kind, summary, **detail)
            except Exception:
                pass

    # ---- registration / lifecycle ---------------------------------------------------------------
    def add(self, child: Child) -> Child:
        with self._lock:
            self.children[child.name] = child
        return child

    def start(self) -> None:
        with self._lock:
            for child in self.children.values():
                if child.state == "stopped":
                    self._start_child(child)
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="o-supervisor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        self._thread = None
        with self._lock:
            for child in self.children.values():
                self._stop_child(child)

    def _loop(self) -> None:
        while not self._stop.wait(POLL_S):
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001
                self._note("note", f"supervisor poll failed: {type(exc).__name__}: {exc}")

    def _start_child(self, child: Child) -> bool:
        now = self.clock()
        try:
            if child.worker is not None:
                ok = child.worker.start()
                if ok is False:
                    raise RuntimeError(getattr(child.worker, "last_error", "") or "worker refused to start")
            else:
                child.proc = self.popen(list(child.argv or []), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, env=child.env)
        except Exception as exc:  # noqa: BLE001
            child.state = "backoff"
            child.next_start = now + child.backoff
            self._note("remediate", f"supervisor: {child.name} failed to start ({type(exc).__name__}: {exc}); retry in {child.backoff:.0f}s",
                       child=child.name, error=str(exc), backoff_s=child.backoff)
            child.backoff = min(child.backoff * 2, BACKOFF_MAX_S)
            child.starts.append(now)
            return False
        child.state = "running"
        child.started_at = now
        child.starts.append(now)
        self._note("note", f"supervisor: started {child.name}", child=child.name,
                   pid=getattr(child.proc, "pid", None), restarts=child.restarts)
        return True

    def _stop_child(self, child: Child) -> None:
        try:
            if child.worker is not None:
                child.worker.stop()
            elif child.proc is not None and child.proc.poll() is None:
                child.proc.terminate()
                try:
                    child.proc.wait(timeout=5)
                except Exception:
                    child.proc.kill()
        except Exception:
            pass
        if child.state != "failed":
            child.state = "stopped"

    # ---- the supervise step ----------------------------------------------------------------------
    def poll(self) -> None:
        now = self.clock()
        with self._lock:
            for child in self.children.values():
                if child.state == "running":
                    if child.alive():
                        if now - child.started_at >= STABLE_S:
                            child.backoff = BACKOFF_BASE_S  # s6 readiness: a stable run resets the backoff
                        continue
                    code = child.proc.poll() if child.proc is not None else None
                    child.last_exit = code
                    if child.restart == "no" or (child.restart == "on-failure" and code == 0):
                        child.state = "stopped"
                        self._note("note", f"supervisor: {child.name} exited ({code}); not restarting", child=child.name, exit_code=code)
                        continue
                    child.starts = [t for t in child.starts if now - t <= BURST_INTERVAL_S]
                    if len(child.starts) >= BURST:
                        child.state = "failed"
                        self._note("remediate", f"supervisor: {child.name} hit the start limit ({BURST} in {BURST_INTERVAL_S:.0f}s); giving up",
                                   child=child.name, exit_code=code)
                        continue
                    self._note("remediate", f"supervisor: {child.name} exited ({code}); restart in {child.backoff:.0f}s",
                               child=child.name, exit_code=code, backoff_s=child.backoff)
                    child.state = "backoff"
                    child.next_start = now + child.backoff
                    child.backoff = min(child.backoff * 2, BACKOFF_MAX_S)
                    self.check_dependencies()
                elif child.state == "backoff" and now >= child.next_start:
                    child.starts = [t for t in child.starts if now - t <= BURST_INTERVAL_S]
                    if len(child.starts) >= BURST:
                        child.state = "failed"
                        self._note("remediate", f"supervisor: {child.name} hit the start limit ({BURST} in {BURST_INTERVAL_S:.0f}s); giving up",
                                   child=child.name)
                        continue
                    child.restarts += 1
                    self._start_child(child)

    def check_dependencies(self) -> list[str]:
        missing = missing_packages(self.packages, self.find_spec)
        if not missing:
            return []
        if not self.auto_repair:
            self._note("remediate", f"supervisor: missing engine packages {missing}; auto-repair is off (O_AUTO_REPAIR=1 enables it)",
                       missing=missing, auto_repair=False)
            return missing
        for module in missing:
            argv = repair_command(self.packages[module], self.project_root, which=self.which)
            via = "nix" if "develop" in argv else ("devbox" if "devbox" in Path(argv[0]).name else "pip")
            try:
                proc = self.runner(argv, capture_output=True, text=True, timeout=REPAIR_TIMEOUT_S, stdin=subprocess.DEVNULL)
                ok = proc.returncode == 0
                err = (proc.stderr or "")[-500:]
            except Exception as exc:  # noqa: BLE001
                ok, err = False, f"{type(exc).__name__}: {exc}"
            importlib.invalidate_caches()
            row = {"module": module, "package": self.packages[module], "via": via, "ok": ok, "argv": list(argv)}
            self.repairs.append(row)
            self._note("remediate", f"supervisor: repair {self.packages[module]} via {via} {'ok' if ok else 'failed'}",
                       **row, error=None if ok else err)
        return missing_packages(self.packages, self.find_spec)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": c.name, "state": c.state, "restarts": c.restarts, "last_exit": c.last_exit,
                     "kind": "worker" if c.worker is not None else "process"} for c in self.children.values()]
