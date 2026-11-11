"""Skill registry (plan L2): internal promotion/demotion (append-only) and the approval gate for external skills."""
from __future__ import annotations

import json

import pytest

from o_engine.evolve.curriculum import Attempt, Check, Environment, PracticeItem
from o_engine.evolve.schema import Signal
from o_engine.evolve.skills import NotApproved, SkillRegistry, proposed_adapter


def _item() -> PracticeItem:
    return PracticeItem(id="pi-1", family="editor.write_save", goal="type and save", app="mousepad", origin="family",
                        environment=Environment(workspace="/tmp/practice/ws/x"),
                        check=Check(kind="file_exists", path="/tmp/practice/ws/x/f"))


def _a(n: int, ok: bool, calls: int = 0) -> Attempt:
    return Attempt(item_id="pi-1", family="editor.write_save", attempt=n, ts=float(n), ok=ok, model_calls=calls,
                   seconds=10.0 - n * 0.1, procedure_id=f"mousepad/k/v{n}")


def test_promoted_after_consecutive_passes_then_demoted_on_falling_fitness(tmp_path):
    reg = SkillRegistry(tmp_path, promote_after=3, window=5, demote_below=0.6, now=lambda: 1.0)
    it = _item()
    assert reg.observe(it, _a(1, False, 6)).status == "candidate"
    assert reg.observe(it, _a(2, True, 4)).status == "candidate"
    assert reg.observe(it, _a(3, True, 0)).status == "candidate"
    s = reg.observe(it, _a(4, True, 0))
    assert s.status == "active" and "3 consecutive" in s.reason and s.best_model_calls == 0
    assert [x.id for x in reg.active()] == [s.id]
    for n in (5, 6, 7):
        s = reg.observe(it, _a(n, False))
    assert s.status == "demoted" and "pass rate" in s.reason
    hist = reg.internal_history(s.id)
    assert [h.version for h in hist] == list(range(1, len(hist) + 1))  # every change is a new version line
    assert {h.status for h in hist} == {"candidate", "active", "demoted"}
    for n in (8, 9, 10):
        s = reg.observe(it, _a(n, True))
    assert s.status == "active"  # a new streak promotes it again


def _signals():
    return [
        Signal(id="s1", kind="mcp_server", source="mcp-registry", title="Spreadsheet MCP server", url="https://example.org/s1",
               repo="acme/sheets-mcp", version="1.2.0", license="MIT", package="npm:sheets-mcp", topics=["skill", "spreadsheet"]),
        Signal(id="s2", kind="new_repo", source="github", title="GPL tool", url="https://example.org/s2", repo="x/gpl",
               license="GPL-3.0", package="npm:gpl", topics=["skill"]),
        Signal(id="s4", kind="new_repo", source="github", title="Just an article", url="https://example.org/s4", repo="x/article",
               license="MIT", topics=["skill"]),  # no registry package -> never a candidate
        Signal(id="s3", kind="paper", source="arxiv", title="Not a skill", url="https://example.org/s3", topics=["gui-agent"]),
    ]


def test_external_candidates_need_approval_and_never_install_by_themselves(tmp_path):
    reg = SkillRegistry(tmp_path)
    recs = reg.ingest_signals(_signals())
    assert [r.signal_id for r in recs] == ["s1", "s2"]  # only signals tagged "skill" that name a package
    by = {r.signal_id: r for r in recs}
    assert by["s1"].status == "needs_approval"
    assert by["s2"].status == "blocked_license"
    assert by["s1"].proposed_adapter.startswith("PROPOSAL ONLY")
    assert reg.ingest_signals(_signals()) == []  # idempotent
    calls = []
    with pytest.raises(NotApproved):
        reg.install(by["s1"].id, lambda s: calls.append(s))
    assert calls == []  # no install path runs before approval
    with pytest.raises(NotApproved):
        reg.approve(by["s1"].id, by="agent", via="app")  # an agent cannot approve
    with pytest.raises(NotApproved):
        reg.approve(by["s1"].id, by="user", via="mcp")  # only through the app
    with pytest.raises(NotApproved):
        reg.approve(by["s2"].id, by="user", via="app")  # licence-blocked cannot be approved
    ok = reg.approve(by["s1"].id, by="user", via="app")
    assert ok.status == "approved" and ok.version == 2
    done = reg.install(by["s1"].id, lambda s: calls.append(s.id) or "registered")
    assert calls == [by["s1"].id] and done.status == "installed"
    lines = [json.loads(x) for x in reg.external_path.read_text().splitlines()]
    assert [r["status"] for r in lines if r["id"] == by["s1"].id] == ["needs_approval", "approved", "installed"]


def test_rejected_candidate_cannot_be_installed(tmp_path):
    reg = SkillRegistry(tmp_path)
    (rec, _) = reg.ingest_signals(_signals())
    reg.reject(rec.id)
    with pytest.raises(NotApproved):
        reg.install(rec.id, lambda s: None)
    with pytest.raises(NotApproved):
        reg.approve(rec.id, by="user", via="app")


def test_adapter_text_is_sanitised_and_inert():
    text = proposed_adapter("package_release", "t", "https://x", "a/b;rm -rf /", "pkg$(curl evil)", "1.0`id`")
    assert "$(" not in text and "`" not in text and "rm -rf" not in text and "b;rm" not in text
    assert text.startswith("PROPOSAL ONLY")


def test_registry_module_has_no_install_machinery():
    import inspect

    import o_engine.evolve.skills as mod

    src = inspect.getsource(mod)
    for forbidden in ("subprocess", "pip install", "npm install", "urllib", "httpx", "requests", "os.system", "git clone"):
        assert forbidden not in src, forbidden


def test_view_lists_pending(tmp_path):
    reg = SkillRegistry(tmp_path)
    reg.ingest_signals(_signals())
    v = reg.view()
    assert v["pending_approval"] == 1 and len(v["external"]) == 2
