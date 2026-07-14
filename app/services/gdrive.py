"""Lop bao Google Drive API v3.

Bao gom:
- Luong OAuth "dan URL": tao link dang nhap voi redirect ve
  http://localhost:8090/ (loopback). Chay trong Docker khong co trinh duyet,
  nen sau khi nguoi dung cho phep, trinh duyet cua HO se bao "khong ket noi
  duoc localhost:8090" — do la binh thuong; ho copy URL tren thanh dia chi
  (chua ?code=...&state=...) va dan lai vao app de doi lay token.
- Liet ke toan bo cay thu muc (BFS) -> {relpath -> RemoteFile}.
- Upload resumable (giu nguyen mtime qua truong modifiedTime), download theo
  chunk ve file .syncpart roi rename atomic, chuyen file vao Thung rac Drive.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

# Redirect loopback dung http (khong phai https) — hop le theo chuan OAuth
# cho "installed app", nhung oauthlib mac dinh chan http. Hai bien nay chi
# noi long kiem tra PHIA CLIENT cho URL dan vao; token van trao doi qua HTTPS
# toi may chu Google.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from google_auth_oauthlib.flow import Flow  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload  # noqa: E402

from config import (  # noqa: E402
    CREDENTIALS_FILE,
    DOWNLOAD_CHUNK,
    SCOPES,
    TOKEN_FILE,
    UPLOAD_CHUNK,
)
from services.common import SyncCancelled  # noqa: E402

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps"
REDIRECT_URI = "http://localhost:8090/"
_LIST_FIELDS = "nextPageToken, files(id, name, mimeType, size, md5Checksum, modifiedTime)"


@dataclass(frozen=True)
class RemoteFile:
    id: str
    name: str
    relpath: str
    size: Optional[int]      # None voi file Google native (Docs/Sheets/...)
    md5: Optional[str]
    mtime: float
    mime: str

    @property
    def is_google_native(self) -> bool:
        return self.mime.startswith(GOOGLE_NATIVE_PREFIX)


# --------------------------------------------------------------------------- #
# Chuyen doi thoi gian
# --------------------------------------------------------------------------- #
def _rfc3339_to_ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _ts_to_rfc3339(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _escape_q(value: str) -> str:
    """Escape gia tri chuoi trong query cua Drive API."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
def save_credentials(creds: Credentials) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass  # vi du: bind mount tu Windows khong ho tro chmod


def delete_credentials() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


def load_saved_credentials() -> Optional[Credentials]:
    """Doc token.json; tu dong refresh neu het han. None neu chua dang nhap."""
    if not TOKEN_FILE.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    except (ValueError, KeyError):
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            return None
        save_credentials(creds)
        return creds
    return None


def create_auth_flow() -> tuple[Flow, str]:
    """Tao (flow, auth_url). Flow phai duoc giu lai de goi finish_auth_flow."""
    flow = Flow.from_client_secrets_file(
        str(CREDENTIALS_FILE), scopes=SCOPES, redirect_uri=REDIRECT_URI
    )
    auth_url, _state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    return flow, auth_url


def finish_auth_flow(flow: Flow, pasted: str) -> Credentials:
    """Hoan tat dang nhap tu URL redirect (hoac ma code tho) nguoi dung dan vao."""
    pasted = pasted.strip()
    if not pasted:
        raise ValueError("Bạn chưa dán URL/mã xác thực.")
    if pasted.lower().startswith("http"):
        flow.fetch_token(authorization_response=pasted)
    else:
        flow.fetch_token(code=pasted)
    creds = flow.credentials
    save_credentials(creds)
    return creds


