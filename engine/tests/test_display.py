import os
import shutil
import sys

import pytest

from o_engine import display as display_mod
from o_engine.display import IsolatedDisplay


def test_defaults():
    d = IsolatedDisplay()
    assert d.display == ":99"
    assert d.size == "1280x800x24"
    assert d.lock_path.name == ".X99-lock"


def test_unavailable_when_xvfb_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    d = IsolatedDisplay()
    assert d.available is False
    assert d.env() == {}
    d.start()
    assert d.running is False
    d.stop()


def test_unavailable_off_linux(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/Xvfb")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert IsolatedDisplay().available is False


def test_available_on_linux_with_xvfb(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/Xvfb")
    monkeypatch.setattr(sys, "platform", "linux")
    assert IsolatedDisplay().available is True


def test_start_spawns_once_and_env(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/Xvfb")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    calls = []

    class FakeProc:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    def fake_popen(args, **kw):
        calls.append((list(args), kw))
        return FakeProc()

    monkeypatch.setattr(display_mod.subprocess, "Popen", fake_popen)
    d = IsolatedDisplay(display=":99", size="800x600x24", lock_dir=tmp_path)
    d.start()
    d.start()
    assert len(calls) == 1
    args, kw = calls[0]
    assert args[:2] == ["Xvfb", ":99"]
    assert "-screen" in args and "800x600x24" in args
    assert "-nolisten" in args and "tcp" in args
    assert kw.get("stdin") == display_mod.subprocess.DEVNULL
    assert "DISPLAY" not in kw.get("env", {}) or kw["env"].get("DISPLAY") is None
    assert d.running is True
    assert d.env() == {"DISPLAY": ":99"}
    assert os.environ["DISPLAY"] == ":0"
    d.stop()
    assert d.running is False
    assert os.environ["DISPLAY"] == ":0"


def test_existing_lock_means_already_running(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/Xvfb")
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / ".X99-lock").write_text("12345\n")
    calls = []
    monkeypatch.setattr(display_mod.subprocess, "Popen", lambda *a, **k: calls.append(a))
    d = IsolatedDisplay(lock_dir=tmp_path)
    d.start()
    assert calls == []
    assert d.running is True
    assert d.env() == {"DISPLAY": ":99"}
    d.stop()


def test_env_empty_when_not_running(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/Xvfb")
    monkeypatch.setattr(sys, "platform", "linux")
    assert IsolatedDisplay(lock_dir="/nonexistent-o-lock-dir").env() == {}


def test_isolated_display_refuses_the_host_display(monkeypatch, tmp_path):
    from o_engine import display as display_mod
    monkeypatch.setenv("DISPLAY", ":99")
    d = display_mod.IsolatedDisplay(":99")
    monkeypatch.setattr(d, "lock_path", tmp_path / ".X99-lock", raising=False)
    (tmp_path / ".X99-lock").write_text("1")
    assert d.is_host is True
    d.start()
    assert d.running is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert display_mod.IsolatedDisplay(":99").is_host is False
