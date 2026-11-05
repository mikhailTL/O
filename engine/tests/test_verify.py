import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from o_engine import verify
from o_engine.schema import VerifyTarget


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if self.path == "/" else 404
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def local_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def no_playwright(monkeypatch):
    def broken(url, timeout_ms):
        raise RuntimeError("chromium executable missing")

    monkeypatch.setattr(verify, "_status_via_playwright", broken)


def test_httpx_fallback_ok(local_server, no_playwright):
    out = verify.verify_target(VerifyTarget(port=local_server, timeout_seconds=5))
    assert out["ok"] is True
    assert out["status"] == 200
    assert out["via"] == "httpx"
    assert "chromium" in (out["error"] or "")
    assert isinstance(out["elapsed_ms"], int) and out["elapsed_ms"] >= 0


def test_httpx_fallback_status_mismatch(local_server, no_playwright):
    out = verify.verify_target(VerifyTarget(port=local_server, path="/missing", timeout_seconds=5))
    assert out["ok"] is False
    assert out["status"] == 404
    assert "404" in out["error"]


def test_httpx_fallback_expected_status(local_server, no_playwright):
    out = verify.verify_target(VerifyTarget(port=local_server, path="/missing", expect_status=404, timeout_seconds=5))
    assert out["ok"] is True


def test_connection_refused(no_playwright):
    out = verify.verify_target(VerifyTarget(port=1, timeout_seconds=1))
    assert out["ok"] is False
    assert out["status"] is None
    assert out["error"]


def test_playwright_path_used_when_available(monkeypatch, local_server):
    calls = []

    def fake(url, timeout_ms):
        calls.append((url, timeout_ms))
        return 200

    monkeypatch.setattr(verify, "_status_via_playwright", fake)
    out = verify.verify_target(VerifyTarget(port=local_server, timeout_seconds=3))
    assert out == {"ok": True, "status": 200, "elapsed_ms": out["elapsed_ms"], "error": None, "via": "playwright"}
    assert calls == [(f"http://127.0.0.1:{local_server}/", 3000)]


def test_playwright_launch_is_headless_only():
    import inspect

    src = inspect.getsource(verify._status_via_playwright)
    assert "headless=True" in src
