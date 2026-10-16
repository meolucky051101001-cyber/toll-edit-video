# AutoDub Video Tools (Tool V1 Production)

> [!IMPORTANT]
> **Tool V1 là hệ thống chính thức (OFFICIAL PRODUCTION):** Công cụ ổn định, hoàn chỉnh, tích hợp đầy đủ quy trình tự động 8 bước lồng tiếng/dịch thuật AI trên card NVIDIA GPU và phục vụ sản xuất thực tế qua Telegram Bot và Dashboard Monitor.

---

## Mục lục

1. [Tổng quan hệ thống](#1-tổng-quan-hệ-thống)
2. [Quy trình xử lý tự động của Tool V1](#2-quy-trình-xử-lý-tự-động-của-tool-v1)
3. [Danh mục Model AI & Cấu hình Runtime](#3-danh-mục-model-ai--cấu-hình-runtime)
4. [Cài đặt & Khởi động Tool V1](#4-cài-đặt--khởi-động-tool-v1)
5. [Cổng dịch vụ & Giao diện điều khiển](#5-cổng-dịch-vụ--giao-diện-điều-khiển)
6. [Kiểm thử & Đảm bảo chất lượng](#6-kiểm-thử--đảm-bảo-chất-lượng)
7. [Bảo mật & Quản lý dữ liệu](#7-bảo-mật--quản-lý-dữ-liệu)

---

## 1. Tổng quan hệ thống

| Tên công cụ / Nhánh | Trạng thái | Cổng truy cập | Vai trò chính |
| --- | --- | --- | --- |
| **Tool V1 (Production)**<br>[`tool-v1`](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-v1) / [`main`](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/main) | **ĐANG SỬ DỤNG (Chính thức)** | Dashboard: `http://127.0.0.1:8088`<br>Tool Control: `http://127.0.0.1:8090` | Hệ thống lồng tiếng & dịch video tự động 8 bước trọn gói, tích hợp Telegram bot, Dashboard theo dõi real-time, GPU BS-RoFormer, Whisper Turbo & Gemini AI failover. |
| **AI Video Research Tool**<br>[`tool-tim-kiem-video`](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-tim-kiem-video) | **Độc lập** | Web: `http://localhost:3000`<br>API: `http://localhost:8000` | Ứng dụng nghiên cứu và thu thập video độc lập (cào dữ liệu Douyin/Xiaohongshu); không dùng chung quy trình với Tool V1. |

---

## 2. Quy trình xử lý tự động của Tool V1

Tool V1 vận hành một quy trình khép kín gồm 8 công đoạn liên hoàn, tối ưu hóa tốc độ và chất lượng:

1. **Tiếp nhận & Chuẩn bị:** Tự động nhận link video (Douyin, TikTok, Xiaohongshu) qua Telegram Bot hoặc file video đưa vào hàng đợi; trích xuất audio stream chuẩn WAV 44.1kHz.
2. **Tách nguồn âm thanh (Vocal Separation):** Sử dụng **BS-RoFormer (SDR 12.97 dB)** chạy trực tiếp trên GPU (CUDA 0) thông qua môi trường cách ly `backend/model_venv_separator` để tách sạch giọng gốc và nhạc nền (BGM).
3. **Nhận dạng lời nói (ASR):** Sử dụng **Whisper Large-v3 Turbo** để bóc tách lời thoại và sinh timestamp chính xác từng mili-giây.
4. **Nhận diện phụ đề gốc (OCR):** Quét khung hình bằng **PP-OCRv6** với thuật toán Smart Skip và cân bằng hình học phụ đề (OCR Geometry auto-heal) để xác định vị trí và thời gian phụ đề gốc cần che.
5. **Dịch thuật AI theo ngữ cảnh:** Động cơ dịch đa kênh với cơ chế **Gemini Multi-key Failover Pool** (tự động đảo key dự phòng khi chạm quota/rate-limit), kết hợp prompt dịch tự nhiên, chuẩn văn phong Việt Nam.
6. **Lồng tiếng & Căn nhịp đọc (TTS & Timing):** Tổng hợp giọng đọc tự nhiên (CapCut TTS đa luồng 6 worker chất lượng cao, Vie-TTS / VieNeu 14 giọng Bắc/Trung/Nam, Edge TTS dự phòng; loại bỏ hoàn toàn phụ thuộc model RVC nặng nề), kết hợp bộ giải thuật **Timing Solver & Speed Limiter (tối đa 1.45x)** chống méo tiếng hoặc nuốt chữ.
7. **Hòa âm thông minh (Audio Mixing):** Bộ trộn **Hierarchical Soft Ducking** tự động hạ nhạc nền khi có lời nói, nâng nhạc nền ở khoảng lặng và chuẩn hóa âm lượng theo chuẩn quốc tế **EBU R128 (-15 LUFS)**.
8. **Render video tăng tốc phần cứng:** Xuất video chất lượng cao thông qua **FFmpeg NVENC H.264** (NVIDIA Hardware Encoder), che phụ đề cũ mượt mà và nhúng phụ đề mới.

---

## 3. Danh mục Model AI & Cấu hình Runtime

Tool V1 quản lý các model tập trung qua `backend/v1_model_registry.py` và `backend/ai/v1_model_policy.py`:

| Công đoạn | Model mặc định | Cơ chế tăng tốc / Runtime | Fallback / Dự phòng |
| --- | --- | --- | --- |
| **Separation** | BS-RoFormer Ep317 (SDR 12.97dB) | CUDA:0 (`backend/model_venv_separator`) | Demucs v4 (htdemucs) |
| **ASR** | Whisper Large-v3 Turbo | CUDA:0 (Faster-Whisper FP16) | Standard Whisper / Fallback Cache |
| **OCR** | PP-OCRv6 (ONNX Runtime) | ONNX Runtime GPU/DirectML | Smart Skip / Reuse visual bbox |
| **Dịch thuật** | Gemini Flash (Multi-key Pool) | HTTP REST đa key + cache phiên | Secondary API keys / Tiered fallback |
| **TTS** | CapCut TTS (6-workers) & Vie-TTS (VieNeu 14 giọng) | Async streaming pooling & fallback tự động | Edge-TTS (4-workers) |
| **Render** | FFmpeg NVENC H.264 | NVIDIA NVENC Hardware Video Encoder | libx264 CPU encode |

---

## 4. Cài đặt & Khởi động Tool V1

### 4.1. Yêu cầu hệ thống
- Hệ điều hành: Windows 10/11 64-bit.
- GPU: NVIDIA GeForce (Khuyến nghị từ RTX 3050 trở lên, tối thiểu 4GB-6GB VRAM, đã cài CUDA Driver).
- Phần mềm: Python 3.10, FFmpeg và ffprobe đã có trong biến môi trường `PATH`.

### 4.2. Khởi động dịch vụ
Tại thư mục gốc của Tool V1:

- **Khởi động chạy ngầm toàn bộ hệ thống (Khuyến nghị):**
  Chạy file `KhoiDongAn.vbs` bằng cách nhấp đúp chuột.
  File này sẽ tự động khởi động:
  - Telegram Bot V1 (lắng nghe và xử lý job).
  - Dashboard Web Server (port 8088).
  - Tool Control Server (port 8090).
- **Khởi động trực tiếp có cửa sổ console (Debug / Kiểm tra log):**
  Chạy `start_bot.bat`.
- **Dừng toàn bộ hệ thống:**
  Chạy `stop_bot.bat` hoặc `TatHoanToan.bat` để tắt sạch bot, dashboard, các tiến trình Python và FFmpeg đang chạy ngầm.

---

## 5. Cổng dịch vụ & Giao diện điều khiển

- **Bảng điều khiển chính (Dashboard V1):** `http://127.0.0.1:8088/`
  - Theo dõi tiến độ 8 bước xử lý theo thời gian thực.
  - Xem danh sách video đầu vào, hàng đợi và thành phẩm.
  - Trình phát video trước/sau khi dịch, log hệ thống, công tắc nhận dạng giọng tự động.
  - Studio Quy Trình: `http://127.0.0.1:8088/quy-trinh`
  - Studio Kịch Bản: `http://127.0.0.1:8088/kich-ban`
- **Trung tâm điều khiển (Tool Control):** `http://127.0.0.1:8090/`
  - Quản lý trạng thái bot, dashboard, heartbeat, tiến trình xử lý và hàng đợi.

---

## 6. Kiểm thử & Đảm bảo chất lượng

Tool V1 đi kèm bộ kiểm thử tự động toàn diện:
~~~powershell
# Chạy bộ kiểm thử nghiệm thu sản xuất
.\backend\venv\Scripts\python.exe -m pytest tests/test_verify_production_readiness.py tests/test_v1_quality_gate.py
~~~
- Trạng thái kiểm tra sẵn sàng BS-RoFormer tách âm: `CUDA:0, SDR 12.97 dB (Ready: True)`.
- Tất cả kiểm thử nghiệm thu đều vượt qua (12/12 passed).

---

## 7. Bảo mật & Quản lý dữ liệu

- **Không commit secrets:** Tuyệt đối không commit file `backend/.env`, chứa `BOT_TOKEN`, `GEMINI_API_KEY`, cookie mạng xã hội hoặc API keys cá nhân.
- **Thư mục làm việc:** Dữ liệu runtime (`workspace/`, thư mục video, cache âm thanh, log) được loại trừ trong `.gitignore`.
- **Dữ liệu model nặng:** Trọng số model và môi trường ảo (`backend/venv/`, `backend/model_venv_separator/`) được lưu trữ cục bộ, không đẩy lên Git.
