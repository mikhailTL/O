"""Long-term goals ("agenda"): objectives O works on every day for days or weeks.

Each goal lives in ``<root>/<id>/``:

* ``journal.jsonl`` is the record: append-only (``open('a')``), one JSON object per line, never
  rewritten. The first line is ``{"kind": "created", "goal": {...}}``; later lines are
  ``{"kind": "cycle", "cycle": {ts, plan, did, metrics, reflection, next, flags}}``,
  ``{"kind": "status", ...}`` and ``{"kind": "autonomy", ...}``.
* ``goal.json`` is a cache derived from the journal (rewritten atomically); deleting it loses nothing.

Safety is part of the data model, not a convention:

* every goal's ``checkpoints`` include ``CHECKPOINT_KINDS`` (publish, send messages as the user,
  spend money, create accounts, accept terms, delete user data); the app refuses those actions
  until the user taps Approve on a prepared checkpoint card;
* the default autonomy is ``act_with_checkpoints``; ``act`` (with an explicit ``allowed`` list) can
  only be set through ``set_autonomy``, never by a model draft or a proposal, and ``allowed`` never
  contains ``spend`` or ``create_account`` (O never does those itself);
* metrics are readings with a ``source``; a bare number is stored as ``unverified``, and a
  reflection that claims virality / monetization / a reached target the platform numbers do not
  show is flagged ``unsupported_claim`` and called out in the next cycle's prompt.

Drafting a goal from text and proposing goals from the learning profile make at most one
local-only ``complete_json`` call each (the cloud fallback is never used); both have a
deterministic fallback.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CHECKPOINT_KINDS: tuple[str, ...] = ("publish", "send_message", "spend", "create_account", "accept_terms", "delete_data")
NEVER_BY_O: tuple[str, ...] = ("spend", "create_account")  # the user does these; O only prepares them
KNOWN_KINDS: tuple[str, ...] = CHECKPOINT_KINDS + ("upload_file", "change_settings", "contact_people")
AUTONOMIES = ("propose", "act_with_checkpoints", "act")
CADENCES = ("daily", "weekdays", "every_n_hours")
STATUSES = ("active", "paused", "archived", "done")
DAY_S = 86400.0
DETAIL_CYCLES = 30
TREND_CYCLES = 7
SUMMARY_POINTS = 14
MAX_TEXT = 2000
MAX_LIST = 20
MAX_PROPOSALS = 3
PROMPT_MAX = 6000
ID_RE = re.compile(r"^g-[0-9a-f]{6,32}$")

Autonomy = Literal["propose", "act_with_checkpoints", "act"]
Cadence = Literal["daily", "weekdays", "every_n_hours"]
Status = Literal["active", "paused", "archived", "done"]
Origin = Literal["user", "agent"]


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _kind_order(kinds: Iterable[str]) -> tuple[str, ...]:
    seen = []
    for k in kinds:
        if k not in seen:
            seen.append(k)
    return tuple(sorted(seen, key=lambda k: (KNOWN_KINDS.index(k) if k in KNOWN_KINDS else len(KNOWN_KINDS), k)))


# --------------------------------------------------------------------------------- models


class Metric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    target: float | None = Field(default=None, ge=0)
    unit: str = Field(default="", max_length=40)
    source: str = Field(default="self_report", min_length=1, max_length=80)


class Goal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=ID_RE.pattern)
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=MAX_TEXT)
    success_metrics: tuple[Metric, ...] = Field(default=(), max_length=8)
    horizon_days: int = Field(ge=1, le=730)
    cadence: Cadence = "daily"
    every_hours: int | None = Field(default=None, ge=1, le=168)
    time: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    autonomy: Autonomy = "act_with_checkpoints"
    checkpoints: tuple[str, ...] = CHECKPOINT_KINDS
    allowed: tuple[str, ...] = ()
    status: Status = "active"
    created: float
    origin: Origin = "user"

    @field_validator("checkpoints", mode="before")
    @classmethod
    def _always_checkpoint(cls, v: Any) -> tuple[str, ...]:
        extra = [str(k) for k in (v or ()) if isinstance(k, str) and re.fullmatch(r"[a-z_]{1,40}", k)]
        return _kind_order(list(CHECKPOINT_KINDS) + extra)

    @field_validator("allowed", mode="before")
    @classmethod
    def _allowed_kinds(cls, v: Any) -> tuple[str, ...]:
        out = []
        for k in v or ():
            if k not in KNOWN_KINDS:
                raise ValueError(f"unknown action kind {k!r}")
            if k not in NEVER_BY_O:
                out.append(k)
        return _kind_order(out)

    @model_validator(mode="after")
    def _cadence_hours(self) -> "Goal":
        if self.cadence == "every_n_hours" and self.every_hours is None:
            object.__setattr__(self, "every_hours", 24)
        if self.cadence != "every_n_hours" and self.every_hours is not None:
            object.__setattr__(self, "every_hours", None)
        return self

    @property
    def primary(self) -> Metric | None:
        return self.success_metrics[0] if self.success_metrics else None


class Reading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float
    source: str = Field(default="unverified", min_length=1, max_length=80)

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("metric value must be a finite number")
        return v


class CycleRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: float
    plan: tuple[str, ...] = Field(default=(), max_length=MAX_LIST)
    did: tuple[str, ...] = Field(default=(), max_length=MAX_LIST)
    metrics: dict[str, Reading] = Field(default_factory=dict)
    reflection: str = Field(default="", max_length=MAX_TEXT)
    next: str = Field(default="", max_length=MAX_TEXT)
    checkpoints: tuple[str, ...] = Field(default=(), max_length=MAX_LIST)
    task_id: str | None = Field(default=None, max_length=128)
    flags: tuple[str, ...] = ()

    @field_validator("plan", "did", "checkpoints", mode="before")
    @classmethod
    def _lines(cls, v: Any) -> Any:
        if v is None:
            return ()
        if not isinstance(v, (list, tuple)):
            raise ValueError("must be a list of strings")
        return tuple(_clip(x, 400) for x in v if str(x).strip())

    @classmethod
    def from_body(cls, body: dict[str, Any], ts: float) -> "CycleRecord":
        """A cycle record from an agent's JSON. Bare-number metrics become ``unverified`` readings."""
        if not isinstance(body, dict):
            raise ValueError("cycle record must be an object")
        metrics: dict[str, Any] = {}
        raw = body.get("metrics") or {}
        if not isinstance(raw, dict):
            raise ValueError("metrics must be an object")
        for name, v in list(raw.items())[:16]:
            key = re.sub(r"[^a-z0-9_]+", "_", str(name).lower()).strip("_")[:40]
            if not key:
                continue
            if isinstance(v, bool):
                raise ValueError(f"metric {name!r} must be a number")
            if isinstance(v, (int, float)):
                metrics[key] = {"value": float(v), "source": "unverified"}
            elif isinstance(v, dict) and v.get("value") is not None:
                metrics[key] = {"value": v.get("value"), "source": _clip(v.get("source") or "unverified", 80)}
            elif v is None or (isinstance(v, dict) and v.get("value") is None):
                continue  # measured nothing: honest absence, not a zero
            else:
                raise ValueError(f"metric {name!r} must be a number or {{value, source}}")
        fields = {k: body.get(k) for k in ("plan", "did", "checkpoints") if body.get(k) is not None}
        rec = cls(ts=float(ts), metrics=metrics, reflection=_clip(body.get("reflection") or "", MAX_TEXT),
                  next=_clip(body.get("next") or "", MAX_TEXT), task_id=body.get("task_id"), **fields)
        flags = []
        if not rec.plan and not rec.did:
            flags.append("incomplete")
        if any(r.source == "unverified" for r in rec.metrics.values()):
            flags.append("unverified_metrics")
        return rec.model_copy(update={"flags": tuple(flags)})


