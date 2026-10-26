# BÀN GIAO TOÀN BỘ DỮ LIỆU & ĐƯỜNG DẪN: TOOL DASHBOARD (TOOL V1)

> **Cập nhật:** 2026-10-26 · **Thư mục gốc:** `C:\tool v1` · **Nhánh Git:** `tool-v1` (commit tham chiếu `242f21c` + bản vá vận hành 2026-10-26)

---

## 1. BẢNG ĐƯỜNG DẪN TRUY CẬP WEB (URLs)

| Tên Giao Diện | Đường Dẫn URL | Cổng | File Template HTML | Backend Phục Vụ |
| :--- | :--- | :--- | :--- | :--- |
| **1. Tool V1 Dashboard (Chính)** | `http://127.0.0.1:8088/` | `8088` | `C:\tool v1\backend\templates\dashboard.html` | `C:\tool v1\backend\main.py` |
| **2. Studio Quy Trình (CapCut Editor)** | `http://127.0.0.1:8088/quy-trinh` | `8088` | `C:\tool v1\backend\templates\workflow.html` | `C:\tool v1\backend\workflow_api.py` |
| **3. Studio Kịch Bản AI & Doodle** | `http://127.0.0.1:8088/kich-ban` | `8088` | `C:\tool v1\backend\templates\script_studio.html` | `C:\tool v1\backend\script_api.py` |
| **4. A2UI Studio (Agent UI)** | `http://127.0.0.1:8088/a2ui` | `8088` | `C:\tool v1\backend\templates\a2ui_studio.html` | `C:\tool v1\backend\main.py` |
| **5. Control Plane (Bật/Tắt Dịch Vụ)** | `http://127.0.0.1:8090/` | `8090` | *(Inline HTML trong `tool_control.py`, nhúng qua `<iframe>` trên Dashboard)* | `C:\tool v1\backend\tool_control.py` |

---

## 2. BẢNG ĐƯỜNG DẪN THƯ MỤC & DỮ LIỆU HỆ THỐNG (FILESYSTEM PATHS)

| Thành Phần | Đường Dẫn Tuyệt Đối | Ghi Chú |
| :--- | :--- | :--- |
| **Thư mục gốc dự án (`project_root`)** | `C:\tool v1` | Chứa toàn bộ mã nguồn `backend`, `docs`, `tests`, `bin`, `models` |
| **Môi trường Python (`venv`)** | `C:\tool v1\backend\venv\Scripts\python.exe` | Python 3.10 + PyTorch CUDA + FastAPI + Whisper + PaddleOCR |
| **File cấu hình môi trường (`.env`)** | `C:\tool v1\backend\.env` | Chứa `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `AUTODUB_*` *(Bảo mật)* |
| **Thư mục Video Phôi đầu vào (`input_dir`)** | `D:\workspace\downloads` *(hoặc `D:\video phôi`)* | Nơi chứa video gốc tải về hoặc người dùng chọn qua Dashboard |
| **Thư mục Video Thành Phẩm (`output_dir`)** | `D:\banve` | Nơi lưu các video đã lồng tiếng hoàn chỉnh (`Dubbed_*.mp4`) |
| **Thư mục Workspace tạm (`workspace`)** | `D:\workspace` *(và `C:\tool v1\workspace`)* | Lưu các thư mục job từng video (`original.srt`, `translated.srt`, `project.json`, RoFormer stems...) |
| **Thư mục Cache Model AI (`model_cache`)** | `C:\tool v1\models` | Lưu checkpoint BS-RoFormer, Whisper, SenseVoice, PP-OCR, VieNeu/RVC |
| **Thư mục AppData AutoDub (`data_root`)** | `C:\Users\admin\AppData\Local\AutoDub` | Lưu cấu hình người dùng, preset, lịch sử, token control plane |
| **File trạng thái tiến độ (`job_status.json`)** | `C:\tool v1\workspace\job_status.json` | Lưu trạng thái thời gian thực của 2 luồng Worker |
| **File cấu hình Mixer âm thanh** | `C:\tool v1\workspace\audio_settings.json` | Lưu BGM dB, Dubbing dB, Ducking mode, Separation engine |
| **File cấu hình Worker & VRAM** | `C:\tool v1\workspace\worker_settings.json` | Lưu số luồng (`concurrency`: 1 hoặc 2), ngưỡng bảo vệ VRAM |
| **File từ điển thuật ngữ (`Glossary`)** | `C:\tool v1\workspace\custom_glossary.json` | Lưu từ điển ép dịch cố định Trung -> Việt |
| **Nhật ký hệ thống (`Logs`)** | `C:\tool v1\backend\app.log` & `C:\tool v1\workspace\bot_system\service_logs\dashboard.log` | Log chi tiết của FastAPI server và supervisor |
| **Lối tắt ngoài Desktop** | `C:\Users\admin\OneDrive\Desktop\Giao_Dien_Quan_Sat_Tool.bat` / `Bang_Dieu_Khien_Tool_V1.url` | Mở nhanh Dashboard từ màn hình Desktop |

---

## 3. BẢN ĐỒ MÃ NGUỒN CỦA TOOL DASHBOARD (`C:\tool v1\backend`)

### 3.1. Giao diện Frontend (`backend/templates/dashboard.html` - 437 KB, ~7.450 dòng)
Giao diện Dashboard chính là một Single-Page Application (SPA) Dark Mode gồm các khu vực và 9 hộp thoại (Modals):
1. **Thanh Header & Điều hướng**:
   - Hiển thị trạng thái kết nối, nhiệt độ GPU, VRAM (`NVIDIA GeForce RTX 4050 Laptop GPU - 6GB`), chế độ tập trung (Focus Mode).
   - Các nút chuyển nhanh sang: **Tool V1** (`/`) · **Quy Trình** (`/quy-trinh`) · **Kịch Bản** (`/kich-ban`).
2. **Khung Điều Khiển Dịch Vụ (Iframe `http://127.0.0.1:8090/`)**:
   - Bật / Tắt / Khởi động lại Dashboard Server & Telegram Bot.
   - Chọn giọng đọc nhanh.
