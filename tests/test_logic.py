#!/usr/bin/env python3
"""Plain-stdlib tests for the pure logic (no pytest, no Streamlit).

Run from the repo root:

    python tests/test_logic.py

Covers utils formatting, the local scanner (excludes + MD5), the comparison
engine (all statuses, size vs MD5), and plan building (directions, conflict
policies, mirror ordering).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make the app package importable.
_APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(_APP_DIR))

import config  # noqa: E402
from services.compare import (  # noqa: E402
    DIFFERENT,
    GOOGLE_NATIVE,
    IDENTICAL,
    LOCAL_ONLY,
    REMOTE_ONLY,
    compare_maps,
)
from services.gdrive import FOLDER_MIME, DriveClient, RemoteFile, _safe_name  # noqa: E402
from services.scanner import LocalFile, md5_of, scan_local  # noqa: E402
from services.sync import (  # noqa: E402
    CONFLICT_FORCE,
    CONFLICT_NEWER,
    CONFLICT_SKIP,
    DIR_BOTH,
    DIR_DOWN,
    DIR_UP,
    OP_DELETE_LOCAL,
    OP_DOWNLOAD,
    OP_TRASH_REMOTE,
    OP_UPDATE_LOCAL,
    OP_UPDATE_REMOTE,
    OP_UPLOAD,
    _safe_join,
    build_plan,
)
from services.compare import ComparisonItem  # noqa: E402
from utils import human_eta, human_rate, human_size, ts_to_str  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _local(rel: str, size: int = 10, mtime: float = 1000.0, path: str = "/x") -> LocalFile:
    return LocalFile(relpath=rel, path=Path(path), size=size, mtime=mtime)


def _remote(
    rel: str,
    size=10,
    mtime: float = 1000.0,
    md5: str | None = "abc",
    mime: str = "application/octet-stream",
) -> RemoteFile:
    return RemoteFile(id="id-" + rel, name=rel, relpath=rel, size=size, md5=md5, mtime=mtime, mime=mime)


def _item(rel, status, local=None, remote=None, newer=None) -> ComparisonItem:
    return ComparisonItem(relpath=rel, status=status, local=local, remote=remote, newer=newer)


# --------------------------------------------------------------------------- #
# utils
# --------------------------------------------------------------------------- #
def test_human_size():
    assert human_size(None) == ""
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1536) == "1.5 KB"
    assert human_size(1024 * 1024) == "1.0 MB"
    assert human_size(5 * 1024**3) == "5.0 GB"


def test_human_rate_and_eta():
    assert human_rate(0) == "—"
    assert human_rate(-5) == "—"
    assert human_rate(1024).endswith("/s")
    assert human_eta(None) == "—"
    assert human_eta(0) == "—"
    assert human_eta(5) == "5s"
    assert human_eta(65) == "1p 05s"
    assert human_eta(3661) == "1g 01p"


def test_ts_to_str():
    assert ts_to_str(0) == ""
    assert ts_to_str(None) == ""
    assert len(ts_to_str(1_700_000_000)) == len("2023-11-14 22:13")


# --------------------------------------------------------------------------- #
# scanner
# --------------------------------------------------------------------------- #
def test_scan_local_excludes_and_paths():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.txt").write_text("hello")
        (root / "Thumbs.db").write_text("junk")          # excluded pattern
        sub = root / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"\x00\x01\x02")
        junk_dir = root / "$RECYCLE.BIN"                  # excluded dir
        junk_dir.mkdir()
        (junk_dir / "c.txt").write_text("nope")

        files, errors = scan_local(root, config.DEFAULT_EXCLUDES)

        assert set(files.keys()) == {"a.txt", "sub/b.bin"}, files.keys()
        assert files["sub/b.bin"].size == 3
        assert files["a.txt"].relpath == "a.txt"
        assert errors == []


def test_md5_of_matches_hashlib():
    import hashlib

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.bin"
        data = os.urandom(9000)
        p.write_bytes(data)
        assert md5_of(p) == hashlib.md5(data).hexdigest()


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #
def test_compare_size_match_statuses():
    local = {
        "same.txt": _local("same.txt", size=10),
        "diff.txt": _local("diff.txt", size=10, mtime=2000.0),
        "onlyL.txt": _local("onlyL.txt", size=5),
        # A native file that also has a local counterpart -> GOOGLE_NATIVE only
        # applies when the relpath exists on BOTH sides (see compare_maps).
        "doc": _local("doc", size=42),
    }
    remote = {
        "same.txt": _remote("same.txt", size=10),
        "diff.txt": _remote("diff.txt", size=20, mtime=1000.0),
        "onlyR.txt": _remote("onlyR.txt", size=7),
        "doc": _remote("doc", size=None, md5=None, mime="application/vnd.google-apps.document"),
    }
    items, counts, byte_totals = compare_maps(local, remote, use_md5=False)
    by_rel = {it.relpath: it for it in items}

    assert by_rel["same.txt"].status == IDENTICAL
    assert by_rel["diff.txt"].status == DIFFERENT
    assert by_rel["diff.txt"].newer == "local"   # local mtime newer
    assert by_rel["onlyL.txt"].status == LOCAL_ONLY
    assert by_rel["onlyR.txt"].status == REMOTE_ONLY
    assert by_rel["doc"].status == GOOGLE_NATIVE
    assert counts[IDENTICAL] == 1 and counts[DIFFERENT] == 1
    assert byte_totals[LOCAL_ONLY] == 5


def test_compare_remote_only_native_is_skipped():
    # A Google-native file that exists only on Drive must be GOOGLE_NATIVE
    # (skipped), never REMOTE_ONLY (which would plan a doomed download / a
    # dangerous mirror-trash).
    local = {}
    remote = {
        "doc": _remote("doc", size=None, md5=None, mime="application/vnd.google-apps.document"),
        "file.bin": _remote("file.bin", size=10),
    }
    items, _counts, _bytes = compare_maps(local, remote, use_md5=False)
    by_rel = {it.relpath: it.status for it in items}
    assert by_rel["doc"] == GOOGLE_NATIVE
    assert by_rel["file.bin"] == REMOTE_ONLY


def test_safe_name_neutralizes_traversal():
    assert _safe_name("a/b") == "a_b"
    assert _safe_name("a\\b") == "a_b"
    assert _safe_name("..") == "__"
    assert _safe_name(".") == "_"
    assert _safe_name("normal.txt") == "normal.txt"
    assert _safe_name("..foo") == "..foo"  # only a bare ".." is dangerous


class _FakeExec:
    def __init__(self, value):
        self._value = value

    def execute(self, num_retries=0):
        return self._value


class _FakeFiles:
    """Service gia du de test list_tree: 1 lan get(root) + list() phan trang."""

    def __init__(self, pages, root_id):
        self._pages = pages
        self._root_id = root_id

    def get(self, fileId, fields):  # noqa: N803 — khop chu ky Google client
        return _FakeExec({"id": self._root_id})

    def list(self, q, fields, pageSize, pageToken=None):  # noqa: N803
        idx = pageToken or 0
        page = dict(self._pages[idx])
        if idx + 1 < len(self._pages):
            page["nextPageToken"] = idx + 1
        return _FakeExec(page)


class _FakeService:
    def __init__(self, pages, root_id):
        self._files = _FakeFiles(pages, root_id)

    def files(self):
        return self._files


def test_list_tree_flat_reconstruction():
    pages = [
        {
            "files": [
                {"id": "R", "name": "My Drive", "mimeType": FOLDER_MIME, "parents": []},
                {"id": "A", "name": "Photos", "mimeType": FOLDER_MIME, "parents": ["R"]},
                {"id": "f1", "name": "a.txt", "mimeType": "text/plain", "size": "10",
                 "md5Checksum": "x", "modifiedTime": "2024-01-01T00:00:00.000Z",
                 "parents": ["A"]},
            ]
        },
        {
            "files": [
                {"id": "f2", "name": "b.bin", "mimeType": "application/octet-stream",
                 "size": "20", "md5Checksum": "y",
                 "modifiedTime": "2024-01-01T00:00:00.000Z", "parents": ["R"]},
                # Muc ngoai cay root (parents lung tung) -> phai bi bo qua.
                {"id": "f3", "name": "orphan.bin", "mimeType": "application/octet-stream",
                 "size": "5", "parents": ["ZZZ"]},
            ]
        },
    ]
    client = DriveClient.__new__(DriveClient)  # bo qua __init__ (khong can creds that)
    client.service = _FakeService(pages, root_id="R")

    files, folders, warnings = client.list_tree("root")

    assert set(files.keys()) == {"Photos/a.txt", "b.bin"}, files.keys()
    assert folders[""] == "R" and folders["Photos"] == "A"
    assert files["Photos/a.txt"].size == 10 and files["b.bin"].md5 == "y"
    assert "orphan.bin" not in {f.name for f in files.values()}


def test_safe_join_blocks_escape():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Normal path stays inside.
        assert _safe_join(root, "sub/a.txt") == root / "sub" / "a.txt"
        # Traversal is rejected.
        try:
            _safe_join(root, "../../etc/passwd")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for traversal path")


def test_compare_md5_distinguishes_same_size():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        good = root / "good.bin"
        bad = root / "bad.bin"
        content = b"A" * 100
        good.write_bytes(content)
        bad.write_bytes(b"B" * 100)  # same size, different content
        import hashlib

        real_md5 = hashlib.md5(content).hexdigest()

        local = {
            "good.bin": LocalFile("good.bin", good, 100, 1000.0),
            "bad.bin": LocalFile("bad.bin", bad, 100, 1000.0),
        }
        remote = {
            "good.bin": _remote("good.bin", size=100, md5=real_md5),
            "bad.bin": _remote("bad.bin", size=100, md5=real_md5),
        }
        items, _counts, _bytes = compare_maps(local, remote, use_md5=True)
        by_rel = {it.relpath: it.status for it in items}
        assert by_rel["good.bin"] == IDENTICAL
        assert by_rel["bad.bin"] == DIFFERENT


# --------------------------------------------------------------------------- #
# build_plan
# --------------------------------------------------------------------------- #
def _sample_items():
    return [
        _item("onlyL.txt", LOCAL_ONLY, local=_local("onlyL.txt")),
        _item("onlyR.txt", REMOTE_ONLY, remote=_remote("onlyR.txt")),
        _item(
            "diff.txt",
            DIFFERENT,
            local=_local("diff.txt", mtime=2000.0),
            remote=_remote("diff.txt", mtime=1000.0),
            newer="local",
        ),
        _item("same.txt", IDENTICAL, local=_local("same.txt"), remote=_remote("same.txt")),
    ]


def test_plan_up_newer():
    actions, skipped = build_plan(_sample_items(), DIR_UP, CONFLICT_NEWER, mirror=False)
    ops = {a.relpath: a.op for a in actions}
    assert ops == {"onlyL.txt": OP_UPLOAD, "diff.txt": OP_UPDATE_REMOTE}
    assert skipped == 0


def test_plan_up_skip_conflict():
    # diff.txt newer is local; DIR_UP with newer -> uploads. Flip newer to remote.
    items = _sample_items()
    items[2] = _item(
        "diff.txt", DIFFERENT,
        local=_local("diff.txt", mtime=1000.0),
        remote=_remote("diff.txt", mtime=2000.0),
        newer="remote",
    )
    actions, skipped = build_plan(items, DIR_UP, CONFLICT_NEWER, mirror=False)
    ops = {a.op for a in actions}
    assert OP_UPDATE_REMOTE not in ops  # remote is newer, so up-newer skips it
    assert skipped == 1


def test_plan_up_force_and_mirror():
    actions, skipped = build_plan(_sample_items(), DIR_UP, CONFLICT_FORCE, mirror=True)
    ops = [(a.op, a.relpath) for a in actions]
    # onlyR should be trashed (mirror, one-way up); deletions come last.
    assert ("upload", "onlyL.txt") in ops
    assert ("update_remote", "diff.txt") in ops
    assert ops[-1] == (OP_TRASH_REMOTE, "onlyR.txt")


def test_plan_down_and_mirror():
    actions, _ = build_plan(_sample_items(), DIR_DOWN, CONFLICT_FORCE, mirror=True)
    ops = [(a.op, a.relpath) for a in actions]
    assert (OP_DOWNLOAD, "onlyR.txt") in ops
    assert (OP_UPDATE_LOCAL, "diff.txt") in ops
    assert ops[-1] == (OP_DELETE_LOCAL, "onlyL.txt")  # deletion last


def test_plan_both_newer_wins():
    items = [
        _item("l.txt", DIFFERENT, local=_local("l.txt", mtime=2000.0),
              remote=_remote("l.txt", mtime=1000.0), newer="local"),
        _item("r.txt", DIFFERENT, local=_local("r.txt", mtime=1000.0),
              remote=_remote("r.txt", mtime=2000.0), newer="remote"),
        _item("tie.txt", DIFFERENT, local=_local("tie.txt"),
              remote=_remote("tie.txt"), newer=None),
    ]
    actions, skipped = build_plan(items, DIR_BOTH, CONFLICT_NEWER, mirror=False)
    ops = {a.relpath: a.op for a in actions}
    assert ops["l.txt"] == OP_UPDATE_REMOTE
    assert ops["r.txt"] == OP_UPDATE_LOCAL
    assert "tie.txt" not in ops   # undecidable -> skipped
    assert skipped == 1


def test_plan_both_skip_policy():
    items = [
        _item("l.txt", DIFFERENT, local=_local("l.txt", mtime=2000.0),
              remote=_remote("l.txt", mtime=1000.0), newer="local"),
    ]
    actions, skipped = build_plan(items, DIR_BOTH, CONFLICT_SKIP, mirror=False)
    assert actions == [] and skipped == 1


# --------------------------------------------------------------------------- #
# cancel (nut Dung khi dang quet/so sanh)
# --------------------------------------------------------------------------- #
def test_scan_local_cancel_raises():
    import threading

    from services.common import SyncCancelled

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.txt").write_text("hello")
        cancel = threading.Event()
        cancel.set()
        try:
            scan_local(root, [], cancel=cancel)
        except SyncCancelled:
            pass
        else:
            raise AssertionError("scan_local phai raise SyncCancelled khi bi huy")


def test_compare_md5_cancel_raises():
    import hashlib
    import threading

    from services.common import SyncCancelled

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.bin"
        data = b"x" * 100
        p.write_bytes(data)
        digest = hashlib.md5(data).hexdigest()

        local = {"f.bin": LocalFile("f.bin", p, len(data), 1000.0)}
        remote = {"f.bin": _remote("f.bin", size=len(data), md5=digest)}

        cancel = threading.Event()
        cancel.set()
        try:
            compare_maps(local, remote, use_md5=True, cancel=cancel)
        except SyncCancelled:
            pass
        else:
            raise AssertionError("compare_maps phai raise SyncCancelled khi bi huy")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"❌ {fn.__name__}: {exc.__class__.__name__}: {exc}")
            import traceback

            traceback.print_exc()
        else:
            passed += 1
            print(f"✅ {fn.__name__}")
    print(f"\n{passed} passed, {failed} failed ({len(tests)} total).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
