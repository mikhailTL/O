import json
from dataclasses import dataclass, field

import pytest

from o_engine.actionlog import ActionLog
from o_engine.graph import GRAPH_EDGES, GRAPH_NODES, Deps, build_graph, run_job
from o_engine.remediate import Patch, Remediator
from o_engine.schema import ProvisionRequest


class Profile:
    def model_dump(self):
        return {"cwd": "proj", "shell_history": ["ls"], "tree": {}}


class StubContext:
    def __init__(self):
        self.calls = []

    def profile(self, cwd):
        self.calls.append(cwd)
        return Profile()


class StubParser:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])

    def parse(self, text, goal):
        self.calls.append((text, goal))
        steps = self.replies.pop(0) if self.replies else [
            {"target_directory": "proj", "command_string": "echo fixed", "timeout_seconds": 5}
        ]
        return ProvisionRequest(label=goal, steps=steps)

    def describe(self):
        return "stub-parser"


@dataclass
class StepResult:
    index: int
    command: str
    cwd: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    elapsed_ms: int = 1
    timed_out: bool = False


class StubSandbox:
    def __init__(self, root, failing=()):
        self.root = root
        self.failing = set(failing)
        self.runs = []

    def run(self, step, index, env_extra=None, isolated_display=False):
        self.runs.append((index, step.command_string, env_extra, isolated_display))
        if step.command_string in self.failing:
            return StepResult(
                index, step.command_string, step.target_directory, 1,
                stderr="npm WARN deprecated\nModuleNotFoundError: No module named 'flask'\nTraceback here\n",
            )
        return StepResult(index, step.command_string, step.target_directory, 0, stdout="ok\n")


@dataclass
class CacheEntry:
    fingerprint: str
    request: ProvisionRequest
    platform: str
    runs: int = 0


class StubCache:
    def __init__(self):
        self.entries = {}
        self.replays = []
        self.stores = []
        self.bumps = []

    def lookup(self, fp):
        return self.entries.get(fp)

    def store(self, fp, request, platform):
        if fp in self.entries:
            return self.entries[fp]
        entry = CacheEntry(fp, request, platform)
        self.entries[fp] = entry
        self.stores.append(fp)
        return entry

    def replay(self, entry, sandbox):
        self.replays.append(entry.fingerprint)
        return StepResult(0, f"replay {entry.fingerprint[:8]}", "proj", 0, stdout="replayed\n")

    def bump_runs(self, fp):
        self.bumps.append(fp)
        self.entries[fp].runs += 1


class StubDisplay:
    def env(self):
        return {"DISPLAY": ":99"}


@dataclass
class Harness:
    log: ActionLog
    context: StubContext
    parser: StubParser
    sandbox: StubSandbox
    cache: StubCache
    verify_calls: list = field(default_factory=list)
    patches: list = field(default_factory=list)
    deps: Deps = None
    graph: object = None


def make(tmp_data_dir, tmp_path, failing=(), replies=None, max_attempts=3, verify_ok=True):
    h = Harness(
        log=ActionLog(tmp_data_dir),
        context=StubContext(),
        parser=StubParser(replies),
        sandbox=StubSandbox(tmp_path / "sandbox", failing),
        cache=StubCache(),
    )

    def verify_target(target):
        h.verify_calls.append(target)
        return {"ok": verify_ok, "status": 200 if verify_ok else 0, "elapsed_ms": 3, "error": None if verify_ok else "refused"}

    def apply_patch(sandbox, patch):
        h.patches.append((sandbox, patch))
        return {"path": patch.path, "replaced": 1}

    h.deps = Deps(
        context=h.context,
        parser=h.parser,
        sandbox=h.sandbox,
        cache=h.cache,
        verify_target=verify_target,
        display=StubDisplay(),
        log=h.log,
        max_attempts=max_attempts,
        platform="linux",
        apply_patch=apply_patch,
    )
    h.graph = build_graph(h.deps)
    return h


