"""Command line entry point: ``o-engine serve|run|cache ls|context|check|computer run|computer procedures|
tools ls|tools call|script run|swarm run|web run``."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import uuid
from pathlib import Path
from typing import Any

from .server import EngineSettings, health_report

REQUIRED_PROBES = ("vllm", "screenpipe", "playwright")


def _settings(args: argparse.Namespace) -> EngineSettings:
    data_dir = getattr(args, "data_dir", None)
    return EngineSettings.from_env(data_dir=data_dir)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    settings = _settings(args)
    if not settings.token:
        print("O_ENGINE_TOKEN is not set; refusing to serve without a token", file=sys.stderr)
        return 2
    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .schema import SchemaViolation, parse_request
    from .server import EngineDeps

    settings = _settings(args)
    try:
        request = parse_request(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, SchemaViolation) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    deps = EngineDeps(settings)
    job_id = uuid.uuid4().hex
    (settings.sandbox_root_path / job_id).mkdir(parents=True, exist_ok=True)

    def approve_waiter(_job_id: str, _timeout: float) -> bool:
        return settings.permission_mode != "ask"

    state: dict[str, Any]
    try:
        result = deps.run_job(deps.graph, request, job_id, settings.permission_mode, approve_waiter)
        state = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    except Exception as exc:
        state = {"id": job_id, "label": request.label, "done": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(state, indent=2, default=str))
    return 0 if state.get("ok") else 1


def cmd_cache(args: argparse.Namespace) -> int:
    from .cache import ScriptCache

    settings = _settings(args)
    cache = ScriptCache(str(settings.data_dir_path / "cache"))
    rows = [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in cache.index()]
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("cache is empty")
        return 0
    print(f"{'fingerprint':<16} {'platform':<8} {'runs':>4}  label")
    for r in rows:
        print(f"{str(r.get('fingerprint', ''))[:16]:<16} {str(r.get('platform', '')):<8} {str(r.get('runs', '')):>4}  {r.get('label', '')}")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    from .context import ContextStore

    settings = _settings(args)
    store = ContextStore(settings.data_dir, settings.screenpipe_db)
    profile = store.profile(args.cwd or os.getcwd())
    print(json.dumps(profile.model_dump() if hasattr(profile, "model_dump") else profile, indent=2, default=str))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    settings = _settings(args)
    report = health_report(settings)
    rows = [
        ("vllm", report["vllm"], settings.vllm_url, True),
        ("screenpipe", report["screenpipe"], settings.screenpipe_db, True),
        ("playwright", report["playwright"], "chromium", True),
        ("xvfb", report["xvfb"], "Xvfb on PATH", platform.system() == "Linux"),
    ]
    print(f"o-engine {report['version']}")
    missing = False
    for name, ok, where, required in rows:
        mark = "ok " if ok else ("MISSING" if required else "absent ")
        tag = "required" if required else "optional"
        print(f"  {name:<11} {mark:<8} {tag:<9} {where}")
        if required and not ok:
            missing = True
    return 1 if missing else 0


def cmd_computer_run(args: argparse.Namespace) -> int:
    from .server import CU_CANDIDATES_NEED_LINUX, EngineDeps

    settings = _settings(args)
    candidates = int(args.candidates or settings.cu_candidates or 1)
    if candidates > 1 and not sys.platform.startswith("linux"):
        # same refusal as POST /computer/run, before any display or backend is touched
        print(json.dumps({"ok": False, "error": CU_CANDIDATES_NEED_LINUX}, indent=2))
        return 2
    deps = EngineDeps(settings)
    job_id = uuid.uuid4().hex
    state: dict[str, Any]
    try:
        result = deps.run_cu_job(deps.cu_graph, args.goal, args.app or "", job_id, candidates)
        state = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    except Exception as exc:
        state = {"id": job_id, "label": args.goal[:200], "done": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(state, indent=2, default=str))
    return 0 if state.get("ok") else 1


def cmd_computer_procedures(args: argparse.Namespace) -> int:
    from .cu.procedures import ProcedureStore

    settings = _settings(args)
    store = ProcedureStore(str(settings.data_dir_path / "procedures"))
    rows = [r.model_dump() if hasattr(r, "model_dump") else (dict(r) if isinstance(r, dict) else vars(r)) for r in store.list()]
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("no procedures stored")
        return 0
    print(f"{'procedure_id':<40} {'app':<18} {'v':>3}  goal")
    for r in rows:
        pid = str(r.get("procedure_id") or r.get("id") or r.get("path") or "")
        print(f"{pid[:40]:<40} {str(r.get('app', ''))[:18]:<18} {str(r.get('version', '')):>3}  {str(r.get('goal', ''))[:60]}")
    return 0


# ---------------------------------------------------------------------------- Phase 2


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_tools_ls(args: argparse.Namespace) -> int:
    from .server import EngineDeps

    deps = EngineDeps(_settings(args))
    tools = deps.mcp_router.list_tools(refresh=True)
    if args.json:
        _print_json({"tools": tools})
        return 0
    if not tools:
        print(f"no MCP tools (servers file: {deps.settings.mcp_servers_path})")
        return 0
    print(f"{'server':<20} {'tool':<32} description")
    for t in tools:
        print(f"{str(t.get('server', ''))[:20]:<20} {str(t.get('name', ''))[:32]:<32} {str(t.get('description', ''))[:70]}")
    return 0


def cmd_tools_call(args: argparse.Namespace) -> int:
    from .mcp_router import ToolCall
    from .server import EngineDeps

    try:
        parsed = json.loads(args.args_json) if args.args_json else {}
    except json.JSONDecodeError as exc:
        _print_json({"ok": False, "content": f"args are not JSON: {exc.msg} at {exc.pos}", "is_error": True, "elapsed_ms": 0})
        return 2
    if not isinstance(parsed, dict):
        _print_json({"ok": False, "content": "args must be a JSON object", "is_error": True, "elapsed_ms": 0})
        return 2
    try:
        call = ToolCall(server=args.server, tool=args.tool, args=parsed)
    except Exception as exc:
        _print_json({"ok": False, "content": str(exc), "is_error": True, "elapsed_ms": 0})
        return 2
    result = EngineDeps(_settings(args)).mcp_router.call(call)
    _print_json(result.model_dump())
    return 0 if result.ok else 1


def cmd_script_run(args: argparse.Namespace) -> int:
    from .executor import Sandbox, SandboxEscape
    from .graph import step_record
    from .schema import ScriptPayload
    from .server import DEFAULT_SCRIPT_TIMEOUT_S, SCRIPT_SANDBOX_PREFIX, EngineDeps

    settings = _settings(args)
    try:
        code = Path(args.file).read_text(encoding="utf-8")
        payload = ScriptPayload(language=args.language, code=code, target_directory=args.dir or ".",
                                timeout_seconds=int(args.timeout or DEFAULT_SCRIPT_TIMEOUT_S))
    except (OSError, ValueError) as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 2
    deps = EngineDeps(settings)
    sandbox_dir = settings.sandbox_root_path / f"{SCRIPT_SANDBOX_PREFIX}{uuid.uuid4().hex}"
    try:
        result = deps.runner.run(payload, Sandbox(str(sandbox_dir)), 0, None, False)
    except SandboxEscape as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 2
    record = step_record(result, 0)
    record["sandbox"] = str(sandbox_dir)
    _print_json(record)
    return 0 if record["exit_code"] == 0 and not record["timed_out"] else 1


def cmd_swarm_run(args: argparse.Namespace) -> int:
    from .server import SWARM_NO_PLAN, EngineDeps, _swarm_result_dump

    deps = EngineDeps(_settings(args))
    try:
        result = _swarm_result_dump(deps.crew.run(args.objective, args.cwd or os.getcwd()))
    except Exception as exc:
        _print_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    ok = result.get("request") is not None
    _print_json({"ok": ok, "error": None if ok else SWARM_NO_PLAN, "result": result})
    return 0 if ok else 1


def cmd_web_run(args: argparse.Namespace) -> int:
    from .server import EngineDeps
    from .web import DOM_TIMEOUT_S

    deps = EngineDeps(_settings(args))
    runner = deps.web_runner()
    if getattr(runner, "via", "") == "browser-use":
        result = runner.run(args.task, args.url)
    elif args.url:
        result = runner.run([{"op": "goto", "url": args.url}, {"op": "read"}], DOM_TIMEOUT_S)
    else:
        _print_json({"ok": False, "via": getattr(runner, "via", "playwright-dom"),
                     "error": "browser_use is not installed; pass --url for the DOM runner"})
        return 2
    dumped = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    _print_json(dumped)
    return 0 if dumped.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="o-engine", description="O AI provisioning engine")
    p.add_argument("--data-dir", default=None, help="engine data directory (default: O_DATA_DIR or ~/.o-engine)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    s.set_defaults(func=cmd_serve)

    r = sub.add_parser("run", help="run one ProvisionRequest JSON file synchronously")
    r.add_argument("file")
    r.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("cache", help="script cache")
    csub = c.add_subparsers(dest="cache_command", required=True)
    ls = csub.add_parser("ls", help="list cached scripts")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    ls.set_defaults(func=cmd_cache)

    x = sub.add_parser("context", help="print the workspace profile")
    x.add_argument("cwd", nargs="?", default=None)
    x.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    x.set_defaults(func=cmd_context)

    k = sub.add_parser("check", help="probe required pieces")
    k.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    k.set_defaults(func=cmd_check)

    cu = sub.add_parser("computer", help="computer use (accessibility-first desktop automation)")
    cusub = cu.add_subparsers(dest="computer_command", required=True)
    cr = cusub.add_parser("run", help="plan, run, judge and store a computer-use goal synchronously")
    cr.add_argument("goal")
    cr.add_argument("--app", default="", help="application id the goal targets (e.g. libreoffice-calc)")
    cr.add_argument("--candidates", type=int, default=None, help="best-of-N candidates (Linux; default O_CU_CANDIDATES)")
    cr.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    cr.set_defaults(func=cmd_computer_run)
    cp = cusub.add_parser("procedures", help="list stored procedures (append-only, with lineage)")
    cp.add_argument("--json", action="store_true")
    cp.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    cp.set_defaults(func=cmd_computer_procedures)

    # ---- Phase 2 ----
    t = sub.add_parser("tools", help="MCP tools (servers from <data_dir>/mcp-servers.json)")
    tsub = t.add_subparsers(dest="tools_command", required=True)
    tl = tsub.add_parser("ls", help="list the tools of every configured MCP server")
    tl.add_argument("--json", action="store_true")
    tl.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    tl.set_defaults(func=cmd_tools_ls)
    tc = tsub.add_parser("call", help="call one tool: tools call <server> <tool> '<json args>'")
    tc.add_argument("server")
    tc.add_argument("tool")
    tc.add_argument("args_json", nargs="?", default="{}", help="JSON object of arguments (default {})")
    tc.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    tc.set_defaults(func=cmd_tools_call)

    sc = sub.add_parser("script", help="multi-language script runner")
    scsub = sc.add_subparsers(dest="script_command", required=True)
    sr = scsub.add_parser("run", help="run a script file in a fresh sandbox: script run <language> <file> [--dir]")
    sr.add_argument("language", choices=("python", "bash", "javascript", "powershell"))
    sr.add_argument("file")
    sr.add_argument("--dir", default=None, help="target_directory inside the sandbox (default .)")
    sr.add_argument("--timeout", type=int, default=None, help="seconds (default 120)")
    sr.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    sr.set_defaults(func=cmd_script_run)

    sw = sub.add_parser("swarm", help="role-partitioned planning (telemetry audit, plan, inspection)")
    swsub = sw.add_subparsers(dest="swarm_command", required=True)
    swr = swsub.add_parser("run", help='run the crew on an objective: swarm run "<objective>"')
    swr.add_argument("objective")
    swr.add_argument("--cwd", default=None, help="workspace the objective targets (default: current directory)")
    swr.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    swr.set_defaults(func=cmd_swarm_run)

    w = sub.add_parser("web", help="headless web automation")
    wsub = w.add_subparsers(dest="web_command", required=True)
    wr = wsub.add_parser("run", help='web run "<task>" [--url]  (browser-use, or goto+read via Playwright when absent)')
    wr.add_argument("task")
    wr.add_argument("--url", default=None)
    wr.add_argument("--data-dir", default=argparse.SUPPRESS, dest="data_dir")
    wr.set_defaults(func=cmd_web_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
