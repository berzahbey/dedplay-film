"""Arşiv tarayıcı: klasörleri gezer, her dosyanın çekim tarihini ve konumunu veritabanına yazar."""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

from . import db
from .analyze import analyze_item
from .config import MEDIA_DIRS, PHOTO_EXT, VIDEO_EXT, SKIP_DIRS

state = {"running": False, "phase": "", "found": 0, "done": 0, "todo": 0, "new": 0, "removed": 0,
         "analyzed": 0, "duplicates": 0,
         "started": None, "finished": None, "error": None}
_thread = None

DATE_TAGS = ["DateTimeOriginal", "SubSecDateTimeOriginal", "CreateDate", "MediaCreateDate",
             "TrackCreateDate", "CreationDate"]
FN_DT = re.compile(r"(19\d{2}|20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
                   r"(?:[ _\-T]?([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d))?")


def _kind(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    return None


def _parse_exif_date(v):
    if not v or not isinstance(v, str):
        return None
    m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", v)
    if not m:
        return None
    y, mo, d, h, mi, s = map(int, m.groups())
    if y < 1990 or mo == 0 or d == 0:
        return None
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def _date_from_filename(name):
    m = FN_DT.search(name)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    h, mi, s = (int(m.group(i)) if m.group(i) else 12 if i == 4 else 0 for i in (4, 5, 6))
    try:
        dt = datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
    return dt if datetime(1990, 1, 1) < dt < datetime.now() else None


def _google_sidecar(path):
    """Google Takeout dosyalarında tarih genelde yanındaki .json içindedir."""
    d, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    cands = [path + ".json", path + ".supplemental-metadata.json",
             os.path.join(d, stem + ".json")]
    try:
        for f in os.listdir(d):
            if f.endswith(".json") and f.startswith(name[:40]):
                cands.append(os.path.join(d, f))
    except OSError:
        pass
    for c in cands:
        if os.path.isfile(c):
            try:
                with open(c, encoding="utf-8") as fh:
                    j = json.load(fh)
                ts = int(j.get("photoTakenTime", {}).get("timestamp", 0))
                if ts > 0:
                    geo = j.get("geoData") or {}
                    lat, lon = geo.get("latitude") or None, geo.get("longitude") or None
                    return datetime.fromtimestamp(ts), lat, lon
            except Exception:
                continue
    return None


def _exiftool(paths):
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
        f.write("\n".join(paths))
        argfile = f.name
    try:
        cmd = ["exiftool", "-json", "-n", "-fast", "-q", "-q", "-charset", "filename=utf8",
               "-api", "QuickTimeUTC=1", "-api", "LargeFileSupport=1",
               *[f"-{t}" for t in DATE_TAGS],
               "-GPSLatitude", "-GPSLongitude", "-ImageWidth", "-ImageHeight",
               "-Orientation", "-Rotation", "-Duration", "-@", argfile]
        out = subprocess.run(cmd, capture_output=True, timeout=900).stdout
        try:
            return {r["SourceFile"]: r for r in json.loads(out or b"[]")}
        except json.JSONDecodeError:
            return {}
    finally:
        os.unlink(argfile)


def _build_row(path, st, meta):
    kind = _kind(path)
    taken, src, lat, lon = None, None, None, None
    tags = DATE_TAGS if kind == "photo" else ["CreationDate"] + [t for t in DATE_TAGS if t != "CreationDate"]
    for t in tags:
        taken = _parse_exif_date(meta.get(t))
        if taken:
            src = "exif"
            break
    lat, lon = meta.get("GPSLatitude"), meta.get("GPSLongitude")
    if not taken:
        g = _google_sidecar(path)
        if g:
            taken, src = g[0], "google"
            lat, lon = lat or g[1], lon or g[2]
    if not taken:
        taken = _date_from_filename(os.path.basename(path))
        src = "filename" if taken else None
    if not taken:
        taken, src = datetime.fromtimestamp(st.st_mtime), "mtime"
    w, h = meta.get("ImageWidth"), meta.get("ImageHeight")
    if meta.get("Orientation") in (5, 6, 7, 8) or meta.get("Rotation") in (90, 270, -90):
        w, h = h, w
    dur = meta.get("Duration")
    try:
        dur = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat, lon) != (0, 0):
        pass
    else:
        lat = lon = None
    return (path, os.path.dirname(path), kind, st.st_size, st.st_mtime,
            taken.strftime("%Y-%m-%d %H:%M:%S"), src, lat, lon, w, h, dur)


