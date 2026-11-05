"""Phase 2 routes: /tools/list, /tools/call, /script/run, /swarm/run, /web/run, /health keys,
and the ``swarm: true`` rule of /provision."""
from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from o_engine.executor import Sandbox
from o_engine.mcp_router import MCPRouter, ToolCall
from o_engine.runners import NativeRunner
from o_engine.schema import SchemaViolation, parse_steps
from o_engine.server import EngineSettings, create_app
from o_engine.swarm import RoleRun, SwarmResult
from o_engine.web import PlaywrightDomRunner, WebResult

TOKEN = "phase2-token"
PAGE = b"<!doctype html><html><head><title>p2</title></head><body><h1 id='h'>phase two page</h1></body></html>"


# ----------------------------------------------------------------------------- stubs


class StubRouter(MCPRouter):
    """A real MCPRouter with one pre-listed stdio server; ``_dispatch`` records instead of connecting."""

    def __init__(self, log):
        super().__init__({"stub": {"command": "true"}}, log)
        self._tools["stub"] = {
            "echo": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "n": {"type": "integer"}},
                "required": ["text"],
                "additionalProperties": False,
            }
        }
        self._descriptions["stub"] = {"echo": "echo text back"}
        self.dispatched: list[tuple[str, str, dict]] = []

    def _dispatch(self, server, tool, args):
        self.dispatched.append((server, tool, dict(args)))
        return False, f"echo:{args.get('text')}"


class StubParser:
    def __init__(self, fail=False):
        self.fail = fail

    def describe(self):
        return "stub"

    def parse(self, text, goal=""):
        if self.fail:
            raise SchemaViolation("stub parser refuses")
        return parse_steps('[{"target_directory": ".", "command_string": "echo compiled", "timeout_seconds": 30}]',
                           label=goal or "parsed")


class StubProfile:
    def model_dump(self):
        return {"cwd": "/tmp/x", "shell_history": ["ls"], "tree": {}, "config_files": [], "telemetry": []}


class StubContext:
    def profile(self, cwd, telemetry_seconds=120):
        return StubProfile()


class StubCrew:
    """Returns a SwarmResult with three role runs; ``request`` is None when the parser fails."""

    name = "stub"

    def __init__(self, parser):
        self.parser = parser
        self.calls: list[tuple[str, str]] = []

    def run(self, objective, cwd):
        self.calls.append((objective, cwd))
        try:
            request = self.parser.parse(objective, goal=objective)
            ok = True
        except SchemaViolation:
            request, ok = None, False
        roles = [
            RoleRun(role="telemetry_auditor", elapsed_ms=1, ok=True, note="profile"),
            RoleRun(role="subprocess_compiler", elapsed_ms=2, ok=ok, note="plan" if ok else "schema violation"),
            RoleRun(role="environment_inspector", elapsed_ms=3, ok=True, note="nothing to inspect"),
        ]
        return SwarmResult(profile=StubProfile().model_dump(), request=request, inspection={}, roles=roles)


def _ok_run_job(graph, request, job_id, mode, approve_waiter):
    return {
        "id": job_id, "label": request.label, "phase": "report", "attempt": 1, "max_attempts": 3, "cached": False,
        "steps": [{"index": i, "command": s.command_string, "cwd": s.target_directory, "exit_code": 0,
                   "stdout_tail": "", "stderr_tail": "", "elapsed_ms": 1, "timed_out": False}
                  for i, s in enumerate(request.steps)],
        "done": True, "ok": True,
    }


class StubDeps:
    def __init__(self, settings, log):
        self.settings = settings
        self.parser = StubParser()
        self.context = StubContext()
        self.graph = object()
        self.run_job = _ok_run_job
        self.run_jobs: list = []
        self.mcp_router = StubRouter(log)
        self.runner = NativeRunner()
        self.crew = StubCrew(self.parser)
        self.display = None
        self.web_runs: list = []

    def web_runner(self, display=None, scripted=False):
        # browser_use absent: the DOM runner is what get_web_runner would return either way
        return PlaywrightDomRunner(display=None)


# ----------------------------------------------------------------------------- fixtures


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()


def _playwright_ready():
    from o_engine.verify import playwright_ready

    return playwright_ready()


