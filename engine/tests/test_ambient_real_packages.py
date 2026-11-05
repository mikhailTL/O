"""Adapters exercised against the REAL third-party packages (open-rppg, microsandbox, websockets,
paho-mqtt). Each test skips when its package is not installed, so the default engine suite stays
offline; run this file in an environment with the ``ambient`` extra installed to exercise them."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import threading
import time

import numpy as np
import pytest

from o_engine.ambient import bus as busmod
from o_engine.ambient.sensors import csi
from o_engine.ambient.sensors.rppg import RPPGSensor, open_rppg_box, open_rppg_hr, open_rppg_status
from o_engine.schema import ProvisionStep

BPM = 72.0
FPS = 30.0


def _need_rppg():
    pytest.importorskip("cv2")
    ok, reason = open_rppg_status()
    if not ok:
        pytest.skip(reason)
    import rppg

    return rppg


def _skin(i, h=72, w=72, seed=0):
    rng = np.random.default_rng(seed + i)
    p = np.sin(2 * np.pi * BPM / 60.0 * i / FPS)
    img = np.array([200.0, 150.0, 120.0]) + np.array([1.0, 3.0, 1.5]) * p + rng.normal(0, 0.8, (h, w, 3))
    return np.clip(img, 0, 255).astype(np.uint8)


def _face_frame(i, t=None):
    """Cartoon face whose skin pulses at BPM; phase from frame index, or wall time ``t`` when given."""
    import cv2

    p = np.sin(2 * np.pi * BPM / 60.0 * (i / FPS if t is None else t))
    f = np.full((480, 640, 3), 40, np.uint8)
    col = tuple(int(c) for c in np.clip(np.array([200.0, 150.0, 120.0]) + np.array([2.0, 6.0, 3.0]) * p, 0, 255))
    cv2.ellipse(f, (320, 240), (110, 150), 0, 0, 360, col, -1)
    for ex in (275, 365):
        cv2.circle(f, (ex, 200), 14, (250, 250, 250), -1)
        cv2.circle(f, (ex, 200), 6, (30, 20, 10), -1)
    cv2.ellipse(f, (320, 320), (40, 12), 0, 0, 180, (120, 40, 50), -1)
    cv2.line(f, (320, 220), (310, 280), (150, 100, 90), 3)
    return f


# ----------------------------------------------------------------------------- open-rppg


def test_open_rppg_model_recovers_pulse_from_cropped_roi_tensor():
    rppg = _need_rppg()
    assert "FacePhys.rlap" in rppg.supported_models
    model = rppg.Model("FacePhys.rlap")
    faces = np.stack([_skin(i) for i in range(int(FPS * 20))])
    res = model.process_faces_tensor(faces, fps=FPS)
    assert set(res) == {"hr", "SQI", "hrv", "latency"}
    assert abs(float(res["hr"]) - BPM) < 3.0
    assert abs(open_rppg_hr(model, window_s=20) - BPM) < 3.0


def test_open_rppg_detector_box_maps_to_xywh_on_synthetic_face():
    rppg = _need_rppg()
    model = rppg.Model("FacePhys.rlap")
    frames = np.stack([_face_frame(i) for i in range(int(FPS * 6))])
    model.process_video_tensor(frames, fps=FPS)
    box = open_rppg_box(model)
    assert box is not None
    x, y, w, h = box
    assert w > 40 and h > 40 and 0 <= x < 640 and 0 <= y < 480


def test_face_finder_falls_back_to_open_rppg_blazeface_on_opencv5():
    _need_rppg()
    import cv2

    from o_engine.ambient.sensors.rppg import FaceFinder

    ok, reason, kind = FaceFinder.status()
    assert ok, reason
    if hasattr(cv2, "CascadeClassifier") or importlib.util.find_spec("mediapipe") is not None:
        pytest.skip(f"this environment uses {kind}")
    assert kind == "blazeface"
    f = FaceFinder()
    f.open()
    box, landmarks = f.find(_face_frame(0))
    assert box is not None and landmarks is None and box[2] > 60 and box[3] > 60
    assert f.find(np.full((480, 640, 3), 40, np.uint8)) == (None, None)


def test_rppg_sensor_runs_real_open_rppg_loop_with_injected_frames():
    rppg = _need_rppg()
    bus = busmod.Bus()
    got = []
    bus.subscribe("sensor/face", lambda t, p: got.append(p))
    counter = {"i": 0}
    t0 = time.time()

    def capture():
        i = counter["i"]
        counter["i"] += 1
        delay = t0 + i / FPS - time.time()
        if delay > 0:
            time.sleep(delay)
        return _face_frame(i, time.time() - t0)

    sensor = RPPGSensor(bus, None, enabled=True, heart=True, capture=capture, fps=FPS, model_factory=rppg.Model)
    assert sensor.start()
    deadline = time.time() + 90
    switched, live = None, None
    while time.time() < deadline:
        time.sleep(0.25)
        bus.flush(0.1)
        if switched is None and sensor.pulse_via == "open-rppg":
            switched = time.time()
            published_before_model = len(got)
        cur = sensor.live()
        if switched and cur and cur["via"].startswith("open-rppg") and time.time() - switched > 13:
            live = cur
            break
    sensor.stop(timeout=10)
    bus.flush(2)
    bus.stop()
    assert switched is not None, sensor.last_error
    assert published_before_model > 0  # presence flows while the model is still loading
    after = got[published_before_model:]
    assert after and any(p["face"] for p in after)  # open-rppg's BlazeFace box drives presence
    assert all(set(p) == {"face", "approaching", "ts"} for p in got)
    assert live is not None and live["via"] == "open-rppg:FacePhys.rlap" and abs(live["bpm"] - BPM) < 8, live
    assert sensor.live() is None  # stop() clears the in-memory value
    assert sensor.last_error == ""


# ----------------------------------------------------------------------------- microsandbox


def test_microsandbox_real_volume_bind_and_status():
    ms = pytest.importorskip("microsandbox")
    from o_engine.ambient.microsandbox import MicrosandboxSandbox, microsandbox_status

    mc = ms.Volume.bind("/tmp")
    assert mc.kind == ms.MountKind.BIND and mc.bind == "/tmp"
    assert ms.is_runtime_installed() is True
    ok, reason = microsandbox_status()
    if not os.access("/dev/kvm", os.R_OK | os.W_OK) and os.uname().sysname == "Linux":
        assert not ok and "/dev/kvm" in reason
    kw = MicrosandboxSandbox(job_dir="/tmp").create_kwargs()
    assert isinstance(kw["volumes"], dict) and kw["volumes"]["/work"].kind == ms.MountKind.BIND


@pytest.mark.skipif(os.environ.get("O_TEST_MSB_REAL") != "1", reason="set O_TEST_MSB_REAL=1 (pulls an OCI image)")
def test_microsandbox_real_sdk_accepts_our_arguments(tmp_path):
    pytest.importorskip("microsandbox")
    from o_engine.ambient.microsandbox import MicrosandboxSandbox

    sb = MicrosandboxSandbox(job_dir=tmp_path, image="alpine", memory=512, status=(True, ""))
    res = sb.run(ProvisionStep(target_directory=".", command_string="echo hi > out.txt && cat out.txt",
                               timeout_seconds=60), 0)
    sb.destroy()
    if os.access("/dev/kvm", os.R_OK | os.W_OK) or os.uname().sysname != "Linux":
        assert res.exit_code == 0 and res.stdout.strip() == "hi" and (tmp_path / "out.txt").read_text().strip() == "hi"
    else:
        # The SDK validated every argument (a wrong one raises TypeError before any work) and pulled the
        # image; only the VM boot fails without KVM.
        assert res.exit_code == 127 and "MicrosandboxError" in res.stderr and "TypeError" not in res.stderr


# ----------------------------------------------------------------------------- RuView over real websockets / paho


EDGE = {"type": "edge_vitals", "node_id": "n1", "presence": True, "fall_detected": False, "motion": True,
        "breathing_rate_bpm": 14.5, "heartrate_bpm": 66.0, "n_persons": 1, "person_count_valid": True,
        "motion_energy": 0.7, "presence_score": 0.9, "rssi": -52.0}
UPDATE = {"type": "sensing_update", "timestamp": 1.0, "source": "esp32", "tick": 7, "nodes": [],
          "features": {"mean_rssi": -50.0, "variance": 1.0, "motion_band_power": 0.5},
          "classification": {"motion_level": "present_still", "presence": True, "confidence": 0.9},
          "vital_signs": {"breathing_rate_bpm": 13.0, "heart_rate_bpm": 62.0},
          "persons": [{"id": 1, "keypoints": [{"name": "nose", "x": 1.0}]}]}


def test_ruview_ws_source_against_real_websockets_server(monkeypatch):
    pytest.importorskip("websockets")
    from websockets.asyncio.server import serve

    seen_headers = {}
    started = threading.Event()
    port_box = {}
    loop = asyncio.new_event_loop()

    async def handler(ws):
        seen_headers["auth"] = ws.request.headers.get("Authorization")
        await ws.send(json.dumps({"type": "connection_established", "node_id": "n1", "version": "x", "capabilities": []}))
        await ws.send(json.dumps(EDGE))
        await ws.send(json.dumps(UPDATE))
        await asyncio.sleep(5)

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port_box["port"] = server.sockets[0].getsockname()[1]
            started.set()
            await asyncio.sleep(8)

    th = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    th.start()
    assert started.wait(5)
    monkeypatch.setenv("RUVIEW_API_TOKEN", "tok123")
    bus = busmod.Bus()
    got = []
    bus.subscribe("sensor/csi", lambda t, p: got.append(p))
    s = csi.CSISensor(bus, enabled=True, ruview_ws=f"ws://127.0.0.1:{port_box['port']}/ws/sensing", vitals_opt_in=True)
    assert s.start()
    deadline = time.time() + 5
    while time.time() < deadline and len(got) < 2:
        bus.flush(0.1)
        time.sleep(0.05)
    s.stop(timeout=3)
    bus.stop()
    assert seen_headers["auth"] == "Bearer tok123"
    assert [(p["presence"], p["motion"]) for p in got[:2]] == [(True, 1.0), (True, 0.0)]
    assert all("heart" not in json.dumps(p) and "breath" not in json.dumps(p) for p in got)
    assert s.live()["breathing"] == 13.0


def test_ruview_mqtt_with_real_paho_client_and_message_objects():
    pytest.importorskip("paho.mqtt.client")
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    rc, _mid = client.subscribe([(t, 0) for t in csi.RUVIEW_MQTT_TOPICS])  # validates the filters offline
    assert rc == mqtt.MQTT_ERR_NO_CONN
    src = csi.RuViewMQTTSource("mqtt://user:pw@broker.local:1884")
    assert (src.host, src.port, src.username, src.topics) == ("broker.local", 1884, "user", csi.RUVIEW_MQTT_TOPICS)

    class Factory:
        def __call__(self):
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            self.client.connect = lambda host, port: 0
            self.client.loop_start = lambda: None
            self.client.loop_stop = lambda: None
            self.client.disconnect = lambda: None
            return self.client

    factory = Factory()
    stop = threading.Event()
    it = csi.RuViewMQTTSource("mqtt://127.0.0.1", client_factory=factory).messages(stop)
    pending = []

    def pump():
        pending.append(next(it))

    th = threading.Thread(target=pump)
    th.start()
    time.sleep(0.2)
    msg = mqtt.MQTTMessage(topic=b"homeassistant/binary_sensor/wifi_densepose_abc-node1/presence/state")
    msg.payload = b"ON"
    factory.client.on_message(factory.client, None, msg)
    th.join(3)
    stop.set()
    topic, payload = pending[0]
    s = csi.CSISensor(None, enabled=True, ruview_mqtt="mqtt://x")
    assert s.feed_ruview((topic, payload), ts=1.0)["presence"] is True
    motion = s.feed_ruview(("homeassistant/sensor/wifi_densepose_abc-node1/motion_level/state", b"100"), ts=2.0)
    assert motion["presence"] is True and motion["motion"] == 1.0
    raw = s.feed_ruview(("ruview/abc-node1/raw/edge_vitals", json.dumps(EDGE).encode()), ts=3.0)
    assert raw["presence"] is True and raw["motion"] == 1.0
    assert s.feed_ruview(("homeassistant/sensor/wifi_densepose_abc-node1/heart_rate/state", b"70"), ts=4.0) is None
