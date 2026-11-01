# BÁO CÁO TRẠNG THÁI TOOL V1 (PROJECT STATE & HANDOFF)

*Cập nhật: 02/11/2026. Đã hoàn thành đợt cleanup an toàn Tool V1 (C0–C9), cắt chuyển Python 3.12.14, thu hẹp lệnh dừng theo scope và dọn dẹp mã nguồn lịch sử.*

---

## 1. TỔNG QUAN HỆ THỐNG

Tool V1 là hệ thống **duy nhất** đang vận hành. Tool V2 đã ngừng sử dụng và không còn thư mục `C:\tool v2`; mọi tài liệu/tính năng nhắc tới V2, RVC hay dashboard cổng 8089 chỉ là lịch sử.

| Thành phần | Cổng | File chính |
| --- | --- | --- |
| Dashboard + Workflow API | `http://127.0.0.1:8088` | `backend/main.py` |
| Tool Control (chọn giọng dùng chung) | `http://127.0.0.1:8090` | `backend/tool_control.py` |
| Telegram Bot + Background Service | — | `backend/telegram_bot.py`, `backend/background_service.py` |

Đường dẫn giao diện:

- Dashboard: `http://127.0.0.1:8088/`
- Studio Quy Trình: `http://127.0.0.1:8088/quy-trinh`
- Studio Kịch Bản: `http://127.0.0.1:8088/kich-ban`
- Kiểm tra sức khỏe: `GET /api/status`, `GET /api/preflight`, script CLI `scripts/verify_runtime_health.py`

Khởi động / dừng:

- Khởi động toàn bộ: `KhoiDongAn.vbs` (hoặc `start_bot.bat` để thấy console).
- Chỉ mở giao diện quan sát: `Giao_Dien_Quan_Sat_Tool.bat`.
- Dừng an toàn theo phạm vi (`backend/v1_shutdown.py`):
  - `TatHoanToan.bat`: Dừng toàn bộ hệ thống Tool V1 (`--scope all`), hỗ trợ `--preview` để kiểm tra trước và chặn dừng khi có job đang chạy (báo busy, cần `--force` nếu cố ý dừng).
  - `stop_bot.bat`: Chỉ dừng Telegram dubbing bot (`--scope telegram`), không tác động downloader độc lập hay tiến trình khác.
  - `stop_telebot_downloader.bat`: Chỉ dừng bot tải video độc lập (`--scope downloader`).

---

## 2. QUY TRÌNH 8 BƯỚC VÀ CƠ CHẾ FAIL-CLOSED

Pipeline legacy (`PIPELINE_MODE=legacy`) do `backend/v1_orchestrator.py` điều phối, có manifest/checkpoint để resume:

1. Tải & chuẩn bị video, trích WAV 44.1 kHz.
2. Tách giọng/nhạc bằng BS-RoFormer (GPU) hoặc Demucs dự phòng.
3. ASR Whisper Large-v3 Turbo (SenseVoice tùy chọn).
4. OCR PP-OCRv6 (Smart Skip + OCR Geometry auto-heal).
5. Dịch thuật Gemini đa key, có kiểm tra chất lượng theo lô.
6. TTS CapCut / Vie-TTS / Edge + Timing Solver (speed limit).
7. Hòa âm soft-ducking, chuẩn hóa EBU R128.
8. Render NVENC H.264, che phụ đề cũ, nhúng phụ đề mới, QC rồi mới giao video.

Điểm chặn lỗi:

- `V1_QC_POLICY=BLOCK` trong `backend/v1_feature_flags.py` và `backend/main.py`.
- Lỗi toàn vẹn nghiêm trọng **luôn** ném `CriticalQualityError` trước khi giao video, kể cả khi chọn `WARN` hoặc `REPORT_ONLY`.
- Stage 3 và Stage 4 gọi OCR với `strict_geometry=True`; phụ đề phát hiện được nhưng không xác định được vùng che sẽ dừng job (`OCRGeometryError`) thay vì đoán.
- Stage 5 gọi `translate_subtitles(strict=True)`; chỉ cho phép fallback khi đặt `V1_ALLOW_TRANSLATION_FALLBACK=true`.
- Video không có phụ đề gốc vẫn hợp lệ và đi tiếp bình thường.

