"""Cau hinh trung tam cho ung dung.

Moi thiet lap deu doc tu bien moi truong de than thien voi Docker.
Gia tri mac dinh phu hop cho ca chay dev (ngoai Docker) lan production.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
_DEFAULT_BASE = APP_DIR.parent  # khi chay dev: thu muc goc cua repo

# --- Duong dan ---------------------------------------------------------------
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", str(_DEFAULT_BASE / "secrets")))
DATA_DIR = Path(os.getenv("DATA_DIR", str(_DEFAULT_BASE / "data")))
SEAGATE_PATH = Path(os.getenv("SEAGATE_PATH", "/data/seagate"))

CREDENTIALS_FILE = SECRETS_DIR / "credentials.json"   # OAuth client (tai tu Google Cloud)
TOKEN_FILE = SECRETS_DIR / "token.json"               # token nguoi dung (app tu tao)
DB_FILE = DATA_DIR / "sync_history.db"                # lich su dong bo (SQLite)

# --- Google Drive ------------------------------------------------------------
# Pham vi "drive" la bat buoc: can doc TOAN BO My Drive de so sanh va ghi de
# file co san. KHONG duoc tu y mo rong/thu hep scope o noi khac.
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Thu muc goc tren Drive dung de so sanh/dong bo.
#   "root"        -> toan bo My Drive
#   "Backup/Seagate" -> thu muc con (tao tu dong khi upload neu chua co)
DRIVE_ROOT_DEFAULT = os.getenv("DRIVE_ROOT_FOLDER", "root").strip() or "root"

# --- Bao mat -----------------------------------------------------------------
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# URL cua chinh ung dung, dung lam redirect OAuth (loopback kieu web). Sau khi
# nguoi dung cho phep tren Google, trinh duyet quay ve day kem ?code=... va app
# tu doi lay token. Doi neu chay sau reverse proxy (vd https://sync.example.com/).
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8501/")

# --- Hieu nang ---------------------------------------------------------------
UPLOAD_CHUNK = 8 * 1024 * 1024    # 8 MiB / lan gui (resumable upload)
DOWNLOAD_CHUNK = 8 * 1024 * 1024
HASH_CHUNK = 4 * 1024 * 1024

# Thu muc "thung rac" cuc bo tren o Seagate khi bat che do mirror huong xuong.
LOCAL_TRASH_DIRNAME = ".sync_trash"

# Mau loai tru mac dinh (file he thong / rac cua Windows & macOS).
DEFAULT_EXCLUDES = [
    "System Volume Information",
    "$RECYCLE.BIN",
    "RECYCLER",
    ".Trashes",
    ".Trash-*",
    LOCAL_TRASH_DIRNAME,
    ".Spotlight-V100",
    ".fseventsd",
    ".TemporaryItems",
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
    "._*",
    "*.tmp",
    "*.syncpart",
]


def ensure_dirs() -> None:
    """Tao san cac thu muc ghi duoc (secrets/, data/)."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
