"""Arşiv düzenleyici.

Hedef yapı (arşiv kökü = /DATA/Media):
    Photos/<yıl>/...        tüm fotoğraflar
    video/<yıl>/...         tüm videolar
    Photos/Tarihsiz, video/Tarihsiz   çekim tarihi bilinmeyenler
    Arsiv/Gelen             yeni dosyalar buraya atılır
    Arsiv/Ekran ve Kaydedilenler/Resimler|Videolar/<yıl>   ekran görüntüleri, kaydedilenler
    (Klasör adlarında Türkçe karakter yok: bazı istemciler, ör. ZimaOS, bunları seçemiyor.)
Korunan klasörler (ör. "kenan dugun", "Necla video") olduğu gibi kalır: içlerindeki dosyalar
taşınmaz ve asla çöpe atılmaz.
Gelen kutusu (Gelen): yeni dosyalar buraya atılır; birkaç dakikada bir işlenir — kopyalar çöpe,
diğerleri Resimler/Videolar'a.
Her taşıma kaydedilir ve geri alınabilir.
"""
import json
import os
import shutil
import threading
import time
import traceback
from datetime import datetime

from . import cleaner, db, scanner
from .config import BACKUP_DIR, DATA_DIR, INBOX_FEEDS, MEDIA_DIRS, TRASH_DIR

SETTINGS_FILE = os.path.join(DATA_DIR, "ayarlar.json")
DEFAULTS = {
    "photos_dir": "Photos",
    "videos_dir": "video",
    "inbox": "Arsiv/Gelen",
    "other_dir": "Arsiv/Ekran ve Kaydedilenler",
    "docs_dir": "Arsiv/Belgeler",
    "protected": ["video/kenan dugun", "video/Necla video", "Photos/muzaffer resim"],
    "include_bursts": True,
    "include_similar": True,
    "short_mode": "keep",    # fotoğrafı bulunmayan 3 sn'den kısa videolar: keep | trash
    "live_mode": "keep",     # canlı fotoğraf videoları: keep = fotoğrafın yanında, trash = çöpe
    "feeds_on": [],         # telefon adı gibi görünmeyen ama Gelen'e alınacak yedek klasörleri
    "feeds_off": [],        # telefon gibi görünen ama alınmayacak yedek klasörleri
    "auto": False,          # ilk büyük temizlikten sonra açılır
    "last_full": None,
}
# Eski klasör düzeni → yeni düzen (kayıtlı ayarlar kendiliğinden güncellenir)
OLD_LAYOUT = {"photos_dir": ["Resimler"], "videos_dir": ["Videolar"], "inbox": ["Gelen", "Arşiv/Gelen"],
              "other_dir": ["Ekran ve Kaydedilenler", "Arşiv/Ekran ve Kaydedilenler"]}
OLD_TRASH = ["Dedplay Çöp Kutusu", "Arşiv/Dedplay Çöp Kutusu"]
OLD_PARENTS = ["Arşiv"]   # boşalınca kaldırılır
state = cleaner.state
_lock = threading.Lock()


def settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    for k, olds in OLD_LAYOUT.items():
        if s.get(k) in olds:
            s[k] = DEFAULTS[k]
    return s


def save_settings(**kw):
    s = settings()
    s.update({k: v for k, v in kw.items() if k in DEFAULTS})
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    return s


def root():
    return cleaner.archive_root()


def abs_dir(rel):
    return os.path.join(root(), rel)


def protected_dirs(s=None):
    s = s or settings()
    return [abs_dir(p).rstrip("/") for p in s["protected"]]


def is_protected(path, prot=None):
    prot = protected_dirs() if prot is None else prot
    return any(path.startswith(p + "/") for p in prot)


def candidate_folders():
    """Arayüzde 'olduğu gibi kalsın' seçimi için: arşivdeki birinci/ikinci seviye klasörler."""
    s = settings()
    skip = {s["photos_dir"], s["videos_dir"], s["inbox"], s["other_dir"], s["docs_dir"]}
    out = []
    with db.db() as con:
        rows = con.execute("SELECT folder, COUNT(*) n FROM media GROUP BY folder").fetchall()
    agg = {}
    r0 = root()
    for r in rows:
        rel = os.path.relpath(r["folder"], r0)
        parts = rel.split("/")
        if len(parts) < 2 or any(rel == d or rel.startswith(d + "/") for d in skip) \
                or parts[0] == s["inbox"].split("/")[0] \
                or (parts[0] in (s["photos_dir"], s["videos_dir"]) and (parts[1].isdigit() or parts[1] == "Tarihsiz")):
            continue
        key = "/".join(parts[:2])
        agg[key] = agg.get(key, 0) + r["n"]
    for k, n in sorted(agg.items(), key=lambda x: x[0].lower()):
        out.append({"path": k, "count": n, "protected": k in s["protected"]})
    return out


