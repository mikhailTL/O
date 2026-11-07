"""Deliberate practice (plan L2): O gets faster and cheaper at its own tasks while the user is away.

``Practicer.run`` takes items from the ``Curriculum`` queue and runs each one with the real
computer-use graph (``cu.graph``: planner, grounder, judge, procedure store, heal loop) on an
isolated display, then:

1. checks the end state on disk (``curriculum.evaluate_check``; no model call): an attempt passes only
   when the graph reports ok AND the ground-truth check holds;
2. records the outcome: ``Curriculum.record`` (spaced repetition), ``fitness.jsonl`` here, the app's
   newest interface ``fitness.jsonl`` for replays (``InterfaceStore.write_fitness``), one ``own``
   learning observation (feeds the app_skill profile), and ``SkillRegistry.observe`` (promotion);
3. on a pass, distils the procedure the graph stored into a new version (``distill``): screenshot
   questions (paid judge calls) dropped, consecutive typing merged, and the item's ground-truth check
   appended as a ``state_assert`` so the next replay is verified by state with zero model calls. The
   new version is appended with ``parent`` lineage; nothing is ever rewritten;
4. on a failure, appends the lesson to the app's interface notes (the planner reads them on the next
   fresh plan) and ``lessons.jsonl``; when a replayed version that had passed before now fails, the last
   passing script is re-appended as the newest version (a revert is a new version, never a delete). A
   stored version the ground-truth check proved wrong (graph ok, file wrong) or whose replay failed goes
   into ``quarantine.jsonl``; the practice graph's procedure view (``QuarantinedProcedures``) skips it, so
   the next attempt replays the newest good version or plans fresh. The quarantined file stays on disk.
   Lessons come from deterministic rules over the failure (``Practicer.rules``) plus what the isolated
   display shows right after it (``_diagnose``: does the target exist now, editable/dialog landmarks).

Guards, all checked before every item and enforced, not advisory:

* presence: only while the user is away (``presence_fn() == USER_ABSENT``, the ambient presence
  machine), or idle for ``idle_s`` by OS idle time when presence is unknown;
* display: the environment must be an isolated display that is running and is not the host's DISPLAY;
  there is no code path that drives the host display;
* spend: a rolling 24 h budget (setting ``practiceBudgetUsd``) in ``ledger.jsonl``; each item runs under a
  ``DollarCappedMeter`` that refuses the next paid call once the item's $ or call cap (or the remaining
  budget) is used; the meter's reported cost is what goes in the ledger;
* time: an item that runs past its ``max_seconds`` is recorded as a failure.

Nothing here installs, downloads or runs third-party code; practice uses only O's existing tools.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from ..cu import meter as cu_meter
from .curriculum import Attempt, Curriculum, PracticeItem, evaluate_check, reset_workspace

ABSENT = "USER_ABSENT"
UNKNOWN = "UNKNOWN"
DEFAULT_BUDGET_USD = 0.50
BUDGET_WINDOW_S = 86400.0
DEFAULT_IDLE_S = 600.0
PRACTICE_BASE_DISPLAY = 140  # isolated practice displays live at :140+, away from the job slots (:99+)
SPI_LAUNCHER = "/usr/libexec/at-spi-bus-launcher"


class PracticeRefused(RuntimeError):
    """A guard said no (user present, host display, budget spent, no isolated display)."""


# ------------------------------------------------------------------------------- spend

class DollarCappedMeter(cu_meter.JobMeter):
    """A ``JobMeter`` that also refuses the next paid call once ``max_usd`` has been spent."""

    def __init__(self, job_id: str, max_paid: int | None, max_usd: float) -> None:
        super().__init__(job_id, max_paid)
        self.max_usd = max(0.0, float(max_usd))

    @property
    def exhausted(self) -> bool:
        return super().exhausted or self.cost_usd >= self.max_usd

    def charge_paid(self, kind: str) -> None:
        if self.cost_usd >= self.max_usd:
            with self._lock:
                self.refused += 1
            raise cu_meter.ModelCallCapReached(self, kind)
        super().charge_paid(kind)


class SpendLedger:
    """``ledger.jsonl``: one ``{ts, item_id, cost_usd, model_calls}`` line per attempt. Append-only."""

    def __init__(self, path: str | os.PathLike[str], now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now
        self._lock = threading.Lock()

    def add(self, item_id: str, cost_usd: float, model_calls: int) -> None:
        row = {"ts": float(self.now()), "item_id": item_id, "cost_usd": round(float(cost_usd), 6), "model_calls": int(model_calls)}
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    def spent(self, window_s: float = BUDGET_WINDOW_S) -> float:
        if not self.path.is_file():
            return 0.0
        cutoff = float(self.now()) - window_s
        total = 0.0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if float(row.get("ts", 0)) >= cutoff:
                total += float(row.get("cost_usd", 0.0))
        return round(total, 6)


def budget_from_settings(settings: Any = None) -> float:
    """``practiceBudgetUsd``: EngineSettings.practice_budget_usd, else env O_PRACTICE_BUDGET_USD, else 0.50."""
    for name in ("practice_budget_usd", "practiceBudgetUsd"):
        v = getattr(settings, name, None) if settings is not None else None
        if isinstance(v, (int, float)) and v >= 0:
            return float(v)
    try:
        return max(0.0, float(os.environ.get("O_PRACTICE_BUDGET_USD", DEFAULT_BUDGET_USD)))
    except ValueError:
        return DEFAULT_BUDGET_USD


# ------------------------------------------------------------------------------- distillation

def _steps(raw: dict) -> list[dict]:
    return list(raw.get("steps") or [])


def check_to_state(check: Any) -> list[dict]:
    """The item's ground-truth check as ``StateCheck`` dicts (``file_contains`` per line / ``file_exists``)."""
    if check is None:
        return []
    if check.kind in ("file_lines", "file_contains") and check.expect:
        return [{"kind": "file_contains", "arg": check.path, "expect": s} for s in check.expect]
    if check.kind == "file_exists":
        return [{"kind": "file_exists", "arg": check.path}]
    return []


