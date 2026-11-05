"""Benzer kareler: aynı yerde, birkaç dakika içinde çekilmiş ama poz/açı biraz farklı fotoğraflar.

Görsel iz (dhash) bunları yakalayamaz, çünkü kişi kıpırdamış, kamera biraz kaymıştır. Burada
bilgisayarlı görü kullanılır: her fotoğrafta SIFT ayırt edici noktaları çıkarılır, iki fotoğrafın
noktaları eşleştirilir ve geometrik olarak tutarlı (RANSAC) eşleşme sayısına bakılır. Aynı
sahnenin iki karesi yüzlerce tutarlı eşleşme verir, farklı sahneler birkaç tane.


Yanlış birleştirmeyi önlemek için: sadece 3 dakika içinde çekilenler karşılaştırılır, bir grup
ilk karesinden 3 dakikadan uzağa uzamaz ve en fazla 12 kare olur; yeni kare hem grubun ilk
karesine hem de bir önceki kareye benzemelidir.
"""
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
import os

from . import db

WINDOW = 180          # sn: karşılaştırma penceresi
MAX_SPAN = 180        # sn: bir grubun ilk ve son karesi arası en fazla
MAX_GROUP = 12
MIN_INLIERS = 35
MIN_AREA = 0.18      # tutarlı noktalar resmin en az %18'ine yayılmalı (köşedeki tarih damgası yetmez)
MIN_OVERLAP = 0.70   # hizalanınca iki kare en az %70 örtüşmeli
MAX_DIFF = 0.45      # hizalanmış karelerde farklı piksel oranı en fazla %45 (kişiler/düzen değişmişse büyür)
ANALYSIS_W = 640
MAX_NEIGHBORS = 12


def _feat(path):
    import cv2
    from .analyze import load_image
    import numpy as np
    im = load_image(path, ANALYSIS_W)
    g = np.ascontiguousarray(np.asarray(im.convert("L")))
    sift = cv2.SIFT_create(nfeatures=1500)
    kp, des = sift.detectAndCompute(g, None)
    if des is None or len(kp) < 20:
        return None
    return np.float32([k.pt for k in kp]), des, g


def _match(fa, fb):
    """Dönüş: (tutarlı eşleşme, yayılma alanı, örtüşme, farklı piksel oranı)."""
    import cv2
    import numpy as np
    (pa, da, ga), (pb, dbb, gb) = fa, fb
    m = cv2.BFMatcher().knnMatch(da, dbb, k=2)
    good = [x for x, y in (p for p in m if len(p) == 2) if x.distance < 0.75 * y.distance]
    if len(good) < 12:
        return 0, 0.0, 0.0, 1.0
    a = np.float32([pa[x.queryIdx] for x in good])
    b = np.float32([pb[x.trainIdx] for x in good])
    H, mask = cv2.findHomography(b, a, cv2.RANSAC, 4.0)
    if H is None:
        return 0, 0.0, 0.0, 1.0
    inl = mask.ravel().astype(bool)
    n = int(inl.sum())
    h, w = ga.shape
    pts = a[inl]
    area = float(cv2.contourArea(cv2.convexHull(pts))) / (w * h) if len(pts) >= 3 else 0.0
    gb2 = cv2.resize(gb, (gb.shape[1], gb.shape[0]))
    warped = cv2.warpPerspective(gb2, H, (w, h))
    valid = cv2.warpPerspective(np.ones_like(gb2), H, (w, h)) > 0
    overlap = float(valid.mean())
    ea = cv2.GaussianBlur(ga, (0, 0), 3).astype(np.float32)
    eb = cv2.GaussianBlur(warped, (0, 0), 3).astype(np.float32)
    d = np.abs(ea - eb)[valid]
    diff = float((d > 25).mean()) if d.size else 1.0
    return n, round(area, 3), round(overlap, 3), round(diff, 3)


