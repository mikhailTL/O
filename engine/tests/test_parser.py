import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from o_engine import parser as parser_mod
from o_engine.parser import ClaudeCodeParser, LocalModelParser, Parser
from o_engine.schema import ProvisionRequest, SchemaViolation

STEPS = [{"target_directory": ".", "command_string": "npm install", "timeout_seconds": 120}]
STEPS_JSON = json.dumps(STEPS)


def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


def _chunks(text, size=4):
    out = [_chunk(text[i : i + size]) for i in range(0, len(text), size)]
    out.append(SimpleNamespace(choices=[]))
    return out


class FakeOpenAI:
    calls = []
    reply = ""
    error = None
    token_delay = 0.0

    def __init__(self, base_url=None, api_key=None, timeout=None, max_retries=None, **kw):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        FakeOpenAI.calls.append(kw)
        if FakeOpenAI.error is not None:
            raise FakeOpenAI.error
        if FakeOpenAI.token_delay:
            return _slow_stream(_chunks(FakeOpenAI.reply), FakeOpenAI.token_delay)
        return iter(_chunks(FakeOpenAI.reply))


def _slow_stream(chunks, delay):
    for c in chunks:
        time.sleep(delay)
        yield c


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    FakeOpenAI.calls = []
    FakeOpenAI.reply = ""
    FakeOpenAI.error = None
    FakeOpenAI.token_delay = 0.0
    monkeypatch.setattr(parser_mod, "OpenAI", FakeOpenAI)
    yield


@pytest.fixture
def fake_run(monkeypatch):
    state = {"calls": [], "stdout": "", "returncode": 0, "error": None}

    def run(cmd, **kw):
        state["calls"].append((cmd, kw))
        if state["error"] is not None:
            raise state["error"]
        return subprocess.CompletedProcess(cmd, state["returncode"], stdout=state["stdout"], stderr="")

    monkeypatch.setattr(parser_mod.subprocess, "run", run)
    return state


def test_local_parser_streams_and_filters_prose():
    FakeOpenAI.reply = "Sure! Here are the steps:\n```json\n" + STEPS_JSON + "\n```\nHope this helps."
    p = LocalModelParser("http://127.0.0.1:8000/v1", "test-model")
    req = p.parse("npm ERR! missing script", "install deps")
    assert isinstance(req, ProvisionRequest)
    assert req.label == "install deps"
    assert req.steps[0].command_string == "npm install"
    call = FakeOpenAI.calls[0]
    assert call["stream"] is True
    assert call["temperature"] == 0
    assert call["model"] == "test-model"
    assert call["messages"][0]["role"] == "system"
    assert "JSON array" in call["messages"][0]["content"]
    assert "npm ERR! missing script" in call["messages"][-1]["content"]


def test_local_parser_client_points_at_vllm():
    FakeOpenAI.reply = STEPS_JSON
    p = LocalModelParser("http://127.0.0.1:8000/v1", "m", timeout=7)
    p.parse("x", "y")
    assert p.client.base_url == "http://127.0.0.1:8000/v1"
    assert p.client.api_key == "local"
    assert p.client.timeout == 7


def test_local_parser_default_timeout_and_no_retries():
    p = LocalModelParser("u", "m")
    assert p.timeout == 8.0
    assert p.client.timeout == 8.0
    assert p.client.max_retries == 0


def test_local_parser_hung_stream_times_out():
    FakeOpenAI.reply = STEPS_JSON * 3
    FakeOpenAI.token_delay = 0.5
    p = LocalModelParser("u", "m", timeout=1)
    started = time.monotonic()
    with pytest.raises(SchemaViolation) as ei:
        p.parse("e", "g")
    assert time.monotonic() - started < 3
    assert "timed out" in str(ei.value)


def test_local_parser_brackets_in_strings_survive():
    FakeOpenAI.reply = json.dumps(
        [{"target_directory": ".", "command_string": 'grep "[x]" f || echo "}"', "timeout_seconds": 5}]
    ) + " ]]"
    req = LocalModelParser("u", "m").parse("e", "g")
    assert req.steps[0].command_string == 'grep "[x]" f || echo "}"'


def test_local_parser_raises_schema_violation_on_garbage():
    FakeOpenAI.reply = "I cannot help with that."
    with pytest.raises(SchemaViolation):
        LocalModelParser("u", "m").parse("e", "g")


def test_local_parser_raises_schema_violation_on_extra_keys():
    FakeOpenAI.reply = json.dumps([{**STEPS[0], "note": "hi"}])
    with pytest.raises(SchemaViolation):
        LocalModelParser("u", "m").parse("e", "g")


