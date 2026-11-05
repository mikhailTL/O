"""Proximity wake briefing: on USER_ABSENT -> USER_PRESENT, summarise the milestones recorded since
the absence began in exactly three conversational sentences.

One local-only model call (``ModelClient.local_only()``; nothing leaves the machine). When the model
is unavailable or does not return three sentences, a deterministic template does. The brief is
kept as ``latest`` (``since`` = start of the away period, epoch seconds) and published on
``agent/brief``; the app shows it and speaks it when the user enabled that (``speak`` is a library
hook only; the engine passes none). Only
milestone kinds and titles are used; no sensor or biometric value is ever part of the prompt.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from . import ABSENT, PRESENT
from .bus import PRIORITY_HIGH

SYSTEM = ("You write a spoken executive brief for a user who just came back to their computer. "
          "Exactly three short sentences, plain conversational English, no lists, no markdown, no greeting "
          "beyond the first words. Say what finished, what needed an error worked around, and what still needs "
          "attention. Use only the facts given.")
MAX_PROMPT_ROWS = 40
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    text = " ".join(str(text or "").split())
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def _clean_title(title: str, limit: int = 80) -> str:
    t = re.sub(r"[.!?]+", " ", " ".join(str(title or "").split())).strip().strip("\"'")
    return (t[: limit - 1] + "…") if len(t) > limit else (t or "an untitled task")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _duration(seconds: float | None) -> str:
    if seconds is None or seconds < 60:
        return "a moment"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return _plural(minutes, "minute")
    hours, mins = divmod(minutes, 60)
    return _plural(hours, "hour") + (f" {_plural(mins, 'minute')}" if mins else "")


def template_brief(rows: list[dict[str, Any]], away_s: float | None = None) -> str:
    """Three sentences, always."""
    by = {k: [r for r in rows if r.get("kind") == k] for k in ("completed", "error_bypassed", "failed", "checkpoint")}
    done = by["completed"] + by["error_bypassed"]
    away = _duration(away_s)
    if done:
        last = _clean_title(done[-1].get("title", ""))
        s1 = f"While you were away for {away}, I finished {_plural(len(done), 'background task')}, most recently {last}."
    elif rows:
        s1 = f"While you were away for {away}, I kept working in the background but nothing finished cleanly."
    else:
        s1 = f"While you were away for {away}, no background tasks ran."
    if by["error_bypassed"]:
        s2 = f"{_plural(len(by['error_bypassed']), 'task')} hit errors that I worked around automatically."
    elif by["checkpoint"]:
        s2 = f"I recorded {_plural(len(by['checkpoint']), 'checkpoint')}, the latest being {_clean_title(by['checkpoint'][-1].get('title', ''))}."
    else:
        s2 = "Nothing needed an error workaround."
    if by["failed"]:
        s3 = f"{_plural(len(by['failed']), 'task')} failed and need{'s' if len(by['failed']) == 1 else ''} your attention, starting with {_clean_title(by['failed'][0].get('title', ''))}."
    else:
        s3 = "Nothing needs your attention right now."
    return " ".join((s1, s2, s3))


def normalise_model_brief(text: str) -> str | None:
    """The model's text when it is exactly three sentences (after trimming quotes/markdown), else None."""
    text = re.sub(r"[*_`#>]+", "", str(text or "")).strip().strip("\"'")
    sentences = split_sentences(text)
    if len(sentences) != 3 or any(len(s) > 400 for s in sentences):
        return None
    return " ".join(sentences)


def facts(rows: list[dict[str, Any]], away_s: float | None) -> str:
    lines = [f"Away for: {_duration(away_s)}", f"Milestones: {len(rows)}"]
    for r in rows[-MAX_PROMPT_ROWS:]:
        lines.append(f"- {r.get('kind')}: {_clean_title(r.get('title', ''), 120)}")
    return "\n".join(lines)


class Briefer:
    def __init__(self, milestones: Any, model: Any = None, bus: Any = None, log: Any = None,
                 speak: Callable[[str], Any] | None = None, wall: Callable[[], float] = time.time) -> None:
        self.milestones = milestones
        self.model = model
        self.bus = bus
        self.log = log
        self.speak = speak
        self.wall = wall
        self.latest: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._sid: int | None = None
        self.threads: list[threading.Thread] = []

    def attach(self) -> None:
        if self.bus is not None and self._sid is None:
            self._sid = self.bus.subscribe("presence/state", lambda _t, p: self.on_presence(p), PRIORITY_HIGH)

    def detach(self) -> None:
        if self.bus is not None and self._sid is not None:
            self.bus.unsubscribe(self._sid)
            self._sid = None

    def on_presence(self, msg: dict) -> None:
        """High-priority listener: hand the brief to its own thread so the bus never waits on a model."""
        if msg.get("state") != PRESENT or msg.get("prev") != ABSENT:
            return
        since = msg.get("absent_start")
        t = threading.Thread(target=self.build, args=(since, msg.get("ts")), name="o-brief", daemon=True)
        self.threads.append(t)
        t.start()

    def build(self, since: float | None, until: float | None = None) -> dict[str, Any]:
        until = float(until or self.wall())
        rows = self.milestones.since(since or 0.0)
        away = (until - since) if since else None
        text, via = None, "template"
        local = self.model.local_only() if self.model is not None and hasattr(self.model, "local_only") else self.model
        if local is not None and rows:
            try:
                text = normalise_model_brief(local.complete(SYSTEM, facts(rows, away)))
                via = "local" if text else "template"
            except Exception:
                text = None
        if text is None:
            text = template_brief(rows, away)
            via = "template"
        counts = {k: sum(1 for r in rows if r.get("kind") == k) for k in ("completed", "error_bypassed", "failed", "checkpoint")}
        brief = {"text": text, "sentences": split_sentences(text), "since": since, "until": until,
                 "count": len(rows), "counts": counts, "via": via, "ts": self.wall()}
        with self._lock:
            self.latest = brief
        if self.log is not None:
            try:
                self.log.record("note", f"brief ready ({via}, {len(rows)} milestones)", via=via, count=len(rows), counts=counts)
            except Exception:
                pass
        if self.bus is not None:
            try:
                self.bus.publish("agent/brief", {"text": text, "count": len(rows), "via": via, "ts": brief["ts"]},
                                 priority=PRIORITY_HIGH)
            except Exception:
                pass
        if self.speak is not None:
            try:
                self.speak(text)
            except Exception:
                pass
        return brief

    def get_latest(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self.latest) if self.latest else None
