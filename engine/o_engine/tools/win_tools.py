"""Windows system tools: PowerShell, Settings pages, winget, registry values.

Wiring. O's agent tools are registered on the Node side (``main/tools/bridge-mcp.js``, tool ids
``mcp__o__<name>``), so the engine exposes these as HTTP routes and the bridge wraps them the way it wraps
``/script/run``. ``server.py`` includes ``win_router(...)`` only when ``sys.platform == 'win32'``; elsewhere
the routes do not exist (404). All routes sit behind the usual ``x-o-token`` guard.

    POST /win/powershell      {"script": str, "timeout_s": int=60}
                              -> {"ok", "exit_code", "stdout", "stderr", "elapsed_ms", "timed_out", "exe"}
    POST /win/open_settings   {"page": "display" | "ms-settings:display" | ""}
                              -> {"ok": true, "uri": "ms-settings:display"}
    POST /win/winget          {"id": "Microsoft.PowerToys", "source": "winget"?, "scope": "user"|"machine"?,
                               "timeout_s": int=600}
                              -> the /win/powershell shape (winget's output) plus "id"
    POST /win/registry        {"hive": "HKCU", "key": "Software\\\\X", "name": "Y", "value": ..., "type": "REG_SZ"}
                              -> {"ok": true, "path": "HKEY_CURRENT_USER\\\\Software\\\\X\\\\Y", "type": "REG_SZ"}

Refusals. A request that touches a protected area is answered ``403`` with the structured body
``{"detail": {"refused": true, "rule": <label>, "reason": <why>, "matched": <what matched>}}`` and a
``policy`` entry in the action log. The rules are ``WIN_PROTECTED_RULES``, the Windows form of O's standing
constraints: O never handles passwords / credentials or their stores, and never disables or weakens security
software (Windows Defender, SmartScreen, the firewall, UAC, the Security Center services, PowerShell's
execution policy). O also never makes payments and never bypasses CAPTCHA / bot checks; those guards live in
the browser layer and nothing here reaches them. Malformed input is a ``422``, never a refusal.

Suggested bridge tool names for ``main/tools/bridge-mcp.js``: ``win_powershell``, ``win_open_settings``,
``win_winget_install``, ``win_set_registry``.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_SCRIPT_CHARS = 64_000
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600
WINGET_TIMEOUT_S = 600
CREATE_NO_WINDOW = 0x08000000

HIVES: dict[str, str] = {
    "HKLM": "HKEY_LOCAL_MACHINE", "HKEY_LOCAL_MACHINE": "HKEY_LOCAL_MACHINE",
    "HKCU": "HKEY_CURRENT_USER", "HKEY_CURRENT_USER": "HKEY_CURRENT_USER",
    "HKU": "HKEY_USERS", "HKEY_USERS": "HKEY_USERS",
    "HKCR": "HKEY_CLASSES_ROOT", "HKEY_CLASSES_ROOT": "HKEY_CLASSES_ROOT",
    "HKCC": "HKEY_CURRENT_CONFIG", "HKEY_CURRENT_CONFIG": "HKEY_CURRENT_CONFIG",
}
REG_TYPES: tuple[str, ...] = ("REG_SZ", "REG_EXPAND_SZ", "REG_DWORD", "REG_QWORD", "REG_MULTI_SZ", "REG_BINARY")
_SETTINGS_PAGE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_WINGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class WinToolRefused(Exception):
    """A protected-area refusal. ``payload`` is the structured body the route returns."""

    def __init__(self, rule: "ProtectedRule", matched: str) -> None:
        super().__init__(f"refused ({rule.label}): {rule.reason}")
        self.rule = rule
        self.matched = matched

    @property
    def payload(self) -> dict[str, Any]:
        return {"refused": True, "rule": self.rule.label, "reason": self.rule.reason, "matched": self.matched}


class WinToolError(RuntimeError):
    """The tool could not run (not on Windows, executable missing, bad value)."""


@dataclass(frozen=True)
class ProtectedRule:
    """One protected area. ``registry`` are case-insensitive globs over ``HIVE\\key`` and ``HIVE\\key\\name``;
    ``script`` are case-insensitive regexes over a PowerShell script (also applied to a winget id)."""

    label: str
    reason: str
    registry: tuple[str, ...] = ()
    script: tuple[str, ...] = ()


# The Windows form of O's standing constraints. Kept deliberately coarse: a whole protected subtree or a whole
# cmdlet is refused rather than individual switches, so a reworded request lands on the same rule.
WIN_PROTECTED_RULES: tuple[ProtectedRule, ...] = (
    ProtectedRule(
        "credentials",
        "O never reads, writes or handles passwords, credentials or credential stores",
        registry=(
            "HKEY_LOCAL_MACHINE\\SAM*",
            "HKEY_LOCAL_MACHINE\\SECURITY*",
            "HKEY_LOCAL_MACHINE\\SYSTEM\\*\\Control\\Lsa*",
            "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon\\*Password*",
            "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon\\AutoAdminLogon",
            "*\\Microsoft\\Credentials*",
            "*\\Microsoft\\Vault*",
            "*\\Microsoft\\Protect*",
            "*Password*",
        ),
        script=(
            r"\bGet-Credential\b", r"\bPSCredential\b", r"\bConvertTo-SecureString\b", r"\bConvertFrom-SecureString\b",
            r"\bGet-StoredCredential\b", r"\bCredentialManager\b", r"\bcmdkey\b", r"\bvaultcmd\b",
            r"\bSet-LocalUser\b.*-Password\b", r"\bNew-LocalUser\b.*-Password\b", r"\bnet\s+user\b\s+\S+\s+\S+",
            r"\\Microsoft\\(Credentials|Vault|Protect)\b", r"\bLogin Data\b", r"\\Control\\Lsa\b",
            r"\bHKLM[:\\]+(SAM|SECURITY)\b", r"\bHKEY_LOCAL_MACHINE\\(SAM|SECURITY)\b", r"\\config\\(SAM|SECURITY)\b",
        ),
    ),
    ProtectedRule(
        "defender",
        "O never disables, weakens or excludes paths from Windows Defender / Windows Security",
        registry=(
            "*\\Windows Defender*",
            "*\\Windows Defender Security Center*",
            "HKEY_LOCAL_MACHINE\\SYSTEM\\*\\Services\\WinDefend*",
            "HKEY_LOCAL_MACHINE\\SYSTEM\\*\\Services\\WdNisSvc*",
            "HKEY_LOCAL_MACHINE\\SYSTEM\\*\\Services\\Sense*",
            "HKEY_LOCAL_MACHINE\\SYSTEM\\*\\Services\\SecurityHealthService*",
            "HKEY_LOCAL_MACHINE\\SYSTEM\\*\\Services\\wscsvc*",
        ),
        script=(
            r"\bSet-MpPreference\b", r"\bAdd-MpPreference\b", r"\bRemove-MpPreference\b",
            r"\bUninstall-WindowsFeature\b.*Defender", r"\bDisable-WindowsOptionalFeature\b.*Defender",
            r"\b(Stop|Set|Disable)-Service\b.*\b(WinDefend|WdNisSvc|Sense|SecurityHealthService|wscsvc|MpsSvc)\b",
            r"\bsc(\.exe)?\s+(stop|config|delete)\s+(WinDefend|WdNisSvc|Sense|SecurityHealthService|wscsvc|MpsSvc)\b",
            r"\bWindows Defender\b", r"\bDisableAntiSpyware\b", r"\bDisableRealtimeMonitoring\b",
        ),
    ),
    ProtectedRule(
        "smartscreen",
        "O never turns off SmartScreen or other reputation checks",
        registry=("*\\SmartScreen*", "*\\Explorer\\SmartScreenEnabled", "*\\PhishingFilter*"),
        script=(r"\bSmartScreen", r"\bEnableSmartScreen\b"),
    ),
    ProtectedRule(
        "firewall",
        "O never disables the Windows Firewall or opens it wholesale",
        registry=(
            "*\\WindowsFirewall*",
            "HKEY_LOCAL_MACHINE\\SYSTEM\\*\\Services\\SharedAccess\\Parameters\\FirewallPolicy*",
            "HKEY_LOCAL_MACHINE\\SYSTEM\\*\\Services\\MpsSvc*",
            "HKEY_LOCAL_MACHINE\\SYSTEM\\*\\Services\\BFE*",
        ),
        script=(
            r"\bSet-NetFirewallProfile\b", r"\bnetsh\b.*\badvfirewall\b.*\b(set|reset)\b",
            r"\bNew-NetFirewallRule\b.*-Action\s+Allow.*-Direction\s+Inbound.*(-Program\s+Any|-LocalPort\s+Any|-RemoteAddress\s+Any)",
            r"\bDisable-NetFirewallRule\b", r"\bRemove-NetFirewallRule\b",
        ),
    ),
    ProtectedRule(
        "uac",
        "O never changes User Account Control or the elevation prompt",
        registry=(
            "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System*",
            "*\\EnableLUA", "*\\ConsentPromptBehaviorAdmin", "*\\ConsentPromptBehaviorUser", "*\\PromptOnSecureDesktop",
        ),
        script=(r"\bEnableLUA\b", r"\bConsentPromptBehavior(Admin|User)\b", r"\bPromptOnSecureDesktop\b",
                r"Policies\\System\b"),
    ),
    ProtectedRule(
        "execution-policy",
        "O never relaxes PowerShell's execution policy or script-block logging",
        registry=("*\\PowerShell\\*ExecutionPolicy*", "*\\PowerShell\\ScriptBlockLogging*"),
        script=(r"\bSet-ExecutionPolicy\b", r"\bScriptBlockLogging\b"),
    ),
)


# ------------------------------------------------------------------------- checks


def registry_path(hive: str, key: str, name: str | None = None) -> str:
    """``HKEY_...\\key[\\name]`` with the hive expanded and separators normalised; ``WinToolError`` on a bad hive."""
    full = HIVES.get((hive or "").strip().upper())
    if full is None:
        raise WinToolError(f"unknown registry hive {hive!r} (HKLM, HKCU, HKU, HKCR, HKCC)")
    k = (key or "").replace("/", "\\").strip("\\ ")
    if not k or ".." in k.split("\\"):
        raise WinToolError("registry key must be a non-empty relative path under the hive")
    path = f"{full}\\{k}"
    return f"{path}\\{name}" if name else path


def check_registry(hive: str, key: str, name: str | None = None) -> ProtectedRule | None:
    """The protected rule a registry location falls under, else None."""
    key_path = registry_path(hive, key).lower()
    full_path = registry_path(hive, key, name).lower() if name else key_path
    for rule in WIN_PROTECTED_RULES:
        for glob in rule.registry:
            g = glob.lower()
            if fnmatchcase(key_path, g) or fnmatchcase(full_path, g):
                return rule
    return None


def check_script(script: str) -> tuple[ProtectedRule, str] | None:
    """``(rule, matched_text)`` for the first protected pattern a script hits, else None."""
    text = script or ""
    for rule in WIN_PROTECTED_RULES:
        for pattern in rule.script:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return rule, m.group(0)[:120]
    return None


def _refuse_script(script: str) -> None:
    hit = check_script(script)
    if hit is not None:
        raise WinToolRefused(hit[0], hit[1])


# ------------------------------------------------------------------------- tools


def powershell_exe() -> str:
    """``pwsh`` when installed, else Windows PowerShell."""
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def powershell_args(script: str, exe: str | None = None) -> list[str]:
    """The argv for one script: no profile, non-interactive, the script passed base64/UTF-16LE encoded so
    quoting and newlines survive the command line unchanged."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [exe or powershell_exe(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def run_powershell(script: str, timeout_s: int | float = DEFAULT_TIMEOUT_S, *, exe: str | None = None) -> dict[str, Any]:
    """Run ``script`` and return ``{ok, exit_code, stdout, stderr, elapsed_ms, timed_out, exe}``. Protected
    scripts raise ``WinToolRefused`` before anything runs."""
    if not isinstance(script, str) or not script.strip():
        raise WinToolError("script must be a non-empty string")
    if len(script) > MAX_SCRIPT_CHARS:
        raise WinToolError(f"script longer than {MAX_SCRIPT_CHARS} characters")
    timeout = max(1.0, min(float(timeout_s or DEFAULT_TIMEOUT_S), float(MAX_TIMEOUT_S)))
    _refuse_script(script)
    args = powershell_args(script, exe)
    started = time.monotonic()
    kwargs: dict[str, Any] = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, stdin=subprocess.DEVNULL)
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "exit_code": -1, "stdout": _text(exc.stdout), "stderr": _text(exc.stderr),
                "elapsed_ms": int((time.monotonic() - started) * 1000), "timed_out": True, "exe": args[0]}
    except OSError as exc:
        raise WinToolError(f"cannot start {args[0]}: {exc}") from None
    return {"ok": proc.returncode == 0, "exit_code": int(proc.returncode), "stdout": _text(proc.stdout),
            "stderr": _text(proc.stderr), "elapsed_ms": int((time.monotonic() - started) * 1000),
            "timed_out": False, "exe": args[0]}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def settings_uri(page: str | None) -> str:
    """``"display"`` / ``"ms-settings:display"`` / ``""`` -> ``"ms-settings:display"`` / ``"ms-settings:"``."""
    p = (page or "").strip().lower()
    if p.startswith("ms-settings:"):
        p = p[len("ms-settings:"):]
    if p and not _SETTINGS_PAGE.match(p):
        raise WinToolError(f"settings page {page!r} is not a valid ms-settings: page name")
    return f"ms-settings:{p}"


