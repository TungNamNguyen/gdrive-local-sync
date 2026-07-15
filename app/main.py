"""Streamlit UI for the Seagate <-> Google Drive sync app.

Four tabs: Compare / Sync / History / Guide. Every user-facing string is
Vietnamese; source code and comments are English.

Flow: log in (security) -> connect Drive (OAuth) -> scan & compare
(ScanRunner) -> build a plan -> sync (SyncRunner) -> browse history.

This file is the only UI layer. All logic lives in app/services/* (which
never imports Streamlit). Both long-running tasks (scan, sync) run on their
own threads so the user can press Stop/Cancel; the UI only reads snapshots
and polls.
"""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

import config
import security
from services import history
from services.compare import ALL_STATUSES, STATUS_VI
from services.gdrive import (
    CREDENTIALS_FILE,
    DriveClient,
    build_web_auth_url,
    delete_credentials,
    exchange_code,
    load_saved_credentials,
)
from services import drive_cache
from services.scan import (
    DRIVE_INCREMENTAL,
    PHASE_COMPARE,
    PHASE_DRIVE,
    PHASE_LOCAL,
    PHASE_VI,
    ScanRunner,
    ScanState,
)
from services.sync import (
    CONFLICT_FORCE,
    CONFLICT_NEWER,
    CONFLICT_SKIP,
    DIR_BOTH,
    DIR_DOWN,
    DIR_UP,
    DIRECTION_VI,
    OP_DELETE_LOCAL,
    OP_TRASH_REMOTE,
    OP_VI,
    ProgressState,
    SyncRunner,
    build_plan,
)
from utils import human_eta, human_rate, human_size

POLL_INTERVAL = 0.7  # seconds — progress poll cadence while a task runs (per CLAUDE.md)

CONFLICT_VI = {
    CONFLICT_NEWER: "Bên mới hơn thắng",
    CONFLICT_FORCE: "Luôn ghi đè theo hướng đã chọn",
    CONFLICT_SKIP: "Bỏ qua file khác nhau",
}


# --------------------------------------------------------------------------- #
# Initialization
# --------------------------------------------------------------------------- #
def _init_app() -> None:
    st.set_page_config(
        page_title="Seagate ⇄ Google Drive Sync",
        page_icon="🔄",
        layout="wide",
    )
    config.ensure_dirs()
    history.init_db()


def _get_creds():
    """Return the saved credentials (session-cached to avoid re-reading the file)."""
    creds = st.session_state.get("creds")
    if creds is not None:
        return creds
    creds = load_saved_credentials()
    if creds is not None:
        st.session_state["creds"] = creds
    return creds


def _drive_root() -> str:
    return st.session_state.get("drive_root", config.DRIVE_ROOT_DEFAULT)


def _reset_comparison() -> None:
    """Drop stale scan/compare/plan results (called when the source config changes)."""
    for key in (
        "local_files",
        "local_errors",
        "remote_files",
        "remote_folders",
        "remote_warnings",
        "cmp_items",
        "cmp_counts",
        "cmp_bytes",
        "plan_actions",
        "plan_skipped",
        "plan_meta",
    ):
        st.session_state.pop(key, None)


def _clear_scan() -> None:
    st.session_state.pop("scan_state", None)
    st.session_state.pop("scan_runner", None)


def _abort_scan() -> None:
    """Stop a running scan thread (if any) — called on source change/logout."""
    state = st.session_state.get("scan_state")
    if state is not None:
        state.cancel.set()
    _clear_scan()


# --------------------------------------------------------------------------- #
# Sidebar: configuration + Google account
# --------------------------------------------------------------------------- #
def _render_sidebar() -> object | None:
    st.sidebar.title("🔄 Seagate ⇄ Drive")

    st.sidebar.subheader("Cấu hình")
    # The Seagate path comes from .env; no need to display it — only warn when missing.
    seagate = config.SEAGATE_PATH
    if not seagate.is_dir():
        st.sidebar.error(f"Không thấy ổ Seagate tại `{seagate}` — kiểm tra kết nối/mount.")

    drive_root = st.sidebar.text_input(
        "Thư mục gốc trên Drive",
        value=_drive_root(),
        help="`root` = toàn bộ My Drive, hoặc ví dụ `Backup/Seagate`.",
    )
    drive_root = (drive_root or "root").strip() or "root"
    if drive_root != st.session_state.get("drive_root"):
        st.session_state["drive_root"] = drive_root
        _abort_scan()  # a running scan would produce results for the old root
        _reset_comparison()

    st.sidebar.divider()
    return _render_account(drive_root)