# --------------------------------------------------------------------------------- honesty

_CLAIM = re.compile(
    r"\b(went viral|gone viral|going viral|is viral|viral|monetiz(?:ed|ation\s+(?:is\s+)?(?:on|enabled|approved|unlocked|reached|achieved))"
    r"|thresholds?\s+(?:reached|met|hit|crossed)|goal\s+(?:reached|achieved|met|done|complete)|hit\s+(?:the|our|my)\s+target"
    r"|target\s+(?:reached|met|hit))\b",
    re.I,
)
_NEGATED = re.compile(r"\b(not|no|isn't|wasn't|never|nowhere near|far from|before|until|toward|towards)\s+(?:\w+\s+){0,2}$", re.I)


def claims_success(text: str) -> bool:
    for m in _CLAIM.finditer(str(text or "")):
        if not _NEGATED.search(text[: m.start()]):
            return True
    return False


def targets_met(goal: Goal, latest: dict[str, Reading]) -> bool:
    """Every metric with a target has a platform-sourced reading at or above it."""
    targeted = [m for m in goal.success_metrics if m.target is not None]
    if not targeted:
        return False
    for m in targeted:
        r = latest.get(m.name)
        if r is None or r.source in ("unverified", "self_report") or r.value < float(m.target or 0):
            return False
    return True


