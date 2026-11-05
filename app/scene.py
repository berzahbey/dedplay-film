"""Benzer kareler: aynı yerde, birkaç dakika içinde çekilmiş ama poz/açı biraz farklı fotoğraflar.

Görsel iz (dhash) bunları yakalayamaz, çünkü kişi kıpırdamış, kamera biraz kaymıştır. Burada
bilgisayarlı görü kullanılır: her fotoğrafta SIFT ayırt edici noktaları çıkarılır, iki fotoğrafın
noktaları eşleştirilir ve geometrik olarak tutarlı (RANSAC) eşleşme sayısına bakılır. Aynı
sahnenin iki karesi yüzlerce tutarlı eşleşme verir, farklı sahneler birkaç tane.
(Örnek karelerle ölçüldü: aynı sahne 67–560, farklı sahne en fazla 11 tutarlı eşleşme; eşik 35.)

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
MIN_RATIO = 0.05
ANALYSIS_W = 640
MAX_NEIGHBORS = 12


def _feat(path):
    import cv2
    from .analyze import load_image
    import numpy as np
    im = load_image(path, ANALYSIS_W)
    g = np.asarray(im.convert("L"))
    sift = cv2.SIFT_create(nfeatures=1500)
    kp, des = sift.detectAndCompute(g, None)
    if des is None or len(kp) < 20:
        return None
    return np.float32([k.pt for k in kp]), des


def _match(fa, fb):
    import cv2
    import numpy as np
    (pa, da), (pb, dbb) = fa, fb
    m = cv2.BFMatcher().knnMatch(da, dbb, k=2)
    good = [x for x, y in (p for p in m if len(p) == 2) if x.distance < 0.75 * y.distance]
    if len(good) < 12:
        return 0, 0.0
    a = np.float32([pa[x.queryIdx] for x in good])
    b = np.float32([pb[x.trainIdx] for x in good])
    _, mask = cv2.findFundamentalMat(a, b, cv2.FM_RANSAC, 3.0, 0.99)
    inl = int(mask.sum()) if mask is not None else 0
    return inl, inl / max(1, min(len(pa), len(pb)))


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
            out.append((a, b, 0, 0.0))
            continue
        try:
            inl, r = _match(feats[a], feats[b])
        except Exception:
            inl, r = 0, 0.0
        out.append((a, b, inl, r))
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
        known = {(a, b): (n, q) for a, b, n, q in con.execute("SELECT a, b, inl, ratio FROM scene_pairs")}
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
                    con.executemany("INSERT OR REPLACE INTO scene_pairs(a,b,inl,ratio) VALUES(?,?,?,?)", res)
                for a, b, n, q in res:
                    known[(a, b)] = (n, q)
                done += len(res)
                if progress:
                    progress(done, len(todo))

    def same(a, b):
        n, q = known.get((a, b)) or known.get((b, a)) or (0, 0.0)
        return n >= MIN_INLIERS and q >= MIN_RATIO

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
