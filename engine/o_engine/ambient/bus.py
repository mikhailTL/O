"""Local message bus for derived sensor features and presence events.

Default: an in-process asyncio pub/sub running on its own daemon thread (no broker to install).
``O_BUS_URL=mqtt://host:port`` (paho-mqtt) or ``redis://host:port/db`` (redis-py Pub/Sub) switches
to a broker when one is running and the client package imports; otherwise the in-process bus is
used and the reason is kept in ``Bus.reason``.

Privacy rule enforced here: a payload may not carry raw signals or biometrics (``FORBIDDEN_KEYS``);
``publish`` raises ``ValueError`` instead of sending them. Nothing on the bus is written to disk.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

TOPICS = frozenset({
    "sensor/csi", "sensor/sonar", "sensor/face", "presence/state", "agent/milestone", "agent/brief",
})
FORBIDDEN_KEYS = frozenset({
    "bpm", "heart_rate", "hr", "stress", "breathing", "breathing_rate", "respiration", "vitals", "pose",
    "keypoints", "frame", "frames", "image", "audio", "samples", "raw", "csi", "amplitude", "phase",
    "landmarks", "rgb",
})
PRIORITY_HIGH = 100
PRIORITY_NORMAL = 0
TOPIC_PREFIX = "o/"

Callback = Callable[[str, dict], None]


def check_payload(topic: str, payload: Any) -> dict:
    """A JSON-safe copy of ``payload``; ValueError for an unknown topic, non-dict or forbidden key."""
    if topic not in TOPICS:
        raise ValueError(f"unknown bus topic {topic!r}; expected one of {sorted(TOPICS)}")
    if not isinstance(payload, dict):
        raise ValueError("bus payload must be a dict")

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in FORBIDDEN_KEYS:
                    raise ValueError(f"bus payload key {k!r} is not allowed (raw signals and biometrics stay on-device)")
                walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                walk(v)

    walk(payload)
    return json.loads(json.dumps(payload, default=str))


class Bus:
    """In-process pub/sub. ``publish`` never blocks the caller; callbacks run on the bus thread in
    descending subscriber priority, and messages are dispatched in descending message priority."""

    kind = "inproc"

    def __init__(self, log: Any = None) -> None:
        self.log = log
        self.reason = ""
        self._subs: dict[str, list[tuple[int, int, Callback]]] = {}
        self._subs_lock = threading.Lock()
        self._seq = itertools.count()
        self._last: dict[str, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.PriorityQueue | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._pending = 0
        self._pending_lock = threading.Condition()
        self.errors = 0

    # ---------------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "Bus":
        with self._start_lock:
            if self.running:
                return self
            self._ready.clear()
            self._thread = threading.Thread(target=self._thread_main, name="o-bus", daemon=True)
            self._thread.start()
            self._ready.wait(5.0)
        self._connect()
        return self

    def stop(self) -> None:
        self._disconnect()
        loop = self._loop
        if loop is not None and self.running:
            loop.call_soon_threadsafe(lambda: self._queue.put_nowait((-(10**9), -1, "", None)))  # type: ignore[union-attr]
            self._thread.join(2.0)  # type: ignore[union-attr]
        self._loop = None
        self._thread = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._queue = asyncio.PriorityQueue()
        self._ready.set()
        try:
            loop.run_until_complete(self._dispatch_loop())
        finally:
            loop.close()

    async def _dispatch_loop(self) -> None:
        assert self._queue is not None
        while True:
            _prio, _seq, topic, payload = await self._queue.get()
            if payload is None and topic == "":
                return
            try:
                self._fanout(topic, payload)
            finally:
                with self._pending_lock:
                    self._pending -= 1
                    self._pending_lock.notify_all()

    def _fanout(self, topic: str, payload: dict) -> None:
        with self._subs_lock:
            subs = sorted(self._subs.get(topic, []), key=lambda s: (-s[0], s[1]))
        for _prio, _sid, cb in subs:
            try:
                cb(topic, dict(payload))
            except Exception as exc:  # noqa: BLE001 - one bad subscriber never stops the bus
                self.errors += 1
                if self.log is not None:
                    try:
                        self.log.record("note", f"bus subscriber on {topic} failed", error=f"{type(exc).__name__}: {exc}")
                    except Exception:
                        pass

    # ---------------------------------------------------------------- pub/sub
    def subscribe(self, topic: str, callback: Callback, priority: int = PRIORITY_NORMAL) -> int:
        if topic not in TOPICS:
            raise ValueError(f"unknown bus topic {topic!r}")
        sid = next(self._seq)
        with self._subs_lock:
            self._subs.setdefault(topic, []).append((int(priority), sid, callback))
        return sid

    def unsubscribe(self, sid: int) -> None:
        with self._subs_lock:
            for topic, subs in self._subs.items():
                self._subs[topic] = [s for s in subs if s[1] != sid]

    def publish(self, topic: str, payload: dict, priority: int = PRIORITY_NORMAL) -> None:
        clean = check_payload(topic, payload)
        self._last[topic] = clean
        self._transmit(topic, clean, priority)

    def last(self, topic: str) -> dict | None:
        value = self._last.get(topic)
        return dict(value) if value is not None else None

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until every published message has been delivered (tests and shutdown)."""
        deadline = time.monotonic() + timeout
        with self._pending_lock:
            while self._pending > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._pending_lock.wait(left)
        return True

    # ---------------------------------------------------------------- transport
    def _transmit(self, topic: str, payload: dict, priority: int) -> None:
        self._deliver_local(topic, payload, priority)

    def _deliver_local(self, topic: str, payload: dict, priority: int = PRIORITY_NORMAL) -> None:
        if not self.running:
            self.start()
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        with self._pending_lock:
            self._pending += 1
        item = (-int(priority), next(self._seq), topic, payload)
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def _connect(self) -> None:
        pass

    def _disconnect(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "running": self.running, "reason": self.reason}


