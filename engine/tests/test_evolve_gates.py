"""Plan L gates, protected paths and baseline on tiny temp git repos (real pytest/node subprocesses, no models)."""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from o_engine.evolve.sandbox import UnsafeHostRunner
from o_engine.evolve import baseline as bl
from o_engine.evolve import builder, gates, protected
from o_engine.evolve.schema import GateReport, Proposal

REAL_REPO = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

FAKE_OFFLINE = textwrap.dedent('''
    import argparse, json
    from pathlib import Path
    ap = argparse.ArgumentParser()
    for a in ("--episodes", "--grounder", "--judge", "--zoom", "--out"):
        ap.add_argument(a)
    a = ap.parse_args()
    s = json.loads((Path(__file__).resolve().parents[2] / "score.json").read_text())
    rows = []
    for z in ("off", "on"):
        h = s[z]
        rows.append(f"| {z} | **all** | 2 | 10 | {h} | {10 - h} | x | 2 | 2 | 100.0% |")
    Path(a.out).write_text("## table\\n| zoom | app | episodes | steps | hits | misses | hit rate | judged | agree | agreement |\\n" + "\\n".join(rows) + "\\n")
''')


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory) -> tuple[Path, dict]:
    r = tmp_path_factory.mktemp("tiny") / "repo"
    _write(r, {
        "engine/pyproject.toml": '[project]\nname = "x"\ndependencies = [\n  "pydantic>=2.6",\n]\n',
        "engine/o_engine/__init__.py": "",
        "engine/tests/test_base.py": "def test_one():\n    assert 1 + 1 == 2\n\n\ndef test_two():\n    assert True\n",
        "engine/eval/__init__.py": "",
        "engine/eval/osworld/__init__.py": "",
        "engine/eval/osworld/offline.py": FAKE_OFFLINE,
        "engine/score.json": json.dumps({"off": 8, "on": 9}),
        "engine/tests/fixtures/offline/.keep": "",
        "main/agent.js": "'use strict';\nmodule.exports = {};\n",
        "main/main.js": "'use strict';\nconst PROXY_KEYS = ['proxyToken'];\nconst x = 1;\n",
        "main/tools/selftest-a.js": "'use strict';\nconsole.log('ok a1');\nconsole.log('ok a2');\n",
        "package.json": json.dumps({"name": "t", "dependencies": {"ws": "^8.0.0"}}, indent=2) + "\n",
        "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {"": {"dependencies": {"ws": "^8.0.0"}}}}),
        ".gitignore": "__pycache__/\n*.pyc\n",
    })
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    base = bl.compute_baseline(r, node=NODE or "node")
    return r, base


_N = [0]


def _candidate(tiny, tmp_path: Path, files: dict[str, str], delete: tuple[str, ...] = (), pid: str | None = None,
               new_deps: list[str] | None = None, commit: bool = True) -> tuple[Path, Proposal]:
    repo, _ = tiny
    _N[0] += 1
    p = Proposal(id=pid or f"t{_N[0]}", signal_ids=["s"], subsystem="util", title="Add clamp",
                 rationale="r", spec="s", acceptance_test="engine/tests/test_clamp.py::test_clamp", score=0.5,
                 new_dependencies=new_deps or [])
    wd = tmp_path / "wd"
    builder.make_checkout(repo, wd, f"evolve/{p.id}")
    _write(wd, files)
    for d in delete:
        (wd / d).unlink()
    if commit:
        builder.commit_all(wd, p)
    return wd, p


GOOD = {
    "engine/o_engine/clamp.py": "def clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n",
    "engine/tests/test_clamp.py": "from o_engine.clamp import clamp\n\n\ndef test_clamp():\n    assert clamp(5, 0, 3) == 3\n    assert clamp(-1, 0, 3) == 0\n",
}


def _cfg(**kw) -> gates.GateConfig:
    # Tiny temp repos written by the test itself: the dev-only host runner stands in for the sandbox here
    # (test_evolve_sandbox.py covers the routing through a real sandbox runner).
    kw.setdefault("runner_for", UnsafeHostRunner)
    return gates.GateConfig(node=NODE or "node", license_lookup=kw.pop("license_lookup", lambda e, n: "MIT"), **kw)


def _by(report: GateReport) -> dict:
    return {c.name: c for c in report.checks}


# ---------------------------------------------------------------- baseline

