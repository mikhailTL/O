"""Windows computer-use backend (uia.py) and its construction by server.py, exercised on any OS through a
monkeypatched ``sys.platform`` and fake ``uiautomation`` / ``pyautogui`` / ``mss`` modules."""
from __future__ import annotations

import importlib
import sys
import types

import pytest

from o_engine.cu.backends.base import BackendError, Node, Rect
from o_engine.cu.schema import Target


# ------------------------------------------------------------------ fakes


class FakeRect:
    def __init__(self, left, top, w, h):
        self.left, self.top, self._w, self._h = left, top, w, h

    def width(self):
        return self._w

    def height(self):
        return self._h


class FakePattern:
    def __init__(self, **attrs):
        self.calls: list[tuple] = []
        for k, v in attrs.items():
            setattr(self, k, v)

    def __getattr__(self, item):
        if item.startswith("_"):
            raise AttributeError(item)

        def _call(*a):
            self.calls.append((item, *a))

        return _call


class FakeCtrl:
    def __init__(self, type_name, name="", rect=(0, 0, 100, 40), children=(), patterns=None, pid=0, hwnd=0,
                 value=None, focusable=False):
        self.ControlTypeName = type_name
        self.Name = name
        self.BoundingRectangle = FakeRect(*rect)
        self._children = list(children)
        self._patterns = dict(patterns or {})
        self.ProcessId = pid
        self.NativeWindowHandle = hwnd
        self.IsOffscreen = False
        self.IsEnabled = True
        self.HasKeyboardFocus = False
        self.IsKeyboardFocusable = focusable
        self.focused = 0
        self.activated = 0
        if value is not None:
            self._patterns["GetValuePattern"] = FakePattern(Value=value)

    def GetChildren(self):
        return list(self._children)

    def SetFocus(self):
        self.focused += 1

    def SetActive(self):
        self.activated += 1

    def __getattr__(self, item):
        if item.startswith("Get") and item.endswith("Pattern"):
            return lambda: self._patterns.get(item)
        raise AttributeError(item)


class FakePyAutoGui(types.ModuleType):
    def __init__(self):
        super().__init__("pyautogui")
        self.FAILSAFE = True
        self.PAUSE = 0.1
        self.calls: list[tuple] = []

    def __getattr__(self, item):
        if item.startswith("_") or item in ("FAILSAFE", "PAUSE", "calls"):
            raise AttributeError(item)

        def _call(*a, **kw):
            self.calls.append((item, a, kw))

        return _call


def install_fakes(monkeypatch, tops, foreground=0, pyautogui=True):
    """A fake ``uiautomation`` whose desktop has ``tops`` as top-level windows; returns (uia_module, pyautogui)."""
    fake_uia = types.ModuleType("uiautomation")
    root = FakeCtrl("PaneControl", "Desktop", children=tops)
    fake_uia.GetRootControl = lambda: root
    fake_uia.GetForegroundWindow = lambda: foreground
    monkeypatch.setitem(sys.modules, "uiautomation", fake_uia)
    pg = FakePyAutoGui()
    if pyautogui:
        monkeypatch.setitem(sys.modules, "pyautogui", pg)
    else:
        monkeypatch.setitem(sys.modules, "pyautogui", None)
    import o_engine.cu.backends.uia as uia_mod

    uia_mod = importlib.reload(uia_mod)
    assert uia_mod._IMPORT_ERROR is None
    return uia_mod, pg


@pytest.fixture(autouse=True)
def _restore_uia_module():
    yield
    import o_engine.cu.backends.uia as uia_mod

    importlib.reload(uia_mod)  # back to the real (unimportable on Linux) state for other test files


