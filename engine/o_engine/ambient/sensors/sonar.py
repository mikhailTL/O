"""Acoustic FMCW sonar: nearest-reflector range and micro-Doppler motion from the speaker and a
stereo microphone pair.

Implemented from first principles in NumPy. Pattern sources: ``yctung/LibAcousticSensing``
(chirp design, frame-synchronous matched filtering against the direct path) and
``KevinsWang/fmcw-acoustic-gesture-recognition`` (frame-to-frame range-profile differencing as the
motion feature); also ``ksiddharth/LLAP``-style acoustic tracking.

A linear 18-22 kHz chirp (inaudible to most adults, Hann-tapered) repeats every ``frame_s``. Each
recorded frame is matched-filtered against the transmitted chirp; the strongest early peak is the
direct speaker-to-mic path, which removes playback/record latency, and reflector range is
``c * (lag - direct_lag) / fs / 2``. Motion is the energy of the change of the complex range profile
between consecutive frames (static clutter cancels). Audio is processed block by block and
discarded; nothing is stored. ``sounddevice`` is used when it imports and PortAudio is present.
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np

from . import SensorWorker

FS = 48000
F0 = 18000.0
F1 = 22000.0
CHIRP_S = 0.010
FRAME_S = 0.040
SPEED_OF_SOUND = 343.0
MIN_RANGE_M = 0.15
MAX_RANGE_M = 3.0
FRAMES_PER_BLOCK = 8


def chirp(fs: int = FS, f0: float = F0, f1: float = F1, duration: float = CHIRP_S) -> np.ndarray:
    """A linear up-chirp ``f0 -> f1`` over ``duration`` seconds, Hann-tapered, float32 in [-1, 1]."""
    n = int(round(fs * duration))
    t = np.arange(n) / fs
    k = (f1 - f0) / duration
    sig = np.sin(2 * np.pi * (f0 * t + 0.5 * k * t ** 2))
    return (sig * np.hanning(n)).astype(np.float32)


def chirp_train(frames: int, fs: int = FS, frame_s: float = FRAME_S, **kw: Any) -> np.ndarray:
    """``frames`` chirps, one at the start of every ``frame_s`` slot (the playback buffer)."""
    tx = chirp(fs, **kw)
    slot = int(round(fs * frame_s))
    out = np.zeros(slot * frames, dtype=np.float32)
    for i in range(frames):
        out[i * slot: i * slot + tx.size] = tx
    return out


def matched_filter(rx: np.ndarray, tx: np.ndarray) -> np.ndarray:
    """Complex analytic cross-correlation of ``rx`` with ``tx`` for lags 0..len(rx)-1 (FFT based)."""
    rx = np.asarray(rx, dtype=float)
    tx = np.asarray(tx, dtype=float)
    n = int(2 ** np.ceil(np.log2(rx.size + tx.size)))
    spec = np.fft.fft(rx, n) * np.conj(np.fft.fft(tx, n))
    spec[n // 2 + 1:] = 0  # analytic signal: keep positive frequencies only
    spec[1: n // 2] *= 2
    return np.fft.ifft(spec)[: rx.size]


def range_profile(rx: np.ndarray, tx: np.ndarray, fs: int = FS) -> tuple[np.ndarray, np.ndarray, int]:
    """(complex profile, ranges in metres relative to the direct path, direct-path lag index).

    ``rx`` may be mono (n,) or multi-channel (n, ch); channels are combined by mean magnitude
    alignment on the shared direct path.
    """
    rx = np.asarray(rx, dtype=float)
    chans = rx[:, None] if rx.ndim == 1 else rx
    profiles = np.stack([matched_filter(chans[:, c], tx) for c in range(chans.shape[1])], axis=1)
    mag = np.abs(profiles).mean(axis=1)
    direct = int(np.argmax(mag))
    prof = profiles.mean(axis=1)[direct:]
    ranges = np.arange(prof.size) * SPEED_OF_SOUND / fs / 2.0
    return prof, ranges, direct


def nearest_reflector(rx: np.ndarray, tx: np.ndarray, fs: int = FS, min_range: float = MIN_RANGE_M,
                      max_range: float = MAX_RANGE_M, rel_threshold: float = 0.1) -> float | None:
    """Range in metres of the first reflector peak beyond ``min_range`` (None when there is none)."""
    prof, ranges, _ = range_profile(rx, tx, fs)
    env = np.abs(prof)
    if env.size == 0 or env[0] <= 0:
        return None
    sel = np.where((ranges >= min_range) & (ranges <= max_range))[0]
    if sel.size == 0:
        return None
    seg = env[sel]
    thr = rel_threshold * env[0]
    peaks = [i for i in range(1, seg.size - 1) if seg[i] >= seg[i - 1] and seg[i] > seg[i + 1] and seg[i] >= thr]
    if not peaks:
        return None
    return float(ranges[sel[peaks[0]]])


def frame_profiles(block: np.ndarray, tx: np.ndarray, fs: int = FS, frame_s: float = FRAME_S,
                   max_range: float = MAX_RANGE_M) -> np.ndarray:
    """Range profiles (frames, bins) of a recorded block of back-to-back chirp frames."""
    slot = int(round(fs * frame_s))
    bins = int(np.ceil(2 * max_range / SPEED_OF_SOUND * fs))
    block = np.asarray(block, dtype=float)
    n = block.shape[0] // slot
    out = []
    for i in range(n):
        prof, _, _ = range_profile(block[i * slot:(i + 1) * slot], tx, fs)
        p = np.zeros(bins, dtype=complex)
        p[: min(bins, prof.size)] = prof[:bins]
        out.append(p)
    return np.array(out) if out else np.zeros((0, bins), dtype=complex)


def motion_energy(profiles: np.ndarray, min_bin: int = 0) -> float:
    """Micro-Doppler motion: mean energy of frame-to-frame profile change over profile energy."""
    profiles = np.asarray(profiles)
    if profiles.shape[0] < 2:
        return 0.0
    p = profiles[:, min_bin:]
    diff = np.abs(np.diff(p, axis=0)) ** 2
    ref = np.mean(np.abs(p) ** 2) + 1e-12
    return float(diff.mean() / ref)


def sounddevice_status() -> tuple[bool, str]:
    try:
        import sounddevice as sd  # noqa: F401
    except ImportError:
        return False, "sounddevice is not installed (pip extra 'ambient')"
    except OSError as exc:
        return False, f"PortAudio unavailable: {exc}"
    try:
        dev = sd.query_devices(kind="input")
        out = sd.query_devices(kind="output")
    except Exception as exc:  # noqa: BLE001
        return False, f"no audio device: {exc}"
    if int(dev.get("max_input_channels", 0)) < 2:
        return False, "the default input has fewer than 2 channels (stereo microphones needed)"
    if int(out.get("max_output_channels", 0)) < 1:
        return False, "no audio output for the chirp"
    if float(dev.get("default_samplerate", FS)) < 2 * F1:
        return False, "the input sample rate is too low for 22 kHz"
    return True, ""


class SonarSensor(SensorWorker):
    """Publishes ``sensor/sonar`` ``{range_m, motion, ts}``."""

    name = "sonar"
    topic = "sensor/sonar"
    description = ("Ultrasonic sonar (18-22 kHz chirps from your speaker, heard by your microphones) to sense "
                   "motion near the screen. Audio is processed live and never recorded or stored.")

    def __init__(self, bus: Any = None, log: Any = None, enabled: bool = False, recorder: Any = None,
                 fs: int = FS, frames_per_block: int = FRAMES_PER_BLOCK) -> None:
        super().__init__(bus, log, enabled)
        self.fs = fs
        self.frames_per_block = frames_per_block
        self.tx = chirp(fs)
        self.train = chirp_train(frames_per_block, fs)
        self._recorder = recorder  # callable(train) -> (n, 2) block; tests inject one

    def available(self) -> tuple[bool, str]:
        if self._recorder is not None:
            return True, ""
        return sounddevice_status()

    def _record(self) -> np.ndarray:
        if self._recorder is not None:
            return np.asarray(self._recorder(self.train))
        import sounddevice as sd

        block = sd.playrec(self.train, samplerate=self.fs, channels=2, dtype="float32", blocking=True)
        return np.asarray(block)

    def process(self, block: np.ndarray, ts: float | None = None) -> dict[str, Any]:
        """Features of one recorded block; the block itself is dropped by the caller."""
        profiles = frame_profiles(block, self.tx, self.fs)
        min_bin = int(MIN_RANGE_M * 2 / SPEED_OF_SOUND * self.fs)
        motion = motion_energy(profiles, min_bin)
        slot = int(round(self.fs * FRAME_S))
        rng = nearest_reflector(np.asarray(block)[:slot], self.tx, self.fs) if np.asarray(block).shape[0] >= slot else None
        return {"range_m": rng, "motion": motion, "ts": time.time() if ts is None else ts}

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            block = self._record()
            payload = self.process(block)
            del block
            self.emit(payload)
