"""``o_engine.llm.claude_cli``: the one paid-call runner. The proxy (O_PROXY_URL) is preferred when it is set, the
CLI is the fallback and the only transport for calls that read a file (``cwd``), and the four callers all go
through it."""
from __future__ import annotations

import json
import subprocess

import pytest

from o_engine import llm
from o_engine.schema import SchemaViolation


@pytest.fixture
def cli(monkeypatch):
    state = {"calls": [], "stdout": json.dumps({"result": "from cli", "total_cost_usd": 0.02}), "returncode": 0}

    def run(cmd, **kw):
        state["calls"].append((cmd, kw))
        return subprocess.CompletedProcess(cmd, state["returncode"], stdout=state["stdout"], stderr="cli stderr")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv("O_PROXY_URL", raising=False)
    monkeypatch.delenv("O_PROXY_TOKEN", raising=False)
    return state


@pytest.fixture
def proxy(monkeypatch):
    state = {"calls": [], "status": 200, "content": "from proxy", "cost": "0.5", "raise": None}

    class R:
        def __init__(self):
            self.status_code = state["status"]
            self.headers = {"x-o-cost-usd": state["cost"]}
            self.text = "proxy body"

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": state["content"]}}], "usage": {"total_tokens": 3}}

    def post(url, json=None, headers=None, timeout=None):
        state["calls"].append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if state["raise"]:
            raise state["raise"]
        return R()

    import httpx

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("O_PROXY_URL", "http://127.0.0.1:8787/v1/")
    monkeypatch.setenv("O_PROXY_TOKEN", "tok")
    return state


def test_cli_path_matches_the_old_per_module_behaviour(cli):
    payload = llm.claude_cli("hello", claude_path="/opt/claude", timeout=7, system="SYS")
    assert payload["result"] == "from cli" and payload["total_cost_usd"] == 0.02 and payload["via"] == "cli"
    cmd, kw = cli["calls"][0]
    assert cmd == ["/opt/claude", "-p", "--output-format", "json"]
    assert kw["input"] == "SYS\n\nhello" and kw["capture_output"] is True and kw["text"] is True and kw["timeout"] == 7
    assert "cwd" not in kw
    llm.claude_cli("x", cwd="/tmp/work")
    assert cli["calls"][1][1]["cwd"] == "/tmp/work"


def test_cli_errors_keep_their_messages(cli, monkeypatch):
    cli["returncode"] = 3
    with pytest.raises(SchemaViolation, match="claude cli exited 3: cli stderr"):
        llm.claude_cli("x")
    cli["returncode"] = 0
    cli["stdout"] = "not json"
    with pytest.raises(SchemaViolation, match="claude cli output is not JSON"):
        llm.claude_cli("x")
    cli["stdout"] = json.dumps({"result": 5})
    with pytest.raises(SchemaViolation, match="no string result"):
        llm.claude_cli("x")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("claude")))
    with pytest.raises(SchemaViolation, match="claude cli failed to run"):
        llm.claude_cli("x")


def test_proxy_is_preferred_when_configured(cli, proxy):
    payload = llm.claude_cli("hello", system="SYS", timeout=9)
    assert payload["result"] == "from proxy" and payload["via"] == "proxy" and payload["total_cost_usd"] == 0.5
    assert cli["calls"] == [], "no claude -p process was spawned"
    call = proxy["calls"][0]
    assert call["url"] == "http://127.0.0.1:8787/v1/chat/completions"
    assert call["json"]["model"] == llm.PROXY_MODEL and call["json"]["stream"] is False
    assert call["json"]["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hello"}]
    assert call["headers"]["authorization"] == "Bearer tok" and call["headers"]["x-o-caller"] == llm.PROXY_CALLER


def test_proxy_failure_falls_back_to_the_cli(cli, proxy):
    proxy["status"] = 502
    payload = llm.claude_cli("hello")
    assert payload["via"] == "cli" and len(proxy["calls"]) == 1 and len(cli["calls"]) == 1
    proxy["status"] = 200
    proxy["raise"] = ConnectionError("down")
    assert llm.claude_cli("hello")["via"] == "cli"


def test_a_call_that_reads_a_file_always_uses_the_cli(cli, proxy):
    payload = llm.claude_cli("read screen.png", cwd="/tmp/x")
    assert payload["via"] == "cli" and proxy["calls"] == [] and cli["calls"][0][1]["cwd"] == "/tmp/x"
    assert llm.claude_cli("y", allow_proxy=False)["via"] == "cli" and proxy["calls"] == []


def test_the_four_callers_share_the_runner(cli, proxy):
    from o_engine.cu.ground import ClaudeCodeGrounder
    from o_engine.cu.judge import ClaudeCodeJudge
    from o_engine.cu.planner import ClaudeCodeClient
    from o_engine.parser import ClaudeCodeParser

    proxy["content"] = '[{"target_directory": ".", "command_string": "npm install", "timeout_seconds": 30}]'
    req = ClaudeCodeParser().parse("npm ERR", "install deps")
    assert req.steps[0].command_string == "npm install" and len(proxy["calls"]) == 1 and cli["calls"] == []
    proxy["content"] = "plan text"
    assert ClaudeCodeClient().complete("S", "U") == "plan text" and len(proxy["calls"]) == 2
    assert proxy["calls"][1]["json"]["messages"][0] == {"role": "system", "content": "S"}
    # image readers pin to the CLI (workdir given)
    cli["stdout"] = json.dumps({"result": '{"ok": true, "reason": "saved"}'})
    assert ClaudeCodeJudge().judge(b"png", "saved?").ok is True and len(cli["calls"]) == 1
    cli["stdout"] = json.dumps({"result": '{"x": 1, "y": 2, "w": 3, "h": 4}'})
    assert ClaudeCodeGrounder().ground(b"png", "Save").x == 1 and len(cli["calls"]) == 2
    assert len(proxy["calls"]) == 2, "neither image call went through the proxy"
