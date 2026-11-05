"""Plan J: pure-NumPy signal math on synthetic signals (CSI, sonar, rPPG)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from o_engine.ambient.sensors import csi, rppg, sonar


# ----------------------------------------------------------------------------- CSI


def _csi_frames(n, subc=52, fs=100.0, motion=False, seed=0):
    rng = np.random.default_rng(seed)
    k = np.arange(subc) - (subc - 1) / 2
    base_amp = 10 + 2 * np.cos(k / 6.0)
    frames = []
    for i in range(n):
        t = i / fs
        sto = 0.07 * k * rng.uniform(-1, 1)  # random timing offset each packet (linear in k)
        cfo = rng.uniform(-np.pi, np.pi)  # random carrier offset each packet
        amp = base_amp.copy()
        phase = 0.3 * np.sin(k / 9.0)
        if motion:
            amp = amp * (1 + 0.25 * np.sin(2 * np.pi * 6.0 * t + k / 4.0))
            phase = phase + 0.8 * np.sin(2 * np.pi * 6.0 * t) * (k / k.max()) ** 2
        amp = amp + rng.normal(0, 0.02, subc)
        frames.append(amp * np.exp(1j * (phase + sto + cfo)))
    return frames


def test_sanitize_phase_removes_linear_offset():
    k = np.arange(30) - 14.5
    true = 0.2 * np.sin(k / 5.0)
    true = true - true.mean()
    raw = np.exp(1j * (true + 0.3 * k + 1.7))
    clean = csi.sanitize_phase(raw)
    # what remains is the nonlinear part only (the linear fit of `true` is removed too)
    slope = (true[-1] - true[0]) / (k[-1] - k[0])
    expect = true - slope * k - (true - slope * k).mean()
    assert np.allclose(clean, expect, atol=1e-9)


def test_csi_variance_drop_is_detected():
    moving = csi.frames_to_features(_csi_frames(128, motion=True), fs=100.0)
    still = csi.frames_to_features(_csi_frames(128, motion=False, seed=1), fs=100.0)
    assert moving["variance"] > 10 * still["variance"]
    assert still["variance"] < 0.002 <= moving["variance"]  # below/above the presence threshold


def test_csi_doppler_energy_rises_with_motion():
    moving = csi.frames_to_features(_csi_frames(128, motion=True), fs=100.0)
    still = csi.frames_to_features(_csi_frames(128, motion=False, seed=2), fs=100.0)
    assert moving["doppler_energy"] > 20 * max(still["doppler_energy"], 1e-12)


def test_parse_esp32_line_imag_then_real():
    line = "CSI_DATA,STA,aa:bb:cc:dd:ee:ff,-42,11,1,0,0,0,0,0,0,0,0,-91,0,6,0,0,0,0,0,0,0,8,[3 4 -1 2 0 5]"
    vec, rssi = csi.parse_esp32_line(line)
    assert rssi == -42.0
    assert np.allclose(vec, [4 + 3j, 2 - 1j, 5 + 0j])
    with pytest.raises(ValueError):
        csi.parse_esp32_line("no array here")


def test_parse_csiread_frame_shapes():
    a = np.arange(1, 7) + 1j
    assert np.allclose(csi.parse_csiread_frame(a), a)
    multi = np.stack([a, a * 3], axis=1)  # (subc, nrx)
    assert np.allclose(csi.parse_csiread_frame(multi), a * 2)

    class Reader:
        csi = a[None, :]

    assert np.allclose(csi.parse_csiread_frame(Reader()), a)
    pairs = np.stack([a.real, a.imag], axis=-1)
    assert np.allclose(csi.parse_csiread_frame({"csi": pairs}), a)


def test_parse_ruview_message_keeps_room_level_only():
    msg = {"presence": True, "occupancy": 3, "motion_level": 0.4, "pose": [[1, 2]], "persons": [{"id": 1}],
           "vitals": {"breathing_rate": 14.0, "heart_rate": 70}}
    out = csi.parse_ruview_message(json.dumps(msg))
    assert out == {"presence": True, "motion": 0.4, "breathing": 14.0}
    assert csi.parse_ruview_message({"occupancy": 0}) == {"presence": False, "motion": 0.0}
    assert csi.parse_ruview_message({"activity": "walking"})["motion"] == 1.0
    assert csi.parse_ruview_message("not json") is None


# ----------------------------------------------------------------------------- sonar


def _echo(range_m, latency=137, gain=0.3, frames=1, shift_per_frame=0.0, seed=0):
    rng = np.random.default_rng(seed)
    tx = sonar.chirp()
    slot = int(round(sonar.FS * sonar.FRAME_S))
    rx = np.zeros((slot * frames, 2))
    for f in range(frames):
        d = int(round(2 * (range_m + f * shift_per_frame) / sonar.SPEED_OF_SOUND * sonar.FS))
        o = f * slot + latency
        for c, g in ((0, 1.0), (1, 0.85)):
            rx[o:o + tx.size, c] += g * tx
            rx[o + d:o + d + tx.size, c] += gain * tx
    return rx + 0.005 * rng.standard_normal(rx.shape), tx


def test_chirp_band_is_18_to_22_khz():
    tx = sonar.chirp()
    spec = np.abs(np.fft.rfft(tx, 1 << 15))
    freqs = np.fft.rfftfreq(1 << 15, 1 / sonar.FS)
    inband = spec[(freqs >= 17500) & (freqs <= 22500)].sum()
    assert inband / spec.sum() > 0.95
    assert tx.size == int(sonar.FS * sonar.CHIRP_S) and np.abs(tx).max() <= 1.0


def test_reflector_at_0_8_m_is_ranged():
    rx, tx = _echo(0.8)
    got = sonar.nearest_reflector(rx, tx)
    assert got is not None and abs(got - 0.8) < 0.03


def test_reflector_ranging_ignores_latency():
    for latency in (0, 400, 900):
        rx, tx = _echo(1.5, latency=latency)
        assert abs(sonar.nearest_reflector(rx, tx) - 1.5) < 0.03


def test_micro_doppler_motion_energy():
    tx = sonar.chirp()
    still, _ = _echo(0.8, frames=8)
    moving, _ = _echo(0.8, frames=8, shift_per_frame=0.004)
    m_still = sonar.motion_energy(sonar.frame_profiles(still, tx), 40)
    m_move = sonar.motion_energy(sonar.frame_profiles(moving, tx), 40)
    assert m_move > 10 * m_still


def test_sonar_process_emits_derived_features_only():
    rx, _ = _echo(0.8, frames=4)
    s = sonar.SonarSensor(recorder=lambda train: rx)
    out = s.process(rx, ts=1.0)
    assert set(out) == {"range_m", "motion", "ts"}
    assert abs(out["range_m"] - 0.8) < 0.03


# ----------------------------------------------------------------------------- rPPG


def _skin_rgb(hz=1.2, fs=30.0, seconds=10.0, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(0, seconds, 1 / fs)
    base = np.array([0.6, 0.45, 0.35]) * 200
    pulse = np.sin(2 * np.pi * hz * t)[:, None] * np.array([0.33, 0.77, 0.53]) * 0.01
    illum = (1 + 0.05 * np.sin(2 * np.pi * 0.1 * t))[:, None]
    return base * (1 + pulse) * illum + rng.normal(0, 0.3, (t.size, 3))


def test_pos_recovers_72_bpm():
    bpm = rppg.pos_bpm(_skin_rgb(1.2), 30.0)
    assert bpm is not None and abs(bpm - 72.0) < 3.0


def test_pos_recovers_other_rates():
    assert abs(rppg.pos_bpm(_skin_rgb(1.5, seed=3), 30.0) - 90.0) < 3.0


def test_approach_detector():
    det = rppg.ApproachDetector(window_s=1.0, growth=0.15)
    flags = [det.update(0.05 * (1 + 0.5 * i / 20), i * 0.05) for i in range(30)]
    assert flags[-1] is True
    det.reset()
    assert not any(det.update(0.05, i * 0.05) for i in range(30))


def test_roi_means_from_box():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    frame[:, :] = (200, 150, 100)
    means = rppg.roi_means(frame, box=(20, 20, 60, 60))
    assert np.allclose(means, [200, 150, 100])
    assert rppg.roi_means(frame) is None


def test_stress_proxy_bounds():
    assert rppg.stress_proxy(70, None) == 0.0
    assert rppg.stress_proxy(70, 70) == 0.0
    assert rppg.stress_proxy(130, 70) == 1.0


def test_parse_ruview_sensing_server_schema():
    """Field names from RuView's wifi_densepose/client/ws.py and sensing-server main.rs."""
    edge = {"type": "edge_vitals", "node_id": "n1", "presence": True, "fall_detected": False, "motion": False,
            "breathing_rate_bpm": 15.0, "heartrate_bpm": 70.0, "n_persons": 1, "motion_energy": 0.2,
            "presence_score": 0.8, "rssi": -50.0}
    assert csi.parse_ruview_message(edge) == {"presence": True, "motion": 0.0, "breathing": 15.0}
    assert csi.parse_ruview_message({**edge, "motion": True, "breathing_rate_bpm": None}) == {"presence": True, "motion": 1.0}
    assert csi.parse_ruview_message({"type": "edge_vitals", "n_persons": 0, "motion": False})["presence"] is False
    upd = {"type": "sensing_update", "classification": {"motion_level": "present_moving", "presence": True, "confidence": 0.9},
           "features": {"variance": 3.0, "motion_band_power": 0.4}, "vital_signs": {"breathing_rate_bpm": 12.0, "heart_rate_bpm": 60.0},
           "persons": [{"id": 1, "keypoints": []}], "pose_keypoints": [[0, 0, 0, 1]]}
    assert csi.parse_ruview_message(json.dumps(upd)) == {"presence": True, "motion": 1.0, "breathing": 12.0}
    assert csi.parse_ruview_message({"type": "sensing_update", "classification": {"motion_level": "absent"}}) == {"presence": False, "motion": 0.0}
    assert csi.parse_ruview_message({"type": "pose_data", "persons": []}) == {"presence": False, "motion": 0.0}
    assert csi.parse_ruview_message({"type": "connection_established", "node_id": "n1"}) is None
    assert csi.parse_ruview_message({"motion_level": "present_still", "presence": True}) == {"presence": True, "motion": 0.0}


