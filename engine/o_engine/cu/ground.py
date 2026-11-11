"""Vision grounding: screenshot + target text -> bounding box.

``Grounder`` orders backends: primary VLM -> fallback (Claude Code CLI) once ->
``None``. ``LocatorCache`` (keyed by app, target text and the screenshot hash) is
consulted and filled by the executor, not here. It is append-only: a key is
written once and never updated; a changed screen produces a new key.

Zoom-in grounding (plan I-B / research P3): ``ZoomGrounder`` wraps any backend,
grounds once on the full screenshot, crops around that centre, grounds again on
the crop and remaps. The second answer's distance from the crop centre is the
consistency signal; ``confidence = 1 - dist/crop_diag``. ``Grounder`` gates on
it: a result below ``min_confidence`` is discarded (so the executor never writes
it to the cache) and the next backend runs. ``MultiCropVote`` repeats the zoom
over ``k`` shifted crops and keeps the mode of the rounded centres.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import sqlite3
import struct
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..llm import claude_cli, cli_command
from ..schema import SchemaViolation
from ..tokenfilter import TokenFilter
from . import meter

LOCAL_TIMEOUT_SECONDS = 8.0
CLAUDE_TIMEOUT_SECONDS = 120

GROUND_SYSTEM_PROMPT = (
    "You locate user-interface elements in screenshots.\n"
    "Given a screenshot and a description of one element, answer with ONLY a JSON object:\n"
    '{"x": <int>, "y": <int>, "w": <int>, "h": <int>}\n'
    "x,y is the top-left corner and w,h the size of the element's bounding box, in image pixels.\n"
    "No prose, no markdown, no code fences, no other keys. If the element is absent, answer {}."
)


def build_ground_prompt(target_text: str) -> str:
    return f"Element to locate: {target_text}\nJSON bounding box:"


class GroundResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = ""

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)


class _BBox(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=1)
    h: int = Field(ge=1)


def parse_bbox(text: str, source: str) -> GroundResult | None:
    """Extract the first JSON object from model text and validate it as a bbox.

    ``{}`` (element absent) returns ``None``; anything malformed raises ``SchemaViolation``.
    """
    tf = TokenFilter()
    filtered = tf.feed(text or "")
    if not filtered:
        raise SchemaViolation("grounder produced no JSON object")
    try:
        obj = json.loads(filtered)
    except json.JSONDecodeError as e:
        raise SchemaViolation(f"grounder output is not JSON: {e.msg}") from None
    if not isinstance(obj, dict):
        raise SchemaViolation("grounder output must be a JSON object")
    if not obj:
        return None
    try:
        box = _BBox.model_validate(obj)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise SchemaViolation(problems) from None
    return GroundResult(x=box.x, y=box.y, w=box.w, h=box.h, confidence=1.0, source=source)


def screen_sha(png_bytes: bytes) -> str:
    return hashlib.sha256(png_bytes).hexdigest()


def locator_key(app: str, target_text: str, screen_hash: str) -> str:
    return hashlib.sha256(f"{app}|{target_text}|{screen_hash}".encode("utf-8")).hexdigest()


def png_data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


class GrounderBackend(Protocol):
    name: str

    def ground(self, png_bytes: bytes, target_text: str) -> GroundResult | None: ...


class VisionGrounder:
    """Asks a local OpenAI-compatible vision model for the element's bbox."""

    name = "vlm"

    def __init__(self, vllm_url: str, model: str, timeout: float = LOCAL_TIMEOUT_SECONDS) -> None:
        self.base_url = vllm_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.client = OpenAI(base_url=self.base_url, api_key="local", timeout=timeout, max_retries=0)

    def _messages(self, png_bytes: bytes, target_text: str) -> list[dict]:
        return [
            {"role": "system", "content": GROUND_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": png_data_url(png_bytes)}},
                    {"type": "text", "text": build_ground_prompt(target_text)},
                ],
            },
        ]

    def _tokens(self, png_bytes: bytes, target_text: str):
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=self._messages(png_bytes, target_text),
            stream=True,
            temperature=0,
        )
        deadline = time.monotonic() + self.timeout
        try:
            for chunk in stream:
                if time.monotonic() > deadline:
                    raise SchemaViolation(f"vision model timed out after {self.timeout:g} s")
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

    def ground(self, png_bytes: bytes, target_text: str) -> GroundResult | None:
        meter.count_local("grounder")
        tf = TokenFilter()
        filtered = tf.filter_stream(self._tokens(png_bytes, target_text))
        if not filtered:
            raise SchemaViolation("vision model produced no JSON object")
        return parse_bbox(filtered, self.name)


