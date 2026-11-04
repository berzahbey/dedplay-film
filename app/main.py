import os
import subprocess

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import db, orient, planner, render, scanner, styles
from .analyze import load_image
from .config import MEDIA_DIRS, MUSIC_DEFAULT, MUSIC_DIR, MUSIC_EXT, OUTPUT_DIR, THUMB_DIR

db.init()
app = FastAPI(title="Dedplay Film")
STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


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
        r = con.execute("SELECT path, kind, rotate_fix, auto_rot FROM media WHERE id=?", (mid,)).fetchone()
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
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", r["path"],
                                "-frames:v", "1", "-vf", (rv + "," if rv else "") + "scale=360:-2", out],
                               timeout=60)
        except Exception:
            raise HTTPException(500)
        if not os.path.exists(out):
            raise HTTPException(500)
    return FileResponse(out, headers={"Cache-Control": "max-age=604800"})


class RotateReq(BaseModel):
    deg: int


@app.post("/api/media/{mid}/rotate")
def rotate(mid: int, req: RotateReq):
    """Elle yön düzeltmesi (saat yönünde 0/90/180/270). Orijinal dosyaya dokunulmaz."""
    deg = req.deg % 360
    if deg not in (0, 90, 180, 270):
        raise HTTPException(400, "Geçersiz açı")
    with db.db() as con:
        con.execute("UPDATE media SET rotate_fix=? WHERE id=?", (deg, mid))
    return {"rot": deg}


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
    out = planner.plan(req.start, req.end, req.folder, req.target_minutes or None,
                       req.photo_sec, req.video_sec, req.title, req.style)
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
