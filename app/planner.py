"""Akıllı seçim: bir tarih aralığından filme girecek en iyi kareleri seçer."""
import math
import os
import re
from collections import OrderedDict
from datetime import datetime, date

from . import db
from .analyze import hamming
from .dedupe import find_duplicates
from . import story, styles

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
         "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
SCREENSHOT = re.compile(r"screenshot|screen_shot|schermafbeelding|ekran|bildschirmfoto|capture", re.I)

TITLE_SEC, INTRO_SEC, OUTRO_SEC = 3.0, 4.5, 3.5


def tr_date(d, weekday=True):
    s = f"{d.day} {AYLAR[d.month - 1]} {d.year}"
    return s + (f", {GUNLER[d.weekday()]}" if weekday else "")


def tr_range(a, b):
    if a == b:
        return tr_date(a, False)
    if a.year == b.year and a.month == 1 and a.day == 1 and b.month == 12 and b.day == 31:
        return str(a.year)
    if a.year == b.year:
        if a.month == b.month:
            return f"{a.day}–{b.day} {AYLAR[a.month - 1]} {a.year}"
        return f"{a.day} {AYLAR[a.month - 1]} – {b.day} {AYLAR[b.month - 1]} {a.year}"
    return f"{tr_date(a, False)} – {tr_date(b, False)}"


def _segments(item, video_sec):
    return story.segments(item.get("duration"), video_sec)


def cost(item, photo_sec, video_sec, trans=0.0):
    if item["kind"] == "photo":
        return max(photo_sec - trans, 1.0)
    return sum(max(d - trans, 0.8) for _, d in _segments(item, video_sec))


def _load(start, end, folder):
    q = "SELECT * FROM media WHERE dup_of IS NULL AND taken_at BETWEEN ? AND ?"
    args = [f"{start} 00:00:00", f"{end} 23:59:59"]
    if folder:
        q += " AND (folder = ? OR folder LIKE ?)"
        args += [folder, folder.rstrip("/") + "/%"]
    q += " ORDER BY taken_at"
    with db.db() as con:
        return [dict(r) for r in con.execute(q, args)]


def _filter(rows):
    photo_stems = {(r["folder"], os.path.splitext(os.path.basename(r["path"]))[0].lower())
                   for r in rows if r["kind"] == "photo"}
    out = []
    for r in rows:
        name = os.path.basename(r["path"])
        if SCREENSHOT.search(name):
            continue
        if r["kind"] == "photo":
            if r["width"] and r["height"] and r["width"] * r["height"] < 300_000:
                continue  # çok küçük (ikon, sticker)
            if name.lower().endswith(".png"):
                continue  # PNG çoğunlukla ekran görüntüsü / grafik
        else:
            dur = r["duration"] or 0
            stem = os.path.splitext(name)[0].lower()
            if dur and dur < 4 and (r["folder"], stem) in photo_stems:
                continue  # iPhone Live Photo videosu (fotoğrafın kopyası)
            if dur and dur < 1.2:
                continue
        out.append(r)
    return out


def _dedupe(rows, stats):
    """Art arda çekilmiş benzer fotoğraflardan en neti kalır."""
    out, last, li = [], None, -1
    for r in rows:
        if r["kind"] != "photo":
            out.append(r)
            continue
        if last is not None:
            dt = (datetime.fromisoformat(r["taken_at"]) - datetime.fromisoformat(last["taken_at"])).total_seconds()
            if dt <= 2 or (dt <= 120 and hamming(r["dhash"], last["dhash"]) <= 40):
                stats["duplicates"] += 1
                if (r["sharp"] or 0) > (last["sharp"] or 0):
                    out[li] = r
                    last = r
                continue
        out.append(r)
        last, li = r, len(out) - 1
    return out