class ClaudeCodeGrounder:
    """Fallback: writes the screenshot to a temp file and runs `claude -p --output-format json`."""

    name = "claude"

    def __init__(self, claude_path: str = "claude", timeout: float = CLAUDE_TIMEOUT_SECONDS) -> None:
        self.claude_path = claude_path
        self.timeout = timeout

    def _command(self) -> list[str]:
        return cli_command(self.claude_path)

    def ground(self, png_bytes: bytes, target_text: str) -> GroundResult | None:
        # The PNG goes in a fresh directory that is also the CLI's working directory: `claude -p` reads files in
        # its working directory without a permission prompt, but a Read of a /tmp file outside it is denied in
        # headless mode (real run: every screenshot check failed with "permission denied"). A file read pins the
        # call to the CLI (the proxy carries text only).
        meter.charge_paid("grounder")
        workdir = tempfile.mkdtemp(prefix="o-ground-")
        path = os.path.join(workdir, "screen.png")
        try:
            with open(path, "wb") as f:
                f.write(png_bytes)
            prompt = (
                GROUND_SYSTEM_PROMPT
                + f"\n\nThe screenshot is the PNG file at: {path}\nRead that image.\n\n"
                + build_ground_prompt(target_text)
            )
            payload = claude_cli(prompt, claude_path=self.claude_path, timeout=self.timeout, cwd=workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        meter.add_cost(payload)
        return parse_bbox(payload["result"], self.name)


# ---------------------------------------------------------------- zoom-in grounding

CropBox = tuple[int, int, int, int]  # left, top, right, bottom (right/bottom exclusive)
CropFn = Callable[[bytes, CropBox], bytes]
SizeFn = Callable[[bytes], tuple[int, int]]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(png_bytes: bytes) -> tuple[int, int]:
    """(width, height) from the IHDR chunk; pure Python, no decoder needed."""
    if len(png_bytes) < 24 or not png_bytes.startswith(_PNG_SIGNATURE) or png_bytes[12:16] != b"IHDR":
        raise SchemaViolation("screenshot is not a PNG image")
    w, h = struct.unpack(">II", png_bytes[16:24])
    if w < 1 or h < 1:
        raise SchemaViolation("PNG has no pixels")
    return int(w), int(h)


def crop_png(png_bytes: bytes, box: CropBox) -> bytes:
    """Default ``CropFn``: crop with Pillow (a declared dependency, ``pillow>=10`` in pyproject).

    ``ZoomGrounder``/``MultiCropVote`` take ``crop_fn``/``size_fn`` for callers that decode
    screenshots some other way; this is only the default.
    """
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as im:
        out = io.BytesIO()
        im.crop(box).save(out, format="PNG")
    return out.getvalue()


def _crop_window(centre: tuple[float, float], size: tuple[int, int], scale: float,
                 shift: tuple[float, float] = (0.0, 0.0)) -> CropBox:
    """A ``scale``-sized window around ``centre`` (shifted by ``shift`` crop fractions), clamped to the image."""
    width, height = size
    cw = max(1, min(width, int(round(width * scale))))
    ch = max(1, min(height, int(round(height * scale))))
    left = int(round(centre[0] - cw / 2 + shift[0] * cw))
    top = int(round(centre[1] - ch / 2 + shift[1] * ch))
    left = min(max(left, 0), width - cw)
    top = min(max(top, 0), height - ch)
    return (left, top, left + cw, top + ch)


def _remap(step2: GroundResult, box: CropBox, size: tuple[int, int], source: str, confidence: float) -> GroundResult:
    width, height = size
    x = min(max(step2.x + box[0], 0), width - 1)
    y = min(max(step2.y + box[1], 0), height - 1)
    w = max(1, min(step2.w, width - x))
    h = max(1, min(step2.h, height - y))
    return GroundResult(x=x, y=y, w=w, h=h, confidence=min(max(confidence, 0.0), 1.0), source=source)


def zoom_consistency(step2: GroundResult, box: CropBox) -> float:
    """Distance of the step-2 centre (crop coordinates) from the crop centre, over the crop half-diagonal.

    0 = step 2 agrees exactly with step 1; 1 = step 2 sits in a crop corner (or further out).
    The half-diagonal (not the full diagonal) is the normaliser so that an in-crop answer can
    span the whole 0..1 range; with the full diagonal nothing inside the crop could fall below 0.5
    and the 0.3 gate would never fire.
    """
    cw, ch = box[2] - box[0], box[3] - box[1]
    cx, cy = step2.center
    dist = math.hypot(cx - cw / 2, cy - ch / 2)
    half_diag = (math.hypot(cw, ch) / 2) or 1.0
    return min(max(dist / half_diag, 0.0), 1.0)


class _ZoomBase:
    def __init__(self, inner: GrounderBackend, scale: float = 0.5,
                 crop_fn: CropFn | None = None, size_fn: SizeFn | None = None) -> None:
        if not 0.0 < scale <= 1.0:
            raise ValueError("scale must be in (0, 1]")
        self.inner = inner
        self.scale = scale
        self.crop_fn: CropFn = crop_fn or crop_png
        self.size_fn: SizeFn = size_fn or png_size

    def _zoom(self, png_bytes: bytes, target_text: str, step1: GroundResult, size: tuple[int, int],
              shift: tuple[float, float] = (0.0, 0.0)) -> tuple[GroundResult, CropBox, float]:
        """Crop around step 1, ground on the crop, remap. Returns (result, crop box, consistency)."""
        box = _crop_window(step1.center, size, self.scale, shift)
        step2 = self.inner.ground(self.crop_fn(png_bytes, box), target_text)
        if step2 is None:
            # the element vanished in the zoom: keep step 1 but call it fully inconsistent
            return GroundResult(x=step1.x, y=step1.y, w=step1.w, h=step1.h, confidence=0.0, source=self.name), box, 1.0
        consistency = zoom_consistency(step2, box)
        confidence = min(1.0 - consistency, step1.confidence, step2.confidence)
        return _remap(step2, box, size, self.name, confidence), box, consistency


class ZoomGrounder(_ZoomBase):
    """Two-step grounding: full screenshot -> crop ``scale`` around the answer -> ground again -> remap.

    ``confidence = 1 - dist(step2 centre, crop centre) / (crop diagonal / 2)`` (clamped to 0..1,
    never above what ``inner`` itself reported). ``last_consistency`` keeps the raw distance ratio.
    """

    name = "zoom"

    def __init__(self, inner: GrounderBackend, scale: float = 0.5,
                 crop_fn: CropFn | None = None, size_fn: SizeFn | None = None) -> None:
        super().__init__(inner, scale, crop_fn, size_fn)
        self.last_consistency: float | None = None
        self.last_crop: CropBox | None = None

    def ground(self, png_bytes: bytes, target_text: str) -> GroundResult | None:
        self.last_consistency = None
        self.last_crop = None
        step1 = self.inner.ground(png_bytes, target_text)
        if step1 is None:
            return None
        size = self.size_fn(png_bytes)
        result, box, consistency = self._zoom(png_bytes, target_text, step1, size)
        self.last_consistency = consistency
        self.last_crop = box
        return result


class MultiCropVote(_ZoomBase):
    """Zoom over ``k`` crops shifted by ``shift`` (fraction of the crop) and keep the mode.

    Centres are rounded to a ``grid``-pixel lattice before voting; ties go to the earliest
    crop. ``confidence = votes/k * mean zoom confidence of the agreeing crops``.
    """

    name = "vote"
    _SHIFTS = ((0.0, 0.0), (1.0, 1.0), (-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0))

    def __init__(self, inner: GrounderBackend, k: int = 3, shift: float = 0.1, scale: float = 0.5,
                 grid: int = 8, crop_fn: CropFn | None = None, size_fn: SizeFn | None = None) -> None:
        super().__init__(inner, scale, crop_fn, size_fn)
        if k < 1:
            raise ValueError("k must be >= 1")
        self.k = k
        self.shift = shift
        self.grid = max(1, int(grid))
        self.last_votes: int = 0
        self.last_candidates: list[GroundResult] = []

    def _shifts(self) -> list[tuple[float, float]]:
        out = []
        for i in range(self.k):
            base = self._SHIFTS[i % len(self._SHIFTS)]
            ring = 1 + i // len(self._SHIFTS)
            out.append((base[0] * self.shift * ring, base[1] * self.shift * ring))
        return out

    def ground(self, png_bytes: bytes, target_text: str) -> GroundResult | None:
        self.last_votes = 0
        self.last_candidates = []
        step1 = self.inner.ground(png_bytes, target_text)
        if step1 is None:
            return None
        size = self.size_fn(png_bytes)
        candidates = [self._zoom(png_bytes, target_text, step1, size, shift)[0] for shift in self._shifts()]
        self.last_candidates = candidates
        keys = [(round(c.center[0] / self.grid), round(c.center[1] / self.grid)) for c in candidates]
        mode_key, votes = Counter(keys).most_common(1)[0]
        agreeing = [c for c, key in zip(candidates, keys) if key == mode_key]
        self.last_votes = votes
        winner = agreeing[0]
        confidence = (votes / self.k) * (sum(c.confidence for c in agreeing) / len(agreeing))
        return GroundResult(x=winner.x, y=winner.y, w=winner.w, h=winner.h,
                            confidence=min(max(confidence, 0.0), 1.0), source=self.name)


def target_text(target: Any) -> str:
    """Stable text for a target: ``Target.describe()`` when available, else the plain string."""
    if isinstance(target, str):
        return target
    describe = getattr(target, "describe", None)
    if callable(describe):
        return str(describe())
    if isinstance(target, dict):
        return " ".join(f"{k}={v!r}" for k, v in sorted(target.items()) if v is not None)
    return str(target)


class LocatorCache:
    """SQLite table of grounded boxes keyed by (app, target text, screen hash).

    ``put`` is INSERT OR IGNORE: a key is written once; nothing is updated or removed.
    The executor owns lookups (resolved_by == 'locator'); ``Grounder`` never reads it.
    """

    def __init__(self, sqlite_path: str | os.PathLike[str]) -> None:
        self.path = Path(sqlite_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS locators ("
            " key TEXT PRIMARY KEY, app TEXT NOT NULL, target TEXT NOT NULL, screen_sha TEXT NOT NULL,"
            " x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL,"
            " confidence REAL NOT NULL DEFAULT 1.0, source TEXT NOT NULL DEFAULT '', created REAL NOT NULL)"
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Additive only: older files lacking ``confidence``/``source`` get the columns with defaults."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(locators)")}
        if "confidence" not in cols:
            self._conn.execute("ALTER TABLE locators ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0")
        if "source" not in cols:
            self._conn.execute("ALTER TABLE locators ADD COLUMN source TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def key(app: str, target: Any, screen_hash: str) -> str:
        return locator_key(app, target_text(target), screen_hash)

    def get(self, app: str, target: Any, screen_hash: str) -> dict | None:
        row = self._conn.execute(
            "SELECT x, y, w, h, confidence, source FROM locators WHERE key = ?",
            (self.key(app, target, screen_hash),),
        ).fetchone()
        if row is None:
            return None
        return {"x": row[0], "y": row[1], "w": row[2], "h": row[3], "confidence": row[4], "source": row[5]}

    def put(self, app: str, target: Any, screen_hash: str, bbox: Any) -> bool:
        """Returns True when written, False when the key already existed (left untouched)."""
        box = bbox.model_dump() if isinstance(bbox, GroundResult) else dict(bbox)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO locators (key, app, target, screen_sha, x, y, w, h, confidence, source, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.key(app, target, screen_hash), app, target_text(target), screen_hash,
             int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"]),
             float(box.get("confidence", 1.0)), str(box.get("source", "")), time.time()),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM locators").fetchone()[0])

    def close(self) -> None:
        self._conn.close()


class Grounder:
    """primary -> fallback once -> None. Returns ``{"x","y","w","h","confidence","source"}``.

    ``target`` is a ``cu.schema.Target`` (its ``describe()`` becomes the prompt text) or a
    plain string. ``last_result`` keeps the full ``GroundResult``; ``last_source`` says
    which backend answered; ``last_confidence`` is its confidence.

    Consistency gate: a backend answer with ``confidence < min_confidence`` is rejected
    (recorded in ``last_errors``) and the next backend runs. Because the executor only
    writes what ``ground`` returns, a gated answer never reaches ``LocatorCache``.
    """

    def __init__(self, primary: GrounderBackend | None, fallback: GrounderBackend | None = None,
                 min_confidence: float = 0.3) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0, 1]")
        self.primary = primary
        self.fallback = fallback
        self.min_confidence = float(min_confidence)
        self.last_source: str = "none"
        self.last_result: GroundResult | None = None
        self.last_confidence: float | None = None
        self.last_errors: list[str] = []

    def ground(self, png_bytes: bytes, target: Any) -> dict | None:
        self.last_source = "none"
        self.last_result = None
        self.last_confidence = None
        self.last_errors = []
        text = target_text(target)
        for backend in (self.primary, self.fallback):
            if backend is None:
                continue
            try:
                result = backend.ground(png_bytes, text)
            except meter.ModelCallCapReached:
                raise
            except Exception as e:
                self.last_errors.append(f"{backend.name}: {e}")
                continue
            if result is None:
                self.last_errors.append(f"{backend.name}: element not found")
                continue
            if result.confidence < self.min_confidence:
                self.last_errors.append(
                    f"{backend.name}: confidence {result.confidence:.2f} below gate {self.min_confidence:.2f}"
                )
                continue
            self.last_source = backend.name
            self.last_result = result
            self.last_confidence = result.confidence
            return {"x": result.x, "y": result.y, "w": result.w, "h": result.h,
                    "confidence": result.confidence, "source": result.source}
        return None
