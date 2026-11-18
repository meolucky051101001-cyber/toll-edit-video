# AutoDub Video Tools (Tool V1 Production)

> [!IMPORTANT]
> **THÔNG BÁO VỀ TRẠNG THÁI HỆ THỐNG (Cập nhật 18/11/2026):**
> - **Tool V1 là hệ thống được duy trì chính thức (OFFICIAL PRODUCTION):** Chạy trên **Python 3.12.14** (`backend/venv`, dự phòng rollback Python 3.10.11 tại `backend/venv_py310`), tích hợp đầy đủ quy trình tự động 8 bước lồng tiếng/dịch thuật AI trên card NVIDIA GPU và đã hoàn tất đợt gia cố an toàn & dọn dẹp mã thừa C0–C9 (`1217 passed, 0 failed`).

---

## Mục lục

1. [Tổng quan & Phân định hệ thống](#1-tổng-quan--phân-định-hệ-thống)
2. [Quy trình xử lý tự động của Tool V1](#2-quy-trình-xử-lý-tự-động-của-tool-v1)
3. [Danh mục Model AI & Cấu hình Runtime](#3-danh-mục-model-ai--cấu-hình-runtime)
4. [Cài đặt & Khởi động Tool V1](#4-cài-đặt--khởi-động-tool-v1)
5. [Cổng dịch vụ & Giao diện điều khiển](#5-cổng-dịch-vụ--giao-diện-điều-khiển)
6. [Kiểm thử & Đảm bảo chất lượng](#6-kiểm-thử--đảm-bảo-chất-lượng)
7. [Bảo mật & Quản lý dữ liệu](#7-bảo-mật--quản-lý-dữ-liệu)

---

## 1. Tổng quan & Phân định hệ thống

| Tên công cụ / Nhánh | Trạng thái | Cổng truy cập | Vai trò chính |
| --- | --- | --- | --- |
| **Tool V1 (Production)**<br>[`tool-v1`](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-v1) / [`main`](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/main) | **ĐANG SỬ DỤNG (Chính thức)** | Dashboard: `http://127.0.0.1:8088`<br>Tool Control: `http://127.0.0.1:8090` | Hệ thống lồng tiếng & dịch video tự động 8 bước trọn gói, tích hợp Telegram bot, Dashboard theo dõi real-time, GPU BS-RoFormer, Whisper Turbo & Gemini AI failover. |

---

## 2. Quy trình xử lý tự động của Tool V1

Tool V1 vận hành một quy trình khép kín gồm 8 công đoạn liên hoàn, tối ưu hóa tốc độ và chất lượng:

1. **Tiếp nhận & Chuẩn bị:** Tự động nhận link video (Douyin, TikTok, Xiaohongshu) qua Telegram Bot hoặc file video đưa vào hàng đợi; trích xuất audio stream chuẩn WAV 44.1kHz.
2. **Tách nguồn âm thanh (Vocal Separation):** Sử dụng **BS-RoFormer (SDR 12.97 dB)** chạy trực tiếp trên GPU (CUDA 0) thông qua môi trường cách ly `backend/model_venv_separator` để tách sạch giọng gốc và nhạc nền (BGM).
3. **Nhận dạng lời nói (ASR):** Mặc định dùng **Hybrid Faster-Whisper Large-v3 Turbo + SenseVoice-Small (CTC 60ms)**: Whisper cho ngữ nghĩa, SenseVoice căn timestamp chính xác ~60ms và lọc tạp âm nhạc nền. Có thể chọn riêng từng job: Whisper Turbo, SenseVoice-Small hoặc Paraformer-large-zh (tự fallback về Whisper khi gặp đa ngôn ngữ).
4. **Nhận diện phụ đề gốc (OCR):** Quét khung hình bằng **PP-OCRv6 Tiny (phát hiện) + PP-OCRv5 Server (nhận dạng)** chạy ONNX CUDA, kết hợp thuật toán Smart Skip và cân bằng hình học phụ đề (OCR Geometry auto-heal) để xác định vị trí và thời gian phụ đề gốc cần che.
5. **Dịch thuật AI theo ngữ cảnh:** Động cơ dịch đa kênh với cơ chế **Gemini Multi-key Failover Pool** (tự động đảo key dự phòng khi chạm quota/rate-limit), kết hợp prompt dịch tự nhiên, chuẩn văn phong Việt Nam.
6. **Lồng tiếng & Căn nhịp đọc (TTS & Timing):** Tổng hợp giọng đọc tự nhiên (CapCut TTS đa luồng 6 worker chất lượng cao, Vie-TTS / VieNeu 14 giọng Bắc/Trung/Nam, Edge TTS dự phòng; loại bỏ hoàn toàn phụ thuộc model RVC nặng nề), kết hợp bộ giải thuật **Timing Solver & Speed Limiter (tối đa 1.45x)** chống méo tiếng hoặc nuốt chữ.
7. **Hòa âm thông minh (Audio Mixing):** Bộ trộn **Hierarchical Soft Ducking** tự động hạ nhạc nền khi có lời nói, nâng nhạc nền ở khoảng lặng và chuẩn hóa âm lượng theo chuẩn quốc tế **EBU R128 (-15 LUFS)**.
8. **Render video tăng tốc phần cứng:** Xuất video chất lượng cao thông qua **FFmpeg NVENC H.264** (NVIDIA Hardware Encoder), che phụ đề cũ mượt mà và nhúng phụ đề mới.

---

## 3. Danh mục Model AI & Cấu hình Runtime

Tool V1 quản lý các model tập trung qua `backend/v1_model_registry.py` và `backend/ai/v1_model_policy.py`:

| Công đoạn | Model mặc định | Cơ chế tăng tốc / Runtime | Fallback / Dự phòng |
| --- | --- | --- | --- |
| **Separation** | BS-RoFormer Ep317 (SDR 12.97dB) cho video ngắn (≤ 7 phút); Demucs v4 cho video dài | CUDA:0 (`backend/model_venv_separator`) | Demucs v4 (htdemucs) ↔ BS-RoFormer |
| **ASR** | Hybrid Faster-Whisper Large-v3 Turbo + SenseVoice-Small (CTC 60ms); tùy chọn: Whisper Turbo, SenseVoice-Small, Paraformer-large-zh | CUDA:0 (Faster-Whisper FP16 + SenseVoice ONNX INT8) | Whisper Large-v3 Turbo |
| **OCR** | PP-OCRv6 Tiny Det + PP-OCRv5 Server Rec (ONNX) | ONNX Runtime CUDA (GPU:0) | Smart Skip / Reuse visual bbox |
| **Dịch thuật** | Gemini Flash (Multi-key Pool) | HTTP REST đa key + cache phiên | Secondary API keys / Tiered fallback |
| **TTS** | CapCut TTS (6-workers) & Vie-TTS (VieNeu 14 giọng) | Async streaming pooling & fallback tự động | Edge-TTS (4-workers) |
| **Render** | FFmpeg NVENC H.264 | NVIDIA NVENC Hardware Video Encoder | libx264 CPU encode |

---

## 4. Cài đặt & Khởi động Tool V1

### 4.1. Yêu cầu hệ thống
- Hệ điều hành: Windows 10/11 64-bit.
- GPU: NVIDIA GeForce (Khuyến nghị từ RTX 3050 trở lên, tối thiểu 4GB-6GB VRAM, đã cài CUDA Driver).
- Phần mềm: **Python 3.12.14** là runtime chính tại `backend\venv` (bản dự phòng rollback Python 3.10.11 đặt tại `backend\venv_py310`; không còn dùng đường dẫn staging `venv_py312` sau khi đã cutover). FFmpeg và ffprobe đã có trong biến môi trường `PATH`.

### 4.2. Khởi động & Dừng dịch vụ theo đúng phạm vi (Scoped Shutdown)
Tại thư mục gốc của Tool V1:

- **Khởi động chạy ngầm toàn bộ hệ thống (Khuyến nghị):**
  Chạy file `KhoiDongAn.vbs` bằng cách nhấp đúp chuột.
  File này sẽ tự động khởi động:
  - Telegram Bot V1 (lắng nghe và xử lý job).
  - Dashboard Web Server (port 8088).
  - Tool Control Server (port 8090).
- **Khởi động trực tiếp có cửa sổ console (Debug / Kiểm tra log):**
  Chạy `start_bot.bat`.
- **Dừng dịch vụ an toàn theo đúng scope (`backend/v1_shutdown.py`):**
  Các script dừng hiện tại chỉ tác động tới các tiến trình đã xác minh thuộc project root của Tool V1 (không kill mù quáng theo tên `python.exe`/`ffmpeg.exe` toàn máy và không tin PID cũ nếu không khớp script + creation time). Mặc định nếu có job đang xử lý, lệnh sẽ báo busy (exit code `2`) trừ khi truyền `--force`:
  - `scripts\windows\TatHoanToan.bat`: Dừng toàn bộ hệ thống Tool V1 (`--scope all`: supervisor/watchdog, control, dashboard, Telegram bot và worker con). Hỗ trợ xem trước không gây tác động qua `scripts\windows\TatHoanToan.bat --preview` (hoặc `-WhatIf`).
  - `stop_bot.bat`: Chỉ dừng đúng phạm vi Telegram dubbing bot (`--scope telegram`), không tắt nhầm downloader độc lập hay ứng dụng khác. Hỗ trợ `stop_bot.bat --preview`.
  - `stop_telebot_downloader.bat`: Chỉ dừng riêng bot tải video độc lập (`--scope downloader`). Hỗ trợ `stop_telebot_downloader.bat --preview`.

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

## 6. Kiểm thử, Sức khỏe Runtime & Đóng gói Phát hành

### 6.1. Kiểm tra sức khỏe chỉ đọc (`verify_runtime_health.py`)
~~~powershell
# Kiểm tra nhanh (ngân sách mặc định 5 giây; khi máy vừa khởi động/cold import có thể báo FAIL/incomplete)
.\backend\venv\Scripts\python.exe -B scripts\verify_runtime_health.py

# Kiểm chứng đầy đủ 25/25 hạng mục preflight read-only, pip check và cổng 8088/8090 khi máy cold hoặc tải cao
.\backend\venv\Scripts\python.exe -B scripts\verify_runtime_health.py --timeout 30
~~~

### 6.2. Chạy bộ kiểm thử tự động
~~~powershell
# Chạy kiểm thử trên runtime chính Python 3.12.14
.\backend\venv\Scripts\python.exe -B -m pytest tests -q -rs -p no:cacheprovider

# Chạy kiểm thử tương thích trên môi trường dự phòng Python 3.10.11 (chạy tuần tự, không chạy song song 2 suite)
.\backend\venv_py310\Scripts\python.exe -B -m pytest tests -q -rs -p no:cacheprovider

# Chạy 11 bài kiểm thử Node cho giao diện & wrapper Dashboard
$jsFiles = Get-ChildItem -Path "tests/js" -Recurse -Filter "*.test.js" | ForEach-Object { $_.FullName }
node --test $jsFiles
~~~

### 6.3. Đóng gói phát hành & Manifest SHA256
`package_v1_release.py` và `scripts/generate_release_manifest.py` dùng chung một chính sách chọn file (ưu tiên Git tracked + allowlist rõ ràng, bao gồm source/config `tools/render_node/package.json` và `package-lock.json`, loại bỏ `node_modules`, `.env`, model nặng và dữ liệu runtime):
~~~powershell
# Kiểm tra danh sách file dự kiến đóng gói mà không ghi file (--dry-run)
.\backend\venv\Scripts\python.exe package_v1_release.py --dry-run
.\backend\venv\Scripts\python.exe scripts\generate_release_manifest.py --dry-run

# Tạo gói ZIP qua staging an toàn và xuất RELEASE_MANIFEST.sha256 + RELEASE_METADATA.json
.\backend\venv\Scripts\python.exe package_v1_release.py
.\backend\venv\Scripts\python.exe scripts\generate_release_manifest.py --test-log path\to\pytest.log
~~~

### 6.4. Các công cụ lịch sử đã vô hiệu hóa (Retired Entrypoints) & Module dùng chung
- **Đã ngừng sử dụng (Retired, exit code `1`, không có side effect khi import):** `backend/enqueue_missing_batch.py`, `backend/sync_capcut_queue.py`, `RESTORE_SAFE_BASELINE.bat`, `ROLLBACK_TRANSLATION_FIX.bat`, `RESTORE_VOICE_LOCK_BACKUP.bat`, `scripts/windows/rollback_to_safe_state.ps1`, `fixes_for_v1/apply_fix_to_v1.bat`, `fixes_for_v1/rollback_tool_v1.bat`, `scripts/setup_telegram_bot_v2.ps1`. Để khôi phục môi trường hoặc mã nguồn, chỉ sử dụng `git log` / `git revert` theo từng commit hoặc `scripts\rollback_py310.bat` khi cần đổi lại interpreter.
- **Các tiện ích CLI vẫn được giữ hỗ trợ:** `scripts/verify_runtime_health.py`, `scripts/cutover_py312.bat`, `scripts/rollback_py310.bat`, `scripts/generate_release_manifest.py`, `package_v1_release.py`, `tools/sheet_crypto.py`, `telebot_video_downloader.py`.
- **Phạm vi module `backend/pipeline_v2/`:** Gói `backend/pipeline_v2/` vẫn được giữ lại vì cung cấp các tiện ích dùng chung cho Tool V1 (`atomic_io`, `interprocess_lock`, `stage_validation`, `qc`, `cover_qc`). Không tự ý xóa `backend/pipeline_v2/`.

---

## 7. Bảo mật & Quản lý dữ liệu

- **Không commit secrets:** Tuyệt đối không commit file `backend/.env`, chứa `BOT_TOKEN`, `GEMINI_API_KEY`, cookie mạng xã hội hoặc API keys cá nhân.
- **Thư mục làm việc:** Dữ liệu runtime (`workspace/`, thư mục video, cache âm thanh, log) được loại trừ trong `.gitignore`.
- **Dữ liệu model nặng:** Trọng số model và môi trường ảo (`backend/venv/`, `backend/model_venv_separator/`) được lưu trữ cục bộ, không đẩy lên Git.

## 8. Môi trường Python 3.12 song song

Xem [hướng dẫn Phase 5](docs/phase5_python312.md) cho dependency, file chuẩn
`requirements.lock.txt`, giới hạn wheel CapCut local và quy trình cutover/rollback
thủ công. `requirements.lock` include file chuẩn để giữ tương thích lệnh cũ.
Không chạy script chuyển môi trường khi dịch vụ Tool V1 còn hoạt động.

Bản sửa tồn đọng sau Phase 6 và golden baseline trước refactor được ghi trong
[báo cáo chuẩn bị Phase 7](docs/pre_phase7_hardening.md).

Phần tách bốn hàm lớn và bằng chứng giữ nguyên hành vi được ghi trong
[báo cáo Phase 7](docs/phase7_function_refactor.md), gồm bước nghiệm thu video
ngắn do người dùng chạy trên dashboard sau khi chủ động nạp code mới.

Báo cáo trạng thái hiện hành của toàn bộ codebase và kết quả đợt dọn dẹp an toàn
C0–C9 (02/11/2026) được ghi tại [docs/PROJECT_STATE.md](docs/PROJECT_STATE.md)
và [docs/cleanup_tool_v1_20261102.md](docs/cleanup_tool_v1_20261102.md).
