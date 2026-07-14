"""Giao dien Streamlit cho ung dung dong bo Seagate <-> Google Drive.

Bon tab: So sanh / Dong bo / Lich su / Huong dan. Toan bo chuoi hien thi
bang tieng Viet; ma nguon va chu thich bang tieng Anh.

Luong: dang nhap (security) -> ket noi Drive (OAuth) -> quet & so sanh
(ScanRunner) -> lap ke hoach -> dong bo (SyncRunner) -> xem lich su.

File nay la lop UI duy nhat. Moi logic nam trong app/services/* (khong
import Streamlit). Ca hai tac vu dai (quet, dong bo) chay o thread rieng nen
nguoi dung bam Dung/Huy duoc; UI chi doc qua snapshot() va poll.
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
from services.scan import (
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

POLL_INTERVAL = 0.7  # giay — nhip poll tien do khi dang chay (theo CLAUDE.md)

CONFLICT_VI = {
    CONFLICT_NEWER: "Bên mới hơn thắng",
    CONFLICT_FORCE: "Luôn ghi đè theo hướng đã chọn",
    CONFLICT_SKIP: "Bỏ qua file khác nhau",
}


# --------------------------------------------------------------------------- #
# Khoi tao
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
    """Tra ve credentials da luu (cache trong session de tranh doc file lien tuc)."""
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
    """Xoa ket qua quet/so sanh/ke hoach cu (goi khi doi cau hinh nguon)."""
    for key in (
        "local_files",
        "local_errors",
        "remote_files",
        "remote_folders",
        "remote_warnings",
        "cmp_items",
        "cmp_counts",
        "cmp_bytes",
        "cmp_used_md5",
        "plan_actions",
        "plan_skipped",
        "plan_meta",
    ):
        st.session_state.pop(key, None)


def _clear_scan() -> None:
    st.session_state.pop("scan_state", None)
    st.session_state.pop("scan_runner", None)


def _abort_scan() -> None:
    """Dung thread quet dang chay (neu co) — goi khi doi nguon/dang xuat."""
    state = st.session_state.get("scan_state")
    if state is not None:
        state.cancel.set()
    _clear_scan()


# --------------------------------------------------------------------------- #
# Sidebar: cau hinh + tai khoan Google
# --------------------------------------------------------------------------- #
def _render_sidebar() -> object | None:
    st.sidebar.title("🔄 Seagate ⇄ Drive")

    st.sidebar.subheader("Cấu hình")
    seagate = config.SEAGATE_PATH
    if seagate.is_dir():
        st.sidebar.caption(f"💽 Ổ Seagate: `{seagate}`")
    else:
        st.sidebar.error(f"Không thấy ổ Seagate tại `{seagate}` — kiểm tra kết nối/mount.")

    drive_root = st.sidebar.text_input(
        "Thư mục gốc trên Drive",
        value=_drive_root(),
        help="`root` = toàn bộ My Drive, hoặc ví dụ `Backup/Seagate`.",
    )
    drive_root = (drive_root or "root").strip() or "root"
    if drive_root != st.session_state.get("drive_root"):
        st.session_state["drive_root"] = drive_root
        _abort_scan()  # lan quet dang chay se cho ra ket qua cua goc cu
        _reset_comparison()

    st.sidebar.divider()
    return _render_account(drive_root)


def _handle_oauth_callback() -> None:
    """Xu ly khi Google redirect ve app kem ?code=... (hoac ?error=...).

    Trang da reload hoan toan nen session_state trong, ta doi `code` lay token
    truc tiep tu credentials.json (khong can flow cu), luu lai, roi don URL.
    """
    params = st.query_params
    if "code" in params and st.session_state.get("creds") is None:
        try:
            creds = exchange_code(params["code"], config.OAUTH_REDIRECT_URI)
            st.session_state["creds"] = creds
            st.session_state.pop("remote_email", None)
        except Exception as exc:  # noqa: BLE001 — hien thi loi thay vi crash
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
            except Exception as exc:  # noqa: BLE001 — chi de hien thi
                email = f"(không lấy được email: {exc})"
            st.session_state["remote_email"] = email
        st.sidebar.success(f"Đã đăng nhập:\n\n**{email}**")
        if st.sidebar.button("🔌 Đăng xuất Google", use_container_width=True):
            delete_credentials()
            for key in ("creds", "remote_email"):
                st.session_state.pop(key, None)
            _abort_scan()
            _reset_comparison()
            st.rerun()
        return creds

    # Chua dang nhap Google --------------------------------------------------
    if not CREDENTIALS_FILE.exists():
        st.sidebar.error(
            "Thiếu `secrets/credentials.json`.\n\n"
            "Tải OAuth client (Desktop app) từ Google Cloud Console và đặt vào "
            "thư mục `secrets/`."
        )
        return None

    # "Dang nhap voi Google": dieu huong sang trang Google, quay ve app tu dong.
    auth_url = build_web_auth_url(config.OAUTH_REDIRECT_URI)
    st.sidebar.link_button(
        "🔐 Đăng nhập với Google", auth_url, type="primary", use_container_width=True
    )
    st.sidebar.caption("Bấm nút trên → chọn tài khoản Google → tự quay lại đây.")
    return None


# --------------------------------------------------------------------------- #
# Tab 1 — So sanh
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

    # Dang quet o thread nen -> chi hien tien do + nut Dung.
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

    use_md5 = st.checkbox(
        "Đối chiếu MD5 cho file cùng kích thước (chậm hơn, chắc chắn hơn)",
        value=False,
        help="Mặc định chỉ so khớp kích thước. Bật MD5 để kiểm tra nội dung "
        "chính xác tuyệt đối (phải đọc lại file trên ổ Seagate).",
    )

    disabled = creds is None or not config.SEAGATE_PATH.is_dir()
    if creds is None:
        st.info("⬅️ Hãy kết nối Google Drive ở thanh bên trái trước.")

    if st.button("🔍 Quét & So sánh", type="primary", disabled=disabled):
        _start_scan(creds, use_md5)

    if "cmp_items" not in st.session_state:
        return

    _render_comparison_results()


def _start_scan(creds, use_md5: bool) -> None:
    """Khoi dong ScanRunner o thread nen roi rerun de vao man hinh tien do.

    Phai chay o thread nen thi nut Dung moi bam duoc: neu quet ngay trong lan
    chay script nay, Streamlit bi chan cho den khi quet xong.
    """
    _reset_comparison()  # ket qua cu khong con hop le tu luc bat dau quet lai
    state = ScanState(use_md5=use_md5)
    runner = ScanRunner(
        creds=creds,
        seagate_root=config.SEAGATE_PATH,
        exclude_patterns=_current_excludes(),
        drive_root_path=_drive_root(),
        use_md5=use_md5,
        state=state,
    )
    runner.start()
    st.session_state["scan_state"] = state
    st.session_state["scan_runner"] = runner
    st.rerun()


def _render_scan_progress() -> None:
    state: ScanState = st.session_state["scan_state"]
    snap = state.snapshot()

    # Xong & thanh cong -> nap ket qua vao session roi ve man hinh ket qua.
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
        st.write(
            f"{icon} ☁️ **Google Drive** — {snap['drive_files']:,} tệp · "
            f"{snap['drive_folders']:,} thư mục"
        )

    if snap["phase"] == PHASE_COMPARE:
        st.write("⏳ 🔍 **So sánh**")
        if snap["use_md5"] and snap["hash_total"]:
            frac = min(snap["hash_done"] / snap["hash_total"], 1.0)
            st.progress(
                frac,
                text=f"MD5 {frac * 100:.0f}% "
                f"({human_size(snap['hash_done'])}/{human_size(snap['hash_total'])}) · "
                f"{snap['hash_current']}",
            )

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

    # Da dung hoac loi -----------------------------------------------------
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

    # Bang chi tiet co bo loc.
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
# Tab 2 — Dong bo
# --------------------------------------------------------------------------- #
def render_sync_tab(creds) -> None:
    st.header("Đồng bộ")

    # Neu dang chay (hoac vua xong) thi hien tien do, khong cho cau hinh moi.
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

    # Xac nhan mirror: bat buoc go XOA.
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

    if snap["current"]:
        line = f"Đang xử lý: `{snap['current']}`"
        if snap["current_frac"] is not None:
            line += f" — {snap['current_frac'] * 100:.0f}%"
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

    # Da xong -----------------------------------------------------------------
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
# Tab 3 — Lich su
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
# Tab 4 — Huong dan
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
- Bấm **Quét & So sánh** để đối chiếu ổ Seagate với Google Drive theo đường dẫn.
- Mặc định so khớp **kích thước**. Bật **Đối chiếu MD5** để chắc chắn nội dung
  giống hệt (chậm hơn).
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
    _handle_oauth_callback()  # xu ly ?code=... khi Google redirect ve

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
