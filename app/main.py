import os
import subprocess

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import cleaner, db, faces, organize, orient, planner, render, scanner, styles
from .analyze import load_image
from .config import MEDIA_DIRS, MUSIC_DEFAULT, MUSIC_DIR, MUSIC_EXT, OUTPUT_DIR, THUMB_DIR

db.init()
app = FastAPI(title="Dedplay Film")
STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/film", response_class=HTMLResponse)
def film_page():
    with open(os.path.join(STATIC, "film.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/icon.png")
@app.get("/favicon.ico")
def icon():
    return FileResponse(os.path.join(STATIC, "icon.png"), media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})


# ---------- Arşiv ----------

@app.get("/api/status")
def status():
    with db.db() as con:
        r = con.execute("SELECT COUNT(*) n, SUM(kind='photo') p, SUM(kind='video') v, "
                        "SUM(analyzed=0) pend FROM media").fetchone()
    with db.db() as con:
        dups = con.execute("SELECT COUNT(*) FROM media WHERE dup_of IS NOT NULL").fetchone()[0]
    return {"scan": scanner.state, "duplicates": dups, "total": r["n"] or 0, "photos": r["p"] or 0,
            "videos": r["v"] or 0, "pending_analysis": r["pend"] or 0}


@app.post("/api/scan")
def scan():
    return {"started": scanner.start()}


@app.get("/api/years")
def years():
    with db.db() as con:
        rows = con.execute("SELECT substr(taken_at,1,7) ym, SUM(kind='photo') p, SUM(kind='video') v "
                           "FROM media GROUP BY ym ORDER BY ym DESC").fetchall()
    out = {}
    for r in rows:
        y, m = r["ym"][:4], int(r["ym"][5:7])
        e = out.setdefault(y, {"year": y, "photos": 0, "videos": 0, "months": {}})
        e["photos"] += r["p"]
        e["videos"] += r["v"]
        e["months"][m] = {"photos": r["p"], "videos": r["v"]}
    return list(out.values())


def _label(folder):
    for root in MEDIA_DIRS:
        if folder == root or folder.startswith(root + "/"):
            return os.path.basename(root) + folder[len(root):]
    return folder


@app.get("/api/folders")
def folders():
    with db.db() as con:
        rows = con.execute("SELECT folder, COUNT(*) n, MIN(taken_at) a, MAX(taken_at) b "
                           "FROM media GROUP BY folder").fetchall()
    agg = {}
    for r in rows:
        # İlk iki seviyeyi grupla (ör. Photos/iPhone 14 Plus)
        for root in MEDIA_DIRS:
            if r["folder"] == root or r["folder"].startswith(root + "/"):
                parts = r["folder"][len(root):].strip("/").split("/")
                for depth in (1, 2):
                    if len(parts) >= depth and parts[0]:
                        key = os.path.join(root, *parts[:depth])
                        e = agg.setdefault(key, {"path": key, "label": _label(key), "count": 0,
                                                 "from": r["a"], "to": r["b"]})
                        e["count"] += r["n"]
                        e["from"] = min(e["from"], r["a"])
                        e["to"] = max(e["to"], r["b"])
    return sorted(agg.values(), key=lambda e: e["label"].lower())


@app.get("/api/thumb/{mid}")
def thumb(mid: int):
    with db.db() as con:
        r = con.execute("SELECT path, kind, rotate_fix, auto_rot, meta_rot FROM media WHERE id=?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404)
    rot = orient.effective(dict(r))
    out = os.path.join(THUMB_DIR, f"{mid}_r{rot}.jpg")
    if not os.path.exists(out):
        try:
            if r["kind"] == "photo":
                load_image(r["path"], 360).rotate(-rot, expand=True).save(out, quality=80)
            else:
                rv = orient.vf_rotate(rot)
                for ss in ("1", "0"):  # 1 sn'den kısa videolarda baştan dene
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", ss, "-i", r["path"],
                                    "-frames:v", "1", "-vf", (rv + "," if rv else "") + "scale=360:-2", out],
                                   timeout=60)
                    if os.path.exists(out) and os.path.getsize(out) > 0:
                        break
        except Exception:
            raise HTTPException(500)
        if not os.path.exists(out):
            raise HTTPException(500)
    return FileResponse(out, headers={"Cache-Control": "max-age=604800"})