def _handle_oauth_callback() -> None:
    """Handle Google redirecting back to the app with ?code=... (or ?error=...).

    The page fully reloaded so session_state is empty; exchange `code` for a
    token straight from credentials.json (no original flow needed), save it,
    then clean the URL.
    """
    params = st.query_params
    if "code" in params and st.session_state.get("creds") is None:
        try:
            creds = exchange_code(params["code"], config.OAUTH_REDIRECT_URI)
            st.session_state["creds"] = creds
            st.session_state.pop("remote_email", None)
        except Exception as exc:  # noqa: BLE001 — show the error instead of crashing
            st.session_state["oauth_error"] = f"Đăng nhập Google thất bại: {exc}"
        st.query_params.clear()
        st.rerun()
    elif "error" in params:
        st.session_state["oauth_error"] = f"Google từ chối cấp quyền: {params['error']}"
        st.query_params.clear()
        st.rerun()


def _render_account(drive_root: str) -> object | None:
    st.sidebar.subheader("Tài khoản Google Drive")

    if st.session_state.get("oauth_error"):
        st.sidebar.error(st.session_state.pop("oauth_error"))

    creds = _get_creds()
    if creds is not None:
        email = st.session_state.get("remote_email")
        if not email:
            try:
                email = DriveClient(creds).user_email()
            except Exception as exc:  # noqa: BLE001 — display only
                email = f"(không lấy được email: {exc})"
            st.session_state["remote_email"] = email
        st.sidebar.success(f"Đã đăng nhập:\n\n**{email}**")
        if st.sidebar.button("🔌 Đăng xuất Google", use_container_width=True):
            delete_credentials()
            drive_cache.clear()  # the Drive cache belongs to the old account
            for key in ("creds", "remote_email"):
                st.session_state.pop(key, None)
            _abort_scan()
            _reset_comparison()
            st.rerun()
        return creds

    # Not signed in to Google yet ---------------------------------------------
    if not CREDENTIALS_FILE.exists():
        st.sidebar.error(
            "Thiếu `secrets/credentials.json`.\n\n"
            "Tải OAuth client (Desktop app) từ Google Cloud Console và đặt vào "
            "thư mục `secrets/`."
        )
        return None

    # "Sign in with Google": navigate to Google, return to the app automatically.
    auth_url = build_web_auth_url(config.OAUTH_REDIRECT_URI)
    st.sidebar.link_button(
        "🔐 Đăng nhập với Google", auth_url, type="primary", use_container_width=True
    )
    st.sidebar.caption("Bấm nút trên → chọn tài khoản Google → tự quay lại đây.")
    return None


# --------------------------------------------------------------------------- #
# Tab 1 — Compare
# --------------------------------------------------------------------------- #
# Never scan/sync the app's own artifacts, even if the user edits the exclude
# box: uploading .sync_trash would re-upload files the user just deleted, and
# .syncpart files are half-finished downloads.
_ALWAYS_EXCLUDE = [config.LOCAL_TRASH_DIRNAME, f"{config.LOCAL_TRASH_DIRNAME}/*", "*.syncpart"]


def _current_excludes() -> list[str]:
    raw = st.session_state.get("exclude_text")
    if raw is None:
        patterns = list(config.DEFAULT_EXCLUDES)
    else:
        patterns = [line.strip() for line in raw.splitlines() if line.strip()]
    for pat in _ALWAYS_EXCLUDE:
        if pat not in patterns:
            patterns.append(pat)
    return patterns


def render_compare_tab(creds) -> None:
    st.header("So sánh Seagate ⇄ Drive")

    # A scan is running on the background thread -> show progress + Stop only.
    if st.session_state.get("scan_state") is not None:
        _render_scan_progress()
        return

    with st.expander("⚙️ Mẫu loại trừ (bỏ qua khi quét)"):
        st.text_area(
            "Mỗi dòng một mẫu (fnmatch, không phân biệt hoa/thường)",
            value="\n".join(config.DEFAULT_EXCLUDES),
            key="exclude_text",
            height=180,
        )

    disabled = creds is None or not config.SEAGATE_PATH.is_dir()
    if creds is None:
        st.info("⬅️ Hãy kết nối Google Drive ở thanh bên trái trước.")

    col_scan, col_full = st.columns([3, 2])
    if col_scan.button("🔍 Quét & So sánh", type="primary", disabled=disabled):
        _start_scan(creds, force_full=False)
    if col_full.button(
        "🔄 Quét lại toàn bộ",
        disabled=disabled,
        help="Bỏ qua cache thay đổi, tải lại toàn bộ danh sách Drive từ đầu. "
        "Dùng khi nghi kết quả quét nhanh bị lệch.",
    ):
        _start_scan(creds, force_full=True)

    if "cmp_items" not in st.session_state:
        return

    _render_comparison_results()