# --------------------------------------------------------------------------------- drafting

_MULT = {"k": 1_000, "m": 1_000_000, "thousand": 1_000, "million": 1_000_000}
_NUM_NOUN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(k|m|thousand|million)?\s+((?:[a-z][a-z'-]*\s+){0,2}?)([a-z][a-z'-]*s)\b", re.I)
_HORIZON = re.compile(r"\b(?:in|within|over|for)\s+(\d{1,3})\s*(day|week|month|year)s?\b", re.I)
_EVERY_H = re.compile(r"\bevery\s+(\d{1,3})\s*h(?:ours?|rs?)?\b", re.I)
_LEAD = re.compile(r"^\s*(?:please\s+)?(?:make\s+(?:this|it)\s+a\s+goal\s*[:,-]?\s*|set\s+a\s+goal\s*(?:to|:)?\s*|goal\s*:\s*)", re.I)
_STOP_NOUNS = {"days", "weeks", "months", "years", "hours", "times"}


def _source_for(text: str) -> str:
    t = text.lower()
    if "youtube" in t or re.search(r"\byt\b", t):
        return "connector:youtube"
    if re.search(r"newsletter|substack|beehiiv|mailchimp|convertkit", t):
        return "connector:newsletter"
    if re.search(r"podcast", t):
        return "connector:podcast"
    if re.search(r"tiktok", t):
        return "connector:tiktok"
    if re.search(r"instagram", t):
        return "connector:instagram"
    if re.search(r"\b(shop|store|etsy|shopify|sales)\b", t):
        return "connector:shop"
    return "self_report"


def _title(text: str) -> str:
    t = _LEAD.sub("", " ".join(str(text).split())).strip(" .")
    t = t[:1].upper() + t[1:] if t else "Long-term goal"
    return _clip(t, 80)


def draft_goal(text: str, now: float | None = None, origin: str = "user") -> Goal:
    """Deterministic goal from one sentence: title, metrics with targets and sources, horizon, cadence."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("goal text must not be empty")
    now = time.time() if now is None else float(now)
    low = text.lower()
    source = _source_for(text)
    metrics: list[Metric] = []
    youtube = source == "connector:youtube"
    if youtube:
        views_target = None
        for m in _NUM_NOUN.finditer(text):
            if m.group(4).lower() == "views":
                views_target = float(m.group(1).replace(",", "")) * _MULT.get((m.group(2) or "").lower(), 1)
        metrics.append(Metric(name="views", target=views_target, unit="views", source=source))
        monetize = bool(re.search(r"monetiz|partner program|\bypp\b|ad revenue|money|income", low))
        subs = None
        for m in _NUM_NOUN.finditer(text):
            if m.group(4).lower() == "subscribers":
                subs = float(m.group(1).replace(",", "")) * _MULT.get((m.group(2) or "").lower(), 1)
        if subs is not None or monetize:
            metrics.append(Metric(name="subscribers", target=subs if subs is not None else 1000.0, unit="subscribers", source=source))
        if monetize:
            # YouTube Partner Program thresholds: 1,000 subscribers and 4,000 public watch hours (12 months).
            metrics.append(Metric(name="watch_hours", target=4000.0, unit="hours", source=source))
    else:
        for m in _NUM_NOUN.finditer(text):
            noun = m.group(4).lower()
            if noun in _STOP_NOUNS:
                continue
            target = float(m.group(1).replace(",", "")) * _MULT.get((m.group(2) or "").lower(), 1)
            name = re.sub(r"[^a-z0-9_]+", "_", noun).strip("_")[:40] or "progress"
            metrics.append(Metric(name=name, target=target, unit=noun, source=source if source != "self_report" else "self_report"))
            break
    if not metrics:
        metrics.append(Metric(name="progress", target=100.0, unit="%", source="self_report"))
    horizon = 60
    hm = _HORIZON.search(text)
    if hm:
        n, unit = int(hm.group(1)), hm.group(2).lower()
        horizon = max(1, min(730, n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]))
    elif not re.search(r"grow|monetiz|subscriber|follower|viral|audience|launch|\d", low):
        horizon = 30
    cadence: str = "daily"
    every = None
    em = _EVERY_H.search(text)
    if em:
        cadence, every = "every_n_hours", max(1, min(168, int(em.group(1))))
    elif re.search(r"\b(every\s+)?week\s*days?\b|\bweekdays?\b|monday to friday|mon-fri", low):
        cadence = "weekdays"
    return Goal(id=new_goal_id(), title=_title(text), objective=_clip(_LEAD.sub("", text), MAX_TEXT), success_metrics=tuple(metrics),
                horizon_days=horizon, cadence=cadence, every_hours=every, created=now, origin=origin)


def new_goal_id() -> str:
    return "g-" + uuid.uuid4().hex[:12]


DRAFT_SYSTEM_PROMPT = (
    "You turn one person's long-term objective into a measurable goal for an assistant that works on it every day. "
    "Reply with exactly one JSON object: {\"title\": str (<= 80 chars, sentence case), \"objective\": str, "
    "\"success_metrics\": [{\"name\": snake_case, \"target\": number|null, \"unit\": str, \"source\": str}], "
    "\"horizon_days\": int, \"cadence\": \"daily\"|\"weekdays\"|\"every_n_hours\", \"every_hours\": int|null}. "
    "Metrics must be numbers the platform itself reports (source like \"connector:youtube\"); use \"self_report\" only "
    "when no platform can measure it. Never invent shortcuts such as buying views or followers. No prose."
)

PROPOSE_SYSTEM_PROMPT = (
    "You propose up to three long-term goals an assistant could own for this person, based only on what it has "
    "learned about them (preferences, habits, recurring tasks). Each must be useful, measurable from a real source "
    "and doable without spending money, creating accounts or acting publicly without approval. Reply with exactly "
    "one JSON object {\"goals\": [{\"title\", \"objective\", \"success_metrics\": [{\"name\", \"target\", \"unit\", "
    "\"source\"}], \"horizon_days\", \"cadence\"}]}. Reply {\"goals\": []} when nothing fits. No prose."
)


def _sanitize_draft(raw: Any, fallback: Goal, origin: str) -> Goal | None:
    """A model's draft merged over ``fallback``. Safety fields (autonomy, allowed, checkpoints, status) are never read."""
    if not isinstance(raw, dict):
        return None
    title = _clip(raw.get("title") or "", 80)
    objective = _clip(raw.get("objective") or fallback.objective, MAX_TEXT)
    if not title:
        return None
    metrics: list[Metric] = []
    for m in raw.get("success_metrics") or []:
        if not isinstance(m, dict):
            continue
        name = re.sub(r"[^a-z0-9_]+", "_", str(m.get("name") or "").lower()).strip("_")[:40]
        try:
            tgt = m.get("target")
            metrics.append(Metric(name=name, target=None if tgt is None else float(tgt), unit=_clip(m.get("unit") or "", 40),
                                  source=_clip(m.get("source") or "self_report", 80)))
        except Exception:
            continue
    try:
        horizon = int(raw.get("horizon_days") or fallback.horizon_days)
    except (TypeError, ValueError):
        horizon = fallback.horizon_days
    cadence = raw.get("cadence") if raw.get("cadence") in CADENCES else fallback.cadence
    every = raw.get("every_hours") if cadence == "every_n_hours" else None
    try:
        return Goal(id=fallback.id, title=title, objective=objective, success_metrics=tuple(metrics[:8]) or fallback.success_metrics,
                    horizon_days=max(1, min(730, horizon)), cadence=cadence, every_hours=every if isinstance(every, int) else None,
                    created=fallback.created, origin=origin)
    except Exception:
        return None


# --------------------------------------------------------------------------------- cycle prompt


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}".rstrip("0").rstrip(".")


