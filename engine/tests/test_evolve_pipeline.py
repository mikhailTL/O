import threading

import pytest

from .evolve_fakes import FakeGitHub, Fakes, make, prop, sig
from o_engine.evolve.pipeline import (
    GitHub, LocalStore, StoreAdapter, auto_ship_allowed, evolve_settings, pr_body, run_cycle,
)
from o_engine.evolve.schema import GateCheck, GateReport


def _three():
    return Fakes(signals=[sig(1), sig(2, "arxiv"), sig(3)],
                 proposals=[prop(1, 0.9, risk="medium"), prop(2, 0.8, risk="medium"), prop(3, 0.1, risk="medium")])


def test_run_cycle_builds_top_k_in_parallel_and_opens_prs(tmp_path):
    f = _three()
    f.barrier = threading.Barrier(2)  # both builds must be running at once or this times out
    ev = make(tmp_path, f)
    r = run_cycle(ev.settings(), ev.deps, evolver=ev)
    assert r["scan"]["new_signals"] == 3 and r["scan"]["new_proposals"] == 3
    assert sorted(r["built"]) == ["p1", "p2"]
    gh = ev.deps.github
    assert len(gh.prs) == 2 and not gh.merged  # medium risk: waits for approval
    body = gh.prs[0]["body"]
    assert "| engine_pytest | pass | 1200 | 1201 |" in body and "offline_eval" in body
    assert "Signals cited" in body and "https://github.com/x/lib" in body and "Spend:** $1.00" in body
    assert {ev.store.get(i).status for i in ("p1", "p2")} == {"pr_open"} and ev.store.get("p3").status == "proposed"
    events = [e["event"] for e in ev.journal.read(proposal_id="p1")]
    assert events == ["proposed", "build_started", "built", "gated", "pr_opened"]
    assert f.pushed == ["evolve/p1", "evolve/p2"] or sorted(f.pushed) == ["evolve/p1", "evolve/p2"]


def test_no_token_records_compare_url(tmp_path):
    f = Fakes(signals=[sig(1)], proposals=[prop(1)])
    ev = make(tmp_path, f, github=FakeGitHub(token=""))
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    v = ev.proposal_view(ev.store.get("p1"))
    assert v["status"] == "gated_pass"
    assert v["compare_url"] == "https://github.com/o/O/compare/main...evolve/p1"
    assert not ev.deps.github.prs


def test_low_risk_auto_ships_with_release_tag(tmp_path):
    f = Fakes(signals=[sig(1)], proposals=[prop(1, risk="low")])
    ev = make(tmp_path, f)
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    gh = ev.deps.github
    assert gh.merged == [1] and gh.releases[0]["tag"] == "evolve-p1" and gh.releases[0]["sha"] == "merge1"
    assert ev.store.get("p1").status == "approved"
    assert ev.awaiting_install() == [{"id": "p1", "title": "Upgrade 1", "pr_number": 1, "merge_sha": "merge1",
                                      "release": "evolve-p1", "previous": "base1"}]


@pytest.mark.parametrize("p,mode,ok", [
    (prop(1, risk="low"), "low_risk", True),
    (prop(1, risk="medium"), "low_risk", False),
    (prop(1, risk="low", new_deps=["pypi:x==1"]), "low_risk", False),
    (prop(1, risk="low"), "never", False),
    (prop(1, risk="medium", dep_only=True, new_deps=["pypi:x==2"]), "deps_only", True),
    (prop(1, risk="low"), "deps_only", False),
])
def test_auto_ship_policy(p, mode, ok):
    rep = GateReport(proposal_id="p1", branch="b", passed=True, checks=[GateCheck(name="t", passed=True)])
    assert auto_ship_allowed(p, rep, mode, [])[0] is ok
    assert auto_ship_allowed(p, rep, mode, ["main/actionlog.js"])[0] is False
    bad = rep.model_copy(update={"passed": False})
    assert auto_ship_allowed(p, bad, mode, [])[0] is False


