"""Runs an OScript against a CUBackend, one step at a time, with evidence for every step.

Element resolution order (per step that names a target):
  1. backend.find(target)                       -> resolved_by="a11y"
  2. locator_cache.get(app, target, screen_hash) -> resolved_by="locator"  (bbox dict)
  3. grounder.ground(screenshot, target)         -> resolved_by="vision"   (bbox dict)
  4. structured failure (StepResult.ok=False, RunResult.failed_index) — never an exception.

Each step writes two action-log entries: ``tool`` before it acts (what will happen, the
before-digest of the tree and the screenshot hash) and ``result`` after (ok/error, the
after-digest, elapsed time). Pixel fallbacks are the backend's decision and are gated by
its ``allow_pixels`` flag; the executor only ever asks for API-level actions.

Loop breaker (P5): the runner keeps a ring of the last ``loop_window`` ``(screen_sha, step_digest)``
pairs it has executed. ``screen_sha`` is the sha256 tree digest, combined with the screenshot hash
only when the backend allows pixels (an isolated display — never the host). ``step_digest`` is the
sha256 of the step's compact JSON. When the pair about to run already occurs ``loop_threshold - 1``
times in the ring, the step is not executed: it fails with ``reason="loop"``, ``status="loop"`` and
``evidence`` listing every repeat, so the heal loop can replan instead of spinning.

Wave-2 ops (all optional collaborators are keyword ctor args; a missing one is a structured failure,
never a silent pass):

* ``state_assert`` (P4) -> ``state_judge.verify(checks, job_id, baseline)``. The baseline (sha256 of
  every ``file_sha_changed`` path named anywhere in the script) is taken by ``state_judge.snapshot``
  at ``run()`` start, i.e. before any step; a passed assert refreshes it for the paths it checked.
* ``script`` (P8) -> ``runner.run(payload, sandbox, index, env_extra={"DISPLAY": <isolated>},
  isolated_display=False)``. The DISPLAY handed over is only ever the isolated display's name
  (``display=`` ctor arg, else the backend's ``display``); ``Sandbox.run`` already strips the host's.
  No display known -> the script runs with no DISPLAY at all. Nonzero exit / timeout -> failure.
* ``call`` (P7) -> the newest ``interface_store.api_source(app)`` is gated by ``interfaces.check_ast``
  and ``check_source`` (any hit is a "forbidden" failure, nothing runs), then executed in a namespace
  holding ``backend`` (a wrapper whose ``find`` accepts dict targets) and ``Target``; ``fn`` is
  ``<ns>.<name>`` and ``<name>`` is looked up by bare name (the ``<ns>`` is not trusted). A function
  whose first parameter is ``backend`` (the InterfaceBuilder contract) gets that wrapper passed
  positionally; the step's ``args`` are keyword arguments and unknown/missing keys are a structured
  failure before the function runs.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shlex
import subprocess
import time
from collections import deque
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .backends.base import BackendError, CUBackend, Node, synthetic_node
from .schema import (
    AssertStep,
    CallStep,
    ClickStep,
    FindStep,
    InvokeStep,
    KeyStep,
    LaunchStep,
    OScript,
    ScreenshotAssertStep,
    ScriptStep,
    SetValueStep,
    StateAssertStep,
    Target,
    TypeStep,
    WaitStep,
)

ResolvedBy = Literal["a11y", "locator", "vision"]
StepStatus = Literal["ok", "failed", "loop"]
FailureReason = Literal["loop"]
DEFAULT_POLL_INTERVAL = 0.25
DEFAULT_LOOP_THRESHOLD = 3
DEFAULT_LOOP_WINDOW = 8
DEFAULT_FIND_SETTLE_POLLS = 8  # x poll_interval: how long a find waits for a just-opened window's tree to settle
# Steps after which the tree may still be changing (a dialog mapping, a window opening) when the next find runs.
_SETTLE_AFTER = frozenset({"launch", "click", "key", "type", "set_value", "invoke", "call", "script"})


class Grounder(Protocol):
    def ground(self, screenshot: bytes, target: Target) -> dict | None: ...


class Judge(Protocol):
    def ask(self, screenshot: bytes, question: str) -> Any: ...


class LocatorCache(Protocol):
    def get(self, app: str, target: Target, screen_hash: str) -> dict | None: ...


class StepResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    op: str
    ok: bool
    status: StepStatus = "ok"
    resolved_by: ResolvedBy | None = None
    error: str | None = None
    reason: FailureReason | None = None
    evidence: tuple[dict, ...] = ()
    note: str | None = None
    elapsed_ms: int = Field(ge=0)
    before_digest: str
    after_digest: str
    screenshot_sha: str | None = None


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    steps: tuple[StepResult, ...] = ()
    failed_index: int | None = None

    @property
    def error(self) -> str | None:
        if self.failed_index is None:
            return None
        return self.steps[self.failed_index].error


class _StepFailure(Exception):
    """Internal: a step did not succeed. Carries the resolution source if one was reached,
    and for categorised failures (``reason="loop"``) the evidence rows for the replan prompt."""

    def __init__(
        self,
        message: str,
        resolved_by: ResolvedBy | None = None,
        *,
        reason: FailureReason | None = None,
        evidence: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.resolved_by = resolved_by
        self.reason = reason
        self.evidence = list(evidence or [])


class ScriptRunner:
    def __init__(
        self,
        backend: CUBackend,
        log: Any,
        grounder: Grounder | None = None,
        judge: Judge | None = None,
        locator_cache: LocatorCache | None = None,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        popen: Callable[..., Any] = subprocess.Popen,
        loop_threshold: int = DEFAULT_LOOP_THRESHOLD,
        loop_window: int = DEFAULT_LOOP_WINDOW,
        state_judge: Any = None,
        runner: Any = None,
        sandbox: Any = None,
        display: Any = None,
        interface_store: Any = None,
        find_settle_polls: int = DEFAULT_FIND_SETTLE_POLLS,
    ) -> None:
        if loop_threshold < 2:
            raise ValueError("loop_threshold must be >= 2")
        if loop_window < loop_threshold:
            raise ValueError("loop_window must be >= loop_threshold")
        self.backend = backend
        self.log = log
        self.grounder = grounder
        self.judge = judge
        self.locator_cache = locator_cache
        self.poll_interval = poll_interval
        self.loop_threshold = loop_threshold
        self.loop_window = loop_window
        self.state_judge = state_judge
        self.runner = runner
        self.sandbox = sandbox
        self.display = display
        self.interface_store = interface_store
        self.find_settle_polls = max(0, int(find_settle_polls))
        self._prev_op: str | None = None
        self._clock = clock
        self._sleep = sleep
        self._popen = popen
        self._baseline: dict[str, str | None] = {}
        self._baselines: dict[int, dict[str, str | None]] = {}
        # ring of (screen_sha, step_digest, index, step_text, op) for the steps already executed
        self._ring: deque[tuple[str, str, int, str, str]] = deque(maxlen=loop_window)
        self._loop_evidence: list[dict] = []

    # ------------------------------------------------------------------ run

    def loop_evidence(self) -> list[dict]:
        """Rows ``{index, op, step, screen_sha}`` of the repeats that tripped the last loop break
        (the oldest first, the refused step last), or ``[]`` when no loop was detected."""
        return [dict(row) for row in self._loop_evidence]

    def baseline(self, index: int | None = None) -> dict[str, str | None]:
        """The ``file_sha_changed`` baseline in force before step ``index`` ran (the current one when
        ``index`` is None): sandbox-relative path -> sha256 or None (absent)."""
        if index is None:
            return dict(self._baseline)
        return dict(self._baselines.get(index, {}))

    def _snapshot_baseline(self, script: OScript) -> dict[str, str | None]:
        paths = _sha_paths(script)
        if not paths or self.state_judge is None:
            return {}
        snapshot = getattr(self.state_judge, "snapshot", None)
        if not callable(snapshot):
            return {}
        try:
            return dict(snapshot(paths))
        except Exception:  # noqa: BLE001 - a missing baseline fails the check later, never the run
            return {}

    def run(self, script: OScript, job_id: str, start: int = 0) -> RunResult:
        """Run ``script.steps[start:]``. ``start > 0`` resumes a healed script at the replaced step: the steps
        before it already ran (re-running a ``key ctrl+shift+s`` / ``type`` would redo the save), so they are
        skipped, and only the refs that the remaining steps use are re-found, through the tree, without acting."""
        refs: dict[str, Node] = {}
        results: list[StepResult] = []
        self._ring.clear()
        self._loop_evidence = []
        self._baselines = {}
        self._baseline = self._snapshot_baseline(script)
        self._prev_op = None
        start = max(0, min(int(start or 0), len(script.steps) - 1))
        if start:
            self._rebind_refs(script, start, refs, job_id)
        for i, step in enumerate(script.steps):
            if i < start:
                continue
            self._baselines[i] = dict(self._baseline)
            result = self._run_step(script, i, step, refs, job_id, index=i)
            self._prev_op = step.op
            results.append(result)
            if not result.ok:
                return RunResult(ok=False, steps=tuple(results), failed_index=i)
        return RunResult(ok=True, steps=tuple(results), failed_index=None)

    def _rebind_refs(self, script: OScript, start: int, refs: dict[str, Node], job_id: str) -> None:
        """Re-find (tree only, no action, no vision) every ref bound before ``start`` that a later step uses."""
        used: set[str] = set()
        for step in script.steps[start:]:
            ref = getattr(step, "ref", None)
            if ref:
                used.add(ref)
            for t in (getattr(step, "target", None), getattr(step, "for_", None)):
                if isinstance(t, Target) and t.within:
                    used.add(t.within)
        found: list[str] = []
        missing: list[str] = []
        for step in script.steps[:start]:
            if not isinstance(step, FindStep) or step.as_ not in used:
                continue
            try:
                node = self.backend.find(step.target, within=self._scope(step.target, refs))
            except Exception:  # noqa: BLE001 - an unbound ref fails the step that uses it
                node = None
            if node is None:
                missing.append(step.as_)
            else:
                refs[step.as_] = node
                found.append(step.as_)
        self.log.record("note", f"{job_id}: resuming at step {start}; steps 0-{start - 1} already ran and are not "
                        f"repeated" + (f"; re-found {', '.join(found)}" if found else "")
                        + (f"; could not re-find {', '.join(missing)}" if missing else ""),
                        job_id=job_id, start=start, refs=found, missing=missing)

    def _run_step(self, script: OScript, i: int, step: Any, refs: dict[str, Node], job_id: str, index: int = 0) -> StepResult:
        started = self._clock()
        summary = f"{job_id}: cu step {i} {step.op} {_describe(step)}".rstrip()
        before_digest = ""
        screenshot: bytes | None = None
        screenshot_sha: str | None = None
        resolved_by: ResolvedBy | None = None
        note: str | None = None
        error: str | None = None
        reason: FailureReason | None = None
        evidence: list[dict] = []
        try:
            nodes = self.backend.tree()
            if self._dismiss_dialogs(nodes, job_id, i):
                nodes = self.backend.tree()
            before_digest = self.backend.tree_digest(nodes)
            screenshot = self.backend.screenshot()
            screenshot_sha = hashlib.sha256(screenshot).hexdigest()
        except Exception as e:
            error = f"could not capture evidence before step: {e}"
        self.log.record(
            "tool",
            summary,
            job_id=job_id,
            app=script.app,
            index=i,
            op=step.op,
            step=step.model_dump(by_alias=True, exclude_none=True),
            before_digest=before_digest,
            screenshot_sha=screenshot_sha,
        )
        if error is None:
            try:
                self._check_loop(i, step, before_digest, screenshot_sha)
                resolved_by, note = self._dispatch(script, step, refs, screenshot or b"", screenshot_sha or "", job_id, i)
            except _StepFailure as e:
                error, resolved_by, reason, evidence = str(e), e.resolved_by, e.reason, e.evidence
            except (BackendError, Exception) as e:  # noqa: BLE001 - every failure becomes a structured result
                error = f"{type(e).__name__}: {e}" if not isinstance(e, BackendError) else str(e)
        try:
            after_digest = self.backend.tree_digest(self.backend.tree())
        except Exception as e:
            after_digest = ""
            error = error or f"could not capture evidence after step: {e}"
        elapsed_ms = max(0, int((self._clock() - started) * 1000))
        status: StepStatus = "ok" if error is None else (reason or "failed")
        result = StepResult(
            index=i,
            op=step.op,
            ok=error is None,
            status=status,
            resolved_by=resolved_by,
            error=error,
            reason=reason,
            evidence=tuple(evidence),
            note=note,
            elapsed_ms=elapsed_ms,
            before_digest=before_digest,
            after_digest=after_digest,
            screenshot_sha=screenshot_sha,
        )
        self.log.record(
            "result",
            f"{summary} -> {'ok' if result.ok else 'FAILED: ' + (error or '')}",
            job_id=job_id,
            app=script.app,
            index=i,
            op=step.op,
            ok=result.ok,
            status=status,
            resolved_by=resolved_by,
            error=error,
            reason=reason,
            evidence=list(evidence),
            note=note,
            elapsed_ms=elapsed_ms,
            before_digest=before_digest,
            after_digest=after_digest,
            screenshot_sha=screenshot_sha,
            tree_changed=bool(before_digest and after_digest and before_digest != after_digest),
        )
        return result

    # --------------------------------------------------------- loop breaker

    def _screen_sha(self, tree_digest: str, screenshot_sha: str | None) -> str:
        """Identity of the screen a step runs on: the a11y tree digest, widened with the screenshot
        hash only when the backend allows pixels (an isolated display, never the host)."""
        if screenshot_sha and getattr(self.backend, "allow_pixels", False):
            return hashlib.sha256(f"{tree_digest}:{screenshot_sha}".encode()).hexdigest()
        return tree_digest

    def _check_loop(self, i: int, step: Any, tree_digest: str, screenshot_sha: str | None) -> None:
        screen_sha = self._screen_sha(tree_digest, screenshot_sha)
        step_digest = _step_digest(step)
        text = f"{step.op} {_describe(step)}".rstrip()
        repeats = [row for row in self._ring if row[0] == screen_sha and row[1] == step_digest]
        self._ring.append((screen_sha, step_digest, i, text, step.op))
        if len(repeats) + 1 < self.loop_threshold:
            return
        evidence = [{"index": idx, "op": op, "step": txt, "screen_sha": sha} for sha, _, idx, txt, op in repeats]
        evidence.append({"index": i, "op": step.op, "step": text, "screen_sha": screen_sha})
        self._loop_evidence = evidence
        indices = ", ".join(str(e["index"]) for e in evidence)
        raise _StepFailure(
            f"loop: step {i} ({text}) would run for the {_ordinal(len(evidence))} time on an unchanged screen "
            f"(steps {indices}); stopping before executing it",
            reason="loop",
            evidence=evidence,
        )

    # ------------------------------------------------------------- dispatch

    def _dispatch(
        self, script: OScript, step: Any, refs: dict[str, Node], screenshot: bytes, screen_hash: str,
        job_id: str = "", index: int = 0,
    ) -> tuple[ResolvedBy | None, str | None]:
        if isinstance(step, LaunchStep):
            return None, self._launch(step)
        if isinstance(step, WaitStep):
            return self._wait(script, step, refs), None
        if isinstance(step, FindStep):
            node, how = self._resolve(script.app, step.target, refs, screenshot, screen_hash)
            refs[step.as_] = node
            return how, None
        if isinstance(step, ClickStep):
            self.backend.click(self._ref(refs, step.ref))
            return None, None
        if isinstance(step, TypeStep):
            node = self._ref(refs, step.ref) if step.ref else None
            if node is not None and node.synthetic:
                return None, self._pixel_fill(node, step.text, replace=False, job_id=job_id, index=index)
            why = self._type_replaces(node, step.text) if node is not None else None
            if why:
                # Real run 3: `type ~/lisbon.txt` into the Save As name entry appended to the prefilled
                # "lisbon.txt" on each re-run ("lisbon.txt~/lisbon.txt~/..."). A file name is a whole value.
                self.backend.set_value(node, step.text)
                note = f"type into {why}: replaced its value (set_value) instead of inserting at the cursor"
                self.log.record("note", f"{job_id}: step {index}: {note}", job_id=job_id, index=index,
                                coerced="type->set_value", node=node.id)
                return None, note
            self.backend.type_text(node, step.text)
            return None, None
        if isinstance(step, KeyStep):
            # Every real backend's key() is itself a pixel action, so the coercion only applies where pixels are allowed.
            if getattr(self.backend, "allow_pixels", False) and not is_key_chord(step.keys):
                # Real run: the planner sent `key ~/lisbon.txt`; xdotool rejected it as a key sequence.
                note = f"key {step.keys!r} is not a key chord; typed it as text instead"
                self.log.record("note", f"{job_id}: step {index}: {note}", job_id=job_id, index=index,
                                coerced="key->type", keys=step.keys)
                self.backend.type_text(None, step.keys)
                return None, note
            self.backend.key(step.keys)
            return None, None
        if isinstance(step, SetValueStep):
            node = self._ref(refs, step.ref)
            if node.synthetic:
                return None, self._pixel_fill(node, step.value, replace=True, job_id=job_id, index=index)
            self.backend.set_value(node, step.value)
            return None, None
        if isinstance(step, InvokeStep):
            node = self._ref(refs, step.ref)
            if node.synthetic:
                self._require_pixels_for(node, f"invoke {step.action!r}")
                self.backend.click(node)
                note = f"invoke {step.action!r} on screen region {node.id}: clicked its center"
                self.log.record("note", f"{job_id}: step {index}: {note}", job_id=job_id, index=index,
                                coerced="invoke->click", node=node.id)
                return None, note
            self.backend.invoke(node, step.action)
            return None, None
        if isinstance(step, AssertStep):
            return self._assert(step, refs), None
        if isinstance(step, ScreenshotAssertStep):
            return None, self._screenshot_assert(step, screenshot)
        if isinstance(step, StateAssertStep):
            return None, self._state_assert(step, job_id)
        if isinstance(step, ScriptStep):
            return None, self._script(step, index)
        if isinstance(step, CallStep):
            return None, self._call(script.app, step)
        raise _StepFailure(f"unsupported op {step.op!r}")

    def _type_replaces(self, node: Node, text: str) -> str | None:
        """Why a ``type`` into ``node`` must replace its value rather than insert at the cursor, or None.

        The rule (also in the planner prompt): ``set_value`` replaces a field's value, ``type`` inserts at the
        cursor. A ``type`` is still a replacement when it targets a single-line entry inside a file chooser /
        save-open dialog (the file-name field), or a single-line entry that already holds text and the typed
        text is a whole value (a path or a file name). Multi-line editors are never coerced."""
        if node.role not in _ENTRY_ROLES or "\n" in text or not text.strip():
            return None
        states = {s.lower() for s in (node.states or [])}
        if "multi line" in states:
            return None
        try:
            chain = _ancestors(self.backend.tree(), node.id)
        except Exception:  # noqa: BLE001 - no tree: keep insert semantics
            chain = []
        in_dialog = next((a for a in chain if _is_file_dialog(a)), None)
        if in_dialog is not None:
            return f"the file-name entry of {in_dialog.role} {in_dialog.name!r}"
        single = "single line" in states or any(a.role in ("dialog", "alert") for a in chain)
        if single and (node.text or "").strip() and _looks_like_whole_value(text):
            return f"single-line entry {node.name!r} that already held text"
        return None

    def _require_pixels_for(self, node: Node, what: str) -> None:
        if not getattr(self.backend, "allow_pixels", False):
            raise _StepFailure(
                f"{what}: {node.id!r} was found only on screen, by vision or the locator cache, and pixel actions "
                "are disabled on this display; only O's isolated display allows them"
            )
        if node.rect is None:
            raise _StepFailure(f"{what}: screen region {node.id!r} has no rectangle")

    def _pixel_fill(self, node: Node, text: str, *, replace: bool, job_id: str, index: int) -> str:
        """A field found only by vision / the locator cache: click its center, select all when the value is
        replaced (set_value), then type into the focused widget. Pixel actions exist only on the isolated display."""
        op = "set_value" if replace else "type"
        self._require_pixels_for(node, op)
        x, y = node.rect.center  # type: ignore[union-attr]
        self.backend.pixel_click(x, y)
        if replace:
            self.backend.key("ctrl+a")
        self.backend.type_text(None, text)
        note = (f"{op} on screen region {node.id}: clicked ({x},{y})"
                + (", selected all" if replace else "") + f", typed {len(text)} chars")
        self.log.record("note", f"{job_id}: step {index}: {note}", job_id=job_id, index=index,
                        coerced=f"{op}->pixels", node=node.id)
        return note

    @staticmethod
    def _ref(refs: dict[str, Node], name: str) -> Node:
        try:
            return refs[name]
        except KeyError:
            raise _StepFailure(f"ref {name!r} was never found") from None

    def _scope(self, target: Target, refs: dict[str, Node]) -> Node | None:
        if target.within is None:
            return None
        scope = self._ref(refs, target.within)
        if scope.synthetic:
            raise _StepFailure(f"within={target.within!r} is a screen region, not an accessibility node")
        return scope

    def _resolve(
        self, app: str, target: Target, refs: dict[str, Node], screenshot: bytes, screen_hash: str
    ) -> tuple[Node, ResolvedBy]:
        scope = self._scope(target, refs)
        node = self.backend.find(target, within=scope)
        if node is not None:
            return node, "a11y"
        node = self._settle_find(target, scope)
        if node is not None:
            return node, "a11y"
        if self.locator_cache is not None:
            bbox = self.locator_cache.get(app, target, screen_hash)
            if _bbox_ok(bbox):
                return synthetic_node(target, bbox, "locator"), "locator"
        if self.grounder is not None:
            bbox = self.grounder.ground(screenshot, target)
            if _bbox_ok(bbox):
                put = getattr(self.locator_cache, "put", None)
                if callable(put):
                    try:
                        put(app, target, screen_hash, dict(bbox))
                    except Exception:  # noqa: BLE001 - the cache is a convenience, never a failure
                        pass
                return synthetic_node(target, bbox, "vision"), "vision"
        tried = ["a11y"] + (["locator"] if self.locator_cache is not None else []) + (["vision"] if self.grounder else [])
        raise _StepFailure(f"element not found ({target.describe()}); tried {', '.join(tried)}")

    def _settle_find(self, target: Target, scope: Node | None) -> Node | None:
        """Right after an action (ctrl+s, a click) the window it opens may not be in the tree yet: re-read the
        tree while it is still changing, up to ``find_settle_polls`` times, before falling back to the screen.
        Real run: the Save As entry was looked up before the dialog mapped and resolved by vision instead."""
        if self._prev_op not in _SETTLE_AFTER or not self.find_settle_polls:
            return None
        try:
            last = self.backend.tree_digest(self.backend.tree())
        except Exception:  # noqa: BLE001
            return None
        for _ in range(self.find_settle_polls):
            self._sleep(self.poll_interval)
            node = self.backend.find(target, within=scope)
            if node is not None:
                return node
            try:
                now = self.backend.tree_digest(self.backend.tree())
            except Exception:  # noqa: BLE001
                return None
            if now == last:
                return None
            last = now
        return None

    def _wait(self, script: OScript, step: WaitStep, refs: dict[str, Node]) -> ResolvedBy:
        scope = self._scope(step.for_, refs)
        deadline = self._clock() + step.timeout_s
        while True:
            if self.backend.find(step.for_, within=scope) is not None:
                return "a11y"
            if self._clock() >= deadline:
                raise _StepFailure(f"wait timed out after {step.timeout_s}s ({step.for_.describe()})")
            self._sleep(min(self.poll_interval, max(0.0, deadline - self._clock())) or self.poll_interval)

    def _assert(self, step: AssertStep, refs: dict[str, Node]) -> ResolvedBy:
        scope = self._scope(step.target, refs)
        node = self.backend.find(step.target, within=scope)
        if node is None:
            raise _StepFailure(f"assert: element not found ({step.target.describe()})")
        if step.state not in node.states:
            raise _StepFailure(
                f"assert: {node.role!r} {node.name!r} is not {step.state} (states: {', '.join(node.states) or 'none'})",
                resolved_by="a11y",
            )
        return "a11y"

    def _screenshot_assert(self, step: ScreenshotAssertStep, screenshot: bytes) -> str:
        if self.judge is None:
            return f"screenshot_assert skipped: no judge configured ({step.question[:80]!r})"
        ask = self.judge.ask
        # A mid-script screen check never spends a paid fallback call: the judge may answer from the focused
        # window's a11y text or defer to the job's final goal check (cu/judge.py Judge policy).
        if _accepts(ask, "tree") and _accepts(ask, "final"):
            verdict = ask(screenshot, step.question, tree=self.backend.tree, final=False)
        else:
            verdict = ask(screenshot, step.question)
        ok, reason = _verdict(verdict)
        if not ok:
            raise _StepFailure(f"screenshot_assert failed: {reason or step.question}")
        return f"judge: {reason}" if reason else "judge: yes"

    # ------------------------------------------------- wave-2 ops (P4/P8/P7)

    def _state_assert(self, step: StateAssertStep, job_id: str) -> str:
        if self.state_judge is None:
            raise _StepFailure("state_assert: no state judge configured")
        verdict = self.state_judge.verify(step.checks, job_id, self._baseline or None)
        ok, reason = _verdict(verdict)
        if not ok:
            raise _StepFailure(f"state_assert failed: {reason}")
        checked = [c.arg for c in step.checks if c.kind == "file_sha_changed"]
        if checked:
            self._baseline = {**self._baseline, **self._snapshot_paths(checked)}
        return f"state: {reason}" if reason else "state: ok"

    def _snapshot_paths(self, paths: list[str]) -> dict[str, str | None]:
        snapshot = getattr(self.state_judge, "snapshot", None)
        if not callable(snapshot):
            return {}
        try:
            return dict(snapshot(paths))
        except Exception:  # noqa: BLE001
            return {}

    def display_name(self) -> str | None:
        """The isolated display a ``script`` step gets, or None. Never the host's DISPLAY."""
        name = _display_name(self.display)
        if name is None:
            name = _display_name(getattr(self.backend, "display", None))
        if not name:
            return None
        host = os.environ.get("DISPLAY")
        if host and name.strip() == host.strip():
            raise _StepFailure(f"script: display {name!r} is the host display; scripts run only on an isolated one")
        return name

    def _script(self, step: ScriptStep, index: int) -> str:
        if self.runner is None:
            raise _StepFailure("script: no runner configured")
        if self.sandbox is None:
            raise _StepFailure("script: no sandbox configured")
        display = self.display_name()
        env_extra = {"DISPLAY": display} if display else None
        result = self.runner.run(step.script, self.sandbox, index, env_extra=env_extra, isolated_display=False)
        exit_code = int(getattr(result, "exit_code", 1))
        stderr = str(getattr(result, "stderr", "") or "")
        stdout = str(getattr(result, "stdout", "") or "")
        if getattr(result, "timed_out", False):
            raise _StepFailure(f"script timed out after {step.script.timeout_seconds}s: {stderr.strip()[-400:]}")
        if exit_code != 0:
            raise _StepFailure(f"script exited {exit_code}: {(stderr.strip() or stdout.strip())[-400:]}")
        return f"script ok ({step.script.language}, display {display or 'none'}): {stdout.strip()[-400:]}"

    def _call(self, app: str, step: CallStep) -> str:
        name = step.fn.split(".", 1)[1]
        store = self.interface_store
        if store is None:
            raise _StepFailure(f"unknown function {step.fn!r}: no interface store configured")
        try:
            source = store.api_source(app)
        except Exception as e:  # noqa: BLE001
            raise _StepFailure(f"unknown function {step.fn!r}: interface store failed ({type(e).__name__}: {e})") from None
        if not source:
            raise _StepFailure(f"unknown function {step.fn!r}: no interface for app {app!r}")
        listed = _listed_functions(store, app)
        if listed is not None and name not in listed:
            raise _StepFailure(f"unknown function {step.fn!r}: not in api.json ({', '.join(sorted(listed)) or 'none'})")
        from .interfaces import check_ast, check_source

        bad = check_ast(source) + check_source(source)
        if bad:
            raise _StepFailure(f"call {step.fn!r}: interface source contains forbidden fragments: {', '.join(bad)}")
        api_backend = _ApiBackend(self.backend)
        namespace: dict[str, Any] = {"backend": api_backend, "Target": Target, "__name__": "o_api"}
        try:
            exec(compile(source, f"interfaces/{app}/api.py", "exec"), namespace)  # noqa: S102 - generated, source-checked interface
        except Exception as e:  # noqa: BLE001
            raise _StepFailure(f"call {step.fn!r}: interface failed to load ({type(e).__name__}: {e})") from None
        fn = namespace.get(name)
        if not callable(fn):
            raise _StepFailure(f"unknown function {step.fn!r}")
        # InterfaceBuilder emits ``(backend, ...)`` signatures: the backend is passed positionally, never
        # looked up as a module global. A function without a leading ``backend`` parameter is called as is.
        args = dict(step.args)
        params = _parameters(fn)
        positional = (api_backend,) if params and params[0] == "backend" else ()
        _check_args(step.fn, params[len(positional):], args, fn)
        try:
            value = fn(*positional, **args)
        except _StepFailure:
            raise
        except Exception as e:  # noqa: BLE001
            raise _StepFailure(f"call {step.fn!r} raised {type(e).__name__}: {e}") from None
        return f"call {step.fn} -> {str(value)[:200]}" if value is not None else f"call {step.fn} ok"

    def _dismiss_dialogs(self, nodes: Any, job_id: str, index: int) -> bool:
        """Known first-run / session-restore dialogs are declined through the a11y tree before a step runs."""
        fn = getattr(self.backend, "dismiss_known_dialogs", None)
        if not callable(fn):
            return False
        try:
            done = list(fn(nodes) or [])
        except Exception as e:  # noqa: BLE001 - a dialog check never fails a step
            self.log.record("note", f"{job_id}: dialog check failed ({type(e).__name__}: {e})", job_id=job_id, index=index)
            return False
        for line in done:
            self.log.record("note", f"{job_id}: dismissed dialog before step {index}: {line}", job_id=job_id, index=index)
        if done:
            self._sleep(self.poll_interval)
        return bool(done)

    def _launch(self, step: LaunchStep) -> str:
        try:
            args = shlex.split(step.cmd)
        except ValueError as e:
            raise _StepFailure(f"launch: cannot parse cmd: {e}") from None
        if not args:
            raise _StepFailure("launch: empty cmd")
        # Idempotent: an app that already has a window on this display is focused, not started again (a second
        # instance stacks over the first and splits input from what the screenshot shows). A cmd with file or
        # other positional arguments still launches, since focusing would silently drop what it asks to open.
        app = os.path.basename(args[0])
        if not [a for a in args[1:] if not a.startswith("-")]:
            focus = getattr(self.backend, "focus_existing", None)
            existing = None
            if callable(focus):
                try:
                    existing = focus(app)
                except Exception:  # noqa: BLE001 - fall through to a normal launch
                    existing = None
            if existing:
                return f"already running: focused the existing {app} window {existing!r}; no second instance started"
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        env.update(self.backend.env())
        try:
            proc = self._popen(
                args,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            raise _StepFailure(f"launch: {e}") from None
        return f"launched pid {getattr(proc, 'pid', '?')}: {step.cmd}"


# ------------------------------------------------------------------ helpers

_ENTRY_ROLES = frozenset({"text", "entry", "editbar", "combo box"})
_FILE_DIALOG_WORDS = ("save", "open", "export", "import", "file", "choose", "select", "rename", "location")


def _ancestors(nodes: Any, node_id: str) -> list[Node]:
    """The chain of ancestors of the node with ``node_id`` (nearest first), or [] when it is not in ``nodes``."""
    stack: list[tuple[Node, tuple[Node, ...]]] = [(n, ()) for n in reversed(list(nodes or []))]
    while stack:
        node, chain = stack.pop()
        if node.id == node_id:
            return list(reversed(chain))
        for child in reversed(node.children or []):
            stack.append((child, (*chain, node)))
    return []


def _is_file_dialog(node: Node) -> bool:
    if node.role == "file chooser":
        return True
    if node.role not in ("dialog", "alert"):
        return False
    name = (node.name or "").lower()
    return any(w in name for w in _FILE_DIALOG_WORDS)


def _looks_like_whole_value(text: str) -> bool:
    """A path or a file name (``~/lisbon.txt``, ``/tmp/a``, ``report.csv``): something a field holds whole."""
    t = text.strip()
    if t.startswith(("~", "/", "./", "../")) or "/" in t or "\\" in t:
        return True
    import re as _re

    return bool(_re.fullmatch(r"[\w .()\-]+\.[A-Za-z0-9]{1,8}", t))



class _ApiBackend:
    """What a generated interface sees as ``backend``: the real backend, with ``find`` accepting
    a dict target (``{"role":..,"name":..}``) as well as a ``Target``."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def find(self, target: Any, within: Any = None) -> Any:
        if isinstance(target, dict):
            target = Target.model_validate(target)
        elif isinstance(target, str):
            target = Target(name=target)
        return self._backend.find(target, within=within) if within is not None else self._backend.find(target)


MODIFIERS = frozenset({
    "ctrl", "control", "alt", "shift", "super", "meta", "cmd", "command", "win", "hyper", "altgr", "option",
    "control_l", "control_r", "alt_l", "alt_r", "shift_l", "shift_r", "super_l", "super_r", "meta_l", "meta_r",
})
KEYSYMS = frozenset(k.lower() for k in (
    "Return", "Enter", "KP_Enter", "Tab", "ISO_Left_Tab", "Escape", "Esc", "BackSpace", "Delete", "Del", "Insert",
    "Home", "End", "Page_Up", "Page_Down", "Prior", "Next", "PageUp", "PageDown", "Up", "Down", "Left", "Right",
    "space", "Menu", "Print", "Pause", "Break", "Scroll_Lock", "Num_Lock", "Caps_Lock", "minus", "plus", "equal",
    "comma", "period", "slash", "backslash", "semicolon", "apostrophe", "grave", "bracketleft", "bracketright",
    "asciitilde", "underscore", "question", "exclam", "at", "numbersign", "dollar", "percent", "asciicircum",
    "ampersand", "asterisk", "parenleft", "parenright", "colon", "quotedbl", "less", "greater", "bar",
    "braceleft", "braceright",
    *MODIFIERS,
))


def _is_keysym(token: str) -> bool:
    t = token.lower()
    if len(token) == 1:
        return token.isprintable() and not token.isspace()
    if t in KEYSYMS or t.startswith("xf86") or t.startswith("kp_"):
        return True
    return t[0] == "f" and t[1:].isdigit() and 1 <= int(t[1:]) <= 35


def is_key_chord(keys: str) -> bool:
    """True for what ``key`` is for: one chord (``ctrl+s``, ``Return``, ``ctrl+shift+t``) or a space-separated
    run of chords (``ctrl+a Delete``). Text such as ``~/lisbon.txt`` or ``hello world`` is not one."""
    tokens = keys.split()
    if not tokens:
        return False
    for chord in tokens:
        parts = chord.split("+")
        if chord.endswith("+") and len(chord) > 1:  # "ctrl++" -> ctrl, "+"
            parts = parts[:-2] + ["+"]
        if not parts or any(not p for p in parts):
            return False
        *mods, last = parts
        if any(m.lower() not in MODIFIERS for m in mods) or not _is_keysym(last):
            return False
        if len(tokens) > 1 and len(parts) == 1 and len(last) == 1:
            return False  # "a b c": letters typed one by one are text
    return True


def _accepts(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _parameters(fn: Any) -> list[str]:
    """Names of ``fn``'s positional-or-keyword / keyword-only parameters, in order (empty when unknown)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return []
    kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return [p.name for p in sig.parameters.values() if p.kind in kinds]


def _check_args(fn_name: str, accepted: list[str], args: dict[str, Any], fn: Any) -> None:
    """Structured failure for argument keys the function does not accept (unless it takes ``**kwargs``)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return
    unknown = sorted(k for k in args if k not in accepted)
    required = [p.name for p in sig.parameters.values()
                if p.name in accepted and p.default is inspect.Parameter.empty
                and p.kind is not inspect.Parameter.VAR_POSITIONAL]
    missing = [k for k in required if k not in args]
    if unknown or missing:
        parts = []
        if unknown:
            parts.append(f"unknown {', '.join(repr(k) for k in unknown)}")
        if missing:
            parts.append(f"missing {', '.join(repr(k) for k in missing)}")
        raise _StepFailure(f"call {fn_name!r}: bad arguments: {'; '.join(parts)}; accepted ({', '.join(accepted)})")


def _listed_functions(store: Any, app: str) -> set[str] | None:
    read = getattr(store, "read", None)
    if not callable(read):
        return None
    try:
        meta = read(app)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(meta, dict):
        return None
    return {str(f.get("name")) for f in (meta.get("functions") or []) if isinstance(f, dict)}


def _display_name(display: Any) -> str | None:
    if display is None:
        return None
    if isinstance(display, str):
        return display or None
    for attr in ("name", "display"):
        val = getattr(display, attr, None)
        if isinstance(val, str) and val:
            return val
    return None


def _sha_paths(script: OScript) -> list[str]:
    out: list[str] = []
    for step in script.steps:
        if isinstance(step, StateAssertStep):
            for c in step.checks:
                if c.kind == "file_sha_changed" and c.arg not in out:
                    out.append(c.arg)
    return out


def _bbox_ok(bbox: Any) -> bool:
    if not isinstance(bbox, dict):
        return False
    try:
        return all(isinstance(bbox[k], (int, float)) and not isinstance(bbox[k], bool) for k in ("x", "y", "w", "h"))
    except KeyError:
        return False


def _verdict(v: Any) -> tuple[bool, str]:
    """Accept (ok, reason), an object with .ok/.reason, a dict {ok, reason}, or a bare bool."""
    if isinstance(v, tuple) and len(v) == 2:
        return bool(v[0]), str(v[1] or "")
    if isinstance(v, dict):
        return bool(v.get("ok")), str(v.get("reason") or "")
    if hasattr(v, "ok"):
        return bool(v.ok), str(getattr(v, "reason", "") or "")
    return bool(v), ""


def _step_digest(step: Any) -> str:
    payload = step.model_dump(by_alias=True, exclude_none=True)
    compact = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _describe(step: Any) -> str:
    if isinstance(step, LaunchStep):
        return step.cmd
    if isinstance(step, WaitStep):
        return f"for {step.for_.describe()} ≤{step.timeout_s}s"
    if isinstance(step, FindStep):
        return f"as {step.as_}: {step.target.describe()}"
    if isinstance(step, (ClickStep, SetValueStep, InvokeStep)):
        extra = getattr(step, "action", None) or ("value" if isinstance(step, SetValueStep) else "")
        return f"ref {step.ref}" + (f" {extra}" if extra else "")
    if isinstance(step, TypeStep):
        return f"ref {step.ref} ({len(step.text)} chars)" if step.ref else f"({len(step.text)} chars)"
    if isinstance(step, KeyStep):
        return step.keys
    if isinstance(step, AssertStep):
        return f"{step.target.describe()} is {step.state}"
    if isinstance(step, ScreenshotAssertStep):
        return step.question[:80]
    if isinstance(step, StateAssertStep):
        checks = ", ".join(f"{c.kind} {c.arg[:40]!r}" for c in step.checks[:5])
        more = f", +{len(step.checks) - 5}" if len(step.checks) > 5 else ""
        return f"{len(step.checks)} check(s): {checks}{more}"
    if isinstance(step, ScriptStep):
        return f"{step.script.language} {len(step.script.code)} chars in {step.script.target_directory}"
    if isinstance(step, CallStep):
        return f"{step.fn}({', '.join(step.args)})"
    return ""