def _start_scan(creds, force_full: bool) -> None:
    """Start ScanRunner on a background thread, then rerun into the progress screen.

    The background thread is what makes the Stop button clickable: scanning
    inline in this script run would block Streamlit until the scan finished.
    """
    _reset_comparison()  # old results are stale the moment a rescan starts
    state = ScanState()
    runner = ScanRunner(
        creds=creds,
        seagate_root=config.SEAGATE_PATH,
        exclude_patterns=_current_excludes(),
        drive_root_path=_drive_root(),
        state=state,
        account=st.session_state.get("remote_email"),
        force_full=force_full,
    )
    runner.start()
    st.session_state["scan_state"] = state
    st.session_state["scan_runner"] = runner
    st.rerun()


def _render_scan_progress() -> None:
    state: ScanState = st.session_state["scan_state"]
    snap = state.snapshot()

    # Finished successfully -> load results into the session, back to results screen.
    if snap["finished"] and snap["result"] is not None:
        st.session_state.update(snap["result"])
        _clear_scan()
        st.rerun()
        return

    st.subheader("Đang quét & so sánh")

    done_local = snap["phase"] != PHASE_LOCAL
    icon = "✅" if done_local else "⏳"
    st.write(
        f"{icon} 💽 **Ổ Seagate** — {snap['local_files']:,} tệp · "
        f"{human_size(snap['local_bytes'])}"
    )

    if snap["phase"] in (PHASE_DRIVE, PHASE_COMPARE):
        done_drive = snap["phase"] != PHASE_DRIVE
        icon = "✅" if done_drive else "⏳"
        fast = " ⚡ quét nhanh (chỉ hỏi thay đổi)" if snap["drive_mode"] == DRIVE_INCREMENTAL else ""
        st.write(
            f"{icon} ☁️ **Google Drive** — {snap['drive_files']:,} tệp · "
            f"{snap['drive_folders']:,} thư mục{fast}"
        )

    if snap["phase"] == PHASE_COMPARE:
        st.write("⏳ 🔍 **So sánh**")

    if not snap["finished"]:
        st.caption(f"Đã chạy {human_eta(snap['elapsed'])} · {PHASE_VI[snap['phase']]}")
        if snap["cancel_requested"]:
            st.warning("Đang dừng… chờ thao tác hiện tại kết thúc an toàn.")
        elif st.button("⛔ Dừng quét", type="secondary"):
            state.cancel.set()
            st.rerun()
        time.sleep(POLL_INTERVAL)
        st.rerun()
        return

    # Stopped or failed ------------------------------------------------------
    if snap["cancelled"]:
        st.info("⛔ Đã dừng quét theo yêu cầu — chưa có kết quả so sánh.")
    elif snap["error"]:
        st.error(f"Lỗi khi quét/so sánh: {snap['error']}")

    if st.button("↩️ Quay lại"):
        _clear_scan()
        st.rerun()


