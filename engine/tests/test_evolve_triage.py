"""Evolve triage: deterministic prefilter, strict validation, protected areas, batching and budget. Fakes only."""
from __future__ import annotations

import json
import re
import os

import pytest

from o_engine.evolve import triage as T
from o_engine.evolve.capmap import CapmapError, load_capmap, match_subsystems, validate_capmap
from o_engine.evolve.schema import Signal
from o_engine.evolve.store import EvolveStore

CM = load_capmap()


def sig(i, **kw) -> Signal:
    base = dict(id=f"s{i}", kind="new_repo", source="github", title=f"org/gui-agent-{i}: GUI grounding with zoom",
                url=f"https://github.com/org/r{i}", repo=f"org/r{i}", stars=500, stars_7d=200, license="MIT",
                topics=["gui-agent", "computer-use"], published="2026-11-05T00:00:00Z")
    base.update(kw)
    return Signal(**base)


def good(ids, **kw):
    p = {"signal_ids": ids, "subsystem": "cu_grounding", "title": "Add cascade grounder",
         "rationale": "org/r1 reports +5 on ScreenSpot-Pro", "files": ["engine/o_engine/cu/ground.py"],
         "spec": "Add CascadeGrounder in cu/ground.py", "acceptance_test": "pytest engine/tests/test_cu_ground.py::test_cascade",
         "expected_gain": "offline grounding hit rate +3pts", "risk": "low", "dependency_only": False,
         "new_dependencies": [], "score": 0.7}
    p.update(kw)
    return p


class FakeClient:
    def __init__(self, answers, cost=0.1):
        self.answers = list(answers)
        self.cost = cost
        self.spent_usd = 0.0
        self.prompts = []
        self.budgets = []

    def complete(self, system, user, max_budget_usd=None):
        self.prompts.append(user)
        self.budgets.append(max_budget_usd)
        self.spent_usd += self.cost
        a = self.answers.pop(0) if self.answers else '{"proposals": []}'
        return a(user) if callable(a) else a


# ---------------------------------------------------------------- prefilter
def test_prefilter_rules():
    signals = [
        sig(1),
        sig(2, license="GPL-3.0"),
        sig(3, license=""),
        sig(4, stars=10),
        sig(5, kind="paper", source="arxiv", license="", repo="", summary="no code", stars=0),
        sig(6, title="cooking recipes", topics=["food"], summary=""),
        sig(7, kind="repo_release", license="", topics=["watch", "gui-agent"]),
        sig(8, kind="package_release", source="pypi", license="", title="numpy 3.0 (pypi)", topics=["dependency"], repo="numpy/numpy"),
        sig(9, kind="changelog", source="anthropic", license="", title="anthropics/claude-code 2.1.300", topics=["anthropic"]),
        sig(10, kind="package_release", source="pypi", license="", title="numpy 2.9 (pypi)", topics=["dependency"], repo="numpy/numpy"),
    ]
    kept, dropped = T.prefilter(signals, CM)
    ids = {c.signal.id for c in kept}
    assert ids == {"s1", "s8", "s9"}
    why = dict(dropped)
    assert "GPL-3.0" in why["s2"] and "unknown" in why["s3"] and "stars" in why["s4"]
    assert why["s5"] == "paper without code" and "overlap" in why["s6"] and "license" in why["s7"]
    assert why["s10"] == "older release of the same repo"
    assert "model_routing" in next(c for c in kept if c.signal.id == "s9").subsystems


def test_prefilter_top_n_and_ranking():
    signals = [sig(i, stars_7d=i * 10) for i in range(1, 51)]
    kept, dropped = T.prefilter(signals, CM, max_signals=40)
    assert len(kept) == 40 and kept[0].signal.id == "s50"
    assert sum(1 for _, r in dropped if "top-N" in r) == 10


def test_relevance_matching_uses_capmap_topics():
    hits = match_subsystems("a browser-agent for web automation", ["mcp-server"], CM)
    assert "browser" in hits and "mcp" in hits


# ---------------------------------------------------------------- validation
def test_validate_accepts_good_and_folds_files_into_spec():
    c = T.Candidate(sig(1), {"cu_grounding": 3}, 0.5, True)
    p = T.validate_proposal(good(["s1"]), {"s1": c}, CM)
    assert p.spec.startswith("Files: engine/o_engine/cu/ground.py") and p.licenses_ok and p.score == 0.7
    assert p.id == T.proposal_id(["s1"], "Add cascade grounder")