def test_parse_ruview_home_assistant_state_topics():
    t = "homeassistant/binary_sensor/wifi_densepose_abc-node1/presence/state"
    assert csi.parse_ruview_message(b"ON", topic=t) == {"presence": True}
    assert csi.parse_ruview_message("OFF", topic=t) == {"presence": False}
    assert csi.parse_ruview_message("50", topic=t.replace("binary_sensor", "sensor").replace("presence", "motion_level")) == {"motion": 0.5}
    assert csi.parse_ruview_message("2", topic="homeassistant/sensor/wifi_densepose_x/person_count/state") == {"presence": True}
    assert csi.parse_ruview_message("70", topic="homeassistant/sensor/wifi_densepose_x/heart_rate/state") is None
    assert csi.parse_ruview_message("ON", topic="homeassistant/binary_sensor/other_device/presence/state") is None
    s = csi.CSISensor(None, enabled=True, ruview_mqtt="mqtt://x")
    assert s.feed_ruview((t, b"ON"), ts=1.0)["motion"] == 0.0
    assert s.feed_ruview(("homeassistant/sensor/wifi_densepose_abc-node1/motion_level/state", b"100"), ts=2.0)["motion"] == 1.0
    assert s.feed_ruview((t, b"OFF"), ts=3.0) == {"variance": 0.0, "doppler_energy": 0.0, "presence": False, "motion": 0.0,
                                                  "source": "ruview", "ts": 3.0}
    src = csi.RuViewMQTTSource("mqtt://h:1883")
    assert src.topics == csi.RUVIEW_MQTT_TOPICS and csi.RuViewMQTTSource("mqtt://h/ruview/#").topics == ("ruview/#",)
    assert csi.RuViewWSSource("", token="k").connect_kwargs() == {"additional_headers": {"Authorization": "Bearer k"}}
    assert csi.RuViewWSSource("ws://x", token="").url == "ws://x"