def distill(raw: dict, check: Any = None) -> tuple[dict | None, list[str]]:
    """A cheaper equivalent of a passing script, or (None, []) when nothing can be removed or added.

    * ``screenshot_assert`` steps go (each one is a judge question, paid when no local VLM answers);
    * two ``type`` steps in a row into the same ref become one;
    * the item's ground-truth check is appended as one ``state_assert`` unless the script has one, so a
      replay is decided by state (``StateJudge``) instead of a final screenshot question.
    The result is re-parsed by ``cu.schema.parse_script``; an invalid result is discarded."""
    from ..cu.schema import parse_script

    steps = _steps(raw)
    out: list[dict] = []
    notes: list[str] = []
    for s in steps:
        if s.get("op") == "screenshot_assert":
            notes.append("dropped screenshot_assert")
            continue
        if (s.get("op") == "type" and out and out[-1].get("op") == "type" and out[-1].get("ref") == s.get("ref")):
            out[-1] = {**out[-1], "text": str(out[-1].get("text", "")) + str(s.get("text", ""))}
            notes.append("merged consecutive type")
            continue
        out.append(dict(s))
    checks = check_to_state(check)
    if checks and not any(s.get("op") == "state_assert" for s in out):
        out.append({"op": "state_assert", "checks": checks})
        notes.append(f"added state_assert ({len(checks)} check(s))")
    if not notes or not out:
        return None, []
    candidate = {**raw, "steps": out}
    try:
        parsed = parse_script(candidate)
    except Exception as e:  # noqa: BLE001
        return None, [f"distilled script invalid: {e}"]
    from ..cu.planner import script_to_dict

    return script_to_dict(parsed), notes


# ------------------------------------------------------------------------------- environments

class PracticeEnvironment:
    """What a practice item runs in. ``display`` is the isolated X display name; ``backend`` the CU backend
    bound to it. Subclasses must make ``is_isolated`` true only for a running non-host display."""

    display: str = ""
    backend: Any = None
    desktop_home: str | None = None

    @property
    def is_isolated(self) -> bool:
        return False

    def start(self) -> None:
        pass

    def reset(self, item: PracticeItem) -> None:
        """Fresh state for one attempt: the item's workspace recreated, the item's apps closed."""
        reset_workspace(item.environment)

    def stop(self) -> None:
        pass


def _host_display() -> str:
    return os.environ.get("DISPLAY", "").split(".")[0]


