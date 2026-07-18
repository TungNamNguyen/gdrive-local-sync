"""Wrapper around the Google Drive API v3.

Contains:
- Web-style OAuth: build_web_auth_url() creates a sign-in link that redirects
  back to the app itself (?code=...); exchange_code() trades the code for a
  token. REDIRECT_URI (port 8090) only serves the host-side helper
  scripts/authorize.py (opens a browser, captures the code automatically).
- Full flat listing + in-memory tree building -> {relpath -> RemoteFile},
  plus incremental listing via the Changes API.
- Resumable uploads (mtime preserved through modifiedTime), chunked downloads
  to a .syncpart file followed by an atomic rename, exports of Google-native
  files to Office formats (files.export), and moving files to the Drive Trash.
- Transfers retry transient connection drops (e.g. IncompleteRead) that the
  Google client's own num_retries does not cover.
"""
from __future__ import annotations

import http.client
import os
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, TypeVar

# The loopback redirect uses http (not https) — valid per the OAuth spec for
# "installed apps", but oauthlib rejects http by default. These two variables
# only relax the CLIENT-side checks for the pasted URL; the token exchange
# itself still goes over HTTPS to Google's servers.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import httplib2  # noqa: E402
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

# Export formats for Google-native files: source mime -> (export mime, local
# extension). Only these types have a useful file counterpart; the remaining
# native types (Forms, Maps, ...) cannot be exported and stay skipped.
EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}


def export_ext(mime: str) -> Optional[str]:
    """Local extension a Google-native mime exports to (None = not exportable)."""
    fmt = EXPORT_FORMATS.get(mime)
    return fmt[1] if fmt else None

# Metadata fields shared by files.list and changes.list — the cache in
# drive_cache.py stores exactly these fields; changing them requires clearing
# old caches.
_ITEM_FIELDS = "id, name, mimeType, size, modifiedTime, parents"
_LIST_FIELDS = f"nextPageToken, files({_ITEM_FIELDS})"
_CHANGES_FIELDS = (
    f"nextPageToken, newStartPageToken, changes(fileId, removed, file({_ITEM_FIELDS}, trashed))"
)


@dataclass(frozen=True)
class RemoteFile:
    id: str
    name: str
    relpath: str
    size: Optional[int]      # None for Google-native files (Docs/Sheets/...)
    mtime: float
    mime: str

    @property
    def is_google_native(self) -> bool:
        return self.mime.startswith(GOOGLE_NATIVE_PREFIX)