def _walk(roots=None):
    for root in roots or MEDIA_DIRS:
        if not os.path.isdir(root):
            continue
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                p = os.path.join(d, f)
                if _kind(p):
                    yield p


def _orient_item(args):
    from .orient import check_video
    mid, path, dur = args
    b, a = check_video(path, dur)
    return mid, b, a


def _source_pass():
    """Kamera bilgisi (Make/Model) ve ekran görüntüsü işaretleri → kaynak türü (ekran / kaydedilen / kamera)."""
    from .sources import classify
    with db.db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id,path,kind,width,height,lat FROM media WHERE src_checked IS NULL")]
    if not rows:
        return
    state["phase"] = "Ekran görüntüleri ve kaydedilen dosyalar ayırt ediliyor"
    state["todo"], state["done"] = len(rows), 0
    for i in range(0, len(rows), 300):
        part = rows[i:i + 300]
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
            f.write("\n".join(r["path"] for r in part))
            argfile = f.name
        try:
            out = subprocess.run(["exiftool", "-json", "-fast", "-q", "-q", "-charset", "filename=utf8",
                                  "-Make", "-Model", "-Software", "-UserComment", "-AndroidMake", "-AndroidModel",
                                  "-ContentIdentifier", "-MediaGroupUUID",
                                  "-@", argfile], capture_output=True, timeout=900).stdout
            meta = {m["SourceFile"]: m for m in json.loads(out or b"[]")}
        except Exception:
            meta = {}
        finally:
            os.unlink(argfile)
        upd = []
        for r in part:
            m = meta.get(r["path"], {})
            r["make"] = str(m.get("Make") or m.get("AndroidMake") or "")[:60]
            r["model"] = str(m.get("Model") or m.get("AndroidModel") or "")[:60]
            r["software"] = str(m.get("Software") or "")[:80]
            r["ucomment"] = str(m.get("UserComment") or "")[:80]
            live = m.get("ContentIdentifier") or m.get("MediaGroupUUID")
            upd.append((r["make"], r["model"], r["software"], r["ucomment"], classify(r),
                        str(live) if live else None, r["id"]))
        with db.db() as con:
            con.executemany("UPDATE media SET make=?,model=?,software=?,ucomment=?,src=?,live_id=?,src_checked=1 "
                            "WHERE id=?", upd)
        state["done"] += len(part)


_SUFFIX = re.compile(r"(\s*\(\d+\)|-edited|-düzenlendi)$", re.I)


def _stem(p):
    st = os.path.splitext(os.path.basename(p))[0].lower()
    return _SUFFIX.sub("", st)