@pytest.mark.parametrize("patch,needle", [
    ({"signal_ids": ["zzz"]}, "signal_ids"),
    ({"subsystem": "warp_drive"}, "subsystem"),
    ({"score": 1.5}, "score"),
    ({"score": True}, "score"),
    ({"risk": "extreme"}, "schema"),
    ({"new_dependencies": ["requests"]}, "new_dependencies"),
    ({"files": ["/etc/passwd"]}, "files"),
    ({"files": ["engine/../../x"]}, "files"),
    ({"files": []}, "name the files"),
    ({"extra": 1}, "unknown fields"),
    ({"dependency_only": "yes"}, "boolean"),
    ({"title": ""}, "title"),
    ({"files": ["main/link.js"]}, "protected"),
    ({"files": ["main/actionlog.js"]}, "protected"),
    ({"files": ["engine/o_engine/evolve/gates.py"]}, "protected"),
    ({"spec": "Relax consentFilter so sensors turn on faster"}, "consent"),
    ({"spec": "Auto-approve the permission prompt for safe tools"}, "permission"),
    ({"title": "Skip the payment guard on trusted sites"}, "payment"),
    ({"spec": "Lower the diff budget in the evolve gates"}, "evolve gates"),
])
def test_validate_rejects(patch, needle):
    c = T.Candidate(sig(1), {"cu_grounding": 3}, 0.5, True)
    with pytest.raises(ValueError) as e:
        T.validate_proposal(good(["s1"], **patch), {"s1": c}, CM)
    assert needle in str(e.value)


def test_dependency_only_and_license_flag():
    pkg = T.Candidate(sig(8, kind="package_release", license="", topics=["dependency"]), {"_dependency": 1}, 0.3, False)
    p = T.validate_proposal(good(["s8"], subsystem="model_routing", files=[], dependency_only=True,
                                 new_dependencies=["pypi:openai==3.46.0", "npm:@playwright/mcp@0.0.90"]), {"s8": pkg}, CM)
    assert p.dependency_only and p.licenses_ok  # a version bump is not code adoption
    rel = T.Candidate(sig(7, kind="repo_release", license="", topics=["dependency"]), {"cu_grounding": 1}, 0.3, False)
    p2 = T.validate_proposal(good(["s7"]), {"s7": rel}, CM)
    assert p2.licenses_ok is False


# ---------------------------------------------------------------- triage
def test_triage_batches_of_twenty_and_budget_passed():
    signals = [sig(i) for i in range(1, 46)]
    answer = lambda user: json.dumps({"proposals": [good([re.search(r'"id": "([^"]+)"', user).group(1)],
                                                         title=f"p{len(user)}")]})
    client = FakeClient([answer, answer, answer], cost=0.05)
    res = T.TriageResult()
    out = T.triage(signals, CM, client, budget_usd=1.0, max_signals=45, result=res)
    assert len(client.prompts) == 3  # 20 + 20 + 5
    assert all(p.count('"id":') <= 20 for p in client.prompts)
    assert client.budgets[0] == pytest.approx(1.0) and client.budgets[1] == pytest.approx(0.95)
    assert res.spent_usd == pytest.approx(0.15) and len(out) >= 1
    assert out == sorted(out, key=lambda p: p.score, reverse=True)


def test_triage_stops_at_budget_and_survives_bad_batches():
    signals = [sig(i) for i in range(1, 61)]
    client = FakeClient(["not json at all", '{"proposals": [{"bad": 1}]}', '{"proposals": []}'], cost=0.45)
    res = T.TriageResult()
    out = T.triage(signals, CM, client, budget_usd=1.0, max_signals=60, result=res)
    assert out == []
    assert len(client.prompts) == 3  # 0.45, 0.90 spent; 0.10 remaining still allows a third batch
    assert any("batch 1" in e for e in res.errors) and res.rejected and res.rejected[0][1].startswith("unknown fields")
    client2 = FakeClient([], cost=0.6)
    res2 = T.TriageResult()
    T.triage(signals, CM, client2, budget_usd=1.0, max_signals=60, result=res2)
    assert len(client2.prompts) == 2 and any("budget reached" in e for e in res2.errors)


def test_triage_stores_proposals_and_ledger():
    st = EvolveStore(":memory:")
    client = FakeClient([json.dumps({"proposals": [good(["s1"])]})])
    out = T.triage([sig(1)], CM, client, budget_usd=0.5, store=st)
    assert st.get_proposal(out[0].id).status == "proposed"
    assert st.decisions(out[0].id)[0]["reason"] == "triage"


def test_triage_client_is_lean():
    cmd = T.TriageClient(claude_path="claude").command("SYS", 0.42)
    assert cmd[:4] == ["claude", "-p", "--output-format", "json"]
    i = cmd.index("--tools")
    assert cmd[i + 1] == "" and "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.42"
    assert "--dangerously-skip-permissions" not in cmd


