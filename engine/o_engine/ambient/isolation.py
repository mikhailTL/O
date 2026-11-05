"""Headless execution while the user is away.

``select_isolated`` picks the first available of microsandbox (every OS) -> Firecracker (Linux+KVM)
-> Docker (the existing ``DockerSandbox``) -> None (the current local path sandbox) and logs the
choice and every fall-back. ``IsolatedJobSandbox`` presents that backend with the ``executor.Sandbox``
interface the provisioning graph and the native runner already use (``root``, ``resolve``,
``run(step, index, env_extra, isolated_display)``), so the graph is reused unchanged: absolute paths
inside the job root are re-rooted at ``/work`` and cached scripts under ``import_roots`` are copied
into the job dir first. When PRESENT nothing here is used. Before every step, ``run`` passes the job's
``PauseGate`` checkpoint (ambient/pause.py): a low-priority job waits there while the user is back.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from ..executor import Sandbox, StepResult

WORK_MOUNT = "/work"
IMPORT_DIR = ".o-imports"
ORDER = ("microsandbox", "firecracker", "docker")
_ABS_PATH = re.compile(r"(?<![\w.~-])(/[A-Za-z0-9._@%+=:,/-]+)")


def _record(log: Any, kind: str, summary: str, **detail: Any) -> None:
    if log is not None:
        try:
            log.record(kind, summary, **detail)
        except Exception:
            pass


def default_candidates(job_dir: str, settings_like: dict | None = None) -> list[Any]:
    from ..containment import DockerSandbox
    from .firecracker import FirecrackerSandbox
    from .microsandbox import MicrosandboxSandbox

    s = settings_like or {}
    return [
        MicrosandboxSandbox(job_dir=job_dir),
        FirecrackerSandbox(job_dir=job_dir),
        DockerSandbox(image=s.get("dockerImage") or "python:3.12-slim", job_dir=job_dir,
                      network=s.get("dockerNetwork") or "bridge"),
    ]


def select_isolated(job_dir: str, log: Any = None, settings_like: dict | None = None,
                    candidates: Callable[[str, dict | None], list[Any]] | None = None, job_id: str = "") -> Any:
    """The first available backend, or None for the local sandbox. Logged either way."""
    tried: list[str] = []
    for backend in (candidates or default_candidates)(job_dir, settings_like):
        try:
            ok = bool(backend.available)
        except Exception as exc:  # noqa: BLE001
            ok, tried_reason = False, f"{type(exc).__name__}: {exc}"
            tried.append(f"{getattr(backend, 'name', '?')}: {tried_reason}")
            continue
        if ok:
            _record(log, "note", f"{job_id + ': ' if job_id else ''}headless job isolated in {backend.name}"
                    + (f" (skipped {'; '.join(tried)})" if tried else ""), job_id=job_id, backend=backend.name, tried=tried)
            return backend
        tried.append(f"{backend.name}: {backend.reason}")
    _record(log, "note", f"{job_id + ': ' if job_id else ''}no isolated backend available; headless job uses the local "
            f"sandbox ({'; '.join(tried)})", job_id=job_id, backend="local", tried=tried)
    return None


class IsolatedJobSandbox:
    """``executor.Sandbox``-shaped adapter over a ``DisposableSandbox`` rooted at the job dir."""

    def __init__(self, root: str | Path, backend: Any, import_roots: Iterable[str | Path] = (), job_id: str = "",
                 gate: Any = None) -> None:
        self._local = Sandbox(str(root))
        self.job_id = job_id
        self.gate = gate
        self.root = self._local.root
        self.backend = backend
        self.import_roots = [Path(p).resolve() for p in import_roots]
        self.backend_name = getattr(backend, "name", "local")

    def resolve(self, target_directory: str) -> Path:
        return self._local.resolve(target_directory)

    def _relative(self, target_directory: str) -> str:
        resolved = self._local.resolve(target_directory)
        rel = resolved.relative_to(self.root).as_posix()
        return rel or "."

    def localize(self, command: str) -> str:
        """Re-root job-dir paths at /work; copy importable files (cached scripts) into the job dir."""
        def sub(m: re.Match) -> str:
            raw = m.group(1)
            p = Path(raw)
            try:
                resolved = p.resolve()
            except OSError:
                return raw
            if resolved == self.root or self.root in resolved.parents:
                rel = resolved.relative_to(self.root).as_posix()
                return WORK_MOUNT + ("/" + rel if rel and rel != "." else "")
            if resolved.is_file() and any(r in resolved.parents for r in self.import_roots):
                digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:10]
                dest = self.root / IMPORT_DIR / f"{digest}-{resolved.name}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(resolved, dest)
                return f"{WORK_MOUNT}/{IMPORT_DIR}/{dest.name}"
            return raw

        return _ABS_PATH.sub(sub, command)

    def run(self, step: Any, index: int = 0, env_extra: Any = None, isolated_display: bool = False) -> StepResult:
        if self.gate is not None and self.job_id:
            self.gate.checkpoint(self.job_id, step=index)  # safe point: between steps (interrupt on return)
        if self.backend is None:
            return self._local.run(step, index, env_extra=env_extra, isolated_display=isolated_display)
        if getattr(step, "command_string", None) is None:
            raise ValueError("script steps must go through a Runner")
        moved = step.model_copy(update={"target_directory": self._relative(step.target_directory),
                                        "command_string": self.localize(step.command_string)})
        return self.backend.run(moved, index)

    def destroy(self) -> None:
        if self.backend is not None:
            try:
                self.backend.destroy()
            except Exception:
                pass


class IsolatedSandboxes:
    """One ``IsolatedJobSandbox`` per job id (built on first use, destroyed by ``release``)."""

    def __init__(self, sandbox_root: Path, log: Any = None, settings_like: dict | None = None,
                 import_roots: Iterable[str | Path] = (), candidates: Any = None, gate: Any = None) -> None:
        self.sandbox_root = Path(sandbox_root)
        self.gate = gate
        self.log = log
        self.settings_like = settings_like
        self.import_roots = list(import_roots)
        self.candidates = candidates
        self._by_job: dict[str, IsolatedJobSandbox] = {}
        self._lock = threading.Lock()
        self.last_backend: str | None = None

    def for_job(self, job_id: str) -> IsolatedJobSandbox:
        with self._lock:
            sb = self._by_job.get(job_id)
            if sb is None:
                root = self.sandbox_root / job_id
                root.mkdir(parents=True, exist_ok=True)
                backend = select_isolated(str(root), self.log, self.settings_like, self.candidates, job_id=job_id)
                sb = IsolatedJobSandbox(root, backend, self.import_roots, job_id=job_id, gate=self.gate)
                self._by_job[job_id] = sb
                self.last_backend = sb.backend_name
            return sb

    def release(self, job_id: str) -> None:
        with self._lock:
            sb = self._by_job.pop(job_id, None)
        if sb is not None:
            sb.destroy()
            if sb.backend is not None:
                _record(self.log, "note", f"{job_id}: isolated sandbox destroyed ({sb.backend_name})",
                        job_id=job_id, backend=sb.backend_name)
