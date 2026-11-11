"""Evolve watchers + store: fakes only (httpx.MockTransport). Live scan runs only with O_NET=1."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from o_engine.evolve import sources as S
from o_engine.evolve.capmap import CAPMAP_PATH, load_capmap
from o_engine.evolve.schema import Proposal, Signal
from o_engine.evolve.store import EvolveStore, default_path

NOW = datetime.now(timezone.utc)


def ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mini_capmap(**over) -> dict:
    cm = {
        "subsystems": {"cu_grounding": {"description": "d", "current": "c", "files": [], "metrics": [], "limits": [],
                                        "watch_topics": ["gui-agent", "computer-use"]}},
        "watch_repos": ["acme/agent"],
        "search_topics": ["computer-use"],
        "arxiv": {"categories": ["cs.AI"], "terms": ["GUI agent"]},
    }
    cm.update(over)
    return cm


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "engine").mkdir()
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"ws": "^8.0.0"}, "devDependencies": {"electron": "^33"}}))
    (tmp_path / "engine" / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=68"]\n\n[project]\nname = "o-engine"\n'
        'dependencies = [\n  "httpx>=0.27",\n  "pydantic>=2.6",\n]\n\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8"]\nambient = [\n  "redis>=5",  # comment\n]\n'
        '\n[tool.setuptools.packages.find]\ninclude = ["o_engine*"]\n')
    return tmp_path


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
 <entry><id>http://arxiv.org/abs/2611.00001v1</id><title>Zoom GUI agent</title>
  <summary>We ground GUI agent clicks. Code: https://github.com/lab/zoomer.</summary>
  <published>{recent}</published><arxiv:comment>10 pages</arxiv:comment>
  <category term="cs.AI"/><link href="http://arxiv.org/abs/2611.00001v1"/></entry>
 <entry><id>http://arxiv.org/abs/2611.00002v1</id><title>No code GUI agent</title>
  <summary>Nothing linked.</summary><published>{recent}</published><category term="cs.AI"/></entry>
 <entry><id>http://arxiv.org/abs/2601.00003v1</id><title>Old GUI agent</title>
  <summary>https://github.com/lab/old</summary><published>{old}</published><category term="cs.AI"/></entry>
</feed>"""


