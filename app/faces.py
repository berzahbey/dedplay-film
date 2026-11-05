"""Yüz tanıma (tamamen yerel, ücretsiz: dlib + face_recognition_models).

- Her fotoğrafta yüzler bulunur, her yüz 128 sayılık bir "yüz izine" çevrilir.
  İki iz arasındaki uzaklık < 0.55 ise aynı kişi sayılır.
- Kişiler: yüzler benzerliğe göre gruplanır. İsim verilen kişiler korunur; yeni fotoğraflardaki
  yüzler mevcut kişilere eklenir (her seferinde baştan gruplanmaz, isimler kaybolmaz).
- Benzer kareler: iki karede aynı kişiler var mı? (mangal örneği: baba+çocuk ≠ dede+çocuk)
"""
import os
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import db

SAME_PERSON = 0.55      # aynı kişi eşiği (dlib için önerilen 0.6; biraz daha sıkı)
CLUSTER_JOIN = 0.45     # bir yüzü mevcut kişiye eklemek için
GROUP_DIST = 0.45       # kişileri baştan kurarken iki yüzü bağlamak için
MIN_FACE_PX = 80        # kişilere eklenecek en küçük yüz (piksel); küçük/uzak/bulanık yüzler karışıklık yaratır
GROUPING_VERSION = 2
MIN_FACE_FRAC = 0.035   # resmin kısa kenarına göre en küçük yüz (çok küçük/arka plandaki yüzler sayılmaz)
ANALYSIS_SIDE = 1200

_models = None
state = {"running": False, "phase": "", "done": 0, "todo": 0, "error": None}


def _load():
    global _models
    if _models is None:
        import importlib.util
        import dlib
        # face_recognition_models paketini içe aktarmadan (pkg_resources ister) model dosyalarını bul
        spec = importlib.util.find_spec("face_recognition_models")
        mdir = os.path.join(os.path.dirname(spec.origin), "models")
        _models = (dlib.get_frontal_face_detector(),
                   dlib.shape_predictor(os.path.join(mdir, "shape_predictor_5_face_landmarks.dat")),
                   dlib.face_recognition_model_v1(os.path.join(mdir, "dlib_face_recognition_resnet_model_v1.dat")))
    return _models


def detect(path):
    """Dönüş: [(x, y, w, h (0-1 arası, resme göre), yüz izi float32[128])]"""
    from .analyze import load_image
    det, sp, rec = _load()
    im = load_image(path, ANALYSIS_SIDE)
    a = np.asarray(im)
    H, W = a.shape[:2]
    out = []
    for d in det(a, 1):
        w = d.right() - d.left()
        if w < MIN_FACE_FRAC * min(W, H):
            continue
        emb = np.array(rec.compute_face_descriptor(a, sp(a, d)), np.float32)
        out.append((max(0, d.left()) / W, max(0, d.top()) / H, w / W, (d.bottom() - d.top()) / H, emb))
    return out


def _job(args):
    mid, path = args
    try:
        return mid, detect(path)
    except Exception:
        return mid, None


