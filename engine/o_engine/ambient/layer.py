"""``AmbientLayer``: the bus, presence machine, sensors, milestones, brief, TTS, supervisor and the
headless-isolation selector, built once per engine app. Nothing starts a thread or opens a device
until ``start()``, and ``start()`` does nothing unless ``O_AMBIENT=1``. The presence machine,
milestones and brief exist either way, so ``/presence/simulate`` and ``/milestones`` always work."""
from __future__ import annotations

import shlex
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import ABSENT
from .brief import Briefer
from .bus import make_bus
from .isolation import IsolatedSandboxes
from .milestones import MilestoneStore, kind_for_job
from .pause import PauseGate, pop_priority
from .presence import PresenceMachine
from .sensors.csi import VITALS_NOTE, CSISensor
from .sensors.rppg import RPPGSensor
from .sensors.sonar import SonarSensor
from .supervisor import Child, Supervisor
from .tts import tts_available

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
SENSOR_NAMES = {"csi": "wifi-csi", "sonar": "sonar", "camera": "camera-face"}


class _LazyLocalModel:
    """Builds the local-only ModelClient on first use (no OpenAI client until a brief is written)."""

    def __init__(self, url: str, model: str) -> None:
        self.url, self.model = url, model
        self._client: Any = None
        self._lock = threading.Lock()

    def local_only(self) -> Any:
        with self._lock:
            if self._client is None:
                from ..cu.planner import LocalModelClient, ModelClient

                self._client = ModelClient(LocalModelClient(self.url, self.model), None).local_only()
            return self._client