# ---------- Taşıma ----------

def target_for(r, s):
    base = s["photos_dir"] if r["kind"] == "photo" else s["videos_dir"]
    if r.get("src") == "belge":   # belge, evrak, duvar/tamir fotoğrafı (elle ayrılan)
        base = s["docs_dir"]
    elif r.get("src"):  # ekran görüntüsü / kaydı ya da başka yerden kaydedilmiş
        base = os.path.join(s["other_dir"], "Resimler" if r["kind"] == "photo" else "Videolar")
    if r["date_source"] in ("exif", "google", "filename") and r["taken_at"]:
        year = r["taken_at"][:4]
    else:
        year = "Tarihsiz"
    return os.path.join(root(), base, year)


def _free_name(d, name):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return p
    stem, ext = os.path.splitext(name)
    k = 2
    while os.path.exists(os.path.join(d, f"{stem} ({k}){ext}")):
        k += 1
    return os.path.join(d, f"{stem} ({k}){ext}")


def _mv(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(src, dst)


def organize_files(batch):
    """Korunmayan, yerinde olmayan her dosyayı Photos/<yıl> veya video/<yıl>'a taşır."""
    s = settings()
    prot = protected_dirs(s)
    with db.db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id,path,folder,kind,taken_at,date_source,src,live_of,duration,size,short_photo FROM media")]
    lives = [r for r in rows if r["live_of"]]
    rows = [r for r in rows if not r["live_of"]]
    if s.get("short_mode") == "trash" and cleaner.TRASH_DIR:
        rows = _trash_short(rows, prot, batch)
    todo = []
    for r in rows:
        if is_protected(r["path"], prot) or not os.path.isfile(r["path"]):
            continue
        dst_dir = target_for(r, s)
        if r["folder"] != dst_dir:
            todo.append((r, dst_dir))
    state.update(phase="Photos ve video klasörlerine yıllara göre yerleştiriliyor", todo=len(todo), done=0)
    moved = 0
    for r, dst_dir in todo:
        try:
            dst = _free_name(dst_dir, os.path.basename(r["path"]))
            extras = []
            for sc in cleaner._sidecars(r["path"]):
                sdst = dst + sc[len(r["path"]):] if sc.startswith(r["path"]) else \
                    os.path.join(dst_dir, os.path.splitext(os.path.basename(dst))[0] + os.path.splitext(sc)[1])
                _mv(sc, sdst)
                extras.append([sc, sdst])
            _mv(r["path"], dst)
            with db.db() as con:
                con.execute("UPDATE media SET path=?, folder=? WHERE id=?", (dst, dst_dir, r["id"]))
                con.execute("INSERT INTO moves(media_id,src,dst,extras,batch) VALUES(?,?,?,?,?)",
                            (r["id"], r["path"], dst, json.dumps(extras), batch))
                con.execute("UPDATE pins SET path=? WHERE path=?", (dst, r["path"]))
                con.execute("UPDATE trash SET keeper_path=? WHERE keeper_path=?", (dst, r["path"]))
            moved += 1
        except Exception:
            traceback.print_exc()
        state["done"] += 1
    moved += _place_live(lives, s, prot, batch)
    _remove_empty_dirs(prot, s)
    fix_perms()
    return moved


def save_short_frame(r, s=None):
    """Kısa videonun arşivde benzer fotoğrafı yoksa en net karesini fotoğraf olarak kaydeder.
    Dönüş: (fotoğrafı zaten vardı mı, yeni kaydedilen fotoğrafın yolu ya da None)."""
    from . import shorts
    s = s or settings()
    r.setdefault("short_photo", None)
    has = shorts.check([r])[r["id"]]
    if has:
        return True, None
    dest = target_for({"kind": "photo", "date_source": r.get("date_source"), "taken_at": r.get("taken_at"),
                       "src": None}, s)
    return False, shorts.best_frame_to_photo(r, dest)


def _trash_short(rows, prot, batch):
    """Fotoğrafına bağlanamayan, 4 saniyeden kısa video parçalarını çöpe atar.
    Önce: arşivde benzer fotoğrafı yoksa en net karesi fotoğraf olarak kaydedilir (an kaybolmaz)."""
    keep = []
    s = settings()
    for r in rows:
        if (r["kind"] == "video" and r["duration"] is not None and r["duration"] < 4
                and not is_protected(r["path"], prot) and os.path.isfile(r["path"])):
            try:
                had, newp = save_short_frame(r, s)
                dst = cleaner._move(r["path"], os.path.join(cleaner.TRASH_DIR, batch, os.path.relpath(r["path"], root())))
                with db.db() as con:
                    con.execute("INSERT INTO trash(media_id,kind,orig_path,trash_path,size,taken_at,reason,"
                                "keeper_path,extras,batch) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (r["id"], "video", r["path"], dst, r["size"], r["taken_at"],
                                 "çok kısa video" + ("" if had else " (karesi fotoğraf olarak kaydedildi)"),
                                 newp or "", "[]", batch))
                    con.execute("DELETE FROM media WHERE id=?", (r["id"],))
                continue
            except Exception:
                traceback.print_exc()
        keep.append(r)
    return keep


