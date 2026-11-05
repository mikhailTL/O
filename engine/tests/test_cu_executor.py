import hashlib
import json

import pytest

from o_engine.actionlog import ActionLog
from o_engine.cu.backends.base import BackendError, CUBackend, Node, Rect, UnavailableBackend, find_in, get_backend, match
from o_engine.cu.backends.fake import FakeBackend
from o_engine.cu.executor import RunResult, ScriptRunner, StepResult
from o_engine.cu.schema import Target, parse_script


def _tree():
    return Node(
        id="win",
        role="frame",
        name="Untitled 1 - LibreOffice Calc",
        states=["visible", "active"],
        rect=Rect(x=0, y=0, w=1280, h=800),
        children=[
            Node(
                id="cell",
                role="table cell",
                name="A1",
                text="",
                states=["visible", "focused"],
                rect=Rect(x=10, y=10, w=60, h=20),
            ),
            Node(id="cell2", role="table cell", name="A2", states=["visible"], rect=Rect(x=10, y=30, w=60, h=20)),
            Node(id="save", role="push button", name="Save", states=["visible"], rect=Rect(x=100, y=5, w=30, h=20)),
            Node(id="chk", role="check box", name="Wrap text", states=["visible"], rect=Rect(x=200, y=5, w=30, h=20)),
            Node(id="go", role="push button", name="Go", text="Go now", states=["visible"]),
        ],
    )


def _script(steps, app="calc"):
    return parse_script({"app": app, "goal": "test goal", "steps": steps})


@pytest.fixture
def log(tmp_path):
    return ActionLog(tmp_path)


def _entries(tmp_path):
    return [json.loads(l) for l in (tmp_path / "engine-actions.jsonl").read_text().splitlines()]


# ---------------------------------------------------------------- base helpers


def test_match_role_glob_text_case_insensitive():
    n = Node(id="x", role="push button", name="Save As", text="Save the document")
    assert match(n, Target(role="push button"))
    assert match(n, Target(name="save*"))
    assert match(n, Target(name="*AS"))
    assert match(n, Target(text="the doc"))
    assert not match(n, Target(role="frame"))
    assert not match(n, Target(name="Open*"))
    assert not match(n, Target(text="nope"))


def test_find_in_index_and_within():
    tree = [_tree()]
    assert find_in(tree, Target(role="table cell")).id == "cell"
    assert find_in(tree, Target(role="table cell", index=1)).id == "cell2"
    assert find_in(tree, Target(role="table cell", index=2)) is None
    scope = find_in(tree, Target(role="frame"))
    assert find_in(tree, Target(role="push button", name="Go"), within=scope).id == "go"
    assert find_in([scope.children[0]], Target(role="push button")) is None


def test_tree_digest_is_sha256_and_stable():
    d1 = CUBackend.tree_digest([_tree()])
    d2 = CUBackend.tree_digest([_tree()])
    assert d1 == d2 and len(d1) == 64
    other = [_tree()]
    assert CUBackend.tree_digest(FakeBackend(other).tree()) == d1
    fb = FakeBackend(_tree())
    fb.set_state("cell", ["visible"])
    assert CUBackend.tree_digest(fb.tree()) != d1


def test_unavailable_backend_reports_reason():
    b = UnavailableBackend("no gi")
    assert b.available is False and "no gi" in b.reason
    assert b.tree() == []
    assert b.find(Target(role="frame")) is None
    with pytest.raises(BackendError):
        b.click(_tree())


def test_get_backend_returns_a_backend_without_optional_packages():
    b = get_backend(display=":99", allow_pixels=False)
    assert isinstance(b, CUBackend)
    assert isinstance(b.available, bool)
    assert b.allow_pixels is False
    assert isinstance(b.name, str) and b.name


# ------------------------------------------------------------------ executor


