#!/usr/bin/env bash
#
# Disconnect the old Google Drive account and reconnect — OPENS THE BROWSER
# AUTOMATICALLY, no URL pasting.
#
# When to use:
#   - Switching to a different Google account.
#   - Broken token / refreshing the connection.
#
# How to run (in a terminal, from the project directory):
#   ./reconnect-drive.sh                # disconnect, then reconnect
#   ./reconnect-drive.sh --disconnect   # disconnect only (delete the token)
#
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv-tools"
PY="$VENV/bin/python"
PORT=8090

# --- 1. Install the tooling on first run ------------------------------------
if [ ! -x "$PY" ]; then
  echo "-> Lan dau chay: dang cai cong cu (~1 phut)..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet \
    google-api-python-client==2.156.0 \
    google-auth==2.37.0 \
    google-auth-oauthlib==1.2.1 \
    google-auth-httplib2==0.2.0
fi

# --- 2. Disconnect the old account (delete the token) ------------------------
if [ -f secrets/token.json ]; then
  rm -f secrets/token.json
  echo "Da ngat ket noi tai khoan cu (xoa secrets/token.json)."
else
  echo "Chua co ket noi nao (khong co token cu)."
fi

# Disconnect only: stop here.
if [ "${1:-}" = "--disconnect" ]; then
  echo "-> Da ngat ket noi. Vao http://localhost:8501 bam F5 de thay trang thai 'Chua ket noi'."
  exit 0
fi

# --- 3. Clean up a stale authorize process (if port $PORT is still held) ------
pkill -f "scripts/authorize.py" 2>/dev/null || true
sleep 1

# --- 4. Reconnect: open the browser, capture the code automatically ----------
echo "-> Dang mo trinh duyet de cap quyen..."
echo "   Trong trinh duyet: chon tai khoan -> (neu co canh bao) Advanced -> Go to app -> Allow."
echo "   Trang hien 'The authentication flow has completed' la XONG."
echo
"$PY" scripts/authorize.py

echo
echo "Ket noi lai thanh cong! Gio vao http://localhost:8501 va bam F5."