def _render_comparison_results() -> None:
    counts = st.session_state["cmp_counts"]
    byte_totals = st.session_state["cmp_bytes"]
    items = st.session_state["cmp_items"]

    st.subheader("Kết quả")
    cols = st.columns(len(ALL_STATUSES))
    for col, status in zip(cols, ALL_STATUSES):
        col.metric(
            STATUS_VI[status],
            f"{counts.get(status, 0):,}",
            help=f"Tổng: {human_size(byte_totals.get(status, 0))}",
        )

    local_errors = st.session_state.get("local_errors") or []
    warnings = st.session_state.get("remote_warnings") or []
    if local_errors or warnings:
        with st.expander(f"⚠️ Cảnh báo ({len(local_errors) + len(warnings)})"):
            for msg in warnings:
                st.write(f"☁️ {msg}")
            for msg in local_errors[:200]:
                st.write(f"💽 {msg}")

    # Detail table with a status filter.
    chosen = st.multiselect(
        "Lọc theo trạng thái",
        options=ALL_STATUSES,
        default=ALL_STATUSES,
        format_func=lambda s: STATUS_VI[s],
    )
    rows = []
    for it in items:
        if it.status not in chosen:
            continue
        rows.append(
            {
                "Trạng thái": STATUS_VI[it.status],
                "Đường dẫn": it.relpath,
                "KT Seagate": human_size(it.local.size) if it.local else "",
                "KT Drive": human_size(it.remote.size) if (it.remote and it.remote.size is not None) else "",
                "Mới hơn": {"local": "Seagate", "remote": "Drive"}.get(it.newer or "", ""),
            }
        )
    df = pd.DataFrame(rows)
    st.caption(f"Hiển thị {len(rows):,} / {len(items):,} mục.")
    st.dataframe(df, use_container_width=True, hide_index=True, height=420)
    if not df.empty:
        st.download_button(
            "⬇️ Tải CSV",
            data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name="so_sanh.csv",
            mime="text/csv",
        )


# --------------------------------------------------------------------------- #
# Tab 2 — Sync
# --------------------------------------------------------------------------- #
def render_sync_tab(creds) -> None:
    st.header("Đồng bộ")

    # While running (or just finished) show progress; no new configuration.
    if st.session_state.get("progress") is not None:
        _render_progress()
        return

    if "cmp_items" not in st.session_state:
        st.info("Hãy chạy **So sánh** ở tab bên trái trước khi lập kế hoạch.")
        return
    if creds is None:
        st.info("⬅️ Cần kết nối Google Drive để đồng bộ.")
        return

    _render_plan_config()

    if st.session_state.get("plan_actions") is not None:
        _render_plan_and_start(creds)


def _render_plan_config() -> None:
    col1, col2 = st.columns(2)
    with col1:
        direction = st.radio(
            "Hướng đồng bộ",
            options=[DIR_UP, DIR_DOWN, DIR_BOTH],
            format_func=lambda d: DIRECTION_VI[d],
        )
    with col2:
        if direction == DIR_BOTH:
            st.caption("Hai chiều: bên **mới hơn** sẽ thắng. File không xác định "
                       "được bên nào mới hơn sẽ bị bỏ qua.")
            conflict = CONFLICT_NEWER
        else:
            conflict = st.radio(
                "Xử lý file khác nhau (xung đột)",
                options=[CONFLICT_NEWER, CONFLICT_FORCE, CONFLICT_SKIP],
                format_func=lambda c: CONFLICT_VI[c],
            )

    mirror = False
    if direction != DIR_BOTH:
        mirror = st.checkbox(
            "🗑️ Chế độ mirror — xoá bên đích những file không còn ở bên nguồn",
            value=False,
            help="Xoá luôn có thể khôi phục: Drive → Thùng rác; Seagate → thư mục "
            "`.sync_trash/` trên chính ổ đó. Không bao giờ xoá vĩnh viễn.",
        )

    if st.button("📋 Lập kế hoạch", type="primary"):
        actions, skipped = build_plan(
            st.session_state["cmp_items"], direction, conflict, mirror
        )
        st.session_state["plan_actions"] = actions
        st.session_state["plan_skipped"] = skipped
        st.session_state["plan_meta"] = {
            "direction": direction,
            "conflict": conflict,
            "mirror": mirror,
        }


