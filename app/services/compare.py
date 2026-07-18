"""Compare the Seagate (local) file tree with the Google Drive (remote) tree.

The matching key is the relative path. Two files with the same path and the
same size are considered IDENTICAL; different sizes mean DIFFERENT (annotated
with which side is newer by mtime, feeding the "newer wins" policy).

Note on mtime: the app preserves mtime in both directions on upload/download,
so from the first sync onward the "which side is newer" comparison is
trustworthy.
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from services.common import SyncCancelled
from services.gdrive import RemoteFile, export_ext
from services.scanner import LocalFile

# Comparison statuses
IDENTICAL = "identical"
DIFFERENT = "different"
LOCAL_ONLY = "local_only"
REMOTE_ONLY = "remote_only"
GOOGLE_NATIVE = "google_native"

ALL_STATUSES = [IDENTICAL, DIFFERENT, LOCAL_ONLY, REMOTE_ONLY, GOOGLE_NATIVE]

STATUS_VI = {
    IDENTICAL: "✅ Giống nhau",
    DIFFERENT: "⚠️ Khác nhau",
    LOCAL_ONLY: "💽 Chỉ có trên Seagate",
    REMOTE_ONLY: "☁️ Chỉ có trên Drive",
    GOOGLE_NATIVE: "📄 File Google (bỏ qua)",
}

MTIME_TOLERANCE = 2.0  # seconds — absorbs mtime resolution differences between filesystems


@dataclass(frozen=True)
class ComparisonItem:
    relpath: str
    status: str
    local: Optional[LocalFile]
    remote: Optional[RemoteFile]
    # DIFFERENT: which side wins under "newer wins".
    # GOOGLE_NATIVE: "remote" = the exported local copy is outdated.
    newer: Optional[str]  # "local" | "remote" | None
    # GOOGLE_NATIVE only: the exported local copy at "<relpath><ext>"
    # (e.g. "report" -> "report.docx"), paired so it is never treated as
    # LOCAL_ONLY (which would upload it or mirror-delete it).
    export_local: Optional[LocalFile] = None


def _newer_side(local: LocalFile, remote: RemoteFile) -> Optional[str]:
    if abs(local.mtime - remote.mtime) <= MTIME_TOLERANCE:
        return None
    return "local" if local.mtime > remote.mtime else "remote"


def _export_newer(copy: Optional[LocalFile], remote: RemoteFile) -> Optional[str]:
    """'remote' when the Drive doc changed after the local exported copy was made."""
    if copy is None:
        return None
    return "remote" if remote.mtime - copy.mtime > MTIME_TOLERANCE else None


def compare_maps(
    local: dict[str, LocalFile],
    remote: dict[str, RemoteFile],
    cancel: Optional[threading.Event] = None,
) -> tuple[list[ComparisonItem], Counter, dict[str, int]]:
    """Returns (items, counts_by_status, byte_totals_by_status).

    Raises:
        SyncCancelled: the user pressed Stop mid-compare.
    """
    items: list[ComparisonItem] = []
    counts: Counter = Counter()
    byte_totals: dict[str, int] = {s: 0 for s in ALL_STATUSES}

    def _add(item: ComparisonItem) -> None:
        items.append(item)
        counts[item.status] += 1
        size = 0
        if item.local is not None:
            size = item.local.size
        elif item.remote is not None and item.remote.size is not None:
            size = item.remote.size
        byte_totals[item.status] += size

    # Google-native docs have no direct relpath counterpart, but they may have
    # an EXPORTED local copy at "<relpath><ext>" (created by the export
    # feature). Pair doc and copy up front: the copy leaves the local pool so
    # it is never LOCAL_ONLY (no upload, no mirror-delete), and the plan can
    # tell from `newer` whether it needs re-exporting.
    local = dict(local)
    exported: dict[str, LocalFile] = {}
    for rel, rf in remote.items():
        if cancel is not None and cancel.is_set():
            raise SyncCancelled()
        if not rf.is_google_native:
            continue
        ext = export_ext(rf.mime)
        if ext is None:
            continue
        export_rel = rel + ext
        if export_rel in remote:
            continue  # a real Drive file owns that path -> compare it normally
        copy = local.pop(export_rel, None)
        if copy is not None:
            exported[rel] = copy

    both = sorted(set(local) & set(remote))
    only_local = sorted(set(local) - set(remote))
    only_remote = sorted(set(remote) - set(local))

    for rel in both:
        if cancel is not None and cancel.is_set():
            raise SyncCancelled()
        lf, rf = local[rel], remote[rel]
        if rf.is_google_native:
            copy = exported.get(rel)
            _add(ComparisonItem(rel, GOOGLE_NATIVE, lf, rf, _export_newer(copy, rf), copy))
        elif rf.size is None or lf.size != rf.size:
            _add(ComparisonItem(rel, DIFFERENT, lf, rf, _newer_side(lf, rf)))
        else:
            _add(ComparisonItem(rel, IDENTICAL, lf, rf, None))

    for rel in only_local:
        _add(ComparisonItem(rel, LOCAL_ONLY, local[rel], None, None))
    for rel in only_remote:
        rf = remote[rel]
        # Google-native files (Docs/Sheets/...) can never exist on the Seagate
        # side as-is, so they are never planned for download and never
        # mirror-trashed. Mark them GOOGLE_NATIVE even when Drive-only; the
        # optional export op is the only thing that may act on them.
        if rf.is_google_native:
            copy = exported.get(rel)
            _add(ComparisonItem(rel, GOOGLE_NATIVE, None, rf, _export_newer(copy, rf), copy))
        else:
            _add(ComparisonItem(rel, REMOTE_ONLY, None, rf, None))

    items.sort(key=lambda it: it.relpath.casefold())
    return items, counts, byte_totals
