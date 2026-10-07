# TÀI LIỆU BÀN GIAO KỸ THUẬT: HỆ THỐNG GIÁM SÁT & BẢNG ĐIỀU KHIỂN (TOOL V1)
> **Dành cho:** Codex / Developer tiếp quản dự án  
> **Cập nhật:** 2026-09-07  
> **Động cơ cốt lõi:** Tool V1 (Pipeline Mode: Legacy)  
> **Trạng thái:** Đang hoạt động ổn định trên cổng `8088` (130/130 tests PASS).

---

## 1. TỔNG QUAN KIẾN TRÚC (ARCHITECTURE OVERVIEW)

Hệ thống giám sát (Dashboard Monitor) của Tool V1 được thiết kế theo cơ chế **Centralized State Tracker (Bộ theo dõi trạng thái tập trung)** kết hợp **REST API + Web UI SPA**.

```
                         ┌──────────────────────────────────────────────┐
                         │               CÁC NGUỒN CHẠY                 │
                         │  - Dashboard Web ([🚀 Chạy Ngay])            │
                         │  - Desktop Shortcut (Chay_Video_Phoi.bat)    │
                         │  - Telegram Bot (/batch)                     │
                         └──────────────────────┬───────────────────────┘
                                                │
                                                ▼
                         ┌──────────────────────────────────────────────┐
                         │   backend/batch_processor.py                 │
                         │   (Quy trình 6 bước AI Dubbing NVENC GPU)     │
                         └──────────────────────┬───────────────────────┘
                                                │
                                   Cập nhật từng bước (%)
                                                ▼
                         ┌──────────────────────────────────────────────┐
                         │   backend/job_tracker.py                     │
                         │   Thread-safe State & workspace/job_status.json
                         └──────────────────────┬───────────────────────┘
                                                │
                                 Đọc trạng thái qua REST API
                                                ▼
                         ┌──────────────────────────────────────────────┐
                         │   backend/main.py (FastAPI Port 8088)        │
                         │   /api/status, /api/phoi, /api/banve, /logs  │
                         └──────────────────────┬───────────────────────┘
                                                │
                                    Polling mỗi 1.5s - 4s
                                                ▼
                         ┌──────────────────────────────────────────────┐
                         │   Giao diện Dashboard (dashboard.html)       │
                         │   Dark Mode, Real-time %, Video Preview      │
                         └──────────────────────────────────────────────┘
```

---

## 2. BẢN ĐỒ CÁC FILE CHÍNH (FILE MAP & LOCATIONS)

### 2.1. Backend & Tracking
- **`C:\tool v1\backend\job_tracker.py`**  
  Module quản lý trạng thái tiến độ thời gian thực.
  - Lưu cache bộ nhớ và ghi đĩa ra `C:\tool v1\workspace\job_status.json`.
  - Các hàm chính: `get_status()`, `start_batch()`, `start_video()`, `update_step()`, `finish_video()`, `finish_batch()`, `set_error()`, `request_stop()`.
- **`C:\tool v1\backend\batch_processor.py`**  
  Động cơ xử lý hàng loạt video cục bộ.
  - Đã được tích hợp gọi `job_tracker.update_step(...)` xuyên suốt 6 bước.
  - Tự động bỏ qua video đã có thành phẩm trong `D:\banve` và dọn dẹp workspace ổ C khi render xong.
- **`C:\tool v1\backend\main.py`**  
  Máy chủ FastAPI phục vụ Dashboard và các API endpoint.
  - Chạy trên cổng **`8088`** (để không xung đột với cổng `8000` của `ai-video-research-tool`).
  - Điểm vào: `http://127.0.0.1:8088`.
- **`C:\tool v1\backend\shared_state.py`**  
  Chứa cờ `stop_requested = False` dùng để ngắt khẩn cấp tiến trình render khi nhận lệnh từ Dashboard hoặc Telegram `/stop`.

