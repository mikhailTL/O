import json
import sys
import textwrap

import pytest

from o_engine.actionlog import ActionLog
from o_engine.mcp_router import MCPRouter, ToolCall, ToolResult, load_config, validate_args

SERVER_SRC = textwrap.dedent(
    '''
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x

    s = Server("fake")

    @s.tool()
    def echo(text: str) -> str:
        """Echo text back."""
        return text

    @s.tool()
    def add(a: int, b: int) -> int:
        """Add two ints."""
        return a + b

    s.run("stdio")
    '''
)


@pytest.fixture
def server_script(tmp_path):
    p = tmp_path / "fake_server.py"
    p.write_text(SERVER_SRC)
    return p


@pytest.fixture
def log(tmp_data_dir):
    return ActionLog(tmp_data_dir)


@pytest.fixture
def router(server_script, log):
    config = {"fake": {"command": sys.executable, "args": [str(server_script)], "env": {}}}
    return MCPRouter(config, log)


def _entries(log):
    return [json.loads(l) for l in log.jsonl.read_text().splitlines()]


def test_models_strict():
    c = ToolCall(server="s", tool="t", args={"a": 1})
    with pytest.raises(Exception):
        c.tool = "x"
    with pytest.raises(Exception):
        ToolCall(server="s", tool="t", args={}, extra=1)
    with pytest.raises(Exception):
        ToolCall(server="s", tool="t", args="not a dict")
    with pytest.raises(Exception):
        ToolCall(server="", tool="t", args={})
    r = ToolResult(ok=True, content="x", is_error=False, elapsed_ms=1)
    with pytest.raises(Exception):
        r.ok = False
    with pytest.raises(Exception):
        ToolResult(ok=True, content="x", is_error=False, elapsed_ms=-1)


def test_load_config(tmp_path):
    assert load_config(tmp_path / "missing.json") == {}
    p = tmp_path / "mcp-servers.json"
    p.write_text(json.dumps({"mcpServers": {"a": {"command": "x", "args": ["1"]}, "b": {"url": "http://localhost:1/sse"}}}))
    cfg = load_config(p)
    assert cfg == {"a": {"command": "x", "args": ["1"], "env": {}}, "b": {"url": "http://localhost:1/sse"}}
    p.write_text(json.dumps({"c": {"command": "y"}}))
    assert load_config(p) == {"c": {"command": "y", "args": [], "env": {}}}
    p.write_text("not json")
    assert load_config(p) == {}
    p.write_text(json.dumps({"bad": {"nothing": 1}, "ok": {"command": "z"}, "num": 5}))
    assert load_config(p) == {"ok": {"command": "z", "args": [], "env": {}}}


def test_sse_config_parsed_without_connecting(log):
    r = MCPRouter({"remote": {"url": "http://127.0.0.1:9/sse"}}, log)
    assert r.servers == {"remote": {"url": "http://127.0.0.1:9/sse"}}
    assert r.transport("remote") == "sse"
    assert r.transport("nope") is None


