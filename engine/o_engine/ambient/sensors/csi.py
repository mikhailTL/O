"""Wi-Fi CSI room sensing: room-level motion/presence only.

Sources, in order of preference:
1. RuView (``ruvnet/RuView``, MIT): its sensing-server WebSocket (``O_RUVIEW_WS``, default server
   address ``ws://localhost:8765/ws/sensing``; ``RUVIEW_API_TOKEN`` is sent as ``Authorization: Bearer``
   like RuView's own ``wifi_densepose.client.SensingClient``) or its MQTT output
   (``O_RUVIEW_MQTT=mqtt://host:1883[/<topic filter>]``; by default ``ruview/+/raw/#`` firehose topics and
   the Home Assistant ``homeassistant/+/+/+/state`` topics of ``wifi_densepose_*`` devices).
   Message schema, from RuView's ``python/wifi_densepose/client/ws.py`` and sensing-server ``main.rs``:
   ``{"type": "edge_vitals", node_id, presence: bool, motion: bool, motion_energy, presence_score,
   n_persons, breathing_rate_bpm, heartrate_bpm, rssi, fall_detected, ...}``,
   ``{"type": "sensing_update", classification: {motion_level: "absent"|"present_still"|"present_moving",
   presence, confidence}, features: {motion_band_power, variance, ...}, vital_signs:
   {breathing_rate_bpm, heart_rate_bpm, ...}, persons: [...], nodes: [...]}``, ``pose_data`` and
   ``connection_established``; HA state payloads are ``ON``/``OFF`` (``presence``) or a number
   (``motion_level`` %, ``person_count``). RuView already turns ESP32-S3/C6 or
   Intel 5300 / AR9580 CSI into presence, occupancy, activity, vitals and pose. Only room-level
   presence and a motion level are kept; occupancy counts collapse to a boolean, and pose, per-person
   tracks and heart rate are dropped on arrival. Breathing is kept only as a live in-memory value
   for the opted-in user's own space (``vitals_opt_in``), never published, never logged. RuView
   labels its vitals unvalidated; ``VITALS_NOTE`` carries that to the UI.
2. Raw frames: an ESP32 CSI node streaming esp32-csi-tool text lines over UDP (``O_CSI_UDP=host:port``),
   or csiread-style frames (``citysu/csiread``: Intel 5300 / Atheros / Nexmon ``nexmonster/nexmon_csi``)
   handed to ``CSISensor.feed``. These go through the NumPy path below: phase unwrapped across
   subcarriers with the linear (STO/CFO) offset removed, rolling relative amplitude variance, and the
   energy of the sanitized phase in a Doppler band.

Apple Wi-Fi exposes no CSI; on a Mac the only CSI source is a RuView/ESP32 node on the LAN.
No raw frame is kept: each frame is reduced to amplitude and sanitized phase in a bounded ring.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import re
import socket
import threading
import time
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

import numpy as np

from . import SensorWorker

VITALS_NOTE = "RuView vitals are labelled unvalidated by RuView; shown live only, never stored"
DEFAULT_WINDOW = 128
DEFAULT_BAND_HZ = (2.0, 25.0)
EMIT_EVERY_S = 0.5
RUVIEW_MOTION_KEYS = ("motion", "motion_level", "motion_energy", "activity_level", "movement")
RUVIEW_PRESENCE_KEYS = ("presence", "present", "occupied", "human_present")
RUVIEW_BREATHING_KEYS = ("breathing_rate_bpm", "breathing", "breathing_rate", "breathing_bpm", "respiration_rate", "rr")
RUVIEW_DEFAULT_WS = "ws://localhost:8765/ws/sensing"
RUVIEW_TOKEN_ENV = "RUVIEW_API_TOKEN"
RUVIEW_MQTT_TOPICS = ("ruview/+/raw/#", "homeassistant/+/+/+/state")
RUVIEW_MOTION_LEVELS = {"absent": 0.0, "present_still": 0.0, "present_moving": 1.0}
ACTIVE_ACTIVITIES = frozenset({"walking", "moving", "active", "gesture", "running", "standing", "sitting_moving"})
_ESP32_ARRAY = re.compile(r"\[([^\]]*)\]")


# ------------------------------------------------------------------------------------ parsers


def parse_csiread_frame(frame: Any) -> np.ndarray:
    """A 1-D complex vector (subcarriers) from a csiread-style frame.

    Accepts a complex ndarray of shape (subc,), (subc, nrx[, ntx]) or (1, subc, ...), an object with
    a ``csi`` attribute (csiread readers), or a dict with ``csi`` / (``real``, ``imag``) entries.
    Multiple antennas are averaged in the complex domain after the first antenna's phase reference.
    """
    if isinstance(frame, dict):
        if "csi" in frame:
            frame = frame["csi"]
        elif "real" in frame and "imag" in frame:
            frame = np.asarray(frame["real"], dtype=float) + 1j * np.asarray(frame["imag"], dtype=float)
    elif hasattr(frame, "csi") and not isinstance(frame, np.ndarray):
        frame = getattr(frame, "csi")
    arr = np.asarray(frame)
    if not np.iscomplexobj(arr):
        if arr.ndim >= 1 and arr.shape[-1] == 2:
            arr = arr[..., 0] + 1j * arr[..., 1]
        else:
            raise ValueError("CSI frame is not complex and not (..., 2) real/imag pairs")
    while arr.ndim > 1 and arr.shape[0] == 1:
        arr = arr[0]
    if arr.ndim == 1:
        return arr.astype(np.complex128)
    flat = arr.reshape(arr.shape[0], -1)
    return flat.mean(axis=1).astype(np.complex128)


def parse_esp32_line(line: str | bytes) -> tuple[np.ndarray, float | None]:
    """(complex vector, rssi) from an esp32-csi-tool ``CSI_DATA,...,[i r i r ...]`` line.

    The ESP32 buffer interleaves imaginary then real int8 values per subcarrier.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    m = _ESP32_ARRAY.search(line)
    if not m:
        raise ValueError("no CSI array in line")
    values = np.array([int(v) for v in m.group(1).replace(",", " ").split()], dtype=float)
    if values.size < 4 or values.size % 2:
        raise ValueError("CSI array must hold an even number of values")
    imag, real = values[0::2], values[1::2]
    rssi = None
    fields = line.split(",")
    if len(fields) > 3 and fields[0].strip() == "CSI_DATA":
        try:
            rssi = float(fields[3])
        except ValueError:
            rssi = None
    return real + 1j * imag, rssi


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def _typed_ruview(msg: dict[str, Any]) -> dict[str, Any] | None | bool:
    """The RuView sensing-server message types; False when ``msg`` is not one of them."""
    mtype = msg.get("type")
    if mtype in ("connection_established", "pong"):
        return None
    if mtype == "edge_vitals":
        presence = msg.get("presence")
        if presence is None and msg.get("n_persons") is not None:
            presence = (_num(msg.get("n_persons")) or 0) > 0
        motion = _num(msg.get("motion"))
        if motion is None:
            motion = _num(msg.get("motion_energy"))
        if presence is None and motion is None:
            return None
        out: dict[str, Any] = {"presence": bool(presence) if presence is not None else (motion or 0.0) > 0,
                               "motion": float(motion or 0.0)}
        br = _num(msg.get("breathing_rate_bpm"))
        if br is not None:
            out["breathing"] = br
        return out
    if mtype == "sensing_update":
        cls = msg.get("classification") if isinstance(msg.get("classification"), dict) else {}
        level = cls.get("motion_level")
        motion = RUVIEW_MOTION_LEVELS.get(level.lower()) if isinstance(level, str) else _num(level)
        presence = cls.get("presence")
        if presence is None and isinstance(level, str) and level.lower() in RUVIEW_MOTION_LEVELS:
            presence = level.lower() != "absent"
        if presence is None and isinstance(msg.get("persons"), list):
            presence = len(msg["persons"]) > 0
        if presence is None and motion is None:
            return None
        out = {"presence": bool(presence) if presence is not None else (motion or 0.0) > 0,
               "motion": float(motion or 0.0)}
        vs = msg.get("vital_signs") if isinstance(msg.get("vital_signs"), dict) else {}
        br = _num(vs.get("breathing_rate_bpm"))
        if br is not None:
            out["breathing"] = br
        return out
    if mtype == "pose_data":
        persons = msg.get("persons")
        return {"presence": bool(persons), "motion": 0.0} if isinstance(persons, list) else None
    return False


