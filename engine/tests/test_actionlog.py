import hashlib
import json
import threading

import pytest

from o_engine.actionlog import ActionLog, KINDS, get_log


def _chain_ok(path):
    prev = "0" * 64
    for line in path.read_text().splitlines():
        e = json.loads(line)
        assert e["prev"] == prev
        h = e.pop("hash")
        body = json.dumps(e, sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(body.encode()).hexdigest() == h
        prev = h
    return prev


def test_chain_survives_restart_after_large_entry(tmp_path):
    log = ActionLog(tmp_path)
    log.record("note", "big", blob="x" * 10_000)
    last = log._prev
    log2 = ActionLog.__new__(ActionLog)
    log2.__init__(tmp_path)
    log2.record("note", "after restart")
    assert log2._prev != last
    assert _chain_ok(tmp_path / "engine-actions.jsonl") == log2._prev


def test_get_log_is_one_writer_per_dir(tmp_path):
    a = get_log(tmp_path)
    b = get_log(tmp_path)
    assert a is b
    a.record("task", "one")
    b.record("task", "two")
    _chain_ok(tmp_path / "engine-actions.jsonl")


def test_kind_validated(tmp_path):
    log = ActionLog(tmp_path)
    with pytest.raises(ValueError):
        log.record("bogus", "x")
    assert "remediate" in KINDS


def test_threads_keep_chain_intact(tmp_path):
    log = ActionLog(tmp_path)

    def work(i):
        for j in range(50):
            log.record("tool", f"t{i}-{j}")

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    _chain_ok(tmp_path / "engine-actions.jsonl")
    assert len((tmp_path / "engine-actions.log").read_text().splitlines()) == 400


def test_forwarding_never_blocks_the_recording_thread(tmp_path, monkeypatch):
    """The POST to the app runs on the sender thread; record() returns before (and regardless of) delivery."""
    import threading
    import time

    import httpx

    from o_engine import actionlog

    gate = threading.Event()
    posts: list[tuple[str, dict, str]] = []

    class SlowClient:
        def __init__(self, *a, **k):
            pass

        def post(self, url, json=None, headers=None):
            gate.wait(5.0)
            posts.append((url, json, threading.current_thread().name))

    monkeypatch.setattr(httpx, "Client", SlowClient)
    log = actionlog.ActionLog(tmp_path, forward_url="http://127.0.0.1:1/log", token="t")
    t0 = time.monotonic()
    for i in range(20):
        log.record("note", f"n{i}")
    assert time.monotonic() - t0 < 0.5, "record() waited on the app's socket"
    assert posts == []
    gate.set()
    log.flush(5.0)
    assert len(posts) == 20 and [p[1]["summary"] for p in posts] == [f"n{i}" for i in range(20)], "in order"
    assert all(p[2] == "o-log-forward" for p in posts) and posts[0][0] == "http://127.0.0.1:1/log"
    assert log.dropped == 0
    assert len((tmp_path / "engine-actions.jsonl").read_text().splitlines()) == 20


def test_forward_queue_drops_oldest_when_the_app_is_unreachable(tmp_path, monkeypatch):
    import threading

    import httpx

    from o_engine import actionlog

    gate = threading.Event()

    class Stuck:
        def __init__(self, *a, **k):
            pass

        def post(self, *a, **k):
            gate.wait(5.0)

    monkeypatch.setattr(httpx, "Client", Stuck)
    monkeypatch.setattr(actionlog, "FORWARD_QUEUE_MAX", 5)
    log = actionlog.ActionLog(tmp_path, forward_url="http://127.0.0.1:1/log")
    for i in range(12):
        log.record("note", f"n{i}")
    assert log.dropped >= 1 and log._outbox.qsize() <= 5
    gate.set()
    log.flush(2.0)
    _chain_ok(tmp_path / "engine-actions.jsonl")
    assert "error" in KINDS