def request(*commands, verify=None, isolated=False):
    return ProvisionRequest(
        label="set up project",
        steps=[{"target_directory": "proj", "command_string": c, "timeout_seconds": 5} for c in commands],
        verify=verify,
        isolated_display=isolated,
    )


def entries(log, kind=None):
    rows = [json.loads(line) for line in log.jsonl.read_text().splitlines()]
    return [r for r in rows if kind is None or r["kind"] == kind]


def phases(log, job_id):
    return [e["detail"]["phase"] for e in entries(log, "graph") if e["detail"].get("job_id") == job_id]


JOBSTATE_KEYS = {
    "id", "label", "phase", "attempt", "max_attempts", "cached", "steps",
    "diagnostic", "verify", "done", "ok", "error",
}


def test_happy_path_phases_in_order_and_parser_not_called(tmp_data_dir, tmp_path):
    h = make(tmp_data_dir, tmp_path)
    req = request("npm install", "npm run build", verify={"port": 3000})
    state = run_job(h.graph, req, "job-1", "log", approve_waiter=None)
    assert set(state) == JOBSTATE_KEYS
    assert phases(h.log, "job-1") == [
        "context", "validate", "cache_lookup", "gate", "execute", "verify", "cache_store", "report",
    ]
    assert h.parser.calls == []
    assert state["ok"] is True and state["done"] is True
    assert state["phase"] == "report"
    assert state["cached"] is False
    assert state["attempt"] == 0
    assert state["max_attempts"] == 3
    assert [s["command"] for s in state["steps"]] == ["npm install", "npm run build"]
    assert all(s["exit_code"] == 0 for s in state["steps"])
    assert state["verify"]["ok"] is True
    assert h.verify_calls == [req.verify]
    assert h.cache.stores == [req.fingerprint("linux")]
    assert h.context.calls == ["proj"]
    assert [r[3] for r in h.sandbox.runs] == [False, False]


def test_isolated_display_env_passed_to_sandbox(tmp_data_dir, tmp_path):
    h = make(tmp_data_dir, tmp_path)
    run_job(h.graph, request("npm test", isolated=True), "job-x", "auto", approve_waiter=None)
    assert h.sandbox.runs[0][2] == {"DISPLAY": ":99"}
    assert h.sandbox.runs[0][3] is True


def test_second_identical_request_hits_cache_and_replays(tmp_data_dir, tmp_path):
    h = make(tmp_data_dir, tmp_path)
    req = request("npm install")
    first = run_job(h.graph, req, "job-1", "log", approve_waiter=None)
    assert first["cached"] is False
    runs_before = len(h.sandbox.runs)

    second = run_job(h.graph, request("npm install"), "job-2", "log", approve_waiter=None)
    fp = req.fingerprint("linux")
    assert second["cached"] is True
    assert second["ok"] is True
    assert h.cache.replays == [fp]
    assert len(h.sandbox.runs) == runs_before
    assert h.parser.calls == []
    assert h.cache.stores == [fp]
    assert h.cache.bumps == [fp]
    assert phases(h.log, "job-2") == [
        "context", "validate", "cache_lookup", "gate", "execute", "verify", "cache_store", "report",
    ]
    hits = [e for e in entries(h.log, "cache") if e["detail"].get("job_id") == "job-2" and e["detail"].get("hit")]
    assert hits


def test_failing_step_loops_at_most_max_attempts_then_reports_failure(tmp_data_dir, tmp_path):
    h = make(tmp_data_dir, tmp_path, failing={"pip install -r requirements.txt", "echo fixed"}, max_attempts=2)
    state = run_job(h.graph, request("pip install -r requirements.txt"), "job-f", "log", approve_waiter=None)
    assert state["ok"] is False and state["done"] is True
    assert state["phase"] == "report"
    assert state["attempt"] == 2
    assert state["error"]
    ph = phases(h.log, "job-f")
    assert ph.count("remediate") == 2
    assert ph.count("execute") == 3
    assert "cache_store" not in ph
    assert ph[-1] == "report"
    assert len(h.parser.calls) == 2
    assert all(goal == "set up project" for _, goal in h.parser.calls)
    assert "ModuleNotFoundError" in h.parser.calls[0][0]
    assert h.cache.stores == []
    rem = [e for e in entries(h.log, "remediate") if e["detail"].get("job_id") == "job-f"]
    assert len(rem) >= 2
    assert state["diagnostic"]["error_line"].startswith("ModuleNotFoundError")