class RotateReq(BaseModel):
    step: int = 90


@app.post("/api/media/{mid}/rotate")
def rotate(mid: int, req: RotateReq):
    """Elle yön düzeltmesi: her çağrıda saat yönünde 90° daha. Orijinal dosyaya dokunulmaz."""
    with db.db() as con:
        r = con.execute("SELECT rotate_fix, auto_rot, meta_rot FROM media WHERE id=?", (mid,)).fetchone()
        if not r:
            raise HTTPException(404)
        cur = r["rotate_fix"] if r["rotate_fix"] is not None else (r["auto_rot"] or 0)
        new = (int(cur) + req.step) % 360
        con.execute("UPDATE media SET rotate_fix=? WHERE id=?", (new, mid))
    return {"rot": orient.effective({"rotate_fix": new, "meta_rot": r["meta_rot"]})}


@app.get("/api/media/{mid}")
def media_file(mid: int):
    with db.db() as con:
        r = con.execute("SELECT path FROM media WHERE id=?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404)
    return FileResponse(r["path"])


# ---------- Plan ----------

class PlanReq(BaseModel):
    start: str
    end: str
    folder: str | None = None
    target_minutes: float | None = 15
    photo_sec: float = 3.5
    video_sec: float = 7
    title: str | None = None
    style: str = "sinematik"


@app.post("/api/plan")
def plan(req: PlanReq):
    try:
        out = planner.plan(req.start, req.end, req.folder, req.target_minutes or None,
                           req.photo_sec, req.video_sec, req.title, req.style)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Kare seçimi sırasında hata: {e}")
    out["scan_running"] = scanner.state["running"]
    return out


@app.get("/api/styles")
def get_styles():
    return {k: {"label": v["label"], "photo_sec": v["photo_sec"]} for k, v in styles.STYLES.items()}


# ---------- Müzik ----------

def _inside(path, root):
    return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


@app.get("/api/music")
def music(dir: str | None = None):
    dirs = sorted(d for d in os.listdir(MUSIC_DIR) if os.path.isdir(os.path.join(MUSIC_DIR, d))) \
        if os.path.isdir(MUSIC_DIR) else []
    cur = dir if dir is not None else (MUSIC_DEFAULT if MUSIC_DEFAULT in dirs else (dirs[0] if dirs else ""))
    base = os.path.join(MUSIC_DIR, cur)
    files = []
    if os.path.isdir(base) and (_inside(base, MUSIC_DIR) or base.rstrip("/") == MUSIC_DIR.rstrip("/")):
        for d, _, fs in os.walk(base):
            for f in sorted(fs):
                if os.path.splitext(f)[1].lower() in MUSIC_EXT and not f.startswith("."):
                    p = os.path.join(d, f)
                    files.append({"path": p, "name": os.path.relpath(p, base)})
    return {"dirs": dirs, "current": cur, "files": files}


@app.get("/api/music/play")
def music_play(path: str):
    if not _inside(path, MUSIC_DIR) or not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path)


# ---------- Render ----------

class Chapter(BaseModel):
    title: str
    subtitle: str = ""
    ids: list[int]


class RenderReq(BaseModel):
    title: str
    range_text: str = ""
    chapters: list[Chapter]
    music: list[str] = []
    music_volume: float = 0.8
    video_audio: bool = True
    chapter_titles: bool = True
    photo_sec: float = 3.5
    video_sec: float = 7
    style: str = "sinematik"
    letterbox: bool = False
    beat_sync: bool = True


@app.post("/api/render")
def do_render(req: RenderReq):
    for m in req.music:
        if not _inside(m, MUSIC_DIR):
            raise HTTPException(400, "Geçersiz müzik yolu")
    p = req.model_dump()
    p["chapters"] = [c for c in p["chapters"] if c["ids"]]
    if not p["chapters"]:
        raise HTTPException(400, "Filmde hiç kare yok")
    return {"job": render.submit(p)}