def test_a11y_hit_resolves_and_logs_two_entries_per_step(log, tmp_path):
    fb = FakeBackend(_tree(), screenshot=b"PNG-1")
    runner = ScriptRunner(fb, log)
    script = _script(
        [
            {"op": "find", "as": "cell", "target": {"role": "table cell", "name": "a1"}},
            {"op": "click", "ref": "cell"},
            {"op": "type", "ref": "cell", "text": "42"},
            {"op": "key", "keys": "ctrl+s"},
            {"op": "set_value", "ref": "cell", "value": "43"},
            {"op": "invoke", "ref": "cell", "action": "press"},
        ]
    )
    res = runner.run(script, "job-1")
    assert isinstance(res, RunResult)
    assert res.ok is True and res.failed_index is None
    assert len(res.steps) == 6
    assert all(isinstance(s, StepResult) and s.ok for s in res.steps)
    assert res.steps[0].resolved_by == "a11y"
    assert res.steps[0].screenshot_sha == hashlib.sha256(b"PNG-1").hexdigest()
    assert len(res.steps[0].before_digest) == 64 and len(res.steps[0].after_digest) == 64
    assert res.steps[0].elapsed_ms >= 0
    assert [a[0] for a in fb.actions] == ["click", "type_text", "key", "set_value", "invoke"]
    assert ("click", "cell") in fb.actions
    assert "pixel_click" not in [a[0] for a in fb.actions]
    entries = _entries(tmp_path)
    assert len(entries) == 12
    assert [e["kind"] for e in entries] == ["tool", "result"] * 6
    assert all("job-1" in e["summary"] and "cu step" in e["summary"] for e in entries)
    r0 = entries[1]["detail"]
    assert r0["before_digest"] == res.steps[0].before_digest
    assert r0["after_digest"] == res.steps[0].after_digest
    assert r0["screenshot_sha"] == res.steps[0].screenshot_sha
    assert r0["resolved_by"] == "a11y"


class _Grounder:
    def __init__(self, bbox):
        self.bbox = bbox
        self.calls = []

    def ground(self, screenshot, target):
        self.calls.append((screenshot, target))
        return self.bbox


def test_miss_then_grounder_hit_resolves_by_vision(log):
    fb = FakeBackend(_tree(), allow_pixels=True)
    g = _Grounder({"x": 500, "y": 400, "w": 20, "h": 10})
    runner = ScriptRunner(fb, log, grounder=g)
    script = _script(
        [
            {"op": "find", "as": "ghost", "target": {"role": "push button", "name": "Ghost"}},
            {"op": "click", "ref": "ghost"},
        ]
    )
    res = runner.run(script, "job-2")
    assert res.ok is True
    assert res.steps[0].resolved_by == "vision"
    assert len(g.calls) == 1 and g.calls[0][1].name == "Ghost"
    assert ("pixel_click", 510, 405) in fb.actions


def test_locator_cache_consulted_before_grounder(log):
    fb = FakeBackend(_tree(), allow_pixels=True, screenshot=b"shot")
    g = _Grounder({"x": 1, "y": 1, "w": 2, "h": 2})

    class Cache:
        def __init__(self):
            self.gets = []
            self.puts = []

        def get(self, app, target, screen_hash):
            self.gets.append((app, target, screen_hash))
            return {"x": 300, "y": 300, "w": 10, "h": 10}

        def put(self, app, target, screen_hash, bbox):
            self.puts.append(bbox)

    cache = Cache()
    runner = ScriptRunner(fb, log, grounder=g, locator_cache=cache)
    res = runner.run(_script([{"op": "find", "as": "g", "target": {"name": "Ghost"}}, {"op": "click", "ref": "g"}]), "j")
    assert res.ok and res.steps[0].resolved_by == "locator"
    assert g.calls == []
    assert cache.gets[0][0] == "calc" and cache.gets[0][2] == hashlib.sha256(b"shot").hexdigest()
    assert ("pixel_click", 305, 305) in fb.actions


def test_vision_hit_is_written_to_cache(log):
    fb = FakeBackend(_tree(), allow_pixels=True)

    class Cache(dict):
        def get(self, app, target, screen_hash):  # noqa: D401
            return None

        def put(self, app, target, screen_hash, bbox):
            self["last"] = bbox

    cache = Cache()
    runner = ScriptRunner(fb, log, grounder=_Grounder({"x": 1, "y": 2, "w": 3, "h": 4}), locator_cache=cache)
    res = runner.run(_script([{"op": "find", "as": "g", "target": {"name": "Ghost"}}]), "j")
    assert res.ok and cache["last"] == {"x": 1, "y": 2, "w": 3, "h": 4}


def test_miss_everywhere_is_structured_failure(log, tmp_path):
    fb = FakeBackend(_tree())
    runner = ScriptRunner(fb, log, grounder=_Grounder(None))
    script = _script(
        [
            {"op": "key", "keys": "ctrl+n"},
            {"op": "find", "as": "ghost", "target": {"role": "push button", "name": "Ghost"}},
            {"op": "click", "ref": "ghost"},
        ]
    )
    res = runner.run(script, "job-3")
    assert res.ok is False
    assert res.failed_index == 1
    assert len(res.steps) == 2  # stops at the failed step
    assert res.steps[1].ok is False and res.steps[1].resolved_by is None
    assert "Ghost" in res.steps[1].error
    assert len(_entries(tmp_path)) == 4


