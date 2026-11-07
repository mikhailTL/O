"""Watchers for new AI developments (plan L step 1). Each source returns ``list[Signal]``.

Sources (all over ``httpx`` with timeouts, the polite UA ``O-Evolve/1.0``, ETag / If-Modified-Since
revalidation through ``EvolveStore``'s http cache, and rate-limit back-off):

* ``github_releases``: releases of every repo O depends on (``package.json`` deps -> npm registry
  ``links.repository``; ``engine/pyproject.toml`` deps -> PyPI ``project_urls``) plus the curated
  ``watch_repos`` in ``capability_map.json``. When the GitHub REST API refuses a repo for a reason other
  than a rate limit (some proxies only allow attached repos), the repo's ``CHANGELOG.md`` on
  raw.githubusercontent.com is parsed instead (dated headings only).
* ``github_new_repos``: GitHub search by capability-map topics, created in the last N days, sorted by
  stars; ``stars_7d`` is exact for repos younger than 7 days, else the average 7-day velocity since
  creation. Only repos at or above ``min_stars`` become signals, so a repo is signalled when it crosses
  the threshold. CLIs / SDKs / APIs / libraries with docs also get the topic ``skill``.
* ``arxiv``: export.arxiv.org Atom API for the capability map's categories and terms; only papers whose
  abstract or comments link github.com.
* ``anthropic``: Claude Code (CHANGELOG.md, npm publish times) and anthropic-sdk-python (CHANGELOG.md).
* ``mcp_registry``: registry.modelcontextprotocol.io ``/v0/servers?updated_since=``; every server is a
  ``skill`` signal (something O can learn to *use* at runtime). Skipped when unreachable.
* ``packages``: PyPI / npm latest versions of O's own deps -> ``package_release`` signals.

``scan`` runs them concurrently and stores the results; ``scan_all`` is a full-window scan and
``scan_incremental`` resumes from per-source cursors (persisted in the store), so it is cheap enough to
run every 30-60 minutes: unchanged endpoints answer ``304``, only items newer than the cursor are kept.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode, urlparse

import httpx

from .capmap import load_capmap
from .schema import Signal
from .store import EvolveStore, now_iso

UA = "O-Evolve/1.0 (+https://github.com/mikhailTL/O)"
TIMEOUT = httpx.Timeout(15.0, connect=8.0)
REPO_ROOT = Path(__file__).resolve().parents[3]
GH_API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
ANTHROPIC_REPOS = ("anthropics/claude-code", "anthropics/anthropic-sdk-python")
MAX_WAIT_S = 20.0  # longest rate-limit wait honoured inline; longer -> the source stops for this scan
UNMAINTAINED_DAYS = 365
SKILL_WORDS = re.compile(r"\b(cli|command[- ]line|sdk|api|library|toolkit|mcp|plugin|framework)\b", re.I)
_GH_REPO = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


# --------------------------------------------------------------------------- helpers
def signal_id(kind: str, url: str, version: str = "") -> str:
    return hashlib.sha256(f"{kind}|{url}|{version}".encode()).hexdigest()[:20]


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.strptime(v[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def github_repo(url: str | None) -> str:
    """``owner/name`` from any github URL form (git+https, git@, .git suffix, deep links); else ''."""
    if not url:
        return ""
    m = _GH_REPO.search(url)
    if not m:
        return ""
    owner, name = m.group(1), m.group(2)
    name = re.sub(r"\.git$", "", name).rstrip(".")
    if owner.lower() in {"orgs", "sponsors", "topics", "features", "about"} or not name:
        return ""
    return f"{owner}/{name}"


def _clip(text: str | None, n: int) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _sig(**kw: Any) -> Signal:
    kw["title"] = _clip(kw.get("title"), 400) or "(untitled)"
    kw["summary"] = _clip(kw.get("summary"), 4000)
    kw.setdefault("id", signal_id(kw["kind"], kw["url"], kw.get("version", "")))
    kw["topics"] = sorted({t for t in kw.get("topics", []) if t})
    return Signal(**kw)


def _spdx(lic: Any) -> str:
    if isinstance(lic, dict):
        s = lic.get("spdx_id") or ""
        return "" if s in ("NOASSERTION", "NONE") else s
    return ""


class RateLimited(Exception):
    pass


class Blocked(Exception):
    """The endpoint refused for a policy reason (not a rate limit)."""


# --------------------------------------------------------------------------- fetcher
class Fetcher:
    """Conditional GETs with a shared cache, per-host back-off and request accounting."""

    def __init__(self, client: httpx.AsyncClient, store: EvolveStore | None = None, *,
                 github_token: str | None = None, max_wait: float = MAX_WAIT_S,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, concurrency: int = 8) -> None:
        self.client = client
        self.store = store
        self.github_token = github_token
        self.max_wait = max_wait
        self.sleep = sleep
        self.sem = asyncio.Semaphore(concurrency)
        self.blocked_until: dict[str, float] = {}
        self.stats = {"requests": 0, "not_modified": 0, "errors": 0, "rate_limited": 0}

    async def get(self, url: str, params: dict[str, Any] | None = None, *, accept: str | None = None,
                  cache: bool = True) -> str:
        full = url + ("?" + urlencode(params, safe=":/+,") if params else "")
        host = urlparse(full).netloc
        if self.blocked_until.get(host, 0) > time.monotonic():
            raise RateLimited(f"{host} backing off")
        cached = self.store.http_get(full) if (cache and self.store) else None
        headers = {"User-Agent": UA}
        if accept:
            headers["Accept"] = accept
        if host == "api.github.com":
            headers.setdefault("Accept", "application/vnd.github+json")
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if self.github_token:
                headers["Authorization"] = f"Bearer {self.github_token}"
        if cached:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]
        for attempt in range(2):
            async with self.sem:
                self.stats["requests"] += 1
                try:
                    r = await self.client.get(full, headers=headers, timeout=TIMEOUT, follow_redirects=True)
                except httpx.HTTPError as e:
                    self.stats["errors"] += 1
                    raise Blocked(f"{host}: {type(e).__name__}: {e}") from None
            if r.status_code == 304 and cached:
                self.stats["not_modified"] += 1
                return cached["body"]
            if r.status_code == 401 and "Authorization" in headers:
                # An invalid optional token must not break unauthenticated access.
                self.github_token = None
                headers.pop("Authorization", None)
                continue
            if r.status_code in (403, 429):
                wait = self._rate_wait(r)
                if wait is None:
                    self.stats["errors"] += 1
                    raise Blocked(f"{host} {r.status_code}: {_clip(r.text, 160)}")
                self.stats["rate_limited"] += 1
                if wait <= self.max_wait and attempt == 0:
                    await self.sleep(max(wait, 1.0))
                    continue
                self.blocked_until[host] = time.monotonic() + wait
                raise RateLimited(f"{host} rate limited for {int(wait)}s")
            if r.status_code >= 400:
                self.stats["errors"] += 1
                raise Blocked(f"{host} {r.status_code}")
            text = r.text
            if cache and self.store and (r.headers.get("etag") or r.headers.get("last-modified")):
                self.store.http_put(full, r.headers.get("etag", ""), r.headers.get("last-modified", ""), text)
            return text
        raise RateLimited(f"{host} still limited after retry")

    @staticmethod
    def _rate_wait(r: httpx.Response) -> float | None:
        """Seconds to wait when the response is a rate limit, else ``None`` (a policy refusal)."""
        ra = r.headers.get("retry-after")
        if ra:
            try:
                return float(ra)
            except ValueError:
                return 60.0
        if r.headers.get("x-ratelimit-remaining") == "0":
            reset = r.headers.get("x-ratelimit-reset")
            try:
                return max(0.0, float(reset) - time.time()) if reset else 60.0
            except ValueError:
                return 60.0
        if r.status_code == 429:
            return 60.0
        body = r.text.lower()
        if "rate limit" in body:
            return 60.0
        return None

    async def json(self, url: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        return json.loads(await self.get(url, params, **kw))


# --------------------------------------------------------------------------- deps
@dataclass
class Dep:
    ecosystem: str  # "pypi" | "npm"
    name: str
    spec: str
    latest: str = ""
    published: str = ""
    repo: str = ""
    summary: str = ""


_PY_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")


def _pyproject_deps(text: str) -> list[tuple[str, str]]:
    """(name, spec) from ``[project] dependencies`` and every ``[project.optional-dependencies]`` list."""
    out: list[tuple[str, str]] = []
    section = ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].split("#", 1)[0] if not lines[i].lstrip().startswith("#") else ""
        hs = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if hs:
            section = hs.group(1).strip()
            i += 1
            continue
        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=\s*\[(.*)$", line)
        wanted = (section == "project" and m and m.group(1) == "dependencies") or \
                 (section == "project.optional-dependencies" and m)
        if m and wanted:
            buf = m.group(2)
            while "]" not in buf.split("#")[0] and i + 1 < len(lines):
                i += 1
                buf += "\n" + lines[i].split("#", 1)[0]
            for item in re.findall(r'"([^"]+)"', buf.rsplit("]", 1)[0]):
                nm = _PY_NAME.match(item.split(";")[0])
                if nm:
                    out.append((nm.group(1), nm.group(3).strip()))
        i += 1
    return out


def read_declared_deps(root: Path = REPO_ROOT) -> list[Dep]:
    """O's declared deps: package.json (dependencies + devDependencies) and pyproject (deps + extras)."""
    deps: list[Dep] = []
    pj = root / "package.json"
    if pj.exists():
        data = json.loads(pj.read_text())
        for section in ("dependencies", "devDependencies"):
            for name, spec in (data.get(section) or {}).items():
                deps.append(Dep("npm", name, str(spec)))
    pp = root / "engine" / "pyproject.toml"
    if pp.exists():
        seen: set[str] = set()
        for name, spec in _pyproject_deps(pp.read_text()):
            if name.lower() not in seen:
                seen.add(name.lower())
                deps.append(Dep("pypi", name, spec))
    return deps


