"""Structured crash diagnostics.

stderr is scanned line by line for the first recognisable error, the category and
any missing package are lifted from that match, the last 120 s of telemetry are
attached, and the parser is asked for remediation steps. Output is static JSON.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

STDERR_TAIL_LINES = 40

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("missing_module", re.compile(r"ModuleNotFoundError: No module named ['\"]?(?P<pkg>[\w.\-]+)['\"]?")),
    ("missing_module", re.compile(r"ImportError: No module named ['\"]?(?P<pkg>[\w.\-]+)['\"]?")),
    ("missing_module", re.compile(r"Error: Cannot find module ['\"](?P<pkg>[^'\"]+)['\"]")),
    ("missing_module", re.compile(r"Module not found: (?:Error: )?Can't resolve ['\"](?P<pkg>[^'\"]+)['\"]")),
    ("port_in_use", re.compile(r"EADDRINUSE|address already in use", re.IGNORECASE)),
    ("permission", re.compile(r"Permission denied|EACCES|PermissionError", re.IGNORECASE)),
    ("command_not_found", re.compile(r"command not found|is not recognized as an internal or external command|^(?:sh|bash|zsh|dash): (?:\d+: |line \d+: )?(?P<pkg>[\w.\-]+): not found", re.IGNORECASE)),
    ("npm", re.compile(r"npm ERR!|npm error", re.IGNORECASE)),
    ("missing_file", re.compile(r"ENOENT|No such file or directory|FileNotFoundError", re.IGNORECASE)),
    ("pip", re.compile(r"ERROR: Could not find a version|ERROR: No matching distribution found for (?P<pkg>[\w.\-\[\]]+)")),
    ("python_exception", re.compile(r"^\s*(?:\w+\.)*\w+(?:Error|Exception)\b.*")),
    ("generic", re.compile(r"\b(?:error|fatal|failed)\b", re.IGNORECASE)),
)

_PKG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"No module named ['\"]?(?P<pkg>[\w.\-]+)['\"]?"),
    re.compile(r"Cannot find module ['\"](?P<pkg>[^'\"]+)['\"]"),
    re.compile(r"Can't resolve ['\"](?P<pkg>[^'\"]+)['\"]"),
    re.compile(r"(?:bash|sh|zsh): (?:line \d+: )?(?P<pkg>[\w.\-]+): command not found"),
    re.compile(r"['\"]?(?P<pkg>[\w.\-]+)['\"]? is not recognized as an internal or external command"),
    re.compile(r"No matching distribution found for (?P<pkg>[\w.\-\[\]]+)"),
)


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = ""
    cwd: str = ""
    exit_code: int | None = None
    error_line: str = ""
    missing_package: str | None = None
    category: str = "unknown"
    stderr_tail: list[str] = Field(default_factory=list)
    telemetry: list[dict[str, Any]] = Field(default_factory=list)
    suggested_steps: list[dict[str, Any]] = Field(default_factory=list)


def _first_error(stderr: str, stdout: str) -> tuple[str, str, str | None]:
    for text in (stderr, stdout):
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            for category, pattern in _PATTERNS:
                m = pattern.search(line)
                if not m:
                    continue
                pkg = m.groupdict().get("pkg") if m.groupdict() else None
                if not pkg:
                    for pp in _PKG_PATTERNS:
                        pm = pp.search(line)
                        if pm:
                            pkg = pm.group("pkg")
                            break
                if pkg and category == "missing_module":
                    pkg = pkg.split(".")[0] if not pkg.startswith((".", "/")) else pkg
                return line.strip(), category, pkg
    return "", "unknown", None


def diagnose(
    payload: dict[str, Any],
    telemetry_recent: list[dict[str, Any]] | Callable[[], list[dict[str, Any]]] | None,
    parser: Any,
) -> Diagnostic:
    stderr = str(payload.get("stderr") or "")
    stdout = str(payload.get("stdout") or "")
    command = str(payload.get("command") or "")
    cwd = str(payload.get("cwd") or "")
    exit_code = payload.get("exit_code")
    try:
        exit_code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        exit_code = None

    error_line, category, pkg = _first_error(stderr, stdout)

    telemetry: list[dict[str, Any]] = []
    try:
        if callable(telemetry_recent):
            telemetry = list(telemetry_recent() or [])
        elif telemetry_recent:
            telemetry = list(telemetry_recent)
    except Exception:
        telemetry = []

    suggested: list[dict[str, Any]] = []
    if parser is not None and error_line:
        try:
            request = parser.parse(error_line, goal=command)
            suggested = [s.model_dump() for s in request.steps]
        except Exception:
            suggested = []

    tail = [l.rstrip() for l in stderr.splitlines() if l.strip()][-STDERR_TAIL_LINES:]
    return Diagnostic(
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        error_line=error_line,
        missing_package=pkg,
        category=category,
        stderr_tail=tail,
        telemetry=telemetry,
        suggested_steps=suggested,
    )