def _chain_job(args):
    """Bir zaman zinciri içindeki gerekli çiftleri ölçer. Dönüş: [(a, b, inliers, ratio)]."""
    items, pairs = args          # items: {id: path}, pairs: [(a, b)]
    feats, out = {}, []
    for a, b in pairs:
        for x in (a, b):
            if x not in feats:
                try:
                    feats[x] = _feat(items[x])
                except Exception:
                    feats[x] = None
        if feats[a] is None or feats[b] is None:
            out.append((a, b, 0, 0.0, 0.0, 1.0))
            continue
        try:
            out.append((a, b) + _match(feats[a], feats[b]))
        except Exception:
            out.append((a, b, 0, 0.0, 0.0, 1.0))
    return out


def _t(r):
    return datetime.fromisoformat(r["taken_at"]).timestamp()


def find_groups(photos, progress=None):
    """photos: güvenilir tarihli fotoğraflar. Dönüş: [[id, id, ...], ...] benzer kare grupları."""
    rows = sorted((r for r in photos if r["date_source"] in ("exif", "google") and r["taken_at"]), key=_t)
    ts = [_t(r) for r in rows]
    # 1) karşılaştırılacak çiftler (3 dk içinde, en fazla 12 komşu)
    need = []
    for i, r in enumerate(rows):
        for j in range(i + 1, min(len(rows), i + 1 + MAX_NEIGHBORS)):
            if ts[j] - ts[i] > WINDOW:
                break
            need.append((r["id"], rows[j]["id"]))
    with db.db() as con:
        known = {(a, b): v for a, b, *v in con.execute("SELECT a, b, inl, area, overlap, diff FROM scene_pairs2")}
    todo = [p for p in need if p not in known]
    if todo:
        paths = {r["id"]: r["path"] for r in rows}
        # zincirlere böl (her işçi kendi karelerinin özelliklerini bir kez çıkarır)
        jobs, cur, ids = [], [], set()
        for p in todo:
            cur.append(p)
            ids.update(p)
            if len(cur) >= 60:
                jobs.append(({i: paths[i] for i in ids}, cur))
                cur, ids = [], set()
        if cur:
            jobs.append(({i: paths[i] for i in ids}, cur))
        workers = max(1, (os.cpu_count() or 2) // 2)
        done = 0
        with ProcessPoolExecutor(workers) as ex:
            for res in ex.map(_chain_job, jobs):
                with db.db() as con:
                    con.executemany("INSERT OR REPLACE INTO scene_pairs2(a,b,inl,area,overlap,diff) "
                                    "VALUES(?,?,?,?,?,?)", res)
                for a, b, *v in res:
                    known[(a, b)] = v
                done += len(res)
                if progress:
                    progress(done, len(todo))

    def same(a, b):
        n, area, ov, diff = known.get((a, b)) or known.get((b, a)) or (0, 0.0, 0.0, 1.0)
        return n >= MIN_INLIERS and area >= MIN_AREA and ov >= MIN_OVERLAP and diff <= MAX_DIFF

    # 2) temkinli gruplama: yeni kare hem grubun ilk karesine hem bir önceki kareye benzemeli
    groups, open_g = [], []
    for i, r in enumerate(rows):
        placed = False
        for g in open_g:
            first, last = g[0], g[-1]
            if (ts[i] - first[1] <= MAX_SPAN and len(g) < MAX_GROUP
                    and same(first[0], r["id"]) and same(last[0], r["id"])):
                g.append((r["id"], ts[i]))
                placed = True
                break
        if not placed:
            open_g.append([(r["id"], ts[i])])
        # süresi dolan grupları kapat
        still = []
        for g in open_g:
            if ts[i] - g[0][1] > MAX_SPAN:
                if len(g) > 1:
                    groups.append([x for x, _ in g])
            else:
                still.append(g)
        open_g = still
    groups += [[x for x, _ in g] for g in open_g if len(g) > 1]
    return groups
