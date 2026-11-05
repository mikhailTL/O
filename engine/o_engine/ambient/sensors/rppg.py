"""Webcam presence, approach and (opt-in) pulse.

Presence/approach: MediaPipe face landmarks (``O_FACE_MODEL`` = a ``face_landmarker.task`` file for
the MediaPipe Tasks API, or the legacy ``mp.solutions.face_mesh`` when that exists; mediapipe 0.10.32
no longer has it), else OpenCV's bundled Haar cascade (``cv2.CascadeClassifier``, which OpenCV 5
dropped from opencv-python / opencv-python-headless and kept only in opencv-contrib-python), else the
BlazeFace ONNX detector bundled with open-rppg (``rppg.main.FaceDetector``, ``weights/blaze_face.onnx``). Approach is a rising trend of the face-box area.

Pulse (only when the heart setting is on): ``KegangWangCCNU/open-rppg`` (PyPI ``open-rppg`` 0.1.1, MIT)
when it imports, else the POS algorithm (Wang et al. 2017, as in ``ubicomplab/rPPG-Toolbox`` and
``phuselab/pyVHR``) on forehead/cheek ROI means, in NumPy.

open-rppg 0.1.1 API as installed (``rppg/main.py``): ``rppg.Model(model='FacePhys.rlap')`` validates
the name against ``rppg.supported_models`` (``ValueError`` otherwise); ``with model:`` starts its
inference thread; ``model.update_frame(rgb_uint8_HxWx3, ts)`` runs its BlazeFace detector (every 5th
frame, Kalman-smoothed) and queues the face crop; ``model.box`` is ``[[y0, y1], [x0, x1]]`` pixels or
None and ``model.hasface`` is truthy while a face is tracked; ``model.hr(start=-10, return_hrv=False)``
returns ``{'hr', 'SQI', 'hrv', 'latency'}`` (``hr`` may be None) or None before any signal. Its own
``video_capture(0)`` is not used: it blocks forever on its preview lock when the camera yields no frame,
and its ``preview`` is a blocking generator. O feeds frames from its own OpenCV capture instead.
It imports ``pkg_resources``, which setuptools >= 81 no longer ships; the ambient extra pins
``setuptools<81`` and ``open_rppg_status`` reports the missing module instead of crashing.

Privacy: frames are reduced to three ROI means and a face box, then dropped. Only
``{face, approaching, ts}`` reaches the bus. Pulse and the stress proxy live in one in-memory value
(``live()``), shown to the opted-in user and never published, logged, stored, or sent to a model.
"""
from __future__ import annotations

import collections
import importlib.util
import os
import threading
import time
from typing import Any

import numpy as np

from . import SensorWorker

DEFAULT_RPPG_MODEL = "FacePhys.rlap"
HR_BAND_HZ = (0.7, 4.0)
POS_WINDOW_S = 1.6
PULSE_WINDOW_S = 10.0
APPROACH_WINDOW_S = 1.5
APPROACH_GROWTH = 0.15
EMIT_EVERY_S = 0.2
HEART_NOTE = "Pulse and the stress proxy are estimates from your camera, not medical measurements; shown live only."

# MediaPipe face-mesh landmark indices for the three skin ROIs used by POS.
FOREHEAD = (10, 67, 69, 104, 108, 109, 151, 299, 337, 338)
LEFT_CHEEK = (50, 101, 116, 117, 118, 123, 147, 187, 205)
RIGHT_CHEEK = (280, 330, 345, 346, 347, 352, 376, 411, 425)


# ------------------------------------------------------------------------------- signal math


def pos_signal(rgb: np.ndarray, fs: float, window_s: float = POS_WINDOW_S) -> np.ndarray:
    """POS pulse signal from an (N, 3) series of RGB ROI means sampled at ``fs`` Hz."""
    c = np.asarray(rgb, dtype=float).T
    n = c.shape[1]
    win = max(2, int(round(window_s * fs)))
    h = np.zeros(n)
    proj = np.array([[0.0, 1.0, -1.0], [-2.0, 1.0, 1.0]])
    for start in range(0, n - win + 1):
        block = c[:, start:start + win]
        mean = block.mean(axis=1, keepdims=True)
        if np.any(mean == 0):
            continue
        s = proj @ (block / mean)
        sd1 = s[1].std()
        hh = s[0] + (s[0].std() / sd1 if sd1 > 0 else 0.0) * s[1]
        h[start:start + win] += hh - hh.mean()
    return h


