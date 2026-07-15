"""Cache danh sach Drive giua cac lan quet (phuc vu quet tang dan).

Luu map phang {id -> metadata} + changes-token cua lan quet truoc vao mot
file JSON trong DATA_DIR. Lan quet sau chi hoi Drive "co gi thay doi tu
token nay?" (changes.list) roi va cap nhat map — thay vi tai lai toan bo.

Cache gan voi TAI KHOAN (email): doi tai khoan la cache vo nghia -> quet
day du lai. Thu muc goc thi KHONG anh huong: map phang chua ca My Drive,
cay duoc dung lai tu goc bat ky (build_tree).
"""
from __future__ import annotations

import json
import os
from typing import Optional

from config import DATA_DIR

CACHE_FILE = DATA_DIR / "drive_cache.json"

# Chi giu cac truong build_tree can — khop _ITEM_FIELDS ben gdrive.py.
_KEEP_FIELDS = ("id", "name", "mimeType", "size", "modifiedTime", "parents")

# Doi cau truc cache thi tang so nay de vo hieu hoa cache cu.
_VERSION = 1


def _slim(item: dict) -> dict:
    return {k: item[k] for k in _KEEP_FIELDS if k in item}


def load(account: str) -> Optional[tuple[dict[str, dict], str]]:
    """Tra ve (items, token) neu co cache hop le cua dung tai khoan, khong thi None."""
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
    """Ghi cache (atomic: ghi file tam roi os.replace)."""
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
    """Cap nhat map phang tai cho theo ket qua changes.list."""
    for fid in removed:
        items.pop(fid, None)
    for fid, item in upserts.items():
        items[fid] = _slim(item)