# --------------------------------------------------------------------------- #
# Drive client
# --------------------------------------------------------------------------- #
class DriveClient:
    """Moi thread PHAI tu tao DriveClient rieng (httplib2 khong thread-safe)."""

    def __init__(self, creds: Credentials):
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    # ---- Thong tin tai khoan ----
    def user_email(self) -> str:
        about = (
            self.service.about().get(fields="user(emailAddress,displayName)").execute(num_retries=3)
        )
        user = about.get("user", {})
        return user.get("emailAddress") or user.get("displayName") or "(không rõ)"

    # ---- Thu muc ----
    def resolve_folder_path(self, path: str, create: bool = False) -> str:
        """'root' hoac 'A/B/C' -> folder id. create=True: tao cac cap con thieu."""
        path = (path or "").strip().strip("/")
        if path in ("", "root", "My Drive", "MyDrive"):
            return "root"
        parent = "root"
        for part in path.split("/"):
            query = (
                f"name = '{_escape_q(part)}' and '{parent}' in parents "
                f"and mimeType = '{FOLDER_MIME}' and trashed = false"
            )
            resp = (
                self.service.files()
                .list(q=query, fields="files(id)", pageSize=1)
                .execute(num_retries=3)
            )
            found = resp.get("files", [])
            if found:
                parent = found[0]["id"]
            elif create:
                parent = self._create_folder(part, parent)
            else:
                raise FileNotFoundError(path)
        return parent

    def _create_folder(self, name: str, parent_id: str) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        created = self.service.files().create(body=body, fields="id").execute(num_retries=3)
        return created["id"]

    def ensure_folder_path(self, relpath: str, folders: dict[str, str]) -> str:
        """Bao dam chuoi thu muc `relpath` (POSIX, '' = goc) ton tai; tra ve id.

        `folders` la cache {relpath -> id} (bat dau tu ket qua list_tree) va
        duoc cap nhat tai cho khi tao thu muc moi.
        """
        if relpath in folders:
            return folders[relpath]
        parts = [p for p in PurePosixPath(relpath).parts]
        current = ""
        parent_id = folders[""]
        for part in parts:
            current = f"{current}/{part}" if current else part
            if current in folders:
                parent_id = folders[current]
                continue
            parent_id = self._create_folder(part, parent_id)
            folders[current] = parent_id
        return parent_id

    # ---- Liet ke ----
    def list_tree(
        self,
        root_id: str,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> tuple[dict[str, RemoteFile], dict[str, str], list[str]]:
        """BFS toan bo cay duoi root_id.

        Returns:
            files:    {relpath -> RemoteFile}
            folders:  {relpath -> folder_id} (bao gom "" -> root_id)
            warnings: shortcut bi bo qua, ten trung lap...
        """
        files: dict[str, RemoteFile] = {}
        folders: dict[str, str] = {"": root_id}
        warnings: list[str] = []
        queue: list[tuple[str, str]] = [("", root_id)]

        while queue:
            if cancel is not None and cancel.is_set():
                raise SyncCancelled()
            rel_dir, folder_id = queue.pop(0)
            page_token: Optional[str] = None
            while True:
                resp = (
                    self.service.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields=_LIST_FIELDS,
                        pageSize=1000,
                        pageToken=page_token,
                    )
                    .execute(num_retries=3)
                )
                for f in resp.get("files", []):
                    # Ten tren Drive co the chua "/", khong hop le lam duong dan.
                    name = f["name"].replace("/", "_")
                    rel = f"{rel_dir}/{name}" if rel_dir else name
                    mime = f.get("mimeType", "")
                    if mime == FOLDER_MIME:
                        if rel in folders:
                            warnings.append(f"Trùng tên thư mục trên Drive, chỉ dùng bản đầu: {rel}")
                            continue
                        folders[rel] = f["id"]
                        queue.append((rel, f["id"]))
                    elif mime == SHORTCUT_MIME:
                        warnings.append(f"Bỏ qua shortcut: {rel}")
                    else:
                        if rel in files:
                            warnings.append(f"Trùng tên file trên Drive, chỉ dùng bản đầu: {rel}")
                            continue
                        raw_size = f.get("size")
                        files[rel] = RemoteFile(
                            id=f["id"],
                            name=f["name"],
                            relpath=rel,
                            size=int(raw_size) if raw_size is not None else None,
                            md5=f.get("md5Checksum"),
                            mtime=_rfc3339_to_ts(f.get("modifiedTime", "")),
                            mime=mime,
                        )
                if progress_cb is not None:
                    progress_cb(len(files), len(folders))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return files, folders, warnings

    # ---- Truyen tai ----
    def upload_file(
        self,
        local_path: Path,
        name: str,
        parent_id: str,
        mtime: float,
        existing_id: Optional[str] = None,
        progress_cb: Optional[Callable[[int], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> str:
        """Upload (tao moi hoac ghi de). Giu mtime cuc bo qua modifiedTime."""
        size = local_path.stat().st_size
        modified = _ts_to_rfc3339(mtime)

        media = MediaFileUpload(
            str(local_path),
            mimetype="application/octet-stream",
            chunksize=UPLOAD_CHUNK,
            resumable=size > 0,  # resumable upload khong nhan file 0 byte
        )
        if existing_id:
            request = self.service.files().update(
                fileId=existing_id, body={"modifiedTime": modified}, media_body=media, fields="id"
            )
        else:
            body = {"name": name, "parents": [parent_id], "modifiedTime": modified}
            request = self.service.files().create(body=body, media_body=media, fields="id")

        if size > 0:
            response = None
            while response is None:
                if cancel is not None and cancel.is_set():
                    raise SyncCancelled()
                status, response = request.next_chunk(num_retries=5)
                if status is not None and progress_cb is not None:
                    progress_cb(status.resumable_progress)
        else:
            response = request.execute(num_retries=5)
        if progress_cb is not None:
            progress_cb(size)
        return response["id"]

    def download_file(
        self,
        file_id: str,
        dest: Path,
        mtime: float,
        progress_cb: Optional[Callable[[int], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> None:
        """Tai ve file .syncpart truoc, xong moi rename de — khong bao gio de lai file nua vời."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(dest) + ".syncpart")
        request = self.service.files().get_media(fileId=file_id)
        try:
            with open(tmp, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=DOWNLOAD_CHUNK)
                done = False
                while not done:
                    if cancel is not None and cancel.is_set():
                        raise SyncCancelled()
                    status, done = downloader.next_chunk(num_retries=5)
                    if status is not None and progress_cb is not None:
                        progress_cb(status.resumable_progress)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, dest)
        try:
            os.utime(dest, (mtime, mtime))
        except OSError:
            pass

    def trash_file(self, file_id: str) -> None:
        """Chuyen vao Thung rac Drive (khoi phuc duoc) — KHONG xoa vinh vien."""
        self.service.files().update(fileId=file_id, body={"trashed": True}).execute(num_retries=3)
