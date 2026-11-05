import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    folder TEXT NOT NULL,
    kind TEXT NOT NULL,          -- photo | video
    size INTEGER, mtime REAL,
    taken_at TEXT,               -- 'YYYY-MM-DD HH:MM:SS' (yerel saat)
    date_source TEXT,            -- exif | google | filename | mtime
    lat REAL, lon REAL,
    width INTEGER, height INTEGER,
    duration REAL,
    sharp REAL,                  -- netlik puanı (yüksek = net)
    dhash TEXT,                  -- görsel iz (foto: 256 bit, video: 3 kare)
    analyzed INTEGER DEFAULT 0,
    highlights TEXT,
    rotate_fix INTEGER,          -- elle verilen yön düzeltmesi (saat yönü, derece)
    auto_rot INTEGER,            -- otomatik bulunan yön düzeltmesi (NULL = henüz bakılmadı)
    meta_rot INTEGER,            -- dosyadaki yön bilgisini FFmpeg okumuyorsa gereken düzeltme
    dup_of INTEGER               -- başka bir dosyanın kopyasıysa asıl dosyanın id'si
);
CREATE INDEX IF NOT EXISTS idx_media_taken ON media(taken_at);
CREATE INDEX IF NOT EXISTS idx_media_folder ON media(folder);
CREATE TABLE IF NOT EXISTS trash (
    id INTEGER PRIMARY KEY,
    media_id INTEGER, kind TEXT, orig_path TEXT NOT NULL, trash_path TEXT NOT NULL,
    size INTEGER, taken_at TEXT, reason TEXT, keeper_path TEXT, extras TEXT,
    batch TEXT, moved_at TEXT DEFAULT (datetime('now','localtime')),
    status TEXT DEFAULT 'çöpte'      -- çöpte | geri alındı | silindi
);
CREATE INDEX IF NOT EXISTS idx_trash_status ON trash(status);
CREATE TABLE IF NOT EXISTS moves (
    id INTEGER PRIMARY KEY, media_id INTEGER, src TEXT, dst TEXT, extras TEXT, batch TEXT,
    at TEXT DEFAULT (datetime('now','localtime')), undone INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pins (path TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS scene_pairs (a INTEGER, b INTEGER, inl INTEGER, ratio REAL, PRIMARY KEY(a, b));
CREATE TABLE IF NOT EXISTS scene_pairs2 (a INTEGER, b INTEGER, inl INTEGER, area REAL, overlap REAL, diff REAL,
                                         PRIMARY KEY(a, b));
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    title TEXT, status TEXT, progress REAL DEFAULT 0, message TEXT,
    output TEXT, created_at TEXT DEFAULT (datetime('now','localtime')),
    params TEXT
);
"""


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


@contextmanager
def db():
    with _lock:
        con = connect()
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init():
    with db() as con:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(media)")}
        for name, typ in (("sharp", "REAL"), ("dhash", "TEXT"), ("analyzed", "INTEGER DEFAULT 0"),
                          ("dup_of", "INTEGER"), ("highlights", "TEXT"),
                          ("rotate_fix", "INTEGER"), ("auto_rot", "INTEGER"),
                          ("meta_rot", "INTEGER"), ("qhash", "TEXT"), ("faces", "INTEGER"),
                          ("eyes", "INTEGER"), ("pinned", "INTEGER"), ("make", "TEXT"),
                          ("model", "TEXT"), ("software", "TEXT"), ("ucomment", "TEXT"),
                          ("src_checked", "INTEGER"), ("src", "TEXT"), ("smiles", "INTEGER"),
                          ("fsharp", "REAL"), ("expo", "REAL"), ("q_done", "INTEGER"),
                          ("live_id", "TEXT"), ("live_of", "INTEGER"), ("short_photo", "INTEGER")):
            if name not in cols:
                con.execute(f"ALTER TABLE media ADD COLUMN {name} {typ}")
        # Eski sürümün 64 bitlik izleri ve analiz edilmemiş videolar yeniden analiz edilir
        con.execute("UPDATE media SET analyzed=0 WHERE kind='photo' AND length(dhash)=16")
        con.execute("UPDATE media SET analyzed=0 WHERE kind='video' AND dhash IS NULL AND analyzed=1")
        con.execute("CREATE INDEX IF NOT EXISTS idx_media_dup ON media(dup_of)")
        # Önceki sürümün otomatik yön kararları gevşekti (yapraklarda yanlış yüz bulabiliyordu): yeniden bak
        if "meta_rot" not in cols:
            con.execute("UPDATE media SET auto_rot=NULL WHERE kind='video'")
        if "live_id" not in cols:  # canlı fotoğraf kimliği için kaynak bilgisini yeniden oku
            con.execute("UPDATE media SET src_checked=NULL")
        # Klasör bağlantıları değiştiyse kayıtlı yolları güncelle (yeniden tarama gerekmez)
        from .config import PATH_MIGRATE
        for old, new in PATH_MIGRATE:
            old, new = old.rstrip("/") + "/", new.rstrip("/") + "/"
            n = len(old)
            con.execute("UPDATE media SET path=? || substr(path, ?), folder=? || substr(folder || '/', ?) "
                        "WHERE substr(path, 1, ?) = ?", (new, n + 1, new, n + 1, n, old))
            con.execute("UPDATE media SET folder=rtrim(folder, '/')")
        # Geri alınan dosyalar artık kilitlenmiyor: eski kilitleri kaldır
        con.execute("DELETE FROM pins")
        # Yarım kalmış işleri kapat (container yeniden başlarsa)
        con.execute("UPDATE jobs SET status='hata', message='Sunucu yeniden başladı' "
                    "WHERE status IN ('sırada','çalışıyor')")
