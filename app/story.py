"""Sahne kurgusu: kareleri filmde nasıl göstereceğimizi belirler.

- Bir videonun çekildiği sırada / hemen öncesinde-sonrasında, aynı rotada çekilen fotoğraflar
  o videoya "ara plan" (cutaway) olur: video oynarken fotoğraf birkaç saniye ekrana gelir,
  videonun sesi kesilmeden devam eder.
- Aynı yerde art arda çekilmiş iki dikey fotoğraf yan yana tek karede gösterilir.
- Geri kalan fotoğraflar tek tek, Ken Burns hareketiyle.
"""
import math
from datetime import datetime, timedelta

NEAR = timedelta(minutes=10)       # videodan bu kadar önce/sonra çekilen fotoğraflar
NEAR_KM = 3.0                      # video süresi dışındaysa en fazla bu mesafe
PAIR_GAP = timedelta(minutes=20)
CUT_SEC = 2.6                      # ara planın ekranda kalma süresi
MAX_CUTS = 6                       # bir videoya en fazla bu kadar ara plan
LEAD, TAIL, GAP = 2.0, 1.4, 1.2    # ara planlar arasında görünen video süreleri


def _t(r):
    return datetime.fromisoformat(r["taken_at"])


def _km(a, b):
    if a.get("lat") is None or b.get("lat") is None:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def seg_count(dur, video_sec):
    """Videodan kaç kesit alınır: kısa video tamamen, uzun videodan süresine göre 1-6 kesit."""
    dur = dur or 0
    if dur <= video_sec + 1.5:
        return 0
    if dur <= 60:
        return 1
    return min(6, 1 + int(dur // 120))


def segments(dur, video_sec, starts=None):
    """Bir videodan alınacak (başlangıç, süre) kesitleri.
    starts: en iyi anların başlangıçları (highlights.pick); yoksa video boyunca eşit dağıtılır."""
    dur = dur or 0
    n = seg_count(dur, video_sec)
    if n == 0:
        return [(0.0, max(dur, 1.0))]
    if not starts or len(starts) != n:
        step = dur / (n + 1)
        starts = [min(max(0.0, step * (i + 1) - video_sec / 2), dur - video_sec) for i in range(n)]
    return [(s, video_sec) for s in starts]


def _portrait(r):
    return r.get("width") and r.get("height") and r["width"] / r["height"] < 0.9


def build_units(rows, video_sec=7.0):
    """rows: zamana göre sıralı medya kayıtları. Dönüş: birim listesi
    {"type": "photo"|"pair"|"video", "ids": [...], "segments": [{"start","len","cuts":[id]}]}"""
    rows = sorted(rows, key=_t)
    attached = set()
    video_units = {}
    photos = [r for r in rows if r["kind"] == "photo"]

    for v in (r for r in rows if r["kind"] == "video"):
        vs = _t(v)
        dur = v.get("duration") or 0
        ve = vs + timedelta(seconds=dur)
        cands = []
        for p in photos:
            if p["id"] in attached:
                continue
            tp = _t(p)
            if tp < vs - NEAR or tp > ve + NEAR:
                continue
            during = vs <= tp <= ve
            km = _km(p, v)
            if not during and km is not None and km > NEAR_KM:
                continue
            cands.append(p)
        # En yakın zamandakiler öncelikli
        cands.sort(key=lambda p: abs((_t(p) - vs).total_seconds() - dur / 2))
        cands = sorted(cands[:MAX_CUTS], key=_t)

        segs = [{"start": s, "len": l, "cuts": []} for s, l in segments(dur, video_sec, v.get("hl_starts"))]
        for p in cands:
            off = (_t(p) - vs).total_seconds()
            # fotoğrafın zamanına en yakın kesit
            seg = min(segs, key=lambda s: abs(s["start"] + s["len"] / 2 - off))
            n = len(seg["cuts"]) + 1
            need = LEAD + n * CUT_SEC + (n - 1) * GAP + TAIL
            if need <= dur:
                seg["cuts"].append(p["id"])
                attached.add(p["id"])
        for s in segs:
            n = len(s["cuts"])
            if n:
                need = LEAD + n * CUT_SEC + (n - 1) * GAP + TAIL
                s["len"] = max(s["len"], need)
                s["start"] = max(0.0, min(s["start"], dur - s["len"]))
        video_units[v["id"]] = {"type": "video", "ids": [v["id"]], "segments": segs, "t": vs}

    units, pending = [], None
    for r in rows:
        if r["kind"] == "video":
            units.append(video_units[r["id"]])
            pending = None
            continue
        if r["id"] in attached:
            continue
        u = {"type": "photo", "ids": [r["id"]], "t": _t(r)}
        if _portrait(r) and pending is not None and _t(r) - pending["t2"] <= PAIR_GAP:
            km = _km(r, pending["row"])
            if km is None or km <= 1.0:
                pu = pending["unit"]
                pu["type"] = "pair"
                pu["ids"].append(r["id"])
                pending = None
                continue
        units.append(u)
        pending = {"unit": u, "row": r, "t2": _t(r)} if _portrait(r) else None
    for u in units:
        u.pop("t", None)
    return units


def unit_cost(u, photo_sec, rows_by_id):
    if u["type"] == "photo":
        return photo_sec
    if u["type"] == "pair":
        return photo_sec * 1.3
    return sum(s["len"] for s in u["segments"])
