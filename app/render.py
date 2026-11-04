"""Film render motoru (FFmpeg).

Akış: birimler (fotoğraf / ikili fotoğraf / video + ara planlar) → zaman çizgisi
(müziğin vuruşlarına oturtulmuş kesme noktaları) → her klip tam kare sayısıyla üretilir →
geçiş efektleriyle (xfade) birleştirilir → müzik ve video sesleri miksajlanır.
Tüm süreler kare (frame) cinsinden hesaplanır; böylece geçişler ve ses kayma yapmaz.
"""
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

from . import beats as beatmod
from . import db, highlights, story, styles
from .analyze import load_image
from .config import (CRF, FONT, FONT_BOLD, FPS, HEIGHT, OUTPUT_DIR, PRESET, THREADS, WIDTH,
                     WORK_DIR)
from .planner import INTRO_SEC, OUTRO_SEC, TITLE_SEC

W2, H2 = WIDTH * 2, HEIGHT * 2  # Ken Burns için 2x çözünürlükte hazırlanır (titremesiz)
SPF = 48000 // FPS              # kare başına ses örneği
JOIN_GROUP = 10
_q = queue.Queue()
_cancel = set()
_HAS_ZSCALE = None


def _run(cmd, timeout=3600):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-1500:])
    return p


def _venc(crf=None):
    return ["-c:v", "libx264", "-preset", PRESET, "-crf", str(crf or CRF), "-pix_fmt", "yuv420p",
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
    return {"has_audio": a, "transfer": v.get("color_transfer", ""), "duration": dur, "ok": bool(v)}


# ---------- Kareler ----------

def _cover(im, w, h, ybias=0.4):
    r = max(w / im.width, h / im.height)
    im = im.resize((max(w, round(im.width * r)), max(h, round(im.height * r))), Image.LANCZOS)
    x = (im.width - w) // 2
    y = int((im.height - h) * ybias)
    return im.crop((x, y, x + w, y + h))


def _blur_bg(im):
    bg = _cover(im.resize((max(1, im.width // 8), max(1, im.height // 8))), W2 // 8, H2 // 8)
    bg = bg.filter(ImageFilter.GaussianBlur(6)).resize((W2, H2), Image.BILINEAR)
    return ImageEnhance.Brightness(bg).enhance(0.55)


def _paste_shadow(bg, fg, x, y):
    fw, fh = fg.size
    sh = Image.new("RGBA", (fw + 80, fh + 80), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rectangle((40, 40, fw + 40, fh + 40), fill=(0, 0, 0, 150))
    sh = sh.filter(ImageFilter.GaussianBlur(25))
    bg.paste(sh, (x - 40, y - 10), sh)
    bg.paste(fg, (x, y))


def photo_frame(path, out, darken=None):
    """Fotoğrafı 16:9 kareye yerleştirir. Dikey/kare fotoğraflarda arkası bulanık doldurulur."""
    im = load_image(path, 4000)
    ar = im.width / im.height
    if 1.25 <= ar <= 2.1:
        canvas = _cover(im, W2, H2)
    else:
        canvas = _blur_bg(im)
        fh = int(H2 * 0.94)
        fw = int(fh * ar)
        if fw > W2 * 0.94:
            fw = int(W2 * 0.94)
            fh = int(fw / ar)
        _paste_shadow(canvas, im.resize((fw, fh), Image.LANCZOS), (W2 - fw) // 2, (H2 - fh) // 2)
    if darken:
        canvas = ImageEnhance.Brightness(canvas.filter(ImageFilter.GaussianBlur(18))).enhance(darken)
    canvas.save(out, quality=92)
    return out


def pair_frame(paths, out):
    """İki dikey fotoğraf yan yana, hafif kaydırılmış (dergi sayfası gibi)."""
    ims = [load_image(p, 3000) for p in paths]
    canvas = _blur_bg(ims[0])
    gap = int(W2 * 0.03)
    fh = int(H2 * 0.88)
    widths = [int(fh * im.width / im.height) for im in ims]
    total = sum(widths) + gap
    if total > W2 * 0.94:
        k = W2 * 0.94 / total
        fh = int(fh * k)
        widths = [int(w * k) for w in widths]
        total = sum(widths) + gap
    x = (W2 - total) // 2
    for i, (im, w) in enumerate(zip(ims, widths)):
        y = (H2 - fh) // 2 + (-int(H2 * 0.015) if i == 0 else int(H2 * 0.015))
        _paste_shadow(canvas, im.resize((w, fh), Image.LANCZOS), x, y)
        x += w + gap
    canvas.save(out, quality=92)
    return out


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def title_frame(out, title, subtitle="", bg_path=None, big=False):
    im = None
    if bg_path:
        try:
            photo_frame(bg_path, out, darken=0.38)
            im = Image.open(out).convert("RGB")
        except Exception:
            im = None
    if im is None:
        im = Image.new("RGB", (W2, H2), (14, 16, 22))
    d = ImageDraw.Draw(im)
    f1 = _font(FONT_BOLD, 230 if big else 190)
    f2 = _font(FONT, 92 if big else 84)
    while d.textlength(title, font=f1) > W2 * 0.9 and f1.size > 60:
        f1 = _font(FONT_BOLD, f1.size - 10)
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


# ---------- Klipler (tam kare sayısıyla) ----------

KB = [  # Ken Burns hareketleri (zoom, x, y)
    ("1+0.10*on/{N}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),
    ("1.10-0.10*on/{N}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),
    ("1.08", "(iw-iw/zoom)*on/{N}", "(ih-ih/zoom)/2"),
    ("1.08", "(iw-iw/zoom)*(1-on/{N})", "(ih-ih/zoom)/2"),
    ("1+0.08*on/{N}", "(iw-iw/zoom)*0.3", "(ih-ih/zoom)*0.35"),
]


def _kb(idx, n):
    z, x, y = (s.format(N=n) for s in KB[idx % len(KB)])
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={WIDTH}x{HEIGHT}:fps={FPS},setsar=1"


def still_clip(frame, out, frames, idx, grade="", fade_in=0, fade_out=0, audio=True):
    vf = _kb(idx, frames)
    if grade:
        vf += "," + grade
    if fade_in:
        vf += f",fade=t=in:st=0:d={fade_in}"
    if fade_out:
        vf += f",fade=t=out:st={frames / FPS - fade_out:.3f}:d={fade_out}"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", frame]
    if audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-af",
                f"atrim=end_sample={frames * SPF}"]
    cmd += ["-vf", vf, "-frames:v", str(frames), *_venc(17)]
    cmd += [*AENC] if audio else ["-an"]
    _run(cmd + [out])


def video_clip(src, out, start, frames, info, cuts=(), grade="", keep_audio=True):
    """cuts: [(ara_plan_klibi, başlangıç_karesi, kare_sayısı)] — video oynarken üstte görünür."""
    dur = frames / FPS
    tm = ""
    if info["transfer"] in ("arib-std-b67", "smpte2084") and has_zscale():
        # iPhone HDR (HLG/Dolby Vision) → normal SDR, renkler soluk çıkmasın
        tm = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
              "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")
    fc = (f"[0:v]{tm}fps={FPS},split[a][b];"
          f"[a]scale=480:270:force_original_aspect_ratio=increase,crop=480:270,boxblur=12:2,"
          f"eq=brightness=-0.12,scale={WIDTH}:{HEIGHT}[bg];"
          f"[b]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,tpad=stop_mode=clone:stop_duration=5,setsar=1,"
          f"format=yuv420p[b0]")
    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur + 0.5:.3f}", "-i", src]
    last = "b0"
    for k, (cpath, cstart, cframes) in enumerate(cuts, 1):
        inputs += ["-i", cpath]
        cd = cframes / FPS
        fc += (f";[{k}:v]format=yuva420p,fade=t=in:st=0:d=0.35:alpha=1,"
               f"fade=t=out:st={cd - 0.35:.3f}:d=0.35:alpha=1,"
               f"setpts=PTS-STARTPTS+{cstart / FPS:.4f}/TB[c{k}];"
               f"[{last}][c{k}]overlay=eof_action=pass:repeatlast=0,format=yuv420p[b{k}]")
        last = f"b{k}"
    fc += f";[{last}]{grade + ',' if grade else ''}null[v]"
    ns = frames * SPF
    if keep_audio and info["has_audio"]:
        fc += (f";[0:a]aresample=48000,aformat=channel_layouts=stereo,highpass=f=80,"
               f"loudnorm=I=-20:TP=-2:LRA=11,afade=t=in:d=0.08,apad,atrim=end_sample={ns}[aout]")
        amap = "[aout]"
    else:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        fc += f";[{len(cuts) + 1}:a]atrim=end_sample={ns}[aout]"
        amap = "[aout]"
    _run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc, "-map", "[v]",
          "-map", amap, "-frames:v", str(frames), *_venc(17), *AENC, out])


# ---------- Birleştirme (geçiş efektleri) ----------

def _join(items, df, out, work, tag):
    """items: [(yol, kare, geçiş_tipi)] → tek klip. Ardışık klipler df kare örtüşür."""
    if len(items) == 1:
        shutil.move(items[0][0], out)
        return items[0][1]
    groups = [items[i:i + JOIN_GROUP] for i in range(0, len(items), JOIN_GROUP)]
    if len(groups) > 1:
        merged = []
        for gi, g in enumerate(groups):
            gout = os.path.join(work, f"{tag}_{gi}.mkv")
            fr = _join(g, df, gout, work, f"{tag}_{gi}")
            merged.append((gout, fr, g[0][2]))
        return _join(merged, df, out, work, tag + "m")
    ds = df / FPS
    args, fc = [], ""
    for i, (p, _, _) in enumerate(items):
        args += ["-i", p]
        fc += f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[v{i}];"
    acc, prev_v, prev_a = items[0][1], "v0", "0:a"
    for i in range(1, len(items)):
        off = (acc - df) / FPS
        tv, ta = f"x{i}", f"y{i}"
        fc += (f"[{prev_v}][v{i}]xfade=transition={items[i][2]}:duration={ds:.4f}:offset={off:.4f}[{tv}];"
               f"[{prev_a}][{i}:a]acrossfade=d={ds:.4f}:c1=tri:c2=tri[{ta}];")
        acc += items[i][1] - df
        prev_v, prev_a = tv, ta
    fc = fc.rstrip(";")
    _run(["ffmpeg", "-y", "-v", "error", *args, "-filter_complex", fc, "-map", f"[{prev_v}]",
          "-map", f"[{prev_a}]", "-frames:v", str(acc), *_venc(), *AENC, out], timeout=7200)
    for p, _, _ in items:
        if p.startswith(work):
            try:
                os.remove(p)
            except OSError:
                pass
    return acc


# ---------- Müzik ----------

def music_track(files, length, out):
    """Şarkıları sırayla, 3 sn geçişle birleştirir. Dönüş: (yol, vuruş zamanları)."""
    tracks = []
    for f in files:
        d = probe(f)["duration"]
        if d > 8:
            tracks.append((f, d))
    if not tracks:
        return None, []
    beat_cache, seq, beats_all = {}, [], []
    pos, i, X = 0.0, 0, 3.0
    while pos < length + 10 and i < 500:
        f, d = tracks[i % len(tracks)]
        if f not in beat_cache:
            beat_cache[f] = beatmod.detect(f)[0]
        for b in beat_cache[f]:
            t = pos + b
            if (not beats_all or t - beats_all[-1] > 0.25) and not (i > 0 and b < X):
                beats_all.append(t)
        seq.append(f)
        pos += d - X
        i += 1
    args = []
    for f in seq:
        args += ["-i", f]
    if len(seq) == 1:
        fc = "[0:a]aresample=48000,aformat=channel_layouts=stereo[m]"
    else:
        fc, prev = "", "[0:a]"
        for k in range(1, len(seq)):
            lab = f"[x{k}]" if k < len(seq) - 1 else "[mx]"
            fc += f"{prev}[{k}:a]acrossfade=d={X}:c1=tri:c2=tri{lab};"
            prev = lab
        fc += "[mx]aresample=48000,aformat=channel_layouts=stereo[m]"
    fc += f";[m]atrim=0:{length + 10:.3f},afade=t=in:d=1.5,loudnorm=I=-16:TP=-1.5[out]"
    _run(["ffmpeg", "-y", "-v", "error", *args, "-filter_complex", fc, "-map", "[out]",
          "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", out], timeout=3600)
    return out, beats_all


def _timeline(clips, df, beats, stride):
    """Kesme noktalarını (geçiş ortaları) hesaplar; müzik varsa vuruşlara oturtur.
    clips[i]: {"want": istenen görünür kare, "max": en fazla görünür kare (video için)}"""
    grid = [round(b * FPS) for b in beats[::max(1, stride)]] if beats else []
    minv = max(int(1.4 * FPS), df + int(0.6 * FPS))
    B = [0]
    gi = 0
    for c in clips[:-1]:
        cur = B[-1]
        lo, hi = cur + minv, cur + max(minv, min(c["max"], int(c["want"] * 1.45)))
        target = cur + max(c["want"], minv)
        best = None
        while gi < len(grid) and grid[gi] < lo:
            gi += 1
        j = gi
        while j < len(grid) and grid[j] <= hi:
            if best is None or abs(grid[j] - target) < abs(best - target):
                best = grid[j]
            j += 1
        B.append(best if best is not None else min(target, cur + max(minv, c["max"])))
    B.append(B[-1] + max(clips[-1]["want"], minv))
    lens = []
    n = len(clips)
    h = df // 2
    for i in range(n):
        if n == 1:
            lens.append(B[1])
        elif i == 0:
            lens.append(B[1] + h)
        elif i == n - 1:
            lens.append(B[n] - B[n - 1] + (df - h))
        else:
            lens.append(B[i + 1] - B[i] + df)
    return B, lens


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
    st = styles.get(p.get("style"))
    grade = st["grade"] + ("," + styles.LETTERBOX if p.get("letterbox") else "")
    photo_sec = float(p.get("photo_sec") or st["photo_sec"])
    video_sec = float(p.get("video_sec", 7))
    df = max(2, round(st["transition"] * FPS) // 2 * 2)
    trans_list = st["transitions"]

    ids = [i for ch in p["chapters"] for i in ch["ids"]]
    with db.db() as con:
        rows = {r["id"]: dict(r) for r in
                con.execute(f"SELECT * FROM media WHERE id IN ({','.join('?' * len(ids))})", ids)}
    infos = {}
    for i, r in list(rows.items()):  # bozuk/okunamayan dosyaları baştan ayıkla
        if not os.path.exists(r["path"]):
            rows.pop(i)
        elif r["kind"] == "video":
            infos[i] = probe(r["path"])
            if not infos[i]["ok"] or infos[i]["duration"] < 0.8:
                rows.pop(i)
            else:
                r["duration"] = infos[i]["duration"]
                n = story.seg_count(r["duration"], video_sec)
                if n:  # uzun video: en iyi anları bul (sonuç önbelleğe yazılır)
                    _set(jid, message=f"Videodaki en iyi anlar aranıyor: {os.path.basename(r['path'])}")
                    hl = highlights.get(r, r["path"], r["duration"])
                    if not r.get("highlights"):
                        with db.db() as con:
                            con.execute("UPDATE media SET highlights=? WHERE id=?", (json.dumps(hl), i))
                    r["hl_starts"] = highlights.pick(hl, r["duration"], n, video_sec)

    def first_visual(idlist):
        for i in idlist:
            if i in rows and rows[i]["kind"] == "photo":
                return rows[i]["path"]
        for i in idlist:
            if i in rows:
                out = os.path.join(work, f"vbg{i}.jpg")
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{rows[i]['duration'] * 0.3:.2f}",
                                "-i", rows[i]["path"], "-frames:v", "1", out], timeout=120)
                if os.path.exists(out):
                    return out
        return None

    # 1) Mantıksal klip listesi
    clips = [{"kind": "title", "title": p["title"], "sub": p.get("range_text", ""),
              "bg": first_visual(ids), "big": True, "want": int(INTRO_SEC * FPS), "trans": None}]
    k = 0
    for ch in p["chapters"]:
        crow = [rows[i] for i in ch["ids"] if i in rows]
        if not crow:
            continue
        # Çok bölüm varsa 1-2 karelik günler için ayrı başlık kartı göstermeyiz (akış bölünmesin)
        if p.get("chapter_titles", True) and (len(crow) >= 3 or len(p["chapters"]) <= 6):
            clips.append({"kind": "title", "title": ch["title"], "sub": ch.get("subtitle", ""),
                          "bg": first_visual(ch["ids"]), "want": int(TITLE_SEC * FPS), "trans": "fadeblack"})
        first_in_chapter = True
        for u in story.build_units(crow, video_sec):
            tr = "fadeblack" if (first_in_chapter and not p.get("chapter_titles", True)) else trans_list[k % len(trans_list)]
            first_in_chapter = False
            if u["type"] == "video":
                r = rows[u["ids"][0]]
                for sg in u["segments"]:
                    avail = max(1.0, r["duration"] - sg["start"])
                    clips.append({"kind": "video", "row": r, "start": sg["start"], "cuts": sg["cuts"],
                                  "want": int(sg["len"] * FPS), "max": int((avail + 0.3) * FPS), "trans": tr})
                    k += 1
                    tr = trans_list[k % len(trans_list)]
            else:
                mult = 1.3 if u["type"] == "pair" else 1.0
                clips.append({"kind": u["type"], "paths": [rows[i]["path"] for i in u["ids"]],
                              "want": int(photo_sec * mult * FPS), "trans": tr})
                k += 1
    clips.append({"kind": "title", "title": p.get("outro", "Son"), "sub": "Dedplay Film",
                  "want": int(OUTRO_SEC * FPS), "trans": "fadeblack", "bg": None})
    for c in clips:
        c.setdefault("max", 10 ** 9)

    total_steps = len(clips) + 4
    step = 0

    def tick(msg):
        nonlocal step
        step += 1
        if jid in _cancel:
            raise InterruptedError("İptal edildi")
        _set(jid, progress=round(step / total_steps * 100, 1), message=msg)

    # 2) Müzik ve vuruşlar
    est = sum(c["want"] for c in clips) / FPS
    music, beats = None, []
    if p.get("music"):
        _set(jid, message="Müzik ve ritim analiz ediliyor")
        music, beats = music_track(p["music"], est + 30, os.path.join(work, "music.wav"))
    if not p.get("beat_sync", True):
        beats = []
    B, lens = _timeline(clips, df, beats, st["beat_stride"])
    tick("Zaman çizgisi hazır" + (f" ({len(beats)} vuruş)" if beats else ""))

    # 3) Klipler
    items, errors, last_frame = [], 0, None
    for n, (c, L) in enumerate(zip(clips, lens)):
        out = os.path.join(work, f"c{n:05d}.mkv")
        try:
            if c["kind"] == "title":
                fr = title_frame(os.path.join(work, f"t{n}.jpg"), c["title"], c["sub"], c.get("bg"),
                                 big=c.get("big", False))
                still_clip(fr, out, L, 1 if c.get("big") else 0, grade,
                           fade_in=1.0 if n == 0 else 0, fade_out=1.5 if n == len(clips) - 1 else 0)
            elif c["kind"] in ("photo", "pair"):
                fr = os.path.join(work, f"f{n}.jpg")
                if c["kind"] == "pair":
                    pair_frame(c["paths"], fr)
                else:
                    photo_frame(c["paths"][0], fr)
                still_clip(fr, out, L, n, grade)
                last_frame = fr
            else:
                r = c["row"]
                cuts, nc = [], len(c["cuts"])
                lead, tail, cut_f = int(story.LEAD * FPS), int(story.TAIL * FPS), int(story.CUT_SEC * FPS)
                while nc and (L - lead - tail) < nc * cut_f:
                    nc -= 1
                if nc:
                    slot = (L - lead - tail) // nc
                    for j, cid in enumerate(c["cuts"][:nc]):
                        if cid not in rows:
                            continue
                        cf = os.path.join(work, f"cut{n}_{j}.jpg")
                        photo_frame(rows[cid]["path"], cf)
                        cm = os.path.join(work, f"cut{n}_{j}.mkv")
                        still_clip(cf, cm, cut_f, n + j + 2, audio=False)
                        cuts.append((cm, lead + j * slot + (slot - cut_f) // 2, cut_f))
                video_clip(r["path"], out, c["start"], L, infos[r["id"]], cuts, grade,
                           p.get("video_audio", True))
        except InterruptedError:
            raise
        except Exception:
            errors += 1
            traceback.print_exc()
            # Zaman çizgisi bozulmasın: yerine bir önceki kareyi (yoksa koyu kare) koy
            fr = last_frame or title_frame(os.path.join(work, f"e{n}.jpg"), "", "")
            still_clip(fr, out, L, n, grade)
        items.append((out, L, c["trans"] or "fade"))
        tick(f"Klip {n + 1}/{len(clips)}")

    # 4) Geçişlerle birleştir
    _set(jid, message="Geçiş efektleriyle birleştiriliyor")
    film = os.path.join(work, "film.mkv")
    frames = _join(items, df, film, work, "j")
    length = frames / FPS
    tick("Ses miksajı")

    # 5) Müzik + video sesi
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, _safe(p["title"]) + ".mp4")
    k = 2
    while os.path.exists(out):
        out = os.path.join(OUTPUT_DIR, f"{_safe(p['title'])} ({k}).mp4")
        k += 1
    mv = float(p.get("music_volume", 0.8))
    if music:
        fc = ("[0:a]asplit=2[va][sc];"
              f"[1:a]atrim=0:{length:.3f},afade=t=out:st={max(length - 5, 0):.3f}:d=5,volume={mv}[m];"
              "[m][sc]sidechaincompress=threshold=0.02:ratio=6:attack=40:release=700[md];"
              "[va][md]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", film, "-i", music, "-filter_complex", fc,
               "-map", "0:v", "-map", "[a]"]
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", film, "-map", "0:v", "-map", "0:a"]
    _run(cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out], timeout=7200)
    tick("Bitti")
    msg = f"Tamamlandı · {int(length // 60)} dk {int(length % 60)} sn" + (f" ({errors} dosya atlandı)" if errors else "")
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
