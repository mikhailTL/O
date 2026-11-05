"""Append-only cache of rendered provisioning scripts keyed by request fingerprint."""
from __future__ import annotations

import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .executor import Sandbox, StepResult
from .schema import ProvisionRequest, ProvisionStep

_WINDOWS_PLATFORMS = ("win32", "windows", "cygwin")


class CacheExists(Exception):
    """An entry for this fingerprint already exists; entries are never overwritten."""


class CacheEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fingerprint: str = Field(min_length=1)
    label: str
    created: str
    platform: str
    script_path: str
    timeout_seconds: int = Field(ge=1)
    runs: int = Field(default=0, ge=0)


def _is_windows(platform: str) -> bool:
    return platform.lower().startswith(_WINDOWS_PLATFORMS)


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


_SCRIPT_EXT = {"python": ".py", "bash": ".sh", "javascript": ".js", "powershell": ".ps1"}
_SCRIPT_RUN_SH = {
    "python": "python3 -I {f}",
    "bash": "bash {f}",
    "javascript": "node {f}",
    "powershell": "pwsh -NoProfile -NonInteractive -ExecutionPolicy Bypass -File {f}",
}
_SCRIPT_RUN_PS = {
    "python": "& python -I {f}",
    "bash": "& bash {f}",
    "javascript": "& node {f}",
    "powershell": "& pwsh -NoProfile -NonInteractive -ExecutionPolicy Bypass -File {f}",
}


def _heredoc_tag(code: str) -> str:
    tag = "O_STEP_EOF"
    while tag in code:
        tag += "_"
    return tag


def render_script(request: ProvisionRequest, platform: str) -> str:
    """Render the request as one shell script (bash or PowerShell).

    Script steps are written to a temp file in the step directory via a quoted heredoc
    (bash) or ``Set-Content`` (PowerShell), run by the language's interpreter, and
    removed again, so a cached replay reproduces the run without the Runner.
    """
    lines: list[str] = []
    if _is_windows(platform):
        lines.append("$ErrorActionPreference = 'Stop'")
        lines.append("$__o_root = (Get-Location).Path")
        for n, step in enumerate(request.steps):
            lines.append(f"Set-Location -LiteralPath (Join-Path $__o_root {_ps_quote(step.target_directory)})")
            if step.script is not None:
                fname = f".o-step-{n}{_SCRIPT_EXT[step.script.language]}"
                here = "@'\n" + step.script.code.rstrip("\n") + "\n'@"
                lines.append(f"Set-Content -LiteralPath {_ps_quote(fname)} -NoNewline -Encoding UTF8 -Value {here}")
                lines.append(_SCRIPT_RUN_PS[step.script.language].format(f=_ps_quote(fname)))
                lines.append("$__o_rc = $LASTEXITCODE")
                lines.append(f"Remove-Item -LiteralPath {_ps_quote(fname)} -ErrorAction SilentlyContinue")
                lines.append("if ($__o_rc) { exit $__o_rc }")
            else:
                lines.append(step.command_string)
                lines.append("if ($LASTEXITCODE) { exit $LASTEXITCODE }")
        return "\n".join(lines) + "\n"
    lines.append("#!/usr/bin/env bash")
    lines.append("set -e")
    lines.append('__o_root="$PWD"')
    for n, step in enumerate(request.steps):
        lines.append(f'cd "$__o_root"/{shlex.quote(step.target_directory)}')
        if step.script is not None:
            fname = f".o-step-{n}{_SCRIPT_EXT[step.script.language]}"
            tag = _heredoc_tag(step.script.code)
            lines.append(f"cat > {shlex.quote(fname)} <<'{tag}'")
            lines.append(step.script.code.rstrip("\n"))
            lines.append(tag)
            lines.append("__o_rc=0")
            lines.append(_SCRIPT_RUN_SH[step.script.language].format(f=shlex.quote(fname)) + " || __o_rc=$?")
            lines.append(f"rm -f {shlex.quote(fname)}")
            lines.append('[ "$__o_rc" -eq 0 ] || exit "$__o_rc"')
        else:
            lines.append(step.command_string)
    return "\n".join(lines) + "\n"


class ScriptCache:
    def __init__(self, dir: str | os.PathLike[str]):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir = self.dir.resolve()

    def _json_path(self, fingerprint: str) -> Path:
        return self.dir / f"{fingerprint}.json"

    def _read(self, path: Path) -> CacheEntry:
        return CacheEntry.model_validate_json(path.read_text(encoding="utf-8"))

    def lookup(self, fingerprint: str) -> CacheEntry | None:
        path = self._json_path(fingerprint)
        if not path.is_file():
            return None
        return self._read(path)

    def store(self, fingerprint: str, request: ProvisionRequest, platform: str) -> CacheEntry:
        ext = ".ps1" if _is_windows(platform) else ".sh"
        script_name = f"{fingerprint}{ext}"
        script_file = self.dir / script_name
        json_file = self._json_path(fingerprint)
        if json_file.exists() or script_file.exists():
            raise CacheExists(fingerprint)
        entry = CacheEntry(
            fingerprint=fingerprint,
            label=request.label,
            created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            platform=platform,
            script_path=script_name,
            timeout_seconds=max(step.timeout_seconds for step in request.steps),
            runs=0,
        )
        try:
            with open(script_file, "x", encoding="utf-8", newline="\n") as f:
                f.write(render_script(request, platform))
            with open(json_file, "x", encoding="utf-8") as f:
                f.write(entry.model_dump_json(indent=2))
        except FileExistsError:
            raise CacheExists(fingerprint) from None
        if ext == ".sh":
            os.chmod(script_file, 0o755)
        return entry

    def _script_file(self, entry: CacheEntry) -> Path:
        script = (self.dir / entry.script_path).resolve()
        if self.dir not in script.parents:
            raise ValueError(f"script_path {entry.script_path!r} is outside the cache directory")
        if not script.is_file():
            raise FileNotFoundError(str(script))
        return script

    def replay(self, entry: CacheEntry, sandbox: Sandbox) -> StepResult:
        script = self._script_file(entry)
        timeout = entry.timeout_seconds
        if entry.script_path.endswith(".ps1"):
            shell = "pwsh" if sys.platform != "win32" else "powershell"
            command = f"{shell} -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"{script}\""
        else:
            command = f"bash {shlex.quote(str(script))}"
        step = ProvisionStep(target_directory=".", command_string=command, timeout_seconds=timeout)
        return sandbox.run(step, index=0)

    def index(self) -> list[CacheEntry]:
        entries = [self._read(p) for p in self.dir.glob("*.json")]
        return sorted(entries, key=lambda e: e.fingerprint)

    def bump_runs(self, fingerprint: str) -> int:
        path = self._json_path(fingerprint)
        if not path.is_file():
            raise KeyError(fingerprint)
        entry = self._read(path)
        updated = entry.model_copy(update={"runs": entry.runs + 1})
        path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
        return updated.runs