def test_pixel_fallback_gated_by_allow_pixels(log):
    fb = FakeBackend(_tree(), allow_pixels=False)
    runner = ScriptRunner(fb, log, grounder=_Grounder({"x": 1, "y": 1, "w": 2, "h": 2}))
    res = runner.run(_script([{"op": "find", "as": "g", "target": {"name": "Ghost"}}, {"op": "click", "ref": "g"}]), "j")
    assert res.steps[0].ok and res.steps[0].resolved_by == "vision"
    assert res.ok is False and res.failed_index == 1
    assert "pixel" in res.steps[1].error.lower()
    assert all(a[0] != "pixel_click" for a in fb.actions)


def test_backend_exception_becomes_failed_step(log):
    class Boom(FakeBackend):
        def key(self, keys):
            raise RuntimeError("keyboard on fire")

    runner = ScriptRunner(Boom(_tree()), log)
    res = runner.run(_script([{"op": "key", "keys": "a"}, {"op": "key", "keys": "b"}]), "j")
    assert res.ok is False and res.failed_index == 0
    assert "keyboard on fire" in res.steps[0].error


def test_wait_times_out_without_real_sleep(log):
    fb = FakeBackend(_tree())
    clock = {"t": 0.0}
    sleeps = []

    def now():
        return clock["t"]

    def sleep(s):
        sleeps.append(s)
        clock["t"] += s

    runner = ScriptRunner(fb, log, clock=now, sleep=sleep, poll_interval=0.5)
    res = runner.run(_script([{"op": "wait", "for": {"role": "dialog", "name": "Save*"}, "timeout_s": 3}]), "j")
    assert res.ok is False and res.failed_index == 0
    assert "timed out" in res.steps[0].error
    assert sleeps and sum(sleeps) >= 3


def test_wait_succeeds_when_node_appears(log):
    fb = FakeBackend(_tree())
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(s):
        clock["t"] += s
        if clock["t"] >= 1.0:
            fb.add_node("win", Node(id="dlg", role="dialog", name="Save As", states=["visible"]))

    runner = ScriptRunner(fb, log, clock=now, sleep=sleep, poll_interval=0.5)
    res = runner.run(
        _script(
            [
                {"op": "wait", "for": {"role": "dialog", "name": "Save*"}, "timeout_s": 10},
                {"op": "assert", "target": {"role": "dialog", "name": "Save As", "state": "visible"}},
            ]
        ),
        "j",
    )
    assert res.ok is True
    assert res.steps[0].resolved_by == "a11y"
    assert res.steps[0].before_digest != res.steps[0].after_digest


def test_assert_pass_and_fail(log):
    fb = FakeBackend(_tree())
    runner = ScriptRunner(fb, log)
    ok = runner.run(_script([{"op": "assert", "target": {"role": "table cell", "name": "A1", "state": "focused"}}]), "j")
    assert ok.ok is True
    fb.set_state("cell", ["visible"])
    bad = runner.run(_script([{"op": "assert", "target": {"role": "table cell", "name": "A1", "state": "focused"}}]), "j")
    assert bad.ok is False and bad.failed_index == 0 and "focused" in bad.steps[0].error
    missing = runner.run(_script([{"op": "assert", "target": {"role": "dialog", "state": "visible"}}]), "j")
    assert missing.ok is False and "not found" in missing.steps[0].error


def test_assert_within_ref_scope(log):
    fb = FakeBackend(_tree())
    runner = ScriptRunner(fb, log)
    res = runner.run(
        _script(
            [
                {"op": "find", "as": "win", "target": {"role": "frame"}},
                {"op": "assert", "target": {"role": "check box", "within": "win", "state": "checked"}},
            ]
        ),
        "j",
    )
    assert res.ok is False and "checked" in res.steps[1].error
    fb.set_state("chk", ["visible", "checked"])
    res = runner.run(
        _script(
            [
                {"op": "find", "as": "win", "target": {"role": "frame"}},
                {"op": "assert", "target": {"role": "check box", "within": "win", "state": "checked"}},
            ]
        ),
        "j",
    )
    assert res.ok is True