class IsolatedPracticeEnvironment(PracticeEnvironment):
    """Xvfb (``display.IsolatedDisplay``) + a private D-Bus session + the AT-SPI bus + the AT-SPI backend.

    Linux only. Apps launched by practice inherit a throwaway profile (``GSETTINGS_BACKEND=memory`` and
    XDG dirs under ``profile_dir``) so no user settings are read or written."""

    def __init__(self, display: str = f":{PRACTICE_BASE_DISPLAY}", profile_dir: str | os.PathLike[str] | None = None,
                 desktop_home: str | None = None, pygi: str | None = None) -> None:
        from ..display import IsolatedDisplay

        self.display = display
        self.xdisplay = IsolatedDisplay(display)
        self.profile_dir = Path(profile_dir) if profile_dir else Path(f"/tmp/o-practice-profile{display.replace(':', '-')}")
        self.desktop_home = desktop_home
        self.pygi = pygi or os.environ.get("O_PYGI", "/opt/pygi")
        self.backend = None
        self._procs: list[subprocess.Popen] = []
        self._bus_pid: int | None = None
        self._saved_env: dict[str, str | None] = {}

    @property
    def is_isolated(self) -> bool:
        host = _host_display()
        return (not self.xdisplay.is_host) and self.xdisplay.running and self.display.split(".")[0] != host

    def start(self) -> None:
        if not sys.platform.startswith("linux"):
            raise PracticeRefused("isolated practice displays need Linux + Xvfb")
        if self.xdisplay.is_host or self.display.split(".")[0] == _host_display():
            raise PracticeRefused(f"{self.display} is the host display")
        if os.path.exists(f"/tmp/.X{self.display[1:]}-lock"):
            raise PracticeRefused(f"display {self.display} is already in use")
        self.xdisplay.start()
        if not self.xdisplay.running:
            raise PracticeRefused(f"Xvfb did not start on {self.display}")
        if self.pygi and os.path.isdir(self.pygi) and self.pygi not in sys.path:
            sys.path.append(self.pygi)
        bus = subprocess.run(["dbus-daemon", "--session", "--fork", "--print-address=1", "--print-pid=1"],
                             env={**os.environ, "DISPLAY": self.display}, capture_output=True, text=True, check=True)
        addr, pid = bus.stdout.strip().splitlines()[:2]
        self._bus_pid = int(pid)
        prof = self.profile_dir
        env_updates = {"DBUS_SESSION_BUS_ADDRESS": addr, "ACCESSIBILITY_ENABLED": "1", "GSETTINGS_BACKEND": "memory",
                       "XDG_CONFIG_HOME": str(prof / "config"), "XDG_DATA_HOME": str(prof / "data"),
                       "XDG_CACHE_HOME": str(prof / "cache")}
        for k, v in env_updates.items():
            self._saved_env[k] = os.environ.get(k)
            os.environ[k] = v
        self._saved_env["NO_AT_BRIDGE"] = os.environ.pop("NO_AT_BRIDGE", None)
        spi_env = {**os.environ, "DISPLAY": self.display}
        self._procs.append(subprocess.Popen([SPI_LAUNCHER, "--launch-immediately"], env=spi_env, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True))
        time.sleep(1.5)
        from ..cu.backends.atspi import AtspiBackend

        self.backend = AtspiBackend(display=self.display, allow_pixels=True)

    def close_apps(self, apps: Iterable[str]) -> int:
        """Kill windows of ``apps`` on THIS display only (xdotool scoped to ``self.display``)."""
        if not self.is_isolated or not shutil.which("xdotool"):
            return 0
        killed = 0
        env = {**os.environ, "DISPLAY": self.display}
        for app in apps:
            out = subprocess.run(["xdotool", "search", "--class", f"^{app}$"], env=env, capture_output=True, text=True).stdout.split()
            pids = set()
            for wid in out:
                p = subprocess.run(["xdotool", "getwindowpid", wid], env=env, capture_output=True, text=True).stdout.strip()
                if p.isdigit():
                    pids.add(int(p))
            for pid in pids:
                try:
                    os.kill(pid, 15)
                    killed += 1
                except OSError:
                    pass
        if killed:
            time.sleep(1.0)
        return killed

    def reset(self, item: PracticeItem) -> None:
        self.close_apps(item.environment.apps)
        super().reset(item)

    def stop(self) -> None:
        for p in self._procs:
            try:
                os.killpg(p.pid, 15)
            except OSError:
                pass
        self._procs = []
        if self._bus_pid:
            try:
                os.kill(self._bus_pid, 15)
            except OSError:
                pass
            self._bus_pid = None
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._saved_env = {}
        self.xdisplay.stop()


