"""Sync engine.

- build_plan(): comparison result + direction + conflict policy -> a list of
  concrete Actions (showing this plan before running is the dry-run).
- ProgressState: thread-safe progress state polled by the UI.
- SyncRunner: background thread executing the plan; NO Streamlit calls are
  allowed anywhere in this thread.

Data-safety rules (keep intact when modifying):
- Drive-side delete   -> move to the Drive Trash only (recoverable).
- Seagate-side delete -> move into <drive>/.sync_trash/<timestamp>/... only.
- Downloads write to a *.syncpart file first, then rename (atomic).
"""
from __future__ import annotations

import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from config import LOCAL_TRASH_DIRNAME, SYNC_WORKERS
from services import history
from services.common import SyncCancelled
from services.compare import (
    DIFFERENT,
    GOOGLE_NATIVE,
    LOCAL_ONLY,
    REMOTE_ONLY,
    ComparisonItem,
)
from services.gdrive import DriveClient, RemoteFile, export_ext
from services.scanner import LocalFile
from utils import human_size

# Sync directions
DIR_UP = "up"      # Seagate -> Drive
DIR_DOWN = "down"  # Drive -> Seagate
DIR_BOTH = "both"  # two-way (newer wins)

DIRECTION_VI = {
    DIR_UP: "Seagate → Drive",
    DIR_DOWN: "Drive → Seagate",
    DIR_BOTH: "Hai chiều",
}

# Policies for "different" files
CONFLICT_NEWER = "newer"  # only overwrite when the source side is newer
CONFLICT_FORCE = "force"  # always overwrite in the chosen direction
CONFLICT_SKIP = "skip"    # skip

# Operation kinds
OP_UPLOAD = "upload"                # create new on Drive
OP_UPDATE_REMOTE = "update_remote"  # overwrite an existing Drive file
OP_DOWNLOAD = "download"            # create new on the Seagate drive
OP_UPDATE_LOCAL = "update_local"    # overwrite an existing Seagate file
OP_EXPORT_LOCAL = "export_local"    # export a Google-native doc to an Office copy
OP_TRASH_REMOTE = "trash_remote"    # mirror: move a Drive file to the Trash
OP_DELETE_LOCAL = "delete_local"    # mirror: move a Seagate file into .sync_trash

OP_VI = {
    OP_UPLOAD: "⬆️ Tải lên (mới)",
    OP_UPDATE_REMOTE: "⬆️ Ghi đè trên Drive",
    OP_DOWNLOAD: "⬇️ Tải xuống (mới)",
    OP_UPDATE_LOCAL: "⬇️ Ghi đè trên Seagate",
    OP_EXPORT_LOCAL: "📄 Xuất file Google",
    OP_TRASH_REMOTE: "🗑️ Vào Thùng rác Drive",
    OP_DELETE_LOCAL: "🗑️ Vào .sync_trash (Seagate)",
}

_TRANSFER_OPS = {OP_UPLOAD, OP_UPDATE_REMOTE, OP_DOWNLOAD, OP_UPDATE_LOCAL, OP_EXPORT_LOCAL}


@dataclass(frozen=True)
class Action:
    op: str
    relpath: str
    size: int  # bytes to transfer (0 for deletions)
    local: Optional[LocalFile]
    remote: Optional[RemoteFile]


def _safe_join(root: Path, relpath: str) -> Path:
    """Join relpath onto root, GUARANTEEING the result stays inside root.

    Defense in depth: even though names are neutralized at the scan/Drive
    layer, re-check before overwriting/creating any local file.
    """
    dest = root.joinpath(*PurePosixPath(relpath).parts)
    root_resolved = root.resolve()
    try:
        dest.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Đường dẫn thoát khỏi thư mục gốc: {relpath}") from exc
    return dest


def build_plan(
    items: list[ComparisonItem],
    direction: str,
    conflict: str,
    mirror: bool,
    export_native: bool = False,
) -> tuple[list[Action], int]:
    """Returns (actions, skipped_conflict_count). Order: transfers first, deletions last."""
    transfers: list[Action] = []
    deletions: list[Action] = []
    skipped_conflicts = 0
    # Relpaths owned by real files on either side — an export copy must never
    # land on one of these (e.g. Drive holds both the doc "report" AND a real
    # file "report.docx").
    taken = {it.relpath for it in items}

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
            else:  # DIR_BOTH: "newer wins"; undeterminable -> skip
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

        elif it.status == GOOGLE_NATIVE:
            # Optional one-way export of Docs/Sheets/... to an Office copy on
            # the Seagate side. Never uploads, never touches the Drive doc.
            if not export_native or direction not in (DIR_DOWN, DIR_BOTH):
                continue
            assert it.remote is not None
            ext = export_ext(it.remote.mime)
            if ext is None:
                continue  # Forms/Maps/... have no file counterpart
            export_rel = it.relpath + ext
            if export_rel in taken:
                continue
            if it.export_local is None or it.newer == "remote":
                transfers.append(
                    Action(OP_EXPORT_LOCAL, export_rel, it.remote.size or 0,
                           it.export_local, it.remote)
                )
        # IDENTICAL: nothing to do.

    transfers.sort(key=lambda a: a.relpath.casefold())
    deletions.sort(key=lambda a: a.relpath.casefold())
    return transfers + deletions, skipped_conflicts


