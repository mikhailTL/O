"""macOS native computer use: the AX backend's dependencies, the Accessibility (TCC) health fields, and the
headless web model fallback to O's proxy. Runs on any OS: pyobjc / atomacos are faked in sys.modules."""
import importlib
import sys
import tomllib
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from o_engine import server as srv
from o_engine import web
from o_engine.server import EngineSettings, create_app

TOKEN = "mac-test-token"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
INSTALL_SH = Path(__file__).resolve().parents[2] / "install" / "install.sh"


# ------------------------------------------------------------------ fakes


def _fake_appservices(trusted: bool) -> types.ModuleType:
    m = types.ModuleType("ApplicationServices")
    m.AXIsProcessTrusted = lambda: trusted
    for name in ("AXUIElementCopyActionNames", "AXUIElementCopyAttributeValue", "AXUIElementCreateApplication",
                 "AXUIElementGetTypeID", "AXUIElementPerformAction", "AXUIElementSetAttributeValue",
                 "AXValueGetType", "AXValueGetTypeID", "AXValueGetValue"):
        setattr(m, name, lambda *a, **k: None)
    m.kAXValueCGPointType, m.kAXValueCGSizeType = 1, 2
    return m


def _fake_corefoundation() -> types.ModuleType:
    m = types.ModuleType("CoreFoundation")
    m.CFGetTypeID = lambda v: -1
    return m


@pytest.fixture
def mac(monkeypatch):
    """Pretend to be macOS; ``load(atomacos=bool, pyobjc=bool, trusted=bool)`` reloads ax.py against fakes."""
    monkeypatch.setattr(sys, "platform", "darwin")
    import o_engine.cu.backends.ax as ax

    def load(atomacos: bool, pyobjc: bool, trusted: bool = True):
        monkeypatch.setitem(sys.modules, "atomacos", types.ModuleType("atomacos") if atomacos else None)
        monkeypatch.setitem(sys.modules, "ApplicationServices", _fake_appservices(trusted) if pyobjc else None)
        monkeypatch.setitem(sys.modules, "CoreFoundation", _fake_corefoundation() if pyobjc else None)
        monkeypatch.delitem(sys.modules, "o_engine.cu.backends.ax_pyobjc", raising=False)
        return importlib.reload(ax)

    yield load
    monkeypatch.undo()
    sys.modules.pop("o_engine.cu.backends.ax_pyobjc", None)
    importlib.reload(ax)


def _client(tmp_path):
    settings = EngineSettings(token=TOKEN, data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sb"),
                              screenpipe_db=str(tmp_path / "none.db"), vllm_url="http://127.0.0.1:1/v1")
    return TestClient(create_app(settings, deps_factory=lambda s: object()))


def _h():
    return {"x-o-token": TOKEN}


# ------------------------------------------------------------------ health: three states


def test_absent_backend_reports_not_importable(mac, tmp_path):
    mac(atomacos=False, pyobjc=False)
    cu = srv.probe_cu_backend()
    assert cu["name"] == "ax" and cu["available"] is False
    assert cu["reason"].startswith("accessibility backend not importable") and "install.sh" in cu["reason"]
    assert srv.probe_accessibility_trusted() is None
    with _client(tmp_path) as c:
        body = c.get("/health", headers=_h()).json()
        assert set(body["cu_backend"]) == {"name", "available", "reason"}
        assert body["cu_backend"]["available"] is False and body["accessibility_trusted"] is None
        access = c.get("/computer/access", headers=_h()).json()
        assert access["accessibility_trusted"] is None and "install" in access["hint"]


def test_present_but_untrusted_reports_permission_needed(mac, tmp_path):
    ax = mac(atomacos=True, pyobjc=True, trusted=False)
    assert ax.AX_IMPL == "atomacos"
    cu = srv.probe_cu_backend()
    assert cu == {"name": "ax", "available": False, "reason": "accessibility permission needed"}
    with _client(tmp_path) as c:
        body = c.get("/health", headers=_h()).json()
        assert body["accessibility_trusted"] is False
        assert body["cu_backend"]["reason"] == "accessibility permission needed"
        access = c.get("/computer/access", headers=_h()).json()
        assert access["accessibility_trusted"] is False
        assert access["hint"].startswith("Accessibility access is off for O.")
    backend = ax.AxBackend()
    with pytest.raises(Exception) as e:
        backend.tree()
    assert "accessibility permission needed" in str(e.value) and "System Settings" in str(e.value)


def test_present_and_trusted_is_available(mac, tmp_path):
    mac(atomacos=True, pyobjc=True, trusted=True)
    cu = srv.probe_cu_backend()
    assert cu == {"name": "ax", "available": True, "reason": ""}
    with _client(tmp_path) as c:
        body = c.get("/health", headers=_h()).json()
        assert body["accessibility_trusted"] is True and body["cu_backend"]["available"] is True
        assert c.get("/computer/access", headers=_h()).json()["hint"] == ""


def test_pyobjc_fallback_when_atomacos_does_not_import(mac):
    ax = mac(atomacos=False, pyobjc=True, trusted=True)
    assert ax.AX_IMPL == "pyobjc" and ax._IMPORT_ERROR is None
    assert ax.AxBackend().available is True


def test_o_ax_impl_forces_pyobjc(mac, monkeypatch):
    monkeypatch.setenv("O_AX_IMPL", "pyobjc")
    ax = mac(atomacos=True, pyobjc=True, trusted=True)
    assert ax.AX_IMPL == "pyobjc"


def test_accessibility_trusted_is_none_off_macos():
    if sys.platform == "darwin":
        pytest.skip("real macOS")
    assert srv.probe_accessibility_trusted() is None
    from o_engine.cu.backends.ax import accessibility_trusted

    assert accessibility_trusted() is None


