"""Plan J: bus, presence machine, OS idle parsers, milestones, brief, TTS, sensor privacy."""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import types

import numpy as np
import pytest

from o_engine.actionlog import get_log
from o_engine.ambient import ABSENT, PRESENT, UNKNOWN
from o_engine.ambient import bus as busmod
from o_engine.ambient.brief import Briefer, normalise_model_brief, split_sentences, template_brief
from o_engine.ambient.milestones import MilestoneStore, kind_for_job
from o_engine.ambient.presence import PresenceMachine, parse_ioreg_idle, parse_xprintidle
from o_engine.ambient.sensors.csi import CSISensor
from o_engine.ambient.sensors.rppg import RPPGSensor
from o_engine.ambient.tts import Speaker, tts_command


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class ListLog:
    def __init__(self):
        self.rows = []

    def record(self, kind, summary, **detail):
        self.rows.append((kind, summary, detail))
        return {}


# ----------------------------------------------------------------------------- bus


def test_bus_delivers_in_priority_order_and_does_not_block():
    bus = busmod.Bus()
    got = []
    gate = threading.Event()
    bus.subscribe("sensor/csi", lambda t, p: (gate.wait(2), got.append(("low", p["n"]))), priority=0)
    bus.subscribe("sensor/csi", lambda t, p: got.append(("high", p["n"])), priority=100)
    t0 = time.monotonic()
    bus.publish("sensor/csi", {"n": 1})
    assert time.monotonic() - t0 < 0.5  # publish returns before the slow subscriber runs
    gate.set()
    assert bus.flush(2)
    assert got == [("high", 1), ("low", 1)]
    bus.stop()


def test_bus_message_priority_preempts_queue():
    bus = busmod.Bus()
    order = []
    hold = threading.Event()
    bus.subscribe("sensor/sonar", lambda t, p: (hold.wait(2), order.append(p["n"])))
    bus.subscribe("presence/state", lambda t, p: order.append(p["state"]))
    bus.publish("sensor/sonar", {"n": 0})  # occupies the dispatcher
    time.sleep(0.05)
    for n in (1, 2):
        bus.publish("sensor/sonar", {"n": n})
    bus.publish("presence/state", {"state": PRESENT}, priority=busmod.PRIORITY_HIGH)
    hold.set()
    assert bus.flush(2)
    assert order == [0, PRESENT, 1, 2]
    bus.stop()


@pytest.mark.parametrize("payload", [{"bpm": 70}, {"x": {"stress": 0.2}}, {"audio": [1, 2]}, {"pose": []},
                                     {"breathing": 12}])
def test_bus_rejects_raw_signals_and_biometrics(payload):
    with pytest.raises(ValueError):
        busmod.Bus().publish("sensor/face", payload)


def test_bus_rejects_unknown_topic():
    with pytest.raises(ValueError):
        busmod.Bus().publish("sensor/raw", {})


def test_make_bus_selection(monkeypatch):
    assert busmod.make_bus("").kind == "inproc"
    monkeypatch.setitem(sys.modules, "paho", None)
    monkeypatch.setitem(sys.modules, "paho.mqtt", None)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)
    b = busmod.make_bus("mqtt://127.0.0.1:1883")
    assert b.kind == "inproc" and "not installed" in b.reason
    monkeypatch.setitem(sys.modules, "redis", None)
    assert busmod.make_bus("redis://127.0.0.1:6379/0").kind == "inproc"
    assert "unsupported" in busmod.make_bus("amqp://x").reason


def test_mqtt_bus_round_trip_with_fake_client():
    class FakeMQTT:
        def __init__(self):
            self.on_message = None
            self.subs = []

        def connect(self, host, port):
            self.addr = (host, port)

        def subscribe(self, topic):
            self.subs.append(topic)

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

        def publish(self, topic, data):
            self.on_message(self, None, types.SimpleNamespace(topic=topic, payload=data))  # broker echo

    fake = FakeMQTT()
    bus = busmod.MQTTBus("mqtt://10.0.0.5:1999", client_factory=lambda: fake)
    got = []
    bus.subscribe("presence/state", lambda t, p: got.append(p))
    bus.start()
    bus.publish("presence/state", {"state": ABSENT})
    assert bus.flush(2)
    assert fake.addr == ("10.0.0.5", 1999) and fake.subs == ["o/#"]
    assert got == [{"state": ABSENT}]
    bus.stop()


