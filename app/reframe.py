"""Dikey (ve kare / 4:3) videoları tam ekran 16:9 yapar — akıllı kırpma.

Video genişliği 1920'ye büyütülür, içinden 1920x1080'lik bir pencere kesilir. Pencere,
bir kameraman gibi önemli olanı takip eder:
  1. Yüz varsa yüzler (yüz pencerenin üst-orta kısmında kalır)
  2. Yoksa hareket ve detayın yoğun olduğu bölge
  3. O da belirsizse üst-orta kısım (insanların başı genelde burada)
Pencere titremez: küçük kaymalar yok sayılır, hareket yavaş ve yumuşaktır.
"""
import subprocess

import numpy as np

try:
    import cv2
    _FACE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    _PROF = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
except Exception:  # pragma: no cover
    cv2 = None

SAMPLE_FPS = 4
AW = 270  # analiz genişliği


def _sample(path, start, dur, w, h):
    ah = int(round(AW * h / w / 2)) * 2
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", path,
                        "-vf", f"fps={SAMPLE_FPS},scale={AW}:{ah},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=600)
    n = len(p.stdout) // (AW * ah)
    if n == 0:
        return None, ah
    return np.frombuffer(p.stdout[:n * AW * ah], np.uint8).reshape(n, ah, AW), ah


def _targets(frames, ah):
    """Her örnek kare için ilgi merkezinin dikey konumu (0-1 arası) ve güven."""
    n = len(frames)
    ys, conf = np.full(n, np.nan), np.zeros(n)
    prev = None
    for i, f in enumerate(frames):
        found = False
        if cv2 is not None:
            faces = list(_FACE.detectMultiScale(f, 1.15, 4, minSize=(18, 18)))
            if not faces:
                faces = list(_PROF.detectMultiScale(f, 1.15, 4, minSize=(18, 18)))
            if faces:
                # en büyük yüzlerin ağırlıklı merkezi; pencere yüzün biraz altına ortalanır
                areas = np.array([fw * fh for (_, _, fw, fh) in faces], float)
                cy = np.array([y + fh * 0.5 for (_, y, _, fh) in faces], float)
                ys[i] = float((cy * areas).sum() / areas.sum()) / ah
                conf[i] = 1.0
                found = True
        if not found:
            a = f.astype(np.float32)
            edge = np.abs(np.diff(a, axis=1)).mean(axis=1)
            mot = np.abs(a - prev).mean(axis=1) if prev is not None else np.zeros(ah)
            prof = edge / (edge.mean() + 1e-6) + 1.5 * mot / (mot.mean() + 1e-6)
            prof = np.convolve(prof, np.ones(9) / 9, mode="same")
            rows = np.arange(ah) / ah
            prior = np.exp(-0.5 * ((rows - 0.42) / 0.45) ** 2)  # hafif üst-orta tercihi
            wgt = np.maximum(prof - np.median(prof), 0) * prior
            if wgt.sum() < 1e-6:
                continue
            c = float((rows * wgt).sum() / wgt.sum())
            ys[i] = c
            # ilgi tek bir bölgede toplanmışsa güven yüksek, her yere dağılmışsa düşük
            frac = float(wgt[np.abs(rows - c) < 0.15].sum() / wgt.sum())
            conf[i] = float(np.clip((frac - 0.3) / 0.5, 0.15, 0.9))
        prev = f.astype(np.float32)
    return ys, conf


def plan_crop(path, start, dur, w, h, frames_out, fps=30, out_w=1920, out_h=1080):
    """Dönüş: (ölçekli_yükseklik, her çıkış karesi için kırpma y değeri listesi)."""
    sh = int(round(out_w * h / w / 2)) * 2
    span = sh - out_h
    if span <= 0:
        return sh, [0] * frames_out
    default = 0.42
    fr, ah = _sample(path, start, dur, w, h)
    if fr is None or len(fr) == 0:
        y = int(np.clip(default * sh - out_h / 2, 0, span))
        return sh, [y] * frames_out
    ys, conf = _targets(fr, ah)
    # güvene göre varsayılan ile karıştır, sonra aykırı değerleri ayıkla (medyan)
    ys = np.where(np.isnan(ys), default, ys)
    ys = conf * ys + (1 - conf) * default
    k = 5
    pad = np.pad(ys, k // 2, mode="edge")
    ys = np.array([np.median(pad[i:i + k]) for i in range(len(ys))])
    # örnekleri çıkış karelerine yay
    t_s = (np.arange(len(ys)) + 0.5) / SAMPLE_FPS
    t_o = np.arange(frames_out) / fps
    target = np.interp(t_o, t_s, ys) * sh - out_h / 2
    target = np.clip(target, 0, span)
    # kameraman davranışı: geleceği de bildiğimiz için önden giden yumuşatma (gecikmesiz),
    # küçük oynamalar yok sayılır, hareket hız sınırlı ve yumuşak
    sig = 0.6 * fps
    kx = np.arange(-int(3 * sig), int(3 * sig) + 1)
    ker = np.exp(-0.5 * (kx / sig) ** 2)
    ker /= ker.sum()
    smooth = np.convolve(np.pad(target, len(kx) // 2, mode="edge"), ker, mode="valid")[:frames_out]
    dead, max_speed = 50.0, 650.0 / fps
    pos = float(smooth[0])
    out = []
    for tv in smooth:
        err = tv - pos
        if abs(err) > dead:
            pos += float(np.clip(err - np.sign(err) * dead * 0.5, -max_speed, max_speed))
        pos = float(np.clip(pos, 0, span))
        out.append(int(round(pos)))
    return sh, out


def sendcmd_file(ys, fps, path, name="crop@rf"):
    with open(path, "w") as f:
        last = None
        for i, y in enumerate(ys):
            if y != last:
                f.write(f"{i / fps:.4f} {name} y {y};\n")
                last = y
    return path