def analyze(ids_paths, progress=None, workers=None):
    """Yüzleri bulur ve kaydeder. ids_paths: [(id, path)] — daha önce bakılmamış fotoğraflar."""
    if not ids_paths:
        return 0
    workers = workers or max(1, (os.cpu_count() or 2) // 2)
    n = 0
    with ProcessPoolExecutor(workers) as ex:
        for mid, faces in ex.map(_job, ids_paths, chunksize=4):
            with db.db() as con:
                con.execute("DELETE FROM faces WHERE media_id=?", (mid,))
                for x, y, w, h, e in faces or []:
                    con.execute("INSERT INTO faces(media_id,x,y,w,h,emb) VALUES(?,?,?,?,?,?)",
                                (mid, x, y, w, h, e.tobytes()))
                con.execute("UPDATE media SET faces_done=? WHERE id=?", (1 if faces is not None else -1, mid))
            n += 1
            if progress:
                progress(n, len(ids_paths))
    return n


def faces_of(ids):
    """{media_id: [emb, ...]} (yüzü olmayanlar boş liste)."""
    out = {i: [] for i in ids}
    if not ids:
        return out
    with db.db() as con:
        for r in con.execute(f"SELECT media_id, emb FROM faces WHERE media_id IN ({','.join('?' * len(ids))})", list(ids)):
            out[r["media_id"]].append(np.frombuffer(r["emb"], np.float32))
    return out


def same_people(fa, fb):
    """İki karede aynı kişiler mi? Her iki karede de yüz yoksa None (karar verilemez).
    Bir karedeki her yüzün diğerinde bir karşılığı olmalı (yeni biri girdiyse / biri çıktıysa: farklı an)."""
    if not fa and not fb:
        return None
    if not fa or not fb:
        return False
    A, B = np.stack(fa), np.stack(fb)
    d = np.linalg.norm(A[:, None, :] - B[None, :, :], axis=2)
    return bool((d.min(axis=1) < SAME_PERSON).all() and (d.min(axis=0) < SAME_PERSON).all())


# ---------- Kişiler (gruplama) ----------

def _px(r):
    """Yüzün analiz edilen resimdeki yaklaşık genişliği (piksel)."""
    W, H = r["width"] or ANALYSIS_SIDE, r["height"] or ANALYSIS_SIDE
    sc = min(1.0, ANALYSIS_SIDE / max(W, H))
    return r["w"] * (W * H) ** 0.5 * sc


def recluster():
    """Kişileri baştan kurar (Chinese Whispers: ortalamaya değil, yüzlerin birbirine benzerliğine bakar;
    bir grup zamanla alakasız yüzleri toplayan bir çöp kutusuna dönüşmez). Verilen isimler ve gizlenen
    kişiler korunur; "bu kişi değil" denen yüzler o kişiye geri dönmez."""
    import dlib
    from collections import Counter, defaultdict
    with db.db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT f.id, f.emb, f.w, f.person_id, m.width, m.height FROM faces f JOIN media m ON m.id=f.media_id")]
        people = {r["id"]: dict(r) for r in con.execute("SELECT id, name, hidden FROM people")}
        rej = defaultdict(set)
        for r in con.execute("SELECT face_id, person_id FROM face_reject"):
            rej[r[0]].add(r[1])
    good = [r for r in rows if _px(r) >= MIN_FACE_PX]
    small = [r["id"] for r in rows if _px(r) < MIN_FACE_PX]
    state["phase"] = f"Kişiler yeniden gruplanıyor ({len(good)} yüz)"
    labels = dlib.chinese_whispers_clustering(
        [dlib.vector(np.frombuffer(r["emb"], np.float32).astype(float).tolist()) for r in good], GROUP_DIST) if good else []
    groups = defaultdict(list)
    for r, lab in zip(good, labels):
        groups[lab].append(r)
    keep = {pid for pid, p in people.items() if p["name"] or p["hidden"]}   # isimli / gizli kişiler kalır
    assign = [(0, i) for i in small]
    # her isimli kişi en çok yüzünü içeren TEK gruba geçer (başka gruplar "İsimsiz" olur; aynı isim verilirse birleşir)
    best = {}
    for lab, mem in groups.items():
        votes = Counter(r["person_id"] for r in mem if r["person_id"] in keep)
        if votes:
            cand, n = votes.most_common(1)[0]
            if n >= 0.5 * len(mem) and n > best.get(cand, (0, None))[0]:
                best[cand] = (n, lab)
    owner = {lab: pid for pid, (n, lab) in best.items()}
    with db.db() as con:
        for lab, mem in groups.items():
            pid = owner.get(lab)
            if pid is None:
                pid = con.execute("INSERT INTO people(name) VALUES(NULL)").lastrowid
            other = None
            for r in mem:
                if pid in rej.get(r["id"], ()):
                    if other is None:
                        other = con.execute("INSERT INTO people(name) VALUES(NULL)").lastrowid
                    assign.append((other, r["id"]))
                else:
                    assign.append((pid, r["id"]))
        con.executemany("UPDATE faces SET person_id=? WHERE id=?", assign)
        con.execute("DELETE FROM people WHERE name IS NULL AND id NOT IN (SELECT DISTINCT person_id FROM faces WHERE person_id > 0)")
        con.execute("""UPDATE people SET cover=(SELECT f.id FROM faces f WHERE f.person_id=people.id
                       ORDER BY f.w*f.h DESC LIMIT 1)""")
    with open(_marker(), "w") as f:
        f.write(str(GROUPING_VERSION))
    return len(groups)


def _marker():
    from .config import DATA_DIR
    return os.path.join(DATA_DIR, "kisiler-gruplama.txt")


def _grouping_old():
    try:
        return int(open(_marker()).read().strip() or 0) < GROUPING_VERSION
    except Exception:
        return True

def cluster(progress=None):
    """Kişisi atanmamış yüzleri mevcut kişilere ekler ya da yeni kişiler oluşturur."""
    with db.db() as con:
        people = [dict(r) for r in con.execute("SELECT id FROM people")]
        known = con.execute("SELECT person_id, emb FROM faces WHERE person_id > 0").fetchall()
        new = con.execute("SELECT f.id, f.emb, f.w, m.width, m.height FROM faces f JOIN media m ON m.id=f.media_id "
                          "WHERE f.person_id IS NULL ORDER BY f.id").fetchall()
        small = [r["id"] for r in new if _px(r) < MIN_FACE_PX]
        con.executemany("UPDATE faces SET person_id=0 WHERE id=?", [(i,) for i in small])   # 0 = kişilere katılmaz
        new = [r for r in new if _px(r) >= MIN_FACE_PX]
        rej = {}
        for r in con.execute("SELECT face_id, person_id FROM face_reject"):
            rej.setdefault(r[0], set()).add(r[1])
    if not new:
        return 0
    # kişi merkezleri
    cents, cnt = {}, {}
    for r in known:
        e = np.frombuffer(r["emb"], np.float32)
        cents[r["person_id"]] = cents.get(r["person_id"], 0) + e
        cnt[r["person_id"]] = cnt.get(r["person_id"], 0) + 1
    pids = list(cents)
    C = np.stack([cents[p] / cnt[p] for p in pids]) if pids else np.zeros((0, 128), np.float32)
    assign = []
    for k, r in enumerate(new):
        e = np.frombuffer(r["emb"], np.float32)
        pid = None
        if len(pids):
            d = np.linalg.norm(C - e, axis=1)
            no = rej.get(r["id"], ())          # kullanıcı "bu kişi değil" dediyse o kişiye bir daha eklenmez
            for i in np.argsort(d):
                i = int(i)
                if d[i] >= CLUSTER_JOIN:
                    break
                if pids[i] in no:
                    continue
                pid = pids[i]
                cnt[pid] += 1
                C[i] = C[i] + (e - C[i]) / cnt[pid]   # merkezi güncelle
                break
        if pid is None:
            with db.db() as con:
                pid = con.execute("INSERT INTO people(name) VALUES(NULL)").lastrowid
            pids.append(pid)
            cnt[pid] = 1
            C = np.vstack([C, e[None, :]])
        assign.append((pid, r["id"]))
        if progress and k % 500 == 0:
            progress(k, len(new))
    with db.db() as con:
        con.executemany("UPDATE faces SET person_id=? WHERE id=?", assign)
        # kapak: kişinin en büyük yüzü
        con.execute("""UPDATE people SET cover=(SELECT f.id FROM faces f WHERE f.person_id=people.id
                        ORDER BY f.w*f.h DESC LIMIT 1) WHERE cover IS NULL OR cover NOT IN
                        (SELECT id FROM faces f2 WHERE f2.person_id=people.id)""")
    return len(assign)


def merge(target, others):
    with db.db() as con:
        for o in others:
            if o == target:
                continue
            con.execute("UPDATE faces SET person_id=? WHERE person_id=?", (target, o))
            con.execute("UPDATE OR IGNORE face_reject SET person_id=? WHERE person_id=?", (target, o))
            con.execute("DELETE FROM people WHERE id=?", (o,))


def remove(pid, media_ids):
    """Seçilen fotoğraflardaki bu kişiye ait yüzleri kişiden çıkarır; başka bir kişiye ya da yeni kişiye gider."""
    if not media_ids:
        return 0
    with db.db() as con:
        fids = [r[0] for r in con.execute(
            f"SELECT id FROM faces WHERE person_id=? AND media_id IN ({','.join('?' * len(media_ids))})",
            [pid, *media_ids])]
        con.executemany("INSERT OR IGNORE INTO face_reject(face_id, person_id) VALUES(?,?)", [(f, pid) for f in fids])
        con.executemany("UPDATE faces SET person_id=NULL WHERE id=?", [(f,) for f in fids])
    cluster()
    return len(fids)


# ---------- Arka planda tüm arşiv ----------

def run_all():
    if state["running"]:
        return False
    state.update(running=True, phase="Yüzler aranıyor", done=0, todo=0, error=None)

    def go():
        try:
            _load()   # model yüklenemiyorsa hemen hata göster (sessizce her fotoğrafı atlamasın)
            with db.db() as con:
                todo = [(r["id"], r["path"]) for r in con.execute(
                    "SELECT id, path FROM media WHERE kind='photo' AND (faces_done IS NULL OR faces_done=-1) "
                    "ORDER BY taken_at DESC")]
            state["todo"] = len(todo)
            # parça parça: her 200 fotoğrafta bir kişiler güncellenir (beklemeden görünsünler)
            for k in range(0, len(todo), 200):
                part = todo[k:k + 200]
                analyze(part, lambda d, t, k=k: state.update(done=k + d))
                cluster()
            state["phase"] = "Yüzler kişilere ayrılıyor"
            if _grouping_old():
                recluster()
            cluster()
            state["phase"] = "Tamamlandı"
        except Exception as e:
            traceback.print_exc()
            state.update(error=str(e), phase="Hata")
        finally:
            state["running"] = False
            state["finished"] = time.time()
    threading.Thread(target=go, daemon=True).start()
    return True


def _boot():
    """Sunucu açılınca: yüzlere bakılmamış fotoğraf varsa taramayı kendiliğinden başlat."""
    time.sleep(90)
    try:
        with db.db() as con:
            n = con.execute("SELECT COUNT(*) FROM media WHERE kind='photo' AND (faces_done IS NULL OR faces_done=-1)").fetchone()[0]
        if n or _grouping_old():
            run_all()
    except Exception:
        traceback.print_exc()


threading.Thread(target=_boot, daemon=True).start()
