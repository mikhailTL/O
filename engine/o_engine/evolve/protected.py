"""Paths and code markers self-evolution may never modify (plan L, gate "protected paths").

Two layers:

* ``PROTECTED``: repo-relative path globs. Any diff that adds, edits, deletes or renames a matching file
  is a violation. These are the files that implement the permission/approval flow, the payment guard,
  the action log, consent and sensor defaults, containment, secrets handling, the release/update path,
  and evolve's own safety machinery (builder, gates, protected list, baseline, the shared contract that
  holds ``OK_LICENSES``, their tests, the eval harness the gates score against, and the CI workflow).
* ``TOUCH_MARKERS`` / ``REMOVE_MARKERS``: patterns checked against the changed lines of every *other*
  file, for the large shared files (``main/main.js``, ``engine/o_engine/server.py``, ...) that carry a
  few protected lines among much ordinary code. A ``+``/``-`` line matching a touch marker, or a ``-``
  line matching a remove marker, is a violation.

``violations(diff_files, diff_text=None)`` returns human-readable reasons; empty means allowed.
"""
from __future__ import annotations

import posixpath
import re
from functools import lru_cache
from typing import Iterable

# (glob, why). Globs are repo-relative POSIX paths; ``**`` crosses directories, ``*`` stays in one segment.
PROTECTED_RULES: tuple[tuple[str, str], ...] = (
    # permission / approval prompt flow
    ("main/agent.js", "permission flow: spawns claude with --permission-prompt-tool mcp__o__approve, permission modes"),
    ("main/tools/bridge-mcp.js", "permission flow: the `approve` permission-prompt tool and goal checkpoint gate"),
    ("main/tools/checkpoints.js", "permission flow: checkpoint classifier (publish/send/spend/...) used by approve"),
    ("main/proxy.js", "permission flow + secrets: proxy's --permission-prompt-tool wiring and proxyToken auth"),
    ("engine/o_engine/gate.py", "permission flow: engine circuit-breaker (log/ask/auto approval)"),
    # payment guard
    ("extension/guard.js", "payment guard: blocks payment fields and pay/buy buttons in every page"),
    ("extension/background.js", "payment guard: attaches/notifies the guard"),
    ("extension/manifest.json", "payment guard: content-script registration and extension permissions"),
    ("main/link.js", "payment guard: isPaymentUrl refuses payment sites over O Link"),
    # action log
    ("engine/o_engine/actionlog.py", "action log: append-only hash-chained log (engine)"),
    ("main/actionlog.js", "action log: append-only hash-chained log (app)"),
    # consent / sensor defaults
    ("main/settings.js", "consent + secrets: sensor defaults (all off), e2bApiKey/proxyToken sanitising"),
    ("main/ambient.js", "consent: consentFilter (a sensor goes on only via the confirmation sheet)"),
    ("engine/o_engine/ambient/__init__.py", "consent: ambient layer is opt-in, every sensor off by default"),
    ("engine/o_engine/ambient/layer.py", "consent: sensors enabled only from O_AMBIENT + per-sensor settings"),
    ("engine/o_engine/ambient/sensors/__init__.py", "consent: Sensor base, enabled=False default"),
    # containment / sandbox policy
    ("engine/o_engine/containment.py", "containment: disposable E2B/Docker sandboxes, E2B_API_KEY handling"),
    ("engine/o_engine/executor.py", "containment + secrets: path sandbox and scrub_env"),
    ("engine/o_engine/ambient/isolation.py", "containment: headless isolation backend selection"),
    ("engine/o_engine/ambient/microsandbox.py", "containment: microsandbox backend"),
    ("engine/o_engine/ambient/firecracker.py", "containment: Firecracker backend"),
    # secrets handling
    ("main/engine.js", "secrets: passes E2B_API_KEY / engine token into the engine env"),
    # release / update path
    ("main/updater.js", "release path: the auto-updater that ships merged releases"),
    # evolve's own safety machinery
    ("engine/o_engine/evolve/gates.py", "evolve: the gates"),
    ("engine/o_engine/evolve/protected.py", "evolve: this list"),
    ("engine/o_engine/evolve/baseline.py", "evolve: baseline metrics the gates compare against"),
    ("engine/o_engine/evolve/builder.py", "evolve: tool scoping, no --dangerously-skip-permissions, spend cap"),
    ("engine/o_engine/evolve/schema.py", "evolve: shared contract incl. OK_LICENSES"),
    ("engine/tests/test_evolve_gates.py", "evolve: gate tests"),
    ("engine/tests/test_evolve_builder.py", "evolve: builder tests"),
    ("engine/o_engine/evolve/triage.py", "evolve: untrusted-signal handling and proposal validation"),
    ("engine/o_engine/evolve/untrusted.py", "evolve: untrusted-text cleaning, data blocks, verbatim-copy checks"),
    ("engine/o_engine/evolve/sandbox.py", "evolve: the sandbox candidate code runs in (offline, limits, no host fallback)"),
    ("engine/o_engine/evolve/sandbox_test.py", "evolve: o-sandbox-test, the builder's only test command"),
    ("engine/o_engine/evolve/sandbox.Dockerfile", "evolve: the pinned sandbox image"),
    ("engine/o_engine/evolve/pipeline.py", "evolve: auto-ship policy and the sandbox-gated code-build tier"),
    ("engine/o_engine/evolve/routes.py", "evolve: wiring of the sandbox into builder, gates and baseline"),
    ("engine/o_engine/evolve/skills.py", "evolve: who may approve a third-party skill (user, or the Ask-off setting)"),
    ("engine/o_engine/evolve/installer.py", "evolve: pinned skill entries, reserved names, secret scan, sandbox self-test"),
    ("engine/o_engine/evolve/skill_selftest.py", "evolve: the in-sandbox skill self-test"),
    ("engine/tests/test_evolve_installer.py", "evolve: skill installer tests"),
    ("engine/tests/test_evolve_sandbox.py", "evolve: sandbox / untrusted-text tests"),
    ("engine/tests/test_evolve_triage.py", "evolve: triage tests"),
    ("engine/tests/evolve_fakes.py", "evolve: fakes the evolve tests use"),
    # code the test runner loads implicitly: a candidate could fake or skip every gate from these
    ("**/conftest.py", "pytest auto-loads it: could rewrite test results"),
    ("**/pytest.ini", "pytest config: could deselect or redirect tests"),
    ("**/setup.cfg", "pytest/tool config"),
    ("**/tox.ini", "pytest/tool config"),
    ("**/sitecustomize.py", "imported by every Python process"),
    ("**/usercustomize.py", "imported by every Python process"),
    ("**/*.pth", "executed by the Python site module"),
    ("engine/eval/osworld/offline.py", "evolve: offline eval harness the regression gate scores with"),
    ("engine/tests/fixtures/offline/**", "evolve: offline eval episodes the regression gate scores on"),
    (".github/**", "CI: workflows incl. evolve-gates.yml"),
    # repository plumbing that can execute code or redirect git
    (".git/**", "git internals"),
    (".gitattributes", "git filters can execute code"),
    (".gitmodules", "submodules can redirect fetches"),
)

