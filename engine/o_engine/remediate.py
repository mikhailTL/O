"""Bounded self-repair after a failed execute or verify.

The failing step's stderr is read line by line; the first line that matches a
known error pattern anchors the text handed to ``Parser.parse``. The parser
returns replacement steps. A step whose command is ``patch:<path>|||<find>|||<replace>``
is a surgical file edit applied through the sandbox; every other command step
replaces the failing step. A script step (``command_string`` is None) is not a
remediation: it is rejected with a logged note and dropped. Nothing here runs
commands.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from pydantic import ValidationError

from .actionlog import ActionLog
from .executor import Patch  # noqa: F401
from .schema import ProvisionRequest, ProvisionStep, SchemaViolation

PATCH_PREFIX = "patch:"
PATCH_SEP = "|||"
STDERR_TAIL_LINES = 40

ERROR_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ModuleNotFoundError",
        r"ImportError",
        r"npm ERR!",
        r"command not found",
        r"not recognized as an internal or external command",
        r"ENOENT",
        r"EACCES",
        r"EADDRINUSE",
        r"No such file or directory",
        r"Permission denied",
        r"address already in use",
        r"port .* (?:is )?(?:already )?in use",
        r"SyntaxError",
        r"TypeError",
        r"NameError",
        r"FileNotFoundError",
        r"fatal:",
        r"error\[E\d+\]",
        r"\berror\b",
        r"\bfailed\b",
        r"Traceback",
    )
)


def parse_patch(command: str | None) -> Patch | None:
    """The ``Patch`` a command encodes, or None for any other command (including a
    script step's ``None``)."""
    if command is None or not command.startswith(PATCH_PREFIX):
        return None
    parts = command[len(PATCH_PREFIX):].split(PATCH_SEP)
    if len(parts) != 3:
        raise SchemaViolation(f"malformed patch command: expected path{PATCH_SEP}find{PATCH_SEP}replace")
    try:
        return Patch(path=parts[0].strip(), find=parts[1], replace=parts[2])
    except ValidationError as e:
        raise SchemaViolation(f"invalid patch: {e.errors()[0]['msg']}") from None


def first_error_line(text: str) -> tuple[str | None, str | None]:
    """The most specific error line: patterns are ordered specific-first, and a traceback
    names its real error last, so the earliest pattern wins and, within it, the last line."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for pattern in ERROR_PATTERNS:
        for stripped in reversed(lines):
            if pattern.search(stripped):
                return stripped, pattern.pattern
    return None, None


def failing_step(state: dict) -> dict | None:
    for step in reversed(state.get("steps") or []):
        if step.get("exit_code", 0) != 0 or step.get("timed_out"):
            return step
    return None


def failure_text(state: dict) -> str:
    step = failing_step(state)
    if step is not None:
        text = step.get("stderr_tail") or step.get("stdout_tail") or f"exit code {step.get('exit_code')}"
        return text
    verify = state.get("verify") or {}
    if verify and not verify.get("ok", True):
        return f"verify failed: status={verify.get('status')} error={verify.get('error')}"
    return state.get("error") or "unknown failure"


def _default_apply_patch(sandbox: Any, patch: Patch) -> Any:
    from .executor import apply_patch

    return apply_patch(sandbox, patch)


class Remediator:
    def __init__(
        self,
        parser: Any,
        sandbox: Any,
        log: ActionLog,
        apply_patch: Optional[Callable[[Any, Patch], Any]] = None,
    ) -> None:
        self.parser = parser
        self.sandbox = sandbox
        self.log = log
        self.apply_patch = apply_patch or _default_apply_patch

    def plan(self, state: dict) -> list[Patch] | ProvisionRequest:
        parsed = self._parse(state)
        patches = [parse_patch(s.command_string) for s in parsed.steps]
        if patches and all(p is not None for p in patches):
            return patches
        return parsed

    def remediate(self, state: dict, platform: str, plan: list[Patch] | ProvisionRequest | None = None) -> dict:
        job_id = state.get("id")
        label = state["label"]
        attempt = int(state.get("attempt", 0)) + 1
        text = failure_text(state)
        error_line, pattern = first_error_line(text)
        step = failing_step(state)
        diagnostic = {
            "error_line": error_line,
            "pattern": pattern,
            "failed_index": None if step is None else step.get("index"),
            "failed_command": None if step is None else step.get("command"),
            "attempt": attempt,
        }
        headline = error_line or (text.strip().splitlines()[-1] if text.strip() else "no output")
        self.log.record("remediate", f"remediate attempt {attempt}: {headline[:160]}", job_id=job_id, label=label, **diagnostic)
        if plan is None:
            try:
                plan = self.plan(state)
            except SchemaViolation as e:
                self.log.record("remediate", f"remediate attempt {attempt}: parser rejected ({e})", job_id=job_id, attempt=attempt, ok=False)
                return {
                    "attempt": attempt,
                    "cached": False,
                    "diagnostic": {**diagnostic, "parser_error": str(e)},
                    "error": f"remediation parser failed: {e}",
                }

        patches: list[Patch] = []
        replacement: list[ProvisionStep] = []
        rejected = 0
        if isinstance(plan, ProvisionRequest):
            for s in plan.steps:
                if s.command_string is None:
                    rejected += 1
                    language = getattr(s.script, "language", None)
                    self.log.record(
                        "remediate", f"remediate attempt {attempt}: rejected script step ({language}); remediation steps must be commands",
                        job_id=job_id, attempt=attempt, language=language, ok=False,
                    )
                    continue
                patch = parse_patch(s.command_string)
                if patch is None:
                    replacement.append(s)
                else:
                    patches.append(patch)
        else:
            patches = list(plan)
        diagnostic["rejected_script_steps"] = rejected
        if rejected and not patches and not replacement:
            self.log.record("remediate", f"remediate attempt {attempt}: plan held only script steps; nothing to apply",
                            job_id=job_id, attempt=attempt, ok=False)
            return {
                "attempt": attempt,
                "cached": False,
                "diagnostic": diagnostic,
                "error": f"remediation produced only script steps ({rejected}); remediation steps must be commands",
            }

        applied = 0
        for patch in patches:
            result = self.apply_patch(self.sandbox, patch)
            applied += 1
            self.log.record(
                "remediate", f"patched {patch.path}", job_id=job_id, attempt=attempt,
                path=patch.path, find=patch.find[:200], replace=patch.replace[:200], result=result,
            )

        request: ProvisionRequest = state["request"]
        if replacement:
            request = _replace_failing_step(request, step, replacement)
            self.log.record(
                "remediate",
                f"replaced step {None if step is None else step.get('index')} with {len(replacement)} step(s)",
                job_id=job_id, attempt=attempt, commands=[s.command_string for s in replacement],
            )
        else:
            self.log.record("remediate", "retrying unchanged steps after patches", job_id=job_id, attempt=attempt)

        return {
            "request": request,
            "fingerprint": request.fingerprint(platform),
            "attempt": attempt,
            "cached": False,
            "patches_applied": int(state.get("patches_applied", 0)) + applied,
            "diagnostic": {**diagnostic, "patches": applied, "replacement": [s.command_string for s in replacement]},
            "error": None,
        }

    def _parse(self, state: dict, text: str | None = None, error_line: str | None = None) -> ProvisionRequest:
        if text is None:
            text = failure_text(state)
            error_line, _ = first_error_line(text)
        tail = text.splitlines()[-STDERR_TAIL_LINES:]
        body = "\n".join(tail)
        prompt = body if error_line is None or body.startswith(error_line) else f"{error_line}\n{body}"
        return self.parser.parse(prompt, goal=state["label"])


def _replace_failing_step(request: ProvisionRequest, failed: dict | None, replacement: list[ProvisionStep]) -> ProvisionRequest:
    steps = list(request.steps)
    if failed is None or failed.get("index") is None or failed["index"] >= len(steps):
        new_steps = steps + replacement
    else:
        i = int(failed["index"])
        new_steps = steps[:i] + replacement + steps[i + 1:]
    return ProvisionRequest(
        label=request.label,
        steps=tuple(new_steps),
        verify=request.verify,
        isolated_display=request.isolated_display,
    )
