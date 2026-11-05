#!/usr/bin/env python3
"""Offline grounding/judge regression from recorded trajectories (no VM).

Feeds recorded screenshots + instructions to O's ``Grounder`` and ``Judge`` and reports:

* grounding hit rate — the returned box contains the recorded click for that step;
* judge agreement — the judge's verdict on the final screenshot ("was the task done?")
  equals the recorded task result.

Episode layout (one directory per episode under ``--episodes``)::

    <episode>/meta.json
      {"instruction": str, "app": str, "result": bool,
       "steps": [{"screenshot": "<file relative to the episode dir>", "click": [x, y],
                  "target": "<optional text handed to the grounder>"}],
       "final_screenshot": "<optional; defaults to the last step's screenshot>"}

Backends are injected: ``--grounder stub --judge stub`` for tests (``StubGrounder`` answers the
top-left ``STUB_BOX`` for every target, ``StubJudge`` answers ok=True); ``real`` builds the same
``Grounder``/``Judge`` pair ``o_engine.server`` builds, from ``EngineSettings.from_env()``.

``--zoom {off,on,both}`` (default ``off``): ``on`` wraps every grounding backend in
``ZoomGrounder(inner, scale=0.5)`` (two grounder calls per step: full screenshot, then a half-size
crop around the first answer); ``both`` runs the episode set twice and prints one table per setting
under ``## zoom: off`` / ``## zoom: on`` with a leading ``zoom`` column, so the two hit rates can be
read side by side.

Numbers are only what a run prints. This file claims none.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_ENGINE = _HERE.parent.parent
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

META_FILE = "meta.json"
STUB_BOX = {"x": 0, "y": 0, "w": 32, "h": 32}
TABLE_HEADER = "| app | episodes | steps | hits | misses | hit rate | judged | agree | agreement |"
TABLE_ALIGN = "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
ZOOM_TABLE_HEADER = "| zoom " + TABLE_HEADER
ZOOM_TABLE_ALIGN = "|---" + TABLE_ALIGN
ZOOM_CHOICES = ("off", "on", "both")
ZOOM_SCALE = 0.5


# ---------------------------------------------------------------- episodes

@dataclass(frozen=True)
class Step:
    screenshot: Path
    click: tuple[int, int]
    target: str


@dataclass(frozen=True)
class Episode:
    name: str
    app: str
    instruction: str
    result: bool
    steps: list[Step]
    final_screenshot: Path


def _load_episode(ep_dir: Path) -> Episode:
    meta_path = ep_dir / META_FILE
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{meta_path}: not JSON ({e.msg})") from None
    if not isinstance(meta, dict):
        raise ValueError(f"{meta_path}: top level must be an object")
    for key, typ in (("instruction", str), ("app", str), ("result", bool), ("steps", list)):
        if not isinstance(meta.get(key), typ):
            raise ValueError(f"{meta_path}: {key!r} missing or not {typ.__name__}")
    steps: list[Step] = []
    for i, raw in enumerate(meta["steps"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("screenshot"), str):
            raise ValueError(f"{meta_path}: steps[{i}].screenshot missing")
        click = raw.get("click")
        if not (isinstance(click, list) and len(click) == 2 and all(isinstance(v, (int, float)) for v in click)):
            raise ValueError(f"{meta_path}: steps[{i}].click must be [x, y]")
        shot = ep_dir / raw["screenshot"]
        if not shot.is_file():
            raise ValueError(f"{meta_path}: steps[{i}].screenshot {shot} does not exist")
        target = raw.get("target")
        steps.append(Step(shot, (int(click[0]), int(click[1])),
                          str(target) if target else f"the element to click for: {meta['instruction']}"))
    if not steps:
        raise ValueError(f"{meta_path}: steps is empty")
    final = ep_dir / meta["final_screenshot"] if isinstance(meta.get("final_screenshot"), str) else steps[-1].screenshot
    if not final.is_file():
        raise ValueError(f"{meta_path}: final_screenshot {final} does not exist")
    return Episode(ep_dir.name, meta["app"], meta["instruction"], bool(meta["result"]), steps, final)


def load_episodes(episodes_dir: Path | str, limit: int | None = None) -> list[Episode]:
    """Every ``<episodes_dir>/*/meta.json`` in name order; ``limit`` keeps the first N."""
    root = Path(episodes_dir)
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and (p / META_FILE).is_file())
    if limit is not None:
        dirs = dirs[: max(0, int(limit))]
    return [_load_episode(d) for d in dirs]


def dataset_hash(episodes_dir: Path | str) -> str:
    """``git:<HEAD>`` when the directory is inside a git checkout, else ``sha256:<digest>`` of the
    sorted ``<relative path>:<size>`` lines of every file under it."""
    root = Path(episodes_dir).resolve()
    try:
        proc = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            return "git:" + proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    lines = "\n".join(f"{n}:{(root / n).stat().st_size}" for n in names)
    return "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- backends

class StubGrounder:
    """Test backend: every target is at ``STUB_BOX`` (top-left corner).

    Answers a ``GroundResult`` (not a plain dict) so it can also sit inside ``ZoomGrounder``,
    which reads ``.center``/``.confidence`` of its inner backend's answers; ``run`` accepts both.
    """

    name = "stub"

    def ground(self, png_bytes: bytes, target: Any) -> Any:
        from o_engine.cu.ground import GroundResult

        return GroundResult(**STUB_BOX, source=self.name)


class StubJudge:
    """Test backend: every question is answered ok=True."""

    name = "stub"

    def ask(self, png_bytes: bytes, question: str) -> tuple[bool, str]:
        return True, "stub"


def build_real_grounder() -> Any:
    from o_engine.cu.ground import ClaudeCodeGrounder, Grounder, VisionGrounder
    from o_engine.server import EngineSettings

    s = EngineSettings.from_env()
    vision = s.vllm_vision_model or s.vllm_model
    return Grounder(VisionGrounder(s.vllm_url, vision), ClaudeCodeGrounder(s.claude_path))


def build_real_judge() -> Any:
    from o_engine.cu.judge import ClaudeCodeJudge, Judge, VLMJudge
    from o_engine.server import EngineSettings

    s = EngineSettings.from_env()
    vision = s.vllm_vision_model or s.vllm_model
    return Judge(VLMJudge(s.vllm_url, vision), ClaudeCodeJudge(s.claude_path))


GROUNDER_FACTORIES: dict[str, Callable[[], Any]] = {"real": build_real_grounder, "stub": StubGrounder}


def zoomed(grounder: Any, scale: float = ZOOM_SCALE) -> Any:
    """``--zoom on``: wrap in ``ZoomGrounder(inner, scale)``.

    A ``Grounder`` (primary -> fallback -> gate) keeps its gate and gets each backend wrapped, so
    the dict it returns stays the same shape; any other backend is wrapped directly.
    """
    from o_engine.cu.ground import Grounder, ZoomGrounder

    if isinstance(grounder, Grounder):
        primary = ZoomGrounder(grounder.primary, scale=scale) if grounder.primary is not None else None
        fallback = ZoomGrounder(grounder.fallback, scale=scale) if grounder.fallback is not None else None
        return Grounder(primary, fallback, min_confidence=grounder.min_confidence)
    return ZoomGrounder(grounder, scale=scale)
JUDGE_FACTORIES: dict[str, Callable[[], Any]] = {"real": build_real_judge, "stub": StubJudge}


# ---------------------------------------------------------------- scoring

@dataclass
class AppRow:
    app: str
    episodes: int = 0
    steps: int = 0
    hits: int = 0
    failures: int = 0  # grounder returned nothing or raised
    judged: int = 0
    agree: int = 0

    @property
    def misses(self) -> int:
        return self.steps - self.hits

    def add(self, other: "AppRow") -> None:
        self.episodes += other.episodes
        self.steps += other.steps
        self.hits += other.hits
        self.failures += other.failures
        self.judged += other.judged
        self.agree += other.agree


@dataclass
class Report:
    rows: list[AppRow] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)

    @property
    def total(self) -> AppRow:
        t = AppRow("all")
        for r in self.rows:
            t.add(r)
        return t


def as_box(result: Any) -> dict | None:
    """``{x, y, w, h, ...}`` from a grounder answer: a dict, a ``GroundResult`` or ``None``."""
    if result is None:
        return None
    dump = getattr(result, "model_dump", None)
    return dict(dump()) if callable(dump) else dict(result)


def box_contains(box: dict, point: tuple[int, int]) -> bool:
    x, y = point
    return box["x"] <= x < box["x"] + box["w"] and box["y"] <= y < box["y"] + box["h"]


def judge_question(instruction: str) -> str:
    return f"Has this task been completed on screen: {instruction}"


def run(episodes: list[Episode], grounder: Any, judge: Any) -> Report:
    rows: "OrderedDict[str, AppRow]" = OrderedDict()
    report = Report()
    for ep in episodes:
        row = rows.setdefault(ep.app, AppRow(ep.app))
        row.episodes += 1
        for i, step in enumerate(ep.steps):
            row.steps += 1
            png = step.screenshot.read_bytes()
            try:
                box = as_box(grounder.ground(png, step.target))
            except Exception as e:  # a crashed grounder is a miss, not a crash of the run
                box, err = None, str(e)
            else:
                err = None
            hit = bool(box) and box_contains(box, step.click)
            if box is None:
                row.failures += 1
            row.hits += int(hit)
            report.details.append({"episode": ep.name, "app": ep.app, "step": i, "click": list(step.click),
                                   "box": box, "hit": hit, "error": err})
        try:
            ok, reason = judge.ask(ep.final_screenshot.read_bytes(), judge_question(ep.instruction))
        except Exception as e:
            ok, reason = False, f"judge raised: {e}"
        row.judged += 1
        row.agree += int(bool(ok) == ep.result)
        report.details.append({"episode": ep.name, "app": ep.app, "judge": bool(ok), "recorded": ep.result,
                               "reason": reason})
    report.rows = sorted(rows.values(), key=lambda r: r.app)
    return report


# ---------------------------------------------------------------- table

def _pct(num: int, den: int) -> str:
    return f"{100.0 * num / den:.1f}%" if den else "n/a"


def _row(r: AppRow, label: str | None = None, zoom: str | None = None) -> str:
    prefix = f"| {zoom} " if zoom is not None else ""
    return (f"{prefix}| {label or r.app} | {r.episodes} | {r.steps} | {r.hits} | {r.misses} | {_pct(r.hits, r.steps)} "
            f"| {r.judged} | {r.agree} | {_pct(r.agree, r.judged)} |")


def _table(report: Report, zoom: str | None = None) -> list[str]:
    lines = [ZOOM_TABLE_HEADER, ZOOM_TABLE_ALIGN] if zoom is not None else [TABLE_HEADER, TABLE_ALIGN]
    lines += [_row(r, zoom=zoom) for r in report.rows]
    lines.append(_row(report.total, "**all**", zoom=zoom))
    return lines


def markdown(report: Report | dict[str, Report], episodes_dir: Path | str, ds_hash: str, grounder_name: str,
             judge_name: str, zoom: str = "off") -> str:
    """One table for ``zoom`` off/on; with ``both``, ``report`` is ``{"off": Report, "on": Report}`` and
    each gets its own ``## zoom: <setting>`` section whose table carries a leading ``zoom`` column."""
    lines = [
        "# Offline grounding/judge regression",
        "",
        f"- episodes: {Path(episodes_dir).resolve()}",
        f"- dataset hash: {ds_hash}",
        f"- grounder: {grounder_name}",
        f"- judge: {judge_name}",
        f"- zoom: {zoom}" + (f" (ZoomGrounder, scale {ZOOM_SCALE:g})" if zoom != "off" else ""),
        "- hit = returned box contains the recorded click; agree = judge verdict equals the recorded result",
        "",
    ]
    if zoom == "both":
        if not isinstance(report, dict):
            raise ValueError("zoom=both needs {'off': Report, 'on': Report}")
        for setting in ("off", "on"):
            lines += [f"## zoom: {setting}", ""] + _table(report[setting], zoom=setting) + [""]
        return "\n".join(lines).rstrip("\n") + "\n"
    if isinstance(report, dict):
        raise ValueError("a single-setting table takes one Report")
    lines += _table(report)
    return "\n".join(lines) + "\n"


_ROW_RE = re.compile(r"^\|\s*(?:(?P<zoom>off|on)\s*\|\s*)?(?P<app>[^|]+?)\s*\|\s*(?P<episodes>\d+)\s*\|\s*(?P<steps>\d+)\s*\|"
                     r"\s*(?P<hits>\d+)\s*\|\s*(?P<misses>\d+)\s*\|\s*[^|]*\|\s*(?P<judged>\d+)\s*\|\s*(?P<agree>\d+)\s*\|")


def parse_table(text: str) -> dict[str, dict[str, int]]:
    """``{app: {episodes, steps, hits, misses, judged, agree}}`` from a table ``markdown()`` wrote.

    Rows of a ``--zoom both`` table (leading ``zoom`` column) are keyed ``"<zoom>:<app>"``, e.g.
    ``"on:all"``; rows without that column are keyed by app alone.
    """
    out: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        app = m.group("app").strip("* ")
        key = f"{m.group('zoom')}:{app}" if m.group("zoom") else app
        out[key] = {k: int(m.group(k)) for k in ("episodes", "steps", "hits", "misses", "judged", "agree")}
    return out


# ---------------------------------------------------------------- CLI

def _backend_name(obj: Any, requested: str) -> str:
    return getattr(obj, "name", None) or requested


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Offline grounding/judge regression on recorded episodes (no VM).")
    p.add_argument("--episodes", required=True, help="directory of <episode>/meta.json episodes")
    p.add_argument("--limit", type=int, default=None, help="use only the first N episodes (name order)")
    p.add_argument("--out", default=None, help="write the markdown table here (also printed)")
    p.add_argument("--grounder", choices=sorted(GROUNDER_FACTORIES), default="real")
    p.add_argument("--judge", choices=sorted(JUDGE_FACTORIES), default="real")
    p.add_argument("--details", default=None, help="write per-step JSON lines here")
    p.add_argument("--zoom", choices=ZOOM_CHOICES, default="off",
                   help="on: wrap the grounder in ZoomGrounder(scale=0.5); both: run off and on, print both tables")
    return p


def main(argv: list[str] | None = None, grounder_factory: Callable[[], Any] | None = None,
         judge_factory: Callable[[], Any] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        episodes = load_episodes(args.episodes, args.limit)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not episodes:
        print(f"error: no <episode>/{META_FILE} under {args.episodes}", file=sys.stderr)
        return 2
    grounder = (grounder_factory or GROUNDER_FACTORIES[args.grounder])()
    judge = (judge_factory or JUDGE_FACTORIES[args.judge])()
    grounder_name = _backend_name(grounder, args.grounder)
    settings = ("off", "on") if args.zoom == "both" else (args.zoom,)
    reports: dict[str, Report] = {}
    for setting in settings:
        reports[setting] = run(episodes, zoomed(grounder) if setting == "on" else grounder, judge)
    report: Report | dict[str, Report] = reports if args.zoom == "both" else reports[args.zoom]
    text = markdown(report, args.episodes, dataset_hash(args.episodes), grounder_name,
                    _backend_name(judge, args.judge), zoom=args.zoom)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.details:
        with open(args.details, "w", encoding="utf-8") as f:
            for setting in settings:
                for d in reports[setting].details:
                    f.write(json.dumps({"zoom": setting, **d}) + "\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
