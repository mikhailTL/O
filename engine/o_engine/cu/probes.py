"""The one read-only probe allow-list shared by ``StateJudge`` and ``FeasibilityGate``.

A probe command runs only through ``Sandbox.run`` and only when ``is_allowed_probe`` accepts
it: the argv parsed by ``shlex`` must start with one of ``PROBE_ALLOW_LIST`` and the raw text
must contain no shell operator, redirection, substitution or line break, because
``Sandbox.run`` hands the line to a shell.
"""
from __future__ import annotations

import re
import shlex

PROBE_ALLOW_LIST: tuple[str, ...] = (
    "test -f",
    "test -d",
    "sha256sum",
    "stat",
    "grep -c",
    "ls",
    "head -c",
    "command -v",
)
PROBE_REFUSED = "probe not allowed"
_PROBE_FORBIDDEN = re.compile(r"[;&|<>`$\n\r\x00]")


def is_allowed_probe(command: str) -> bool:
    """True only for a single read-only command line that starts with a ``PROBE_ALLOW_LIST`` prefix.

    The check is on the argv parsed by ``shlex`` (so ``/usr/bin/ls`` and ``"ls"`` tricks fail) and
    the raw text must contain no shell operator, redirection, substitution or line break.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    if _PROBE_FORBIDDEN.search(command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    for prefix in PROBE_ALLOW_LIST:
        want = prefix.split()
        if argv[: len(want)] == want:
            return True
    return False
