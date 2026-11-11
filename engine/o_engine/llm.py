"""The one place a paid Claude call is made from the engine.

``claude_cli(prompt)`` answers a prompt with Claude and returns the ``claude -p --output-format json`` payload
shape (``{"result": str, "total_cost_usd": float, ...}``) so every caller keeps reading ``payload["result"]``
and charging ``meter.add_cost(payload)``.

Two transports, same behaviour:

* **O's API proxy** (``O_PROXY_URL`` / ``O_PROXY_TOKEN``, main/proxy.js) when it is configured: one HTTP call to
  its OpenAI-compatible ``/chat/completions``. The proxy keeps a warm ``claude`` process, so this skips the
  cold start of spawning the CLI per call. The proxy carries text only: a call that needs the model to read a
  file next to it (``cwd`` given: the judge's and the grounder's screenshot) always uses the CLI.
* **The CLI** (``<claude_path> -p --output-format json`` with the prompt on stdin), the fallback when no proxy is
  configured or the proxy call fails.

Every failure is a ``SchemaViolation`` with the messages the callers used to produce themselves
(``claude cli failed to run: ...``, ``claude cli exited N: ...``, ``claude cli output is not JSON: ...``,
``claude cli output has no string result``).
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from .schema import SchemaViolation

CLAUDE_TIMEOUT_SECONDS = 120
PROXY_MODEL = "o-ai-code"  # main/proxy.js: JSON-only answers (prose and fences stripped by its TokenFilter)
PROXY_CALLER = "engine"
PROXY_CONNECT_TIMEOUT_S = 2.0


def proxy_settings(proxy_url: str | None = None, proxy_token: str | None = None) -> tuple[str, str]:
    """``(url, token)`` of O's proxy: the explicit values, else ``O_PROXY_URL`` / ``O_PROXY_TOKEN``; ``("", "")``
    when the proxy is off."""
    url = proxy_url if proxy_url is not None else os.environ.get("O_PROXY_URL", "")
    token = proxy_token if proxy_token is not None else os.environ.get("O_PROXY_TOKEN", "")
    return (url or "").strip().rstrip("/"), (token or "").strip()


def cli_command(claude_path: str = "claude") -> list[str]:
    return [claude_path, "-p", "--output-format", "json"]


def claude_cli(
    prompt: str,
    *,
    claude_path: str = "claude",
    timeout: float = CLAUDE_TIMEOUT_SECONDS,
    cwd: str | None = None,
    system: str | None = None,
    proxy_url: str | None = None,
    proxy_token: str | None = None,
    allow_proxy: bool = True,
) -> dict[str, Any]:
    """Answer ``prompt`` (optionally with a separate ``system`` prompt) and return the CLI-shaped payload.

    ``cwd`` pins the call to the CLI (the model must read a file there). ``allow_proxy=False`` does the same.
    When the proxy is configured and fails, the CLI runs as the fallback; its error is the one raised."""
    url, token = proxy_settings(proxy_url, proxy_token)
    if allow_proxy and cwd is None and url:
        try:
            return _via_proxy(prompt, system, url, token, timeout)
        except SchemaViolation:
            pass  # the CLI is the fallback; a proxy that is down must not fail the call
    return _via_cli(prompt, system, claude_path, timeout, cwd)


def _via_cli(prompt: str, system: str | None, claude_path: str, timeout: float, cwd: str | None) -> dict[str, Any]:
    text = f"{system}\n\n{prompt}" if system else prompt
    kwargs: dict[str, Any] = dict(input=text, capture_output=True, text=True, timeout=timeout)
    if cwd is not None:
        kwargs["cwd"] = cwd
    try:
        proc = subprocess.run(cli_command(claude_path), **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
        raise SchemaViolation(f"claude cli failed to run: {e}") from None
    if proc.returncode != 0:
        raise SchemaViolation(f"claude cli exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise SchemaViolation(f"claude cli output is not JSON: {e.msg}") from None
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, str):
        raise SchemaViolation("claude cli output has no string result")
    payload.setdefault("via", "cli")
    return payload


def _via_proxy(prompt: str, system: str | None, url: str, token: str, timeout: float) -> dict[str, Any]:
    import httpx

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    headers = {"x-o-caller": PROXY_CALLER}
    if token:
        headers["authorization"] = f"Bearer {token}"
    try:
        r = httpx.post(url + "/chat/completions", json={"model": PROXY_MODEL, "messages": messages, "stream": False},
                       headers=headers, timeout=httpx.Timeout(timeout, connect=PROXY_CONNECT_TIMEOUT_S))
    except Exception as e:  # noqa: BLE001 - network errors of every kind
        raise SchemaViolation(f"o proxy failed: {type(e).__name__}: {e}") from None
    if r.status_code != 200:
        raise SchemaViolation(f"o proxy answered {r.status_code}: {r.text[:200]}")
    try:
        body = r.json()
    except ValueError as e:
        raise SchemaViolation(f"o proxy output is not JSON: {e}") from None
    content = None
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str):
        raise SchemaViolation("o proxy output has no string result")
    try:
        cost = float(r.headers.get("x-o-cost-usd", "0") or 0)
    except ValueError:
        cost = 0.0
    return {"result": content, "total_cost_usd": max(0.0, cost), "via": "proxy", "usage": body.get("usage")}