def _place_live(lives, s, prot, batch):
    """Canlı fotoğraf videoları: fotoğrafla aynı klasöre, aynı adla (ya da ayara göre çöpe)."""
    n = 0
    with db.db() as con:
        photos = {r["id"]: dict(r) for r in con.execute("SELECT id, path FROM media WHERE kind='photo'")}
    for v in lives:
        p = photos.get(v["live_of"])
        # Fotoğrafı arşivde (diskte) yoksa bu video parçasına dokunulmaz: normal video olarak kalır
        if not p or not os.path.isfile(p["path"]) or not os.path.isfile(v["path"]) or is_protected(v["path"], prot):
            continue
        try:
            if s["live_mode"] == "trash" and cleaner.TRASH_DIR:
                dst = cleaner._move(v["path"], os.path.join(cleaner.TRASH_DIR, batch,
                                                            os.path.relpath(v["path"], root())))
                with db.db() as con:
                    con.execute("INSERT INTO trash(media_id,kind,orig_path,trash_path,size,taken_at,reason,"
                                "keeper_path,extras,batch) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (v["id"], "video", v["path"], dst, os.path.getsize(dst), v["taken_at"],
                                 "canlı fotoğraf videosu", p["path"], "[]", batch))
                    con.execute("DELETE FROM media WHERE id=?", (v["id"],))
                n += 1
                continue
            want = os.path.splitext(p["path"])[0] + os.path.splitext(v["path"])[1]
            if v["path"] == want:
                continue
            dst = want if not os.path.exists(want) else _free_name(os.path.dirname(want), os.path.basename(want))
            _mv(v["path"], dst)
            with db.db() as con:
                con.execute("UPDATE media SET path=?, folder=? WHERE id=?", (dst, os.path.dirname(dst), v["id"]))
                con.execute("INSERT INTO moves(media_id,src,dst,extras,batch) VALUES(?,?,?,?,?)",
                            (v["id"], v["path"], dst, "[]", batch))
            n += 1
        except Exception:
            traceback.print_exc()
    return n


def google_pass(batch):
    """Google Takeout .json bilgi dosyaları: tarih/konum dosyaya yazılır, .json çöpe gider."""
    if not TRASH_DIR:
        return 0
    from . import google_meta
    with db.db() as con:
        rows = [dict(r) for r in con.execute("SELECT id, path, taken_at FROM media")]
    if not google_meta.has_any(rows):
        return 0
    state.update(phase="Google bilgi dosyaları (.json) fotoğraflara işleniyor", done=0, todo=len(rows))
    n = google_meta.clean(rows, lambda jp, m: cleaner.trash_meta(jp, m, batch),
                          lambda d, t: state.update(done=d, todo=t))
    if n:
        print(f"Google bilgi dosyası çöpe taşındı: {n}", flush=True)
    return n


