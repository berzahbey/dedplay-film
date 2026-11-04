"""Görüntü yükleme ve kalite ölçümü (netlik + benzer kare izi)."""
import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

Image.MAX_IMAGE_PIXELS = 300_000_000


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


def photo_metrics(path):
    try:
        im = load_image(path, 640).convert("L")
        a = np.asarray(im, dtype=np.float32)
        lap = (a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4 * a[1:-1, 1:-1])
        sharp = float(lap.var())
        small = np.asarray(im.resize((9, 8), Image.BILINEAR), dtype=np.int16)
        bits = (small[:, 1:] > small[:, :-1]).flatten()
        dh = "%016x" % int("".join("1" if b else "0" for b in bits), 2)
        return sharp, dh
    except Exception:
        return None, None


def hamming(a, b):
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")