3. **Bảng Giám Sát Tiến Độ 2 Luồng (Dual-Worker Pipeline Monitor)**:
   - Theo dõi **Worker 1** và **Worker 2** chạy song song so le (VRAM Guard tự động hạ về 1 luồng nếu VRAM > 85% hoặc trống < 1.5GB).
   - Hiển thị 8 bước xử lý + thời gian từng bước (`step_durations`), % hoàn thành, ETA, nút **Tạm dừng (`Pause`)**, **Tiếp tục (`Resume`)**, **Dừng khẩn cấp (`Stop`)**.
4. **Cấu Hình Xử Lý (Collapsible Settings Panels)**:
   - **Chế độ Router (`v1ModeHeading`)**: `AUTO` (tự động phân loại Short ≤ 7 phút / Long > 7 phút), `SHORT`, `LONG`.
   - **Chọn mô hình ASR**: `SenseVoice + Whisper Hybrid` (nhanh, chuẩn tiếng Trung) hoặc `Faster-Whisper Large-v3-turbo`.
   - **Cấu hình Giọng Đọc AI (`Voice Auto` & `Dual Voice`)**: Chọn tự động nhận diện Nam/Nữ, lồng tiếng kép 2 nhân vật, nghe thử giọng trực tiếp (`Voice Preview`).
   - **Bộ trộn âm thanh (`Audio Mixer`)**: Chỉnh âm lượng nhạc nền BGM (`-2.0 dB`), giọng đọc Dubbing (`+1.0 dB`), chế độ nén nhạc (`Soft Ducking`), engine tách nhạc (`BS-RoFormer` / `Demucs`).
   - **4 Nâng cấp V1 (`Feature Flags` & `Glossary`)**: OCR Batch & Band Lock, Smart Dialogue, Custom Glossary (Từ điển riêng), Frosted Blur, Auto-Cut Silence.
5. **3 Bảng Dữ Liệu Thời Gian Thực**:
   - **Bảng Hàng Chờ (`Queue Table`)**: Danh sách video chờ xử lý, đổi thứ tự lên/xuống, xóa, chạy hàng chờ (`/api/queue/*`).
   - **Bảng Video Phôi (`Phoi Table`)**: Danh sách video trong thư mục nguồn, tải video lên (Drag & Drop / Upload), chọn nhiều video áp dụng Preset hàng loạt, mở trình sửa cấu hình riêng cho từng video.
   - **Bảng Video Thành Phẩm (`Banve Table`)**: Danh sách video đã xuất tại `D:\banve`, nút xem trước (`Video Preview Modal`), nút **Sửa Từng Câu (`Segment Editor Modal`)**, nút tải về.