# --------------------------------------------------------------------------- #
# Time conversion
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
    """Escape a string value inside a Drive API query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _safe_name(name: str) -> str:
    """Neutralize a Drive name so it is safe as ONE local path component.

    Drive allows arbitrary names (including "/", "\\", ".", ".."). Left as-is,
    a file named ".." could make the download path escape the root directory
    (path traversal). Replace separators with "_" and defuse "."/"..".
    """
    name = name.replace("/", "_").replace("\\", "_").replace("\x00", "_")
    if name in (".", ".."):
        name = name.replace(".", "_")  # "." -> "_", ".." -> "__"
    return name


# --------------------------------------------------------------------------- #
# Network retry
# --------------------------------------------------------------------------- #
# Transient failures the Google client does NOT retry by itself: its
# num_retries only re-sends on HTTP 5xx/429 and a few socket errors, while
# e.g. http.client.IncompleteRead (connection dropped mid-chunk) bubbles up
# and would fail the whole transfer.
_TRANSIENT_NET_ERRORS = (
    http.client.HTTPException,  # IncompleteRead, RemoteDisconnected, ...
    ConnectionError,
    TimeoutError,
    ssl.SSLError,
    httplib2.HttpLib2Error,
)

_T = TypeVar("_T")


def _with_net_retry(
    fn: Callable[[], _T],
    cancel: Optional[threading.Event] = None,
    attempts: int = 5,
    base_delay: float = 1.0,
) -> _T:
    """Run fn() again after a transient network error (exponential backoff).

    Safe for chunked transfers: both MediaIoBaseDownload and resumable uploads
    only advance their internal offset after a chunk fully succeeds, so a
    retried call re-requests the same byte range.
    """
    for attempt in range(attempts + 1):
        try:
            return fn()
        except _TRANSIENT_NET_ERRORS:
            if attempt >= attempts:
                raise
            delay = min(base_delay * (2 ** attempt), 16.0)
            if cancel is not None:
                if cancel.wait(delay):
                    raise SyncCancelled()
            elif delay > 0:
                time.sleep(delay)
    raise AssertionError("unreachable")  # for the type checker


def build_tree(
    raw: dict[str, dict],
    root_id: str,
) -> tuple[dict[str, RemoteFile], dict[str, str], list[str]]:
    """Build the {relpath -> ...} tree from the flat {id -> metadata} map — pure in-memory.

    A pure function (no API calls) so incremental scans can reuse it: the cache
    keeps the flat map, each scan just patches the map and rebuilds the tree
    from any root.

    Returns:
        files:    {relpath -> RemoteFile}
        folders:  {relpath -> folder_id} (includes "" -> root_id)
        warnings: skipped shortcuts, duplicate names...
    """
    children: dict[str, list[dict]] = {}
    for f in raw.values():
        for pid in f.get("parents", []):
            children.setdefault(pid, []).append(f)

    files: dict[str, RemoteFile] = {}
    folders: dict[str, str] = {"": root_id}
    warnings: list[str] = []
    visited: set[str] = set()
    queue: list[tuple[str, str]] = [("", root_id)]
    while queue:
        rel_dir, folder_id = queue.pop(0)
        if folder_id in visited:  # cycle guard (corrupted parents)
            continue
        visited.add(folder_id)
        for f in children.get(folder_id, []):
            # Drive names may contain characters that are invalid as local
            # paths (e.g. "/", "..") — neutralize before joining the relpath.
            name = _safe_name(f["name"])
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
                    mtime=_rfc3339_to_ts(f.get("modifiedTime", "")),
                    mime=mime,
                )
    return files, folders, warnings


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
def save_credentials(creds: Credentials) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass  # e.g. a bind mount from Windows may not support chmod


def delete_credentials() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


def load_saved_credentials() -> Optional[Credentials]:
    """Read token.json; auto-refresh when expired. None if not signed in."""
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


# --------------------------------------------------------------------------- #
# Web-style OAuth ("Sign in with Google" inside the app)
# --------------------------------------------------------------------------- #
# The app uses its own URL as the redirect. After the user grants access, the
# browser returns to the app with ?code=... and the app exchanges it for a
# token. Because the page fully RELOADS on redirect (Streamlit loses its
# session_state), we use neither PKCE nor stored state: the flow is rebuilt
# from credentials.json and only `code` is needed for the exchange.
def build_web_auth_url(redirect_uri: str) -> str:
    """Create the Google sign-in URL (redirecting back to the app itself)."""
    flow = Flow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
        autogenerate_code_verifier=False,  # no PKCE so the exchange needs no original flow
    )
    auth_url, _state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    return auth_url


def exchange_code(code: str, redirect_uri: str) -> Credentials:
    """Exchange the authorization code (from the query param) for a token and save it."""
    flow = Flow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
        autogenerate_code_verifier=False,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    save_credentials(creds)
    return creds


# --------------------------------------------------------------------------- #
# Drive client
# --------------------------------------------------------------------------- #
class DriveClient:
    """Every thread MUST create its own DriveClient (httplib2 is not thread-safe)."""

    def __init__(self, creds: Credentials):
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    # ---- Account info ----
    def user_email(self) -> str:
        about = (
            self.service.about().get(fields="user(emailAddress,displayName)").execute(num_retries=3)
        )
        user = about.get("user", {})
        return user.get("emailAddress") or user.get("displayName") or "(không rõ)"

    # ---- Folders ----
    def resolve_folder_path(self, path: str, create: bool = False) -> str:
        """'root' or 'A/B/C' -> folder id. create=True: create missing levels."""
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
        """Ensure the folder chain `relpath` (POSIX, '' = root) exists; return its id.

        `folders` is a {relpath -> id} cache (seeded from the scan result) and
        is updated in place as new folders are created.
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

    # ---- Listing ----
    def real_root_id(self) -> str:
        """Resolve the "root" alias to the real My Drive id (needed to match parents)."""
        return (
            self.service.files().get(fileId="root", fields="id").execute(num_retries=3)["id"]
        )

    def get_start_page_token(self) -> str:
        """Anchor for changes.list — fetched BEFORE the flat sweep so no change is missed."""
        resp = (
            self.service.changes()
            .getStartPageToken(fields="startPageToken")
            .execute(num_retries=3)
        )
        return resp["startPageToken"]

    def fetch_all_items(
        self,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> dict[str, dict]:
        """Flat sweep: fetch every non-trashed item, 1000 per page -> {id -> metadata}.

        API-call count = total items / 1000 (instead of one call per folder as
        in the old BFS — tens of times faster when Drive has many folders).
        """
        raw: dict[str, dict] = {}
        n_files = n_folders = 0
        page_token: Optional[str] = None
        while True:
            if cancel is not None and cancel.is_set():
                raise SyncCancelled()
            resp = (
                self.service.files()
                .list(
                    q="trashed = false",
                    fields=_LIST_FIELDS,
                    pageSize=1000,
                    pageToken=page_token,
                )
                .execute(num_retries=3)
            )
            for f in resp.get("files", []):
                raw[f["id"]] = f
                if f.get("mimeType") == FOLDER_MIME:
                    n_folders += 1
                else:
                    n_files += 1
            if progress_cb is not None:
                progress_cb(n_files, n_folders)  # rough: includes items outside the root, good enough for progress
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return raw

    def fetch_changes(
        self,
        page_token: str,
        cancel: Optional[threading.Event] = None,
    ) -> tuple[dict[str, dict], set[str], str]:
        """Ask Drive "what changed since this token?" (incremental scan).

        Returns:
            (upserts, removed_ids, new_token) — upserts holds new/updated
            metadata; removed_ids covers both hard-deleted and trashed files.

        Raises:
            googleapiclient HttpError when the token is expired/invalid — the
            caller must catch it and fall back to a full flat sweep.
        """
        upserts: dict[str, dict] = {}
        removed: set[str] = set()
        token = page_token
        while True:
            if cancel is not None and cancel.is_set():
                raise SyncCancelled()
            resp = (
                self.service.changes()
                .list(pageToken=token, pageSize=1000, fields=_CHANGES_FIELDS)
                .execute(num_retries=3)
            )
            for ch in resp.get("changes", []):
                fid = ch.get("fileId")
                f = ch.get("file")
                if ch.get("removed") or f is None or f.get("trashed"):
                    removed.add(fid)
                    upserts.pop(fid, None)
                else:
                    f.pop("trashed", None)
                    upserts[fid] = f
                    removed.discard(fid)
            new_token = resp.get("newStartPageToken")
            if new_token:
                return upserts, removed, new_token
            token = resp["nextPageToken"]

    def list_tree(
        self,
        root_id: str,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> tuple[dict[str, RemoteFile], dict[str, str], list[str]]:
        """Full flat sweep, then build the tree under root_id (see build_tree)."""
        real_root = self.real_root_id() if root_id == "root" else root_id
        raw = self.fetch_all_items(progress_cb=progress_cb, cancel=cancel)
        files, folders, warnings = build_tree(raw, real_root)
        if progress_cb is not None:
            progress_cb(len(files), len(folders))  # exact numbers after filtering by root
        return files, folders, warnings

    # ---- Transfers ----
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
        """Upload (create or overwrite). Preserves the local mtime via modifiedTime."""
        size = local_path.stat().st_size
        modified = _ts_to_rfc3339(mtime)

        media = MediaFileUpload(
            str(local_path),
            mimetype="application/octet-stream",
            chunksize=UPLOAD_CHUNK,
            resumable=size > 0,  # resumable upload rejects 0-byte files
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
                status, response = _with_net_retry(
                    lambda: request.next_chunk(num_retries=5), cancel
                )
                if status is not None and progress_cb is not None:
                    progress_cb(status.resumable_progress)
        else:
            response = _with_net_retry(lambda: request.execute(num_retries=5), cancel)
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
        """Download to a .syncpart file first, rename when done — never leaves a half file."""
        request = self.service.files().get_media(fileId=file_id)
        self._run_media_download(request, dest, mtime, progress_cb, cancel)

    def export_file(
        self,
        file_id: str,
        mime: str,
        dest: Path,
        mtime: float,
        progress_cb: Optional[Callable[[int], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> None:
        """Export a Google-native file (Docs/Sheets/...) to its Office/PNG copy.

        One-way only: the exported bytes are generated by Drive on the fly
        (size unknown upfront, max ~10 MB per Drive's export limit). mtime is
        set to the doc's modifiedTime so compare can tell when the local copy
        is outdated.
        """
        export_mime = EXPORT_FORMATS[mime][0]
        request = self.service.files().export_media(fileId=file_id, mimeType=export_mime)
        self._run_media_download(request, dest, mtime, progress_cb, cancel)

    def _run_media_download(
        self,
        request,
        dest: Path,
        mtime: float,
        progress_cb: Optional[Callable[[int], None]],
        cancel: Optional[threading.Event],
    ) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(dest) + ".syncpart")
        try:
            with open(tmp, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=DOWNLOAD_CHUNK)
                done = False
                while not done:
                    if cancel is not None and cancel.is_set():
                        raise SyncCancelled()
                    status, done = _with_net_retry(
                        lambda: downloader.next_chunk(num_retries=5), cancel
                    )
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
        """Move to the Drive Trash (recoverable) — NEVER a permanent delete."""
        self.service.files().update(fileId=file_id, body={"trashed": True}).execute(num_retries=3)