def _remove_empty_dirs(prot, s):
    """Taşımadan sonra boş kalan eski klasörleri kaldırır (korunanlar, kökler ve Gelen hariç)."""
    cur = {abs_dir(s["inbox"]), abs_dir(s["photos_dir"]), abs_dir(s["videos_dir"]), abs_dir(s["other_dir"]),
           abs_dir(s["docs_dir"])}
    old = {abs_dir(v) for vs in OLD_LAYOUT.values() for v in vs} - cur      # eski düzenin klasörleri boşalınca kaldırılır
    keep = ({d.rstrip("/") for d in MEDIA_DIRS} - old) | cur
    junk = {".DS_Store", "Thumbs.db", "desktop.ini"}
    for base in MEDIA_DIRS:
        for d, dirs, files in os.walk(base, topdown=False):
            if d.rstrip("/") in keep or any(d == p or d.startswith(p + "/") for p in prot) \
                    or (TRASH_DIR and (d == TRASH_DIR.rstrip("/") or d.startswith(TRASH_DIR.rstrip("/") + "/"))):
                continue
            try:
                left = [f for f in os.listdir(d) if f not in junk and not f.startswith("._")]
                if not left:
                    for f in os.listdir(d):
                        os.remove(os.path.join(d, f))
                    os.rmdir(d)
            except OSError:
                pass


def reclassify(ids, src):
    """Galeriden seçilenleri belge / normal fotoğraf olarak işaretle ve hemen yerine taşı (arka planda)."""
    ids = list(ids)
    with db.db() as con:
        con.executemany("UPDATE media SET src=?, src_manual=1 WHERE id=?", [(src, i) for i in ids])

    def go():
        while True:
            with _lock:
                if not state["running"] and not scanner.state["running"]:
                    state.update(running=True, phase="Seçilenler yerine taşınıyor", done=0, todo=0, error=None,
                                 kind="run")
                    break
            time.sleep(5)
        try:
            organize_files(datetime.now().strftime("%Y-%m-%d %H.%M") + (" Belgelere" if src else " Fotoğraflara"))
            state["phase"] = "Tamamlandı"
        except Exception as e:
            traceback.print_exc()
            state.update(error=str(e), phase="Hata")
        finally:
            state.update(running=False, finished=time.time())
    threading.Thread(target=go, daemon=True).start()
    return len(ids)


def migrate_layout():
    """Eski Gelen ve 'Ekran ve Kaydedilenler' klasörlerini olduğu gibi Arşiv altına taşır (anında, aynı disk)
    ve kayıtlardaki yolları günceller. Resimler/Videolar içindekiler sıradaki düzenlemede Photos/video'ya gider."""
    s = settings()
    for k, o in [(k, abs_dir(o)) for k in ("inbox", "other_dir") for o in OLD_LAYOUT[k]]:
        n = abs_dir(s[k])
        if o == n or not os.path.isdir(o):
            continue
        if os.path.exists(n) and os.listdir(n):
            continue        # ikisi de doluysa dosyalar tek tek düzenlemede taşınır
        try:
            os.makedirs(os.path.dirname(n), exist_ok=True)
            if os.path.isdir(n):
                os.rmdir(n)
            os.rename(o, n)
        except OSError:
            traceback.print_exc()
            continue
        op, np_ = o.rstrip("/") + "/", n.rstrip("/") + "/"
        L = len(op)
        with db.db() as con:
            con.execute("UPDATE media SET path=? || substr(path, ?) WHERE substr(path, 1, ?) = ?", (np_, L + 1, L, op))
            con.execute("UPDATE media SET folder=? || substr(folder, ?) WHERE folder=? OR substr(folder, 1, ?) = ?",
                        (n.rstrip("/"), L, o.rstrip("/"), L, op))
            con.execute("UPDATE trash SET orig_path=? || substr(orig_path, ?) WHERE substr(orig_path, 1, ?) = ?",
                        (np_, L + 1, L, op))
        print(f"Klasör taşındı: {o} → {n}", flush=True)
    _migrate_trash()


def _migrate_trash():
    """Çöp kutusu klasörü yer değiştirdiyse (ör. Arşiv altına) içindekileri olduğu gibi taşı, kayıtları güncelle."""
    if not TRASH_DIR:
        return
    n = TRASH_DIR.rstrip("/")
    for o in [os.path.join(root(), x) for x in OLD_TRASH]:
        if o != n and os.path.isdir(o) and not (os.path.isdir(n) and os.listdir(n)):
            _move_trash_dir(o, n)
    for par in OLD_PARENTS:   # eski üst klasör (ör. "Arşiv") boşaldıysa kaldır
        d = abs_dir(par)
        try:
            if os.path.isdir(d) and not [f for f in os.listdir(d) if f not in (".DS_Store",) and not f.startswith("._")]:
                shutil.rmtree(d)
        except OSError:
            pass