def test_remediation_with_patch_and_replacement_recovers(tmp_data_dir, tmp_path):
    replies = [[
        {"target_directory": "proj", "command_string": "patch:requirements.txt|||flsk|||flask", "timeout_seconds": 5},
        {"target_directory": "proj", "command_string": "pip install flask", "timeout_seconds": 5},
    ]]
    h = make(tmp_data_dir, tmp_path, failing={"pip install -r requirements.txt"}, replies=replies)
    state = run_job(h.graph, request("pip install -r requirements.txt", "python app.py"), "job-r", "log", approve_waiter=None)
    assert state["ok"] is True
    assert state["attempt"] == 1
    assert state["cached"] is False
    assert len(h.patches) == 1
    assert h.patches[0][0] is h.sandbox
    assert h.patches[0][1] == Patch(path="requirements.txt", find="flsk", replace="flask")
    assert [s["command"] for s in state["steps"]] == ["pip install flask", "python app.py"]
    assert len(h.cache.stores) == 1
    assert phases(h.log, "job-r") == [
        "context", "validate", "cache_lookup", "gate", "execute", "verify",
        "remediate", "gate", "execute", "verify", "cache_store", "report",
    ]


def test_cached_script_that_fails_is_remediated_never_deleted(tmp_data_dir, tmp_path):
    h = make(tmp_data_dir, tmp_path)
    req = request("npm install")
    run_job(h.graph, req, "job-1", "log", approve_waiter=None)
    fp = req.fingerprint("linux")

    def bad_replay(entry, sandbox):
        return StepResult(0, "replay", "proj", 1, stderr="npm ERR! ENOENT no such file\n")

    h.cache.replay = bad_replay
    state = run_job(h.graph, request("npm install"), "job-2", "log", approve_waiter=None)
    assert fp in h.cache.entries
    assert "remediate" in phases(h.log, "job-2")
    assert state["ok"] is True
    assert len(h.parser.calls) == 1


def test_verify_failure_triggers_remediation(tmp_data_dir, tmp_path):
    h = make(tmp_data_dir, tmp_path, max_attempts=1, verify_ok=False)
    state = run_job(h.graph, request("npm start", verify={"port": 5173}), "job-v", "auto", approve_waiter=None)
    assert state["ok"] is False
    assert state["verify"]["ok"] is False
    assert phases(h.log, "job-v").count("remediate") == 1
    assert len(h.parser.calls) == 1


def test_ask_mode_timeout_destroys_queue(tmp_data_dir, tmp_path):
    h = make(tmp_data_dir, tmp_path)
    state = run_job(h.graph, request("rm -rf build"), "job-a", "ask", approve_waiter=lambda job_id, t: None)
    assert state["ok"] is False and state["done"] is True
    assert state["phase"] == "gate"
    assert "QueueDestroyed" in state["error"] or "queue" in state["error"].lower()
    assert h.sandbox.runs == []
    assert phases(h.log, "job-a") == ["context", "validate", "cache_lookup", "gate"]
    policy = [e for e in entries(h.log, "policy") if e["detail"].get("job_id") == "job-a"]
    assert policy[-1]["detail"]["allowed"] is False


def test_ask_mode_deny_destroys_queue(tmp_data_dir, tmp_path):
    h = make(tmp_data_dir, tmp_path)
    state = run_job(h.graph, request("ls"), "job-d", "ask", approve_waiter=lambda job_id, t: False)
    assert state["ok"] is False
    assert h.sandbox.runs == []


def test_ask_mode_allow_runs(tmp_data_dir, tmp_path):
    h = make(tmp_data_dir, tmp_path)
    seen = []
    state = run_job(h.graph, request("ls"), "job-ok", "ask", approve_waiter=lambda job_id, t: seen.append((job_id, t)) or True)
    assert state["ok"] is True
    assert seen == [("job-ok", 60)]


