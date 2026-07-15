"""Scan the Seagate drive (a locally mounted directory).

Returns a map {relative POSIX path -> LocalFile}. The relative path is the
"key" used to match entries against the Google Drive tree.
"""
from __future__ import annotations

import fnmatch
import os
import stat as stat_mod
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from services.common import SyncCancelled


@dataclass(frozen=True)
class LocalFile:
    relpath: str        # POSIX-style relative path, e.g. "Photos/2024/a.jpg"
    path: Path          # absolute path on disk
    size: int
    mtime: float        # unix timestamp


def _matches_any(name: str, relpath: str, patterns: list[str]) -> bool:
    """Case-insensitive match against the file name OR the relative path."""
    name_cf = name.casefold()
    rel_cf = relpath.casefold()
    for pat in patterns:
        p = pat.casefold()
        if fnmatch.fnmatchcase(name_cf, p) or fnmatch.fnmatchcase(rel_cf, p):
            return True
    return False


def scan_local(
    root: Path,
    exclude_patterns: list[str],
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> tuple[dict[str, LocalFile], list[str]]:
    """Walk the whole `root`.

    Returns:
        (files, errors) — errors is the list of skipped read failures (they
        must not abort the whole scan; removable drives often have a few
        locked/corrupt entries).
    """
    root = Path(root)
    files: dict[str, LocalFile] = {}
    errors: list[str] = []
    total_bytes = 0

    def _onerror(err: OSError) -> None:
        errors.append(f"Không đọc được: {getattr(err, 'filename', err)} ({err.strerror or err})")

    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror, followlinks=False):
        if cancel is not None and cancel.is_set():
            raise SyncCancelled()

        rel_dir = Path(dirpath).relative_to(root)
        rel_dir_posix = "" if str(rel_dir) == "." else str(PurePosixPath(rel_dir))

        # Prune excluded directories in place (os.walk will not descend).
        kept_dirs = []
        for d in dirnames:
            d_rel = f"{rel_dir_posix}/{d}" if rel_dir_posix else d
            if not _matches_any(d, d_rel, exclude_patterns):
                kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in filenames:
            rel = f"{rel_dir_posix}/{fname}" if rel_dir_posix else fname
            if _matches_any(fname, rel, exclude_patterns):
                continue
            full = Path(dirpath) / fname
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError as exc:
                errors.append(f"Không đọc được: {full} ({exc.strerror or exc})")
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue  # skip symlinks/devices/sockets...
            files[rel] = LocalFile(relpath=rel, path=full, size=st.st_size, mtime=st.st_mtime)
            total_bytes += st.st_size
            if progress_cb is not None and len(files) % 500 == 0:
                progress_cb(len(files), total_bytes)

    if progress_cb is not None:
        progress_cb(len(files), total_bytes)
    return files, errors