class ProgressState:
    """Progress state shared between the sync workers and the UI.

    Several files can be transferring AT ONCE (SyncRunner runs in parallel),
    so per-file progress lives in `_active` {relpath -> [size, transferred]}.
    """

    def __init__(self, total_files: int, total_bytes: int, direction: str, mode: str):
        self._lock = threading.Lock()
        self.cancel = threading.Event()
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.direction = direction
        self.mode = mode
        self._base_bytes = 0        # bytes of files that are DONE
        self._active: dict[str, list[int]] = {}  # relpath -> [size, transferred]
        self.done_files = 0
        self.failed_files = 0
        self.errors: list[str] = []
        self._log: deque[str] = deque(maxlen=500)
        self._samples: deque[tuple[float, int]] = deque(maxlen=60)
        self.started_at = time.time()
        self.finished = False
        self.fatal_error: Optional[str] = None
        self.session_id: Optional[int] = None

    def _inflight_bytes(self) -> int:
        return sum(t for _s, t in self._active.values())

    # ---- writes (called from the sync workers) ----
    def log(self, message: str) -> None:
        with self._lock:
            self._log.append(f"[{time.strftime('%H:%M:%S')}] {message}")

    def begin_file(self, action: Action) -> None:
        with self._lock:
            self._active[action.relpath] = [action.size, 0]
            self._log.append(
                f"[{time.strftime('%H:%M:%S')}] {OP_VI[action.op]} — {action.relpath}"
                + (f" ({human_size(action.size)})" if action.size else "")
            )

    def set_current_bytes(self, relpath: str, transferred: int) -> None:
        with self._lock:
            entry = self._active.get(relpath)
            if entry is None:
                return
            if entry[0]:
                transferred = min(transferred, entry[0])
            entry[1] = transferred
            self._samples.append((time.time(), self._base_bytes + self._inflight_bytes()))

    def finish_file(self, relpath: str, ok: bool, error: Optional[str] = None) -> None:
        with self._lock:
            size, _t = self._active.pop(relpath, (0, 0))
            self._base_bytes += size
            self._samples.append((time.time(), self._base_bytes + self._inflight_bytes()))
            if ok:
                self.done_files += 1
            else:
                self.failed_files += 1
                if error:
                    if len(self.errors) < 200:
                        self.errors.append(error)
                    self._log.append(f"[{time.strftime('%H:%M:%S')}] ❌ {error}")

    def fatal(self, message: str) -> None:
        with self._lock:
            self.fatal_error = message
            self._log.append(f"[{time.strftime('%H:%M:%S')}] ❌ LỖI NGHIÊM TRỌNG: {message}")

    def mark_finished(self) -> None:
        with self._lock:
            self.finished = True
            self._log.append(f"[{time.strftime('%H:%M:%S')}] 🏁 Kết thúc phiên đồng bộ.")

    # ---- reads (called from the UI) ----
    def snapshot(self) -> dict:
        with self._lock:
            done_bytes = self._base_bytes + self._inflight_bytes()
            speed = 0.0
            now = time.time()
            recent = [(t, b) for (t, b) in self._samples if now - t <= 8.0]
            if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
                speed = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
            remaining = max(self.total_bytes - done_bytes, 0)
            eta = remaining / speed if speed > 0 else None
            active = [
                (rel, (t / s) if s else None)
                for rel, (s, t) in sorted(self._active.items())
            ]
            return {
                "total_files": self.total_files,
                "total_bytes": self.total_bytes,
                "done_files": self.done_files,
                "failed_files": self.failed_files,
                "done_bytes": done_bytes,
                "active": active,
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
    """Thread orchestrating the sync plan. The UI polls via `progress`.

    TRANSFER operations run in parallel, `workers` files at a time (a clear
    win with many small files — time is dominated by per-request latency).
    DELETE operations (mirror) only run AFTER every transfer has finished
    (preserving build_plan's safety ordering). Each worker gets its own
    DriveClient (httplib2 is not thread-safe).
    """

    def __init__(
        self,
        creds,
        seagate_root: Path,
        drive_root_path: str,
        actions: list[Action],
        remote_folders: Optional[dict[str, str]],
        progress: ProgressState,
        workers: Optional[int] = None,
        client_factory: Optional[Callable[[], DriveClient]] = None,
    ):
        super().__init__(daemon=True, name="sync-runner")
        self.creds = creds
        self.seagate_root = Path(seagate_root)
        self.drive_root_path = drive_root_path
        self.actions = actions
        self.remote_folders = dict(remote_folders or {})
        self.progress = progress
        self.workers = max(1, workers if workers is not None else SYNC_WORKERS)
        self._make_client = client_factory or (lambda: DriveClient(self.creds))
        # ensure_folder_path reads/writes the shared `folders` cache and may
        # CREATE folders — it must be serialized, otherwise two workers could
        # create the same folder twice (Drive allows duplicate names -> the
        # tree gets duplicated).
        self._folders_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        p = self.progress
        session_id: Optional[int] = None
        status = "error"
        try:
            p.log(f"Bắt đầu — hướng: {DIRECTION_VI.get(p.direction, p.direction)}, "
                  f"{p.total_files:,} thao tác, {human_size(p.total_bytes)}, "
                  f"{self.workers} luồng song song.")
            client = self._make_client()  # client for the orchestrating thread

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
            transfers = [a for a in self.actions if a.op in _TRANSFER_OPS]
            deletions = [a for a in self.actions if a.op not in _TRANSFER_OPS]
            self._run_batch(transfers, folders, trash_stamp)
            if not p.cancel.is_set():
                self._run_batch(deletions, folders, trash_stamp)
            if p.cancel.is_set():
                p.log("⛔ Đã hủy theo yêu cầu người dùng.")

            if p.cancel.is_set():
                status = "cancelled"
            elif p.failed_files:
                status = "done_with_errors"
            else:
                status = "success"
        except Exception as exc:  # noqa: BLE001 — errors before/outside the per-file loop
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
                except Exception:  # noqa: BLE001 — a history write error must not mask the real one
                    pass
            p.mark_finished()

    # ------------------------------------------------------------------ #
    def _run_batch(
        self,
        actions: list[Action],
        folders: dict[str, str],
        trash_stamp: str,
    ) -> None:
        """Run one batch of actions in parallel; returns when EVERY action is done.

        Workers check cancel before each action: after Cancel, actions not yet
        started are skipped and in-flight ones stop via the cancel Event.
        """
        if not actions:
            return
        p = self.progress
        tls = threading.local()  # one DriveClient per worker thread

        def _worker(action: Action) -> None:
            if p.cancel.is_set():
                return
            client = getattr(tls, "client", None)
            if client is None:
                client = tls.client = self._make_client()
            p.begin_file(action)
            try:
                self._execute(client, action, folders, trash_stamp)
                p.finish_file(action.relpath, ok=True)
            except SyncCancelled:
                p.finish_file(action.relpath, ok=False,
                              error=f"{action.relpath}: đã hủy giữa chừng")
            except Exception as exc:  # noqa: BLE001 — one failed file must not stop the session
                p.finish_file(action.relpath, ok=False, error=f"{action.relpath}: {exc}")

        with ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="sync-worker"
        ) as pool:
            list(pool.map(_worker, actions))

    # ------------------------------------------------------------------ #
    def _execute(
        self,
        client: DriveClient,
        action: Action,
        folders: dict[str, str],
        trash_stamp: str,
    ) -> None:
        p = self.progress

        def _on_bytes(n: int, rel: str = action.relpath) -> None:
            p.set_current_bytes(rel, n)

        if action.op in (OP_UPLOAD, OP_UPDATE_REMOTE):
            assert action.local is not None
            parent_rel = str(PurePosixPath(action.relpath).parent)
            parent_rel = "" if parent_rel == "." else parent_rel
            with self._folders_lock:
                parent_id = client.ensure_folder_path(parent_rel, folders)
            client.upload_file(
                local_path=action.local.path,
                name=PurePosixPath(action.relpath).name,
                parent_id=parent_id,
                mtime=action.local.mtime,
                existing_id=action.remote.id if action.remote else None,
                progress_cb=_on_bytes,
                cancel=p.cancel,
            )

        elif action.op in (OP_DOWNLOAD, OP_UPDATE_LOCAL):
            assert action.remote is not None
            dest = _safe_join(self.seagate_root, action.relpath)
            client.download_file(
                file_id=action.remote.id,
                dest=dest,
                mtime=action.remote.mtime,
                progress_cb=_on_bytes,
                cancel=p.cancel,
            )

        elif action.op == OP_EXPORT_LOCAL:
            assert action.remote is not None
            dest = _safe_join(self.seagate_root, action.relpath)
            client.export_file(
                file_id=action.remote.id,
                mime=action.remote.mime,
                dest=dest,
                mtime=action.remote.mtime,
                progress_cb=_on_bytes,
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

        else:  # defensive — unreachable if build_plan is correct
            raise ValueError(f"Thao tác không hỗ trợ: {action.op}")