def _move_trash_dir(o, n):
    try:
        os.makedirs(os.path.dirname(n), exist_ok=True)
        if os.path.isdir(n):
            os.rmdir(n)
        os.rename(o, n)
    except OSError:
        traceback.print_exc()
        return
    op, np_ = o + "/", n + "/"
    fix = lambda p: np_ + p[len(op):] if isinstance(p, str) and p.startswith(op) else p
    with db.db() as con:
        for r in con.execute("SELECT id, trash_path, extras FROM trash").fetchall():
            ex = r["extras"]
            try:
                ex2 = json.dumps([[a, fix(b)] for a, b in json.loads(ex)]) if ex else ex
            except Exception:
                ex2 = ex
            con.execute("UPDATE trash SET trash_path=?, extras=? WHERE id=?", (fix(r["trash_path"]), ex2, r["id"]))
    print(f"Çöp kutusu taşındı: {o} → {n}", flush=True)


def fix_perms():
    """Uygulamanın (root) oluşturduğu klasör ve dosyaları arşivin sahibine verir (ör. 1000:samba) ve
    herkese açar; böylece ZimaOS istemcisinde, Samba'da, Finder'da seçilip düzenlenebilirler."""
    try:
        st = os.stat(root())
    except OSError:
        return
    uid, gid = st.st_uid, st.st_gid
    if uid == 0:
        return
    bases = list(MEDIA_DIRS) + ([TRASH_DIR] if TRASH_DIR else [])
    for base in bases:
        if not os.path.isdir(base):
            continue
        for d, dirs, files in os.walk(base):
            for p, mode in [(d, 0o777)] + [(os.path.join(d, f), 0o666) for f in files]:
                try:
                    s = os.lstat(p)
                    if s.st_uid == 0:
                        os.lchown(p, uid, gid)
                        os.chmod(p, mode if os.path.isdir(p) else (s.st_mode & 0o777) | 0o666)
                except OSError:
                    pass


def undo_moves(batch):
    """Bir düzenleme turunu geri alır: dosyalar eski klasörlerine döner."""
    with db.db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM moves WHERE batch=? AND undone=0 ORDER BY id DESC", (batch,))]
    n = 0
    for m in rows:
        if not os.path.isfile(m["dst"]):
            continue
        back = _free_name(os.path.dirname(m["src"]), os.path.basename(m["src"]))
        _mv(m["dst"], back)
        for src, dst in json.loads(m["extras"] or "[]"):
            if os.path.exists(dst):
                _mv(dst, src)
        with db.db() as con:
            con.execute("UPDATE media SET path=?, folder=? WHERE id=?", (back, os.path.dirname(back), m["media_id"]))
            con.execute("UPDATE moves SET undone=1 WHERE id=?", (m["id"],))
        n += 1
    return n


def move_batches():
    with db.db() as con:
        return [dict(r) for r in con.execute(
            "SELECT batch, COUNT(*) n, SUM(undone) undone FROM moves GROUP BY batch ORDER BY batch DESC LIMIT 20")]


# ---------- Tam akış ----------

def _faces_later():
    """Yeni fotoğraflarda yüzleri arka planda bul ve kişilere ekle (Kişiler sekmesi)."""
    try:
        from . import faces
        faces.run_all()
    except Exception:
        traceback.print_exc()


def apply_and_organize():
    """Onaydan sonra: bulunan kopyaları çöpe at, kalanları düzenle."""
    with _lock:
        if state["running"]:
            return False
        state.update(running=True, phase="Çöp kutusuna taşınıyor", done=0, todo=0, error=None, kind="apply")

    def go():
        batch = datetime.now().strftime("%Y-%m-%d %H.%M") + " Elle temizlik"
        try:
            cleaner._apply(keep_running=True, batch=batch)
            organize_files(batch)
            google_pass(batch)
            save_settings(auto=True, last_full=batch)
            state.update(phase="Tamamlandı", last_run=batch)
            _faces_later()
        except Exception as e:
            traceback.print_exc()
            state.update(error=str(e), phase="Hata")
        finally:
            state.update(running=False, finished=time.time())
    threading.Thread(target=go, daemon=True).start()
    return True