def test_triage_client_sums_cost_with_fake_cli(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"result\":\"{\\\"proposals\\\":[]}\",\"total_cost_usd\":0.0123}'\n")
    fake.chmod(0o755)
    c = T.TriageClient(claude_path=str(fake))
    assert c.complete("s", "u", 0.5) == '{"proposals":[]}'
    c.complete("s", "u", 0.5)
    assert c.spent_usd == pytest.approx(0.0246)
    bad = tmp_path / "bad"
    bad.write_text("#!/bin/sh\necho '{\"is_error\":true,\"subtype\":\"error_max_budget_usd\",\"total_cost_usd\":0.2}'\nexit 1\n")
    bad.chmod(0o755)
    cb = T.TriageClient(claude_path=str(bad))
    with pytest.raises(T.TriageError):
        cb.complete("s", "u")
    assert cb.spent_usd == pytest.approx(0.2)  # spend is counted even when the call fails


def test_capmap_validation_rejects_bad_shapes():
    with pytest.raises(CapmapError):
        validate_capmap({"subsystems": {}})
    with pytest.raises(CapmapError):
        validate_capmap({"subsystems": {"x": {"description": "d"}}})
    with pytest.raises(CapmapError):
        validate_capmap({"subsystems": {"x": {k: [] for k in ("files", "metrics", "limits", "watch_topics")}
                                              | {"description": "", "current": ""}}, "watch_repos": ["nope"]})


@pytest.mark.skipif(os.environ.get("O_NET") != "1", reason="live model test; set O_NET=1")
def test_live_triage_tiny():
    client = T.TriageClient()
    out = T.triage([sig(1)], CM, client, budget_usd=0.2)
    assert client.spent_usd <= 0.25 and all(0 <= p.score <= 1 for p in out)


# ---------------------------------------------------------------- failures are reported, not swallowed

def test_triage_reports_which_signals_settled_failed_or_waited():
    signals = [sig(i) for i in range(1, 46)] + [sig(99, kind="paper", summary="no code here", repo="")]
    answer = lambda user: json.dumps({"proposals": [good([re.search(r'"id": "([^"]+)"', user).group(1)], title=f"p{len(user)}")]})
    client = FakeClient(["garbage", answer], cost=0.5)  # batch 1 fails, batch 2 answers, batch 3 is beyond the budget
    res = T.TriageResult()
    out = T.triage(signals, CM, client, budget_usd=1.0, max_signals=45, result=res)
    assert len(out) == 1 and len(client.prompts) == 2
    kept = [c.signal.id for c in res.kept]
    assert set(res.failed) == set(kept[:20]) and set(res.unreached) == set(kept[40:])
    assert set(res.done) == set(kept[20:40]) | {"s99"}  # the paper without code was filtered out for good
    assert not (set(res.done) & set(res.failed)) and not (set(res.failed) & set(res.unreached))
    assert len(res.done) + len(res.failed) + len(res.unreached) == len(signals)


def test_triage_raises_when_every_batch_fails():
    signals = [sig(i) for i in range(1, 26)]
    client = FakeClient(["not json", "still not json"], cost=0.1)
    res = T.TriageResult()
    with pytest.raises(T.TriageError, match="every triage batch failed"):
        T.triage(signals, CM, client, budget_usd=1.0, max_signals=30, result=res)
    assert len(res.failed) == 25 and not res.done and res.spent_usd == pytest.approx(0.2)

    class Broken:  # the CLI itself is unusable: the pipeline must hear the reason, not get []
        spent_usd = 0.0

        def complete(self, system, user, max_budget_usd=None):
            raise T.TriageError("the claude CLI is not logged in: run `claude login`")

    with pytest.raises(T.TriageError, match="not logged in"):
        T.triage([sig(1)], CM, Broken(), budget_usd=0.5)


def test_explain_cli_failure_names_the_usual_causes():
    assert "too old" in T.explain_cli_failure(1, "error: unknown option '--strict-mcp-config'")
    assert "not logged in" in T.explain_cli_failure(1, "Not logged in. Please run `claude login`")
    assert "out of budget" in T.explain_cli_failure(1, "API error 429: rate limit exceeded")
    assert T.explain_cli_failure(2, "something else").startswith("claude cli exited 2: something else")
    assert "no JSON output" in T.explain_cli_failure(0, "")
    missing = T.TriageClient(claude_path="/nonexistent/claude-cli-for-test")
    with pytest.raises(T.TriageError, match="not found"):
        missing.complete("s", "u")


def test_validated_proposal_keeps_its_files():
    c = T.Candidate(sig(1), {"cu_grounding": 3}, 0.5, True)
    p = T.validate_proposal(good(["s1"], files=["docs/grounding.md"]), {"s1": c}, CM)
    assert p.files == ["docs/grounding.md"]
