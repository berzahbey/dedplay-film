"""Kopya Temizleyici.

1. Kopya grupları:
   - Birebir aynı dosyalar (adı farklı olsa da içerik aynı)
   - Aynı görüntü: farklı boyut / sıkıştırma / ad (WhatsApp, Google Fotoğraflar kopyaları,
     yeniden kaydedilmiş videolar) — görsel iz ile
   - Seri çekim: birkaç saniye içinde art arda çekilmiş, neredeyse aynı fotoğraflar
2. Her grupta en iyi dosya kalır: gerçek çekim tarihi, çözünürlük, netlik, açık gözler, az sıkıştırma.
3. Diğerleri çöp kutusuna TAŞINIR (silinmez). Klasör yapısı korunur, geri alınabilir.
   Kalıcı silme sadece "Çöpü boşalt" ile olur.
"""
import hashlib
import json
import math
import os
import shutil
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime

from . import db
from .analyze import hamming
from .config import DATA_DIR, MEDIA_DIRS, TRASH_DIR
from .dedupe import _UF, _informative, find_duplicates

GROUPS_FILE = os.path.join(DATA_DIR, "kopya-gruplari.json")
BURST_SEC = 10        # seri çekim: art arda kareler arası en fazla süre
BURST_DIFF = 40       # seri çekimde ardışık kareler arası en fazla fark (256 bit)
BURST_ANCHOR = 52     # grubun ilk karesine göre en fazla fark (zincirleme kaymayı önler)
DATE_RANK = {"exif": 3, "google": 2, "filename": 1, "mtime": 0}
BAD_NAMES = ("whatsapp", "-wa", "_wa", "screenshot", "kopya", "copy", "(1)", "(2)", "edited", "düzenlen")

state = {"running": False, "phase": "", "done": 0, "todo": 0, "error": None, "finished": None, "kind": ""}
_lock = threading.Lock()


def archive_root():
    """Arşiv klasörlerinin ortak üst klasörü (çöpte aynı yapı kurulur)."""
    return os.path.commonpath(MEDIA_DIRS) if len(MEDIA_DIRS) > 1 else os.path.dirname(MEDIA_DIRS[0])


# ---------- Hızlı içerik izi (birebir kopyalar) ----------

