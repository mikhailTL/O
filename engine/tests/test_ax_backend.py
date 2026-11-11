"""macOS AX backend: app-scoped tree walks, window filtering before conversion, the short per-app tree cache, handle
safety across concurrent reads, and the per-backend lock. Runs on any OS: atomacos, AppKit and
ApplicationServices are faked in sys.modules."""
from __future__ import annotations

import importlib
import sys
import threading
import types

import pytest

from o_engine.cu.backends.base import backend_find, backend_tree
from o_engine.cu.schema import Target


# ------------------------------------------------------------------ fakes


class El:
    """One fake AX element: attribute reads are counted so a test can prove a subtree was never walked."""

    reads: dict[str, int] = {}

    def __init__(self, role, title="", children=(), value=None, actions=("Press",)):
        self._attrs = {"AXRole": role, "AXTitle": title, "AXChildren": list(children), "AXValue": value,
                       "AXPosition": (1, 2), "AXSize": (30, 40), "AXEnabled": True}
        self._actions = list(actions)
        self.pressed = 0

    def __getattr__(self, name):
        if name.startswith("AX"):
            El.reads[self._attrs.get("AXTitle", "")] = El.reads.get(self._attrs.get("AXTitle", ""), 0) + 1
            if name in self._attrs:
                return self._attrs[name]
            raise AttributeError(name)
        raise AttributeError(name)

    def getActions(self):  # noqa: N802
        return list(self._actions)

    def Press(self):  # noqa: N802
        self.pressed += 1


class App:
    def __init__(self, pid, name, bundle, windows, policy=0):
        self.pid, self.name, self.bundle, self.policy = pid, name, bundle, policy
        self.el = El("AXApplication", name, children=[El("AXMenuBar", "menubar", children=[El("AXMenu", "File")]), *windows])

    def processIdentifier(self):  # noqa: N802
        return self.pid

    def localizedName(self):  # noqa: N802
        return self.name

    def bundleIdentifier(self):  # noqa: N802
        return self.bundle

    def activationPolicy(self):  # noqa: N802
        return self.policy

    bundleURL = executableURL = lambda self: None  # noqa: E731


class Desktop:
    def __init__(self, apps, front):
        self.apps = {a.pid: a for a in apps}
        self.front = front

    def runningApplications(self):  # noqa: N802
        return list(self.apps.values())

    def frontmostApplication(self):  # noqa: N802
        return self.apps[self.front]


def _button(name):
    return El("AXButton", name)


@pytest.fixture
def ax(monkeypatch):
    """A fake desktop: TextEdit (pid 11, two windows) frontmost, Safari (pid 22, one window), a background agent."""
    El.reads = {}
    textedit = App(11, "TextEdit", "com.apple.TextEdit", [
        El("AXWindow", "notes.txt", children=[_button("Save"), El("AXTextArea", "body", value="hello")]),
        El("AXWindow", "Untitled 2", children=[_button("Save"), _button("Close")]),
    ])
    safari = App(22, "Safari", "com.apple.Safari", [El("AXWindow", "Apple", children=[_button("Reload")])])
    agent = App(33, "Agent", "com.x.agent", [], policy=2)
    desk = Desktop([textedit, safari, agent], front=11)

    atom = types.ModuleType("atomacos")
    atom.getAppRefByPid = lambda pid: desk.apps[pid].el
    appkit = types.ModuleType("AppKit")
    appkit.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: desk)
    appserv = types.ModuleType("ApplicationServices")
    appserv.AXIsProcessTrusted = lambda: True
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "atomacos", atom)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "ApplicationServices", appserv)
    import o_engine.cu.backends.ax as mod

    mod = importlib.reload(mod)
    backend = mod.AxBackend()
    backend.desk = desk
    yield backend
    monkeypatch.undo()
    importlib.reload(mod)


# ------------------------------------------------------------------ scoping


def test_tree_defaults_to_the_frontmost_app_only(ax):
    roots = ax.tree()
    assert [r.name for r in roots] == ["TextEdit"]
    assert roots[0].id == "p11" and roots[0].role == "application"
    assert [c.role for c in roots[0].children] == ["menubar", "window", "window"]
    assert "Reload" not in El.reads, "Safari's subtree was never read"


def test_tree_scopes_to_the_named_app_by_name_bundle_or_pid(ax):
    for app in ("Safari", "safari", "com.apple.Safari", "22", 22):
        roots = ax.tree(app=app)
        assert [r.name for r in roots] == ["Safari"], app
    assert ax.tree(pid=22)[0].id == "p22"
    assert ax.tree(app="Nope") == []
    assert ax.resolve_pid("text") == 11, "a substring of the localized name resolves too"


