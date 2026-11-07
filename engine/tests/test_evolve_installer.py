"""Skill installer (evolve/installer.py): pinned MCP entries, reserved names, no secrets, sandbox self-test,
append-only provenance with uninstall superseding, and the in-sandbox MCP handshake script itself."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from .evolve_fakes import FakeSkillRunner, fake_fetch, no_sandbox, skill_sandbox
from o_engine.evolve.installer import SELFTEST, InstallRefused, McpInstaller, pin
from o_engine.evolve.schema import Signal
from o_engine.evolve.skills import NotApproved, SkillRegistry


class Sk:
    def __init__(self, package, release, license="MIT", id="xs-1"):
        self.id, self.package, self.release, self.license = id, package, release, license
        self.url, self.signal_id, self.decided_by, self.decided_via = "https://example.org/x", "s1", "user", "app"


def test_pins_exact_npm_and_pypi_releases():
    assert pin(Sk("npm:screen-ocr-mcp", "1.2.0")).entry() == {"type": "stdio", "command": "npx", "args": ["-y", "screen-ocr-mcp@1.2.0"]}
    p = pin(Sk("npm:@acme/sheets-mcp", "v0.4.1"))
    assert p.name == "acme-sheets-mcp" and p.entry()["args"] == ["-y", "@acme/sheets-mcp@0.4.1"]
    assert pin(Sk("pypi:mcp-server-time", "2025.9.25")).entry() == {"type": "stdio", "command": "uvx", "args": ["mcp-server-time==2025.9.25"]}


@pytest.mark.parametrize("package,release", [
    ("npm:x", "^1.2.0"), ("npm:x", "latest"), ("npm:x", ""), ("npm:x", "1.x"), ("npm:x", ">=1.0.0"),
    ("pypi:x", "~=1.0"), ("x", "1.0.0"), ("cargo:x", "1.0.0"), ("npm:x;rm -rf /", "1.0.0"),
    ("npm:git+https://evil/x.git", "1.0.0"), ("npm:../x", "1.0.0"),
])
def test_refuses_anything_it_cannot_pin(package, release):
    with pytest.raises(InstallRefused):
        pin(Sk(package, release))


@pytest.mark.parametrize("package", ["npm:o", "npm:playwright", "pypi:O", "pypi:Playwright"])
def test_refuses_reserved_names(package, tmp_path):
    with pytest.raises(InstallRefused, match="reserved"):
        McpInstaller(tmp_path, sandbox=skill_sandbox, fetch=fake_fetch).install(Sk(package, "1.0.0"))


def test_never_writes_secrets(tmp_path):
    inst = McpInstaller(tmp_path, sandbox=skill_sandbox, fetch=fake_fetch)
    with pytest.raises(InstallRefused, match="secret"):
        inst.install(Sk("npm:ghp_" + "a1b2" * 9, "1.0.0"))
    assert not inst.config_path.exists() and inst.rows() == []
    inst.install(Sk("npm:screen-ocr-mcp", "1.2.0"))
    cfg = json.loads(inst.config_path.read_text())
    entry = cfg["mcpServers"]["screen-ocr-mcp"]
    assert set(entry) == {"type", "command", "args"}  # never env / headers: nothing secret can ride along
    assert (inst.config_path.stat().st_mode & 0o077) == 0


def test_refuses_unlicensed_even_if_called_directly(tmp_path):
    with pytest.raises(InstallRefused, match="licence"):
        McpInstaller(tmp_path, sandbox=skill_sandbox, fetch=fake_fetch).install(Sk("npm:x", "1.0.0", license="GPL-3.0"))


def test_self_test_runs_in_the_sandbox_before_activation(tmp_path):
    FakeSkillRunner.calls.clear()
    inst = McpInstaller(tmp_path, sandbox=skill_sandbox, fetch=fake_fetch)
    r = inst.install(Sk("npm:screen-ocr-mcp", "1.2.0"))
    assert r["tested"] is True and r["backend"] == "docker" and "3 tools" in r["detail"]
    call = FakeSkillRunner.calls[-1]
    assert call["argv"] == ["python3", "-I", SELFTEST.name, "npm", "screen-ocr-mcp", "1.2.0"]
    assert call["staged"] == sorted([SELFTEST.name, "node_modules"])  # fetched package + the test script only
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("o-skill-")]  # staging removed
    bad = McpInstaller(tmp_path / "b", sandbox=lambda: skill_sandbox(ok=False), fetch=fake_fetch)
    with pytest.raises(InstallRefused, match="self-test failed"):
        bad.install(Sk("npm:screen-ocr-mcp", "1.2.0"))
    assert bad.rows() == [] and not bad.config_path.exists()


def test_no_sandbox_installs_untested(tmp_path):
    fetched = []
    inst = McpInstaller(tmp_path, sandbox=no_sandbox, fetch=lambda p, d: fetched.append(p))
    r = inst.install(Sk("npm:screen-ocr-mcp", "1.2.0"))
    assert r["tested"] is False and r["backend"] == "none" and "untested" in r["detail"] and fetched == []


def test_uninstall_supersedes_append_only(tmp_path):
    inst = McpInstaller(tmp_path, sandbox=skill_sandbox, fetch=fake_fetch)
    inst.install(Sk("npm:a-mcp", "1.0.0", id="xs-a"))
    inst.install(Sk("npm:b-mcp", "2.0.0", id="xs-b"))
    with pytest.raises(InstallRefused, match="already installed"):
        inst.install(Sk("npm:a-mcp", "1.0.1", id="xs-c"))
    assert inst.uninstall(Sk("npm:a-mcp", "1.0.0", id="xs-a")) == {"removed": True, "name": "a-mcp"}
    rows = inst.rows()
    assert [r["action"] for r in rows] == ["install", "install", "uninstall"] and rows[-1]["supersedes"] == rows[0]["seq"]
    assert list(json.loads(inst.config_path.read_text())["mcpServers"]) == ["b-mcp"]
    assert set(inst.active()) == {"xs-b"} and inst.uninstall(Sk("npm:a-mcp", "1.0.0", id="xs-a")) == {"removed": False}
    assert {"package", "version", "license", "source_url", "signal_id", "tested", "by"} <= set(rows[0])  # provenance


def test_registry_auto_approval_only_when_asking_is_off(tmp_path):
    reg = SkillRegistry(tmp_path)
    (rec,) = reg.ingest_signals([Signal(id="s1", kind="mcp_server", source="mcp-registry", title="t", url="https://x",
                                        version="1.2.0", license="MIT", package="npm:t-mcp", topics=["skill"])])
    assert rec.package == "npm:t-mcp" and rec.release == "1.2.0"
    with pytest.raises(NotApproved):
        reg.auto_approve(rec.id, ask_before_install=True)
    with pytest.raises(NotApproved):
        reg.install(rec.id, lambda s: None)
    a = reg.auto_approve(rec.id, ask_before_install=False)
    assert (a.status, a.decided_by, a.decided_via) == ("approved", "auto", "setting")
    inst = McpInstaller(tmp_path / "i", sandbox=no_sandbox, fetch=fake_fetch)
    assert reg.install(rec.id, inst).status == "installed"
    with pytest.raises(NotApproved):
        reg.auto_approve(rec.id, ask_before_install=False)  # only a pending candidate
    assert reg.uninstall(rec.id, inst.uninstall).status == "uninstalled"
    with pytest.raises(NotApproved):
        reg.uninstall(rec.id, inst.uninstall)


FAKE_SERVER = r"""
const rl = require('readline').createInterface({ input: process.stdin });
const say = (m) => process.stdout.write(JSON.stringify(m) + '\n');
rl.on('line', (l) => { const m = JSON.parse(l);
  if (m.method === 'initialize') say({ jsonrpc: '2.0', id: m.id, result: { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fake', version: '1.0.0' } } });
  if (m.method === 'tools/list') say({ jsonrpc: '2.0', id: m.id, result: { tools: [{ name: 'ocr' }, { name: 'shot' }] } });
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_selftest_script_speaks_mcp(tmp_path):
    pkg = tmp_path / "node_modules" / "fake-mcp"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": "fake-mcp", "version": "1.0.0", "bin": {"fake-mcp": "server.js"}}))
    (pkg / "server.js").write_text(FAKE_SERVER)
    r = subprocess.run([sys.executable, "-I", str(SELFTEST), "npm", "fake-mcp", "1.0.0"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout.strip().splitlines()[-1]) == {"ok": True, "tools": 2}
    (pkg / "server.js").write_text("process.stdin.on('data', () => {});")  # never answers
    r2 = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(SELFTEST.parent)!r}); "
                         "import skill_selftest as s; s.WAIT_S = 1.0; sys.exit(s.main(['npm', 'fake-mcp', '1.0.0']))"],
                        cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r2.returncode == 1 and "no MCP answer" in r2.stdout


def test_switch_and_installer_are_protected_from_evolve():
    from o_engine.evolve import protected

    for f in ("engine/o_engine/evolve/installer.py", "engine/o_engine/evolve/skill_selftest.py", "engine/o_engine/evolve/skills.py"):
        assert protected.match(f), f
    diff = "--- a/main/scheduler.js\n+++ b/main/scheduler.js\n+    O_EVOLVE_ASK_BEFORE_INSTALL: '0',\n"
    assert any("Ask before installing" in v for v in protected.violations(["main/scheduler.js"], diff))
