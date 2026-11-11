import builtins
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from o_engine.web import BrowserUseRunner, DomAction, PlaywrightDomRunner, WebResult, get_web_runner, launch_env

PAGE = b"""<!doctype html><html><head><title>t</title></head><body>
<h1 id="h">Hello from the stub</h1>
<form id="f" onsubmit="return false">
  <input id="name" name="name" type="text">
  <button id="go" type="button" onclick="document.getElementById('out').textContent='hi ' + document.getElementById('name').value">Go</button>
</form>
<p id="out"></p>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture(scope="module")
def site():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()


def _playwright_ready():
    from o_engine.verify import playwright_ready

    return playwright_ready()


needs_chromium = pytest.mark.skipif(not _playwright_ready(), reason="playwright chromium not installed")


def test_browser_use_absent_means_unavailable_and_run_reports(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "browser_use" or name.startswith("browser_use."):
            raise ModuleNotFoundError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("browser_use", None)
    r = BrowserUseRunner("http://127.0.0.1:8000/v1", "qwen")
    assert r.available is False
    out = r.run("do anything")
    assert isinstance(out, WebResult)
    assert out.ok is False and out.via == "browser-use" and "not installed" in (out.error or "")
    assert out.steps == [] and out.screenshots_sha == []


def test_get_web_runner_falls_back_to_dom_when_browser_use_missing(monkeypatch):
    monkeypatch.setattr(BrowserUseRunner, "available", property(lambda self: False))
    assert isinstance(get_web_runner("browser-use", "http://x", "m"), PlaywrightDomRunner)
    assert isinstance(get_web_runner("playwright"), PlaywrightDomRunner)


def test_get_web_runner_prefers_browser_use_when_available(monkeypatch):
    monkeypatch.setattr(BrowserUseRunner, "available", property(lambda self: True))
    r = get_web_runner("browser-use", "http://127.0.0.1:8000/v1", "m", display={"DISPLAY": ":99"})
    assert isinstance(r, BrowserUseRunner) and r.vllm_url == "http://127.0.0.1:8000/v1"


def test_dom_action_schema_is_strict():
    with pytest.raises(Exception):
        DomAction.model_validate({"op": "goto", "url": "http://x", "extra": 1})
    with pytest.raises(Exception):
        DomAction.model_validate({"op": "screenshot"})
    r = PlaywrightDomRunner().run([{"op": "nope"}])
    assert r.ok is False and r.via == "playwright-dom" and "ValidationError" in (r.error or "")


def test_launch_env_strips_host_display_without_touching_os_environ(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("PATH_MARKER", "kept")
    env = launch_env(None)
    assert "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env and env["PATH_MARKER"] == "kept"
    assert os.environ["DISPLAY"] == ":0" and os.environ["WAYLAND_DISPLAY"] == "wayland-0", "os.environ is never mutated"

    class D:
        def env(self):
            return {"DISPLAY": ":99", "SECRET": "x"}

    env = launch_env(D())
    assert env["DISPLAY"] == ":99" and "WAYLAND_DISPLAY" not in env and "SECRET" not in env
    assert os.environ["DISPLAY"] == ":0"
    assert launch_env({"DISPLAY": ":98"})["DISPLAY"] == ":98" and launch_env(":97")["DISPLAY"] == ":97"


def test_dom_runner_passes_launch_env_to_chromium(monkeypatch):
    """The browser gets ``env=`` at launch; nothing in the run path writes os.environ."""
    import types

    monkeypatch.setenv("DISPLAY", ":0")
    seen: dict = {}

    class _Page:
        def set_default_timeout(self, ms):
            pass

        def inner_text(self, sel):
            return "body text"

    class _Browser:
        def new_page(self):
            return _Page()

        def close(self):
            seen["closed"] = True

    class _Chromium:
        def launch(self, **kw):
            seen["launch"] = kw
            return _Browser()

    class _PW:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _PW()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    r = PlaywrightDomRunner(display={"DISPLAY": ":99"}).run([{"op": "read"}], timeout_s=5)
    assert r.ok is True and r.final_text == "body text"
    assert seen["launch"]["headless"] is True
    assert seen["launch"]["env"]["DISPLAY"] == ":99" and "WAYLAND_DISPLAY" not in seen["launch"]["env"]
    assert seen["closed"] is True
    assert os.environ["DISPLAY"] == ":0"


def test_browser_use_objects_are_created_and_closed_on_one_loop(monkeypatch):
    """Browser start, agent run and browser close all happen inside the single asyncio.run of a job."""
    import asyncio
    import types

    events: list = []

    class _History:
        def final_result(self):
            return "done"

        action_names = ["goto"]
        screenshots = []

    class _Browser:
        def __init__(self, **kw):
            events.append(("init", kw.get("env", {}).get("DISPLAY")))
            self.loop = None

        async def start(self):
            self.loop = asyncio.get_running_loop()
            events.append(("start", id(self.loop)))

        async def close(self):
            events.append(("close", id(asyncio.get_running_loop())))

    class _Agent:
        def __init__(self, task, llm, browser, **kw):
            events.append(("agent", task))

        async def run(self):
            events.append(("run", id(asyncio.get_running_loop())))
            return _History()

    mod = types.ModuleType("browser_use")
    mod.Agent, mod.Browser = _Agent, _Browser
    monkeypatch.setitem(sys.modules, "browser_use", mod)
    monkeypatch.setattr(BrowserUseRunner, "_make_llm", lambda self: object())
    monkeypatch.setattr(BrowserUseRunner, "_agent_kwargs", lambda self: {})
    r = BrowserUseRunner("http://127.0.0.1:1/v1", "m", display={"DISPLAY": ":99"}).run("task", url="http://x")
    assert r.ok is True and r.final_text == "done", r.error
    kinds = [e[0] for e in events]
    assert kinds == ["init", "start", "agent", "run", "close"]
    assert events[0][1] == ":99"
    loops = {e[1] for e in events if e[0] in ("start", "run", "close")}
    assert len(loops) == 1, "start, run and close used the same event loop"


@needs_chromium
def test_dom_runner_goto_and_read_returns_page_text(site):
    r = PlaywrightDomRunner().run([{"op": "goto", "url": site}, {"op": "read"}], timeout_s=30)
    assert r.ok is True, r.error
    assert r.via == "playwright-dom"
    assert "Hello from the stub" in r.final_text
    assert r.steps[0]["op"] == "goto" and r.steps[0]["status"] == 200
    assert r.steps[1]["op"] == "read" and "Hello from the stub" in r.steps[1]["text"]
    assert r.screenshots_sha == []


@needs_chromium
def test_dom_runner_fill_and_click_on_form(site):
    r = PlaywrightDomRunner().run([
        {"op": "goto", "url": site},
        {"op": "fill", "selector": "#name", "text": "mikhail"},
        {"op": "click", "selector": "#go"},
        {"op": "wait", "selector": "#out"},
        {"op": "read", "selector": "#out"},
    ], timeout_s=30)
    assert r.ok is True, r.error
    assert r.final_text == "hi mikhail"
    assert [s["op"] for s in r.steps] == ["goto", "fill", "click", "wait", "read"]
    assert all(s["ok"] for s in r.steps)


@needs_chromium
def test_dom_runner_reports_failed_step_without_raising(site):
    r = PlaywrightDomRunner().run([{"op": "goto", "url": site}, {"op": "click", "selector": "#does-not-exist"}], timeout_s=2)
    assert r.ok is False
    assert "step 1 (click) failed" in (r.error or "")
    assert r.steps[-1]["ok"] is False and r.steps[-1]["op"] == "click"
