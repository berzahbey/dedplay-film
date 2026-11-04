"""Film render motoru: seçilen fotoğraf/videolardan 1080p film üretir (FFmpeg)."""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from . import db
from .analyze import load_image
from .config import (CRF, FONT, FONT_BOLD, FPS, HEIGHT, OUTPUT_DIR, PRESET, THREADS, WIDTH,
                     WORK_DIR)
from .planner import INTRO_SEC, OUTRO_SEC, TITLE_SEC, _segments

W2, H2 = WIDTH * 2, HEIGHT * 2  # Ken Burns için 2x çözünürlükte hazırlanır (titremesiz)
_q = queue.Queue()
_cancel = set()
_HAS_ZSCALE = None


def _run(cmd, timeout=1800):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-1500:])
    return p


def _venc():
    return ["-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF), "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-threads", str(THREADS)]


AENC = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]


def has_zscale():
    global _HAS_ZSCALE
    if _HAS_ZSCALE is None:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True).stdout.decode()
        _HAS_ZSCALE = " zscale " in out and " tonemap " in out
    return _HAS_ZSCALE


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                        "-show_format", path], capture_output=True)
    try:
        j = json.loads(p.stdout or b"{}")
    except json.JSONDecodeError:
        j = {}
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = any(s.get("codec_type") == "audio" for s in j.get("streams", []))
    dur = float(j.get("format", {}).get("duration") or v.get("duration") or 0)
    return {"has_audio": a, "transfer": v.get("color_transfer", ""), "duration": dur}


# ---------- Kareler ----------

def _cover(im, w, h, ybias=0.4):
    r = max(w / im.width, h / im.height)
    im = im.resize((max(w, round(im.width * r)), max(h, round(im.height * r))), Image.LANCZOS)
    x = (im.width - w) // 2
    y = int((im.height - h) * ybias)
    return im.crop((x, y, x + w, y + h))