def test_local_available_uses_httpx_models_endpoint(monkeypatch):
    seen = {}

    def fake_get(url, timeout=None, **kw):
        seen["url"] = url
        seen["timeout"] = timeout
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(parser_mod.httpx, "get", fake_get)
    assert LocalModelParser("http://127.0.0.1:8000/v1", "m").available() is True
    assert seen["url"] == "http://127.0.0.1:8000/v1/models"
    assert seen["timeout"] == 2


def test_local_available_false_on_error(monkeypatch):
    def fake_get(url, timeout=None, **kw):
        raise parser_mod.httpx.ConnectError("down")

    monkeypatch.setattr(parser_mod.httpx, "get", fake_get)
    assert LocalModelParser("http://127.0.0.1:8000/v1", "m").available() is False

    monkeypatch.setattr(parser_mod.httpx, "get", lambda url, timeout=None, **kw: SimpleNamespace(status_code=503))
    assert LocalModelParser("http://127.0.0.1:8000/v1", "m").available() is False


def test_claude_parser_runs_cli_and_extracts_result(fake_run):
    fake_run["stdout"] = json.dumps({"type": "result", "result": "Here:\n```json\n" + STEPS_JSON + "\n```"})
    p = ClaudeCodeParser(claude_path="/opt/bin/claude")
    req = p.parse("stderr text", "fix build")
    assert req.steps[0].timeout_seconds == 120
    cmd, kw = fake_run["calls"][0]
    assert cmd[0] == "/opt/bin/claude"
    assert "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "stderr text" in kw["input"]
    assert "JSON array" in kw["input"]
    assert kw["capture_output"] is True
    assert kw["text"] is True
    assert kw["timeout"] > 0


def test_claude_parser_nonzero_exit_raises(fake_run):
    fake_run["returncode"] = 1
    fake_run["stdout"] = ""
    with pytest.raises(SchemaViolation):
        ClaudeCodeParser().parse("e", "g")


def test_claude_parser_non_json_stdout_raises(fake_run):
    fake_run["stdout"] = "not json"
    with pytest.raises(SchemaViolation):
        ClaudeCodeParser().parse("e", "g")


def test_claude_parser_result_without_steps_raises(fake_run):
    fake_run["stdout"] = json.dumps({"result": "I do not know."})
    with pytest.raises(SchemaViolation):
        ClaudeCodeParser().parse("e", "g")


def test_claude_parser_missing_binary_raises(fake_run):
    fake_run["error"] = FileNotFoundError("claude")
    with pytest.raises(SchemaViolation):
        ClaudeCodeParser().parse("e", "g")


def test_parser_uses_local_first(fake_run):
    FakeOpenAI.reply = STEPS_JSON
    p = Parser(LocalModelParser("u", "m"), ClaudeCodeParser())
    req = p.parse("err", "goal")
    assert req.label == "goal"
    assert fake_run["calls"] == []
    assert p.describe() == "local"


def test_parser_falls_back_on_local_exception(fake_run):
    FakeOpenAI.error = ConnectionError("vllm down")
    fake_run["stdout"] = json.dumps({"result": STEPS_JSON})
    p = Parser(LocalModelParser("u", "m"), ClaudeCodeParser())
    req = p.parse("err", "goal")
    assert req.steps[0].command_string == "npm install"
    assert len(fake_run["calls"]) == 1
    assert p.describe() == "fallback"


def test_parser_falls_back_on_local_schema_violation(fake_run):
    FakeOpenAI.reply = "nope"
    fake_run["stdout"] = json.dumps({"result": STEPS_JSON})
    p = Parser(LocalModelParser("u", "m"), ClaudeCodeParser())
    assert p.parse("err", "goal").label == "goal"
    assert p.describe() == "fallback"


def test_parser_both_fail_raises_schema_violation(fake_run):
    FakeOpenAI.reply = "nope"
    fake_run["stdout"] = json.dumps({"result": "also nope"})
    p = Parser(LocalModelParser("u", "m"), ClaudeCodeParser())
    with pytest.raises(SchemaViolation) as ei:
        p.parse("err", "goal")
    assert "local" in str(ei.value) and "fallback" in str(ei.value)
    assert len(fake_run["calls"]) == 1
    assert p.describe() == "none"


def test_parser_fallback_exception_becomes_schema_violation(fake_run):
    FakeOpenAI.error = RuntimeError("boom")
    fake_run["error"] = OSError("no claude")
    p = Parser(LocalModelParser("u", "m"), ClaudeCodeParser())
    with pytest.raises(SchemaViolation):
        p.parse("err", "goal")


def test_parser_without_fallback_raises(fake_run):
    FakeOpenAI.error = RuntimeError("boom")
    p = Parser(LocalModelParser("u", "m"), None)
    with pytest.raises(SchemaViolation):
        p.parse("err", "goal")
    assert p.describe() == "none"


def test_parser_describe_before_parse():
    assert Parser(LocalModelParser("u", "m"), ClaudeCodeParser()).describe() == "none"
