"""MCP tool router.

Connects to user-configured MCP servers (stdio subprocesses or SSE endpoints) with
the ``mcp`` SDK, lists their tools with the tools' JSON input schemas, and dispatches
``ToolCall``s. Every call is validated against the cached schema *before* it is sent
(full JSON Schema 2020-12 through ``jsonschema``, which the ``mcp`` SDK depends on;
a small built-in subset checker when that import fails), and every call is logged as
kind ``tool`` then ``result``. Methods are
synchronous; asyncio runs in a private loop on a private thread. Nothing here raises
on a bad call: unknown server, unknown tool, invalid args, unreachable server and
server-side failures all come back as ``ToolResult(is_error=True)``.

Config shape (same as Claude Code's ``mcpServers``)::

    {"name": {"command": "...", "args": [...], "env": {...}} | {"url": "https://.../sse"}}
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from pydantic import BaseModel, ConfigDict, Field

CONNECT_TIMEOUT_SECONDS = 10.0
CALL_TIMEOUT_SECONDS = 60.0


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    server: str = Field(min_length=1, max_length=200)
    tool: str = Field(min_length=1, max_length=200)
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    ok: bool
    content: str
    is_error: bool
    elapsed_ms: int = Field(ge=0)


# ---------------------------------------------------------------------------
# config


def load_config(path: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Read ``mcp-servers.json``. Missing or malformed file -> {}. Accepts a bare map or {"mcpServers": {...}}."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("mcpServers"), dict):
        raw = raw["mcpServers"]
    return normalize_config(raw)


def normalize_config(raw: Any) -> dict[str, dict[str, Any]]:
    """Keep only well-formed entries; fill ``args``/``env`` defaults for stdio servers."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        if isinstance(entry.get("url"), str) and entry["url"]:
            out[name] = {"url": entry["url"]}
            if isinstance(entry.get("headers"), dict):
                out[name]["headers"] = {str(k): str(v) for k, v in entry["headers"].items()}
            continue
        if isinstance(entry.get("command"), str) and entry["command"]:
            args = entry.get("args") or []
            env = entry.get("env") or {}
            if not isinstance(args, list) or not isinstance(env, dict):
                continue
            out[name] = {"command": entry["command"], "args": [str(a) for a in args], "env": {str(k): str(v) for k, v in env.items()}}
    return out


# ---------------------------------------------------------------------------
# JSON-schema subset checker (no dependency)

_TYPE_NAMES = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def _is_type(value: Any, name: str) -> bool:
    kinds = _TYPE_NAMES.get(name)
    if kinds is None:
        return True  # unknown type keyword: do not block
    if isinstance(value, bool) and name in ("integer", "number"):
        return False
    if name == "number" and isinstance(value, float) and value != value:  # NaN
        return False
    return isinstance(value, kinds)


def _jsonschema_validator(schema: Any) -> Any:
    """A Draft 2020-12 validator for ``schema`` from the ``jsonschema`` package, or None.

    ``jsonschema`` is a hard dependency of the ``mcp`` SDK, so it is normally present;
    when it is missing or the schema itself is malformed, None sends ``validate_args``
    to the built-in subset checker.
    """
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except Exception:  # ImportError, or a broken install
        return None
    try:
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())
    except Exception:
        return None


def _error_path(error: Any, root: str) -> str:
    parts = [root]
    for part in getattr(error, "absolute_path", ()) or ():
        parts.append(f"[{part}]" if isinstance(part, int) else f".{part}")
    return "".join(parts)


def validate_args(schema: Any, value: Any, path: str = "args") -> list[str]:
    """Problems found checking ``value`` against ``schema``; [] means acceptable.

    Full Draft 2020-12 validation (``$ref``/``$defs``, pattern, format, uniqueItems,
    not, if/then, ...) through ``jsonschema`` when importable; the subset checker
    below otherwise.
    """
    if not isinstance(schema, dict):
        return []
    validator = _jsonschema_validator(schema)
    if validator is not None:
        errors = sorted(validator.iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path])
        return [f"{_error_path(e, path)}: {e.message}" for e in errors]
    return _validate_subset(schema, value, path)


