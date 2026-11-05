"""Localhost HTTP API used by the O desktop app.

One FastAPI app per ``EngineSettings``. Every request carries ``x-o-token``;
jobs run on a small thread pool, each in its own sandbox subdirectory, and every
state change is written to the action log.
"""
from __future__ import annotations

import hmac
import os
import platform
import queue
import shutil
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .actionlog import ActionLog, get_log
from .genui import UIStore, frame_to_json, oui, parse_frame, valid_surface
from .schema import ProvisionRequest, SchemaViolation, VerifyTarget, parse_request, parse_steps

PHASES = ("context", "validate", "cache_lookup", "gate", "execute", "verify", "remediate", "cache_store", "report")
CU_PHASES = ("context", "plan", "ground", "execute", "judge", "heal", "store", "report")
HEALTH_TIMEOUT_S = 2.0
MAX_WORKERS = 4
CU_BASE_DISPLAY = 99
CU_MAX_CANDIDATES = 8
UI_SSE_POLL_S = 0.25
UI_SSE_KEEPALIVE_S = 15.0
UI_SSE_DONE_GRACE_S = 1.0
CU_CANDIDATES_NEED_LINUX = "candidates > 1 needs isolated displays (Linux/Xvfb)"


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _default_screenpipe_db() -> str:
    return str(Path.home() / ".screenpipe" / "db.sqlite")


def _truthy_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


class EngineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = ""
    log_url: str | None = None
    log_token: str = ""
    vllm_url: str = "http://127.0.0.1:8000/v1"
    vllm_model: str = "meta-llama/Meta-Llama-3-8B-Instruct"
    screenpipe_db: str = Field(default_factory=_default_screenpipe_db)
    sandbox_root: str = Field(default_factory=lambda: str(Path.home() / "o-sandbox"))
    permission_mode: str = "log"
    max_attempts: int = 3
    claude_path: str = "claude"
    data_dir: str = Field(default_factory=lambda: str(Path.home() / ".o-engine"))
    cu_candidates: int = 1
    cu_max_heals: int = 3
    vllm_vision_model: str = ""
    # ---- Wave 2 computer use (plan I-J): the reactive milestone loop and its gates ----
    cu_steps_per_turn: int = Field(default=5, ge=1, le=20)  # O_CU_STEPS_PER_TURN: steps per next_steps turn
    cu_max_turns: int = Field(default=20, ge=1, le=200)  # O_CU_MAX_TURNS: turns per job before giving up
    cu_ground_confidence: float = Field(default=0.3, ge=0.0, le=1.0)  # O_CU_GROUND_CONFIDENCE: Grounder gate
    cu_step_candidates: int = Field(default=1, ge=1, le=8)  # O_CU_STEP_CANDIDATES: per-step candidate plans
    cu_feasibility: bool = True  # O_CU_FEASIBILITY: read-only probes + one model question before plan
    cu_zoom: bool = True  # O_CU_ZOOM: wrap the vision grounder in ZoomGrounder
    cu_comparative: bool = True  # O_CU_COMPARATIVE: best-of-N ranks all candidates in one model call
    cu_interface_builder: bool = True  # O_CU_INTERFACE_BUILDER: build api.py per app after a fresh store
    # ---- Phase 2 (tooling core / containment / swarm / headless web) ----
    mcp_servers_file: str = ""  # "" -> <data_dir>/mcp-servers.json (see mcp_servers_path)
    high_risk_sandbox: str = "docker"
    e2b_api_key: str = ""
    docker_image: str = "python:3.12-slim"
    docker_network: str = "bridge"
    swarm: bool = True
    web_prefer: str = "browser-use"
    # ---- Phase 3 (GenUI) ----
    genui: bool = True  # O_GENUI: emit GenUI frames at all
    ui_url: str | None = None  # O_UI_URL: the app endpoint every GenUI frame is pushed to (optional)
    # ---- learning / adaptation ----
    learning: bool = True  # O_LEARNING: record observations, derive the profile, inject it into cu prompts
    learning_dir: str = ""  # O_LEARNING_DIR: "" -> <data_dir>/learning (see learning_path)
    learning_model: bool = True  # O_LEARNING_MODEL: allow one local-only merge call for near-duplicate items
    # ---- ambient sensing / autonomous infrastructure (plan J); everything off by default ----
    ambient: bool = False  # O_AMBIENT: master switch (sensors, presence thread, supervisor)
    sensor_csi: bool = False  # O_SENSOR_WIFI (O_SENSOR_CSI also read)
    sensor_sonar: bool = False  # O_SENSOR_SONAR
    sensor_camera: bool = False  # O_SENSOR_CAMERA
    sensor_heart: bool = False  # O_SENSOR_HEART_RATE (O_SENSOR_HEART also read): live pulse/breathing, memory only
    absent_after_s: float = Field(default=300.0, ge=5.0, le=86400.0)  # O_ABSENT_AFTER_S
    brief_tts: bool = False  # O_BRIEF_TTS: reported only; the app speaks the brief, the engine never does
    bus_url: str = ""  # O_BUS_URL: "" in-process, mqtt://host:port or redis://host:port/db
    csi_udp: str = ""  # O_CSI_UDP: host:port an ESP32 CSI node streams to
    ruview_ws: str = ""  # O_RUVIEW_WS: RuView sensing WebSocket URL
    ruview_mqtt: str = ""  # O_RUVIEW_MQTT: mqtt://host:port/<topic> of RuView's MQTT output
    rppg_model: str = "FacePhys.rlap"  # O_RPPG_MODEL: open-rppg model when installed
    auto_repair: bool = False  # O_AUTO_REPAIR: let the supervisor reinstall a missing engine package
    vllm_cmd: str = ""  # O_VLLM_CMD: argv to (re)start a local vLLM under the supervisor
    screenpipe_cmd: str = ""  # O_SCREENPIPE_CMD: argv to (re)start screenpipe under the supervisor

    @classmethod
    def from_env(cls, **overrides: Any) -> "EngineSettings":
        values: dict[str, Any] = {
            "token": _env("O_ENGINE_TOKEN"),
            "log_url": _env("O_LOG_URL") or None,
            "log_token": _env("O_LOG_TOKEN") or _env("O_ENGINE_TOKEN"),
            "vllm_url": _env("O_VLLM_URL", "http://127.0.0.1:8000/v1"),
            "vllm_model": _env("O_VLLM_MODEL", "meta-llama/Meta-Llama-3-8B-Instruct"),
            "screenpipe_db": _env("O_SCREENPIPE_DB", _default_screenpipe_db()),
            "sandbox_root": _env("O_SANDBOX_ROOT", str(Path.home() / "o-sandbox")),
            "permission_mode": _env("O_PERMISSION_MODE", "log"),
            "max_attempts": int(_env("O_MAX_ATTEMPTS", "3")),
            "claude_path": _env("O_CLAUDE_PATH", "claude"),
            "data_dir": _env("O_DATA_DIR", str(Path.home() / ".o-engine")),
            "cu_candidates": int(_env("O_CU_CANDIDATES", "1")),
            "cu_max_heals": int(_env("O_CU_MAX_HEALS", "3")),
            "vllm_vision_model": _env("O_VLLM_VISION_MODEL", ""),
            # Wave 2 computer use
            "cu_steps_per_turn": int(_env("O_CU_STEPS_PER_TURN", "5")),
            "cu_max_turns": int(_env("O_CU_MAX_TURNS", "20")),
            "cu_ground_confidence": float(_env("O_CU_GROUND_CONFIDENCE", "0.3")),
            "cu_step_candidates": int(_env("O_CU_STEP_CANDIDATES", "1")),
            "cu_feasibility": _truthy_env("O_CU_FEASIBILITY", True),
            "cu_zoom": _truthy_env("O_CU_ZOOM", True),
            "cu_comparative": _truthy_env("O_CU_COMPARATIVE", True),
            "cu_interface_builder": _truthy_env("O_CU_INTERFACE_BUILDER", True),
            # Phase 2
            "mcp_servers_file": _env("O_MCP_SERVERS_FILE", ""),
            "high_risk_sandbox": _env("O_HIGH_RISK_SANDBOX", "docker"),
            "e2b_api_key": _env("E2B_API_KEY", ""),
            "docker_image": _env("O_DOCKER_IMAGE", "python:3.12-slim"),
            "docker_network": _env("O_DOCKER_NETWORK", "bridge"),
            "swarm": _truthy_env("O_SWARM", True),
            "web_prefer": _env("O_WEB_PREFER", "browser-use"),
            # Phase 3
            "genui": _truthy_env("O_GENUI", True),
            "ui_url": _env("O_UI_URL") or None,
            # learning
            "learning": _truthy_env("O_LEARNING", True),
            "learning_dir": _env("O_LEARNING_DIR", ""),
            "learning_model": _truthy_env("O_LEARNING_MODEL", True),
            # ambient (plan J)
            "ambient": _truthy_env("O_AMBIENT", False),
            "sensor_csi": _truthy_env("O_SENSOR_WIFI", _truthy_env("O_SENSOR_CSI", False)),
            "sensor_sonar": _truthy_env("O_SENSOR_SONAR", False),
            "sensor_camera": _truthy_env("O_SENSOR_CAMERA", False),
            "sensor_heart": _truthy_env("O_SENSOR_HEART_RATE", _truthy_env("O_SENSOR_HEART", False)),
            "absent_after_s": float(_env("O_ABSENT_AFTER_S", "300")),
            "brief_tts": _truthy_env("O_BRIEF_TTS", False),
            "bus_url": _env("O_BUS_URL", ""),
            "csi_udp": _env("O_CSI_UDP", ""),
            "ruview_ws": _env("O_RUVIEW_WS", ""),
            "ruview_mqtt": _env("O_RUVIEW_MQTT", ""),
            "rppg_model": _env("O_RPPG_MODEL", "FacePhys.rlap"),
            "auto_repair": _truthy_env("O_AUTO_REPAIR", False),
            "vllm_cmd": _env("O_VLLM_CMD", ""),
            "screenpipe_cmd": _env("O_SCREENPIPE_CMD", ""),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    @property
    def data_dir_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def sandbox_root_path(self) -> Path:
        return Path(self.sandbox_root)

    # ---- Phase 2 ----
    @property
    def mcp_servers_path(self) -> Path:
        return Path(self.mcp_servers_file) if self.mcp_servers_file else self.data_dir_path / "mcp-servers.json"

    @property
    def learning_path(self) -> Path:
        return Path(self.learning_dir) if self.learning_dir else self.data_dir_path / "learning"

    @property
    def settings_like(self) -> dict[str, Any]:
        """The camelCase view containment.py / swarm.py read (``_setting(settings_like, 'dockerImage', ...)``)."""
        return {
            "highRiskSandbox": self.high_risk_sandbox,
            "dockerImage": self.docker_image,
            "dockerNetwork": self.docker_network,
            "e2bApiKey": self.e2b_api_key,
            "swarmEnabled": self.swarm,
            "vllmUrl": self.vllm_url,
            "vllmModel": self.vllm_model,
        }


class EngineDeps:
    """Everything the routes need. Built lazily so a missing sibling module fails at startup, not import."""

    def __init__(self, settings: EngineSettings) -> None:
        from .cache import ScriptCache
        from .context import ContextStore
        from .display import IsolatedDisplay
        from .executor import Sandbox
        from .graph import Deps, build_graph, run_job
        from .parser import ClaudeCodeParser, LocalModelParser, Parser

        self.settings = settings
        self.context = ContextStore(settings.data_dir, settings.screenpipe_db)
        self.parser = Parser(
            LocalModelParser(settings.vllm_url, settings.vllm_model),
            ClaudeCodeParser(settings.claude_path),
        )
        self.cache = ScriptCache(str(settings.data_dir_path / "cache"))
        self.display = IsolatedDisplay()
        self.sandbox_root = settings.sandbox_root_path
        self.verify = verify_target
        self.run_job = run_job
        self.log = get_log(settings.data_dir, settings.log_url, settings.log_token or settings.token)
        self.ui = UIStore(forward_url=settings.ui_url, token=settings.log_token or settings.token) if settings.genui else None
        # Phase 2 lazies (mcp_router / crew / web runners) live here; the runner is built now because
        # the graph's execute node needs it and NativeRunner construction is free.
        self._p2: dict[str, Any] = {}
        self._p2_lock = threading.RLock()
        self._graph_fields: dict[str, Any] = dict(
            context=self.context,
            parser=self.parser,
            cache=self.cache,
            display=self.display,
            verify_target=self.verify,
            log=self.log,
            max_attempts=settings.max_attempts,
            platform=sys.platform,
            runner=self.runner,
            high_risk_sandbox=settings.high_risk_sandbox,
            settings_like=settings.settings_like,
            ui=self.ui,
        )
        self.graph = build_graph(
            Deps(sandbox_factory=lambda job_id: Sandbox(str(self.sandbox_root / job_id)), **self._graph_fields)
        )
        # Computer use is built lazily: nothing below imports a platform accessibility package
        # until the first /computer/* request, so /health stays importable everywhere.
        self._cu: dict[str, Any] = {}
        self._cu_lock = threading.RLock()
        self.cu_displays = cu_display_names(settings.cu_candidates)

    def cu_backend(self, slot: int = 0) -> Any:
        """One backend per candidate slot. Linux: its own ``IsolatedDisplay`` (``:99 + slot``); pixel
        actions are allowed only when that display is actually running after ``start()`` (no Xvfb, no
        pixels). Elsewhere: a single host-API backend with ``allow_pixels=False``."""
        from .cu.backends import get_backend

        with self._cu_lock:
            key = f"backend:{slot}"
            if key not in self._cu:
                fake_tree = os.environ.get("O_CU_FAKE_TREE", "")
                if fake_tree:
                    # Demo / test hook: a scripted accessibility tree (JSON list of Node dicts) served by the
                    # in-memory FakeBackend. No display, no pixels, nothing on the host is touched.
                    import json as _json

                    from .cu.backends.base import Node
                    from .cu.backends.fake import FakeBackend

                    raw = _json.loads(Path(fake_tree).read_text(encoding="utf-8"))
                    nodes = [Node.model_validate(n) for n in (raw if isinstance(raw, list) else [raw])]
                    self.log.record("note", "computer-use backend: scripted tree from O_CU_FAKE_TREE",
                                    path=fake_tree, slot=int(slot), nodes=len(nodes))
                    self._cu[key] = FakeBackend(nodes, allow_pixels=False)
                elif sys.platform.startswith("linux"):
                    from .display import IsolatedDisplay

                    display = IsolatedDisplay(f":{CU_BASE_DISPLAY + int(slot)}")
                    display.start()
                    self._cu[f"display:{slot}"] = display
                    is_host = bool(getattr(display, "is_host", False))
                    allow_pixels = bool(display.running) and not is_host
                    state = "is the host display: pixels refused" if is_host else ("running" if allow_pixels else "not running")
                    self.log.record("note", f"computer-use display {display.display} {state}",
                                    display=display.display, slot=int(slot), allow_pixels=allow_pixels)
                    self._cu[key] = get_backend(display=display.display, allow_pixels=allow_pixels)
                else:
                    self._cu[key] = get_backend(display=None, allow_pixels=False)
            return self._cu[key]

    def cu_display_status(self) -> list[dict[str, Any]]:
        """``[{display, running}]`` per candidate slot; ``running`` is only ever True for an isolated
        display whose Xvfb is up right now."""
        with self._cu_lock:
            rows = []
            for i, name in enumerate(self.cu_displays):
                display = self._cu.get(f"display:{i}")
                rows.append({"display": name, "running": bool(display is not None and display.running)})
            return rows

    def stop_cu_displays(self) -> None:
        """Stop every isolated display this process started (lifespan shutdown). Never raises."""
        with self._cu_lock:
            displays = [v for k, v in self._cu.items() if k.startswith("display:")]
        for display in displays:
            try:
                display.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must finish
                self.log.record("note", f"computer-use display {getattr(display, 'display', '?')} stop failed", error=str(exc))

    @property
    def cu_procedures(self) -> Any:
        from .cu.procedures import ProcedureStore

        with self._cu_lock:
            if "procedures" not in self._cu:
                self._cu["procedures"] = ProcedureStore(str(self.settings.data_dir_path / "procedures"))
            return self._cu["procedures"]

    @property
    def cu_interfaces(self) -> Any:
        """``InterfaceStore`` under ``<data_dir>/interfaces`` (beside the markdown notes). Read by
        ``GET /computer/interfaces`` and by the planner's progressive disclosure; written only by the builder."""
        from .cu.interfaces import InterfaceStore

        with self._cu_lock:
            if "interfaces" not in self._cu:
                self._cu["interfaces"] = InterfaceStore(self.settings.data_dir_path / "interfaces")
            return self._cu["interfaces"]

    def cu_sandbox(self, job_id: str) -> Any:
        """The job's own sandbox directory (``<sandbox_root>/<job_id>``), created on first use."""
        from .executor import Sandbox

        return Sandbox(str(self.sandbox_root / job_id))

    @property
    def cu_deps(self) -> Any:
        import dataclasses

        from .cu.graph import CUDeps
        from .cu.ground import ClaudeCodeGrounder, Grounder, LocatorCache, VisionGrounder, ZoomGrounder
        from .cu.interfaces import InterfaceBuilder, InterfaceNotes
        from .cu.judge import ClaudeCodeJudge, Judge, StateJudge, VLMJudge
        from .cu.planner import ClaudeCodeClient, LocalModelClient, ModelClient, Planner

        s = self.settings
        with self._cu_lock:
            if "deps" not in self._cu:
                vision_model = s.vllm_vision_model or s.vllm_model
                notes: Any = InterfaceNotes(str(s.data_dir_path / "interfaces"))
                if self.learning is not None:
                    from .learning import LearnedNotes

                    notes = LearnedNotes(notes, self.learning)  # planner + graph notes seam: for_prompt(app)
                model = ModelClient(LocalModelClient(s.vllm_url, s.vllm_model), ClaudeCodeClient(s.claude_path))
                primary: Any = VisionGrounder(s.vllm_url, vision_model)
                if s.cu_zoom:
                    primary = ZoomGrounder(primary)
                store = self.cu_interfaces
                builder = None
                if s.cu_interface_builder:
                    builder = InterfaceBuilder(
                        model, store, self.runner, self.cu_sandbox("interfaces"), procedures=self.cu_procedures,
                        backend_bootstrap=interface_bootstrap(),
                    )
                fields: dict[str, Any] = dict(
                    backend_factory=self.cu_backend,
                    grounder=Grounder(primary, ClaudeCodeGrounder(s.claude_path), min_confidence=s.cu_ground_confidence),
                    locator_cache=LocatorCache(s.data_dir_path / "locators.sqlite"),
                    judge=Judge(VLMJudge(s.vllm_url, vision_model), ClaudeCodeJudge(s.claude_path)),
                    planner=Planner(model, notes),
                    procedures=self.cu_procedures,
                    notes=notes,
                    log=self.log,
                    max_heals=s.cu_max_heals,
                    candidates=s.cu_candidates,
                    displays=list(self.cu_displays),
                    ui=self.ui,
                    feasibility=PerJobFeasibilityGate(model, self.cu_backend, self.cu_sandbox, self.log) if s.cu_feasibility else None,
                    state_judge_factory=lambda sandbox, backend: StateJudge(sandbox, backend),
                    interface_store=store,
                    interface_builder=builder,
                    runner=self.runner,
                    sandbox_for=self.cu_sandbox,
                    steps_per_turn=s.cu_steps_per_turn,
                    max_turns=s.cu_max_turns,
                    step_candidates=s.cu_step_candidates,
                    comparative=s.cu_comparative,
                )
                # ``comparative`` reaches BestOfN only once CUDeps (cu/graph.py) carries the field; until then
                # the setting is read and logged by name here and the graph's default applies.
                known = {f.name for f in dataclasses.fields(CUDeps)}
                dropped = sorted(k for k in fields if k not in known)
                if dropped:
                    self.log.record("note", "computer-use settings without a CUDeps field (graph default applies)",
                                    settings=dropped)
                self._cu["deps"] = CUDeps(**{k: v for k, v in fields.items() if k in known})
            return self._cu["deps"]

    @property
    def cu_graph(self) -> Any:
        from .cu.graph import build_cu_graph

        deps = self.cu_deps
        with self._cu_lock:
            if "graph" not in self._cu:
                self._cu["graph"] = build_cu_graph(deps)
            return self._cu["graph"]

    def run_cu_job(self, graph: Any, goal: str, app: str, job_id: str, candidates: int = 1, **kw: Any) -> dict:
        from .cu.graph import run_cu_job

        return run_cu_job(graph, goal, app, job_id, candidates, max_heals=self.settings.cu_max_heals, **kw)

    @property
    def learning(self) -> Any:
        """``LearningStore`` under ``learning_path`` (None when ``learning`` is off). Reads screenpipe app
        focus, interface fitness and procedure counts; its optional merge call is local-only."""
        if not self.settings.learning:
            return None
        with self._cu_lock:
            if "learning" not in self._cu:
                self._cu["learning"] = build_learning(self.settings, lambda: self.cu_interfaces, lambda: self.cu_procedures)
            return self._cu["learning"]

    def absent_graph(self, isolated: Any) -> Any:
        """The provisioning graph for USER_ABSENT (plan J.3): the same fixed topology and deps as
        ``graph``; only the per-job sandbox differs (``isolated.for_job``: microsandbox ->
        Firecracker -> Docker -> local). Built once, on the first headless job."""
        from .graph import Deps, build_graph

        with self._p2_lock:
            if "absent_graph" not in self._p2:
                self._p2["absent_graph"] = build_graph(Deps(sandbox_factory=isolated.for_job, **self._graph_fields))
            return self._p2["absent_graph"]

    # ------------------------------------------------------------------ Phase 2

    @property
    def runner(self) -> Any:
        """The multi-language script runner (native subprocess backend; never raises)."""
        from .runners import get_runner

        with self._p2_lock:
            if "runner" not in self._p2:
                self._p2["runner"] = get_runner("native")
            return self._p2["runner"]

    @property
    def mcp_router(self) -> Any:
        """``MCPRouter`` over ``<data_dir>/mcp-servers.json`` (or O_MCP_SERVERS_FILE). Connects lazily per server."""
        from .mcp_router import MCPRouter, load_config

        with self._p2_lock:
            if "mcp_router" not in self._p2:
                self._p2["mcp_router"] = MCPRouter(load_config(self.settings.mcp_servers_path), self.log)
            return self._p2["mcp_router"]

    @property
    def crew(self) -> Any:
        """The swarm crew (crewAI when importable and ``swarm`` is on, else ``SimpleCrew``). No display
        judge is wired: the inspector only probes verify ports, so nothing here starts an Xvfb."""
        from .swarm import SwarmDeps, get_crew

        with self._p2_lock:
            if "crew" not in self._p2:
                swarm_deps = SwarmDeps(context=self.context, parser=self.parser, verify_target=self.verify)
                self._p2["crew"] = get_crew(swarm_deps, self.log, self.settings.settings_like)
            return self._p2["crew"]

    def web_runner(self, display: Any = None, scripted: bool = False) -> Any:
        """A fresh web runner per call (runners hold no state). ``scripted`` forces the Playwright DOM
        runner; otherwise ``web_prefer`` decides and browser-use falls back to the DOM runner when absent."""
        from .web import PlaywrightDomRunner, get_web_runner

        display = self.display if display is None else display
        if scripted:
            return PlaywrightDomRunner(display=display)
        s = self.settings
        return get_web_runner(s.web_prefer, s.vllm_url, s.vllm_model, display=display)


def build_learning(settings: EngineSettings, interfaces: Any = None, procedures: Any = None) -> Any:
    from .cu.planner import LocalModelClient, ModelClient
    from .learning import LearningStore

    model = ModelClient(LocalModelClient(settings.vllm_url, settings.vllm_model), None) if settings.learning_model else None
    return LearningStore(settings.learning_path, screenpipe_db=settings.screenpipe_db or None,
                         interfaces=interfaces, procedures=procedures, model=model)


def build_goals(settings: EngineSettings) -> Any:
    """``GoalStore`` under ``<data_dir>/goals``; its drafting / proposal call is local-only (as learning's)."""
    from .cu.planner import LocalModelClient, ModelClient
    from .goals import GoalStore

    model = ModelClient(LocalModelClient(settings.vllm_url, settings.vllm_model), None) if settings.learning_model else None
    return GoalStore(settings.data_dir_path / "goals", model=model)


class PresenceSimulateRequest(BaseModel):
    """Body of ``POST /presence/simulate`` (demos and tests only; every call is logged)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    state: str = Field(pattern="^(USER_PRESENT|USER_ABSENT)$")
    reason: str | None = Field(default=None, max_length=200)


class LearnObserveRequest(BaseModel):
    """Body of ``POST /learn/observe``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(pattern="^(task|correction|screen|own)$")
    text: str = Field(min_length=1, max_length=2000)
    task_id: str | None = Field(default=None, max_length=128)
    outcome: str | None = Field(default=None, max_length=32)
    app: str | None = Field(default=None, max_length=200)
    duration_s: float | None = Field(default=None, ge=0)
    ts: float | None = None


class LearnFeedbackRequest(BaseModel):
    """Body of ``POST /learn/feedback``: exactly one of ``forget`` / ``confirm`` (a profile item id)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    forget: str | None = Field(default=None, min_length=1, max_length=64)
    confirm: str | None = Field(default=None, min_length=1, max_length=64)


def learn_outcome(store: Any, text: str, job_id: str, final: dict[str, Any], app: str = "", elapsed_s: float = 0.0) -> None:
    """Record one finished job as a ``task`` observation (the user's request + O's outcome). Never raises."""
    if store is None or not str(text or "").strip():
        return
    outcome = "infeasible" if final.get("infeasible") else ("ok" if final.get("ok") else "failed")
    try:
        store.observe("task", str(text), task_id=job_id, outcome=outcome, app=app or None,
                      duration_s=max(0.0, float(elapsed_s)))
    except Exception:
        pass


def cu_display_names(candidates: int) -> list[str]:
    n = max(1, min(int(candidates or 1), CU_MAX_CANDIDATES))
    if sys.platform.startswith("linux"):
        return [f":{CU_BASE_DISPLAY + i}" for i in range(n)]
    return ["host"]


def interface_bootstrap() -> str:
    """Python prepended to every interface smoke script: puts the engine on ``sys.path`` and binds
    ``backend`` to the platform backend on the display the runner was given (an isolated one; the
    builder refuses the host display before anything runs). Pixels stay off."""
    engine_dir = str(Path(__file__).resolve().parent.parent)
    return (
        "import os, sys\n"
        f"sys.path.insert(0, {engine_dir!r})\n"
        "from o_engine.cu.backends import get_backend\n"
        "backend = get_backend(display=os.environ[\"DISPLAY\"], allow_pixels=False)\n"
    )


class PerJobFeasibilityGate:
    """``check(goal, app, job_id)`` builds one ``FeasibilityGate`` over the slot-0 backend and that job's
    sandbox, so the probes (``tree``, ``which``, ...) see the backend the job will drive and only the
    job's own directory. Nothing is built until the first fresh goal reaches ``context``."""

    def __init__(self, model: Any, backend_for: Callable[[int], Any], sandbox_for: Callable[[str], Any], log: Any) -> None:
        self.model = model
        self.backend_for = backend_for
        self.sandbox_for = sandbox_for
        self.log = log

    def check(self, goal: str, app: str, job_id: str) -> Any:
        from .cu.feasibility import FeasibilityGate

        return FeasibilityGate(self.model, self.backend_for(0), self.sandbox_for(job_id), self.log).check(goal, app, job_id)


def interface_rows(store: Any) -> list[dict[str, Any]]:
    """``[{app, latest, functions:[{name, description, signature}], fitness}]`` for ``GET /computer/interfaces``.
    Fitness comes from ``store.read()`` (computed from ``fitness.jsonl`` at read time), never from an
    ``api.json`` field; function bodies are never returned."""
    list_apps = getattr(store, "list_apps", None)
    if callable(list_apps):
        apps = [str(a) for a in list_apps()]
    else:
        root = Path(getattr(store, "dir", ""))
        apps = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    rows: list[dict[str, Any]] = []
    for app in sorted(apps):
        meta = store.read(app)
        if not isinstance(meta, dict):
            continue
        functions = [
            {"name": str(fn.get("name", "")), "description": str(fn.get("description", "") or ""),
             "signature": str(fn.get("signature", "") or "")}
            for fn in (meta.get("functions") or []) if isinstance(fn, dict)
        ]
        latest = meta.get("version")
        if latest is None:
            latest = store.latest(app)
        fitness = meta.get("fitness")
        rows.append({"app": app, "latest": latest, "functions": functions,
                     "fitness": dict(fitness) if isinstance(fitness, dict) else None})
    return rows


def probe_cu_backend() -> dict[str, Any]:
    """``{name, available, reason}`` for the host accessibility backend. Never raises, never imports
    gi/uiautomation/atomacos itself: the backend package decides and reports."""
    try:
        from .cu.backends import get_backend

        backend = get_backend(display=None, allow_pixels=False)
    except Exception as exc:
        return {"name": "none", "available": False, "reason": f"{type(exc).__name__}: {exc}"}
    name = str(getattr(backend, "name", type(backend).__name__))
    available = bool(getattr(backend, "available", True))
    reason = getattr(backend, "reason", None) or ("" if available else "backend reports unavailable")
    return {"name": name, "available": available, "reason": str(reason)}


def verify_target(target: VerifyTarget) -> dict:
    from .verify import verify_target as _vt

    return _vt(target)


class _Job:
    def __init__(self, job_id: str, request: Any, max_attempts: int, phases: tuple[str, ...] = PHASES) -> None:
        self.id = job_id
        self.request = request
        self.phases = phases
        self.event = threading.Event()
        self.decision: bool | None = None
        self.state: dict[str, Any] = {
            "id": job_id,
            "label": request.label,
            "phase": "context",
            "attempt": 0,
            "max_attempts": max_attempts,
            "cached": False,
            "steps": [],
            "done": False,
            "ok": False,
        }


class _Jobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}

    def add(self, job: _Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> _Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def set_state(self, job_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.state = state

    def approve_waiter(self, job_id: str, timeout: float) -> bool | None:
        job = self.get(job_id)
        if job is None:
            return None
        if not job.event.wait(timeout):
            return None
        return job.decision

    def resolve(self, job_id: str, allow: bool) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.decision = bool(allow)
        job.event.set()
        return True


def _probe_vllm(url: str) -> bool:
    try:
        r = httpx.get(url.rstrip("/") + "/models", timeout=HEALTH_TIMEOUT_S)
        return r.status_code == 200
    except Exception:
        return False


def _probe_playwright() -> bool:
    try:
        from .verify import playwright_ready

        return playwright_ready()
    except Exception:
        return False


def cu_display_status(settings: EngineSettings, deps: Any = None) -> list[dict[str, Any]]:
    """``[{display, running}]``: live status from ``deps`` when they exist, else every display not running
    (nothing can be running before the first /computer/* request builds them)."""
    status = getattr(deps, "cu_display_status", None) if deps is not None else None
    if callable(status):
        try:
            rows = status()
            return [{"display": str(r.get("display")), "running": bool(r.get("running"))} for r in rows]
        except Exception:
            pass
    return [{"display": name, "running": False} for name in cu_display_names(settings.cu_candidates)]


def health_report(settings: EngineSettings, deps: Any = None) -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "vllm": _probe_vllm(settings.vllm_url),
        "screenpipe": bool(settings.screenpipe_db) and os.path.isfile(settings.screenpipe_db),
        "xvfb": shutil.which("Xvfb") is not None,
        "playwright": _probe_playwright(),
        "cu_backend": probe_cu_backend(),
        "cu_displays": cu_display_status(settings, deps),
        **phase2_health(settings),
    }


# ---------------------------------------------------------------------------- Phase 2
# Health probes (cheap and bounded), request bodies and job phases for the tooling core,
# the disposable layer, the swarm and headless web. Routes are registered in create_app
# under the "Phase 2 routes" block.

SWARM_PHASES = ("context", "plan", "verify", "report")
WEB_PHASES = ("context", "run", "report")
SWARM_ROLE_PHASE = {"telemetry_auditor": "context", "subprocess_compiler": "plan", "environment_inspector": "verify"}
SWARM_NO_PLAN = "the compiler role produced no valid plan"
SCRIPT_SANDBOX_PREFIX = "script-"
DEFAULT_SCRIPT_TIMEOUT_S = 120
DOCKER_PROBE_TTL_S = 30.0
_docker_probe: dict[str, Any] = {"at": 0.0, "ok": False}
_docker_probe_lock = threading.Lock()


def _module_present(name: str) -> bool:
    """True when ``name`` is importable, without importing it (find_spec only)."""
    try:
        import importlib.util

        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _probe_docker() -> bool:
    """``docker info`` answers within HEALTH_TIMEOUT_S; cached for DOCKER_PROBE_TTL_S so /health stays cheap."""
    import subprocess

    with _docker_probe_lock:
        now = time.monotonic()
        if now - _docker_probe["at"] < DOCKER_PROBE_TTL_S:
            return bool(_docker_probe["ok"])
        ok = False
        exe = shutil.which("docker")
        if exe:
            try:
                proc = subprocess.run([exe, "info"], capture_output=True, timeout=HEALTH_TIMEOUT_S, stdin=subprocess.DEVNULL)
                ok = proc.returncode == 0
            except Exception:
                ok = False
        _docker_probe.update(at=now, ok=ok)
        return ok


def _count_mcp_servers(settings: EngineSettings) -> int:
    try:
        from .mcp_router import load_config

        return len(load_config(settings.mcp_servers_path))
    except Exception:
        return 0


def phase2_health(settings: EngineSettings) -> dict[str, Any]:
    """Keys merged into /health: mcp_servers, docker, e2b, crewai, browser_use, open_interpreter."""
    return {
        "mcp_servers": _count_mcp_servers(settings),
        "docker": _probe_docker(),
        "e2b": bool(settings.e2b_api_key) and _module_present("e2b"),
        "crewai": _module_present("crewai"),
        "browser_use": _module_present("browser_use"),
        "open_interpreter": _module_present("interpreter"),
    }


class ScriptRunRequest(BaseModel):
    """Body of ``POST /script/run``; the same fields as ``ScriptPayload`` with defaults for the two
    the bridge usually omits. Converted to a strict ``ScriptPayload`` before anything runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1)
    target_directory: str = Field(default=".", min_length=1, max_length=1024)
    timeout_seconds: int = Field(default=DEFAULT_SCRIPT_TIMEOUT_S, ge=1)


class SwarmRequest(BaseModel):
    """Body of ``POST /swarm/run``. ``label`` mirrors ProvisionRequest so ``_Job`` is reused as is."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1, max_length=4000)
    cwd: str | None = Field(default=None, min_length=1, max_length=1024)

    @property
    def label(self) -> str:
        return self.objective[:200]


class WebRequest(BaseModel):
    """Body of ``POST /web/run``. ``actions`` (DomAction dicts) forces the scripted Playwright DOM
    runner; otherwise ``task`` goes to browser-use, or, when it is absent, to a goto+read of ``url``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str = Field(default="", max_length=4000)
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    actions: list[dict[str, Any]] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)

    @property
    def label(self) -> str:
        return (self.task or self.url or "web")[:200]


class _SwarmProvision:
    """Placeholder request for a ``/provision`` body with ``swarm: true`` and ``steps: []``: the real
    ``ProvisionRequest`` is compiled by the crew inside the job thread."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.label = str(body.get("label") or "")[:200]
        self.steps: tuple = ()


def _swarm_result_dump(result: Any) -> dict[str, Any]:
    out = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    req = out.get("request")
    if req is not None and not isinstance(req, dict) and hasattr(req, "model_dump"):
        out["request"] = req.model_dump()
    return out


def _swarm_failed_phase(roles: list[dict[str, Any]]) -> str:
    """The phase of the first failed role (context|plan|verify), or 'report' when all ran ok."""
    for r in roles:
        if not r.get("ok", False):
            return SWARM_ROLE_PHASE.get(str(r.get("role")), "report")
    return "report"


def _coerce_state(job: _Job, result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    if not isinstance(result, dict):
        raise TypeError(f"run_job returned {type(result).__name__}, expected JobState dict")
    state = {**job.state, **result}
    state["id"] = job.id
    state.setdefault("label", job.request.label)
    state["done"] = True
    state["ok"] = bool(state.get("ok", False))
    if state.get("phase") not in getattr(job, "phases", PHASES):
        state["phase"] = "report"
    return state


class CURequest(BaseModel):
    """Body of ``POST /computer/run``. ``label`` mirrors ProvisionRequest so ``_Job`` is reused as is."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: str = Field(min_length=1, max_length=2000)
    app: str = Field(default="", max_length=200)
    candidates: int = Field(default=1, ge=1, le=CU_MAX_CANDIDATES)
    display: str | None = Field(default=None, max_length=32)
    procedure_id: str | None = Field(default=None, max_length=512)

    @property
    def label(self) -> str:
        return self.goal[:200]


def _procedure_row(row: Any) -> dict[str, Any]:
    if hasattr(row, "model_dump"):
        return row.model_dump()
    if isinstance(row, dict):
        return dict(row)
    return {k: getattr(row, k) for k in ("procedure_id", "id", "app", "goal", "version", "parent", "healed_step", "reason", "path", "created") if hasattr(row, k)}


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("procedure_id") or row.get("id") or row.get("path") or "")


def _node_dict(node: Any) -> dict[str, Any]:
    if hasattr(node, "model_dump"):
        return node.model_dump()
    get = (lambda k: node.get(k)) if isinstance(node, dict) else (lambda k: getattr(node, k, None))
    out = {k: get(k) for k in ("id", "role", "name", "text", "rect", "states")}
    rect = out.get("rect")
    if rect is not None and not isinstance(rect, (dict, list, tuple)):
        out["rect"] = rect.model_dump() if hasattr(rect, "model_dump") else {k: getattr(rect, k, None) for k in ("x", "y", "w", "h")}
    out["states"] = list(out.get("states") or [])
    out["children"] = [_node_dict(c) for c in (get("children") or [])]
    return out


def create_app(
    settings: EngineSettings,
    deps_factory: Callable[[EngineSettings], Any] | None = None,
) -> FastAPI:
    log = get_log(settings.data_dir, settings.log_url, settings.log_token or settings.token)
    jobs = _Jobs()
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="o-job")

    state: dict[str, Any] = {"deps": None}
    from .ambient.layer import AmbientLayer

    ambient = AmbientLayer(settings, log)  # plan J; threads/devices only after start() with O_AMBIENT=1

    def _stop_cu_displays() -> None:
        d = state["deps"]
        stop = getattr(d, "stop_cu_displays", None) if d is not None else None
        if callable(stop):
            try:
                stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must finish
                log.record("note", "computer-use display shutdown failed", error=f"{type(exc).__name__}: {exc}")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.sandbox_root_path.mkdir(parents=True, exist_ok=True)
        log.record("note", "engine started", version=__version__, permission_mode=settings.permission_mode)
        try:
            ambient.start()
        except Exception as exc:  # noqa: BLE001 - the ambient layer never stops the engine
            log.record("note", "ambient layer failed to start", error=f"{type(exc).__name__}: {exc}")
        yield
        pool.shutdown(wait=False, cancel_futures=True)
        _stop_cu_displays()
        ambient.stop()
        log.record("note", "engine stopped")

    app = FastAPI(title="O engine", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    deps_lock = threading.Lock()

    def deps() -> Any:
        with deps_lock:  # a job thread and a route may race to build deps; there must be exactly one
            if state["deps"] is None:
                state["deps"] = (deps_factory or EngineDeps)(settings)
            return state["deps"]

    def require_token(request: Request) -> None:
        presented = request.headers.get("x-o-token", "")
        if not settings.token or not hmac.compare_digest(presented.encode(), settings.token.encode()):
            raise HTTPException(status_code=401, detail="invalid or missing x-o-token")

    guarded = [Depends(require_token)]

    @app.exception_handler(SchemaViolation)
    def _schema_violation(_: Request, exc: SchemaViolation) -> JSONResponse:
        log.record("policy", "schema violation rejected", error=str(exc))
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    def _graph_for_presence(d: Any, job: _Job) -> tuple[Any, bool]:
        """Plan J.3: while USER_ABSENT a queued provision/script job runs headless in an isolated
        sandbox (same graph topology). PRESENT/UNKNOWN keeps the existing selection unchanged."""
        absent_graph = getattr(d, "absent_graph", None)
        if not ambient.absent or not callable(absent_graph):
            return d.graph, False
        try:
            graph = absent_graph(ambient.isolated)
        except Exception as exc:  # noqa: BLE001 - fall back to the normal path, logged
            log.record("note", f"job {job.id}: headless graph unavailable; running normally", job_id=job.id,
                       error=f"{type(exc).__name__}: {exc}")
            return d.graph, False
        job.state["headless"] = True
        log.record("note", f"job {job.id}: user absent; running headless in an isolated sandbox", job_id=job.id,
                   presence=ambient.presence.state)
        return graph, True

    def _run(job: _Job) -> None:
        sandbox_dir = settings.sandbox_root_path / job.id
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        log.record("graph", f"job {job.id} started", job_id=job.id, label=job.request.label, sandbox=str(sandbox_dir))
        started = time.monotonic()
        d = deps()

        def progress(state: dict[str, Any]) -> None:
            jobs.set_state(job.id, {**job.state, **state, "id": job.id, "done": False})

        graph, headless = _graph_for_presence(d, job)
        try:
            import inspect as _inspect
            kwargs = {"progress": progress} if "progress" in _inspect.signature(d.run_job).parameters else {}
            result = d.run_job(graph, job.request, job.id, settings.permission_mode, jobs.approve_waiter, **kwargs)
            final = _coerce_state(job, result)
            if isinstance(result, dict) and result.get("queue_destroyed"):
                final["queue_destroyed"] = True
                log.record("policy", f"job {job.id}: queue destroyed ({result.get('error')})", job_id=job.id, queue_destroyed=True)
        except Exception as exc:
            final = {**job.state, "phase": "report", "done": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if headless:
                ambient.isolated.release(job.id)
        if headless:
            final["headless"] = True
            final["isolation"] = ambient.isolated.last_backend or "local"
        jobs.set_state(job.id, final)
        learn_outcome(getattr(d, "learning", None), job.request.label, job.id, final, elapsed_s=time.monotonic() - started)
        log.record(
            "result",
            f"job {job.id} {'ok' if final['ok'] else 'failed'}",
            job_id=job.id,
            ok=final["ok"],
            phase=final.get("phase"),
            attempt=final.get("attempt"),
            cached=final.get("cached"),
            error=final.get("error"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        ambient.record_job(job.id, job.request.label, final, "provision")

    @app.get("/health", dependencies=guarded)
    def health() -> dict[str, Any]:
        # deps are read only if already built: /health never constructs them (and never starts a display)
        return health_report(settings, state["deps"])

    @app.post("/provision", dependencies=guarded)
    def provision(body: dict[str, Any]) -> dict[str, str]:
        if isinstance(body, dict) and body.get("swarm"):
            return _provision_swarm(body)  # Phase 2: defined in the "Phase 2 routes" block below
        request = parse_request(body)
        job = _Job(uuid.uuid4().hex, request, settings.max_attempts)
        jobs.add(job)
        log.record("task", f"provision queued: {request.label}", job_id=job.id, steps=len(request.steps), mode=settings.permission_mode)
        pool.submit(_run, job)
        return {"job_id": job.id}

    @app.get("/jobs/{job_id}", dependencies=guarded)
    def get_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return job.state

    @app.post("/jobs/{job_id}/approve", dependencies=guarded)
    def approve(job_id: str, body: dict[str, Any]) -> dict[str, bool]:
        allow = bool(body.get("allow", False))
        if jobs.get(job_id) is None:
            raise HTTPException(status_code=404, detail="unknown job")
        applies = settings.permission_mode == "ask"
        if applies:
            jobs.resolve(job_id, allow)
        log.record("policy", f"job {job_id} {'approved' if allow else 'denied'}", job_id=job_id, allow=allow, applied=applies)
        return {"ok": applies}

    @app.post("/cache/lookup", dependencies=guarded)
    def cache_lookup(body: dict[str, Any]) -> dict[str, Any]:
        request = parse_request(body.get("request"))
        fp = request.fingerprint(sys.platform)
        entry = deps().cache.lookup(fp)
        out: dict[str, Any] = {"hit": entry is not None and entry is not False, "fingerprint": fp}
        if out["hit"]:
            path = getattr(entry, "script_path", None) or (entry.get("script_path") if isinstance(entry, dict) else entry)
            out["script_path"] = str(path)
        log.record("cache", f"cache {'hit' if out['hit'] else 'miss'} for {request.label}", fingerprint=fp)
        return out

    @app.get("/cache", dependencies=guarded)
    def cache_list() -> list[dict[str, Any]]:
        rows = deps().cache.index()
        return [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in rows]

    @app.post("/diagnose", dependencies=guarded)
    def diagnose_route(body: dict[str, Any]) -> dict[str, Any]:
        from .diagnose import diagnose

        d = deps()
        telemetry = getattr(d.context, "recent_telemetry", None)
        result = diagnose(body, telemetry, d.parser)
        log.record("remediate", f"diagnosed: {result.category}", command=result.command, missing_package=result.missing_package)
        return result.model_dump()

    @app.post("/parse", dependencies=guarded)
    def parse(body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text") or "")
        if not text.strip():
            raise SchemaViolation("text is empty")
        stripped = text.strip()
        if stripped.startswith(("[", "```", "~~~")):
            request = parse_steps(stripped, label="parsed")
        else:
            request = deps().parser.parse(text, goal=stripped.splitlines()[0][:200])
        log.record("note", "parsed steps", count=len(request.steps))
        return {"steps": [s.model_dump() for s in request.steps]}

    @app.post("/verify", dependencies=guarded)
    def verify_route(body: dict[str, Any]) -> dict[str, Any]:
        try:
            target = VerifyTarget.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        result = deps().verify(target)
        log.record("verify", f"verify :{target.port}{target.path} -> {result.get('status')}", **result)
        return result

    @app.get("/context", dependencies=guarded)
    def context_route(cwd: str | None = None) -> dict[str, Any]:
        profile = deps().context.profile(cwd or os.getcwd())
        return profile.model_dump() if hasattr(profile, "model_dump") else dict(profile)

    # ------------------------------------------------------------ computer use

    def _run_cu(job: _Job) -> None:
        req: CURequest = job.request
        log.record("graph", f"computer job {job.id} started", job_id=job.id, label=req.label, app=req.app,
                   candidates=req.candidates, display=req.display)
        started = time.monotonic()
        d = deps()

        def progress(state: dict[str, Any]) -> None:
            jobs.set_state(job.id, {**job.state, **state, "id": job.id, "done": False})

        try:
            result = d.run_cu_job(d.cu_graph, req.goal, req.app, job.id, req.candidates, progress=progress,
                                  procedure_id=req.procedure_id)
            final = _coerce_state(job, result)
        except Exception as exc:
            final = {**job.state, "phase": "report", "done": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        final["infeasible"] = bool(final.get("infeasible", False))
        jobs.set_state(job.id, final)
        learn_outcome(getattr(d, "learning", None), req.goal, job.id, final, req.app, time.monotonic() - started)
        log.record(
            "result",
            f"computer job {job.id} {'infeasible' if final['infeasible'] else 'ok' if final['ok'] else 'failed'}",
            job_id=job.id,
            ok=final["ok"],
            infeasible=final["infeasible"],
            phase=final.get("phase"),
            attempt=final.get("attempt"),
            cached=final.get("cached"),
            procedure_id=final.get("procedure_id"),
            error=final.get("error"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        ambient.record_job(job.id, req.goal, final, "computer")

    def _queue_cu(req: CURequest, kind: str) -> dict[str, str]:
        job = _Job(uuid.uuid4().hex, req, settings.cu_max_heals, phases=CU_PHASES)
        job.state["candidates"] = req.candidates
        job.state["procedure_id"] = req.procedure_id
        job.state["infeasible"] = False
        jobs.add(job)
        log.record("task", f"computer {kind} queued: {req.label}", job_id=job.id, app=req.app,
                   candidates=req.candidates, procedure_id=req.procedure_id)
        pool.submit(_run_cu, job)
        return {"job_id": job.id}

    def _parse_cu(body: dict[str, Any]) -> CURequest:
        try:
            return CURequest.model_validate(body or {})
        except Exception as exc:
            raise SchemaViolation(str(exc)) from None

    @app.post("/computer/run", dependencies=guarded)
    def computer_run(body: dict[str, Any]) -> dict[str, str]:
        req = _parse_cu(body)
        if req.candidates > 1 and not sys.platform.startswith("linux"):
            raise SchemaViolation(CU_CANDIDATES_NEED_LINUX)
        return _queue_cu(req, "run")

    @app.get("/computer/procedures", dependencies=guarded)
    def computer_procedures() -> list[dict[str, Any]]:
        return [_procedure_row(r) for r in deps().cu_procedures.list()]

    @app.post("/computer/replay", dependencies=guarded)
    def computer_replay(body: dict[str, Any]) -> dict[str, str]:
        pid = str((body or {}).get("procedure_id") or "")
        if not pid:
            raise SchemaViolation("procedure_id is required")
        store = deps().cu_procedures
        get = getattr(store, "get", None)
        found = get(pid) if callable(get) else None
        match = _procedure_row(found) if found is not None else None
        if match is None:
            rows = [_procedure_row(r) for r in store.list()]
            match = next((r for r in rows if _row_id(r) == pid), None)
        if match is None:
            raise HTTPException(status_code=404, detail="unknown procedure")
        req = CURequest(goal=str(match.get("goal") or ""), app=str(match.get("app") or ""), candidates=1, procedure_id=pid)
        return _queue_cu(req, "replay")

    @app.get("/computer/interfaces", dependencies=guarded)
    def computer_interfaces() -> dict[str, Any]:
        return {"apps": interface_rows(deps().cu_interfaces)}

    @app.get("/computer/tree", dependencies=guarded)
    def computer_tree(window: str | None = None) -> dict[str, Any]:
        backend = deps().cu_backend(0)
        nodes = backend.tree(window) if window else backend.tree()
        return {"window": window, "nodes": [_node_dict(n) for n in (nodes or [])]}

    @app.post("/computer/find", dependencies=guarded)
    def computer_find(body: dict[str, Any]) -> dict[str, Any]:
        from .cu.schema import Target

        try:
            target = Target.model_validate((body or {}).get("target") or {})
        except Exception as exc:
            raise SchemaViolation(str(exc)) from None
        node = deps().cu_backend(0).find(target)
        return {"found": node is not None, "node": _node_dict(node) if node is not None else None}

    # ------------------------------------------------------------ Phase 2 routes
    # /tools/list, /tools/call, /script/run, /swarm/run, /web/run and the `swarm: true` rule of
    # /provision. Swarm and web jobs always run on the thread pool (web.py uses asyncio.run, which
    # cannot run inside the server's event loop); /tools/* and /script/run answer inline.

    def _validate(model: Any, body: Any) -> Any:
        try:
            return model.model_validate(body or {})
        except Exception as exc:
            raise SchemaViolation(str(exc)) from None

    @app.get("/tools/list", dependencies=guarded)
    def tools_list(refresh: bool = False) -> dict[str, Any]:
        tools = deps().mcp_router.list_tools(refresh=bool(refresh))
        log.record("note", f"mcp tools listed: {len(tools)}", count=len(tools), refresh=bool(refresh))
        return {"tools": [dict(t) for t in tools]}

    @app.post("/tools/call", dependencies=guarded)
    def tools_call(body: dict[str, Any]) -> dict[str, Any]:
        from .mcp_router import ToolCall

        call = _validate(ToolCall, body)
        # The router validates args against the tool's JSON schema before dispatching and reports
        # problems as is_error=True in content, so this is always a 200 with the ToolResult shape.
        result = deps().mcp_router.call(call)
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)

    @app.post("/script/run", dependencies=guarded)
    def script_run(body: dict[str, Any]) -> dict[str, Any]:
        from .executor import Sandbox, SandboxEscape
        from .graph import step_record
        from .schema import ScriptPayload

        req = _validate(ScriptRunRequest, body)
        payload = _validate(ScriptPayload, req.model_dump())
        script_id = f"{SCRIPT_SANDBOX_PREFIX}{uuid.uuid4().hex}"
        sandbox_dir = settings.sandbox_root_path / script_id
        headless = ambient.absent
        sandbox = ambient.isolated.for_job(script_id) if headless else Sandbox(str(sandbox_dir))
        log.record("task", f"script run queued: {payload.language} ({len(payload.code)} chars)",
                   language=payload.language, chars=len(payload.code), sandbox=str(sandbox_dir),
                   target_directory=payload.target_directory, timeout_seconds=payload.timeout_seconds)
        try:
            result = deps().runner.run(payload, sandbox, 0, None, False)
        except SandboxEscape as exc:
            log.record("policy", "script target_directory rejected", error=str(exc), sandbox=str(sandbox_dir))
            raise SchemaViolation(str(exc)) from None
        finally:
            if headless:
                ambient.isolated.release(script_id)
        record = step_record(result, 0)
        record["sandbox"] = str(sandbox_dir)
        if headless:
            record["headless"] = True
            record["isolation"] = getattr(sandbox, "backend_name", "local")
        log.record("result", f"script run exit={record['exit_code']} ({payload.language})",
                   language=payload.language, exit_code=record["exit_code"], elapsed_ms=record["elapsed_ms"],
                   timed_out=record["timed_out"], sandbox=str(sandbox_dir))
        return record

    def _run_swarm(job: _Job) -> None:
        req: SwarmRequest = job.request
        cwd = req.cwd or os.getcwd()
        log.record("graph", f"swarm job {job.id} started", job_id=job.id, label=req.label, cwd=cwd)
        started = time.monotonic()
        # The crew runs its three roles sequentially and only returns when all have run; the phase
        # the state shows afterwards is "report", or the phase of the first role that failed
        # (context|plan|verify = telemetry_auditor|subprocess_compiler|environment_inspector).
        try:
            result = deps().crew.run(req.objective, cwd)
            dumped = _swarm_result_dump(result)
            roles = [dict(r) for r in dumped.get("roles") or []]
            compiled = dumped.get("request") is not None
            final = {
                **job.state,
                "phase": "report" if compiled else _swarm_failed_phase(roles),
                "done": True,
                "ok": compiled,
                "steps": [s for s in (dumped.get("request") or {}).get("steps", [])] if compiled else [],
                "result": dumped,
                "error": None if compiled else SWARM_NO_PLAN,
            }
        except Exception as exc:
            final = {**job.state, "phase": "report", "done": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        jobs.set_state(job.id, final)
        log.record("result", f"swarm job {job.id} {'ok' if final['ok'] else 'failed'}", job_id=job.id, ok=final["ok"],
                   phase=final.get("phase"), error=final.get("error"),
                   roles=[(r.get("role"), r.get("ok")) for r in (final.get("result") or {}).get("roles", [])],
                   elapsed_ms=int((time.monotonic() - started) * 1000))
        ambient.record_job(job.id, req.label, final, "swarm")

    @app.post("/swarm/run", dependencies=guarded)
    def swarm_run(body: dict[str, Any]) -> dict[str, str]:
        req = _validate(SwarmRequest, body)
        job = _Job(uuid.uuid4().hex, req, 1, phases=SWARM_PHASES)
        job.state["result"] = None
        jobs.add(job)
        log.record("task", f"swarm queued: {req.label}", job_id=job.id, cwd=req.cwd)
        pool.submit(_run_swarm, job)
        return {"job_id": job.id}

    def _run_swarm_provision(job: _Job) -> None:
        """``/provision`` with ``swarm: true``: the crew audits the workspace and compiles a plan
        before the graph runs. Rule: the crew's compiled steps REPLACE the caller's steps only when
        the caller passed ``steps: []``; when the caller supplied steps they are kept verbatim and
        the crew's profile/plan are attached under ``state["swarm"]`` for the caller to inspect.
        ``verify`` from the crew's request is used only when the caller gave none."""
        placeholder: _SwarmProvision = job.request
        body = dict(placeholder.body)
        provided = list(body.get("steps") or [])
        cwd = str(body.pop("cwd", None) or (provided[0].get("target_directory") if provided and isinstance(provided[0], dict) else None) or os.getcwd())
        body.pop("swarm", None)
        label = str(body.get("label") or "")
        log.record("graph", f"job {job.id} swarm context", job_id=job.id, label=label, cwd=cwd, provided_steps=len(provided))
        try:
            result = deps().crew.run(label, cwd)
            dumped = _swarm_result_dump(result)
        except Exception as exc:
            dumped = {"profile": {}, "request": None, "inspection": {}, "roles": [], "error": f"{type(exc).__name__}: {exc}"}
        compiled = dumped.get("request")
        job.state["swarm"] = {k: dumped.get(k) for k in ("profile", "inspection", "roles", "error") if k in dumped}
        job.state["swarm"]["compiled_steps"] = len((compiled or {}).get("steps", []))
        if not provided:
            if not compiled:
                final = {**job.state, "phase": "context", "done": True, "ok": False, "error": SWARM_NO_PLAN}
                jobs.set_state(job.id, final)
                log.record("result", f"job {job.id} failed", job_id=job.id, ok=False, phase="context", error=SWARM_NO_PLAN)
                return
            body["steps"] = list(compiled.get("steps", []))
            if body.get("verify") is None and compiled.get("verify") is not None:
                body["verify"] = compiled["verify"]
        try:
            request = parse_request(body)
        except SchemaViolation as exc:
            final = {**job.state, "phase": "validate", "done": True, "ok": False, "error": str(exc)}
            jobs.set_state(job.id, final)
            log.record("policy", "schema violation rejected", job_id=job.id, error=str(exc))
            return
        job.request = request
        job.state["label"] = request.label
        log.record("task", f"provision (swarm) compiled: {request.label}", job_id=job.id, steps=len(request.steps),
                   replaced=not provided, mode=settings.permission_mode)
        _run(job)

    def _provision_swarm(body: dict[str, Any]) -> dict[str, str]:
        label = body.get("label")
        if not isinstance(label, str) or not label.strip():
            raise SchemaViolation("label is required")
        steps = body.get("steps")
        if steps is None:
            raise SchemaViolation("steps is required (pass [] to let the swarm compile the plan)")
        if steps:
            # caller-supplied steps must already be valid: the crew never changes them
            parse_request({k: v for k, v in body.items() if k not in ("swarm", "cwd")})
        job = _Job(uuid.uuid4().hex, _SwarmProvision(body), settings.max_attempts)
        jobs.add(job)
        log.record("task", f"provision (swarm) queued: {label[:200]}", job_id=job.id, steps=len(steps), mode=settings.permission_mode)
        pool.submit(_run_swarm_provision, job)
        return {"job_id": job.id}

    def _run_web(job: _Job) -> None:
        from .web import DOM_TIMEOUT_S, WebResult

        req: WebRequest = job.request
        log.record("graph", f"web job {job.id} started", job_id=job.id, label=req.label, url=req.url,
                   scripted=req.actions is not None)
        started = time.monotonic()
        d = deps()
        try:
            jobs.set_state(job.id, {**job.state, "phase": "run"})
            if req.actions is not None:
                runner = d.web_runner(scripted=True)
                result = runner.run(req.actions, req.timeout_seconds or DOM_TIMEOUT_S)
            else:
                runner = d.web_runner()
                if getattr(runner, "via", "") == "browser-use":
                    result = runner.run(req.task, req.url)
                elif req.url:
                    # browser-use absent: the DOM runner can still open the page and read it
                    result = runner.run([{"op": "goto", "url": req.url}, {"op": "read"}], req.timeout_seconds or DOM_TIMEOUT_S)
                else:
                    result = WebResult(ok=False, via=getattr(runner, "via", "playwright-dom"),
                                       error="browser_use is not installed; pass url or actions for the DOM runner")
            dumped = result.model_dump() if hasattr(result, "model_dump") else dict(result)
            final = {**job.state, "phase": "report", "done": True, "ok": bool(dumped.get("ok")),
                     "result": dumped, "error": dumped.get("error")}
        except Exception as exc:
            final = {**job.state, "phase": "report", "done": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        jobs.set_state(job.id, final)
        log.record("result", f"web job {job.id} {'ok' if final['ok'] else 'failed'}", job_id=job.id, ok=final["ok"],
                   via=(final.get("result") or {}).get("via"), error=final.get("error"),
                   elapsed_ms=int((time.monotonic() - started) * 1000))
        ambient.record_job(job.id, req.label, final, "web")

    @app.post("/web/run", dependencies=guarded)
    def web_run(body: dict[str, Any]) -> dict[str, str]:
        from .web import DomAction

        req = _validate(WebRequest, body)
        if req.actions is not None:
            for a in req.actions:
                _validate(DomAction, a)
        elif not req.task and not req.url:
            raise SchemaViolation("one of task, url or actions is required")
        job = _Job(uuid.uuid4().hex, req, 1, phases=WEB_PHASES)
        job.state["result"] = None
        jobs.add(job)
        log.record("task", f"web queued: {req.label}", job_id=job.id, url=req.url, scripted=req.actions is not None)
        pool.submit(_run_web, job)
        return {"job_id": job.id}

    # -------------------------------------------------------- end Phase 2 routes

    # ------------------------------------------------------------ Phase 3: GenUI
    fallback_ui = UIStore(forward_url=settings.ui_url, token=settings.log_token or settings.token)

    def ui_store() -> UIStore:
        store = getattr(deps(), "ui", None)
        return store if isinstance(store, UIStore) else fallback_ui

    def _sse_event(seq: int, frame: Any, fmt: str) -> str:
        if fmt == "oui":
            payload = oui.compile([frame]).rstrip("\n")
        else:
            payload = frame_to_json(frame)
        data = "".join(f"data: {line}\n" for line in payload.split("\n"))
        return f"id: {seq}\nevent: frame\n{data}\n"

    def _ui_stream(job_id: str, fmt: str, since: int):
        store = ui_store()
        surface = f"job:{job_id}"
        q = store.subscribe(surface)
        try:
            last = since
            for seq, frame in store.entries(surface, since):
                yield _sse_event(seq, frame, fmt)
                last = seq
            if store.ended(surface) and last >= store.last_seq(surface):
                return
            idle = 0.0
            while True:
                try:
                    seq, frame = q.get(timeout=UI_SSE_POLL_S)
                except queue.Empty:
                    idle += UI_SSE_POLL_S
                    job = jobs.get(job_id)
                    if job is not None and job.state.get("done") and idle >= UI_SSE_DONE_GRACE_S:
                        return  # the job finished without an end frame (aborted before report)
                    if idle >= UI_SSE_KEEPALIVE_S:
                        idle = 0.0
                        yield ": keepalive\n\n"
                    continue
                idle = 0.0
                if seq <= last:
                    continue
                last = seq
                yield _sse_event(seq, frame, fmt)
                if getattr(frame, "type", "") == "end":
                    return
        finally:
            store.unsubscribe(surface, q)

    @app.get("/jobs/{job_id}/ui", dependencies=guarded)
    def job_ui(job_id: str, format: str = "a2ui", since: int = 0) -> StreamingResponse:
        if jobs.get(job_id) is None:
            raise HTTPException(status_code=404, detail="unknown job")
        if format not in ("a2ui", "oui"):
            raise HTTPException(status_code=422, detail="format must be a2ui or oui")
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(_ui_stream(job_id, format, max(0, int(since))), media_type="text/event-stream", headers=headers)

    @app.post("/ui/emit", dependencies=guarded)
    def ui_emit(body: dict[str, Any]) -> dict[str, int]:
        raw = (body or {}).get("frames")
        if not isinstance(raw, list) or not raw:
            raise SchemaViolation("frames must be a non-empty list")
        frames = []
        for i, item in enumerate(raw):
            try:
                frame = parse_frame(item)
            except SchemaViolation as exc:
                raise SchemaViolation(f"frames[{i}]: {exc}") from None
            if not valid_surface(frame.surface):
                raise SchemaViolation(f"frames[{i}]: surface must start with chat: or job:")
            frames.append(frame)
        store = ui_store()
        for frame in frames:
            store.append(frame)
        log.record("note", f"ui frames accepted: {len(frames)}", count=len(frames),
                   surfaces=sorted({f.surface for f in frames}))
        return {"accepted": len(frames)}

    app.state.ui_store = ui_store  # callable: the UIStore the graphs emit into (deps.ui, else the fallback)
    # ------------------------------------------------------------ end Phase 3

    # ------------------------------------------------------------ learning
    fallback_learning: dict[str, Any] = {}

    def learning_store() -> Any:
        if not settings.learning:
            raise HTTPException(status_code=503, detail="learning is disabled (O_LEARNING=0)")
        store = getattr(deps(), "learning", None)
        if store is not None:
            return store
        with deps_lock:
            if "store" not in fallback_learning:
                fallback_learning["store"] = build_learning(settings)
            return fallback_learning["store"]

    def _short(text: str) -> str:
        from .learning import LOG_TEXT, scrub

        t = scrub(" ".join(str(text or "").split()))
        return t if len(t) <= LOG_TEXT else t[: LOG_TEXT - 1] + "…"

    @app.get("/learn/profile", dependencies=guarded)
    def learn_profile(topic: str | None = None, app_name: str | None = Query(default=None, alias="app")) -> dict[str, Any]:
        return learning_store().profile(topic=topic, app=app_name)

    @app.post("/learn/observe", dependencies=guarded)
    def learn_observe(body: dict[str, Any]) -> dict[str, Any]:
        req = _validate(LearnObserveRequest, body)
        store = learning_store()
        try:
            obs = store.observe(req.source, req.text, task_id=req.task_id, outcome=req.outcome, app=req.app,
                                duration_s=req.duration_s, ts=req.ts)
        except ValueError as exc:
            raise SchemaViolation(str(exc)) from None
        store.merge_async()
        log.record("note", f"learning: observed {obs.source}: {_short(obs.text)}", id=obs.id, source=obs.source,
                   app=obs.app, outcome=obs.outcome, items=len(store.items()))
        return {"ok": True, "id": obs.id}

    @app.post("/learn/feedback", dependencies=guarded)
    def learn_feedback(body: dict[str, Any]) -> dict[str, Any]:
        req = _validate(LearnFeedbackRequest, body)
        if bool(req.forget) == bool(req.confirm):
            raise SchemaViolation("exactly one of forget or confirm is required")
        store = learning_store()
        item_id = str(req.forget or req.confirm)
        item = store.item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown profile item")
        obs = store.forget(item_id) if req.forget else store.confirm(item_id)
        after = store.item(item_id)
        verb = "forget" if req.forget else "confirm"
        log.record("note", f"learning: {verb} {item['kind']}: {_short(item['text'])}", id=obs.id, item_id=item_id,
                   verb=verb, item_kind=item["kind"])
        return {"ok": True, "id": obs.id, "item": after}
    # ------------------------------------------------------------ end learning

    # ------------------------------------------------------------ goals (long-term agenda)
    fallback_goals: dict[str, Any] = {}

    def goal_store() -> Any:
        store = getattr(deps(), "goals", None)
        if store is not None:
            return store
        with deps_lock:
            if "store" not in fallback_goals:
                fallback_goals["store"] = build_goals(settings)
            return fallback_goals["store"]

    def _profile() -> dict[str, Any]:
        if not settings.learning:
            return {}
        try:
            return learning_store().profile()
        except Exception:
            return {}

    def _goal_or_404(gid: str) -> dict[str, Any]:
        g = goal_store().get(gid)
        if g is None:
            raise HTTPException(status_code=404, detail="unknown goal")
        return g

    @app.get("/goals", dependencies=guarded)
    def goals_list(archived: bool = True) -> dict[str, Any]:
        return {"goals": goal_store().list(include_archived=bool(archived))}

    @app.post("/goals", dependencies=guarded)
    def goals_create(body: dict[str, Any]) -> dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        store = goal_store()
        try:
            if isinstance(body.get("proposal"), dict):
                g = store.create_from_proposal(body["proposal"])
            else:
                text = body.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                    raise SchemaViolation("text must be a non-empty string of at most 2000 characters (or pass a proposal)")
                g = store.create_from_text(text)
        except ValueError as exc:
            raise SchemaViolation(str(exc)) from None
        log.record("note", f"goals: created {g['id']}: {_short(g['title'])}", goal_id=g["id"], origin=g["origin"],
                   autonomy=g["autonomy"], cadence=g["cadence"], horizon_days=g["horizon_days"])
        return {"ok": True, "goal": g}

    @app.post("/goals/propose", dependencies=guarded)
    def goals_propose(body: dict[str, Any] | None = None) -> dict[str, Any]:
        props = goal_store().propose(_profile())
        log.record("note", f"goals: proposed {len(props)}", count=len(props))
        return {"proposals": props}

    @app.get("/goals/{gid}", dependencies=guarded)
    def goals_detail(gid: str) -> dict[str, Any]:
        from .goals import next_cycle_prompt, profile_lines

        det = goal_store().detail(gid)
        if det is None:
            raise HTTPException(status_code=404, detail="unknown goal")
        det["next_cycle_prompt"] = next_cycle_prompt(det["goal"], det["cycles"], profile_text=profile_lines(_profile()))
        return det

    @app.post("/goals/{gid}/cycle", dependencies=guarded)
    def goals_cycle(gid: str, body: dict[str, Any]) -> dict[str, Any]:
        _goal_or_404(gid)
        try:
            cyc = goal_store().record_cycle(gid, body)
        except ValueError as exc:
            msg = str(exc)
            if msg.startswith("goal is "):
                raise HTTPException(status_code=409, detail=msg) from None
            raise SchemaViolation(msg) from None
        except Exception as exc:  # pydantic ValidationError and friends
            raise SchemaViolation(str(exc)) from None
        log.record("note", f"goals: cycle {gid}: {_short(cyc.get('reflection') or '(no reflection)')}", goal_id=gid,
                   metrics={k: v.get("value") for k, v in (cyc.get("metrics") or {}).items()}, flags=list(cyc.get("flags") or []))
        return {"ok": True, "cycle": cyc}

    def _goal_status(gid: str, status: str) -> dict[str, Any]:
        _goal_or_404(gid)
        try:
            g = goal_store().set_status(gid, status)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        log.record("note", f"goals: {status} {gid}", goal_id=gid, status=status)
        return {"ok": True, "goal": g}

    @app.post("/goals/{gid}/pause", dependencies=guarded)
    def goals_pause(gid: str) -> dict[str, Any]:
        return _goal_status(gid, "paused")

    @app.post("/goals/{gid}/resume", dependencies=guarded)
    def goals_resume(gid: str) -> dict[str, Any]:
        return _goal_status(gid, "active")

    @app.post("/goals/{gid}/archive", dependencies=guarded)
    def goals_archive(gid: str) -> dict[str, Any]:
        return _goal_status(gid, "archived")

    @app.post("/goals/{gid}/autonomy", dependencies=guarded)
    def goals_autonomy(gid: str, body: dict[str, Any]) -> dict[str, Any]:
        _goal_or_404(gid)
        body = body if isinstance(body, dict) else {}
        allowed = body.get("allowed") or []
        if not isinstance(allowed, list):
            raise SchemaViolation("allowed must be a list of action kinds")
        try:
            g = goal_store().set_autonomy(gid, str(body.get("autonomy") or ""), allowed)
        except ValueError as exc:
            raise SchemaViolation(str(exc)) from None
        log.record("policy", f"goals: autonomy {gid} -> {g['autonomy']}", goal_id=gid, autonomy=g["autonomy"], allowed=g["allowed"])
        return {"ok": True, "goal": g}
    # ------------------------------------------------------------ end goals

    # ------------------------------------------------------------ ambient (plan J)
    from .ambient.milestones import MilestoneIn

    @app.get("/presence", dependencies=guarded)
    def presence_route() -> dict[str, Any]:
        return ambient.presence_view()

    @app.post("/presence/simulate", dependencies=guarded)
    def presence_simulate(body: dict[str, Any]) -> dict[str, Any]:
        req = _validate(PresenceSimulateRequest, body)
        log.record("policy", f"presence simulated: {req.state}", state=req.state, reason=req.reason or "")
        ambient.presence.simulate(req.state, req.reason or "")
        return {"ok": True, "presence": ambient.presence_view()}

    @app.get("/sensors", dependencies=guarded)
    def sensors_route() -> dict[str, Any]:
        return ambient.sensors_view()

    @app.get("/sensors/live", dependencies=guarded)
    def sensors_live() -> dict[str, Any]:
        # The opted-in user's own pulse/breathing, from memory. Deliberately never logged.
        return ambient.heart_live()

    @app.get("/milestones", dependencies=guarded)
    def milestones_list(since: float = 0.0, limit: int = Query(default=200, ge=1, le=5000)) -> dict[str, Any]:
        return {"milestones": ambient.milestones.since(since, limit)}

    @app.post("/milestones", dependencies=guarded)
    def milestones_create(body: dict[str, Any]) -> dict[str, Any]:
        req = _validate(MilestoneIn, body)
        row = ambient.milestones.record(req.kind, req.title, task_id=req.task_id, detail=req.detail,
                                        presence=ambient.presence.state, ts=req.ts)
        log.record("note", f"milestone {row['kind']}: {_short(row['title'])}", id=row["id"], milestone_kind=row["kind"],
                   task_id=row["task_id"], source="app")
        return {"ok": True, "milestone": row}

    @app.get("/brief/latest", dependencies=guarded)
    def brief_latest() -> dict[str, Any]:
        return {"brief": ambient.briefer.get_latest()}

    app.state.ambient = ambient
    # ------------------------------------------------------------ end ambient

    app.state.settings = settings
    app.state.log = log
    app.state.jobs = jobs
    return app