def test_graph_edges_match_plan_diagram(tmp_data_dir, tmp_path):
    expected_nodes = {"context", "validate", "cache_lookup", "gate", "execute", "verify", "cache_store", "remediate", "report"}
    expected_edges = {
        ("__start__", "context"),
        ("context", "validate"),
        ("validate", "cache_lookup"),
        ("cache_lookup", "gate"),
        ("gate", "execute"),
        ("execute", "verify"),
        ("verify", "cache_store"),
        ("verify", "remediate"),
        ("verify", "report"),
        ("remediate", "gate"),
        ("cache_store", "report"),
        ("report", "__end__"),
    }
    assert set(GRAPH_NODES) == expected_nodes
    assert set(GRAPH_EDGES) == expected_edges

    h = make(tmp_data_dir, tmp_path)
    g = h.graph.get_graph()
    assert set(g.nodes) - {"__start__", "__end__"} == expected_nodes
    assert {(e.source, e.target) for e in g.edges} == set(GRAPH_EDGES)


def test_remediator_plan_extracts_error_and_patches(tmp_data_dir, tmp_path):
    log = ActionLog(tmp_data_dir)
    parser = StubParser([[
        {"target_directory": "proj", "command_string": "patch:a.py|||x|||y", "timeout_seconds": 5},
    ]])
    sandbox = StubSandbox(tmp_path)
    applied = []
    rem = Remediator(parser, sandbox, log, apply_patch=lambda sb, p: applied.append(p))
    req = request("python a.py")
    state = {
        "id": "j", "label": "set up project", "request": req, "attempt": 0, "max_attempts": 3,
        "steps": [{"index": 0, "command": "python a.py", "cwd": "proj", "exit_code": 1,
                   "stdout_tail": "", "stderr_tail": "warning: thing\nbash: python: command not found\n",
                   "elapsed_ms": 1}],
        "verify": None, "patches_applied": 0, "cached": False, "fingerprint": req.fingerprint("linux"),
    }
    plan = rem.plan(state)
    assert plan == [Patch(path="a.py", find="x", replace="y")]
    assert parser.calls[0][0].startswith("bash: python: command not found")
    update = rem.remediate(state, platform="linux", plan=plan)
    assert applied == plan
    assert update["attempt"] == 1
    assert update["patches_applied"] == 1
    assert update["request"] == req
    assert update["cached"] is False
    assert update["diagnostic"]["error_line"] == "bash: python: command not found"
    assert [e["kind"] for e in entries(log)].count("remediate") >= 1


def test_remediator_replaces_failing_step(tmp_data_dir, tmp_path):
    log = ActionLog(tmp_data_dir)
    parser = StubParser([[
        {"target_directory": "proj", "command_string": "npm ci", "timeout_seconds": 9},
    ]])
    rem = Remediator(parser, StubSandbox(tmp_path), log, apply_patch=lambda sb, p: None)
    req = request("npm install", "npm run build")
    state = {
        "id": "j", "label": "set up project", "request": req, "attempt": 1, "max_attempts": 3,
        "steps": [
            {"index": 0, "command": "npm install", "cwd": "proj", "exit_code": 0, "stdout_tail": "", "stderr_tail": "", "elapsed_ms": 1},
            {"index": 1, "command": "npm run build", "cwd": "proj", "exit_code": 1, "stdout_tail": "",
             "stderr_tail": "npm ERR! missing script: build\n", "elapsed_ms": 1},
        ],
        "verify": None, "patches_applied": 0, "cached": False, "fingerprint": req.fingerprint("linux"),
    }
    plan = rem.plan(state)
    assert isinstance(plan, ProvisionRequest)
    update = rem.remediate(state, platform="linux", plan=plan)
    assert [s.command_string for s in update["request"].steps] == ["npm install", "npm ci"]
    assert update["request"].label == "set up project"
    assert update["fingerprint"] == update["request"].fingerprint("linux")
    assert update["attempt"] == 2
