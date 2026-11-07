"""Practice curriculum (plan L2): what O should practise next, learned from its own experience.

A human gets better at a task by noticing where they failed or were slow and practising exactly
that, with the hard items coming back soon and the easy ones only as occasional re-checks. This
module builds that queue for O from telemetry that already exists; it never runs anything itself.

Sources (each optional; a missing file is no items, never an error):

(a) experience: ``learning`` observations (``source == "task"``: goal, app, outcome, duration) joined by
    job id with the action log's ``report`` rows (``model_calls``, ``cost_usd``, ``cached``) -> one item
    per failed goal and per slow (>= ``slow_s``) or expensive (>= ``expensive_usd`` / ``expensive_calls``)
    success; healed procedure versions (lineage says the stored script broke once) and interface
    versions whose last ``fitness.jsonl`` line is below 100 % -> re-verification items.
(b) capability gaps: ``capability_map.json`` subsystems that no practice attempt has measured yet raise
    the priority of the built-in families tagged with them.
(c) task families: the built-in local families (``FAMILIES``: a text editor on local temp files built
    from ``tools/e2e/fixtures.py``) and the offline eval episodes (``engine/tests/fixtures/offline``;
    screenshot-only, so recorded as not runnable with the reason).

Every item names its goal, its environment (always an isolated display; local temp files only), a
success check that is evaluated against the real end state with no model call, and a time / $ /
model-call budget. Goals that would publish, send, buy, pay or create accounts are never runnable.

Spaced repetition (Leitner boxes): a failure resets the item to box 0 and brings it back after
``FAIL_INTERVAL_S``; each pass moves it up one box (``INTERVALS_S``); box >= ``MASTERED_BOX`` is
"mastered" and only re-checked rarely. ``interval_scale`` compresses time (0 = everything due now)
for a supervised session.

Storage under ``<dir>``: ``items.jsonl`` (item definitions; a newer line for the same id supersedes
the older one) and ``attempts.jsonl`` (one line per practice outcome). Both are append-only
(``open('a')``); nothing is ever rewritten or deleted. The schedule is derived from them on read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

ENGINE_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = ENGINE_DIR.parent
OFFLINE_EPISODES = ENGINE_DIR / "tests" / "fixtures" / "offline"
CAPABILITY_MAP = Path(__file__).resolve().parent / "capability_map.json"

FAIL_INTERVAL_S = 120.0
INTERVALS_S = (600.0, 3600.0, 6 * 3600.0, 86400.0, 3 * 86400.0, 7 * 86400.0, 30 * 86400.0)
MASTERED_BOX = 4
SLOW_S = 120.0
EXPENSIVE_USD = 0.25
EXPENSIVE_CALLS = 8
DEFAULT_MAX_SECONDS = 300.0
DEFAULT_MAX_USD = 0.40
DEFAULT_MAX_CALLS = 10

Origin = Literal["failure", "slow", "expensive", "healed", "fitness", "gap", "family", "fixture"]
ORIGIN_WEIGHT = {"failure": 6.0, "fitness": 5.0, "healed": 4.0, "expensive": 3.0, "slow": 3.0, "gap": 2.0,
                 "family": 1.0, "fixture": 0.5}
CheckKind = Literal["file_lines", "file_contains", "file_not_contains", "file_exists"]

# Never practised, whatever the source: publishing, messaging as the user, money, accounts, deleting user data.
UNSAFE_GOAL = re.compile(
    r"\b(buy|purchase|pay|payment|checkout|order|subscribe|sign[ -]?up|create (an )?account|register|log ?in|"
    r"send|email|e-mail|message|tweet|post|publish|upload|share|delete|remove|uninstall|install|transfer|wire)\b",
    re.I,
)
_HOME_PATH = re.compile(r"(~/[^\s'\"`,;)]+|/(?:home|root|Users)/[^\s'\"`,;)]+)")


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Check(_M):
    """A ground-truth end-state check, evaluated on disk with no model call."""

    kind: CheckKind
    path: str = Field(min_length=1)
    expect: tuple[str, ...] = ()  # file_lines: the exact lines; file_contains/not_contains: substrings


class Environment(_M):
    display: Literal["isolated"] = "isolated"  # practice never runs on the host display
    setup: str = "empty"  # a key of SETUPS
    workspace: str = ""  # absolute dir the setup (re)creates before every attempt
    apps: tuple[str, ...] = ()  # executables the item needs on the isolated display


class Budget(_M):
    max_seconds: float = Field(default=DEFAULT_MAX_SECONDS, gt=0)
    max_usd: float = Field(default=DEFAULT_MAX_USD, ge=0)
    max_model_calls: int = Field(default=DEFAULT_MAX_CALLS, ge=0)


class PracticeItem(_M):
    id: str
    family: str
    goal: str = Field(min_length=1, max_length=4000)
    app: str = Field(min_length=1)
    origin: Origin
    environment: Environment
    check: Check | None = None
    budget: Budget = Budget()
    subsystems: tuple[str, ...] = ()
    source_ref: str = ""  # job id / procedure id / episode name the item came from
    runnable: bool = True
    reason: str = ""  # why not runnable
    ts: float = 0.0


class Attempt(_M):
    item_id: str
    family: str
    attempt: int = Field(ge=1)
    ts: float
    ok: bool
    graph_ok: bool = False
    check_ok: bool = False
    steps: int = 0
    model_calls: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    cached: bool = False
    procedure_id: str | None = None
    distilled_id: str | None = None
    error: str | None = None


class Schedule(_M):
    item_id: str
    box: int
    attempts: int
    passes: int
    streak: int  # consecutive passes ending at the last attempt
    last_ts: float | None
    due_ts: float
    mastered: bool


# ------------------------------------------------------------------------------- families

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _setup_empty(ws: Path) -> dict:
    return {}


def _setup_notes(ws: Path) -> dict:
    from importlib import util

    spec = util.spec_from_file_location("o_e2e_fixtures", REPO_DIR / "tools" / "e2e" / "fixtures.py")
    if spec is None or spec.loader is None:  # the repo copy is missing: same three files, inline
        _write(ws / "notes" / "ideas.txt", "App ideas\n1. A pantry tracker that reads grocery receipts\n"
               "2. A study timer that blocks distracting sites\n3. Voice notes that turn into todo lists\n")
        _write(ws / "notes" / "reading-list.md", "# Reading\n- Designing Data-Intensive Applications (ch. 5 "
               "replication)\n- The Mom Test\n- Paul Graham: Do Things That Don't Scale\n")
        return {}
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.make_notes(ws)
    return {}


SETUPS: dict[str, Callable[[Path], dict]] = {"empty": _setup_empty, "notes": _setup_notes}


def reset_workspace(env: Environment) -> dict:
    """Recreate ``env.workspace`` from scratch for one attempt. Only ever touches a directory named by a
    practice item, and refuses anything that is not an absolute path containing a ``practice`` segment."""
    if not env.workspace:
        return {}
    ws = Path(env.workspace)
    if not ws.is_absolute() or "practice" not in ws.parts:
        raise ValueError(f"refusing to reset {ws}: not a practice workspace")
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    return SETUPS.get(env.setup, _setup_empty)(ws)


def _family_specs(ws_root: Path) -> list[dict]:
    """The built-in local task families: Mousepad on files in a practice workspace."""
    def ws(name: str) -> str:
        return str(ws_root / name)

    w1, w2, w3, w4 = ws("write-save"), ws("open-append"), ws("replace-line"), ws("two-files")
    return [
        {"family": "editor.write_save", "setup": "empty", "workspace": w1,
         "goal": (f"In the Mousepad text editor, type exactly these three lines, one per line: 'alpha one', "
                  f"'beta two', 'gamma three'. Then save the document as {w1}/lines.txt"),
         "check": Check(kind="file_lines", path=f"{w1}/lines.txt", expect=("alpha one", "beta two", "gamma three")),
         "subsystems": ("cu_planning", "cu_grounding", "cu_verification")},
        {"family": "editor.open_append", "setup": "notes", "workspace": w2,
         "goal": (f"Open the file {w2}/notes/ideas.txt in Mousepad, add one new last line "
                  f"'4. A habit tracker for practice sessions', and save the file under the same name."),
         "check": Check(kind="file_contains", path=f"{w2}/notes/ideas.txt",
                        expect=("App ideas", "3. Voice notes that turn into todo lists",
                                "4. A habit tracker for practice sessions")),
         "subsystems": ("cu_planning", "cu_backends", "cu_memory")},
        {"family": "editor.replace_line", "setup": "notes", "workspace": w3,
         "goal": (f"Open the file {w3}/notes/reading-list.md in Mousepad. Replace its whole contents with exactly "
                  f"these two lines: '# Reading' and '- Shape Up'. Save the file under the same name."),
         "check": Check(kind="file_lines", path=f"{w3}/notes/reading-list.md", expect=("# Reading", "- Shape Up")),
         "subsystems": ("cu_planning", "cu_verification")},
        {"family": "editor.save_as_copy", "setup": "notes", "workspace": w4,
         "goal": (f"Open the file {w4}/notes/ideas.txt in Mousepad and save a copy of it as {w4}/ideas-copy.txt "
                  f"(File > Save As), without changing its text."),
         "check": Check(kind="file_contains", path=f"{w4}/ideas-copy.txt",
                        expect=("App ideas", "1. A pantry tracker that reads grocery receipts")),
         "subsystems": ("cu_grounding", "cu_backends")},
    ]


def item_id(family: str, goal: str) -> str:
    return "pi-" + hashlib.sha256(f"{family}\n{goal}".encode()).hexdigest()[:16]


def evaluate_check(check: Check | None) -> tuple[bool, str]:
    """Ground truth for one attempt. No check -> (False, reason): an item without one is never 'passed'."""
    if check is None:
        return False, "item has no state check"
    p = Path(check.path)
    if check.kind == "file_exists":
        return (p.is_file(), "file present" if p.is_file() else "file not found")
    if not p.is_file():
        return False, f"{p} not found"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"unreadable: {e}"
    if check.kind == "file_lines":
        got = [ln.rstrip() for ln in text.strip("\n").splitlines()]
        want = list(check.expect)
        return (got == want, "lines match" if got == want else f"lines {got[:6]!r} != {want!r}")
    if check.kind == "file_contains":
        missing = [s for s in check.expect if s not in text]
        return (not missing, "all substrings present" if not missing else f"missing {missing!r}")
    present = [s for s in check.expect if s in text]
    return (not present, "none present" if not present else f"still present {present!r}")


# ------------------------------------------------------------------------------- curriculum

def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


class Curriculum:
    def __init__(
        self,
        dir: str | os.PathLike[str],
        *,
        data_dir: str | os.PathLike[str] | None = None,
        procedures: Any = None,
        interfaces: Any = None,
        capability_map: str | os.PathLike[str] | None = CAPABILITY_MAP,
        offline_episodes: str | os.PathLike[str] | None = OFFLINE_EPISODES,
        interval_scale: float = 1.0,
        now: Callable[[], float] = time.time,
        which: Callable[[str], str | None] = shutil.which,
        slow_s: float = SLOW_S,
        expensive_usd: float = EXPENSIVE_USD,
        expensive_calls: int = EXPENSIVE_CALLS,
    ) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.items_path = self.dir / "items.jsonl"
        self.attempts_path = self.dir / "attempts.jsonl"
        self.workspaces = self.dir / "ws"
        self.data_dir = Path(data_dir) if data_dir else None
        self.procedures = procedures
        self.interfaces = interfaces
        self.capability_map = Path(capability_map) if capability_map else None
        self.offline_episodes = Path(offline_episodes) if offline_episodes else None
        self.interval_scale = max(0.0, float(interval_scale))
        self.now = now
        self.which = which
        self.slow_s, self.expensive_usd, self.expensive_calls = slow_s, expensive_usd, expensive_calls
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ record (append-only)

    def items(self) -> dict[str, PracticeItem]:
        out: dict[str, PracticeItem] = {}
        for row in _read_jsonl(self.items_path):
            try:
                it = PracticeItem.model_validate(row)
            except Exception:
                continue
            out[it.id] = it  # newest line for an id supersedes older ones
        return out

    def attempts(self, item_id: str | None = None) -> list[Attempt]:
        out = []
        for row in _read_jsonl(self.attempts_path):
            try:
                a = Attempt.model_validate(row)
            except Exception:
                continue
            if item_id is None or a.item_id == item_id:
                out.append(a)
        return out

    def _append(self, path: Path, rows: Iterable[BaseModel]) -> None:
        lines = [r.model_dump_json() for r in rows]
        if not lines:
            return
        with self._lock, path.open("a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    def add(self, items: Iterable[PracticeItem]) -> list[PracticeItem]:
        """Append items that are new or changed (same id, different definition). Returns those written."""
        have = self.items()
        new = []
        for it in items:
            old = have.get(it.id)
            if old is not None and old.model_dump(exclude={"ts"}) == it.model_dump(exclude={"ts"}):
                continue
            new.append(it.model_copy(update={"ts": float(self.now())}))
        self._append(self.items_path, new)
        return new

    def record(self, outcome: Attempt) -> Schedule:
        self._append(self.attempts_path, [outcome])
        return self.schedule(outcome.item_id)

    def next_attempt_number(self, item_id: str) -> int:
        return len(self.attempts(item_id)) + 1

    # ------------------------------------------------------------------ spaced repetition

    def schedule(self, item_id: str, attempts: list[Attempt] | None = None) -> Schedule:
        rows = attempts if attempts is not None else self.attempts(item_id)
        box, streak, passes, last = 0, 0, 0, None
        due = 0.0
        for a in rows:
            last = a.ts
            if a.ok:
                passes += 1
                streak += 1
                due = a.ts + INTERVALS_S[min(box, len(INTERVALS_S) - 1)] * self.interval_scale
                box = min(box + 1, len(INTERVALS_S))
            else:
                streak = 0
                box = 0
                due = a.ts + FAIL_INTERVAL_S * self.interval_scale
        return Schedule(item_id=item_id, box=box, attempts=len(rows), passes=passes, streak=streak, last_ts=last,
                        due_ts=due, mastered=box >= MASTERED_BOX)

    def queue(self, limit: int | None = None, include_unrunnable: bool = False, due_only: bool = True) -> list[tuple[PracticeItem, Schedule]]:
        """Items in practice order: due first; then origin weight (failures first), lower box, longer overdue."""
        now = float(self.now())
        by_item: dict[str, list[Attempt]] = {}
        for a in self.attempts():
            by_item.setdefault(a.item_id, []).append(a)
        gaps = self.unmeasured_subsystems()
        rows = []
        for it in self.items().values():
            if not it.runnable and not include_unrunnable:
                continue
            sch = self.schedule(it.id, by_item.get(it.id, []))
            if due_only and sch.due_ts > now:
                continue
            weight = ORIGIN_WEIGHT.get(it.origin, 1.0) + (1.0 if gaps.intersection(it.subsystems) else 0.0)
            rows.append((it, sch, (-weight, sch.box, sch.due_ts)))
        rows.sort(key=lambda r: r[2])
        out = [(it, sch) for it, sch, _ in rows]
        return out[:limit] if limit is not None else out

    # ------------------------------------------------------------------ sources

    def family_items(self) -> list[PracticeItem]:
        out = []
        for spec in _family_specs(self.workspaces):
            missing = [a for a in ("mousepad",) if not self.which(a)]
            out.append(PracticeItem(
                id=item_id(spec["family"], spec["goal"]), family=spec["family"], goal=spec["goal"], app="mousepad",
                origin="family", environment=Environment(setup=spec["setup"], workspace=spec["workspace"], apps=("mousepad",)),
                check=spec["check"], subsystems=tuple(spec["subsystems"]), source_ref="evolve.curriculum.FAMILIES",
                runnable=not missing, reason=f"not installed: {', '.join(missing)}" if missing else ""))
        return out

    def fixture_items(self) -> list[PracticeItem]:
        """Offline eval episodes: recorded screenshots with no live app or state check -> not runnable here."""
        root = self.offline_episodes
        if root is None or not root.is_dir():
            return []
        out = []
        for d in sorted(p for p in root.iterdir() if (p / "meta.json").is_file()):
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                goal, app = str(meta["instruction"]), str(meta["app"])
            except Exception:
                continue
            exe = app.split("_")[0]
            have = bool(self.which(exe) or self.which(app))
            reason = "screenshot-only episode: no live environment or state check" + ("" if have else f"; {app} not installed")
            out.append(PracticeItem(
                id=item_id(f"offline.{d.name}", goal), family=f"offline.{app}", goal=goal, app=app, origin="fixture",
                environment=Environment(setup="empty", apps=(exe,)), check=None, subsystems=("cu_grounding",),
                source_ref=str(d.name), runnable=False, reason=reason))
        return out

    def _redirect(self, goal: str, ws: Path) -> tuple[str, list[str]] | None:
        """``goal`` with every home/absolute user path moved into the practice workspace (never the user's files),
        and those new paths. None when the goal is unsafe to practise."""
        if UNSAFE_GOAL.search(goal):
            return None
        moved: list[str] = []

        def sub(m: re.Match) -> str:
            name = os.path.basename(m.group(1).rstrip("/")) or "file"
            new = str(ws / name)
            moved.append(new)
            return new

        return _HOME_PATH.sub(sub, goal), moved

    def experience_items(self) -> list[PracticeItem]:
        """(a): failed goals and slow/expensive successes from the learning record + action log, plus healed
        procedures and interfaces below full fitness."""
        out: list[PracticeItem] = []
        if self.data_dir is None:
            return out
        reports: dict[str, dict] = {}
        for path in (self.data_dir / "engine-actions.jsonl",):
            for row in _read_jsonl(path):
                det = row.get("detail") or {}
                if row.get("kind") == "task" and isinstance(det, dict) and det.get("job_id") and "ok" in det:
                    reports[str(det["job_id"])] = det
        seen: set[str] = set()
        for row in _read_jsonl(self.data_dir / "learning" / "observations.jsonl") + _read_jsonl(self.data_dir / "observations.jsonl"):
            if row.get("source") != "task" or not row.get("text") or not row.get("app"):
                continue
            goal, app, outcome = str(row["text"]), str(row["app"]), str(row.get("outcome") or "")
            rep = reports.get(str(row.get("task_id") or ""), {})
            calls = rep.get("model_calls") or {}
            paid = int(calls.get("paid", 0)) if isinstance(calls, dict) else 0
            cost = float(rep.get("cost_usd") or 0.0)
            dur = float(row.get("duration_s") or 0.0)
            if outcome == "failed":
                origin = "failure"
            elif outcome == "ok" and not rep.get("cached") and (cost >= self.expensive_usd or paid >= self.expensive_calls):
                origin = "expensive"
            elif outcome == "ok" and not rep.get("cached") and dur >= self.slow_s:
                origin = "slow"
            else:
                continue
            fam = f"experience.{app}"
            key = item_id(fam, goal)
            if key in seen:
                continue
            seen.add(key)
            ws = self.workspaces / key
            red = self._redirect(goal, ws)
            if red is None:
                out.append(PracticeItem(id=key, family=fam, goal=goal, app=app, origin=origin,
                                        environment=Environment(workspace=str(ws), apps=(app,)), source_ref=str(row.get("task_id") or ""),
                                        runnable=False, reason="goal would publish, message, pay, install, delete or use an account"))
                continue
            pgoal, moved = red
            check = Check(kind="file_exists", path=moved[0]) if moved else None
            runnable = bool(check) and bool(self.which(app.split()[0]))
            reason = "" if runnable else ("no checkable end state (the goal names no file)" if not check else f"{app} not installed")
            out.append(PracticeItem(
                id=key, family=fam, goal=pgoal, app=app, origin=origin,
                environment=Environment(setup="empty", workspace=str(ws), apps=(app,)), check=check,
                budget=Budget(max_usd=min(DEFAULT_MAX_USD, max(0.1, cost * 1.5)) if cost else DEFAULT_MAX_USD),
                subsystems=("cu_planning",), source_ref=str(row.get("task_id") or ""), runnable=runnable, reason=reason))
        out.extend(self._lineage_items())
        return out

    def _lineage_items(self) -> list[PracticeItem]:
        out = []
        procs = self.procedures
        if procs is not None:
            try:
                listed = procs.list()
            except Exception:
                listed = []
            for p in listed:
                if getattr(p, "healed_step", None) is None:
                    continue
                fam = f"replay.{p.app}"
                key = item_id(fam, p.goal)
                out.append(PracticeItem(id=key, family=fam, goal=p.goal, app=p.app, origin="healed",
                                        environment=Environment(apps=(p.app,)), check=None, source_ref=p.id,
                                        subsystems=("cu_verification",), runnable=False,
                                        reason="re-verify by replay (procedure healed); needs a state check to practise"))
        store = self.interfaces
        if store is not None:
            try:
                apps = [p.name for p in Path(store.dir).iterdir() if p.is_dir()]
            except Exception:
                apps = []
            for app in apps:
                v = store.latest(app)
                fit = store.last_fitness(app, v) if v else None
                if fit and int(fit.get("replayed", 0)) > int(fit.get("passed", 0)):
                    fam = f"interface.{app}"
                    out.append(PracticeItem(id=item_id(fam, f"v{v}"), family=fam, goal=f"replay the {app} procedures (interface v{v})",
                                            app=app, origin="fitness", environment=Environment(apps=(app,)),
                                            source_ref=f"{app}/v{v}", subsystems=("cu_memory",), runnable=False,
                                            reason=f"fitness {fit.get('passed')}/{fit.get('replayed')}: replay through InterfaceBuilder.fitness"))
        return out

    def unmeasured_subsystems(self) -> set[str]:
        """(b): capability-map subsystems that no practice attempt has exercised yet."""
        if self.capability_map is None or not self.capability_map.is_file():
            return set()
        try:
            subs = set((json.loads(self.capability_map.read_text(encoding="utf-8")).get("subsystems") or {}).keys())
        except ValueError:
            return set()
        items = self.items()
        measured = {s for a in self.attempts() if a.item_id in items for s in items[a.item_id].subsystems}
        return subs - measured

    def gap_report(self) -> dict[str, list[str]]:
        """subsystem -> family ids that practise it (empty list = a gap no local family covers)."""
        gaps = self.unmeasured_subsystems()
        fams: dict[str, list[str]] = {s: [] for s in gaps}
        for it in self.family_items():
            for s in it.subsystems:
                if s in fams:
                    fams[s].append(it.family)
        return fams

    def refresh(self) -> dict[str, int]:
        """Rebuild the candidate set from every source and append what is new. Returns counts per origin."""
        gaps = self.unmeasured_subsystems()
        fam = [it.model_copy(update={"origin": "gap"}) if gaps.intersection(it.subsystems) and not self.attempts(it.id) else it
               for it in self.family_items()]
        all_items = self.experience_items() + fam + self.fixture_items()
        written = self.add(all_items)
        counts: dict[str, int] = {}
        for it in written:
            counts[it.origin] = counts.get(it.origin, 0) + 1
        return counts

    # ------------------------------------------------------------------ learning curve

    def learning_curve(self, family: str | None = None) -> dict[str, dict]:
        """Per family: attempts in order with success / steps / model calls / seconds / cost, and first->last."""
        items = self.items()
        curves: dict[str, dict] = {}
        for a in self.attempts():
            fam = a.family or (items[a.item_id].family if a.item_id in items else "?")
            if family and fam != family:
                continue
            c = curves.setdefault(fam, {"attempts": []})
            c["attempts"].append({"n": len(c["attempts"]) + 1, "ok": a.ok, "steps": a.steps, "model_calls": a.model_calls,
                                  "seconds": round(a.seconds, 2), "cost_usd": round(a.cost_usd, 6), "cached": a.cached,
                                  "procedure_id": a.procedure_id})
        for c in curves.values():
            rows = c["attempts"]
            first, last = rows[0], rows[-1]
            c["first"], c["last"] = first, last
            c["success_rate"] = round(sum(r["ok"] for r in rows) / len(rows), 3)
            c["improved"] = bool(last["ok"] and (not first["ok"] or last["model_calls"] < first["model_calls"]
                                                 or last["seconds"] < first["seconds"] or last["steps"] < first["steps"]))
        return curves