async def resolve_dep(f: Fetcher, d: Dep) -> Dep:
    try:
        if d.ecosystem == "pypi":
            j = await f.json(f"https://pypi.org/pypi/{quote(d.name)}/json")
            info = j.get("info") or {}
            d.latest = info.get("version") or ""
            d.summary = info.get("summary") or ""
            files = (j.get("releases") or {}).get(d.latest) or j.get("urls") or []
            times = [x.get("upload_time_iso_8601") or "" for x in files if isinstance(x, dict)]
            d.published = min([t for t in times if t], default="")
            urls = list((info.get("project_urls") or {}).values()) + [info.get("home_page") or ""]
            for u in urls:
                r = github_repo(u)
                if r:
                    d.repo = r
                    break
        else:
            j = await f.json("https://registry.npmjs.org/-/v1/search", {"text": d.name, "size": 5})
            for o in j.get("objects") or []:
                p = o.get("package") or {}
                if p.get("name") == d.name:
                    d.latest = p.get("version") or ""
                    d.published = p.get("date") or ""
                    d.summary = p.get("description") or ""
                    links = p.get("links") or {}
                    d.repo = github_repo(links.get("repository")) or github_repo(links.get("homepage"))
                    break
    except (Blocked, RateLimited, ValueError, KeyError):
        pass
    return d


