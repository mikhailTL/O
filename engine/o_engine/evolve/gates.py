"""Gate stage (plan L, stage 5): decide whether a built proposal may become a PR.

``run_gates(proposal, workdir, baseline) -> GateReport``. The diff is ``baseline["commit"]..HEAD`` of
``workdir`` (the checkout the builder committed into). Checks, in order:

static (cheap, on the diff):
  clean_worktree, protected_paths, diff_budget, secret_scan, dangerous_patterns,
  declared_dependencies, license_scan, lockfile_consistency
dynamic (run the candidate's code, always inside the sandbox ``GateConfig.runner_for`` gives, see
evolve/sandbox.py: offline, resource-limited, the worktree its only read-write mount):
  acceptance_test, engine_pytest, node_selftests, offline_osworld
Without ``runner_for`` the dynamic checks are not run (reported failed): candidate code never runs on the host.
The one exception is a diff that runs no code at all (docs, licence text, dependency pins only; see
``sandbox.no_exec_paths``): with ``waive_dynamic_for_no_exec`` the dynamic checks are waived (passed, ``skipped``)
so documentation and dependency-pin upgrades can ship from installs that have no Docker or E2B.

With ``fail_fast`` (default) a failed static check skips the dynamic ones; they are then reported as
failed with ``detail="not run: ..."`` so the report still lists every gate.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import baseline as bl
from . import protected
from . import sandbox as sbx
from .schema import OK_LICENSES, GateCheck, GateReport, Proposal

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # pragma: no cover - Python 3.10
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ImportError:
        _toml = None  # type: ignore[assignment]


@dataclass
class GateConfig:
    max_files: int = 25
    max_lines: int = 1200  # added + removed
    osworld_tolerance: float = 0.0  # allowed drop in each offline rate (absolute, 0..1)
    grounder: str = "stub"
    judge: str = "stub"
    fail_fast: bool = True
    run_pytest: bool = True
    run_node: bool = True
    run_offline: bool = True
    node: str = "node"
    # (ecosystem, name) -> SPDX-ish license string or None; injected in tests, network by default.
    license_lookup: Callable[[str, str], str | None] | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    # worktree -> evolve.sandbox.Runner. None: dynamic gates are not run (candidate code never runs on the host).
    runner_for: Callable[[Path], Any] | None = None
    # With no runner: a diff that executes nothing (docs / dependency pins only) passes the dynamic gates as waived.
    waive_dynamic_for_no_exec: bool = True


# ---------------------------------------------------------------- scanners

SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("private key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
    ("AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("GitHub token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    ("Anthropic key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI-style key", r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9]{32,}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("Stripe key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("E2B key", r"\be2b_[A-Za-z0-9]{20,}\b"),
    ("npm token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("hardcoded credential",
     r"(?i)\b[\w-]*(?:api[_-]?key|secret|token|passw(?:or)?d|auth)[\w-]*\b[\"']?\s*[:=]\s*[\"'][A-Za-z0-9+/_\-.]{24,}[\"']"),
)

DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("permission bypass", r"dangerously-?skip-?permissions|dangerouslySkipPermissions|bypassPermissions"),
    ("TLS verification disabled",
     r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED|_create_unverified_context"
     r"|CERT_NONE|check_hostname\s*=\s*False|(?:^|\s)(?:curl|wget)\b[^\n]*\s(?:-k|--insecure|--no-check-certificate)\b"
     r"|PYTHONHTTPSVERIFY\s*=\s*['\"]?0"),
    ("dynamic code execution", r"(?<![\w.])(?:eval|exec)\s*\(|\bnew\s+Function\s*\(|\bvm\.runIn\w*Context\s*\("),
    ("unsafe deserialisation", r"\bpickle\.loads?\s*\(|\bmarshal\.loads\s*\(|\byaml\.load\s*\((?![^)]*Loader\s*=\s*yaml\.SafeLoader)"),
    ("shell pipe from network", r"(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z)?sh\b"),
)
_SCAN_SKIP = re.compile(r"(^|/)(package-lock\.json|.*\.lock)$")


def _scan(lines_by_file: Mapping[str, list[tuple[str, str]]], patterns: tuple[tuple[str, str], ...],
          skip_lockfiles: bool = True) -> list[str]:
    rx = [(name, re.compile(p)) for name, p in patterns]
    hits: list[str] = []
    for path, lines in lines_by_file.items():
        if skip_lockfiles and _SCAN_SKIP.search(path):
            continue
        for sign, line in lines:
            if sign != "+":
                continue
            for name, r in rx:
                if r.search(line):
                    hits.append(f"{path}: {name}")
    return sorted(set(hits))


# ---------------------------------------------------------------- dependencies

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _toml_strings_fallback(text: str) -> dict[str, list[str]]:
    """Dependency arrays from a pyproject without a TOML parser (Python 3.10, no tomli)."""
    out: dict[str, list[str]] = {}
    section = ""
    for m in re.finditer(r"^\s*\[([^\]]+)\]\s*$|^\s*([A-Za-z0-9_.-]+)\s*=\s*\[(.*?)\]", text, re.M | re.S):
        if m.group(1):
            section = m.group(1).strip()
            continue
        key = m.group(2)
        if (section == "project" and key == "dependencies") or section == "project.optional-dependencies":
            out[f"{section}.{key}"] = re.findall(r"[\"']([^\"']+)[\"']", m.group(3))
    return out


def py_deps(text: str | None) -> dict[str, str]:
    """``{normalized name: requirement}`` across [project].dependencies and every optional group."""
    if not text:
        return {}
    reqs: list[str] = []
    if _toml is not None:
        data = _toml.loads(text)
        proj = data.get("project", {})
        reqs += list(proj.get("dependencies", []))
        for group in (proj.get("optional-dependencies") or {}).values():
            reqs += list(group)
    else:
        for v in _toml_strings_fallback(text).values():
            reqs += v
    out: dict[str, str] = {}
    for r in reqs:
        m = _REQ_NAME.match(r)
        if m:
            out[_norm_py(m.group(1))] = r
    return out


def npm_deps(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    data = json.loads(text)
    out: dict[str, str] = {}
    for sec in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        out.update({k: str(v) for k, v in (data.get(sec) or {}).items()})
    return out


_LICENSE_ALIASES = {
    "mit": "MIT", "mit license": "MIT", "the mit license": "MIT", "expat": "MIT",
    "apache-2.0": "Apache-2.0", "apache 2.0": "Apache-2.0", "apache2": "Apache-2.0", "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0", "apache software license": "Apache-2.0", "apache-2": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "bsd": "BSD-3-Clause", "bsd license": "BSD-3-Clause", "bsd-3-clause": "BSD-3-Clause", "new bsd": "BSD-3-Clause",
    "3-clause bsd": "BSD-3-Clause", "bsd 3-clause": "BSD-3-Clause", "bsd-2-clause": "BSD-2-Clause",
    "simplified bsd": "BSD-2-Clause", "isc": "ISC", "isc license": "ISC", "isc license (iscl)": "ISC",
    "0bsd": "0BSD", "unlicense": "Unlicense", "the unlicense": "Unlicense", "the unlicense (unlicense)": "Unlicense",
    "mpl-2.0": "MPL-2.0", "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0", "mpl 2.0": "MPL-2.0",
}


def normalize_license(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("License :: "):
        s = s.split("::")[-1].strip()
    return _LICENSE_ALIASES.get(s.lower(), s)


def license_ok(raw: str | None) -> bool:
    """SPDX-ish check: an ``OR`` expression needs one allowed side, ``AND`` needs all."""
    if not raw:
        return False
    expr = raw.strip().strip("()")
    if re.search(r"\s+OR\s+", expr, re.I):
        return any(license_ok(p) for p in re.split(r"\s+OR\s+", expr, flags=re.I))
    if re.search(r"\s+AND\s+", expr, re.I):
        return all(license_ok(p) for p in re.split(r"\s+AND\s+", expr, flags=re.I))
    return normalize_license(expr) in OK_LICENSES


def _http_json(url: str, timeout: float = 15) -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "o-evolve-gates"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # default SSL context: verification on
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def registry_license(ecosystem: str, name: str) -> str | None:
    """License of the latest release on PyPI (``license_expression``, ``license``, then classifiers) or npm."""
    q = urllib.parse.quote(name, safe="@")
    if ecosystem == "pypi":
        data = _http_json(f"https://pypi.org/pypi/{q}/json")
        if not data:
            return None
        info = data.get("info") or {}
        for key in ("license_expression", "license"):
            v = (info.get(key) or "").strip()
            if v and len(v) < 100 and "\n" not in v:
                return v
        for c in info.get("classifiers") or []:
            if c.startswith("License :: OSI Approved :: "):
                return c
        return None
    if ecosystem == "npm":
        data = _http_json(f"https://registry.npmjs.org/{q.replace('/', '%2F')}/latest")
        if not data:
            return None
        lic = data.get("license")
        if isinstance(lic, dict):
            lic = lic.get("type")
        return str(lic) if lic else None
    return None


# ---------------------------------------------------------------- diff

@dataclass
class Diff:
    base: str
    head: str
    files: list[str]
    added: int
    removed: int
    text: str
    lines: dict[str, list[tuple[str, str]]]
    status: dict[str, str]  # path -> A/M/D

    @property
    def total_lines(self) -> int:
        return self.added + self.removed


def collect_diff(workdir: Path | str, base: str) -> Diff:
    wd = Path(workdir)
    head = bl.git(wd, "rev-parse", "HEAD").strip()
    numstat = bl.git(wd, "diff", "--no-renames", "--numstat", base, head)
    files, added, removed = [], 0, 0
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a, r, f = parts
        files.append(f)
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0
    status = {}
    for line in bl.git(wd, "diff", "--no-renames", "--name-status", base, head).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status[parts[-1]] = parts[0][:1]
    text = bl.git(wd, "diff", "--no-renames", "--no-color", "--no-ext-diff", "-U0", base, head)
    return Diff(base, head, files, added, removed, text, protected.changed_lines(text), status)


def _show(wd: Path, rev: str, path: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(wd), "show", f"{rev}:{path}"], capture_output=True, text=True,
                          timeout=60)
    return proc.stdout if proc.returncode == 0 else None


# ---------------------------------------------------------------- checks

def _check(name: str, passed: bool, detail: str = "", before: float | None = None,
           after: float | None = None) -> GateCheck:
    return GateCheck(name=name, passed=bool(passed), detail=detail[:4000], metric_before=before, metric_after=after)


def _where(c: GateCheck, backend: str) -> GateCheck:
    return c.model_copy(update={"detail": f"[sandbox: {backend}] {c.detail}"[:4000]})


def check_clean(wd: Path) -> GateCheck:
    dirty = bl.git(wd, "status", "--porcelain", "--untracked-files=all").strip()
    return _check("clean_worktree", not dirty, "committed state is what is gated" if not dirty
                  else "uncommitted changes:\n" + dirty[:1500])


def check_protected(diff: Diff) -> GateCheck:
    v = protected.violations(diff.files, diff.text)
    return _check("protected_paths", not v, "no protected path or code touched" if not v else "\n".join(v),
                  0, float(len(v)))


def check_budget(diff: Diff, cfg: GateConfig) -> GateCheck:
    ok = 0 < len(diff.files) <= cfg.max_files and diff.total_lines <= cfg.max_lines
    detail = (f"{len(diff.files)} files (max {cfg.max_files}), +{diff.added}/-{diff.removed} = {diff.total_lines} "
              f"lines (max {cfg.max_lines})" + ("; empty diff" if not diff.files else ""))
    return _check("diff_budget", ok, detail, float(cfg.max_lines), float(diff.total_lines))


def check_secrets(diff: Diff) -> GateCheck:
    hits = _scan(diff.lines, SECRET_PATTERNS)
    return _check("secret_scan", not hits, "no secrets in added lines" if not hits else "\n".join(hits),
                  0, float(len(hits)))


def check_dangerous(diff: Diff) -> GateCheck:
    hits = _scan(diff.lines, DANGEROUS_PATTERNS)
    return _check("dangerous_patterns", not hits, "none added" if not hits else "\n".join(hits), 0, float(len(hits)))


def dependency_changes(wd: Path, diff: Diff) -> dict[str, dict[str, str]]:
    """New or changed deps: ``{"pypi": {name: req}, "npm": {name: range}}``."""
    out: dict[str, dict[str, str]] = {"pypi": {}, "npm": {}}
    for f in diff.files:
        if f.endswith("pyproject.toml"):
            before, after = py_deps(_show(wd, diff.base, f)), py_deps(_show(wd, diff.head, f))
            out["pypi"].update({k: v for k, v in after.items() if before.get(k) != v})
        elif f.endswith("package.json") and "node_modules/" not in f:
            before, after = npm_deps(_show(wd, diff.base, f)), npm_deps(_show(wd, diff.head, f))
            out["npm"].update({k: v for k, v in after.items() if before.get(k) != v})
    return out


def _declared(proposal: Proposal) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"pypi": set(), "npm": set()}
    for d in proposal.new_dependencies:
        eco, _, spec = d.partition(":")
        if eco == "pypi":
            m = _REQ_NAME.match(spec)
            if m:
                out["pypi"].add(_norm_py(m.group(1)))
        elif eco == "npm":
            name = spec[: spec.rfind("@")] if spec.rfind("@") > 0 else spec
            out["npm"].add(name)
    return out


def check_declared(proposal: Proposal, changes: Mapping[str, Mapping[str, str]]) -> GateCheck:
    decl = _declared(proposal)
    undeclared = [f"{eco}:{n}" for eco in ("pypi", "npm") for n in changes[eco] if n not in decl[eco]]
    n = sum(len(v) for v in changes.values())
    return _check("declared_dependencies", not undeclared,
                  f"{n} dependency change(s), all declared in the proposal" if not undeclared
                  else "not in proposal.new_dependencies: " + ", ".join(undeclared), None, float(n))


def check_licenses(changes: Mapping[str, Mapping[str, str]], cfg: GateConfig) -> GateCheck:
    lookup = cfg.license_lookup or registry_license
    bad, good = [], []
    for eco in ("pypi", "npm"):
        for name in sorted(changes[eco]):
            lic = lookup(eco, name)
            (good if license_ok(lic) else bad).append(f"{eco}:{name}={normalize_license(lic) or 'unknown'}")
    if not bad and not good:
        return _check("license_scan", True, "no new dependencies", None, 0.0)
    return _check("license_scan", not bad, ("ok: " + ", ".join(good) if good else "") +
                  (("\nnot allowed (" + "/".join(OK_LICENSES) + "): " + ", ".join(bad)) if bad else ""),
                  None, float(len(good) + len(bad)))


def check_lockfiles(wd: Path, diff: Diff, changes: Mapping[str, Mapping[str, str]]) -> GateCheck:
    problems: list[str] = []
    for f in diff.files:
        if f.endswith("pyproject.toml") and diff.status.get(f) != "D":
            text = _show(wd, diff.head, f)
            try:
                py_deps(text)
            except Exception as e:  # malformed TOML
                problems.append(f"{f}: does not parse ({e})")
    if changes["npm"]:
        for pj in [f for f in diff.files if f.endswith("package.json") and "node_modules/" not in f]:
            lock = str(Path(pj).with_name("package-lock.json").as_posix())
            had = _show(wd, diff.base, lock) is not None
            has_text = _show(wd, diff.head, lock)
            if had or has_text is not None:
                if lock not in diff.files:
                    problems.append(f"{pj} dependencies changed but {lock} was not updated")
                elif has_text is not None:
                    try:
                        root_deps = (json.loads(has_text).get("packages") or {}).get("", {})
                        want = npm_deps(_show(wd, diff.head, pj))
                        got: dict[str, str] = {}
                        for sec in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                            got.update(root_deps.get(sec) or {})
                        if want != got:
                            problems.append(f"{lock} root dependencies do not match {pj}")
                    except ValueError as e:
                        problems.append(f"{lock}: not JSON ({e})")
    if changes["pypi"]:
        for lockname in ("uv.lock", "poetry.lock", "pdm.lock", "requirements.lock"):
            for f in diff.files:
                if f.endswith("pyproject.toml"):
                    lock = str(Path(f).with_name(lockname).as_posix())
                    if _show(wd, diff.base, lock) is not None and lock not in diff.files:
                        problems.append(f"{f} dependencies changed but {lock} was not updated")
    return _check("lockfile_consistency", not problems, "\n".join(problems) or "consistent (no stale lockfile)")


_PY_TEST_FILE = re.compile(r"^engine/tests/(?:.*/)?test_[\w]+\.py$")
_NODE_TEST_FILE = re.compile(r"^main/tools/(?:selftest[\w-]*|e2e-[\w-]+)\.js$")
_NEW_PY_TEST = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(")


def acceptance_targets(proposal: Proposal, diff: Diff) -> tuple[list[str], list[str], list[str]]:
    """(pytest args relative to engine/, node selftest files, new test names) for the acceptance gate."""
    py_files = [f for f in diff.files if _PY_TEST_FILE.match(f) and diff.status.get(f) != "D"]
    node_files = [f for f in diff.files if _NODE_TEST_FILE.match(f) and diff.status.get(f) != "D"]
    new_tests: list[str] = []
    for f in py_files:
        for sign, line in diff.lines.get(f, []):
            m = _NEW_PY_TEST.match(line) if sign == "+" else None
            if m:
                new_tests.append(f"{f}::{m.group(1)}")
    for f in node_files:
        if any(sign == "+" and re.search(r"\bassert\b", line) for sign, line in diff.lines.get(f, [])):
            new_tests.append(f)
    named = re.findall(r"(?:engine/)?tests/[\w/]+\.py(?:::[\w\[\]-]+)?", proposal.acceptance_test)
    named = [n if n.startswith("engine/") else f"engine/{n}" for n in named]
    picked = [n for n in named if n.split("::")[0] in py_files]
    py_args = [p[len("engine/"):] for p in (picked or py_files)]
    return py_args, node_files, new_tests


def check_acceptance(proposal: Proposal, diff: Diff, wd: Path, cfg: GateConfig,
                     env: Mapping[str, str], runner: Any = None) -> GateCheck:
    py_args, node_files, new_tests = acceptance_targets(proposal, diff)
    if not new_tests:
        return _check("acceptance_test", False, "no new test in the diff (need a new `def test_*` in engine/tests "
                      "or new asserts in a main/tools selftest)", None, 0.0)
    parts, ok, passed = [], True, 0
    if py_args:
        r = bl.run_engine_pytest(wd, tuple(py_args), env=env, runner=runner)
        good = r["returncode"] == 0 and r["failed"] == 0 and r["passed"] > 0
        ok &= good
        passed += r["passed"]
        parts.append(f"pytest {' '.join(py_args)}: {r['passed']} passed, {r['failed']} failed"
                     + ("" if good else "\n" + r["tail"][-1200:]))
    if node_files:
        r = bl.run_node_selftests(wd, node_files, node=cfg.node, env=env, runner=runner)
        ok &= r["all_passed"]
        passed += r["total_ok"]
        parts.append(f"node {' '.join(node_files)}: " + ", ".join(
            f"{f} rc={v['returncode']} ok={v['ok']}" for f, v in r["files"].items()))
    parts.insert(0, "new: " + ", ".join(new_tests[:20]))
    return _check("acceptance_test", ok, "\n".join(parts), 0.0, float(passed))


def check_pytest(base: Mapping[str, Any], wd: Path, env: Mapping[str, str], runner: Any = None) -> GateCheck:
    b = base.get("engine_pytest") or {}
    r = bl.run_engine_pytest(wd, env=env, runner=runner)
    new_fail = sorted(set(r["failed_ids"]) - set(b.get("failed_ids", [])))
    vanished = sorted(set(b.get("passed_ids", [])) - set(r["passed_ids"]) - set(r["failed_ids"]))
    ok = not new_fail and not vanished and r["passed"] >= int(b.get("passed", 0)) and r["passed"] > 0
    detail = (f"before {b.get('passed', '?')} passed/{b.get('failed', '?')} failed/{b.get('skipped', '?')} skipped; "
              f"after {r['passed']}/{r['failed']}/{r['skipped']} in {r['duration_s']}s")
    if new_fail:
        detail += "\nnew failures: " + ", ".join(new_fail[:30])
    if vanished:
        detail += "\nbaseline tests no longer run: " + ", ".join(vanished[:30])
    if not ok and not new_fail and not vanished:
        detail += "\n" + r["tail"][-1000:]
    return _check("engine_pytest", ok, detail, float(b.get("passed", 0)), float(r["passed"]))


def check_node(base: Mapping[str, Any], wd: Path, diff: Diff, cfg: GateConfig, env: Mapping[str, str],
               runner: Any = None) -> GateCheck:
    b = base.get("node_selftests") or {}
    files = sorted(set((b.get("files") or {}).keys()) | set(bl.discover_selftests(wd)))
    r = bl.run_node_selftests(wd, files, node=cfg.node, env=env, runner=runner)
    bad = [f"{f} rc={v['returncode']}" + (f" ({v['tail'][-300:]})" if v["returncode"] != 0 else "")
           for f, v in r["files"].items() if v["returncode"] != 0]
    fewer = [f"{f} ok {(b.get('files') or {}).get(f, {}).get('ok', 0)}->{v['ok']}" for f, v in r["files"].items()
             if v["ok"] < int((b.get("files") or {}).get(f, {}).get("ok", 0))]
    ok = not bad and not fewer
    detail = f"{len(files)} selftests, ok lines {b.get('total_ok', '?')} -> {r['total_ok']} in {r['duration_s']}s"
    if bad:
        detail += "\nfailed: " + "; ".join(bad)
    if fewer:
        detail += "\nfewer checks than baseline: " + ", ".join(fewer)
    return _check("node_selftests", ok, detail, float(b.get("total_ok", 0)), float(r["total_ok"]))


def check_offline(base: Mapping[str, Any], wd: Path, cfg: GateConfig, env: Mapping[str, str],
                  runner: Any = None) -> GateCheck:
    b = base.get("offline_osworld") or {}
    r = bl.run_offline_eval(wd, grounder=cfg.grounder, judge=cfg.judge, env=env, runner=runner)
    bm, am = b.get("metrics") or {}, r["metrics"]
    regress, lines = [], []
    if r["returncode"] != 0 or not am:
        return _check("offline_osworld", False, f"eval failed rc={r['returncode']}\n{r.get('tail', '')}",
                      (bm.get("off:all") or {}).get("hit_rate"), None)
    for key in sorted(set(bm) | set(am)):
        for metric in ("hit_rate", "agreement"):
            before = (bm.get(key) or {}).get(metric)
            after = (am.get(key) or {}).get(metric)
            lines.append(f"{key} {metric}: {before if before is None else round(before, 4)} -> "
                         f"{after if after is None else round(after, 4)}")
            if before is not None and (after is None or after < before - cfg.osworld_tolerance):
                regress.append(f"{key} {metric}")
    detail = (f"grounder={cfg.grounder} judge={cfg.judge} tolerance={cfg.osworld_tolerance}\n" + "\n".join(lines)
              + (("\nregressed: " + ", ".join(regress)) if regress else ""))
    return _check("offline_osworld", not regress, detail, (bm.get("off:all") or {}).get("hit_rate"),
                  (am.get("off:all") or {}).get("hit_rate"))


# ---------------------------------------------------------------- entry point

STATIC_CHECKS = ("clean_worktree", "protected_paths", "diff_budget", "secret_scan", "dangerous_patterns",
                 "declared_dependencies", "license_scan", "lockfile_consistency")
DYNAMIC_CHECKS = ("acceptance_test", "engine_pytest", "node_selftests", "offline_osworld")


def run_gates(proposal: Proposal, workdir: Path | str, baseline: Mapping[str, Any] | str | Path | None,
              config: GateConfig | None = None, *, spent_usd: float = 0.0) -> GateReport:
    cfg = config or GateConfig()
    wd = Path(workdir).resolve()
    if isinstance(baseline, (str, Path)):
        baseline = json.loads(Path(baseline).read_text(encoding="utf-8"))
    baseline = baseline or {}
    base_commit = str(baseline.get("commit") or "")
    branch = bl.git(wd, "rev-parse", "--abbrev-ref", "HEAD").strip()
    head = bl.git(wd, "rev-parse", "HEAD").strip()
    anc = subprocess.run(["git", "-C", str(wd), "merge-base", "--is-ancestor", base_commit, head],
                         capture_output=True, timeout=60)
    if not base_commit or anc.returncode != 0:
        chk = _check("baseline_ancestor", False, f"baseline commit {base_commit or '<none>'} is not an ancestor of {head}")
        return GateReport(proposal_id=proposal.id, branch=branch, commit=head, passed=False, checks=[chk],
                          spent_usd=spent_usd)
    diff = collect_diff(wd, base_commit)
    changes = dependency_changes(wd, diff)
    checks = [check_clean(wd), check_protected(diff), check_budget(diff, cfg), check_secrets(diff),
              check_dangerous(diff), check_declared(proposal, changes), check_licenses(changes, cfg),
              check_lockfiles(wd, diff, changes)]
    static_failed = [c.name for c in checks if not c.passed]
    if static_failed and cfg.fail_fast:
        checks += [_check(n, False, "not run: static checks failed (" + ", ".join(static_failed) + ")")
                   for n in DYNAMIC_CHECKS]
    elif cfg.runner_for is None and cfg.waive_dynamic_for_no_exec and sbx.no_exec_paths(diff.files):
        checks += [GateCheck(name=n, passed=True, skipped=True,
                             detail="waived: the change runs no code (docs or dependency pins only), so there is nothing to execute")
                   for n in DYNAMIC_CHECKS]
    elif cfg.runner_for is None:
        checks += [_check(n, False, "not run: no sandbox (candidate code never runs on the host)")
                   for n in DYNAMIC_CHECKS]
    else:
        env = bl.untrusted_env(cfg.extra_env)
        runner = cfg.runner_for(wd)
        try:
            where = getattr(runner, "name", "sandbox")
            checks.append(_where(check_acceptance(proposal, diff, wd, cfg, env, runner), where))
            checks.append(_where(check_pytest(baseline, wd, env, runner), where) if cfg.run_pytest
                          else _check("engine_pytest", False, "disabled in config"))
            checks.append(_where(check_node(baseline, wd, diff, cfg, env, runner), where) if cfg.run_node
                          else _check("node_selftests", False, "disabled in config"))
            checks.append(_where(check_offline(baseline, wd, cfg, env, runner), where) if cfg.run_offline
                          else _check("offline_osworld", False, "disabled in config"))
        finally:
            close = getattr(runner, "close", None)
            if callable(close):
                close()
    return GateReport(proposal_id=proposal.id, branch=branch, commit=head, passed=all(c.passed for c in checks),
                      checks=checks, diff_files=diff.files, diff_lines=diff.total_lines, spent_usd=spent_usd)
