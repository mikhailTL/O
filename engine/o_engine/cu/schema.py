"""OScript: the strict, immutable contract for a computer-use run.

A script is a JSON object ``{app, goal, steps}``. Every step is discriminated by
``op`` and carries exactly the fields its op needs; anything else is a
``SchemaViolation``. Element references (``as``/``ref``/``within``) are checked at
parse time so a script can never name an element it has not found first.
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..schema import _FENCE, _LIST_MARKER, _PROSE_OPENER, SchemaViolation, ScriptPayload

MAX_TEXT = 4000
MAX_STEPS = 200
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 300
NAME_PATTERN = r"^[a-z_][a-z0-9_]*$"
FN_PATTERN = r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$"
MAX_CHECKS = 50
MAX_CALL_ARGS = 50

CHECK_KINDS = ("file_exists", "file_contains", "file_sha_changed", "tree", "shell_true")
CheckKind = Literal["file_exists", "file_contains", "file_sha_changed", "tree", "shell_true"]

STATES = ("focused", "checked", "visible", "enabled", "selected", "expanded", "active", "showing", "editable")
State = Literal["focused", "checked", "visible", "enabled", "selected", "expanded", "active", "showing", "editable"]

_STRICT = ConfigDict(frozen=True, extra="forbid", strict=True, populate_by_name=True)


class Target(BaseModel):
    """How an element is located. ``name`` is a case-insensitive glob; ``text`` a substring."""

    model_config = _STRICT

    role: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)
    text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)
    index: int = Field(default=0, ge=0, le=10_000)
    within: str | None = Field(default=None, pattern=NAME_PATTERN, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def _blank_is_absent(cls, data: Any) -> Any:
        # Planners copy unnamed tree nodes (`text ''`) as "name": ""; an empty selector means "any", i.e. absent.
        # Real run: three of four jobs aborted on "name: String should have at least 1 character".
        if isinstance(data, dict) and any(data.get(k) == "" for k in ("role", "name", "text")):
            data = {k: v for k, v in data.items() if not (k in ("role", "name", "text") and v == "")}
        return data

    @model_validator(mode="after")
    def _needs_a_selector(self) -> "Target":
        if self.role is None and self.name is None and self.text is None:
            raise ValueError("target needs at least one of role, name, text")
        return self

    def describe(self) -> str:
        parts = []
        if self.role:
            parts.append(f"role={self.role!r}")
        if self.name:
            parts.append(f"name={self.name!r}")
        if self.text:
            parts.append(f"text={self.text!r}")
        if self.index:
            parts.append(f"index={self.index}")
        if self.within:
            parts.append(f"within={self.within}")
        return " ".join(parts)


class LaunchStep(BaseModel):
    model_config = _STRICT
    op: Literal["launch"]
    cmd: str = Field(min_length=1, max_length=MAX_TEXT)

    @field_validator("cmd")
    @classmethod
    def _cmd_plain(cls, v: str) -> str:
        if "\x00" in v or "\n" in v or _FENCE.search(v):
            raise ValueError("cmd must be a single plain command line")
        return v


class WaitStep(BaseModel):
    model_config = _STRICT
    op: Literal["wait"]
    for_: Target = Field(alias="for")
    timeout_s: int = Field(default=10, ge=MIN_TIMEOUT_S, le=MAX_TIMEOUT_S)


class FindStep(BaseModel):
    model_config = _STRICT
    op: Literal["find"]
    as_: str = Field(alias="as", pattern=NAME_PATTERN, max_length=64)
    target: Target


class ClickStep(BaseModel):
    model_config = _STRICT
    op: Literal["click"]
    ref: str = Field(pattern=NAME_PATTERN, max_length=64)


class TypeStep(BaseModel):
    model_config = _STRICT
    op: Literal["type"]
    ref: str | None = Field(default=None, pattern=NAME_PATTERN, max_length=64)
    text: str = Field(max_length=MAX_TEXT)


class KeyStep(BaseModel):
    model_config = _STRICT
    op: Literal["key"]
    keys: str = Field(min_length=1, max_length=200)


class SetValueStep(BaseModel):
    model_config = _STRICT
    op: Literal["set_value"]
    ref: str = Field(pattern=NAME_PATTERN, max_length=64)
    value: str = Field(max_length=MAX_TEXT)


class InvokeStep(BaseModel):
    model_config = _STRICT
    op: Literal["invoke"]
    ref: str = Field(pattern=NAME_PATTERN, max_length=64)
    action: str = Field(min_length=1, max_length=100)


class AssertStep(BaseModel):
    """``{"op":"assert","target":{...,"state":"focused"}}``: state may sit inside the target or beside it."""

    model_config = _STRICT
    op: Literal["assert"]
    target: Target
    state: State

    @model_validator(mode="before")
    @classmethod
    def _lift_state(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("target"), dict) and "state" in data["target"]:
            if "state" in data:
                raise ValueError("state given both on the step and inside target")
            data = dict(data)
            target = dict(data["target"])
            data["state"] = target.pop("state")
            data["target"] = target
        return data


class ScreenshotAssertStep(BaseModel):
    model_config = _STRICT
    op: Literal["screenshot_assert"]
    question: str = Field(min_length=1, max_length=MAX_TEXT)


class StateCheck(BaseModel):
    """One probe of the real system state, checked by ``StateJudge`` with no model call.

    ``arg`` is a sandbox-relative path (``file_exists``, ``file_contains``, ``file_sha_changed``),
    a JSON ``Target`` object or a name glob (``tree``), or a single read-only shell command line
    (``shell_true``). ``expect`` is the required substring (``file_contains``), a state name
    (``tree``), ``"false"`` to require absence (``file_exists``), the exact new digest
    (``file_sha_changed``) or the exact stdout (``shell_true``).
    """

    model_config = _STRICT
    kind: CheckKind
    arg: str = Field(min_length=1, max_length=MAX_TEXT)
    expect: str | None = Field(default=None, max_length=MAX_TEXT)

    @model_validator(mode="after")
    def _arg_fits_kind(self) -> "StateCheck":
        if "\x00" in self.arg or (self.expect is not None and "\x00" in self.expect):
            raise ValueError("check contains NUL")
        if self.kind == "file_contains" and not self.expect:
            raise ValueError("file_contains needs expect")
        if self.kind != "tree" and any(ch in self.arg for ch in "\n\r"):
            what = "command line" if self.kind == "shell_true" else "path"
            raise ValueError(f"{self.kind} arg must be a single {what}")
        return self


class StateAssertStep(BaseModel):
    """``{"op":"state_assert","checks":[{"kind":..,"arg":..,"expect":..}, ...]}`` — all checks must hold."""

    model_config = _STRICT
    op: Literal["state_assert"]
    checks: tuple[StateCheck, ...] = Field(min_length=1, max_length=MAX_CHECKS)

    @field_validator("checks", mode="before")
    @classmethod
    def _list_to_tuple(cls, v: Any) -> Any:
        return tuple(v) if isinstance(v, list) else v


class ScriptStep(BaseModel):
    """``{"op":"script","script": ScriptPayload}`` — code-first action; the G1 payload is reused as-is."""

    model_config = _STRICT
    op: Literal["script"]
    script: ScriptPayload


CallArg = Union[str, int, float, bool]


class CallStep(BaseModel):
    """``{"op":"call","fn":"<app>.<function>","args":{...}}`` — an evolved per-app interface function."""

    model_config = _STRICT
    op: Literal["call"]
    fn: str = Field(pattern=FN_PATTERN, max_length=129)
    args: dict[str, CallArg] = Field(default_factory=dict)

    @field_validator("args")
    @classmethod
    def _args_bounded(cls, v: dict[str, CallArg]) -> dict[str, CallArg]:
        if len(v) > MAX_CALL_ARGS:
            raise ValueError(f"call has more than {MAX_CALL_ARGS} args")
        for k, val in v.items():
            if not re.match(NAME_PATTERN, k):
                raise ValueError(f"arg name {k!r} is not an identifier")
            if isinstance(val, str) and ("\x00" in val or len(val) > MAX_TEXT):
                raise ValueError(f"arg {k!r} is not a plain bounded string")
        return v


Step = Annotated[
    Union[
        LaunchStep,
        WaitStep,
        FindStep,
        ClickStep,
        TypeStep,
        KeyStep,
        SetValueStep,
        InvokeStep,
        AssertStep,
        ScreenshotAssertStep,
        StateAssertStep,
        ScriptStep,
        CallStep,
    ],
    Field(discriminator="op"),
]

OPS = (
    "launch", "wait", "find", "click", "type", "key", "set_value", "invoke", "assert", "screenshot_assert",
    "state_assert", "script", "call",
)


class OScript(BaseModel):
    """One computer-use procedure: an app, a goal, and 1..200 strictly typed steps."""

    model_config = _STRICT

    app: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=MAX_TEXT)
    steps: tuple[Step, ...] = Field(min_length=1, max_length=MAX_STEPS)

    @field_validator("steps", mode="before")
    @classmethod
    def _list_to_tuple(cls, v: Any) -> Any:
        return tuple(v) if isinstance(v, list) else v

    @field_validator("app")
    @classmethod
    def _app_plain(cls, v: str) -> str:
        if any(ch in v for ch in "\n\r\x00") or _FENCE.search(v):
            raise ValueError("app must be a plain identifier")
        return v

    @field_validator("goal")
    @classmethod
    def _goal_is_a_goal(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("goal contains NUL")
        if _FENCE.search(v):
            raise ValueError("goal contains a markdown fence")
        for line in v.splitlines():
            if _LIST_MARKER.match(line):
                raise ValueError("goal contains a markdown list")
            if _PROSE_OPENER.match(line):
                raise ValueError(f"goal reads as conversation: {line.strip()[:60]!r}")
        return v

    @model_validator(mode="after")
    def _refs_resolve(self) -> "OScript":
        bound: set[str] = set()
        for i, step in enumerate(self.steps):
            targets: list[Target] = []
            if isinstance(step, WaitStep):
                targets.append(step.for_)
            elif isinstance(step, (FindStep, AssertStep)):
                targets.append(step.target)
            for t in targets:
                if t.within is not None and t.within not in bound:
                    raise ValueError(f"step {i}: within={t.within!r} is not a ref found by an earlier step")
            ref = getattr(step, "ref", None)
            if ref is not None and ref not in bound:
                raise ValueError(f"step {i}: ref={ref!r} is not a ref found by an earlier step")
            if isinstance(step, FindStep):
                if step.as_ in bound:
                    raise ValueError(f"step {i}: as={step.as_!r} is already bound")
                bound.add(step.as_)
        return self

    def to_json(self) -> str:
        return json.dumps(self.model_dump(by_alias=True, exclude_none=True), sort_keys=True, separators=(",", ":"))


class Milestone(BaseModel):
    """One sub-goal of a reactive plan (P1). ``done_when`` is a ``Target`` that must be present in the
    accessibility tree when the milestone is done, state checks (``{"checks": [...]}`` or a list of
    ``StateCheck``; e.g. ``file_exists`` on a saved file) that ``StateJudge`` verifies with no model call,
    or a yes/no question for the screenshot judge."""

    model_config = _STRICT

    goal: str = Field(min_length=1, max_length=MAX_TEXT)
    done_when: tuple[StateCheck, ...] | Target | Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]

    @field_validator("done_when", mode="before")
    @classmethod
    def _checks_form(cls, v: Any) -> Any:
        if isinstance(v, dict) and "checks" in v:
            if set(v) != {"checks"}:
                raise ValueError("a checks done_when holds only 'checks'")
            v = v["checks"]
        if isinstance(v, list):
            if not v or len(v) > MAX_CHECKS:
                raise ValueError(f"done_when checks: 1 to {MAX_CHECKS}")
            return tuple(StateCheck.model_validate(c) if isinstance(c, dict) else c for c in v)
        return v

    @property
    def checks(self) -> tuple[StateCheck, ...] | None:
        return self.done_when if isinstance(self.done_when, tuple) else None


def parse_script(raw: Any) -> OScript:
    """Parse JSON text, bytes, or a decoded object into an OScript. Any irregularity is a hard failure."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as e:
            raise SchemaViolation(f"script is not UTF-8: {e.reason}") from None
    if isinstance(raw, str):
        text = raw.strip()
        if _FENCE.search(text):
            raise SchemaViolation("script is wrapped in a markdown fence")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise SchemaViolation(f"script is not JSON: {e.msg} at {e.pos}") from None
    if isinstance(raw, OScript):
        return raw
    if not isinstance(raw, dict):
        raise SchemaViolation("script must be a JSON object")
    try:
        return OScript.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise SchemaViolation(problems) from None