### 2.2. Giao diện (Frontend Dashboard)
- **`C:\tool v1\backend\templates\dashboard.html`**  
  Giao diện Dashboard Web SPA (Single Page Application):
  - Dark mode glassmorphic UI, responsive.
  - Tự động cập nhật:
    - `/api/status` mỗi 1.5s
    - `/api/phoi` mỗi 4.0s
    - `/api/banve` mỗi 5.0s
    - `/api/logs` mỗi 2.5s
  - Trình phát video popup (Video Modal Player) hỗ trợ tua trực tiếp (`/api/stream/{folder}/{filename}`).
  - Live terminal console có tùy chọn tự cuộn (Auto-scroll) và tô màu cú pháp (Syntax Highlight).

### 2.3. Lối tắt Desktop (Desktop Launchers)
- **`C:\Users\admin\OneDrive\Desktop\Giao_Dien_Quan_Sat_Tool.bat`**  
  File batch khởi động máy chủ (nếu chưa chạy) và mở giao diện dưới dạng **Chrome App Window** độc lập. Cú pháp chuẩn ASCII, không lỗi ngoặc hay ký tự tiếng Việt.
- **`C:\Users\admin\OneDrive\Desktop\Bang_Dieu_Khien_Tool_V1.url`**  
  Lối tắt web chuẩn của Windows. Nhấp đúp mở thẳng `http://127.0.0.1:8088/` trên trình duyệt mặc định.
- **`C:\Users\admin\OneDrive\Desktop\Chay_Video_Phoi.bat`**  
  Chạy batch xử lý toàn bộ video trong `D:\video phôi`.

### 2.4. Kiểm thử tự động (Unit Tests)
- **`C:\tool v1\tests\test_dashboard_and_tracker.py`**  
  Test toàn bộ vòng đời của `job_tracker` và các endpoint `/api/status`, `/api/phoi`, `/api/banve`, `/`.

---

## 3. DANH SÁCH REST API ENDPOINTS

Tất cả API chạy trên `http://127.0.0.1:8088`:

| Phương thức | Endpoint | Chức năng | Dữ liệu trả về |
|---|---|---|---|
| `GET` | `/` | Trả về giao diện Dashboard HTML | HTML Document |
| `GET` | `/api/status` | Lấy tiến độ thời gian thực của tác vụ hiện tại | JSON (`active`, `step`, `percent`, `elapsed_seconds`, `eta_seconds`, `video_name`, ...) |
| `GET` | `/api/phoi` | Danh sách video phôi trong `D:\video phôi` | JSON (`files`, `total_count`, `total_size_mb`) |
| `GET` | `/api/banve` | Danh sách video thành phẩm trong `D:\banve` | JSON (`files`, `total_count`, `total_size_mb`) |
| `GET` | `/api/logs` | 100 dòng nhật ký mới nhất từ `app.log` | JSON (`logs`: chuỗi text log) |
| `POST` | `/api/run-batch` | Bắt đầu chạy ngầm hàng đợi video phôi | JSON (`status`: "started" / "busy", `message`) |
| `POST` | `/api/stop-batch` | Dừng tiến trình batch đang chạy | JSON (`status`: "stopping", `message`) |
| `POST` | `/api/open-folder` | Mở thư mục trên Explorer (`folder`: "phoi" \| "banve") | JSON (`status`: "ok", `path`) |
| `GET` | `/api/stream/{folder}/{filename}` | Stream video để xem trước trực tiếp trên web | Video/mp4 FileResponse (có hỗ trợ Range header) |

---

## 4. QUY ĐỔI BƯỚC XỬ LÝ SANG TIẾN ĐỘ (%)