class World:
    """A fake internet. ``rest_blocked`` mimics a proxy that refuses /repos/* on api.github.com."""

    def __init__(self, rest_blocked: bool = False) -> None:
        self.rest_blocked = rest_blocked
        self.calls: list[httpx.Request] = []
        self.mcp_servers = [self.mcp("io.github.acme/files", ts(1)), self.mcp("io.github.acme/old", ts(40))]
        self.releases = {"acme/agent": [{"tag_name": "v2.0", "name": "v2.0", "published_at": ts(2), "body": "new planner",
                                         "html_url": "https://github.com/acme/agent/releases/v2.0", "draft": False},
                                        {"tag_name": "v1.0", "published_at": ts(60), "body": "old", "draft": False}],
                         "encode/httpx": [{"tag_name": "0.29.0", "published_at": ts(3), "body": "http3", "draft": False}]}
        self.etag_hits = 0

    @staticmethod
    def mcp(name: str, published: str) -> dict:
        return {"server": {"name": name, "description": "Read files", "version": "1.0.0",
                           "repository": {"url": f"https://github.com/{name.split('/')[-1]}/srv"},
                           "remotes": [{"type": "streamable-http", "url": "https://x"}]},
                "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "publishedAt": published}}}

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.headers["user-agent"].startswith("O-Evolve/1.0")
        u, path = req.url, req.url.path
        host = u.host
        if host == "pypi.org":
            name = path.split("/")[2]
            ver = {"httpx": "0.29.0", "pydantic": "2.14.0", "pytest": "9.0.0", "redis": "6.0.0"}[name]
            repo = {"httpx": "https://github.com/encode/httpx", "pydantic": "https://github.com/pydantic/pydantic"}.get(name, "")
            if req.headers.get("if-none-match") == f'"{name}"':
                self.etag_hits += 1
                return httpx.Response(304)
            return httpx.Response(200, headers={"etag": f'"{name}"'}, json={
                "info": {"version": ver, "summary": f"{name} lib", "project_urls": {"Source": repo} if repo else {}},
                "releases": {ver: [{"upload_time_iso_8601": ts(3 if name == "httpx" else 30)}]}})
        if host == "registry.npmjs.org" and path == "/-/v1/search":
            name = u.params["text"]
            return httpx.Response(200, json={"objects": [{"package": {
                "name": name, "version": "9.0.0", "date": ts(1), "description": "d",
                "links": {"repository": f"git+https://github.com/{name}js/{name}.git"}}}]})
        if host == "registry.npmjs.org" and path == "/fast-mcp":
            return httpx.Response(200, json={"dist-tags": {"latest": "1.4.0"}, "time": {"1.4.0": ts(1)}})
        if host == "registry.npmjs.org":
            return httpx.Response(200, json={"time": {"2.1.300": ts(1), "2.1.299": ts(30)}})
        if host == "api.github.com" and path.startswith("/search/repositories"):
            q = u.params["q"]
            if q.startswith("repo:"):
                return httpx.Response(200, json={"items": [{"license": {"spdx_id": "MIT"}, "stargazers_count": 7,
                                                            "archived": False, "topics": ["gui-agent"]}]})
            return httpx.Response(200, json={"items": [
                {"full_name": "new/fast", "html_url": "https://github.com/new/fast", "description": "A CLI for computer-use",
                 "stargazers_count": 300, "created_at": ts(3), "license": {"spdx_id": "MIT"}, "topics": ["computer-use"],
                 "homepage": "https://fast.dev", "archived": False, "fork": False},
                {"full_name": "new/older", "html_url": "https://github.com/new/older", "description": "gui agent",
                 "stargazers_count": 140, "created_at": ts(14), "license": {"spdx_id": "NOASSERTION"},
                 "topics": [], "archived": False, "fork": False},
                {"full_name": "new/dead", "stargazers_count": 999, "created_at": ts(2), "archived": True, "fork": False},
                {"full_name": "new/tiny", "stargazers_count": 3, "created_at": ts(2), "archived": False, "fork": False}]})
        if host == "api.github.com" and path.startswith("/repos/"):
            if self.rest_blocked:
                return httpx.Response(403, json={"message": "GitHub access to this repository is not enabled for this session."})
            parts = path.split("/")
            repo = f"{parts[2]}/{parts[3]}"
            if path.endswith("/releases"):
                return httpx.Response(200, json=self.releases.get(repo, []))
            return httpx.Response(200, json={"license": {"spdx_id": "Apache-2.0"}, "archived": False,
                                             "stargazers_count": 1000, "topics": ["computer-use"]})
        if host == "raw.githubusercontent.com":
            if path == "/new/fast/HEAD/package.json":  # a published MCP server: resolves to npm:fast-mcp
                return httpx.Response(200, json={"name": "fast-mcp", "bin": {"fast-mcp": "cli.js"},
                                                 "dependencies": {"@modelcontextprotocol/sdk": "^1.0.0"}})
            if path.startswith("/anthropics/claude-code/"):
                return httpx.Response(200, text="# Changelog\n\n## 2.1.300\n\n- Added MCP tasks\n\n## 2.1.299\n\n- old\n")
            if path.startswith("/anthropics/anthropic-sdk-python/"):
                return httpx.Response(200, text=f"## [2.6.0](https://x) ({ts(1)[:10]})\n\n* feat\n\n## [2.0.0](https://x) (2026-01-01)\n")
            if path.startswith("/acme/agent/"):
                return httpx.Response(200, text=f"# Changes\n\n## [2.0] - {ts(2)[:10]}\n\n- new planner\n\n## [1.0] - 2025-01-01\n")
            return httpx.Response(404)
        if host == "export.arxiv.org":
            return httpx.Response(200, text=ATOM.format(recent=ts(2), old=ts(300)))
        if host == "registry.modelcontextprotocol.io":
            since = S.parse_ts(u.params["updated_since"])
            items = [m for m in self.mcp_servers
                     if S.parse_ts(m["_meta"]["io.modelcontextprotocol.registry/official"]["publishedAt"]) >= since]
            return httpx.Response(200, json={"servers": items, "metadata": {"count": len(items)}})
        return httpx.Response(404)


def run_scan(store: EvolveStore, world: World, root: Path, **kw) -> S.ScanReport:
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(world.handler)) as client:
            return await S.ascan(store, client=client, capmap=kw.pop("capmap", mini_capmap()), root=root,
                                 sleep=kw.pop("sleep", _nosleep), **kw)
    return asyncio.run(go())


