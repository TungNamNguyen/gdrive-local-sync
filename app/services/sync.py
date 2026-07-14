"""Engine dong bo.

- build_plan(): tu ket qua so sanh + huong + chinh sach xung dot -> danh sach
  Action cu the (dry-run chinh la viec hien thi ke hoach nay truoc khi chay).
- ProgressState: trang thai tien do thread-safe cho giao dien poll.
- SyncRunner: thread nen thuc thi ke hoach; KHONG duoc goi bat ky ham
  Streamlit nao trong thread nay.

Quy tac an toan du lieu (giu nguyen khi sua doi):
- Xoa phia Drive  -> chi chuyen vao Thung rac Drive (khoi phuc duoc).
- Xoa phia Seagate -> chi di chuyen vao <o>/.sync_trash/<timestamp>/...
- Download ghi ra file *.syncpart roi moi rename de (atomic).
"""
from __future__ import annotations

import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from config import LOCAL_TRASH_DIRNAME
from services import history
from services.common import SyncCancelled
from services.compare import (
    DIFFERENT,
    LOCAL_ONLY,
    REMOTE_ONLY,
    ComparisonItem,
)
from services.gdrive import DriveClient, RemoteFile
from services.scanner import LocalFile
from utils import human_size

# Huong dong bo
DIR_UP = "up"      # Seagate -> Drive
DIR_DOWN = "down"  # Drive -> Seagate
DIR_BOTH = "both"  # hai chieu (moi hon thang)

DIRECTION_VI = {
    DIR_UP: "Seagate → Drive",
    DIR_DOWN: "Drive → Seagate",
    DIR_BOTH: "Hai chiều",
}

# Chinh sach cho file "khac nhau"
CONFLICT_NEWER = "newer"  # chi ghi de neu phia nguon moi hon
CONFLICT_FORCE = "force"  # luon ghi de theo huong da chon
CONFLICT_SKIP = "skip"    # bo qua

# Cac loai thao tac
OP_UPLOAD = "upload"                # tao moi tren Drive
OP_UPDATE_REMOTE = "update_remote"  # ghi de noi dung file Drive co san
OP_DOWNLOAD = "download"            # tao moi tren Seagate
OP_UPDATE_LOCAL = "update_local"    # ghi de file Seagate co san
OP_TRASH_REMOTE = "trash_remote"    # mirror: chuyen file Drive vao Thung rac
OP_DELETE_LOCAL = "delete_local"    # mirror: chuyen file Seagate vao .sync_trash

OP_VI = {
    OP_UPLOAD: "⬆️ Tải lên (mới)",
    OP_UPDATE_REMOTE: "⬆️ Ghi đè trên Drive",
    OP_DOWNLOAD: "⬇️ Tải xuống (mới)",
    OP_UPDATE_LOCAL: "⬇️ Ghi đè trên Seagate",
    OP_TRASH_REMOTE: "🗑️ Vào Thùng rác Drive",
    OP_DELETE_LOCAL: "🗑️ Vào .sync_trash (Seagate)",
}

_TRANSFER_OPS = {OP_UPLOAD, OP_UPDATE_REMOTE, OP_DOWNLOAD, OP_UPDATE_LOCAL}


@dataclass(frozen=True)
class Action:
    op: str
    relpath: str
    size: int  # byte se truyen tai (0 voi thao tac xoa)
    local: Optional[LocalFile]
    remote: Optional[RemoteFile]


def build_plan(
    items: list[ComparisonItem],
    direction: str,
    conflict: str,
    mirror: bool,
) -> tuple[list[Action], int]:
    """Returns (actions, so_xung_dot_bi_bo_qua). Thu tu: truyen tai truoc, xoa sau."""
    transfers: list[Action] = []
    deletions: list[Action] = []
    skipped_conflicts = 0

    for it in items:
        if it.status == LOCAL_ONLY:
            assert it.local is not None
            if direction in (DIR_UP, DIR_BOTH):
                transfers.append(Action(OP_UPLOAD, it.relpath, it.local.size, it.local, None))
            elif direction == DIR_DOWN and mirror:
                deletions.append(Action(OP_DELETE_LOCAL, it.relpath, 0, it.local, None))

        elif it.status == REMOTE_ONLY:
            assert it.remote is not None
            if direction in (DIR_DOWN, DIR_BOTH):
                transfers.append(
                    Action(OP_DOWNLOAD, it.relpath, it.remote.size or 0, None, it.remote)
                )
            elif direction == DIR_UP and mirror:
                deletions.append(Action(OP_TRASH_REMOTE, it.relpath, 0, None, it.remote))

        elif it.status == DIFFERENT:
            assert it.local is not None and it.remote is not None
            if direction == DIR_UP:
                if conflict == CONFLICT_FORCE or (conflict == CONFLICT_NEWER and it.newer == "local"):
                    transfers.append(
                        Action(OP_UPDATE_REMOTE, it.relpath, it.local.size, it.local, it.remote)
                    )
                else:
                    skipped_conflicts += 1
            elif direction == DIR_DOWN:
                if conflict == CONFLICT_FORCE or (conflict == CONFLICT_NEWER and it.newer == "remote"):
                    transfers.append(
                        Action(OP_UPDATE_LOCAL, it.relpath, it.remote.size or 0, it.local, it.remote)
                    )
                else:
                    skipped_conflicts += 1
            else:  # DIR_BOTH: "moi hon thang"; khong xac dinh duoc -> bo qua
                if conflict == CONFLICT_SKIP or it.newer is None:
                    skipped_conflicts += 1
                elif it.newer == "local":
                    transfers.append(
                        Action(OP_UPDATE_REMOTE, it.relpath, it.local.size, it.local, it.remote)
                    )
                else:
                    transfers.append(
                        Action(OP_UPDATE_LOCAL, it.relpath, it.remote.size or 0, it.local, it.remote)
                    )
        # IDENTICAL & GOOGLE_NATIVE: khong lam gi.

    transfers.sort(key=lambda a: a.relpath.casefold())
    deletions.sort(key=lambda a: a.relpath.casefold())
    return transfers + deletions, skipped_conflicts


