"""Step judging: accessibility assertions first, state probes second, screenshot judge last.

``a11y_assert`` checks a tree node against a target's role/name/state without any
model call. ``StateJudge`` verifies a ``state_assert`` step's checks against the real
system (files in the job sandbox, the accessibility tree, a read-only shell probe) and
also makes no model call. ``VLMJudge`` / ``ClaudeCodeJudge`` answer a yes/no question
about a screenshot with ``{"ok": bool, "reason": str}``; ``Judge`` orders them primary ->
fallback once and never raises: an unavailable judge is a failed verdict.

Intended order in ``cu/graph.py:judge`` (wave-2 wiring; cheap and exact before costly
and fuzzy, after IRA / VAGEN):

1. a11y assert — ``assert``/``wait`` targets against the tree (``a11y_assert``).
2. state checks — ``state_assert`` steps through ``StateJudge.verify`` with the
   ``baseline`` captured by ``StateJudge.snapshot`` before the step ran.
3. VLM screenshot question — ``Judge.judge`` only when neither 1 nor 2 can decide,
   i.e. the step carries no ``assert``/``state_assert`` and declares a
   ``screenshot_assert``/``expect``. No VLM call is made when 1 or 2 decided.

Shell probes (``shell_true``) run only through ``Sandbox.run`` and only when the
command starts with one of ``PROBE_ALLOW_LIST`` (from ``cu/probes.py``, shared with the
feasibility gate) and contains no shell operators; any other command is refused with
reason ``"probe not allowed"`` and is never executed.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, ValidationError

from ..schema import ProvisionStep, SchemaViolation
from ..tokenfilter import TokenFilter
from . import meter
from .ground import LOCAL_TIMEOUT_SECONDS, CLAUDE_TIMEOUT_SECONDS, png_data_url
from .probes import PROBE_ALLOW_LIST, is_allowed_probe  # noqa: F401  (re-exported: the one shared allow-list)
from .schema import StateCheck, Target
from .treeutil import _get

JUDGE_SYSTEM_PROMPT = (
    "You verify the state of a computer screen from a screenshot.\n"
    "Answer the question about the screenshot with ONLY a JSON object:\n"
    '{"ok": true|false, "reason": "<one short sentence>"}\n'
    "ok is true only when the screenshot clearly shows the condition holds.\n"
    "No prose, no markdown, no code fences, no other keys."
)


def build_judge_prompt(question: str) -> str:
    return f"Question: {question}\nJSON verdict:"


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    reason: str = ""
    source: str = ""


class _RawVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    ok: bool
    reason: str = ""


def parse_verdict(text: str, source: str) -> JudgeVerdict:
    tf = TokenFilter()
    filtered = tf.feed(text or "")
    if not filtered:
        raise SchemaViolation("judge produced no JSON object")
    try:
        obj = json.loads(filtered)
    except json.JSONDecodeError as e:
        raise SchemaViolation(f"judge output is not JSON: {e.msg}") from None
    if not isinstance(obj, dict):
        raise SchemaViolation("judge output must be a JSON object")
    try:
        raw = _RawVerdict.model_validate(obj)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise SchemaViolation(problems) from None
    return JudgeVerdict(ok=raw.ok, reason=raw.reason, source=source)


class JudgeBackend(Protocol):
    name: str

    def judge(self, png_bytes: bytes, question: str) -> JudgeVerdict: ...


class VLMJudge:
    name = "vlm"

    def __init__(self, vllm_url: str, model: str, timeout: float = LOCAL_TIMEOUT_SECONDS) -> None:
        self.base_url = vllm_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.client = OpenAI(base_url=self.base_url, api_key="local", timeout=timeout, max_retries=0)

    def _tokens(self, png_bytes: bytes, question: str):
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": png_data_url(png_bytes)}},
                        {"type": "text", "text": build_judge_prompt(question)},
                    ],
                },
            ],
            stream=True,
            temperature=0,
        )
        deadline = time.monotonic() + self.timeout
        try:
            for chunk in stream:
                if time.monotonic() > deadline:
                    raise SchemaViolation(f"judge model timed out after {self.timeout:g} s")
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    yield content
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def judge(self, png_bytes: bytes, question: str) -> JudgeVerdict:
        meter.count_local("judge")
        tf = TokenFilter()
        filtered = tf.filter_stream(self._tokens(png_bytes, question))
        if not filtered:
            raise SchemaViolation("judge model produced no JSON object")
        return parse_verdict(filtered, self.name)


class ClaudeCodeJudge:
    name = "claude"

    def __init__(self, claude_path: str = "claude", timeout: float = CLAUDE_TIMEOUT_SECONDS) -> None:
        self.claude_path = claude_path
        self.timeout = timeout

    def _command(self) -> list[str]:
        return [self.claude_path, "-p", "--output-format", "json"]

    def judge(self, png_bytes: bytes, question: str) -> JudgeVerdict:
        # The PNG goes in a fresh directory that is also the CLI's working directory: `claude -p` reads files in
        # its working directory without a permission prompt, but a Read of a /tmp file outside it is denied in
        # headless mode (real run: every screenshot check failed with "permission denied").
        meter.charge_paid("judge")
        workdir = tempfile.mkdtemp(prefix="o-judge-")
        path = os.path.join(workdir, "screen.png")
        try:
            with open(path, "wb") as f:
                f.write(png_bytes)
            prompt = (
                JUDGE_SYSTEM_PROMPT
                + f"\n\nThe screenshot is the PNG file at: {path}\nRead that image.\n\n"
                + build_judge_prompt(question)
            )
            try:
                proc = subprocess.run(
                    self._command(), input=prompt, capture_output=True, text=True, timeout=self.timeout, cwd=workdir
                )
            except (OSError, subprocess.SubprocessError) as e:
                raise SchemaViolation(f"claude cli failed to run: {e}") from None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
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
        return parse_verdict(result, self.name)


_QUOTED = re.compile(
    r"""(?:^|(?<=[\s:(\[,]))(?:"([^"]+)"|'([^']+)'|\u201c([^\u201d]+)\u201d|\u2018([^\u2019]+)\u2019)(?=$|[\s.,;:)\]?!])"""
)
_NEGATION = re.compile(r"\b(no|not|none|without|never|nothing|n't|absent|missing|removed|deleted|empty|blank|cleared)\b|n't\b", re.I)
_INVERT = re.compile(r"\bnot\b|n't\b|\bnon-?empty\b", re.I)
_EMPTY = re.compile(r"\b(empty|blank|cleared)\b", re.I)
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_LINE_COUNT = re.compile(r"\bexactly\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:non-empty\s+)?lines?\b", re.I)
_TEXT_ROLES = {"text", "entry", "document text", "paragraph", "terminal", "editbar", "password text", "spin button"}