def parse_ruview_ha_state(topic: str, payload: Any) -> dict[str, Any] | None:
    """A RuView Home Assistant state topic ``homeassistant/<kind>/wifi_densepose_<node>/<slug>/state``.

    ``presence`` is ``ON``/``OFF``; ``motion_level`` a 0-100 %; ``person_count`` a count. Other slugs
    (heart_rate, breathing_rate, semantic primitives) are ignored: they are biometric or derived.
    """
    parts = (topic or "").split("/")
    if len(parts) != 5 or parts[0] != "homeassistant" or parts[4] != "state" or not parts[2].startswith("wifi_densepose_"):
        return None
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    text = str(payload).strip()
    if text.startswith("{"):
        try:
            text = str(json.loads(text).get("state", ""))
        except Exception:
            return None
    slug = parts[3]
    if slug == "presence":
        if text.upper() not in ("ON", "OFF"):
            return None
        return {"presence": text.upper() == "ON"}
    if slug == "motion_level":
        v = _num(text)
        return None if v is None else {"motion": float(np.clip(v / 100.0, 0.0, 1.0))}
    if slug == "person_count":
        v = _num(text)
        return None if v is None else {"presence": v > 0}
    return None


def parse_ruview_message(msg: Any, topic: str = "") -> dict[str, Any] | None:
    """Room-level fields from a RuView sensing message: ``{presence, motion, breathing?}``.

    Pose, keypoints, per-person tracks, heart rate and identities are dropped here, before anything
    else sees them. ``breathing`` is returned for the caller's in-memory live value only. The real
    sensing-server types are parsed by their schema; anything else goes through a defensive search
    of known key names (older builds, bridges, hand-rolled publishers).
    """
    if topic.startswith("homeassistant/"):
        return parse_ruview_ha_state(topic, msg)
    if isinstance(msg, (bytes, str)):
        try:
            msg = json.loads(msg)
        except Exception:
            return None
    if not isinstance(msg, dict):
        return None
    typed = _typed_ruview(msg)
    if typed is not False:
        return typed  # type: ignore[return-value]
    flat: dict[str, Any] = {}
    for key in ("sensing", "data", "result", "state", "classification", "vitals", "vital_signs", "features"):
        sub = msg.get(key)
        if isinstance(sub, dict):
            flat.update(sub)
    flat.update({k: v for k, v in msg.items() if not isinstance(v, (dict, list))})
    presence: bool | None = None
    for key in RUVIEW_PRESENCE_KEYS:
        if key in flat:
            v = flat[key]
            presence = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "present", "occupied")
            break
    if presence is None:
        for key in ("occupancy", "person_count", "n_persons", "persons", "count"):
            if key in flat:
                try:
                    presence = float(flat[key]) > 0
                except (TypeError, ValueError):
                    pass
                break
    motion: float | None = None
    for key in RUVIEW_MOTION_KEYS:
        if key in flat:
            v = flat[key]
            motion = RUVIEW_MOTION_LEVELS.get(v.lower()) if isinstance(v, str) else _num(v)
            break
    activity = flat.get("activity")
    if motion is None and isinstance(activity, str):
        motion = 1.0 if activity.lower() in ACTIVE_ACTIVITIES else 0.0
    if presence is None and motion is None:
        return None
    out: dict[str, Any] = {"presence": bool(presence) if presence is not None else (motion or 0.0) > 0,
                           "motion": float(motion or 0.0)}
    for key in RUVIEW_BREATHING_KEYS:
        if key in flat:
            try:
                out["breathing"] = float(flat[key])
            except (TypeError, ValueError):
                pass
            break
    return out


