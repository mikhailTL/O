"""Windows system tools (o_engine/tools/win_tools.py): protected-area refusals, PowerShell shell-out, settings
URIs, winget argv and registry writes, all without Windows (subprocess / winreg / startfile are faked)."""
from __future__ import annotations

import base64
import subprocess
import sys
import types

import pytest
from fastapi.testclient import TestClient

from o_engine.tools import win_tools as wt


class FakeCompleted:
    def __init__(self, returncode=0, stdout="out", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def fake_run(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def run(args, **kwargs):
        calls.append((list(args), kwargs))
        return FakeCompleted()

    monkeypatch.setattr(wt.subprocess, "run", run)
    return calls


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize("script, rule", [
    ("Set-MpPreference -DisableRealtimeMonitoring $true", "defender"),
    ("Add-MpPreference -ExclusionPath C:\\tools", "defender"),
    ("Stop-Service WinDefend", "defender"),
    ("Set-ItemProperty 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender' DisableAntiSpyware 1", "defender"),
    ("reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System /v EnableLUA /d 0", "uac"),
    ("Set-ItemProperty ... ConsentPromptBehaviorAdmin 0", "uac"),
    ("netsh advfirewall set allprofiles state off", "firewall"),
    ("Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False", "firewall"),
    ("$c = Get-Credential", "credentials"),
    ("ConvertTo-SecureString 'x' -AsPlainText -Force", "credentials"),
    ("cmdkey /list", "credentials"),
    ("reg save HKLM\\SAM C:\\sam", "credentials"),
    ("Copy-Item $env:LOCALAPPDATA\\Microsoft\\Vault\\x .", "credentials"),
    ("Set-ExecutionPolicy Unrestricted", "execution-policy"),
    ("Set-ItemProperty HKLM:\\...\\Explorer SmartScreenEnabled Off", "smartscreen"),
])
def test_protected_scripts_are_refused_before_running(script, rule, fake_run):
    with pytest.raises(wt.WinToolRefused) as exc:
        wt.run_powershell(script)
    assert exc.value.rule.label == rule
    assert exc.value.payload["refused"] is True and exc.value.payload["matched"]
    assert fake_run == [], "nothing was executed"


@pytest.mark.parametrize("hive, key, name, rule", [
    ("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "EnableLUA", "uac"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows Defender", "DisableAntiSpyware", "defender"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Real-Time Protection", "DisableRealtimeMonitoring", "defender"),
    ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\WinDefend", "Start", "defender"),
    ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\SharedAccess\\Parameters\\FirewallPolicy\\StandardProfile", "EnableFirewall", "firewall"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\WindowsFirewall\\DomainProfile", "EnableFirewall", "firewall"),
    ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\MpsSvc", "Start", "firewall"),
    ("HKLM", "SAM\\SAM", "x", "credentials"),
    ("HKLM", "SECURITY\\Policy", "x", "credentials"),
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Lsa", "RunAsPPL", "credentials"),
    ("HKCU", "Software\\Microsoft\\Credentials", "x", "credentials"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon", "DefaultPassword", "credentials"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "SmartScreenEnabled", "smartscreen"),
    ("HKLM", "SOFTWARE\\Microsoft\\PowerShell\\1\\ShellIds\\Microsoft.PowerShell", "ExecutionPolicy", "execution-policy"),
])
def test_protected_registry_locations_are_refused(hive, key, name, rule):
    class NeverWinreg:
        def __getattr__(self, item):
            raise AssertionError(f"winreg.{item} was touched")

    assert wt.check_registry(hive, key, name).label == rule
    with pytest.raises(wt.WinToolRefused) as exc:
        wt.set_registry_value(hive, key, name, 0, "REG_DWORD", winreg=NeverWinreg())
    assert exc.value.rule.label == rule and exc.value.payload["matched"].startswith("HKEY_")


def test_ordinary_locations_and_scripts_pass():
    assert wt.check_registry("HKCU", "Software\\O\\Prefs", "Theme") is None
    assert wt.check_registry("HKCU", "Control Panel\\Desktop", "Wallpaper") is None
    assert wt.check_registry("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Hidden") is None
    assert wt.check_script("Get-Process | Sort-Object CPU -Descending | Select-Object -First 5") is None
    assert wt.check_script("Get-MpComputerStatus | Select AntivirusEnabled") is None
    assert wt.check_script("Get-NetFirewallProfile | Select Name, Enabled") is None
    assert wt.check_script("New-Item -ItemType Directory -Path $env:USERPROFILE\\Projects") is None


# ------------------------------------------------------------------ PowerShell


def test_run_powershell_shells_out_with_encoded_command(fake_run, monkeypatch):
    monkeypatch.setattr(wt, "powershell_exe", lambda: "pwsh")
    script = "Get-Date; Write-Output 'héllo \"quoted\"'"
    out = wt.run_powershell(script, timeout_s=30)
    assert out == {"ok": True, "exit_code": 0, "stdout": "out", "stderr": "", "elapsed_ms": out["elapsed_ms"],
                   "timed_out": False, "exe": "pwsh"}
    (args, kwargs), = fake_run
    assert args[:4] == ["pwsh", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert base64.b64decode(args[4]).decode("utf-16-le") == script
    assert kwargs["timeout"] == 30.0 and kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert "creationflags" not in kwargs, "CREATE_NO_WINDOW only on Windows"


def test_run_powershell_reports_failure_and_timeout(monkeypatch):
    monkeypatch.setattr(wt.subprocess, "run", lambda args, **kw: FakeCompleted(returncode=3, stdout="", stderr="bad"))
    out = wt.run_powershell("Exit 3")
    assert out["ok"] is False and out["exit_code"] == 3 and out["stderr"] == "bad"

    def timeout(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"], output=b"partial", stderr=None)

    monkeypatch.setattr(wt.subprocess, "run", timeout)
    out = wt.run_powershell("Start-Sleep 999", timeout_s=5)
    assert out["timed_out"] is True and out["exit_code"] == -1 and out["stdout"] == "partial" and out["ok"] is False


def test_run_powershell_input_validation(fake_run):
    with pytest.raises(wt.WinToolError):
        wt.run_powershell("   ")
    with pytest.raises(wt.WinToolError):
        wt.run_powershell("x" * (wt.MAX_SCRIPT_CHARS + 1))
    assert fake_run == []


def test_run_powershell_clamps_timeout(fake_run):
    wt.run_powershell("Get-Date", timeout_s=10_000)
    assert fake_run[0][1]["timeout"] == float(wt.MAX_TIMEOUT_S)


# ------------------------------------------------------------------ settings, winget, registry


def test_open_settings_builds_ms_settings_uri():
    opened = []
    assert wt.open_settings("display", starter=opened.append) == {"ok": True, "uri": "ms-settings:display"}
    assert wt.open_settings("ms-settings:network-wifi", starter=opened.append)["uri"] == "ms-settings:network-wifi"
    assert wt.open_settings("", starter=opened.append)["uri"] == "ms-settings:"
    assert opened == ["ms-settings:display", "ms-settings:network-wifi", "ms-settings:"]
    with pytest.raises(wt.WinToolError):
        wt.open_settings("display; calc.exe", starter=opened.append)
    with pytest.raises(wt.WinToolError):
        wt.open_settings("http://example.com", starter=opened.append)


def test_winget_install_argv(fake_run, monkeypatch):
    monkeypatch.setattr(wt.shutil, "which", lambda name: None)
    out = wt.winget_install("Microsoft.PowerToys", scope="user")
    assert out["ok"] is True and out["id"] == "Microsoft.PowerToys"
    args, kwargs = fake_run[0]
    assert args == ["winget", "install", "--id", "Microsoft.PowerToys", "--exact", "--silent", "--disable-interactivity",
                    "--accept-package-agreements", "--accept-source-agreements", "--scope", "user"]
    assert kwargs["timeout"] == float(wt.WINGET_TIMEOUT_S)
    with pytest.raises(wt.WinToolError):
        wt.winget_install("Bad Id; rm")
    with pytest.raises(wt.WinToolError):
        wt.winget_install("X.Y", scope="everyone")


def test_set_registry_value_writes_through_winreg():
    class FakeWinreg:
        HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = 1, 2
        KEY_SET_VALUE = 0x2
        REG_SZ, REG_DWORD, REG_MULTI_SZ, REG_BINARY, REG_QWORD, REG_EXPAND_SZ = 1, 4, 7, 3, 11, 2
        calls: list[tuple] = []

        def CreateKeyEx(self, hive, sub, reserved, access):
            self.calls.append(("create", hive, sub, access))
            return "H"

        def SetValueEx(self, handle, name, reserved, typ, data):
            self.calls.append(("set", handle, name, typ, data))

        def CloseKey(self, handle):
            self.calls.append(("close", handle))

    reg = FakeWinreg()
    out = wt.set_registry_value("HKCU", "Software\\O\\Prefs", "Theme", "dark", "REG_SZ", winreg=reg)
    assert out == {"ok": True, "path": "HKEY_CURRENT_USER\\Software\\O\\Prefs\\Theme", "type": "REG_SZ"}
    assert reg.calls == [("create", 1, "Software\\O\\Prefs", 0x2), ("set", "H", "Theme", 1, "dark"), ("close", "H")]
    reg.calls.clear()
    wt.set_registry_value("hklm", "SOFTWARE/O", "Count", "0x10", "REG_DWORD", winreg=reg)
    assert reg.calls[1] == ("set", "H", "Count", 4, 16)
    wt.set_registry_value("HKCU", "Software\\O", "List", ["a", "b"], "REG_MULTI_SZ", winreg=reg)
    assert reg.calls[-2][4] == ["a", "b"]
    wt.set_registry_value("HKCU", "Software\\O", "Blob", "de ad", "REG_BINARY", winreg=reg)
    assert reg.calls[-2][4] == b"\xde\xad"
    for bad in [("HKXX", "k", "n", "v", "REG_SZ"), ("HKCU", "k", "n", 5, "REG_SZ"), ("HKCU", "k", "n", -1, "REG_DWORD"),
                ("HKCU", "k", "n", "v", "REG_NOPE"), ("HKCU", "..\\k", "n", "v", "REG_SZ"), ("HKCU", "k", "", "v", "REG_SZ")]:
        with pytest.raises(wt.WinToolError):
            wt.set_registry_value(*bad, winreg=reg)


# ------------------------------------------------------------------ routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    from o_engine import server as srv

    monkeypatch.setattr(sys, "platform", "win32")
    settings = srv.EngineSettings(token="tok", data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sb"))
    app = srv.create_app(settings)
    with TestClient(app) as c:
        c.headers["x-o-token"] = "tok"
        yield c, settings


def test_routes_refuse_protected_and_run_plain(client, fake_run, monkeypatch):
    c, settings = client
    monkeypatch.setattr(wt, "powershell_exe", lambda: "powershell")
    r = c.post("/win/powershell", json={"script": "Set-MpPreference -DisableRealtimeMonitoring $true"})
    assert r.status_code == 403
    assert r.json()["detail"]["refused"] is True and r.json()["detail"]["rule"] == "defender"
    assert fake_run == []
    r = c.post("/win/powershell", json={"script": "Get-Date", "timeout_s": 5})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["exe"] == "powershell"
    assert fake_run[0][0][:3] == ["powershell", "-NoProfile", "-NonInteractive"]
    r = c.post("/win/registry", json={"hive": "HKLM", "key": "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System",
                                      "name": "EnableLUA", "value": 0, "type": "REG_DWORD"})
    assert r.status_code == 403 and r.json()["detail"]["rule"] == "uac"
    r = c.post("/win/open_settings", json={"page": "bad page!"})
    assert r.status_code == 422
    r = c.post("/win/powershell", json={"script": ""})
    assert r.status_code == 422
    assert c.post("/win/powershell", json={"script": "Get-Date"}, headers={"x-o-token": "nope"}).status_code == 401
    log_text = (settings.data_dir_path / "engine-actions.log").read_text(encoding="utf-8")
    assert "win tool powershell refused: defender" in log_text and "win tool registry refused: uac" in log_text


def test_open_settings_route_uses_startfile(client, monkeypatch):
    c, _ = client
    opened = []
    monkeypatch.setattr(wt.os, "startfile", opened.append, raising=False)
    r = c.post("/win/open_settings", json={"page": "display"})
    assert r.status_code == 200 and r.json() == {"ok": True, "uri": "ms-settings:display"} and opened == ["ms-settings:display"]


def test_winget_route(client, fake_run, monkeypatch):
    c, _ = client
    monkeypatch.setattr(wt.shutil, "which", lambda name: "C:\\winget.exe")
    r = c.post("/win/winget", json={"id": "Git.Git"})
    assert r.status_code == 200 and r.json()["id"] == "Git.Git" and fake_run[0][0][:4] == ["C:\\winget.exe", "install", "--id", "Git.Git"]
    assert c.post("/win/winget", json={"id": "Git.Git", "scope": "all"}).status_code == 422