@app.get("/api/jobs")
def jobs():
    with db.db() as con:
        rows = con.execute("SELECT id,title,status,progress,message,output,created_at FROM jobs "
                           "ORDER BY id DESC LIMIT 30").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["output"] and os.path.exists(d["output"]):
            d["size_mb"] = round(os.path.getsize(d["output"]) / 1e6, 1)
            d["file"] = os.path.basename(d["output"])
        out.append(d)
    return out


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: int):
    render.cancel(jid)
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: int):
    """Filmi diskten ve listeden siler. Sadece çıktı klasöründeki dosya silinir."""
    with db.db() as con:
        r = con.execute("SELECT status, output FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        raise HTTPException(404)
    if r["status"] in ("sırada", "çalışıyor"):
        raise HTTPException(400, "Önce işi iptal et")
    out = r["output"]
    if out and os.path.isfile(out):
        if not _inside(out, OUTPUT_DIR):
            raise HTTPException(400, "Bu dosya film klasörünün dışında")
        os.remove(out)
    with db.db() as con:
        con.execute("DELETE FROM jobs WHERE id=?", (jid,))
    return {"ok": True}


@app.get("/api/jobs/{jid}/file")
def job_file(jid: int, download: int = 0):
    with db.db() as con:
        r = con.execute("SELECT output FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r or not r["output"] or not os.path.exists(r["output"]):
        raise HTTPException(404)
    return FileResponse(r["output"], media_type="video/mp4",
                        filename=os.path.basename(r["output"]) if download else None)


# ================= Kopya Temizleyici + Düzenleyici =================

@app.get("/api/clean/status")
def clean_status():
    st = dict(cleaner.state)
    st["summary"] = cleaner.summary()
    st["settings"] = organize.settings()
    with db.db() as con:
        t = con.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM trash WHERE status='çöpte'").fetchone()
    st["trash"] = {"files": t["n"], "bytes": t["b"]}
    st["scan"] = scanner.state
    st["faces"] = faces.state
    with db.db() as con:
        st["live"] = con.execute("SELECT COUNT(*) FROM media WHERE live_of IS NOT NULL").fetchone()[0]
        st["short"] = con.execute("SELECT COUNT(*) FROM media WHERE kind='video' AND live_of IS NULL "
                                  "AND duration < 4").fetchone()[0]
    return st


class FindReq(BaseModel):
    include_bursts: bool = True
    include_similar: bool = True


@app.post("/api/clean/find")
def clean_find(req: FindReq):
    organize.save_settings(include_bursts=req.include_bursts, include_similar=req.include_similar)
    return {"started": organize.run(full=True, apply_trash=False)}


@app.post("/api/clean/apply")
def clean_apply():
    if not cleaner.TRASH_DIR:
        raise HTTPException(400, "Çöp kutusu klasörü ayarlı değil")
    return {"started": organize.apply_and_organize()}


@app.post("/api/clean/inbox")
def clean_inbox():
    """Gelen klasörünü şimdi işle (beklemeden)."""
    s = organize.settings()
    inbox = organize.abs_dir(s["inbox"])
    os.makedirs(inbox, exist_ok=True)
    organize.feed_pass(quiet=30)
    return {"started": organize.run(full=False, scope=[inbox])}


@app.get("/api/clean/groups")
def clean_groups(offset: int = 0, limit: int = 40, reason: str | None = None):
    gs = [g for g in cleaner.load_groups()
          if any(m["id"] not in cleaner.keeps_of(g) and not m.get("locked") for m in g["members"])]
    if reason:
        gs = [g for g in gs if g["reason"] == reason]
    return {"total": len(gs), "groups": gs[offset:offset + limit]}


class GroupReq(BaseModel):
    keep: int | None = None
    skip: bool | None = None
    toggle: int | None = None


@app.get("/api/clean/groups/{gid}/get")
def clean_group_get(gid: int):
    for g in cleaner.load_groups():
        if g["gid"] == gid:
            return g
    raise HTTPException(404)


@app.post("/api/clean/groups/{gid}")
def clean_group(gid: int, req: GroupReq):
    g = cleaner.update_group(gid, req.keep, req.skip, req.toggle)
    if not g:
        raise HTTPException(404)
    return g


@app.get("/api/settings")
def get_settings():
    return {"settings": organize.settings(), "folders": organize.candidate_folders()}


class SettingsReq(BaseModel):
    protected: list[str] | None = None
    live_mode: str | None = None
    short_mode: str | None = None
    auto: bool | None = None
    include_bursts: bool | None = None


@app.post("/api/settings")
def set_settings(req: SettingsReq):
    return organize.save_settings(**{k: v for k, v in req.model_dump().items() if v is not None})


@app.post("/api/pins/clear")
def pins_clear():
    """Çöpten geri alınanların 'dokunma' kilidini kaldırır: bir sonraki taramada yeniden değerlendirilirler."""
    with db.db() as con:
        n = con.execute("SELECT COUNT(*) FROM pins").fetchone()[0]
        con.execute("DELETE FROM pins")
    return {"cleared": n}


@app.get("/api/moves")
def moves():
    return organize.move_batches()


class BatchReq(BaseModel):
    batch: str


@app.post("/api/moves/undo")
def moves_undo(req: BatchReq):
    if cleaner.state["running"]:
        raise HTTPException(400, "Önce süren işlemin bitmesini bekle")
    return {"undone": organize.undo_moves(req.batch)}


# ---- Çöp kutusu ----

@app.get("/api/trash")
def trash(offset: int = 0, limit: int = 200, batch: str | None = None):
    return cleaner.trash_list(offset, limit, batch)


class TrashReq(BaseModel):
    ids: list[int] | None = None
    batch: str | None = None
    all: bool = False
    pin: bool = False  # geri alınanlar bir sonraki taramada normal şekilde yeniden değerlendirilir


@app.post("/api/trash/restore")
def trash_restore(req: TrashReq):
    if not (req.ids or req.batch or req.all):
        raise HTTPException(400, "Seçim yok")
    if cleaner.state["running"]:
        raise HTTPException(400, "Önce süren işlemin bitmesini bekle")
    n = cleaner.restore(req.ids, req.batch, req.pin)
    return {"restored": n}


@app.post("/api/trash/delete")
def trash_delete(req: TrashReq):
    """KALICI silme. Tümü için all=true gerekir (yanlışlıkla boş istekle her şey silinmesin)."""
    if not (req.ids or req.batch or req.all):
        raise HTTPException(400, "Seçim yok")
    n, freed = cleaner.empty(req.ids, req.batch)
    return {"deleted": n, "freed": freed}


@app.get("/api/trash/thumb/{tid}")
def trash_thumb(tid: int):
    with db.db() as con:
        r = con.execute("SELECT media_id, kind, trash_path FROM trash WHERE id=?", (tid,)).fetchone()
    if not r:
        raise HTTPException(404)
    for f in os.listdir(THUMB_DIR):
        if f.startswith(f"{r['media_id']}_r"):
            return FileResponse(os.path.join(THUMB_DIR, f))
    out = os.path.join(THUMB_DIR, f"t{tid}.jpg")
    if not os.path.exists(out):
        try:
            if r["kind"] == "photo":
                load_image(r["trash_path"], 360).save(out, quality=80)
            else:
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", r["trash_path"],
                                "-frames:v", "1", "-vf", "scale=360:-2", out], timeout=60)
        except Exception:
            raise HTTPException(500)
    if not os.path.exists(out):
        raise HTTPException(404)
    return FileResponse(out)