class Quarantine:
    """``quarantine.jsonl``: procedure versions practice proved wrong (the graph said ok, the ground-truth
    check said no; or a replay failed). Append-only; a quarantined version stays on disk untouched."""

    def __init__(self, path: str | os.PathLike[str], now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now

    def add(self, procedure_id: str, reason: str) -> None:
        if not procedure_id or procedure_id in self.ids():
            return
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": float(self.now()), "procedure_id": procedure_id, "reason": reason[:500]}, sort_keys=True) + "\n")

    def ids(self) -> set[str]:
        if not self.path.is_file():
            return set()
        out = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.add(str(json.loads(line)["procedure_id"]))
            except (ValueError, KeyError):
                continue
        return out


class QuarantinedProcedures:
    """A ``ProcedureStore`` view for the practice graph: ``latest`` skips quarantined versions (the newest
    good one is replayed, or none -> a fresh plan). Every write goes to the real store unchanged."""

    def __init__(self, inner: Any, quarantine: Quarantine) -> None:
        self.inner = inner
        self.quarantine = quarantine

    def latest(self, app: str, goal: str) -> Any:
        bad = self.quarantine.ids()
        for proc in reversed(self.inner.versions(app, goal)):
            if proc.id not in bad:
                return proc
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def practice_deps(base: Any, env: PracticeEnvironment, *, notes: Any = None, quarantine: Quarantine | None = None) -> Any:
    """``base`` (the engine's ``CUDeps``) re-pointed at the practice environment: one slot, its backend, its
    display; the shared planner / judge / grounder / procedure store are kept so practice learning is the
    same memory real jobs use (behind the practice quarantine when one is given). The interface builder and
    best-of-N are off (spend)."""
    from ..cu.judge import StateJudge

    home = env.desktop_home
    procs = base.procedures
    if quarantine is not None and procs is not None:
        procs = QuarantinedProcedures(procs, quarantine)
    return dataclasses.replace(
        base,
        procedures=procs,
        backend_factory=lambda slot: env.backend,
        displays=[env.display],
        candidates=1,
        step_candidates=1,
        interface_builder=None,
        state_judge_factory=lambda sandbox, backend: StateJudge(sandbox, backend, desktop_home=home),
        notes=notes if notes is not None else base.notes,
    )


# ------------------------------------------------------------------------------- the loop

def _paid(snapshot: dict) -> int:
    return int(snapshot.get("paid", 0)) if isinstance(snapshot, dict) else 0


class Practicer:
    def __init__(
        self,
        curriculum: Curriculum,
        dir: str | os.PathLike[str],
        *,
        env: PracticeEnvironment,
        deps: Any = None,
        graph: Any = None,
        run_job: Callable[..., dict] | None = None,
        budget_usd: float = DEFAULT_BUDGET_USD,
        presence_fn: Callable[[], str] | None = None,
        idle_fn: Callable[[], float | None] | None = None,
        idle_s: float = DEFAULT_IDLE_S,
        skills: Any = None,
        learning: Any = None,
        interfaces: Any = None,
        now: Callable[[], float] = time.time,
        clock: Callable[[], float] = time.monotonic,
        max_heals: int = 2,
    ) -> None:
        self.curriculum = curriculum
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.env = env
        self.deps = deps
        self.graph = graph
        self.run_job = run_job or self._default_run_job
        self.budget_usd = max(0.0, float(budget_usd))
        self.presence_fn = presence_fn
        self.idle_fn = idle_fn
        self.idle_s = idle_s
        self.skills = skills
        self.learning = learning
        self.interfaces = interfaces
        self.now = now
        self.clock = clock
        self.max_heals = max_heals
        self.ledger = SpendLedger(self.dir / "ledger.jsonl", now=now)
        self.fitness_path = self.dir / "fitness.jsonl"
        self.lessons_path = self.dir / "lessons.jsonl"
        self.quarantine = Quarantine(self.dir / "quarantine.jsonl", now=now)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ guards

    def user_away(self) -> tuple[bool, str]:
        state = UNKNOWN
        if self.presence_fn is not None:
            try:
                state = str(self.presence_fn() or UNKNOWN)
            except Exception:  # noqa: BLE001
                state = UNKNOWN
        if state == ABSENT:
            return True, "user absent"
        if state != UNKNOWN:
            return False, f"presence is {state}"
        idle = None
        if self.idle_fn is not None:
            try:
                idle = self.idle_fn()
            except Exception:  # noqa: BLE001
                idle = None
        if idle is not None and idle >= self.idle_s:
            return True, f"idle {idle:.0f}s >= {self.idle_s:.0f}s"
        return False, "presence unknown and not idle long enough" if idle is not None else "presence unknown"

    def remaining_usd(self) -> float:
        return round(max(0.0, self.budget_usd - self.ledger.spent()), 6)

    def can_practice(self) -> tuple[bool, str]:
        away, why = self.user_away()
        if not away:
            return False, why
        if not self.env.is_isolated:
            return False, "no isolated practice display (practice never uses the host display)"
        if self.remaining_usd() <= 0:
            return False, f"practice budget spent (${self.budget_usd:.2f} per 24h)"
        return True, why

    # ------------------------------------------------------------------ one attempt

    def _default_run_job(self, item: PracticeItem, job_id: str, meter: cu_meter.JobMeter) -> dict:
        from ..cu.graph import _run_cu_job, build_cu_graph

        if self.graph is None:
            if self.deps is None:
                raise PracticeRefused("no computer-use deps to practise with")
            self.graph = build_cu_graph(self.deps)
        with cu_meter.metered(meter):
            return _run_cu_job(self.graph, item.goal, item.app, job_id, 1, self.max_heals, None, None)

    def _procedures(self) -> Any:
        """The real procedure store (unwrapped from the practice quarantine view)."""
        store = getattr(self.deps, "procedures", None) if self.deps is not None else None
        return getattr(store, "inner", store)

    def attempt(self, item: PracticeItem) -> Attempt:
        ok, why = self.can_practice()
        if not ok:
            raise PracticeRefused(why)
        n = self.curriculum.next_attempt_number(item.id)
        job_id = f"practice-{item.id[3:11]}-{n}-{int(self.now())}"
        remaining = self.remaining_usd()
        meter = DollarCappedMeter(job_id, item.budget.max_model_calls, min(item.budget.max_usd, remaining))
        self.env.reset(item)
        t0 = self.clock()
        error = None
        try:
            state = self.run_job(item, job_id, meter)
        except Exception as e:  # noqa: BLE001 - a crashed attempt is a failed attempt
            state = {"ok": False, "error": f"{type(e).__name__}: {e}", "steps": [], "cached": False}
        seconds = self.clock() - t0
        snap = meter.snapshot()
        check_ok, check_reason = evaluate_check(item.check)
        graph_ok = bool(state.get("ok"))
        over_time = seconds > item.budget.max_seconds
        passed = graph_ok and check_ok and not over_time
        if not passed:
            error = state.get("error") or ("" if check_ok else f"state check failed: {check_reason}")
            if over_time:
                error = f"over time ({seconds:.0f}s > {item.budget.max_seconds:.0f}s); {error or ''}".strip("; ")
            if graph_ok and not check_ok:
                error = f"graph reported ok but the ground-truth check failed: {check_reason}"
        pid = state.get("procedure_id")
        steps = len(state.get("steps") or [])
        distilled = None
        if passed:
            distilled = self._distill(item, pid)
        else:
            self._lesson(item, n, error or "failed", state, check_ok)
            if pid and (graph_ok or state.get("cached")):
                # the stored version is proven wrong: never replay it in practice again (it stays on disk)
                self.quarantine.add(pid, f"{item.family} attempt {n}: {error or 'failed'}")
            self._revert_if_regressed(item, pid)
        outcome = Attempt(item_id=item.id, family=item.family, attempt=n, ts=float(self.now()), ok=passed,
                          graph_ok=graph_ok, check_ok=check_ok, steps=steps, model_calls=_paid(snap),
                          cost_usd=round(meter.cost_usd, 6), seconds=round(seconds, 3), cached=bool(state.get("cached")),
                          procedure_id=pid, distilled_id=distilled, error=(error or None) and str(error)[:500])
        self.ledger.add(item.id, meter.cost_usd, _paid(snap))
        self.curriculum.record(outcome)
        self._fitness(item, outcome)
        return outcome

    # ------------------------------------------------------------------ learning from the attempt

    def _distill(self, item: PracticeItem, pid: str | None) -> str | None:
        store = self._procedures()
        if store is None:
            return None
        proc = store.get(pid) if pid else store.latest(item.app, item.goal)
        if proc is None:
            proc = store.latest(item.app, item.goal)
        if proc is None:
            return None
        try:
            from ..cu.planner import script_to_dict

            raw = script_to_dict(store.load_script(proc))
        except Exception:  # noqa: BLE001
            return None
        new, notes = distill(raw, item.check)
        if new is None:
            return None
        stored = store.store(proc.app, proc.goal, new, parent=proc.id, reason=("practice distill: " + "; ".join(sorted(set(notes))))[:500])
        return stored.id

    def _revert_if_regressed(self, item: PracticeItem, pid: str | None) -> str | None:
        """A replayed version failed after an earlier version of this goal passed: append the last passing
        script as the newest version (lineage: parent = the failing one)."""
        store = self._procedures()
        if store is None:
            return None
        passing = [a for a in self.curriculum.attempts(item.id) if a.ok and (a.distilled_id or a.procedure_id)]
        if not passing:
            return None
        good_id = passing[-1].distilled_id or passing[-1].procedure_id
        latest = store.latest(item.app, item.goal)
        if latest is None or latest.id == good_id:
            return None
        good = store.get(good_id)
        if good is None:
            return None
        from ..cu.planner import script_to_dict

        try:
            raw = script_to_dict(store.load_script(good))
            if script_to_dict(store.load_script(latest)) == raw:
                return None  # the passing script itself failed this time: quarantine only, no duplicate version
        except Exception:  # noqa: BLE001
            return None
        stored = store.store(item.app, item.goal, raw, parent=latest.id, reason=f"practice revert: {latest.id} failed; restoring {good_id}")
        return stored.id

    def _diagnose(self, failed_cmd: str) -> str:
        """What the isolated display shows right after the failure, so the next plan does not repeat it:
        whether the target a failed find/wait named exists NOW (a timing mistake) and the editable /
        dialog landmarks of the tree (a wrong-selector mistake). Read-only; never raises."""
        backend = getattr(self.env, "backend", None)
        if backend is None:
            return ""
        out: list[str] = []
        m = re.search(r"role='([^']+)'", failed_cmd or "") or re.search(r"target=([^:]+):", failed_cmd or "")
        if m and ("find" in failed_cmd or "wait" in failed_cmd):
            try:
                from ..cu.schema import Target

                hit = backend.find(Target(role=m.group(1)))
            except Exception:  # noqa: BLE001
                hit = None
            if hit is not None:
                out.append(f"role='{m.group(1)}' exists now: it appeared only after the previous step settled, so add a "
                           f"wait for it before the find")
            else:
                out.append(f"no node has role='{m.group(1)}'")
        marks: list[str] = []

        def walk(node: Any, depth: int = 0) -> None:
            if len(marks) >= 8 or depth > 40:
                return
            role = str(getattr(node, "role", "") or "")
            states = set(getattr(node, "states", None) or ())
            if "editable" in states or role in ("dialog", "file chooser", "alert", "entry", "text"):
                marks.append(f"{role} '{str(getattr(node, 'name', '') or '')[:40]}'" + (" [editable]" if "editable" in states else ""))
            for c in getattr(node, "children", None) or ():
                walk(c, depth + 1)

        try:
            for root in backend.tree() or ():
                walk(root)
        except Exception:  # noqa: BLE001
            pass
        if marks:
            out.append("tree landmarks: " + ", ".join(marks))
        return "; ".join(out)

    @staticmethod
    def rules(item: PracticeItem, error: str, steps: list[dict], check_ok: bool = False) -> list[str]:
        """Deterministic reflection over one failed attempt: failure classes seen in real practice runs,
        each turned into an instruction for the next plan."""
        out: list[str] = []
        cmds = [str(s.get("command", "")) for s in steps]
        first_fail = next((i for i, s in enumerate(steps) if s.get("exit_code") not in (0, None)), None)
        before = cmds[: first_fail if first_fail is not None else len(cmds)]
        if item.environment.apps and not any(c.startswith("launch") for c in before):
            out.append(f"the task starts with no {item.environment.apps[0]} window open: launch {item.environment.apps[0]} "
                       f"first, then wait for its window")
        if "SandboxEscape" in error or "outside sandbox" in error:
            out.append("a script step cannot write outside its sandbox: make the change and save it through the "
                       "app's own GUI (keyboard shortcut or File menu dialog)")
        if "ground-truth check failed" in error:
            out.append("the app looked done but the file on disk was wrong: check the saved file's exact contents "
                       "(state_assert file_contains) before reporting done")
        if item.check is not None and item.check.kind == "file_lines" and item.check.expect:
            try:
                got = [ln.rstrip() for ln in Path(item.check.path).read_text(encoding="utf-8").strip("\n").splitlines()]
            except OSError:
                got = []
            want = list(item.check.expect)
            if len(got) > len(want) and got[-len(want):] == want:
                out.append("the new text was added to the old text instead of replacing it: select all (ctrl+a) in the "
                           "text area before typing the replacement")
        if check_ok and "ground-truth" not in error:
            out.append("the end state on disk was already right; what failed was the plan's own check or the step "
                       "budget: verify a saved file with state_assert file_contains on the expected lines (no shell "
                       "probes such as grep -c, whose exit code is 1 when the count is 0) and stop once it holds")
        if "model-call cap" in error:
            out.append("the attempt ran out of model calls: plan fewer, larger turns")
        return out

    def _lesson(self, item: PracticeItem, n: int, error: str, state: dict, check_ok: bool = False) -> None:
        steps = list(state.get("steps") or [])
        failed = [s for s in steps if s.get("exit_code") not in (0, None)]
        cmd = str(failed[-1].get("command", ""))[:160] if failed else ""
        step_txt = f" at '{cmd}'" if failed else ""
        seen = self._diagnose(cmd)
        rules = self.rules(item, error, steps, check_ok)
        text = (f"practice pitfall ({item.family}, attempt {n}): failed{step_txt}: {error[:300]}"
                + (f". Seen: {seen}" if seen else "") + ("".join(f"\nLesson: {r}" for r in rules)))
        row = {"ts": float(self.now()), "item_id": item.id, "family": item.family, "app": item.app, "attempt": n, "lesson": text}
        with self._lock, self.lessons_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        notes = getattr(self.deps, "notes", None) if self.deps is not None else None
        if notes is not None:
            try:
                notes.append(item.app, text, goal=item.goal[:200])
            except Exception:  # noqa: BLE001
                pass

    def _fitness(self, item: PracticeItem, a: Attempt) -> None:
        row = {"ts": a.ts, "item_id": item.id, "family": item.family, "app": item.app, "attempt": a.attempt, "ok": a.ok,
               "cached": a.cached, "procedure_id": a.procedure_id, "model_calls": a.model_calls, "cost_usd": a.cost_usd,
               "seconds": a.seconds, "steps": a.steps}
        with self._lock, self.fitness_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        if a.cached and self.interfaces is not None:
            try:
                if self.interfaces.latest(item.app) is not None:
                    self.interfaces.write_fitness(item.app, 1, int(a.ok))
            except Exception:  # noqa: BLE001
                pass
        if self.learning is not None:
            try:
                self.learning.observe("own", f"practice {item.family}: {'ok' if a.ok else 'failed'} in {a.steps} steps, "
                                      f"{a.model_calls} model calls, {a.seconds:.0f}s", task_id=f"practice-{item.id}",
                                      outcome="ok" if a.ok else "failed", app=item.app, duration_s=a.seconds)
            except Exception:  # noqa: BLE001
                pass
        if self.skills is not None:
            try:
                self.skills.observe(item, a)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ session

    def run(self, max_items: int = 10, max_seconds: float = 1800.0, items: list[PracticeItem] | None = None) -> dict:
        """Practise due items (or ``items``) until the queue, time, budget or presence says stop."""
        t0 = self.clock()
        done: list[Attempt] = []
        stopped = "queue empty"
        queue = items if items is not None else [it for it, _ in self.curriculum.queue()]
        for item in queue:
            if len(done) >= max_items:
                stopped = "max items"
                break
            if self.clock() - t0 >= max_seconds:
                stopped = "session time"
                break
            ok, why = self.can_practice()
            if not ok:
                stopped = why
                break
            if not item.runnable:
                continue
            done.append(self.attempt(item))
        return {"attempts": [a.model_dump() for a in done], "stopped": stopped, "spent_24h_usd": self.ledger.spent(),
                "budget_usd": self.budget_usd, "curves": self.curriculum.learning_curve()}