def test_screenshot_assert_with_and_without_judge(log, tmp_path):
    fb = FakeBackend(_tree(), screenshot=b"IMG")

    class Judge:
        def __init__(self, verdict):
            self.verdict = verdict
            self.calls = []

        def ask(self, screenshot, question):
            self.calls.append((screenshot, question))
            return self.verdict

    j = Judge((True, "looks saved"))
    res = ScriptRunner(fb, log, judge=j).run(_script([{"op": "screenshot_assert", "question": "Saved?"}]), "j")
    assert res.ok is True and j.calls == [(b"IMG", "Saved?")]
    res = ScriptRunner(fb, log, judge=Judge((False, "dialog still open"))).run(
        _script([{"op": "screenshot_assert", "question": "Saved?"}]), "j"
    )
    assert res.ok is False and "dialog still open" in res.steps[0].error
    res = ScriptRunner(fb, log, judge=Judge(False)).run(_script([{"op": "screenshot_assert", "question": "Saved?"}]), "j")
    assert res.ok is False
    # no judge: skipped with a note, still passes, still exactly two log entries
    before = len(_entries(tmp_path))
    res = ScriptRunner(fb, log).run(_script([{"op": "screenshot_assert", "question": "Saved?"}]), "j")
    assert res.ok is True and res.steps[0].note and "skipped" in res.steps[0].note
    assert len(_entries(tmp_path)) - before == 2


def test_launch_uses_backend_display_and_does_not_block(log):
    fb = FakeBackend(_tree(), display=":77")
    spawned = []

    class P:
        pid = 4242

        def poll(self):
            return None

    def popen(args, **kw):
        spawned.append((args, kw))
        return P()

    runner = ScriptRunner(fb, log, popen=popen)
    res = runner.run(_script([{"op": "launch", "cmd": "libreoffice --calc"}]), "j")
    assert res.ok is True
    args, kw = spawned[0]
    assert args == ["libreoffice", "--calc"]
    assert kw["env"]["DISPLAY"] == ":77"
    assert kw.get("start_new_session") is True
    assert res.steps[0].note and "4242" in res.steps[0].note


def test_run_never_raises_even_if_backend_screenshot_fails(log):
    class Bad(FakeBackend):
        def screenshot(self):
            raise OSError("no display")

    res = ScriptRunner(Bad(_tree()), log).run(_script([{"op": "key", "keys": "a"}]), "j")
    assert res.ok is False and res.failed_index == 0 and "no display" in res.steps[0].error


def test_fake_backend_set_value_mutates_tree_text():
    fb = FakeBackend(_tree())
    cell = fb.find(Target(role="table cell", name="A1"))
    fb.set_value(cell, "99")
    assert fb.find(Target(role="table cell", name="A1")).text == "99"
    fb.type_text(cell, "x")
    assert fb.find(Target(role="table cell", name="A1")).text == "99x"
    fb.invoke(fb.find(Target(name="Wrap text")), "click")
    assert "checked" in fb.find(Target(name="Wrap text")).states


# --------------------------------------------------------------- loop breaker


def _repeat(step, n):
    return [dict(step) for _ in range(n)]


def test_loop_breaker_stops_at_third_identical_pair(log, tmp_path):
    fb = FakeBackend(_tree())  # a `key` never changes the fake tree
    runner = ScriptRunner(fb, log)
    res = runner.run(_script(_repeat({"op": "key", "keys": "ctrl+z"}, 10)), "job-loop")
    assert res.ok is False and res.failed_index == 2
    assert len(res.steps) == 3
    assert [s.status for s in res.steps] == ["ok", "ok", "loop"]
    assert res.steps[0].reason is None and res.steps[0].evidence == ()
    bad = res.steps[2]
    assert bad.ok is False and bad.reason == "loop" and bad.error.startswith("loop:")
    assert [e["index"] for e in bad.evidence] == [0, 1, 2]
    assert all("ctrl+z" in e["step"] and e["op"] == "key" for e in bad.evidence)
    assert len({e["screen_sha"] for e in bad.evidence}) == 1
    # the backend was asked to act exactly twice: the loop is caught *before* the third repeat
    assert fb.actions == [("key", "ctrl+z"), ("key", "ctrl+z")]
    entries = _entries(tmp_path)
    assert len(entries) == 6
    assert [e["detail"]["status"] for e in entries if e["kind"] == "result"] == ["ok", "ok", "loop"]
    assert entries[5]["detail"]["reason"] == "loop"
    assert [e["index"] for e in entries[5]["detail"]["evidence"]] == [0, 1, 2]
    assert runner.loop_evidence() == list(bad.evidence)


def test_loop_breaker_evidence_is_empty_without_a_loop(log):
    runner = ScriptRunner(FakeBackend(_tree()), log)
    assert runner.loop_evidence() == []
    res = runner.run(_script([{"op": "key", "keys": "a"}, {"op": "key", "keys": "b"}]), "j")
    assert res.ok is True and runner.loop_evidence() == []


def test_loop_breaker_ignores_differing_steps(log):
    fb = FakeBackend(_tree())
    runner = ScriptRunner(fb, log)
    steps = [{"op": "key", "keys": f"f{i}"} for i in range(1, 11)]
    res = runner.run(_script(steps), "j")
    assert res.ok is True and len(res.steps) == 10
    assert all(s.status == "ok" for s in res.steps)


