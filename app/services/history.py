"""Sync history (SQLite).

Every function opens its own connection (safe to call from both the sync
thread and the UI thread). WAL mode allows concurrent reads/writes without
blocking each other.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from config import DB_FILE

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    direction       TEXT NOT NULL,
    mode            TEXT NOT NULL,
    planned_files   INTEGER NOT NULL,
    planned_bytes   INTEGER NOT NULL,
    done_files      INTEGER NOT NULL DEFAULT 0,
    failed_files    INTEGER NOT NULL DEFAULT 0,
    done_bytes      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    details         TEXT
);
"""


def _conn() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)


def start_session(direction: str, mode: str, planned_files: int, planned_bytes: int) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (started_at, direction, mode, planned_files, planned_bytes, status)"
            " VALUES (?, ?, ?, ?, ?, 'running')",
            (datetime.now().isoformat(timespec="seconds"), direction, mode, planned_files, planned_bytes),
        )
        return int(cur.lastrowid)


def finish_session(
    session_id: int,
    done_files: int,
    failed_files: int,
    done_bytes: int,
    status: str,
    errors: Optional[list[str]] = None,
) -> None:
    details = json.dumps((errors or [])[:100], ensure_ascii=False)
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET finished_at=?, done_files=?, failed_files=?, done_bytes=?,"
            " status=?, details=? WHERE id=?",
            (
                datetime.now().isoformat(timespec="seconds"),
                done_files,
                failed_files,
                done_bytes,
                status,
                details,
                session_id,
            ),
        )


def fetch_sessions(limit: int = 100) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