def test_failed_gates_and_protected_paths_never_open_a_pr(tmp_path):
    f = Fakes(signals=[sig(1), sig(2)], proposals=[prop(1, 0.9), prop(2, 0.8)], fail_gates={"p1"}, protected={"p2"})
    ev = make(tmp_path, f, settings={"evolveTopK": 2})
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    assert ev.store.get("p1").status == "gated_fail" and ev.store.get("p2").status == "gated_fail"
    assert not ev.deps.github.prs and not f.pushed
    assert "protected_paths" in ev.proposal_view(ev.store.get("p2"))["violations"]


def test_protected_callable_blocks_even_if_gates_pass(tmp_path):
    f = Fakes(signals=[sig(1)], proposals=[prop(1)])
    ev = make(tmp_path, f, protected=lambda files: ["engine/o_engine/evolve/gates.py"])
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    assert ev.store.get("p1").status == "gated_fail" and not ev.deps.github.merged


def test_build_crash_is_contained(tmp_path):
    p = prop(1).model_copy(update={"id": "boom1"})
    f = Fakes(signals=[sig(1)], proposals=[p])
    ev = make(tmp_path, f)
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    assert ev.store.get("boom1").status == "gated_fail"
    assert "client crashed" in ev.proposal_view(ev.store.get("boom1"))["error"]


def test_rolling_budget_caps_builds(tmp_path):
    f = Fakes(signals=[sig(i) for i in range(1, 6)], proposals=[prop(i, 1 - i / 10, risk="medium") for i in range(1, 6)], spent=2.0)
    clock = [1_000_000.0]
    ev = make(tmp_path, f, settings={"evolveBudgetUsd": 5.0, "evolvePerProposalUsd": 2.0, "evolveSlotsAway": 4}, clock=clock)
    b = ev.budget()
    assert b["daily_pace_usd"] == 4.0 and b["available_usd"] == 4.0
    r = ev.tick("away")
    assert r["started"] == ["p1", "p2"]  # the third would exceed today's pace (reserved budget counts)
    ev.wait()
    assert [x[1] for x in f.built] == [2.0, 2.0]  # per-proposal cap handed to the builder
    assert ev.budget()["spent_24h"] == 4.0
    assert ev.tick("away")["started"] == []
    clock[0] += 86400 + 1
    assert ev.tick("away")["started"] == ["p3"]  # 1.0 left this week
    ev.wait()
    assert f.built[-1][1] == pytest.approx(1.0)
    clock[0] += 86400 + 1
    assert ev.tick("away")["started"] == []  # weekly cap spent
    clock[0] += 7 * 86400
    assert ev.tick("away")["started"] == ["p4", "p5"]
    ev.wait()
    assert evolve_settings(env={})["evolvePerProposalUsd"] == 1.5


def test_tick_presence_slots_and_scan_interval(tmp_path):
    f = Fakes(signals=[sig(i) for i in range(1, 6)], proposals=[prop(i, 1 - i / 10, risk="medium") for i in range(1, 6)], spent=0.5)
    clock = [1_000_000.0]
    ev = make(tmp_path, f, settings={"evolveSlotsActive": 1, "evolveSlotsAway": 3, "evolveBudgetUsd": 100, "evolvePerProposalUsd": 1}, clock=clock)
    r = ev.tick("active"); ev.wait()
    assert r["scan"]["new_signals"] == 5 and len(r["started"]) == 1
    r = ev.tick("away"); ev.wait()
    assert r["scan"] is None and f.scans == 1 and len(r["started"]) == 3  # no rescan within 45 min
    clock[0] += 46 * 60
    r = ev.tick("active"); ev.wait()
    assert f.scans == 2 and r["scan"]["new_signals"] == 0 and len(r["started"]) == 1
    assert len(f.triaged) == 1  # triage only sees new signals


def test_disabled_does_nothing(tmp_path):
    f = _three()
    ev = make(tmp_path, f, settings={"evolveEnabled": False})
    assert ev.tick("away") == {"ok": True, "enabled": False}
    assert run_cycle(ev.settings(), ev.deps, evolver=ev)["enabled"] is False and f.scans == 0