def test_loop_breaker_ignores_repeats_when_the_screen_changes(log):
    fb = FakeBackend(_tree())
    # `type` into A1 appends text, so the tree is different before every step
    res = ScriptRunner(fb, log).run(
        _script([{"op": "find", "as": "cell", "target": {"role": "table cell", "name": "A1"}}] + _repeat({"op": "type", "ref": "cell", "text": "x"}, 9)),
        "j",
    )
    assert res.ok is True and len(res.steps) == 10
    assert fb.find(Target(name="A1")).text == "x" * 9


def test_loop_breaker_threshold_and_window_are_configurable(log):
    script = _script(_repeat({"op": "key", "keys": "ctrl+z"}, 10))
    res = ScriptRunner(FakeBackend(_tree()), log, loop_threshold=5).run(script, "j")
    assert res.ok is False and res.failed_index == 4 and res.steps[4].status == "loop"
    assert [e["index"] for e in res.steps[4].evidence] == [0, 1, 2, 3, 4]
    # threshold 2: the second occurrence already trips
    res = ScriptRunner(FakeBackend(_tree()), log, loop_threshold=2).run(script, "j")
    assert res.ok is False and res.failed_index == 1
    # the window is a ring: repeats spaced wider than the window never accumulate.
    # ctrl+z sits at steps 0, 5, 10, ...; two earlier repeats are only in view with a window >= 11.
    spaced = []
    for i in range(12):
        spaced.append({"op": "key", "keys": "ctrl+z"})
        spaced.extend({"op": "key", "keys": f"f{i}-{k}"} for k in range(4))
    res = ScriptRunner(FakeBackend(_tree()), log, loop_window=8).run(_script(spaced), "j")
    assert res.ok is True and len(res.steps) == 60
    res = ScriptRunner(FakeBackend(_tree()), log, loop_window=11).run(_script(spaced), "j")
    assert res.ok is False and res.failed_index == 10 and res.steps[10].status == "loop"
    assert [e["index"] for e in res.steps[10].evidence] == [0, 5, 10]
    with pytest.raises(ValueError):
        ScriptRunner(FakeBackend(_tree()), log, loop_threshold=1)
    with pytest.raises(ValueError):
        ScriptRunner(FakeBackend(_tree()), log, loop_threshold=4, loop_window=3)


def test_loop_breaker_ring_resets_between_runs(log):
    runner = ScriptRunner(FakeBackend(_tree()), log, loop_threshold=3)
    two = _script(_repeat({"op": "key", "keys": "ctrl+z"}, 2))
    assert runner.run(two, "j").ok is True
    assert runner.run(two, "j").ok is True  # a fresh run does not inherit the previous ring


def test_loop_breaker_uses_screenshot_only_on_an_isolated_display(log):
    # host display (allow_pixels=False): a changing screenshot with a static tree still counts as a loop
    n = {"i": 0}

    class Flicker(FakeBackend):
        def screenshot(self):
            n["i"] += 1
            return b"shot-%d" % n["i"]

    res = ScriptRunner(Flicker(_tree(), allow_pixels=False), log).run(_script(_repeat({"op": "key", "keys": "a"}, 5)), "j")
    assert res.ok is False and res.failed_index == 2
    # isolated display (allow_pixels=True): the screenshot is part of the screen identity
    res = ScriptRunner(Flicker(_tree(), allow_pixels=True), log).run(_script(_repeat({"op": "key", "keys": "a"}, 5)), "j")
    assert res.ok is True
    res = ScriptRunner(FakeBackend(_tree(), allow_pixels=True), log).run(_script(_repeat({"op": "key", "keys": "a"}, 5)), "j")
    assert res.ok is False and res.failed_index == 2


# ---------------------------------------------------------------- wave 2: state_assert / script / call dispatch

from o_engine.cu.executor import _describe  # noqa: E402
from o_engine.cu.judge import StateJudge  # noqa: E402
from o_engine.executor import Sandbox, StepResult as ShellResult  # noqa: E402


class _CountingStateJudge(StateJudge):
    def __init__(self, sandbox, backend):
        super().__init__(sandbox, backend)
        self.verify_calls = []
        self.snapshot_calls = []

    def snapshot(self, paths):
        self.snapshot_calls.append(list(paths))
        return super().snapshot(paths)

    def verify(self, checks, job_id, baseline=None):
        self.verify_calls.append((job_id, dict(baseline or {})))
        return super().verify(checks, job_id, baseline)


