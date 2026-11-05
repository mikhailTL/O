import json
import subprocess
from types import SimpleNamespace

import pytest

from o_engine.cu import planner as planner_mod
from o_engine.cu.planner import (
    BestOfN,
    ClaudeCodeClient,
    LocalModelClient,
    ModelClient,
    Planner,
    goal_question,
)
from o_engine.cu.schema import OScript, parse_script
from o_engine.schema import SchemaViolation

SCRIPT = {
    "app": "calc",
    "goal": "put 42 in A1",
    "steps": [
        {"op": "launch", "cmd": "libreoffice --calc"},
        {"op": "wait", "for": {"role": "frame", "name": "*Calc*"}, "timeout_s": 20},
        {"op": "find", "as": "cell", "target": {"role": "table cell", "name": "A1"}},
        {"op": "click", "ref": "cell"},
        {"op": "type", "ref": "cell", "text": "42"},
        {"op": "assert", "target": {"role": "table cell", "name": "A1", "state": "focused"}},
    ],
}


def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


def _chunks(text, size=7):
    out = [_chunk(text[i : i + size]) for i in range(0, len(text), size)]
    out.append(SimpleNamespace(choices=[]))
    return out


class FakeOpenAI:
    calls = []
    reply = ""
    error = None

    def __init__(self, base_url=None, api_key=None, timeout=None, max_retries=None, **kw):
        self.max_retries = max_retries
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        FakeOpenAI.calls.append(kw)
        if FakeOpenAI.error is not None:
            raise FakeOpenAI.error
        return iter(_chunks(FakeOpenAI.reply))


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeOpenAI.calls = []
    FakeOpenAI.reply = ""
    FakeOpenAI.error = None
    monkeypatch.setattr(planner_mod, "OpenAI", FakeOpenAI)
    yield


@pytest.fixture
def fake_run(monkeypatch):
    state = {"calls": [], "stdout": "", "returncode": 0}

    def run(cmd, **kw):
        state["calls"].append((cmd, kw))
        return subprocess.CompletedProcess(cmd, state["returncode"], stdout=state["stdout"], stderr="")

    monkeypatch.setattr(planner_mod.subprocess, "run", run)
    return state