def photo_frame(path, out, darken=None):
    """Fotoğrafı 16:9 kareye yerleştirir. Dikey/kare fotoğraflarda arkası bulanık doldurulur."""
    im = load_image(path, 4000)
    ar = im.width / im.height
    if 1.25 <= ar <= 2.1:
        canvas = _cover(im, W2, H2)
    else:
        bg = _cover(im.resize((max(1, im.width // 8), max(1, im.height // 8))), W2 // 8, H2 // 8)
        bg = bg.filter(ImageFilter.GaussianBlur(6)).resize((W2, H2), Image.BILINEAR)
        bg = ImageEnhance.Brightness(bg).enhance(0.55)
        fh = int(H2 * 0.94)
        fw = int(fh * ar)
        if fw > W2 * 0.94:
            fw = int(W2 * 0.94)
            fh = int(fw / ar)
        fg = im.resize((fw, fh), Image.LANCZOS)
        sh = Image.new("RGBA", (fw + 80, fh + 80), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rectangle((40, 40, fw + 40, fh + 40), fill=(0, 0, 0, 150))
        sh = sh.filter(ImageFilter.GaussianBlur(25))
        bg.paste(sh, ((W2 - fw) // 2 - 40, (H2 - fh) // 2 - 10), sh)
        bg.paste(fg, ((W2 - fw) // 2, (H2 - fh) // 2))
        canvas = bg
    if darken:
        canvas = ImageEnhance.Brightness(canvas.filter(ImageFilter.GaussianBlur(18))).enhance(darken)
    canvas.save(out, quality=92)
    return out


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def title_frame(out, title, subtitle="", bg_path=None, big=False):
    if bg_path:
        try:
            photo_frame(bg_path, out, darken=0.38)
            im = Image.open(out).convert("RGB")
        except Exception:
            im = None
    else:
        im = None
    if im is None:
        im = Image.new("RGB", (W2, H2), (14, 16, 22))
    d = ImageDraw.Draw(im)
    f1 = _font(FONT_BOLD, 230 if big else 190)
    f2 = _font(FONT, 92 if big else 84)
    tw = d.textlength(title, font=f1)
    while tw > W2 * 0.9 and f1.size > 60:
        f1 = _font(FONT_BOLD, f1.size - 10)
        tw = d.textlength(title, font=f1)
    cy = H2 // 2 - (60 if subtitle else 0)
    d.text((W2 / 2, cy), title, font=f1, fill=(255, 255, 255), anchor="mm",
           stroke_width=3, stroke_fill=(0, 0, 0))
    if subtitle:
        d.line((W2 / 2 - 160, cy + f1.size * 0.62, W2 / 2 + 160, cy + f1.size * 0.62),
               fill=(232, 178, 74), width=8)
        d.text((W2 / 2, cy + f1.size * 0.62 + 110), subtitle, font=f2, fill=(235, 235, 235),
               anchor="mm")
    im.save(out, quality=92)
    return out


# ---------- Klipler ----------

KB = [  # Ken Burns hareketleri (zoom ifadesi, x, y)
    ("1+0.10*on/{N}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),
    ("1.10-0.10*on/{N}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),
    ("1.08", "(iw-iw/zoom)*on/{N}", "(ih-ih/zoom)/2"),
    ("1.08", "(iw-iw/zoom)*(1-on/{N})", "(ih-ih/zoom)/2"),
    ("1+0.08*on/{N}", "(iw-iw/zoom)*0.3", "(ih-ih/zoom)*0.35"),
]


def still_clip(frame, out, dur, idx, fade_in=0.0, fade_out=0.0):
    n = int(round(dur * FPS))
    z, x, y = (s.format(N=n) for s in KB[idx % len(KB)])
    vf = f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={WIDTH}x{HEIGHT}:fps={FPS},setsar=1"
    if fade_in:
        vf += f",fade=t=in:st=0:d={fade_in}"
    if fade_out:
        vf += f",fade=t=out:st={dur - fade_out:.3f}:d={fade_out}"
    _run(["ffmpeg", "-y", "-v", "error", "-i", frame, "-f", "lavfi", "-t", f"{dur:.3f}",
          "-i", "anullsrc=r=48000:cl=stereo", "-vf", vf, "-frames:v", str(n),
          *_venc(), *AENC, "-shortest", out])


def video_clip(src, out, start, dur, info, keep_audio=True):
    tm = ""
    if info["transfer"] in ("arib-std-b67", "smpte2084") and has_zscale():
        # iPhone HDR (HLG/Dolby Vision) → normal SDR, renkler soluk çıkmasın
        tm = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
              "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")
    fc = (f"[0:v]{tm}fps={FPS},split[a][b];"
          f"[a]scale=480:270:force_original_aspect_ratio=increase,crop=480:270,boxblur=12:2,"
          f"eq=brightness=-0.12,scale={WIDTH}:{HEIGHT}[bg];"
          f"[b]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,tpad=stop_mode=clone:stop_duration=3,setsar=1,format=yuv420p[v]")
    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    if keep_audio and info["has_audio"]:
        fc += (f";[0:a]aresample=48000,aformat=channel_layouts=stereo,highpass=f=80,"
               f"loudnorm=I=-20:TP=-2:LRA=11,afade=t=in:d=0.08,"
               f"afade=t=out:st={max(dur - 0.15, 0):.3f}:d=0.15,apad[aout]")
        amap = "[aout]"
    else:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = "1:a"
    _run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc, "-map", "[v]",
          "-map", amap, "-t", f"{dur:.3f}", *_venc(), *AENC, out], timeout=3600)


# ---------- Müzik ----------

def music_track(files, length, out, work):
    durs = []
    for f in files:
        d = probe(f)["duration"]
        if d > 5:
            durs.append((f, d))
    if not durs:
        return None
    seq, tot, i = [], 0.0, 0
    while tot < length + 5 and i < 500:
        f, d = durs[i % len(durs)]
        seq.append(f)
        tot += d - 3
        i += 1
    args, fc = [], ""
    for f in seq:
        args += ["-i", f]
    if len(seq) == 1:
        fc = "[0:a]aresample=48000,aformat=channel_layouts=stereo[m]"
    else:
        prev = "[0:a]"
        for k in range(1, len(seq)):
            lab = f"[x{k}]" if k < len(seq) - 1 else "[mx]"
            fc += f"{prev}[{k}:a]acrossfade=d=3:c1=tri:c2=tri{lab};"
            prev = lab
        fc += "[mx]aresample=48000,aformat=channel_layouts=stereo[m]"
    fc += f";[m]atrim=0:{length:.3f},afade=t=in:d=1.5,afade=t=out:st={max(length - 5, 0):.3f}:d=5,loudnorm=I=-16:TP=-1.5[out]"
    _run(["ffmpeg", "-y", "-v", "error", *args, "-filter_complex", fc, "-map", "[out]",
          "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", out], timeout=3600)
    return out


# ---------- İş akışı ----------

def _set(jid, **kw):
    with db.db() as con:
        sets = ",".join(f"{k}=?" for k in kw)
        con.execute(f"UPDATE jobs SET {sets} WHERE id=?", [*kw.values(), jid])


def _safe(name):
    name = re.sub(r'[\\/:*?"<>|]+', "-", name).strip(" .")
    return name[:120] or "film"


def _render(jid, p):
    work = os.path.join(WORK_DIR, f"job{jid}")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    photo_sec, video_sec = float(p.get("photo_sec", 3.5)), float(p.get("video_sec", 7))
    ids = [i for ch in p["chapters"] for i in ch["ids"]]
    with db.db() as con:
        rows = {r["id"]: dict(r) for r in
                con.execute(f"SELECT * FROM media WHERE id IN ({','.join('?' * len(ids))})", ids)}
    total_steps = len(ids) + len(p["chapters"]) + 4
    step, clips, errors = 0, [], 0

    def tick(msg):
        nonlocal step
        step += 1
        if jid in _cancel:
            raise InterruptedError("İptal edildi")
        _set(jid, progress=round(step / total_steps * 100, 1), message=msg)

    def first_photo(idlist):
        for i in idlist:
            if i in rows and rows[i]["kind"] == "photo":
                return rows[i]["path"]
        for i in idlist:  # sadece video varsa videodan bir kare al
            if i in rows:
                out = os.path.join(work, f"vbg{i}.jpg")
                at = (rows[i]["duration"] or 2) * 0.3
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", rows[i]["path"],
                                "-frames:v", "1", out], timeout=120)
                if os.path.exists(out):
                    return out
        return None

    n = 0
    all_first = first_photo(ids)
    fr = title_frame(os.path.join(work, "intro.jpg"), p["title"], p.get("range_text", ""), all_first, big=True)
    c = os.path.join(work, f"{n:05d}.mkv")
    still_clip(fr, c, INTRO_SEC, 1, fade_in=1.0, fade_out=0.6)
    clips.append(c)
    n += 1
    tick("Açılış hazırlandı")

    for ci, ch in enumerate(p["chapters"]):
        if p.get("chapter_titles", True):
            fr = title_frame(os.path.join(work, f"t{ci}.jpg"), ch["title"], ch.get("subtitle", ""),
                             first_photo(ch["ids"]))
            c = os.path.join(work, f"{n:05d}.mkv")
            still_clip(fr, c, TITLE_SEC, 0, fade_in=0.5, fade_out=0.5)
            clips.append(c)
            n += 1
        tick(f"{ch['title']} {ch.get('subtitle', '')}")
        for i in ch["ids"]:
            r = rows.get(i)
            try:
                if not r:
                    raise FileNotFoundError(i)
                if r["kind"] == "photo":
                    fr = photo_frame(r["path"], os.path.join(work, "f.jpg"))
                    c = os.path.join(work, f"{n:05d}.mkv")
                    still_clip(fr, c, photo_sec, n)
                    clips.append(c)
                    n += 1
                else:
                    info = probe(r["path"])
                    r["duration"] = info["duration"] or r["duration"]
                    for st, du in _segments(r, video_sec):
                        c = os.path.join(work, f"{n:05d}.mkv")
                        video_clip(r["path"], c, st, du, info, p.get("video_audio", True))
                        clips.append(c)
                        n += 1
            except InterruptedError:
                raise
            except Exception:
                errors += 1
                traceback.print_exc()
            tick(f"{ch['title']}: {os.path.basename(r['path']) if r else i}")

    fr = title_frame(os.path.join(work, "outro.jpg"), p.get("outro", "Son"), "Dedplay Film")
    c = os.path.join(work, f"{n:05d}.mkv")
    still_clip(fr, c, OUTRO_SEC, 1, fade_in=0.5, fade_out=1.5)
    clips.append(c)
    tick("Klipler birleştiriliyor")

    lst = os.path.join(work, "list.txt")
    with open(lst, "w") as f:
        f.writelines(f"file '{c}'\n" for c in clips)
    film = os.path.join(work, "film.mkv")
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", film])
    length = probe(film)["duration"]
    tick("Müzik hazırlanıyor")

    music = None
    if p.get("music"):
        music = music_track(p["music"], length, os.path.join(work, "music.wav"), work)
    tick("Son kurgu ve ses miksajı")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, _safe(p["title"]) + ".mp4")
    k = 2
    while os.path.exists(out):
        out = os.path.join(OUTPUT_DIR, f"{_safe(p['title'])} ({k}).mp4")
        k += 1
    mv = float(p.get("music_volume", 0.8))
    if music:
        fc = ("[0:a]asplit=2[va][sc];"
              f"[1:a]volume={mv}[m];"
              "[m][sc]sidechaincompress=threshold=0.02:ratio=6:attack=40:release=700[md];"
              "[va][md]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", film, "-i", music, "-filter_complex", fc,
               "-map", "0:v", "-map", "[a]"]
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", film, "-map", "0:v", "-map", "0:a"]
    _run(cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out], timeout=7200)
    shutil.rmtree(work, ignore_errors=True)
    msg = "Tamamlandı" + (f" ({errors} dosya atlandı)" if errors else "")
    _set(jid, status="bitti", progress=100, message=msg, output=out)


def _worker():
    while True:
        jid = _q.get()
        with db.db() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row or jid in _cancel:
            continue
        _set(jid, status="çalışıyor", message="Başladı")
        t0 = time.time()
        try:
            _render(jid, json.loads(row["params"]))
        except InterruptedError:
            _set(jid, status="iptal", message="İptal edildi")
        except Exception as e:
            traceback.print_exc()
            _set(jid, status="hata", message=str(e)[-400:])
        finally:
            shutil.rmtree(os.path.join(WORK_DIR, f"job{jid}"), ignore_errors=True)
            print(f"iş {jid}: {time.time() - t0:.0f} sn", flush=True)


def submit(params):
    with db.db() as con:
        cur = con.execute("INSERT INTO jobs(title,status,progress,message,params) VALUES(?,?,?,?,?)",
                          (params["title"], "sırada", 0, "Sırada bekliyor", json.dumps(params)))
        jid = cur.lastrowid
    _q.put(jid)
    return jid


def cancel(jid):
    _cancel.add(jid)
    with db.db() as con:
        con.execute("UPDATE jobs SET status='iptal', message='İptal edildi' WHERE id=? AND status='sırada'", (jid,))


threading.Thread(target=_worker, daemon=True).start()