def _walk(nodes: Any):
    for root in nodes or ():
        stack = [root]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(list(_get(n, "children") or [])))


def tree_text_verdict(question: str, nodes: Any) -> JudgeVerdict | None:
    """Answer a yes/no screen question from the focused window's accessibility text, with no model call.

    Decides only the shapes it can read exactly: every quoted string in the question present in the
    window's text (optionally ``exactly N lines`` of the text element holding them), or an "empty"
    question about the editable text. Anything else (no quotes, negations around quotes) returns None.
    """
    q = str(question or "")
    texts = [str(_get(n, "text") or "") for n in _walk(nodes) if str(_get(n, "role") or "").lower() in _TEXT_ROLES]
    if not texts:
        return None
    quoted = [next(g for g in m.groups() if g) for m in _QUOTED.finditer(q)]
    quoted = [s.strip() for s in quoted if s and s.strip()]
    if not quoted:
        if _EMPTY.search(q) and not _INVERT.search(q):
            empty = all(not t.strip() for t in texts)
            return JudgeVerdict(ok=empty, source="tree-text",
                                reason="the window's text elements are empty" if empty else
                                f"the window's text is not empty ({sum(len(t) for t in texts)} chars)")
        return None
    if _NEGATION.search(q):
        return None  # "no 'x'", "without 'x'": presence logic would invert; not decided here
    hay = "\n".join(texts)
    missing = [s for s in quoted if s.lower() not in hay.lower()]
    if missing:
        return JudgeVerdict(ok=False, source="tree-text",
                            reason=f"{missing[0]!r} is not in the focused window's text")
    m = _LINE_COUNT.search(q)
    if m:
        want = int(m.group(1)) if m.group(1).isdigit() else _NUMBERS[m.group(1).lower()]
        holder = next((t for t in texts if quoted[0].lower() in t.lower()), hay)
        have = len([ln for ln in holder.splitlines() if ln.strip()])
        if have != want:
            return JudgeVerdict(ok=False, source="tree-text", reason=f"the text has {have} non-empty lines, not {want}")
    return JudgeVerdict(ok=True, source="tree-text", reason=f"the focused window's text contains {len(quoted)} quoted string(s)")