def run(full=True, apply_trash=True, scope=None):
    """Tara → kopyaları bul → çöpe at → düzenle. Arka planda."""
    with _lock:
        if state["running"]:
            return False
        state.update(running=True, phase="Başlıyor", done=0, todo=0, error=None, kind="run")
    threading.Thread(target=_run, args=(full, apply_trash, scope), daemon=True).start()
    return True


def _run(full, apply_trash, scope):
    # Her tur çöp kutusunda kendi klasörüne gider: "2026-11-05 14.30 Gelen" / "... Arşiv taraması"
    batch = datetime.now().strftime("%Y-%m-%d %H.%M") + (" Arşiv taraması" if full else " Gelen")
    try:
        s = settings()
        os.makedirs(abs_dir(s["inbox"]), exist_ok=True)
        state["phase"] = "Arşiv taranıyor"
        scanner.scan_now(None if full else scope)
        state["phase"] = "Kopyalar aranıyor"
        cleaner._find(s["include_bursts"], keep_running=True, include_similar=s["include_similar"])
        if apply_trash:
            cleaner._apply(keep_running=True, batch=batch)
            organize_files(batch)
            google_pass(batch)
            if full:
                save_settings(auto=True, last_full=batch)
        state["phase"] = "Tamamlandı"
        state["last_run"] = batch
        _faces_later()
    except Exception as e:
        traceback.print_exc()
        state.update(error=str(e), phase="Hata")
    finally:
        state.update(running=False, finished=time.time())


# ---------- Gelen kutusu izleyici ----------

def _inbox_ready(path, quiet=120):
    """Gelen kutusunda kopyalanması bitmiş (en az 2 dk dokunulmamış) medya var mı?"""
    now = time.time()
    found = False
    for d, _, files in os.walk(path):
        for f in files:
            if f.startswith("."):
                continue
            if scanner._kind(f):
                try:
                    if now - os.path.getmtime(os.path.join(d, f)) < quiet:
                        return False       # hâlâ kopyalanıyor olabilir, bekle
                except OSError:
                    continue
                found = True
    return found


import re as _re
PHONE_RE = _re.compile(r"iphone|ipad|android|galaxy|pixel|redmi|xiaomi|huawei|honor|oneplus|oppo|vivo|realme|"
                       r"motorola|nokia|sony|xperia|samsung|\bsm-|phone|telefon", _re.I)


def feed_candidates():
    """/DATA/Backup altındaki klasörler: telefon yedeği gibi görünenler kendiliğinden açık."""
    s = settings()
    env = {os.path.basename(p.rstrip("/")) for p in INBOX_FEEDS}
    out = []
    try:
        names = sorted(n for n in os.listdir(BACKUP_DIR)
                       if not n.startswith((".", "$")) and os.path.isdir(os.path.join(BACKUP_DIR, n)))
    except OSError:
        names = []
    for n in names:
        phone = bool(PHONE_RE.search(n)) or n in env
        on = (phone or n in s["feeds_on"]) and n not in s["feeds_off"]
        cnt = 0
        for d, _, files in os.walk(os.path.join(BACKUP_DIR, n)):
            cnt += sum(1 for f in files if not f.startswith(".") and scanner._kind(f))
            if cnt > 9999:
                break
        out.append({"name": n, "path": os.path.join(BACKUP_DIR, n), "phone": phone, "on": on, "count": cnt})
    return out


def feed_dirs():
    dirs = [c["path"] for c in feed_candidates() if c["on"]]
    return dirs + [p for p in INBOX_FEEDS if os.path.dirname(p.rstrip("/")) != BACKUP_DIR.rstrip("/")]


def set_feed(name, on):
    s = settings()
    fon, foff = [x for x in s["feeds_on"] if x != name], [x for x in s["feeds_off"] if x != name]
    if on:
        fon.append(name)
    else:
        foff.append(name)
    save_settings(feeds_on=fon, feeds_off=foff)


_TMP_EXT = (".tmp", ".part", ".partial", ".download", ".crdownload", ".uploading")