def dominant_bpm(signal: np.ndarray, fs: float, band: tuple[float, float] = HR_BAND_HZ) -> float | None:
    """Peak frequency of ``signal`` inside ``band`` Hz, in beats per minute (zero-padded FFT)."""
    x = np.asarray(signal, dtype=float)
    if x.size < int(fs * 2) or fs <= 0:
        return None
    x = x - np.polyval(np.polyfit(np.arange(x.size), x, 1), np.arange(x.size))
    nfft = max(4096, int(2 ** np.ceil(np.log2(x.size * 8))))
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size), nfft))
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    sel = (freqs >= band[0]) & (freqs <= band[1])
    if not sel.any() or spec[sel].max() <= 0:
        return None
    return float(freqs[sel][np.argmax(spec[sel])] * 60.0)


def pos_bpm(rgb: np.ndarray, fs: float) -> float | None:
    return dominant_bpm(pos_signal(rgb, fs), fs)


def stress_proxy(bpm: float, baseline: float | None) -> float:
    """0..1 arousal proxy from pulse elevation over the user's own baseline (not a medical measure)."""
    if baseline is None or baseline <= 0:
        return 0.0
    return float(np.clip((bpm - baseline) / 40.0, 0.0, 1.0))


def roi_means(frame: np.ndarray, landmarks: np.ndarray | None = None, box: tuple[int, int, int, int] | None = None) -> np.ndarray | None:
    """Mean RGB over forehead + both cheeks. ``landmarks`` are (468+, 2) normalized x,y (MediaPipe);
    without them the ROIs are fixed fractions of the face ``box`` (x, y, w, h in pixels)."""
    img = np.asarray(frame)
    hgt, wid = img.shape[:2]
    rects: list[tuple[int, int, int, int]] = []
    if landmarks is not None and len(landmarks) >= 468:
        pts = np.asarray(landmarks)[:, :2] * np.array([wid, hgt])
        for idx in (FOREHEAD, LEFT_CHEEK, RIGHT_CHEEK):
            p = pts[list(idx)]
            x0, y0 = np.floor(p.min(axis=0)).astype(int)
            x1, y1 = np.ceil(p.max(axis=0)).astype(int)
            rects.append((x0, y0, x1, y1))
    elif box is not None:
        x, y, w, h = box
        rects = [(x + int(0.3 * w), y + int(0.08 * h), x + int(0.7 * w), y + int(0.22 * h)),
                 (x + int(0.15 * w), y + int(0.5 * h), x + int(0.35 * w), y + int(0.68 * h)),
                 (x + int(0.65 * w), y + int(0.5 * h), x + int(0.85 * w), y + int(0.68 * h))]
    else:
        return None
    sums = np.zeros(3)
    count = 0
    for x0, y0, x1, y1 in rects:
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(wid, x1), min(hgt, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        patch = img[y0:y1, x0:x1, :3].reshape(-1, 3).astype(float)
        sums += patch.sum(axis=0)
        count += patch.shape[0]
    return sums / count if count else None


class ApproachDetector:
    """``update(area, ts) -> bool``: True while the face-box area grows by ``growth`` across the window."""

    def __init__(self, window_s: float = APPROACH_WINDOW_S, growth: float = APPROACH_GROWTH) -> None:
        self.window_s = window_s
        self.growth = growth
        self._pts: collections.deque = collections.deque()

    def reset(self) -> None:
        self._pts.clear()

    def update(self, area: float, ts: float) -> bool:
        self._pts.append((float(ts), float(area)))
        while self._pts and ts - self._pts[0][0] > self.window_s:
            self._pts.popleft()
        if len(self._pts) < 4:
            return False
        t = np.array([p[0] for p in self._pts])
        a = np.array([p[1] for p in self._pts])
        if t[-1] - t[0] < self.window_s * 0.5:
            return False
        slope, icpt = np.polyfit(t - t[0], a, 1)
        start = icpt
        end = icpt + slope * (t[-1] - t[0])
        return bool(slope > 0 and start > 0 and (end - start) / start >= self.growth)


# ------------------------------------------------------------------------------- face finders


class FaceFinder:
    """``find(rgb_frame) -> (box | None, landmarks | None)`` via MediaPipe or OpenCV."""

    def __init__(self, model_path: str = "") -> None:
        self.model_path = model_path or os.environ.get("O_FACE_MODEL", "")
        self.kind = "none"
        self._impl: Any = None

    @staticmethod
    def status(model_path: str = "") -> tuple[bool, str, str]:
        model_path = model_path or os.environ.get("O_FACE_MODEL", "")
        if importlib.util.find_spec("cv2") is None:
            return False, "opencv-python is not installed (pip extra 'ambient')", "none"
        if importlib.util.find_spec("mediapipe") is not None:
            if model_path and os.path.isfile(model_path):
                return True, "", "mediapipe-tasks"
            try:
                import mediapipe as mp

                if hasattr(mp, "solutions"):
                    return True, "", "mediapipe-legacy"
            except Exception:
                pass
        try:
            import cv2

            if hasattr(cv2, "CascadeClassifier"):
                return True, "", "opencv-haar"
        except Exception as exc:  # noqa: BLE001
            return False, f"opencv failed to import ({type(exc).__name__}: {exc})", "none"
        if open_rppg_status()[0]:
            return True, "", "blazeface"
        return False, ("no face detector: OpenCV 5 has no CascadeClassifier outside opencv-contrib-python; install "
                       "opencv-contrib-python or open-rppg, or set O_FACE_MODEL to a MediaPipe face_landmarker.task"), "none"

    def open(self) -> None:
        _, _, kind = self.status(self.model_path)
        self.kind = kind
        if kind == "mediapipe-tasks":
            import mediapipe as mp
            from mediapipe.tasks.python import vision
            from mediapipe.tasks.python.core.base_options import BaseOptions

            opts = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=self.model_path), num_faces=1)
            self._impl = (mp, vision.FaceLandmarker.create_from_options(opts))
        elif kind == "mediapipe-legacy":
            import mediapipe as mp

            self._impl = mp.solutions.face_mesh.FaceMesh(max_num_faces=1, refine_landmarks=False)
        elif kind == "opencv-haar":
            import cv2

            self._impl = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        elif kind == "blazeface":
            from importlib.resources import files

            from rppg.main import FaceDetector  # type: ignore[import-not-found]

            self._impl = FaceDetector(str(files("rppg").joinpath("weights").joinpath("blaze_face.onnx")))

    def find(self, rgb: np.ndarray) -> tuple[tuple[int, int, int, int] | None, np.ndarray | None]:
        h, w = rgb.shape[:2]
        if self.kind == "mediapipe-tasks":
            mp, lm = self._impl
            res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
            faces = getattr(res, "face_landmarks", None) or []
            if not faces:
                return None, None
            pts = np.array([[p.x, p.y] for p in faces[0]])
        elif self.kind == "mediapipe-legacy":
            res = self._impl.process(rgb)
            faces = getattr(res, "multi_face_landmarks", None) or []
            if not faces:
                return None, None
            pts = np.array([[p.x, p.y] for p in faces[0].landmark])
        elif self.kind == "opencv-haar":
            import cv2

            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            found = self._impl.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5, minSize=(60, 60))
            if len(found) == 0:
                return None, None
            x, y, bw, bh = max(found, key=lambda r: r[2] * r[3])
            return (int(x), int(y), int(bw), int(bh)), None
        elif self.kind == "blazeface":
            found = self._impl.detect(np.ascontiguousarray(rgb[..., :3].astype(np.uint8)))
            if not found:
                return None, None
            x0, y0, x1, y1 = np.asarray(found[0][0], dtype=float)
            x0, y0 = max(0.0, x0), max(0.0, y0)
            x1, y1 = min(float(w), x1), min(float(h), y1)
            if x1 <= x0 or y1 <= y0:
                return None, None
            return (int(x0), int(y0), int(x1 - x0), int(y1 - y0)), None
        else:
            return None, None
        x0, y0 = (pts.min(axis=0) * [w, h]).astype(int)
        x1, y1 = (pts.max(axis=0) * [w, h]).astype(int)
        return (int(x0), int(y0), int(x1 - x0), int(y1 - y0)), pts