DEFERRED_REASON = "not checked on screen: no local judge answered; deferred to the final goal check"


class Judge:
    """primary -> (tree text) -> fallback once. Never raises (except at the job's model-call cap).

    Cost policy (real run: most of a job's paid calls were intermediate screen checks while vLLM was down):

    * identical (screenshot sha256, question) pairs are answered once per job (the job meter's cache);
    * when the primary (local VLM) does not answer and ``tree`` is given, the focused window's
      accessibility text decides when it can (``tree_text_verdict``);
    * only a ``final=True`` question (the job's goal check) reaches the paid fallback. A non-final one
      that nothing local could decide passes as ``source="deferred"`` and is counted on the job meter, so
      the graph runs the final goal check before reporting success.

    ``final`` defaults to True so a caller that does not know about the policy keeps the old behaviour.
    """

    def __init__(self, primary: JudgeBackend | None, fallback: JudgeBackend | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.last_source: str = "none"

    def judge(self, png_bytes: bytes, question: str, tree: Any = None, final: bool = True) -> JudgeVerdict:
        self.last_source = "none"
        m = meter.current()
        if m is not None:
            hit = m.cached_verdict(png_bytes, question)
            if hit is not None and (final or hit.source != "deferred"):
                self.last_source = hit.source
                return hit
        errors: list[str] = []
        verdict = self._ask(self.primary, png_bytes, question, errors)
        if verdict is None and tree is not None:
            try:
                nodes = tree() if callable(tree) else tree
                verdict = tree_text_verdict(question, nodes)
            except meter.ModelCallCapReached:
                raise
            except Exception as e:  # noqa: BLE001 - an unreadable tree just means "not decided here"
                errors.append(f"tree-text: {type(e).__name__}: {e}")
        if verdict is None and not final and self.fallback is not None:
            if m is not None:
                m.deferred += 1
            self.last_source = "deferred"
            return JudgeVerdict(ok=True, reason=DEFERRED_REASON, source="deferred")
        if verdict is None:
            verdict = self._ask(self.fallback, png_bytes, question, errors)
        if verdict is None:
            if not errors:
                errors.append("no judge backend configured")
            return JudgeVerdict(ok=False, reason="judge unavailable (" + "; ".join(errors) + ")", source="none")
        self.last_source = verdict.source or "judge"
        if m is not None:
            m.cache_verdict(png_bytes, question, verdict)
        return verdict

    def _ask(self, backend: JudgeBackend | None, png_bytes: bytes, question: str, errors: list[str]) -> JudgeVerdict | None:
        if backend is None:
            return None
        try:
            v = backend.judge(png_bytes, question)
        except meter.ModelCallCapReached:
            raise
        except Exception as e:
            errors.append(f"{backend.name}: {e}")
            return None
        if not v.source:
            v = JudgeVerdict(ok=v.ok, reason=v.reason, source=backend.name)
        return v

    def ask(self, png_bytes: bytes, question: str, tree: Any = None, final: bool = True) -> tuple[bool, str]:
        """Executor-facing form of ``judge``."""
        v = self.judge(png_bytes, question, tree=tree, final=final)
        return v.ok, v.reason


def _wanted_states(target: Any) -> list[str]:
    raw = _get(target, "states") or _get(target, "state") or []
    if isinstance(raw, str):
        raw = [s for s in raw.split("|") if s]
    return [str(s).strip().lower() for s in raw if str(s).strip()]


def a11y_assert(node: Any, target: Any, state: Any = None) -> JudgeVerdict:
    """Check a tree ``Node`` (role, name, text, states) against a ``Target`` with no model call.

    ``node`` may be ``None`` (element not found). ``target.name`` is a glob. Required states come
    from ``state`` (an AssertStep's ``state``, str or list) and/or ``target.state``/``target.states``;
    all must be present in ``node.states`` (case-insensitive).
    """
    if node is None:
        return JudgeVerdict(ok=False, reason="element not found in accessibility tree", source="a11y")
    role = _get(target, "role")
    if role and str(_get(node, "role", "")).lower() != str(role).lower():
        return JudgeVerdict(ok=False, reason=f"role is {_get(node, 'role')!r}, expected {role!r}", source="a11y")
    name = _get(target, "name")
    if name:
        actual = str(_get(node, "name", "") or "")
        if not fnmatch.fnmatchcase(actual, str(name)) and not fnmatch.fnmatchcase(actual.lower(), str(name).lower()):
            return JudgeVerdict(ok=False, reason=f"name {actual!r} does not match {name!r}", source="a11y")
    text = _get(target, "text")
    if text:
        actual_text = str(_get(node, "text", "") or "")
        if str(text) not in actual_text:
            return JudgeVerdict(ok=False, reason=f"text {text!r} not in element text", source="a11y")
    have = {str(s).lower() for s in (_get(node, "states") or [])}
    wanted = _wanted_states(target)
    if state:
        extra = [s for s in state.split("|") if s] if isinstance(state, str) else list(state)
        wanted += [str(s).strip().lower() for s in extra if str(s).strip()]
    for want in wanted:
        if want not in have:
            return JudgeVerdict(ok=False, reason=f"state {want!r} not set (states: {sorted(have)})", source="a11y")
    return JudgeVerdict(ok=True, reason="accessibility assertion holds", source="a11y")


# ------------------------------------------------------------------ StateJudge (P4)

_SHA_HEX = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Any) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (OSError, ValueError):
        return None


