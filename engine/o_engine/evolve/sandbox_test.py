"""``o-sandbox-test``: the only command the builder's Claude Code session may run.

    o-sandbox-test pytest [pytest args...]   engine pytest (cwd engine/), e.g. ``pytest -q tests/test_x.py``
    o-sandbox-test node main/tools/selftest-x.js
    o-sandbox-test selftests                 every main/tools/selftest*.js

Each call re-selects the build's sandbox from the read-only config written by ``sandbox.install_wrapper`` and runs
the command there (offline, resource-limited, worktree-only). Started with ``python -I`` so nothing in the worktree
(the current directory) can shadow an import; the trusted engine path comes from the config.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

USAGE = ("usage: o-sandbox-test pytest [args...] | o-sandbox-test node main/tools/<file>.js | "
         "o-sandbox-test selftests")
_NODE_FILE = re.compile(r"^(?:main|tools)/[A-Za-z0-9_./-]+\.js$")


def plan(args: list[str], python: str) -> list[tuple[list[str], str]]:
    """``[(argv, cwd relative to the worktree), ...]`` for one wrapper call; ``ValueError`` when not allowed."""
    if not args:
        raise ValueError(USAGE)
    sub, rest = args[0], args[1:]
    if sub == "pytest":
        for a in rest:
            if "\x00" in a or len(a) > 400:
                raise ValueError("bad pytest argument")
        return [([python, "-m", "pytest", "-p", "no:cacheprovider", *rest], "engine")]
    if sub == "node":
        if len(rest) != 1 or not _NODE_FILE.match(rest[0]) or ".." in rest[0]:
            raise ValueError("node takes exactly one main/tools/*.js (or tools/*.js) file")
        return [(["node", rest[0]], ".")]
    if sub == "selftests":
        if rest:
            raise ValueError("selftests takes no arguments")
        return [(["sh", "-c", 'rc=0; for t in main/tools/selftest*.js; do echo "== $t"; node "$t" || rc=1; done; exit $rc'], ".")]
    raise ValueError(USAGE)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] != "--config":
        print("o-sandbox-test: missing --config (run the generated wrapper, not this file)", file=sys.stderr)
        return 2
    cfg = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    engine = str(cfg["engine"])
    if engine not in sys.path:
        sys.path.insert(0, engine)
    from o_engine.evolve import sandbox

    choice = sandbox.choose(cfg.get("mode", "auto"))
    if not choice.available:
        print(f"o-sandbox-test: no sandbox available ({choice.reason}); tests cannot run", file=sys.stderr)
        return 3
    runner = choice.runner(cfg["worktree"])
    try:
        steps = plan(argv[2:], runner.python)
    except ValueError as e:
        print(f"o-sandbox-test: {e}", file=sys.stderr)
        return 2
    rc = 0
    with runner:
        for cmd, cwd in steps:
            r = runner.run(cmd, cwd=cwd, timeout=float(cfg.get("timeout_s", 900)))
            sys.stdout.write(r.stdout[-60000:])
            sys.stderr.write(r.stderr[-20000:])
            if r.timed_out:
                print(f"o-sandbox-test: timed out", file=sys.stderr)
            rc = rc or r.returncode
    print(f"[o-sandbox-test: ran in {runner.name} sandbox, exit {rc}]")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
