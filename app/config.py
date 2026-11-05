import os

# Uygulamanın oluşturduğu klasör/dosyaları herkes açabilsin (ZimaOS istemcisi, Samba, Finder)
os.umask(0)

# Klasörler (docker-compose içinde bağlanır)
MEDIA_DIRS = [p.strip() for p in os.environ.get("MEDIA_DIRS", "/media/photos,/media/videos").split(",") if p.strip()]
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/media/music")
MUSIC_DEFAULT = os.environ.get("MUSIC_DEFAULT", "muzaffer muzik")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
# Çöp kutusu: arşivle AYNI bağlama noktası altında olmalı (taşıma anında olsun, kopyalama değil)
TRASH_DIR = os.environ.get("TRASH_DIR", "")
# Gelen kutusunu besleyen klasörler (ör. ZimaOS iPhone yedeği): bitmiş dosyalar Gelen'e taşınır
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backup")   # ZimaOS istemci yedekleri (telefon klasörleri kendiliğinden bulunur)
INBOX_FEEDS = [p.strip() for p in os.environ.get("INBOX_FEEDS", "").split(",") if p.strip()]
# Eski sürümün yolları → yeni yollar (veritabanı yeniden taramaya gerek kalmadan güncellenir)
PATH_MIGRATE = [tuple(x.split("=", 1)) for x in os.environ.get("PATH_MIGRATE", "").split(",") if "=" in x]

DB_PATH = os.path.join(DATA_DIR, "dedplay-film.db")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
WORK_DIR = os.path.join(DATA_DIR, "work")

FONT = os.environ.get("FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_BOLD = os.environ.get("FONT_BOLD", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

# Film ayarları
WIDTH, HEIGHT, FPS = 1920, 1080, 30
CRF = int(os.environ.get("CRF", "20"))
PRESET = os.environ.get("PRESET", "veryfast")
THREADS = os.environ.get("FFMPEG_THREADS", "0")

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".mkv", ".avi", ".mts"}
MUSIC_EXT = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".opus"}
SKIP_DIRS = {"@eaDir", ".thumbnails", ".trash", ".Trash", "#recycle", "@Recycle"}

for d in (DATA_DIR, THUMB_DIR, WORK_DIR, OUTPUT_DIR) + ((TRASH_DIR,) if TRASH_DIR else ()):
    os.makedirs(d, exist_ok=True)
