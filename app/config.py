"""Central configuration for the app.

Every setting is read from environment variables to stay Docker-friendly.
Defaults work both for local development (outside Docker) and production.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
_DEFAULT_BASE = APP_DIR.parent  # repo root when running in local dev

# --- Paths --------------------------------------------------------------------
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", str(_DEFAULT_BASE / "secrets")))
DATA_DIR = Path(os.getenv("DATA_DIR", str(_DEFAULT_BASE / "data")))
SEAGATE_PATH = Path(os.getenv("SEAGATE_PATH", "/data/seagate"))

CREDENTIALS_FILE = SECRETS_DIR / "credentials.json"   # OAuth client (from Google Cloud)
TOKEN_FILE = SECRETS_DIR / "token.json"               # user token (created by the app)
DB_FILE = DATA_DIR / "sync_history.db"                # sync history (SQLite)

# --- Google Drive --------------------------------------------------------------
# The "drive" scope is required: the app must read the WHOLE My Drive to compare
# and overwrite existing files. Never widen/narrow the scope anywhere else.
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Root folder on Drive used for comparison/sync.
#   "root"           -> the entire My Drive
#   "Backup/Seagate" -> a subfolder (created automatically on upload if missing)
DRIVE_ROOT_DEFAULT = os.getenv("DRIVE_ROOT_FOLDER", "root").strip() or "root"

# --- Security -------------------------------------------------------------------
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# The app's own URL, used as the OAuth redirect (web-style loopback). After the
# user grants access on Google, the browser returns here with ?code=... and the
# app exchanges it for a token. Change when behind a reverse proxy
# (e.g. https://sync.example.com/).
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8501/")

# --- Performance -----------------------------------------------------------------
UPLOAD_CHUNK = 8 * 1024 * 1024    # 8 MiB per request (resumable upload)
DOWNLOAD_CHUNK = 8 * 1024 * 1024

# Number of files transferred in parallel during sync (each worker gets its own
# DriveClient). 3-4 is the sweet spot: clearly faster with many small files while
# staying far below Drive API rate limits. Set 1 for sequential transfers.
SYNC_WORKERS = max(1, int(os.getenv("SYNC_WORKERS", "4") or "4"))

# Local "trash" directory on the Seagate drive used by mirror-mode deletions.
LOCAL_TRASH_DIRNAME = ".sync_trash"

# Default exclude patterns (Windows & macOS system/junk files).
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
    """Create the writable directories (secrets/, data/) up front."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