# --------------------------------------------------------------------------- context
@dataclass
class Ctx:
    fetcher: Fetcher
    capmap: dict[str, Any]
    since: dict[str, datetime]  # per source
    limit: int
    root: Path
    cursors: dict[str, dict[str, Any]]
    min_stars: int = 25
    deps: list[Dep] = field(default_factory=list)
    new_cursors: dict[str, dict[str, Any]] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Learned cheapness: REST refused by policy (skip it for a day) and repos without a CHANGELOG.md.
    rest_blocked: bool = False
    no_changelog: set[str] = field(default_factory=set)

    def since_for(self, source: str) -> datetime:
        return self.since.get(source) or self.since["default"]


async def repo_meta(ctx: Ctx, repo: str) -> dict[str, Any]:
    """license/archived/stars/pushed/topics for a repo: REST first, search ``repo:`` when REST is refused."""
    if repo in ctx._meta:
        return ctx._meta[repo]
    meta: dict[str, Any] = {}
    try:
        if ctx.rest_blocked:
            raise Blocked("REST refused earlier")
        j = await ctx.fetcher.json(f"{GH_API}/repos/{repo}")
    except Blocked:
        try:
            s = await ctx.fetcher.json(f"{GH_API}/search/repositories", {"q": f"repo:{repo}"})
            items = s.get("items") or []
            j = items[0] if items else {}
        except (Blocked, RateLimited):
            j = {}
    except RateLimited:
        j = {}
    if j:
        meta = {"license": _spdx(j.get("license")), "archived": bool(j.get("archived")),
                "stars": int(j.get("stargazers_count") or 0), "pushed": j.get("pushed_at") or "",
                "topics": list(j.get("topics") or []), "description": j.get("description") or ""}
    ctx._meta[repo] = meta
    return meta


