# Đồng bộ ổ Seagate ⇄ Google Drive

Ứng dụng web (Streamlit) để **so sánh và đồng bộ** file giữa một ổ cứng gắn ngoài
**Seagate** (mount cục bộ) và **Google Drive** (My Drive). Triển khai bằng Docker
Compose với cấu hình bảo mật chặt. Giao diện tiếng Việt; mã nguồn tiếng Anh.

## Tính năng

- **Quét** cả hai bên và **so sánh** theo đường dẫn tương đối (cùng đường dẫn +
  cùng kích thước = giống nhau) — có tiến độ trực tiếp và nút **Dừng quét** bất
  cứ lúc nào. Từ lần quét thứ hai, phía Drive chỉ hỏi **những gì thay đổi**
  (Changes API) nên chỉ mất vài giây.
- **Lập kế hoạch** đồng bộ: lên / xuống / hai chiều, chính sách xử lý xung đột,
  tùy chọn **mirror** (xóa file thừa ở bên đích).
- **Chạy nền** với tiến độ trực tiếp: số file, dung lượng, tốc độ, ETA, nút **Hủy**.
- **Lịch sử** mọi phiên đồng bộ lưu trong SQLite, xuất được CSV.
- **An toàn**: xóa luôn khôi phục được (Drive → Thùng rác, Seagate → `.sync_trash/`),
  không bao giờ xóa vĩnh viễn.

## Yêu cầu

- Docker + Docker Compose (khuyến nghị), hoặc Python 3.12 để chạy dev.
- Một ổ Seagate đã được mount trên máy chủ.
- **OAuth client** của Google (loại **Desktop app**) — xem bên dưới.

## 1. Tạo OAuth client trên Google Cloud

1. Vào [Google Cloud Console](https://console.cloud.google.com/) → tạo project.
2. **APIs & Services → Library** → bật **Google Drive API**.
3. **APIs & Services → OAuth consent screen**: chọn *External*, điền thông tin cơ
   bản, thêm chính email của bạn vào **Test users**.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID** →
   Application type **Desktop app** → **Download JSON**.
5. Đổi tên file tải về thành `credentials.json` và đặt vào thư mục `secrets/`.

> Phạm vi (scope) sử dụng đúng một quyền: `https://www.googleapis.com/auth/drive`.

## 2. Chạy bằng Docker (khuyến nghị)

```bash
cp .env.example .env          # rồi sửa SEAGATE_MOUNT và APP_PASSWORD
docker compose up -d --build
docker compose logs -f        # mở http://localhost:8501
```

Lần đầu vào giao diện, ở thanh bên trái bấm **Bắt đầu kết nối** để liên kết Google
Drive (xem mục *Kết nối Drive* bên dưới).

## 3. Chạy dev (không cần Docker)

```bash
pip install -r requirements.txt
SEAGATE_PATH=/đường/dẫn/tới/seagate streamlit run app/main.py
```

Tùy chọn: tạo `token.json` sẵn bằng trình duyệt trên máy (thay vì luồng dán URL):

```bash
python scripts/authorize.py
```

## Kết nối Drive (luồng dán URL trong Docker)

Container không có trình duyệt, nên dùng luồng "dán URL":

1. Bấm **Bắt đầu kết nối** → mở link **cấp quyền Google** → **Cho phép**.
2. Trình duyệt sẽ báo *không mở được localhost:8090* — **điều này bình thường**.
3. Copy **toàn bộ URL** trên thanh địa chỉ (chứa `?code=...`) → dán vào ô trong
   app → **Hoàn tất kết nối**. Token lưu vào `secrets/token.json` và tự làm mới.

## Cách dùng

1. **So sánh** — bấm *Quét & So sánh*. Lần đầu quét đầy đủ; các lần sau tự quét
   nhanh (⚡ chỉ hỏi thay đổi). Nghi kết quả lệch thì bấm *🔄 Quét lại toàn bộ*.
   Muốn ngừng giữa chừng thì bấm *⛔ Dừng quét* (quét chỉ đọc nên dừng lúc nào
   cũng an toàn; sẽ không có kết quả so sánh, cần quét lại).
2. **Đồng bộ** — chọn hướng + cách xử lý xung đột → *Lập kế hoạch* để xem trước →
   *Bắt đầu đồng bộ*. Với **mirror** phải gõ `XOA` để xác nhận.
3. **Lịch sử** — xem lại các phiên đã chạy, tải CSV.

> ⚠️ **Không sửa đổi hai bên** trong lúc đang đồng bộ.

## Biến môi trường

| Biến                | Mặc định        | Ý nghĩa                                                        |
| ------------------- | --------------- | ------------------------------------------------------------- |
| `SEAGATE_MOUNT`     | *(bắt buộc)*    | Đường dẫn ổ Seagate trên host, mount vào `/data/seagate`      |
| `SEAGATE_PATH`      | `/data/seagate` | Đường dẫn app quét (trong container / khi chạy dev)           |
| `APP_PASSWORD`      | *(trống)*       | Mật khẩu đăng nhập giao diện (để trống sẽ cảnh báo)           |
| `DRIVE_ROOT_FOLDER` | `root`          | Thư mục Drive để đối chiếu (`root` = toàn bộ My Drive)        |
| `SECRETS_DIR`       | `./secrets`     | Nơi chứa `credentials.json` + `token.json`                    |
| `DATA_DIR`          | `./data`        | Nơi chứa `sync_history.db`                                    |
| `TZ`                | —               | Múi giờ, ví dụ `Asia/Ho_Chi_Minh`                            |

## An toàn dữ liệu

- **Không xóa vĩnh viễn**: Drive → **Thùng rác**; Seagate → `.sync_trash/<thời-điểm>/`.
- **Mirror** chỉ dùng cho đồng bộ một chiều và phải gõ `XOA` để xác nhận.
- `mtime` được giữ nguyên hai chiều nên "bên mới hơn thắng" đáng tin cậy.
- File Google (Docs/Sheets/Slides) không có kích thước → luôn được **bỏ qua**.

## Bảo mật triển khai

- Cổng chỉ mở trên `127.0.0.1:8501`. Muốn truy cập từ xa, đặt **reverse proxy**
  (HTTPS + xác thực) phía trước, **không** đổi sang `0.0.0.0`.
- Container chạy **non-root** (UID 1000), rootfs **read-only** + tmpfs `/tmp`,
  `cap_drop: [ALL]`, `no-new-privileges`.
- Không log token/nội dung thông tin xác thực.

## Kiểm thử

```bash
python tests/test_logic.py
```

## Cấu trúc thư mục

```
app/
  main.py            # Giao diện Streamlit (4 tab)
  config.py          # Cấu hình từ biến môi trường
  security.py        # Cổng đăng nhập APP_PASSWORD
  utils.py           # Định dạng kích thước/tốc độ/thời gian
  services/          # Logic thuần Python (không import Streamlit)
scripts/authorize.py # Tạo token.json bằng trình duyệt (tùy chọn)
tests/test_logic.py  # Kiểm thử bằng assert (không cần pytest)
secrets/             # credentials.json + token.json (gitignored)
data/                # sync_history.db (gitignored)
```

## Xử lý sự cố

- **Thiếu `credentials.json`** → tải OAuth client (Desktop app) và đặt vào `secrets/`.
- **Không thấy ổ Seagate** → kiểm tra `SEAGATE_MOUNT` trong `.env` và ổ đã mount chưa.
- **`access_denied` khi cấp quyền** → thêm email của bạn vào *Test users* trong
  OAuth consent screen.
- **Token hết hạn / lỗi refresh** → *Ngắt kết nối Drive* rồi kết nối lại.