def open_rppg_status() -> tuple[bool, str]:
    if importlib.util.find_spec("rppg") is None:
        return False, "open-rppg is not installed; using the POS fallback"
    if importlib.util.find_spec("pkg_resources") is None:
        return False, ("open-rppg needs pkg_resources (setuptools<81; pip install 'setuptools<81'); "
                       "using the POS fallback")
    return True, ""


def open_rppg_box(model: Any) -> tuple[int, int, int, int] | None:
    """open-rppg's tracked face as (x, y, w, h), from ``model.box`` = [[y0, y1], [x0, x1]]."""
    box = getattr(model, "box", None)
    if box is None or not getattr(model, "hasface", 0):
        return None
    try:
        (y0, y1), (x0, x1) = np.asarray(box, dtype=float).reshape(2, 2)
    except Exception:
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return int(x0), int(y0), int(x1 - x0), int(y1 - y0)


def open_rppg_hr(model: Any, window_s: float = PULSE_WINDOW_S) -> float | None:
    """Heart rate from ``model.hr(start=-window, return_hrv=False)`` or None."""
    try:
        res = model.hr(start=-window_s, return_hrv=False)
    except TypeError:
        res = model.hr(start=-window_s)
    value = res.get("hr") if isinstance(res, dict) else res
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and np.isfinite(value) and value > 0 else None