def _validate_subset(schema: Any, value: Any, path: str = "args") -> list[str]:
    """Dependency-free checker: type / required / additionalProperties / enum / items / anyOf."""
    problems: list[str] = []
    if not isinstance(schema, dict):
        return problems
    typ = schema.get("type")
    if typ is not None:
        allowed = typ if isinstance(typ, list) else [typ]
        if not any(_is_type(value, str(t)) for t in allowed):
            problems.append(f"{path}: expected {' or '.join(map(str, allowed))}, got {type(value).__name__}")
            return problems
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} is not one of {schema['enum']!r}")
        return problems
    if "const" in schema and value != schema["const"]:
        problems.append(f"{path}: must equal {schema['const']!r}")
        return problems
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            if not any(_validate_subset(opt, value, path) == [] for opt in options):
                problems.append(f"{path}: matches none of the {key} alternatives")
                return problems
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for opt in all_of:
            problems.extend(_validate_subset(opt, value, path))
    if isinstance(value, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for name in required:
            if name not in value:
                problems.append(f"{path}.{name}: required")
        for name, sub in props.items():
            if name in value:
                problems.extend(_validate_subset(sub, value[name], f"{path}.{name}"))
        additional = schema.get("additionalProperties", True)
        extra = [k for k in value if k not in props]
        if additional is False and extra:
            problems.append(f"{path}: unexpected properties {sorted(extra)!r}")
        elif isinstance(additional, dict):
            for k in extra:
                problems.extend(_validate_subset(additional, value[k], f"{path}.{k}"))
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                problems.extend(_validate_subset(items, item, f"{path}[{i}]"))
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            problems.append(f"{path}: fewer than {min_items} items")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            problems.append(f"{path}: more than {max_items} items")
    if isinstance(value, str):
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(value) < min_len:
            problems.append(f"{path}: shorter than {min_len}")
        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and len(value) > max_len:
            problems.append(f"{path}: longer than {max_len}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if isinstance(lo, (int, float)) and value < lo:
            problems.append(f"{path}: below minimum {lo}")
        if isinstance(hi, (int, float)) and value > hi:
            problems.append(f"{path}: above maximum {hi}")
    return problems


# ---------------------------------------------------------------------------
# router


class _Loop:
    """A private asyncio loop on a daemon thread; ``run`` blocks the caller with a bound."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="o-mcp-router", daemon=True)
        self.thread.start()

    def run(self, coro: Any, timeout: float) -> Any:
        async def bounded() -> Any:
            return await asyncio.wait_for(coro, timeout)

        future = asyncio.run_coroutine_threadsafe(bounded(), self.loop)
        try:
            return future.result(timeout + 5)
        except TimeoutError:
            future.cancel()
            raise


class MCPRouter:
    def __init__(
        self,
        config: dict[str, dict[str, Any]] | None,
        log: Any,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        call_timeout: float = CALL_TIMEOUT_SECONDS,
    ) -> None:
        self.servers = normalize_config(config or {})
        self.log = log
        self.connect_timeout = float(connect_timeout)
        self.call_timeout = float(call_timeout)
        self._tools: dict[str, dict[str, dict[str, Any]]] = {}  # server -> tool -> input schema
        self._descriptions: dict[str, dict[str, str]] = {}
        self._failed: dict[str, str] = {}  # server -> reason the last connect failed
        self._loop: _Loop | None = None
        self._lock = threading.Lock()

    # -- public ----------------------------------------------------------------

    def transport(self, server: str) -> str | None:
        entry = self.servers.get(server)
        if not entry:
            return None
        return "sse" if "url" in entry else "stdio"

    def list_tools(self, refresh: bool = False) -> list[dict[str, Any]]:
        """All tools of all configured servers: [{server, name, description, input_schema}]."""
        out: list[dict[str, Any]] = []
        for server in self.servers:
            self._ensure_listed(server, refresh)
            for name, schema in self._tools.get(server, {}).items():
                out.append({
                    "server": server,
                    "name": name,
                    "description": self._descriptions.get(server, {}).get(name, ""),
                    "input_schema": schema,
                })
        return out

    def call(self, call: ToolCall) -> ToolResult:
        started = time.monotonic()
        self._record("tool", f"mcp call {call.server}/{call.tool}", server=call.server, tool=call.tool, args=call.args)
        is_error, content = self._call_inner(call)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        result = ToolResult(ok=not is_error, content=content, is_error=is_error, elapsed_ms=elapsed_ms)
        self._record("result", f"mcp result {call.server}/{call.tool} {'error' if is_error else 'ok'} {elapsed_ms}ms",
                     server=call.server, tool=call.tool, ok=result.ok, is_error=is_error, elapsed_ms=elapsed_ms,
                     content=content[:2000])
        return result

    # -- internals -------------------------------------------------------------

    def _call_inner(self, call: ToolCall) -> tuple[bool, str]:
        if call.server not in self.servers:
            return True, f"unknown MCP server {call.server!r}; configured: {sorted(self.servers)}"
        self._ensure_listed(call.server, False)
        tools = self._tools.get(call.server)
        if tools is None:
            return True, f"MCP server {call.server!r} unreachable: {self._failed.get(call.server, 'unknown error')}"
        if call.tool not in tools:
            return True, f"unknown tool {call.tool!r} on MCP server {call.server!r}; available: {sorted(tools)}"
        problems = validate_args(tools[call.tool], call.args)
        if problems:
            return True, "arguments rejected by tool schema: " + "; ".join(problems)
        try:
            return self._dispatch(call.server, call.tool, call.args)
        except Exception as e:  # noqa: BLE001 - never raise to the caller
            return True, f"MCP call failed: {e.__class__.__name__}: {e}"

    def _dispatch(self, server: str, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        """Send the (already validated) call. Returns (is_error, text content)."""
        return self._loop_runner().run(self._async_call(server, tool, args), self.connect_timeout + self.call_timeout)

    def _ensure_listed(self, server: str, refresh: bool) -> None:
        with self._lock:
            if server in self._tools and not refresh:
                return
            try:
                listed = self._loop_runner().run(self._async_list(server), self.connect_timeout)
            except Exception as e:  # noqa: BLE001
                reason = f"{e.__class__.__name__}: {e}" if str(e) else e.__class__.__name__
                self._failed[server] = reason
                self._tools.pop(server, None)
                self._record("note", f"mcp server {server!r} unreachable: {reason[:200]}", server=server, error=reason)
                return
            self._failed.pop(server, None)
            self._tools[server] = {name: schema for name, _desc, schema in listed}
            self._descriptions[server] = {name: desc for name, desc, _schema in listed}

    def _loop_runner(self) -> _Loop:
        if self._loop is None:
            self._loop = _Loop()
        return self._loop

    def _record(self, kind: str, summary: str, **detail: Any) -> None:
        try:
            self.log.record(kind, summary, **detail)
        except Exception:
            pass

    # -- async side (runs on the private loop) --------------------------------

    @asynccontextmanager
    async def _session(self, server: str) -> AsyncIterator[Any]:
        from mcp import ClientSession

        entry = self.servers[server]
        if "url" in entry:
            from mcp.client.sse import sse_client

            async with sse_client(entry["url"], headers=entry.get("headers"), timeout=self.connect_timeout) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            return
        from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

        params = StdioServerParameters(
            command=entry["command"],
            args=list(entry.get("args", [])),
            env={**get_default_environment(), **entry.get("env", {})},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def _async_list(self, server: str) -> list[tuple[str, str, dict[str, Any]]]:
        async with self._session(server) as session:
            result = await session.list_tools()
        out: list[tuple[str, str, dict[str, Any]]] = []
        for tool in result.tools:
            schema = getattr(tool, "input_schema", None)
            if schema is None:
                schema = getattr(tool, "inputSchema", None)
            if not isinstance(schema, dict):
                schema = {"type": "object"}
            out.append((tool.name, tool.description or "", schema))
        return out

    async def _async_call(self, server: str, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        async with self._session(server) as session:
            result = await session.call_tool(tool, args, read_timeout_seconds=self.call_timeout)
        is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
        content = _content_text(getattr(result, "content", None))
        if not content:
            structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
            if structured is not None:
                content = json.dumps(structured, sort_keys=True, default=str)
        return is_error, content


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    parts: list[str] = []
    for block in content if isinstance(content, (list, tuple)) else [content]:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        kind = getattr(block, "type", "") or (block.get("type") if isinstance(block, dict) else "")
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind:
            parts.append(f"[{kind} content omitted]")
        else:
            parts.append(str(block))
    return "\n".join(parts)


__all__ = ["ToolCall", "ToolResult", "MCPRouter", "load_config", "normalize_config", "validate_args"]