def test_state_assert_delegates_to_state_judge_with_pre_step_baseline(log, tmp_path):
    root = tmp_path / "sb"
    sandbox = Sandbox(root)
    (root / "report.csv").write_text("v1")
    fb = FakeBackend(_tree())
    sj = _CountingStateJudge(sandbox, fb)
    # the `type` step is what changes the file: patch the fake to rewrite it on type_text
    original = fb.type_text

    def type_and_save(node, text):
        original(node, text)
        (root / "report.csv").write_text("v2:" + text)

    fb.type_text = type_and_save
    script = _script([
        {"op": "find", "as": "cell", "target": {"role": "table cell", "name": "A1"}},
        {"op": "type", "ref": "cell", "text": "42"},
        {"op": "state_assert", "checks": [
            {"kind": "file_sha_changed", "arg": "report.csv"},
            {"kind": "file_contains", "arg": "report.csv", "expect": "42"},
            {"kind": "tree", "arg": "A1"},
        ]},
    ])
    runner = ScriptRunner(fb, log, state_judge=sj)
    res = runner.run(script, "sa")
    assert res.ok is True, res.steps[-1].error
    assert sj.snapshot_calls[0] == ["report.csv"], "baseline taken once at run start, before any step"
    assert len(sj.verify_calls) == 1 and sj.verify_calls[0][0] == "sa"
    assert sj.verify_calls[0][1]["report.csv"] == hashlib.sha256(b"v1").hexdigest()
    assert res.steps[2].note.startswith("state: 3 state check(s) hold")
    assert runner.baseline(2)["report.csv"] == hashlib.sha256(b"v1").hexdigest()
    assert runner.baseline()["report.csv"] == hashlib.sha256(b"v2:42").hexdigest(), "a passed assert refreshes the baseline"
    assert sj.model_calls == 0
    entries = _entries(tmp_path)
    assert "3 check(s): file_sha_changed 'report.csv'" in entries[4]["summary"]

    # verdict flips when the file does not change: structured failure, never an exception
    fb.type_text = original
    res = ScriptRunner(fb, log, state_judge=sj).run(script, "sa2")
    assert res.ok is False and res.failed_index == 2 and "state_assert failed" in res.steps[2].error
    assert "unchanged since baseline" in res.steps[2].error


def test_state_assert_without_a_judge_fails_closed(log):
    fb = FakeBackend(_tree())
    res = ScriptRunner(fb, log).run(_script([{"op": "state_assert", "checks": [{"kind": "file_exists", "arg": "x"}]}]), "s")
    assert res.ok is False and "no state judge configured" in res.steps[0].error


class _SpyRunner:
    def __init__(self, exit_code=0, timed_out=False):
        self.calls = []
        self.exit_code = exit_code
        self.timed_out = timed_out

    def run(self, payload, sandbox, index=0, env_extra=None, isolated_display=False):
        self.calls.append({"payload": payload, "sandbox": sandbox, "index": index,
                           "env_extra": dict(env_extra) if env_extra else env_extra, "isolated_display": isolated_display})
        return ShellResult(index=index, command="python3 x.py", cwd=".", exit_code=self.exit_code, stdout="hello\n",
                           stderr="" if self.exit_code == 0 else "boom\n", elapsed_ms=1, timed_out=self.timed_out)


def _script_step(code="print(1)"):
    return {"op": "script", "script": {"language": "python", "code": code, "target_directory": ".", "timeout_seconds": 5}}