@pytest.mark.parametrize(
    "schema,args,ok",
    [
        ({"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}, {"a": 1}, True),
        ({"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}, {"a": "1"}, False),
        ({"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}, {}, False),
        ({"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}, {"a": True}, False),
        ({"type": "object", "properties": {"a": {"type": "number"}}}, {"a": 1}, True),
        ({"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}, {"a": "x", "b": 1}, False),
        ({"type": "object", "properties": {"a": {"type": "string"}}}, {"a": "x", "b": 1}, True),
        ({"type": "object", "properties": {"a": {"type": ["string", "null"]}}}, {"a": None}, True),
        ({"type": "object", "properties": {"a": {"type": "array", "items": {"type": "integer"}}}}, {"a": [1, 2]}, True),
        ({"type": "object", "properties": {"a": {"type": "array", "items": {"type": "integer"}}}}, {"a": [1, "2"]}, False),
        ({"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "boolean"}}, "required": ["b"]}}}, {"a": {"b": True}}, True),
        ({"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "boolean"}}, "required": ["b"]}}}, {"a": {}}, False),
        ({"type": "object", "properties": {"a": {"enum": ["x", "y"]}}}, {"a": "z"}, False),
        ({"type": "object", "properties": {"a": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}, {"a": None}, True),
        ({"type": "object", "properties": {"a": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}, {"a": "s"}, False),
        ({}, {"anything": 1}, True),
    ],
)
def test_validate_args(schema, args, ok):
    problems = validate_args(schema, args)
    assert (problems == []) is ok, problems


def test_list_tools_and_calls(router, log):
    tools = router.list_tools()
    names = sorted((t["server"], t["name"]) for t in tools)
    assert names == [("fake", "add"), ("fake", "echo")]
    echo = next(t for t in tools if t["name"] == "echo")
    assert echo["input_schema"]["properties"]["text"]["type"] == "string"
    assert "text" in echo["input_schema"]["required"]
    assert tools == router.list_tools()  # cached

    r = router.call(ToolCall(server="fake", tool="echo", args={"text": "hello"}))
    assert isinstance(r, ToolResult)
    assert r.ok is True and r.is_error is False
    assert r.content == "hello"
    assert r.elapsed_ms >= 0

    r2 = router.call(ToolCall(server="fake", tool="add", args={"a": 2, "b": 3}))
    assert r2.ok is True
    assert r2.content.strip() == "5"

    kinds = [e["kind"] for e in _entries(log)]
    assert kinds.count("tool") == 2
    assert kinds.count("result") == 2
    tool_entry = next(e for e in _entries(log) if e["kind"] == "tool")
    assert tool_entry["detail"]["server"] == "fake"
    assert tool_entry["detail"]["tool"] == "echo"


def test_schema_rejects_before_dispatch(router, log, monkeypatch):
    router.list_tools()
    dispatched = []
    monkeypatch.setattr(router, "_dispatch", lambda *a, **k: dispatched.append(a) or (True, "x"))
    r = router.call(ToolCall(server="fake", tool="add", args={"a": "two", "b": 3}))
    assert r.ok is False and r.is_error is True
    assert "a" in r.content and "integer" in r.content
    assert dispatched == []
    missing = router.call(ToolCall(server="fake", tool="add", args={"a": 1}))
    assert missing.is_error is True and "b" in missing.content
    assert dispatched == []
    kinds = [e["kind"] for e in _entries(log)]
    assert kinds.count("tool") == 2 and kinds.count("result") == 2


NESTED_SERVER_SRC = textwrap.dedent(
    '''
    import pathlib
    from pydantic import BaseModel

    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x

    s = Server("nested")

    class Item(BaseModel):
        name: str
        count: int

    @s.tool()
    def stash(item: Item) -> str:
        """Record an item (writes a marker file)."""
        pathlib.Path(%r).write_text(f"{item.name}:{item.count}")
        return "stashed"

    s.run("stdio")
    '''
)


def test_nested_ref_schema_rejects_before_dispatch(tmp_path, log):
    marker = tmp_path / "marker.txt"
    script = tmp_path / "nested_server.py"
    script.write_text(NESTED_SERVER_SRC % str(marker))
    router = MCPRouter({"nested": {"command": sys.executable, "args": [str(script)], "env": {}}}, log)
    tools = router.list_tools()
    stash = next(t for t in tools if t["name"] == "stash")
    schema = stash["input_schema"]
    # The tool's schema reaches the nested model through $ref/$defs; the subset checker cannot follow it.
    assert "$defs" in schema or "$ref" in json.dumps(schema)

    dispatched = []
    real_dispatch = router._dispatch
    router._dispatch = lambda *a: dispatched.append(a) or real_dispatch(*a)

    r = router.call(ToolCall(server="nested", tool="stash", args={"item": {"name": "x", "count": "three"}}))
    assert r.ok is False and r.is_error is True
    assert r.content.startswith("arguments rejected by tool schema")
    assert "count" in r.content
    assert dispatched == []
    assert not marker.exists()
    results = [e for e in _entries(log) if e["kind"] == "result"]
    assert results and results[-1]["detail"]["is_error"] is True

    good = router.call(ToolCall(server="nested", tool="stash", args={"item": {"name": "x", "count": 3}}))
    assert good.ok is True
    assert len(dispatched) == 1
    assert marker.read_text() == "x:3"


@pytest.mark.parametrize(
    "schema,args,ok",
    [
        ({"type": "object", "properties": {"a": {"$ref": "#/$defs/I"}}, "$defs": {"I": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}}, {"a": {"n": "1"}}, False),
        ({"type": "object", "properties": {"a": {"$ref": "#/$defs/I"}}, "$defs": {"I": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}}, {"a": {"n": 1}}, True),
        ({"type": "object", "properties": {"a": {"type": "string", "pattern": "^[a-z]+$"}}}, {"a": "ABC"}, False),
        ({"type": "object", "properties": {"a": {"type": "array", "uniqueItems": True}}}, {"a": [1, 1]}, False),
        ({"type": "object", "properties": {"a": {"not": {"type": "string"}}}}, {"a": "s"}, False),
        ({"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "if": {"properties": {"a": {"const": 1}}}, "then": {"required": ["b"]}}, {"a": 1}, False),
        ({"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "if": {"properties": {"a": {"const": 1}}}, "then": {"required": ["b"]}}, {"a": 2}, True),
    ],
)
def test_validate_args_full_draft2020(schema, args, ok):
    problems = validate_args(schema, args)
    assert (problems == []) is ok, problems


def test_validate_args_falls_back_without_jsonschema(monkeypatch):
    import o_engine.mcp_router as m

    monkeypatch.setattr(m, "_jsonschema_validator", lambda schema: None)
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    assert validate_args(schema, {"a": 1}) == []
    assert validate_args(schema, {"a": "1"}) != []
    assert validate_args({"type": "object", "properties": {"a": {"type": "string", "pattern": "^x$"}}}, {"a": "y"}) == []  # subset checker ignores pattern


def test_unknown_server_and_tool(router):
    r = router.call(ToolCall(server="nope", tool="echo", args={"text": "x"}))
    assert r.ok is False and r.is_error is True
    assert "nope" in r.content
    r2 = router.call(ToolCall(server="fake", tool="missing", args={}))
    assert r2.ok is False and r2.is_error is True
    assert "missing" in r2.content


def test_server_error_result_is_error(router):
    # A tool that raises inside the server comes back as is_error, not an exception here.
    router.list_tools()
    router._tools["fake"]["add"] = {"type": "object"}  # loosen the cached schema so the server sees bad args
    r = router.call(ToolCall(server="fake", tool="add", args={"a": "two", "b": 3}))
    assert r.ok is False and r.is_error is True
    assert r.content


def test_unreachable_server_lists_nothing_and_errors_on_call(log, tmp_path):
    router = MCPRouter({"dead": {"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"], "env": {}}}, log)
    assert router.list_tools() == []
    r = router.call(ToolCall(server="dead", tool="x", args={}))
    assert r.is_error is True
    assert "dead" in r.content


def test_empty_config(log):
    router = MCPRouter({}, log)
    assert router.list_tools() == []
    assert router.call(ToolCall(server="a", tool="b", args={})).is_error is True
