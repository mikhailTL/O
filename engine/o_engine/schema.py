"""Strict type and boundary enforcement.

Every provisioning step the engine is asked to run must parse into an immutable
``ProvisionStep``: one directory, one hard timeout, and exactly one of a shell
``command_string`` or a multi-language ``script`` payload. Conversational text,
markdown, or extra properties raise ``SchemaViolation``; nothing partial is ever
accepted.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MAX_TIMEOUT_SECONDS = 60 * 60
MAX_COMMAND_LENGTH = 4000
MAX_SCRIPT_LENGTH = 65536

ScriptLanguage = Literal["python", "bash", "javascript", "powershell"]
SCRIPT_LANGUAGES: tuple[str, ...] = ("python", "bash", "javascript", "powershell")
RiskLevel = Literal["normal", "high"]
EXACTLY_ONE_RULE = "a step must have exactly one of command_string or script"

_FENCE = re.compile(r"```|~~~")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_PROSE_OPENER = re.compile(
    r"^\s*(?:sure|okay|ok|here(?:'s| is| are)|let'?s|i(?:'ll| will| would| can)|first(?:ly)?|next|then|now|"
    r"finally|step\s*\d+|note:|the following|you (?:can|should)|we (?:can|should|need))\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}\s+[A-Za-z]{3,}[.!?](\s|$)")


class SchemaViolation(ValueError):
    """Raised when input does not conform to the step schema. Aborts the queue."""


def _check_dir(v: str) -> str:
    if "\n" in v or "\r" in v or "\x00" in v:
        raise ValueError("target_directory must be a single path")
    if _FENCE.search(v):
        raise ValueError("target_directory contains markdown")
    return v


class ScriptPayload(BaseModel):
    """Source code in one language, run in one directory, with one hard timeout."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    language: ScriptLanguage
    code: str = Field(min_length=1, max_length=MAX_SCRIPT_LENGTH)
    target_directory: str = Field(min_length=1, max_length=1024)
    timeout_seconds: int = Field(ge=1, le=MAX_TIMEOUT_SECONDS)

    @field_validator("target_directory")
    @classmethod
    def _dir_is_plain(cls, v: str) -> str:
        return _check_dir(v.strip())

    @field_validator("code")
    @classmethod
    def _code_has_no_nul(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("code contains NUL")
        return v


class ProvisionStep(BaseModel):
    """One shell command OR one script, run in one directory, with one hard timeout.

    Exactly one of ``command_string`` / ``script`` is set. When ``script`` is set,
    ``target_directory`` and ``timeout_seconds`` are the payload's (copied when
    omitted; rejected when they differ).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, str_strip_whitespace=True)

    target_directory: str = Field(min_length=1, max_length=1024)
    command_string: str | None = Field(default=None, min_length=1, max_length=MAX_COMMAND_LENGTH)
    timeout_seconds: int = Field(ge=1, le=MAX_TIMEOUT_SECONDS)
    script: ScriptPayload | None = None

    @model_validator(mode="before")
    @classmethod
    def _exactly_one_and_script_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        has_command = data.get("command_string") is not None
        script = data.get("script")
        if has_command == (script is not None):
            raise ValueError(EXACTLY_ONE_RULE)
        if script is None:
            return data
        if isinstance(script, ScriptPayload):
            s_dir, s_timeout = script.target_directory, script.timeout_seconds
        elif isinstance(script, dict):
            s_dir, s_timeout = script.get("target_directory"), script.get("timeout_seconds")
        else:
            return data
        out = dict(data)
        for key, value in (("target_directory", s_dir), ("timeout_seconds", s_timeout)):
            if key not in out or out[key] is None:
                out[key] = value
            elif out[key] != value:
                raise ValueError(f"step {key} must equal script.{key} when a script is given")
        return out

    @model_validator(mode="after")
    def _consistent(self) -> "ProvisionStep":
        if (self.command_string is None) == (self.script is None):
            raise ValueError(EXACTLY_ONE_RULE)
        if self.script is not None:
            if self.script.target_directory != self.target_directory:
                raise ValueError("step target_directory must equal script.target_directory when a script is given")
            if self.script.timeout_seconds != self.timeout_seconds:
                raise ValueError("step timeout_seconds must equal script.timeout_seconds when a script is given")
        return self

    @field_validator("target_directory")
    @classmethod
    def _dir_is_plain(cls, v: str) -> str:
        return _check_dir(v)

    @field_validator("command_string")
    @classmethod
    def _command_is_a_command(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if "\x00" in v:
            raise ValueError("command_string contains NUL")
        if _FENCE.search(v):
            raise ValueError("command_string contains a markdown fence")
        for line in v.splitlines():
            if _LIST_MARKER.match(line):
                raise ValueError("command_string contains a markdown list")
            if _PROSE_OPENER.match(line):
                raise ValueError(f"command_string reads as conversation: {line.strip()[:60]!r}")
            if _SENTENCE.search(line) and not _looks_like_shell(line):
                raise ValueError(f"command_string contains prose: {line.strip()[:60]!r}")
        return v


_SHELL_HINTS = re.compile(r"(^|\s)(--?[a-z]|\$|\||&&|;|>|<|\./|/|=|\\)")


def _looks_like_shell(line: str) -> bool:
    return bool(_SHELL_HINTS.search(line))


class VerifyTarget(BaseModel):
    """A local HTTP endpoint that must answer after the steps run."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    port: int = Field(ge=1, le=65535)
    path: str = Field(default="/", max_length=512)
    expect_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: int = Field(default=30, ge=1, le=600)

    @field_validator("path")
    @classmethod
    def _path_ok(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("path must start with /")
        return v


class ProvisionRequest(BaseModel):
    """The whole unit of work the DAG processes. Immutable once parsed."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    label: str = Field(min_length=1, max_length=200)
    steps: tuple[ProvisionStep, ...] = Field(min_length=1, max_length=64)
    verify: VerifyTarget | None = None
    isolated_display: bool = False
    risk: RiskLevel = "normal"

    @field_validator("steps", mode="before")
    @classmethod
    def _list_to_tuple(cls, v: Any) -> Any:
        return tuple(v) if isinstance(v, list) else v

    def fingerprint(self, platform: str) -> str:
        """Stable hash of what will run (script payloads and risk included).

        Label is excluded: identical steps hit the same cache entry.
        """
        payload = {
            "platform": platform,
            "steps": [s.model_dump() for s in self.steps],
            "verify": self.verify.model_dump() if self.verify else None,
            "isolated_display": self.isolated_display,
            "risk": self.risk,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_request(raw: Any) -> ProvisionRequest:
    """Parse JSON text or an already-decoded object. Any irregularity is a hard failure."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="strict")
    if isinstance(raw, str):
        text = raw.strip()
        if _FENCE.search(text):
            raise SchemaViolation("request is wrapped in a markdown fence")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise SchemaViolation(f"request is not JSON: {e.msg} at {e.pos}") from None
    if not isinstance(raw, dict):
        raise SchemaViolation("request must be a JSON object")
    try:
        return ProvisionRequest.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise SchemaViolation(problems) from None


def parse_steps(raw: Any, label: str = "steps") -> ProvisionRequest:
    """Parse a bare list of steps (what an LLM parser returns) into a request."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="strict")
    if isinstance(raw, str):
        text = raw.strip()
        if _FENCE.search(text):
            raise SchemaViolation("steps are wrapped in a markdown fence")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise SchemaViolation(f"steps are not JSON: {e.msg} at {e.pos}") from None
    if not isinstance(raw, list):
        raise SchemaViolation("steps must be a JSON array")
    return parse_request({"label": label, "steps": raw})
