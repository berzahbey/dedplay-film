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
    """Toplam düzeltme = dosya bilgisinden gelen (meta_rot) + elle (yoksa otomatik)."""
    base = int(row.get("meta_rot") or 0)
    if row.get("rotate_fix") is not None:
        return (base + int(row["rotate_fix"])) % 360
    return (base + int(row.get("auto_rot") or 0)) % 360


def meta_fix(path):
    """Bazı dosyalarda yön bilgisi FFmpeg'in okumadığı bir yerde durur (oynatıcılar doğru gösterir,
    FFmpeg yan çözer). ExifTool'un okuduğu yön ile FFmpeg'in uyguladığı yön farklıysa farkı döndürür."""
    import json as _json
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream_tags=rotate:stream_side_data=rotation", "-of", "json", path],
                           capture_output=True, timeout=60)
        st = (_json.loads(p.stdout or b"{}").get("streams") or [{}])[0]
        r_ff = 0
        if "rotate" in (st.get("tags") or {}):
            r_ff = int(float(st["tags"]["rotate"]))
        for sd in st.get("side_data_list") or []:
            if "rotation" in sd:
                r_ff = -int(sd["rotation"])  # FFmpeg: saat yönünün tersi pozitif
        e = subprocess.run(["exiftool", "-n", "-j", "-Rotation", path], capture_output=True, timeout=60)
        r_ex = int(float((_json.loads(e.stdout or b"[{}]")[0]).get("Rotation") or 0))
    except Exception:
        return 0
    r_ff, r_ex = r_ff % 360, r_ex % 360
    if r_ex in (90, 180, 270) and r_ff == 0:
        return r_ex
    return 0


def _frames(path, dur, n=6, rv=""):
    out = []
    for k in range(n):
        t = dur * (0.1 + 0.8 * k / max(1, n - 1))
        try:
            p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", (rv + "," if rv else "") + "scale=360:360:force_original_aspect_ratio=decrease,format=gray",
                            "-f", "image2pipe", "-vcodec", "pgm", "-"], capture_output=True, timeout=60)
        except Exception:
            continue
        if p.stdout and cv2 is not None:
            img = cv2.imdecode(np.frombuffer(p.stdout, np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                out.append(img)
    return out


def check_video(path, dur):
    """(dosya bilgisinden düzeltme, otomatik düzeltme). Hata olursa (0, 0)."""
    try:
        base = meta_fix(path)
        return base, detect_video(path, dur, base)
    except Exception:
        return 0, 0


def detect_video(path, dur, base=0):
    """Dönüş: önerilen ek saat yönü dönüşü (0 = dokunma). Yanlış alarm olmasın diye çok temkinli:
    düz yönde hiç yüz yokken başka bir yönde en az 2 farklı karede net yüz bulunmalı."""
    if cv2 is None or not dur:
        return 0
    frames = _frames(path, dur, rv=vf_rotate(base))
    if not frames:
        return 0
    hits = {}
    for deg in (0, 90, 180, 270):
        hits[deg] = 0
        for f in frames:
            g = np.ascontiguousarray(np.rot90(f, k=-(deg // 90)))
            if len(_FACE.detectMultiScale(g, 1.1, 8, minSize=(32, 32))):
                hits[deg] += 1
    best = max(hits, key=hits.get)
    if best != 0 and hits[best] >= 2 and hits[0] == 0 and sorted(hits.values())[-2] == 0:
        return best
    return 0
