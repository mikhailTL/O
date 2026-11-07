"""Skill self-test, run INSIDE the offline evolve sandbox by ``installer.McpInstaller.selftest`` (never on the host).

``python3 -I skill_selftest.py <npm|pypi> <package> <version>`` with the staged package in the working directory
(``node_modules/`` from ``npm install --ignore-scripts``, or ``wheels/`` from ``pip download``). Starts the MCP
server over stdio and requires answers to ``initialize`` and ``tools/list``. Prints one JSON line:
``{"ok": true, "tools": N}`` or ``{"ok": false, "error": "..."}``. Standard library only.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

SITE = "/tmp/o-skill-site"
WAIT_S = 45.0


def _say(ok: bool, **kw: object) -> int:
    print(json.dumps({"ok": ok, **kw}))
    return 0 if ok else 1


def npm_argv(package: str) -> list[str]:
    pj = Path("node_modules") / package / "package.json"
    meta = json.loads(pj.read_text(encoding="utf-8"))
    b = meta.get("bin")
    if isinstance(b, dict):
        short = package.split("/")[-1]
        rel = b.get(short) or next(iter(b.values()), "")
    else:
        rel = b or meta.get("main") or "index.js"
    return ["node", str(pj.parent / str(rel))]


def pypi_argv(package: str, version: str) -> tuple[list[str], dict[str, str]]:
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--find-links", "wheels", "--target", SITE,
                        "--quiet", f"{package}=={version}"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("offline install failed: " + (r.stderr or r.stdout)[-200:])
    norm = package.lower().replace("-", "_")
    for ep in Path(SITE).glob("*.dist-info/entry_points.txt"):
        if not ep.parent.name.lower().replace("-", "_").startswith(norm):
            continue
        section = ""
        for line in ep.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[console_scripts]" and "=" in line:
                target = line.split("=", 1)[1].strip()
                mod, _, fn = target.partition(":")
                code = f"import sys, {mod.strip()} as m; sys.exit(m.{fn.strip() or 'main'}())"
                return [sys.executable, "-c", code], {"PYTHONPATH": SITE}
    raise RuntimeError("the package has no console script to start")


def handshake(argv: list[str], extra_env: dict[str, str]) -> int:
    env = {**os.environ, **extra_env}
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            env=env, bufsize=1)
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout], daemon=True).start()  # type: ignore[union-attr]

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def answer(mid: int) -> dict:
        while True:
            line = lines.get(timeout=WAIT_S)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == mid:
                return msg

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "o-skill-selftest", "version": "1"}}})
        init = answer(1)
        if "result" not in init:
            return _say(False, error=f"initialize failed: {str(init.get('error'))[:200]}")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tl = answer(2)
        tools = (tl.get("result") or {}).get("tools")
        if not isinstance(tools, list):
            return _say(False, error="tools/list did not return a tool list")
        return _say(True, tools=len(tools))
    except queue.Empty:
        return _say(False, error=f"no MCP answer within {WAIT_S:.0f}s")
    finally:
        proc.kill()


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("npm", "pypi"):
        return _say(False, error="usage: skill_selftest.py <npm|pypi> <package> <version>")
    eco, package, version = argv
    try:
        if eco == "npm":
            cmd, env = npm_argv(package), {}
        else:
            cmd, env = pypi_argv(package, version)
    except Exception as exc:  # noqa: BLE001
        return _say(False, error=f"{type(exc).__name__}: {exc}"[:300])
    return handshake(cmd, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