def _parse_selector(arg: str) -> Target:
    text = arg.strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"tree selector is not JSON: {e.msg}") from None
        if not isinstance(obj, dict):
            raise ValueError("tree selector must be a JSON object")
        try:
            return Target.model_validate(obj)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
            raise ValueError(f"tree selector invalid: {problems}") from None
    return Target(name=text)


class StateJudge:
    """Verifies ``state_assert`` checks against the real system. Never calls a model, never raises.

    ``sandbox`` is the job's ``Sandbox`` (``resolve(path)`` and ``run(ProvisionStep)``); ``backend``
    is the ``CUBackend`` whose ``find(target)`` answers ``tree`` checks. Every verdict has
    ``source="state"``. Checks run in order and the first failure ends the verdict, so a refused
    or failing check never lets a later shell probe run.
    """

    PROBE_TIMEOUT_S = 10
    MAX_FILE_BYTES = 16 * 1024 * 1024

    def __init__(self, sandbox: Any, backend: Any) -> None:
        self.sandbox = sandbox
        self.backend = backend
        self.model_calls = 0  # always 0; exposed so wiring tests can assert it

    # ---------------------------------------------------------------- baseline

    def snapshot(self, paths: Any) -> dict[str, str | None]:
        """sha256 of each sandbox-relative path now (``None`` when absent/unreadable/outside)."""
        out: dict[str, str | None] = {}
        for p in paths:
            key = str(p)
            try:
                out[key] = _sha256_file(self.sandbox.resolve(key))
            except Exception:
                out[key] = None
        return out

    # ---------------------------------------------------------------- verify

    def verify(self, checks: Any, job_id: str, baseline: dict[str, str | None] | None = None) -> JudgeVerdict:
        items = list(checks)
        if not items:
            return JudgeVerdict(ok=False, reason="no checks given", source="state")
        for i, check in enumerate(items, start=1):
            try:
                ok, reason = self._one(check, baseline)
            except Exception as e:  # a broken probe is a failed check, never a crash
                ok, reason = False, f"{type(e).__name__}: {e}"
            if not ok:
                return JudgeVerdict(ok=False, reason=f"{reason} [check {i}: {check.kind} {check.arg!r}]", source="state")
        return JudgeVerdict(ok=True, reason=f"{len(items)} state check(s) hold", source="state")

    def _path(self, arg: str) -> Any:
        try:
            return self.sandbox.resolve(arg)
        except Exception as e:
            raise ValueError(f"path resolves outside sandbox ({e})") from None

    def _one(self, check: StateCheck, baseline: dict[str, str | None] | None) -> tuple[bool, str]:
        kind = check.kind
        if kind == "file_exists":
            path = self._path(check.arg)
            present = os.path.exists(path)
            want_absent = (check.expect or "").strip().lower() == "false"
            if want_absent:
                return (not present, "file absent" if not present else "file still present")
            return (present, "file present" if present else "file not found")
        if kind == "file_contains":
            path = self._path(check.arg)
            if not os.path.isfile(path):
                return False, "file not found"
            if os.path.getsize(path) > self.MAX_FILE_BYTES:
                return False, "file too large to scan"
            with open(path, "rb") as f:
                data = f.read()
            needle = (check.expect or "").encode("utf-8")
            if needle in data:
                return True, "substring present"
            return False, f"substring {check.expect!r} not in file"
        if kind == "file_sha_changed":
            if baseline is None or check.arg not in baseline:
                return False, "no baseline digest captured for this path"
            before = baseline[check.arg]
            now = _sha256_file(self._path(check.arg))
            if check.expect is not None:
                want = check.expect.strip().lower()
                if not _SHA_HEX.match(want):
                    return False, "expect is not a sha256 hex digest"
                if now != want:
                    return False, f"digest {now} is not the expected one"
            if now is None:
                return False, "file not found" if before is None else "file removed"
            if now == before:
                return False, "file unchanged since baseline"
            return True, "digest changed"
        if kind == "tree":
            target = _parse_selector(check.arg)
            node = self.backend.find(target)
            if node is None:
                return False, f"element not found in accessibility tree ({target.describe()})"
            if check.expect:
                want = check.expect.strip().lower()
                have = {str(s).lower() for s in (getattr(node, "states", None) or [])}
                if want not in have:
                    return False, f"state {want!r} not set (states: {sorted(have)})"
            return True, "element present"
        if kind == "shell_true":
            if not is_allowed_probe(check.arg):
                return False, "probe not allowed; read-only allow-list: " + ", ".join(PROBE_ALLOW_LIST)
            step = ProvisionStep(
                target_directory=".", command_string=check.arg, timeout_seconds=self.PROBE_TIMEOUT_S
            )
            result = self.sandbox.run(step)
            if getattr(result, "timed_out", False):
                return False, "probe timed out"
            if result.exit_code != 0:
                return False, f"probe exited {result.exit_code}: {(result.stderr or '').strip()[:200]}"
            if check.expect is not None and (result.stdout or "").strip() != check.expect.strip():
                return False, f"probe stdout {(result.stdout or '').strip()[:200]!r} != {check.expect!r}"
            return True, "probe true"
        return False, f"unknown check kind {kind!r}"