| Bước | Tên bước | % Tiến độ | Công nghệ thực thi |
|---|---|---|---|
| Bắt đầu | Khởi tạo job | 5% | Tạo thư mục tạm workspace |
| **Bước 1/6** | Trích xuất âm thanh gốc | **10%** | FFmpeg extract WAV 44.1kHz |
| **Bước 2/6** | Demucs tách giọng & giữ nhạc nền | **25%** | `htdemucs` Fast multi-threading |
| **Bước 3/6** | Faster-Whisper nhận dạng giọng nói | **40%** | Faster-Whisper `large-v3-turbo` / `large-v3` |
| **Bước 3.5/6** | Quét vị trí phụ đề gốc | **55%** | `PP-OCRv6 Tiny` tracking subtitle band |
| **Bước 4/6** | Gemini Dịch phụ đề | **70%** | Gemini 3.8 Flash (Fallback 3.7/3.6/Google Translate) |
| **Bước 5/6** | Lồng tiếng AI | **85%** | Model RVC Chí Mai / Edge-TTS Hoài My |
| **Bước 6/6** | Render video thành phẩm | **95%** | FFmpeg NVIDIA RTX 4050 Hardware (`h264_nvenc`) |
| **Hoàn tất** | Lưu thành phẩm ra `D:\banve` | **100%** | Di chuyển file, dọn dẹp workspace |

---

## 5. CẤU HÌNH & BIẾN MÔI TRƯỜNG (ENVIRONMENT VARIABLES)

Các biến cấu hình trong `C:\tool v1\backend\.env`:
- `PIPELINE_MODE=legacy`: **Bắt buộc** để giữ Tool V1 hoạt động, không bật Tool V2.
- `AUTODUB_PORT=8088`: Cổng mạng của Dashboard và API.
- `AUTODUB_INPUT_DIR=D:\video phôi`: Thư mục chứa video gốc đầu vào.
- `AUTODUB_OUTPUT_DIR=D:\banve`: Thư mục xuất video lồng tiếng thành phẩm.
- `AUTODUB_WORKSPACE=C:\tool v1\workspace`: Thư mục tạm trong quá trình render (được tự động xóa sau mỗi video để bảo vệ dung lượng ổ C).
- `GEMINI_API_KEY`: API Key dùng cho bước 4 (Dịch thuật).

---

## 6. LỆNH VẬN HÀNH & KIỂM THỬ (CLI / RUN COMMANDS)

1. **Chạy máy chủ Dashboard thủ công (Debug):**
   ```powershell
   cd "C:\tool v1\backend"
   & ".\venv\Scripts\python.exe" main.py
   ```

2. **Chạy toàn bộ Test Suite (130 tests):**
   ```powershell
   cd "C:\tool v1"
   & ".\backend\venv\Scripts\python.exe" -m unittest discover -s tests -p "test_*.py"
   ```

3. **Chạy riêng Test Dashboard & Tracker:**
   ```powershell
   cd "C:\tool v1"
   & ".\backend\venv\Scripts\python.exe" -m unittest tests/test_dashboard_and_tracker.py
   ```

4. **Kiểm tra tiến trình đang chiếm cổng 8088:**
   ```powershell
   Get-NetTCPConnection -LocalPort 8088 -ErrorAction SilentlyContinue
   ```

---

## 7. GỢI Ý MỞ RỘNG CHO CODEX (NEXT STEPS / EXTENSIONS)

1. **WebSocket / SSE thay cho Polling:**
   - Hiện tại Dashboard đang dùng `setInterval` fetch polling (1.5s - 4s), rất ổn định và nhẹ máy.
   - Nếu muốn độ trễ tính bằng mili-giây, có thể thêm endpoint `@app.websocket("/ws/status")` trong `main.py` để push trực tiếp mỗi khi `job_tracker.update_step` được gọi.
2. **Kéo - Thả file trực tiếp (Drag & Drop Upload):**
   - Có thể thêm vùng thả file trên `dashboard.html` gửi `POST /api/upload_phoi` để copy file trực tiếp vào `D:\video phôi` ngay từ trình duyệt.
3. **Cấu hình tùy chọn lồng tiếng từ Dashboard:**
   - Thêm dropdown trên Dashboard để người dùng chọn: Giọng RVC Chí Mai / Giọng Nam / Giọng Nữ Edge-TTS trước khi bấm nút `[🚀 Bắt Đầu Xử Lý Video Phôi]`.
