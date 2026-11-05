"""Error text -> ProvisionRequest via a local vLLM model, with Claude Code CLI as fallback.

Both backends receive the same instruction, stream or return free text, and
that text is run through ``TokenFilter`` so only the JSON array reaches
``parse_steps``. ``Parser`` orders them: local first, fallback once, then
``SchemaViolation``.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Protocol

import httpx
from openai import OpenAI

from .schema import ProvisionRequest, SchemaViolation, parse_steps
from .tokenfilter import TokenFilter

SYSTEM_PROMPT = (
    "You convert a failing command's output into shell steps that fix it.\n"
    "Respond with ONLY a JSON array. No prose, no markdown, no code fences, no keys other than those listed.\n"
    "Each element is an object with exactly three keys:\n"
    '  "target_directory": string, directory the command runs in (relative to the sandbox root, "." for root)\n'
    '  "command_string": string, one shell command line\n'
    '  "timeout_seconds": integer, 1..3600\n'
    'Example: [{"target_directory": ".", "command_string": "npm install", "timeout_seconds": 300}]\n'
    "Never ask questions. Never explain. Output the array and nothing else."
)

CLAUDE_TIMEOUT_SECONDS = 120
LOCAL_TIMEOUT_SECONDS = 8.0


def build_user_prompt(text: str, goal: str) -> str:
    return f"Goal: {goal}\n\nOutput to fix:\n{text}\n\nJSON array of steps:"


class StepParserBackend(Protocol):
    name: str

    def parse(self, text: str, goal: str) -> ProvisionRequest: ...


class LocalModelParser:
    """Streams chat.completions from a vLLM OpenAI-compatible server."""

    name = "local"

    def __init__(self, base_url: str, model: str, timeout: float = LOCAL_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.client = OpenAI(base_url=self.base_url, api_key="local", timeout=timeout, max_retries=0)

    def available(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/models", timeout=2)
        except Exception:
            return False
        return 200 <= resp.status_code < 300

    def _tokens(self, text: str, goal: str):
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(text, goal)},
            ],
            stream=True,
            temperature=0,
        )
        deadline = time.monotonic() + self.timeout
        try:
            for chunk in stream:
                if time.monotonic() > deadline:
                    raise SchemaViolation(f"local model timed out after {self.timeout:g} s")
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    yield content
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def parse(self, text: str, goal: str) -> ProvisionRequest:
        tf = TokenFilter()
        filtered = tf.filter_stream(self._tokens(text, goal))
        if not filtered:
            raise SchemaViolation("local model produced no JSON array")
        return parse_steps(filtered, goal)


class ClaudeCodeParser:
    """Runs `claude -p --output-format json` with the prompt on stdin."""

    name = "fallback"

    def __init__(self, claude_path: str = "claude", timeout: float = CLAUDE_TIMEOUT_SECONDS) -> None:
        self.claude_path = claude_path
        self.timeout = timeout

    def _command(self) -> list[str]:
        return [self.claude_path, "-p", "--output-format", "json"]

    def parse(self, text: str, goal: str) -> ProvisionRequest:
        prompt = SYSTEM_PROMPT + "\n\n" + build_user_prompt(text, goal)
        try:
            proc = subprocess.run(
                self._command(),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
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
        tf = TokenFilter()
        filtered = tf.feed(result)
        if not filtered:
            raise SchemaViolation("claude cli produced no JSON array")
        return parse_steps(filtered, goal)


class Parser:
    """Local first; on any failure try the fallback once; otherwise SchemaViolation."""

    def __init__(self, local: StepParserBackend | None, fallback: StepParserBackend | None) -> None:
        self.local = local
        self.fallback = fallback
        self._answered: str = "none"

    def describe(self) -> str:
        return self._answered

    def parse(self, text: str, goal: str) -> ProvisionRequest:
        self._answered = "none"
        errors: list[str] = []
        for backend in (self.local, self.fallback):
            if backend is None:
                continue
            try:
                req = backend.parse(text, goal)
            except Exception as e:
                errors.append(f"{backend.name}: {e}")
                continue
            self._answered = backend.name
            return req
        if not errors:
            errors.append("no parser backend configured")
        raise SchemaViolation("all parser backends failed (" + "; ".join(errors) + ")")
