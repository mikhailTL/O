"""Baseline metrics for the current main (plan L, stage 5 "no metric regression vs the stored baseline").

``compute_baseline(repo_root)`` measures one checkout, with no diff applied:

* engine pytest: passed / failed / skipped / errors counts plus the failing and passing test ids
  (from a JUnit XML report written outside the checkout);
* node selftests: every ``main/tools/selftest*.js`` and ``main/tools/e2e-*.js``, exit code and the
  count of ``ok`` lines each prints;
* offline OSWorld eval (``engine/eval/osworld/offline.py``) on the bundled episodes, ``--zoom both``:
  grounding hit rate and judge agreement for ``off:all`` and ``on:all``. Backends default to ``stub``
  (no model calls; this then measures the harness and the zoom wrapper, not grounding quality);
  ``real`` uses the engine's vLLM/Claude backends;
* wall-clock timing of each suite.

``store_baseline`` writes ``<data dir>/evolve/baseline-<commit>.json``; ``load_baseline`` reads it.
The same ``run_*`` measurement functions are what ``gates.py`` runs on a candidate worktree. With ``runner``
(an ``evolve.sandbox.Runner``) every command runs inside that sandbox instead of on this machine; reports are
written to a fresh host temp dir that is the sandbox's only other mount. ``compute_baseline(..., runner=...)`` /
``sandboxed_baseline`` measure main in the same sandbox the candidate is gated in, so environment differences
(skips, missing tools) cancel out.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
NODE_SELFTEST_GLOBS = ("main/tools/selftest*.js", "main/tools/e2e-*.js")
OFFLINE_EPISODES = "engine/tests/fixtures/offline"
PYTEST_TIMEOUT_S = 1800
NODE_TIMEOUT_S = 300
OFFLINE_TIMEOUT_S = 900
_OK_LINE = re.compile(r"^\s*ok\b")
# Same row grammar as offline.py's parse_table, kept here so a candidate cannot change how it is scored.
_ROW_RE = re.compile(r"^\|\s*(?:(?P<zoom>off|on)\s*\|\s*)?(?P<app>[^|]+?)\s*\|\s*(?P<episodes>\d+)\s*\|\s*(?P<steps>\d+)\s*\|"
                     r"\s*(?P<hits>\d+)\s*\|\s*(?P<misses>\d+)\s*\|\s*[^|]*\|\s*(?P<judged>\d+)\s*\|\s*(?P<agree>\d+)\s*\|")
_SECRET_PART = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")


def data_dir() -> Path:
    return Path(os.environ.get("O_DATA_DIR") or (Path.home() / ".o-engine"))


def baseline_path(commit: str, root: Path | str | None = None) -> Path:
    return Path(root or data_dir()) / "evolve" / f"baseline-{commit}.json"


def untrusted_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Env for running candidate code: no secrets, no app endpoints, a throwaway engine data dir."""
    # GIT_CONFIG_* goes as a set: keeping GIT_CONFIG_COUNT/VALUE_n after the KEY filter drops GIT_CONFIG_KEY_n makes
    # every git call fail ("missing config key"), and the host's git config has no business in candidate runs.
    env = {k: v for k, v in os.environ.items()
           if not any(p in k.upper() for p in _SECRET_PART) and not k.upper().startswith(("O_", "GIT_CONFIG"))}
    env["O_DATA_DIR"] = tempfile.mkdtemp(prefix="o-evolve-data-")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    if extra:
        env.update(extra)
    return env


def git(repo: Path | str, *args: str, check: bool = True, timeout: float = 120) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def head_commit(repo: Path | str) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


# ---------------------------------------------------------------- engine pytest