def test_user_approve_merges_and_reject_closes(tmp_path):
    f = Fakes(signals=[sig(1), sig(2)], proposals=[prop(1, 0.9, risk="high"), prop(2, 0.8, risk="medium")])
    ev = make(tmp_path, f)
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    gh = ev.deps.github
    r = ev.approve("p1")
    assert r["status"] == "approved" and gh.merged == [ev.journal.last("p1", ("pr_opened",))["pr_number"]] and gh.releases[-1]["tag"] == "evolve-p1"
    assert ev.journal.last("p1", ("approved",))["by"] == "user"
    n2 = ev.journal.last("p2", ("pr_opened",))["pr_number"]
    assert ev.reject("p2", "not now")["status"] == "rejected" and gh.closed == [n2]
    assert ev.approve("p2")["ok"] is False


def test_approving_a_proposal_queues_it_first(tmp_path):
    f = Fakes(signals=[sig(1), sig(2)], proposals=[prop(1, 0.9, risk="medium"), prop(2, 0.1, risk="medium")])
    ev = make(tmp_path, f, settings={"evolveSlotsActive": 0})
    ev.tick("active")
    assert ev.approve("p2")["status"] == "accepted"
    assert [p.id for p in ev.candidates()] == ["p2", "p1"]


def test_ship_and_rollback_handshake(tmp_path):
    f = Fakes(signals=[sig(1), sig(2)], proposals=[prop(1, 0.9), prop(2, 0.5)])
    ev = make(tmp_path, f, settings={"evolveTopK": 1})
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    assert ev.shipped("p1", "evolve-p1", "sha0")["status"] == "shipped"
    r = ev.rollback("p1", "felt slower")
    assert r["status"] == "rolled_back" and r["ref"] == "sha0"
    assert ev.rollback_requests() == [{"id": "p1", "ref": "sha0", "reason": "felt slower"}]
    ev.rollback("p1", applied=True, ref="sha0")
    assert ev.rollback_requests() == []
    assert ev.rollback("p2")["ok"] is False  # never shipped


def test_skills_tier_adopts_practice_but_candidates_wait(tmp_path):
    class Reg:
        def __init__(self):
            self.approved = []

        def adopt_practiced(self):
            return [{"id": "k1", "name": "rename files by date"}]

        def candidates(self):
            return [{"id": "mcp-x", "name": "x MCP server", "kind": "mcp_server", "status": "candidate"}]

        def approve(self, sid):
            self.approved.append(sid)
            return {"id": sid, "status": "adopted"}

    reg = Reg()
    ev = make(tmp_path, Fakes(), skills=reg)
    assert ev.tick("active")["skills_adopted"] == 1
    assert reg.approved == []  # ticking never installs third-party code
    assert ev.status()["waiting_for_you"] == 1 and ev.skill_candidates()[0]["id"] == "mcp-x"
    assert ev.decide_skill("mcp-x", True)["ok"] and reg.approved == ["mcp-x"]
    texts = [x["text"] for x in ev.feed()]
    assert "Learned a skill from practice: rename files by date" in texts


def test_feed_is_plain_words_newest_first(tmp_path):
    f = Fakes(signals=[sig(1)], proposals=[prop(1)])
    clock = [1_000_000.0]
    ev = make(tmp_path, f, clock=clock)
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    texts = [x["text"] for x in ev.feed()]
    assert texts[0] == "Shipping Upgrade 1 (all checks passed, low risk)"
    assert "Upgrade 1 passed every check" in texts and "Read 1 new development, 1 worth trying" in texts
    clock[0] += 2 * 86400
    assert ev.feed(hours=24) == []