class ProgressState:
    """Trang thai tien do dung chung giua thread dong bo va giao dien."""

    def __init__(self, total_files: int, total_bytes: int, direction: str, mode: str):
        self._lock = threading.Lock()
        self.cancel = threading.Event()
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.direction = direction
        self.mode = mode
        self._base_bytes = 0        # byte cua cac file DA xong
        self._current_bytes = 0     # byte da truyen cua file dang xu ly
        self.done_files = 0
        self.failed_files = 0
        self.current = ""
        self.current_size = 0
        self.errors: list[str] = []
        self._log: deque[str] = deque(maxlen=500)
        self._samples: deque[tuple[float, int]] = deque(maxlen=60)
        self.started_at = time.time()
        self.finished = False
        self.fatal_error: Optional[str] = None
        self.session_id: Optional[int] = None

    # ---- ghi (goi tu thread dong bo) ----
    def log(self, message: str) -> None:
        with self._lock:
            self._log.append(f"[{time.strftime('%H:%M:%S')}] {message}")

    def begin_file(self, action: Action) -> None:
        with self._lock:
            self.current = action.relpath
            self.current_size = action.size
            self._current_bytes = 0
            self._log.append(
                f"[{time.strftime('%H:%M:%S')}] {OP_VI[action.op]} — {action.relpath}"
                + (f" ({human_size(action.size)})" if action.size else "")
            )

    def set_current_bytes(self, transferred: int) -> None:
        with self._lock:
            if self.current_size:
                transferred = min(transferred, self.current_size)
            self._current_bytes = transferred
            self._samples.append((time.time(), self._base_bytes + self._current_bytes))

    def finish_file(self, ok: bool, error: Optional[str] = None) -> None:
        with self._lock:
            self._base_bytes += self.current_size
            self._current_bytes = 0
            self._samples.append((time.time(), self._base_bytes))
            if ok:
                self.done_files += 1
            else:
                self.failed_files += 1
                if error:
                    if len(self.errors) < 200:
                        self.errors.append(error)
                    self._log.append(f"[{time.strftime('%H:%M:%S')}] ❌ {error}")
            self.current = ""
            self.current_size = 0

    def fatal(self, message: str) -> None:
        with self._lock:
            self.fatal_error = message
            self._log.append(f"[{time.strftime('%H:%M:%S')}] ❌ LỖI NGHIÊM TRỌNG: {message}")

    def mark_finished(self) -> None:
        with self._lock:
            self.finished = True
            self._log.append(f"[{time.strftime('%H:%M:%S')}] 🏁 Kết thúc phiên đồng bộ.")

    # ---- doc (goi tu giao dien) ----
    def snapshot(self) -> dict:
        with self._lock:
            done_bytes = self._base_bytes + self._current_bytes
            speed = 0.0
            now = time.time()
            recent = [(t, b) for (t, b) in self._samples if now - t <= 8.0]
            if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
                speed = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
            remaining = max(self.total_bytes - done_bytes, 0)
            eta = remaining / speed if speed > 0 else None
            current_frac = (
                self._current_bytes / self.current_size if self.current_size else None
            )
            return {
                "total_files": self.total_files,
                "total_bytes": self.total_bytes,
                "done_files": self.done_files,
                "failed_files": self.failed_files,
                "done_bytes": done_bytes,
                "current": self.current,
                "current_frac": current_frac,
                "speed": speed,
                "eta": eta,
                "elapsed": now - self.started_at,
                "finished": self.finished,
                "cancel_requested": self.cancel.is_set(),
                "fatal": self.fatal_error,
                "errors": list(self.errors),
                "log": list(self._log)[-200:],
            }


