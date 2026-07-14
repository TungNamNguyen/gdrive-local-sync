#!/usr/bin/env bash
#
# Ngắt kết nối Google Drive cũ và kết nối lại — TỰ MỞ TRÌNH DUYỆT, KHÔNG PHẢI DÁN URL.
#
# Dùng khi nào:
#   - Muốn đổi sang tài khoản Google khác.
#   - Token hỏng / muốn làm mới kết nối.
#
# Cách chạy (trong Terminal, từ thư mục dự án):
#   ./reconnect-drive.sh                # ngắt kết nối cũ rồi kết nối lại
#   ./reconnect-drive.sh --disconnect   # chỉ ngắt kết nối (xoá token), không kết nối lại
#
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv-tools"
PY="$VENV/bin/python"
PORT=8090

# --- 1. Tự cài công cụ nếu chưa có (chỉ lần đầu) ----------------------------
if [ ! -x "$PY" ]; then
  echo "→ Lần đầu chạy: đang cài công cụ (~1 phút)…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet \
    google-api-python-client==2.156.0 \
    google-auth==2.37.0 \
    google-auth-oauthlib==1.2.1 \
    google-auth-httplib2==0.2.0
fi

# --- 2. Ngắt kết nối cũ (xoá token) ----------------------------------------
if [ -f secrets/token.json ]; then
  rm -f secrets/token.json
  echo "✅ Đã ngắt kết nối tài khoản cũ (xoá secrets/token.json)."
else
  echo "ℹ️  Chưa có kết nối nào (không có token cũ)."
fi

# Chỉ ngắt kết nối, dừng ở đây.
if [ "${1:-}" = "--disconnect" ]; then
  echo "→ Đã ngắt kết nối. Vào http://localhost:8501 bấm F5 để thấy trạng thái 'Chưa kết nối'."
  exit 0
fi

# --- 3. Dọn tiến trình cấp quyền cũ (nếu còn treo cổng $PORT) ---------------
pkill -f "scripts/authorize.py" 2>/dev/null || true
sleep 1

# --- 4. Kết nối lại: mở trình duyệt, tự bắt code ---------------------------
echo "→ Đang mở trình duyệt để cấp quyền…"
echo "   Trong trình duyệt: chọn tài khoản → (nếu có cảnh báo) Advanced → Go to app → Allow."
echo "   Trang sẽ hiện 'The authentication flow has completed' là XONG."
echo
"$PY" scripts/authorize.py

echo
echo "🎉 Kết nối lại thành công! Giờ vào http://localhost:8501 và bấm F5."