def test_store_adapter_uses_record_decision_ledger(tmp_path):
    class Store:
        def __init__(self):
            self.rows, self.props = [], {}

        def add_signals(self, s):
            return []  # a scanner already stored them

        def add_proposal(self, p):
            self.props.setdefault(p.id, p)

        def get_proposal(self, pid):
            p = self.props.get(pid)
            st = [r for r in self.rows if r[0] == pid]
            return p.model_copy(update={"status": st[-1][1]}) if p and st else p

        def proposals(self):
            return [self.get_proposal(i) for i in self.props]

        def record_decision(self, pid, status, reason="", metrics=None):
            self.rows.append((pid, status, reason))

        def decisions(self):
            return [{"proposal_id": a, "status": b, "reason": c} for a, b, c in self.rows]

    st = Store()
    f = Fakes(signals=[sig(1)], proposals=[prop(1, risk="medium")], fail_gates={"p1"})
    ev = make(tmp_path, f, store=st)
    run_cycle(ev.settings(), ev.deps, evolver=ev)
    assert [r[1] for r in st.rows] == ["building", "built", "gated_fail"]
    assert st.rows[-1][2].startswith("failed: engine_pytest")
    assert ev.store.ledger()[-1]["status"] == "gated_fail"
    assert f.triaged == [["s1"]]  # new to the local snapshot even though the store returned []


def test_local_store_round_trip(tmp_path):
    ls = LocalStore(tmp_path / "s.json")
    assert [s.id for s in ls.add_signals([sig(1), sig(2)])] == ["s1", "s2"] and ls.add_signals([sig(1)]) == []
    ls.upsert_proposal(prop(1))
    again = LocalStore(tmp_path / "s.json")
    assert again.get_proposal("p1").title == "Upgrade 1" and again.get_signal("s2").source == "github"
    assert StoreAdapter(None, again).proposals()[0].id == "p1"


def test_evolve_settings_env_and_defaults():
    s = evolve_settings(env={})
    assert s["evolveEnabled"] is True and s["evolveBudgetUsd"] == 10.0 and s["evolveAutoShip"] == "low_risk"
    s = evolve_settings(env={"O_EVOLVE_ENABLED": "0", "O_EVOLVE_BUDGET_USD": "4.5", "O_EVOLVE_AUTO_SHIP": "bogus",
                             "GITHUB_TOKEN": "gh", "O_EVOLVE_SLOTS_AWAY": "99"})
    assert s["evolveEnabled"] is False and s["evolveBudgetUsd"] == 4.5 and s["evolveAutoShip"] == "low_risk"
    assert s["evolveGithubToken"] == "gh" and s["evolveSlotsAway"] == 8
    assert evolve_settings({"evolveAutoShip": "never"}, env={})["evolveAutoShip"] == "never"


def test_github_client_requests():
    calls = []

    class Resp:
        def __init__(self, code, j):
            self.status_code, self._j, self.text, self.content = code, j, "", b"x"

        def json(self):
            return self._j

    class Client:
        def request(self, method, url, json=None, headers=None):
            calls.append((method, url, json, headers["authorization"]))
            if url.endswith("/pulls"):
                return Resp(201, {"number": 7, "html_url": "u", "base": {"sha": "b"}})
            if url.endswith("/merge"):
                return Resp(200, {"sha": "m", "merged": True})
            return Resp(422, {})

    gh = GitHub("o/O", "tok", client=Client())
    assert gh.open_pr("evolve/p1", "main", "t", "body") == {"number": 7, "url": "u", "base_sha": "b"}
    assert gh.merge_pr(7, "t")["sha"] == "m"
    with pytest.raises(RuntimeError):
        gh.create_release("evolve-p1", "m", "n", "b")
    assert calls[0][:2] == ("POST", "https://api.github.com/repos/o/O/pulls") and calls[0][3] == "Bearer tok"
    assert calls[1][2]["merge_method"] == "squash"


def test_pr_body_marks_failures():
    rep = GateReport(proposal_id="p1", branch="b", passed=False, checks=[GateCheck(name="secret_scan", passed=False, detail="a|b")])
    body = pr_body(prop(1), rep, [])
    assert "FAILED" in body and "| secret_scan | FAIL |" in body and "a/b" in body