# ------------------------------------------------------------------------------- signal math


def sanitize_phase(csi: np.ndarray, subcarriers: np.ndarray | None = None) -> np.ndarray:
    """Phase unwrapped across subcarriers with the linear term (slope + offset) removed.

    The slope comes from timing offset (STO/SFO) and the offset from CFO; neither is motion.
    """
    phase = np.unwrap(np.angle(np.asarray(csi)))
    k = np.arange(phase.size, dtype=float) - (phase.size - 1) / 2.0 if subcarriers is None else np.asarray(subcarriers, float)
    span = k[-1] - k[0]
    slope = (phase[-1] - phase[0]) / span if span else 0.0
    return phase - slope * k - phase.mean() + slope * k.mean()


def band_energy(series: np.ndarray, fs: float, band: tuple[float, float]) -> float:
    """Mean power of ``series`` (detrended, Hann-windowed) inside ``band`` Hz."""
    x = np.asarray(series, dtype=float)
    if x.size < 8 or fs <= 0:
        return 0.0
    x = x - np.polyval(np.polyfit(np.arange(x.size), x, 1), np.arange(x.size))
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2 / x.size
    freqs = np.fft.rfftfreq(x.size, 1.0 / fs)
    sel = (freqs >= band[0]) & (freqs <= min(band[1], fs / 2.0))
    return float(spec[sel].mean()) if sel.any() else 0.0