class StubClient:
    """parser_like: .complete(system, user) -> str, replies popped in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        if not self.replies:
            raise AssertionError("unexpected model call")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class StubNotes:
    def for_prompt(self, app, max_chars=6000):
        return f"NOTES-FOR-{app}"


def test_local_client_streams_text_and_fallback_once(fake_run):
    FakeOpenAI.reply = "hello world"
    local = LocalModelClient("http://127.0.0.1:8000/v1", "m")
    assert local.client.max_retries == 0
    mc = ModelClient(local, ClaudeCodeClient("claude"))
    assert mc.complete("sys", "usr") == "hello world"
    assert mc.describe() == "local"
    assert FakeOpenAI.calls[0]["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
    FakeOpenAI.error = RuntimeError("vllm down")
    fake_run["stdout"] = json.dumps({"result": "from claude {\"a\":1}"})
    assert mc.complete("s", "u") == 'from claude {"a":1}'
    assert mc.describe() == "fallback" and len(fake_run["calls"]) == 1
    assert mc.complete_json("s", "u") == '{"a":1}'
    fake_run["returncode"] = 2
    with pytest.raises(SchemaViolation, match="all model backends failed"):
        mc.complete("s", "u")


def test_planner_plans_oscript_with_notes_and_tree():
    client = StubClient("Here you go:\n```json\n" + json.dumps(SCRIPT) + "\n```")
    p = Planner(client, StubNotes())
    script = p.plan("put 42 in A1", "calc", "frame 'Calc' > table cell A1")
    assert isinstance(script, OScript) and len(script.steps) == 6
    system, user = client.calls[0]
    assert "NOTES-FOR-calc" in user and "table cell A1" in user and "put 42 in A1" in user
    assert "ONLY one JSON object" in system
    assert p.calls == 1


@pytest.mark.parametrize("reply", ["I cannot do that.", "[]", '{"app":"calc"}', json.dumps({**SCRIPT, "extra": 1})])
def test_planner_garbage_is_schema_violation(reply):
    with pytest.raises(SchemaViolation):
        Planner(StubClient(reply)).plan("g", "calc", "")


def test_planner_uses_injected_parse_script_stub():
    def stub_parse(raw):
        obj = json.loads(raw)
        return SimpleNamespace(app=obj["app"], goal=obj["goal"], steps=tuple(obj["steps"]))

    s = Planner(StubClient('{"app":"a","goal":"g","steps":[{"op":"key","keys":"ctrl+s"}]}'), parse_script=stub_parse).plan("g", "a")
    assert s.steps[0]["op"] == "key"


def test_replan_step_returns_one_validated_step():
    script = parse_script(SCRIPT)
    client = StubClient('{"op":"find","as":"cell","target":{"role":"table cell","name":"Cell A1"}}')
    p = Planner(client)
    step = p.replan_step(script, 2, "element not found: role='table cell' name='A1'", "table cell 'Cell A1'")
    assert step.op == "find" and step.target.name == "Cell A1"
    assert "Failed step index: 2" in client.calls[0][1] and "element not found" in client.calls[0][1]
    healed = p.replace_step(script, 2, step)
    assert isinstance(healed, OScript) and healed.steps[2].target.name == "Cell A1"
    assert script.steps[2].target.name == "A1"
    with pytest.raises(SchemaViolation):
        Planner(StubClient('{"op":"click","ref":"nope"}')).replan_step(script, 3, "err", "")
    with pytest.raises(SchemaViolation):
        Planner(StubClient("{}")).replan_step(script, 99, "err", "")


def _result(ok, step_oks, failed_index=-1, png=b"png"):
    return SimpleNamespace(ok=ok, steps=[{"ok": o} for o in step_oks], failed_index=failed_index, screenshot=png)


class StubGoalJudge:
    def __init__(self, answers):
        self.answers = list(answers)
        self.questions = []

    def judge(self, png, question):
        self.questions.append(question)
        ok = self.answers.pop(0)
        return SimpleNamespace(ok=ok, reason="stub")


def test_best_of_n_picks_highest_score():
    short = {**SCRIPT, "steps": SCRIPT["steps"][:5]}
    client = StubClient(json.dumps(SCRIPT), json.dumps(short), json.dumps(SCRIPT))
    planner = Planner(client)
    results = {
        0: _result(True, [True] * 6),            # goal ok 1 + 1 - 0.06 = 1.94
        1: _result(True, [True] * 5),            # goal ok 1 + 1 - 0.05 = 1.95  <- winner
        2: _result(False, [True, True, False], failed_index=2),  # 0 + 2/6 - 0.06 = 0.27
    }
    slots = []

    def run_fn(script, slot):
        slots.append((slot, len(script.steps)))
        return results[slot]

    judge = StubGoalJudge([True, True, False])
    out = BestOfN(3, planner, run_fn, judge).run("put 42 in A1", "calc", ["t0", "t1", "t2"])
    assert out["winner"] is results[1] and out["winner_index"] == 1
    assert [round(s, 2) for s in out["scores"]] == [1.94, 1.95, 0.27]
    assert slots == [(0, 6), (1, 5), (2, 6)]
    assert planner.calls == 3 and len(out["candidates"]) == 3
    assert judge.questions[0] == goal_question("put 42 in A1")
    assert "t2" in client.calls[2][1]


def test_best_of_n_n1_is_plain_run_without_judge_call():
    client = StubClient(json.dumps(SCRIPT))
    judge = StubGoalJudge([])
    res = _result(True, [True] * 6)
    out = BestOfN(1, Planner(client), lambda s, slot: res, judge).run("g", "calc", "tree")
    assert out["winner"] is res and judge.questions == [] and out["scores"] == [pytest.approx(1.94)]


def test_best_of_n_survives_a_failed_candidate_and_callable_judge():
    client = StubClient("garbage", json.dumps(SCRIPT))
    res = _result(False, [True, False], failed_index=1)
    judged = []

    def judge(script, result, goal):
        judged.append(goal)
        return True

    out = BestOfN(2, Planner(client), lambda s, slot: res, judge).run("g", "calc", "")
    assert out["winner_index"] == 1 and out["candidates"][0]["error"].startswith("SchemaViolation")
    assert out["scores"][0] == float("-inf") and judged == ["g"]
    with pytest.raises(SchemaViolation, match="no candidate"):
        BestOfN(1, Planner(StubClient("nope")), lambda s, slot: res, None).run("g", "calc", "")


# ---------------------------------------------------------------- wave 2 (I-H): P1, P6, P7, P8, P9 planner wiring

from o_engine.cu.memory import WorkingMemory
from o_engine.cu.planner import (
    STATIC_PREFIX,
    behaviour_narrative,
    build_plan_prompt,
    build_replan_prompt,
    render_interfaces,
)
from o_engine.cu.schema import Milestone, Target

MILESTONES_REPLY = json.dumps({
    "milestones": [
        {"goal": "open the sheet", "done_when": {"role": "frame", "name": "*Calc*"}},
        {"goal": "enter 42 in A1", "done_when": {"role": "table cell", "name": "A1", "state": "focused"}},
        {"goal": "save as report.ods", "done_when": "Is the window title 'report.ods'?"},
    ]
})


def test_milestone_model_is_frozen_and_strict():
    m = Milestone(goal="g", done_when="Is it done?")
    assert m.done_when == "Is it done?"
    t = Milestone.model_validate({"goal": "g", "done_when": {"role": "frame", "name": "*Calc*"}})
    assert isinstance(t.done_when, Target) and t.done_when.role == "frame"
    with pytest.raises(Exception):
        Milestone(goal="g", done_when="q", extra=1)
    with pytest.raises(Exception):
        m.goal = "x"
    with pytest.raises(Exception):
        Milestone(goal="", done_when="q")


def test_plan_milestones_parses_three_and_falls_back_on_model_error():
    client = StubClient("sure:\n" + MILESTONES_REPLY)
    p = Planner(client, StubNotes())
    ms = p.plan_milestones("put 42 in A1 and save as report.ods", "calc")
    assert len(ms) == 3 and all(isinstance(m, Milestone) for m in ms)
    assert isinstance(ms[0].done_when, Target) and ms[0].done_when.name == "*Calc*"
    assert isinstance(ms[1].done_when, Target) and ms[1].done_when.name == "A1"  # state lifted off the target
    assert ms[2].done_when == "Is the window title 'report.ods'?"
    assert p.calls == 1 and len(client.calls) == 1
    assert "NOTES-FOR-calc" in client.calls[0][1] and "put 42 in A1" in client.calls[0][1]
    assert client.calls[0][1].startswith(STATIC_PREFIX.rstrip())

    for reply in (RuntimeError("vllm down"), "I cannot.", '{"milestones": []}', '{"milestones": [{"goal": ""}]}'):
        fb = Planner(StubClient(reply)).plan_milestones("do the thing", "calc")
        assert len(fb) == 1 and fb[0].goal == "do the thing" and fb[0].done_when == goal_question("do the thing")

    many = json.dumps({"milestones": [{"goal": f"m{i}", "done_when": f"q{i}?"} for i in range(12)]})
    assert len(Planner(StubClient(many)).plan_milestones("g", "calc")) == 8


def _next_reply(n_steps):
    steps = [{"op": "key", "keys": f"ctrl+{i}"} for i in range(n_steps)]
    return json.dumps({"app": "calc", "goal": "enter 42", "steps": steps})


def test_next_steps_caps_at_max_steps_and_rejects_unknown_ops():
    m = Milestone(goal="enter 42 in A1", done_when={"role": "table cell", "name": "A1"})
    mem = WorkingMemory.from_goal("enter 42 in A1; do not overwrite report.ods")
    client = StubClient(_next_reply(9))
    p = Planner(client)
    fns = [{"name": "set_cell", "description": "Set a cell by address", "signature": "(backend, address, value)"}]
    script = p.next_steps(m, "frame 'Calc' > table cell A1", mem, "NOTES", interfaces=fns, max_steps=5, app="calc")
    assert isinstance(script, OScript) and len(script.steps) == 5 and script.app == "calc"
    assert p.calls == 1 and len(client.calls) == 1
    system, user = client.calls[0]
    assert "ONLY one JSON object" in system and "state_assert" in system and '"op":"script"' in system
    assert user.startswith(STATIC_PREFIX.rstrip())
    assert "## Working memory" in user and "Constraint: do not overwrite report.ods" in user
    assert "Available functions (use op call):" in user and "- calc.set_cell(backend, address, value): Set a cell by address" in user
    assert "Milestone: enter 42 in A1" in user and "element present: role='table cell' name='A1'" in user
    assert "Plan at most 5 step(s)." in user and "NOTES" in user and "table cell A1" in user
    # the prefix is identical regardless of memory/tree; the memory block sits right after it
    assert user.index("## Working memory") == len(STATIC_PREFIX.rstrip()) + 2

    s1 = Planner(StubClient(_next_reply(1))).next_steps(m, "", None, "", max_steps=1, app="calc")
    assert len(s1.steps) == 1

    bad = json.dumps({"app": "calc", "goal": "g", "steps": [{"op": "teleport", "to": "A1"}]})
    with pytest.raises(SchemaViolation):
        Planner(StubClient(bad)).next_steps(m, "", None, "", max_steps=5, app="calc")
    with pytest.raises(SchemaViolation):
        Planner(StubClient("no json here")).next_steps(m, "", None, "", max_steps=5, app="calc")
    with pytest.raises(ValueError):
        Planner(StubClient()).next_steps(m, "", None, "", max_steps=0, app="calc")


def test_next_steps_accepts_script_state_assert_and_call_ops():
    reply = json.dumps({
        "app": "calc", "goal": "convert csvs", "steps": [
            {"op": "script", "script": {"language": "bash", "code": "for f in *.csv; do soffice --convert-to ods \"$f\"; done",
                                         "target_directory": "/tmp/work", "timeout_seconds": 60}},
            {"op": "state_assert", "checks": [{"kind": "file_exists", "arg": "/tmp/work/a.ods"}]},
            {"op": "call", "fn": "calc.set_cell", "args": {"address": "A1", "value": "42"}},
        ],
    })
    m = Milestone(goal="convert csvs", done_when="Are all .ods files present?")
    script = Planner(StubClient(reply)).next_steps(m, "", None, "", max_steps=5, app="calc")
    assert [s.op for s in script.steps] == ["script", "state_assert", "call"]


def test_static_prefix_bytes_identical_across_turns():
    m1 = WorkingMemory.from_goal("fill the form; never submit twice").add("state", "dialog open")
    m2 = WorkingMemory.from_goal("rename files").add("facts", "there are 12 files").add("todos", "rename the rest")
    a = build_plan_prompt("fill the form", "forms", "NOTES-A", "tree A\nrow 1", memory=m1)
    b = build_plan_prompt("rename files", "files", "", "completely different tree", memory=m2)
    marker = "## Working memory"
    assert a[: a.index(marker)] == b[: b.index(marker)] == STATIC_PREFIX.rstrip() + "\n\n"
    assert a.encode() [: a.index(marker)] == b.encode()[: b.index(marker)]
    # dynamic context comes after the memory block, never before it
    assert a.index("Goal: fill the form") > a.index(marker) and a.index("tree A") > a.index("Goal:")
    assert "- Constraint: never submit twice" in a and "- there are 12 files" in b
    r1 = build_replan_prompt("{}", 0, "err", "", "tree X", memory=m1)
    r2 = build_replan_prompt('{"a":1}', 3, "other", "notes", "tree Y", memory=m2)
    assert r1[: r1.index(marker)] == r2[: r2.index(marker)] == a[: a.index(marker)]
    # the rules the research memo asks for are in the static prefix
    assert "Prefer op script for file, format and bulk work" in STATIC_PREFIX
    assert "Follow every script, save, export or submit step with a state_assert" in STATIC_PREFIX
    # without memory the prompt is prefix + dynamic context (old callers keep working)
    plain = build_plan_prompt("g", "calc", "", "")
    assert plain.startswith(STATIC_PREFIX.rstrip()) and marker not in plain and plain.endswith("OScript JSON object:")


def test_replan_prompt_carries_loop_evidence_and_rule():
    script = parse_script(SCRIPT)
    evidence = [
        {"index": 3, "op": "click", "step": '{"op":"click","ref":"cell"}', "screen_sha": "abc123"},
        {"index": 3, "op": "click", "step": '{"op":"click","ref":"cell"}', "screen_sha": "abc123"},
        {"index": 3, "op": "click", "step": '{"op":"click","ref":"cell"}', "screen_sha": "abc123"},
    ]
    client = StubClient('{"op":"key","keys":"Return"}')
    mem = WorkingMemory.from_goal("put 42 in A1")
    step = Planner(client).replan_step(script, 3, "loop", "tree", memory=mem, loop_evidence=evidence)
    assert step.op == "key"
    system, user = client.calls[0]
    assert "Loop evidence" in user and "must not be reissued unchanged" in user
    assert user.count('"screen_sha": "abc123"') == 3 and '"index": 3' in user
    assert "## Working memory" in user and user.startswith(STATIC_PREFIX.rstrip())
    assert "must\nnot be the repeated step reissued unchanged" in system or "reissued unchanged" in system
    prompt = build_replan_prompt("{}", 0, "e", "", "")
    assert "Loop evidence" not in prompt
    assert "Loop evidence" in build_replan_prompt("{}", 0, "e", "", "", loop_evidence=evidence[:1])


def test_render_interfaces_progressive_disclosure():
    fns = [
        {"name": "set_cell", "description": "Set a cell", "signature": "(backend, address, value)", "body": "SECRET BODY"},
        {"name": "calc.save_as", "description": "", "signature": "(backend, path)"},
        {"name": "", "description": "ignored"},
    ]
    block = render_interfaces(fns, "calc")
    lines = block.splitlines()
    assert lines[0] == "Available functions (use op call):" and lines[1].startswith("fn must be written exactly as listed")
    assert lines[2:] == ["- calc.set_cell(backend, address, value): Set a cell", "- calc.save_as(backend, path)"]
    assert "SECRET BODY" not in block and render_interfaces([], "calc") == "" and render_interfaces(None) == ""
    user = build_plan_prompt("g", "calc", "", "tree", interfaces=fns)
    assert "Available functions (use op call):" in user and user.index("Available functions") < user.index("tree summary:\ntree")


def _candidate(ok, step_oks, script=None, error=None, reason=""):
    steps = [{"index": i, "op": "key", "ok": o, "status": "ok" if o else "failed", "resolved_by": "a11y",
              "after_digest": f"sha{i}", "error": None if o else "boom"} for i, o in enumerate(step_oks)]
    return {"script": parse_script(script or SCRIPT), "result": SimpleNamespace(ok=ok, steps=steps, failed_index=None),
            "judge": {"ok": ok, "reason": reason}, "error": error}


def test_rank_comparative_calls_model_once_and_returns_passing_candidate():
    short = {**SCRIPT, "steps": SCRIPT["steps"][:3]}
    cands = [
        _candidate(False, [True, True, False], reason="file report.ods missing"),
        _candidate(True, [True, True, True], script=short, reason="state_assert file_exists passed"),
        _candidate(False, [True, False], reason="loop"),
    ]
    client = StubClient('{"ranking": [1, 0, 2]}')
    planner = Planner(client)
    b = BestOfN(3, planner, lambda s, slot: None, None, comparative=True)
    assert b.rank_comparative(cands, "save as report.ods") == 1
    assert len(client.calls) == 1 and planner.calls == 1 and b.rank_calls == 1
    system, user = client.calls[0]
    assert '{"ranking"' in system and "Goal: save as report.ods" in user
    assert "Candidate 0: 3 step(s), outcome=fail" in user and "Candidate 1: 3 step(s), outcome=pass" in user
    assert "judge: state_assert file_exists passed" in user and "resolved_by=a11y" in user and "screen=sha0" in user
    assert "status=failed" in user and "error=boom" in user
    # garbage ranking -> fewest steps among passing candidates (index 1 has 3 steps, index 3 has 2)
    cands2 = cands + [_candidate(True, [True, True], script={**SCRIPT, "steps": SCRIPT["steps"][:2]})]
    assert BestOfN(2, Planner(StubClient("nonsense")), lambda s, slot: None, comparative=True).rank_comparative(cands2) == 3
    assert BestOfN(2, Planner(StubClient('{"ranking": ["x", 99, 3]}')), lambda s, slot: None).rank_comparative(cands2) == 3
    assert BestOfN(2, Planner(StubClient(RuntimeError("down"))), lambda s, slot: None).rank_comparative(cands2) == 3
    # nothing passes -> index 0; errored candidates never win; one candidate -> 0 without a model call
    losers = [_candidate(False, [False]), _candidate(False, [True, False], error="SchemaViolation: x")]
    assert BestOfN(2, Planner(StubClient('{"ranking": [1, 0]}')), lambda s, slot: None).rank_comparative(losers) == 0
    solo = StubClient()
    assert BestOfN(2, Planner(solo), lambda s, slot: None).rank_comparative([cands[0]]) == 0 and solo.calls == []
    with pytest.raises(ValueError):
        BestOfN(2, Planner(solo), lambda s, slot: None).rank_comparative([])


def test_behaviour_narrative_accepts_graph_run_records():
    cand = {"run_records": [
        {"index": 0, "command": "launch libreoffice --calc", "cwd": "", "exit_code": 0, "stdout_tail": "resolved_by=a11y",
         "stderr_tail": "", "elapsed_ms": 1, "timed_out": False},
        {"index": 1, "command": "click cell", "cwd": "", "exit_code": 1, "stdout_tail": "", "stderr_tail": "not found",
         "elapsed_ms": 1, "timed_out": False},
    ], "ok": False}
    text = behaviour_narrative(cand, 2)
    assert text.startswith("Candidate 2: 2 step(s), outcome=fail")
    assert "0. launch libreoffice --calc | resolved_by=a11y | status=ok" in text
    assert "1. click cell | resolved_by=- | status=failed | error=not found" in text


def test_best_of_n_comparative_run_ranks_once_and_skips_per_candidate_judge():
    short = {**SCRIPT, "steps": SCRIPT["steps"][:5]}
    client = StubClient(json.dumps(SCRIPT), json.dumps(short), json.dumps(SCRIPT), '{"ranking": [2, 1, 0]}')
    planner = Planner(client)
    results = {0: _result(True, [True] * 6), 1: _result(True, [True] * 5), 2: _result(True, [True] * 6)}
    judge = StubGoalJudge([True, True, True])
    out = BestOfN(3, planner, lambda s, slot: results[slot], judge, comparative=True).run("g", "calc", "t")
    assert out["winner"] is results[2] and out["winner_index"] == 2
    assert judge.questions == [] and planner.calls == 4 and len(client.calls) == 4
    assert out["scores"] == [0.0, 0.0, 1.0]
    # the old scoring path is untouched when comparative is off
    client2 = StubClient(json.dumps(SCRIPT), json.dumps(short))
    out2 = BestOfN(2, Planner(client2), lambda s, slot: results[slot], StubGoalJudge([True, True])).run("g", "calc", "t")
    assert out2["winner_index"] == 1 and [round(s, 2) for s in out2["scores"]] == [1.94, 1.95]


def test_candidate_steps_returns_k_single_step_scripts():
    m = Milestone(goal="enter 42", done_when="Is 42 in A1?")
    replies = [_next_reply(1), _next_reply(3), "garbage", _next_reply(1)]
    client = StubClient(*replies)
    p = Planner(client)
    out = p.candidate_steps(m, "tree", None, "", k=4, app="calc")
    assert len(out) == 3 and all(isinstance(s, OScript) and len(s.steps) == 1 for s in out)
    assert p.calls == 4 and all("Plan at most 1 step(s)." in u for _, u in client.calls)
    assert len(Planner(StubClient(_next_reply(1), _next_reply(1))).candidate_steps(m, "", None, "", k=2, app="calc")) == 2
    with pytest.raises(SchemaViolation, match="no step candidate"):
        Planner(StubClient("x", "y")).candidate_steps(m, "", None, "", k=2, app="calc")
    with pytest.raises(ValueError):
        Planner(StubClient()).candidate_steps(m, "", None, "", k=0, app="calc")


def test_plan_keeps_old_signature_and_accepts_memory():
    client = StubClient(json.dumps(SCRIPT))
    mem = WorkingMemory.from_goal("put 42 in A1")
    s = Planner(client).plan("put 42 in A1", "calc", "tree", memory=mem)
    assert isinstance(s, OScript) and "## Working memory" in client.calls[0][1]
    assert "state_assert" in client.calls[0][0] and "prefer script" in client.calls[0][0].lower()


def test_call_fn_uses_slugged_app_and_prompt_states_the_rule():
    from o_engine.cu.planner import fn_slug

    assert fn_slug("libreoffice-calc") == "libreoffice_calc" and fn_slug("Calc 7.6") == "calc_7_6" and fn_slug("9x") == "app_9x"
    fns = [{"name": "set_cell", "description": "d", "signature": "(backend, a)"}, {"name": "libre-calc.save", "signature": "(backend)"}]
    block = render_interfaces(fns, "libreoffice-calc")
    assert "- libreoffice_calc.set_cell(backend, a): d" in block and "- libre_calc.save(backend)" in block
    assert '"libreoffice_calc.<function>"' in block and "outside [a-z0-9_]" in block
    m = Milestone(goal="g", done_when="q?")
    reply = json.dumps({"app": "libreoffice-calc", "goal": "g", "steps": [{"op": "call", "fn": "libreoffice_calc.set_cell", "args": {"a": "1"}}]})
    s = Planner(StubClient(reply)).next_steps(m, "", None, "", interfaces=fns, max_steps=2, app="libreoffice-calc")
    assert s.steps[0].fn == "libreoffice_calc.set_cell"


def test_model_client_local_only_never_touches_the_fallback(fake_run):
    """Memory latency: ``local_only()`` is the local-model-only view ``cu.memory.extract`` uses, so a vLLM
    outage can never turn into a 120 s ``claude -p`` call per turn."""
    FakeOpenAI.reply = 'x {"a":1}'
    local = LocalModelClient("http://127.0.0.1:8000/v1", "m")
    mc = ModelClient(local, ClaudeCodeClient("claude"))
    only = mc.local_only()
    assert only is not None and only.complete_json("s", "u") == '{"a":1}' and only.complete("s", "u") == 'x {"a":1}'
    FakeOpenAI.error = RuntimeError("vllm down")
    fake_run["stdout"] = json.dumps({"result": '{"b":2}'})
    with pytest.raises(SchemaViolation):
        only.complete_json("s", "u")
    assert fake_run["calls"] == [], "the fallback was never run"
    assert mc.complete_json("s", "u") == '{"b":2}', "the full client still falls back"
    assert ModelClient(None, ClaudeCodeClient("claude")).local_only() is None
