"""Cong dang nhap don gian bang mat khau (APP_PASSWORD).

Streamlit khong co xac thuc tich hop, nen lop nay chan toan bo giao dien
cho toi khi nguoi dung nhap dung mat khau. So sanh bang hmac.compare_digest
de chong timing attack. Voi trien khai ra Internet, hay dat them reverse
proxy (HTTPS + basic auth) phia truoc — xem README.
"""
from __future__ import annotations

import hmac
import time

import streamlit as st

import config


def require_login() -> None:
    """Dung toan bo app (st.stop) neu chua dang nhap."""
    password = config.APP_PASSWORD

    if not password:
        st.sidebar.warning(
            "⚠️ Chưa đặt `APP_PASSWORD` trong file `.env` — "
            "giao diện đang mở cho mọi người truy cập được cổng này."
        )
        return

    if st.session_state.get("auth_ok"):
        with st.sidebar:
            if st.button("🚪 Đăng xuất", width="stretch"):
                st.session_state["auth_ok"] = False
                st.rerun()
        return

    st.title("🔐 Seagate ⇄ Google Drive Sync")
    st.caption("Nhập mật khẩu (biến `APP_PASSWORD` trong file `.env`) để tiếp tục.")
    with st.form("login_form"):
        entered = st.text_input("Mật khẩu", type="password")
        submitted = st.form_submit_button("Đăng nhập", type="primary")
    if submitted:
        if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            time.sleep(1.0)  # lam cham brute-force
            st.error("Sai mật khẩu.")
    st.stop()