6. **Bảng Nhật Ký Trực Tiếp (`Live Terminal Log Table`)**:
   - Giải thích log thông minh sang tiếng Việt (`explainLog`), lọc theo mức độ, phóng to/thu nhỏ chữ, tự cuộn, xem chi tiết từng dòng log.
7. **9 Modals chuyên dụng trong `dashboard.html`**:
   - `#presetManagerModal` (Giai đoạn A: Quản lý Preset cấu hình)
   - `#videoConfigModal` (Giai đoạn A: Cấu hình riêng cho từng video phôi)
   - `#preRunSummaryModal` (Giai đoạn A: Tóm tắt cấu hình trước khi chạy hàng loạt)
   - `#segmentEditorModal` (Giai đoạn C: Sửa bản dịch, đổi giọng, đổi tốc độ và render lại từng câu phụ đề trong video thành phẩm)
   - `#recoveryPlanModal` (Giai đoạn D: Khôi phục thông minh video bị lỗi/dừng giữa chừng từ bước đã lưu checkpoint)
   - `#videoModal` (Trình xem video trực tiếp hỗ trợ tua HTTP Range)
   - `#logDetailModal` (Xem chi tiết log lỗi/stacktrace)
   - `#preflightModal` (Kiểm tra sức khỏe hệ thống: FFmpeg, CUDA, VRAM, ổ đĩa, API Key)
   - `#taskHistoryModal` (Lịch sử các lần chạy và thống kê thời gian)

---

### 3.2. Các Module Backend Phục Vụ Dashboard
| File Backend | Chức Năng Chính |
| :--- | :--- |
| `backend/main.py` | Khởi tạo FastAPI, chứa toàn bộ API cốt lõi của Dashboard (`/`, `/api/status`, `/api/phoi`, `/api/banve`, `/api/queue/*`, `/api/vram`, `/api/worker-settings`, `/api/audio-settings`, `/api/voice-auto`, `/api/v1/*`). |
| `backend/dashboard_media.py` | Phân trang, lọc, tìm kiếm và kiểm tra trạng thái thành phẩm của danh sách video phôi & bản vẽ. |
| `backend/job_tracker.py` | Quản lý trạng thái tiến độ thread-safe cho nhiều worker (`workers["1"]`, `workers["2"]`), ghi `workspace/job_status.json`. |
| `backend/v1_orchestrator.py` | Điều phối quy trình 8 bước V1, quản lý checkpoint, retry, GPU gatekeeper, timing solver, quality gate. |
| `backend/v1_vram_monitor.py` & `worker_settings.py` | Đọc thông số GPU NVIDIA (`nvidia-smi` / `torch.cuda`), tính toán mức an toàn VRAM và tự động điều tiết số luồng worker. |
| `backend/phase_a_routes.py` (`preset_service.py`, `job_config_service.py`) | **Phase A**: Quản lý Preset, cấu hình nháp từng video (`video-draft`), chạy batch theo cấu hình riêng. |
| `backend/phase_b_routes.py` (`voice_preview_service.py`) | **Phase B**: Sinh và phát audio nghe thử cho từng giọng đọc AI có cache. |
| `backend/phase_c_routes.py` (`segment_editor_service.py`) | **Phase C**: Đọc danh sách từng câu sub của video đã hoàn thành, cho phép sửa text/giọng/tốc độ của 1 câu bất kỳ, chỉ chạy lại TTS câu đó và ghép lại video cực nhanh. |
| `backend/phase_d_routes.py` (`recovery_service.py`) | **Phase D**: Phân tích các file trung gian trong `workspace/<job_id>`, lập kế hoạch khôi phục (`recover` các bước đã xong, chỉ chạy bước thiếu) hoặc chạy lại (`restart`). |
| `backend/tool_control.py` | Máy chủ độc lập trên cổng `8090`, cho phép dừng/khởi động lại dịch vụ Dashboard và Telegram Bot ngay trên giao diện web. |
| `backend/background_service.py` | Windows Supervisor chạy ngầm: tự động bật lại `main.py` hoặc `telegram_bot.py` sau 5 giây nếu tiến trình tắt/crash. Riêng dashboard: **bỏ qua spawn nếu `http://127.0.0.1:8088/api/status` đã khỏe**, và chạy smoke test (`py_compile` + import `workflow_api`/`script_api`) trước khi spawn để không restart-loop khi code lỗi. |

