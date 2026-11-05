"""Planning OScripts from a goal, replanning one failed step, and best-of-N selection.

``ModelClient`` is the text-completion counterpart of ``parser.Parser``: a local
OpenAI-compatible model first, Claude Code CLI as the one fallback. ``Planner``
turns its text into an ``OScript`` through ``cu.schema.parse_script`` (strict;
garbage raises ``SchemaViolation``). ``BestOfN`` plans N candidates, runs each
through a caller-supplied ``run_fn(script, slot)`` and keeps the highest score.

Reactive loop (P1): ``Planner.plan_milestones`` splits a goal into 1..8
``Milestone`` rows and ``Planner.next_steps`` plans at most ``max_steps`` steps
for one milestone against the *current* tree. ``Planner.candidate_steps`` samples
``k`` single-step scripts for the graph's per-step candidate judging (P9).

Prompt layout (P6): every user prompt is ``STATIC_PREFIX`` (byte-identical across
turns), then the working-memory block at a fixed position, then dynamic context,
so a vLLM prefix cache keeps hitting.

``BestOfN.rank_comparative`` (P9) builds one behaviour narrative per candidate and
asks the model once for a ranking instead of scoring each run.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Callable, Protocol

from openai import OpenAI

from ..schema import SchemaViolation
from ..tokenfilter import TokenFilter
from . import meter
from .treeutil import _get

LOCAL_TIMEOUT_SECONDS = 30.0
CLAUDE_TIMEOUT_SECONDS = 180
MAX_MILESTONES = 8
DEFAULT_STEPS_PER_TURN = 5

_STEP_VOCABULARY = (
    "Allowed steps (each an object with exactly these keys):\n"
    '  {"op":"launch","cmd":"<shell command>"}\n'
    '  {"op":"wait","for":{"role":"<role>","name":"<glob>"},"timeout_s":<int>}\n'
    '  {"op":"find","as":"<ref>","target":{"role":"<role>","name":"<glob>"}}\n'
    '  {"op":"click","ref":"<ref>"}\n'
    '  {"op":"type","ref":"<ref>","text":"<text>"}\n'
    '  {"op":"key","keys":"<e.g. ctrl+s>"}\n'
    '  {"op":"set_value","ref":"<ref>","value":"<value>"}\n'
    '  {"op":"invoke","ref":"<ref>","action":"<action>"}\n'
    '  {"op":"assert","target":{"role":"<role>","name":"<glob>","state":"focused|checked|visible"}}\n'
    '  {"op":"screenshot_assert","question":"<yes/no question about the screen>"}\n'
    '  {"op":"state_assert","checks":[{"kind":"file_exists|file_contains|file_sha_changed|tree|shell_true",'
    '"arg":"<path, target JSON or read-only command>","expect":"<optional expected value>"}]}\n'
    '  {"op":"script","script":{"language":"python|bash|javascript|powershell","code":"<code>",'
    '"target_directory":"<absolute dir>","timeout_seconds":<int>}}\n'
    '  {"op":"call","fn":"<app>.<function>","args":{"<name>":<value>}}\n'
    "Target fields: role, name (glob), text, index, within (ref)."
)

PROMPT_RULES = (
    "Rules:\n"
    "- Prefer op script for file, format and bulk work; use the GUI only for what the app exposes solely in its UI.\n"
    "- Follow every script, save, export or submit step with a state_assert (or a tree assert) that proves its effect.\n"
    "- Prefer accessibility targets taken from the tree summary. Use find before click/type/set_value/invoke.\n"
    "- When an available function fits the work, use op call instead of GUI steps.\n"
    "- Never explain."
)

# Byte-identical head of every user prompt (plan, next_steps, replan). The working-memory block, when
# given, follows it at a fixed position; dynamic context (tree, notes, script, errors) comes after.
STATIC_PREFIX = "## OScript planner\n" + _STEP_VOCABULARY + "\n" + PROMPT_RULES + "\n"

PLAN_SYSTEM_PROMPT = (
    "You write deterministic UI automation scripts (OScript) for a desktop app.\n"
    "Respond with ONLY one JSON object, no prose, no markdown, no code fences:\n"
    '{"app": "<app id>", "goal": "<goal>", "steps": [ ... ]}\n'
    + _STEP_VOCABULARY + "\n"
    "Prefer accessibility targets taken from the tree summary. Use find before click/type/set_value/invoke.\n"
    "Prefer script for file/format/bulk work and GUI for what the app only exposes in its UI; follow every\n"
    "script, save, export or submit step with a state_assert or tree assert. End with an assert,\n"
    "state_assert or screenshot_assert that proves the goal. Never explain."
)

REPLAN_SYSTEM_PROMPT = (
    "You repair one failed step of a UI automation script (OScript).\n"
    "Respond with ONLY one JSON object describing the single replacement step, no prose, no markdown.\n"
    "Use the same step vocabulary as the script (launch, wait, find, click, type, key, set_value, invoke,\n"
    "assert, screenshot_assert, state_assert, script, call). Prefer accessibility targets from the tree\n"
    "summary. When loop evidence is given the step repeated on an unchanged screen: the replacement must\n"
    "not be the repeated step reissued unchanged. Never explain."
)

MILESTONES_SYSTEM_PROMPT = (
    "You split a desktop automation goal into ordered milestones for a reactive executor.\n"
    "Respond with ONLY one JSON object, no prose, no markdown, no code fences:\n"
    '{"milestones": [{"goal": "<sub-goal>", "done_when": <check>}, ...]}\n'
    "<check> is either an accessibility target object {\"role\": \"<role>\", \"name\": \"<glob>\"} that must be\n"
    f"present when the milestone is done, or a yes/no question string for a screenshot judge. 1 to {MAX_MILESTONES}\n"
    "milestones, each independently checkable, in execution order. Never explain."
)

NEXT_STEPS_SYSTEM_PROMPT = (
    "You plan the NEXT few steps of a deterministic UI automation script (OScript) toward one milestone,\n"
    "against the current accessibility tree. Respond with ONLY one JSON object, no prose, no markdown:\n"
    '{"app": "<app id>", "goal": "<milestone goal>", "steps": [ ... ]}\n'
    + _STEP_VOCABULARY + "\n"
    "Plan at most the number of steps the prompt allows; stop early when the milestone check can be\n"
    "asserted. Prefer script for file/format/bulk work and GUI for what the app only exposes in its UI;\n"
    "follow every script, save, export or submit step with a state_assert or tree assert. Never explain."
)

RANK_SYSTEM_PROMPT = (
    "You compare several recorded attempts (behaviour narratives) at the same desktop automation goal.\n"
    "Rank them best first: a passing attempt beats a failing one; among passing attempts prefer the one\n"
    "whose verified checks match the goal and that took fewer steps. Respond with ONLY one JSON object:\n"
    '{"ranking": [<candidate index>, ...]}. Never explain.'
)


def _memory_block(memory: Any) -> str | None:
    """``memory.render()`` (or a str) without trailing whitespace; ``None`` when there is no memory."""
    if memory is None:
        return None
    render = getattr(memory, "render", None)
    text = render() if callable(render) else str(memory)
    return text.rstrip() or None


def _assemble(memory: Any, dynamic: list[str]) -> str:
    """``STATIC_PREFIX`` + memory block (fixed position) + dynamic context, double-newline separated."""
    parts = [STATIC_PREFIX.rstrip()]
    block = _memory_block(memory)
    if block is not None:
        parts.append(block)
    parts.extend(dynamic)
    return "\n\n".join(parts)


def fn_slug(app: str) -> str:
    """The ``<app>`` half of a ``call`` step's ``fn``: lower-case, every char outside ``[a-z0-9_]`` -> ``_``
    (``FN_PATTERN`` allows exactly one dot and no hyphens: ``libreoffice-calc`` -> ``libreoffice_calc``)."""
    slug = re.sub(r"[^a-z0-9_]", "_", (app or "").lower())
    if not slug or not re.match(r"[a-z_]", slug[0]):
        slug = "app_" + slug
    return slug


def render_interfaces(interfaces: Any, app: str = "") -> str:
    """The "Available functions (use op call)" block from ``InterfaceStore.functions(app)`` rows
    (name, description, signature only: progressive disclosure, bodies on request). Function names
    are rendered exactly as ``fn`` must be emitted: ``fn_slug(app)`` + "." + name."""
    slug = fn_slug(app) if app else ""
    rows: list[str] = []
    for fn in interfaces or ():
        name = str(_get(fn, "name") or "").strip()
        if not name:
            continue
        if "." in name:
            prefix, _, bare = name.rpartition(".")
            name = f"{fn_slug(prefix)}.{bare}"
        elif slug:
            name = f"{slug}.{name}"
        sig = str(_get(fn, "signature") or "").strip()
        desc = str(_get(fn, "description") or "").strip()
        rows.append(f"- {name}{sig}" + (f": {desc}" if desc else ""))
    if not rows:
        return ""
    head = "Available functions (use op call):"
    rule = (
        f'fn must be written exactly as listed: "{slug or "<app slug>"}.<function>" where the app slug is the app id'
        " in lower case with every character outside [a-z0-9_] replaced by \"_\" (one dot, no hyphens)."
    )
    return head + "\n" + rule + "\n" + "\n".join(rows)


def render_loop_evidence(rows: Any) -> str:
    lines = [
        "Loop evidence (this step repeated on an unchanged screen; the repeated step must not be reissued unchanged):"
    ]
    for row in rows or ():
        lines.append(json.dumps(row if isinstance(row, dict) else {"row": str(row)}, sort_keys=True, default=str))
    return "\n".join(lines)


def build_plan_prompt(goal: str, app: str, notes: str, tree_summary: str, memory: Any = None, interfaces: Any = ()) -> str:
    parts = [f"App: {app}", f"Goal: {goal}"]
    if notes:
        parts.append("Interface notes for this app:\n" + notes)
    block = render_interfaces(interfaces, app)
    if block:
        parts.append(block)
    if tree_summary:
        parts.append("Current accessibility tree summary:\n" + tree_summary)
    parts.append("OScript JSON object:")
    return _assemble(memory, parts)


def build_replan_prompt(
    script_json: str,
    failed_index: int,
    error: str,
    notes: str,
    tree_summary: str,
    memory: Any = None,
    loop_evidence: list[dict] | None = None,
) -> str:
    parts = [
        "Script:\n" + script_json,
        f"Failed step index: {failed_index}",
        f"Error: {error}",
    ]
    if loop_evidence:
        parts.append(render_loop_evidence(loop_evidence))
    if notes:
        parts.append("Interface notes for this app:\n" + notes)
    if tree_summary:
        parts.append("Current accessibility tree summary:\n" + tree_summary)
    parts.append("Replacement step JSON object:")
    return _assemble(memory, parts)


def build_milestones_prompt(goal: str, app: str, notes: str, memory: Any = None) -> str:
    parts = [f"App: {app}", f"Goal: {goal}"]
    if notes:
        parts.append("Interface notes for this app:\n" + notes)
    parts.append("Milestones JSON object:")
    return _assemble(memory, parts)


def build_next_steps_prompt(
    milestone: Any, tree_summary: str, memory: Any, notes: str, interfaces: Any = (), max_steps: int = DEFAULT_STEPS_PER_TURN, app: str = ""
) -> str:
    parts = []
    if app:
        parts.append(f"App: {app}")
    parts.append(f"Milestone: {_get(milestone, 'goal')}")
    parts.append("Milestone done when: " + _done_when_text(_get(milestone, "done_when")))
    parts.append(f"Plan at most {max_steps} step(s).")
    if notes:
        parts.append("Interface notes for this app:\n" + notes)
    block = render_interfaces(interfaces, app)
    if block:
        parts.append(block)
    if tree_summary:
        parts.append("Current accessibility tree summary:\n" + tree_summary)
    parts.append("OScript JSON object:")
    return _assemble(memory, parts)


def _done_when_text(done_when: Any) -> str:
    if done_when is None:
        return "(unspecified)"
    if isinstance(done_when, str):
        return done_when
    describe = getattr(done_when, "describe", None)
    if callable(describe):
        return "element present: " + describe()
    return "element present: " + json.dumps(done_when, sort_keys=True, default=str)


class ModelClientLike(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class LocalModelClient:
    """Streams a chat completion from a vLLM OpenAI-compatible server; returns the raw text."""

    name = "local"

    def __init__(self, base_url: str, model: str, timeout: float = LOCAL_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.client = OpenAI(base_url=self.base_url, api_key="local", timeout=timeout, max_retries=0)

    def complete(self, system: str, user: str) -> str:
        meter.count_local("planner")
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            stream=True,
            temperature=0,
        )
        deadline = time.monotonic() + self.timeout
        out: list[str] = []
        try:
            for chunk in stream:
                if time.monotonic() > deadline:
                    raise SchemaViolation(f"local model timed out after {self.timeout:g} s")
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    out.append(content)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        return "".join(out)


class ClaudeCodeClient:
    """Runs `claude -p --output-format json` with system+user on stdin; returns the result text."""

    name = "fallback"

    def __init__(self, claude_path: str = "claude", timeout: float = CLAUDE_TIMEOUT_SECONDS) -> None:
        self.claude_path = claude_path
        self.timeout = timeout

    def _command(self) -> list[str]:
        return [self.claude_path, "-p", "--output-format", "json"]

    def complete(self, system: str, user: str) -> str:
        meter.charge_paid("planner")
        try:
            proc = subprocess.run(
                self._command(), input=system + "\n\n" + user, capture_output=True, text=True, timeout=self.timeout
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise SchemaViolation(f"claude cli failed to run: {e}") from None
        if proc.returncode != 0:
            raise SchemaViolation(f"claude cli exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise SchemaViolation(f"claude cli output is not JSON: {e.msg}") from None
        meter.add_cost(payload)
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, str):
            raise SchemaViolation("claude cli output has no string result")
        return result


class ModelClient:
    """Local first; on any failure the fallback once; otherwise SchemaViolation."""

    def __init__(self, local: ModelClientLike | None, fallback: ModelClientLike | None = None) -> None:
        self.local = local
        self.fallback = fallback
        self._answered = "none"

    def describe(self) -> str:
        return self._answered

    def complete(self, system: str, user: str) -> str:
        self._answered = "none"
        errors: list[str] = []
        for backend in (self.local, self.fallback):
            if backend is None:
                continue
            try:
                text = backend.complete(system, user)
            except meter.ModelCallCapReached:
                raise
            except Exception as e:
                errors.append(f"{getattr(backend, 'name', type(backend).__name__)}: {e}")
                continue
            self._answered = getattr(backend, "name", type(backend).__name__)
            return text
        if not errors:
            errors.append("no model backend configured")
        raise SchemaViolation("all model backends failed (" + "; ".join(errors) + ")")

    def complete_json(self, system: str, user: str) -> str:
        """``complete`` with only the first balanced JSON value kept."""
        text = self.complete(system, user)
        filtered = TokenFilter().feed(text)
        if not filtered:
            raise SchemaViolation("model produced no JSON value")
        return filtered

    def local_only(self) -> "ModelClient | None":
        """A view of this client that answers from the local backend only (no fallback): what latency-bound
        callers such as ``cu.memory.extract`` use. ``None`` when no local backend is configured."""
        if self.local is None:
            return None
        return ModelClient(self.local, None)


def extract_json(text: str) -> str:
    filtered = TokenFilter().feed(text or "")
    if not filtered:
        raise SchemaViolation("model produced no JSON value")
    return filtered


def _resolve_parse_script(parse_script: Callable[[Any], Any] | None) -> Callable[[Any], Any]:
    if parse_script is not None:
        return parse_script
    try:
        from .schema import parse_script as real  # type: ignore
    except ImportError:
        raise SchemaViolation("o_engine.cu.schema.parse_script is not available") from None
    return real


def script_to_dict(script: Any) -> dict:
    if isinstance(script, dict):
        return dict(script)
    dump = getattr(script, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True, exclude_none=True)
    raise TypeError(f"cannot serialize script of type {type(script).__name__}")


def script_to_json(script: Any) -> str:
    return json.dumps(script_to_dict(script), sort_keys=True, separators=(",", ":"))


def step_to_dict(step: Any) -> dict:
    if isinstance(step, dict):
        return dict(step)
    dump = getattr(step, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True, exclude_none=True)
    raise TypeError(f"cannot serialize step of type {type(step).__name__}")


class Planner:
    """Goal + app + notes + tree summary -> OScript; one failed step -> one replacement step."""

    def __init__(self, parser_like: ModelClientLike, notes: Any = None, parse_script: Callable[[Any], Any] | None = None) -> None:
        self.client = parser_like
        self.notes = notes
        self._parse_script = parse_script
        self.calls = 0

    def _notes_for(self, app: str) -> str:
        if self.notes is None:
            return ""
        fn = getattr(self.notes, "for_prompt", None)
        if callable(fn):
            try:
                return fn(app) or ""
            except Exception:
                return ""
        return str(self.notes)

    def parse_script(self, raw: Any) -> Any:
        return _resolve_parse_script(self._parse_script)(raw)

    def complete_json(self, system: str, user: str) -> str:
        """One model call counted in ``calls``; only the first balanced JSON value is kept."""
        self.calls += 1
        fn = getattr(self.client, "complete_json", None)
        if callable(fn):
            return fn(system, user)
        return extract_json(self.client.complete(system, user))

    def plan(self, goal: str, app: str, tree_summary: str = "", memory: Any = None, interfaces: Any = ()) -> Any:
        text = self.complete_json(
            PLAN_SYSTEM_PROMPT, build_plan_prompt(goal, app, self._notes_for(app), tree_summary, memory, interfaces)
        )
        return self.parse_script(text)

    # ------------------------------------------------------------------ P1 reactive loop

    def plan_milestones(self, goal: str, app: str, notes: str | None = None, memory: Any = None) -> tuple[Any, ...]:
        """1..8 ``Milestone`` rows from one model call. Any model or parse failure degrades to a single
        milestone (the whole goal, checked by ``goal_question``) so the graph behaves as plan-once."""
        from .schema import Milestone

        fallback = (Milestone(goal=goal, done_when=goal_question(goal)),)
        notes_text = self._notes_for(app) if notes is None else notes
        try:
            raw = json.loads(self.complete_json(MILESTONES_SYSTEM_PROMPT, build_milestones_prompt(goal, app, notes_text, memory)))
        except meter.ModelCallCapReached:
            raise
        except Exception:
            return fallback
        rows = raw.get("milestones") if isinstance(raw, dict) else raw
        if not isinstance(rows, list) or not rows:
            return fallback
        out: list[Any] = []
        for row in rows[:MAX_MILESTONES]:
            if not isinstance(row, dict):
                return fallback
            row = dict(row)
            done = row.get("done_when")
            if isinstance(done, dict) and "state" in done:
                done = {k: v for k, v in done.items() if k != "state"}
                row["done_when"] = done
            try:
                out.append(Milestone.model_validate(row))
            except Exception:
                return fallback
        return tuple(out)

    def next_steps(
        self,
        milestone: Any,
        tree_summary: str,
        memory: Any = None,
        notes: str | None = None,
        interfaces: Any = (),
        max_steps: int = DEFAULT_STEPS_PER_TURN,
        app: str = "",
    ) -> Any:
        """One model call -> an OScript of at most ``max_steps`` steps toward ``milestone`` (extra steps
        are dropped before validation; unknown ops raise ``SchemaViolation`` through ``parse_script``).
        ``interfaces`` is ``InterfaceStore.functions(app)`` (names/descriptions/signatures only)."""
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        notes_text = self._notes_for(app) if notes is None else notes
        text = self.complete_json(
            NEXT_STEPS_SYSTEM_PROMPT,
            build_next_steps_prompt(milestone, tree_summary, memory, notes_text, interfaces, max_steps, app),
        )
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise SchemaViolation(f"next steps are not JSON: {e.msg}") from None
        if not isinstance(obj, dict):
            raise SchemaViolation("next steps must be a JSON object")
        steps = obj.get("steps")
        if isinstance(steps, list) and len(steps) > max_steps:
            obj = {**obj, "steps": steps[:max_steps]}
        if app and not obj.get("app"):
            obj = {**obj, "app": app}
        if not obj.get("goal"):
            obj = {**obj, "goal": str(_get(milestone, "goal") or "")}
        return self.parse_script(json.dumps(obj))

    def candidate_steps(
        self,
        milestone: Any,
        tree_summary: str,
        memory: Any = None,
        notes: str | None = None,
        k: int = 1,
        interfaces: Any = (),
        app: str = "",
    ) -> list[Any]:
        """``k`` single-step scripts (``k`` model calls) for per-step candidate judging. A candidate whose
        reply fails validation is skipped; when none validates ``SchemaViolation`` is raised."""
        if k < 1:
            raise ValueError("k must be >= 1")
        out: list[Any] = []
        errors: list[str] = []
        for _ in range(k):
            try:
                out.append(self.next_steps(milestone, tree_summary, memory, notes, interfaces, 1, app))
            except SchemaViolation as e:
                errors.append(str(e))
        if not out:
            raise SchemaViolation("no step candidate validated (" + "; ".join(errors) + ")")
        return out

    def replan_step(
        self,
        script: Any,
        failed_index: int,
        error: str,
        tree_summary: str = "",
        memory: Any = None,
        loop_evidence: list[dict] | None = None,
    ) -> Any:
        self.calls += 1
        sdict = script_to_dict(script)
        steps = list(sdict.get("steps") or [])
        if not 0 <= failed_index < len(steps):
            raise SchemaViolation(f"failed_index {failed_index} out of range for {len(steps)} steps")
        app = str(sdict.get("app", ""))
        text = self.client.complete(
            REPLAN_SYSTEM_PROMPT,
            build_replan_prompt(
                json.dumps(sdict, sort_keys=True), failed_index, error, self._notes_for(app), tree_summary, memory, loop_evidence
            ),
        )
        try:
            obj = json.loads(extract_json(text))
        except json.JSONDecodeError as e:
            raise SchemaViolation(f"replacement step is not JSON: {e.msg}") from None
        if not isinstance(obj, dict):
            raise SchemaViolation("replacement step must be a JSON object")
        healed = self.replace_step(script, failed_index, obj)
        return list(getattr(healed, "steps", None) or healed["steps"])[failed_index]

    def replace_step(self, script: Any, index: int, step: Any) -> Any:
        """A new, revalidated script with ``steps[index]`` replaced."""
        sdict = script_to_dict(script)
        steps = [step_to_dict(s) for s in (sdict.get("steps") or [])]
        if not 0 <= index < len(steps):
            raise SchemaViolation(f"step index {index} out of range for {len(steps)} steps")
        steps[index] = step_to_dict(step)
        sdict["steps"] = steps
        return self.parse_script(json.dumps(sdict))


def goal_question(goal: str) -> str:
    return f"Has this goal been fully achieved on screen: {goal}?"


def _step_pass_ratio(result: Any, script: Any) -> float:
    n = len(getattr(script, "steps", None) or script_to_dict(script).get("steps") or [])
    if n == 0:
        return 0.0
    steps = getattr(result, "steps", None) or []
    passed = 0
    for s in steps:
        ok = s.get("ok") if isinstance(s, dict) else getattr(s, "ok", None)
        if ok:
            passed += 1
    if not steps:
        if getattr(result, "ok", False):
            return 1.0
        fi = getattr(result, "failed_index", None)
        return (fi / n) if isinstance(fi, int) and fi >= 0 else 0.0
    return min(1.0, passed / n)


def _final_screenshot(result: Any) -> bytes | None:
    for attr in ("screenshot", "final_screenshot", "png"):
        v = getattr(result, attr, None)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
    steps = getattr(result, "steps", None) or []
    for s in reversed(list(steps)):
        for attr in ("after_png", "screenshot", "png"):
            v = s.get(attr) if isinstance(s, dict) else getattr(s, attr, None)
            if isinstance(v, (bytes, bytearray)):
                return bytes(v)
    return None


def _candidate_records(candidate: Any) -> list[dict]:
    """Normalised step rows of one best-of-N candidate: ``{index, step, resolved_by, status, ok, screen, error}``.
    Accepts ``run_records`` (graph ``step_record`` rows) or the raw ``result.steps`` (executor rows)."""
    rows = _get(candidate, "run_records")
    if rows is None:
        rows = _get(_get(candidate, "result"), "steps") or []
    script_steps = list(_get(_get(candidate, "script"), "steps", None) or [])
    out: list[dict] = []
    for i, row in enumerate(list(rows)):
        idx = _get(row, "index")
        idx = int(idx) if isinstance(idx, int) else i
        text = _get(row, "command") or _get(row, "step") or ""
        if not text:
            if idx < len(script_steps):
                text = json.dumps(step_to_dict(script_steps[idx]), sort_keys=True, default=str)
            else:
                text = str(_get(row, "op") or "")
        ok = _get(row, "ok")
        if ok is None:
            exit_code = _get(row, "exit_code")
            ok = (exit_code == 0) if isinstance(exit_code, int) else bool(_get(row, "passed", True))
        status = _get(row, "status") or ("ok" if ok else "failed")
        screen = _get(row, "screen_sha") or _get(row, "screenshot_sha") or _get(row, "after_digest") or ""
        error = _get(row, "error") or _get(row, "stderr_tail") or _get(row, "reason") or ""
        note = _get(row, "note") or _get(row, "stdout_tail") or ""
        resolved_by = _get(row, "resolved_by") or ""
        if not resolved_by and isinstance(note, str) and note.startswith("resolved_by="):
            resolved_by = note[len("resolved_by="):]
        out.append({
            "index": idx, "step": str(text)[:300], "resolved_by": str(resolved_by), "status": str(status),
            "ok": bool(ok), "screen": str(screen)[:12], "error": str(error)[:200],
        })
    return out


def _candidate_ok(candidate: Any) -> bool | None:
    """The candidate's goal verdict when one is known: ``ok``/``goal_ok``, a ``judge`` row, else ``result.ok``."""
    for key in ("ok", "goal_ok"):
        v = _get(candidate, key)
        if isinstance(v, bool):
            return v
    judge = _get(candidate, "judge")
    if judge is not None:
        v = _get(judge, "ok")
        if isinstance(v, bool):
            return v
    result = _get(candidate, "result")
    if result is not None:
        v = _get(result, "ok")
        if isinstance(v, bool):
            return v
    return None


