"""Headless check that a local port answers.

Playwright's headless Chromium is used when it is installed; otherwise a plain
``httpx`` GET stands in and the result says so. Nothing here can open a window.
"""
from __future__ import annotations

import time

import httpx

from .schema import VerifyTarget


def _status_via_playwright(url: str, timeout_ms: int) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            response = page.goto(url, timeout=timeout_ms, wait_until="commit")
            if response is None:
                raise RuntimeError("navigation returned no response")
            return int(response.status)
        finally:
            browser.close()


def _status_via_httpx(url: str, timeout_s: float) -> int:
    with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
        return int(client.get(url).status_code)


def verify_target(target: VerifyTarget) -> dict:
    url = f"http://127.0.0.1:{target.port}{target.path}"
    started = time.monotonic()
    status: int | None = None
    error: str | None = None
    via = "playwright"
    try:
        status = _status_via_playwright(url, target.timeout_seconds * 1000)
    except Exception as pw_err:
        via = "httpx"
        note = f"playwright unavailable ({type(pw_err).__name__}: {str(pw_err).splitlines()[0][:200]}); used httpx"
        try:
            status = _status_via_httpx(url, float(target.timeout_seconds))
            error = note
        except Exception as http_err:
            error = f"{note}; {type(http_err).__name__}: {http_err}"
    elapsed_ms = int((time.monotonic() - started) * 1000)
    ok = status is not None and status == target.expect_status
    if status is not None and not ok:
        mismatch = f"expected status {target.expect_status}, got {status}"
        error = f"{mismatch}; {error}" if error else mismatch
    return {"ok": ok, "status": status, "elapsed_ms": elapsed_ms, "error": error, "via": via}


def playwright_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
        import os

        return bool(path) and os.path.exists(path)
    except Exception:
        return False