---

## 4. DANH MỤC ĐẦY ĐỦ REST API CỦA DASHBOARD (`http://127.0.0.1:8088`)

### A. Giám sát & Điều khiển Batch
- `GET /` — Trả về trang HTML Dashboard chính (`dashboard.html`).
- `GET /api/auth-token` — Lấy `X-Local-Control-Token` bảo mật cho các lệnh POST/DELETE.
- `GET /api/status` — Trạng thái toàn cục: tiến độ từng worker, thông số router, mixer, separator, VRAM, lịch sử gần nhất.
- `GET /api/vram` — Thông số GPU VRAM thời gian thực (MB đã dùng, MB trống, %, nhiệt độ, khuyến nghị số luồng).
- `GET /api/logs` — Đọc log mới nhất từ `app.log`.
- `GET /api/preflight` — Kiểm tra toàn diện phần cứng, thư mục, model, FFmpeg, GPU trước khi chạy.
- `GET /api/task-history` — Lịch sử các video đã hoàn thành và thời gian xử lý từng bước.
- `POST /api/run-batch` — Chạy hàng loạt video phôi.
- `POST /api/pause-video` / `POST /api/resume-video` — Tạm dừng / Tiếp tục video đang xử lý.
- `POST /api/stop-batch` — Dừng toàn bộ batch.
- `POST /api/retry-video` — Chạy lại video bị lỗi.

### B. Quản lý Video Phôi, Thành Phẩm & Hàng Chờ
- `GET /api/phoi` — Danh sách video phôi (hỗ trợ phân trang, tìm kiếm, trạng thái draft/preset).
- `GET /api/banve` — Danh sách video thành phẩm trong `D:\banve`.
- `POST /api/choose-input-folder` / `POST /api/input-folder` — Đổi thư mục chứa video phôi.
- `POST /api/input-video` — Upload file video mới từ máy lên thư mục phôi.
- `POST /api/open-folder` — Mở thư mục `phoi` hoặc `banve` trong Windows Explorer.
- `GET /api/stream/{folder}/{filename}` — Phát trực tuyến video phôi hoặc thành phẩm (hỗ trợ tua).
- `GET /api/download/{filename}` — Tải file video thành phẩm về máy.
- `GET /api/queue` · `POST /api/queue/process` · `POST /api/queue/move` · `POST /api/queue/delete` · `POST /api/queue/clear` — Quản lý hàng đợi xử lý.

### C. Cấu hình AI, Giọng đọc, Mixer & Worker
- `GET / POST /api/worker-settings` — Chỉnh số luồng chạy song song (1 hoặc 2 worker) và bật/tắt Auto VRAM Guard.
- `GET / POST /api/audio-settings` — Chỉnh âm lượng BGM, âm lượng lồng tiếng, chế độ ducking, engine tách nhạc.
- `GET / POST /api/voice-auto` — Cấu hình tự động chọn giọng Nam/Nữ, lồng tiếng 2 nhân vật, chọn model ASR.
- `GET /api/v1/router-modes` — Chế độ định tuyến độ dài video (`AUTO`, `SHORT`, `LONG`).
- `GET / POST /api/v1/feature-flags` — Bật/tắt các tính năng nâng cao của V1 (OCR band lock, Smart condensation, Auto-cut silence, Proxy 540p...).
- `GET / POST /api/v1/glossary` — Đọc/lưu từ điển thuật ngữ dịch thuật tùy chỉnh.
- `GET /api/v1/gemini-health` — Kiểm tra tình trạng API Gemini.
- `GET /api/v1/job-manifest/{job_id}` & `/api/v1/qc-report/{job_id}` — Xem manifest và báo cáo kiểm định chất lượng (QC) của từng job.

### D. Các API Giai đoạn A, B, C, D (Preset, Nghe thử giọng, Sửa câu, Khôi phục)
- **Phase A (Presets & Drafts)**:
  - `GET / POST /api/presets` · `GET / DELETE /api/presets/{preset_id}`
  - `POST /api/config/resolve`
  - `GET /api/video-drafts` · `GET / POST / DELETE /api/video-draft/{video_name}`
  - `POST /api/jobs/batch`
