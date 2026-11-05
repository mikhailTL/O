"""Declarative streaming GenUI: frames, the closed component set, the per-surface store, emitters, "oui".

The engine never renders anything itself; it emits validated A2UI-style frames the renderer maps onto its
own DOM. Model text never becomes a frame without passing ``parse_frame``. ``Sandbox`` frames carry
engine-generated HTML/CSS/JS only and render in a sandboxed iframe on the panel side.

Wire contract (one JSON object per frame; ``a2ui`` is always ``"1"``)::

    {"a2ui":"1","type":"begin","surface":"job:<id>","title":"..."}
    {"a2ui":"1","type":"update","surface":"...","components":[{"id":"s0","component":{"Step":{...}}}, ...]}
    {"a2ui":"1","type":"data","surface":"...","path":"steps.0.status","value":"ok"}
    {"a2ui":"1","type":"sandbox","surface":"...","sandbox_id":"...","html":"...","css":"...","js":"..."}
    {"a2ui":"1","type":"end","surface":"...","ok":true}

Component set (closed): Column, Row, Card, Text, Step, Tree, Progress, KeyValue, Button, Preview. Button
actions (closed): open-log, open-file, retry, approve, deny. Anything else fails ``parse_frame`` with
``SchemaViolation``.

"oui" is the token-compressed form of the same frames (see ``compile``/``decode``)::

    @begin job:1 "Install deps"
    @update job:1
    Col#plan{s0 s1}
    Step#s0[l="npm install";st=run;p=0.4]
    @data job:1 steps.0.status ok
    @sandbox job:1 sb1 {"html":"...","css":"","js":""}
    @end job:1 true

One component per line, ``<Type>#<id>[<k>=<v>;...]{<child ids>}``; values are bare when they match
``[A-Za-z0-9_.:/@-]+`` and JSON-encoded otherwise; keys at their default are omitted.
``decode(compile(frames)) == frames`` exactly.
"""
from __future__ import annotations

import collections
import html as _html
import json
import queue
import re
import threading
import time
from typing import Annotated, Any, Iterable, Literal, Optional, Union

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, model_validator

from .schema import SchemaViolation

A2UI_VERSION = "1"
STORE_MAXLEN = 2000
FORWARD_TIMEOUT_S = 0.5
SURFACE_PREFIXES = ("chat:", "job:")

_TOKEN = r"[A-Za-z0-9_.:/@-]+"
_TOKEN_RE = re.compile(rf"^{_TOKEN}$")
_SURFACE_RE = re.compile(rf"^(chat|job):{_TOKEN}$")

StepStatus = Literal["pending", "running", "ok", "failed", "skipped"]
ButtonAction = Literal["open-log", "open-file", "retry", "approve", "deny"]
TextTone = Literal["muted", "normal", "error"]

_STRICT = ConfigDict(strict=True, frozen=True, extra="forbid")


# ---------------------------------------------------------------- components

class _Component(BaseModel):
    model_config = _STRICT


class Column(_Component):
    type: Literal["Column"] = "Column"
    children: list[str] = Field(default_factory=list)


class Row(_Component):
    type: Literal["Row"] = "Row"
    children: list[str] = Field(default_factory=list)


class Card(_Component):
    type: Literal["Card"] = "Card"
    title: str = ""
    children: list[str] = Field(default_factory=list)


class Text(_Component):
    type: Literal["Text"] = "Text"
    text: str
    tone: TextTone = "normal"


class Step(_Component):
    type: Literal["Step"] = "Step"
    label: str
    status: StepStatus = "pending"
    detail: str = ""
    progress: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class TreeNode(_Component):
    id: str = Field(pattern=_TOKEN_RE.pattern)
    label: str
    status: StepStatus = "pending"
    children: list["TreeNode"] = Field(default_factory=list)


class Tree(_Component):
    type: Literal["Tree"] = "Tree"
    nodes: list[TreeNode] = Field(default_factory=list)


class Progress(_Component):
    type: Literal["Progress"] = "Progress"
    value: float = Field(ge=0.0, le=1.0)
    label: str = ""


class KeyValue(_Component):
    type: Literal["KeyValue"] = "KeyValue"
    items: list[Annotated[list[str], Field(min_length=2, max_length=2)]] = Field(default_factory=list)