def test_redis_bus_round_trip_with_fake_client():
    import queue as q

    class FakePubSub:
        def __init__(self):
            self.q = q.Queue()

        def psubscribe(self, pattern):
            self.pattern = pattern

        def get_message(self, timeout=0.5):
            try:
                return self.q.get(timeout=timeout)
            except q.Empty:
                return None

        def close(self):
            pass

    class FakeRedis:
        def __init__(self):
            self.ps = FakePubSub()

        def pubsub(self):
            return self.ps

        def publish(self, channel, data):
            self.ps.q.put({"type": "pmessage", "channel": channel, "data": data})

    bus = busmod.RedisBus("redis://x", client_factory=FakeRedis)
    got = []
    bus.subscribe("agent/milestone", lambda t, p: got.append(p["id"]))
    bus.start()
    bus.publish("agent/milestone", {"id": 7})
    deadline = time.time() + 2
    while not got and time.time() < deadline:
        time.sleep(0.01)
    assert got == [7]
    bus.stop()


# ----------------------------------------------------------------------------- presence


def _machine(enabled=("csi",), idle=None, absent_after=300.0):
    clock, wall = Clock(), Clock(1_700_000_000.0)
    log = ListLog()
    bus = busmod.Bus()
    published = []
    bus.subscribe("presence/state", lambda t, p: published.append(p))
    m = PresenceMachine(bus, log, absent_after, sensors_enabled=lambda: set(enabled),
                        idle_source=(lambda: idle[0]) if idle is not None else (lambda: None),
                        clock=clock, wall=wall)
    m.attach()
    return m, clock, wall, bus, published, log


def test_presence_absent_after_quiet_window_then_instant_present():
    m, clock, wall, bus, published, log = _machine(enabled=("csi", "camera", "sonar"))
    assert m.state == UNKNOWN
    m.on_face({"face": True, "approaching": False})
    assert m.state == PRESENT
    clock.t += 200
    m.on_csi({"variance": 0.0001, "doppler_energy": 0.0})  # still: below threshold
    m.on_face({"face": False, "approaching": False})
    m.tick()
    assert m.state == PRESENT
    clock.t += 101
    m.on_sonar({"motion": 0.001})  # no motion
    m.tick()
    assert m.state == ABSENT and m.absent_since == wall.t
    m.on_csi({"variance": 0.001, "doppler_energy": 0.5})  # micro-Doppler: instant PRESENT
    assert m.state == PRESENT
    bus.flush(2)
    assert [p["state"] for p in published] == [PRESENT, ABSENT, PRESENT]
    assert published[-1]["prev"] == ABSENT and published[-1]["absent_start"] == wall.t
    notes = [r for r in log.rows if r[0] == "note" and r[1].startswith("presence:")]
    assert len(notes) == 3 and "USER_ABSENT -> USER_PRESENT" in notes[-1][1]
    bus.stop()


def test_presence_csi_variance_or_face_keeps_user_present():
    m, clock, *_ = _machine(enabled=("csi", "camera"))
    m.on_face({"face": True})
    for _ in range(10):
        clock.t += 100
        m.on_csi({"variance": 0.01, "doppler_energy": 0.0})  # variance above threshold: someone is there
        m.tick()
    assert m.state == PRESENT
    for _ in range(10):
        clock.t += 100
        m.on_face({"face": True, "approaching": False})
        m.tick()
    assert m.state == PRESENT


def test_presence_face_approaching_wakes_and_debounce():
    m, clock, *_ = _machine(enabled=("camera",), absent_after=10)
    m.on_face({"face": True})
    clock.t += 11
    m.tick()
    assert m.state == ABSENT
    m.on_face({"face": True, "approaching": False})  # merely visible does not flip ABSENT
    assert m.state == ABSENT
    m.on_face({"face": True, "approaching": True})
    assert m.state == PRESENT
    clock.t += 11  # quiet again, but the dwell since the last transition is respected
    m.min_dwell_s = 30
    m.tick()
    assert m.state == PRESENT
    clock.t += 30
    m.tick()
    assert m.state == ABSENT