@app.get("/api/trash/file/{tid}")
def trash_file(tid: int):
    with db.db() as con:
        r = con.execute("SELECT trash_path FROM trash WHERE id=?", (tid,)).fetchone()
    if not r or not os.path.isfile(r["trash_path"]):
        raise HTTPException(404)
    return FileResponse(r["trash_path"])


# ---- Kütüphane: Tüm resimler / Tüm videolar ----

def _lib_where(kind):
    # kind: photo | video | other (ekran görüntüleri ve başka yerden kaydedilenler)
    if kind == "other":
        return "src IS NOT NULL AND live_of IS NULL", []
    return "kind=? AND src IS NULL AND live_of IS NULL", [kind]   # canlı foto videoları fotoğrafın parçası


class IdsReq(BaseModel):
    ids: list[int]


@app.post("/api/library/trash")
def library_trash(req: IdsReq):
    """Galeride seçilen dosyaları çöp kutusuna taşır (elle). Geri alınabilir."""
    if cleaner.state["running"]:
        raise HTTPException(400, "Önce süren işlemin bitmesini bekle")
    return {"trashed": cleaner.trash_ids(req.ids, "elle seçildi")}


@app.get("/api/library/years")
def library_years(kind: str = "photo", short: int = 0):
    w, a = _lib_where(kind)
    if short:
        w += " AND kind='video' AND (duration IS NULL OR duration < 4)"
    with db.db() as con:
        rows = con.execute("SELECT CASE WHEN date_source IN ('exif','google','filename') THEN substr(taken_at,1,4) "
                           f"ELSE 'Tarihsiz' END y, COUNT(*) n FROM media WHERE {w} GROUP BY y ORDER BY y DESC",
                           a).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/library")