class SyncRunner(threading.Thread):
    """Thread thuc thi ke hoach dong bo. Giao dien poll qua `progress`."""

    def __init__(
        self,
        creds,
        seagate_root: Path,
        drive_root_path: str,
        actions: list[Action],
        remote_folders: Optional[dict[str, str]],
        progress: ProgressState,
    ):
        super().__init__(daemon=True, name="sync-runner")
        self.creds = creds
        self.seagate_root = Path(seagate_root)
        self.drive_root_path = drive_root_path
        self.actions = actions
        self.remote_folders = dict(remote_folders or {})
        self.progress = progress

    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: C901 — luong dieu phoi tuyen tinh, de doc
        p = self.progress
        session_id: Optional[int] = None
        status = "error"
        try:
            p.log(f"Bắt đầu — hướng: {DIRECTION_VI.get(p.direction, p.direction)}, "
                  f"{p.total_files:,} thao tác, {human_size(p.total_bytes)}.")
            client = DriveClient(self.creds)  # client rieng cho thread nay

            needs_remote_write = any(
                a.op in (OP_UPLOAD, OP_UPDATE_REMOTE) for a in self.actions
            )
            root_id = self.remote_folders.get("")
            if root_id is None:
                root_id = client.resolve_folder_path(
                    self.drive_root_path, create=needs_remote_write
                )
            folders = self.remote_folders
            folders[""] = root_id

            session_id = history.start_session(
                p.direction, p.mode, p.total_files, p.total_bytes
            )
            p.session_id = session_id

            trash_stamp = time.strftime("%Y%m%d-%H%M%S")
            for action in self.actions:
                if p.cancel.is_set():
                    p.log("⛔ Đã hủy theo yêu cầu người dùng.")
                    break
                p.begin_file(action)
                try:
                    self._execute(client, action, folders, trash_stamp)
                    p.finish_file(ok=True)
                except SyncCancelled:
                    p.finish_file(ok=False, error=f"{action.relpath}: đã hủy giữa chừng")
                    p.log("⛔ Đã hủy theo yêu cầu người dùng.")
                    break
                except Exception as exc:  # noqa: BLE001 — 1 file loi khong dung ca phien
                    p.finish_file(ok=False, error=f"{action.relpath}: {exc}")

            if p.cancel.is_set():
                status = "cancelled"
            elif p.failed_files:
                status = "done_with_errors"
            else:
                status = "success"
        except Exception as exc:  # noqa: BLE001 — loi truoc/ngoai vong lap file
            p.fatal(str(exc))
            status = "error"
        finally:
            snap = p.snapshot()
            if session_id is not None:
                try:
                    history.finish_session(
                        session_id,
                        done_files=snap["done_files"],
                        failed_files=snap["failed_files"],
                        done_bytes=snap["done_bytes"],
                        status=status,
                        errors=snap["errors"],
                    )
                except Exception:  # noqa: BLE001 — khong de loi ghi lich su che loi chinh
                    pass
            p.mark_finished()

    # ------------------------------------------------------------------ #
    def _execute(
        self,
        client: DriveClient,
        action: Action,
        folders: dict[str, str],
        trash_stamp: str,
    ) -> None:
        p = self.progress

        if action.op in (OP_UPLOAD, OP_UPDATE_REMOTE):
            assert action.local is not None
            parent_rel = str(PurePosixPath(action.relpath).parent)
            parent_rel = "" if parent_rel == "." else parent_rel
            parent_id = client.ensure_folder_path(parent_rel, folders)
            client.upload_file(
                local_path=action.local.path,
                name=PurePosixPath(action.relpath).name,
                parent_id=parent_id,
                mtime=action.local.mtime,
                existing_id=action.remote.id if action.remote else None,
                progress_cb=p.set_current_bytes,
                cancel=p.cancel,
            )

        elif action.op in (OP_DOWNLOAD, OP_UPDATE_LOCAL):
            assert action.remote is not None
            dest = self.seagate_root.joinpath(*PurePosixPath(action.relpath).parts)
            client.download_file(
                file_id=action.remote.id,
                dest=dest,
                mtime=action.remote.mtime,
                progress_cb=p.set_current_bytes,
                cancel=p.cancel,
            )

        elif action.op == OP_TRASH_REMOTE:
            assert action.remote is not None
            client.trash_file(action.remote.id)

        elif action.op == OP_DELETE_LOCAL:
            assert action.local is not None
            trash_dest = self.seagate_root / LOCAL_TRASH_DIRNAME / trash_stamp
            dest = trash_dest.joinpath(*PurePosixPath(action.relpath).parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(action.local.path), str(dest))

        else:  # phong ve — khong bao gio xay ra neu build_plan dung
            raise ValueError(f"Thao tác không hỗ trợ: {action.op}")
