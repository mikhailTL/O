"""Skill registry (plan L2): what O can do, earned by practice or approved by the user.

Two kinds of skill, both stored append-only (``open('a')``; the newest line for an id is its current
state, older lines are the history; nothing is rewritten or deleted):

* internal (``internal.jsonl``): a practised procedure. ``observe(item, attempt)`` is called by the
  practice loop after every attempt. A skill is ``candidate`` until it passes ``promote_after``
  consecutive attempts, then ``active``; an active skill whose pass rate over its last
  ``window`` attempts falls below ``demote_below`` becomes ``demoted`` (and can be promoted again by a
  new streak). Every status change is a new version line with the evidence that caused it.
* external (``external.jsonl``): a third-party tool O has only READ ABOUT, from evolve signals tagged
  ``skill`` (``ingest_signals``). It is stored as metadata plus a proposed adapter TEXT and starts at
  ``needs_approval`` (``blocked_license`` when the licence is not on ``OK_LICENSES``). This module never
  downloads, installs or executes anything. ``approve`` records the user's decision and is the only way
  to ``approved`` by the person. The one other way is ``auto_approve``, which the Evolver calls only while the
  person's own setting ``evolveAskBeforeInstall`` is off (recorded as ``decided_by="auto"``,
  ``decided_via="setting"``); a licence-blocked candidate can never be approved either way.
  ``install(id, installer)`` refuses anything not approved one of those two ways and only calls the ``installer``
  the caller passes in (``evolve/installer.py``: pinned MCP entry, sandbox self-test, provenance);
  ``uninstall(id, uninstaller)`` reverses an installed skill with a new ``uninstalled`` version line.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .schema import OK_LICENSES

PROMOTE_AFTER = 3
WINDOW = 5
DEMOTE_BELOW = 0.6

InternalStatus = Literal["candidate", "active", "demoted"]
ExternalStatus = Literal["needs_approval", "blocked_license", "approved", "rejected", "installed", "install_failed",
                         "uninstalled"]
AUTO_BY, AUTO_VIA = "auto", "setting"  # approvals made because the person turned "Ask before installing" off


class NotApproved(PermissionError):
    """An install was asked for a skill the user has not approved in the app."""


class InternalSkill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: int = Field(ge=1)
    status: InternalStatus
    family: str
    app: str
    goal: str
    procedure_id: str | None = None
    attempts: int = 0
    passes: int = 0
    streak: int = 0
    window_rate: float = 0.0
    best_model_calls: int | None = None
    best_seconds: float | None = None
    reason: str = ""
    ts: float = 0.0


class ExternalSkill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: int = Field(ge=1)
    status: ExternalStatus
    signal_id: str
    kind: str  # signal kind: mcp_server / repo_release / new_repo / package_release / ...
    title: str
    url: str
    repo: str = ""
    package: str = ""  # "npm:<name>" / "pypi:<name>" (installable) or a bare name (older records)
    release: str = ""  # the package release the signal named; the installer pins exactly this
    license: str = ""
    summary: str = ""
    proposed_adapter: str  # inert text for the user to read; never executed here
    decided_by: str | None = None
    decided_via: str | None = None
    note: str = ""
    ts: float = 0.0


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


_SAFE = re.compile(r"[^A-Za-z0-9._/@:+=-]")


def proposed_adapter(kind: str, title: str, url: str, repo: str, package: str, version: str) -> str:
    """Human-readable adapter proposal. Plain text; every value is sanitised so it cannot carry a payload."""
    repo_s, pkg_s, ver_s = _SAFE.sub("", repo)[:120], _SAFE.sub("", package)[:120], _SAFE.sub("", version)[:40]
    name = (pkg_s or repo_s.split("/")[-1] or "tool").lower()
    lines = [f"PROPOSAL ONLY: nothing below has been downloaded, installed or run. Source: {url[:300]}"]
    if kind == "mcp_server":
        lines += ["Adapter: register as an MCP server for Claude Code (server name below) after the user approves.",
                  f"  name: {name}", f"  source: {repo_s or pkg_s}{('@' + ver_s) if ver_s else ''}",
                  "  launch: decided at install time from the pinned release; run inside O's containment (no host display, no network beyond the allowlist)."]
    elif kind == "package_release" and pkg_s:
        lines += ["Adapter: a pinned dependency wrapped behind one O tool function after the user approves.",
                  f"  pin: {pkg_s}=={ver_s or '<release>'}", "  wrapper: o_engine tool with typed inputs/outputs; the evolve gates (tests, licence, secrets) run before it ships."]
    else:
        lines += ["Adapter: a CLI/library wrapped as one O tool after the user approves.",
                  f"  repo: {repo_s or '<unknown>'}{('@' + ver_s) if ver_s else ''}",
                  "  wrapper: o_engine tool with typed inputs/outputs; built by the evolve builder in a worktree and gated like any upgrade."]
    lines.append(f"  title: {title[:200]}")
    return "\n".join(lines)


class SkillRegistry:
    def __init__(self, dir: str | os.PathLike[str], *, promote_after: int = PROMOTE_AFTER, window: int = WINDOW,
                 demote_below: float = DEMOTE_BELOW, now: Callable[[], float] = time.time) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.internal_path = self.dir / "internal.jsonl"
        self.external_path = self.dir / "external.jsonl"
        self.history_path = self.dir / "attempts.jsonl"  # per-skill outcome history (append-only)
        self.promote_after = max(1, int(promote_after))
        self.window = max(1, int(window))
        self.demote_below = float(demote_below)
        self.now = now
        self._lock = threading.RLock()

    def _append(self, path: Path, row: dict) -> None:
        with self._lock, path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    # ------------------------------------------------------------------ internal

    def internal(self) -> dict[str, InternalSkill]:
        out: dict[str, InternalSkill] = {}
        for row in _read(self.internal_path):
            try:
                s = InternalSkill.model_validate(row)
            except Exception:
                continue
            out[s.id] = s
        return out

    def internal_history(self, skill_id: str) -> list[InternalSkill]:
        return [InternalSkill.model_validate(r) for r in _read(self.internal_path) if r.get("id") == skill_id]

    @staticmethod
    def skill_id(app: str, goal: str) -> str:
        return "sk-" + hashlib.sha256(f"{app}\n{goal}".encode()).hexdigest()[:16]

    def observe(self, item: Any, attempt: Any) -> InternalSkill:
        """Fold one practice attempt in; append a new version only when something about the skill changed."""
        sid = self.skill_id(item.app, item.goal)
        self._append(self.history_path, {"id": sid, "ts": float(attempt.ts), "ok": bool(attempt.ok),
                                         "model_calls": int(attempt.model_calls), "seconds": float(attempt.seconds),
                                         "procedure_id": attempt.distilled_id or attempt.procedure_id})
        hist = [r for r in _read(self.history_path) if r.get("id") == sid]
        streak = 0
        for r in reversed(hist):
            if not r.get("ok"):
                break
            streak += 1
        recent = hist[-self.window:]
        rate = sum(1 for r in recent if r.get("ok")) / len(recent)
        oks = [r for r in hist if r.get("ok")]
        prev = self.internal().get(sid)
        status: str = prev.status if prev else "candidate"
        reason = ""
        if status != "active" and streak >= self.promote_after:
            status, reason = "active", f"promoted: {streak} consecutive passes"
        elif status == "active" and len(recent) >= min(self.window, self.promote_after) and rate < self.demote_below:
            status, reason = "demoted", f"demoted: pass rate {rate:.2f} over last {len(recent)} < {self.demote_below}"
        pid = (attempt.distilled_id or attempt.procedure_id) if attempt.ok else (prev.procedure_id if prev else None)
        rec = InternalSkill(
            id=sid, version=(prev.version + 1) if prev else 1, status=status,  # type: ignore[arg-type]
            family=item.family, app=item.app, goal=item.goal, procedure_id=pid, attempts=len(hist), passes=len(oks),
            streak=streak, window_rate=round(rate, 3),
            best_model_calls=min((int(r["model_calls"]) for r in oks), default=None),
            best_seconds=min((float(r["seconds"]) for r in oks), default=None),
            reason=reason or (prev.reason if prev and prev.status == status else ""), ts=float(self.now()))
        if prev is None or rec.model_dump(exclude={"version", "ts"}) != prev.model_dump(exclude={"version", "ts"}):
            self._append(self.internal_path, rec.model_dump())
            return rec
        return prev

    def active(self) -> list[InternalSkill]:
        return [s for s in self.internal().values() if s.status == "active"]

    # ------------------------------------------------------------------ external (approval-gated)

    def external(self) -> dict[str, ExternalSkill]:
        out: dict[str, ExternalSkill] = {}
        for row in _read(self.external_path):
            try:
                s = ExternalSkill.model_validate(row)
            except Exception:
                continue
            out[s.id] = s
        return out

    def ingest_signals(self, signals: Iterable[Any]) -> list[ExternalSkill]:
        """Record every signal tagged ``skill`` (``"skill" in topics``) as a candidate. Idempotent per signal."""
        have = {s.signal_id for s in self.external().values()}
        out = []
        for sig in signals:
            get = (lambda k, d=None: sig.get(k, d)) if isinstance(sig, dict) else (lambda k, d=None: getattr(sig, k, d))
            topics = [str(t).lower() for t in (get("topics") or [])]
            if "skill" not in topics:
                continue
            sid_sig = str(get("id") or "")
            if not sid_sig or sid_sig in have:
                continue
            kind = str(get("kind") or "")
            repo = str(get("repo") or "")
            lic = str(get("license") or "")
            title = str(get("title") or repo or sid_sig)[:400]
            url = str(get("url") or "")
            package = str(get("package") or "") or (repo.split("/")[-1] if kind == "package_release" else "")
            status = "needs_approval" if lic in OK_LICENSES else "blocked_license"
            rec = ExternalSkill(
                id="xs-" + hashlib.sha256(sid_sig.encode()).hexdigest()[:16], version=1, status=status,  # type: ignore[arg-type]
                signal_id=sid_sig, kind=kind, title=title, url=url, repo=repo, package=package[:240],
                release=str(get("version") or "")[:64], license=lic,
                summary=str(get("summary") or "")[:2000],
                proposed_adapter=proposed_adapter(kind, title, url, repo, package.split(":", 1)[-1], str(get("version") or "")),
                note="" if status == "needs_approval" else f"licence {lic or 'unknown'} is not one of {', '.join(OK_LICENSES)}",
                ts=float(self.now()))
            self._append(self.external_path, rec.model_dump())
            have.add(sid_sig)
            out.append(rec)
        return out

    def pending(self) -> list[ExternalSkill]:
        return [s for s in self.external().values() if s.status == "needs_approval"]

    def _decide(self, skill_id: str, status: str, by: str, via: str, note: str) -> ExternalSkill:
        cur = self.external().get(skill_id)
        if cur is None:
            raise KeyError(skill_id)
        rec = cur.model_copy(update={"version": cur.version + 1, "status": status, "decided_by": by, "decided_via": via,
                                     "note": note[:500], "ts": float(self.now())})
        self._append(self.external_path, rec.model_dump())
        return rec

    def approve(self, skill_id: str, *, by: str, via: str, note: str = "") -> ExternalSkill:
        """The user's explicit approval, recorded. ``by`` must be ``"user"`` and ``via`` ``"app"``: an agent,
        a model reply or a signal can never approve. A licence-blocked candidate cannot be approved."""
        if by != "user" or via != "app":
            raise NotApproved("only the user can approve a skill, through the app")
        cur = self.external().get(skill_id)
        if cur is None:
            raise KeyError(skill_id)
        if cur.status != "needs_approval":
            raise NotApproved(f"{skill_id} is {cur.status}; only a needs_approval candidate can be approved")
        return self._decide(skill_id, "approved", by, via, note or "approved by the user in the app")

    def auto_approve(self, skill_id: str, *, ask_before_install: bool) -> ExternalSkill:
        """Approval on the person's behalf, only because they turned "Ask before installing new tools and
        upgrades" off. ``ask_before_install`` is that setting as the Evolver read it; True refuses."""
        if ask_before_install is not False:
            raise NotApproved("O asks before installing: only the user can approve this skill, in the app")
        cur = self.external().get(skill_id)
        if cur is None:
            raise KeyError(skill_id)
        if cur.status != "needs_approval" or cur.license not in OK_LICENSES:
            raise NotApproved(f"{skill_id} is {cur.status}; only a licensed needs_approval candidate can be approved")
        return self._decide(skill_id, "approved", AUTO_BY, AUTO_VIA, "approved automatically: Ask before installing is off")

    def reject(self, skill_id: str, *, by: str = "user", via: str = "app", note: str = "") -> ExternalSkill:
        return self._decide(skill_id, "rejected", by, via, note or "rejected")

    @staticmethod
    def _approved(s: ExternalSkill) -> bool:
        return s.status == "approved" and ((s.decided_by, s.decided_via) in (("user", "app"), (AUTO_BY, AUTO_VIA)))

    def install(self, skill_id: str, installer: Callable[[ExternalSkill], Any]) -> ExternalSkill:
        """Run the caller's ``installer`` for an approved skill and record ``installed`` (or ``install_failed``
        when the installer refuses: unpinnable, reserved name, secret, failed sandbox self-test). Refuses anything
        whose newest record is not ``approved`` by the user via the app or by ``auto_approve``. This module ships
        no installer."""
        cur = self.external().get(skill_id)
        if cur is None:
            raise KeyError(skill_id)
        if not self._approved(cur):
            raise NotApproved(f"{skill_id} is {cur.status}; the user must approve it in the app before anything is installed")
        if not callable(installer):
            raise NotApproved("no installer given")
        by, via = cur.decided_by or "user", cur.decided_via or "app"
        try:
            result = installer(cur)
        except Exception as exc:  # noqa: BLE001 - recorded; nothing was activated
            self._decide(skill_id, "install_failed", by, via, f"not installed: {type(exc).__name__}: {exc}"[:500])
            raise
        return self._decide(skill_id, "installed", by, via, f"installed: {str(result)[:300]}")

    def uninstall(self, skill_id: str, uninstaller: Callable[[ExternalSkill], Any], *, by: str = "user",
                  via: str = "app") -> ExternalSkill:
        """Reverse an installed skill: the caller's ``uninstaller`` supersedes its config entry; a new
        ``uninstalled`` version line records who did it. History is never rewritten."""
        cur = self.external().get(skill_id)
        if cur is None:
            raise KeyError(skill_id)
        if cur.status != "installed":
            raise NotApproved(f"{skill_id} is {cur.status}; only an installed skill can be removed")
        uninstaller(cur)
        return self._decide(skill_id, "uninstalled", by, via, "removed")

    def view(self) -> dict[str, Any]:
        """For an app panel: active/candidate/demoted internal skills and external candidates awaiting approval."""
        internal = sorted(self.internal().values(), key=lambda s: (s.status != "active", s.family))
        return {"internal": [s.model_dump() for s in internal],
                "external": [s.model_dump() for s in sorted(self.external().values(), key=lambda s: s.ts)],
                "pending_approval": len(self.pending())}