def library(kind: str = "photo", year: str | None = None, offset: int = 0, limit: int = 120,
            short: int = 0):
    w, args = _lib_where(kind)
    if short:  # çok kısa videolar (4 sn'den kısa ya da süresi okunamamış) — hangi klasörde olursa olsun
        w, args = "kind='video' AND live_of IS NULL AND (duration IS NULL OR duration < 4)", []
    q = f"SELECT id, path, taken_at, duration, width, height, kind FROM media WHERE {w}"
    if year == "Tarihsiz":
        q += " AND (date_source NOT IN ('exif','google','filename') OR date_source IS NULL)"
    elif year:
        q += " AND date_source IN ('exif','google','filename') AND substr(taken_at,1,4)=?"
        args.append(year)
    with db.db() as con:
        total = con.execute(q.replace("SELECT id, path, taken_at, duration, width, height, kind", "SELECT COUNT(*)"),
                            args).fetchone()[0]
        rows = con.execute(q + " ORDER BY taken_at DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    root = cleaner.archive_root()
    items = [{"id": r["id"], "name": os.path.basename(r["path"]),
              "folder": os.path.relpath(os.path.dirname(r["path"]), root),
              "taken_at": r["taken_at"], "duration": r["duration"], "kind": r["kind"]} for r in rows]
    if short and rows:  # her kısa video için: arşivde fotoğrafı var mı?
        from . import shorts
        with db.db() as con:
            vs = [dict(x) for x in con.execute(
                f"SELECT id, path, duration, short_photo FROM media WHERE id IN ({','.join('?' * len(rows))})",
                [r["id"] for r in rows])]
        has = shorts.check(vs)
        for it in items:
            it["has_photo"] = bool(has.get(it["id"]))
    return {"total": total, "items": items}


# ================= Kişiler (yüz tanıma) =================

@app.get("/api/people")
def people(include_small: int = 0):
    with db.db() as con:
        rows = con.execute("""SELECT p.id, p.name, p.cover, COUNT(DISTINCT f.media_id) n
                              FROM people p JOIN faces f ON f.person_id=p.id JOIN media m ON m.id=f.media_id
                              WHERE p.hidden=0 GROUP BY p.id ORDER BY (p.name IS NULL), n DESC""").fetchall()
        pend = con.execute("SELECT COUNT(*) FROM media WHERE kind='photo' AND (faces_done IS NULL OR faces_done=-1)").fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM media WHERE kind='photo'").fetchone()[0]
    out = [dict(r) for r in rows if include_small or r["n"] >= 5 or r["name"]]
    return {"people": out, "pending": pend, "total": total, "state": faces.state,
            "small": sum(1 for r in rows if r["n"] < 5 and not r["name"])}


@app.post("/api/people/run")
def people_run():
    return {"started": faces.run_all()}


@app.get("/api/people/{pid}/photos")
def person_photos(pid: int, offset: int = 0, limit: int = 200):
    with db.db() as con:
        total = con.execute("SELECT COUNT(DISTINCT f.media_id) FROM faces f JOIN media m ON m.id=f.media_id "
                            "WHERE f.person_id=?", (pid,)).fetchone()[0]
        rows = con.execute("""SELECT m.id, m.path, m.taken_at, MIN(f.id) fid FROM faces f JOIN media m ON m.id=f.media_id
                              WHERE f.person_id=? GROUP BY m.id ORDER BY m.taken_at DESC LIMIT ? OFFSET ?""",
                           (pid, limit, offset)).fetchall()
        p = con.execute("SELECT id, name FROM people WHERE id=?", (pid,)).fetchone()
    root = cleaner.archive_root()
    return {"person": dict(p) if p else None, "total": total,
            "items": [{"id": r["id"], "name": os.path.basename(r["path"]), "kind": "photo",
                       "folder": os.path.relpath(os.path.dirname(r["path"]), root), "taken_at": r["taken_at"],
                       "fid": r["fid"]}
                      for r in rows]}


class RemoveReq(BaseModel):
    ids: list[int]


@app.post("/api/people/{pid}/remove")
def person_remove(pid: int, req: RemoveReq):
    return {"removed": faces.remove(pid, req.ids)}


class PersonReq(BaseModel):
    name: str | None = None
    hidden: bool | None = None


@app.post("/api/people/{pid}")
def person_update(pid: int, req: PersonReq):
    with db.db() as con:
        if req.name is not None:
            con.execute("UPDATE people SET name=? WHERE id=?", (req.name.strip() or None, pid))
        if req.hidden is not None:
            con.execute("UPDATE people SET hidden=? WHERE id=?", (1 if req.hidden else 0, pid))
    _merge_same_names()
    return {"ok": True}


def _merge_same_names():
    """Aynı ismi verilen kişiler tek kişi olur (büyük/küçük harf ve boşluk farkı önemsiz)."""
    with db.db() as con:
        rows = con.execute("SELECT id, name FROM people WHERE name IS NOT NULL ORDER BY id").fetchall()
    by = {}
    for r in rows:
        by.setdefault(" ".join(r["name"].split()).casefold(), []).append(r["id"])
    for ids in by.values():
        if len(ids) > 1:
            faces.merge(ids[0], ids[1:])


class MergeReq(BaseModel):
    ids: list[int]


try:
    _merge_same_names()
except Exception:
    pass


@app.post("/api/people-merge")
def people_merge(req: MergeReq):
    if len(req.ids) < 2:
        raise HTTPException(400, "En az iki kişi seç")
    with db.db() as con:
        named = [r["id"] for r in con.execute(
            f"SELECT id FROM people WHERE name IS NOT NULL AND id IN ({','.join('?' * len(req.ids))})", req.ids)]
    target = named[0] if named else req.ids[0]
    faces.merge(target, [i for i in req.ids if i != target])
    return {"target": target}


@app.get("/api/face/{fid}/thumb")
def face_thumb(fid: int):
    out = os.path.join(THUMB_DIR, f"face{fid}.jpg")
    if not os.path.exists(out):
        with db.db() as con:
            r = con.execute("SELECT f.x,f.y,f.w,f.h,m.path FROM faces f JOIN media m ON m.id=f.media_id WHERE f.id=?",
                            (fid,)).fetchone()
        if not r:
            raise HTTPException(404)
        im = load_image(r["path"], 1600)
        W, H = im.size
        cx, cy = (r["x"] + r["w"] / 2) * W, (r["y"] + r["h"] / 2) * H
        half = max(r["w"] * W, r["h"] * H) * 0.9
        im.crop((int(cx - half), int(cy - half), int(cx + half), int(cy + half))).resize((200, 200)).save(out, quality=85)
    return FileResponse(out, headers={"Cache-Control": "max-age=604800"})
