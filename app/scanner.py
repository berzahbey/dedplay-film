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
    for t in DATE_TAGS:
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


def _walk():
    for root in MEDIA_DIRS:
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


def _scan():
    state.update(running=True, phase="Dosyalar listeleniyor", found=0, done=0, todo=0, new=0,
                 removed=0, analyzed=0, started=time.time(), finished=None, error=None)
    workers = int(os.environ.get("ANALYZE_WORKERS", max(1, (os.cpu_count() or 2) // 2)))
    try:
        with db.db() as con:
            known = {r["path"]: (r["size"], r["mtime"]) for r in con.execute("SELECT path,size,mtime FROM media")}
        seen, todo = set(), []
        for p in _walk():
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
