"""Cache of the Drive listing between scans (enables incremental scanning).

Stores the flat map {id -> metadata} plus the changes-token of the previous
scan in a JSON file under DATA_DIR. The next scan only asks Drive "what
changed since this token?" (changes.list) and patches the map — instead of
re-downloading the whole listing.

The cache is tied to the ACCOUNT (email): a different account makes the cache
meaningless -> full rescan. The root folder does NOT matter: the flat map
covers the whole My Drive and the tree is rebuilt from any root (build_tree).
"""
from __future__ import annotations

import json
import os
from typing import Optional

from config import DATA_DIR

CACHE_FILE = DATA_DIR / "drive_cache.json"

# Keep only the fields build_tree needs — must match _ITEM_FIELDS in gdrive.py.
_KEEP_FIELDS = ("id", "name", "mimeType", "size", "modifiedTime", "parents")

# Bump this number whenever the cache structure changes to invalidate old caches.
_VERSION = 1


def _slim(item: dict) -> dict:
    return {k: item[k] for k in _KEEP_FIELDS if k in item}


def load(account: str) -> Optional[tuple[dict[str, dict], str]]:
    """Return (items, token) if a valid cache exists for this account, else None."""
    if not account or not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("version") != _VERSION or data.get("account") != account:
        return None
    items = data.get("items")
    token = data.get("token")
    if not isinstance(items, dict) or not token:
        return None
    return items, token


def save(account: str, token: str, items: dict[str, dict]) -> None:
    """Write the cache (atomic: temp file + os.replace)."""
    if not account:
        return
    payload = {
        "version": _VERSION,
        "account": account,
        "token": token,
        "items": {fid: _slim(it) for fid, it in items.items()},
    }
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)


def clear() -> None:
    CACHE_FILE.unlink(missing_ok=True)


def apply_changes(
    items: dict[str, dict],
    upserts: dict[str, dict],
    removed: set[str],
) -> None:
    """Patch the flat map in place with the results of changes.list."""
    for fid in removed:
        items.pop(fid, None)
    for fid, item in upserts.items():
        items[fid] = _slim(item)