def test_presence_os_idle_fallback():
    idle = [1.0]
    m, clock, *_ = _machine(enabled=(), idle=idle, absent_after=300)
    m.tick()
    assert m.state == PRESENT and m.source == "os_idle"
    idle[0] = 301.0
    m.tick()
    assert m.state == ABSENT
    idle[0] = 0.5
    m.tick()
    assert m.state == PRESENT


def test_presence_without_sensors_or_idle_stays_unknown():
    m, *_ = _machine(enabled=(), idle=None)
    m.tick()
    assert m.state == UNKNOWN and "unavailable" in m.reason


def test_presence_simulate_holds_until_real_event():
    idle = [1.0]
    m, *_ = _machine(enabled=(), idle=idle)
    m.simulate(ABSENT, "demo")
    m.tick()
    assert m.state == ABSENT and m.source == "simulated"
    with pytest.raises(ValueError):
        m.simulate("SLEEPING")


def test_os_idle_parsers():
    ioreg = b'  |   "HIDIdleTime" = 4200000000\n  |   "Other" = 1\n'
    assert parse_ioreg_idle(ioreg) == pytest.approx(4.2)
    assert parse_ioreg_idle("nothing") is None
    assert parse_xprintidle(b"1500\n") == 1.5
    assert parse_xprintidle("x") is None


# ----------------------------------------------------------------------------- milestones


def test_milestones_table_wal_and_check(tmp_path):
    store = MilestoneStore(tmp_path / "m.sqlite")
    a = store.record("completed", "build app", task_id="t1", presence=ABSENT, ts=10.0)
    store.record("error_bypassed", "install deps", task_id="t2", ts=20.0)
    store.record("failed", "deploy", ts=30.0, detail="token=abcdef0123456789abcdef secret")
    assert a["id"] == 1 and store.journal_mode() == "wal"
    rows = store.since(15.0)
    assert [r["kind"] for r in rows] == ["error_bypassed", "failed"]
    with pytest.raises(ValueError):
        store.record("bogus", "x")
    conn = sqlite3.connect(tmp_path / "m.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO agent_milestones(ts, kind, title) VALUES (1, 'bogus', 'x')")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_milestones)")]
    assert cols == ["id", "ts", "task_id", "kind", "title", "detail", "presence"]


def test_kind_for_job():
    assert kind_for_job({"ok": True, "attempt": 1}) == "completed"
    assert kind_for_job({"ok": True, "attempt": 2}) == "error_bypassed"
    assert kind_for_job({"ok": False}) == "failed"


# ----------------------------------------------------------------------------- brief


ROWS = [
    {"kind": "completed", "title": "Compile the web app. Then test."},
    {"kind": "error_bypassed", "title": "Install numpy"},
    {"kind": "failed", "title": "Deploy to staging!"},
    {"kind": "checkpoint", "title": "Research notes saved"},
]


@pytest.mark.parametrize("rows", [[], ROWS, ROWS[:1], ROWS[2:3], ROWS[3:]])
def test_template_brief_is_exactly_three_sentences(rows):
    text = template_brief(rows, away_s=3720)
    assert len(split_sentences(text)) == 3, text


def test_template_brief_keeps_paths_in_titles():
    # real run: "Keep ./notes/summary.md updated ..." came out as "Keep  /notes/summary md updated"
    text = template_brief([{"kind": "completed", "title": "Keep ./notes/summary.md updated. Done!"}], away_s=60)
    assert "./notes/summary.md updated" in text, text
    assert len(split_sentences(text)) == 3, text


def test_normalise_model_brief():
    assert normalise_model_brief("**One.** Two! Three?") == "One. Two! Three?"
    assert normalise_model_brief("Only one.") is None
    assert normalise_model_brief("One. Two. Three. Four.") is None


class FakeModel:
    def __init__(self, reply=None, fail=False):
        self.reply, self.fail, self.calls = reply, fail, []
        self.local_calls = 0

    def local_only(self):
        self.local_calls += 1
        return self

    def complete(self, system, user):
        self.calls.append((system, user))
        if self.fail:
            raise RuntimeError("vllm down")
        return self.reply