def _wire(topic: str, payload: dict, priority: int) -> bytes:
    return json.dumps({"topic": topic, "priority": int(priority), "payload": payload}).encode()


def _unwire(data: bytes | str) -> tuple[str, dict, int] | None:
    try:
        obj = json.loads(data)
        topic = str(obj["topic"])
        payload = check_payload(topic, obj["payload"])
        return topic, payload, int(obj.get("priority", 0))
    except Exception:
        return None


class MQTTBus(Bus):
    """Publishes to an MQTT broker under ``o/<topic>``; broker messages feed local subscribers."""

    kind = "mqtt"

    def __init__(self, url: str, log: Any = None, client_factory: Callable[[], Any] | None = None) -> None:
        super().__init__(log)
        self.url = url
        parsed = urlparse(url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 1883
        if client_factory is None:
            import paho.mqtt.client as mqtt  # noqa: F401 - import guard: make_bus catches ImportError

            def client_factory() -> Any:
                try:
                    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                except AttributeError:
                    return mqtt.Client()

        self._client_factory = client_factory
        self._client: Any = None

    def _connect(self) -> None:
        if self._client is not None:
            return
        client = self._client_factory()

        def on_message(_c: Any, _u: Any, msg: Any) -> None:
            got = _unwire(msg.payload)
            if got is not None:
                self._deliver_local(*got)

        client.on_message = on_message
        client.connect(self.host, self.port)
        client.subscribe(TOPIC_PREFIX + "#")
        client.loop_start()
        self._client = client

    def _disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def _transmit(self, topic: str, payload: dict, priority: int) -> None:
        if not self.running:
            self.start()
        self._client.publish(TOPIC_PREFIX + topic, _wire(topic, payload, priority))


class RedisBus(Bus):
    """Redis Pub/Sub on channels ``o/<topic>``; a listener thread feeds local subscribers."""

    kind = "redis"

    def __init__(self, url: str, log: Any = None, client_factory: Callable[[], Any] | None = None) -> None:
        super().__init__(log)
        self.url = url
        if client_factory is None:
            import redis  # noqa: F401 - import guard: make_bus catches ImportError

            client_factory = lambda: redis.Redis.from_url(url)  # noqa: E731
        self._client_factory = client_factory
        self._client: Any = None
        self._pubsub: Any = None
        self._listener: threading.Thread | None = None
        self._stop = threading.Event()

    def _connect(self) -> None:
        if self._client is not None:
            return
        self._client = self._client_factory()
        self._pubsub = self._client.pubsub()
        self._pubsub.psubscribe(TOPIC_PREFIX + "*")
        self._stop.clear()
        self._listener = threading.Thread(target=self._listen, name="o-bus-redis", daemon=True)
        self._listener.start()

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._pubsub.get_message(timeout=0.5)
            except Exception:
                time.sleep(0.5)
                continue
            if msg and msg.get("type") in ("message", "pmessage"):
                got = _unwire(msg.get("data") or b"")
                if got is not None:
                    self._deliver_local(*got)

    def _disconnect(self) -> None:
        self._stop.set()
        if self._pubsub is not None:
            try:
                self._pubsub.close()
            except Exception:
                pass
        self._client = None
        self._pubsub = None

    def _transmit(self, topic: str, payload: dict, priority: int) -> None:
        if not self.running:
            self.start()
        self._client.publish(TOPIC_PREFIX + topic, _wire(topic, payload, priority))


def make_bus(url: str = "", log: Any = None) -> Bus:
    """The bus ``O_BUS_URL`` asks for; the in-process bus (with ``reason``) when that cannot be built."""
    url = (url or "").strip()
    if not url:
        return Bus(log)
    scheme = urlparse(url).scheme.lower()
    try:
        if scheme in ("mqtt", "mqtts", "tcp"):
            return MQTTBus(url, log)
        if scheme in ("redis", "rediss"):
            return RedisBus(url, log)
        reason = f"unsupported O_BUS_URL scheme {scheme!r}; using the in-process bus"
    except ImportError as exc:
        reason = f"{scheme} client not installed ({exc.name or exc}); using the in-process bus"
    bus = Bus(log)
    bus.reason = reason
    if log is not None:
        try:
            log.record("note", reason, bus_url_scheme=scheme)
        except Exception:
            pass
    return bus