def _render_plan_and_start(creds) -> None:
    actions = st.session_state["plan_actions"]
    skipped = st.session_state["plan_skipped"]
    meta = st.session_state["plan_meta"]

    st.divider()
    st.subheader("Kế hoạch")
    total_bytes = sum(a.size for a in actions)
    c1, c2, c3 = st.columns(3)
    c1.metric("Số thao tác", f"{len(actions):,}")
    c2.metric("Tổng dữ liệu truyền", human_size(total_bytes))
    c3.metric("Bỏ qua (xung đột)", f"{skipped:,}")

    if not actions:
        st.success("Không có gì để đồng bộ — hai bên đã khớp theo hướng đã chọn. 🎉")
        return

    op_counts: dict[str, int] = {}
    for a in actions:
        op_counts[a.op] = op_counts.get(a.op, 0) + 1
    st.write(" · ".join(f"{OP_VI[op]}: **{n}**" for op, n in op_counts.items()))

    with st.expander("Xem chi tiết kế hoạch", expanded=False):
        df = pd.DataFrame(
            {
                "Thao tác": [OP_VI[a.op] for a in actions],
                "Đường dẫn": [a.relpath for a in actions],
                "Kích thước": [human_size(a.size) if a.size else "" for a in actions],
            }
        )
        st.dataframe(df, use_container_width=True, hide_index=True, height=360)

    # Mirror confirmation: typing XOA is mandatory.
    can_start = True
    if meta["mirror"]:
        deletions = sum(1 for a in actions if a.op in (OP_TRASH_REMOTE, OP_DELETE_LOCAL))
        st.warning(
            f"⚠️ Chế độ **mirror** sẽ chuyển **{deletions}** file bên đích vào "
            "Thùng rác/`.sync_trash`. Gõ `XOA` để xác nhận."
        )
        confirm = st.text_input("Xác nhận xoá", placeholder="Gõ XOA")
        can_start = confirm.strip() == "XOA"

    if st.button("🚀 Bắt đầu đồng bộ", type="primary", disabled=not can_start):
        mode = meta["conflict"] + ("+mirror" if meta["mirror"] else "")
        progress = ProgressState(len(actions), total_bytes, meta["direction"], mode)
        runner = SyncRunner(
            creds=creds,
            seagate_root=config.SEAGATE_PATH,
            drive_root_path=_drive_root(),
            actions=actions,
            remote_folders=st.session_state.get("remote_folders"),
            progress=progress,
        )
        runner.start()
        st.session_state["progress"] = progress
        st.session_state["runner"] = runner
        st.rerun()


def _render_progress() -> None:
    progress: ProgressState = st.session_state["progress"]
    snap = progress.snapshot()

    st.subheader("Tiến độ đồng bộ")

    if snap["total_bytes"] > 0:
        frac = min(snap["done_bytes"] / snap["total_bytes"], 1.0)
    elif snap["total_files"] > 0:
        frac = min(snap["done_files"] / snap["total_files"], 1.0)
    else:
        frac = 1.0
    st.progress(frac, text=f"{human_size(snap['done_bytes'])} / {human_size(snap['total_bytes'])}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("File xong", f"{snap['done_files']:,} / {snap['total_files']:,}")
    c2.metric("Lỗi", f"{snap['failed_files']:,}")
    c3.metric("Tốc độ", human_rate(snap["speed"]))
    c4.metric("Còn lại (ETA)", human_eta(snap["eta"]))

    for rel, frac in snap["active"]:
        line = f"Đang xử lý: `{rel}`"
        if frac is not None:
            line += f" — {frac * 100:.0f}%"
        st.caption(line)

    if snap["fatal"]:
        st.error(f"Lỗi nghiêm trọng: {snap['fatal']}")

    with st.expander("Nhật ký", expanded=not snap["finished"]):
        st.code("\n".join(snap["log"][-40:]) or "(chưa có)", language=None)

    if not snap["finished"]:
        if snap["cancel_requested"]:
            st.warning("Đang huỷ… chờ thao tác hiện tại kết thúc an toàn.")
        elif st.button("🛑 Huỷ", type="secondary"):
            progress.cancel.set()
            st.rerun()
        time.sleep(POLL_INTERVAL)
        st.rerun()
        return

    # Finished -----------------------------------------------------------------
    if snap["failed_files"]:
        st.warning(f"Hoàn tất với {snap['failed_files']:,} lỗi.")
        if snap["errors"]:
            with st.expander(f"Chi tiết lỗi ({len(snap['errors'])})"):
                for e in snap["errors"]:
                    st.write(f"• {e}")
    elif snap["cancel_requested"]:
        st.info("Đã huỷ theo yêu cầu.")
    else:
        st.success("Đồng bộ hoàn tất! 🎉")

    if st.button("✅ Xong (quét lại để đồng bộ tiếp)"):
        st.session_state.pop("progress", None)
        st.session_state.pop("runner", None)
        _reset_comparison()
        st.rerun()


# --------------------------------------------------------------------------- #
# Tab 3 — History
# --------------------------------------------------------------------------- #
_STATUS_LABEL = {
    "success": "✅ Thành công",
    "done_with_errors": "⚠️ Xong (có lỗi)",
    "cancelled": "⛔ Đã huỷ",
    "error": "❌ Lỗi",
    "running": "⏳ Đang chạy",
}


def render_history_tab() -> None:
    st.header("Lịch sử đồng bộ")
    if st.button("🔄 Làm mới"):
        st.rerun()

    sessions = history.fetch_sessions(limit=200)
    if not sessions:
        st.info("Chưa có phiên đồng bộ nào.")
        return

    rows = []
    for s in sessions:
        rows.append(
            {
                "ID": s["id"],
                "Bắt đầu": s["started_at"],
                "Kết thúc": s.get("finished_at") or "",
                "Hướng": DIRECTION_VI.get(s["direction"], s["direction"]),
                "Chế độ": s["mode"],
                "Trạng thái": _STATUS_LABEL.get(s["status"], s["status"]),
                "File xong": f"{s['done_files']:,}/{s['planned_files']:,}",
                "Lỗi": s["failed_files"],
                "Dữ liệu": human_size(s["done_bytes"]),
            }
        )
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True, height=460)
    st.download_button(
        "⬇️ Tải CSV",
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name="lich_su_dong_bo.csv",
        mime="text/csv",
    )


