"""Webcam presence, approach and (opt-in) pulse.

Presence/approach: MediaPipe face landmarks (``O_FACE_MODEL`` = a ``face_landmarker.task`` file for
the MediaPipe Tasks API, or the legacy ``mp.solutions.face_mesh`` when that exists), else OpenCV's
bundled Haar cascade. Approach is a rising trend of the face-box area.

Pulse (only when the heart setting is on): ``KegangWangCCNU/open-rppg`` (MIT; FacePhys by default,
also PhysMamba, RhythmMamba, PhysFormer, EfficientPhys, TSCAN; ``rppg.Model()``,
``model.video_capture(0)``, ``model.preview``, ``model.hr(start=-10)``) when it imports, else the
POS algorithm (Wang et al. 2017, as in ``ubicomplab/rPPG-Toolbox`` and ``phuselab/pyVHR``) on
forehead/cheek ROI means, in NumPy.

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
        return True, "", "opencv-haar"

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
        else:
            return None, None
        x0, y0 = (pts.min(axis=0) * [w, h]).astype(int)
        x1, y1 = (pts.max(axis=0) * [w, h]).astype(int)
        return (int(x0), int(y0), int(x1 - x0), int(y1 - y0)), pts


def open_rppg_status() -> tuple[bool, str]:
    if importlib.util.find_spec("rppg") is None:
        return False, "open-rppg is not installed; using the POS fallback"
    return True, ""


# ------------------------------------------------------------------------------------- worker


class RPPGSensor(SensorWorker):
    """Publishes ``sensor/face`` ``{face, approaching, ts}``; pulse stays in ``live()``."""

    name = "camera"
    topic = "sensor/face"
    description = ("Webcam presence: detects whether a face is in front of the screen and moving closer. "
                   "Frames are processed live and never stored. " + HEART_NOTE)

    def __init__(self, bus: Any = None, log: Any = None, enabled: bool = False, heart: bool = False,
                 camera_index: int = 0, rppg_model: str = DEFAULT_RPPG_MODEL, capture: Any = None,
                 finder: Any = None, fps: float = 30.0) -> None:
        super().__init__(bus, log, enabled)
        self.heart = bool(heart)
        self.camera_index = camera_index
        self.rppg_model = rppg_model or DEFAULT_RPPG_MODEL
        self.fps = fps
        self._capture = capture  # callable() -> RGB frame or None; tests inject one
        self._finder = finder
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
        if self._finder is None:
            self._finder = FaceFinder()
            self._finder.open()
        if self._capture is not None:
            while not self._stop.is_set():
                frame = self._capture()
                if frame is None:
                    time.sleep(0.05)
                    continue
                self.process(np.asarray(frame), time.time())
            return
        if self.heart and open_rppg_status()[0]:
            try:
                self._run_open_rppg()
                return
            except Exception as exc:  # noqa: BLE001 - fall back to OpenCV + POS
                self.last_error = f"open-rppg failed ({type(exc).__name__}: {exc}); using POS"
                self.pulse_via = "pos"
        self._run_opencv()

    def _run_opencv(self) -> None:
        import cv2

        cap = cv2.VideoCapture(self.camera_index)
        try:
            while not self._stop.is_set():
                ok, bgr = cap.read()
                if not ok:
                    time.sleep(0.1)
                    continue
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                del bgr
                self.process(rgb, time.time())
                del rgb
        finally:
            cap.release()

    def _run_open_rppg(self) -> None:
        import rppg

        try:
            model = rppg.Model(self.rppg_model)
        except TypeError:
            model = rppg.Model()
        self.pulse_via = "open-rppg"
        with model.video_capture(self.camera_index):
            last_hr = 0.0
            while not self._stop.is_set():
                preview = getattr(model, "preview", None)
                frame = preview[0] if isinstance(preview, tuple) else preview
                now = time.time()
                if frame is not None:
                    self.process(np.asarray(frame), now)
                if now - last_hr >= 1.0:
                    last_hr = now
                    try:
                        hr = model.hr(start=-10)
                        value = hr.get("hr") if isinstance(hr, dict) else hr
                        if value:
                            self.set_live(float(value), now, f"open-rppg:{self.rppg_model}")
                    except Exception:
                        pass
                time.sleep(1.0 / self.fps)
