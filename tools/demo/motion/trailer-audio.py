#!/usr/bin/env python3
"""Sound for the O AI trailer: synthesized from the film's cue list (the sidecar JSON's facts.sfx), no samples.

    python3 tools/demo/motion/trailer-audio.py docs/videos/o-trailer.json out.wav

Cues: tick (clock/strobe click), flash (white-frame hit), whoosh (smear), flip (panel), key (typing), riser (d ms),
burn (d ms crackle), impact (sub thump), chord (the lockup). Under it: a sub drone and a clock that stops dead at the
last impact, the way the reference's music cuts at its climax.
"""
import json, sys, math
import numpy as np

SR = 48000

def env(n, a, d, sustain=0.0, r=None):
    """attack/decay(/release) envelope over n samples (a, d, r in samples)."""
    e = np.zeros(n)
    a = max(1, min(a, n)); e[:a] = np.linspace(0, 1, a)
    d = min(d, n - a)
    if d > 0: e[a:a + d] = np.linspace(1, sustain, d)
    if a + d < n: e[a + d:] = sustain
    if r: r = min(r, n); e[-r:] *= np.linspace(1, 0, r)
    return e

def noise(n, seed): return np.random.default_rng(seed).standard_normal(n)

def bandpass(x, lo, hi):
    X = np.fft.rfft(x); f = np.fft.rfftfreq(len(x), 1 / SR)
    m = np.clip((f - lo) / max(1, lo * 0.3), 0, 1) * np.clip((hi - f) / max(1, hi * 0.3), 0, 1)
    return np.fft.irfft(X * m, len(x))

def sweep_noise(n, f0, f1, seed, width=0.8):
    """noise whose band-pass centre sweeps f0 -> f1 (log), in 24 chunks"""
    x = noise(n, seed); out = np.zeros(n); k = 24; step = n // k + 1
    for i in range(k):
        c = f0 * (f1 / f0) ** (i / (k - 1)); s = slice(i * step, min(n, (i + 1) * step))
        if s.start >= n: break
        seg = bandpass(x[s], c / (1 + width), c * (1 + width)); out[s] = seg
    # smooth chunk seams
    return out

def sine(n, f0, f1=None, phase=0.0):
    t = np.arange(n) / SR
    if f1 is None: return np.sin(2 * math.pi * f0 * t + phase)
    f = f0 * (f1 / f0) ** (t / t[-1]); ph = 2 * math.pi * np.cumsum(f) / SR
    return np.sin(ph + phase)

def ms(x): return int(SR * x / 1000)

def cue_tick(seed, bright=1.0):
    n = ms(70); x = noise(n, seed) * env(n, 2, 60) * 0.5
    x = bandpass(x, 1800 * bright, 7000 * bright) + sine(n, 2600 * bright) * env(n, 1, 40) * 0.25
    return x * 0.55

def clock_tick(seed, tock=False, gain=1.0):
    """a mechanical escapement: a sharp click, a short metallic ring, a wooden body thunk, a tiny room tail"""
    n = ms(160); r = np.random.default_rng(seed)
    click = bandpass(noise(n, seed), 2200, 9000) * env(n, 1, ms(7)) * 1.6
    ring_ = sine(n, 1180 if not tock else 940) * env(n, 1, ms(45)) * 0.35 + sine(n, 3300 if not tock else 2700) * env(n, 1, ms(18)) * 0.2
    body = sine(n, 190 if not tock else 150, 120 if not tock else 95) * env(n, 1, ms(38)) * 0.9
    tail = bandpass(noise(n, seed + 9), 500, 4000) * env(n, ms(2), ms(150)) * 0.08
    return (click + ring_ + body + tail) * gain

def cue_key(seed):
    n = ms(40); x = bandpass(noise(n, seed), 2500, 9000) * env(n, 1, 30) * 0.35
    return x

def cue_flash(seed):
    n = ms(420); x = bandpass(noise(n, seed), 1200, 14000) * env(n, 3, 380) * 0.8
    x += sine(n, 5200, 3900) * env(n, 2, 220) * 0.12
    return x

def cue_whoosh(seed, d=380):
    n = ms(d); x = sweep_noise(n, 300, 5000, seed) * env(n, ms(d * 0.45), ms(d * 0.5)) * 1.6
    x += sweep_noise(n, 5000, 400, seed + 1) * env(n, ms(d * 0.5), ms(d * 0.45)) * 0.6
    return x

def cue_flip(seed):
    n = ms(220); x = sweep_noise(n, 900, 4500, seed) * env(n, ms(20), ms(180)) * 1.1
    x += sine(n, 320, 70) * env(n, 2, ms(160)) * 0.5
    return x

def cue_riser(seed, d):
    n = ms(d); x = sweep_noise(n, 120, 3500, seed, 0.5) * np.linspace(0.05, 1, n) ** 2 * 1.3
    x += sine(n, 55, 220) * np.linspace(0, 0.35, n) ** 1.5
    return x * env(n, ms(50), 0, 1.0, ms(30))