# ------------------------------------------------------------------ dependencies


def test_pyproject_declares_macos_only_ax_dependencies():
    deps = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    by_name = {}
    for d in deps:
        name = d.split(";")[0].strip()
        for op in (">=", "==", "<", ">", "~="):
            name = name.split(op)[0]
        by_name[name.strip().lower()] = d
    for pkg in ("atomacos", "pyobjc-core", "pyobjc-framework-cocoa", "pyobjc-framework-quartz",
                "pyobjc-framework-applicationservices"):
        assert pkg in by_name, pkg
        spec, _, marker = by_name[pkg].partition(";")
        assert marker.replace('"', "'").replace(" ", "") == "sys_platform=='darwin'", by_name[pkg]
    assert by_name["atomacos"].startswith("atomacos>=3.3")
    # nothing macOS-only leaks into the unconditional list
    assert not any(("pyobjc" in d or "atomacos" in d) and "darwin" not in d for d in deps)


def test_installer_installs_and_checks_ax_on_macos():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "step_macos_ax()" in text and '[ "$OS" = "Darwin" ] || return 0' in text
    assert "atomacos>=3.3" in text and "pyobjc-framework-ApplicationServices" in text
    assert "accessibility_trusted()" in text
    main = text[text.index("# ---------- main ----------"):]
    assert main.index("step_required_packages") < main.index("step_macos_ax") < main.index("step_engine_check")


# ------------------------------------------------------------------ headless web model


def _probe(answers):
    return lambda url, token: answers.get(url, False)


def test_web_llm_prefers_vllm_when_it_answers():
    ep = web.select_web_llm("http://gpu:8000/v1", "llama", "http://127.0.0.1:8787/v1", "tok", "haiku",
                            probe=_probe({"http://gpu:8000/v1": True, "http://127.0.0.1:8787/v1": True}))
    assert ep["via"] == "vllm" and ep["base_url"] == "http://gpu:8000/v1" and ep["model"] == "llama"
    assert ep["headers"] == {}


def test_web_llm_falls_back_to_o_proxy_with_fast_model():
    ep = web.select_web_llm("http://127.0.0.1:8000/v1", "llama", "http://127.0.0.1:8787/v1/", "tok", "",
                            probe=_probe({"http://127.0.0.1:8787/v1": True}))
    assert ep["via"] == "proxy" and ep["base_url"] == "http://127.0.0.1:8787/v1"
    assert ep["api_key"] == "tok" and ep["model"] == web.PROXY_MODEL
    assert ep["headers"] == {"x-o-caller": "browser-use"} and ep["fast_model"] == "haiku"


def test_web_llm_without_proxy_keeps_vllm_for_the_error():
    ep = web.select_web_llm("http://127.0.0.1:8000/v1", "llama", "", "", "haiku", probe=_probe({}))
    assert ep["via"] == "none" and ep["base_url"] == "http://127.0.0.1:8000/v1"
    ep = web.select_web_llm("http://127.0.0.1:8000/v1", "llama", "http://127.0.0.1:8787/v1", "", "haiku",
                            probe=_probe({"http://127.0.0.1:8787/v1": True}))
    assert ep["via"] == "none", "no token: the proxy would refuse every request"


def test_browser_use_runner_builds_proxy_llm_and_text_only_agent(monkeypatch):
    seen = {}

    class FakeChat:
        def __init__(self, model, base_url, api_key, default_headers=None, add_schema_to_system_prompt=False):
            seen.update(model=model, base_url=base_url, api_key=api_key, headers=default_headers,
                        schema=add_schema_to_system_prompt)

    llm_mod = types.ModuleType("browser_use.llm")
    llm_mod.ChatOpenAI = FakeChat
    monkeypatch.setitem(sys.modules, "browser_use.llm", llm_mod)
    r = web.BrowserUseRunner("http://127.0.0.1:8000/v1", "llama", proxy_url="http://127.0.0.1:8787/v1",
                             proxy_token="tok", fast_model="haiku")
    monkeypatch.setattr(web, "_endpoint_answers", lambda url, token="": url.endswith(":8787/v1"))
    r._make_llm()
    assert seen == {"model": "o-ai-code", "base_url": "http://127.0.0.1:8787/v1", "api_key": "tok",
                    "headers": {"x-o-caller": "browser-use"}, "schema": True}
    assert r._agent_kwargs() == {"use_vision": False}


def test_browser_use_runner_vllm_path_unchanged(monkeypatch):
    seen = {}

    class OldChat:  # no default_headers / add_schema_to_system_prompt: extras are dropped
        def __init__(self, model, base_url, api_key):
            seen.update(model=model, base_url=base_url)

    llm_mod = types.ModuleType("browser_use.llm")
    llm_mod.ChatOpenAI = OldChat
    monkeypatch.setitem(sys.modules, "browser_use.llm", llm_mod)
    monkeypatch.setattr(web, "_endpoint_answers", lambda url, token="": True)
    r = web.BrowserUseRunner("http://gpu:8000/v1", "llama", proxy_url="http://127.0.0.1:8787/v1", proxy_token="tok")
    r._make_llm()
    assert seen == {"model": "llama", "base_url": "http://gpu:8000/v1"} and r._agent_kwargs() == {}


def test_engine_settings_read_proxy_env(monkeypatch):
    monkeypatch.setenv("O_PROXY_URL", "http://127.0.0.1:8787/v1")
    monkeypatch.setenv("O_PROXY_TOKEN", "tok")
    monkeypatch.setenv("O_FAST_MODEL", "")
    s = EngineSettings.from_env()
    assert s.proxy_url == "http://127.0.0.1:8787/v1" and s.proxy_token == "tok" and s.fast_model == "haiku"