def behaviour_narrative(candidate: Any, index: int) -> str:
    """One readable trace of a candidate run: outcome, judge reason, then each step with how its target
    was resolved, its status and the screen hash after it."""
    records = _candidate_records(candidate)
    verdict = _candidate_ok(candidate)
    error = _get(candidate, "error")
    outcome = "error" if error else ("pass" if verdict else "fail" if verdict is False else "unknown")
    lines = [f"Candidate {index}: {len(records)} step(s), outcome={outcome}"]
    if error:
        lines.append(f"  error: {str(error)[:200]}")
    judge = _get(candidate, "judge")
    reason = _get(judge, "reason") if judge is not None else None
    if reason:
        lines.append(f"  judge: {str(reason)[:200]}")
    for r in records:
        line = f"  {r['index']}. {r['step']} | resolved_by={r['resolved_by'] or '-'} | status={r['status']}"
        if r["screen"]:
            line += f" | screen={r['screen']}"
        if r["error"]:
            line += f" | error={r['error']}"
        lines.append(line)
    return "\n".join(lines)


def _fallback_winner(candidates: list[Any]) -> int:
    """Fewest steps among passing candidates (ties -> lowest index); no passing candidate -> 0."""
    best = -1
    best_n = 0
    for i, c in enumerate(candidates):
        if _get(c, "error") or _candidate_ok(c) is not True:
            continue
        n = len(_candidate_records(c))
        if best < 0 or n < best_n:
            best, best_n = i, n
    return best if best >= 0 else 0


