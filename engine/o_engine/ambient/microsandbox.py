"""microsandbox (``superradcompany/microsandbox``, Apache-2.0) microVM sandbox: libkrun microVMs on
Apple Silicon macOS, Linux/KVM and Windows/WHP. First choice for headless jobs while the user is away.

Aligned to the installed SDK, PyPI ``microsandbox`` 0.7.13 (``microsandbox/_microsandbox.pyi``):

* No server or daemon. The wheel bundles the ``msb`` runtime and ``libkrunfw``
  (``microsandbox._bundled``); ``microsandbox.is_runtime_installed()`` says whether a complete pair
  resolves (``MSB_PATH`` / ``MSB_LIBKRUNFW_PATH`` override it). The host still needs hardware
  virtualization: ``/dev/kvm`` on Linux, Apple Silicon on macOS, WHP on Windows. Without KVM,
  ``Sandbox.create`` pulls the image and then raises ``MicrosandboxError`` ("sandbox process exited
  (signal: 6 (SIGABRT)) before agent relay became available").
* ``await Sandbox.create(name, *, image=, cpus=, memory=<MiB>, volumes={guest_path: MountConfig},
  replace=, ...)`` -> ``Sandbox``. ``volumes`` must be a Mapping (a list raises ``TypeError: volumes
  must be a Mapping``); ``Volume.bind(host_path)`` builds the ``MountConfig`` for a bind mount.
* ``await sb.exec(cmd, args, *, cwd=, env=, timeout=)`` -> ``ExecOutput`` with properties
  ``exit_code: int``, ``success: bool``, ``stdout_text: str``, ``stderr_text: str``.
* ``await sb.stop()`` then ``await Sandbox.remove(name)`` (``SandboxNotFoundError`` if already gone).

Every coroutine runs on a private event loop owned by the job thread (never the server's loop).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import platform as _platform
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..containment import DisposableSandbox, _relative_target
from ..executor import StepResult

WORK_MOUNT = "/work"
DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_CPUS = 1
DEFAULT_MEMORY_MIB = 1024
TIMEOUT_EXIT_CODE = 124
CREATE_TIMEOUT_S = 180.0
KVM_DEVICE = "/dev/kvm"


def _runtime_installed() -> tuple[bool, str]:
    try:
        import microsandbox  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        return False, f"microsandbox SDK failed to import ({type(exc).__name__}: {exc})"
    try:
        if not microsandbox.is_runtime_installed():
            return False, "microsandbox runtime (msb + libkrunfw) does not resolve; reinstall the wheel or set MSB_PATH"
    except Exception as exc:  # noqa: BLE001
        return False, f"microsandbox runtime check failed ({type(exc).__name__}: {exc})"
    return True, ""


def microsandbox_status(find_spec: Callable[[str], Any] = importlib.util.find_spec, platform: str | None = None,
                        machine: str | None = None, kvm_path: str = KVM_DEVICE,
                        access: Callable[[str, int], bool] = os.access,
                        runtime_check: Callable[[], tuple[bool, str]] | None = None) -> tuple[bool, str]:
    """(usable, reason). Checks the SDK, the host's virtualization, then the bundled runtime."""
    platform = sys.platform if platform is None else platform
    machine = (_platform.machine() if machine is None else machine).lower()
    try:
        if find_spec("microsandbox") is None:
            return False, "microsandbox Python SDK is not installed (pip extra 'ambient')"
    except Exception:
        return False, "microsandbox Python SDK is not importable"
    if platform.startswith("linux"):
        if not access(kvm_path, os.R_OK | os.W_OK):
            return False, f"no KVM ({kvm_path} missing or not read/writable by this user); microsandbox needs hardware virtualization"
    elif platform == "darwin":
        if machine not in ("arm64", "aarch64"):
            return False, "microsandbox on macOS needs Apple Silicon"
    elif not platform.startswith("win"):
        return False, f"microsandbox does not support {platform}"
    return (runtime_check or _runtime_installed)()