---

## 3. MÔI TRƯỜNG RUNTIME

| Môi trường | Mục đích | Phiên bản / Ghi chú |
| --- | --- | --- |
| `backend/venv` | Ứng dụng chính (FastAPI, bot, orchestrator) | **Python 3.12.14** (runtime sản xuất chính thức) |
| `backend/venv_py310` | Môi trường dự phòng rollback | **Python 3.10.11** (sẵn sàng chuyển đổi qua `scripts/rollback_py310.bat`) |
| `backend/model_venv` | PP-OCRv6 cách ly | `paddleocr==3.7.0`, `onnxruntime-gpu==1.26.0` |
| `backend/model_venv_separator` | BS-RoFormer tách âm | Model nặng, không đẩy lên Git |

Quy tắc dependency:

- `requirements.txt` (root) **không** chứa `paddleocr`; gói này chỉ cài trong runtime cách ly.
- Pin OCR nằm ở `backend/requirements-v1-models.txt` và `backend/requirements-models.txt`.
- Cài runtime OCR: `powershell -File backend/setup_v1_models.ps1` (thêm `-Preload` để tải sẵn model).

---

## 4. DANH MỤC GIỌNG ĐỌC

Nguồn sự thật: `backend/voice_selection.py` (`DEFAULT_CATALOG`), lưu lựa chọn tại `workspace/bot_system/control/voice_selection.json`.

- **12 giọng CapCut TTS** — mặc định `capcut-BV562_streaming` (Mai, nữ chuẩn); có `capcut-BV075_streaming` (nam trẻ) và các giọng nam/nữ khác.
- **14 giọng Vie-TTS (48 kHz)** — Hải Đăng, Mai Anh, Trúc Ly, Thiện Minh, Thùy Dung, Adam Bựa, Thái Sơn, Phạm Tuyên, Ngọc Huyền, Ngọc Trân, Quang Sơn, Mỹ Duyên, Quỳnh Anh, Thanh Bình.
- **2 giọng Microsoft Edge TTS** — Hoài My, Nam Minh (dự phòng).

RVC và giọng `chi-mai` đã bị gỡ; mã cũ được tự động ánh xạ sang CapCut Mai. Chọn và nghe thử giọng tại Tool Control 8090.

---

## 5. KIỂM THỬ VÀ ĐẢM BẢO CHẤT LƯỢNG

Hệ thống kiểm thử bao phủ toàn diện:

- **Bộ kiểm thử đầy đủ (Full Suite):**
  - Python 3.12.14: **1217 passed, 0 failed, 79 skipped**.
  - Python 3.10.11: **1217 passed, 0 failed, 79 skipped** (261 subtests passed).
  - Node tests: **11 passed, 0 failed** (`node --test tests/js/*.test.js`).
  - AST Audits & Golden Cases: 100% PASS (72/72 ASS visual matches).
- **Bộ kiểm thử tất định trong CI (`.github/workflows/python-quality.yml`):**
  - Chạy ma trận song song Python 3.12 (chính) và Python 3.10 (dự phòng) qua 17 file kiểm thử tất định (174 tests).
  - Job Node 20 kiểm thử giao diện & subtitle renderer.
- **Kiểm tra sức khỏe runtime chỉ đọc:**
  - `python scripts/verify_runtime_health.py --timeout 30` xác minh 25/25 tiêu chí preflight, pip dependencies và kết nối dịch vụ.
- **Đóng gói phát hành:**
  - `package_v1_release.py --dry-run` và `scripts/generate_release_manifest.py --dry-run` đồng bộ 561 file chuẩn.

---

## 6. KẾT QUẢ ĐỢT DỌN DẸP AN TOÀN TOOL V1 (02/11/2026 — C0 ĐẾN C9)

Đợt dọn dẹp mã nguồn thừa và gia cố an toàn thực hiện theo kế hoạch chi tiết (`CLEANUP_PLAN.md`), xuất phát từ tag neo `v1-pre-cleanup-20261101` (`d8cff9241336ff7ce7b7b1efb0ec02f4008efa6f`), với các kết quả cụ thể:

