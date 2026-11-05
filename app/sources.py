"""Kaynak türü: kamerayla mı çekilmiş, yoksa ekran görüntüsü / ekran kaydı / başka bir yerden
(WhatsApp, Instagram, TikTok, TV, internet) kaydedilmiş mi?

İpuçları:
  - Dosya adı (Screenshot_, RPReplay, IMG-…-WA…, FB_IMG, snaptik, videoplayback …)
  - Kamera bilgisi yok (Make/Model boş) — telefon kamerası her zaman yazar
  - Ekran görüntüsü işareti (iPhone UserComment "Screenshot", Software alanı)
  - PNG ya da tam olarak bir ekran çözünürlüğünde, kamera bilgisi olmayan resim
Sonuç: "ekran" (ekran görüntüsü/kaydı), "kaydedilen" (başka yerden gelen) ya da None (kamera).
"""
import os
import re

SCREEN_NAME = re.compile(
    r"screenshot|screen[ _-]?shot|screen[ _-]?record|screenrecording|rpreplay|ekran[ _-]?(resmi|görüntüsü|goruntusu|kayd)|"
    r"schermafbeelding|schermopname|bildschirmfoto|capture d.écran|simulator screen", re.I)
SAVED_NAME = re.compile(
    r"(^|[-_ ])wa\d{3,}|-wa\d|whatsapp|^fb_img|^received_|telegram|instagram|^insta|snapchat|tiktok|snaptik|ssstik|"
    r"musicaldown|videoplayback|youtube|^yt[-_]|pinterest|twitter|^tw[-_]|^download|^indir|inshot|capcut|"
    r"^image\s*\(\d+\)|^images?\d*\.|^unnamed|^photo_\d{4}-\d\d-\d\d|^video_\d{4}-\d\d-\d\d|^tv[-_ ]|kayıt|^rec[-_]", re.I)
CAMERA_NAME = re.compile(r"^(img_\d|img_e\d|vid_\d|pxl_|mvimg|dsc|dscn|dji_|gopr|gx\d|mov_\d|\d{8}_\d{6})", re.I)

# Telefon/bilgisayar ekran çözünürlükleri (kısa kenar, uzun kenar)
SCREENS = {(750, 1334), (828, 1792), (1080, 1920), (1080, 2340), (1080, 2400), (1125, 2436), (1170, 2532),
           (1179, 2556), (1242, 2208), (1242, 2688), (1284, 2778), (1290, 2796), (1440, 2560), (1440, 3040),
           (1440, 3200), (720, 1280), (720, 1600), (1366, 768), (1440, 900), (1536, 864), (1600, 900),
           (1680, 1050), (1920, 1200), (2560, 1440), (2560, 1600), (2880, 1800), (3024, 1964), (3456, 2234)}


def _norm(w, h):
    return (min(w, h), max(w, h)) if w and h else None


def classify(r):
    name = os.path.basename(r["path"])
    low = name.lower()
    cam = bool((r.get("make") or "").strip() or (r.get("model") or "").strip())
    note = f"{r.get('software') or ''} {r.get('ucomment') or ''}".lower()
    if SCREEN_NAME.search(low) or "screenshot" in note:
        return "ekran"
    if cam:
        return None                      # kamera bilgisi var → gerçek çekim
    if SAVED_NAME.search(low):
        return "kaydedilen"
    if r["kind"] == "photo":
        if low.endswith(".png"):
            return "ekran"
        dims = _norm(r.get("width"), r.get("height"))
        if dims in {_norm(a, b) for a, b in SCREENS} and not CAMERA_NAME.search(low):
            return "ekran"
        return None
    # Video: aksiyon kameraları, araç kameraları ve bazı Android telefonlar kamera bilgisi yazmaz.
    # Bu yüzden videoyu sadece adı açıkça belli ediyorsa (yukarıdaki kurallar) ayırırız; yoksa kendi çekimidir.
    return None


VERSION = 2  # kurallar değişince kayıtlı dosyalar yeniden sınıflandırılır

LABEL = {"ekran": "Ekran görüntüsü / kaydı", "kaydedilen": "Başka yerden kaydedilen"}