def test_window_filter_is_applied_before_conversion(ax):
    roots = ax.tree(window="notes*")
    assert len(roots) == 1 and [w.name for w in roots[0].children] == ["notes.txt"]
    assert "Close" not in El.reads, "the non-matching window's children were never read"
    assert "File" not in El.reads, "with a window filter the menu bar is not walked"
    assert ax.tree(window="zzz*") == [], "no matching window: the app is not returned"
    # ids are positional in the app's real child list, so they are stable with or without a filter
    full = {w.name: w.id for w in ax.tree()[0].children}
    assert roots[0].children[0].id == full["notes.txt"]


def test_find_is_scoped_to_the_app(ax):
    assert ax.find(Target(role="button", name="Reload")) is None, "Reload lives in Safari, not the frontmost app"
    assert ax.find(Target(role="button", name="Reload"), app="Safari").name == "Reload"
    assert backend_find(ax, Target(role="button", name="Reload"), app="Safari").name == "Reload"
    assert backend_tree(ax, app="Safari")[0].name == "Safari"
    assert backend_tree(ax, "notes*", app="TextEdit")[0].children[0].name == "notes.txt"


def test_focus_existing_reads_only_that_app(ax):
    assert ax.focus_existing("Safari") == "Apple"
    assert ax.focus_existing("Nope") is None


def test_tree_all_lists_regular_apps_only(ax):
    assert [r.name for r in ax.tree_all()] == ["TextEdit", "Safari"], "activationPolicy != 0 is skipped"


# ------------------------------------------------------------------ cache


def test_tree_is_cached_per_app_for_a_short_time(ax):
    now = [100.0]
    ax._clock = lambda: now[0]
    ax.tree()
    ax.tree()
    ax.find(Target(name="Save"))
    assert ax.walks == 1, "before/find/after reads within one step reuse the walk"
    ax.tree(app="Safari")
    assert ax.walks == 2, "another app is another cache key"
    now[0] += ax.cache_ttl_s + 0.01
    ax.tree()
    assert ax.walks == 3, "the cache expires"
    ax.invalidate()
    ax.tree()
    assert ax.walks == 4


def test_cache_ttl_default_is_about_one_and_a_half_seconds():
    import o_engine.cu.backends.ax as mod

    assert 1.0 <= mod.TREE_CACHE_TTL_S <= 2.0


# ------------------------------------------------------------------ handles and the lock


def test_a_tree_read_of_another_app_keeps_this_apps_handles(ax):
    save = ax.find(Target(role="button", name="Save"))
    assert save is not None
    ax.invalidate()
    ax.tree(app="Safari")  # a concurrent /computer/tree for another app
    el = ax._handles.get(save.id)
    assert el is not None, "TextEdit's handles survive a Safari walk"
    ax.click(save)
    assert el.pressed == 1


def test_click_rewalks_the_owning_app_when_the_handle_is_gone(ax):
    save = ax.find(Target(role="button", name="Save"))
    ax._handles.clear()
    ax.invalidate()
    ax.click(save)
    assert ax._handles[save.id].pressed == 1


def test_public_calls_hold_one_reentrant_lock(ax):
    assert isinstance(ax._lock, type(threading.RLock()))
    seen = []

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            seen.append("enter")
            return self.inner.__enter__()

        def __exit__(self, *a):
            seen.append("exit")
            return self.inner.__exit__(*a)

    ax._lock = Spy(ax._lock)
    ax.tree()
    ax.find(Target(name="Save"))
    ax.click(ax.find(Target(name="Save")))
    assert seen.count("enter") >= 3 and seen.count("enter") == seen.count("exit")


def test_concurrent_tree_reads_and_clicks_do_not_lose_handles(ax):
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                ax.invalidate()
                ax.tree(app="Safari")
                ax.tree()
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

    def clicker():
        for _ in range(50):
            try:
                node = ax.find(Target(role="button", name="Close"))
                assert node is not None
                ax.click(node)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=clicker)
    t1.start()
    t2.start()
    t2.join(10)
    stop.set()
    t1.join(10)
    assert errors == []
    close = [w for w in ax.desk.apps[11].el.AXChildren if w.AXTitle == "Untitled 2"][0].AXChildren[1]
    assert close.pressed == 50
