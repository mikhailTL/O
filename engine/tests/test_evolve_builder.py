"""Plan L builder: isolated checkout, scoped `claude -p` run, spend/timeout kill, commit. Fake CLI, temp git repos."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from o_engine.evolve import builder
from o_engine.evolve.schema import Proposal

FAKE = textwrap.dedent(r'''
    import json, os, sys, time
    from pathlib import Path
    prompt = sys.stdin.read()
    rec = {"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "prompt": prompt}
    Path(os.environ["FAKE_RECORD"]).write_text(json.dumps(rec))
    mode = os.environ.get("FAKE_MODE", "patch")
    def emit(ev):
        print(json.dumps(ev), flush=True)
    emit({"type": "system", "subtype": "init", "session_id": "s1"})
    if mode == "patch":
        Path("engine/o_engine").mkdir(parents=True, exist_ok=True)
        Path("engine/o_engine/clamp.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n")
        emit({"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 1000, "output_tokens": 100}}})
        emit({"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 1000, "output_tokens": 100}}})
        emit({"type": "result", "subtype": "success", "total_cost_usd": 0.0123, "num_turns": 2})
    elif mode == "noop":
        emit({"type": "result", "subtype": "success", "total_cost_usd": 0.001})
    elif mode == "spend":
        Path("partial.txt").write_text("half done\n")
        for i in range(1000):
            emit({"type": "assistant", "message": {"id": f"m{i}", "usage": {"input_tokens": 0, "output_tokens": 20000}}})
            time.sleep(0.05)
        emit({"type": "result", "total_cost_usd": 999})
    elif mode == "hang":
        time.sleep(60)
''')


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "engine" / "o_engine").mkdir(parents=True)
    (r / "engine" / "o_engine" / "__init__.py").write_text("")
    (r / "README.md").write_text("o\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def fake(tmp_path: Path) -> list[str]:
    f = tmp_path / "fake_claude.py"
    f.write_text(FAKE)
    return [sys.executable, str(f)]


def _proposal(pid: str = "p-clamp-1") -> Proposal:
    return Proposal(id=pid, signal_ids=["sig1"], subsystem="cu.grounding", title="Add clamp util",
                    rationale="Small helper for box math.", spec="Add engine/o_engine/clamp.py with clamp(x, lo, hi).",
                    acceptance_test="engine/tests/test_clamp.py::test_clamp bounds both sides", score=0.5)


def _env(tmp_path: Path, mode: str) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.update({"FAKE_MODE": mode, "FAKE_RECORD": str(tmp_path / "record.json"), "GITHUB_TOKEN": "ghp_x",
                "MY_API_KEY": "k", "SOME_SECRET": "s", "DB_PASSWORD": "p", "O_PERM_TOKEN": "t", "O_PERM_URL": "u",
                "CLAUDE_CODE_OAUTH_TOKEN": "oauth-keep", "HARMLESS": "1", "IS_SANDBOX": "1", "CCR_AUTO_MODE_ALLOW": "x",
                "CLAUDE_CODE_ENTRYPOINT": "parent", "CLAUDECODE": "1"})
    return env


def test_build_commits_patch_on_evolve_branch_in_worktree(repo, fake, tmp_path):
    wd = tmp_path / "work" / "p-clamp-1"
    res = builder.build(_proposal(), repo, wd, fake, 1.0, env=_env(tmp_path, "patch"), timeout_s=60)
    assert res.branch == "evolve/p-clamp-1"
    assert len(res.commit) == 40
    assert res.spent_usd == pytest.approx(0.0123)  # result's total_cost_usd wins over the estimate
    assert Path(res.transcript_path).is_file()
    assert '"type": "result"' in Path(res.transcript_path).read_text()
    assert _git(wd, "rev-parse", "--abbrev-ref", "HEAD").strip() == "evolve/p-clamp-1"
    assert (wd / ".git").is_file()  # a worktree, not a clone
    assert "evolve/p-clamp-1" in _git(repo, "branch", "--list")
    assert _git(wd, "show", "--name-only", "--format=", "HEAD").split() == ["engine/o_engine/clamp.py"]
    msg = _git(wd, "log", "-1", "--format=%B")
    assert msg.startswith("evolve(p-clamp-1): Add clamp util") and "Proposal: p-clamp-1" in msg
    assert _git(repo, "rev-parse", "HEAD") != _git(wd, "rev-parse", "HEAD")  # main untouched
    assert not (repo / "engine" / "o_engine" / "clamp.py").exists()


def test_cli_args_are_scoped_and_never_skip_permissions(repo, fake, tmp_path):
    builder.build(_proposal(), repo, tmp_path / "w", fake, 2.5, env=_env(tmp_path, "patch"), max_turns=7)
    rec = json.loads((tmp_path / "record.json").read_text())
    argv = rec["argv"]
    assert argv[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.50"
    wt = (tmp_path / "w").resolve()
    assert argv[argv.index("--allowedTools") + 1] == (  # no sandbox: no shell at all; files only inside the checkout
        f"Read(/{wt}/**),Edit(/{wt}/**),Write(/{wt}/**),Glob,Grep")
    assert argv[argv.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--setting-sources") + 1] == ""  # user/project allow rules cannot widen the list
    assert "--strict-mcp-config" in argv
    denied = argv[argv.index("--disallowedTools") + 1]
    assert "Read(~/.ssh/**)" in denied and "Read(~/Library/Application Support/O/**)" in denied
    assert not any("dangerously" in a for a in argv)
    assert Path(rec["cwd"]).resolve() == (tmp_path / "w").resolve()
    with pytest.raises(builder.BuildError):
        builder.build_command(["claude", "--dangerously-skip-permissions"], 5, 1.0)


def test_env_is_scrubbed_except_claude_auth(repo, fake, tmp_path):
    builder.build(_proposal(), repo, tmp_path / "w", fake, 1.0, env=_env(tmp_path, "patch"))
    env = json.loads((tmp_path / "record.json").read_text())["env"]
    for k in ("GITHUB_TOKEN", "MY_API_KEY", "SOME_SECRET", "DB_PASSWORD", "O_PERM_TOKEN", "O_PERM_URL", "IS_SANDBOX",
              "CCR_AUTO_MODE_ALLOW", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE"):
        assert k not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-keep"
    assert env["HARMLESS"] == "1"


def test_prompt_carries_spec_acceptance_and_protected_rules(repo, fake, tmp_path):
    builder.build(_proposal(), repo, tmp_path / "w", fake, 1.0, env=_env(tmp_path, "patch"))
    prompt = json.loads((tmp_path / "record.json").read_text())["prompt"]
    assert "Add engine/o_engine/clamp.py" in prompt
    assert "engine/tests/test_clamp.py::test_clamp" in prompt
    for path in ("main/agent.js", "engine/o_engine/actionlog.py", "engine/o_engine/evolve/gates.py", "extension/guard.js"):
        assert path in prompt
    assert "Never disable TLS" in prompt


def test_budget_kills_run_and_still_commits_partial_work(repo, fake, tmp_path):
    t0 = time.monotonic()
    # 20k output tokens per message at $75/Mtok = $1.50 each: the 2nd message crosses $2.
    res = builder.build(_proposal(), repo, tmp_path / "w", fake, 2.0, env=_env(tmp_path, "spend"), timeout_s=60)
    assert time.monotonic() - t0 < 20
    assert 2.0 <= res.spent_usd < 5.0
    msg = _git(tmp_path / "w", "log", "-1", "--format=%B")
    assert "stop=budget" in msg
    assert "partial.txt" in _git(tmp_path / "w", "show", "--name-only", "--format=", "HEAD")


def test_timeout_kills_hung_cli(repo, fake, tmp_path):
    t0 = time.monotonic()
    res = builder.build(_proposal(), repo, tmp_path / "w", fake, 1.0, env=_env(tmp_path, "hang"), timeout_s=1.5)
    assert time.monotonic() - t0 < 20
    assert res.commit == ""  # nothing changed


def test_noop_run_returns_empty_commit(repo, fake, tmp_path):
    res = builder.build(_proposal(), repo, tmp_path / "w", fake, 1.0, env=_env(tmp_path, "noop"))
    assert res.commit == "" and res.spent_usd == pytest.approx(0.001)


def test_falls_back_to_local_clone_when_worktree_fails(repo, fake, tmp_path):
    _git(repo, "branch", "evolve/p-clamp-1")  # worktree add -b now fails: branch exists
    wd = tmp_path / "w"
    res = builder.build(_proposal(), repo, wd, fake, 1.0, env=_env(tmp_path, "patch"))
    assert (wd / ".git").is_dir()  # a clone
    assert res.branch == "evolve/p-clamp-1" and len(res.commit) == 40
    assert "Builder: claude -p (clone)" in _git(wd, "log", "-1", "--format=%B")


def test_rejects_bad_ids_nonempty_workdir_and_zero_budget(repo, fake, tmp_path):
    with pytest.raises(builder.BuildError):
        builder.build(_proposal("../x"), repo, tmp_path / "w", fake, 1.0)
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "f").write_text("x")
    with pytest.raises(builder.BuildError):
        builder.build(_proposal(), repo, tmp_path / "busy", fake, 1.0)
    with pytest.raises(builder.BuildError):
        builder.build(_proposal(), repo, tmp_path / "w2", fake, 0)


def test_meter_counts_each_message_once():
    m = builder._Meter()
    ev = {"type": "assistant", "message": {"id": "a", "usage": {"output_tokens": 1_000_000}}}
    m.feed(ev)
    m.feed(ev)
    assert m.spent == pytest.approx(75.0)
    m.feed({"type": "result", "total_cost_usd": 0.5})
    assert m.spent == 0.5


def test_scrub_env_unit():
    out = builder.scrub_env({"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "x", "NPM_TOKEN": "y", "ANTHROPIC_API_KEY": "z",
                             "O_ENGINE_URL": "u", "HOME": "/h"})
    assert out == {"PATH": "/bin", "ANTHROPIC_API_KEY": "z", "HOME": "/h"}


def test_remove_checkout(repo, fake, tmp_path):
    wd = tmp_path / "w"
    builder.build(_proposal(), repo, wd, fake, 1.0, env=_env(tmp_path, "patch"))
    builder.remove_checkout(repo, wd)
    assert not wd.exists()
    assert str(wd) not in _git(repo, "worktree", "list")


def test_no_exec_build_proceeds_without_a_sandbox(repo, fake, tmp_path):
    from o_engine.evolve.sandbox import SandboxChoice

    none = lambda mode: SandboxChoice(False, "none", mode, reason="docker: not installed")  # noqa: E731
    wd = tmp_path / "work" / "docs"
    with pytest.raises(builder.BuildError, match="no sandbox"):
        builder.build(_proposal("p-code"), repo, wd, fake, 1.0, env=_env(tmp_path, "patch"), timeout_s=60,
                      sandbox_mode="auto", chooser=none)
    res = builder.build(_proposal("p-docs"), repo, tmp_path / "work" / "docs2", fake, 1.0, env=_env(tmp_path, "patch"),
                        timeout_s=60, sandbox_mode="auto", chooser=none, needs_sandbox=False)
    assert res.branch == "evolve/p-docs" and len(res.commit) == 40
    rec = json.loads((tmp_path / "record.json").read_text())
    assert "Bash" not in " ".join(rec["argv"]) and "You cannot run commands" in rec["prompt"]  # no shell without a sandbox


def test_build_without_git_fails_fast(fake, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "README.md").write_text("installed from a zip\n")
    with pytest.raises(builder.BuildError, match="installed without git"):
        builder.build(_proposal(), plain, tmp_path / "wd", fake, 1.0, env=_env(tmp_path, "patch"), timeout_s=60)
    assert not (tmp_path / "wd").exists()