class Button(_Component):
    type: Literal["Button"] = "Button"
    label: str
    action: ButtonAction
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class Preview(_Component):
    type: Literal["Preview"] = "Preview"
    sandbox_id: str = Field(pattern=_TOKEN_RE.pattern)
    height: int = Field(default=240, ge=16, le=4000)


Component = Union[Column, Row, Card, Text, Step, Tree, Progress, KeyValue, Button, Preview]
COMPONENT_TYPES: dict[str, type[_Component]] = {
    c.model_fields["type"].default: c  # type: ignore[attr-defined]
    for c in (Column, Row, Card, Text, Step, Tree, Progress, KeyValue, Button, Preview)
}
_component_adapter: TypeAdapter = TypeAdapter(Component)


class ComponentEntry(BaseModel):
    """``{"id": "...", "component": {"<Type>": {...}}}`` on the wire; ``component`` is the typed model here."""

    model_config = _STRICT

    id: str = Field(pattern=_TOKEN_RE.pattern)
    component: Component = Field(discriminator="type")

    @model_validator(mode="before")
    @classmethod
    def _unwrap(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        comp = data.get("component")
        if isinstance(comp, dict) and "type" not in comp:
            if len(comp) != 1:
                raise ValueError("component must be {<Type>: {...}} with exactly one type")
            (kind, props), = comp.items()
            if kind not in COMPONENT_TYPES:
                raise ValueError(f"unknown component {kind!r}")
            if not isinstance(props, dict):
                raise ValueError(f"{kind} properties must be an object")
            data = {**data, "component": {"type": kind, **props}}
        return data

    def wire(self) -> dict[str, Any]:
        props = self.component.model_dump(mode="json", exclude={"type"}, exclude_none=True)
        return {"id": self.id, "component": {self.component.type: props}}


# ---------------------------------------------------------------- frames

class _Frame(BaseModel):
    model_config = _STRICT
    a2ui: Literal["1"] = A2UI_VERSION
    surface: str = Field(pattern=_SURFACE_RE.pattern)

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Begin(_Frame):
    type: Literal["begin"] = "begin"
    title: str = ""


class Update(_Frame):
    type: Literal["update"] = "update"
    components: list[ComponentEntry] = Field(min_length=1)

    def wire(self) -> dict[str, Any]:
        return {"a2ui": self.a2ui, "type": "update", "surface": self.surface,
                "components": [c.wire() for c in self.components]}


class Data(_Frame):
    type: Literal["data"] = "data"
    path: str = Field(pattern=_TOKEN_RE.pattern)
    value: JsonValue


class End(_Frame):
    type: Literal["end"] = "end"
    ok: bool


class Sandbox(_Frame):
    type: Literal["sandbox"] = "sandbox"
    sandbox_id: str = Field(pattern=_TOKEN_RE.pattern)
    html: str
    css: str = ""
    js: str = ""


Frame = Union[Begin, Update, Data, End, Sandbox]
_frame_adapter: TypeAdapter = TypeAdapter(Annotated[Frame, Field(discriminator="type")])


def parse_frame(raw: Any) -> Frame:
    """Validate one wire frame (dict or JSON text). Unknown component/property/action -> SchemaViolation."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise SchemaViolation(f"frame is not JSON: {e}") from None
    if isinstance(raw, _Frame):
        return raw  # type: ignore[return-value]
    if not isinstance(raw, dict):
        raise SchemaViolation("frame must be a JSON object")
    try:
        return _frame_adapter.validate_python(raw)
    except ValidationError as e:
        errs = e.errors()
        first = errs[0] if errs else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise SchemaViolation(f"invalid frame: {first.get('msg', 'validation error')} at {loc or 'root'}") from None


def frame_to_wire(frame: Frame) -> dict[str, Any]:
    return frame.wire()


def frame_to_json(frame: Frame) -> str:
    return json.dumps(frame.wire(), separators=(",", ":"), ensure_ascii=False)


def valid_surface(surface: str) -> bool:
    """``chat:<token>`` or ``job:<token>``; ``parse_frame`` enforces the same rule on every frame."""
    return isinstance(surface, str) and bool(_SURFACE_RE.match(surface))


# ---------------------------------------------------------------- store

class UIStore:
    """Per-surface ring buffer (``maxlen`` frames) with monotonically increasing per-surface sequence numbers
    and queue subscribers for SSE. Thread-safe: emitters run on job threads, readers on the server."""

    def __init__(self, maxlen: int = STORE_MAXLEN, forward_url: str | None = None, token: str | None = None) -> None:
        self.maxlen = int(maxlen)
        self.forward_url = forward_url or None
        self.token = token or ""
        self._lock = threading.Lock()
        self._frames: dict[str, collections.deque] = {}
        self._seq: dict[str, int] = {}
        self._ended: set[str] = set()
        self._subs: dict[str, list[queue.Queue]] = {}
        self._client = httpx.Client(timeout=FORWARD_TIMEOUT_S) if self.forward_url else None
        self._outbox: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._sender: Optional[threading.Thread] = None
        if self._client is not None:
            self._sender = threading.Thread(target=self._send_loop, name="o-ui-forward", daemon=True)
            self._sender.start()

    def append(self, frame: Frame) -> int:
        """Store a validated frame; returns its sequence number on that surface."""
        if not isinstance(frame, _Frame):
            frame = parse_frame(frame)
        with self._lock:
            surface = frame.surface
            buf = self._frames.get(surface)
            if buf is None:
                buf = self._frames[surface] = collections.deque(maxlen=self.maxlen)
            seq = self._seq.get(surface, 0) + 1
            self._seq[surface] = seq
            buf.append((seq, frame))
            if isinstance(frame, End):
                self._ended.add(surface)
            elif isinstance(frame, Begin):
                self._ended.discard(surface)
            subs = list(self._subs.get(surface, ()))
        for q in subs:
            q.put((seq, frame))
        self._forward(seq, frame)
        return seq

    def _forward(self, seq: int, frame: Frame) -> None:
        """Fire-and-forget push to the app (``POST O_UI_URL`` with ``{"frames": [frame]}`` and ``x-o-token``)
        from a daemon thread, in order; the job thread never waits. The store is the record."""
        if not self._client:
            return
        self._outbox.put({"frames": [frame.wire()]})

    def _send_loop(self) -> None:
        while True:
            body = self._outbox.get()
            if body is None:
                return
            try:
                self._client.post(self.forward_url, json=body, headers={"x-o-token": self.token})
            except Exception:
                pass
            finally:
                self._outbox.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait (bounded) for queued forwards to be sent; tests and shutdown use it."""
        if self._client is None:
            return
        deadline = time.monotonic() + timeout
        while not self._outbox.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        # the last item may still be in flight: give it the request timeout
        end = time.monotonic() + FORWARD_TIMEOUT_S
        while self._outbox.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.01)

    def entries(self, surface: str, since: int = 0) -> list[tuple[int, Frame]]:
        with self._lock:
            return [(s, f) for s, f in self._frames.get(surface, ()) if s > since]

    def frames(self, surface: str, since: int = 0) -> list[Frame]:
        return [f for _, f in self.entries(surface, since)]

    def last_seq(self, surface: str) -> int:
        with self._lock:
            return self._seq.get(surface, 0)

    def ended(self, surface: str) -> bool:
        with self._lock:
            return surface in self._ended

    def surfaces(self) -> list[str]:
        with self._lock:
            return list(self._frames.keys())

    def subscribe(self, surface: str) -> "queue.Queue[tuple[int, Frame]]":
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.setdefault(surface, []).append(q)
        return q

    def unsubscribe(self, surface: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subs.get(surface)
            if subs and q in subs:
                subs.remove(q)
            if subs is not None and not subs:
                self._subs.pop(surface, None)


# ---------------------------------------------------------------- emitters

_PREVIEW_CSS = (
    "body{margin:0;font:13px system-ui,sans-serif;color:#e6e6e6;background:#141414}"
    ".card{display:flex;gap:12px;align-items:center;padding:10px 14px;border:1px solid #2a2a2a;border-radius:8px;margin:8px}"
    ".ok{color:#7ed07e}.failed{color:#e07a7a}.muted{color:#8a8a8a}"
)


def status_card_html(title: str, ok: bool, elapsed_ms: int, status: Any = None) -> str:
    """The tiny engine-generated card a verify/judge Preview shows. All text is escaped; no model text."""
    cls = "ok" if ok else "failed"
    word = "ok" if ok else "failed"
    extra = f' <span class="muted">status {_html.escape(str(status))}</span>' if status not in (None, "") else ""
    return (f'<div class="card"><b>{_html.escape(str(title))}</b>'
            f'<span class="{cls}">{word}</span>{extra}'
            f'<span class="muted">{int(elapsed_ms)} ms</span></div>')


class JobSurface:
    """Emitter for one job's surface ``job:<id>``: phase tree, per-step Step components + data patches,
    an optional Preview sandbox, and begin/end."""

    def __init__(self, store: UIStore, job_id: str, title: str, phases: Iterable[str] = ()) -> None:
        self.store = store
        self.job_id = str(job_id)
        self.surface = f"job:{self.job_id}"
        self.title = str(title)
        self.phases = [str(p) for p in phases]
        self._steps: list[str] = []
        self._previews: list[str] = []
        self._visited: list[str] = []  # phases in the order ``phase()`` was called (revisits repeat)
        self._failed_phase: Optional[str] = None  # the phase during which a step last failed
        self._begun = False
        self._ended = False
        self._lock = threading.Lock()

    # -- helpers
    def _emit(self, frame: Frame) -> None:
        self.store.append(frame)

    def _root(self) -> ComponentEntry:
        children = ["phases", "current", "steps"] + list(self._previews)
        return ComponentEntry(id="root", component=Column(children=children))

    def _steps_col(self) -> ComponentEntry:
        return ComponentEntry(id="steps", component=Column(children=list(self._steps)))

    # -- API
    def begin(self) -> None:
        with self._lock:
            if self._begun:
                return
            self._begun = True
        self._emit(Begin(surface=self.surface, title=self.title))
        self._emit(Update(surface=self.surface, components=[
            self._root(),
            ComponentEntry(id="phases", component=Tree(nodes=[TreeNode(id=p, label=p) for p in self.phases])),
            ComponentEntry(id="current", component=Step(label="starting", status="pending")),
            self._steps_col(),
        ]))

    def _tree(self, current: Optional[str], ok: Optional[bool]) -> ComponentEntry:
        """The phase Tree. While running: ``current`` running, phases visited earlier ok, the rest pending.
        At the end (``ok`` given): visited ok, on failure the last visited failed, never visited skipped."""
        phase = str(current) if current is not None else None
        visited = set(self._visited)
        nodes = []
        for p in self.phases:
            st: StepStatus
            if ok is None:
                st = "running" if p == phase else "ok" if p in visited else "pending"
            elif p not in visited:
                st = "skipped"
            elif not ok and p == (self._failed_phase or (self._visited[-1] if self._visited else None)):
                st = "failed"
            else:
                st = "ok"
            nodes.append(TreeNode(id=p, label=p, status=st))
        return ComponentEntry(id="phases", component=Tree(nodes=nodes))

    def phase(self, phase: str, attempt: int = 0) -> None:
        if not self._begun:
            self.begin()
        phase = str(phase)
        with self._lock:
            self._visited.append(phase)
        self._emit(Update(surface=self.surface, components=[
            self._tree(phase, None),
            ComponentEntry(id="current", component=Step(label=str(phase), status="running",
                                                        detail=f"attempt {int(attempt)}" if attempt else "")),
        ]))

    def step(self, index: int, label: str, status: StepStatus, detail: str = "",
             progress: Optional[float] = None) -> None:
        if not self._begun:
            self.begin()
        sid = f"step-{int(index)}"
        if status == "failed" and self._visited:
            self._failed_phase = self._visited[-1]
        with self._lock:
            new = sid not in self._steps
            if new:
                self._steps.append(sid)
        if new or status == "running":
            comps = [ComponentEntry(id=sid, component=Step(label=str(label), status=status, detail=str(detail),
                                                           progress=progress))]
            if new:
                comps.append(self._steps_col())
            self._emit(Update(surface=self.surface, components=comps))
        self._emit(Data(surface=self.surface, path=f"steps.{int(index)}.status", value=status))
        if detail:
            self._emit(Data(surface=self.surface, path=f"steps.{int(index)}.detail", value=str(detail)))
        if progress is not None:
            self._emit(Data(surface=self.surface, path=f"steps.{int(index)}.progress", value=float(progress)))

    def preview(self, sandbox_id: str, html: str, css: str = "", js: str = "", height: int = 96) -> None:
        if not self._begun:
            self.begin()
        pid = f"preview-{sandbox_id}"
        self._emit(Sandbox(surface=self.surface, sandbox_id=str(sandbox_id), html=str(html), css=str(css), js=str(js)))
        with self._lock:
            if pid not in self._previews:
                self._previews.append(pid)
        self._emit(Update(surface=self.surface, components=[
            ComponentEntry(id=pid, component=Preview(sandbox_id=str(sandbox_id), height=int(height))),
            self._root(),
        ]))

    def end(self, ok: bool) -> None:
        with self._lock:
            if self._ended:
                return
            self._ended = True
        if not self._begun:
            self.begin()
        # One last Tree before End so no phase is left "running": visited -> ok, on failure the last
        # visited -> failed, never visited (e.g. remediate on a clean run) -> skipped.
        self._emit(Update(surface=self.surface, components=[
            self._tree(None, bool(ok)),
            ComponentEntry(id="current", component=Step(label="done" if ok else "failed", status="ok" if ok else "failed")),
        ]))
        self._emit(End(surface=self.surface, ok=bool(ok)))


# -- graph hook helpers (the graphs call these; nothing happens when ``ui`` is None)

def job_surface(ui: Any, tracker: dict, job_id: str, title: str, phases: Iterable[str]) -> Optional[JobSurface]:
    """The tracker-scoped JobSurface for a run; created (and ``begin`` emitted) on first use."""
    if ui is None:
        return None
    surface = tracker.get("ui")
    if surface is None:
        surface = JobSurface(ui, job_id, title, phases)
        tracker["ui"] = surface
        surface.begin()
    return surface


def ui_phase(tracker: dict, phase: str, attempt: int = 0) -> None:
    surface = tracker.get("ui")
    if surface is not None:
        try:
            surface.phase(phase, attempt)
        except Exception:
            pass


def ui_step(tracker: dict, index: int, label: str, status: StepStatus, detail: str = "",
            progress: Optional[float] = None) -> None:
    surface = tracker.get("ui")
    if surface is not None:
        try:
            surface.step(index, label, status, detail, progress)
        except Exception:
            pass


def ui_preview_status(tracker: dict, title: str, ok: bool, elapsed_ms: int, status: Any = None) -> None:
    surface = tracker.get("ui")
    if surface is not None:
        try:
            sid = f"verify-{surface.job_id}"
            surface.preview(sid, status_card_html(title, ok, elapsed_ms, status), _PREVIEW_CSS, "", height=64)
        except Exception:
            pass


def ui_end(tracker: dict, ok: bool) -> None:
    surface = tracker.get("ui")
    if surface is not None:
        try:
            surface.end(ok)
        except Exception:
            pass


# ---------------------------------------------------------------- oui

class oui:
    """Token-compressed form. ``compile(frames) -> str``; ``decode(text) -> list[Frame]``."""

    TYPES: dict[str, str] = {"Column": "Col", "Row": "Row", "Card": "Card", "Text": "Txt", "Step": "Step",
                             "Tree": "Tree", "Progress": "Prog", "KeyValue": "KV", "Button": "Btn", "Preview": "Prev"}
    SHORT_TO_TYPE: dict[str, str] = {v: k for k, v in TYPES.items()}
    # per component: field -> short key (children go in {...}, never in [...])
    KEYS: dict[str, dict[str, str]] = {
        "Column": {}, "Row": {},
        "Card": {"title": "t"},
        "Text": {"text": "t", "tone": "tn"},
        "Step": {"label": "l", "status": "st", "detail": "d", "progress": "p"},
        "Tree": {"nodes": "n"},
        "Progress": {"value": "v", "label": "l"},
        "KeyValue": {"items": "i"},
        "Button": {"label": "l", "action": "a", "payload": "pl"},
        "Preview": {"sandbox_id": "s", "height": "h"},
    }
    STATUS: dict[str, str] = {"pending": "pend", "running": "run", "ok": "ok", "failed": "fail", "skipped": "skip"}
    STATUS_BACK: dict[str, str] = {v: k for k, v in STATUS.items()}

    # -- value encoding
    @staticmethod
    def _enc(value: Any) -> str:
        if isinstance(value, str) and _TOKEN_RE.match(value):
            return value
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _dec(token: str, as_str: bool) -> Any:
        if token[:1] in ('"', "[", "{") or not as_str:
            try:
                return json.loads(token)
            except ValueError:
                raise SchemaViolation(f"oui: bad value {token!r}") from None
        return token

    @classmethod
    def _component_line(cls, entry: ComponentEntry) -> str:
        comp = entry.component
        kind = comp.type
        parts: list[str] = []
        for field_name, short in cls.KEYS[kind].items():
            value = getattr(comp, field_name)
            info = type(comp).model_fields[field_name]
            if not info.is_required() and value == info.get_default(call_default_factory=True):
                continue
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json")
            elif isinstance(value, list) and value and hasattr(value[0], "model_dump"):
                value = [v.model_dump(mode="json") for v in value]
            if kind == "Step" and field_name == "status":
                value = cls.STATUS[value]
            parts.append(f"{short}={cls._enc(value)}")
        line = f"{cls.TYPES[kind]}#{entry.id}"
        if parts:
            line += "[" + ";".join(parts) + "]"
        children = getattr(comp, "children", None)
        if children:
            line += "{" + " ".join(children) + "}"
        return line

    @classmethod
    def compile(cls, frames: Iterable[Frame]) -> str:
        lines: list[str] = []
        for frame in frames:
            if not isinstance(frame, _Frame):
                frame = parse_frame(frame)
            if isinstance(frame, Begin):
                lines.append(f"@begin {frame.surface} {cls._enc(frame.title)}")
            elif isinstance(frame, Update):
                lines.append(f"@update {frame.surface}")
                lines.extend(cls._component_line(c) for c in frame.components)
            elif isinstance(frame, Data):
                lines.append(f"@data {frame.surface} {frame.path} {json.dumps(frame.value, separators=(',', ':'), ensure_ascii=False)}")
            elif isinstance(frame, End):
                lines.append(f"@end {frame.surface} {'true' if frame.ok else 'false'}")
            elif isinstance(frame, Sandbox):
                body = json.dumps({"html": frame.html, "css": frame.css, "js": frame.js}, separators=(",", ":"), ensure_ascii=False)
                lines.append(f"@sandbox {frame.surface} {frame.sandbox_id} {body}")
        return "\n".join(lines) + ("\n" if lines else "")

    # -- decoding
    @staticmethod
    def _scan_value(text: str, pos: int) -> tuple[str, int]:
        """Return (raw token, next pos) for a value starting at ``pos`` inside ``[...]``."""
        if pos >= len(text):
            raise SchemaViolation("oui: missing value")
        ch = text[pos]
        if ch == '"':
            i = pos + 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    return text[pos:i + 1], i + 1
                i += 1
            raise SchemaViolation("oui: unterminated string")
        if ch in "[{":
            try:
                _, end = json.JSONDecoder().raw_decode(text, pos)
            except ValueError:
                raise SchemaViolation("oui: bad JSON value") from None
            return text[pos:end], end
        i = pos
        while i < len(text) and text[i] not in ";]":
            i += 1
        return text[pos:i], i

    @classmethod
    def _parse_component(cls, line: str) -> ComponentEntry:
        m = re.match(rf"^([A-Za-z]+)#({_TOKEN})", line)
        if not m:
            raise SchemaViolation(f"oui: bad component line {line!r}")
        short, cid = m.group(1), m.group(2)
        kind = cls.SHORT_TO_TYPE.get(short)
        if kind is None:
            raise SchemaViolation(f"oui: unknown component {short!r}")
        pos = m.end()
        props: dict[str, Any] = {}
        back = {v: k for k, v in cls.KEYS[kind].items()}
        model = COMPONENT_TYPES[kind]
        if pos < len(line) and line[pos] == "[":
            pos += 1
            while True:
                eq = line.find("=", pos)
                if eq < 0:
                    raise SchemaViolation(f"oui: bad property in {line!r}")
                key = line[pos:eq].strip()
                field_name = back.get(key)
                if field_name is None:
                    raise SchemaViolation(f"oui: unknown property {key!r} for {kind}")
                raw, pos = cls._scan_value(line, eq + 1)
                ann = model.model_fields[field_name].annotation
                as_str = ann is str or (kind == "Step" and field_name == "status") or field_name in ("tone", "action")
                value = cls._dec(raw, as_str)
                if kind == "Step" and field_name == "status" and isinstance(value, str):
                    value = cls.STATUS_BACK.get(value, value)
                props[field_name] = value
                if pos >= len(line):
                    raise SchemaViolation(f"oui: unterminated [ in {line!r}")
                if line[pos] == ";":
                    pos += 1
                    continue
                if line[pos] == "]":
                    pos += 1
                    break
                raise SchemaViolation(f"oui: unexpected {line[pos]!r} in {line!r}")
        if pos < len(line) and line[pos] == "{":
            end = line.find("}", pos)
            if end < 0:
                raise SchemaViolation(f"oui: unterminated {{ in {line!r}")
            props["children"] = line[pos + 1:end].split()
            pos = end + 1
        if line[pos:].strip():
            raise SchemaViolation(f"oui: trailing text in {line!r}")
        try:
            return ComponentEntry.model_validate({"id": cid, "component": {kind: props}})
        except ValidationError as e:
            raise SchemaViolation(f"oui: invalid {kind}#{cid}: {e.errors()[0].get('msg')}") from None

    @classmethod
    def decode(cls, text: str) -> list[Frame]:
        frames: list[Frame] = []
        pending: Optional[tuple[str, list[ComponentEntry]]] = None

        def flush() -> None:
            nonlocal pending
            if pending is not None:
                surface, comps = pending
                if not comps:
                    raise SchemaViolation("oui: @update with no components")
                frames.append(parse_frame({"a2ui": A2UI_VERSION, "type": "update", "surface": surface,
                                           "components": [c.wire() for c in comps]}))
                pending = None

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("@"):
                flush()
                head, _, rest = line.partition(" ")
                if head == "@update":
                    pending = (rest.strip(), [])
                    continue
                surface, _, rest = rest.strip().partition(" ")
                if head == "@begin":
                    frames.append(parse_frame({"a2ui": A2UI_VERSION, "type": "begin", "surface": surface,
                                               "title": cls._dec(rest.strip(), True) if rest.strip() else ""}))
                elif head == "@data":
                    path, _, value = rest.strip().partition(" ")
                    try:
                        val = json.loads(value)
                    except ValueError:
                        raise SchemaViolation(f"oui: bad @data value {value!r}") from None
                    frames.append(parse_frame({"a2ui": A2UI_VERSION, "type": "data", "surface": surface, "path": path, "value": val}))
                elif head == "@end":
                    ok = rest.strip()
                    if ok not in ("true", "false"):
                        raise SchemaViolation("oui: @end needs true|false")
                    frames.append(parse_frame({"a2ui": A2UI_VERSION, "type": "end", "surface": surface, "ok": ok == "true"}))
                elif head == "@sandbox":
                    sid, _, body = rest.strip().partition(" ")
                    try:
                        obj = json.loads(body)
                    except ValueError:
                        raise SchemaViolation("oui: bad @sandbox body") from None
                    if not isinstance(obj, dict):
                        raise SchemaViolation("oui: @sandbox body must be an object")
                    frames.append(parse_frame({"a2ui": A2UI_VERSION, "type": "sandbox", "surface": surface, "sandbox_id": sid,
                                               "html": obj.get("html", ""), "css": obj.get("css", ""), "js": obj.get("js", "")}))
                else:
                    raise SchemaViolation(f"oui: unknown directive {head!r}")
                continue
            if pending is None:
                raise SchemaViolation(f"oui: component line outside @update: {line!r}")
            pending[1].append(cls._parse_component(line))
        flush()
        return frames


compile = oui.compile  # noqa: A001 - module-level aliases per the plan (``oui.compile``/``oui.decode``)
decode = oui.decode

__all__ = [
    "A2UI_VERSION", "Begin", "Button", "ButtonAction", "Card", "Column", "Component", "ComponentEntry", "Data", "End",
    "Frame", "JobSurface", "KeyValue", "Preview", "Progress", "Row", "Sandbox", "Step", "StepStatus", "Text", "Tree",
    "TreeNode", "UIStore", "Update", "compile", "decode", "frame_to_json", "frame_to_wire", "job_surface", "oui",
    "parse_frame", "status_card_html", "ui_end", "ui_phase", "ui_preview_status", "ui_step", "valid_surface",
]
