# Bắt đầu nhanh với Tool V1

Tài liệu hướng dẫn nhanh cho người vận hành hệ thống lồng tiếng & dịch video tự động Tool V1. Chi tiết kiến trúc và danh mục model nằm trong [README.md](README.md).

> [!NOTE]
> **Tool V2 đã ngừng sử dụng:** Nhánh `tool-v2` và toàn bộ dịch vụ V2 đã dừng phát triển. **Tool V1** là hệ thống chuẩn production đang hoạt động hàng ngày.

---

## 1. Khởi động hệ thống

- **Chạy ngầm (Khuyến nghị hàng ngày):**
  Nhấp đúp chuột vào file `KhoiDongAn.vbs`.
  Hệ thống sẽ chạy ngầm cả 3 thành phần: Telegram Bot, Dashboard V1 (port 8088) và Tool Control (port 8090).
- **Chạy trực tiếp có console:**
  Chạy `start_bot.bat` để xem log trực tiếp trên màn hình.
- **Dừng dịch vụ theo đúng phạm vi (Scoped Shutdown):**
  - `TatHoanToan.bat`: Dừng toàn bộ hệ thống Tool V1 (`--scope all`), bảo vệ job đang chạy (báo busy nếu chưa truyền `--force`) và hỗ trợ xem trước qua `TatHoanToan.bat --preview`.
  - `stop_bot.bat`: Chỉ dừng riêng Telegram dubbing bot (`--scope telegram`). Hỗ trợ `stop_bot.bat --preview`.
  - `stop_telebot_downloader.bat`: Chỉ dừng riêng bot tải video độc lập (`--scope downloader`). Hỗ trợ `stop_telebot_downloader.bat --preview`.

---

## 2. Truy cập giao diện

- **Dashboard V1 (Giám sát tiến độ & Video):** [http://127.0.0.1:8088/](http://127.0.0.1:8088/)
- **Trung tâm điều khiển (Tool Control):** [http://127.0.0.1:8090/](http://127.0.0.1:8090/)
- **Studio Quy Trình:** [http://127.0.0.1:8088/quy-trinh](http://127.0.0.1:8088/quy-trinh)
- **Studio Kịch Bản:** [http://127.0.0.1:8088/kich-ban](http://127.0.0.1:8088/kich-ban)

---

## 3. Cấu hình & Biến môi trường

- Cấu hình đặt tại `backend/.env` (tuyệt đối không commit file `.env` lên Git).
- Runtime Python chính: **Python 3.12.14** tại `backend/venv/Scripts/python.exe` (môi trường dự phòng rollback Python 3.10.11 đặt tại `backend/venv_py310/Scripts/python.exe`).
- Runtime tách âm BS-RoFormer: `backend/model_venv_separator/Scripts/python.exe` (sử dụng GPU CUDA:0).
- Các script lịch sử nguy hiểm (`RESTORE_SAFE_BASELINE.bat`, `rollback_to_safe_state.ps1`, `backend/enqueue_missing_batch.py`, `backend/sync_capcut_queue.py`, ...) đã được vô hiệu hóa an toàn (retired, trả mã lỗi `1`).

---

## 4. Kiểm tra sức khỏe hệ thống & Đóng gói

~~~powershell
# Kiểm tra sức khỏe chỉ đọc đầy đủ (dùng --timeout 30 để tránh FAIL/incomplete khi máy cold import vượt ngưỡng 5 giây mặc định)
.\backend\venv\Scripts\python.exe -B scripts\verify_runtime_health.py --timeout 30

# Chạy kiểm thử nhanh
.\backend\venv\Scripts\python.exe -B -m pytest tests/test_v1_shutdown_scope.py tests/test_v1_retired_entrypoints.py tests/test_runtime_health.py -q

# Kiểm tra danh sách đóng gói release (--dry-run)
.\backend\venv\Scripts\python.exe package_v1_release.py --dry-run
~~~
