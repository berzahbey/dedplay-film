"""Uzun videolardan en iyi anları bulur.

Her saniye için puan:
  + ses canlılığı (konuşma, gülüşme, kalabalık) — ama sürekli rüzgar uğultusu değil
  + makul hareket (bir şeyler oluyor) — aşırı sallantı ceza alır
  + netlik ve aydınlık — karanlık, kapalı lens, bulanık kareler ceza alır
Sonra video boyunca birbirinden uzak, en yüksek puanlı pencereler seçilir ve kesme noktaları
konuşmanın ortasına denk gelmesin diye sessiz anlara kaydırılır.
"""
import json
import subprocess

import numpy as np

W, H = 64, 36


def _frames(path, dur):
    """Sadece anahtar kareleri çözer (hızlı): ~1 kare/sn."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-skip_frame", "nokey", "-i", path, "-vsync", "vfr",
                        "-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=1800)
    n = len(p.stdout) // (W * H)
    if n < 3:
        return None, None
    fr = np.frombuffer(p.stdout[:n * W * H], np.uint8).reshape(n, H, W).astype(np.float32)
    t = np.linspace(0, dur, n, endpoint=False)
    return t, fr


def _audio(path, dur):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "8000",
                        "-f", "s16le", "-"], capture_output=True, timeout=1800)
    a = np.frombuffer(p.stdout, np.int16).astype(np.float32) / 32768
    if len(a) < 8000:
        return None
    hop = 2000  # 0.25 sn
    n = len(a) // hop
    rms = np.sqrt((a[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-9)
    return 20 * np.log10(rms)  # dB, 4 değer/sn


def _z(x):
    s = x.std()
    return (x - x.mean()) / s if s > 1e-6 else np.zeros_like(x)


def analyze(path, dur):
    """Dönüş: {"score": [saniye başına puan], "quiet": [sessiz an zamanları]}"""
    secs = max(1, int(dur))
    score = np.zeros(secs)
    quiet = []
    t, fr = _frames(path, dur)
    if fr is not None:
        idx = np.clip(t.astype(int), 0, secs - 1)
        bright = fr.mean(axis=(1, 2))
        lap = np.abs(fr[:, 1:-1, 1:-1] * 4 - fr[:, :-2, 1:-1] - fr[:, 2:, 1:-1] - fr[:, 1:-1, :-2] - fr[:, 1:-1, 2:]).mean(axis=(1, 2))
        motion = np.r_[0, np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))]
        m = np.minimum(motion, np.percentile(motion, 80))  # sallantıyı ödüllendirme
        shaky = motion > np.percentile(motion, 95) * 1.0
        s = 0.8 * _z(lap) + 0.6 * _z(m) - 1.5 * shaky - 3.0 * (bright < 25) - 2.0 * (bright > 240)
        for i, v in zip(idx, s):
            score[i] = v
        # anahtar kareler arası boşlukları doldur
        last = 0.0
        for i in range(secs):
            if score[i] == 0:
                score[i] = last
            last = score[i]
    db = _audio(path, dur)
    if db is not None:
        per = db[: (len(db) // 4) * 4].reshape(-1, 4)
        loud = per.mean(axis=1)
        var = per.std(axis=1)          # konuşma/gülüşme dalgalanır, uğultu sabittir
        a = 0.6 * _z(np.clip(loud, -50, None)) + 0.6 * _z(var)
        n = min(len(a), secs)
        score[:n] += a[:n]
        thr = np.percentile(db, 25)
        quiet = [i / 4 for i in range(1, len(db) - 1) if db[i] <= thr and db[i] <= db[i - 1] and db[i] <= db[i + 1]]
    # baş ve sondaki ilk/son 1.5 sn genelde kamera açılıp kapanırken sallanır
    score[: min(2, secs)] -= 2
    score[-2:] -= 2
    return {"score": [round(float(x), 3) for x in score], "quiet": quiet[:4000]}


def pick(hl, dur, count, seg_len):
    """Puanı en yüksek, birbirinden uzak `count` pencere. Dönüş: başlangıç saniyeleri (sıralı)."""
    sc = np.array(hl["score"]) if hl and hl.get("score") else None
    L = max(1, int(round(seg_len)))
    if sc is None or len(sc) <= L:
        step = dur / (count + 1)
        return [max(0, step * (i + 1) - seg_len / 2) for i in range(count)]
    win = np.convolve(sc, np.ones(L) / L, mode="valid")  # win[i]: [i, i+L) ortalaması
    zone = len(sc) / count * 0.6                          # pencereler arası en az mesafe
    chosen = []
    order = np.argsort(-win)
    for i in order:
        if all(abs(i - c) >= max(L + 2, zone) for c in chosen):
            chosen.append(int(i))
        if len(chosen) == count:
            break
    quiet = hl.get("quiet") or []
    out = []
    for c in sorted(chosen):
        s = float(c)
        near = [q for q in quiet if abs(q - s) <= 1.0]  # kesmeyi sessiz ana kaydır
        if near:
            s = min(near, key=lambda q: abs(q - s))
        out.append(max(0.0, min(s, dur - seg_len)))
    return out


def get(con_row, path, dur):
    """Önbellekli analiz (media.highlights sütunu)."""
    if con_row.get("highlights"):
        try:
            return json.loads(con_row["highlights"])
        except Exception:
            pass
    return analyze(path, dur)
