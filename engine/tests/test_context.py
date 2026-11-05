import hashlib
import json
import os
import sqlite3
import time

import pytest

from o_engine.context import (
    ContextStore,
    LocalTelemetry,
    ScreenpipeTelemetry,
    WorkspaceProfile,
    read_shell_history,
    walk_tree,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(home), 1) if p.startswith("~") else p)
    return home


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name": "demo"}\n')
    (ws / "Makefile").write_text("all:\n\techo hi\n")
    (ws / "src").mkdir()
    (ws / "src" / "a.py").write_text("print(1)\n")
    (ws / "src" / "deep").mkdir()
    (ws / "src" / "deep" / "deeper").mkdir()
    (ws / "src" / "deep" / "deeper" / "toodeep").mkdir()
    (ws / "src" / "deep" / "deeper" / "toodeep" / "x.txt").write_text("x")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "lib.js").write_text("x")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref")
    (ws / "venv").mkdir()
    (ws / ".venv").mkdir()
    return ws


def _make_screenpipe_db(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE frames (id INTEGER PRIMARY KEY, timestamp TEXT, app_name TEXT, window_name TEXT);
        CREATE TABLE ocr_text (frame_id INTEGER, text TEXT);
        """
    )
    for i, (ts, app, win, text) in enumerate(rows, start=1):
        conn.execute("INSERT INTO frames VALUES (?,?,?,?)", (i, ts, app, win))
        conn.execute("INSERT INTO ocr_text VALUES (?,?)", (i, text))
    conn.commit()
    conn.close()


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def test_shell_history_tails_bash_zsh_powershell(fake_home):
    (fake_home / ".bash_history").write_text("\n".join(f"b{i}" for i in range(300)) + "\n")
    (fake_home / ".zsh_history").write_text(": 1700000000:0;z1\n: 1700000001:0;z2\n")
    ps = fake_home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "PowerShell" / "PSReadLine"
    ps.mkdir(parents=True)
    (ps / "ConsoleHost_history.txt").write_text("p1\np2\n")
    hist = read_shell_history(limit=200)
    shells = {s for s, _ in hist}
    assert shells == {"bash", "zsh", "powershell"}
    bash = [c for s, c in hist if s == "bash"]
    assert len(bash) == 200
    assert bash[-1] == "b299"
    assert [c for s, c in hist if s == "zsh"] == ["z1", "z2"]
    assert [c for s, c in hist if s == "powershell"] == ["p1", "p2"]


def test_shell_history_missing_files_is_empty(fake_home):
    assert read_shell_history() == []


def test_walk_tree_depth_and_ignores(workspace):
    tree = walk_tree(str(workspace), depth=3)
    assert tree["name"] == "ws"
    names = {c["name"] for c in tree["children"]}
    assert "node_modules" not in names
    assert ".git" not in names
    assert "venv" not in names
    assert ".venv" not in names
    assert {"package.json", "Makefile", "src"} <= names
    src = next(c for c in tree["children"] if c["name"] == "src")
    deep = next(c for c in src["children"] if c["name"] == "deep")
    deeper = next(c for c in deep["children"] if c["name"] == "deeper")
    assert deeper.get("children", []) == []
    assert deeper.get("truncated") is True


def test_screenpipe_recent_filters_by_time(tmp_path):
    db = tmp_path / "screenpipe.db"
    now = time.time()
    _make_screenpipe_db(
        db,
        [
            (_iso(now - 10), "Code", "main.py - O", "def main"),
            (_iso(now - 1000), "Terminal", "bash", "old text"),
        ],
    )
    rows = ScreenpipeTelemetry(str(db)).recent(120)
    assert len(rows) == 1
    assert rows[0]["app"] == "Code"
    assert rows[0]["window"] == "main.py - O"
    assert rows[0]["text"] == "def main"
    assert abs(rows[0]["ts"] - (now - 10)) < 2


def test_screenpipe_defensive_on_bad_schema(tmp_path):
    db = tmp_path / "weird.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    assert ScreenpipeTelemetry(str(db)).recent(120) == []
    assert ScreenpipeTelemetry(str(tmp_path / "missing.db")).recent(120) == []
    assert ScreenpipeTelemetry(None).recent(120) == []


def test_local_telemetry_record_and_recent(tmp_data_dir):
    t = LocalTelemetry(str(tmp_data_dir))
    t.record("hello")
    t.record("world", app="Term", window="w")
    rows = t.recent(60)
    assert [r["text"] for r in rows] == ["hello", "world"]
    assert rows[1]["app"] == "Term"
    assert rows[1]["window"] == "w"
    assert all(abs(r["ts"] - time.time()) < 5 for r in rows)
    path = tmp_data_dir / "telemetry.jsonl"
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    old = dict(json.loads(lines[0]))
    old["ts"] = time.time() - 10_000
    with path.open("a") as f:
        f.write(json.dumps(old) + "\n")
    assert len(t.recent(60)) == 2


def test_collect_and_profile(fake_home, workspace, tmp_data_dir, monkeypatch):
    (fake_home / ".bash_history").write_text("npm install\nnpm test\n")
    store = ContextStore(str(tmp_data_dir), screenpipe_db=None)
    store.collect(str(workspace))
    conn = sqlite3.connect(tmp_data_dir / "context.db")
    assert conn.execute("SELECT COUNT(*) FROM shell_history").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM tree_snapshots").fetchone()[0] == 1
    cfg = conn.execute("SELECT path, sha256, head FROM config_files ORDER BY path").fetchall()
    conn.close()
    paths = [os.path.basename(p) for p, _, _ in cfg]
    assert paths == ["Makefile", "package.json"]
    pkg = next(r for r in cfg if r[0].endswith("package.json"))
    assert pkg[1] == hashlib.sha256(b'{"name": "demo"}\n').hexdigest()
    assert pkg[2].startswith('{"name": "demo"}')

    store.telemetry.record("fallback line")
    prof = store.profile(str(workspace))
    assert isinstance(prof, WorkspaceProfile)
    assert prof.cwd == str(workspace)
    assert prof.shell_history == ["npm install", "npm test"]
    assert prof.tree["name"] == "ws"
    assert {c["path"].split(os.sep)[-1] for c in prof.config_files} == {"Makefile", "package.json"}
    assert prof.telemetry[-1]["text"] == "fallback line"
    assert set(prof.telemetry[-1]) == {"ts", "app", "window", "text"}
    assert prof.platform
    assert prof.collected_at > 0
    dumped = json.loads(prof.model_dump_json())
    assert set(dumped) == {"cwd", "collected_at", "shell_history", "tree", "config_files", "telemetry", "platform"}


def test_profile_prefers_screenpipe_when_present(fake_home, workspace, tmp_data_dir, tmp_path):
    db = tmp_path / "sp.db"
    now = time.time()
    _make_screenpipe_db(db, [(_iso(now - 5), "Browser", "docs", "ocr here")])
    store = ContextStore(str(tmp_data_dir), screenpipe_db=str(db))
    prof = store.profile(str(workspace))
    assert [t["text"] for t in prof.telemetry] == ["ocr here"]


def test_profile_without_collect_still_builds(fake_home, workspace, tmp_data_dir):
    prof = ContextStore(str(tmp_data_dir), screenpipe_db=None).profile(str(workspace))
    assert prof.tree["name"] == "ws"
    assert any(c["path"].endswith("package.json") for c in prof.config_files)
