"""So sanh cay file Seagate (local) voi cay file Google Drive (remote).

Khoa doi chieu la duong dan tuong doi. Mac dinh hai file cung duong dan va
cung kich thuoc duoc coi la GIONG NHAU (nhanh). Bat "so khop MD5" de kiem tra
noi dung chinh xac tuyet doi (cham hon vi phai doc lai file tren o Seagate).

Luu y ve mtime: khi app upload/download, mtime duoc giu nguyen hai phia, nen
tu lan dong bo dau tro di, so sanh "ben nao moi hon" la dang tin cay.
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional

from services.gdrive import RemoteFile
from services.scanner import LocalFile, md5_of

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
    use_md5: bool = False,
    hash_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> tuple[list[ComparisonItem], Counter, dict[str, int]]:
    """Returns (items, dem_theo_trang_thai, tong_byte_theo_trang_thai).

    hash_cb(bytes_da_hash, tong_byte_can_hash, file_hien_tai) — chi goi khi
    use_md5=True va co file can kiem tra checksum.
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

    # Vong 1: xu ly moi truong hop khong can hash.
    md5_candidates: list[str] = []
    for rel in both:
        lf, rf = local[rel], remote[rel]
        if rf.is_google_native:
            _add(ComparisonItem(rel, GOOGLE_NATIVE, lf, rf, None))
        elif rf.size is None or lf.size != rf.size:
            _add(ComparisonItem(rel, DIFFERENT, lf, rf, _newer_side(lf, rf)))
        elif use_md5 and rf.md5:
            md5_candidates.append(rel)  # cung size — can hash de chac chan
        else:
            _add(ComparisonItem(rel, IDENTICAL, lf, rf, None))

    # Vong 2: hash MD5 cac ung vien (co bao cao tien do).
    if md5_candidates:
        total_hash_bytes = sum(local[r].size for r in md5_candidates)
        hashed = 0
        for rel in md5_candidates:
            if cancel is not None and cancel.is_set():
                break
            lf, rf = local[rel], remote[rel]

            def _chunk(n: int) -> None:
                nonlocal hashed
                hashed += n
                if hash_cb is not None:
                    hash_cb(hashed, total_hash_bytes, rel)

            try:
                digest = md5_of(lf.path, cancel=cancel, chunk_cb=_chunk)
            except OSError:
                _add(ComparisonItem(rel, DIFFERENT, lf, rf, _newer_side(lf, rf)))
                continue
            if digest == rf.md5:
                _add(ComparisonItem(rel, IDENTICAL, lf, rf, None))
            else:
                _add(ComparisonItem(rel, DIFFERENT, lf, rf, _newer_side(lf, rf)))

    for rel in only_local:
        _add(ComparisonItem(rel, LOCAL_ONLY, local[rel], None, None))
    for rel in only_remote:
        _add(ComparisonItem(rel, REMOTE_ONLY, None, remote[rel], None))

    items.sort(key=lambda it: it.relpath.casefold())
    return items, counts, byte_totals