class BestOfN:
    """Plan n scripts, run each in its own slot, score, keep the best.

    ``run_fn(script, slot) -> RunResult``. ``judge`` is either an object with
    ``judge(png_bytes, question) -> JudgeVerdict`` (applied to the result's final
    screenshot, when one is present) or a callable ``judge(script, result, goal) ->
    JudgeVerdict | bool``. Score = goal judge ok (1.0) + step pass ratio (0..1)
    - 0.01 * number of steps. With n == 1 the single run is returned unjudged.

    ``comparative=True`` (P9) replaces per-candidate scoring: after every slot ran,
    ``rank_comparative`` builds one behaviour narrative per candidate and asks the
    model once for a ranking; the per-candidate judge is not called. The graph can
    also call ``rank_comparative`` directly on its own candidate rows.
    """

    def __init__(
        self, n: int, planner: Planner, run_fn: Callable[[Any, int], Any], judge: Any = None, comparative: bool = False
    ) -> None:
        if n < 1:
            raise ValueError("n must be >= 1")
        self.n = n
        self.planner = planner
        self.run_fn = run_fn
        self.judge = judge
        self.comparative = comparative
        self.rank_calls = 0

    def _goal_ok(self, script: Any, result: Any, goal: str) -> float:
        if self.judge is None:
            return 1.0 if getattr(result, "ok", False) else 0.0
        verdict: Any
        judge_fn = getattr(self.judge, "judge", None)
        if callable(judge_fn):
            png = _final_screenshot(result)
            if png is None:
                return 1.0 if getattr(result, "ok", False) else 0.0
            verdict = judge_fn(png, goal_question(goal))
        else:
            verdict = self.judge(script, result, goal)
        ok = verdict if isinstance(verdict, bool) else bool(getattr(verdict, "ok", False))
        return 1.0 if ok else 0.0

    def score(self, script: Any, result: Any, goal: str) -> float:
        n_steps = len(getattr(script, "steps", None) or script_to_dict(script).get("steps") or [])
        return self._goal_ok(script, result, goal) + _step_pass_ratio(result, script) - 0.01 * n_steps

    def rank_comparative(self, candidates: list[Any], goal: str = "") -> int:
        """Index of the best candidate after ONE model call over all behaviour narratives.

        Each candidate is a dict (or object) with any of ``run_records`` (graph ``step_record`` rows),
        ``result`` (executor ``RunResult``), ``script``, ``ok``/``goal_ok``/``judge`` ({ok, reason}) and
        ``error``. A reply that is not ``{"ranking":[idx,...]}`` with a usable first index, or a model
        failure, falls back to the fewest-step passing candidate (else index 0). Errored candidates
        never win.
        """
        if not candidates:
            raise ValueError("no candidates to rank")
        if len(candidates) == 1:
            return 0
        narratives = [behaviour_narrative(c, i) for i, c in enumerate(candidates)]
        user = "\n\n".join(([f"Goal: {goal}"] if goal else []) + narratives + ["Ranking JSON object:"])
        self.rank_calls += 1
        try:
            obj = json.loads(self.planner.complete_json(RANK_SYSTEM_PROMPT, user))
        except meter.ModelCallCapReached:
            raise
        except Exception:
            return _fallback_winner(candidates)
        ranking = obj.get("ranking") if isinstance(obj, dict) else None
        if not isinstance(ranking, list):
            return _fallback_winner(candidates)
        for idx in ranking:
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            if 0 <= idx < len(candidates) and not _get(candidates[idx], "error"):
                return idx
        return _fallback_winner(candidates)

    def run(self, goal: str, app: str, tree_summaries: Any = "") -> dict:
        if isinstance(tree_summaries, str):
            summaries = [tree_summaries] * self.n
        else:
            summaries = list(tree_summaries)
            if not summaries:
                summaries = [""]
            summaries = [summaries[i % len(summaries)] for i in range(self.n)]
        comparative = self.comparative and self.n > 1
        candidates: list[dict] = []
        scores: list[float] = []
        best_i = -1
        for slot in range(self.n):
            entry: dict = {"slot": slot, "script": None, "result": None, "error": None}
            try:
                script = self.planner.plan(goal, app, summaries[slot])
                entry["script"] = script
                result = self.run_fn(script, slot)
                entry["result"] = result
                if comparative:
                    s = 0.0
                elif self.n == 1:
                    s = _step_pass_ratio(result, script) - 0.01 * len(script_to_dict(script).get("steps") or [])
                    s += 1.0 if getattr(result, "ok", False) else 0.0
                else:
                    s = self.score(script, result, goal)
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
                s = float("-inf")
            candidates.append(entry)
            scores.append(s)
            if best_i < 0 or s > scores[best_i]:
                best_i = slot
        if comparative and any(c["result"] is not None for c in candidates):
            best_i = self.rank_comparative(candidates, goal)
            if candidates[best_i]["result"] is None:
                best_i = next(i for i, c in enumerate(candidates) if c["result"] is not None)
            scores = [float("-inf") if c["result"] is None else (1.0 if i == best_i else 0.0) for i, c in enumerate(candidates)]
        if best_i < 0 or candidates[best_i]["result"] is None:
            errors = "; ".join(c["error"] or "no result" for c in candidates)
            raise SchemaViolation(f"no candidate produced a run ({errors})")
        return {
            "winner": candidates[best_i]["result"],
            "winner_index": best_i,
            "script": candidates[best_i]["script"],
            "candidates": candidates,
            "scores": scores,
        }