# ------------------------------------------------------------------------------------- worker


class RPPGSensor(SensorWorker):
    """Publishes ``sensor/face`` ``{face, approaching, ts}``; pulse stays in ``live()``."""

    name = "camera"
    topic = "sensor/face"
    description = ("Webcam presence: detects whether a face is in front of the screen and moving closer. "
                   "Frames are processed live and never stored. " + HEART_NOTE)

    def __init__(self, bus: Any = None, log: Any = None, enabled: bool = False, heart: bool = False,
                 camera_index: int = 0, rppg_model: str = DEFAULT_RPPG_MODEL, capture: Any = None,
                 finder: Any = None, fps: float = 30.0, model_factory: Any = None) -> None:
        super().__init__(bus, log, enabled)
        self.heart = bool(heart)
        self.camera_index = camera_index
        self.rppg_model = rppg_model or DEFAULT_RPPG_MODEL
        self.fps = fps
        self._capture = capture  # callable() -> RGB frame or None; tests inject one
        self._finder = finder
        self._model_factory = model_factory  # name -> open-rppg Model; tests inject one
        self.approach = ApproachDetector()
        self._rgb: collections.deque = collections.deque(maxlen=int(PULSE_WINDOW_S * fps))
        self._rgb_ts: collections.deque = collections.deque(maxlen=int(PULSE_WINDOW_S * fps))
        self._live: dict[str, Any] | None = None
        self._live_lock = threading.Lock()
        self._baseline: collections.deque = collections.deque(maxlen=60)
        self._last_emit = 0.0
        self._last_pulse = 0.0
        self.pulse_via = "none"

    def available(self) -> tuple[bool, str]:
        if self._capture is not None:
            return True, ""
        ok, reason, _ = FaceFinder.status()
        return ok, reason

    def heart_status(self) -> dict[str, Any]:
        ok, reason = open_rppg_status()
        return {"enabled": self.heart, "via": "open-rppg" if ok else "pos", "model": self.rppg_model if ok else None,
                "reason": reason, "note": HEART_NOTE}

    # ---- per-frame processing (worker thread or tests) -----------------------------------------
    def process(self, rgb: np.ndarray, ts: float, face_box: Any = None, landmarks: Any = None,
                found: bool = False) -> dict[str, Any] | None:
        """One frame -> maybe a bus payload. ``found`` means box/landmarks came from the caller."""
        if not found:
            finder = self._finder
            face_box, landmarks = finder.find(rgb) if finder is not None else (None, None)
        h, w = rgb.shape[:2]
        face = face_box is not None
        approaching = False
        if face:
            approaching = self.approach.update(face_box[2] * face_box[3] / float(w * h), ts)
            if self.heart and self.pulse_via != "open-rppg":
                means = roi_means(rgb, landmarks, face_box)
                if means is not None:
                    self._rgb.append(means)
                    self._rgb_ts.append(ts)
                    self._update_pulse(ts)
        else:
            self.approach.reset()
        if ts - self._last_emit < EMIT_EVERY_S and not approaching:
            return None
        self._last_emit = ts
        payload = {"face": face, "approaching": approaching, "ts": ts}
        self.emit(payload)
        return payload

    def _update_pulse(self, ts: float) -> None:
        if ts - self._last_pulse < 1.0 or len(self._rgb) < int(self.fps * 5):
            return
        self._last_pulse = ts
        t = np.asarray(self._rgb_ts)
        dt = np.diff(t)
        fs = float(1.0 / np.median(dt[dt > 0])) if (dt > 0).any() else self.fps
        bpm = pos_bpm(np.vstack(self._rgb), fs)
        if bpm is not None:
            self.pulse_via = "pos"
            self.set_live(bpm, ts, "pos")

    def set_live(self, bpm: float, ts: float, via: str) -> None:
        if not self.heart:
            return
        self._baseline.append(bpm)
        baseline = float(np.median(self._baseline)) if len(self._baseline) >= 10 else None
        with self._live_lock:
            self._live = {"bpm": round(float(bpm), 1), "stress": round(stress_proxy(bpm, baseline), 3),
                          "ts": ts, "via": via, "note": HEART_NOTE}

    def live(self) -> dict[str, Any] | None:
        """The current pulse/stress value (memory only; None unless the heart setting is on)."""
        if not self.heart:
            return None
        with self._live_lock:
            return dict(self._live) if self._live else None

    def stop(self, timeout: float = 2.0) -> None:
        super().stop(timeout)
        with self._live_lock:
            self._live = None
        self._rgb.clear()
        self._rgb_ts.clear()

    # ---- capture loops ---------------------------------------------------------------------------
    def _run_loop(self) -> None:
        """Face finder (+ POS) from the first frame; open-rppg takes over once its model has loaded
        (FacePhys takes ~25 s to build on a laptop CPU, so presence must not wait for it)."""
        frames, release = self._frame_source()
        loader: threading.Thread | None = None
        holder: dict[str, Any] = {}
        if self.heart and (self._model_factory is not None or open_rppg_status()[0]):
            def load() -> None:
                try:
                    holder["model"] = self._make_model()
                except Exception as exc:  # noqa: BLE001
                    holder["error"] = exc

            loader = threading.Thread(target=load, name="o-rppg-load", daemon=True)
            loader.start()
        try:
            finder_ok = True
            if self._finder is None:
                try:
                    finder = FaceFinder()
                    finder.open()
                    if finder.kind == "none":
                        raise RuntimeError(FaceFinder.status()[1] or "no face detector")
                    self._finder = finder
                except Exception as exc:  # noqa: BLE001
                    if loader is None:
                        raise
                    finder_ok = False
                    self.last_error = f"face finder unavailable ({type(exc).__name__}: {exc}); waiting for open-rppg"
            while not self._stop.is_set():
                if loader is not None and not loader.is_alive():
                    loader = None
                    if "model" in holder:
                        try:
                            self._run_open_rppg(frames, holder.pop("model"))
                            return
                        except Exception as exc:  # noqa: BLE001 - fall back to the face finder + POS
                            self.last_error = f"open-rppg failed ({type(exc).__name__}: {exc}); using POS"
                    else:
                        exc = holder.get("error")
                        self.last_error = f"open-rppg failed to load ({type(exc).__name__}: {exc}); using POS"
                    self.pulse_via = "pos"
                    if not finder_ok:
                        raise RuntimeError(self.last_error)
                frame = frames()
                if frame is None:
                    time.sleep(0.05)
                    continue
                if finder_ok:
                    self.process(np.asarray(frame), time.time())
                del frame
        finally:
            release()

    def _frame_source(self) -> tuple[Any, Any]:
        """(read() -> RGB uint8 frame | None, release())."""
        if self._capture is not None:
            return self._capture, lambda: None
        import cv2

        cap = cv2.VideoCapture(self.camera_index)

        def read() -> Any:
            ok, bgr = cap.read()
            if not ok:
                time.sleep(0.1)
                return None
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        return read, cap.release

    def _make_model(self) -> Any:
        if self._model_factory is not None:
            return self._model_factory(self.rppg_model)
        import rppg

        supported = list(getattr(rppg, "supported_models", []) or [])
        name = self.rppg_model if (not supported or self.rppg_model in supported) else DEFAULT_RPPG_MODEL
        if name != self.rppg_model:
            self.last_error = f"open-rppg has no model {self.rppg_model!r}; using {name}"
        return rppg.Model(name)

    def _run_open_rppg(self, frames: Any, model: Any = None) -> None:
        model = self._make_model() if model is None else model
        self.pulse_via = "open-rppg"
        if self.last_error.startswith("face finder unavailable"):
            self.last_error = ""
        self._rgb.clear()
        self._rgb_ts.clear()
        with model:
            last_hr = 0.0
            while not self._stop.is_set():
                frame = frames()
                if frame is None:
                    time.sleep(0.05)
                    continue
                rgb = np.ascontiguousarray(np.asarray(frame, dtype=np.uint8)[..., :3])
                now = time.time()
                model.update_frame(rgb, now)
                self.process(rgb, now, face_box=open_rppg_box(model), landmarks=None, found=True)
                del rgb, frame
                if now - last_hr >= 1.0:
                    last_hr = now
                    try:
                        bpm = open_rppg_hr(model)
                    except Exception:
                        bpm = None
                    if bpm is not None:
                        self.set_live(bpm, now, f"open-rppg:{self.rppg_model}")
