"""Multi-language script runners.

A ``Runner`` turns a ``ScriptPayload`` into a ``StepResult``. ``NativeRunner`` is
always present: it writes the code to a temp file inside the sandbox and runs the
language's interpreter through ``Sandbox.run`` so env scrubbing, the timeout and
the DISPLAY rules are exactly those of a shell step. ``OpenInterpreterRunner``
delegates to Open Interpreter's ``interpreter.computer.run`` when that package is
importable and reports ``available=False`` otherwise. ``get_runner`` never raises.
"""
from __future__ import annotations

import os
import shlex
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping

from .executor import ISOLATED_DISPLAY, Sandbox, StepResult
from .schema import ProvisionStep, ScriptPayload

_MISSING_CWD_EXIT_CODE = 127
_TIMEOUT_EXIT_CODE = 124
_IS_WINDOWS = os.name == "nt"

# language -> (executable, *flags before the file path). Patchable for tests / platforms.
NATIVE_COMMANDS: dict[str, tuple[str, ...]] = {
    "python": ("python" if _IS_WINDOWS else "python3", "-I"),
    "bash": ("bash",),
    "javascript": ("node",),
    "powershell": ("powershell", "-NoProfile", "-NonInteractive", "-File") if _IS_WINDOWS else ("pwsh", "-NoProfile", "-NonInteractive", "-File"),
}
EXTENSIONS: dict[str, str] = {"python": "py", "bash": "sh", "javascript": "js", "powershell": "ps1"}
TEMP_PREFIX = ".o-run-"

# Open Interpreter's language names differ from ours for shells.
_OI_LANGUAGES: dict[str, str] = {"python": "python", "bash": "shell", "javascript": "javascript", "powershell": "powershell"}
_OI_ERROR_MARKERS = ("Traceback (most recent call last)",)


class Runner(ABC):
    """Runs a ScriptPayload inside a Sandbox and reports a StepResult."""

    name: str = "runner"
    available: bool = True
    reason: str = ""

    @abstractmethod
    def run(
        self,
        payload: ScriptPayload,
        sandbox: Sandbox,
        index: int = 0,
        env_extra: Mapping[str, str] | None = None,
        isolated_display: bool = False,
    ) -> StepResult:
        """Execute ``payload.code`` in ``payload.target_directory`` under ``sandbox``."""


def _failed(index: int, command: str, cwd: str, exit_code: int, stderr: str, elapsed_ms: int = 0, timed_out: bool = False) -> StepResult:
    return StepResult(index=index, command=command, cwd=cwd, exit_code=exit_code, stdout="", stderr=stderr,
                      elapsed_ms=elapsed_ms, timed_out=timed_out)


