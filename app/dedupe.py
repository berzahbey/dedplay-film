"""Kopya tespiti: adı farklı ama görüntüsü aynı fotoğraf ve videoları bulur.

Fotoğraf: 256 bitlik görsel iz. İz 16 parçaya bölünür; aynı parçayı paylaşanlar aday olur
(≤15 bit farkta en az bir parça birebir aynıdır), sonra tam fark ölçülür. Böylece 100.000
fotoğraf bile saniyeler içinde karşılaştırılır.
Video: süre ±1 sn + 3 karenin izi.
Her grupta en iyi kopya kalır: güvenilir tarih > yüksek çözünürlük > uzun süre > netlik.
"""
from collections import defaultdict

from .analyze import hamming

PHOTO_MAX_DIFF = 14      # 256 bitte en fazla 14 bit fark → aynı fotoğraf
VIDEO_MAX_DIFF = 24      # 3 karede toplam (768 bit)
BANDS = 16
DATE_RANK = {"exif": 0, "google": 1, "filename": 2, "mtime": 3}


def _better(a, b):
    """a, b'den daha iyi kopya mı?"""
    ka = (DATE_RANK.get(a["date_source"], 4), -((a["width"] or 0) * (a["height"] or 0)),
          -(a["duration"] or 0), -(a["sharp"] or 0), a["id"])
    kb = (DATE_RANK.get(b["date_source"], 4), -((b["width"] or 0) * (b["height"] or 0)),
          -(b["duration"] or 0), -(b["sharp"] or 0), b["id"])
    return ka < kb


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _informative(h):
    ones = bin(int(h, 16)).count("1")
    return 24 <= ones <= 232  # düz siyah/beyaz karelerin izi anlamsızdır


def _photo_pairs(rows, uf):
    buckets = defaultdict(list)
    step = len(rows[0]["dhash"]) // BANDS if rows else 4
    for r in rows:
        h = r["dhash"]
        cands = {}
        keys = [(b, h[b * step:(b + 1) * step]) for b in range(BANDS)]
        for k in keys:
            lst = buckets[k]
            if len(lst) < 200:  # çok kalabalık parça bilgi taşımaz
                for c in lst:
                    cands[c["id"]] = c
        for c in cands.values():
            if hamming(h, c["dhash"]) <= PHOTO_MAX_DIFF:
                uf.union(c["id"], r["id"])
        for k in keys:
            buckets[k].append(r)


def _video_parts(h):
    """Videonun 3 kare izinden anlamlı olanlar (düz siyah/tek renk kareler sayılmaz)."""
    return [h[i * 64:(i + 1) * 64] for i in range(3)]


def _video_pairs(rows, uf):
    rows = sorted(rows, key=lambda r: r["duration"] or 0)
    for i, a in enumerate(rows):
        pa = _video_parts(a["dhash"])
        for b in rows[i + 1:]:
            if (b["duration"] or 0) - (a["duration"] or 0) > 1.0:
                break
            pb = _video_parts(b["dhash"])
            # her iki videoda da anlamlı olan karelerden en az 2'si benzer olmalı
            good = [(x, y) for x, y in zip(pa, pb) if _informative(x) and _informative(y)]
            if len(good) >= 2 and sum(hamming(x, y) for x, y in good) <= VIDEO_MAX_DIFF * len(good) / 3:
                uf.union(a["id"], b["id"])


def find_duplicates(rows):
    """rows: dict listesi (id, kind, dhash, date_source, width, height, duration, sharp).
    Dönüş: {kopya_id: asıl_id}"""
    uf = _UF()
    photos = [r for r in rows if r["kind"] == "photo" and r.get("dhash") and len(r["dhash"]) == 64
              and _informative(r["dhash"])]
    videos = [r for r in rows if r["kind"] == "video" and r.get("dhash") and len(r["dhash"]) == 192]
    _photo_pairs(photos, uf)
    _video_pairs(videos, uf)
    by_id = {r["id"]: r for r in photos + videos}
    groups = defaultdict(list)
    for i in by_id:
        groups[uf.find(i)].append(by_id[i])
    out = {}
    for g in groups.values():
        if len(g) < 2:
            continue
        best = g[0]
        for r in g[1:]:
            if _better(r, best):
                best = r
        for r in g:
            if r is not best:
                out[r["id"]] = best["id"]
    return out