def _deblur(rows, stats):
    vals = sorted(r["sharp"] for r in rows if r["kind"] == "photo" and r["sharp"] is not None)
    if len(vals) < 5:
        return rows
    med = vals[len(vals) // 2]
    lim = min(30.0, 0.2 * med)
    out = []
    for r in rows:
        if r["kind"] == "photo" and r["sharp"] is not None and r["sharp"] < lim:
            stats["blurry"] += 1
            continue
        out.append(r)
    return out


def _score(r):
    if r["kind"] == "video":
        return 3.0 + min((r["duration"] or 0) / 30, 1.0)
    s = math.log1p(r["sharp"] or 100) / 2
    if r["date_source"] == "exif":
        s += 0.3
    if r["lat"] is not None:
        s += 0.1
    return s


def _pick(items, budget, photo_sec, video_sec, trans):
    total = sum(cost(i, photo_sec, video_sec, trans) for i in items)
    if total <= budget:
        return items
    avg = total / len(items)
    k = max(1, int(budget / avg))
    # Zaman çizgisini k parçaya böl, her parçadan en iyisini al → dönem boyunca dengeli dağılım
    chosen = []
    n = len(items)
    for b in range(k):
        lo, hi = b * n // k, (b + 1) * n // k
        if hi > lo:
            chosen.append(max(items[lo:hi], key=_score))
    return chosen


def _count_global_dups(start, end, folder):
    q = "SELECT COUNT(*) FROM media WHERE dup_of IS NOT NULL AND taken_at BETWEEN ? AND ?"
    args = [f"{start} 00:00:00", f"{end} 23:59:59"]
    if folder:
        q += " AND (folder = ? OR folder LIKE ?)"
        args += [folder, folder.rstrip("/") + "/%"]
    with db.db() as con:
        return con.execute(q, args).fetchone()[0]


def plan(start, end, folder=None, target_minutes=15, photo_sec=3.5, video_sec=7.0, title=None,
         style="sinematik"):
    st = styles.get(style)
    trans = st["transition"]
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    stats = {"total": 0, "duplicates": 0, "blurry": 0, "filtered": 0, "chosen": 0,
             "cutaways": 0, "pairs": 0}
    rows = _load(start, end, folder)
    gdups = _count_global_dups(start, end, folder)
    stats["total"] = len(rows) + gdups
    rows = _filter(rows)
    stats["filtered"] = stats["total"] - gdups - len(rows)
    # Farklı adlı aynı kareler (tarama henüz arşiv geneli kopya taramasını bitirmemiş olabilir)
    local = find_duplicates(rows)
    rows = [r for r in rows if r["id"] not in local]
    stats["duplicates"] = gdups + len(local)
    rows = _dedupe(rows, stats)
    rows = _deblur(rows, stats)
    if not rows:
        return {"chapters": [], "stats": stats, "mode": None, "duration": 0}

    first = date.fromisoformat(rows[0]["taken_at"][:10])
    last = date.fromisoformat(rows[-1]["taken_at"][:10])
    mode = "day" if (last - first).days <= 45 else "month"

    groups = OrderedDict()
    for r in rows:
        dd = date.fromisoformat(r["taken_at"][:10])
        key = dd.isoformat() if mode == "day" else dd.strftime("%Y-%m")
        groups.setdefault(key, []).append(r)

    if target_minutes:
        budget = target_minutes * 60 - INTRO_SEC - OUTRO_SEC - TITLE_SEC * len(groups)
        budget = max(budget, 30)
        costs = {k: sum(cost(i, photo_sec, video_sec, trans) for i in v) for k, v in groups.items()}
        # Kalabalık günler/aylar daha çok yer alır ama sakin dönemler de kaybolmaz (karekök)
        alloc = {k: 0.0 for k in groups}
        remaining, open_keys = budget, set(groups)
        for _ in range(5):
            w = {k: math.sqrt(costs[k]) for k in open_keys}
            sw = sum(w.values()) or 1
            spent = 0
            for k in list(open_keys):
                add = remaining * w[k] / sw
                if alloc[k] + add >= costs[k]:
                    spent += costs[k] - alloc[k]
                    alloc[k] = costs[k]
                    open_keys.discard(k)
                else:
                    alloc[k] += add
                    spent += add
            remaining -= spent
            if remaining <= 1 or not open_keys:
                break
        for k in groups:
            groups[k] = _pick(groups[k], alloc[k], photo_sec, video_sec, trans)

    chapters, dur = [], INTRO_SEC + OUTRO_SEC
    for k, items in groups.items():
        if not items:
            continue
        if mode == "day":
            dd = date.fromisoformat(k)
            ttl, sub = f"{(dd - first).days + 1}. Gün", tr_date(dd)
        else:
            y, m = map(int, k.split("-"))
            ttl, sub = AYLAR[m - 1], str(y)
        units = story.build_units(items, video_sec)
        role = {}
        for u in units:
            if u["type"] == "pair":
                stats["pairs"] += 1
                for i in u["ids"]:
                    role[i] = "pair"
            elif u["type"] == "video":
                for sg in u["segments"]:
                    for c in sg["cuts"]:
                        role[c] = "cutaway"
                        stats["cutaways"] += 1
        c = sum(story.unit_cost(u, photo_sec, None) - trans * max(1, len(u.get("segments", [1])))
                for u in units)
        dur += c + TITLE_SEC
        chapters.append({"key": k, "title": ttl, "subtitle": sub,
                         "items": [_public(i, role.get(i["id"])) for i in items]})
        stats["chosen"] += len(items)

    return {"mode": mode, "chapters": chapters, "stats": stats, "duration": round(dur),
            "title": title or f"Dedplay Film · {tr_range(d0, d1)}", "style": style,
            "range_text": tr_range(d0, d1) if tr_range(d0, d1).isdigit() else tr_range(first, last)}


def _public(r, role=None):
    return {"id": r["id"], "kind": r["kind"], "taken_at": r["taken_at"], "duration": r["duration"],
            "name": os.path.basename(r["path"]), "role": role}