# --------------------------------------------------------------------------- #
# Tab 4 — Guide
# --------------------------------------------------------------------------- #
def render_guide_tab() -> None:
    st.header("Hướng dẫn sử dụng")
    st.markdown(
        """
### 1. Đăng nhập Google
1. (Chỉ lần đầu) Đặt file `credentials.json` (OAuth client dạng **Desktop app**,
   tải từ Google Cloud Console) vào thư mục `secrets/`.
2. Ở sidebar bấm **🔐 Đăng nhập với Google** → chọn tài khoản → **Cho phép**.
   Trình duyệt tự quay lại app, không phải dán gì cả.
3. Muốn **đổi tài khoản**: bấm **🔌 Đăng xuất Google** rồi đăng nhập lại.

> App chưa qua Google verification nên lần cấp quyền sẽ có màn hình cảnh báo —
> bấm **Advanced → Go to app** để tiếp tục (an toàn vì app do chính bạn tạo).

### 2. So sánh
- Bấm **Quét & So sánh** để đối chiếu ổ Seagate với Google Drive theo đường dẫn
  (hai file cùng đường dẫn + cùng kích thước = giống nhau).
- Từ lần quét thứ hai, phía Drive chỉ hỏi **những gì thay đổi** nên rất nhanh (⚡).
  Nếu nghi kết quả bị lệch, bấm **🔄 Quét lại toàn bộ**.
- Đang quét muốn ngừng thì bấm **⛔ Dừng quét**. Quét chỉ **đọc**, nên dừng giữa
  chừng hoàn toàn an toàn — không có gì thay đổi trên ổ Seagate hay Drive; chỉ là
  không có kết quả so sánh, hãy quét lại từ đầu.

### 3. Đồng bộ
- Chọn **hướng** (lên / xuống / hai chiều) và cách xử lý **xung đột**, rồi
  **Lập kế hoạch** để xem trước.
- **Mirror** (chỉ cho một chiều) sẽ xoá file thừa ở bên đích — phải gõ `XOA`
  để xác nhận.
- Bấm **Bắt đầu đồng bộ** và theo dõi tiến độ trực tiếp; có thể **Huỷ** giữa chừng.

### An toàn dữ liệu
- Xoá luôn **khôi phục được**: trên Drive vào **Thùng rác**, trên Seagate vào
  thư mục `.sync_trash/<thời-điểm>/`. Không bao giờ xoá vĩnh viễn.
- **Đừng sửa đổi hai bên** trong lúc đang đồng bộ.
- Thời gian sửa (mtime) được giữ nguyên hai chiều, nên "bên mới hơn thắng"
  đáng tin cậy kể từ lần đồng bộ đầu tiên.
"""
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    _init_app()
    security.require_login()
    _handle_oauth_callback()  # handle ?code=... when Google redirects back

    creds = _render_sidebar()

    tab_compare, tab_sync, tab_history, tab_guide = st.tabs(
        ["🔍 So sánh", "🔄 Đồng bộ", "📜 Lịch sử", "📖 Hướng dẫn"]
    )
    with tab_compare:
        render_compare_tab(creds)
    with tab_sync:
        render_sync_tab(creds)
    with tab_history:
        render_history_tab()
    with tab_guide:
        render_guide_tab()


if __name__ == "__main__":
    main()