def link_live():
    """Canlı fotoğrafların (Live Photo) 1-4 sn'lik video parçalarını fotoğraflarına bağlar.
    Sıra: 1) Apple'ın ortak kimliği (ContentIdentifier)  2) aynı ad (eki "(2)" vb. atılmış) ve
    ±3 sn içinde çekilmiş  3) adı farklı olsa da aynı saniyede (±1 sn) çekilmiş bir fotoğraf.
    Klasörleri ayrılmış olsalar bile (Resimler/Videolar) eşleşir."""
    import bisect
    with db.db() as con:
        photos = [dict(r) for r in con.execute(
            "SELECT id,path,folder,taken_at,live_id FROM media WHERE kind='photo' AND taken_at IS NOT NULL")]
        vids = [dict(r) for r in con.execute(
            "SELECT id,path,folder,taken_at,live_id,duration FROM media WHERE kind='video' AND "
            "(duration IS NULL OR duration <= 6)")]

    def ts(x):
        try:
            return datetime.fromisoformat(x).timestamp()
        except (TypeError, ValueError):
            return None

    by_live = {p["live_id"]: p["id"] for p in photos if p["live_id"]}
    by_folder = {(p["folder"], _stem(p["path"])): p for p in photos}
    timeline = sorted(((ts(p["taken_at"]), p) for p in photos if ts(p["taken_at"]) is not None), key=lambda x: x[0])
    by_stem = {}
    for t, p in timeline:
        by_stem.setdefault(_stem(p["path"]), []).append((t, p))
    times = [t for t, _ in timeline]
    links = []
    for v in vids:
        pid = by_live.get(v["live_id"]) if v["live_id"] else None
        if pid is None and (v["duration"] or 0) <= 4.5:
            # aynı klasörde aynı adlı fotoğraf → kesin eşleşme (tarih bilgisi olmasa da)
            p = by_folder.get((v["folder"], _stem(v["path"])))
            if p:
                pid = p["id"]
        if pid is None and (v["duration"] or 0) <= 4.5:
            tv = ts(v["taken_at"])
            if tv is not None:
                cands = []
                # aynı ad: saat dilimi farkı olabilir (video UTC'den çevrilmiş, fotoğraf yerel saat) →
                # tam saat farkları da kabul (seyahatte çekilenler)
                for t, p in by_stem.get(_stem(v["path"]), []):
                    d = abs(t - tv)
                    off = min(d % 3600, 3600 - d % 3600)
                    if d <= 14 * 3600 + 3 and off <= 3:
                        cands.append((0, d, p))
                i = bisect.bisect_left(times, tv - 3)
                while i < len(times) and times[i] <= tv + 3:
                    p = timeline[i][1]
                    dt = abs(times[i] - tv)
                    if dt <= 1 and _stem(p["path"]) != _stem(v["path"]):
                        cands.append((1, dt, p))
                    i += 1
                if cands:
                    pid = min(cands, key=lambda c: (c[0], c[1]))[2]["id"]
        links.append((pid, v["id"]))
    with db.db() as con:
        con.executemany("UPDATE media SET live_of=? WHERE id=?", links)
        con.execute("UPDATE media SET src=NULL WHERE live_of IS NOT NULL")
    return sum(1 for p, _ in links if p)


def dedupe_all():
    """Arşiv genelinde kopya gruplarını yeniden hesaplar ve dup_of alanını günceller."""
    from .dedupe import find_duplicates
    with db.db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id,kind,dhash,date_source,width,height,duration,sharp FROM media WHERE dhash IS NOT NULL")]
    dups = find_duplicates(rows)
    with db.db() as con:
        con.execute("UPDATE media SET dup_of=NULL WHERE dup_of IS NOT NULL")
        con.executemany("UPDATE media SET dup_of=? WHERE id=?", [(v, k) for k, v in dups.items()])
    state["duplicates"] = len(dups)
    return len(dups)


def _analyze_ids(pool, items):
    """items: [(id, kind, path, duration)] → netlik/iz hesaplanır, veritabanına yazılır."""
    if not items:
        return
    res = list(pool.map(analyze_item, items, chunksize=8))
    with db.db() as con:
        con.executemany("UPDATE media SET sharp=?,dhash=?,analyzed=1 WHERE id=?",
                        [(s, h, mid) for mid, s, h in res])
    state["analyzed"] += len(res)


def _under(p, roots):
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)


