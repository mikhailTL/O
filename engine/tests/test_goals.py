"""Long-term goals ("agenda"): store, journal, honesty checks, proposals, cycle prompt, routes."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from o_engine.goals import (
    CHECKPOINT_KINDS,
    NEVER_BY_O,
    CycleRecord,
    Goal,
    GoalStore,
    draft_goal,
    next_cycle_prompt,
)
from o_engine.server import EngineSettings, create_app

TOKEN = "test-token-goals"
DAY = 86400.0


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeLocal:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeClient:
    def __init__(self, local):
        self._local = local
        self.cloud_calls = 0

    def local_only(self):
        return self._local

    def complete_json(self, system, user):
        self.cloud_calls += 1
        raise AssertionError("cloud fallback must never be used")


# ------------------------------------------------------------------ model


def test_goal_defaults_are_safe():
    g = Goal(id="g-abc123", title="T", objective="O", success_metrics=[{"name": "views", "target": 10, "unit": "views", "source": "connector:youtube"}],
             horizon_days=30, cadence="daily", created=1.0)
    assert g.autonomy == "act_with_checkpoints"
    assert list(g.checkpoints) == list(CHECKPOINT_KINDS)
    assert g.status == "active" and g.origin == "user" and g.allowed == ()


def test_checkpoints_always_include_the_required_kinds():
    g = Goal(id="g-abc123", title="T", objective="O", success_metrics=[], horizon_days=30, cadence="daily", created=1.0,
             checkpoints=["publish"])
    assert set(CHECKPOINT_KINDS) <= set(g.checkpoints)
    g2 = Goal(id="g-abc123", title="T", objective="O", success_metrics=[], horizon_days=30, cadence="daily", created=1.0,
              checkpoints=["publish", "upload_file"])
    assert "upload_file" in g2.checkpoints and set(CHECKPOINT_KINDS) <= set(g2.checkpoints)


def test_allowed_never_contains_spend_or_create_account():
    g = Goal(id="g-abc123", title="T", objective="O", success_metrics=[], horizon_days=30, cadence="daily", created=1.0,
             autonomy="act", allowed=["publish", "spend", "create_account"])
    assert g.allowed == ("publish",)
    assert set(NEVER_BY_O) == {"spend", "create_account"}
    with pytest.raises(ValidationError):
        Goal(id="g-abc123", title="T", objective="O", success_metrics=[], horizon_days=30, cadence="daily", created=1.0,
             allowed=["teleport"])


def test_goal_rejects_bad_fields():
    base = dict(id="g-abc123", title="T", objective="O", success_metrics=[], horizon_days=30, cadence="daily", created=1.0)
    for bad in ({"cadence": "hourly"}, {"autonomy": "yolo"}, {"horizon_days": 0}, {"id": "../etc"}, {"origin": "web"}):
        with pytest.raises(ValidationError):
            Goal(**{**base, **bad})


def test_cycle_record_normalizes_metrics_and_flags_unverified():
    c = CycleRecord.from_body({"plan": ["a"], "did": ["b"], "metrics": {"views": 120, "subs": {"value": 9, "source": "connector:youtube"}},
                               "reflection": "ok", "next": "more"}, ts=5.0)
    assert c.metrics["views"].source == "unverified" and c.metrics["subs"].source == "connector:youtube"
    assert "unverified_metrics" in c.flags
    assert c.ts == 5.0


def test_cycle_record_without_work_is_incomplete():
    c = CycleRecord.from_body({"reflection": ""}, ts=1.0)
    assert "incomplete" in c.flags


# ------------------------------------------------------------------ drafting from text


def test_draft_youtube_monetization():
    g = draft_goal("Grow a YouTube channel about desk setups to monetization", now=100.0)
    names = [m.name for m in g.success_metrics]
    assert names[0] == "views"
    assert {"subscribers", "watch_hours"} <= set(names)
    subs = next(m for m in g.success_metrics if m.name == "subscribers")
    hours = next(m for m in g.success_metrics if m.name == "watch_hours")
    assert subs.target == 1000 and hours.target == 4000
    assert all(m.source == "connector:youtube" for m in g.success_metrics)
    assert g.cadence == "daily" and g.horizon_days == 60 and g.autonomy == "act_with_checkpoints"
    assert g.title.startswith("Grow a YouTube channel")


def test_draft_newsletter_number_and_horizon():
    g = draft_goal("Get me to 10k newsletter subscribers in 3 months", now=100.0)
    m = g.success_metrics[0]
    assert m.name == "subscribers" and m.target == 10000 and m.source == "connector:newsletter"
    assert g.horizon_days == 90


def test_draft_cadence_variants():
    assert draft_goal("Every weekday, grow my podcast to 500 listeners").cadence == "weekdays"
    g = draft_goal("Every 6 hours check and grow my shop to 50 sales")
    assert g.cadence == "every_n_hours" and g.every_hours == 6


def test_draft_without_numbers_uses_honest_self_report():
    g = draft_goal("Learn to cook properly")
    assert g.success_metrics[0].source == "self_report"


# ------------------------------------------------------------------ store


def test_create_journal_and_derived_goal_json(tmp_path):
    s = GoalStore(tmp_path, clock=Clock())
    g = s.create_from_text("Grow a YouTube channel about desk setups to monetization")
    d = tmp_path / g["id"]
    lines = (d / "journal.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["kind"] == "created"
    derived = json.loads((d / "goal.json").read_text())
    assert derived["id"] == g["id"] and derived["status"] == "active" and derived["cycles"] == 0
    # goal.json is a cache: deleting it changes nothing
    (d / "goal.json").unlink()
    assert GoalStore(tmp_path).get(g["id"])["title"] == g["title"]


def test_cycles_append_only_and_series(tmp_path):
    clock = Clock()
    s = GoalStore(tmp_path, clock=clock)
    gid = s.create_from_text("Grow a YouTube channel about desk setups to monetization")["id"]
    for i, v in enumerate([100, 180, 260]):
        clock.t += DAY
        s.record_cycle(gid, {"plan": [f"p{i}"], "did": [f"d{i}"], "metrics": {"views": {"value": v, "source": "connector:youtube"}},
                             "reflection": f"r{i}", "next": f"n{i}"})
    raw = (tmp_path / gid / "journal.jsonl").read_text().splitlines()
    assert [json.loads(l)["kind"] for l in raw] == ["created", "cycle", "cycle", "cycle"]
    det = s.detail(gid)
    assert [p["value"] for p in det["series"]["views"]] == [100, 180, 260]
    assert len(det["cycles"]) == 3 and det["cycles"][-1]["reflection"] == "r2"
    assert det["day"] == 4 and det["goal"]["horizon_days"] == 60
    assert det["goal"]["cycles"] == 3


def test_detail_keeps_last_30_cycles(tmp_path):
    clock = Clock()
    s = GoalStore(tmp_path, clock=clock)
    gid = s.create_from_text("Get me to 10k newsletter subscribers")["id"]
    for i in range(35):
        clock.t += 3600
        s.record_cycle(gid, {"plan": ["x"], "did": ["y"], "metrics": {"subscribers": {"value": i, "source": "connector:newsletter"}},
                             "reflection": "r", "next": "n"})
    det = s.detail(gid)
    assert len(det["cycles"]) == 30 and det["cycles"][0]["metrics"]["subscribers"]["value"] == 5
    assert len(det["series"]["subscribers"]) == 30


def test_status_transitions(tmp_path):
    s = GoalStore(tmp_path, clock=Clock())
    gid = s.create_from_text("Get me to 10k newsletter subscribers")["id"]
    assert s.set_status(gid, "paused")["status"] == "paused"
    assert s.set_status(gid, "active")["status"] == "active"
    assert s.set_status(gid, "archived")["status"] == "archived"
    with pytest.raises(ValueError):
        s.record_cycle(gid, {"plan": ["x"], "did": ["y"], "reflection": "r", "next": "n"})
    with pytest.raises(ValueError):
        s.set_status(gid, "active")  # archived is final
    assert [g["id"] for g in s.list()] == [gid]
    assert s.list(include_archived=False) == []


def test_unknown_and_malicious_ids(tmp_path):
    s = GoalStore(tmp_path)
    assert s.get("g-000000") is None
    with pytest.raises(KeyError):
        s.record_cycle("g-000000", {})
    assert s.get("../../etc") is None


def test_honesty_flags_unsupported_virality_claims(tmp_path):
    s = GoalStore(tmp_path, clock=Clock())
    gid = s.create_from_text("Grow a YouTube channel about desk setups to monetization")["id"]
    c = s.record_cycle(gid, {"plan": ["x"], "did": ["y"], "reflection": "The channel went viral and is now monetized!",
                             "metrics": {"views": {"value": 300, "source": "connector:youtube"},
                                         "subscribers": {"value": 40, "source": "connector:youtube"}}, "next": "n"})
    assert "unsupported_claim" in c["flags"]
    ok = s.record_cycle(gid, {"plan": ["x"], "did": ["y"], "reflection": "Views up 20% on the second video; still far from 1000 subs.",
                              "metrics": {"views": {"value": 360, "source": "connector:youtube"}}, "next": "n"})
    assert "unsupported_claim" not in ok["flags"]


def test_honest_claim_when_platform_numbers_meet_targets(tmp_path):
    s = GoalStore(tmp_path, clock=Clock())
    gid = s.create_from_text("Grow a YouTube channel about desk setups to monetization")["id"]
    c = s.record_cycle(gid, {"plan": ["x"], "did": ["y"], "reflection": "Monetization thresholds reached.",
                             "metrics": {"views": {"value": 90000, "source": "connector:youtube"},
                                         "subscribers": {"value": 1200, "source": "connector:youtube"},
                                         "watch_hours": {"value": 4100, "source": "connector:youtube"}}, "next": "n"})
    assert "unsupported_claim" not in c["flags"]


def test_model_drafts_goal_locally_and_cannot_raise_autonomy(tmp_path):
    reply = json.dumps({"title": "Desk-setup channel to YPP", "objective": "Reach YouTube Partner Program thresholds",
                        "success_metrics": [{"name": "subscribers", "target": 1000, "unit": "subscribers", "source": "connector:youtube"}],
                        "horizon_days": 90, "cadence": "daily", "autonomy": "act", "allowed": ["publish"], "checkpoints": []})
    client = FakeClient(FakeLocal(reply))
    s = GoalStore(tmp_path, model=client, clock=Clock())
    g = s.create_from_text("Grow a YouTube channel about desk setups to monetization")
    assert g["title"] == "Desk-setup channel to YPP" and g["horizon_days"] == 90
    assert g["autonomy"] == "act_with_checkpoints" and g["allowed"] == []
    assert set(CHECKPOINT_KINDS) <= set(g["checkpoints"])
    assert client.cloud_calls == 0 and len(client._local.calls) == 1


def test_model_garbage_falls_back_to_deterministic(tmp_path):
    for reply in ("not json", json.dumps({"title": ""}), RuntimeError("down")):
        s = GoalStore(tmp_path / str(id(reply)), model=FakeClient(FakeLocal(reply)), clock=Clock())
        g = s.create_from_text("Get me to 10k newsletter subscribers")
        assert g["success_metrics"][0]["target"] == 10000


def test_set_autonomy_is_explicit(tmp_path):
    s = GoalStore(tmp_path, clock=Clock())
    gid = s.create_from_text("Grow a YouTube channel about desk setups to monetization")["id"]
    g = s.set_autonomy(gid, "act", ["publish", "spend"])
    assert g["autonomy"] == "act" and g["allowed"] == ["publish"]
    g = s.set_autonomy(gid, "act_with_checkpoints", ["publish"])
    assert g["allowed"] == []  # allowed only means something under "act"


# ------------------------------------------------------------------ proposals


PROFILE = {
    "preferences": [{"id": "p1", "kind": "preference", "text": "Prefers short summaries", "confidence": 0.9}],
    "habits": [{"id": "h1", "kind": "habit", "text": "Uses Figma on weekday mornings", "app": "Figma", "confidence": 0.7}],
    "app_skills": [],
    "suggestions": [{"id": "s1", "kind": "habit", "text": "You summarize your inbox most Mondays", "confidence": 0.8,
                     "suggest_schedule": {"prompt": "Summarize my inbox", "when": "Mondays at 08:00", "cron": "0 8 * * 1"}}],
}


def test_propose_deterministic_from_profile(tmp_path):
    s = GoalStore(tmp_path, clock=Clock())
    props = s.propose(PROFILE)
    assert 1 <= len(props) <= 3
    assert all(p["origin"] == "agent" and p["autonomy"] == "act_with_checkpoints" for p in props)
    assert any("inbox" in p["objective"].lower() for p in props)
    assert s.list() == []  # proposals are not goals until accepted


def test_propose_empty_profile_is_empty(tmp_path):
    assert GoalStore(tmp_path).propose({"preferences": [], "habits": [], "app_skills": [], "suggestions": []}) == []


def test_propose_with_local_model_caps_at_three(tmp_path):
    reply = json.dumps({"goals": [{"title": f"G{i}", "objective": f"Objective {i}", "success_metrics": [], "horizon_days": 30,
                                   "cadence": "daily", "autonomy": "act"} for i in range(5)]})
    s = GoalStore(tmp_path, model=FakeClient(FakeLocal(reply)), clock=Clock())
    props = s.propose(PROFILE)
    assert len(props) == 3 and all(p["autonomy"] == "act_with_checkpoints" for p in props)


def test_accept_proposal_creates_agent_goal(tmp_path):
    s = GoalStore(tmp_path, clock=Clock())
    p = s.propose(PROFILE)[0]
    g = s.create_from_proposal({**p, "autonomy": "act", "allowed": ["publish"]})
    assert g["origin"] == "agent" and g["autonomy"] == "act_with_checkpoints" and g["allowed"] == []
    assert s.get(g["id"])["title"] == p["title"]


# ------------------------------------------------------------------ cycle prompt


def test_next_cycle_prompt_contents(tmp_path):
    clock = Clock()
    s = GoalStore(tmp_path, clock=clock)
    gid = s.create_from_text("Grow a YouTube channel about desk setups to monetization")["id"]
    for i, v in enumerate([100, 140, 210, 330, 400, 520, 610, 700, 820]):
        clock.t += DAY
        s.record_cycle(gid, {"plan": ["film"], "did": ["posted short"], "metrics": {"views": {"value": v, "source": "connector:youtube"}},
                             "reflection": f"reflection {i}", "next": f"tomorrow {i}"})
    det = s.detail(gid)
    p = next_cycle_prompt(det["goal"], det["cycles"], profile_text="- Prefers short summaries", now=clock.t)
    assert det["goal"]["title"] in p and gid in p
    assert "day 10 of 60" in p.lower()
    assert "reflection 8" in p and "tomorrow 8" in p
    assert "400, 520, 610, 700, 820" in p  # last 7 values of the primary metric
    for kind in CHECKPOINT_KINDS:
        assert kind in p
    low = p.lower()
    assert "plan today" in low and "measure" in low and "reflection" in low and "tomorrow" in low
    assert "never create accounts" in low and "buy" in low and "bots" in low
    assert "goal_checkpoint" in p and '"goal_id"' in p and "```json" in p
    assert "Prefers short summaries" in p
    assert len(p) <= 6000


def test_next_cycle_prompt_surfaces_flags(tmp_path):
    s = GoalStore(tmp_path, clock=Clock())
    gid = s.create_from_text("Grow a YouTube channel about desk setups to monetization")["id"]
    s.record_cycle(gid, {"plan": ["x"], "did": ["y"], "reflection": "It went viral!", "metrics": {"views": 5}, "next": "n"})
    det = s.detail(gid)
    p = next_cycle_prompt(det["goal"], det["cycles"], now=Clock().t)
    assert "not supported" in p.lower() and "unverified" in p.lower()


def test_first_cycle_prompt(tmp_path):
    s = GoalStore(tmp_path, clock=Clock())
    g = s.create_from_text("Get me to 10k newsletter subscribers")
    p = next_cycle_prompt(g, [], now=Clock().t)
    assert "day 1 of" in p.lower() and "first cycle" in p.lower()


# ------------------------------------------------------------------ routes


@pytest.fixture
def settings(tmp_path):
    return EngineSettings(token=TOKEN, data_dir=str(tmp_path / "data"), sandbox_root=str(tmp_path / "sandbox"),
                          screenpipe_db=str(tmp_path / "missing.db"), vllm_url="http://127.0.0.1:1/v1", learning_model=False)


class _Deps:
    pass


@pytest.fixture
def client(settings):
    app = create_app(settings, deps_factory=lambda s: _Deps())
    with TestClient(app) as c:
        yield c


def _h():
    return {"x-o-token": TOKEN}


def test_goal_routes_require_token(client):
    for method, path, body in [("get", "/goals", None), ("post", "/goals", {"text": "x"}), ("post", "/goals/propose", {}),
                               ("get", "/goals/g-abc123", None), ("post", "/goals/g-abc123/cycle", {}),
                               ("post", "/goals/g-abc123/pause", {}), ("post", "/goals/g-abc123/resume", {}),
                               ("post", "/goals/g-abc123/archive", {}), ("post", "/goals/g-abc123/autonomy", {})]:
        r = getattr(client, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 401, path


def test_goal_routes_round_trip(client, settings):
    r = client.post("/goals", json={"text": "Grow a YouTube channel about desk setups to monetization"}, headers=_h())
    assert r.status_code == 200 and r.json()["ok"] is True
    g = r.json()["goal"]
    gid = g["id"]
    assert g["autonomy"] == "act_with_checkpoints"
    lst = client.get("/goals", headers=_h()).json()["goals"]
    assert [x["id"] for x in lst] == [gid] and lst[0]["day"] == 1 and lst[0]["primary"]["name"] == "views"
    c = client.post(f"/goals/{gid}/cycle", json={"plan": ["script"], "did": ["drafted video"],
                                                 "metrics": {"views": {"value": 42, "source": "connector:youtube"}},
                                                 "reflection": "first upload pending approval", "next": "thumbnail A/B"}, headers=_h())
    assert c.status_code == 200 and c.json()["cycle"]["metrics"]["views"]["value"] == 42
    det = client.get(f"/goals/{gid}", headers=_h()).json()
    assert det["series"]["views"][0]["value"] == 42 and len(det["cycles"]) == 1
    assert "next_cycle_prompt" in det and gid in det["next_cycle_prompt"]
    assert client.post(f"/goals/{gid}/pause", headers=_h()).json()["goal"]["status"] == "paused"
    assert client.post(f"/goals/{gid}/resume", headers=_h()).json()["goal"]["status"] == "active"
    a = client.post(f"/goals/{gid}/autonomy", json={"autonomy": "act", "allowed": ["publish", "spend"]}, headers=_h()).json()
    assert a["goal"]["allowed"] == ["publish"]
    assert client.post(f"/goals/{gid}/archive", headers=_h()).json()["goal"]["status"] == "archived"
    assert client.post(f"/goals/{gid}/cycle", json={"plan": ["x"], "did": ["y"], "reflection": "r", "next": "n"}, headers=_h()).status_code == 409
    rows = [json.loads(l) for l in (settings.data_dir_path / "engine-actions.jsonl").read_text().splitlines() if l.strip()]
    assert any(r["summary"].startswith("goals: created") for r in rows)


def test_goal_routes_validation(client):
    assert client.post("/goals", json={}, headers=_h()).status_code == 422
    assert client.post("/goals", json={"text": "  "}, headers=_h()).status_code == 422
    assert client.get("/goals/g-000000", headers=_h()).status_code == 404
    assert client.get("/goals/..%2Fetc", headers=_h()).status_code in (404, 422)
    assert client.post("/goals/g-000000/cycle", json={}, headers=_h()).status_code == 404
    gid = client.post("/goals", json={"text": "Get me to 10k newsletter subscribers"}, headers=_h()).json()["goal"]["id"]
    assert client.post(f"/goals/{gid}/cycle", json={"plan": "not a list"}, headers=_h()).status_code == 422
    assert client.post(f"/goals/{gid}/autonomy", json={"autonomy": "yolo"}, headers=_h()).status_code == 422


def test_goal_propose_route_and_accept(client):
    r = client.post("/goals/propose", json={}, headers=_h())
    assert r.status_code == 200 and r.json()["proposals"] == []  # nothing learned yet: nothing to propose
    import time as _time

    base = _time.time()
    for k in range(1, 5):  # four past weeks, same weekday and hour: a recurring task
        client.post("/learn/observe", json={"source": "task", "text": "Summarize my inbox", "outcome": "ok", "ts": base - k * 7 * DAY},
                    headers=_h())
    props = client.post("/goals/propose", json={}, headers=_h()).json()["proposals"]
    assert 1 <= len(props) <= 3
    g = client.post("/goals", json={"proposal": props[0]}, headers=_h()).json()["goal"]
    assert g["origin"] == "agent"