PROTECTED: tuple[str, ...] = tuple(g for g, _ in PROTECTED_RULES)

# Changed lines (added or removed) matching these, in any file, are protected territory.
TOUCH_MARKERS: tuple[tuple[str, str], ...] = (
    (r"permission-prompt-tool|mcp__o__approve|permissionPromptTool", "permission-prompt wiring"),
    (r"\bpermissionMode\b|PERMISSION_MODES|O_PERMISSION_MODE|\bpermission_mode\b", "permission mode"),
    (r"dangerously-?skip-?permissions|dangerouslySkipPermissions|bypassPermissions", "permission bypass"),
    (r"consentFilter|\bconsented\b", "sensor consent"),
    (r"\bsensor(Wifi|Sonar|Camera|HeartRate)\b|O_SENSOR_|\bO_AMBIENT\b|\bsensor_(csi|sonar|camera|heart)\b|vitals_opt_in",
     "sensor defaults"),
    (r"e2bApiKey|e2b_api_key|E2B_API_KEY|proxyToken|proxy_token|x-o-token|O_PERM_TOKEN|O_ENGINE_TOKEN",
     "secrets"),
    (r"\bscrub_env\b|_SCRUB_", "secret scrubbing"),
    (r"isPaymentUrl|PAY_HOST|\b__oGuard\b", "payment guard"),
    (r"highRiskSandbox|select_sandbox|select_isolated|run_high_risk", "containment policy"),
    (r"evolveSandbox|O_EVOLVE_SANDBOX|O_EVOLVE_DEV_UNSAFE|unsafe-host-dev|o-sandbox-test|runner_for", "evolve sandbox"),
    (r"evolveAskBeforeInstall|O_EVOLVE_ASK_BEFORE_INSTALL|evolveAutoShip|O_EVOLVE_AUTO_SHIP|auto_approve|RESERVED_MCP|installedSkillServers",
     "the person's Ask before installing switch and the installed-skill merge"),
    (r"\[tool\.pytest|addopts|testpaths", "pytest configuration"),
)

