"""Görüntü yükleme, kalite ölçümü ve görsel parmak izi (kopya tespiti için)."""
import subprocess

import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

Image.MAX_IMAGE_PIXELS = 300_000_000
HASH_HEX = 64  # 256 bit


def load_image(path, max_side=None):
    im = Image.open(path)
    if max_side and im.format == "JPEG":
        im.draft("RGB", (max_side, max_side))  # hızlı küçültülmüş okuma
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def dhash_gray(arr17x16):
    """17x16 gri diziden 256 bitlik fark izi (yeniden boyutlandırma/sıkıştırmaya dayanıklı)."""
    a = np.asarray(arr17x16, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return np.packbits(bits).tobytes().hex()


def photo_metrics(path):
    try:
        im = load_image(path, 640).convert("L")
        a = np.asarray(im, dtype=np.float32)
        lap = (a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4 * a[1:-1, 1:-1])
        sharp = float(lap.var())
        dh = dhash_gray(im.resize((17, 16), Image.BILINEAR))
        return sharp, dh
    except Exception:
        return None, None


def video_fingerprint(path, duration):
    """Videonun %15, %50 ve %85'inden birer kare izi → farklı adlı aynı videoları bulur."""
    if not duration or duration <= 0:
        return None
    out = []
    for f in (0.15, 0.5, 0.85):
        try:
            p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{duration * f:.2f}", "-i", path,
                                "-frames:v", "1", "-vf", "scale=17:16:flags=area,format=gray",
                                "-f", "rawvideo", "-"], capture_output=True, timeout=60)
            if len(p.stdout) != 17 * 16:
                return None
            out.append(dhash_gray(np.frombuffer(p.stdout, np.uint8).reshape(16, 17)))
        except Exception:
            return None
    return "".join(out)


def analyze_item(args):
    """İşçi süreçte çalışır: (id, kind, path, duration) → (id, sharp, dhash)."""
    mid, kind, path, duration = args
    if kind == "photo":
        s, h = photo_metrics(path)
        return mid, s, h
    return mid, None, video_fingerprint(path, duration)


_POP = np.array([bin(i).count("1") for i in range(256)], np.uint8)


def to_bytes(h):
    return np.frombuffer(bytes.fromhex(h), np.uint8)


def hamming(a, b):
    if not a or not b or len(a) != len(b):
        return 10_000
    return int(_POP[to_bytes(a) ^ to_bytes(b)].sum())