class CSIFeatures:
    """Rolling CSI features over ``window`` packets: ``{variance, doppler_energy}``."""

    def __init__(self, window: int = DEFAULT_WINDOW, band: tuple[float, float] = DEFAULT_BAND_HZ) -> None:
        self.window = int(window)
        self.band = band
        self._amp: collections.deque = collections.deque(maxlen=self.window)
        self._phase: collections.deque = collections.deque(maxlen=self.window)
        self._ts: collections.deque = collections.deque(maxlen=self.window)

    def push(self, csi: np.ndarray, ts: float) -> None:
        csi = np.asarray(csi)
        if self._amp and len(self._amp[-1]) != csi.size:
            self.reset()
        self._amp.append(np.abs(csi))
        self._phase.append(sanitize_phase(csi))
        self._ts.append(float(ts))

    def reset(self) -> None:
        self._amp.clear()
        self._phase.clear()
        self._ts.clear()

    @property
    def ready(self) -> bool:
        return len(self._amp) >= max(8, self.window // 4)

    def fs(self) -> float:
        if len(self._ts) < 2:
            return 0.0
        dt = np.diff(np.asarray(self._ts))
        dt = dt[dt > 0]
        return float(1.0 / np.median(dt)) if dt.size else 0.0

    def features(self) -> dict[str, float]:
        if not self.ready:
            return {"variance": 0.0, "doppler_energy": 0.0}
        amp = np.vstack(self._amp)
        mean = amp.mean(axis=0)
        rel_var = float(np.mean(amp.var(axis=0) / np.maximum(mean ** 2, 1e-12)))
        phase = np.vstack(self._phase)
        series = np.unwrap(phase, axis=0)
        doppler = float(np.mean([band_energy(series[:, k], self.fs(), self.band) for k in range(series.shape[1])]))
        return {"variance": rel_var, "doppler_energy": doppler}


# ------------------------------------------------------------------------------------ sources


def parse_hostport(spec: str, default_port: int = 5500) -> tuple[str, int]:
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty host:port")
    host, _, port = spec.rpartition(":")
    if not host:
        return spec, default_port
    return host.strip("[]"), int(port)


class ESP32UDPSource:
    """Yields ``(complex csi, ts)`` from esp32-csi-tool lines arriving over UDP."""

    def __init__(self, spec: str, timeout: float = 0.5) -> None:
        self.host, self.port = parse_hostport(spec)
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(self.timeout)
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def read(self) -> tuple[np.ndarray, float] | None:
        if self._sock is None:
            self.open()
        try:
            data, _ = self._sock.recvfrom(65535)  # type: ignore[union-attr]
        except socket.timeout:
            return None
        for line in data.splitlines():
            try:
                csi, _ = parse_esp32_line(line)
                return csi, time.time()
            except ValueError:
                continue
        return None


def ruview_package() -> str | None:
    """Name of an importable RuView Python package (PyO3 build), or None."""
    for name in ("ruview", "wifi_densepose", "ruview_py"):
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except Exception:
            continue
    return None


class RuViewWSSource:
    """Messages from RuView's sensing WebSocket (``websockets`` sync client, ``additional_headers``
    for the bearer token as in websockets >= 14; ``recv(timeout=)`` raises ``TimeoutError``)."""

    kind = "ruview-ws"

    def __init__(self, url: str, connect: Any = None, token: str | None = None) -> None:
        import os

        self.url = url or RUVIEW_DEFAULT_WS
        self._connect = connect
        self.token = token if token is not None else os.environ.get(RUVIEW_TOKEN_ENV, "")
        self._ws: Any = None

    def connect_kwargs(self) -> dict[str, Any]:
        return {"additional_headers": {"Authorization": f"Bearer {self.token}"}} if self.token else {}

    def messages(self, stop: threading.Event) -> Iterator[Any]:
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect  # type: ignore[no-redef]
        with connect(self.url, **self.connect_kwargs()) as ws:
            self._ws = ws
            while not stop.is_set():
                try:
                    yield ws.recv(timeout=1.0)
                except TimeoutError:
                    continue


class RuViewMQTTSource:
    """``(topic, payload)`` from RuView's MQTT output (paho-mqtt 2 ``CallbackAPIVersion.VERSION2``).

    The URL path, when given, is the one topic filter; otherwise ``RUVIEW_MQTT_TOPICS``.
    """

    kind = "ruview-mqtt"

    def __init__(self, url: str, client_factory: Any = None) -> None:
        parsed = urlparse(url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 1883
        self.username = parsed.username
        self.password = parsed.password
        rest = url.split("://", 1)[-1]  # not parsed.path: MQTT's '#' wildcard is a URL fragment marker
        path = rest.split("/", 1)[1] if "/" in rest else ""
        self.topics = (path,) if path else RUVIEW_MQTT_TOPICS
        self._factory = client_factory

    @property
    def topic(self) -> str:
        return self.topics[0]

    def messages(self, stop: threading.Event) -> Iterator[Any]:
        import queue as _q

        q: _q.Queue = _q.Queue(maxsize=256)
        factory = self._factory
        if factory is None:
            import paho.mqtt.client as mqtt

            def factory() -> Any:
                try:
                    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                except AttributeError:
                    return mqtt.Client()

        client = factory()

        def on_message(_c: Any, _u: Any, m: Any) -> None:
            if not q.full():
                q.put_nowait((str(getattr(m, "topic", "")), m.payload))

        client.on_message = on_message
        if self.username and hasattr(client, "username_pw_set"):
            client.username_pw_set(self.username, self.password)
        client.connect(self.host, self.port)
        client.subscribe([(t, 0) for t in self.topics])
        client.loop_start()
        try:
            while not stop.is_set():
                try:
                    yield q.get(timeout=1.0)
                except _q.Empty:
                    continue
        finally:
            client.loop_stop()
            client.disconnect()


# ------------------------------------------------------------------------------------- worker


class CSISensor(SensorWorker):
    """Publishes ``sensor/csi`` ``{variance, doppler_energy, presence?, motion?, source, ts}``."""

    name = "csi"
    topic = "sensor/csi"
    description = ("Wi-Fi CSI room sensing: room-level motion and presence only, computed on this machine. "
                   "No per-person tracking or identification; raw CSI is never stored. " + VITALS_NOTE + ".")

    def __init__(self, bus: Any = None, log: Any = None, enabled: bool = False, udp: str = "",
                 ruview_ws: str = "", ruview_mqtt: str = "", vitals_opt_in: bool = False,
                 window: int = DEFAULT_WINDOW, source: Any = None) -> None:
        super().__init__(bus, log, enabled)
        self.udp = udp
        self.ruview_ws = ruview_ws
        self.ruview_mqtt = ruview_mqtt
        self.vitals_opt_in = bool(vitals_opt_in)
        self.features = CSIFeatures(window)
        self._source = source
        self._live: dict[str, Any] | None = None
        self._ruview_state: dict[str, Any] = {}
        self._last_emit_mono = 0.0

    def source_kind(self) -> str:
        if self._source is not None:
            return getattr(self._source, "kind", "injected")
        if self.ruview_ws:
            return "ruview-ws"
        if self.ruview_mqtt:
            return "ruview-mqtt"
        if self.udp:
            return "esp32-udp"
        return "none"

    def available(self) -> tuple[bool, str]:
        kind = self.source_kind()
        if kind == "none":
            pkg = ruview_package()
            hint = f" (RuView package {pkg!r} is installed; point O_RUVIEW_WS at its sensing WebSocket)" if pkg else ""
            return False, ("no CSI source configured: set O_RUVIEW_WS / O_RUVIEW_MQTT for a RuView node, or "
                           "O_CSI_UDP=host:port for an ESP32 CSI node; Apple Wi-Fi exposes no CSI" + hint)
        if kind == "ruview-ws" and self._source is None and importlib.util.find_spec("websockets") is None:
            return False, "websockets is not installed (needed for the RuView sensing WebSocket)"
        if kind == "ruview-mqtt" and self._source is None and importlib.util.find_spec("paho") is None:
            return False, "paho-mqtt is not installed (needed for RuView over MQTT)"
        if kind == "esp32-udp":
            try:
                parse_hostport(self.udp)
            except ValueError as exc:
                return False, f"O_CSI_UDP is not host:port ({exc})"
        return True, ""

    # ---- inputs (all on the worker thread or a test) --------------------------------------------
    def feed(self, frame: Any, ts: float | None = None) -> dict[str, Any] | None:
        """One raw frame (csiread-style object/array or an ESP32 line). Returns the emitted payload."""
        ts = time.time() if ts is None else float(ts)
        csi = parse_esp32_line(frame)[0] if isinstance(frame, (str, bytes)) else parse_csiread_frame(frame)
        self.features.push(csi, ts)
        return self._maybe_emit(ts, self.features.features() | {"source": "raw"})

    def feed_ruview(self, msg: Any, ts: float | None = None, topic: str = "") -> dict[str, Any] | None:
        """One RuView message (WS frame, MQTT payload, or a ``(topic, payload)`` pair)."""
        ts = time.time() if ts is None else float(ts)
        if isinstance(msg, tuple) and len(msg) == 2 and isinstance(msg[0], str):
            topic, msg = msg
        parsed = parse_ruview_message(msg, topic=topic)
        if parsed is None:
            return None
        breathing = parsed.pop("breathing", None)
        if self.vitals_opt_in and breathing is not None:
            self._live = {"breathing": float(breathing), "ts": ts, "note": VITALS_NOTE}
        # HA state topics carry one field each; merge into the last known room state.
        state = self._ruview_state
        state.update(parsed)
        if "presence" not in state:
            state["presence"] = float(state.get("motion", 0.0)) > 0
        motion = float(state.get("motion", 0.0)) if state["presence"] else 0.0
        payload = {"variance": motion, "doppler_energy": motion, "presence": bool(state["presence"]),
                   "motion": motion, "source": "ruview"}
        return self._maybe_emit(ts, payload, force=True)

    def _maybe_emit(self, ts: float, payload: dict[str, Any], force: bool = False) -> dict[str, Any] | None:
        now = time.monotonic()
        if not force and (not self.features.ready or now - self._last_emit_mono < EMIT_EVERY_S):
            return None
        self._last_emit_mono = now
        payload = {**payload, "ts": ts}
        self.emit(payload)
        return payload

    def live(self) -> dict[str, Any] | None:
        """The opted-in user's own breathing value (memory only), or None."""
        return dict(self._live) if (self.vitals_opt_in and self._live) else None

    def _run_loop(self) -> None:
        kind = self.source_kind()
        if kind in ("ruview-ws", "ruview-mqtt") or (self._source is not None and hasattr(self._source, "messages")):
            src = self._source or (RuViewWSSource(self.ruview_ws) if kind == "ruview-ws" else RuViewMQTTSource(self.ruview_mqtt))
            for msg in src.messages(self._stop):
                if self._stop.is_set():
                    break
                self.feed_ruview(msg)
            return
        src = self._source or ESP32UDPSource(self.udp)
        try:
            while not self._stop.is_set():
                got = src.read()
                if got is None:
                    continue
                csi, ts = got
                self.features.push(csi, ts)
                self._maybe_emit(ts, self.features.features() | {"source": "raw"})
        finally:
            close = getattr(src, "close", None)
            if callable(close):
                close()


def frames_to_features(frames: Iterable[np.ndarray], fs: float, window: int = DEFAULT_WINDOW) -> dict[str, float]:
    """Offline helper: features of a sequence of complex frames sampled at ``fs`` Hz."""
    f = CSIFeatures(window)
    for i, frame in enumerate(frames):
        f.push(frame, i / fs)
    return f.features()
