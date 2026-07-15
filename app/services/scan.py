"""Scan & compare running on a background thread (so the user can press Stop).

Same model as sync.SyncRunner: a thread-safe state object (`ScanState`) plus a
thread (`ScanRunner`). The UI only reads via snapshot(); pressing Stop sets the
`cancel` Event.

The Drive side scans INCREMENTALLY when possible: the first scan does a full
flat sweep and saves {id -> metadata} + a changes-token to drive_cache; later
scans only call changes.list (1-2 API calls) and rebuild the tree from the
cache. A broken/expired cache or a different account falls back to a full scan.

Why a background thread: if scanning ran inline in the Streamlit script run,
the run would block until the scan finished and NO button could be clicked.

NO Streamlit calls are allowed anywhere in this file.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from services import drive_cache
from services.common import SyncCancelled
from services.compare import compare_maps
from services.gdrive import DriveClient, build_tree
from services.scanner import scan_local

# Phases of a scan
PHASE_LOCAL = "local"      # reading the Seagate drive
PHASE_DRIVE = "drive"      # listing the Drive tree
PHASE_COMPARE = "compare"  # comparing

PHASE_VI = {
    PHASE_LOCAL: "Đang quét ổ Seagate…",
    PHASE_DRIVE: "Đang quét Google Drive…",
    PHASE_COMPARE: "Đang so sánh…",
}

# Drive scan modes
DRIVE_FULL = "full"                # full flat sweep
DRIVE_INCREMENTAL = "incremental"  # only ask for changes since last scan (changes.list)


class ScanState:
    """Scan/compare progress, shared between the scan thread and the UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self.cancel = threading.Event()
        self.started_at = time.time()

        self._phase = PHASE_LOCAL
        self._drive_mode = DRIVE_FULL
        self._local_files = 0
        self._local_bytes = 0
        self._drive_files = 0
        self._drive_folders = 0

        self._finished = False
        self._cancelled = False
        self._error: Optional[str] = None
        self._result: Optional[dict] = None

    # ---- writes (called from the scan thread) ----
    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def set_drive_mode(self, mode: str) -> None:
        with self._lock:
            self._drive_mode = mode

    def on_local(self, nfiles: int, nbytes: int) -> None:
        with self._lock:
            self._local_files = nfiles
            self._local_bytes = nbytes

    def on_drive(self, nfiles: int, nfolders: int) -> None:
        with self._lock:
            self._drive_files = nfiles
            self._drive_folders = nfolders

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

    # ---- reads (called from the UI) ----
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self._phase,
                "drive_mode": self._drive_mode,
                "local_files": self._local_files,
                "local_bytes": self._local_bytes,
                "drive_files": self._drive_files,
                "drive_folders": self._drive_folders,
                "elapsed": time.time() - self.started_at,
                "finished": self._finished,
                "cancelled": self._cancelled,
                "cancel_requested": self.cancel.is_set(),
                "error": self._error,
                "result": self._result,
            }


class ScanRunner(threading.Thread):
    """Thread that scans both sides and compares. The UI polls via `state`."""

    def __init__(
        self,
        creds,
        seagate_root: Path,
        exclude_patterns: list[str],
        drive_root_path: str,
        state: ScanState,
        account: Optional[str] = None,
        force_full: bool = False,
    ):
        super().__init__(daemon=True, name="scan-runner")
        self.creds = creds
        self.seagate_root = Path(seagate_root)
        self.exclude_patterns = list(exclude_patterns)
        self.drive_root_path = drive_root_path
        self.state = state
        self.account = account or ""
        self.force_full = force_full

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

            # 2) Google Drive — own client for this thread (httplib2 is not
            # thread-safe), same as SyncRunner.
            s.set_phase(PHASE_DRIVE)
            client = DriveClient(self.creds)
            remote, folders, warnings = self._scan_drive(client)

            # 3) Compare.
            s.set_phase(PHASE_COMPARE)
            items, counts, byte_totals = compare_maps(local, remote, cancel=s.cancel)
            s.set_result(
                {
                    "local_files": local,
                    "local_errors": local_errors,
                    "remote_files": remote,
                    "remote_folders": folders,
                    "remote_warnings": warnings,
                    "cmp_items": items,
                    "cmp_counts": counts,
                    "cmp_bytes": byte_totals,
                }
            )
        except SyncCancelled:
            s.mark_cancelled()
        except Exception as exc:  # noqa: BLE001 — surface the error in the UI instead of crashing
            s.set_error(str(exc))
        finally:
            s.mark_finished()

    # ------------------------------------------------------------------ #
    def _scan_drive(
        self, client: DriveClient
    ) -> tuple[dict, dict, list[str]]:
        """Get the flat map (incrementally when cached) and build the tree under the root."""
        s = self.state

        raw: Optional[dict[str, dict]] = None
        token: Optional[str] = None

        cached = None if self.force_full else drive_cache.load(self.account)
        if cached is not None:
            items, old_token = cached
            try:
                upserts, removed, token = client.fetch_changes(old_token, cancel=s.cancel)
            except SyncCancelled:
                raise
            except Exception:  # noqa: BLE001 — token expired/broken -> full scan
                raw = None
            else:
                drive_cache.apply_changes(items, upserts, removed)
                raw = items
                s.set_drive_mode(DRIVE_INCREMENTAL)

        if raw is None:
            s.set_drive_mode(DRIVE_FULL)
            # Fetch the token BEFORE sweeping: changes that happen during the
            # sweep get replayed next time (duplicate upserts are harmless,
            # missing a change is not).
            token = client.get_start_page_token()
            raw = client.fetch_all_items(progress_cb=s.on_drive, cancel=s.cancel)

        # Root folder: "" / "root" -> real id; a subfolder path -> API lookup.
        try:
            root_id = client.resolve_folder_path(self.drive_root_path, create=False)
        except FileNotFoundError:
            drive_cache.save(self.account, token, raw)  # cache stays valid for next time
            return {}, {}, [
                f"Thư mục '{self.drive_root_path}' chưa tồn tại trên Drive — "
                "sẽ được tạo khi tải lên."
            ]
        if root_id == "root":
            root_id = client.real_root_id()

        files, folders, warnings = build_tree(raw, root_id)
        s.on_drive(len(files), len(folders))
        drive_cache.save(self.account, token, raw)
        return files, folders, warnings