def test_brief_uses_local_model_once(tmp_path):
    store = MilestoneStore(tmp_path / "m.sqlite")
    for i, r in enumerate(ROWS):
        store.record(r["kind"], r["title"], ts=100.0 + i)
    store.record("completed", "before absence", ts=50.0)
    model = FakeModel("I built the app. One install needed a workaround. The deploy failed and needs you.")
    spoken = []
    b = Briefer(store, model, speak=spoken.append, wall=lambda: 200.0)
    brief = b.build(since=90.0)
    assert brief["via"] == "local" and len(brief["sentences"]) == 3 and brief["count"] == 4
    assert model.local_calls == 1 and len(model.calls) == 1
    assert "before absence" not in model.calls[0][1]
    assert spoken == [brief["text"]] and b.get_latest()["text"] == brief["text"]


def test_brief_template_fallback(tmp_path):
    store = MilestoneStore(tmp_path / "m.sqlite")
    store.record("completed", "x", ts=100.0)
    for model in (FakeModel(fail=True), FakeModel("Too short."), None):
        brief = Briefer(store, model, wall=lambda: 200.0).build(since=90.0)
        assert brief["via"] == "template" and len(brief["sentences"]) == 3


def test_brief_listens_for_present_after_absent(tmp_path):
    bus = busmod.Bus()
    store = MilestoneStore(tmp_path / "m.sqlite")
    store.record("completed", "nightly build", ts=time.time())
    b = Briefer(store, None, bus=bus)
    b.attach()
    briefs = []
    bus.subscribe("agent/brief", lambda t, p: briefs.append(p))
    bus.publish("presence/state", {"state": PRESENT, "prev": UNKNOWN, "absent_start": None})
    bus.flush(2)
    assert b.get_latest() is None  # only an ABSENT -> PRESENT transition briefs
    bus.publish("presence/state", {"state": PRESENT, "prev": ABSENT, "absent_start": time.time() - 60})
    bus.flush(2)
    for t in b.threads:
        t.join(5)
    bus.flush(2)
    assert b.get_latest() is not None and briefs and briefs[0]["count"] == 1
    bus.stop()


# ----------------------------------------------------------------------------- TTS


def test_tts_commands_per_platform():
    which = lambda name: f"/bin/{name}"  # noqa: E731
    assert tts_command("hi", "darwin", which) == (["/bin/say", "-f", "-"], b"hi")
    argv, data = tts_command("hi", "win32", which)
    assert argv[0] == "/bin/powershell" and "SpeechSynthesizer" in argv[-1] and data == b"hi"
    assert tts_command("hi", "linux", which) == (["/bin/espeak-ng", "--stdin"], b"hi")
    only_spd = lambda name: "/bin/spd-say" if name == "spd-say" else None  # noqa: E731
    assert tts_command("hi; rm -rf /", "linux", only_spd) == (["/bin/spd-say", "-w", "--", "hi; rm -rf /"], None)
    assert tts_command("hi", "linux", lambda n: None) is None
    assert tts_command("   ", "darwin", which) is None


def test_speaker_is_opt_in_and_non_blocking():
    calls = []

    class Proc:
        stdin = None

        def wait(self, timeout=None):
            time.sleep(0.5)

    def popen(argv, **kw):
        calls.append(argv)
        return Proc()

    assert Speaker(enabled=False, popen=popen).speak("hello") is False and calls == []
    t0 = time.monotonic()
    assert Speaker(enabled=True, popen=popen, platform="darwin").speak("hello") in (True, False)
    assert time.monotonic() - t0 < 0.3


# ----------------------------------------------------------------------------- sensor privacy