- **Phase B (Voice Preview)**:
  - `GET /api/voice-preview/catalog` · `GET /api/voice-preview/sample-texts`
  - `POST /api/voice-preview` · `GET /api/voice-preview/{preview_id}/audio` · `DELETE /api/voice-preview/cache`
- **Phase C (Segment Editor - Sửa từng câu sub/voice)**:
  - `GET /api/jobs/{job_id}/segments`
  - `GET /api/jobs/{job_id}/segments/{segment_id}/audio`
  - `POST /api/jobs/{job_id}/regenerate`
  - `POST /api/jobs/{job_id}/publish-revised`
- **Phase D (Crash Recovery - Khôi phục thông minh)**:
  - `GET /api/jobs/{job_id}/resume-plan`
  - `POST /api/jobs/{job_id}/resume`

---

## 5. PROMPT BÀN GIAO (COPY NGUYÊN KHỐI GỬI CHO TOOL / AGENT KHÁC)

```text
===BẮT ĐẦU PROMPT BÀN GIAO TOOL DASHBOARD===

VAI TRÒ: Bạn là Kỹ sư Full-Stack (Python FastAPI + Vanilla JS + PyTorch CUDA + FFmpeg NVENC) tiếp quản hệ thống "Tool V1 Dashboard & AutoDub Pipeline" trên máy Windows.

1. THÔNG TIN MÔI TRƯỜNG & ĐƯỜNG DẪN CỐT LÕI
- Thư mục gốc dự án: C:\tool v1 (Git branch: tool-v1)
- Python Venv: C:\tool v1\backend\venv\Scripts\python.exe (Python 3.10)
- Phần cứng: GPU NVIDIA GeForce RTX 4050 Laptop (6GB VRAM), hỗ trợ CUDA, NVENC/NVDEC.
- File biến môi trường: C:\tool v1\backend\.env (TUYỆT ĐỐI KHÔNG in hoặc làm lộ API key/token trong .env và douyin_cookies.txt).
- Thư mục dữ liệu thực tế:
  + Video đầu vào (input_dir): D:\workspace\downloads (hoặc D:\video phôi)
  + Video thành phẩm (output_dir): D:\banve
  + Workspace tạm (workspace): D:\workspace và C:\tool v1\workspace
  + Cache mô hình AI (model_cache): C:\tool v1\models
  + Dữ liệu cấu hình (data_root): C:\Users\admin\AppData\Local\AutoDub

2. CÁC ĐƯỜNG DẪN WEB (URLs) & CỔNG DỊCH VỤ
- Dashboard chính (Giám sát, Hàng đợi, Cấu hình, Sửa từng câu, Khôi phục): http://127.0.0.1:8088/
  -> Template: C:\tool v1\backend\templates\dashboard.html (~7.450 dòng)
  -> Backend: C:\tool v1\backend\main.py + phase_a/b/c/d_routes.py
- Studio Quy Trình (CapCut Video Editor & Anti-Reup): http://127.0.0.1:8088/quy-trinh
  -> Template: C:\tool v1\backend\templates\workflow.html + C:\tool v1\backend\static\js\timeline.js
  -> Backend: C:\tool v1\backend\workflow_api.py
- Studio Kịch Bản AI & Doodle Video: http://127.0.0.1:8088/kich-ban
  -> Template: C:\tool v1\backend\templates\script_studio.html
  -> Backend: C:\tool v1\backend\script_api.py
- A2UI Studio (Agent-to-User Interface): http://127.0.0.1:8088/a2ui
  -> Template: C:\tool v1\backend\templates\a2ui_studio.html
- Control Plane (Điều khiển bật/tắt dịch vụ & chọn giọng): http://127.0.0.1:8090/
  -> Backend: C:\tool v1\backend\tool_control.py (nhúng qua iframe trên Dashboard)

3. CƠ CHẾ VẬN HÀNH & KHỞI ĐỘNG LẠI SERVER
- Dịch vụ cổng 8088 (main.py) và Telegram Bot (telegram_bot.py) được giám sát tự động bởi Windows supervisor: C:\tool v1\backend\background_service.py.
- Chống khởi động trùng: `main.py` kiểm tra `/api/status` trước khi bind cổng — nếu đã có dashboard khỏe trên 8088 thì tự thoát mã 0; supervisor cũng bỏ qua spawn khi 8088 còn khỏe (tránh vòng lặp WinError 10048).
- Khi sửa code Python ở backend: **không kill/restart khi đang có batch hoặc GPU worker chạy**. Chỉ nạp lại server vào lúc rảnh; supervisor sẽ tự khởi động lại `main.py` sau ~5 giây, hoặc bật lại thủ công theo quy trình ở mục 3.1.
- Log vòng đời supervisor: `C:\tool v1\workspace\bot_system\service_logs\dashboard_supervisor.log` (mỗi dòng Started PID / Exited code).
- Kiểm tra cổng 8088: `netstat -ano | findstr :8088` hoặc `curl.exe -s http://127.0.0.1:8088/api/status`.
- Các request POST/DELETE nội bộ yêu cầu header bảo mật `X-Local-Control-Token` (lấy từ biến JS `CONTROL_PLANE_TOKEN` hoặc `GET /api/auth-token`) và `X-Dashboard-Input: 1`.

