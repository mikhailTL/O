"""Per-app interfaces: markdown notes and executable, versioned Python APIs.

Notes (``InterfaceNotes``): ``interfaces/<app>.md`` grown from run summaries. Each
run appends one ``## <iso date> <goal>`` section of at most 40 lines (a
model-written digest when a client is given, else the trimmed raw summary). The
file is capped at 400 lines by dropping whole sections from the top (oldest);
existing sections are never edited. ``for_prompt`` returns the newest notes that
fit a character budget.

Executable interfaces (``InterfaceStore`` / ``InterfaceBuilder``):
``interfaces/<app>/v<N>/api.py`` + ``api.json``. The builder makes one bounded
``complete_json`` call that turns a passing procedure into functions over a
``backend`` object (``find`` / ``invoke`` / ``set_value`` / ``tree`` only) plus a
smoke script. Generated source passes two static gates: ``check_ast`` (no imports,
no ``__import__``/``exec``/``eval``/``open``/``getattr``... calls, no attribute of
``backend`` outside the four methods, no dunder attributes) and ``check_source``
(a closed list of forbidden fragments). Then the smoke script runs through a
``Runner`` on a display the caller names explicitly (never the host display). A
pass appends v<N+1>; a failure records nothing. Versions are written with
``open(..., 'x')`` and never rewritten: fitness (replay pass-rate over the newest
procedure of each goal) is appended as one JSON line to the newest version's
``fitness.jsonl``, and ``read()`` reports the last line as ``"fitness"``.
"""
from __future__ import annotations

import ast
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MAX_SECTION_LINES = 40
MAX_FILE_LINES = 400
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")

SUMMARY_SYSTEM_PROMPT = (
    "You maintain terse engineering notes about how a desktop application's user interface behaves,\n"
    "for use by an automation planner. From the run summary, write at most 30 lines of markdown\n"
    "bullets under three headings: 'What worked', 'Tree landmarks' (accessibility roles and names\n"
    "worth targeting), 'Pitfalls'. No preamble, no code fences, no headings other than those three,\n"
    "nothing speculative."
)


def _slug(app: str) -> str:
    s = _SLUG.sub("-", app.strip()).strip("-.")
    return s or "app"


def _is_heading(line: str) -> bool:
    return line.startswith("## ")


def split_sections(text: str) -> list[list[str]]:
    """Split note text into sections (each starts with a ``## `` line). Leading text is its own section."""
    sections: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if _is_heading(line) and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)
    return sections


class InterfaceNotes:
    def __init__(self, dir: str | os.PathLike[str]) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, app: str) -> Path:
        return self.dir / f"{_slug(app)}.md"

    def read(self, app: str) -> str:
        p = self.path(app)
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8")

    def _write(self, app: str, text: str) -> None:
        p = self.path(app)
        fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _section(goal: str, body: str, when: datetime | None = None) -> list[str]:
        when = when or datetime.now(timezone.utc)
        heading = f"## {when.isoformat(timespec='seconds')} {goal}".rstrip()
        lines = [heading]
        for raw in body.splitlines():
            line = raw.rstrip()
            if _is_heading(line):
                line = "#" + line  # demote: a run summary never opens a new section
            if not line and (not lines[-1]):
                continue
            lines.append(line)
            if len(lines) >= MAX_SECTION_LINES:
                break
        while len(lines) > 1 and not lines[-1]:
            lines.pop()
        return lines

    def append(self, app: str, run_summary_text: str, model_client: Any = None, goal: str = "") -> str:
        """Append one section and enforce the file cap. Returns the section text that was written."""
        body = run_summary_text or ""
        if model_client is not None:
            try:
                digest = model_client.complete(SUMMARY_SYSTEM_PROMPT, f"App: {app}\nGoal: {goal}\n\nRun summary:\n{body}")
            except Exception:
                digest = ""
            if digest and digest.strip():
                body = digest
        section = self._section(goal or "run", body)
        existing = self.read(app)
        sections = split_sections(existing) if existing.strip() else []
        sections.append(section)
        total = sum(len(s) for s in sections)
        while total > MAX_FILE_LINES and len(sections) > 1:
            dropped = sections.pop(0)
            total -= len(dropped)
        text = "\n".join("\n".join(s) for s in sections) + "\n"
        self._write(app, text)
        return "\n".join(section)

    def sections(self, app: str) -> list[str]:
        text = self.read(app)
        return ["\n".join(s) for s in split_sections(text)] if text.strip() else []

    def for_prompt(self, app: str, max_chars: int = 6000) -> str:
        """Newest sections that fit ``max_chars``; the newest section alone is truncated if it is too long."""
        parts = self.sections(app)
        if not parts:
            return ""
        chosen: list[str] = []
        used = 0
        for s in reversed(parts):
            cost = len(s) + (1 if chosen else 0)
            if used + cost > max_chars:
                break
            chosen.insert(0, s)
            used += cost
        if not chosen:
            return parts[-1][-max_chars:]
        return "\n".join(chosen)


