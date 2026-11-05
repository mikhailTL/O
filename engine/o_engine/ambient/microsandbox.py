"""microsandbox (``superradcompany/microsandbox``, Apache-2.0) microVM sandbox: libkrun microVMs on
Apple Silicon macOS, Linux/KVM and Windows/WHP with sub-100 ms boot. First choice for headless
jobs while the user is away, on every OS.

Uses the async Python SDK (``Sandbox.create(name, image=, cpus=, memory=)``, ``await sb.exec(cmd,
args)`` -> ``.stdout_text``, ``await sb.stop()``) on a private event loop owned by the job thread
(never the server's loop). The job directory is offered as a ``/work`` volume; an SDK that does not
take ``volumes`` runs the step without the mount and ``synced`` is False (the caller logs it).
Needs the ``microsandbox`` package and a reachable microsandbox server (``msb`` on PATH or
``MSB_SERVER_URL``).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ..containment import DisposableSandbox, _relative_target
from ..executor import StepResult

WORK_MOUNT = "/work"
DEFAULT_IMAGE = "microsandbox/python"
DEFAULT_CPUS = 1
DEFAULT_MEMORY_MIB = 1024
TIMEOUT_EXIT_CODE = 124


def microsandbox_status(env: Any = None, which: Any = shutil.which) -> tuple[bool, str]:
    env = os.environ if env is None else env
    try:
        if importlib.util.find_spec("microsandbox") is None:
            return False, "microsandbox Python SDK is not installed (pip extra 'ambient')"
    except Exception:
        return False, "microsandbox Python SDK is not importable"
    if not (which("msb") or env.get("MSB_SERVER_URL")):
        return False, "no microsandbox server: install msb and run `msb server start`, or set MSB_SERVER_URL"
    return True, ""


class MicrosandboxSandbox(DisposableSandbox):
    name = "microsandbox"

    def __init__(self, job_dir: str | os.PathLike[str] = ".", image: str | None = None, cpus: int = DEFAULT_CPUS,
                 memory: int = DEFAULT_MEMORY_MIB, sdk: Any = None, status: tuple[bool, str] | None = None) -> None:
        self.job_dir = Path(job_dir).resolve()
        self.image = image or os.environ.get("O_MSB_IMAGE", DEFAULT_IMAGE)
        self.cpus = cpus
        self.memory = memory
        self._sdk = sdk
        self._status = status
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sb: Any = None
        self.synced = True

    @property
    def reason(self) -> str:
        if self._sdk is not None:
            return ""
        ok, reason = self._status if self._status is not None else microsandbox_status()
        return "" if ok else reason

    @property
    def available(self) -> bool:
        return self.reason == ""

    def _sandbox_cls(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        from microsandbox import Sandbox  # type: ignore[import-not-found]

        return Sandbox

    def _run_async(self, coro: Any, timeout: float | None = None) -> Any:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(asyncio.wait_for(coro, timeout) if timeout else coro)

    async def _create(self) -> Any:
        cls = self._sandbox_cls()
        name = f"o-{uuid.uuid4().hex[:12]}"
        try:
            return await cls.create(name, image=self.image, cpus=self.cpus, memory=self.memory,
                                    volumes=[f"{self.job_dir}:{WORK_MOUNT}"])
        except TypeError:
            self.synced = False
            return await cls.create(name, image=self.image, cpus=self.cpus, memory=self.memory)

    def run(self, step: Any, index: int = 0) -> StepResult:
        started = time.monotonic()
        rel = _relative_target(getattr(step, "target_directory", "."))
        cwd = WORK_MOUNT if rel == "." else f"{WORK_MOUNT}/{rel}"
        command, label = self._command_for(step, self.job_dir / ".o-scripts", WORK_MOUNT)
        timeout = float(getattr(step, "timeout_seconds", 600) or 600)
        try:
            if self._sb is None:
                self._sb = self._run_async(self._create(), timeout=60)
            res = self._run_async(self._sb.exec("sh", ["-c", f"mkdir -p {cwd} && cd {cwd} && {command}"]), timeout=timeout)
        except asyncio.TimeoutError:
            return StepResult(index=index, command=label, cwd=cwd, exit_code=TIMEOUT_EXIT_CODE, stdout="",
                              stderr=f"microsandbox step timed out after {timeout:g} s",
                              elapsed_ms=int((time.monotonic() - started) * 1000), timed_out=True)
        except Exception as exc:  # noqa: BLE001
            return StepResult(index=index, command=label, cwd=cwd, exit_code=127, stdout="",
                              stderr=f"microsandbox failed: {type(exc).__name__}: {exc}",
                              elapsed_ms=int((time.monotonic() - started) * 1000), timed_out=False)
        stdout = getattr(res, "stdout_text", None)
        stderr = getattr(res, "stderr_text", None)
        stdout = stdout() if callable(stdout) else stdout
        stderr = stderr() if callable(stderr) else stderr
        code = getattr(res, "exit_code", None)
        if code is None:
            code = getattr(res, "returncode", None)
        if code is None:
            success = getattr(res, "success", None)
            code = 0 if success in (None, True) else 1
        return StepResult(index=index, command=label, cwd=cwd, exit_code=int(code), stdout=str(stdout or ""),
                          stderr=str(stderr or ""), elapsed_ms=int((time.monotonic() - started) * 1000), timed_out=False)

    def extract(self, relpath: str) -> bytes:
        target = (self.job_dir / relpath).resolve()
        if target != self.job_dir and self.job_dir not in target.parents:
            raise ValueError(f"{relpath!r} resolves outside the job directory")
        return target.read_bytes()

    def destroy(self) -> None:
        sb, self._sb = self._sb, None
        if sb is not None:
            try:
                self._run_async(sb.stop(), timeout=20)
            except Exception:
                pass
        if self._loop is not None:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None
