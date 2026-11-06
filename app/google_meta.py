"""Google Fotoğraflar (Takeout) bilgi dosyaları: "IMG_1.jpg.supplemental-metadata.json" gibi.

İçlerindeki çekim tarihi ve konum, eksikse fotoğrafın/videonun kendisine yazılır (exiftool, dosya tarihi
korunur); sonra .json dosyası çöp kutusuna taşınır (geri alınabilir). Yazılamayan biçimlerde (ör. .avi)
.json olduğu gibi kalır: bilgi kaybolmasın.
"""
import json
import os
import subprocess
import traceback
from datetime import datetime, timezone

from . import db

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".3gp")
PHOTO_EXT = (".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".webp", ".dng")


_ls = {}


def _list(d):
    if d not in _ls:
        try:
            _ls[d] = [f for f in os.listdir(d) if f.lower().endswith(".json")]
        except OSError:
            _ls[d] = []
    return _ls[d]


def jsons_of(path):
    """Bir medya dosyasının Google bilgi dosyaları (kısaltılmış adlar dahil: .supplemental-met.json, .supp.json)."""
    d, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    out = []
    try:
        for f in _list(d):
            fl = f.lower()
            if not fl.endswith(".json"):
                continue
            if f.startswith(name + ".") or f == stem + ".json":
                out.append(os.path.join(d, f))
    except OSError:
        pass
    return out


def _read(jpath):
    try:
        with open(jpath, encoding="utf-8") as fh:
            j = json.load(fh)
    except Exception:
        return None
    if not isinstance(j, dict) or not ("photoTakenTime" in j or "geoData" in j or "creationTime" in j):
        return None    # Google bilgi dosyası değil: dokunma
    ts = int((j.get("photoTakenTime") or {}).get("timestamp", 0) or 0)
    geo = j.get("geoDataExif") or {}
    if not geo.get("latitude"):
        geo = j.get("geoData") or {}
    lat, lon, alt = geo.get("latitude") or 0.0, geo.get("longitude") or 0.0, geo.get("altitude") or 0.0
    return {"ts": ts if ts > 0 else None, "lat": lat if (lat or lon) else None, "lon": lon if (lat or lon) else None,
            "alt": alt, "desc": (j.get("description") or "").strip()}


def _has(path):
    """Dosyanın içinde tarih ve konum zaten var mı?"""
    try:
        out = subprocess.run(["exiftool", "-json", "-n", "-q", "-q", "-DateTimeOriginal", "-CreateDate",
                              "-CreationDate", "-GPSLatitude", "-GPSCoordinates", path],
                             capture_output=True, timeout=60).stdout
        m = (json.loads(out or b"[{}]") or [{}])[0]
    except Exception:
        return False, False
    date = any(str(m.get(k) or "").strip() not in ("", "0000:00:00 00:00:00") for k in
               ("DateTimeOriginal", "CreateDate", "CreationDate"))
    gps = bool(m.get("GPSLatitude") or m.get("GPSCoordinates"))
    return date, gps


def embed(path, info):
    """Eksik tarih/konumu dosyaya yazar. Dönüş: True (gerek yok ya da yazıldı) / False (yazılamadı)."""
    low = path.lower()
    if not low.endswith(VIDEO_EXT + PHOTO_EXT):
        return False
    has_date, has_gps = _has(path)
    args = []
    if info["ts"] and not has_date:
        local = datetime.fromtimestamp(info["ts"]).strftime("%Y:%m:%d %H:%M:%S")
        utc = datetime.fromtimestamp(info["ts"], timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
        if low.endswith(VIDEO_EXT):
            args += [f"-QuickTime:CreateDate={utc}", f"-QuickTime:ModifyDate={utc}",
                     f"-Keys:CreationDate={local}"]
        else:
            args += [f"-DateTimeOriginal={local}", f"-CreateDate={local}"]
    if info["lat"] is not None and not has_gps:
        lat, lon = info["lat"], info["lon"]
        if low.endswith(VIDEO_EXT):
            args += [f"-Keys:GPSCoordinates={lat}, {lon}, {info['alt'] or 0}",
                     f"-UserData:GPSCoordinates={lat}, {lon}, {info['alt'] or 0}"]
        else:
            args += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                     f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]
    if not args:
        return True
    try:
        r = subprocess.run(["exiftool", "-q", "-q", "-m", "-P", "-overwrite_original", *args, path],
                           capture_output=True, timeout=300)
        if r.returncode != 0:
            return False
        d2, g2 = _has(path)   # gerçekten yazıldı mı? yazılmadıysa .json kalsın (bilgi kaybolmasın)
        return (d2 or not info["ts"]) and (g2 or info["lat"] is None)
    except Exception:
        traceback.print_exc()
        return False


def clean(rows, trash_one, progress=None):
    """rows: [{id, path}] arşivdeki medya. Bilgiyi dosyaya yazar, .json'u trash_one(json_path, media) ile çöpe atar."""
    _ls.clear()
    rows = [m for m in rows if _list(os.path.dirname(m["path"]))]
    n = 0
    for k, m in enumerate(rows):
        if progress and k % 50 == 0:
            progress(k, len(rows))
        for jp in jsons_of(m["path"]):
            info = _read(jp)
            if info is None:
                continue
            if embed(m["path"], info):
                try:
                    trash_one(jp, m)
                    n += 1
                except Exception:
                    traceback.print_exc()
    return n


def has_any(rows):
    """Hızlı kontrol: bu dosyaların klasörlerinde .json var mı?"""
    dirs = {os.path.dirname(m["path"]) for m in rows}
    for d in dirs:
        try:
            if any(f.lower().endswith(".json") for f in os.listdir(d)):
                return True
        except OSError:
            pass
    return False