1. **C1 (`F01`) — Thu hẹp phạm vi dừng dịch vụ (`backend/v1_shutdown.py`):**
   - Xác minh chính xác project root, script path và creation time; loại bỏ việc tin PID telemetry cũ hoặc kill theo tên `python.exe`/`ffmpeg.exe` toàn máy.
   - Hỗ trợ `--preview` không gây tác động; mặc định chặn dừng nếu phát hiện job đang chạy; dừng đúng Scheduled Task và khôi phục khi cần.
2. **C2 (`F02`, `F03`) — Vô hiệu hóa tại chỗ 9 công cụ lịch sử có side effect:**
   - Thay thế các script import queue và rollback cũ bằng stub an toàn thoát mã lỗi `1`, không có side effect khi import (`backend/enqueue_missing_batch.py`, `backend/sync_capcut_queue.py`, các file `.bat`/`.ps1` rollback cũ).
3. **C3 (`F08`) — Loại bỏ mã chết OCR & trạng thái dịch thuật lỗi thời:**
   - Xóa nhánh EasyOCR không thể chạm tới và `_best_cluster_samples` trong `backend/ocr_utils.py`.
   - Xóa `_gemini_transient_failure` và các biến thừa không đọc trong `backend/ai/translation.py`.
   - Giữ nguyên 3 module HOLD theo hợp đồng hệ thống (`v1_free_gpt.py`, `v1_telegram_errors.py`, `qc_service.py`).
4. **C6 (`F09`) — Đưa test thật vào pytest collection:**
   - Chuyển 8 test từ `backend/test_recovery_service.py` vào `tests/test_recovery_service.py` với cô lập hoàn toàn môi trường, biến file cũ thành shim mỏng.
5. **C5 (`F04`) — Hợp nhất Whiteboard Animator:**
   - Quy hoạch `backend/whiteboard_animator/` làm bản chuẩn duy nhất (giữ NVENC, extra frames, timing fit). Chuyển 11 file lồng nhau trong `backend/whiteboard_animator/whiteboard_animator/` thành shim re-export mỏng.
6. **C7 (`F07`) — Thống nhất đóng gói phát hành & Manifest:**
   - Đồng bộ danh sách 561 file giữa `package_v1_release.py` và `scripts/generate_release_manifest.py`, loại bỏ các script debug lịch sử và test thủ công phụ thuộc môi trường máy chủ.
7. **C8 (`F10`) — Cập nhật CI Python 3.12 & tài liệu hóa:**
   - Bổ sung matrix Python 3.12 + 3.10 và job Node vào `.github/workflows/python-quality.yml`.
8. **C4 (`F05`, `F06`) — Xóa vật lý 69 file debug & test thủ công lịch sử:**
   - Thực thi `git rm` sau khi người dùng phê duyệt manifest (65 file debug/crawler lịch sử trong `backend/scripts_and_tests/*.py`, `_kinetic_v3ref.py`, script one-off + 4 test thủ công C6), giảm 6.321 dòng mã không dùng.

---

## 7. NGUYÊN TẮC VẬN HÀNH AN TOÀN

- Không kill/khởi động lại dashboard, bot hoặc job đang chạy nếu chưa được phép rõ ràng. Khi cần dừng, luôn ưu tiên chạy lệnh có `--preview` để kiểm tra tiến trình mục tiêu.
- Không đọc/in `.env`, `douyin_cookies.txt`, token hay API key.
- Không tự xóa/ghi đè file stop, queue hoặc dữ liệu thật trong `workspace/`.
- Giữ `PIPELINE_MODE=legacy`; giữ nguyên 8 bước, Telegram bot, `/quy-trinh`, `/kich-ban`.
- FFmpeg crop/blur phải ép `format=yuv420p`.
- Giao diện 100% tiếng Việt, dark mode.
- Visual sub-to-speech là tùy chọn, mặc định tắt (`V1_VISUAL_SUB_TO_SPEECH=false`).