def feed_pass(quiet=90):
    """Besleyen klasörlerdeki (ör. /DATA/Backup/iPhone 14 Plus) yüklenmesi bitmiş dosyaları
    Gelen/<klasör adı>/ altına taşır; gerisini Gelen akışı yapar (kopya → çöp, yeni → Photos/video)."""
    feeds = feed_dirs()
    if not feeds:
        return 0
    inbox = abs_dir(settings()["inbox"])
    now, n = time.time(), 0
    for feed in feeds:
        if not os.path.isdir(feed):
            continue
        dest_root = os.path.join(inbox, os.path.basename(feed.rstrip("/")))
        for d, dirs, files in os.walk(feed):
            dirs[:] = [x for x in dirs if not x.startswith(".") and not x.startswith("$")]
            for f in files:
                if f.startswith(".") or f.lower().endswith(_TMP_EXT):
                    continue
                src = os.path.join(d, f)
                try:
                    st = os.stat(src)
                except OSError:
                    continue
                if now - st.st_ctime < quiet or now - st.st_mtime < quiet:
                    continue           # hâlâ yükleniyor olabilir
                dd = os.path.join(dest_root, os.path.relpath(d, feed))
                os.makedirs(dd, exist_ok=True)
                dst = _free_name(dd, f)
                try:
                    try:
                        os.rename(src, dst)
                    except OSError:    # farklı bağlama noktası: önce gizli adla kopyala, sonra aç
                        tmp = os.path.join(dd, "." + os.path.basename(dst) + ".dp-part")
                        shutil.copy2(src, tmp)
                        os.rename(tmp, dst)
                        os.remove(src)
                    n += 1
                except OSError:
                    traceback.print_exc()
        for d, dirs, files in os.walk(feed, topdown=False):   # boşalan alt klasörler
            if d.rstrip("/") != feed.rstrip("/"):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
    if n:
        print(f"Besleyen klasörlerden Gelen'e taşındı: {n}", flush=True)
        fix_perms()
    return n


def _feeds():
    while True:
        time.sleep(60)
        try:
            if settings()["auto"]:
                feed_pass()
        except Exception:
            traceback.print_exc()


def _watch():
    last_full = time.time()
    while True:
        time.sleep(300)
        try:
            s = settings()
            if not s["auto"] or state["running"] or scanner.state["running"]:
                continue
            inbox = abs_dir(s["inbox"])
            if os.path.isdir(inbox) and _inbox_ready(inbox):
                run(full=False, scope=[inbox])
            elif time.time() - last_full > 6 * 3600:   # 6 saatte bir tüm arşiv (başka yerlere eklenenler)
                last_full = time.time()
                run(full=True)
        except Exception:
            traceback.print_exc()


def _old_left():
    s = settings()
    return any(os.path.isdir(abs_dir(o)) and abs_dir(o) != abs_dir(s[k])
               for k in ("photos_dir", "videos_dir") for o in OLD_LAYOUT[k])


def _migrate_soon():
    """Eski Resimler/Videolar klasörlerinde dosya kaldıysa, süren iş bitince doğrudan yerine taşı
    (kopya taraması beklenmez; sadece taşıma — dakikalar sürer)."""
    time.sleep(30)
    while _old_left():
        try:
            with _lock:
                busy = state["running"] or scanner.state["running"]
                if not busy:
                    state.update(running=True, phase="Photos ve video klasörlerine taşınıyor", done=0, todo=0,
                                 error=None, kind="run")
            if busy:
                time.sleep(30)
                continue
            try:
                organize_files(datetime.now().strftime("%Y-%m-%d %H.%M") + " Yeni klasör düzeni")
                state["phase"] = "Tamamlandı"
            finally:
                state.update(running=False, finished=time.time())
            break
        except Exception:
            traceback.print_exc()
            break


try:
    migrate_layout()
except Exception:
    traceback.print_exc()
threading.Thread(target=fix_perms, daemon=True).start()
threading.Thread(target=_feeds, daemon=True).start()


def _google_soon():
    """Güncellemeden sonra bir kez: arşivdeki Google .json dosyalarını hemen işle (6 saat beklemeden)."""
    time.sleep(90)
    while True:
        with _lock:
            if not state["running"] and not scanner.state["running"]:
                state.update(running=True, phase="Google bilgi dosyaları", done=0, todo=0, error=None, kind="run")
                break
        time.sleep(30)
    try:
        google_pass(datetime.now().strftime("%Y-%m-%d %H.%M") + " Google bilgi dosyaları")
        state["phase"] = "Tamamlandı"
    except Exception as e:
        traceback.print_exc()
        state.update(error=str(e), phase="Hata")
    finally:
        state.update(running=False, finished=time.time())


threading.Thread(target=_google_soon, daemon=True).start()
threading.Thread(target=_migrate_soon, daemon=True).start()
threading.Thread(target=_watch, daemon=True).start()
