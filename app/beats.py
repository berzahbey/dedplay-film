"""Müzikte vuruş (beat) tespiti — sadece numpy, ek kütüphane yok.

Yöntem: spektral akı ile vuruş gücü eğrisi → otokorelasyonla tempo → dinamik programlama ile
vuruş zamanları (Ellis 2007).
"""
import subprocess

import numpy as np

SR, HOP, NFFT = 11025, 256, 1024


def _decode(path, max_sec=900):
    p = subprocess.run(["ffmpeg", "-v", "error", "-t", str(max_sec), "-i", path, "-ac", "1",
                        "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, timeout=600)
    return np.frombuffer(p.stdout, np.float32)


def _onset(y):
    n = 1 + (len(y) - NFFT) // HOP
    if n < 10:
        return None
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
    win = np.hanning(NFFT).astype(np.float32)
    env = []
    prev = None
    for i in range(0, n, 2000):  # bellek dostu parçalar
        frames = y[idx[i:i + 2000]] * win
        mag = np.log1p(10 * np.abs(np.fft.rfft(frames, axis=1)))
        if prev is not None:
            mag_full = np.vstack([prev[None, :], mag])
        else:
            mag_full = np.vstack([mag[:1], mag])
        flux = np.maximum(mag_full[1:] - mag_full[:-1], 0).sum(axis=1)
        env.append(flux)
        prev = mag[-1]
    o = np.concatenate(env)
    o = o - np.convolve(o, np.ones(16) / 16, mode="same")  # yerel ortalamayı çıkar
    o = np.maximum(o, 0)
    return o / (o.std() + 1e-9)


def _tempo(o):
    fps = SR / HOP
    lo, hi = int(fps * 60 / 180), int(fps * 60 / 70)
    ac = np.correlate(o[:6000], o[:6000], mode="full")[len(o[:6000]) - 1:]
    lags = np.arange(lo, hi + 1)
    bpm = 60 * fps / lags
    w = np.exp(-0.5 * (np.log2(bpm / 120) / 0.9) ** 2)  # 120 BPM civarını tercih et
    return lags[np.argmax(ac[lo:hi + 1] * w)]


def _track(o, period, tight=100.0):
    n = len(o)
    score = o.copy()
    back = np.full(n, -1)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    offs = np.arange(-hi, -lo + 1)
    pen = -tight * np.log(-offs / period) ** 2
    for t in range(hi, n):
        cand = score[t + offs] + pen
        k = int(np.argmax(cand))
        score[t] = o[t] + cand[k]
        back[t] = t + offs[k]
    t = int(np.argmax(score[-int(period):])) + n - int(period)
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    return np.array(beats[::-1])


def detect(path):
    """Dönüş: (vuruş zamanları [sn], tempo BPM). Başarısızsa ([], None)."""
    try:
        y = _decode(path)
        o = _onset(y)
        if o is None:
            return [], None
        period = _tempo(o)
        b = _track(o, period)
        fps = SR / HOP
        lat = NFFT / 2 / SR  # pencere merkezi
        return [float(x / fps + lat) for x in b], 60 * fps / period
    except Exception:
        return [], None