# Removed lines matching these weaken a protected property (logging); adding them is fine.
REMOVE_MARKERS: tuple[tuple[str, str], ...] = (
    (r"\bactionLog\b|\bActionLog\b|\bget_log\(|\blogApp\(", "action-log call"),
)


@lru_cache(maxsize=None)
def _glob_re(glob: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(r".*")
            i += 2
        elif glob[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def normalize(path: str) -> str | None:
    """Repo-relative POSIX path, or ``None`` when it is absolute or escapes the repo."""
    p = path.strip().replace("\\", "/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        return None
    p = posixpath.normpath(p)
    if p == ".." or p.startswith("../"):
        return None
    return p


def match(path: str) -> str | None:
    """The reason ``path`` is protected, or ``None``. Case-insensitive (macOS/Windows filesystems)."""
    p = normalize(path)
    if p is None:
        return "path is absolute or escapes the repository"
    low = p.lower()
    for glob, why in PROTECTED_RULES:
        if _glob_re(glob.lower()).match(low):
            return why
    return None


def changed_lines(diff_text: str) -> dict[str, list[tuple[str, str]]]:
    """``{path: [("+"|"-", line), ...]}`` from a unified ``git diff`` (``--no-renames`` recommended)."""
    out: dict[str, list[tuple[str, str]]] = {}
    cur: str | None = None
    old: str | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            cur = old = None
            continue
        if raw.startswith("--- "):
            name = raw[4:].split("\t")[0]
            old = None if name == "/dev/null" else (name[2:] if name.startswith("a/") else name)
            continue
        if raw.startswith("+++ "):
            name = raw[4:].split("\t")[0]
            cur = (name[2:] if name.startswith("b/") else name) if name != "/dev/null" else old
            if cur is not None:
                out.setdefault(cur, [])
            continue
        if cur is None:
            continue
        if raw.startswith("+"):
            out[cur].append(("+", raw[1:]))
        elif raw.startswith("-"):
            out[cur].append(("-", raw[1:]))
    return out


def violations(diff_files: Iterable[str], diff_text: str | None = None) -> list[str]:
    """Reasons the change is not allowed: each protected path touched, then each protected marker hit."""
    found: list[str] = []
    seen: set[str] = set()
    for f in diff_files:
        why = match(f)
        if why and f not in seen:
            seen.add(f)
            found.append(f"{f}: {why}")
    if diff_text:
        touch = [(re.compile(p), why) for p, why in TOUCH_MARKERS]
        remove = [(re.compile(p), why) for p, why in REMOVE_MARKERS]
        for path, lines in changed_lines(diff_text).items():
            if match(path):
                continue  # already reported as a protected path
            hits: list[str] = []
            for sign, line in lines:
                for rx, why in touch:
                    if rx.search(line) and why not in hits:
                        hits.append(why)
                if sign == "-":
                    for rx, why in remove:
                        if rx.search(line) and why not in hits:
                            hits.append(why)
            for why in hits:
                found.append(f"{path}: changes protected code ({why})")
    return found