@needs_node
def test_baseline_measures_and_round_trips(tiny, tmp_path):
    repo, base = tiny
    assert base["commit"] == _git(repo, "rev-parse", "HEAD").strip()
    assert base["engine_pytest"]["passed"] == 2 and base["engine_pytest"]["failed"] == 0
    assert base["node_selftests"]["files"]["main/tools/selftest-a.js"]["ok"] == 2
    assert base["node_selftests"]["all_passed"] is True
    assert base["offline_osworld"]["metrics"]["off:all"]["hit_rate"] == pytest.approx(0.8)
    assert base["offline_osworld"]["metrics"]["on:all"]["hit_rate"] == pytest.approx(0.9)
    assert set(base["timing"]) == {"engine_pytest_s", "node_selftests_s", "offline_osworld_s", "total_s"}
    path = bl.store_baseline(base, tmp_path)
    assert path == tmp_path / "evolve" / f"baseline-{base['commit']}.json"
    assert bl.load_baseline(base["commit"], tmp_path) == json.loads(json.dumps(base))
    assert bl.ensure_baseline(repo, tmp_path)["created"] == base["created"]  # stored one reused


def test_parse_junit_counts():
    xml = ('<testsuites><testsuite><testcase classname="a" name="x"/><testcase classname="a" name="y">'
           '<failure/></testcase><testcase classname="a" name="z"><skipped/></testcase>'
           '<testcase classname="b" name="e"><error/></testcase></testsuite></testsuites>')
    r = bl.parse_junit(xml)
    assert (r["passed"], r["failed"], r["skipped"], r["errors"]) == (1, 2, 1, 1)
    assert r["failed_ids"] == ["a::y", "b::e"]


def test_untrusted_env_has_no_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("O_PERM_URL", "u")
    env = bl.untrusted_env()
    assert "GITHUB_TOKEN" not in env and "O_PERM_URL" not in env and env["O_DATA_DIR"]


# ---------------------------------------------------------------- protected

def test_every_literal_protected_path_exists_in_the_repo():
    missing = [g for g in protected.PROTECTED if "*" not in g and not (REAL_REPO / g).exists()
               and g not in (".gitattributes", ".gitmodules")]
    assert missing == []


def test_protected_paths_and_globs():
    v = protected.violations(["main/agent.js", "engine/o_engine/clamp.py", ".github/workflows/evolve-gates.yml",
                              "engine/tests/fixtures/offline/ep/meta.json", "../etc/passwd", "MAIN/ActionLog.js",
                              "engine/o_engine/evolve/gates.py"])
    joined = "\n".join(v)
    for p in ("main/agent.js", ".github/workflows/evolve-gates.yml", "engine/tests/fixtures/offline/ep/meta.json",
              "../etc/passwd", "MAIN/ActionLog.js", "engine/o_engine/evolve/gates.py"):
        assert p in joined
    assert "clamp.py" not in joined
    assert protected.violations(["engine/o_engine/clamp.py", "renderer/panel.js"]) == []


def test_protected_markers_in_shared_files():
    diff = textwrap.dedent('''\
        diff --git a/main/main.js b/main/main.js
        --- a/main/main.js
        +++ b/main/main.js
        @@ -1 +1 @@
        -const PROXY_KEYS = ['proxyToken'];
        +const PROXY_KEYS = [];
        diff --git a/engine/o_engine/server.py b/engine/o_engine/server.py
        --- a/engine/o_engine/server.py
        +++ b/engine/o_engine/server.py
        @@ -1 +0,0 @@
        -    log = get_log(settings)
        diff --git a/engine/o_engine/web.py b/engine/o_engine/web.py
        --- a/engine/o_engine/web.py
        +++ b/engine/o_engine/web.py
        @@ -0,0 +1 @@
        +    log = get_log(settings)
        ''')
    v = protected.violations(["main/main.js", "engine/o_engine/server.py", "engine/o_engine/web.py"], diff)
    assert any("main/main.js" in x and "secrets" in x for x in v)
    assert any("server.py" in x and "action-log" in x for x in v)
    assert not any("web.py" in x for x in v)  # adding logging is fine


# ---------------------------------------------------------------- scanners