async def _nosleep(_s: float) -> None:
    return None


# ---------------------------------------------------------------- helpers
def test_github_repo_forms():
    assert S.github_repo("git+https://github.com/websockets/ws.git") == "websockets/ws"
    assert S.github_repo("git@github.com:foo/bar.git") == "foo/bar"
    assert S.github_repo("https://github.com/a/b/tree/main/x") == "a/b"
    assert S.github_repo("https://gitlab.com/a/b") == ""


def test_declared_deps_reads_package_json_and_pyproject_lists_only(root):
    deps = {(d.ecosystem, d.name): d.spec for d in S.read_declared_deps(root)}
    assert deps == {("npm", "ws"): "^8.0.0", ("npm", "electron"): "^33", ("pypi", "httpx"): ">=0.27",
                    ("pypi", "pydantic"): ">=2.6", ("pypi", "pytest"): ">=8", ("pypi", "redis"): ">=5"}


def test_declared_deps_of_this_repo_cover_known_packages():
    names = {d.name for d in S.read_declared_deps()}
    assert {"@playwright/mcp", "ws", "electron", "pydantic", "httpx", "mcp", "browser-use", "microsandbox"} <= names
    assert "setuptools>=68" not in names and "o_engine*" not in names


def test_parse_changelog_styles():
    e = S.parse_changelog("## [2.6.0](https://x) (2026-11-05)\n* a\n## 2.1.3\n- b\n## [1.0] - 2025-01-02\n")
    assert [(x["version"], x["date"]) for x in e] == [("2.6.0", "2026-11-05"), ("2.1.3", ""), ("1.0", "2025-01-02")]


def test_signal_ids_are_stable_and_distinct():
    assert S.signal_id("paper", "u", "") == S.signal_id("paper", "u", "")
    assert S.signal_id("paper", "u", "1") != S.signal_id("paper", "u", "2")


# ---------------------------------------------------------------- full scan
def test_scan_all_sources_with_rest_api(root):
    store, world = EvolveStore(":memory:"), World()
    r = run_scan(store, world, root)
    assert all(v["ok"] for k, v in r.per_source.items() if k != "deps"), r.per_source
    by = {}
    for s in r.new:
        by.setdefault(s.kind, []).append(s)
    rel = {s.repo: s for s in by["repo_release"]}
    assert set(rel) == {"acme/agent", "encode/httpx"}  # old v1.0 outside the window
    assert rel["acme/agent"].license == "Apache-2.0" and "watch" in rel["acme/agent"].topics
    assert "dependency" in rel["encode/httpx"].topics
    pk = {s.title for s in by["package_release"]}
    assert "httpx 0.29.0 (pypi)" in pk and "ws 9.0.0 (npm)" in pk and not any("pydantic" in t for t in pk)
    repos = {s.repo: s for s in by["new_repo"]}
    assert set(repos) == {"new/fast", "new/older"}  # archived and below-min-stars dropped
    assert repos["new/fast"].stars_7d == 300 and "skill" in repos["new/fast"].topics
    # the skill tag survives only because the repo resolved to a published MCP-server package
    assert repos["new/fast"].package == "npm:fast-mcp" and repos["new/fast"].version == "1.4.0"
    assert "skill" not in repos["new/older"].topics  # no package.json / pyproject -> not a candidate
    assert repos["new/older"].stars_7d == 70 and repos["new/older"].license == ""
    assert [p.repo for p in by["paper"]] == ["lab/zoomer"]
    mcp = by["mcp_server"]
    # a registry server with no npm/PyPI package (hosted / remote only) is not an installable candidate
    assert len(mcp) == 1 and "skill" not in mcp[0].topics and mcp[0].repo == "files/srv"
    ch = {s.version for s in by["changelog"]}
    assert ch == {"2.1.300", "2.6.0"}
    # second identical scan: everything is a duplicate
    again = run_scan(store, world, root)
    assert again.new == []