def open_settings(page: str | None = "", *, starter: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Open a Settings page by ``ms-settings:`` URI (ShellExecute). Only the page opens; nothing is changed."""
    uri = settings_uri(page)
    start = starter or getattr(os, "startfile", None)
    if not callable(start):
        raise WinToolError("opening ms-settings: URIs needs Windows (os.startfile)")
    try:
        start(uri)
    except OSError as exc:
        raise WinToolError(f"cannot open {uri}: {exc}") from None
    return {"ok": True, "uri": uri}


def winget_args(package_id: str, source: str | None = None, scope: str | None = None) -> list[str]:
    args = ["winget", "install", "--id", package_id, "--exact", "--silent", "--disable-interactivity",
            "--accept-package-agreements", "--accept-source-agreements"]
    if source:
        args += ["--source", source]
    if scope:
        args += ["--scope", scope]
    return args


def winget_install(package_id: str, source: str | None = None, scope: str | None = None,
                   timeout_s: int | float = WINGET_TIMEOUT_S) -> dict[str, Any]:
    """``winget install --id <id> -e`` with agreements accepted; the result has the /win/powershell shape."""
    pid = (package_id or "").strip()
    if not _WINGET_ID.match(pid):
        raise WinToolError(f"winget id {package_id!r} is not a valid package id")
    if source and not re.match(r"^[A-Za-z0-9._-]{1,64}$", source):
        raise WinToolError(f"winget source {source!r} is not valid")
    if scope and scope not in ("user", "machine"):
        raise WinToolError("winget scope must be 'user' or 'machine'")
    _refuse_script(pid)
    exe = shutil.which("winget")
    if exe is None and sys.platform == "win32":
        raise WinToolError("winget is not installed (App Installer from the Microsoft Store)")
    args = winget_args(pid, source, scope)
    if exe:
        args[0] = exe
    started = time.monotonic()
    kwargs: dict[str, Any] = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=max(1.0, min(float(timeout_s or WINGET_TIMEOUT_S), float(MAX_TIMEOUT_S))),
                                  stdin=subprocess.DEVNULL)
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "id": pid, "exit_code": -1, "stdout": _text(exc.stdout), "stderr": _text(exc.stderr),
                "elapsed_ms": int((time.monotonic() - started) * 1000), "timed_out": True, "exe": args[0]}
    except OSError as exc:
        raise WinToolError(f"cannot start winget: {exc}") from None
    return {"ok": proc.returncode == 0, "id": pid, "exit_code": int(proc.returncode), "stdout": _text(proc.stdout),
            "stderr": _text(proc.stderr), "elapsed_ms": int((time.monotonic() - started) * 1000),
            "timed_out": False, "exe": args[0]}


def coerce_registry_value(value: Any, reg_type: str) -> Any:
    """The Python value ``winreg.SetValueEx`` takes for ``reg_type``; ``WinToolError`` when it does not fit."""
    t = (reg_type or "").upper()
    if t not in REG_TYPES:
        raise WinToolError(f"registry type {reg_type!r} is not one of {', '.join(REG_TYPES)}")
    try:
        if t in ("REG_SZ", "REG_EXPAND_SZ"):
            if not isinstance(value, str):
                raise WinToolError(f"{t} needs a string value")
            return value
        if t in ("REG_DWORD", "REG_QWORD"):
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise WinToolError(f"{t} needs an integer value")
            n = int(value, 0) if isinstance(value, str) else int(value)
            limit = 0xFFFFFFFF if t == "REG_DWORD" else 0xFFFFFFFFFFFFFFFF
            if not 0 <= n <= limit:
                raise WinToolError(f"{t} value {n} is out of range")
            return n
        if t == "REG_MULTI_SZ":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise WinToolError("REG_MULTI_SZ needs a list of strings")
            return list(value)
        if not isinstance(value, str):
            raise WinToolError("REG_BINARY needs a hex string")
        return bytes.fromhex(value.replace(" ", ""))
    except ValueError as exc:
        raise WinToolError(f"{t}: {exc}") from None


def set_registry_value(hive: str, key: str, name: str, value: Any, reg_type: str = "REG_SZ", *,
                       winreg: Any = None) -> dict[str, Any]:
    """Create ``hive\\key`` if needed and set ``name`` to ``value``. Protected locations raise ``WinToolRefused``
    before the key is opened. ``winreg`` defaults to the stdlib module (Windows only)."""
    if not isinstance(name, str) or not name.strip():
        raise WinToolError("registry value name must be a non-empty string")
    path = registry_path(hive, key, name)
    rule = check_registry(hive, key, name)
    if rule is not None:
        raise WinToolRefused(rule, path)
    data = coerce_registry_value(value, reg_type)
    t = reg_type.upper()
    if winreg is None:
        try:
            import winreg as _winreg  # type: ignore
        except ImportError:
            raise WinToolError("the registry exists only on Windows") from None
        winreg = _winreg
    hive_const = getattr(winreg, HIVES[hive.strip().upper()])
    sub_key = (key or "").replace("/", "\\").strip("\\ ")
    try:
        handle = winreg.CreateKeyEx(hive_const, sub_key, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(handle, name, 0, getattr(winreg, t), data)
        finally:
            winreg.CloseKey(handle)
    except OSError as exc:
        raise WinToolError(f"cannot write {path}: {exc}") from None
    return {"ok": True, "path": path, "type": t}


# ------------------------------------------------------------------------- routes


class PowerShellRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str = Field(min_length=1, max_length=MAX_SCRIPT_CHARS)
    timeout_s: int = Field(default=DEFAULT_TIMEOUT_S, ge=1, le=MAX_TIMEOUT_S)


class OpenSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: str = Field(default="", max_length=128)


class WingetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    source: str | None = Field(default=None, max_length=64)
    scope: str | None = Field(default=None, pattern="^(user|machine)$")
    timeout_s: int = Field(default=WINGET_TIMEOUT_S, ge=1, le=MAX_TIMEOUT_S)


class RegistryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hive: str = Field(min_length=3, max_length=20)
    key: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=256)
    value: Any
    type: str = Field(default="REG_SZ", max_length=16)


def _parse(model: Any, body: Any) -> Any:
    try:
        return model.model_validate(body or {})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def win_router(guard: list[Any], log: Any = None) -> APIRouter:
    """``/win/*`` routes. Included by ``server.create_app`` only on ``sys.platform == 'win32'``."""
    router = APIRouter(prefix="/win", dependencies=guard)

    def note(kind: str, msg: str, **fields: Any) -> None:
        if log is not None:
            try:
                log.record(kind, msg, **fields)
            except Exception:  # noqa: BLE001 - logging never fails a tool call
                pass

    def guarded_call(tool: str, fn: Callable[[], dict[str, Any]], **fields: Any) -> dict[str, Any]:
        try:
            result = fn()
        except WinToolRefused as exc:
            note("policy", f"win tool {tool} refused: {exc.rule.label}", tool=tool, **exc.payload, **fields)
            raise HTTPException(status_code=403, detail=exc.payload) from None
        except WinToolError as exc:
            note("note", f"win tool {tool} failed: {exc}", tool=tool, error=str(exc), **fields)
            raise HTTPException(status_code=422, detail=str(exc)) from None
        summary = {k: v for k, v in result.items() if k not in ("stdout", "stderr")}
        note("result", f"win tool {tool} ok={result.get('ok')}", tool=tool, **{**fields, **summary})
        return result

    @router.post("/powershell")
    def powershell(body: dict[str, Any]) -> dict[str, Any]:
        req = _parse(PowerShellRequest, body)
        note("task", f"win powershell ({len(req.script)} chars)", chars=len(req.script), timeout_s=req.timeout_s)
        return guarded_call("powershell", lambda: run_powershell(req.script, req.timeout_s), chars=len(req.script))

    @router.post("/open_settings")
    def open_settings_route(body: dict[str, Any]) -> dict[str, Any]:
        req = _parse(OpenSettingsRequest, body)
        return guarded_call("open_settings", lambda: open_settings(req.page), page=req.page)

    @router.post("/winget")
    def winget(body: dict[str, Any]) -> dict[str, Any]:
        req = _parse(WingetRequest, body)
        note("task", f"winget install {req.id}", id=req.id, source=req.source, scope=req.scope)
        return guarded_call("winget", lambda: winget_install(req.id, req.source, req.scope, req.timeout_s), id=req.id)

    @router.post("/registry")
    def registry(body: dict[str, Any]) -> dict[str, Any]:
        req = _parse(RegistryRequest, body)
        return guarded_call("registry", lambda: set_registry_value(req.hive, req.key, req.name, req.value, req.type),
                            hive=req.hive, key=req.key, name=req.name, type=req.type)

    return router