4. KIẾN TRÚC CHỨC NĂNG CỦA TOOL DASHBOARD (dashboard.html + main.py)
- Giám sát 2 luồng Worker (Worker 1 & Worker 2) với cơ chế Auto VRAM Guard (v1_vram_monitor.py + worker_settings.py).
- Pipeline 8 bước (v1_orchestrator.py):
  (1) Tải/Chuẩn bị video -> (2) Tách nhạc BS-RoFormer/Demucs -> (3) ASR SenseVoice+Whisper Hybrid -> (4) OCR tìm vùng sub Trung (PP-OCR) -> (5) Dịch ngữ cảnh bằng Gemini + Custom Glossary -> (6) Lồng tiếng TTS/RVC khớp thời lượng -> (7) Mix âm thanh Soft Ducking -> (8) Render 9:16 NVENC (che sub Trung, phủ sub Việt, chống re-up).
- 4 Phân hệ nâng cao trên Dashboard:
  + Phase A (phase_a_routes.py, preset_service.py, job_config_service.py): Quản lý Preset cấu hình, lưu cấu hình nháp riêng cho từng video phôi, xem bảng tóm tắt trước khi chạy batch.
  + Phase B (phase_b_routes.py, voice_preview_service.py): Nghe thử giọng đọc AI trực tiếp trên Dashboard.
  + Phase C (phase_c_routes.py, segment_editor_service.py): Mở video thành phẩm, sửa văn bản/đổi giọng/đổi tốc độ của từng câu phụ đề riêng lẻ, tái tạo audio câu đó và xuất lại video hoàn chỉnh mà không cần chạy lại từ đầu.
  + Phase D (phase_d_routes.py, recovery_service.py): Kiểm tra checkpoint trong workspace, khôi phục thông minh (Smart Resume) các video bị gián đoạn chỉ từ bước còn thiếu.

5. NGUYÊN TẮC BẮT BUỘC KHI CHỈNH SỬA / NÂNG CẤP
- Giữ `PIPELINE_MODE=legacy` (Tool V1). Không làm hỏng quy trình 8 bước, Telegram bot, hoặc các tính năng ở `/quy-trinh` và `/kich-ban`.
- Mọi filter FFmpeg xử lý khung hình 9:16/crop/blur phải ép `format=yuv420p` trước khi split/crop để tránh lỗi sọc xanh nửa phải của bộ giải mã NVDEC (`nv12`).
- Giao diện 100% tiếng Việt, giữ phong cách Dark Mode hiện tại.
- Chạy kiểm thử với `C:\tool v1\backend\venv\Scripts\python.exe -m pytest tests/` trước khi kết thúc công việc.

BẮT ĐẦU CÔNG VIỆC:
Hãy kiểm tra trạng thái git (`git status --short`, `git log -n 5 --oneline`) và kiểm tra phản hồi của `http://127.0.0.1:8088/api/status`, sau đó xác nhận bạn đã nắm rõ kiến trúc Tool Dashboard để nhận yêu cầu tiếp theo từ tôi.

===KẾT THÚC PROMPT BÀN GIAO TOOL DASHBOARD===
```