def test_script_step_goes_to_runner_with_isolated_display_never_host(log, tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    sandbox = Sandbox(tmp_path / "sb")
    fb = FakeBackend(_tree(), display=":99")
    spy = _SpyRunner()
    res = ScriptRunner(fb, log, runner=spy, sandbox=sandbox).run(_script([{"op": "key", "keys": "a"}, _script_step()]), "sc")
    assert res.ok is True and len(spy.calls) == 1
    call = spy.calls[0]
    assert call["env_extra"] == {"DISPLAY": ":99"} and call["isolated_display"] is False
    assert call["index"] == 1 and call["sandbox"] is sandbox and call["payload"].language == "python"
    assert res.steps[1].note == "script ok (python, display :99): hello"
    entries = _entries(tmp_path)
    tool = [e for e in entries if e["kind"] == "tool" and e["detail"]["op"] == "script"]
    result = [e for e in entries if e["kind"] == "result" and e["detail"]["op"] == "script"]
    assert len(tool) == 1 and len(result) == 1 and result[0]["detail"]["ok"] is True
    assert "python 8 chars in ." in tool[0]["summary"]

    # an explicit display object wins over the backend's; no display at all -> no DISPLAY handed over
    class Disp:
        name = ":101"

    spy2 = _SpyRunner()
    ScriptRunner(fb, log, runner=spy2, sandbox=sandbox, display=Disp()).run(_script([_script_step()]), "sc2")
    assert spy2.calls[0]["env_extra"] == {"DISPLAY": ":101"}
    spy3 = _SpyRunner()
    ScriptRunner(FakeBackend(_tree()), log, runner=spy3, sandbox=sandbox).run(_script([_script_step()]), "sc3")
    assert spy3.calls[0]["env_extra"] is None and spy3.calls[0]["isolated_display"] is False

    # the host display is refused outright
    spy4 = _SpyRunner()
    res = ScriptRunner(FakeBackend(_tree(), display=":0"), log, runner=spy4, sandbox=sandbox).run(_script([_script_step()]), "sc4")
    assert res.ok is False and "host display" in res.steps[0].error and spy4.calls == []


def test_script_step_failures_are_structured(log, tmp_path):
    sandbox = Sandbox(tmp_path / "sb")
    fb = FakeBackend(_tree())
    res = ScriptRunner(fb, log, runner=_SpyRunner(exit_code=2), sandbox=sandbox).run(_script([_script_step()]), "f1")
    assert res.ok is False and res.steps[0].error == "script exited 2: boom"
    res = ScriptRunner(fb, log, runner=_SpyRunner(timed_out=True), sandbox=sandbox).run(_script([_script_step()]), "f2")
    assert res.ok is False and "timed out" in res.steps[0].error
    res = ScriptRunner(fb, log, sandbox=sandbox).run(_script([_script_step()]), "f3")
    assert res.ok is False and "no runner configured" in res.steps[0].error
    res = ScriptRunner(fb, log, runner=_SpyRunner()).run(_script([_script_step()]), "f4")
    assert res.ok is False and "no sandbox configured" in res.steps[0].error


class _Store:
    def __init__(self, source, names=None):
        self.source = source
        self.names = names

    def api_source(self, app, version=None):
        return self.source

    def read(self, app, version=None):
        if self.names is None:
            return None
        return {"functions": [{"name": n} for n in self.names]}


API = (
    "def set_cell(cell, value):\n"
    "    node = backend.find({'role': 'table cell', 'name': cell})\n"
    "    backend.set_value(node, value)\n"
    "    return 'set ' + cell\n"
    "\n"
    "def toggle_wrap():\n"
    "    backend.invoke(backend.find(Target(role='check box', name='Wrap text')), 'toggle')\n"
)


def test_call_step_runs_generated_api_against_backend(log, tmp_path):
    fb = FakeBackend(_tree())
    runner = ScriptRunner(fb, log, interface_store=_Store(API, names=["set_cell", "toggle_wrap"]))
    res = runner.run(_script([
        {"op": "call", "fn": "calc.set_cell", "args": {"cell": "A2", "value": "7"}},
        {"op": "call", "fn": "calc.toggle_wrap"},
    ]), "c")
    assert res.ok is True, res.steps[res.failed_index].error if res.failed_index is not None else ""
    assert fb.actions == [("set_value", "cell2", "7"), ("invoke", "chk", "toggle")]
    assert res.steps[0].note == "call calc.set_cell -> set A2" and res.steps[1].note == "call calc.toggle_wrap ok"
    assert "checked" in fb.find(Target(role="check box", name="Wrap text")).states
    entries = _entries(tmp_path)
    assert "calc.set_cell(cell, value)" in entries[0]["summary"]


def test_call_step_unknown_function_or_store_is_structured_failure(log):
    fb = FakeBackend(_tree())
    res = ScriptRunner(fb, log).run(_script([{"op": "call", "fn": "calc.set_cell", "args": {}}]), "c1")
    assert res.ok is False and res.steps[0].error.startswith("unknown function 'calc.set_cell'")
    res = ScriptRunner(fb, log, interface_store=_Store(None)).run(_script([{"op": "call", "fn": "calc.set_cell"}]), "c2")
    assert res.ok is False and "no interface for app" in res.steps[0].error
    res = ScriptRunner(fb, log, interface_store=_Store(API, names=["set_cell"])).run(_script([{"op": "call", "fn": "calc.nope"}]), "c3")
    assert res.ok is False and "not in api.json" in res.steps[0].error
    res = ScriptRunner(fb, log, interface_store=_Store(API)).run(_script([{"op": "call", "fn": "calc.nope"}]), "c4")
    assert res.ok is False and res.steps[0].error == "unknown function 'calc.nope'"
    bad = ScriptRunner(fb, log, interface_store=_Store("import os\ndef f():\n    return 1\n"))
    res = bad.run(_script([{"op": "call", "fn": "calc.f"}]), "c5")
    assert res.ok is False and "forbidden fragments" in res.steps[0].error and fb.actions == []
    raising = ScriptRunner(fb, log, interface_store=_Store("def f(x):\n    raise ValueError('bad ' + x)\n"))
    res = raising.run(_script([{"op": "call", "fn": "calc.f", "args": {"x": "arg"}}]), "c6")
    assert res.ok is False and res.steps[0].error == "call 'calc.f' raised ValueError: bad arg"


def test_call_step_passes_backend_first_to_a_real_generated_api(log, tmp_path):
    """I2: InterfaceBuilder emits ``(backend, ...)`` signatures; the executor must supply ``backend``."""
    from o_engine.cu.interfaces import InterfaceStore

    store = InterfaceStore(tmp_path / "ifaces")
    store.write("calc", [{
        "name": "set_cell",
        "description": "Type value into the named cell.",
        "signature": "(backend, cell: str, value: str) -> None",
        "body": "node = backend.find({'role': 'table cell', 'name': cell})\nif node is None:\n    raise RuntimeError('no cell ' + cell)\nbackend.set_value(node, value)",
    }])
    assert "def set_cell(backend, cell: str, value: str) -> None:" in store.api_source("calc")
    fb = FakeBackend(_tree())
    res = ScriptRunner(fb, log, interface_store=store).run(
        _script([{"op": "call", "fn": "calc.set_cell", "args": {"cell": "A1", "value": "42"}}]), "real")
    assert res.ok is True, res.steps[0].error
    assert fb.actions == [("set_value", "cell", "42")]
    assert fb.find(Target(role="table cell", name="A1")).text == "42"
    assert res.steps[0].note == "call calc.set_cell ok"


def test_call_step_rejects_ast_escapes_before_running(log, tmp_path):
    from o_engine.cu.interfaces import InterfaceStore

    store = InterfaceStore(tmp_path / "ifaces")
    store.write("calc", [{
        "name": "set_cell", "description": "d", "signature": "(backend, cell, value)",
        "body": "os = __import__(\"os\")\nbackend.set_value(backend.find({'name': cell}), value)",
    }])
    fb = FakeBackend(_tree())
    res = ScriptRunner(fb, log, interface_store=store).run(
        _script([{"op": "call", "fn": "calc.set_cell", "args": {"cell": "A1", "value": "42"}}]), "esc")
    assert res.ok is False and "forbidden" in res.steps[0].error and "__import__" in res.steps[0].error
    assert fb.actions == []
    # a backend method outside the allow-list is caught the same way (no substring matches it)
    store2 = InterfaceStore(tmp_path / "ifaces2")
    store2.write("calc", [{"name": "f", "description": "d", "signature": "(backend)", "body": "backend.click(1, 2)"}])
    res = ScriptRunner(fb, log, interface_store=store2).run(_script([{"op": "call", "fn": "calc.f"}]), "esc2")
    assert res.ok is False and "forbidden" in res.steps[0].error and "backend.click" in res.steps[0].error
    assert fb.actions == []


def test_call_step_unknown_arg_key_is_structured_failure(log, tmp_path):
    from o_engine.cu.interfaces import InterfaceStore

    store = InterfaceStore(tmp_path / "ifaces")
    store.write("calc", [{"name": "set_cell", "description": "d", "signature": "(backend, cell, value)",
                          "body": "backend.set_value(backend.find({'name': cell}), value)"}])
    fb = FakeBackend(_tree())
    res = ScriptRunner(fb, log, interface_store=store).run(
        _script([{"op": "call", "fn": "calc.set_cell", "args": {"cell": "A1", "val": "42"}}]), "args")
    assert res.ok is False and fb.actions == []
    err = res.steps[0].error
    assert err.startswith("call 'calc.set_cell': bad arguments")
    assert "val" in err and "(cell, value)" in err


def test_describe_covers_every_op():
    s = _script([
        {"op": "state_assert", "checks": [{"kind": "file_exists", "arg": "a.txt"}, {"kind": "shell_true", "arg": "test -f b"}]},
        _script_step("print('x')"),
        {"op": "call", "fn": "calc.set_cell", "args": {"cell": "A1", "value": "1"}},
    ])
    assert _describe(s.steps[0]) == "2 check(s): file_exists 'a.txt', shell_true 'test -f b'"
    assert _describe(s.steps[1]) == "python 10 chars in ."
    assert _describe(s.steps[2]) == "calc.set_cell(cell, value)"