class MicrosandboxSandbox(DisposableSandbox):
    name = "microsandbox"

    def __init__(self, job_dir: str | os.PathLike[str] = ".", image: str | None = None, cpus: int = DEFAULT_CPUS,
                 memory: int = DEFAULT_MEMORY_MIB, sdk: Any = None, volume: Any = None,
                 status: tuple[bool, str] | None = None) -> None:
        self.job_dir = Path(job_dir).resolve()
        self.image = image or os.environ.get("O_MSB_IMAGE", DEFAULT_IMAGE)
        self.cpus = cpus
        self.memory = memory
        self._sdk = sdk
        self._volume = volume
        self._status = status
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sb: Any = None
        self.vm_name = ""
        self.synced = True

    @property
    def reason(self) -> str:
        if self._sdk is not None and self._status is None:
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

    def _volume_cls(self) -> Any:
        if self._volume is not None:
            return self._volume
        from microsandbox import Volume  # type: ignore[import-not-found]

        return Volume

    def _run_async(self, coro: Any, timeout: float | None = None) -> Any:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(asyncio.wait_for(coro, timeout) if timeout else coro)

    def create_kwargs(self) -> dict[str, Any]:
        return {"image": self.image, "cpus": int(self.cpus), "memory": int(self.memory),
                "volumes": {WORK_MOUNT: self._volume_cls().bind(str(self.job_dir))}, "replace": True}

    async def _create(self) -> Any:
        self.vm_name = f"o-{uuid.uuid4().hex[:12]}"
        return await self._sandbox_cls().create(self.vm_name, **self.create_kwargs())

    def run(self, step: Any, index: int = 0) -> StepResult:
        started = time.monotonic()
        rel = _relative_target(getattr(step, "target_directory", "."))
        cwd = WORK_MOUNT if rel == "." else f"{WORK_MOUNT}/{rel}"
        command, label = self._command_for(step, self.job_dir / ".o-scripts", WORK_MOUNT)
        timeout = float(getattr(step, "timeout_seconds", 600) or 600)

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            if self._sb is None:
                self._sb = self._run_async(self._create(), timeout=CREATE_TIMEOUT_S)
            res = self._run_async(self._sb.exec("sh", ["-c", f"mkdir -p {cwd} && cd {cwd} && {command}"],
                                                timeout=timeout), timeout=timeout + 15)
        except asyncio.TimeoutError:
            return StepResult(index=index, command=label, cwd=cwd, exit_code=TIMEOUT_EXIT_CODE, stdout="",
                              stderr=f"microsandbox step timed out after {timeout:g} s", elapsed_ms=elapsed(), timed_out=True)
        except Exception as exc:  # noqa: BLE001 - ExecTimeoutError, MicrosandboxError, ...
            timed_out = type(exc).__name__ == "ExecTimeoutError"
            return StepResult(index=index, command=label, cwd=cwd, exit_code=TIMEOUT_EXIT_CODE if timed_out else 127,
                              stdout="", stderr=f"microsandbox failed: {type(exc).__name__}: {exc}",
                              elapsed_ms=elapsed(), timed_out=timed_out)
        code = getattr(res, "exit_code", None)
        if code is None:
            code = 0 if getattr(res, "success", True) else 1
        return StepResult(index=index, command=label, cwd=cwd, exit_code=int(code),
                          stdout=str(getattr(res, "stdout_text", "") or ""),
                          stderr=str(getattr(res, "stderr_text", "") or ""), elapsed_ms=elapsed(), timed_out=False)

    def extract(self, relpath: str) -> bytes:
        target = (self.job_dir / relpath).resolve()
        if target != self.job_dir and self.job_dir not in target.parents:
            raise ValueError(f"{relpath!r} resolves outside the job directory")
        return target.read_bytes()

    def destroy(self) -> None:
        sb, self._sb = self._sb, None
        if sb is not None:
            try:
                self._run_async(sb.stop(), timeout=30)
            except Exception:
                pass
        if self.vm_name and self._loop is not None:
            try:
                self._run_async(self._sandbox_cls().remove(self.vm_name), timeout=30)
            except Exception:
                pass
        if self._loop is not None:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None