_CL_HEAD = re.compile(r"^#{2,3}\s+\[?v?(\d+\.\d+(?:\.\d+)?(?:[-.][0-9A-Za-z.]+)?)\]?(?:\([^)]*\))?"
                      r"\s*(?:[-–(]\s*)?\(?(\d{4}-\d{2}-\d{2})?", re.M)


def parse_changelog(text: str, max_entries: int = 10) -> list[dict[str, str]]:
    """``[{version, date, body}]`` from a Keep-a-Changelog / release-please style CHANGELOG.md."""
    heads = list(_CL_HEAD.finditer(text))
    out = []
    for i, m in enumerate(heads[:max_entries]):
        end = heads[i + 1].start() if i + 1 < len(heads) else min(len(text), m.end() + 6000)
        out.append({"version": m.group(1), "date": m.group(2) or "", "body": text[m.end():end].strip()})
    return out


async def _changelog_entries(ctx: Ctx, repo: str) -> list[dict[str, str]]:
    if repo in ctx.no_changelog:
        return []
    try:
        return parse_changelog(await ctx.fetcher.get(f"{RAW}/{repo}/HEAD/CHANGELOG.md"))
    except Blocked as e:
        if " 404" in str(e):
            ctx.no_changelog.add(repo)
    except RateLimited:
        pass
    return []


async def _repo_releases(ctx: Ctx, repo: str, since: datetime, kind: str, source: str,
                         topics: list[str], times: dict[str, str] | None = None) -> list[Signal]:
    """New releases of one repo since ``since`` (REST API, CHANGELOG.md fallback)."""
    entries: list[dict[str, str]] = []
    try:
        if ctx.rest_blocked:
            raise Blocked("REST refused earlier")
        rels = await ctx.fetcher.json(f"{GH_API}/repos/{repo}/releases", {"per_page": 10})
        for r in rels if isinstance(rels, list) else []:
            if r.get("draft"):
                continue
            entries.append({"version": r.get("tag_name") or r.get("name") or "",
                            "date": r.get("published_at") or "", "body": r.get("body") or "",
                            "url": r.get("html_url") or f"https://github.com/{repo}/releases",
                            "name": r.get("name") or "", "prerelease": "1" if r.get("prerelease") else ""})
    except Blocked as e:
        if "not enabled" in str(e) or " 403" in str(e):
            ctx.rest_blocked = True
        entries = await _changelog_entries(ctx, repo)
    else:
        if not entries:  # repos that publish no GitHub releases often keep a CHANGELOG.md
            entries = await _changelog_entries(ctx, repo)
    out: list[Signal] = []
    fresh = []
    day = since.replace(hour=0, minute=0, second=0, microsecond=0)
    for e in entries:
        date = e.get("date") or (times or {}).get(e["version"].lstrip("v"), "")
        dt = parse_ts(date)
        # Date-only changelog headings compare at day granularity; dedupe drops repeats.
        if dt is None or dt < (day if len(date) <= 10 else since):
            continue
        fresh.append((e, date))
    if not fresh:
        return out
    meta = await repo_meta(ctx, repo)
    if meta.get("archived"):
        return out
    for e, date in fresh[: ctx.limit]:
        url = e.get("url") or f"https://github.com/{repo}/blob/HEAD/CHANGELOG.md#{e['version'].replace('.', '')}"
        out.append(_sig(kind=kind, source=source, repo=repo, version=e["version"], url=url, published=date,
                        title=f"{repo} {e.get('name') or e['version']}" + (" (pre-release)" if e.get("prerelease") else ""),
                        summary=e.get("body", ""), stars=meta.get("stars", 0), license=meta.get("license", ""),
                        topics=topics + list(meta.get("topics", []))))
    return out


