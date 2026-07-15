"""Simple password login gate (APP_PASSWORD).

Streamlit has no built-in authentication, so this layer blocks the whole UI
until the user enters the correct password. Comparison uses
hmac.compare_digest to resist timing attacks. For Internet-facing deployments
put a reverse proxy (HTTPS + basic auth) in front — see README.
"""
from __future__ import annotations

import hmac
import time

import streamlit as st

import config


def require_login() -> None:
    """Stop the whole app (st.stop) until the user is logged in."""
    password = config.APP_PASSWORD

    if not password:
        st.sidebar.warning(
            "⚠️ Chưa đặt `APP_PASSWORD` trong file `.env` — "
            "giao diện đang mở cho mọi người truy cập được cổng này."
        )
        return

    if st.session_state.get("auth_ok"):
        with st.sidebar:
            if st.button("🚪 Đăng xuất (thoát app)", use_container_width=True):
                st.session_state["auth_ok"] = False
                st.rerun()
        return

    st.title("🔐 Seagate ⇄ Google Drive Sync")
    with st.form("login_form"):
        entered = st.text_input("Mật khẩu", type="password")
        submitted = st.form_submit_button("Đăng nhập", type="primary")
    if submitted:
        if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            time.sleep(1.0)  # slow down brute-force attempts
            st.error("Sai mật khẩu.")
    st.stop()