def day_of(goal: dict[str, Any] | Goal, now: float) -> int:
    created = goal.created if isinstance(goal, Goal) else float(goal.get("created") or now)
    return max(1, int((float(now) - created) // DAY_S) + 1)


def profile_lines(profile: dict[str, Any] | None, n: int = 8) -> str:
    if not isinstance(profile, dict):
        return ""
    rows = []
    for key in ("preferences", "habits", "suggestions"):
        for it in profile.get(key) or []:
            if isinstance(it, dict) and it.get("text"):
                rows.append("- " + _clip(it["text"], 160))
    return "\n".join(rows[:n])


def next_cycle_prompt(goal: dict[str, Any], cycles: list[dict[str, Any]], profile_text: str = "", now: float | None = None,
                      notes: Iterable[str] = ()) -> str:
    """Today's task prompt for one goal: objective, metric trend (last 7 cycles), the last reflection and
    plan for today, what O knows about the user, the checkpoints, and the cycle-record contract."""
    g = Goal.model_validate({k: v for k, v in goal.items() if k in Goal.model_fields})
    now = time.time() if now is None else float(now)
    day = day_of(g, now)
    recent = list(cycles or [])[-TREND_CYCLES:]
    lines = [
        f"Goal cycle for \"{g.title}\" (goal {g.id}), day {day} of {g.horizon_days}.",
        f"Objective: {g.objective}",
        "",
        "Success metrics (read each from its source; never estimate or invent a number):",
    ]
    for m in g.success_metrics:
        vals = [c.get("metrics", {}).get(m.name, {}).get("value") for c in recent]
        vals = [v for v in vals if isinstance(v, (int, float))]
        trend = ", ".join(_fmt(v) for v in vals) if vals else "no readings yet"
        lines.append(f"- {m.name}: target {_fmt(m.target)}{(' ' + m.unit) if m.unit else ''} · source {m.source} · last {len(vals)}: {trend}")
    if not cycles:
        lines += ["", "This is the first cycle: set up how you will measure each metric, then start the work."]
    else:
        last = cycles[-1]
        lines += ["", f"Last reflection: {_clip(last.get('reflection') or '(none)', 600)}",
                  f"Planned for today: {_clip(last.get('next') or '(nothing planned)', 600)}"]
        flags = set(last.get("flags") or [])
        if "unsupported_claim" in flags:
            lines.append("Correction needed: the last reflection claimed a result that is not supported by the platform metrics. Say so plainly today.")
        if "unverified_metrics" in flags:
            lines.append("Some of the last readings were unverified (no platform source). Read them from the platform today, or report them as missing.")
        if "incomplete" in flags:
            lines.append("The last cycle recorded no plan and no work. Do real work today.")
    for n in list(notes)[:8]:
        lines.append(f"Since last cycle: {_clip(n, 300)}")
    if profile_text.strip():
        lines += ["", "What O knows about this person (apply what fits):", profile_text.strip()]
    lines += [
        "",
        f"Autonomy: {g.autonomy}. Checkpoints that always need the person's tap: {', '.join(g.checkpoints)}."
        + (f" Allowed without a tap for this goal: {', '.join(g.allowed)}." if g.autonomy == "act" and g.allowed else ""),
        "",
        "Today:",
        "1. Plan today's work toward the metrics: 3 to 6 concrete steps, the highest-leverage first.",
        "2. Do the work in the background. Share the plan as ui_emit frames so the person can follow along.",
        "3. Measure: read every metric from its platform source through the person's connected accounts (mcp_tools / mcp_call). If a source is not connected, record that metric as missing and say what to connect.",
        "4. Write an honest reflection: what moved, what did not, and why. No claims of virality, monetization or a reached target unless the platform numbers show it.",
        "5. Decide what to change tomorrow.",
        "",
        "Rules: work only through the person's own channels and accounts. O must never create accounts, never spend money, never buy views, followers, subscribers or engagement, never use bots or fake comments, and never work around a platform's rules.",
        "At a checkpoint (publishing or posting publicly, sending messages as the person, spending, creating an account, accepting terms, deleting their data): prepare everything (for a video: the finished file, title, description, thumbnail and schedule) and call mcp__o__goal_checkpoint with the exact action. Do not perform it; the person approves, edits or skips it in O.",
        "",
        "End your final message with the cycle record, one JSON object in a ```json block:",
        "```json",
        json.dumps({"goal_id": g.id, "plan": ["..."], "did": ["..."],
                    "metrics": {(g.primary.name if g.primary else "progress"): {"value": 0, "source": g.primary.source if g.primary else "self_report"}},
                    "reflection": "...", "next": "...", "checkpoints": ["..."]}),
        "```",
    ]
    out = "\n".join(lines)
    return out if len(out) <= PROMPT_MAX else out[: PROMPT_MAX - 1] + "…"


# --------------------------------------------------------------------------------- store


class GoalStore:
    """Goals under ``root``; one directory per goal. Thread-safe."""

    def __init__(self, root: str | Path, model: Any = None, clock: Callable[[], float] | None = None) -> None:
        self.root = Path(root)
        self.model = model
        self.clock = clock or time.time
        self._lock = threading.RLock()

    # ---- files

    def _dir(self, goal_id: str) -> Path | None:
        if not isinstance(goal_id, str) or not ID_RE.match(goal_id):
            return None
        return self.root / goal_id

    def _append(self, goal_id: str, row: dict[str, Any]) -> None:
        d = self._dir(goal_id)
        assert d is not None
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "journal.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _rows(self, goal_id: str) -> list[dict[str, Any]]:
        d = self._dir(goal_id)
        if d is None or not (d / "journal.jsonl").exists():
            return []
        out = []
        for line in (d / "journal.jsonl").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line from a crash: skip it, keep the rest
        return out

    def _replay(self, goal_id: str) -> tuple[Goal, list[dict[str, Any]]] | None:
        rows = self._rows(goal_id)
        if not rows or rows[0].get("kind") != "created":
            return None
        g = Goal.model_validate(rows[0]["goal"])
        cycles: list[dict[str, Any]] = []
        for r in rows[1:]:
            k = r.get("kind")
            if k == "cycle" and isinstance(r.get("cycle"), dict):
                cycles.append(r["cycle"])
            elif k == "status" and r.get("status") in STATUSES:
                g = g.model_copy(update={"status": r["status"]})
            elif k == "autonomy" and r.get("autonomy") in AUTONOMIES:
                g = Goal.model_validate({**g.model_dump(), "autonomy": r["autonomy"], "allowed": r.get("allowed") or []})
        return g, cycles

    def _derived(self, g: Goal, cycles: list[dict[str, Any]]) -> dict[str, Any]:
        out = g.model_dump(mode="json")
        out["success_metrics"] = [dict(m) for m in out["success_metrics"]]
        out["cycles"] = len(cycles)
        out["last_cycle"] = cycles[-1]["ts"] if cycles else None
        latest: dict[str, Any] = {}
        for c in cycles:
            for k, v in (c.get("metrics") or {}).items():
                latest[k] = v
        out["latest"] = latest
        return out

    def _write_derived(self, goal_id: str, derived: dict[str, Any]) -> None:
        d = self._dir(goal_id)
        assert d is not None
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".goal-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(derived, f, indent=1, sort_keys=True)
            os.replace(tmp, d / "goal.json")
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _refresh(self, goal_id: str) -> dict[str, Any]:
        rep = self._replay(goal_id)
        if rep is None:
            raise KeyError(goal_id)
        derived = self._derived(*rep)
        self._write_derived(goal_id, derived)
        return derived

    # ---- writing

    def create(self, goal: Goal) -> dict[str, Any]:
        with self._lock:
            d = self._dir(goal.id)
            if d is None or (d / "journal.jsonl").exists():
                raise ValueError("goal id already exists")
            self._append(goal.id, {"kind": "created", "ts": goal.created, "goal": goal.model_dump(mode="json")})
            return self._refresh(goal.id)

    def _local_model(self) -> Any:
        fn = getattr(self.model, "local_only", None)
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception:
            return None

    def draft(self, text: str, origin: str = "user") -> Goal:
        fallback = draft_goal(text, now=self.clock(), origin=origin)
        local = self._local_model()
        if local is None:
            return fallback
        try:
            raw = json.loads(local.complete_json(DRAFT_SYSTEM_PROMPT, _clip(text, MAX_TEXT)))
        except Exception:
            return fallback
        return _sanitize_draft(raw, fallback, origin) or fallback

    def create_from_text(self, text: str) -> dict[str, Any]:
        return self.create(self.draft(text))

    def create_from_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        """Accept a proposal (from ``propose``): safety fields are reset to the defaults whatever it says."""
        if not isinstance(proposal, dict):
            raise ValueError("proposal must be an object")
        base = draft_goal(proposal.get("objective") or proposal.get("title") or "", now=self.clock(), origin="agent")
        g = _sanitize_draft(proposal, base, "agent")
        if g is None:
            raise ValueError("proposal needs a title and an objective")
        return self.create(g)

    def record_cycle(self, goal_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rep = self._replay(goal_id)
            if rep is None:
                raise KeyError(goal_id)
            g, cycles = rep
            if g.status in ("archived", "done"):
                raise ValueError(f"goal is {g.status}")
            rec = CycleRecord.from_body(body, ts=self.clock())
            latest: dict[str, Reading] = {}
            for c in cycles:
                for k, v in (c.get("metrics") or {}).items():
                    try:
                        latest[k] = Reading.model_validate(v)
                    except Exception:
                        continue
            latest.update(rec.metrics)
            if claims_success(rec.reflection) and not targets_met(g, latest):
                rec = rec.model_copy(update={"flags": rec.flags + ("unsupported_claim",)})
            cyc = rec.model_dump(mode="json")
            self._append(goal_id, {"kind": "cycle", "ts": rec.ts, "cycle": cyc})
            self._refresh(goal_id)
            return cyc

    def set_status(self, goal_id: str, status: str) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError("unknown status")
        with self._lock:
            rep = self._replay(goal_id)
            if rep is None:
                raise KeyError(goal_id)
            g, _ = rep
            if g.status == "archived" and status != "archived":
                raise ValueError("an archived goal cannot be resumed; create it again")
            self._append(goal_id, {"kind": "status", "ts": self.clock(), "status": status})
            return self._refresh(goal_id)

    def set_autonomy(self, goal_id: str, autonomy: str, allowed: Iterable[str] = ()) -> dict[str, Any]:
        """The person's explicit choice. ``allowed`` only counts under ``act`` and never holds spend / create_account."""
        if autonomy not in AUTONOMIES:
            raise ValueError("unknown autonomy")
        allowed = [k for k in (allowed or []) if k not in NEVER_BY_O] if autonomy == "act" else []
        for k in allowed:
            if k not in KNOWN_KINDS:
                raise ValueError(f"unknown action kind {k!r}")
        with self._lock:
            if self._replay(goal_id) is None:
                raise KeyError(goal_id)
            self._append(goal_id, {"kind": "autonomy", "ts": self.clock(), "autonomy": autonomy, "allowed": allowed})
            return self._refresh(goal_id)

    # ---- reading

    def get(self, goal_id: str) -> dict[str, Any] | None:
        rep = self._replay(goal_id) if self._dir(goal_id) is not None else None
        return self._derived(*rep) if rep else None

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and ID_RE.match(p.name) and (p / "journal.jsonl").exists())

    def summary(self, goal_id: str) -> dict[str, Any] | None:
        rep = self._replay(goal_id)
        if rep is None:
            return None
        g, cycles = rep
        d = self._derived(g, cycles)
        now = self.clock()
        p = g.primary
        primary = None
        if p is not None:
            pts = [c["metrics"][p.name]["value"] for c in cycles if p.name in (c.get("metrics") or {})]
            primary = {"name": p.name, "unit": p.unit, "target": p.target, "source": p.source,
                       "latest": pts[-1] if pts else None, "series": pts[-SUMMARY_POINTS:]}
        return {**d, "day": day_of(g, now), "primary": primary,
                "pending_flags": list((cycles[-1].get("flags") if cycles else None) or [])}

    def list(self, include_archived: bool = True) -> list[dict[str, Any]]:
        out = []
        for gid in self.ids():
            s = self.summary(gid)
            if s is None or (not include_archived and s["status"] == "archived"):
                continue
            out.append(s)
        out.sort(key=lambda s: (s["status"] != "active", -float(s.get("created") or 0)))
        return out

    def detail(self, goal_id: str) -> dict[str, Any] | None:
        rep = self._replay(goal_id) if self._dir(goal_id) is not None else None
        if rep is None:
            return None
        g, cycles = rep
        recent = cycles[-DETAIL_CYCLES:]
        series: dict[str, list[dict[str, Any]]] = {}
        for c in recent:
            for k, v in (c.get("metrics") or {}).items():
                series.setdefault(k, []).append({"ts": c["ts"], "value": v.get("value"), "source": v.get("source")})
        return {"goal": self._derived(g, cycles), "day": day_of(g, self.clock()), "cycles": recent, "series": series}

    # ---- proposals

    def propose(self, profile: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Up to three goals O could own, from the learning profile. Not stored until accepted."""
        profile = profile if isinstance(profile, dict) else {}
        if not any(profile.get(k) for k in ("preferences", "habits", "suggestions", "app_skills")):
            return []
        local = self._local_model()
        if local is not None:
            try:
                user = json.dumps({k: [_clip(i.get("text"), 160) for i in (profile.get(k) or []) if isinstance(i, dict)][:12]
                                   for k in ("preferences", "habits", "suggestions")})
                raw = json.loads(local.complete_json(PROPOSE_SYSTEM_PROMPT, user))
                out = []
                for item in (raw.get("goals") if isinstance(raw, dict) else None) or []:
                    if not isinstance(item, dict):
                        continue
                    base = draft_goal(item.get("objective") or item.get("title") or "goal", now=self.clock(), origin="agent")
                    g = _sanitize_draft(item, base, "agent")
                    if g is not None:
                        out.append(self._proposal(g))
                    if len(out) >= MAX_PROPOSALS:
                        break
                if out:
                    return out
            except Exception:
                pass
        return self._propose_deterministic(profile)

    def _proposal(self, g: Goal) -> dict[str, Any]:
        d = g.model_dump(mode="json")
        d.pop("id", None)
        d["proposal_id"] = "p-" + hashlib.sha256(g.title.encode()).hexdigest()[:10]
        return d

    def _propose_deterministic(self, profile: dict[str, Any]) -> list[dict[str, Any]]:
        now = self.clock()
        out: list[dict[str, Any]] = []
        for s in profile.get("suggestions") or []:
            sch = (s or {}).get("suggest_schedule") or {}
            prompt = _clip(sch.get("prompt") or "", 120)
            if not prompt:
                continue
            cron = str(sch.get("cron") or "").split()
            weekdays = len(cron) == 5 and cron[4] in ("1-5", "MON-FRI")
            g = Goal(id=new_goal_id(), title=_clip(f"Take over: {prompt[:1].lower() + prompt[1:]}", 80),
                     objective=f"Run \"{prompt}\" {sch.get('when') or 'on its usual schedule'} without being asked, and get it right "
                               f"the first time so it never needs a correction.",
                     success_metrics=(Metric(name="on_time_runs", target=8.0, unit="runs", source="o_task_log"),
                                      Metric(name="corrections", target=0.0, unit="corrections", source="o_task_log")),
                     horizon_days=30, cadence="weekdays" if weekdays else "daily", created=now, origin="agent")
            out.append(self._proposal(g))
            if len(out) >= 2:
                break
        for h in profile.get("habits") or []:
            if len(out) >= MAX_PROPOSALS:
                break
            app = (h or {}).get("app")
            if not app:
                continue
            g = Goal(id=new_goal_id(), title=_clip(f"Have {app} sessions ready before they start", 80),
                     objective=f"Learn the {app} routine ({_clip(h.get('text'), 120)}) and prepare files, tabs and notes before each "
                               f"session so it starts ready.",
                     success_metrics=(Metric(name="ready_sessions", target=20.0, unit="sessions", source="o_task_log"),),
                     horizon_days=30, cadence="weekdays", created=now, origin="agent")
            out.append(self._proposal(g))
        return out[:MAX_PROPOSALS]