# ----------------------------------------------------------------- executable interfaces

_VERSION_DIR = re.compile(r"^v(\d+)$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROMPT_CHARS = 6000  # cap on each prompt ingredient (script JSON, backend doc)
_BACKEND_METHODS = ("find", "invoke", "set_value", "tree")

# Closed list. Generated api.py bodies and smoke scripts may contain none of these.
FORBIDDEN_FRAGMENTS: tuple[str, ...] = (
    "import os",
    "from os ",
    "from os.",
    "import subprocess",
    "from subprocess",
    "import pyautogui",
    "from pyautogui",
    "pyautogui",
    "pixel",
    "xdotool",
)

BUILDER_SYSTEM_PROMPT = (
    "You turn one solved desktop-automation script into a small reusable Python API.\n"
    "Reply with exactly one JSON object: {\"functions\": [{\"name\", \"description\", \"signature\", \"body\"}], \"smoke\": \"<python>\"}.\n"
    "Rules: every signature starts with '(backend' and lists the other parameters as plain Python;\n"
    "'body' is the function body only (no 'def' line), indented from column 0;\n"
    "the body may use only the 'backend' object and its methods find(target_dict) -> node|None,\n"
    "invoke(node, action), set_value(node, value), tree() -> nodes; targets are dicts with\n"
    "role/name/text/index; never import os, subprocess or pyautogui, never touch pixels or xdotool;\n"
    "'smoke' calls each function once against the running app using 'backend' and exits 0 on success.\n"
    "No prose, no code fences."
)


class InterfaceViolation(Exception):
    """A generated interface broke a builder rule (bad display, bad shape, forbidden source)."""


def check_source(text: str) -> list[str]:
    """The forbidden fragments present in ``text`` (case-insensitive, substring match)."""
    low = (text or "").lower()
    return [frag for frag in FORBIDDEN_FRAGMENTS if frag in low]


_FORBIDDEN_CALLS = frozenset({
    "__import__", "exec", "eval", "compile", "open", "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "breakpoint", "input",
})


def check_ast(text: str) -> list[str]:
    """Violations of the ``backend`` allow-list in ``text``, found by walking its AST (empty = clean).

    Rejects every ``import``/``from ... import``; every call whose callee is one of the names in
    ``_FORBIDDEN_CALLS``; every attribute of the name ``backend`` outside find/invoke/set_value/tree;
    and every dunder attribute on anything. Unparseable text is one violation."""
    try:
        tree = ast.parse(text or "")
    except SyntaxError as e:
        return [f"syntax error: {e.msg} (line {e.lineno})"]
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(f"line {node.lineno}: import")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            out.append(f"line {node.lineno}: call to {node.func.id}")
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("__") and attr.endswith("__"):
                out.append(f"line {node.lineno}: dunder attribute {attr}")
            elif isinstance(node.value, ast.Name) and node.value.id == "backend" and attr not in _BACKEND_METHODS:
                out.append(f"line {node.lineno}: backend.{attr} is not in {'/'.join(_BACKEND_METHODS)}")
    return out


def _render_function(fn: dict) -> str:
    doc = (fn.get("description") or "").replace('"""', "'").strip()
    lines = [f"def {fn['name']}{fn['signature']}:"]
    if doc:
        lines.append(f'    """{doc}"""')
    body = [("    " + line) if line.strip() else "" for line in (fn.get("body") or "").splitlines()]
    if not any(line.strip() for line in body):
        body = ["    pass"]
    lines.extend(body)
    return "\n".join(lines).rstrip() + "\n"


def render_api(functions: list[dict]) -> str:
    """api.py text for ``functions`` (each: name, description, signature, body)."""
    parts = ["# Generated by o_engine.cu.interfaces.InterfaceBuilder. Uses only `backend` (find/invoke/set_value/tree).\n"]
    parts.extend(_render_function(fn) for fn in functions)
    return "\n\n".join(parts)


class InterfaceStore:
    """Append-only ``interfaces/<app>/v<N>/{api.py,api.json,fitness.jsonl}``. No file is ever rewritten."""

    def __init__(self, dir: str | os.PathLike[str]) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def app_dir(self, app: str) -> Path:
        return self.dir / _slug(app)

    def version_dir(self, app: str, version: int) -> Path:
        return self.app_dir(app) / f"v{version}"

    def versions(self, app: str) -> list[int]:
        d = self.app_dir(app)
        if not d.is_dir():
            return []
        out = []
        for p in d.iterdir():
            m = _VERSION_DIR.match(p.name)
            if m and p.is_dir() and (p / "api.json").is_file():
                out.append(int(m.group(1)))
        return sorted(out)

    def latest(self, app: str) -> int | None:
        v = self.versions(app)
        return v[-1] if v else None

    def read(self, app: str, version: int | None = None) -> dict | None:
        """The ``api.json`` of ``version`` (newest when omitted) with ``"fitness"`` set to the last line of
        that version's ``fitness.jsonl`` (None when there is none), computed at read time; or None."""
        if version is None:
            version = self.latest(app)
        if version is None:
            return None
        p = self.version_dir(app, version) / "api.json"
        if not p.is_file():
            return None
        meta = json.loads(p.read_text(encoding="utf-8"))
        meta["fitness"] = self.last_fitness(app, version)
        return meta

    def last_fitness(self, app: str, version: int) -> dict | None:
        """The last record of ``fitness.jsonl`` for ``version``, or None."""
        p = self.version_dir(app, version) / "fitness.jsonl"
        if not p.is_file():
            return None
        last = None
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = line
        return json.loads(last) if last else None

    def api_source(self, app: str, version: int | None = None) -> str | None:
        if version is None:
            version = self.latest(app)
        if version is None:
            return None
        p = self.version_dir(app, version) / "api.py"
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def functions(self, app: str) -> list[dict]:
        """Names, descriptions and signatures of the newest version (no bodies: progressive disclosure)."""
        meta = self.read(app)
        if not meta:
            return []
        return [{"name": f["name"], "description": f.get("description", ""), "signature": f.get("signature", "")}
                for f in meta.get("functions", [])]

    def source(self, app: str, fn: str) -> str | None:
        """The full ``def`` source of ``fn`` in the newest api.py, on request."""
        text = self.api_source(app)
        if text is None:
            return None
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == fn:
                return ast.get_source_segment(text, node)
        return None

    def write(self, app: str, functions: list[dict]) -> int:
        """Append the next version. Never touches an existing one: ``open(..., 'x')`` on both files."""
        version = (self.latest(app) or 0) + 1
        vdir = self.version_dir(app, version)
        vdir.mkdir(parents=True, exist_ok=True)
        meta = {
            "version": version,
            "parent": version - 1 if version > 1 else None,
            "functions": [
                {"name": f["name"], "description": f.get("description", ""), "signature": f.get("signature", ""),
                 "passed_on": list(f.get("passed_on", []))}
                for f in functions
            ],
            "fitness": None,
        }
        with open(vdir / "api.py", "x", encoding="utf-8", newline="\n") as f:
            f.write(render_api(functions))
        with open(vdir / "api.json", "x", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        return version

    def write_fitness(self, app: str, replayed: int, passed: int) -> int | None:
        """Append one ``{"ts","version","replayed","passed"}`` line to the newest version's ``fitness.jsonl``.
        api.py and api.json are never touched. Returns the version, or None when the app has none."""
        version = self.latest(app)
        if version is None:
            return None
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "version": version,
            "replayed": int(replayed),
            "passed": int(passed),
        }
        with open(self.version_dir(app, version) / "fitness.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
        return version


def _validate_reply(text: str) -> tuple[list[dict], str]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as e:
        raise InterfaceViolation(f"builder reply is not JSON: {e}") from None
    if not isinstance(payload, dict):
        raise InterfaceViolation("builder reply is not an object")
    functions = payload.get("functions")
    smoke = payload.get("smoke")
    if not isinstance(functions, list) or not functions:
        raise InterfaceViolation("builder reply has no functions")
    if not isinstance(smoke, str) or not smoke.strip():
        raise InterfaceViolation("builder reply has no smoke script")
    out: list[dict] = []
    seen: set[str] = set()
    for fn in functions:
        if not isinstance(fn, dict):
            raise InterfaceViolation("function entry is not an object")
        name, sig, body = fn.get("name"), fn.get("signature"), fn.get("body")
        desc = fn.get("description", "")
        if not isinstance(name, str) or not _IDENT.match(name) or name in seen:
            raise InterfaceViolation(f"bad function name {name!r}")
        if not isinstance(sig, str) or not sig.strip().startswith("(") or not re.match(r"^\(\s*backend\b", sig.strip()):
            raise InterfaceViolation(f"{name}: signature must start with '(backend'")
        if not isinstance(body, str) or not body.strip():
            raise InterfaceViolation(f"{name}: empty body")
        if not isinstance(desc, str):
            raise InterfaceViolation(f"{name}: description must be a string")
        seen.add(name)
        out.append({"name": name, "description": desc.strip(), "signature": sig.strip(), "body": body})
    return out, smoke


class InterfaceBuilder:
    """One bounded model call -> api.py candidate -> static check -> smoke run -> append version."""

    def __init__(
        self,
        model: Any,
        store: InterfaceStore,
        runner: Any,
        sandbox: Any,
        procedures: Any = None,
        backend_bootstrap: str = "",
        timeout_seconds: int = 120,
    ) -> None:
        """``runner`` is a ``runners.Runner`` (``NativeRunner`` in production); ``sandbox`` an
        ``executor.Sandbox``; ``procedures`` a ``ProcedureStore`` (for script text and fitness);
        ``backend_bootstrap`` is Python prepended to every smoke script that must define ``backend``."""
        self.model = model
        self.store = store
        self.runner = runner
        self.sandbox = sandbox
        self.procedures = procedures
        self.backend_bootstrap = backend_bootstrap
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------- prompt

    def _script_text(self, procedure: Any, script: Any) -> str:
        if script is None and self.procedures is not None:
            try:
                script = self.procedures.load_script(procedure)
            except Exception:
                script = None
        if script is None:
            return ""
        try:
            from .planner import script_to_dict
            raw = script_to_dict(script)
        except Exception:
            raw = script
        try:
            return json.dumps(raw, sort_keys=True)[:_PROMPT_CHARS]
        except (TypeError, ValueError):
            return str(raw)[:_PROMPT_CHARS]

    def _user_prompt(self, app: str, procedure: Any, backend_api_doc: str, script: Any) -> str:
        goal = getattr(procedure, "goal", "") or ""
        pid = getattr(procedure, "id", "") or ""
        return (
            f"App: {app}\nProcedure: {pid}\nGoal: {goal}\n\n"
            f"Backend API:\n{(backend_api_doc or '')[:_PROMPT_CHARS]}\n\n"
            f"Solved script (JSON):\n{self._script_text(procedure, script)}\n\n"
            "JSON object:"
        )

    def _ask(self, system: str, user: str) -> str:
        ask = getattr(self.model, "complete_json", None)
        if callable(ask):
            return ask(system, user)
        from .planner import extract_json
        return extract_json(self.model.complete(system, user))

    # ------------------------------------------------------------- build

    def smoke_code(self, api_source: str, smoke: str) -> str:
        return "\n".join(
            [self.backend_bootstrap.rstrip(), "", "# --- api ---", api_source.rstrip(), "", "# --- smoke ---", smoke.rstrip(), ""]
        )

    def _check_display(self, display: Any) -> str:
        if not isinstance(display, str) or not display.strip():
            raise InterfaceViolation("display must be given explicitly (an isolated display, never the host's)")
        host = os.environ.get("DISPLAY")
        if host and display.strip() == host.strip():
            raise InterfaceViolation(f"display {display!r} is the host display; smoke scripts run only on an isolated one")
        return display.strip()

    def build(self, app: str, procedure: Any, backend_api_doc: str, display: str, script: Any = None) -> int | None:
        """Append v<N+1> for ``app`` when the generated API passes its smoke run; else None and nothing written.

        Raises ``InterfaceViolation`` only for a missing/host ``display``; every model-side problem
        (bad JSON, bad shape, forbidden source, syntax error, smoke failure) returns None."""
        display = self._check_display(display)
        try:
            reply = self._ask(BUILDER_SYSTEM_PROMPT, self._user_prompt(app, procedure, backend_api_doc, script))
            functions, smoke = _validate_reply(reply)
        except Exception:
            return None
        # Gate 1: AST allow-list on every body and the smoke script. Gate 2: forbidden fragments.
        for fn in functions:
            if check_ast(fn["body"]) or check_source(fn["body"]) or check_source(fn["signature"]):
                return None
        if check_ast(smoke) or check_source(smoke):
            return None
        api_source = render_api(functions)
        try:
            ast.parse(api_source)
            ast.parse(smoke)
        except SyntaxError:
            return None
        from ..schema import ScriptPayload

        try:
            payload = ScriptPayload(language="python", code=self.smoke_code(api_source, smoke), target_directory=".",
                                    timeout_seconds=self.timeout_seconds)
            result = self.runner.run(payload, self.sandbox, env_extra={"DISPLAY": display}, isolated_display=False)
        except Exception:
            return None
        if getattr(result, "timed_out", False) or getattr(result, "exit_code", 1) != 0:
            return None
        pid = getattr(procedure, "id", None)
        for fn in functions:
            fn["passed_on"] = [pid] if pid else []
        try:
            return self.store.write(app, functions)
        except FileExistsError:
            return None

    # ------------------------------------------------------------- fitness

    def fitness(self, app: str, replay_fn: Callable[[str], bool]) -> dict:
        """Replay the newest procedure of each goal of ``app`` (``ProcedureStore.newest``, which may be an
        unverified heal; superseded versions are not replayed) through ``replay_fn(procedure_id) -> bool``
        and append the counts to the newest version's ``fitness.jsonl``. Returns ``{"version", "replayed", "passed"}``."""
        ids = sorted(p.id for p in self.procedures.newest(app)) if self.procedures is not None else []
        version = self.store.latest(app)
        if version is None or not ids:
            return {"version": version, "replayed": 0, "passed": 0}
        passed = 0
        for pid in ids:
            try:
                ok = bool(replay_fn(pid))
            except Exception:
                ok = False
            passed += int(ok)
        self.store.write_fitness(app, len(ids), passed)
        return {"version": version, "replayed": len(ids), "passed": passed}
