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
- **Tắt toàn bộ hệ thống:**
  Chạy `TatHoanToan.bat` hoặc `stop_bot.bat`.

---

## 2. Truy cập giao diện

- **Dashboard V1 (Giám sát tiến độ & Video):** [http://127.0.0.1:8088/](http://127.0.0.1:8088/)
- **Trung tâm điều khiển (Tool Control):** [http://127.0.0.1:8090/](http://127.0.0.1:8090/)
- **Studio Quy Trình:** [http://127.0.0.1:8088/quy-trinh](http://127.0.0.1:8088/quy-trinh)
- **Studio Kịch Bản:** [http://127.0.0.1:8088/kich-ban](http://127.0.0.1:8088/kich-ban)

---

## 3. Cấu hình & Biến môi trường

- Cấu hình đặt tại `backend/.env`.
- Runtime tách âm BS-RoFormer: `backend/model_venv_separator/Scripts/python.exe` (sử dụng GPU CUDA:0).
- Không commit file `.env` lên GitHub.

---

## 4. Kiểm tra sức khỏe hệ thống

~~~powershell
.\backend\venv\Scripts\python.exe -m pytest tests/test_verify_production_readiness.py tests/test_v1_quality_gate.py
~~~
