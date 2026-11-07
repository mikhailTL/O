"""Installs an approved external skill as a pinned MCP server for O (plan L2, "Ask before installing").

``McpInstaller`` is the ``installer`` / ``uninstaller`` that ``SkillRegistry.install`` / ``uninstall`` call. It
runs only for a skill the registry already holds as approved (by the person in the app, or automatically because
the person turned ``evolveAskBeforeInstall`` off). What it does:

1. **Pin.** The skill's package must be ``npm:<name>`` or ``pypi:<name>`` with an exact release (``1.2.3``; no
   ranges, tags or ``latest``). The entry is built from those two validated strings only:
   npm -> ``{"type": "stdio", "command": "npx", "args": ["-y", "<name>@<version>"]}``,
   pypi -> ``{"type": "stdio", "command": "uvx", "args": ["<name>==<version>"]}``. No ``env``, no headers:
   signal text never reaches the config, and the serialized entry must pass the same secret scan as upgrades.
2. **Name.** Derived from the package (``@scope/x`` -> ``scope-x``). ``o`` and ``playwright`` are reserved
   (O's own servers; main/agent.js also deletes them from every merged map) and are refused.
3. **Self-test, before activation**, when the evolve sandbox (``evolveSandbox``: Docker / E2B) is available:
   * fetch (host, network to the package registry only): ``npm install --ignore-scripts`` into an empty staging
     dir, or ``pip download --only-binary=:all:`` (wheels only). Neither runs any of the package's code on
     this computer: lifecycle scripts are off and no sdist is built;
   * test (sandbox, offline: Docker ``--network none`` / E2B internet off): the staged package is started and
     must answer an MCP ``initialize`` and ``tools/list`` over stdio (``skill_selftest.py``).
   A failing self-test refuses the install (nothing is activated). With no sandbox the skill is installed and
   recorded ``tested: false``; the Evolution feed says "untested".
4. **Record.** ``<dir>/installs.jsonl`` is append-only provenance (skill id, signal, source URL, licence,
   package, version, entry, test result, who approved). ``<dir>/mcp-servers.json`` is the active set derived from
   it, which main/agent.js merges under the person's own ``mcpServers`` setting (theirs win on a name clash) when
   it writes o-mcp.json. Uninstall appends a row that supersedes the install and rewrites the active set.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .gates import SECRET_PATTERNS
from .schema import OK_LICENSES

RESERVED = frozenset({"o", "playwright"})
NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]{0,100}/)?[a-z0-9][a-z0-9._-]{0,100}$")
PYPI_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,100}[A-Za-z0-9])?$")
NPM_VERSION = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z.-]{1,40})?$")
PYPI_VERSION = re.compile(r"^\d{1,6}(?:\.\d{1,6}){0,3}(?:(?:a|b|rc)\d{1,4})?(?:\.post\d{1,4})?$")
SELFTEST = Path(__file__).with_name("skill_selftest.py")
FETCH_TIMEOUT_S = 300
TEST_TIMEOUT_S = 120
_SECRETS = [re.compile(p) for _, p in SECRET_PATTERNS]


class InstallRefused(ValueError):
    """The skill cannot be installed safely (not pinnable, reserved name, secret, failed self-test)."""


@dataclass(frozen=True)
class Pin:
    ecosystem: str  # npm | pypi
    package: str
    version: str
    name: str  # the mcpServers key

    def entry(self) -> dict[str, Any]:
        if self.ecosystem == "npm":
            return {"type": "stdio", "command": "npx", "args": ["-y", f"{self.package}@{self.version}"]}
        return {"type": "stdio", "command": "uvx", "args": [f"{self.package}=={self.version}"]}


def server_name(package: str) -> str:
    n = re.sub(r"[^a-z0-9._-]+", "-", package.lower().lstrip("@").replace("/", "-")).strip("-._")
    return n[:64]


def pin(skill: Any) -> Pin:
    """The exact package release this skill installs, or ``InstallRefused``."""
    raw = str(getattr(skill, "package", "") or "")
    version = str(getattr(skill, "release", "") or "").strip().lstrip("v")
    if ":" not in raw:
        raise InstallRefused("not installable: the signal names no npm or PyPI package")
    eco, package = raw.split(":", 1)
    eco = eco.strip().lower()
    package = package.strip()
    if eco == "npm":
        ok_name, ok_ver = NPM_NAME.match(package), NPM_VERSION.match(version)
    elif eco == "pypi":
        ok_name, ok_ver = PYPI_NAME.match(package), PYPI_VERSION.match(version)
    else:
        raise InstallRefused(f"not installable: unsupported registry {eco[:20]!r}")
    if not ok_name:
        raise InstallRefused("not installable: the package name is not a plain registry name")
    if not ok_ver:
        raise InstallRefused(f"cannot pin: {version[:40]!r} is not an exact release")
    name = server_name(package)
    if not name or name in RESERVED or package.lower() in RESERVED:
        raise InstallRefused(f"refused: {name or package!r} is reserved for O's own servers")
    return Pin(eco, package, version, name)


def secret_hits(obj: Any) -> list[str]:
    text = json.dumps(obj, sort_keys=True)
    return [name for (name, _), rx in zip(SECRET_PATTERNS, _SECRETS) if rx.search(text)]


def _read(path: Path) -> list[dict[str, Any]]:
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


def default_fetch(p: Pin, staging: Path) -> None:
    """Download the pinned release into ``staging`` without running any of its code on this computer."""
    if p.ecosystem == "npm":
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            raise InstallRefused("cannot fetch: npm is not installed")
        argv = [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund", "--omit=dev", "--no-save",
                "--prefix", str(staging), f"{p.package}@{p.version}"]
    else:
        argv = ["python3" if shutil.which("python3") else "python", "-m", "pip", "download", "--only-binary=:all:",
                "--no-cache-dir", "--dest", str(staging / "wheels"), f"{p.package}=={p.version}"]
    env = {k: v for k, v in os.environ.items() if not re.search(r"(?i)token|secret|key|passw|auth", k)}
    env["npm_config_ignore_scripts"] = "true"
    r = subprocess.run(argv, cwd=staging, capture_output=True, text=True, timeout=FETCH_TIMEOUT_S, env=env,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise InstallRefused(f"could not fetch {p.package} {p.version}: {(r.stderr or r.stdout).strip()[-300:]}")


class McpInstaller:
    """``installer`` / ``uninstaller`` for ``SkillRegistry``. ``sandbox()`` -> ``sandbox.SandboxChoice``."""

    def __init__(self, dir: str | os.PathLike[str], sandbox: Callable[[], Any] | None = None,
                 fetch: Callable[[Pin, Path], None] = default_fetch, now: Callable[[], float] = time.time) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.dir / "installs.jsonl"
        self.config_path = self.dir / "mcp-servers.json"
        self.sandbox, self.fetch, self.now = sandbox, fetch, now
        self._lock = threading.RLock()

    # ------------------------------------------------------------ provenance

    def rows(self) -> list[dict[str, Any]]:
        return _read(self.ledger_path)

    def active(self) -> dict[str, dict[str, Any]]:
        """skill id -> its current install row (uninstalled ones dropped)."""
        cur: dict[str, dict[str, Any]] = {}
        for r in self.rows():
            sid = str(r.get("skill_id") or "")
            if r.get("action") == "install":
                cur[sid] = r
            elif r.get("action") == "uninstall":
                cur.pop(sid, None)
        return cur

    def _append(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            row = {"seq": len(self.rows()) + 1, "ts": float(self.now()), **row}
            with self.ledger_path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
            self._write_config()
        return row

    def _write_config(self) -> None:
        servers = {r["name"]: r["entry"] for r in self.active().values()}
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"mcpServers": servers}, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.config_path)

    # ------------------------------------------------------------ self-test

    def selftest(self, p: Pin) -> dict[str, Any]:
        """Fetch, then start the server in the offline sandbox and require an MCP handshake. Raises on failure;
        returns ``tested: False`` when there is no sandbox."""
        choice = None
        if self.sandbox is not None:
            try:
                choice = self.sandbox()
            except Exception:  # noqa: BLE001 - a probe that crashes is no sandbox
                choice = None
        if choice is None or not getattr(choice, "available", False):
            why = getattr(choice, "reason", "") or "no sandbox"
            return {"tested": False, "backend": "none", "detail": f"untested: no sandbox to test it in ({why})"[:300]}
        staging = Path(tempfile.mkdtemp(prefix="o-skill-", dir=self.dir))
        try:
            self.fetch(p, staging)
            shutil.copy(SELFTEST, staging / SELFTEST.name)
            runner = choice.runner(staging)
            try:
                r = runner.run(["python3", "-I", SELFTEST.name, p.ecosystem, p.package, p.version],
                               timeout=TEST_TIMEOUT_S)
            finally:
                try:
                    runner.close()
                except Exception:  # noqa: BLE001
                    pass
            out = _last_json(r.stdout)
            if r.returncode != 0 or not out.get("ok"):
                why = out.get("error") or (r.stderr or r.stdout or "").strip()[-200:] or f"exit {r.returncode}"
                raise InstallRefused(f"self-test failed in the {choice.backend} sandbox: {why}"[:400])
            return {"tested": True, "backend": choice.backend,
                    "detail": f"tested in sandbox: answered MCP, {int(out.get('tools') or 0)} tools"}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ------------------------------------------------------------ install / uninstall

    def __call__(self, skill: Any) -> dict[str, Any]:
        return self.install(skill)

    def install(self, skill: Any) -> dict[str, Any]:
        lic = str(getattr(skill, "license", "") or "")
        if lic not in OK_LICENSES:  # the registry already refuses these; the installer does not trust that alone
            raise InstallRefused(f"licence {lic or 'unknown'} is not on the allow-list")
        p = pin(skill)
        entry = p.entry()
        hits = secret_hits({"name": p.name, "entry": entry})
        if hits:
            raise InstallRefused("refused: the config entry would contain a secret (" + ", ".join(hits) + ")")
        clash = [sid for sid, r in self.active().items() if r.get("name") == p.name and sid != skill.id]
        if clash:
            raise InstallRefused(f"refused: a server named {p.name!r} is already installed")
        test = self.selftest(p)
        row = self._append({
            "action": "install", "skill_id": skill.id, "name": p.name, "entry": entry, "ecosystem": p.ecosystem,
            "package": p.package, "version": p.version, "license": lic, "signal_id": getattr(skill, "signal_id", ""),
            "source_url": str(getattr(skill, "url", "") or "")[:500], "by": getattr(skill, "decided_by", "") or "",
            "via": getattr(skill, "decided_via", "") or "", **test,
        })
        return {k: row[k] for k in ("name", "package", "version", "license", "tested", "backend", "detail", "seq")}

    def uninstall(self, skill: Any, by: str = "user") -> dict[str, Any]:
        sid = getattr(skill, "id", skill)
        cur = self.active().get(sid)
        if cur is None:
            return {"removed": False}
        self._append({"action": "uninstall", "skill_id": sid, "name": cur["name"], "supersedes": cur.get("seq"), "by": by})
        return {"removed": True, "name": cur["name"]}


def _last_json(text: str) -> dict[str, Any]:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                j = json.loads(line)
                return j if isinstance(j, dict) else {}
            except ValueError:
                continue
    return {}