def test_rest_refused_falls_back_to_changelog_and_search_license(root):
    store, world = EvolveStore(":memory:"), World(rest_blocked=True)
    r = run_scan(store, world, root, sources=["github"])
    rel = [s for s in r.new if s.repo == "acme/agent"]
    assert len(rel) == 1 and rel[0].version == "2.0" and rel[0].license == "MIT"  # license via search repo:
    # learned: the next scan skips REST and the 404 CHANGELOG repos entirely
    n_before = len(world.calls)
    run_scan(store, world, root, sources=["github"])
    later = world.calls[n_before:]
    assert not any(c.url.path.endswith("/releases") for c in later)
    assert not any(c.url.host == "raw.githubusercontent.com" and "encode/httpx" in c.url.path for c in later)


def test_etag_revalidation_uses_cache(root):
    store, world = EvolveStore(":memory:"), World()
    run_scan(store, world, root, sources=["packages"])
    run_scan(store, world, root, sources=["packages"])
    assert world.etag_hits == 4  # all four pypi deps answered 304 on the second scan


def test_incremental_returns_only_new_items(root):
    store, world = EvolveStore(":memory:"), World()
    first = run_scan(store, world, root, incremental=True, sources=["mcp-registry"])
    assert len(first.new) == 1
    cur = store.get_cursor("mcp-registry")
    assert cur["last_scan"]
    world.mcp_servers.append(World.mcp("io.github.acme/brand-new", ts(0)))
    second = run_scan(store, world, root, incremental=True, sources=["mcp-registry"])
    assert [s.title for s in second.new] == ["MCP server io.github.acme/brand-new 1.0.0"]
    reg_calls = [c for c in world.calls if "updated_since" in c.url.params]
    sent = S.parse_ts(reg_calls[-1].url.params["updated_since"])
    assert sent > NOW - timedelta(hours=2)  # resumed from the cursor, not the 14-day window


def test_rate_limit_short_wait_retries_long_wait_stops(root):
    store = EvolveStore(":memory:")
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)

    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, text=ATOM.format(recent=ts(1), old=ts(400)))

    async def go(h):
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            return await S.ascan(store, client=c, capmap=mini_capmap(), root=root, sources=["arxiv"], sleep=sleep)

    r = asyncio.run(go(handler))
    assert slept == [2.0] and r.per_source["arxiv"]["ok"] and len(r.new) == 1

    def limited(req):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0",
                                            "x-ratelimit-reset": str(int(datetime.now().timestamp()) + 3600)})

    r2 = asyncio.run(go(limited))
    assert not r2.per_source["arxiv"]["ok"] and "RateLimited" in r2.per_source["arxiv"]["error"]
    assert slept == [2.0]  # an hour-long reset is not slept inline


def test_invalid_github_token_falls_back_to_anonymous():
    seen = []

    def handler(req):
        seen.append(req.headers.get("authorization"))
        if req.headers.get("authorization"):
            return httpx.Response(401)
        return httpx.Response(200, json={"items": []})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            f = S.Fetcher(c, None, github_token="bad")
            return await f.json("https://api.github.com/search/repositories", {"q": "x"})

    assert asyncio.run(go()) == {"items": []}
    assert seen == ["Bearer bad", None]


