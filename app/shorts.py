"""Çok kısa videolar (çoğu, fotoğrafı kaybolmuş canlı fotoğraf parçası).

Hiçbir an kaybolmasın diye bir kısa video çöpe atılmadan önce:
  1. Videonun ortasından bir kare alınır ve arşivdeki TÜM fotoğraflarla görsel olarak karşılaştırılır.
     Aynı ya da çok benzer bir fotoğraf varsa → "fotoğrafı var", video güvenle çöpe atılabilir.
  2. Benzer fotoğraf yoksa → videonun EN NET karesi tam çözünürlükte fotoğraf olarak kaydedilir
     (çekim tarihiyle, Resimler/<yıl> klasörüne), sonra video çöpe gider.
"""
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import db
from .analyze import _POP, dhash_gray

MATCH_DIFF = 30   # 256 bitte en fazla fark: bu kadar yakınsa aynı an sayılır


def _frame_gray(path, t, size=(17, 16)):
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", path, "-frames:v", "1",
                        "-vf", f"scale={size[0]}:{size[1]}:flags=area,format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=60)
    n = size[0] * size[1]
    if len(p.stdout) < n:
        return None
    return np.frombuffer(p.stdout[:n], np.uint8).reshape(size[1], size[0])


def _photo_matrix():
    with db.db() as con:
        rows = con.execute("SELECT id, dhash FROM media WHERE kind='photo' AND length(dhash)=64").fetchall()
    if not rows:
        return [], None
    ids = [r["id"] for r in rows]
    mat = np.frombuffer(bytes.fromhex("".join(r["dhash"] for r in rows)), np.uint8).reshape(len(rows), 32)
    return ids, mat


def _check_one(v, ids, mat):
    dur = v["duration"] or 1.0
    best = None
    for f in (0.5, 0.15, 0.85):
        g = _frame_gray(v["path"], dur * f)
        if g is None:
            continue
        h = np.frombuffer(bytes.fromhex(dhash_gray(g)), np.uint8)
        if mat is not None:
            d = _POP[mat ^ h].sum(axis=1)
            i = int(d.argmin())
            if best is None or d[i] < best[1]:
                best = (ids[i], int(d[i]))
    if best and best[1] <= MATCH_DIFF:
        return best[0]
    return 0


def check(videos):
    """videos: [{id, path, duration}] → {id: eşleşen fotoğraf id'si ya da 0}. Sonuç kaydedilir."""
    todo = [v for v in videos if v.get("short_photo") is None]
    if todo:
        ids, mat = _photo_matrix()
        with ThreadPoolExecutor(4) as ex:
            res = list(ex.map(lambda v: _check_one(v, ids, mat), todo))
        with db.db() as con:
            con.executemany("UPDATE media SET short_photo=? WHERE id=?", [(r, v["id"]) for v, r in zip(todo, res)])
        for v, r in zip(todo, res):
            v["short_photo"] = r
    return {v["id"]: v["short_photo"] for v in videos}


def best_frame_to_photo(v, dest_dir):
    """Videonun en net karesini tam çözünürlükte JPG olarak kaydeder (çekim tarihiyle). Dönüş: yol."""
    import cv2
    dur = v["duration"] or 1.0
    best_t, best_s = dur / 2, -1
    for k in range(9):
        t = dur * (0.08 + 0.84 * k / 8)
        g = _frame_gray(v["path"], t, (480, 270))
        if g is None:
            continue
        s = float(cv2.Laplacian(g.astype(np.float32), cv2.CV_32F).var())
        if s > best_s:
            best_t, best_s = t, s
    os.makedirs(dest_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(v["path"]))[0]
    out = os.path.join(dest_dir, stem + ".JPG")
    k = 2
    while os.path.exists(out):
        out = os.path.join(dest_dir, f"{stem} ({k}).JPG")
        k += 1
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{best_t:.3f}", "-i", v["path"], "-frames:v", "1",
                    "-q:v", "1", out], timeout=120, check=True)
    if v.get("taken_at"):
        dt = v["taken_at"].replace("-", ":", 2)
        subprocess.run(["exiftool", "-q", "-overwrite_original", f"-DateTimeOriginal={dt}", f"-CreateDate={dt}",
                        "-Make=Dedplay", "-Model=Kısa videodan alınan kare", out], timeout=60)
        try:  # dosya tarihi de çekim tarihi olsun
            import time as _t
            ts = _t.mktime(_t.strptime(v["taken_at"], "%Y-%m-%d %H:%M:%S"))
            os.utime(out, (ts, ts))
        except (ValueError, OverflowError):
            pass
    return out
