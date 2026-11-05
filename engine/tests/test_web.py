import builtins
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from o_engine.web import BrowserUseRunner, DomAction, PlaywrightDomRunner, WebResult, _scoped_display, get_web_runner

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


def test_scoped_display_strips_host_display_and_restores(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    with _scoped_display(None) as env:
        assert env == {} and "DISPLAY" not in os.environ and "WAYLAND_DISPLAY" not in os.environ
    assert os.environ["DISPLAY"] == ":0" and os.environ["WAYLAND_DISPLAY"] == "wayland-0"

    class D:
        def env(self):
            return {"DISPLAY": ":99", "SECRET": "x"}

    with _scoped_display(D()) as env:
        assert env == {"DISPLAY": ":99"} and os.environ["DISPLAY"] == ":99" and "WAYLAND_DISPLAY" not in os.environ
    assert os.environ["DISPLAY"] == ":0"


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
