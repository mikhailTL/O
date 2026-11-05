"""Deterministic end-to-end run on a real display: Xvfb :99 + window manager + Mousepad + the real AT-SPI
backend, driven by a scripted planner (no model) and a judge whose only paid backend is a fake ``claude``
CLI that counts its invocations. No real model is ever called.

Checks the real-run failures: exactly one Mousepad window after a ``launch`` of an app that is already
running, the text typed into the window that is in front, the file saved through the GUI, and the
engine-side model-call counter (one paid call: the final goal check; the cap is respected).

Opt-in (it owns display :99 and writes ~/lisbon-test.txt): ``O_REAL_ATSPI=1 python3 -m pytest -q
tests/test_cu_real_atspi.py``. PyGObject is looked up in ``O_PYGI`` (default /opt/pygi) when the engine's
python has none of its own.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
PYGI = os.environ.get("O_PYGI", "/opt/pygi")
DISPLAY = ":99"
TARGET = Path(os.path.expanduser("~/lisbon-test.txt"))
LINES = ("Passport", "Sunscreen", "Phone charger")
SPI_LAUNCHER = "/usr/libexec/at-spi-bus-launcher"
TOOLS = ("Xvfb", "mousepad", "dbus-daemon", "xdotool", "import")


def _pythonpath() -> str:
    parts = [str(ENGINE)] + ([PYGI] if os.path.isdir(PYGI) else []) + [os.environ.get("PYTHONPATH", "")]
    return os.pathsep.join(p for p in parts if p)


def _missing() -> str:
    if os.environ.get("O_REAL_ATSPI") != "1":
        return "opt-in: set O_REAL_ATSPI=1"
    if not sys.platform.startswith("linux"):
        return "linux only"
    gone = [t for t in TOOLS if shutil.which(t) is None] + ([] if os.path.exists(SPI_LAUNCHER) else [SPI_LAUNCHER])
    if gone:
        return "missing: " + ", ".join(gone)
    probe = subprocess.run([sys.executable, "-c", "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi"],
                           env={**os.environ, "PYTHONPATH": _pythonpath()}, capture_output=True)
    if probe.returncode != 0:
        return "gi/Atspi not importable"
    if os.path.exists(f"/tmp/.X{DISPLAY[1:]}-lock"):
        return f"display {DISPLAY} is already in use"
    return ""


@pytest.mark.skipif(bool(_missing()), reason=_missing() or "ok")
def test_mousepad_three_lines_saved_one_window_counter_respected(tmp_path):
    TARGET.unlink(missing_ok=True)
    out = tmp_path / "result.json"
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS")}
    env["PYTHONPATH"] = _pythonpath()
    proc = subprocess.run([sys.executable, __file__, str(out), str(tmp_path)], env=env, capture_output=True, text=True,
                          timeout=240)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    r = json.loads(out.read_text())
    print(json.dumps(r, indent=2))
    assert r["window_manager"], r
    assert r["launch_note"].startswith("already running"), r["launch_note"]
    assert r["x_windows"] == 1 and r["atspi_apps"] == 1, r
    assert r["state"]["ok"] is True, r["state"]["error"]
    assert TARGET.read_text().strip().splitlines() == list(LINES)
    calls = r["state"]["model_calls"]
    assert calls["paid"] == r["fake_cli_calls"] == 1  # the final goal check only
    assert calls["paid"] <= calls["max_paid"] == 2
    assert calls["deferred_checks"] == 1 and calls["by_kind"] == {"claude:judge": 1, "local:judge": 3}
    assert r["state"]["cost_usd"] == pytest.approx(0.01)
    TARGET.unlink(missing_ok=True)


# ------------------------------------------------------------------ child process (real display)

FAKE_CLAUDE = """#!{python}
import json, sys
sys.stdin.read()
with open({count!r}, "a") as f:
    f.write("1\\n")
print(json.dumps({{"result": json.dumps({{"ok": True, "reason": "fake judge: goal met"}}), "total_cost_usd": 0.01}}))
"""


class ScriptedPlanner:
    """Two milestones; each turn's steps are fixed. No model is called."""

    def __init__(self, parse):
        self.parse = parse
        self.turns = 0

    def parse_script(self, raw):
        return self.parse(raw)

    def plan_milestones(self, goal, app, notes="", memory=None):
        from o_engine.cu.schema import Milestone

        return (
            Milestone(goal="write the list",
                      done_when="Does the Mousepad text area show exactly three lines: "
                                + ", ".join(f"'{s}'" for s in LINES) + "?"),
            Milestone(goal="save it", done_when={"role": "frame", "name": f"*{TARGET.name} - Mousepad"}),
        )

    def next_steps(self, milestone, tree_summary, memory=None, notes="", interfaces=(), max_steps=5, app=""):
        self.turns += 1
        if milestone.goal == "write the list":
            steps = [
                {"op": "launch", "cmd": "mousepad"},
                {"op": "find", "as": "editor", "target": {"role": "text"}},
                {"op": "type", "ref": "editor", "text": "\n".join(LINES) + "\n"},
                {"op": "screenshot_assert", "question": "Does the editor show a packing list?"},
            ]
        else:
            steps = [
                {"op": "key", "keys": "ctrl+s"},
                {"op": "wait", "for": {"role": "file chooser"}, "timeout_s": 15},
                {"op": "find", "as": "name", "target": {"role": "text"}},
                {"op": "set_value", "ref": "name", "value": str(TARGET)},
                {"op": "key", "keys": "Return"},
                {"op": "wait", "for": {"role": "frame", "name": f"*{TARGET.name} - Mousepad"}, "timeout_s": 15},
            ]
        return self.parse({"app": app or "mousepad", "goal": milestone.goal, "steps": steps})

    def replan_step(self, *a, **k):
        raise RuntimeError("scripted planner does not heal")

    def replace_step(self, *a, **k):
        raise RuntimeError("scripted planner does not heal")