def parse_junit(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    passed: list[str] = []
    failed: list[str] = []
    skipped = errors = 0
    for case in root.iter("testcase"):
        cid = f"{case.get('classname', '')}::{case.get('name', '')}"
        kids = {c.tag for c in case}
        if "failure" in kids:
            failed.append(cid)
        elif "error" in kids:
            errors += 1
            failed.append(cid)
        elif "skipped" in kids:
            skipped += 1
        else:
            passed.append(cid)
    return {"passed": len(passed), "failed": len(failed), "skipped": skipped, "errors": errors,
            "passed_ids": sorted(passed), "failed_ids": sorted(failed)}


def _sandboxed(runner: Any, argv_of: Any, cwd: str, timeout_s: float, report: str) -> tuple[int, str, str]:
    """(returncode, output tail, report text) for one command in ``runner``; ``argv_of(ref)`` builds the argv."""
    out = Path(tempfile.mkdtemp(prefix="o-evolve-out-"))
    try:
        out.chmod(0o777)  # the sandbox user may differ from ours
    except OSError:
        pass
    try:
        r = runner.run(argv_of(runner.ref(out, report)), cwd=cwd, timeout=timeout_s, out_dir=out)
        tail = (r.stdout + r.stderr)[-2000:]
        rc = -1 if r.timed_out else r.returncode
        if r.timed_out:
            tail = f"timed out after {timeout_s}s\n" + tail
        try:
            text = (out / report).read_text(encoding="utf-8")
        except OSError:
            text = ""
        return rc, tail, text
    finally:
        import shutil

        shutil.rmtree(out, ignore_errors=True)


def run_engine_pytest(root: Path | str, args: tuple[str, ...] = (), timeout_s: float = PYTEST_TIMEOUT_S,
                      env: Mapping[str, str] | None = None, runner: Any = None) -> dict[str, Any]:
    """Run pytest in ``<root>/engine``; ``args`` narrows it (test files / node ids relative to engine/)."""
    engine = Path(root) / "engine"
    if runner is not None:
        t0 = time.monotonic()
        rc, tail, xml = _sandboxed(runner, lambda ref: [runner.python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                                        f"--junitxml={ref}", *args], "engine", timeout_s, "junit.xml")
        try:
            res = parse_junit(xml)
        except ET.ParseError:
            res = {"passed": 0, "failed": 0, "skipped": 0, "errors": 1, "passed_ids": [], "failed_ids": ["<no report>"]}
        res.update({"returncode": rc, "duration_s": round(time.monotonic() - t0, 2), "tail": tail})
        return res
    fd, xml_path = tempfile.mkstemp(prefix="o-evolve-junit-", suffix=".xml")
    os.close(fd)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml_path}", *args]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=engine, capture_output=True, text=True, timeout=timeout_s,
                              env=dict(env) if env is not None else untrusted_env())
        rc, tail = proc.returncode, (proc.stdout + proc.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        rc, tail = -1, f"timed out after {timeout_s}s"
    dt = time.monotonic() - t0
    try:
        res = parse_junit(Path(xml_path).read_text(encoding="utf-8"))
    except (OSError, ET.ParseError):
        res = {"passed": 0, "failed": 0, "skipped": 0, "errors": 1, "passed_ids": [], "failed_ids": ["<no report>"]}
    finally:
        try:
            os.unlink(xml_path)
        except OSError:
            pass
    res.update({"returncode": rc, "duration_s": round(dt, 2), "tail": tail})
    return res


# ---------------------------------------------------------------- node selftests

def discover_selftests(root: Path | str) -> list[str]:
    r = Path(root)
    found: set[str] = set()
    for g in NODE_SELFTEST_GLOBS:
        found.update(p.relative_to(r).as_posix() for p in r.glob(g) if p.is_file())
    return sorted(found)


def run_node_selftests(root: Path | str, files: list[str] | None = None, timeout_s: float = NODE_TIMEOUT_S,
                       node: str = "node", env: Mapping[str, str] | None = None, runner: Any = None) -> dict[str, Any]:
    r = Path(root)
    files = discover_selftests(r) if files is None else files
    out: dict[str, Any] = {}
    t0 = time.monotonic()
    for f in files:
        s = time.monotonic()
        if not (r / f).is_file():
            out[f] = {"returncode": None, "ok": 0, "duration_s": 0.0, "tail": "missing"}
            continue
        if runner is not None:
            rr = runner.run(["node", f], cwd=".", timeout=timeout_s)
            rc, text = (-1 if rr.timed_out else rr.returncode), rr.stdout + rr.stderr
            oks = sum(1 for line in text.splitlines() if _OK_LINE.match(line))
            out[f] = {"returncode": rc, "ok": oks, "duration_s": round(time.monotonic() - s, 2), "tail": text[-800:]}
            continue
        try:
            proc = subprocess.run([node, f], cwd=r, capture_output=True, text=True, timeout=timeout_s,
                                  env=dict(env) if env is not None else untrusted_env())
            rc, text = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            rc, text = -1, f"timed out after {timeout_s}s"
        except OSError as e:
            rc, text = -2, f"cannot run node: {e}"
        oks = sum(1 for line in text.splitlines() if _OK_LINE.match(line))
        out[f] = {"returncode": rc, "ok": oks, "duration_s": round(time.monotonic() - s, 2), "tail": text[-800:]}
    return {"files": out, "total_ok": sum(v["ok"] for v in out.values()),
            "all_passed": all(v["returncode"] == 0 for v in out.values()),
            "duration_s": round(time.monotonic() - t0, 2)}


# ---------------------------------------------------------------- offline OSWorld eval

def parse_offline_table(text: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        app = m.group("app").strip("* ")
        key = f"{m.group('zoom')}:{app}" if m.group("zoom") else app
        row: dict[str, float] = {k: int(m.group(k)) for k in ("episodes", "steps", "hits", "misses", "judged", "agree")}
        row["hit_rate"] = row["hits"] / row["steps"] if row["steps"] else 0.0
        row["agreement"] = row["agree"] / row["judged"] if row["judged"] else 0.0
        out[key] = row
    return out


def run_offline_eval(root: Path | str, grounder: str = "stub", judge: str = "stub",
                     episodes: str = OFFLINE_EPISODES, timeout_s: float = OFFLINE_TIMEOUT_S,
                     env: Mapping[str, str] | None = None, runner: Any = None) -> dict[str, Any]:
    engine = Path(root) / "engine"
    ep_dir = Path(root) / episodes
    if runner is not None:
        t0 = time.monotonic()
        rel_eps = os.path.relpath(episodes, "engine") if not Path(episodes).is_absolute() else episodes
        rc, tail, text = _sandboxed(runner, lambda ref: [runner.python, "-m", "eval.osworld.offline", "--episodes", rel_eps,
                                                         "--grounder", grounder, "--judge", judge, "--zoom", "both",
                                                         "--out", ref], "engine", timeout_s, "offline.md")
        rows = parse_offline_table(text if rc == 0 else "")
        metrics = {k: {"hit_rate": v["hit_rate"], "agreement": v["agreement"], "steps": v["steps"], "judged": v["judged"]}
                   for k, v in rows.items() if k.endswith(":all")}
        return {"returncode": rc, "grounder": grounder, "judge": judge, "episodes": episodes, "metrics": metrics,
                "duration_s": round(time.monotonic() - t0, 2), "tail": tail if rc else ""}
    fd, out_path = tempfile.mkstemp(prefix="o-evolve-offline-", suffix=".md")
    os.close(fd)
    cmd = [sys.executable, "-m", "eval.osworld.offline", "--episodes", str(ep_dir), "--grounder", grounder,
           "--judge", judge, "--zoom", "both", "--out", out_path]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=engine, capture_output=True, text=True, timeout=timeout_s,
                              env=dict(env) if env is not None else untrusted_env())
        rc, tail = proc.returncode, (proc.stdout + proc.stderr)[-1500:]
        text = Path(out_path).read_text(encoding="utf-8") if rc == 0 else ""
    except subprocess.TimeoutExpired:
        rc, tail, text = -1, f"timed out after {timeout_s}s", ""
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    rows = parse_offline_table(text)
    metrics = {k: {"hit_rate": v["hit_rate"], "agreement": v["agreement"], "steps": v["steps"], "judged": v["judged"]}
               for k, v in rows.items() if k.endswith(":all")}
    return {"returncode": rc, "grounder": grounder, "judge": judge, "episodes": episodes, "metrics": metrics,
            "duration_s": round(time.monotonic() - t0, 2), "tail": tail if rc else ""}


# ---------------------------------------------------------------- baseline

def compute_baseline(repo_root: Path | str, *, grounder: str = "stub", judge: str = "stub",
                     node: str = "node", runner: Any = None) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    commit = head_commit(root)
    t0 = time.monotonic()
    py = run_engine_pytest(root, runner=runner)
    nd = run_node_selftests(root, node=node, runner=runner)
    off = run_offline_eval(root, grounder=grounder, judge=judge, runner=runner)
    total = time.monotonic() - t0
    for part in (py, off):
        part.pop("tail", None)
    for v in nd["files"].values():
        v.pop("tail", None)
    return {
        "schema": SCHEMA_VERSION,
        "commit": commit,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "sandbox": getattr(runner, "name", "host"),
        "engine_pytest": py,
        "node_selftests": nd,
        "offline_osworld": off,
        "timing": {"engine_pytest_s": py["duration_s"], "node_selftests_s": nd["duration_s"],
                   "offline_osworld_s": off["duration_s"], "total_s": round(total, 2)},
    }


def store_baseline(baseline: Mapping[str, Any], root: Path | str | None = None) -> Path:
    path = baseline_path(_key(str(baseline["commit"]), str(baseline.get("sandbox") or "host")), root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_baseline(commit: str, root: Path | str | None = None) -> dict[str, Any] | None:
    path = baseline_path(commit, root)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _key(commit: str, sandbox: str) -> str:
    return commit if sandbox == "host" else f"{commit}-{sandbox}"


def ensure_baseline(repo_root: Path | str, root: Path | str | None = None, **kw: Any) -> dict[str, Any]:
    """The stored baseline for ``repo_root``'s HEAD, computing and storing it when absent."""
    commit = head_commit(repo_root)
    found = load_baseline(_key(commit, getattr(kw.get("runner"), "name", "host")), root)
    if found is not None:
        return found
    b = compute_baseline(repo_root, **kw)
    store_baseline(b, root)
    return b


def sandboxed_baseline(repo_root: Path | str, runner_for: Any, backend: str, root: Path | str | None = None,
                       **kw: Any) -> dict[str, Any]:
    """Baseline of ``repo_root``'s HEAD measured in a sandbox: a detached throwaway worktree of HEAD is mounted
    (never the live checkout), measured with ``runner_for(worktree)``, then removed. Cached per commit+backend."""
    commit = head_commit(repo_root)
    found = load_baseline(_key(commit, backend), root)
    if found is not None:
        return found
    wt = Path(tempfile.mkdtemp(prefix="o-evolve-base-")) / "wt"
    git(repo_root, "worktree", "add", "--detach", str(wt), commit)
    try:
        runner = runner_for(wt)
        try:
            b = compute_baseline(wt, runner=runner, **kw)
        finally:
            close = getattr(runner, "close", None)
            if callable(close):
                close()
        store_baseline(b, root)
        return b
    finally:
        git(repo_root, "worktree", "remove", "--force", str(wt), check=False)
        git(repo_root, "worktree", "prune", check=False)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Compute and store evolve baseline metrics for a checkout.")
    ap.add_argument("repo_root")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--grounder", default="stub")
    ap.add_argument("--judge", default="stub")
    a = ap.parse_args()
    b = compute_baseline(a.repo_root, grounder=a.grounder, judge=a.judge)
    print(store_baseline(b, a.data_dir))
