"""Sandboxed step execution and surgical file patching."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field

from .schema import ProvisionStep

ISOLATED_DISPLAY = ":99"
_SCRUB_PREFIXES = ("CLAUDE", "ANTHROPIC", "O_")
_SCRUB_SUBSTRINGS = ("TOKEN", "SECRET", "KEY", "PASSWORD")
_TIMEOUT_EXIT_CODE = 124
_MISSING_CWD_EXIT_CODE = 127


class SandboxEscape(Exception):
    """A path would leave the sandbox root."""


class PatchError(Exception):
    """A patch cannot be applied exactly once."""


class StepResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    command: str
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    elapsed_ms: int = Field(ge=0)
    timed_out: bool


class Patch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=1024)
    find: str = Field(min_length=1, max_length=20000)
    replace: str = Field(max_length=20000)


def tail(s: str, n: int = 40) -> str:
    lines = s.splitlines()
    if len(lines) <= n:
        return s
    return "\n".join(lines[-n:])


def scrub_env(env: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in env.items():
        upper = key.upper()
        if upper.startswith(_SCRUB_PREFIXES):
            continue
        if any(part in upper for part in _SCRUB_SUBSTRINGS):
            continue
        out[key] = value
    return out


class Sandbox:
    def __init__(self, root: str | os.PathLike[str]):
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        self.root = path.resolve()

    def resolve(self, target_directory: str) -> Path:
        candidate = Path(target_directory)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise SandboxEscape(f"{target_directory!r} resolves outside sandbox root")
        return resolved

    def run(
        self,
        step: ProvisionStep,
        index: int = 0,
        env_extra: Mapping[str, str] | None = None,
        isolated_display: bool = False,
    ) -> StepResult:
        if step.command_string is None:
            raise ValueError("script steps must go through a Runner")
        cwd = self.resolve(step.target_directory)
        env = scrub_env(os.environ)
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        if env_extra:
            env.update(env_extra)
        if isolated_display and sys.platform.startswith("linux"):
            env["DISPLAY"] = ISOLATED_DISPLAY
        started = time.monotonic()
        timed_out = False
        try:
            exit_code, stdout, stderr, timed_out = run_command(
                step.command_string, cwd=str(cwd), env=env, timeout=step.timeout_seconds
            )
        except (FileNotFoundError, NotADirectoryError):
            exit_code = _MISSING_CWD_EXIT_CODE
            stdout = ""
            stderr = f"target_directory {cwd} does not exist"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return StepResult(
            index=index,
            command=step.command_string,
            cwd=str(cwd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            elapsed_ms=elapsed_ms,
            timed_out=timed_out,
        )


def run_command(command: str, *, cwd: str, env: Mapping[str, str], timeout: float,
                popen: type[subprocess.Popen] | None = None) -> tuple[int, str, str, bool]:
    """``(exit_code, stdout, stderr, timed_out)`` of ``command`` run through the shell in its own session
    (process group). On timeout the whole group is killed, not just the shell: a ``npm install`` or ``sleep``
    the shell forked would otherwise keep running (and keep the pipes open) after the step reported 124."""
    popen = popen or subprocess.Popen
    kwargs: dict = dict(shell=True, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, errors="replace")
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = popen(command, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, _decode(stdout), _decode(stderr), False
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - the group is dead or unreachable; report what we have
            stdout, stderr = "", ""
        return _TIMEOUT_EXIT_CODE, _decode(stdout), _decode(stderr), True


def kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL ``proc``'s whole process group (POSIX) or the process (Windows). Never raises."""
    import signal

    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def apply_patch(sandbox: Sandbox, patch: Patch) -> str:
    target = Path(patch.path)
    if not target.is_absolute():
        target = sandbox.root / target
    sandbox.resolve(str(target.parent))
    resolved = target.resolve()
    if sandbox.root not in resolved.parents:
        raise SandboxEscape(f"{patch.path!r} resolves outside sandbox root")
    if not resolved.is_file():
        raise PatchError(f"{patch.path!r} is not a file")
    text = resolved.read_text(encoding="utf-8")
    count = text.count(patch.find)
    if count != 1:
        raise PatchError(f"find occurs {count} times in {patch.path!r}; expected exactly 1")
    new_text = text.replace(patch.find, patch.replace, 1)
    data = new_text.encode("utf-8")
    resolved.write_bytes(data)
    return hashlib.sha256(data).hexdigest()
