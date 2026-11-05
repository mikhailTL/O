"""GenUI (Phase 3 / H1): frame schema, oui round trip, the store, the job emitter, graph hooks, routes."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from o_engine import genui
from o_engine.actionlog import ActionLog
from o_engine.genui import (
    Begin, Data, End, JobSurface, Sandbox, UIStore, Update, compile, decode, frame_to_json, oui, parse_frame,
)
from o_engine.schema import SchemaViolation
from o_engine.server import EngineSettings, create_app

TOKEN = "t"
H = {"x-o-token": TOKEN}


def _settings(tmp: Path, **overrides) -> EngineSettings:
    values = dict(
        token=TOKEN, data_dir=str(tmp / "data"), sandbox_root=str(tmp / "sb"),
        screenpipe_db=str(tmp / "missing.db"), vllm_url="http://127.0.0.1:1/v1", claude_path="/nonexistent/claude",
        max_attempts=1, permission_mode="log",
    )
    values.update(overrides)
    return EngineSettings.from_env(**values)


def _update(*entries) -> dict:
    return {"a2ui": "1", "type": "update", "surface": "job:1", "components": list(entries)}


ALL_COMPONENTS = _update(
    {"id": "root", "component": {"Column": {"children": ["row", "card", "tree", "prog", "kv", "btn", "pv"]}}},
    {"id": "row", "component": {"Row": {"children": ["t", "s0"]}}},
    {"id": "card", "component": {"Card": {"title": "semi; and ] bracket", "children": ["t"]}}},
    {"id": "t", "component": {"Text": {"text": "quoted \"text\" with ; and ]", "tone": "error"}}},
    {"id": "s0", "component": {"Step": {"label": "npm install", "status": "running", "detail": "exit 0; ok]", "progress": 0.4}}},
    {"id": "tree", "component": {"Tree": {"nodes": [
        {"id": "a", "label": "A", "status": "ok", "children": [{"id": "b", "label": "B ]; x", "status": "running", "children": []}]},
    ]}}},
    {"id": "prog", "component": {"Progress": {"value": 0.0, "label": "zero"}}},
    {"id": "kv", "component": {"KeyValue": {"items": [["k", "v;]"], ["k2", "plain"]]}}},
    {"id": "btn", "component": {"Button": {"label": "Retry", "action": "retry", "payload": {"job": "1", "n": [1, 2, {"z": None}]}}}},
    {"id": "pv", "component": {"Preview": {"sandbox_id": "sb-1", "height": 120}}},
)


# ---------------------------------------------------------------- schema

def test_unknown_component_rejected():
    with pytest.raises(SchemaViolation, match="unknown component 'Blink'"):
        parse_frame(_update({"id": "x", "component": {"Blink": {"text": "no"}}}))


def test_unknown_property_rejected():
    with pytest.raises(SchemaViolation, match="Extra inputs"):
        parse_frame(_update({"id": "x", "component": {"Text": {"text": "a", "onclick": "x"}}}))


def test_unknown_button_action_rejected():
    with pytest.raises(SchemaViolation, match="action"):
        parse_frame(_update({"id": "x", "component": {"Button": {"label": "a", "action": "shell"}}}))
    frame = parse_frame(_update({"id": "x", "component": {"Button": {"label": "a", "action": "approve"}}}))
    assert frame.components[0].component.action == "approve"


def test_frame_level_strictness():
    with pytest.raises(SchemaViolation):
        parse_frame({"a2ui": "2", "type": "begin", "surface": "job:1", "title": "t"})
    with pytest.raises(SchemaViolation):
        parse_frame({"a2ui": "1", "type": "blink", "surface": "job:1"})
    with pytest.raises(SchemaViolation):
        parse_frame({"a2ui": "1", "type": "end", "surface": "job:1", "ok": "yes"})  # strict bool
    with pytest.raises(SchemaViolation):
        parse_frame({"a2ui": "1", "type": "begin", "surface": "job 1", "title": "t"})  # surface token
    with pytest.raises(SchemaViolation):
        parse_frame("not json")
    with pytest.raises(SchemaViolation, match="surface"):
        parse_frame({"a2ui": "1", "type": "begin", "surface": "evil:s1", "title": "t"})  # prefix enforced
    assert parse_frame(json.dumps({"a2ui": "1", "type": "end", "surface": "chat:s", "ok": False})).ok is False


def test_wire_dump_matches_contract():
    frame = parse_frame(ALL_COMPONENTS)
    wire = json.loads(frame_to_json(frame))
    assert wire["a2ui"] == "1" and wire["type"] == "update" and wire["surface"] == "job:1"
    assert wire["components"][0] == {"id": "root", "component": {"Column": {"children": ["row", "card", "tree", "prog", "kv", "btn", "pv"]}}}
    assert wire["components"][4]["component"] == {"Step": {"label": "npm install", "status": "running", "detail": "exit 0; ok]", "progress": 0.4}}
    bare = json.loads(frame_to_json(parse_frame(_update({"id": "s", "component": {"Step": {"label": "x"}}}))))
    assert bare["components"][0]["component"] == {"Step": {"label": "x", "status": "pending", "detail": ""}}  # no null progress
    assert parse_frame(wire) == frame


# ---------------------------------------------------------------- oui

def test_oui_round_trip_all_components_exact():
    frames = [
        parse_frame({"a2ui": "1", "type": "begin", "surface": "job:1", "title": "Install deps; really]"}),
        parse_frame(ALL_COMPONENTS),
        parse_frame({"a2ui": "1", "type": "data", "surface": "job:1", "path": "steps.0.status", "value": "ok"}),
        parse_frame({"a2ui": "1", "type": "data", "surface": "job:1", "path": "steps.0.progress", "value": 0.75}),
        parse_frame({"a2ui": "1", "type": "data", "surface": "job:1", "path": "meta", "value": {"a": [1, "x"], "b": None}}),
        parse_frame({"a2ui": "1", "type": "sandbox", "surface": "job:1", "sandbox_id": "sb-1",
                     "html": "<div class=\"card\">x ; ] }</div>\n", "css": "body{}", "js": ""}),
        parse_frame({"a2ui": "1", "type": "end", "surface": "job:1", "ok": True}),
    ]
    text = compile(frames)
    assert decode(text) == frames
    assert compile(decode(text)) == text
    lines = text.splitlines()
    assert lines[0] == '@begin job:1 "Install deps; really]"'
    assert lines[1] == "@update job:1"
    assert "Col#root{row card tree prog kv btn pv}" in lines
    assert 'Step#s0[l="npm install";st=run;d="exit 0; ok]";p=0.4]' in lines
    assert "Prev#pv[s=sb-1;h=120]" in lines
    assert "@end job:1 true" == lines[-1]
    assert oui.decode(oui.compile(frames)) == frames


def test_oui_defaults_omitted_and_restored():
    frame = parse_frame(_update({"id": "s", "component": {"Step": {"label": "x"}}},
                                {"id": "c", "component": {"Column": {}}}))
    text = compile([frame])
    assert "Step#s[l=x]" in text and "Col#c" in text
    assert decode(text) == [frame]


def test_oui_rejects_unknown_component_and_garbage():
    with pytest.raises(SchemaViolation, match="unknown component"):
        decode("@update job:1\nBlink#x[t=a]\n")
    with pytest.raises(SchemaViolation, match="unknown property"):
        decode("@update job:1\nTxt#x[zz=a]\n")
    with pytest.raises(SchemaViolation):
        decode("Txt#x[t=a]\n")  # component line outside @update
    with pytest.raises(SchemaViolation):
        decode("@update job:1\nBtn#b[l=a;a=shell]\n")
    with pytest.raises(SchemaViolation):
        decode("@end job:1 maybe\n")


# ---------------------------------------------------------------- store

def test_uistore_ring_buffer_and_subscribe():
    store = UIStore(maxlen=3)
    q = store.subscribe("job:a")
    for i in range(5):
        store.append(Data(surface="job:a", path="n", value=i))
    store.append(Data(surface="job:b", path="n", value=0))
    assert [f.value for f in store.frames("job:a")] == [2, 3, 4]
    assert [s for s, _ in store.entries("job:a")] == [3, 4, 5]
    assert store.frames("job:a", since=4)[0].value == 4
    assert store.last_seq("job:a") == 5
    assert sorted(store.surfaces()) == ["job:a", "job:b"]
    assert q.qsize() == 5 and q.get()[0] == 1
    store.unsubscribe("job:a", q)
    store.append(End(surface="job:a", ok=True))
    assert q.qsize() == 4 and store.ended("job:a") is True
    with pytest.raises(SchemaViolation):
        store.append({"a2ui": "1", "type": "nope", "surface": "job:a"})


class _Capture(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n))
        _Capture.received.append((self.headers.get("x-o-token"), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_):
        pass


def test_uistore_forwards_frames_to_app_in_order():
    _Capture.received = []
    srv = HTTPServer(("127.0.0.1", 0), _Capture)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        store = UIStore(forward_url=f"http://127.0.0.1:{srv.server_port}/ui", token="tok")
        store.append(Begin(surface="job:f", title="fwd"))
        store.append(Data(surface="job:f", path="steps.0.status", value="running"))
        store.append(End(surface="job:f", ok=True))
        store.flush()
    finally:
        srv.shutdown()
    assert [tok for tok, _ in _Capture.received] == ["tok", "tok", "tok"]
    assert [b["frames"][0]["type"] for _, b in _Capture.received] == ["begin", "data", "end"]
    assert _Capture.received[0][1] == {"frames": [{"a2ui": "1", "type": "begin", "surface": "job:f", "title": "fwd"}]}


def test_uistore_forward_failure_is_swallowed_and_fast():
    store = UIStore(forward_url="http://127.0.0.1:1/ui", token="tok")
    t0 = time.monotonic()
    store.append(Begin(surface="job:x", title="x"))
    assert time.monotonic() - t0 < 0.1  # the job thread never waits on the forward
    store.flush()
    assert len(store.frames("job:x")) == 1
    assert UIStore().forward_url is None


# ---------------------------------------------------------------- emitter

def test_jobsurface_emits_begin_update_data_end():
    store = UIStore()
    s = JobSurface(store, "j1", "echo hi", phases=["context", "execute", "report"])
    s.begin()
    s.begin()  # idempotent
    s.phase("context", 0)
    s.phase("execute", 0)
    s.step(0, "echo hi", "running")
    s.step(0, "echo hi", "ok", "exit 0")
    s.preview("verify-j1", "<div>ok</div>", "body{}", "", height=64)
    s.end(True)
    s.end(True)  # idempotent
    frames = store.frames("job:j1")
    types = [f.type for f in frames]
    assert types[0] == "begin" and types[-1] == "end" and frames[0].title == "echo hi"
    assert types.count("begin") == 1 and types.count("end") == 1
    assert "update" in types and "data" in types and "sandbox" in types
    updates = [f for f in frames if isinstance(f, Update)]
    tree = next(c for f in updates for c in f.components if c.id == "phases")
    assert [n.id for n in tree.component.nodes] == ["context", "execute", "report"]
    step_updates = [c for f in updates for c in f.components if c.id == "step-0"]
    assert step_updates[0].component.status == "running"
    data = [(f.path, f.value) for f in frames if isinstance(f, Data)]
    assert ("steps.0.status", "running") in data and ("steps.0.status", "ok") in data and ("steps.0.detail", "exit 0") in data
    sandbox = next(f for f in frames if isinstance(f, Sandbox))
    assert sandbox.sandbox_id == "verify-j1" and "<div>ok</div>" in sandbox.html
    previews = [c for f in updates for c in f.components if c.id == "preview-verify-j1"]
    assert previews and previews[0].component.sandbox_id == "verify-j1"
    assert frames[-1].ok is True
    # every emitted frame survives the wire + oui round trips
    assert decode(compile(frames)) == frames
    assert [parse_frame(json.loads(frame_to_json(f))) for f in frames] == frames


def test_status_card_html_escapes():
    html = genui.status_card_html("<script>alert(1)</script>", True, 12, status=200)
    assert "<script>" not in html and "&lt;script&gt;" in html and "12 ms" in html and "status 200" in html


# ---------------------------------------------------------------- the real provision graph

def test_real_provision_run_emits_frames(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        body = {"label": "echo", "steps": [{"target_directory": ".", "command_string": "echo genui-ok", "timeout_seconds": 30}]}
        job_id = client.post("/provision", json=body, headers=H).json()["job_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            st = client.get(f"/jobs/{job_id}", headers=H).json()
            if st["done"]:
                break
            time.sleep(0.05)
        assert st["done"] and st["ok"], st
    store = app.state.ui_store()
    frames = store.frames(f"job:{job_id}")
    types = [f.type for f in frames]
    assert types[0] == "begin"
    assert types[-1] == "end" and frames[-1].ok is True
    assert types.count("update") >= 1
    data = [(f.path, f.value) for f in frames if isinstance(f, Data)]
    assert ("steps.0.status", "running") in data and ("steps.0.status", "ok") in data
    assert ("steps.0.detail", "exit 0") in data
    # the step component carries the real command; the Tree names the real phases
    updates = [f for f in frames if isinstance(f, Update)]
    step = next(c for f in updates for c in f.components if c.id == "step-0")
    assert step.component.label == "echo genui-ok"
    tree = next(c for f in updates for c in f.components if c.id == "phases")
    assert [n.id for n in tree.component.nodes][:3] == ["context", "validate", "cache_lookup"]
    # verify ok -> a Preview sandbox whose HTML the engine generated
    sandbox = next(f for f in frames if isinstance(f, Sandbox))
    assert sandbox.sandbox_id == f"verify-{job_id}" and "ok" in sandbox.html and "ms" in sandbox.html
    assert decode(compile(frames)) == frames
    # the final Tree (emitted before End): every visited phase ok, nothing running, never-visited phases skipped
    final_tree = [c for f in updates for c in f.components if c.id == "phases"][-1]
    status = {n.id: n.status for n in final_tree.component.nodes}
    assert "running" not in status.values() and "pending" not in status.values()
    assert status["remediate"] == "skipped"
    assert all(status[p] == "ok" for p in ("context", "validate", "cache_lookup", "gate", "execute", "verify", "cache_store", "report"))
    assert "failed" not in status.values()


def _run_job(client: TestClient, body: dict) -> tuple[str, dict]:
    job_id = client.post("/provision", json=body, headers=H).json()["job_id"]
    deadline = time.monotonic() + 30
    st = {}
    while time.monotonic() < deadline:
        st = client.get(f"/jobs/{job_id}", headers=H).json()
        if st["done"]:
            break
        time.sleep(0.05)
    return job_id, st


def test_real_failed_run_marks_exactly_one_phase_failed(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        body = {"label": "boom", "steps": [{"target_directory": ".", "command_string": "exit 3", "timeout_seconds": 30}]}
        job_id, st = _run_job(client, body)
        assert st["done"] and not st["ok"], st
    store = app.state.ui_store()
    frames = store.frames(f"job:{job_id}")
    assert frames[-1].type == "end" and frames[-1].ok is False
    updates = [f for f in frames if isinstance(f, Update)]
    trees = [c for f in updates for c in f.components if c.id == "phases"]
    statuses = [n.status for n in trees[-1].component.nodes]
    assert statuses.count("failed") == 1, statuses
    assert "running" not in statuses and "pending" not in statuses
    failed_phase = [n.id for n in trees[-1].component.nodes if n.status == "failed"][0]
    assert failed_phase == "execute", failed_phase  # the phase in which the step actually failed
    # the last Tree is the one emitted right before End (one frame between: the "current" Step lives in the same update)
    assert frames.index([f for f in updates if any(c.id == "phases" for c in f.components)][-1]) == len(frames) - 2
    data = [(f.path, f.value) for f in frames if isinstance(f, Data)]
    assert ("steps.0.status", "failed") in data and ("steps.0.detail", "exit 3") in data


def test_jobsurface_end_marks_visited_ok_last_failed_unvisited_skipped():
    store = UIStore()
    s = JobSurface(store, "j2", "x", phases=["a", "b", "c", "d"])
    s.phase("a"); s.phase("b"); s.phase("a")  # revisits keep one entry per phase in the tree
    mid = [c for f in store.frames("job:j2") if isinstance(f, Update) for c in f.components if c.id == "phases"][-1]
    assert [(n.id, n.status) for n in mid.component.nodes] == [("a", "running"), ("b", "ok"), ("c", "pending"), ("d", "pending")]
    s.end(False)
    frames = store.frames("job:j2")
    assert frames[-1].type == "end" and frames[-2].type == "update"
    tree = next(c for c in frames[-2].components if c.id == "phases")
    assert [(n.id, n.status) for n in tree.component.nodes] == [("a", "failed"), ("b", "ok"), ("c", "skipped"), ("d", "skipped")]
    assert s._visited == ["a", "b", "a"]
    s2 = JobSurface(store, "j3", "x", phases=["a", "b"])
    s2.phase("a"); s2.end(True)
    tree = next(c for c in store.frames("job:j3")[-2].components if c.id == "phases")
    assert [(n.id, n.status) for n in tree.component.nodes] == [("a", "ok"), ("b", "skipped")]


# ---------------------------------------------------------------- routes

def _sse_events(client: TestClient, url: str) -> list[dict]:
    events: list[dict] = []
    current: dict = {"data": []}
    with client.stream("GET", url, headers=H) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line == "":
                if current["data"]:
                    events.append({**current, "data": "\n".join(current["data"])})
                current = {"data": []}
            elif line.startswith("data: "):
                current["data"].append(line[6:])
            elif line.startswith("id: "):
                current["id"] = int(line[4:])
            elif line.startswith("event: "):
                current["event"] = line[7:]
    return events


def test_job_ui_sse_streams_and_ends(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/jobs/nope/ui", headers=H).status_code == 404
        assert client.get("/jobs/nope/ui").status_code == 401
        body = {"label": "echo", "steps": [{"target_directory": ".", "command_string": "echo sse", "timeout_seconds": 30}]}
        job_id = client.post("/provision", json=body, headers=H).json()["job_id"]
        events = _sse_events(client, f"/jobs/{job_id}/ui")
        assert events, "no SSE events"
        assert all(e["event"] == "frame" for e in events)
        ids = [e["id"] for e in events]
        assert ids == sorted(ids) and ids[0] == 1
        frames = [parse_frame(e["data"]) for e in events]
        assert frames[0].type == "begin" and frames[-1].type == "end" and frames[-1].ok is True
        # replay from `since` after the job is done, and the oui format
        later = _sse_events(client, f"/jobs/{job_id}/ui?since={ids[-2]}")
        assert [e["id"] for e in later] == [ids[-1]]
        ouis = _sse_events(client, f"/jobs/{job_id}/ui?format=oui")
        assert ouis[0]["data"].startswith("@begin job:")
        assert decode("\n".join(e["data"] for e in ouis)) == frames
        assert client.get(f"/jobs/{job_id}/ui?format=xml", headers=H).status_code == 422


def test_ui_emit_route(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    good = {"a2ui": "1", "type": "begin", "surface": "chat:s1", "title": "hello"}
    with TestClient(app) as client:
        assert client.post("/ui/emit", json={"frames": [good]}).status_code == 401
        r = client.post("/ui/emit", json={"frames": [good, {"a2ui": "1", "type": "end", "surface": "chat:s1", "ok": True}]}, headers=H)
        assert r.status_code == 200 and r.json() == {"accepted": 2}
        bad = {"a2ui": "1", "type": "update", "surface": "chat:s1", "components": [{"id": "x", "component": {"Blink": {}}}]}
        r = client.post("/ui/emit", json={"frames": [good, bad]}, headers=H)
        assert r.status_code == 422 and "frames[1]" in r.json()["detail"]
        r = client.post("/ui/emit", json={"frames": [{**good, "surface": "evil:s1"}]}, headers=H)
        assert r.status_code == 422 and "surface" in r.json()["detail"]
        assert client.post("/ui/emit", json={"frames": []}, headers=H).status_code == 422
        assert client.post("/ui/emit", json={}, headers=H).status_code == 422
    store = app.state.ui_store()
    assert [f.type for f in store.frames("chat:s1")] == ["begin", "end"]
    assert store.frames("evil:s1") == []