needs_chromium = pytest.mark.skipif(not _playwright_ready(), reason="playwright chromium not installed")


@pytest.fixture
def settings(tmp_path):
    return EngineSettings(
        token=TOKEN,
        data_dir=str(tmp_path / "data"),
        sandbox_root=str(tmp_path / "sandbox"),
        screenpipe_db=str(tmp_path / "missing.db"),
        vllm_url="http://127.0.0.1:1/v1",
    )


@pytest.fixture
def client(settings):
    from o_engine.actionlog import get_log

    deps = StubDeps(settings, get_log(settings.data_dir, None, settings.token))
    holder = {"deps": deps}
    app = create_app(settings, deps_factory=lambda s: deps)
    with TestClient(app) as c:
        c.deps_holder = holder
        yield c


def _h():
    return {"x-o-token": TOKEN}


def _wait_done(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/jobs/{job_id}", headers=_h()).json()
        if body["done"]:
            return body
        time.sleep(0.02)
    raise AssertionError("job never finished")


# ----------------------------------------------------------------------------- settings


def test_phase2_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("O_ENGINE_TOKEN", "abc")
    monkeypatch.setenv("O_MCP_SERVERS_FILE", str(tmp_path / "servers.json"))
    monkeypatch.setenv("O_HIGH_RISK_SANDBOX", "e2b")
    monkeypatch.setenv("E2B_API_KEY", "k")
    monkeypatch.setenv("O_DOCKER_IMAGE", "alpine")
    monkeypatch.setenv("O_DOCKER_NETWORK", "none")
    monkeypatch.setenv("O_SWARM", "false")
    monkeypatch.setenv("O_WEB_PREFER", "playwright-dom")
    s = EngineSettings.from_env(data_dir=str(tmp_path))
    assert s.mcp_servers_path == tmp_path / "servers.json"
    assert (s.high_risk_sandbox, s.e2b_api_key, s.docker_image, s.docker_network, s.swarm, s.web_prefer) == (
        "e2b", "k", "alpine", "none", False, "playwright-dom")
    like = s.settings_like
    assert like["dockerImage"] == "alpine" and like["e2bApiKey"] == "k" and like["swarmEnabled"] is False
    assert like["vllmUrl"] == s.vllm_url and like["vllmModel"] == s.vllm_model and like["dockerNetwork"] == "none"


def test_phase2_settings_defaults(tmp_path):
    s = EngineSettings(token="t", data_dir=str(tmp_path))
    assert s.mcp_servers_path == tmp_path / "mcp-servers.json"
    assert s.high_risk_sandbox == "docker" and s.docker_network == "bridge" and s.swarm is True
    assert s.web_prefer == "browser-use"


# ----------------------------------------------------------------------------- auth


@pytest.mark.parametrize("method,path,body", [
    ("get", "/tools/list", None),
    ("post", "/tools/call", {"server": "stub", "tool": "echo", "args": {"text": "x"}}),
    ("post", "/script/run", {"language": "python", "code": "print(1)"}),
    ("post", "/swarm/run", {"objective": "x"}),
    ("post", "/web/run", {"task": "x", "url": "http://127.0.0.1:1/"}),
])
def test_phase2_routes_need_token(client, method, path, body):
    r = getattr(client, method)(path, json=body) if body is not None else client.get(path)
    assert r.status_code == 401
    r = client.post(path, json=body, headers={"x-o-token": "wrong"}) if body is not None else client.get(path, headers={"x-o-token": "wrong"})
    assert r.status_code == 401


# ----------------------------------------------------------------------------- /tools


def test_tools_list_returns_stub_tools(client):
    r = client.get("/tools/list", headers=_h())
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert len(tools) == 1
    t = tools[0]
    assert set(t) == {"server", "name", "description", "input_schema"}
    assert (t["server"], t["name"]) == ("stub", "echo")
    assert t["input_schema"]["required"] == ["text"]


def test_tools_call_dispatches_valid_args(client):
    r = client.post("/tools/call", json={"server": "stub", "tool": "echo", "args": {"text": "hi", "n": 2}}, headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "content", "is_error", "elapsed_ms"}
    assert body["ok"] is True and body["is_error"] is False and body["content"] == "echo:hi"
    assert client.deps_holder["deps"].mcp_router.dispatched == [("stub", "echo", {"text": "hi", "n": 2})]


def test_tools_call_wrong_typed_arg_is_error_and_never_dispatches(client):
    r = client.post("/tools/call", json={"server": "stub", "tool": "echo", "args": {"text": 42}}, headers=_h())
    assert r.status_code == 200
    router = client.deps_holder["deps"].mcp_router
    body = r.json()
    assert body["is_error"] is True and body["ok"] is False
    assert "args.text" in body["content"] and "string" in body["content"]
    assert router.dispatched == []
    # missing required and unknown property are reported too, still without dispatch
    r = client.post("/tools/call", json={"server": "stub", "tool": "echo", "args": {"bogus": 1}}, headers=_h())
    assert r.json()["is_error"] is True
    assert "text" in r.json()["content"] and "bogus" in r.json()["content"]
    assert router.dispatched == []


def test_tools_call_unknown_server_and_tool(client):
    r = client.post("/tools/call", json={"server": "nope", "tool": "echo", "args": {}}, headers=_h())
    assert r.status_code == 200 and r.json()["is_error"] is True and "unknown MCP server" in r.json()["content"]
    r = client.post("/tools/call", json={"server": "stub", "tool": "nope", "args": {}}, headers=_h())
    assert r.status_code == 200 and r.json()["is_error"] is True and "unknown tool" in r.json()["content"]


def test_tools_call_bad_body_is_422(client):
    r = client.post("/tools/call", json={"server": "stub"}, headers=_h())
    assert r.status_code == 422
    r = client.post("/tools/call", json={"server": "stub", "tool": "echo", "args": {}, "extra": 1}, headers=_h())
    assert r.status_code == 422


# ----------------------------------------------------------------------------- /script/run


def test_script_run_python_echo_in_fresh_sandbox(client, settings):
    r = client.post("/script/run", json={"language": "python", "code": "import os\nprint('hello', os.getcwd())"}, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"index", "command", "cwd", "exit_code", "stdout_tail", "stderr_tail", "elapsed_ms", "timed_out", "sandbox"}
    assert body["exit_code"] == 0 and body["timed_out"] is False
    assert body["stdout_tail"].startswith("hello ")
    assert body["sandbox"].startswith(str(settings.sandbox_root_path / "script-"))
    assert body["cwd"] == body["sandbox"] or body["stdout_tail"].strip().endswith(body["cwd"])
    assert body["command"].startswith("python3" if sys.platform != "win32" else "python")
    # the temp script file is gone and the sandbox dir exists
    from pathlib import Path

    sb = Path(body["sandbox"])
    assert sb.is_dir() and not list(sb.glob(".o-run-*"))


def test_script_run_nonzero_exit_and_stderr(client):
    r = client.post("/script/run", json={"language": "python", "code": "import sys\nsys.stderr.write('bad')\nsys.exit(3)",
                                         "timeout_seconds": 30}, headers=_h())
    assert r.status_code == 200
    assert r.json()["exit_code"] == 3 and "bad" in r.json()["stderr_tail"]


def test_script_run_two_calls_use_distinct_sandboxes(client):
    a = client.post("/script/run", json={"language": "bash", "code": "pwd"}, headers=_h()).json()
    b = client.post("/script/run", json={"language": "bash", "code": "pwd"}, headers=_h()).json()
    assert a["sandbox"] != b["sandbox"] and a["exit_code"] == 0 and b["exit_code"] == 0


def test_script_run_rejects_bad_language_and_escape(client):
    r = client.post("/script/run", json={"language": "ruby", "code": "puts 1"}, headers=_h())
    assert r.status_code == 422
    r = client.post("/script/run", json={"language": "bash", "code": "pwd", "target_directory": "/"}, headers=_h())
    assert r.status_code == 422 and "outside sandbox" in r.json()["detail"]
    r = client.post("/script/run", json={"language": "bash", "code": "pwd", "extra": True}, headers=_h())
    assert r.status_code == 422


# ----------------------------------------------------------------------------- /swarm/run


def test_swarm_run_job_done_with_three_roles(client):
    r = client.post("/swarm/run", json={"objective": "install the deps", "cwd": "/tmp/proj"}, headers=_h())
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    state = _wait_done(client, job_id)
    assert state["ok"] is True and state["phase"] == "report" and state["error"] is None
    assert state["label"] == "install the deps"
    result = state["result"]
    assert set(result) == {"profile", "request", "inspection", "roles"}
    assert len(result["roles"]) == 3
    assert [r["role"] for r in result["roles"]] == ["telemetry_auditor", "subprocess_compiler", "environment_inspector"]
    assert result["request"]["label"] == "install the deps" and len(result["request"]["steps"]) == 1
    assert state["steps"][0]["command_string"] == "echo compiled"
    assert client.deps_holder["deps"].crew.calls == [("install the deps", "/tmp/proj")]


def test_swarm_run_no_plan_is_failed_in_plan_phase(client):
    client.deps_holder["deps"].crew.parser = StubParser(fail=True)
    job_id = client.post("/swarm/run", json={"objective": "nonsense"}, headers=_h()).json()["job_id"]
    state = _wait_done(client, job_id)
    assert state["ok"] is False and state["done"] is True
    assert state["error"] == "the compiler role produced no valid plan"
    assert state["phase"] == "plan"
    assert state["result"]["request"] is None and len(state["result"]["roles"]) == 3


def test_swarm_run_bad_body(client):
    assert client.post("/swarm/run", json={}, headers=_h()).status_code == 422
    assert client.post("/swarm/run", json={"objective": "x", "steps": []}, headers=_h()).status_code == 422


# ----------------------------------------------------------------------------- /provision swarm: true


def test_provision_swarm_replaces_empty_steps(client):
    r = client.post("/provision", json={"label": "set up project", "steps": [], "swarm": True, "cwd": "/tmp/proj"}, headers=_h())
    assert r.status_code == 200
    state = _wait_done(client, r.json()["job_id"])
    assert state["ok"] is True and state["phase"] == "report"
    assert state["steps"][0]["command"] == "echo compiled"
    assert state["swarm"]["compiled_steps"] == 1 and len(state["swarm"]["roles"]) == 3
    assert client.deps_holder["deps"].crew.calls == [("set up project", "/tmp/proj")]


def test_provision_swarm_keeps_provided_steps(client):
    body = {"label": "keep mine", "swarm": True,
            "steps": [{"target_directory": "/tmp/mine", "command_string": "echo mine", "timeout_seconds": 10}]}
    state = _wait_done(client, client.post("/provision", json=body, headers=_h()).json()["job_id"])
    assert state["ok"] is True
    assert state["steps"][0]["command"] == "echo mine"
    assert state["swarm"]["compiled_steps"] == 1
    assert client.deps_holder["deps"].crew.calls == [("keep mine", "/tmp/mine")]


def test_provision_swarm_no_plan_and_empty_steps_fails(client):
    client.deps_holder["deps"].crew.parser = StubParser(fail=True)
    state = _wait_done(client, client.post("/provision", json={"label": "x", "steps": [], "swarm": True}, headers=_h()).json()["job_id"])
    assert state["ok"] is False and state["error"] == "the compiler role produced no valid plan"


def test_provision_swarm_requires_steps_key_and_valid_given_steps(client):
    assert client.post("/provision", json={"label": "x", "swarm": True}, headers=_h()).status_code == 422
    r = client.post("/provision", json={"label": "x", "swarm": True, "steps": [{"command_string": "ls"}]}, headers=_h())
    assert r.status_code == 422
    # without swarm, an empty steps list is still a schema violation
    assert client.post("/provision", json={"label": "x", "steps": []}, headers=_h()).status_code == 422


# ----------------------------------------------------------------------------- /web/run


@needs_chromium
def test_web_run_actions_via_playwright_dom(client, site):
    body = {"task": "read the heading", "actions": [{"op": "goto", "url": site}, {"op": "read", "selector": "#h"}]}
    r = client.post("/web/run", json=body, headers=_h())
    assert r.status_code == 200
    state = _wait_done(client, r.json()["job_id"], timeout=90)
    assert state["ok"] is True and state["phase"] == "report", state
    result = state["result"]
    assert set(result) == {"ok", "final_text", "steps", "screenshots_sha", "via", "error", "elapsed_ms"}
    assert result["via"] == "playwright-dom"
    assert result["final_text"] == "phase two page"
    assert [s["op"] for s in result["steps"]] == ["goto", "read"] and result["steps"][0]["status"] == 200


@needs_chromium
def test_web_run_task_with_url_falls_back_to_goto_read(client, site, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("browser_use"):
            raise ImportError("no browser_use")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = client.post("/web/run", json={"task": "what does the page say", "url": site}, headers=_h())
    state = _wait_done(client, r.json()["job_id"], timeout=90)
    assert state["ok"] is True
    assert state["result"]["via"] == "playwright-dom" and "phase two page" in state["result"]["final_text"]


def test_web_run_without_browser_use_and_no_url_fails_cleanly(client):
    state = _wait_done(client, client.post("/web/run", json={"task": "just think"}, headers=_h()).json()["job_id"])
    assert state["ok"] is False and state["phase"] == "report"
    assert state["result"]["via"] == "playwright-dom" and "browser_use is not installed" in state["error"]


def test_web_run_bad_actions_and_empty_body(client):
    assert client.post("/web/run", json={}, headers=_h()).status_code == 422
    assert client.post("/web/run", json={"actions": [{"op": "explode"}]}, headers=_h()).status_code == 422
    assert client.post("/web/run", json={"actions": [{"op": "goto", "url": "http://x/", "bogus": 1}]}, headers=_h()).status_code == 422


def test_web_result_shape_is_what_the_job_reports():
    dumped = WebResult(ok=False, via="playwright-dom", error="e").model_dump()
    assert set(dumped) == {"ok", "final_text", "steps", "screenshots_sha", "via", "error", "elapsed_ms"}


# ----------------------------------------------------------------------------- /health


def test_health_has_phase2_keys(client, settings, tmp_path):
    r = client.get("/health", headers=_h())
    assert r.status_code == 200
    body = r.json()
    for key in ("mcp_servers", "docker", "e2b", "crewai", "browser_use", "open_interpreter"):
        assert key in body, key
    assert body["mcp_servers"] == 0
    for key in ("docker", "e2b", "crewai", "browser_use", "open_interpreter"):
        assert isinstance(body[key], bool)
    assert body["e2b"] is False  # no key configured
    # servers file counted once written
    settings.data_dir_path.mkdir(parents=True, exist_ok=True)
    settings.mcp_servers_path.write_text('{"mcpServers": {"a": {"command": "x"}, "b": {"url": "http://h/sse"}, "bad": {}}}')
    assert client.get("/health", headers=_h()).json()["mcp_servers"] == 2


# ----------------------------------------------------------------------------- EngineDeps wiring


def test_engine_deps_phase2_lazies(settings):
    from o_engine.server import EngineDeps
    from o_engine.swarm import SimpleCrew

    deps = EngineDeps(settings)
    assert isinstance(deps.runner, NativeRunner)
    assert deps.graph is not None
    router = deps.mcp_router
    assert router is deps.mcp_router and router.list_tools() == []
    crew = deps.crew
    assert isinstance(crew, SimpleCrew) or getattr(crew, "name", "") == "crewai"
    assert deps.web_runner(scripted=True).via == "playwright-dom"
    assert deps.web_runner().via in ("browser-use", "playwright-dom")


# ----------------------------------------------------------------------------- cli


def test_cli_script_run_and_tools_ls(tmp_path, capsys, monkeypatch):
    import json

    from o_engine.cli import main

    monkeypatch.setenv("O_ENGINE_TOKEN", "t")
    monkeypatch.setenv("O_SANDBOX_ROOT", str(tmp_path / "sb"))
    monkeypatch.setenv("O_SCREENPIPE_DB", str(tmp_path / "none.db"))
    script = tmp_path / "s.py"
    script.write_text("print('cli ok')")
    rc = main(["--data-dir", str(tmp_path / "d"), "script", "run", "python", str(script)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["exit_code"] == 0 and "cli ok" in out["stdout_tail"]
    rc = main(["--data-dir", str(tmp_path / "d"), "tools", "ls"])
    assert rc == 0 and "no MCP tools" in capsys.readouterr().out
    rc = main(["--data-dir", str(tmp_path / "d"), "tools", "call", "nope", "echo", "{}"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["is_error"] is True and "unknown MCP server" in out["content"]
    rc = main(["--data-dir", str(tmp_path / "d"), "tools", "call", "nope", "echo", "not json"])
    assert rc == 2