# --------------------------------------------------------------------------- sources
async def src_github_releases(ctx: Ctx) -> list[Signal]:
    since = ctx.since_for("github")
    dep_repos = {d.repo: d for d in ctx.deps if d.repo}
    watch = [r for r in ctx.capmap.get("watch_repos", []) if r not in ANTHROPIC_REPOS]
    repos = [r for r in dict.fromkeys(list(dep_repos) + watch) if r and r not in ANTHROPIC_REPOS]

    async def one(repo: str) -> list[Signal]:
        topics = ["dependency"] if repo in dep_repos else ["watch"]
        try:
            return await _repo_releases(ctx, repo, since, "repo_release", "github", topics)
        except (RateLimited, Blocked, ValueError):
            return []

    results = await asyncio.gather(*(one(r) for r in repos))
    return [s for group in results for s in group]


async def src_github_new_repos(ctx: Ctx) -> list[Signal]:
    days = max(1, (ctx.now - ctx.since["window"]).days)
    created = (ctx.now - timedelta(days=days)).date().isoformat()
    topics = ctx.capmap.get("search_topics", [])
    out: dict[str, Signal] = {}
    for t in topics:
        try:
            j = await ctx.fetcher.json(f"{GH_API}/search/repositories",
                                       {"q": f"topic:{t} created:>={created}", "sort": "stars", "order": "desc",
                                        "per_page": min(30, max(5, ctx.limit))}, cache=False)
        except RateLimited:
            break
        except Blocked:
            continue
        for it in j.get("items") or []:
            stars = int(it.get("stargazers_count") or 0)
            if it.get("archived") or it.get("fork") or stars < ctx.min_stars:
                continue
            full = it.get("full_name") or ""
            if not full or full in out:
                continue
            c = parse_ts(it.get("created_at")) or ctx.now
            age = max((ctx.now - c).total_seconds() / 86400, 0.5)
            stars_7d = stars if age <= 7 else int(round(stars * 7 / age))
            desc = it.get("description") or ""
            rtopics = list(it.get("topics") or []) + [t, "new"]
            if SKILL_WORDS.search(desc + " " + " ".join(rtopics)) and (it.get("homepage") or it.get("has_pages")
                                                                     or stars >= 100):
                rtopics.append("skill")
            out[full] = _sig(kind="new_repo", source="github", repo=full, url=it.get("html_url") or f"https://github.com/{full}",
                             title=f"{full}: {desc}" if desc else full, summary=desc, published=it.get("created_at") or "",
                             stars=stars, stars_7d=stars_7d, license=_spdx(it.get("license")), topics=rtopics)
    ranked = sorted(out.values(), key=lambda s: (s.stars_7d, s.stars), reverse=True)
    return ranked[: max(ctx.limit, 1) * 2]