class AmbientLayer:
    def __init__(self, settings: Any, log: Any, isolation_candidates: Any = None, model: Any = None) -> None:
        self.settings = settings
        self.log = log
        s = settings
        self.bus = make_bus(s.bus_url, log)
        self.milestones = MilestoneStore(Path(s.data_dir) / "ambient" / "milestones.sqlite", on_record=self._published)
        on = bool(s.ambient)
        self.sensors: dict[str, Any] = {
            "csi": CSISensor(self.bus, log, enabled=on and s.sensor_csi, udp=s.csi_udp, ruview_ws=s.ruview_ws,
                             ruview_mqtt=s.ruview_mqtt, vitals_opt_in=on and s.sensor_heart),
            "sonar": SonarSensor(self.bus, log, enabled=on and s.sensor_sonar),
            "camera": RPPGSensor(self.bus, log, enabled=on and s.sensor_camera, heart=on and s.sensor_camera and s.sensor_heart,
                                 rppg_model=s.rppg_model),
        }
        self.presence = PresenceMachine(self.bus, log, s.absent_after_s, sensors_enabled=self.live_sensors)
        # No automatic speech: the app speaks the brief itself when the user enabled it.
        self.briefer = Briefer(self.milestones, model if model is not None else _LazyLocalModel(s.vllm_url, s.vllm_model),
                               self.bus, log, speak=None)
        self.supervisor = Supervisor(log, auto_repair=s.auto_repair)
        self.pause_gate = PauseGate(log)
        self.isolated = IsolatedSandboxes(Path(s.sandbox_root), log, s.settings_like, import_roots=[s.data_dir],
                                          candidates=isolation_candidates, gate=self.pause_gate)
        self.presence.listeners.append(self.pause_gate.on_presence)  # synchronous, before the bus delivers
        self.presence.attach()
        self.briefer.attach()
        self.started = False

    # ---- lifecycle ----------------------------------------------------------------------------
    def start(self) -> bool:
        if not self.settings.ambient or self.started:
            return False
        self.started = True
        self.bus.start()
        for name, sensor in self.sensors.items():
            if not sensor.enabled:
                continue
            ok, reason = sensor.available()
            if ok:
                self.supervisor.add(Child(name=f"sensor:{name}", worker=sensor, restart="always"))
            else:
                self._note(f"ambient: {name} sensor enabled but unavailable ({reason})", sensor=name, reason=reason)
        for name, cmd in (("vllm", self.settings.vllm_cmd), ("screenpipe", self.settings.screenpipe_cmd)):
            if not cmd:
                continue
            if name == "vllm" and (urlparse(self.settings.vllm_url).hostname or "") not in LOCAL_HOSTS:
                self._note("ambient: vLLM is remote; not supervised", vllm_url=self.settings.vllm_url)
                continue
            self.supervisor.add(Child(name=name, argv=shlex.split(cmd), restart="always"))
        self.supervisor.check_dependencies()
        self.supervisor.start()
        self.presence.start()
        self._note("ambient layer started", sensors=self.live_names(), bus=self.bus.kind, absent_after_s=self.settings.absent_after_s)
        return True

    def stop(self) -> None:
        self.pause_gate.release_all()  # never leave a paused job blocked at shutdown
        if self.started:
            self.presence.stop()
            self.supervisor.stop()
            self.started = False
            self._note("ambient layer stopped")
        self.bus.stop()

    # ---- queries ------------------------------------------------------------------------------
    def live_sensors(self) -> set[str]:
        return {name for name, s in self.sensors.items() if s.enabled and s.running}

    def live_names(self) -> list[str]:
        return sorted(name for name, s in self.sensors.items() if s.enabled)

    @property
    def absent(self) -> bool:
        return self.presence.state == ABSENT

    def presence_view(self) -> dict[str, Any]:
        snap = self.presence.snapshot()
        live = sorted(SENSOR_NAMES[n] for n in self.live_sensors())
        snap["sources"] = live or ([snap["source"]] if snap["source"] in ("os_idle", "simulated") else [])
        snap["ambient"] = bool(self.settings.ambient)
        snap["isolation"] = self.isolated.last_backend
        snap["sensors_live"] = live
        snap["pause"] = self.pause_gate.snapshot()
        return snap

    def sensors_view(self) -> dict[str, Any]:
        rows = []
        for key, sensor in self.sensors.items():
            st = sensor.status()
            st["id"] = key
            st["name"] = SENSOR_NAMES[key]
            if key == "csi":
                st["source"] = sensor.source_kind()
                st["vitals_opt_in"] = sensor.vitals_opt_in
                st["vitals_note"] = VITALS_NOTE
            rows.append(st)
        cam = self.sensors["camera"]
        heart = cam.heart_status()
        cam_ok, cam_reason = cam.available()
        rows.append({
            "id": "heart", "name": "heart-rppg", "topic": None, "enabled": bool(cam.heart),
            "available": cam_ok, "reason": cam_reason or ("" if self.settings.sensor_camera else "needs the camera sensor"),
            "running": cam.running and cam.heart, "last_error": "", "via": heart["via"], "model": heart["model"],
            "description": "Pulse and a stress proxy from your camera (open-rppg when installed, else POS). "
                           + heart["note"],
        })
        tts_ok, tts_reason = tts_available()
        return {
            "ambient": bool(self.settings.ambient),
            "indicator": bool(self.live_sensors()),
            "sensors": rows,
            "bus": self.bus.describe(),
            "tts": {"enabled": bool(self.settings.brief_tts), "available": tts_ok, "reason": tts_reason,
                    "spoken_by": "app"},
            "supervisor": self.supervisor.status(),
        }

    def heart_live(self) -> dict[str, Any]:
        cam, csi = self.sensors["camera"], self.sensors["csi"]
        return {"enabled": bool(cam.heart or csi.vitals_opt_in), "pulse": cam.live(), "breathing": csi.live(),
                "note": cam.heart_status()["note"]}

    # ---- hooks --------------------------------------------------------------------------------
    def take_priority(self, job_id: str, body: Any) -> str:
        """``priority`` from a /provision or /script/run body (removed from it), registered for ``job_id``."""
        return self.pause_gate.register(job_id, pop_priority(body))

    def job_finished(self, job_id: str) -> None:
        self.pause_gate.forget(job_id)

    def record_job(self, job_id: str, label: str, final: dict[str, Any], kind: str = "provision") -> None:
        """Milestone for every finished engine job. Never raises."""
        try:
            err = final.get("error")
            detail = f"{kind}: {err}" if err else f"{kind}: phase={final.get('phase')} attempt={final.get('attempt')}"
            if final.get("headless"):
                detail += f" [headless:{final.get('isolation') or 'local'}]"
            self.milestones.record(kind_for_job(final), label or kind, task_id=job_id, detail=detail,
                                   presence=self.presence.state)
        except Exception as exc:  # noqa: BLE001
            self._note(f"milestone write failed: {type(exc).__name__}: {exc}", job_id=job_id)

    def _published(self, row: dict) -> None:
        if not self.bus.running:
            return  # nothing listens before the bus is up; do not start a thread per finished job
        try:
            self.bus.publish("agent/milestone", {"id": row["id"], "kind": row["kind"], "title": row["title"],
                                                 "task_id": row.get("task_id"), "ts": row["ts"]})
        except Exception:
            pass

    def _note(self, summary: str, **detail: Any) -> None:
        try:
            self.log.record("note", summary, **detail)
        except Exception:
            pass
