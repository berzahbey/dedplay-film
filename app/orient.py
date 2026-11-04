"""Yön düzeltme.

Telefon kayda yere/aşağı bakarken başlarsa video yan kaydedilir; dosyada da yanlış yön yazar,
her oynatıcıda yan görünür. İki çözüm:
  1. Otomatik: videodan birkaç kare alınır, 4 yönde yüz aranır. Yüzler belirgin biçimde başka bir
     yönde bulunursa video o yöne çevrilir. (Yüz yoksa otomatik karar verilmez.)
  2. Elle: önizlemedeki ↻ butonu. Elle verilen yön her zaman önceliklidir.
Değerler saat yönünde derece: 0, 90, 180, 270.
"""
import subprocess

import numpy as np

try:
    import cv2
    _FACE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
except Exception:  # pragma: no cover
    cv2 = None


def vf_rotate(deg):
    """FFmpeg filtresi: görüntüyü saat yönünde döndürür."""
    return {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}.get(deg or 0, "")


def effective(row):
    if row.get("rotate_fix") is not None:
        return int(row["rotate_fix"]) % 360
    return int(row.get("auto_rot") or 0) % 360


def _frames(path, dur, n=6):
    out = []
    for k in range(n):
        t = dur * (0.1 + 0.8 * k / max(1, n - 1))
        p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", "scale=360:360:force_original_aspect_ratio=decrease,format=gray",
                            "-f", "image2pipe", "-vcodec", "pgm", "-"], capture_output=True, timeout=60)
        if p.stdout and cv2 is not None:
            img = cv2.imdecode(np.frombuffer(p.stdout, np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                out.append(img)
    return out


def detect_video(path, dur):
    """Dönüş: önerilen saat yönü dönüşü (0 = dokunma)."""
    if cv2 is None or not dur:
        return 0
    frames = _frames(path, dur)
    if not frames:
        return 0
    score = {}
    for deg in (0, 90, 180, 270):
        c = 0
        for f in frames:
            g = np.ascontiguousarray(np.rot90(f, k=-(deg // 90)))
            c += len(_FACE.detectMultiScale(g, 1.1, 6, minSize=(24, 24)))
        score[deg] = c
    best = max(score, key=score.get)
    if best != 0 and score[best] >= 2 and score[best] >= 2 * score[0] + 1:
        return best
    return 0
