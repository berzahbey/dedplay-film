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

VERSION = 2
AW = 320  # analiz boyutu (uzun kenar)

try:
    import cv2
    _FACE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
except Exception:  # pragma: no cover
    cv2 = None


def _dims(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:stream_side_data=rotation", "-of", "json", path],
                       capture_output=True, timeout=60)
    try:
        st = json.loads(p.stdout)["streams"][0]
        w, h = int(st["width"]), int(st["height"])
        rot = next((int(sd["rotation"]) for sd in st.get("side_data_list", []) if "rotation" in sd), 0)
        return (h, w) if abs(rot) % 180 == 90 else (w, h)
    except Exception:
        return 16, 9


def _frames(path, dur, rv=""):
    """Sadece anahtar kareleri çözer (hızlı): ~1 kare/sn, en-boy oranı korunur."""
    w, h = _dims(path)
    if rv and rv != "hflip,vflip":
        w, h = h, w
    if w >= h:
        fw, fh = AW, max(2, int(round(AW * h / w / 2)) * 2)
    else:
        fw, fh = max(2, int(round(AW * w / h / 2)) * 2), AW
    p = subprocess.run(["ffmpeg", "-v", "error", "-skip_frame", "nokey", "-i", path, "-vsync", "vfr",
                        "-vf", f"{rv + ',' if rv else ''}scale={fw}:{fh},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=1800)
    n = len(p.stdout) // (fw * fh)
    if n < 3:
        return None, None
    fr = np.frombuffer(p.stdout[:n * fw * fh], np.uint8).reshape(n, fh, fw).astype(np.float32)
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


def _rz(x):
    """Aykırı değerlere dayanıklı z-puanı (medyan/MAD), ±3 ile sınırlı."""
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    if mad < 1e-6:
        mad = x.std() or 1.0
    return np.clip((x - med) / mad, -3, 3)


def _z(x):
    s = x.std()
    return (x - x.mean()) / s if s > 1e-6 else np.zeros_like(x)


def analyze(path, dur, rv=""):
    """Dönüş: {"v": sürüm, "score": [saniye başına puan], "quiet": [sessiz an zamanları]}"""
    secs = max(1, int(dur))
    score = np.zeros(secs)
    quiet = []
    t, fr = _frames(path, dur, rv)
    if fr is not None:
        n = len(fr)
        idx = np.clip(t.astype(int), 0, secs - 1)
        bright = fr.mean(axis=(1, 2))
        contrast = fr.std(axis=(1, 2))
        c = fr[:, 1:-1, 1:-1]
        lap = np.abs(c * 4 - fr[:, :-2, 1:-1] - fr[:, 2:, 1:-1] - fr[:, 1:-1, :-2] - fr[:, 1:-1, 2:]).mean(axis=(1, 2))
        lap = lap / (contrast + 8)  # parlaklıktan bağımsız netlik
        cam = np.zeros(n)            # kamera sallantısı / savrulma (kareler arası toplu kayma)
        subj = np.zeros(n)           # sahnedeki hareket (kamera hareketi çıkarılmış)
        faces = np.zeros(n)
        win = np.hanning(fr.shape[1])[:, None] * np.hanning(fr.shape[2])[None, :]
        for i in range(n):
            if cv2 is not None:
                g = fr[i].astype(np.uint8)
                fs = _FACE.detectMultiScale(g, 1.1, 5, minSize=(20, 20))
                faces[i] = min(len(fs), 3)
                if i:
                    (dx, dy), _ = cv2.phaseCorrelate(fr[i - 1] * win, fr[i] * win)
                    cam[i] = float(np.hypot(dx, dy)) / AW
                    m = np.float32([[1, 0, -dx], [0, 1, -dy]])
                    al = cv2.warpAffine(fr[i], m, (fr.shape[2], fr.shape[1]), borderMode=cv2.BORDER_REPLICATE)
                    subj[i] = float(np.abs(al - fr[i - 1])[4:-4, 4:-4].mean())
        dt = np.r_[1.0, np.diff(t)] if n > 1 else np.ones(n)
        usable = (bright > 30) & (contrast > 12)
        cam = np.where(usable, cam / np.maximum(dt, 0.3), 0)   # genişliğin saniye başına kayma oranı
        shake = np.clip(cam * 60, 0, 3)                           # ~%2.5/sn sallantı ≈ 1.5 puan ceza
        s = (1.0 * _rz(lap)                           # net kareler
             + 0.4 * _rz(np.minimum(subj, np.percentile(subj, 85)))  # bir şeyler oluyor
             - 1.5 * shake                            # sallantı / savrulma cezası
             + 0.9 * faces                            # insan yüzü
             - 3.0 * (bright < 30) - 2.0 * (bright > 235) - 1.5 * (contrast < 12))
        for i, v in zip(idx, s):
            score[i] = v
        last = 0.0
        for i in range(secs):  # anahtar kareler arası boşlukları doldur
            if score[i] == 0:
                score[i] = last
            last = score[i]
    db = _audio(path, dur)
    if db is not None:
        per = db[: (len(db) // 4) * 4].reshape(-1, 4)
        loud = per.mean(axis=1)
        var = per.std(axis=1)          # konuşma/gülüşme dalgalanır, uğultu sabittir
        a = 0.5 * _z(np.clip(loud, -50, None)) + 0.5 * _z(var)
        m = min(len(a), secs)
        score[:m] += a[:m]
        thr = np.percentile(db, 25)
        quiet = [i / 4 for i in range(1, len(db) - 1) if db[i] <= thr and db[i] <= db[i - 1] and db[i] <= db[i + 1]]
    score[: min(2, secs)] -= 2   # kayıt başı/sonu genelde sallantılı
    score[-2:] -= 2
    return {"v": VERSION, "score": [round(float(x), 3) for x in score], "quiet": quiet[:4000]}


def pick(hl, dur, count, seg_len):
    """Puanı en yüksek, birbirinden uzak `count` pencere. Dönüş: başlangıç saniyeleri (sıralı)."""
    sc = np.array(hl["score"]) if hl and hl.get("score") else None
    L = max(1, int(round(seg_len)))
    if sc is None or len(sc) <= L:
        step = dur / (count + 1)
        return [max(0, step * (i + 1) - seg_len / 2) for i in range(count)]
    mean = np.convolve(sc, np.ones(L) / L, mode="valid")  # [i, i+L) ortalaması
    worst = np.array([sc[i:i + L].min() for i in range(len(mean))])
    win = mean + 0.4 * np.minimum(worst, 0)               # içinde kötü bir an olan pencere ceza alır
    # Kesitler video boyunca yayılsın ama kötü anlar alınmasın: önce geniş aralıkla,
    # yeterli iyi an bulunamazsa aralık daraltılarak seçilir. Ortalamanın altındakiler alınmaz.
    thr = max(np.percentile(win, 45), win.max() - 4.0)
    order = np.argsort(-win)
    chosen = []
    for zone in (len(sc) / count * 0.6, len(sc) / count * 0.3, L + 2):
        for i in order:
            if len(chosen) == count or win[i] < thr:
                break
            if all(abs(i - c) >= max(L + 2, zone) for c in chosen):
                chosen.append(int(i))
        if len(chosen) == count:
            break
    if not chosen:
        chosen = [int(order[0])]
    quiet = hl.get("quiet") or []
    out = []
    for c in sorted(chosen):
        s = float(c)
        near = [q for q in quiet if abs(q - s) <= 1.0]  # kesmeyi sessiz ana kaydır
        if near:
            s = min(near, key=lambda q: abs(q - s))
        out.append(max(0.0, min(s, dur - seg_len)))
    return out


def get(con_row, path, dur, rv=""):
    """Önbellekli analiz (media.highlights sütunu). Eski sürüm analizleri yenilenir."""
    if con_row.get("highlights"):
        try:
            h = json.loads(con_row["highlights"])
            if h.get("v") == VERSION and h.get("rv", "") == rv:
                return h
        except Exception:
            pass
    h = analyze(path, dur, rv)
    h["rv"] = rv
    return h
