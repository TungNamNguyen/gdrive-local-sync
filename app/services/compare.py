"""So sanh cay file Seagate (local) voi cay file Google Drive (remote).

Khoa doi chieu la duong dan tuong doi. Hai file cung duong dan va cung kich
thuoc duoc coi la GIONG NHAU; khac kich thuoc la KHAC NHAU (kem ben nao moi
hon theo mtime, phuc vu chinh sach "moi hon thang").

Luu y ve mtime: khi app upload/download, mtime duoc giu nguyen hai phia, nen
tu lan dong bo dau tro di, so sanh "ben nao moi hon" la dang tin cay.
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from services.common import SyncCancelled
from services.gdrive import RemoteFile
from services.scanner import LocalFile

# Trang thai so sanh
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

MTIME_TOLERANCE = 2.0  # giay — bu tru do phan giai mtime giua cac he file


@dataclass(frozen=True)
class ComparisonItem:
    relpath: str
    status: str
    local: Optional[LocalFile]
    remote: Optional[RemoteFile]
    newer: Optional[str]  # "local" | "remote" | None (chi co nghia khi DIFFERENT)


def _newer_side(local: LocalFile, remote: RemoteFile) -> Optional[str]:
    if abs(local.mtime - remote.mtime) <= MTIME_TOLERANCE:
        return None
    return "local" if local.mtime > remote.mtime else "remote"


def compare_maps(
    local: dict[str, LocalFile],
    remote: dict[str, RemoteFile],
    cancel: Optional[threading.Event] = None,
) -> tuple[list[ComparisonItem], Counter, dict[str, int]]:
    """Returns (items, dem_theo_trang_thai, tong_byte_theo_trang_thai).

    Raises:
        SyncCancelled: nguoi dung bam Dung giua chung.
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

    both = sorted(set(local) & set(remote))
    only_local = sorted(set(local) - set(remote))
    only_remote = sorted(set(remote) - set(local))

    for rel in both:
        if cancel is not None and cancel.is_set():
            raise SyncCancelled()
        lf, rf = local[rel], remote[rel]
        if rf.is_google_native:
            _add(ComparisonItem(rel, GOOGLE_NATIVE, lf, rf, None))
        elif rf.size is None or lf.size != rf.size:
            _add(ComparisonItem(rel, DIFFERENT, lf, rf, _newer_side(lf, rf)))
        else:
            _add(ComparisonItem(rel, IDENTICAL, lf, rf, None))

    for rel in only_local:
        _add(ComparisonItem(rel, LOCAL_ONLY, local[rel], None, None))
    for rel in only_remote:
        rf = remote[rel]
        # Google-native files (Docs/Sheets/...) can never exist on the Seagate
        # side, so they must always be skipped — never planned for download and
        # never mirror-trashed. Mark them GOOGLE_NATIVE even when Drive-only.
        status = GOOGLE_NATIVE if rf.is_google_native else REMOTE_ONLY
        _add(ComparisonItem(rel, status, None, rf, None))

    items.sort(key=lambda it: it.relpath.casefold())
    return items, counts, byte_totals