_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def parse_arxiv(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", _ATOM):
        def txt(tag: str) -> str:
            el = e.find(tag, _ATOM)
            return (el.text or "").strip() if el is not None and el.text else ""
        cats = [c.get("term") or "" for c in e.findall("a:category", _ATOM)]
        links = [l.get("href") or "" for l in e.findall("a:link", _ATOM)]
        out.append({"id": txt("a:id"), "title": txt("a:title"), "summary": txt("a:summary"),
                    "published": txt("a:published"), "comment": txt("arxiv:comment"), "categories": cats,
                    "links": links})
    return out


async def src_arxiv(ctx: Ctx) -> list[Signal]:
    # arXiv's published date is the submission time and items appear after an announce lag, so the
    # full window is always queried (one request) and the store's dedupe keeps only unseen papers.
    since = ctx.since["window"]
    ax = ctx.capmap.get("arxiv", {})
    cats = " OR ".join(f"cat:{c}" for c in ax.get("categories", ["cs.AI"]))
    terms = " OR ".join(f'abs:"{t}"' for t in ax.get("terms", ["GUI agent"]))
    q = f"({cats}) AND ({terms})"
    text = await ctx.fetcher.get("https://export.arxiv.org/api/query",
                                 {"search_query": q, "sortBy": "submittedDate", "sortOrder": "descending",
                                  "max_results": 200, "start": 0})
    out = []
    terms_l = [t.lower() for t in ax.get("terms", [])]
    for p in parse_arxiv(text):
        dt = parse_ts(p["published"])
        if dt is None or dt < since:
            continue
        blob = p["summary"] + " " + p["comment"] + " " + " ".join(p["links"])
        repo = github_repo(blob)
        if not repo:
            continue
        abs_url = p["id"].replace("http://", "https://")
        matched = [t.replace(" ", "-") for t in terms_l if t in (p["title"] + " " + p["summary"]).lower()]
        out.append(_sig(kind="paper", source="arxiv", url=abs_url, title=p["title"],
                        summary=f"code: https://github.com/{repo} | {p['comment']} | {p['summary']}",
                        published=p["published"], repo=repo, topics=p["categories"] + matched + ["paper-with-code"]))
    return out[: ctx.limit * 2]


async def src_anthropic(ctx: Ctx) -> list[Signal]:
    since = ctx.since_for("anthropic")
    out: list[Signal] = []
    # Claude Code's CHANGELOG.md has no dates: npm's publish times supply them.
    times: dict[str, str] = {}
    try:
        doc = await ctx.fetcher.json("https://registry.npmjs.org/@anthropic-ai/claude-code")
        times = {k: v for k, v in (doc.get("time") or {}).items() if k not in ("created", "modified")}
    except (Blocked, RateLimited, ValueError):
        pass
    for repo in ANTHROPIC_REPOS:
        try:
            out += await _repo_releases(ctx, repo, since, "changelog", "anthropic",
                                        ["anthropic", "claude-code" if "claude-code" in repo else "anthropic-sdk"],
                                        times if repo.endswith("claude-code") else None)
        except (Blocked, RateLimited, ValueError):
            continue
    return out


def _registry_package(srv: dict[str, Any]) -> tuple[str, str]:
    """The first npm / PyPI package an MCP registry entry names, as ("npm:<id>", "<version>"); ("", "") if none.
    Registry text is untrusted: the skill installer re-validates the name and requires an exact version."""
    for p in srv.get("packages") or []:
        if not isinstance(p, dict):
            continue
        rt = str(p.get("registryType") or p.get("registry_type") or "").lower()
        ident = str(p.get("identifier") or p.get("name") or "")[:214]
        if rt in ("npm", "pypi") and ident:
            return f"{rt}:{ident}", str(p.get("version") or "")[:64]
    return "", ""


async def src_mcp_registry(ctx: Ctx) -> list[Signal]:
    since = ctx.since_for("mcp-registry")
    out: list[Signal] = []
    cursor = None
    seen: set[str] = set()
    for _ in range(max(1, math.ceil(ctx.limit * 4 / 100))):
        params: dict[str, Any] = {"limit": 100, "updated_since": iso(since), "version": "latest"}
        if cursor:
            params["cursor"] = cursor
        j = await ctx.fetcher.json("https://registry.modelcontextprotocol.io/v0/servers", params, cache=False)
        for item in j.get("servers") or []:
            srv = item.get("server") or item
            meta = ((item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {})
            name = srv.get("name") or ""
            if not name or name in seen or meta.get("status", "active") != "active":
                continue
            published = meta.get("publishedAt") or meta.get("updatedAt") or ""
            dt = parse_ts(published)
            if dt is not None and dt < since:
                continue
            seen.add(name)
            repo_url = (srv.get("repository") or {}).get("url") or ""
            url = repo_url or srv.get("websiteUrl") or f"https://registry.modelcontextprotocol.io/v0/servers?search={quote(name)}"
            transports = [r.get("type", "") for r in srv.get("remotes") or []] + \
                         [p.get("registryType") or p.get("registry_type") or "" for p in srv.get("packages") or []]
            pkg, pkg_ver = _registry_package(srv)
            out.append(_sig(kind="mcp_server", source="mcp-registry", url=url, version=pkg_ver or srv.get("version") or "",
                            package=pkg,
                            title=f"MCP server {srv.get('title') or name} {srv.get('version') or ''}".strip(),
                            summary=f"{name}: {srv.get('description') or ''}", published=published,
                            repo=github_repo(repo_url), topics=["mcp", "mcp-server", "skill"] + transports))
        cursor = (j.get("metadata") or {}).get("nextCursor") or (j.get("metadata") or {}).get("next_cursor")
        if not cursor or len(out) >= ctx.limit * 4:
            break
    return out


async def src_packages(ctx: Ctx) -> list[Signal]:
    since = ctx.since_for("packages")
    out = []
    for d in ctx.deps:
        dt = parse_ts(d.published)
        if not d.latest or dt is None or dt < since:
            continue
        url = (f"https://pypi.org/project/{d.name}/{d.latest}/" if d.ecosystem == "pypi"
               else f"https://www.npmjs.com/package/{d.name}/v/{d.latest}")
        out.append(_sig(kind="package_release", source=d.ecosystem, url=url, version=d.latest, repo=d.repo,
                        package=f"{d.ecosystem}:{d.name}" if d.ecosystem in ("npm", "pypi") else "",
                        title=f"{d.name} {d.latest} ({d.ecosystem})", published=d.published,
                        summary=f"O declares {d.name}{(' ' + d.spec) if d.spec else ''}; latest {d.latest} "
                                f"published {d.published[:10]}. {d.summary}",
                        topics=["dependency", d.ecosystem, d.name.lower()]))
    return out


SOURCES: dict[str, Callable[[Ctx], Awaitable[list[Signal]]]] = {
    "github": src_github_releases,
    "github-search": src_github_new_repos,
    "arxiv": src_arxiv,
    "anthropic": src_anthropic,
    "mcp-registry": src_mcp_registry,
    "packages": src_packages,
}
_NEEDS_DEPS = {"github", "packages"}


# --------------------------------------------------------------------------- scan
@dataclass
class ScanReport:
    new: list[Signal]
    fetched: dict[str, int]
    per_source: dict[str, dict[str, Any]]
    stats: dict[str, int]
    seconds: float


async def ascan(store: EvolveStore, *, since_days: int = 14, limit: int = 50, incremental: bool = False,
                client: httpx.AsyncClient | None = None, capmap: dict[str, Any] | None = None,
                root: Path = REPO_ROOT, sources: list[str] | None = None, min_stars: int = 25,
                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                overlap: timedelta = timedelta(hours=1)) -> ScanReport:
    t0 = time.monotonic()
    capmap = capmap if capmap is not None else load_capmap()
    names = sources or list(SOURCES)
    now = datetime.now(timezone.utc)
    window = now - timedelta(days=since_days)
    cursors = store.cursors()
    since: dict[str, datetime] = {"default": window, "window": window}
    if incremental:
        for n in names:
            last = parse_ts((cursors.get(n) or {}).get("last_scan"))
            if last:
                since[n] = max(window, last - overlap)
    own = client is None
    client = client or httpx.AsyncClient(headers={"User-Agent": UA}, timeout=TIMEOUT)
    fetcher = Fetcher(client, store, github_token=os.environ.get("GITHUB_TOKEN") or None, sleep=sleep)
    ctx = Ctx(fetcher=fetcher, capmap=capmap, since=since, limit=limit, root=root, cursors=cursors,
              min_stars=min_stars, now=now)
    learned = cursors.get("_learned") or {}
    fresh_learned = (parse_ts(learned.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)) > now - timedelta(days=1)
    if fresh_learned:
        ctx.rest_blocked = bool(learned.get("rest_blocked"))
        ctx.no_changelog = set(learned.get("no_changelog") or [])
    per: dict[str, dict[str, Any]] = {}
    fetched: dict[str, int] = {}
    collected: list[Signal] = []
    try:
        if _NEEDS_DEPS & set(names):
            deps = read_declared_deps(root)
            ctx.deps = list(await asyncio.gather(*(resolve_dep(fetcher, d) for d in deps)))

        async def run(n: str) -> None:
            s0 = time.monotonic()
            try:
                sigs = await SOURCES[n](ctx)
                per[n] = {"ok": True, "error": "", "since": iso(ctx.since_for(n))}
                fetched[n] = len(sigs)
                collected.extend(sigs)
                store.set_cursor(n, {"last_scan": iso(now), "count": len(sigs)})
            except (Blocked, RateLimited, ET.ParseError, ValueError, KeyError, TypeError) as e:
                per[n] = {"ok": False, "error": f"{type(e).__name__}: {_clip(str(e), 200)}"}
                fetched[n] = 0
            per[n]["seconds"] = round(time.monotonic() - s0, 2)

        await asyncio.gather(*(run(n) for n in names))
    finally:
        if own:
            await client.aclose()
    store.set_cursor("_learned", {"ts": learned.get("ts") if fresh_learned else iso(now),
                                  "rest_blocked": ctx.rest_blocked, "no_changelog": sorted(ctx.no_changelog)})
    uniq = list({s.id: s for s in collected}.values())
    new = store.add_signals(uniq)
    new_ids = {s.id for s in new}
    for n in names:
        per[n]["new"] = sum(1 for s in uniq if s.id in new_ids and _source_of(s) == n)
        per[n]["fetched"] = fetched.get(n, 0)
    if ctx.deps:
        per.setdefault("deps", {})["resolved"] = sum(1 for d in ctx.deps if d.latest)
        per["deps"]["with_repo"] = sum(1 for d in ctx.deps if d.repo)
        per["deps"]["total"] = len(ctx.deps)
    return ScanReport(new=new, fetched=fetched, per_source=per, stats=dict(fetcher.stats),
                      seconds=round(time.monotonic() - t0, 2))


def _source_of(s: Signal) -> str:
    if s.source == "github":
        return "github-search" if s.kind == "new_repo" else "github"
    if s.source in ("pypi", "npm"):
        return "packages"
    return s.source


def scan(store: EvolveStore | None = None, **kw: Any) -> ScanReport:
    return asyncio.run(ascan(store or EvolveStore(), **kw))


def scan_all(since_days: int = 14, limit: int = 50, store: EvolveStore | None = None, **kw: Any) -> list[Signal]:
    """Full-window scan of every source; stores and returns the signals that were new."""
    return scan(store, since_days=since_days, limit=limit, incremental=False, **kw).new


def scan_incremental(store: EvolveStore | None = None, *, since_days: int = 14, limit: int = 50,
                     **kw: Any) -> list[Signal]:
    """Resumes each source from its stored cursor (first run: ``since_days``); returns only new signals."""
    return scan(store, since_days=since_days, limit=limit, incremental=True, **kw).new