def test_unreachable_source_is_reported_not_raised(root):
    store = EvolveStore(":memory:")

    def handler(req):
        raise httpx.ConnectError("down", request=req)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await S.ascan(store, client=c, capmap=mini_capmap(), root=root, sources=["mcp-registry"])

    r = asyncio.run(go())
    assert r.per_source["mcp-registry"]["ok"] is False and r.new == []


# ---------------------------------------------------------------- store
def _sig(i: int, **kw) -> Signal:
    base = dict(id=f"s{i}", kind="paper", source="arxiv", title=f"t{i}", url=f"https://x/{i}")
    base.update(kw)
    return Signal(**base)


def _prop(pid="p1") -> Proposal:
    return Proposal(id=pid, signal_ids=["s1"], subsystem="cu_grounding", title="t", rationale="r", spec="s",
                    acceptance_test="a", score=0.5)


def test_store_default_path_respects_o_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("O_DATA_DIR", str(tmp_path))
    assert default_path() == tmp_path / "evolve" / "evolve.sqlite"
    st = EvolveStore()
    assert Path(st.path).exists()


def test_store_dedupes_and_queries(tmp_path):
    st = EvolveStore(tmp_path / "e.sqlite")
    assert len(st.add_signals([_sig(1), _sig(2, topics=["skill"])])) == 2
    assert st.add_signals([_sig(1), _sig(3)]) == [_sig(3)]
    assert st.has_signal("s2") and st.get_signal("s2").topics == ["skill"]
    assert [s.id for s in st.signals(topic="skill")] == ["s2"]
    assert len(st.signals(kind="paper", limit=2)) == 2
    assert st.count_signals() == {"arxiv": 3}


def test_decisions_ledger_is_append_only(tmp_path):
    st = EvolveStore(tmp_path / "e.sqlite")
    st.add_proposal(_prop())
    assert st.add_proposal(_prop()) is False
    st.record_decision("p1", "accepted", "looks good")
    st.record_decision("p1", "rejected", "regressed", {"osworld": 0.4})
    assert st.status("p1") == "rejected" and st.get_proposal("p1").status == "rejected"
    assert [d["status"] for d in st.decisions("p1")] == ["accepted", "rejected"]
    assert st.decisions("p1")[1]["metrics"] == {"osworld": 0.4}
    assert st.proposals(status="rejected")[0].id == "p1" and st.proposals(status="accepted") == []
    with pytest.raises(ValueError):
        st.record_decision("p1", "nonsense")
    with pytest.raises(KeyError):
        st.record_decision("nope", "accepted")
    db = sqlite3.connect(st.path)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE decisions SET status='shipped'")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM decisions")


def test_cursors_roundtrip():
    st = EvolveStore(":memory:")
    assert st.get_cursor("x") == {}
    st.set_cursor("x", {"last_scan": "2026-11-06T00:00:00Z"})
    st.set_cursor("x", {"last_scan": "2026-11-07T00:00:00Z"})
    assert st.cursors() == {"x": {"last_scan": "2026-11-07T00:00:00Z"}}


# ---------------------------------------------------------------- capability map
def test_capability_map_is_valid_and_cites_real_files():
    cm = load_capmap()
    repo = CAPMAP_PATH.resolve().parents[3]
    for key, sub in cm["subsystems"].items():
        for f in sub["files"]:
            assert (repo / f.split(" ")[0]).exists(), f"{key}: {f} does not exist"
    assert "simular-ai/Agent-S" in cm["watch_repos"] and "anthropics/claude-code" in cm["watch_repos"]
    assert {"computer-use", "gui-agent", "mcp-server", "browser-agent", "agent-memory", "screen-parsing"} <= set(cm["search_topics"])


# ---------------------------------------------------------------- live
@pytest.mark.skipif(os.environ.get("O_NET") != "1", reason="live network test; set O_NET=1")
def test_live_scan_smoke(tmp_path):
    st = EvolveStore(tmp_path / "live.sqlite")
    r = S.scan(st, since_days=14, limit=10)
    assert any(v.get("ok") for k, v in r.per_source.items() if k != "deps"), r.per_source
