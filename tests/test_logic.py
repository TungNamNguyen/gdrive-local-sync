#!/usr/bin/env python3
"""Plain-stdlib tests for the pure logic (no pytest, no Streamlit).

Run from the repo root:

    python tests/test_logic.py

Covers utils formatting, the local scanner (excludes), the comparison engine
(all statuses), plan building (directions, conflict policies, mirror
ordering), the flat/incremental Drive listing, and the parallel sync runner.
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
from services import drive_cache  # noqa: E402
from services.gdrive import (  # noqa: E402
    FOLDER_MIME,
    DriveClient,
    RemoteFile,
    _safe_name,
    build_tree,
)
from services.scanner import LocalFile, scan_local  # noqa: E402
from services.sync import (  # noqa: E402
    CONFLICT_FORCE,
    CONFLICT_NEWER,
    CONFLICT_SKIP,
    DIR_BOTH,
    DIR_DOWN,
    DIR_UP,
    OP_DELETE_LOCAL,
    OP_DOWNLOAD,
    OP_EXPORT_LOCAL,
    OP_TRASH_REMOTE,
    OP_UPDATE_LOCAL,
    OP_UPDATE_REMOTE,
    OP_UPLOAD,
    Action,
    ProgressState,
    SyncRunner,
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
    mime: str = "application/octet-stream",
) -> RemoteFile:
    return RemoteFile(id="id-" + rel, name=rel, relpath=rel, size=size, mtime=mtime, mime=mime)


def _item(rel, status, local=None, remote=None, newer=None, export_local=None) -> ComparisonItem:
    return ComparisonItem(relpath=rel, status=status, local=local, remote=remote,
                          newer=newer, export_local=export_local)


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
        "doc": _remote("doc", size=None, mime="application/vnd.google-apps.document"),
    }
    items, counts, byte_totals = compare_maps(local, remote)
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
        "doc": _remote("doc", size=None, mime="application/vnd.google-apps.document"),
        "file.bin": _remote("file.bin", size=10),
    }
    items, _counts, _bytes = compare_maps(local, remote)
    by_rel = {it.relpath: it.status for it in items}
    assert by_rel["doc"] == GOOGLE_NATIVE
    assert by_rel["file.bin"] == REMOTE_ONLY


def test_compare_pairs_native_export_copy():
    # A Google doc "report" with an exported local copy "report.docx": the
    # copy is paired to the doc (not LOCAL_ONLY -> never uploaded or
    # mirror-deleted) and `newer` tells whether it needs re-exporting.
    remote = {
        "report": _remote("report", size=None, mtime=1000.0,
                          mime="application/vnd.google-apps.document"),
    }
    local = {"report.docx": _local("report.docx", size=50, mtime=1000.0)}
    items, counts, _bytes = compare_maps(local, remote)
    assert len(items) == 1, items
    it = items[0]
    assert it.status == GOOGLE_NATIVE
    assert it.export_local is not None and it.export_local.relpath == "report.docx"
    assert it.newer is None  # same mtime -> copy is fresh
    assert counts[LOCAL_ONLY] == 0

    # Doc edited later on Drive -> the copy is outdated.
    remote_newer = {
        "report": _remote("report", size=None, mtime=5000.0,
                          mime="application/vnd.google-apps.document"),
    }
    items2, _c, _b = compare_maps(local, remote_newer)
    assert items2[0].newer == "remote"


def test_compare_export_copy_yields_to_real_remote_file():
    # Drive holds BOTH the doc "report" and a real file "report.docx": the
    # local file must compare against the real file, not pair with the doc.
    remote = {
        "report": _remote("report", size=None,
                          mime="application/vnd.google-apps.document"),
        "report.docx": _remote("report.docx", size=50),
    }
    local = {"report.docx": _local("report.docx", size=50)}
    items, _c, _b = compare_maps(local, remote)
    by_rel = {it.relpath: it for it in items}
    assert by_rel["report.docx"].status == IDENTICAL
    assert by_rel["report"].status == GOOGLE_NATIVE
    assert by_rel["report"].export_local is None


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
    """Fake service for list_tree tests: one get(root) + paginated list()."""

    def __init__(self, pages, root_id):
        self._pages = pages
        self._root_id = root_id

    def get(self, fileId, fields):  # noqa: N803 — matches the Google client signature
        return _FakeExec({"id": self._root_id})

    def list(self, q, fields, pageSize, pageToken=None):  # noqa: N803
        idx = pageToken or 0
        page = dict(self._pages[idx])
        if idx + 1 < len(self._pages):
            page["nextPageToken"] = idx + 1
        return _FakeExec(page)


class _FakeChanges:
    """Fake changes.list: returns all changes in one page + newStartPageToken."""

    def __init__(self, changes, new_token):
        self._changes = changes
        self._new_token = new_token

    def list(self, pageToken, pageSize, fields):  # noqa: N803
        return _FakeExec(
            {"changes": self._changes, "newStartPageToken": self._new_token}
        )


class _FakeService:
    def __init__(self, pages, root_id, changes=None, new_token="tok2"):
        self._files = _FakeFiles(pages, root_id)
        self._changes = _FakeChanges(changes or [], new_token)

    def files(self):
        return self._files

    def changes(self):
        return self._changes


def test_list_tree_flat_reconstruction():
    pages = [
        {
            "files": [
                {"id": "R", "name": "My Drive", "mimeType": FOLDER_MIME, "parents": []},
                {"id": "A", "name": "Photos", "mimeType": FOLDER_MIME, "parents": ["R"]},
                {"id": "f1", "name": "a.txt", "mimeType": "text/plain", "size": "10",
                 "modifiedTime": "2024-01-01T00:00:00.000Z", "parents": ["A"]},
            ]
        },
        {
            "files": [
                {"id": "f2", "name": "b.bin", "mimeType": "application/octet-stream",
                 "size": "20", "modifiedTime": "2024-01-01T00:00:00.000Z",
                 "parents": ["R"]},
                # Item outside the root tree (bogus parents) -> must be skipped.
                {"id": "f3", "name": "orphan.bin", "mimeType": "application/octet-stream",
                 "size": "5", "parents": ["ZZZ"]},
            ]
        },
    ]
    client = DriveClient.__new__(DriveClient)  # skip __init__ (no real creds needed)
    client.service = _FakeService(pages, root_id="R")

    files, folders, warnings = client.list_tree("root")

    assert set(files.keys()) == {"Photos/a.txt", "b.bin"}, files.keys()
    assert folders[""] == "R" and folders["Photos"] == "A"
    assert files["Photos/a.txt"].size == 10 and files["b.bin"].size == 20
    assert "orphan.bin" not in {f.name for f in files.values()}


def test_fetch_changes_and_apply():
    # items currently cached: one file will be modified, one deleted.
    items = {
        "f1": {"id": "f1", "name": "a.txt", "mimeType": "text/plain",
               "size": "10", "parents": ["R"]},
        "f2": {"id": "f2", "name": "b.bin", "mimeType": "application/octet-stream",
               "size": "20", "parents": ["R"]},
    }
    changes = [
        # f1 renamed + resized
        {"fileId": "f1", "file": {"id": "f1", "name": "a2.txt", "mimeType": "text/plain",
                                  "size": "99", "parents": ["R"], "trashed": False}},
        # f2 moved to the trash -> treated as removed
        {"fileId": "f2", "file": {"id": "f2", "name": "b.bin", "trashed": True}},
        # f4 hard-deleted
        {"fileId": "f4", "removed": True},
        # f5 is a new file
        {"fileId": "f5", "file": {"id": "f5", "name": "new.bin",
                                  "mimeType": "application/octet-stream",
                                  "size": "7", "parents": ["R"], "trashed": False}},
    ]
    client = DriveClient.__new__(DriveClient)
    client.service = _FakeService([], root_id="R", changes=changes, new_token="tokNEW")

    upserts, removed, new_token = client.fetch_changes("tokOLD")
    assert new_token == "tokNEW"
    assert set(upserts) == {"f1", "f5"} and removed == {"f2", "f4"}

    drive_cache.apply_changes(items, upserts, removed)
    assert set(items) == {"f1", "f5"}
    assert items["f1"]["name"] == "a2.txt" and items["f1"]["size"] == "99"

    # Rebuild the tree from the patched items: only a2.txt and new.bin remain.
    files, _folders, _warn = build_tree(items, "R")
    assert set(files.keys()) == {"a2.txt", "new.bin"}


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


def test_plan_export_native():
    doc = _remote("doc", size=7, mtime=2000.0,
                  mime="application/vnd.google-apps.document")
    no_copy = _item("doc", GOOGLE_NATIVE, remote=doc)

    # Checkbox off -> natives stay skipped.
    actions, _ = build_plan([no_copy], DIR_DOWN, CONFLICT_NEWER, mirror=False)
    assert actions == []

    # On + a download direction -> export planned at "<rel>.docx".
    actions, _ = build_plan([no_copy], DIR_DOWN, CONFLICT_NEWER, mirror=False,
                            export_native=True)
    assert [(a.op, a.relpath) for a in actions] == [(OP_EXPORT_LOCAL, "doc.docx")]

    # Upload direction never exports.
    actions, _ = build_plan([no_copy], DIR_UP, CONFLICT_NEWER, mirror=False,
                            export_native=True)
    assert actions == []

    # Fresh copy -> nothing to do; outdated copy -> re-export.
    copy = _local("doc.docx", size=50, mtime=2000.0)
    fresh = _item("doc", GOOGLE_NATIVE, remote=doc, export_local=copy)
    actions, _ = build_plan([fresh], DIR_DOWN, CONFLICT_NEWER, mirror=False,
                            export_native=True)
    assert actions == []
    outdated = _item("doc", GOOGLE_NATIVE, remote=doc, export_local=copy, newer="remote")
    actions, _ = build_plan([outdated], DIR_BOTH, CONFLICT_NEWER, mirror=False,
                            export_native=True)
    assert [a.op for a in actions] == [OP_EXPORT_LOCAL]

    # A real file already owns "doc.docx" -> the export stands down.
    real = _item("doc.docx", IDENTICAL, local=_local("doc.docx"),
                 remote=_remote("doc.docx"))
    actions, _ = build_plan([no_copy, real], DIR_DOWN, CONFLICT_NEWER, mirror=False,
                            export_native=True)
    assert actions == []

    # Non-exportable native types (Forms, ...) stay skipped.
    form = _item("form", GOOGLE_NATIVE,
                 remote=_remote("form", size=None, mime="application/vnd.google-apps.form"))
    actions, _ = build_plan([form], DIR_DOWN, CONFLICT_NEWER, mirror=False,
                            export_native=True)
    assert actions == []


# --------------------------------------------------------------------------- #
# network retry (IncompleteRead and friends during transfers)
# --------------------------------------------------------------------------- #
def test_with_net_retry_recovers_and_gives_up():
    import http.client

    from services.gdrive import _with_net_retry

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise http.client.IncompleteRead(b"partial")
        return "ok"

    assert _with_net_retry(flaky, base_delay=0.0) == "ok"
    assert calls["n"] == 3

    def always_broken():
        raise ConnectionResetError("network down")

    try:
        _with_net_retry(always_broken, attempts=2, base_delay=0.0)
    except ConnectionResetError:
        pass
    else:
        raise AssertionError("expected ConnectionResetError once retries ran out")


# --------------------------------------------------------------------------- #
# cancel (the Stop button during scan/compare)
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
            raise AssertionError("scan_local must raise SyncCancelled when cancelled")


def test_compare_cancel_raises():
    import threading

    from services.common import SyncCancelled

    local = {"f.bin": _local("f.bin", size=10)}
    remote = {"f.bin": _remote("f.bin", size=10)}
    cancel = threading.Event()
    cancel.set()
    try:
        compare_maps(local, remote, cancel=cancel)
    except SyncCancelled:
        pass
    else:
        raise AssertionError("compare_maps must raise SyncCancelled when cancelled")


# --------------------------------------------------------------------------- #
# parallel sync
# --------------------------------------------------------------------------- #
def test_progress_state_tracks_parallel_files():
    p = ProgressState(total_files=2, total_bytes=30, direction=DIR_UP, mode="newer")
    a1 = Action(OP_UPLOAD, "a.bin", 10, _local("a.bin", size=10), None)
    a2 = Action(OP_UPLOAD, "b.bin", 20, _local("b.bin", size=20), None)

    p.begin_file(a1)
    p.begin_file(a2)
    p.set_current_bytes("a.bin", 5)
    p.set_current_bytes("b.bin", 100)  # exceeds size -> must be clamped to 20

    snap = p.snapshot()
    assert snap["done_bytes"] == 25, snap["done_bytes"]
    assert dict(snap["active"])["a.bin"] == 0.5

    p.finish_file("a.bin", ok=True)
    p.finish_file("b.bin", ok=False, error="b.bin: loi mang")
    snap = p.snapshot()
    assert snap["done_files"] == 1 and snap["failed_files"] == 1
    assert snap["done_bytes"] == 30 and snap["active"] == []


def test_sync_runner_parallel_local_deletes():
    """Run a real SyncRunner with 3 workers — local deletions only (no Drive
    needed): every file must land in .sync_trash and the progress must match."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        actions = []
        for i in range(9):
            f = root / f"f{i}.bin"
            f.write_bytes(b"x")
            actions.append(
                Action(OP_DELETE_LOCAL, f.name, 0,
                       LocalFile(f.name, f, 1, 1000.0), None)
            )

        progress = ProgressState(len(actions), 0, DIR_DOWN, "newer+mirror")
        runner = SyncRunner(
            creds=None,
            seagate_root=root,
            drive_root_path="root",
            actions=actions,
            remote_folders={"": "root"},  # pre-seeded -> no Drive resolve call
            progress=progress,
            workers=3,
            client_factory=lambda: object(),  # delete_local never touches the client
        )
        runner.start()
        runner.join(timeout=30)

        snap = progress.snapshot()
        assert not runner.is_alive()
        assert snap["finished"] and snap["fatal"] is None
        assert snap["done_files"] == 9 and snap["failed_files"] == 0
        # No originals left; everything sits in .sync_trash/<timestamp>/
        assert not any(fp.name.endswith(".bin") for fp in root.iterdir() if fp.is_file())
        trash = root / config.LOCAL_TRASH_DIRNAME
        moved = list(trash.rglob("*.bin"))
        assert len(moved) == 9, moved


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
            print(f"FAIL {fn.__name__}: {exc.__class__.__name__}: {exc}")
            import traceback

            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {fn.__name__}")
    print(f"\n{passed} passed, {failed} failed ({len(tests)} total).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