def quick_hash(path):
    """Boyut + başından, ortasından, sonundan 64 KB → birebir aynı dosyaları bulur (hızlı)."""
    try:
        size = os.path.getsize(path)
        h = hashlib.sha1(str(size).encode())
        with open(path, "rb") as f:
            for pos in (0, max(0, size // 2 - 32768), max(0, size - 65536)):
                f.seek(pos)
                h.update(f.read(65536))
        return h.hexdigest()
    except OSError:
        return None


# ---------- Kalite: yüzler ve açık gözler ----------

def _eyes(args):
    """Yüz kalitesi: yüz sayısı, gözü açık yüz, gülümseyen yüz, yüzlerin netliği, pozlama."""
    mid, path = args
    try:
        import cv2
        import numpy as np
        from .analyze import load_image
        im = np.asarray(load_image(path, 1200).convert("L"))
        hc = cv2.data.haarcascades
        fc = cv2.CascadeClassifier(hc + "haarcascade_frontalface_default.xml")
        ec = cv2.CascadeClassifier(hc + "haarcascade_eye_tree_eyeglasses.xml")
        sc = cv2.CascadeClassifier(hc + "haarcascade_smile.xml")
        faces = fc.detectMultiScale(im, 1.1, 6, minSize=(48, 48))
        open_eyes = smiles = 0
        fsh = []
        for (x, y, w, h) in sorted(faces, key=lambda f: -f[2] * f[3])[:6]:
            face = im[y:y + h, x:x + w]
            top = face[: int(h * 0.6)]
            if len(ec.detectMultiScale(top, 1.1, 5, minSize=(max(8, w // 9), max(8, w // 9)))) >= 2:
                open_eyes += 1
            low = face[int(h * 0.55):]
            if len(sc.detectMultiScale(low, 1.5, 18, minSize=(max(10, w // 4), max(6, h // 10)))):
                smiles += 1
            fsh.append(float(cv2.Laplacian(face, cv2.CV_64F).var()))
        mean = float(im.mean())
        clip = float(((im < 8) | (im > 247)).mean())
        expo = abs(mean - 120) / 120 + 3 * clip          # 0 = iyi pozlama
        return mid, len(faces), open_eyes, smiles, (sum(fsh) / len(fsh) if fsh else None), round(expo, 3)
    except Exception:
        return mid, 0, 0, 0, None, None


def score(r):
    """Yüksek = daha iyi kopya."""
    name = os.path.basename(r["path"]).lower()
    px = max(1, (r["width"] or 0) * (r["height"] or 0))
    s = 1.0 * DATE_RANK.get(r["date_source"], 0)
    s += 1.0 * math.log2(px)
    s += 0.5 * math.log2(max(1.0, (r["size"] or 1) / px * 100))      # az sıkıştırılmış olan
    if r["kind"] == "photo":
        s += 1.5 * math.log10((r["sharp"] or 0) + 1)
        faces, eyes = r.get("faces") or 0, r.get("eyes") or 0
        s += 0.8 * min(eyes, 4) - 0.6 * max(0, faces - eyes)           # gözü kapalı/kaymış yüz cezası
        s += 0.5 * min(r.get("smiles") or 0, 4)                         # gülümseyen yüzler
        if r.get("fsharp"):
            s += 1.0 * math.log10(r["fsharp"] + 1)                       # yüzler net mi
        if r.get("expo") is not None:
            s -= 1.5 * r["expo"]                                         # karanlık / patlamış kareler
    else:
        s += 1.5 * math.log2((r["duration"] or 0) + 1)                 # kırpılmamış (uzun) olan
    if any(b in name for b in BAD_NAMES):
        s -= 1.0
    if "google" in r["path"].lower() or "takeout" in r["path"].lower():
        s -= 0.3                                                       # eşitlikte orijinal klasör
    s -= 0.001 * len(r["path"])
    return round(s, 4)


# ---------- Grup bulma ----------

def _bursts(photos, uf):
    rows = sorted((r for r in photos if r["date_source"] in ("exif", "google")), key=lambda r: r["taken_at"])
    anchor, prev = None, None
    for r in rows:
        t = datetime.fromisoformat(r["taken_at"])
        if prev is not None:
            dt = (t - datetime.fromisoformat(prev["taken_at"])).total_seconds()
            if (dt <= BURST_SEC and r["folder"] == prev["folder"]
                    and hamming(r["dhash"], prev["dhash"]) <= BURST_DIFF
                    and hamming(r["dhash"], anchor["dhash"]) <= BURST_ANCHOR):
                uf.union(prev["id"], r["id"])
                prev = r
                continue
        anchor, prev = r, r


def _set(**kw):
    state.update(kw)


def find(include_bursts=True):
    """Arka planda tüm arşivde kopya gruplarını bulur ve kopya-gruplari.json'a yazar."""
    with _lock:
        if state["running"]:
            return False
        state.update(running=True, phase="Hazırlanıyor", done=0, todo=0, error=None, finished=None, kind="find")
    threading.Thread(target=_find, args=(include_bursts,), daemon=True).start()
    return True


def _find(include_bursts, keep_running=False, include_similar=True):
    try:
        with db.db() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT m.id,m.path,m.folder,m.kind,m.size,m.taken_at,m.date_source,m.width,m.height,"
                "m.duration,m.sharp,m.dhash,m.qhash,m.faces,m.eyes,m.smiles,m.fsharp,m.expo,m.q_done,"
                "(p.path IS NOT NULL) AS pinned "
                "FROM media m LEFT JOIN pins p ON p.path=m.path WHERE m.live_of IS NULL")]  # canlı foto videoları fotoğrafıyla gider
        rows = [r for r in rows if os.path.exists(r["path"])]
        # 1) birebir kopyalar için hızlı iz (sadece boyutu başka bir dosyayla aynı olanlar)
        by_size = defaultdict(list)
        for r in rows:
            by_size[(r["kind"], r["size"])].append(r)
        need = [r for g in by_size.values() if len(g) > 1 for r in g if not r["qhash"]]
        _set(phase="Birebir aynı dosyalar kontrol ediliyor", todo=len(need), done=0)
        with ThreadPoolExecutor(8) as ex:
            for r, h in zip(need, ex.map(lambda r: quick_hash(r["path"]), need)):
                r["qhash"] = h
                state["done"] += 1
        if need:
            with db.db() as con:
                con.executemany("UPDATE media SET qhash=? WHERE id=?", [(r["qhash"], r["id"]) for r in need])

        _set(phase="Aynı görüntüler aranıyor", todo=0, done=0)
        uf = _UF()
        reason = {}
        exact = defaultdict(list)
        for r in rows:
            if r["qhash"]:
                exact[(r["kind"], r["size"], r["qhash"])].append(r)
        for g in exact.values():
            for r in g[1:]:
                uf.union(g[0]["id"], r["id"])
                reason[r["id"]] = reason[g[0]["id"]] = "birebir aynı"
        for a, b in find_duplicates(rows).items():
            uf.union(a, b)
            reason.setdefault(a, "aynı görüntü")
            reason.setdefault(b, "aynı görüntü")
        if include_bursts:
            _set(phase="Seri çekimler aranıyor")
            photos = [r for r in rows if r["kind"] == "photo" and r["dhash"] and len(r["dhash"]) == 64
                      and _informative(r["dhash"])]
            uf_b = _UF()
            _bursts(photos, uf_b)
            for r in photos:
                root = uf_b.find(r["id"])
                if root != r["id"]:
                    uf.union(root, r["id"])
                    reason.setdefault(r["id"], "seri çekim")
                    reason.setdefault(root, "seri çekim")

        if include_similar:
            _set(phase="Benzer kareler aranıyor (yapay görü: aynı sahne, farklı poz)", todo=0, done=0)
            from .scene import find_groups
            photos = [r for r in rows if r["kind"] == "photo"]
            for g in find_groups(photos, lambda d, t: state.update(done=d, todo=t)):
                for x in g[1:]:
                    uf.union(g[0], x)
                for x in g:
                    reason.setdefault(x, "benzer kare")

        by_id = {r["id"]: r for r in rows}
        groups = defaultdict(list)
        for i in reason:
            groups[uf.find(i)].append(by_id[i])
        groups = [g for g in groups.values() if len(g) > 1]

        # 2) seri çekim gruplarında yüz / açık göz analizi (en güzel kareyi seçmek için)
        need = [r for g in groups for r in g if r["kind"] == "photo" and not r["q_done"]
                and any(reason[x["id"]] in ("seri çekim", "benzer kare") for x in g)]
        if need:
            _set(phase="En güzel kare seçiliyor: yüzler, açık gözler, gülümseme, netlik", todo=len(need), done=0)
            workers = max(1, (os.cpu_count() or 2) // 2)
            res = []
            with ProcessPoolExecutor(workers) as ex:
                for out in ex.map(_eyes, [(r["id"], r["path"]) for r in need], chunksize=4):
                    res.append(out)
                    state["done"] += 1
            for mid, f, e, sm, fs, ex in res:
                by_id[mid].update(faces=f, eyes=e, smiles=sm, fsharp=fs, expo=ex)
            with db.db() as con:
                con.executemany("UPDATE media SET faces=?, eyes=?, smiles=?, fsharp=?, expo=?, q_done=1 WHERE id=?",
                                [(f, e, sm, fs, ex, m) for m, f, e, sm, fs, ex in res])

        from .organize import abs_dir, protected_dirs, settings
        prot = protected_dirs()
        inbox = abs_dir(settings()["inbox"]).rstrip("/") + "/"
        out = []
        for gi, g in enumerate(sorted(groups, key=lambda g: min(r["taken_at"] or "" for r in g))):
            for r in g:
                in_prot = any(r["path"].startswith(p + "/") for p in prot)
                # Korunan klasördeki ya da çöpten geri alınmış dosya asla çöpe gitmez.
                # Korunan klasördeki kopya tercihen "kalan" olur; geri alınan ise sadece dokunulmaz kalır.
                r["locked"] = bool(r.get("pinned")) or in_prot
                # Gelen'deki dosya arşivdekiyle aynı kalitedeyse arşivdeki kalır (Gelen'deki daha iyiyse o kalır)
                r["score"] = score(r) + (1000 if in_prot else 0) - (0.5 if r["path"].startswith(inbox) else 0)
            g.sort(key=lambda r: -r["score"])
            reasons = {reason[r["id"]] for r in g}
            out.append({
                "gid": gi, "kind": g[0]["kind"],
                "reason": "birebir aynı" if reasons == {"birebir aynı"} else
                          ("aynı görüntü" if "aynı görüntü" in reasons or "birebir aynı" in reasons else
                           ("seri çekim" if "seri çekim" in reasons else "benzer kare")),
                "keep": g[0]["id"], "skip": False,
                "members": [{"id": r["id"], "path": r["path"], "name": os.path.basename(r["path"]),
                             "folder": os.path.relpath(r["folder"], archive_root()), "size": r["size"],
                             "w": r["width"], "h": r["height"], "taken_at": r["taken_at"],
                             "duration": r["duration"], "score": r["score"], "locked": r["locked"]} for r in g],
            })
        _save(out)
        _set(phase="Tamamlandı")
    except Exception as e:
        traceback.print_exc()
        _set(error=str(e), phase="Hata")
        if keep_running:
            raise
    finally:
        if not keep_running:
            _set(running=False, finished=time.time())


def _save(groups):
    tmp = GROUPS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False)
    os.replace(tmp, GROUPS_FILE)


def load_groups():
    try:
        with open(GROUPS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def summary(groups=None):
    groups = load_groups() if groups is None else groups
    s = {"groups": 0, "files": 0, "bytes": 0, "photos": 0, "videos": 0,
         "by_reason": {"birebir aynı": 0, "aynı görüntü": 0, "seri çekim": 0, "benzer kare": 0}}
    for g in groups:
        if g["skip"]:
            continue
        gone = [m for m in g["members"] if m["id"] != g["keep"] and not m.get("locked")]
        if not gone:
            continue
        s["groups"] += 1
        s["files"] += len(gone)
        s["bytes"] += sum(m["size"] or 0 for m in gone)
        s["photos" if g["kind"] == "photo" else "videos"] += len(gone)
        s["by_reason"][g["reason"]] = s["by_reason"].get(g["reason"], 0) + len(gone)
    return s


def update_group(gid, keep=None, skip=None):
    groups = load_groups()
    for g in groups:
        if g["gid"] == gid:
            if keep is not None and any(m["id"] == keep for m in g["members"]):
                g["keep"] = keep
            if skip is not None:
                g["skip"] = bool(skip)
            _save(groups)
            return g
    return None


# ---------- Çöp kutusuna taşı / geri al / boşalt ----------

def _sidecars(path):
    """Fotoğrafla birlikte taşınması gereken yan dosyalar (Google .json, iPhone .AAE)."""
    d, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    out = []
    for c in (path + ".json", path + ".supplemental-metadata.json", os.path.join(d, stem + ".AAE"),
              os.path.join(d, stem + ".aae")):
        if os.path.isfile(c):
            out.append(c)
    return out


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        base, ext = os.path.splitext(dst)
        k = 2
        while os.path.exists(f"{base} ({k}){ext}"):
            k += 1
        dst = f"{base} ({k}){ext}"
    try:
        os.rename(src, dst)          # aynı diskte: anında
    except OSError:
        shutil.move(src, dst)        # farklı bağlama noktası: kopyala + sil
    return dst


def apply():
    """Atlanmayan tüm gruplarda kalacak dosya dışındakileri çöp kutusuna taşır (arka planda)."""
    if not TRASH_DIR:
        raise RuntimeError("Çöp kutusu klasörü ayarlanmamış (TRASH_DIR)")
    with _lock:
        if state["running"]:
            return False
        state.update(running=True, phase="Çöp kutusuna taşınıyor", done=0, todo=0, error=None, kind="apply")
    threading.Thread(target=_apply, daemon=True).start()
    return True


def _apply(keep_running=False, batch=None):
    try:
        groups = load_groups()
        batch = batch or datetime.now().strftime("%Y-%m-%d %H.%M")
        root = archive_root()
        todo = [(g, m) for g in groups if not g["skip"] for m in g["members"]
                if m["id"] != g["keep"] and not m.get("locked")]
        from .organize import abs_dir, settings
        inbox = abs_dir(settings()["inbox"]).rstrip("/") + "/"
        state["todo"] = len(todo)
        moved_ids = set()
        for g, m in todo:
            keeper = next(x for x in g["members"] if x["id"] == g["keep"])
            # Güvenlik: kalacak dosya gerçekten yerinde değilse bu gruba dokunma
            if not os.path.isfile(keeper["path"]) or not os.path.isfile(m["path"]) or keeper["path"] == m["path"]:
                state["done"] += 1
                continue
            rel = os.path.relpath(m["path"], root)
            if rel.startswith(".."):
                rel = m["path"].lstrip("/")
            dst = _move(m["path"], os.path.join(TRASH_DIR, batch, rel))
            extras = []
            with db.db() as con:  # canlı fotoğrafın video parçası fotoğrafla birlikte çöpe gider
                lives = [dict(x) for x in con.execute("SELECT id, path FROM media WHERE live_of=?", (m["id"],))]
            for lv in lives:
                if os.path.isfile(lv["path"]):
                    ldst = _move(lv["path"], os.path.join(TRASH_DIR, batch, os.path.relpath(lv["path"], root)))
                    extras.append([lv["path"], ldst])
                    with db.db() as con:
                        con.execute("DELETE FROM media WHERE id=?", (lv["id"],))
            for sc in _sidecars(m["path"]):
                try:
                    sdst = _move(sc, os.path.join(TRASH_DIR, batch, os.path.relpath(sc, root)))
                    extras.append([sc, sdst])
                except OSError:
                    pass
            with db.db() as con:
                con.execute("INSERT INTO trash(media_id,kind,orig_path,trash_path,size,taken_at,reason,"
                            "keeper_path,extras,batch) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (m["id"], g["kind"], m["path"], dst, m["size"], m["taken_at"],
                             "zaten arşivde" if m["path"].startswith(inbox) else g["reason"],
                             keeper["path"], json.dumps(extras), batch))
                con.execute("DELETE FROM media WHERE id=?", (m["id"],))
            moved_ids.add(m["id"])
            state["done"] += 1
        # Taşınanları gruplardan çıkar
        left = []
        for g in groups:
            g["members"] = [m for m in g["members"] if m["id"] not in moved_ids]
            if len(g["members"]) > 1:
                left.append(g)
        _save(left)
        _set(phase=f"Tamamlandı: {len(moved_ids)} dosya çöp kutusuna taşındı", trashed=len(moved_ids))
    except Exception as e:
        traceback.print_exc()
        _set(error=str(e), phase="Hata")
        if keep_running:
            raise
    finally:
        if not keep_running:
            _set(running=False, finished=time.time())


def restore(ids=None, batch=None):
    """Çöpteki dosyaları eski yerlerine geri taşır. Dönüş: geri alınan sayısı."""
    q = "SELECT * FROM trash WHERE status='çöpte'"
    args = []
    if ids:
        q += f" AND id IN ({','.join('?' * len(ids))})"
        args += list(ids)
    elif batch:
        q += " AND batch=?"
        args.append(batch)
    with db.db() as con:
        rows = [dict(r) for r in con.execute(q, args)]
    n = 0
    for r in rows:
        if not os.path.exists(r["trash_path"]):
            continue
        back = _move(r["trash_path"], r["orig_path"])
        for orig, tp in json.loads(r["extras"] or "[]"):
            if os.path.exists(tp):
                _move(tp, orig if back == r["orig_path"] else back + orig[len(r["orig_path"]):])
        with db.db() as con:
            con.execute("UPDATE trash SET status='geri alındı', orig_path=? WHERE id=?", (back, r["id"]))
            # geri alınan dosya bir daha kopya diye çöpe atılmasın
            con.execute("INSERT OR IGNORE INTO pins(path) VALUES(?)", (back,))
        n += 1
    _cleanup_empty_dirs()
    return n


def empty(ids=None, batch=None):
    """Çöpteki dosyaları KALICI olarak siler. Dönüş: (silinen sayı, boşalan bayt)."""
    q = "SELECT * FROM trash WHERE status='çöpte'"
    args = []
    if ids:
        q += f" AND id IN ({','.join('?' * len(ids))})"
        args += list(ids)
    elif batch:
        q += " AND batch=?"
        args.append(batch)
    with db.db() as con:
        rows = [dict(r) for r in con.execute(q, args)]
    n, freed = 0, 0
    for r in rows:
        for p in [r["trash_path"]] + [tp for _, tp in json.loads(r["extras"] or "[]")]:
            # Güvenlik: sadece çöp klasörünün içindeki dosyalar silinir
            if TRASH_DIR and os.path.realpath(p).startswith(os.path.realpath(TRASH_DIR) + os.sep) and os.path.isfile(p):
                freed += os.path.getsize(p)
                os.remove(p)
        with db.db() as con:
            con.execute("UPDATE trash SET status='silindi' WHERE id=?", (r["id"],))
        n += 1
    _cleanup_empty_dirs()
    return n, freed


def _cleanup_empty_dirs():
    if not TRASH_DIR or not os.path.isdir(TRASH_DIR):
        return
    for d, dirs, files in os.walk(TRASH_DIR, topdown=False):
        if d != TRASH_DIR and not os.listdir(d):
            try:
                os.rmdir(d)
            except OSError:
                pass


def trash_list(offset=0, limit=200, batch=None):
    with db.db() as con:
        batches = [dict(r) for r in con.execute(
            "SELECT batch, COUNT(*) n, SUM(size) bytes, SUM(kind='photo') photos, SUM(kind='video') videos "
            "FROM trash WHERE status='çöpte' GROUP BY batch ORDER BY batch DESC")]
        q = "SELECT id,kind,orig_path,trash_path,size,taken_at,reason,keeper_path,batch FROM trash WHERE status='çöpte'"
        args = []
        if batch:
            q += " AND batch=?"
            args.append(batch)
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        items = [dict(r) for r in con.execute(q, args + [limit, offset])]
    root = archive_root()
    for it in items:
        it["name"] = os.path.basename(it["orig_path"])
        it["folder"] = os.path.relpath(os.path.dirname(it["orig_path"]), root)
        it["keeper"] = os.path.relpath(it["keeper_path"], root) if it["keeper_path"] else ""
    return {"batches": batches, "items": items}