def test_license_ok_expressions():
    assert gates.license_ok("MIT") and gates.license_ok("Apache 2.0") and gates.license_ok("BSD License")
    assert gates.license_ok("License :: OSI Approved :: MIT License")
    assert gates.license_ok("(MIT OR GPL-3.0)")
    assert not gates.license_ok("MIT AND GPL-3.0")
    assert not gates.license_ok("GPL-3.0-only") and not gates.license_ok(None) and not gates.license_ok("")


def test_dep_parsers_and_fallback():
    text = '[project]\nname="x"\ndependencies = ["Pydantic_Core>=2", "httpx"]\n[project.optional-dependencies]\ndev = ["pytest>=8"]\n'
    assert set(gates.py_deps(text)) == {"pydantic-core", "httpx", "pytest"}
    assert set(sum(gates._toml_strings_fallback(text).values(), [])) == {"Pydantic_Core>=2", "httpx", "pytest>=8"}
    assert gates.npm_deps('{"dependencies": {"ws": "^8"}, "devDependencies": {"electron": "^33"}}') == {
        "ws": "^8", "electron": "^33"}


# ---------------------------------------------------------------- run_gates

@needs_node
def test_good_patch_passes_every_gate(tiny, tmp_path):
    wd, p = _candidate(tiny, tmp_path, GOOD)
    r = gates.run_gates(p, wd, tiny[1], _cfg(), spent_usd=0.25)
    by = _by(r)
    assert r.passed, [(c.name, c.detail) for c in r.checks if not c.passed]
    assert set(by) == set(gates.STATIC_CHECKS) | set(gates.DYNAMIC_CHECKS)
    assert r.branch == f"evolve/{p.id}" and len(r.commit) == 40 and r.spent_usd == 0.25
    assert sorted(r.diff_files) == sorted(GOOD) and r.diff_lines == 8
    assert by["engine_pytest"].metric_before == 2 and by["engine_pytest"].metric_after == 3
    assert by["acceptance_test"].metric_after == 1 and "test_clamp.py::test_clamp" in by["acceptance_test"].detail
    assert by["offline_osworld"].metric_before == pytest.approx(0.8) == by["offline_osworld"].metric_after
    assert by["node_selftests"].metric_after == 2


@needs_node
def test_protected_edit_and_fake_key_fail_fast(tiny, tmp_path):
    files = dict(GOOD)
    files["main/agent.js"] = "'use strict';\nmodule.exports = { skip: true };\n"
    files["engine/o_engine/cfg.py"] = 'AWS = "AKIAABCDEFGHIJKLMNOP"\n'
    wd, p = _candidate(tiny, tmp_path, files)
    r = gates.run_gates(p, wd, tiny[1], _cfg())
    by = _by(r)
    assert not r.passed
    assert not by["protected_paths"].passed and "main/agent.js" in by["protected_paths"].detail
    assert not by["secret_scan"].passed and "AWS access key" in by["secret_scan"].detail
    assert by["diff_budget"].passed and by["dangerous_patterns"].passed
    assert by["engine_pytest"].detail.startswith("not run")


@needs_node
def test_marker_in_shared_file_is_a_violation(tiny, tmp_path):
    files = dict(GOOD, **{"main/main.js": "'use strict';\nconst PROXY_KEYS = [];\nconst x = 1;\n"})
    wd, p = _candidate(tiny, tmp_path, files)
    by = _by(gates.run_gates(p, wd, tiny[1], _cfg()))
    assert not by["protected_paths"].passed and "secrets" in by["protected_paths"].detail


@needs_node
def test_dangerous_patterns(tiny, tmp_path):
    files = dict(GOOD, **{"engine/o_engine/net.py": textwrap.dedent('''\
        import httpx
        def f(u):
            r = httpx.get(u, verify=False)
            return eval(r.text)
        CMD = ["claude", "--dangerously-skip-permissions"]
        ''')})
    wd, p = _candidate(tiny, tmp_path, files)
    d = _by(gates.run_gates(p, wd, tiny[1], _cfg()))["dangerous_patterns"]
    assert not d.passed
    for name in ("TLS verification disabled", "dynamic code execution", "permission bypass"):
        assert name in d.detail
    assert not gates._scan({"a.py": [("+", "model.eval()")]}, gates.DANGEROUS_PATTERNS)


