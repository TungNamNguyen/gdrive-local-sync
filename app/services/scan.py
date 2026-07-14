"""Quet & so sanh chay o thread nen (de nguoi dung co the bam Dung).

Cung mo hinh voi sync.SyncRunner: mot doi tuong trang thai thread-safe
(`ScanState`) + mot thread (`ScanRunner`). Giao dien chi doc qua snapshot()
va bam nut Dung -> set `cancel` Event.

Vi sao phai chay o thread nen: neu quet ngay trong lan chay script cua
Streamlit, script bi chan cho toi khi quet xong nen KHONG nut nao bam duoc.

KHONG duoc goi bat ky ham Streamlit nao trong file nay.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from services.common import SyncCancelled
from services.compare import ComparisonItem, compare_maps
from services.gdrive import DriveClient
from services.scanner import scan_local

# Cac pha cua mot lan quet
PHASE_LOCAL = "local"      # dang doc o Seagate
PHASE_DRIVE = "drive"      # dang liet ke cay tren Drive
PHASE_COMPARE = "compare"  # dang doi chieu (co the dang hash MD5)

PHASE_VI = {
    PHASE_LOCAL: "Đang quét ổ Seagate…",
    PHASE_DRIVE: "Đang quét Google Drive…",
    PHASE_COMPARE: "Đang so sánh…",
}


class ScanState:
    """Tien do quet/so sanh, dung chung giua thread quet va giao dien."""

    def __init__(self, use_md5: bool):
        self._lock = threading.Lock()
        self.cancel = threading.Event()
        self.use_md5 = use_md5
        self.started_at = time.time()

        self._phase = PHASE_LOCAL
        self._local_files = 0
        self._local_bytes = 0
        self._drive_files = 0
        self._drive_folders = 0
        self._hash_done = 0
        self._hash_total = 0
        self._hash_current = ""

        self._finished = False
        self._cancelled = False
        self._error: Optional[str] = None
        self._result: Optional[dict] = None

    # ---- ghi (goi tu thread quet) ----
    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def on_local(self, nfiles: int, nbytes: int) -> None:
        with self._lock:
            self._local_files = nfiles
            self._local_bytes = nbytes

    def on_drive(self, nfiles: int, nfolders: int) -> None:
        with self._lock:
            self._drive_files = nfiles
            self._drive_folders = nfolders

    def on_hash(self, done: int, total: int, name: str) -> None:
        with self._lock:
            self._hash_done = done
            self._hash_total = total
            self._hash_current = name

    def set_result(self, result: dict) -> None:
        with self._lock:
            self._result = result

    def set_error(self, message: str) -> None:
        with self._lock:
            self._error = message

    def mark_cancelled(self) -> None:
        with self._lock:
            self._cancelled = True

    def mark_finished(self) -> None:
        with self._lock:
            self._finished = True

    # ---- doc (goi tu giao dien) ----
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self._phase,
                "local_files": self._local_files,
                "local_bytes": self._local_bytes,
                "drive_files": self._drive_files,
                "drive_folders": self._drive_folders,
                "hash_done": self._hash_done,
                "hash_total": self._hash_total,
                "hash_current": self._hash_current,
                "use_md5": self.use_md5,
                "elapsed": time.time() - self.started_at,
                "finished": self._finished,
                "cancelled": self._cancelled,
                "cancel_requested": self.cancel.is_set(),
                "error": self._error,
                "result": self._result,
            }


class ScanRunner(threading.Thread):
    """Thread quet hai phia roi so sanh. Giao dien poll qua `state`."""

    def __init__(
        self,
        creds,
        seagate_root: Path,
        exclude_patterns: list[str],
        drive_root_path: str,
        use_md5: bool,
        state: ScanState,
    ):
        super().__init__(daemon=True, name="scan-runner")
        self.creds = creds
        self.seagate_root = Path(seagate_root)
        self.exclude_patterns = list(exclude_patterns)
        self.drive_root_path = drive_root_path
        self.use_md5 = use_md5
        self.state = state

    def run(self) -> None:
        s = self.state
        try:
            # 1) Seagate.
            s.set_phase(PHASE_LOCAL)
            local, local_errors = scan_local(
                self.seagate_root,
                self.exclude_patterns,
                progress_cb=s.on_local,
                cancel=s.cancel,
            )

            # 2) Google Drive — client rieng cho thread nay (httplib2 khong
            # thread-safe), giong SyncRunner.
            s.set_phase(PHASE_DRIVE)
            client = DriveClient(self.creds)
            try:
                root_id = client.resolve_folder_path(self.drive_root_path, create=False)
            except FileNotFoundError:
                remote, folders, warnings = {}, {}, [
                    f"Thư mục '{self.drive_root_path}' chưa tồn tại trên Drive — "
                    "sẽ được tạo khi tải lên."
                ]
            else:
                remote, folders, warnings = client.list_tree(
                    root_id, progress_cb=s.on_drive, cancel=s.cancel
                )

            # 3) So sanh (pha MD5 la pha lau nhat, cancel duoc giua chung).
            s.set_phase(PHASE_COMPARE)
            items, counts, byte_totals = compare_maps(
                local,
                remote,
                use_md5=self.use_md5,
                hash_cb=s.on_hash if self.use_md5 else None,
                cancel=s.cancel,
            )
            s.set_result(
                _result(local, local_errors, remote, folders, warnings,
                        items, counts, byte_totals, self.use_md5)
            )
        except SyncCancelled:
            s.mark_cancelled()
        except Exception as exc:  # noqa: BLE001 — bao loi len UI thay vi crash
            s.set_error(str(exc))
        finally:
            s.mark_finished()


def _result(
    local, local_errors, remote, folders, warnings,
    items: list[ComparisonItem], counts: Counter, byte_totals: dict[str, int],
    use_md5: bool,
) -> dict:
    """Ket qua mot lan quet — giao dien do thang vao st.session_state."""
    return {
        "local_files": local,
        "local_errors": local_errors,
        "remote_files": remote,
        "remote_folders": folders,
        "remote_warnings": warnings,
        "cmp_items": items,
        "cmp_counts": counts,
        "cmp_bytes": byte_totals,
        "cmp_used_md5": use_md5,
    }