def _scan(roots=None):
    state.update(running=True, phase="Dosyalar listeleniyor", found=0, done=0, todo=0, new=0,
                 removed=0, analyzed=0, started=time.time(), finished=None, error=None)
    workers = int(os.environ.get("ANALYZE_WORKERS", max(1, (os.cpu_count() or 2) // 2)))
    try:
        with db.db() as con:
            known = {r["path"]: (r["size"], r["mtime"]) for r in con.execute("SELECT path,size,mtime FROM media")}
        if roots:  # sadece belirli klasörler taranıyor (ör. Gelen kutusu)
            known = {p: v for p, v in known.items() if _under(p, roots)}
        seen, todo = set(), []
        for p in _walk(roots):
            seen.add(p)
            state["found"] += 1
            try:
                st = os.stat(p)
            except OSError:
                continue
            if known.get(p) != (st.st_size, st.st_mtime):
                todo.append((p, st))
        gone = [p for p in known if p not in seen]
        if gone:
            with db.db() as con:
                con.executemany("DELETE FROM media WHERE path=?", [(p,) for p in gone])
        state["removed"] = len(gone)

        # En yeni dosyalar önce: son yıllar hemen kullanılabilir olur
        todo.sort(key=lambda t: t[1].st_mtime, reverse=True)
        state["todo"] = len(todo)
        state["phase"] = "Taranıyor (bu sırada film yapabilirsin)"
        B = 200
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for n, i in enumerate(range(0, len(todo), B)):
                chunk = todo[i:i + B]
                metas = _exiftool([p for p, _ in chunk])
                rows = [_build_row(p, st, metas.get(p, {})) for p, st in chunk]
                with db.db() as con:
                    con.executemany(
                        "INSERT INTO media(path,folder,kind,size,mtime,taken_at,date_source,lat,lon,width,height,duration) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                        "size=excluded.size,mtime=excluded.mtime,taken_at=excluded.taken_at,"
                        "analyzed=0,date_source=excluded.date_source,lat=excluded.lat,lon=excluded.lon,"
                        "width=excluded.width,height=excluded.height,duration=excluded.duration", rows)
                    items = [(r["id"], r["kind"], r["path"], r["duration"]) for r in con.execute(
                        f"SELECT id,kind,path,duration FROM media WHERE path IN ({','.join('?' * len(chunk))})",
                        [p for p, _ in chunk])]
                _analyze_ids(pool, items)
                state["done"] += len(chunk)
                state["new"] += len(chunk)
                if n % 10 == 9:
                    dedupe_all()
            # Önceki taramalardan analiz edilmemiş kalanlar
            with db.db() as con:
                rest = [(r["id"], r["kind"], r["path"], r["duration"]) for r in
                        con.execute("SELECT id,kind,path,duration FROM media WHERE analyzed=0")]
            if rest:
                state["phase"] = "Kalan dosyalar analiz ediliyor"
                for i in range(0, len(rest), B):
                    _analyze_ids(pool, rest[i:i + B])
            # Video yönleri (yan kaydedilmiş videolar) — bir kez yapılır
            with db.db() as con:
                vids = [(r["id"], r["path"], r["duration"] or 0) for r in
                        con.execute("SELECT id,path,duration FROM media WHERE kind='video' AND meta_rot IS NULL")]
            if vids:
                state["phase"] = "Video yönleri kontrol ediliyor"
                state["todo"], state["done"] = len(vids), 0
                for i in range(0, len(vids), 40):
                    part = vids[i:i + 40]
                    res = list(pool.map(_orient_item, part))
                    with db.db() as con:
                        con.executemany("UPDATE media SET meta_rot=?, auto_rot=COALESCE(auto_rot, ?) WHERE id=?",
                                        [(b, a, mid) for mid, b, a in res])
                    state["done"] += len(part)
        _source_pass()
        link_live()
        state["phase"] = "Kopyalar ayıklanıyor"
        dedupe_all()
        state["phase"] = "Tamamlandı"
    except Exception as e:  # pragma: no cover
        traceback.print_exc()
        state["error"] = str(e)
        state["phase"] = "Hata"
    finally:
        state["running"] = False
        state["finished"] = time.time()


def start():
    global _thread
    if state["running"]:
        return False
    _thread = threading.Thread(target=_scan, daemon=True)
    _thread.start()
    return True


def scan_now(roots=None):
    """Taramayı bu iş parçacığında çalıştırır ve bitmesini bekler (düzenleyici kullanır)."""
    while state["running"]:
        time.sleep(2)
    state["running"] = True
    _scan(roots)