def _child(out_path: str, work: str) -> None:
    from o_engine.actionlog import ActionLog
    from o_engine.display import IsolatedDisplay

    work_dir = Path(work)
    display = IsolatedDisplay(DISPLAY)
    display.start()
    procs: list[subprocess.Popen] = []
    bus_pid = None
    try:
        bus = subprocess.run(["dbus-daemon", "--session", "--fork", "--print-address=1", "--print-pid=1"],
                             env={**os.environ, "DISPLAY": DISPLAY}, capture_output=True, text=True, check=True)
        addr, bus_pid = bus.stdout.strip().splitlines()[:2]
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = addr
        bus_env = {**os.environ, "DISPLAY": DISPLAY, "ACCESSIBILITY_ENABLED": "1"}
        bus_env.pop("NO_AT_BRIDGE", None)
        procs.append(subprocess.Popen([SPI_LAUNCHER, "--launch-immediately"], env=bus_env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
        time.sleep(1.5)
        profile = work_dir / "mousepad-profile"
        app_env = {**bus_env, "GSETTINGS_BACKEND": "memory", "XDG_CONFIG_HOME": str(profile / "config"),
                   "XDG_DATA_HOME": str(profile / "data"), "XDG_CACHE_HOME": str(profile / "cache")}
        # the user's own editor, already open on O's display before the job starts
        procs.append(subprocess.Popen(["mousepad", "--disable-server"], env=app_env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True))
        time.sleep(3)
        # what a launch step would inherit (the engine's own env): the same editor profile
        os.environ.update({k: app_env[k] for k in ("GSETTINGS_BACKEND", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")})

        from o_engine.cu.backends.atspi import AtspiBackend
        from o_engine.cu.graph import CUDeps, build_cu_graph, run_cu_job
        from o_engine.cu.judge import ClaudeCodeJudge, Judge, VLMJudge
        from o_engine.cu.schema import parse_script

        count = work_dir / "fake-claude-calls"
        fake = work_dir / "claude"
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable, count=str(count)))
        fake.chmod(0o755)
        backend = AtspiBackend(display=DISPLAY, allow_pixels=True)
        log = ActionLog(str(work_dir / "data"))
        judge = Judge(VLMJudge("http://127.0.0.1:9/v1", "none", timeout=2), ClaudeCodeJudge(str(fake)))
        deps = CUDeps(backend_factory=lambda slot: backend, grounder=None, judge=judge,
                      planner=ScriptedPlanner(parse_script), procedures=None, notes=None, log=log, max_heals=0)
        state = run_cu_job(build_cu_graph(deps), f"Write a 3-line packing list and save it as {TARGET}",
                           "mousepad", "real-atspi", max_heals=0, max_model_calls=2)
        launch_note = next((s["stdout_tail"] for s in state["steps"] if s["command"].startswith("launch")), "")
        x_windows = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "^mousepad$"],
                                   env={**os.environ, "DISPLAY": DISPLAY}, capture_output=True, text=True).stdout.split()
        atspi_apps = [a for a in backend.tree_all() if a.name.lower() == "mousepad"]
        result = {
            "window_manager": display.wm_name or display.wm_reason,
            "launch_note": launch_note,
            "x_windows": len(x_windows),
            "atspi_apps": len(atspi_apps),
            "fake_cli_calls": len(count.read_text().split()) if count.exists() else 0,
            "state": {k: state[k] for k in ("ok", "error", "turns", "model_calls", "cost_usd")},
            "steps": [f"{s['exit_code']} {s['command'][:80]} :: {s['stdout_tail'][:100] or s['stderr_tail'][:100]}"
                      for s in state["steps"]],
        }
        Path(out_path).write_text(json.dumps(result))
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, 15)
            except OSError:
                pass
        subprocess.run(["pkill", "-f", "mousepad --disable-server"], capture_output=True)
        if bus_pid:
            try:
                os.kill(int(bus_pid), 15)
            except OSError:
                pass
        display.stop()


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp())
