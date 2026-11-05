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