def cue_burn(seed, d):
    n = ms(d); r = np.random.default_rng(seed)
    x = bandpass(noise(n, seed), 800, 9000) * 0.25
    crack = np.zeros(n); idx = r.integers(0, n, size=max(10, d // 6)); crack[idx] = r.uniform(0.4, 1.0, size=len(idx)) * r.choice([-1, 1], size=len(idx))
    x += np.convolve(crack, env(ms(12), 1, ms(11)), mode='same') * 0.9
    return x * env(n, ms(200), ms(d * 0.5), 0.6, ms(200))

def cue_impact(seed):
    n = ms(1400); x = sine(n, 95, 28) * env(n, 2, ms(900)) * 1.4
    x += bandpass(noise(n, seed), 60, 2400) * env(n, 1, ms(500)) * 0.8
    return x

def cue_hit(seed):
    n = ms(320); x = sine(n, 120, 45) * env(n, 1, ms(260)) * 1.1
    x += bandpass(noise(n, seed), 200, 3000) * env(n, 1, ms(60)) * 0.5
    return x

def cue_chord(seed):
    n = ms(5200); t = np.arange(n) / SR; x = np.zeros(n)
    for f, a in [(110, 0.5), (164.8, 0.35), (220, 0.3), (277.2, 0.18), (329.6, 0.14), (440, 0.08)]:
        x += np.sin(2 * math.pi * f * t) * a * (1 + 0.08 * np.sin(2 * math.pi * 0.3 * t + f))
    x *= env(n, ms(600), ms(2600), 0.5, ms(1600))
    return x * 0.5

def main(side, out):
    j = json.load(open(side)); cues = j['facts']['sfx']; total = ms(j['facts']['durationMs'] + 400)
    L = np.zeros(total); R = np.zeros(total)
    def put(x, t, pan=0.0, gain=1.0):
        s = ms(t); e = min(total, s + len(x)); x = x[:e - s] * gain
        L[s:e] += x * (1 - max(0, pan)); R[s:e] += x * (1 + min(0, pan))
    # the bed: an intense clock. Tick-tock at 1 Hz under the opening, doubling to 2 Hz when the planets come,
    # 4 Hz under the strobes and the eclipse, racing to 8 Hz into the last impact, where it stops dead.
    last_impact = max([c['t'] for c in cues if c['k'] == 'impact'] + [0])
    bursts = [(c['t'], c.get('d', 600)) for c in cues if c['k'] == 'riser']
    t = 0.0; i = 0
    while t < last_impact - 40:
        rate = 1.0
        if t > last_impact * 0.3: rate = 2.0
        for (bt, bd) in bursts:
            if bt <= t < bt + bd: rate = 4.0 + 4.0 * (t - bt) / max(1, bd)
        if t > last_impact - 1500: rate = 8.0
        gain = 0.8 + 0.5 * min(1.0, t / max(1, last_impact))
        put(clock_tick(int(t) + 11, tock=(i % 2 == 1), gain=gain), t, -0.35 if i % 2 else 0.35, 0.95)
        t += 1000.0 / rate; i += 1
    for c in cues:
        k, tt, seed = c['k'], c['t'], int(c['t'] * 7 + 3)
        if k == 'tick': put(clock_tick(seed, tock=bool(seed % 2), gain=0.55), tt, ((seed % 7) - 3) / 6, 0.7)
        elif k == 'key': put(cue_key(seed), tt, 0.1, 0.6)
        elif k == 'flash': put(cue_flash(seed), tt, 0, 0.75)
        elif k == 'whoosh': put(cue_whoosh(seed), tt - 120, 0.6 if seed % 2 else -0.6, 0.9)
        elif k == 'flip': put(cue_flip(seed), tt - 30, ((seed % 5) - 2) / 4, 0.8)
        elif k == 'riser': n_ = ms(c.get('d', 1500)); put(sine(n_, 45, 110) * np.linspace(0, 0.35, n_) ** 1.5 * env(n_, ms(50), 0, 1.0, ms(30)), tt, 0, 0.8)
        elif k == 'burn': put(cue_burn(seed, c.get('d', 1500)), tt, 0, 0.6)
        elif k == 'impact': put(cue_impact(seed), tt, 0, 1.0)
        elif k == 'hit': put(cue_hit(seed), tt, 0, 0.8)
        elif k == 'chord': put(cue_chord(seed), tt, 0, 0.8)
    # soft master: tanh limiter, -1 dBFS peak
    mix = np.stack([L, R]); mix = np.tanh(mix * 1.1) / math.tanh(1.1)
    mix *= 0.89 / max(1e-6, np.abs(mix).max())
    pcm = (mix.T * 32767).astype('<i2')
    import wave
    with wave.open(out, 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
    print(out, f'{total / SR:.1f}s', len(cues), 'cues')

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