class NativeRunner(Runner):
    """python3 / bash / node / pwsh subprocess, via Sandbox.run, code in a temp file in the sandbox."""

    name = "native"

    def run(
        self,
        payload: ScriptPayload,
        sandbox: Sandbox,
        index: int = 0,
        env_extra: Mapping[str, str] | None = None,
        isolated_display: bool = False,
    ) -> StepResult:
        cwd = sandbox.resolve(payload.target_directory)  # SandboxEscape propagates, as for Sandbox.run
        argv = NATIVE_COMMANDS[payload.language]
        filename = f"{TEMP_PREFIX}{uuid.uuid4().hex}.{EXTENSIONS[payload.language]}"
        command = " ".join(_quote(a) for a in (*argv, filename))
        temp = cwd / filename
        try:
            temp.write_text(payload.code, encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return _failed(index, command, str(cwd), _MISSING_CWD_EXIT_CODE, f"target_directory {cwd} does not exist")
        except OSError as e:
            return _failed(index, command, str(cwd), 1, f"cannot write script to {cwd}: {e}")
        try:
            step = ProvisionStep(target_directory=str(cwd), command_string=command, timeout_seconds=payload.timeout_seconds)
            return sandbox.run(step, index=index, env_extra=env_extra, isolated_display=isolated_display)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass


def _quote(arg: str) -> str:
    if _IS_WINDOWS:
        return f'"{arg}"' if " " in arg else arg
    return shlex.quote(arg)


class OpenInterpreterRunner(Runner):
    """``interpreter.computer.run(language, code)`` when Open Interpreter is importable."""

    name = "open-interpreter"

    def __init__(self) -> None:
        self._interpreter: Any = None
        try:
            from interpreter import interpreter  # type: ignore[import-not-found]
        except Exception as e:  # ImportError, or anything the package raises on import
            self.available = False
            self.reason = f"open-interpreter not importable: {e.__class__.__name__}: {e}"
        else:
            self._interpreter = interpreter
            self.available = True
            self.reason = ""

    def run(
        self,
        payload: ScriptPayload,
        sandbox: Sandbox,
        index: int = 0,
        env_extra: Mapping[str, str] | None = None,
        isolated_display: bool = False,
    ) -> StepResult:
        cwd = sandbox.resolve(payload.target_directory)
        language = _OI_LANGUAGES[payload.language]
        command = f"open-interpreter:{language}"
        if not self.available:
            return _failed(index, command, str(cwd), 1, self.reason)
        # Same DISPLAY rules as Sandbox.run: the host display never reaches the script.
        # Not isolated -> DISPLAY and WAYLAND_DISPLAY are stripped for the duration of
        # the call; isolated -> DISPLAY comes from env_extra, else the ISOLATED_DISPLAY.
        display: str | None = None
        if isolated_display:
            if env_extra and env_extra.get("DISPLAY"):
                display = env_extra["DISPLAY"]
            elif sys.platform.startswith("linux"):
                display = ISOLATED_DISPLAY
        started = time.monotonic()
        outcome: dict[str, Any] = {}

        def work() -> None:
            try:
                outcome["chunks"] = self._interpreter.computer.run(language, payload.code)
            except BaseException as e:  # noqa: BLE001 - the thread must never die silently
                outcome["error"] = e

        with _display_scope(display):
            thread = threading.Thread(target=work, name="o-open-interpreter", daemon=True)
            thread.start()
            thread.join(payload.timeout_seconds)
            timed_out = thread.is_alive()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if timed_out:
            return _failed(index, command, str(cwd), _TIMEOUT_EXIT_CODE,
                           f"open-interpreter did not finish within {payload.timeout_seconds}s", elapsed_ms, True)
        if "error" in outcome:
            e = outcome["error"]
            return _failed(index, command, str(cwd), 1, f"{e.__class__.__name__}: {e}", elapsed_ms)
        stdout, stderr, failed = _collect(outcome.get("chunks"))
        return StepResult(index=index, command=command, cwd=str(cwd), exit_code=1 if failed else 0,
                          stdout=stdout, stderr=stderr, elapsed_ms=elapsed_ms, timed_out=False)


_DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY")


class _display_scope:
    """For the duration of the call: pop DISPLAY and WAYLAND_DISPLAY from os.environ, then
    set DISPLAY to ``display`` when one is given. On exit every variable is restored
    exactly (present with its old value, or absent). The scope always pops; it never skips."""

    def __init__(self, display: str | None) -> None:
        self.display = display
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> None:
        self._saved = {name: os.environ.pop(name, None) for name in _DISPLAY_VARS}
        if self.display is not None:
            os.environ["DISPLAY"] = self.display

    def __exit__(self, *exc: object) -> None:
        for name, old in self._saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        self._saved = {}


def _collect(chunks: Any) -> tuple[str, str, bool]:
    """Open Interpreter yields message dicts; join their text, flag error chunks."""
    out: list[str] = []
    err: list[str] = []
    failed = False
    if chunks is None:
        return "", "", False
    if isinstance(chunks, (str, bytes)):
        chunks = [chunks]
    for chunk in chunks:
        if isinstance(chunk, dict):
            text = chunk.get("content")
            fmt = str(chunk.get("format", ""))
            kind = str(chunk.get("type", ""))
        else:
            text, fmt, kind = chunk, "", ""
        if text is None:
            continue
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        text = str(text)
        is_error = fmt == "error" or kind == "error" or any(m in text for m in _OI_ERROR_MARKERS)
        if is_error:
            failed = True
            err.append(text)
        else:
            out.append(text)
    return "".join(out), "".join(err), failed


def get_runner(prefer: str = "native") -> Runner:
    """The preferred runner when usable, else the native one. Never raises."""
    if prefer == "open-interpreter":
        try:
            oi = OpenInterpreterRunner()
            if oi.available:
                return oi
        except Exception:
            pass
    return NativeRunner()


__all__ = ["Runner", "NativeRunner", "OpenInterpreterRunner", "get_runner", "NATIVE_COMMANDS", "EXTENSIONS", "TEMP_PREFIX"]
