"""Tiny persistent UI preferences (JSON in DATA_DIR).

Streamlit's session_state dies on every page reload (F5), so user choices
like the sync scope would silently revert to their defaults. Preferences
saved here survive reloads and container restarts.
"""
from __future__ import annotations

import json
import os
from typing import Any

from config import DATA_DIR

PREFS_FILE = DATA_DIR / "ui_prefs.json"


def load() -> dict[str, Any]:
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(**updates: Any) -> None:
    """Merge `updates` into the stored preferences (atomic write)."""
    prefs = load()
    prefs.update(updates)
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREFS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PREFS_FILE)