def notepad_desktop():
    save = FakeCtrl("ButtonControl", "Save", rect=(10, 10, 80, 30), patterns={"GetInvokePattern": FakePattern()})
    plain = FakeCtrl("ButtonControl", "Legacy", rect=(200, 300, 60, 20))  # no pattern at all
    field = FakeCtrl("EditControl", "Body", rect=(10, 60, 300, 200), value="hello")
    raw = FakeCtrl("CustomControl", "Canvas", rect=(400, 400, 100, 100), focusable=True)
    win = FakeCtrl("WindowControl", "Untitled - Notepad", rect=(0, 0, 800, 600), children=[save, plain, field, raw],
                   pid=1234, hwnd=77)
    return [win], {"save": save, "plain": plain, "field": field, "raw": raw, "win": win}


# ------------------------------------------------------------------ server construction


def test_cu_backend_on_win32_is_host_display_with_pixels(tmp_path, monkeypatch):
    from o_engine import server as srv
    from o_engine.cu import backends as backends_pkg

    class Recording:
        name = "uia"
        available = True
        dpi_mode = "per-monitor"

        def __init__(self, display=None, allow_pixels=False):
            self.display, self.allow_pixels = display, allow_pixels

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backends_pkg, "get_backend", lambda display=None, allow_pixels=False: Recording(display, allow_pixels))
    settings = srv.EngineSettings(token="t", data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sb"))
    deps = srv.EngineDeps(settings)
    backend = deps.cu_backend(0)
    assert backend.allow_pixels is True and backend.display is None
    assert deps.cu_backend(0) is backend, "one backend per slot"
    assert deps.cu_display_status() == [{"display": "host", "running": True}]
    assert srv.cu_display_status(settings, None) == [{"display": "host", "running": True}]
    assert "computer-use: Windows host display, UI Automation + pixels enabled" in deps.log.text.read_text(encoding="utf-8")
    deps.stop_cu_displays()  # nothing to stop; must not raise


def test_cu_backend_darwin_and_linux_unchanged(tmp_path, monkeypatch):
    from o_engine import server as srv
    from o_engine.cu import backends as backends_pkg

    seen = []
    monkeypatch.setattr(backends_pkg, "get_backend",
                        lambda display=None, allow_pixels=False: seen.append((display, allow_pixels)) or object())
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = srv.EngineSettings(token="t", data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sb"))
    deps = srv.EngineDeps(settings)
    deps.cu_backend(0)
    assert seen == [(None, False)]
    assert deps.cu_display_status() == [{"display": "host", "running": False}]
    assert srv.cu_display_status(settings, None) == [{"display": "host", "running": False}]


def test_win_routes_registered_only_on_win32(tmp_path, monkeypatch):
    from o_engine import server as srv

    settings = srv.EngineSettings(token="t", data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sb"))
    from fastapi.testclient import TestClient

    def status(platform: str, path: str) -> int:
        monkeypatch.setattr(sys, "platform", platform)
        with TestClient(srv.create_app(settings)) as c:
            return c.post(path, json={}, headers={"x-o-token": "t"}).status_code

    for path in ("/win/powershell", "/win/open_settings", "/win/winget", "/win/registry"):
        assert status("linux", path) == 404
        assert status("darwin", path) == 404
        assert status("win32", path) in (200, 422), "registered: validation answers, not 404"


# ------------------------------------------------------------------ backend behaviour


def test_tree_groups_windows_per_process_and_maps_roles(monkeypatch):
    tops, parts = notepad_desktop()
    uia_mod, _ = install_fakes(monkeypatch, tops, foreground=77)
    monkeypatch.setattr(uia_mod, "process_name", lambda pid: "notepad" if pid == 1234 else "")
    b = uia_mod.UiaBackend(allow_pixels=True)
    roots = b.tree()
    assert [(r.role, r.name) for r in roots] == [("application", "notepad")]
    win = roots[0].children[0]
    assert win.role == "frame" and "active" in win.states and win.rect == Rect(x=0, y=0, w=800, h=600)
    roles = {n.name: n.role for n in win.children}
    assert roles == {"Save": "push button", "Legacy": "push button", "Body": "text", "Canvas": "custom"}
    assert b.find(Target(role="text", name="Body")).text == "hello"
    assert b.focus_existing("notepad.exe") == "Untitled - Notepad" and parts["win"].activated == 1
    assert b.focus_existing("calc") is None


def test_click_uses_invoke_pattern_first(monkeypatch):
    tops, parts = notepad_desktop()
    uia_mod, pg = install_fakes(monkeypatch, tops)
    b = uia_mod.UiaBackend(allow_pixels=True)
    b.click(b.find(Target(name="Save")))
    assert parts["save"]._patterns["GetInvokePattern"].calls == [("Invoke",)]
    assert pg.calls == [], "no pixel action when a pattern exists"


def test_click_falls_back_to_pixels_when_no_pattern(monkeypatch):
    tops, _ = notepad_desktop()
    uia_mod, pg = install_fakes(monkeypatch, tops)
    b = uia_mod.UiaBackend(allow_pixels=True)
    b.click(b.find(Target(name="Legacy")))
    assert pg.calls == [("click", (230, 310), {})]
    assert pg.FAILSAFE is False


def test_pixel_fallback_refused_without_allow_pixels(monkeypatch):
    tops, _ = notepad_desktop()
    uia_mod, pg = install_fakes(monkeypatch, tops)
    b = uia_mod.UiaBackend(allow_pixels=False)
    with pytest.raises(BackendError, match="allow_pixels=False"):
        b.click(b.find(Target(name="Legacy")))
    with pytest.raises(BackendError):
        b.key("ctrl+s")
    assert pg.calls == []


def test_invoke_and_set_value_fall_back_instead_of_raising(monkeypatch):
    tops, parts = notepad_desktop()
    uia_mod, pg = install_fakes(monkeypatch, tops)
    b = uia_mod.UiaBackend(allow_pixels=True)
    b.invoke(b.find(Target(name="Legacy")), "click")  # no Invoke pattern -> pixel click
    assert pg.calls[-1] == ("click", (230, 310), {})
    b.set_value(b.find(Target(name="Body")), "world")  # Value pattern
    assert parts["field"]._patterns["GetValuePattern"].calls == [("SetValue", "world")]
    pg.calls.clear()
    b.set_value(b.find(Target(name="Canvas")), "abc")  # no pattern -> click, ctrl+a, type
    assert [c[0] for c in pg.calls] == ["click", "hotkey", "write"]
    assert pg.calls[1][1] == ("ctrl", "a") and pg.calls[2][1] == ("abc",)
    with pytest.raises(BackendError, match="unknown action"):
        b.invoke(b.find(Target(name="Save")), "teleport")


def test_type_text_appends_via_value_pattern_else_types(monkeypatch):
    tops, parts = notepad_desktop()
    uia_mod, pg = install_fakes(monkeypatch, tops)
    b = uia_mod.UiaBackend(allow_pixels=True)
    b.type_text(b.find(Target(name="Body")), " there")
    assert parts["field"]._patterns["GetValuePattern"].calls == [("SetValue", "hello there")]
    b.type_text(b.find(Target(name="Canvas")), "x")
    assert parts["raw"].focused == 1 and pg.calls[-1] == ("write", ("x",), {"interval": uia_mod.TYPE_INTERVAL_S})
    b.type_text(None, "y")
    assert pg.calls[-1][1] == ("y",)


def test_key_chords_and_pixel_helpers(monkeypatch):
    tops, _ = notepad_desktop()
    uia_mod, pg = install_fakes(monkeypatch, tops)
    b = uia_mod.UiaBackend(allow_pixels=True)
    b.key("Ctrl+Shift+S")
    b.key("Return")
    b.pixel_double_click(5, 6)
    b.pixel_right_click(7, 8)
    b.pixel_drag(1, 2, 3, 4)
    b.pixel_scroll(9, 9, -3)
    names = [c[0] for c in pg.calls]
    assert names == ["hotkey", "press", "doubleClick", "rightClick", "moveTo", "dragTo", "scroll"]
    assert pg.calls[0][1] == ("ctrl", "shift", "s") and pg.calls[1][1] == ("enter",)
    assert pg.calls[6] == ("scroll", (-3,), {"x": 9, "y": 9})


def test_multi_monitor_origin_translates_rects_and_clicks(monkeypatch):
    tops, _ = notepad_desktop()
    uia_mod, pg = install_fakes(monkeypatch, tops)
    monkeypatch.setattr(uia_mod, "virtual_screen", lambda: (-1920, -200, 3840, 1280))
    b = uia_mod.UiaBackend(allow_pixels=True)
    legacy = b.find(Target(name="Legacy"))
    assert legacy.rect == Rect(x=2120, y=500, w=60, h=20), "UIA rect shifted into screenshot space"
    b.click(legacy)
    assert pg.calls == [("click", (230, 310), {})], "the pointer goes back to real screen coordinates"


def test_screenshot_uses_mss_then_pil(monkeypatch):
    tops, _ = notepad_desktop()
    uia_mod, _ = install_fakes(monkeypatch, tops)

    class Shot:
        rgb = b"\x00" * 12
        size = (2, 2)

    class Sct:
        monitors = [{"left": -10, "top": 0, "width": 2, "height": 2}]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def grab(self, mon):
            return Shot()

    fake_mss = types.ModuleType("mss")
    fake_mss.mss = Sct
    tools = types.ModuleType("mss.tools")
    tools.to_png = lambda rgb, size: b"PNG" + bytes(size)
    fake_mss.tools = tools
    monkeypatch.setitem(sys.modules, "mss", fake_mss)
    monkeypatch.setitem(sys.modules, "mss.tools", tools)
    b = uia_mod.UiaBackend(allow_pixels=False)  # screenshots never need pixels
    assert b.screenshot() == b"PNG\x02\x02" and b._origin == (-10, 0)

    # mss missing -> PIL.ImageGrab
    monkeypatch.setitem(sys.modules, "mss", None)
    monkeypatch.setitem(sys.modules, "mss.tools", None)
    import PIL

    class Img:
        def save(self, buf, format):
            buf.write(b"PILPNG")

    grab_mod = types.ModuleType("PIL.ImageGrab")
    grab_mod.grab = lambda all_screens=False: Img()
    monkeypatch.setitem(sys.modules, "PIL.ImageGrab", grab_mod)
    monkeypatch.setattr(PIL, "ImageGrab", grab_mod, raising=False)
    assert b.screenshot() == b"PILPNG"


def test_missing_pyautogui_is_a_backend_error(monkeypatch):
    tops, _ = notepad_desktop()
    uia_mod, _ = install_fakes(monkeypatch, tops, pyautogui=False)
    b = uia_mod.UiaBackend(allow_pixels=True)
    with pytest.raises(BackendError, match="pyautogui not importable"):
        b.key("enter")


def test_launch_app_uses_startfile_for_one_token(monkeypatch):
    tops, _ = notepad_desktop()
    uia_mod, _ = install_fakes(monkeypatch, tops)
    started = []
    monkeypatch.setattr(uia_mod.os, "startfile", lambda target: started.append(target), raising=False)
    b = uia_mod.UiaBackend(allow_pixels=True)
    assert "ShellExecute" in b.launch_app("ms-settings:display") and started == ["ms-settings:display"]

    class Proc:
        pid = 42

    popen = []
    monkeypatch.setattr(uia_mod.subprocess, "Popen", lambda args, **kw: popen.append(args) or Proc())
    assert b.launch_app('notepad "C:\\notes.txt"') == "launched pid 42: notepad C:\\notes.txt"
    assert popen == [["notepad", "C:\\notes.txt"]]
    with pytest.raises(BackendError):
        b.launch_app("")


def test_get_backend_on_win32_returns_uia(monkeypatch):
    tops, _ = notepad_desktop()
    install_fakes(monkeypatch, tops)
    monkeypatch.setattr(sys, "platform", "win32")
    from o_engine.cu.backends.base import get_backend

    backend = get_backend(display=None, allow_pixels=True)
    assert backend.name == "uia" and backend.available and backend.allow_pixels is True and backend.dpi_mode == "n/a"
