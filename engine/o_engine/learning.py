"""Learning / adaptation layer: what O has learned from the user's behaviour and its own.

``observations.jsonl`` is the record: append-only (``open('a')``), one ``Observation`` per line,
never rewritten. ``profile.json`` is a cache derived from it (rewritten atomically) holding three
kinds of ``ProfileItem``:

* ``preference``: how the user likes things done (corrections, repeated task choices);
* ``habit``: what the user does when (screen app-focus sessions; recurring tasks become
  ``suggest_schedule`` suggestions);
* ``app_skill``: O's own competence per app (task outcomes, interface fitness, procedure counts).

Forgetting and confirming are observations too (``source: "own"``, ``"forget <id>"`` /
``"confirm <id>"``). Derivation is deterministic; one optional local-only model call may merge
near-duplicate preferences, in the background, and the cloud fallback is never used here.

Privacy: screen observations carry app/window names and durations only, and every observation's
text is scrubbed of email addresses, card/account numbers, password fields and tokens before it
is stored.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TEXT = 2000
SCREEN_TEXT = 200
HABIT_WINDOW_S = 14 * 86400
HABIT_MIN_DAYS = 3
SCREEN_GAP_S = 300.0
SCREEN_MIN_SESSION_S = 30.0
SCREEN_SETTLE_S = 300.0
SEQUENCE_GAP_S = 600.0
SEQUENCE_MIN_FIRST_S = 120.0
MAX_SEQUENCE_HABITS = 5
SUGGEST_MIN = 3
CORRECTION_CONFIDENCE = 0.9
CHOICE_CONFIDENCE = 0.6
CONFIRM_STEP = 0.1
CONFIDENCE_CAP = 0.99
MERGE_JACCARD = 0.6
TOPIC_OVERLAP = 0.3
PROMPT_LINES = 15
PROMPT_MIN_CONFIDENCE = 0.5
LOG_TEXT = 80

Source = Literal["task", "correction", "screen", "own"]
Kind = Literal["preference", "habit", "app_skill"]

# --------------------------------------------------------------------------------- privacy

_SCRUB: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[token]"),
    (re.compile(r"\b(?:sk|pk|rk)[-_](?:live[-_]|test[-_])?[A-Za-z0-9_-]{8,}"), "[token]"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat|glpat|xox[abprs])[-_][A-Za-z0-9_-]{8,}"), "[token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), "[token]"),
    (re.compile(r"(?i)\b(api[_ -]?key|access[_ -]?key|secret[_ -]?key|auth[_ -]?token|token|secret)\b(\s*(?:is|=|:)\s*)\S+"),
     r"\1\2[redacted]"),
    (re.compile(r"(?i)\b(password|passwd|passcode|passphrase|pwd|pin)\b(\s*(?:is|=|:|of)?\s*)\S+"), r"\1\2[redacted]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"), "[account]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[number]"),
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), "[number]"),
    (re.compile(r"\b\d{8,}\b"), "[number]"),
    (re.compile(r"\b[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*[A-Za-z][A-Za-z0-9_-]{30,}\b|\b[A-Za-z0-9_-]{32,}\b"), "[token]"),
)


def scrub(text: str) -> str:
    """``text`` with emails, card/account numbers, password fields and tokens replaced. Idempotent."""
    out = str(text or "")
    for pattern, repl in _SCRUB:
        out = pattern.sub(repl, out)
    return out


# --------------------------------------------------------------------------------- models


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=64)
    ts: float
    source: Source
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    task_id: str | None = Field(default=None, max_length=128)
    outcome: str | None = Field(default=None, max_length=32)
    app: str | None = Field(default=None, max_length=200)
    duration_s: float | None = Field(default=None, ge=0)

    @field_validator("text", "app", "task_id")
    @classmethod
    def _scrubbed(cls, v: str | None) -> str | None:
        return None if v is None else scrub(v)


class Schedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    when: str
    cron: str


class ProfileItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Kind
    text: str
    evidence: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    last_seen: float
    app: str | None = None
    suggest_schedule: Schedule | None = None
    stats: dict[str, float | int | None] | None = None


# --------------------------------------------------------------------------------- text helpers

_WORD = re.compile(r"[a-z0-9$][a-z0-9$'.-]*")
_STOP = frozenset(
    "a an the to for of in on at by from with and or but me my mine i you your it its this that these those "
    "please pls can could would will should just some any all be is are was were do does did so as into "
    "up out about over again also then than now next time always never no not yes ok okay actually".split()
)
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _tokens(text: str) -> list[str]:
    return [w.strip(".-'") for w in _WORD.findall(text.lower()) if w.strip(".-'") and w.strip(".-'") not in _STOP]


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _overlap(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def app_key(app: str | None) -> str:
    """The slug the procedure/interface stores use, lowercased: joins app names across sources."""
    s = _SLUG.sub("-", (app or "").strip()).strip("-.")
    return s.lower()


def _hid(prefix: str, *parts: Any) -> str:
    return f"{prefix}-" + hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _norm_prompt(text: str) -> str:
    return " ".join(re.sub(r"[^\w$ -]+", " ", text.lower()).split())


def _hour_label(hour: int) -> str:
    if hour == 0:
        return "12am"
    if hour < 12:
        return f"{hour}am"
    if hour == 12:
        return "12pm"
    return f"{hour - 12}pm"


_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _local(ts: float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ts)


def _day_phrase(weekdays: set[int]) -> tuple[str, str]:
    """(human phrase, cron day-of-week) for the set of python weekdays a pattern was seen on."""
    if len(weekdays) == 1:
        d = next(iter(weekdays))
        return f"on {_DAYS[d]}s", str((d + 1) % 7)
    if weekdays <= {0, 1, 2, 3, 4}:
        return "on weekdays", "1-5"
    if weekdays <= {5, 6}:
        return "on weekends", "0,6"
    return "daily", "*"


_CORRECTION_LEAD = re.compile(r"^(?:no[,.!]?\s+|nope[,.!]?\s+|actually[,.!]?\s+|next time[,.]?\s+|please\s+|from now on[,.]?\s+)+", re.I)

# (kind, pattern, normalizer) — a "choice" is a constraint phrase inside a task request.
_CHOICES: tuple[tuple[str, re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    ("budget", re.compile(r"\b(?:under|below|less than|at most|max(?:imum)?|no more than|cheaper than)\s+\$\s?(\d[\d,]*(?:\.\d+)?)", re.I),
     lambda m: f"under ${m.group(1).replace(',', '')}"),
    ("format", re.compile(r"\b(?:as|in|to|into)\s+(?:an?\s+)?(pdf|csv|docx|markdown|xlsx|json|png|jpe?g|txt|html)\b", re.I),
     lambda m: f"{m.group(1).upper()} output"),
    ("naming", re.compile(r"\b(?:yyyy-mm-dd|\d{4}-\d{2}-\d{2})\b", re.I), lambda m: "YYYY-MM-DD names"),
    ("seat", re.compile(r"\b(window|aisle)\s+seats?\b", re.I), lambda m: f"{m.group(1).lower()} seats"),
    ("stops", re.compile(r"\b(nonstop|non-stop|direct)\b", re.I), lambda m: "nonstop"),
    ("tone", re.compile(r"\bin\s+an?\s+(\w+)\s+(?:tone|style)\b", re.I), lambda m: f"a {m.group(1).lower()} tone"),
    ("theme", re.compile(r"\b(dark|light)\s+(?:mode|theme)\b", re.I), lambda m: f"{m.group(1).lower()} mode"),
    ("via", re.compile(r"\b(?:using|via|with|in)\s+(chrome|firefox|safari|edge|gmail|outlook|slack|excel|sheets|libreoffice|vscode|figma|notion)\b", re.I),
     lambda m: f"using {m.group(1).capitalize()}"),
)


def task_choices(text: str) -> tuple[list[str], list[str]]:
    """(normalized choices found in a task request, its topic tokens with the choice spans removed)."""
    choices: list[str] = []
    rest = text
    for _kind, pattern, norm in _CHOICES:
        for m in pattern.finditer(text):
            c = norm(m)
            if c not in choices:
                choices.append(c)
        rest = pattern.sub(" ", rest)
    return choices, [t for t in _tokens(rest) if not t.startswith("$") and not t.isdigit()]


# --------------------------------------------------------------------------------- store

MERGE_SYSTEM_PROMPT = (
    "You merge near-duplicate statements about one user's preferences. Reply with exactly one JSON object "
    "{\"merge\": [{\"ids\": [\"<id>\", ...], \"text\": \"<one statement>\"}]}. Merge only items that say the same "
    "thing; each group has at least two ids; text at most 160 characters and adds no new facts. "
    "Reply {\"merge\": []} when nothing is a duplicate. No prose."
)


class LearningStore:
    """``observations.jsonl`` (the record) + ``profile.json`` (a cache derived from it)."""

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        screenpipe_db: str | os.PathLike[str] | None = None,
        interfaces: Any = None,
        procedures: Any = None,
        model: Any = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "observations.jsonl"
        self.profile_path = self.dir / "profile.json"
        self.interfaces = interfaces
        self.procedures = procedures
        self.model = model
        self.now = now
        self.screenpipe = None
        if screenpipe_db:
            from .context import ScreenpipeTelemetry

            self.screenpipe = ScreenpipeTelemetry(screenpipe_db)
        self._lock = threading.RLock()
        self._profile: dict[str, Any] | None = None
        self._merges: list[dict[str, Any]] = []
        self._merge_thread: threading.Thread | None = None
        self._merge_asked: frozenset[str] = frozenset()
        self._load_cached()

    # ------------------------------------------------------------------ record

    def _load_cached(self) -> None:
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._profile = data
            self._merges = [m for m in data.get("merges") or [] if isinstance(m, dict)]

    def observations(self) -> list[Observation]:
        if not self.path.is_file():
            return []
        out: list[Observation] = []
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return []
        for line in lines:
            if not line.strip():
                continue
            try:
                out.append(Observation.model_validate_json(line))
            except Exception:
                continue
        return out

    def _append(self, obs: Iterable[Observation]) -> None:
        rows = [o.model_dump_json(exclude_none=True) for o in obs]
        if not rows:
            return
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(rows) + "\n")

    def observe(
        self,
        source: str,
        text: str,
        task_id: str | None = None,
        outcome: str | None = None,
        app: str | None = None,
        duration_s: float | None = None,
        ts: float | None = None,
        derive: bool = True,
    ) -> Observation:
        """Scrub, append one observation, and (by default) re-derive the profile."""
        limit = SCREEN_TEXT if source == "screen" else MAX_TEXT
        clean = scrub(" ".join(str(text or "").split()))[:limit].strip()
        if not clean:
            raise ValueError("observation text is empty")
        obs = Observation(
            id="obs-" + uuid.uuid4().hex[:16],
            ts=float(self.now() if ts is None else ts),
            source=source,  # type: ignore[arg-type]
            text=clean,
            task_id=task_id or None,
            outcome=(str(outcome).strip().lower()[:32] or None) if outcome is not None else None,
            app=(scrub(app)[:200] or None) if app else None,
            duration_s=None if duration_s is None else max(0.0, float(duration_s)),
        )
        self._append([obs])
        if derive:
            self.derive()
        return obs

    def forget(self, item_id: str) -> Observation:
        return self.observe("own", f"forget {item_id}")

    def confirm(self, item_id: str) -> Observation:
        return self.observe("own", f"confirm {item_id}")

    # ------------------------------------------------------------------ screen ingestion

    def ingest_screen(self, existing: set[str] | None = None) -> int:
        """Turn settled screenpipe app-focus sessions into ``screen`` observations (deduped by a
        deterministic id). Only the app name, the first window name and the duration are kept."""
        sp = self.screenpipe
        if sp is None or not getattr(sp, "available", False):
            return 0
        try:
            rows = sp.recent(int(HABIT_WINDOW_S))
        except Exception:
            return 0
        now = float(self.now())
        frames: list[tuple[float, str, str]] = []
        seen: set[tuple[float, str]] = set()
        for r in rows:
            ts, app = float(r.get("ts") or 0), str(r.get("app") or "").strip()
            if not app or (ts, app) in seen:
                continue
            seen.add((ts, app))
            frames.append((ts, app, str(r.get("window") or "").strip()))
        frames.sort()
        sessions: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        for i, (ts, app, win) in enumerate(frames):
            nxt = frames[i + 1][0] if i + 1 < len(frames) else None
            step = (nxt - ts) if nxt is not None and nxt - ts <= SCREEN_GAP_S else 0.0
            if cur is not None and cur["app"] == app and ts - cur["last"] <= SCREEN_GAP_S:
                cur["dur"] += step
                cur["last"] = ts
            else:
                cur = {"app": app, "start": ts, "last": ts, "dur": step, "window": win}
                sessions.append(cur)
        if existing is None:
            existing = {o.id for o in self.observations()}
        new: list[Observation] = []
        for s in sessions:
            if s["dur"] < SCREEN_MIN_SESSION_S or s["last"] + SCREEN_SETTLE_S > now:
                continue
            oid = _hid("scr", s["app"], round(s["start"]))
            if oid in existing:
                continue
            text = f"focus {s['app']}" + (f": {s['window']}" if s["window"] else "")
            new.append(Observation(id=oid, ts=float(s["start"]), source="screen", text=scrub(text)[:SCREEN_TEXT],
                                   app=s["app"][:200], duration_s=round(float(s["dur"]), 1)))
            existing.add(oid)
        self._append(new)
        return len(new)

    # ------------------------------------------------------------------ derivation

    def derive(self, ingest_screen: bool = True) -> dict[str, Any]:
        """Rebuild ``profile.json`` from the record. Deterministic; never calls a model."""
        with self._lock:
            if ingest_screen:
                self.ingest_screen()
            obs = self.observations()
            now = float(self.now())
            items = self._derive_items(obs, now)
            profile = {
                "version": 1,
                "derived_at": now,
                "observations": len(obs),
                "items": [i.model_dump(mode="json", exclude_none=True) for i in items],
                "merges": list(self._merges),
            }
            self._write_profile(profile)
            self._profile = profile
            return profile

    def _write_profile(self, profile: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(prefix="profile.", suffix=".tmp", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(profile, indent=2, sort_keys=True))
            os.replace(tmp, self.profile_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _control(o: Observation) -> tuple[str, str] | None:
        if o.source != "own":
            return None
        parts = o.text.split()
        if len(parts) == 2 and parts[0] in ("forget", "confirm"):
            return parts[0], parts[1]
        return None

    def _derive_items(self, obs: list[Observation], now: float) -> list[ProfileItem]:
        controls = [(o, c) for o in obs if (c := self._control(o)) is not None]
        data = [o for o in obs if self._control(o) is None]
        suppressed: set[str] = set()
        forgotten: dict[str, float] = {}
        confirms: Counter[str] = Counter()
        for o, (verb, item_id) in sorted(controls, key=lambda p: p[0].ts):
            if verb == "confirm":
                confirms[item_id] += 1
                continue
            at = [d for d in data if d.ts <= o.ts and d.id not in suppressed]
            for item in self._items(at, o.ts):
                if item.id == item_id:
                    suppressed.update(item.evidence)
                    break
            forgotten[item_id] = o.ts
        active = [d for d in data if d.id not in suppressed]
        by_id = {d.id: d for d in active}
        out: list[ProfileItem] = []
        for item in self._items(active, now):
            if item.id in forgotten and not any(by_id[e].ts > forgotten[item.id] for e in item.evidence if e in by_id):
                continue
            if confirms[item.id]:
                item = item.model_copy(update={"confidence": round(min(CONFIDENCE_CAP, item.confidence + CONFIRM_STEP * confirms[item.id]), 3)})
            out.append(item)
        return out

    def _items(self, obs: list[Observation], now: float) -> list[ProfileItem]:
        prefs = self._apply_merges(self._preferences(obs))
        return prefs + self._screen_habits(obs, now) + self._suggestions(obs) + self._app_skills(obs)

    # ---- preferences

    def _preferences(self, obs: list[Observation]) -> list[ProfileItem]:
        items: list[ProfileItem] = []
        clusters: list[dict[str, Any]] = []
        for o in sorted((o for o in obs if o.source == "correction"), key=lambda o: o.ts):
            text = _CORRECTION_LEAD.sub("", o.text).strip() or o.text
            text = text[:1].upper() + text[1:]
            toks = _tokens(text)
            for c in clusters:
                if _jaccard(toks, c["tokens"]) >= MERGE_JACCARD:
                    c["obs"].append(o)
                    c["text"] = text  # the newest phrasing wins
                    break
            else:
                clusters.append({"tokens": toks, "text": text, "obs": [o]})
        for c in clusters:
            first = c["obs"][0]
            n = len(c["obs"])
            items.append(ProfileItem(
                id=_hid("pref", "correction", first.id), kind="preference", text=c["text"][:300],
                evidence=tuple(o.id for o in c["obs"]),
                confidence=round(min(CONFIDENCE_CAP, CORRECTION_CONFIDENCE + 0.03 * (n - 1)), 3),
                last_seen=max(o.ts for o in c["obs"]), app=c["obs"][-1].app,
            ))
        # repeated task choices: >= 2 similar tasks carrying the same choice
        by_choice: dict[str, list[tuple[Observation, list[str]]]] = defaultdict(list)
        for o in sorted((o for o in obs if o.source == "task"), key=lambda o: o.ts):
            choices, topic = task_choices(o.text)
            for c in choices:
                by_choice[c].append((o, topic))
        for choice, rows in by_choice.items():
            groups: list[list[tuple[Observation, list[str]]]] = []
            for row in rows:
                for g in groups:
                    if _overlap(row[1], g[0][1]) >= TOPIC_OVERLAP:
                        g.append(row)
                        break
                else:
                    groups.append([row])
            for g in groups:
                if len(g) < 2:
                    continue
                counts = Counter(t for _, topic in g for t in set(topic))
                order = [t for _, topic in g for t in topic]
                shared = list(dict.fromkeys(t for t in order if counts[t] * 2 >= len(g) and counts[t] >= 2))
                topic_text = " ".join(shared[:4]) or "similar"
                items.append(ProfileItem(
                    id=_hid("pref", "choice", choice, " ".join(sorted(shared[:4]))), kind="preference",
                    text=f"prefers {choice} for {topic_text} tasks", evidence=tuple(o.id for o, _ in g),
                    confidence=round(min(0.85, CHOICE_CONFIDENCE + 0.05 * (len(g) - 2)), 3),
                    last_seen=max(o.ts for o, _ in g), app=g[-1][0].app,
                ))
        return items

    def _apply_merges(self, prefs: list[ProfileItem]) -> list[ProfileItem]:
        if not self._merges:
            return prefs
        by_id = {p.id: p for p in prefs}
        used: set[str] = set()
        merged: list[ProfileItem] = []
        for m in self._merges:
            ids = [i for i in m.get("ids") or [] if i in by_id and i not in used]
            if len(ids) < 2 or len(ids) != len(m.get("ids") or []):
                continue
            group = [by_id[i] for i in ids]
            used.update(ids)
            merged.append(ProfileItem(
                id=sorted(ids)[0], kind="preference", text=scrub(str(m.get("text") or group[0].text))[:300],
                evidence=tuple(dict.fromkeys(e for p in group for e in p.evidence)),
                confidence=max(p.confidence for p in group), last_seen=max(p.last_seen for p in group),
                app=next((p.app for p in group if p.app), None),
            ))
        return [p for p in prefs if p.id not in used] + merged

    # ---- habits

    def _screen_habits(self, obs: list[Observation], now: float) -> list[ProfileItem]:
        sessions = sorted((o for o in obs if o.source == "screen" and o.app and now - HABIT_WINDOW_S <= o.ts <= now
                           and (o.duration_s or 0) >= SCREEN_MIN_SESSION_S), key=lambda o: o.ts)
        items: list[ProfileItem] = []
        buckets: dict[tuple[str, int], dict[_dt.date, list[Observation]]] = defaultdict(lambda: defaultdict(list))
        for o in sessions:
            d = _local(o.ts)
            buckets[(str(o.app), d.hour)][d.date()].append(o)
        for (app, hour), days in sorted(buckets.items()):
            if len(days) < HABIT_MIN_DAYS:
                continue
            phrase, _ = _day_phrase({day.weekday() for day in days})
            ev = [o for day in sorted(days) for o in days[day]]
            items.append(ProfileItem(
                id=_hid("habit", "focus", app_key(app), hour, phrase), kind="habit",
                text=f"opens {app} ~{_hour_label(hour)} {phrase}", evidence=tuple(o.id for o in ev),
                confidence=round(min(0.9, 0.4 + 0.1 * len(days)), 3), last_seen=max(o.ts for o in ev), app=app,
            ))
        pairs: dict[tuple[str, str], dict[_dt.date, list[Observation]]] = defaultdict(lambda: defaultdict(list))
        for a, b in zip(sessions, sessions[1:]):
            if a.app == b.app or (a.duration_s or 0) < SEQUENCE_MIN_FIRST_S:
                continue
            if 0 <= b.ts - (a.ts + (a.duration_s or 0)) <= SEQUENCE_GAP_S and _local(a.ts).date() == _local(b.ts).date():
                pairs[(str(a.app), str(b.app))][_local(a.ts).date()].extend([a, b])
        ranked = sorted(((k, v) for k, v in pairs.items() if len(v) >= HABIT_MIN_DAYS), key=lambda kv: (-len(kv[1]), kv[0]))
        for (a, b), days in ranked[:MAX_SEQUENCE_HABITS]:
            ev = list(dict.fromkeys(o.id for day in sorted(days) for o in days[day]))
            items.append(ProfileItem(
                id=_hid("habit", "seq", app_key(a), app_key(b)), kind="habit", text=f"opens {b} after {a}",
                evidence=tuple(ev), confidence=round(min(0.85, 0.35 + 0.1 * len(days)), 3),
                last_seen=max(o.ts for day in days.values() for o in day), app=b,
            ))
        return items

    def _suggestions(self, obs: list[Observation]) -> list[ProfileItem]:
        groups: dict[str, list[Observation]] = defaultdict(list)
        for o in obs:
            if o.source == "task":
                key = _norm_prompt(o.text)
                if key:
                    groups[key].append(o)
        items: list[ProfileItem] = []
        for key, rows in sorted(groups.items()):
            rows.sort(key=lambda o: o.ts)
            by_date: dict[_dt.date, Observation] = {}
            for o in rows:
                by_date.setdefault(_local(o.ts).date(), o)
            if len(by_date) < SUGGEST_MIN:
                continue
            per_weekday: dict[int, list[Observation]] = defaultdict(list)
            for day, o in by_date.items():
                per_weekday[day.weekday()].append(o)
            wd, hits = max(per_weekday.items(), key=lambda kv: (len(kv[1]), -kv[0]))
            if len(hits) >= SUGGEST_MIN:
                chosen, days = hits, {wd}
            else:
                chosen, days = list(by_date.values()), {d.weekday() for d in by_date}
            hours = sorted(_local(o.ts).hour for o in chosen)
            hour = hours[len(hours) // 2]
            if sum(1 for h in hours if abs(h - hour) <= 1) * 2 < len(hours):
                continue  # no consistent time of day: nothing to schedule
            phrase, dow = _day_phrase(days)
            prompt = rows[-1].text
            when = f"{'every ' + phrase[3:-1] if len(days) == 1 else phrase} ~{_hour_label(hour)}"
            items.append(ProfileItem(
                id=_hid("sugg", key, when), kind="habit", text=f"runs \"{prompt[:120]}\" {when}",
                evidence=tuple(o.id for o in rows), confidence=round(min(0.9, 0.45 + 0.1 * len(chosen)), 3),
                last_seen=rows[-1].ts, app=rows[-1].app,
                suggest_schedule=Schedule(prompt=prompt, when=when, cron=f"0 {hour} * * {dow}"),
            ))
        return items

    # ---- app skills

    def _app_skills(self, obs: list[Observation]) -> list[ProfileItem]:
        stats: dict[str, dict[str, Any]] = {}

        def row(key: str, name: str) -> dict[str, Any]:
            return stats.setdefault(key, {"name": name, "ok": 0, "failed": 0, "obs": [], "last": 0.0})

        for o in obs:
            if o.source not in ("task", "own") or not o.app or not o.outcome:
                continue
            key = app_key(o.app)
            if not key:
                continue
            r = row(key, o.app)
            if o.outcome in ("ok", "success", "succeeded", "pass", "passed", "done", "true"):
                r["ok"] += 1
            else:
                r["failed"] += 1
            r["obs"].append(o.id)
            r["last"] = max(r["last"], o.ts)
        for key, name, fit in self._interface_fitness():
            r = row(key, name)
            r["fitness"] = fit
        for key, name, n_goals, heals in self._procedure_counts():
            r = row(key, name)
            r["procedures"], r["heals"] = n_goals, heals
        items: list[ProfileItem] = []
        for key, r in sorted(stats.items()):
            n = r["ok"] + r["failed"]
            parts: list[str] = []
            scores: list[float] = []
            if n:
                parts.append(f"{r['ok']}/{n} tasks ok")
                scores.append((r["ok"] + 1) / (n + 2))
            fit = r.get("fitness")
            if fit and fit.get("replayed"):
                parts.append(f"interface v{fit['version']} fitness {fit['passed']}/{fit['replayed']}")
                scores.append(fit["passed"] / fit["replayed"])
            if r.get("procedures"):
                parts.append(f"{r['procedures']} saved procedures" + (f", {r['heals']} healed" if r.get("heals") else ""))
            if not parts:
                continue
            items.append(ProfileItem(
                id=_hid("skill", key), kind="app_skill", text=f"{r['name']}: " + "; ".join(parts),
                evidence=tuple(r["obs"]), confidence=round(sum(scores) / len(scores), 3) if scores else 0.5,
                last_seen=r["last"], app=r["name"],
                stats={"tasks": n, "ok": r["ok"], "failed": r["failed"],
                       "success_rate": round(r["ok"] / n, 3) if n else None,
                       "interface_version": fit.get("version") if fit else None,
                       "fitness_passed": fit.get("passed") if fit else None,
                       "fitness_replayed": fit.get("replayed") if fit else None,
                       "procedures": r.get("procedures", 0), "heals": r.get("heals", 0)},
            ))
        return items

    def _resolve(self, provider: Any) -> Any:
        if provider is None:
            return None
        try:
            return provider() if callable(provider) and not hasattr(provider, "list") and not hasattr(provider, "versions") else provider
        except Exception:
            return None

    def _interface_fitness(self) -> list[tuple[str, str, dict[str, Any]]]:
        store = self._resolve(self.interfaces)
        root = Path(getattr(store, "dir", "")) if store is not None else None
        if root is None or not root.is_dir():
            return []
        out = []
        try:
            for p in sorted(root.iterdir()):
                if not p.is_dir():
                    continue
                latest = store.latest(p.name)
                if latest is None:
                    continue
                fit = store.last_fitness(p.name, latest) or {}
                out.append((app_key(p.name), p.name, {"version": latest, "passed": int(fit.get("passed", 0) or 0),
                                                      "replayed": int(fit.get("replayed", 0) or 0)}))
        except Exception:
            return out
        return out

    def _procedure_counts(self) -> list[tuple[str, str, int, int]]:
        store = self._resolve(self.procedures)
        if store is None:
            return []
        try:
            rows = list(store.list())
        except Exception:
            return []
        goals: dict[str, set[str]] = defaultdict(set)
        heals: Counter[str] = Counter()
        names: dict[str, str] = {}
        for p in rows:
            app = str(getattr(p, "app", "") or "")
            key = app_key(app)
            if not key:
                continue
            names.setdefault(key, app)
            goals[key].add(str(getattr(p, "goal", "")))
            if getattr(p, "healed_step", None) is not None:
                heals[key] += 1
        return [(k, names[k], len(goals[k]), heals[k]) for k in sorted(goals)]

    # ------------------------------------------------------------------ optional model merge

    def _local_model(self) -> Any:
        fn = getattr(self.model, "local_only", None)
        if not callable(fn):
            return None  # only a client that can drop its cloud fallback is used here
        try:
            return fn()
        except Exception:
            return None

    def merge_with_model(self) -> int:
        """One local-only ``complete_json`` call that may merge near-duplicate preferences. Returns
        the number of merge groups applied; 0 (and no call) when no local model is configured."""
        local = self._local_model()
        if local is None:
            return 0
        prefs = [i for i in self.items() if i["kind"] == "preference"]
        if len(prefs) < 2:
            return 0
        asked = frozenset(p["id"] for p in prefs)
        if asked == self._merge_asked:
            return 0  # this exact set was already offered to the model
        self._merge_asked = asked
        user = json.dumps([{"id": p["id"], "text": p["text"]} for p in prefs][:40])
        try:
            raw = json.loads(local.complete_json(MERGE_SYSTEM_PROMPT, user))
        except Exception:
            return 0
        known = {p["id"] for p in prefs}
        groups = []
        for g in (raw.get("merge") if isinstance(raw, dict) else None) or []:
            if not isinstance(g, dict):
                continue
            ids = [i for i in g.get("ids") or [] if isinstance(i, str)]
            text = g.get("text")
            if len(set(ids)) >= 2 and set(ids) <= known and isinstance(text, str) and text.strip():
                groups.append({"ids": sorted(set(ids)), "text": scrub(text.strip())[:200]})
        if not groups:
            return 0
        with self._lock:
            self._merges = [m for m in self._merges if not set(m["ids"]) & {i for g in groups for i in g["ids"]}] + groups
            self.derive(ingest_screen=False)
        return len(groups)

    def merge_async(self) -> bool:
        """Start ``merge_with_model`` on a daemon thread when a local model exists and none is running."""
        if self._local_model() is None:
            return False
        with self._lock:
            if self._merge_thread is not None and self._merge_thread.is_alive():
                return False
            t = threading.Thread(target=self._merge_quiet, name="o-learn-merge", daemon=True)
            self._merge_thread = t
        t.start()
        return True

    def _merge_quiet(self) -> None:
        try:
            self.merge_with_model()
        except Exception:
            pass

    # ------------------------------------------------------------------ reading

    def items(self) -> list[dict[str, Any]]:
        profile = self._profile if self._profile is not None else self.derive()
        return [dict(i) for i in profile.get("items") or []]

    def profile(self, topic: str | None = None, app: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """``{preferences, habits, app_skills, suggestions}``; ``topic`` filters by case-insensitive
        substring of an item's text or app; ``app`` keeps app skills (and app-bound items) for that app."""
        needle = (topic or "").strip().lower()
        key = app_key(app) if app else ""
        out: dict[str, list[dict[str, Any]]] = {"preferences": [], "habits": [], "app_skills": [], "suggestions": []}
        for it in sorted(self.items(), key=lambda i: (-float(i.get("confidence", 0)), i.get("id", ""))):
            if needle and needle not in str(it.get("text", "")).lower() and needle not in str(it.get("app") or "").lower():
                continue
            if key and it.get("app") and app_key(it.get("app")) != key:
                continue
            if it["kind"] == "preference":
                out["preferences"].append(it)
            elif it["kind"] == "app_skill":
                out["app_skills"].append(it)
            elif it.get("suggest_schedule"):
                out["suggestions"].append(it)
            else:
                out["habits"].append(it)
        return out

    def item(self, item_id: str) -> dict[str, Any] | None:
        return next((i for i in self.items() if i.get("id") == item_id), None)

    def render_for_prompt(self, app: str | None = None, topic: str | None = None, max_lines: int = PROMPT_LINES) -> str:
        """At most ``max_lines`` lines: the user's relevant preferences, then O's record in ``app``."""
        try:
            items = self.items()
        except Exception:
            return ""
        needle = (topic or "").strip().lower()
        key = app_key(app) if app else ""
        prefs = [i for i in items if i["kind"] == "preference" and float(i.get("confidence", 0)) >= PROMPT_MIN_CONFIDENCE
                 and (not needle or needle in i["text"].lower())
                 and (not key or not i.get("app") or app_key(i.get("app")) == key)]
        prefs.sort(key=lambda i: (-float(i["confidence"]), -float(i.get("last_seen", 0))))
        skills = [i for i in items if i["kind"] == "app_skill" and key and app_key(i.get("app")) == key]
        if not prefs and not skills:
            return ""
        lines = ["Learned from this user's past behaviour (follow unless the goal says otherwise):"]
        room = max(0, max_lines - 1 - (1 if skills else 0))
        lines += [f"- {p['text'][:160]}" for p in prefs[:room]]
        if skills and len(lines) < max_lines:
            lines.append(f"O's record here: {skills[0]['text'][:200]}")
        return "\n".join(lines[:max_lines])


class LearnedNotes:
    """Wraps ``InterfaceNotes`` so ``for_prompt(app)`` (the planner's and the cu graph's notes seam)
    also carries ``LearningStore.render_for_prompt(app)``. Everything else is delegated unchanged."""

    def __init__(self, inner: Any, learning: LearningStore) -> None:
        self._inner = inner
        self._learning = learning

    def for_prompt(self, app: str, *args: Any, **kwargs: Any) -> str:
        base = ""
        fn = getattr(self._inner, "for_prompt", None)
        if callable(fn):
            base = fn(app, *args, **kwargs) or ""
        try:
            learned = self._learning.render_for_prompt(app=app)
        except Exception:
            learned = ""
        return "\n\n".join(p for p in (base, learned) if p)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