@needs_node
def test_missing_new_test_fails_acceptance(tiny, tmp_path):
    wd, p = _candidate(tiny, tmp_path, {"engine/o_engine/clamp.py": GOOD["engine/o_engine/clamp.py"]})
    r = gates.run_gates(p, wd, tiny[1], _cfg())
    by = _by(r)
    assert not r.passed and not by["acceptance_test"].passed and "no new test" in by["acceptance_test"].detail
    assert by["engine_pytest"].passed  # the rest still measured


@needs_node
def test_new_failure_and_deleted_test_fail_engine_pytest(tiny, tmp_path):
    files = dict(GOOD, **{"engine/tests/test_base.py": "def test_one():\n    assert 1 + 1 == 3\n"})
    wd, p = _candidate(tiny, tmp_path, files)
    c = _by(gates.run_gates(p, wd, tiny[1], _cfg()))["engine_pytest"]
    assert not c.passed
    assert "new failures: tests.test_base::test_one" in c.detail
    assert "no longer run: tests.test_base::test_two" in c.detail


@needs_node
def test_broken_or_shrunk_node_selftest_fails(tiny, tmp_path):
    files = dict(GOOD, **{"main/tools/selftest-a.js": "'use strict';\nconsole.log('ok a1');\nprocess.exit(1);\n"})
    wd, p = _candidate(tiny, tmp_path, files)
    c = _by(gates.run_gates(p, wd, tiny[1], _cfg()))["node_selftests"]
    assert not c.passed and "rc=1" in c.detail and "fewer checks" in c.detail


@needs_node
def test_offline_regression_and_tolerance(tiny, tmp_path):
    files = dict(GOOD, **{"engine/score.json": json.dumps({"off": 7, "on": 9})})
    wd, p = _candidate(tiny, tmp_path, files)
    c = _by(gates.run_gates(p, wd, tiny[1], _cfg()))["offline_osworld"]
    assert not c.passed and "regressed: off:all hit_rate" in c.detail
    assert c.metric_before == pytest.approx(0.8) and c.metric_after == pytest.approx(0.7)
    c2 = _by(gates.run_gates(p, wd, tiny[1], _cfg(osworld_tolerance=0.15)))["offline_osworld"]
    assert c2.passed


@needs_node
def test_diff_budget(tiny, tmp_path):
    wd, p = _candidate(tiny, tmp_path, GOOD)
    c = _by(gates.run_gates(p, wd, tiny[1], _cfg(max_lines=5, fail_fast=False)))["diff_budget"]
    assert not c.passed and c.metric_after == 8


@needs_node
def test_new_dependency_license_declaration_and_lockfile(tiny, tmp_path):
    files = dict(GOOD)
    files["engine/pyproject.toml"] = '[project]\nname = "x"\ndependencies = [\n  "pydantic>=2.6",\n  "gplthing>=1",\n]\n'
    files["package.json"] = json.dumps({"name": "t", "dependencies": {"ws": "^8.0.0", "leftpad": "^1.0.0"}})
    wd, p = _candidate(tiny, tmp_path, files, new_deps=["pypi:gplthing>=1"])
    lic = {("pypi", "gplthing"): "GPL-3.0-only", ("npm", "leftpad"): "MIT"}
    by = _by(gates.run_gates(p, wd, tiny[1], _cfg(license_lookup=lambda e, n: lic.get((e, n)))))
    assert not by["license_scan"].passed and "pypi:gplthing=GPL-3.0-only" in by["license_scan"].detail
    assert "npm:leftpad=MIT" in by["license_scan"].detail
    assert not by["declared_dependencies"].passed and "npm:leftpad" in by["declared_dependencies"].detail
    assert not by["lockfile_consistency"].passed and "package-lock.json was not updated" in by["lockfile_consistency"].detail


@needs_node
def test_lockfile_must_match_package_json(tiny, tmp_path):
    files = dict(GOOD)
    files["package.json"] = json.dumps({"name": "t", "dependencies": {"ws": "^8.1.0"}})
    files["package-lock.json"] = json.dumps({"packages": {"": {"dependencies": {"ws": "^8.0.0"}}}})
    wd, p = _candidate(tiny, tmp_path, files, new_deps=["npm:ws@^8.1.0"])
    by = _by(gates.run_gates(p, wd, tiny[1], _cfg()))
    assert by["declared_dependencies"].passed and by["license_scan"].passed
    assert not by["lockfile_consistency"].passed and "do not match" in by["lockfile_consistency"].detail