def test_rppg_publishes_presence_only_and_keeps_pulse_in_memory(tmp_path):
    bus = busmod.Bus()
    got = []
    bus.subscribe("sensor/face", lambda t, p: got.append(p))
    log = get_log(tmp_path / "data")
    sensor = RPPGSensor(bus, log, enabled=True, heart=True, capture=lambda: None, fps=30.0)
    fs = 30.0
    t = np.arange(0, 10, 1 / fs)
    for i, ts in enumerate(t):
        val = 1 + 0.01 * np.sin(2 * np.pi * 1.2 * ts)
        frame = np.zeros((120, 120, 3))
        frame[:, :] = np.array([150.0, 110.0, 90.0]) * np.array([1 + 0.0033 * (val - 1) * 100, val, 1 + 0.0053 * (val - 1) * 100])
        sensor.process(frame, float(ts), face_box=(20, 20, 80, 80), found=True)
    bus.flush(2)
    live = sensor.live()
    assert live is not None and abs(live["bpm"] - 72) < 4
    assert got and all(set(p) == {"face", "approaching", "ts"} for p in got)
    log_text = (tmp_path / "data" / "engine-actions.jsonl").read_text() if (tmp_path / "data" / "engine-actions.jsonl").exists() else ""
    assert "bpm" not in log_text
    off = RPPGSensor(bus, log, enabled=True, heart=False, capture=lambda: None)
    off.set_live(80.0, 1.0, "pos")
    assert off.live() is None
    bus.stop()


def test_csi_ruview_drops_vitals_from_bus():
    bus = busmod.Bus()
    got = []
    bus.subscribe("sensor/csi", lambda t, p: got.append(p))
    s = CSISensor(bus, enabled=True, ruview_ws="ws://x", vitals_opt_in=True)
    s.feed_ruview({"presence": True, "motion_level": 0.3, "vitals": {"breathing_rate": 13, "heart_rate": 64},
                   "pose": [[0, 0]]}, ts=5.0)
    bus.flush(2)
    assert got == [{"variance": 0.3, "doppler_energy": 0.3, "presence": True, "motion": 0.3, "source": "ruview", "ts": 5.0}]
    assert s.live()["breathing"] == 13.0 and "unvalidated" in s.live()["note"]
    s2 = CSISensor(bus, enabled=True, ruview_ws="ws://x", vitals_opt_in=False)
    s2.feed_ruview({"presence": True, "breathing_rate": 13})
    assert s2.live() is None
    bus.stop()


def test_csi_availability_reasons():
    ok, reason = CSISensor().available()
    assert not ok and "O_CSI_UDP" in reason and "Apple" in reason
    assert CSISensor(udp="0.0.0.0:5500").available() == (True, "")
    assert CSISensor(udp="0.0.0.0:notaport").available()[0] is False


def test_csi_udp_source_end_to_end():
    import socket

    from o_engine.ambient.sensors.csi import ESP32UDPSource

    src = ESP32UDPSource("127.0.0.1:0")
    src.open()
    port = src._sock.getsockname()[1]
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.sendto(b"CSI_DATA,STA,aa,-40,[1 2 3 4]\n", ("127.0.0.1", port))
    got = src.read()
    src.close()
    tx.close()
    assert got is not None and np.allclose(got[0], [2 + 1j, 4 + 3j])


def test_sensor_worker_thread_feeds_presence_through_the_bus():
    from o_engine.ambient.sensors.sonar import FRAME_S, FS, SPEED_OF_SOUND, SonarSensor, chirp

    tx = chirp()
    slot = int(round(FS * FRAME_S))
    rng = np.random.default_rng(4)
    calls = {"n": 0}

    def recorder(train):
        calls["n"] += 1
        frames = len(train) // slot
        rx = np.zeros((len(train), 2))
        for f in range(frames):
            d = int(round(2 * (0.8 + f * 0.004 * calls["n"]) / SPEED_OF_SOUND * FS))
            rx[f * slot + 50:f * slot + 50 + tx.size, :] += tx[:, None]
            rx[f * slot + 50 + d:f * slot + 50 + d + tx.size, :] += 0.3 * tx[:, None]
        time.sleep(0.01)
        return rx + 0.005 * rng.standard_normal(rx.shape)

    bus = busmod.Bus()
    m = PresenceMachine(bus, ListLog(), 300, sensors_enabled=lambda: {"sonar"})
    m.attach()
    s = SonarSensor(bus, enabled=True, recorder=recorder)
    assert s.start() and s.running
    deadline = time.time() + 5
    while time.time() < deadline and m.state != PRESENT:
        time.sleep(0.02)
    s.stop()
    assert m.state == PRESENT and "sonar" in m.reason and not s.running
    bus.stop()