@needs_node
def test_dirty_worktree_and_foreign_baseline(tiny, tmp_path):
    wd, p = _candidate(tiny, tmp_path, GOOD)
    (wd / "stray.txt").write_text("x")
    assert not _by(gates.run_gates(p, wd, tiny[1], _cfg()))["clean_worktree"].passed
    r = gates.run_gates(p, wd, {"commit": "0" * 40}, _cfg())
    assert not r.passed and r.checks[0].name == "baseline_ancestor"
    assert not gates.run_gates(p, wd, None, _cfg()).passed


def test_registry_license_parses_pypi_and_npm(monkeypatch):
    answers = {
        "https://pypi.org/pypi/a/json": {"info": {"license_expression": "MIT", "license": ""}},
        "https://pypi.org/pypi/b/json": {"info": {"license": "", "classifiers": [
            "License :: OSI Approved :: BSD License"]}},
        "https://registry.npmjs.org/@s%2Fc/latest": {"license": {"type": "ISC"}},
    }
    monkeypatch.setattr(gates, "_http_json", lambda url, timeout=15: answers.get(url))
    assert gates.registry_license("pypi", "a") == "MIT"
    assert gates.license_ok(gates.registry_license("pypi", "b"))
    assert gates.registry_license("npm", "@s/c") == "ISC"
    assert gates.registry_license("pypi", "missing") is None


# ---------------------------------------------------------------- no sandbox: docs / dependency pins only

def test_docs_only_diff_passes_without_a_sandbox(tiny, tmp_path):
    wd, p = _candidate(tiny, tmp_path, {"docs/upgrades.md": "# Upgrades\n\nHow O evolves.\n", "README.md": "# t\n"})
    r = gates.run_gates(p, wd, {"commit": tiny[1]["commit"], "sandbox": "none"}, _cfg(runner_for=None))
    by = _by(r)
    assert r.passed, [(c.name, c.detail) for c in r.checks if not c.passed]
    assert all(by[n].passed and by[n].skipped and by[n].detail.startswith("waived") for n in gates.DYNAMIC_CHECKS)


def test_dependency_pin_bump_passes_without_a_sandbox(tiny, tmp_path):
    files = {"package.json": json.dumps({"name": "t", "dependencies": {"ws": "^8.18.0"}}, indent=2) + "\n",
             "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {"": {"dependencies": {"ws": "^8.18.0"}}}})}
    wd, p = _candidate(tiny, tmp_path, files, new_deps=["npm:ws@8.18.0"])
    r = gates.run_gates(p, wd, {"commit": tiny[1]["commit"]}, _cfg(runner_for=None))
    assert r.passed, [(c.name, c.detail) for c in r.checks if not c.passed]
    assert _by(r)["engine_pytest"].skipped and _by(r)["declared_dependencies"].passed


def test_code_diff_still_fails_without_a_sandbox(tiny, tmp_path):
    wd, p = _candidate(tiny, tmp_path, dict(GOOD, **{"docs/x.md": "doc\n"}))
    r = gates.run_gates(p, wd, {"commit": tiny[1]["commit"]}, _cfg(runner_for=None))
    by = _by(r)
    assert not r.passed and by["engine_pytest"].detail.startswith("not run: no sandbox") and not by["engine_pytest"].skipped
    r2 = gates.run_gates(p, wd, {"commit": tiny[1]["commit"]}, _cfg(runner_for=None, waive_dynamic_for_no_exec=False))
    assert not r2.passed  # the waiver can be switched off
    wd3, p3 = _candidate(tiny, tmp_path / "b", {"docs/y.md": "doc\n"})
    assert not gates.run_gates(p3, wd3, {"commit": tiny[1]["commit"]}, _cfg(runner_for=None, waive_dynamic_for_no_exec=False)).passed


def test_make_checkout_fails_fast_without_git(tmp_path):
    plain = tmp_path / "installed"
    plain.mkdir()
    (plain / "package.json").write_text("{}")
    assert builder.git_checkout_reason(plain) == builder.NO_GIT_MESSAGE
    assert builder.git_checkout_reason(tmp_path / "nowhere").startswith(builder.NO_GIT_MESSAGE)
    with pytest.raises(builder.BuildError, match="installed without git"):
        builder.make_checkout(plain, tmp_path / "wd", "evolve/x")
    assert not (tmp_path / "wd").exists() or not any((tmp_path / "wd").iterdir())
